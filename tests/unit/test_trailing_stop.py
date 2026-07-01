"""移动止损管理器 TrailingStopManager 单元测试。"""
from __future__ import annotations

from gugu.risk.trailing_stop import TrailingStopManager


class TestTrailingStopInit:
    """初始化止损测试。"""

    def test_init_position_sets_stop_price(self):
        """初始化止损应设置合理的止损价。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        state = mgr.get_state("600519")
        assert state is not None
        # ATR*2=10 → atr_stop=90, 8%=8 → pct_stop=92, 取较小值 90
        assert state["stop_price"] == 90.0  # min(100-10, 100*0.92)
        assert state["entry_price"] == 100.0
        assert state["highest_price"] == 100.0

    def test_init_position_no_atr(self):
        """ATR=0 时使用固定 8% 止损。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=0.0)
        state = mgr.get_state("600519")
        assert state["stop_price"] == 92.0  # 100 * (1 - 0.08)


class TestTrailingStopUpdate:
    """移动止损更新测试。"""

    def test_update_raises_stop_when_price_drops(self):
        """价格跌破止损价时触发止损。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        result = mgr.update(
            "600519", current_price=90.0, current_high=95.0,
            current_date="2026-07-02", atr=5.0
        )
        assert result["should_stop"] is True
        assert "触发止损" in result["reason"]

    def test_update_trails_stop_upward(self):
        """价格上涨时止损线上移。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        # 价格上涨
        result = mgr.update(
            "600519", current_price=110.0, current_high=112.0,
            current_date="2026-07-02", atr=5.0
        )
        assert result["should_stop"] is False
        # 止损线应上移：112 - 5*2 = 102
        assert result["stop_price"] >= 100.0

    def test_update_protects_profit_after_20pct_gain(self):
        """浮盈超 20% 后保本止损。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        # 价格涨 25%
        result = mgr.update(
            "600519", current_price=125.0, current_high=126.0,
            current_date="2026-07-02", atr=5.0
        )
        assert result["should_stop"] is False
        # 止损不低于入场价
        assert result["stop_price"] >= 100.0

    def test_update_does_not_lower_stop(self):
        """价格下跌但未触止损时，止损线不下移。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        # 先上涨
        mgr.update("600519", current_price=110.0, current_high=112.0,
                   current_date="2026-07-02", atr=5.0)
        state_after_up = mgr.get_state("600519")
        stop_after_up = state_after_up["stop_price"]
        # 再下跌但未触止损
        mgr.update("600519", current_price=105.0, current_high=106.0,
                   current_date="2026-07-03", atr=5.0)
        state_after_down = mgr.get_state("600519")
        # 止损线不应下移
        assert state_after_down["stop_price"] >= stop_after_up


class TestTimeStop:
    """5日时间止损测试。"""

    def test_time_stop_triggers_after_5_days_no_profit(self):
        """持有5日无盈利触发时间止损。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        # 6天后，止损价仍低于入场价
        result = mgr.check_time_stop("600519", "2026-07-07")
        assert result["should_stop"] is True
        assert "5日时间止损" in result["reason"]

    def test_time_stop_not_triggered_within_5_days(self):
        """5日内不触发时间止损。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        result = mgr.check_time_stop("600519", "2026-07-03")
        assert result["should_stop"] is False

    def test_time_stop_not_triggered_with_profit(self):
        """有盈利时止损线上移至入场价以上，不触发时间止损。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        # 价格上涨，止损线上移至入场价以上
        mgr.update("600519", current_price=125.0, current_high=126.0,
                   current_date="2026-07-02", atr=5.0)
        result = mgr.check_time_stop("600519", "2026-07-07")
        assert result["should_stop"] is False


class TestRemoveAndState:
    """清除和状态查询测试。"""

    def test_remove_clears_position(self):
        """remove 后不再跟踪该股票。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        mgr.remove("600519")
        assert mgr.get_state("600519") is None

    def test_get_all_states(self):
        """get_all_states 返回所有持仓止损状态。"""
        mgr = TrailingStopManager()
        mgr.init_position("600519", entry_price=100.0, entry_date="2026-07-01", atr=5.0)
        mgr.init_position("000858", entry_price=150.0, entry_date="2026-07-01", atr=8.0)
        states = mgr.get_all_states()
        assert len(states) == 2
        assert "600519" in states
        assert "000858" in states
