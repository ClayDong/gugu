"""财叔动态止盈减仓策略引擎。

出处：财叔实战经验——"立于不败之地，再让利润奔跑"。

策略逻辑（四步）：
1. **首减（+30%）**：盈利达 30% 时减仓 1/3，锁定利润，基本立于不败之地。
   - 平庸之辈：此时开始调整，减是对的；
   - 妖股：现在减了后面心态更稳拿得更久，减也是对的。
   总之，减仓总是对的。
2. **阶梯递减（每 +15% 减 10%）**：首减之后，每上涨 15% 再减仓 10%，
   持续锁利，直到趋势结束。
3. **大跌减半**：单日大跌（跌幅 ≥ 大跌阈值）直接减半仓，风控保命，
   之后随意。

状态持久化：减仓档位记录写入 ``data/take_profit_state.json``，
引擎重启后不丢失、不重复触发。

与现有止损体系的关系：
- ``TrailingStopEngine``（浪谷递进移动止损）负责**止损**——下跌时保护本金；
- 本模块负责**止盈**——上涨时分批锁利，不让利润回吐。
两者互补，共同构成"截断亏损，让利润奔跑"的完整闭环。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from gugu.config import PROJECT_ROOT
from gugu.utils.log import get_logger

logger = get_logger()

# ========== 策略参数（财叔实战经验值）==========
# 第1步：首次止盈触发点（+30%）
FIRST_TAKE_PROFIT_THRESHOLD = 0.30
# 第1步：首次减仓比例（1/3）
FIRST_REDUCE_RATIO = 1 / 3
# 第3步：阶梯止盈步长（每 +15%）
STEP_THRESHOLD = 0.15
# 第3步：阶梯减仓比例（10%）
STEP_REDUCE_RATIO = 0.10
# 第4步：大跌阈值（单日跌幅 ≥ 此值触发减半）
BIG_DROP_THRESHOLD = -0.05  # -5%
# 第4步：大跌减仓比例（1/2）
BIG_DROP_REDUCE_RATIO = 0.50

# 状态持久化路径
_STATE_PATH: Path = PROJECT_ROOT / "data" / "take_profit_state.json"


@dataclass
class TakeProfitState:
    """单只持仓的止盈减仓状态。

    Attributes:
        symbol: 股票代码。
        entry_price: 买入均价（用于计算涨幅）。
        last_reduce_ratio: 上次减仓时的累计涨幅档位（如 0.30、0.45、0.60...）。
            0 表示尚未触发首次减仓。
        last_big_drop_date: 上次触发大跌减半的日期（YYYY-MM-DD），防同日重复。
        first_reduced: 是否已执行首次减仓（+30% 减 1/3）。
        total_reduced: 累计减仓数量（用于通知展示）。
        history: 减仓历史记录列表（每次减仓追加一条）。
    """

    symbol: str
    entry_price: float
    last_reduce_ratio: float = 0.0
    last_big_drop_date: str = ""
    first_reduced: bool = False
    total_reduced: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TakeProfitSignal:
    """止盈减仓信号。

    Attributes:
        action: "reduce"（减仓）或 "hold"（持有不动）。
        reason: 触发原因（人类可读）。
        reduce_ratio: 建议减仓比例（相对于当前持仓，如 0.333=1/3）。
        trigger_step: 触发的策略步骤（1=首减, 3=阶梯, 4=大跌）。
        current_profit_pct: 当前涨幅（小数）。
    """

    action: str  # "reduce" | "hold"
    reason: str
    reduce_ratio: float = 0.0
    trigger_step: int = 0
    current_profit_pct: float = 0.0


class DynamicTakeProfitEngine:
    """财叔动态止盈减仓引擎。

    用法：
        engine = DynamicTakeProfitEngine()
        # 买入时注册
        engine.register(symbol, entry_price)
        # 每次扫描时检查
        signal = engine.check(symbol, current_price, prev_close, today_str)
        if signal.action == "reduce":
            # 执行减仓 + 通知
            engine.mark_reduced(symbol, executed_qty, current_price, signal)

    状态持久化：每次状态变更后自动写入 JSON 文件，引擎重启后加载。
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path = state_path or _STATE_PATH
        self._states: dict[str, TakeProfitState] = {}
        self._load()

    # ========== 状态持久化 ==========

    def _load(self) -> None:
        """从磁盘加载止盈状态（引擎重启后恢复）。"""
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            for symbol, s in data.items():
                self._states[symbol] = TakeProfitState(**s)
            logger.info(f"[take_profit] 加载 {len(self._states)} 个持仓止盈状态")
        except Exception as e:
            logger.warning(f"[take_profit] 状态加载失败: {e}")

    def _save(self) -> None:
        """持久化止盈状态到磁盘。"""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {sym: asdict(s) for sym, s in self._states.items()}
            # P1-5 修复：原 write_text 在写入中途崩溃会导致状态文件截断
            # 改用 atomic_write_text（同目录 tmp → fsync → os.replace 原子替换）
            from gugu.utils.atomic import atomic_write_text
            atomic_write_text(
                self._state_path,
                json.dumps(data, ensure_ascii=False, indent=2),
            )
        except Exception as e:
            logger.error(f"[take_profit] 状态持久化失败: {e}")

    # ========== 生命周期 ==========

    def register(self, symbol: str, entry_price: float) -> None:
        """买入时注册持仓（初始化止盈状态）。

        Args:
            symbol: 股票代码。
            entry_price: 买入均价。
        """
        if entry_price <= 0:
            logger.warning(f"[take_profit] {symbol} 入场价无效: {entry_price}，跳过注册")
            return
        self._states[symbol] = TakeProfitState(
            symbol=symbol,
            entry_price=entry_price,
        )
        self._save()
        logger.info(f"[take_profit] {symbol} 注册止盈监控，入场价 {entry_price:.2f}")

    def unregister(self, symbol: str) -> None:
        """清仓时移除止盈状态。"""
        if symbol in self._states:
            del self._states[symbol]
            self._save()
            logger.info(f"[take_profit] {symbol} 清仓，移除止盈状态")

    # ========== 核心检查逻辑 ==========

    def check(
        self,
        symbol: str,
        current_price: float,
        prev_close: float,
        today_str: str | None = None,
    ) -> TakeProfitSignal:
        """检查是否触发止盈减仓。

        按"财叔四步"顺序判断：
        1. 首减（+30% 减 1/3）
        2. 阶梯递减（每 +15% 减 10%）
        3. 大跌减半（单日大跌 ≥ 5% 减半）

        注意：大跌检查优先于阶梯检查（保命优先于锁利）。

        Args:
            symbol: 股票代码。
            current_price: 当前价。
            prev_close: 昨收价（用于计算单日跌幅）。
            today_str: 今日日期字符串（YYYY-MM-DD），用于防大跌同日重复触发。

        Returns:
            TakeProfitSignal，action="reduce" 时需执行减仓并通知。
        """
        state = self._states.get(symbol)
        if state is None or state.entry_price <= 0:
            return TakeProfitSignal(action="hold", reason="未注册止盈监控")

        if current_price <= 0:
            return TakeProfitSignal(action="hold", reason="当前价无效")

        today = today_str or date.today().isoformat()
        profit_pct = (current_price - state.entry_price) / state.entry_price

        # ===== 第4步：大跌减半（最高优先级，保命优先）=====
        # 单日跌幅 = (current - prev_close) / prev_close
        if prev_close > 0:
            daily_change = (current_price - prev_close) / prev_close
            if daily_change <= BIG_DROP_THRESHOLD:
                # 防同日重复触发
                if state.last_big_drop_date != today:
                    reduce_ratio = BIG_DROP_REDUCE_RATIO
                    # 若尚未首减，大跌时直接减半（比 1/3 更狠）
                    if not state.first_reduced:
                        reduce_ratio = max(reduce_ratio, FIRST_REDUCE_RATIO)
                    return TakeProfitSignal(
                        action="reduce",
                        reason=(
                            f"单日大跌 {daily_change:.1%}（≤{BIG_DROP_THRESHOLD:.0%}），"
                            f"触发第4步减半保命"
                        ),
                        reduce_ratio=reduce_ratio,
                        trigger_step=4,
                        current_profit_pct=profit_pct,
                    )

        # ===== 第1步：首次止盈（+30% 减 1/3）=====
        if not state.first_reduced and profit_pct >= FIRST_TAKE_PROFIT_THRESHOLD:
            return TakeProfitSignal(
                action="reduce",
                reason=(
                    f"盈利 {profit_pct:.1%} ≥ {FIRST_TAKE_PROFIT_THRESHOLD:.0%}，"
                    f"触发第1步首减 1/3（立于不败之地）"
                ),
                reduce_ratio=FIRST_REDUCE_RATIO,
                trigger_step=1,
                current_profit_pct=profit_pct,
            )

        # ===== 第3步：阶梯递减（每 +15% 减 10%）=====
        if state.first_reduced:
            # 计算下次应触发的涨幅档位
            # 首减后 last_reduce_ratio=0.30，下次 0.45，再下次 0.60...
            next_threshold = state.last_reduce_ratio + STEP_THRESHOLD
            if profit_pct >= next_threshold:
                return TakeProfitSignal(
                    action="reduce",
                    reason=(
                        f"盈利 {profit_pct:.1%} ≥ 阶梯档位 {next_threshold:.0%}"
                        f"（+{STEP_THRESHOLD:.0%}），触发第3步减仓 10%"
                    ),
                    reduce_ratio=STEP_REDUCE_RATIO,
                    trigger_step=3,
                    current_profit_pct=profit_pct,
                )

        return TakeProfitSignal(
            action="hold",
            reason=f"盈利 {profit_pct:.1%}，未达止盈阈值",
            current_profit_pct=profit_pct,
        )

    # ========== 减仓后状态更新 ==========

    def mark_reduced(
        self,
        symbol: str,
        executed_qty: int,
        price: float,
        signal: TakeProfitSignal,
        today_str: str | None = None,
    ) -> None:
        """减仓执行后更新止盈状态。

        Args:
            symbol: 股票代码。
            executed_qty: 实际减仓数量。
            price: 减仓价。
            signal: 触发的 TakeProfitSignal。
            today_str: 今日日期字符串。
        """
        state = self._states.get(symbol)
        if state is None:
            logger.warning(f"[take_profit] {symbol} 未注册，无法更新状态")
            return

        today = today_str or date.today().isoformat()
        state.total_reduced += executed_qty

        if signal.trigger_step == 4:
            # 大跌减半
            state.last_big_drop_date = today
        elif signal.trigger_step == 1:
            # 首减
            state.first_reduced = True
            state.last_reduce_ratio = FIRST_TAKE_PROFIT_THRESHOLD
        elif signal.trigger_step == 3:
            # 阶梯递减
            state.last_reduce_ratio += STEP_THRESHOLD

        state.history.append(
            {
                "date": today,
                "step": signal.trigger_step,
                "price": round(price, 2),
                "qty": executed_qty,
                "profit_pct": round(signal.current_profit_pct, 4),
                "reason": signal.reason,
            }
        )
        self._save()
        logger.info(
            f"[take_profit] {symbol} 第{signal.trigger_step}步减仓 {executed_qty}股"
            f"@{price:.2f}（累计涨幅 {signal.current_profit_pct:.1%}）"
        )

    def get_state(self, symbol: str) -> TakeProfitState | None:
        """获取某只股票的止盈状态（供通知展示用）。"""
        return self._states.get(symbol)

    def list_active(self) -> list[str]:
        """列出所有正在监控止盈的股票代码。"""
        return list(self._states.keys())
