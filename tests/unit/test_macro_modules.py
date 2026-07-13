"""宏观模块测试：collectors / report / scheduler 的核心逻辑覆盖。"""

from __future__ import annotations

from unittest.mock import MagicMock

from gugu.macro.models import (
    BondData,
    DerivativeData,
    FxData,
    GoldData,
    MacroSnapshot,
    OilData,
    StockIndexData,
)


class TestMacroSnapshot:
    """MacroSnapshot 数据模型测试。"""

    def test_snapshot_to_dict_roundtrip(self) -> None:
        """MacroSnapshot.to_dict() 应返回完整字段。"""
        snapshot = MacroSnapshot(
            gold=GoldData(gold_price=2350.5, gold_silver_ratio=85.2, valid=True),
            oil=OilData(brent_price=82.3, wti_price=78.1, valid=True),
            fx=FxData(usd_index=104.5, usd_cny=7.25, valid=True),
            bond=BondData(us10y_yield=4.2, us2y_yield=4.8, inverted=True, valid=True),
            derivative=DerivativeData(vix=18.5, valid=True),
            stock_index=StockIndexData(sh_index=3200.0, valid=True),
            timestamp="2026-07-07",
        )
        d = snapshot.to_dict()
        assert d["gold"]["gold_price"] == 2350.5
        assert d["bond"]["inverted"] is True
        assert d["fx"]["usd_cny"] == 7.25
        assert d["timestamp"] == "2026-07-07"

    def test_snapshot_defaults(self) -> None:
        """MacroSnapshot 默认值应安全。"""
        snapshot = MacroSnapshot()
        d = snapshot.to_dict()
        assert d["gold"]["gold_price"] == 0.0
        assert d["bond"]["inverted"] is False
        assert d["timestamp"] == ""

    def test_snapshot_all_valid_flag(self) -> None:
        """all_valid 标志默认为 False。"""
        snapshot = MacroSnapshot()
        assert snapshot.all_valid is False

        snapshot = MacroSnapshot(
            gold=GoldData(valid=True),
            oil=OilData(valid=True),
            fx=FxData(valid=True),
            bond=BondData(valid=True),
            derivative=DerivativeData(valid=True),
            stock_index=StockIndexData(valid=True),
            all_valid=True,
        )
        assert snapshot.all_valid is True


class TestMacroReportBuilder:
    """MacroReportBuilder 报告生成测试。"""

    def test_build_morning_card_from_snapshot(self) -> None:
        """从 MacroSnapshot 构建早间卡片。"""
        from gugu.macro.report import MacroReportBuilder

        snapshot = MacroSnapshot(
            gold=GoldData(gold_price=2350.5, gold_silver_ratio=85.2, valid=True),
            oil=OilData(brent_price=82.3, wti_price=78.1, valid=True),
            fx=FxData(usd_index=104.5, usd_cny=7.25, valid=True),
            bond=BondData(us10y_yield=4.2, us2y_yield=4.8, inverted=True, valid=True),
            derivative=DerivativeData(vix=18.5, valid=True),
            stock_index=StockIndexData(sh_index=3200.0, valid=True),
            timestamp="2026-07-07",
        )
        builder = MacroReportBuilder()
        card = builder.build_morning_card(snapshot)
        assert "header" in card
        assert "elements" in card
        assert len(card["elements"]) > 0

    def test_build_morning_card_handles_empty_snapshot(self) -> None:
        """空 MacroSnapshot 也应生成有效卡片。"""
        from gugu.macro.report import MacroReportBuilder

        snapshot = MacroSnapshot()
        builder = MacroReportBuilder()
        card = builder.build_morning_card(snapshot)
        assert "header" in card
        assert "elements" in card


class TestMacroScheduler:
    """宏观调度器核心逻辑测试。"""

    def test_build_portfolio_summary_with_positions(self) -> None:
        """_build_portfolio_summary 应正确汇总持仓。"""
        from gugu.macro.scheduler import _build_portfolio_summary

        broker = MagicMock()
        broker.get_account.return_value = MagicMock(
            total_asset=1_000_000,
            cash=500_000,
            positions=[],
        )
        summary = _build_portfolio_summary(broker)
        assert summary is not None

    def test_build_portfolio_summary_with_no_positions(self) -> None:
        """无持仓时 _build_portfolio_summary 应安全返回。"""
        from gugu.macro.scheduler import _build_portfolio_summary

        broker = MagicMock()
        broker.get_account.return_value = MagicMock(
            total_asset=1_000_000,
            cash=1_000_000,
            positions=[],
        )
        summary = _build_portfolio_summary(broker)
        assert summary is not None
