"""gugu 止损告警升级机制。

P0-7 修复：track_live 模式下止损触发后，如果用户未及时确认，
系统按配置的时间间隔逐级升级告警（普通 → 紧急 → 严重）。
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
    """单个持仓的止损告警状态。"""

    symbol: str
    first_alert_ts: float = 0.0  # 首次告警时间戳
    last_alert_ts: float = 0.0  # 最近一次告警时间戳
    escalation_level: int = 0  # 当前升级级别（0=首次, 1=紧急, 2=严重）
    acknowledged: bool = False  # 用户是否已确认

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "first_alert_ts": self.first_alert_ts,
            "last_alert_ts": self.last_alert_ts,
            "escalation_level": self.escalation_level,
            "acknowledged": self.acknowledged,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StopLossAlertState:
        return cls(
            symbol=data.get("symbol", ""),
            first_alert_ts=data.get("first_alert_ts", 0.0),
            last_alert_ts=data.get("last_alert_ts", 0.0),
            escalation_level=data.get("escalation_level", 0),
            acknowledged=data.get("acknowledged", False),
        )


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
        """记录止损触发（首次告警）。"""
        now = time.time()
        if symbol not in self._alerts:
            self._alerts[symbol] = StopLossAlertState(
                symbol=symbol,
                first_alert_ts=now,
                last_alert_ts=now,
                escalation_level=0,
            )
        else:
            alert = self._alerts[symbol]
            alert.last_alert_ts = now
            alert.escalation_level = 0
            alert.acknowledged = False
        self._save_state()
        return self._alerts[symbol]

    def check_escalation(self, symbol: str) -> tuple[bool, int, str]:
        """检查是否需要升级告警。

        Returns:
            (needs_alert, level, urgency_label)
            - needs_alert: 是否需要发送告警
            - level: 当前级别（0=首次, 1=紧急, 2=严重）
            - urgency_label: 紧急程度标签
        """
        alert = self._alerts.get(symbol)
        if not alert or alert.acknowledged:
            return False, 0, ""

        now = time.time()
        elapsed_min = (now - alert.first_alert_ts) / 60

        # 检查是否达到下一个升级级别
        next_level = alert.escalation_level + 1
        if next_level > self._max_escalation:
            return False, alert.escalation_level, ""

        # 检查是否达到升级间隔
        if next_level <= len(self._intervals):
            threshold_min = sum(self._intervals[:next_level])
        else:
            threshold_min = self._intervals[-1] * (next_level - len(self._intervals) + 1)

        if elapsed_min >= threshold_min:
            alert.escalation_level = next_level
            alert.last_alert_ts = now
            self._save_state()
            labels = {1: "⚠️ 紧急", 2: "🔴 严重", 3: "🚨 极严重"}
            return True, next_level, labels.get(next_level, f"Level {next_level}")

        return False, alert.escalation_level, ""

    def acknowledge(self, symbol: str) -> None:
        """用户确认告警。"""
        if symbol in self._alerts:
            self._alerts[symbol].acknowledged = True
            self._save_state()

    def clear(self, symbol: str) -> None:
        """清除告警状态（止损已执行或持仓已平仓）。"""
        if symbol in self._alerts:
            del self._alerts[symbol]
            self._save_state()

    def get_pending_alerts(self) -> list[StopLossAlertState]:
        """获取所有待确认的告警。"""
        return [a for a in self._alerts.values() if not a.acknowledged]
