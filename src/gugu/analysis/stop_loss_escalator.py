"""gugu 止损告警升级机制。

P0-7 修复：track_live 模式下止损触发后，如果用户未及时确认，
系统按配置的时间间隔逐级升级告警（普通 → 紧急 → 严重）。

PR1 根治（2026-08-14 审计实证）：
- 旧缺陷：record_stop_loss_triggered() 每次破位检测都把 escalation_level 重置为 0，
  而 check_escalation() 基于 first_alert_ts 立即升级 → 每轮扫描"重置→升级"振荡，
  升级卡 40+ 次尝试全被去重拦截或永远到不了最高级。
- 修复：
  1. 升级级别只进不退（重复触发不重置），ack 终止、clear 移除、价格恢复清除
  2. check_escalation() 只报告"是否需要升级"，不推进状态；
     发送成功（confirm_escalation_sent）后才推进级别与时间戳 → 未送达不升级
  3. 升级节奏基于 last_escalation_ts（上次确认发送时间），平滑推进不振荡
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class StopLossAlertState:
    """单个持仓的止损告警状态。

    状态机：initial(0) → urgent(1) → severe(2) → critical(3)
    - 级别只进不退（acknowledge 终止 / clear 移除 / 价格恢复 clear）
    - last_escalation_ts：最近一次"升级卡确认送达"时间，升级节奏以此为基准
    """

    symbol: str
    first_alert_ts: float = 0.0  # 首次告警时间戳
    last_alert_ts: float = 0.0  # 最近一次止损触发时间戳
    escalation_level: int = 0  # 当前升级级别（0=首次, 1=紧急, 2=严重, 3=极严重）
    acknowledged: bool = False  # 用户是否已确认
    last_escalation_ts: float = 0.0  # 最近一次升级确认送达时间（0=兼容旧文件，视同 first_alert_ts）

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "first_alert_ts": self.first_alert_ts,
            "last_alert_ts": self.last_alert_ts,
            "escalation_level": self.escalation_level,
            "acknowledged": self.acknowledged,
            "last_escalation_ts": self.last_escalation_ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StopLossAlertState:
        inst = cls(
            symbol=data.get("symbol", ""),
            first_alert_ts=data.get("first_alert_ts", 0.0),
            last_alert_ts=data.get("last_alert_ts", 0.0),
            escalation_level=data.get("escalation_level", 0),
            acknowledged=data.get("acknowledged", False),
        )
        # 旧状态文件无 last_escalation_ts：兼容为 first_alert_ts（节奏从首次告警起算）
        last_es = data.get("last_escalation_ts", 0.0) or 0.0
        inst.last_escalation_ts = last_es if last_es > 0 else inst.first_alert_ts
        return inst


class StopLossAlertEscalator:
    """止损告警升级器。

    配置（从 settings.yaml 的 execution.track_live 读取）：
    - escalation_enabled: 是否启用告警升级
    - escalation_intervals: 升级间隔（分钟），如 [30, 60]
    - max_escalation: 最大升级次数
    """

    def __init__(
        self,
        state_file: str | Path = "data/stop_loss_alerts.json",
        escalation_intervals: list[int] | None = None,
        max_escalation: int = 3,
    ) -> None:
        self._state_file = Path(state_file)
        self._intervals = escalation_intervals or [30, 60]  # 默认 30min, 60min
        self._max_escalation = max_escalation
        self._alerts: dict[str, StopLossAlertState] = {}
        self._load_state()

    def _load_state(self) -> None:
        """从文件加载告警状态。"""
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            for symbol, alert_data in data.items():
                self._alerts[symbol] = StopLossAlertState.from_dict(alert_data)
        except Exception as e:
            logger.warning(f"加载止损告警状态失败: {e}")

    def _save_state(self) -> None:
        """持久化告警状态到文件。"""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {s: a.to_dict() for s, a in self._alerts.items()}
            tmp = self._state_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.rename(self._state_file)
        except Exception as e:
            logger.error("保存止损告警状态失败: {}", e)

    def record_stop_loss_triggered(self, symbol: str) -> StopLossAlertState:
        """记录止损触发（首次告警 / 破位持续中的再次触发）。

        PR1：旧实现每次触发都把 escalation_level 重置为 0（振荡根因）。
        现在：新 symbol → 建 initial 状态；已有 → 只更新 last_alert_ts，级别只进不退。
        """
        now = time.time()
        if symbol not in self._alerts:
            self._alerts[symbol] = StopLossAlertState(
                symbol=symbol,
                first_alert_ts=now,
                last_alert_ts=now,
                escalation_level=0,
                last_escalation_ts=now,
            )
        else:
            alert = self._alerts[symbol]
            alert.last_alert_ts = now
            if alert.last_escalation_ts <= 0:
                alert.last_escalation_ts = alert.first_alert_ts
        self._save_state()
        return self._alerts[symbol]

    def check_escalation(self, symbol: str) -> tuple[bool, int, str]:
        """检查是否需要发送升级告警（只报告，不推进状态）。

        Returns:
            (needs_alert, next_level, urgency_label)
            - needs_alert: 距离上次确认送达已超过升级间隔且未达上限
            - next_level: 本次应升级到的级别（alert.escalation_level + 1）
            - urgency_label: 紧急程度标签

        PR1：级别推进移到 confirm_escalation_sent（发送成功才落地），
        升级节奏以 last_escalation_ts 为基准 → 每轮扫描不再重复狂升。
        """
        alert = self._alerts.get(symbol)
        if not alert or alert.acknowledged:
            return False, 0, ""

        now = time.time()
        base = alert.last_escalation_ts or alert.first_alert_ts
        elapsed_min = (now - base) / 60

        next_level = alert.escalation_level + 1
        if next_level > self._max_escalation:
            return False, alert.escalation_level, ""

        if next_level <= len(self._intervals):
            threshold_min = self._intervals[next_level - 1]  # 单次间隔（PR1 修正：非累计和）
        else:
            threshold_min = self._intervals[-1]  # 最后一个间隔重复

        if elapsed_min >= threshold_min:
            labels = {1: "⚠️ 紧急", 2: "🔴 严重", 3: "🚨 极严重"}
            return True, next_level, labels.get(next_level, f"Level {next_level}")

        return False, alert.escalation_level, ""

    def confirm_escalation_sent(self, symbol: str, level: int) -> None:
        """升级卡确认送达后才推进级别与时间戳（PR1：未送达不升级）。

        调用方（engine_mixin._check_stop_loss_escalation）在发送返回 True 后调用。
        """
        alert = self._alerts.get(symbol)
        if not alert:
            return
        now = time.time()
        if level > alert.escalation_level:
            alert.escalation_level = level
        alert.last_escalation_ts = now
        alert.last_alert_ts = now
        self._save_state()

    def acknowledge(self, symbol: str) -> None:
        """用户确认告警。"""
        if symbol in self._alerts:
            self._alerts[symbol].acknowledged = True
            self._save_state()

    def clear(self, symbol: str) -> None:
        """清除告警状态（止损已执行、持仓已平仓或价格恢复）。"""
        if symbol in self._alerts:
            del self._alerts[symbol]
            self._save_state()

    def get_pending_alerts(self) -> list[StopLossAlertState]:
        """获取所有待确认的告警。"""
        return [a for a in self._alerts.values() if not a.acknowledged]
