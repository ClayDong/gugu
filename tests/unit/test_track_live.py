"""track_live 模式单元测试。

验证 LivePositionTracker 的核心安全机制：
1. order() 永远拒绝下单（保护用户真实持仓）
2. update_price() 仅更新现价（不持久化，避免高频IO）
3. update_stop_loss() 持久化止损价（移动止损触发时调用）
4. get_account() / get_portfolio() 正确读取持仓
5. settle_t_plus_1() / reset_daily_start_value() 正常工作
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gugu.execution.track_live import LivePositionTracker


@pytest.fixture
def live_state_file(tmp_path: Path) -> Path:
    """创建临时 live_positions.json 测试文件。"""
    state = {
        "mode": "track_live",
        "initial_capital": 1000000.0,
        "cash": 991879.89,
        "positions": {
            "002714": {
                "symbol": "002714",
                "name": "牧原股份",
                "quantity": 100,
                "available": 100,
                "avg_cost": 36.69,
                "current_price": 37.93,
                "stop_loss_price": 31.89,
                "lowest_price": 36.69,
                "prev_close": 0.0,
                "is_st": False,
                "is_suspended": False,
            },
            "510500": {
                "symbol": "510500",
                "name": "中证500ETF",
                "quantity": 400,
                "available": 400,
                "avg_cost": 8.86,
                "current_price": 8.90,
                "stop_loss_price": 8.15,
                "lowest_price": 8.86,
                "prev_close": 0.0,
                "is_st": False,
                "is_suspended": False,
            },
        },
        "daily_start_value": 1006138.79,
        "updated_at": "2026-07-16T01:01:56",
    }
    state_file = tmp_path / "live_positions.json"
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state_file


@pytest.fixture
def empty_state_file(tmp_path: Path) -> Path:
    """创建空的 live_positions.json 测试文件。"""
    state = {
        "mode": "track_live",
        "initial_capital": 1000000.0,
        "cash": 1000000.0,
        "positions": {},
        "daily_start_value": None,
    }
    state_file = tmp_path / "live_positions_empty.json"
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state_file


class TestLivePositionTrackerLoading:
    """测试持仓文件加载。"""

    def test_load_positions_from_file(self, live_state_file: Path) -> None:
        """正常加载持仓文件。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        portfolio = tracker.get_portfolio()

        assert len(portfolio) == 2
        assert "002714" in portfolio
        assert "510500" in portfolio

        pos = tracker.get_position("002714")
        assert pos is not None
        assert pos.symbol == "002714"
        assert pos.quantity == 100
        assert pos.avg_cost == 36.69
        assert pos.current_price == 37.93
        assert pos.stop_loss_price == 31.89

    def test_load_empty_positions(self, empty_state_file: Path) -> None:
        """加载空持仓文件。"""
        tracker = LivePositionTracker(state_file=empty_state_file)
        portfolio = tracker.get_portfolio()
        assert len(portfolio) == 0

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        """文件不存在时保持空持仓（fail-safe，不抛异常）。"""
        tracker = LivePositionTracker(state_file=tmp_path / "nonexistent.json")
        assert len(tracker.get_portfolio()) == 0
        # 不应抛异常，保持空持仓


class TestLivePositionTrackerOrderRejection:
    """测试 order() 拒绝下单（核心安全机制）。"""

    def test_order_buy_rejected(self, live_state_file: Path) -> None:
        """买入订单被拒绝。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        result = tracker.order("002714", "buy", 100, 38.0)

        assert result.success is False
        assert "track_live" in result.message
        assert result.quantity == 0

    def test_order_sell_rejected(self, live_state_file: Path) -> None:
        """卖出订单被拒绝。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        result = tracker.order("002714", "sell", 100, 38.0)

        assert result.success is False
        assert "track_live" in result.message
        assert result.quantity == 0

    def test_order_does_not_modify_positions(self, live_state_file: Path) -> None:
        """下单后持仓不变（关键安全测试）。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        original_portfolio = tracker.get_portfolio()
        original_002714 = tracker.get_position("002714")

        # 尝试买入
        tracker.order("002714", "buy", 100, 38.0)
        # 尝试卖出
        tracker.order("002714", "sell", 100, 38.0)

        # 持仓应完全不变
        after_portfolio = tracker.get_portfolio()
        after_002714 = tracker.get_position("002714")

        assert len(after_portfolio) == len(original_portfolio)
        assert after_002714 is not None
        assert after_002714.quantity == original_002714.quantity  # type: ignore[union-attr]
        assert after_002714.avg_cost == original_002714.avg_cost  # type: ignore[union-attr]


class TestLivePositionTrackerPriceUpdate:
    """测试价格更新。"""

    def test_update_price_changes_current_price(self, live_state_file: Path) -> None:
        """update_price 更新现价。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        tracker.update_price("002714", 39.50)

        pos = tracker.get_position("002714")
        assert pos is not None
        assert pos.current_price == 39.50

    def test_update_price_tracked_lowest(self, live_state_file: Path) -> None:
        """update_price 追踪最低价。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        # 原始 lowest_price = 36.69
        tracker.update_price("002714", 35.00)  # 低于原最低价

        pos = tracker.get_position("002714")
        assert pos is not None
        assert pos.lowest_price == 35.00

    def test_update_price_does_not_persist(self, live_state_file: Path) -> None:
        """update_price 不持久化（避免高频IO）。

        重新加载后，current_price 应为文件中的原始值，而非更新后的值。
        """
        tracker = LivePositionTracker(state_file=live_state_file)
        tracker.update_price("002714", 39.50)

        # 重新加载
        tracker2 = LivePositionTracker(state_file=live_state_file)
        pos = tracker2.get_position("002714")
        assert pos is not None
        # 应为文件中的原始值 37.93，而非 39.50
        assert pos.current_price == 37.93


class TestLivePositionTrackerStopLoss:
    """测试止损价更新（持久化）。"""

    def test_update_stop_loss_persists(self, live_state_file: Path) -> None:
        """update_stop_loss 持久化止损价。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        tracker.update_stop_loss("002714", 33.50)

        # 重新加载
        tracker2 = LivePositionTracker(state_file=live_state_file)
        pos = tracker2.get_position("002714")
        assert pos is not None
        assert pos.stop_loss_price == 33.50


class TestLivePositionTrackerAccount:
    """测试账户信息。"""

    def test_get_account(self, live_state_file: Path) -> None:
        """get_account 返回正确的账户信息。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        account = tracker.get_account()

        assert account.cash == 991879.89
        # total_value = cash + positions_value
        # 002714: 100 * 37.93 = 3793
        # 510500: 400 * 8.90 = 3560
        # positions_value = 3793 + 3560 = 7353
        expected_positions_value = 100 * 37.93 + 400 * 8.90
        assert account.total_value == pytest.approx(991879.89 + expected_positions_value)
        assert len(account.positions) == 2

    def test_daily_start_value(self, live_state_file: Path) -> None:
        """daily_start_value 属性。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        assert tracker.daily_start_value == 1006138.79

    def test_reset_daily_start_value(self, live_state_file: Path) -> None:
        """reset_daily_start_value 更新日初净值。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        tracker.reset_daily_start_value()

        account = tracker.get_account()
        assert tracker.daily_start_value == pytest.approx(account.total_value)

    def test_settle_t_plus_1(self, live_state_file: Path) -> None:
        """settle_t_plus_1 设置所有持仓为可卖。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        tracker.settle_t_plus_1()

        for pos in tracker.get_portfolio().values():
            assert pos.available == pos.quantity

    def test_trades_empty(self, live_state_file: Path) -> None:
        """trades 属性返回空列表（track_live 不记录系统交易）。"""
        tracker = LivePositionTracker(state_file=live_state_file)
        assert tracker.trades == []


class TestLivePositionTrackerThreadSafety:
    """测试线程安全。"""

    def test_concurrent_update_price(self, live_state_file: Path) -> None:
        """并发 update_price 不会崩溃。"""
        import threading

        tracker = LivePositionTracker(state_file=live_state_file)
        errors: list[Exception] = []

        def update_price_thread() -> None:
            try:
                for i in range(100):
                    tracker.update_price("002714", 37.0 + i * 0.01)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=update_price_thread) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
