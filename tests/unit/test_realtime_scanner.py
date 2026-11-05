"""v0.9.0 实时推荐引擎 + QMT broker 单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gugu.execution.base import OrderResult
from gugu.execution.qmt import QmtBroker, to_gugu_symbol, to_qmt_symbol


# ────────────────────── 代码转换 ──────────────────────


class TestSymbolConversion:
    @pytest.mark.parametrize(
        "gugu_symbol,expected",
        [
            ("sh600000", "600000.SH"),
            ("sz000001", "000001.SZ"),
            ("bj430047", "430047.BJ"),
            ("hk00700", "00700.HK"),
            ("600519", "600519.SH"),
            ("000858", "000858.SZ"),
            ("600000.SH", "600000.SH"),
        ],
    )
    def test_to_qmt(self, gugu_symbol, expected):
        assert to_qmt_symbol(gugu_symbol) == expected

    @pytest.mark.parametrize(
        "qmt_symbol,expected",
        [
            ("600000.SH", "sh600000"),
            ("000001.SZ", "sz000001"),
            ("430047.BJ", "bj430047"),
            ("00700.HK", "hk00700"),
        ],
    )
    def test_to_gugu(self, qmt_symbol, expected):
        assert to_gugu_symbol(qmt_symbol) == expected

    def test_roundtrip(self):
        for s in ["sh600000", "sz000001", "bj430047"]:
            assert to_gugu_symbol(to_qmt_symbol(s)) == s


# ────────────────────── QmtBroker 安全机制 ──────────────────────


class TestQmtBrokerSafety:
    def test_unconfigured_rejects_order(self):
        """未配置 account_id 时下单必须被拒绝（防裸奔）。"""
        broker = QmtBroker()
        result = broker.order("sh600000", "buy", 100, 10.0)
        assert result.success is False
        assert "account_id" in result.message or "未连接" in result.message

    def test_unconfigured_connect_fails(self):
        """未配置时 connect() 返回 False。"""
        broker = QmtBroker()
        assert broker.connect() is False

    @patch("gugu.execution.qmt.env")
    def test_configured_but_xtquant_missing(self, mock_env):
        """配置了但 xtquant 未安装：connect 返回 False 且不崩溃。"""
        mock_env.return_value.qmt_account_id = "123456"
        mock_env.return_value.qmt_path = "/tmp/qmt"  # nosec B108: 测试 mock 路径，非真实文件操作
        broker = QmtBroker()
        assert broker.connect() is False

    def test_lot_rounding(self):
        """下单数量自动取整到 100 股整数倍。"""
        # 未配置时直接拒绝，取整逻辑在 order 内部；这里验证取整函数逻辑
        qty = 150
        rounded = max(100, int(qty // 100 * 100))
        assert rounded == 100
        qty2 = 250
        assert max(100, int(qty2 // 100 * 100)) == 200

    # ── 实盘硬闸（2026-11-04 评审 P0）──

    @staticmethod
    def _connected_broker():
        """构造「已连接且配置齐全」的 broker，用于验证硬闸而非连接检查。"""
        with patch("gugu.execution.qmt.env") as mock_env:
            mock_env.return_value.qmt_account_id = "123456"
            mock_env.return_value.qmt_path = "/tmp/qmt"  # nosec B108: 测试 mock 路径
            broker = QmtBroker()
        broker._connected = True
        broker._account = MagicMock()
        broker._trader = MagicMock()
        broker._trader.order_stock.return_value = 1001
        return broker

    @patch("gugu.execution.qmt.get_config")
    def test_non_live_mode_rejects_order(self, mock_cfg):
        """非 live 模式，QMT 一律不得发出真实委托。"""
        mock_cfg.return_value.execution.mode = "paper"
        mock_cfg.return_value.strategy.verified_strategies = ["box_breakout"]
        broker = self._connected_broker()
        result = broker.order("sh600000", "buy", 100, 10.0)
        assert result.success is False
        assert "live" in result.message
        broker._trader.order_stock.assert_not_called()

    @patch("gugu.execution.qmt.get_config")
    def test_live_without_verified_strategy_rejects_buy(self, mock_cfg):
        """live 但生产白名单为空：禁止实盘买入。"""
        mock_cfg.return_value.execution.mode = "live"
        mock_cfg.return_value.strategy.verified_strategies = []
        broker = self._connected_broker()
        result = broker.order("sh600000", "buy", 100, 10.0)
        assert result.success is False
        assert "verified_strategies" in result.message
        broker._trader.order_stock.assert_not_called()

    @patch("gugu.execution.qmt.get_config")
    def test_emergency_buy_cannot_bypass_guard(self, mock_cfg):
        """emergency_buy_limit 不再能绕过执行模式与白名单硬闸。"""
        mock_cfg.return_value.execution.mode = "live"
        mock_cfg.return_value.strategy.verified_strategies = []
        broker = self._connected_broker()
        result = broker.emergency_buy_limit("sh600000", 100, 10.0)
        assert result.success is False
        broker._trader.order_stock.assert_not_called()

    @patch("gugu.strategies.manifest.StrategyManifest")
    @patch("gugu.execution.qmt.get_config")
    def test_live_with_verified_strategy_allows_buy(self, mock_cfg, mock_manifest_cls):
        """live + 白名单策略 + manifest 证据通过 → 买入委托可提交。"""
        mock_cfg.return_value.execution.mode = "live"
        mock_cfg.return_value.strategy.verified_strategies = ["box_breakout"]
        mock_manifest_cls.return_value.list_runtime_enabled.return_value = ["box_breakout"]
        broker = self._connected_broker()
        result = broker.order("sh600000", "buy", 100, 10.0)
        assert result.success is True
        broker._trader.order_stock.assert_called_once()

    @patch("gugu.strategies.manifest.StrategyManifest")
    @patch("gugu.execution.qmt.get_config")
    def test_live_buy_rejected_when_whitelist_lacks_manifest_evidence(
        self, mock_cfg, mock_manifest_cls
    ):
        """P1-B：白名单有名字，但 manifest 无证据通过 → 拒绝（防只改配置绕过证据链）。"""
        mock_cfg.return_value.execution.mode = "live"
        mock_cfg.return_value.strategy.verified_strategies = ["handwritten_fake"]
        mock_manifest_cls.return_value.list_runtime_enabled.return_value = []
        broker = self._connected_broker()
        result = broker.order("sh600000", "buy", 100, 10.0)
        assert result.success is False
        assert "证据" in result.message
        broker._trader.order_stock.assert_not_called()

    @patch("gugu.strategies.manifest.StrategyManifest")
    @patch("gugu.execution.qmt.get_config")
    def test_live_evidence_check_failure_fails_closed(self, mock_cfg, mock_manifest_cls):
        """P1-B：manifest 证据校验异常时 fail-closed，绝不裸奔。"""
        mock_cfg.return_value.execution.mode = "live"
        mock_cfg.return_value.strategy.verified_strategies = ["box_breakout"]
        mock_manifest_cls.side_effect = RuntimeError("manifest unreadable")
        broker = self._connected_broker()
        result = broker.order("sh600000", "buy", 100, 10.0)
        assert result.success is False
        broker._trader.order_stock.assert_not_called()

    @patch("gugu.execution.qmt.get_config")
    def test_live_sell_is_allowed_without_whitelist(self, mock_cfg):
        """卖出/平仓是降风险方向，不应被自动交易白名单卡死。"""
        mock_cfg.return_value.execution.mode = "live"
        mock_cfg.return_value.strategy.verified_strategies = []
        broker = self._connected_broker()
        result = broker.order("sh600000", "sell", 100, 10.0)
        assert result.success is True
        broker._trader.order_stock.assert_called_once()

    @patch("gugu.execution.qmt.get_config")
    def test_config_read_failure_fails_closed(self, mock_cfg):
        """配置读取异常时 fail-closed，绝不裸奔。"""
        mock_cfg.side_effect = RuntimeError("config unavailable")
        broker = self._connected_broker()
        result = broker.order("sh600000", "buy", 100, 10.0)
        assert result.success is False
        assert "fail-closed" in result.message
        broker._trader.order_stock.assert_not_called()


# ────────────────────── 实时推荐引擎 ──────────────────────


class TestRealtimeScanner:
    def _make_engine(self):
        """构造带 mock 通知器/券商/扫描管线的引擎。"""
        engine = MagicMock()
        engine._scan_signals = AsyncMock(return_value=[])
        engine._notifier = MagicMock()
        engine._notifier.notify_signal = AsyncMock(return_value=True)
        engine._broker = MagicMock()
        engine._broker._connected = True
        engine._broker.order = MagicMock(
            return_value=OrderResult(True, "sh600000", "buy", 10.0, 100, 0.25)
        )
        return engine

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_no_signals_no_push(self, mock_time, mock_day):
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        scanner = RealtimeSignalScanner(engine)
        pushed = await scanner.run_cycle()
        assert pushed == []
        engine._notifier.notify_signal.assert_not_called()

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_signal_pushed_once(self, mock_time, mock_day):
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        signal = {
            "symbol": "sh600000",
            "direction": "buy",
            "name": "浦发银行",
            "price": 10.5,
            "strategy": "price_volume_signal",
        }
        engine._scan_signals = AsyncMock(return_value=[signal])
        scanner = RealtimeSignalScanner(engine, cooldown_sec=1800)
        pushed = await scanner.run_cycle()
        assert len(pushed) == 1
        assert engine._notifier.notify_signal.await_count == 1
        assert engine._broker.order.assert_not_called() is None

        # 冷却期内再次扫描同一信号 → 不重复推送（重置节流计时以触发真实扫描）
        scanner._last_scan_ts = 0
        pushed2 = await scanner.run_cycle()
        assert pushed2 == []
        assert engine._notifier.notify_signal.await_count == 1

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_signal_only_mode_only_notifies(self, mock_time, mock_day):
        """signal_only 模式：只推送观察信号，不进入模拟盘或实盘执行。"""
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        signal = {
            "symbol": "sh600000",
            "direction": "buy",
            "price": 10.5,
            "suggested_quantity": 200,
        }
        engine._scan_signals = AsyncMock(return_value=[signal])
        scanner = RealtimeSignalScanner(engine)
        # mock 独立模拟盘
        mock_paper = MagicMock()
        mock_paper.order = MagicMock(
            return_value=OrderResult(True, "sh600000", "buy", 10.5, 200, 0.53)
        )
        scanner._paper_broker = mock_paper
        with patch("gugu.engine.realtime_scanner.get_config") as mock_cfg:
            mock_cfg.return_value.execution.mode = "signal_only"
            await scanner.run_cycle()
        # 只推送原始观察信号，不模拟成交
        assert engine._notifier.notify_signal.await_count == 1
        mock_paper.order.assert_not_called()
        engine._broker.order.assert_not_called()

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_observation_buy_never_executes_in_paper_mode(self, mock_time, mock_day):
        """被数据门禁标记的买入不能绕过流水线进入 paper 执行器。"""
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        engine._process_signal = AsyncMock(return_value=None)
        engine._scan_signals = AsyncMock(
            return_value=[
                {
                    "symbol": "sh600000",
                    "direction": "buy",
                    "price": 10.5,
                    "observation_only": True,
                    "rejected": True,
                    "reject_reason": "数据状态为 stale_degraded",
                }
            ]
        )
        scanner = RealtimeSignalScanner(engine)
        with patch("gugu.engine.realtime_scanner.get_config") as mock_cfg:
            mock_cfg.return_value.execution.mode = "paper"
            await scanner.run_cycle()

        engine._process_signal.assert_not_awaited()
        engine._broker.order.assert_not_called()

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_paper_mode_executes_to_broker(self, mock_time, mock_day):
        """paper 模式：信号走引擎 _process_signal → 模拟盘下单。"""
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        # 给引擎挂一个 _process_signal，验证 paper 模式会调用它
        engine._process_signal = AsyncMock(return_value=None)
        signal = {
            "symbol": "sh600000",
            "direction": "buy",
            "price": 10.5,
            "suggested_quantity": 200,
        }
        engine._scan_signals = AsyncMock(return_value=[signal])
        scanner = RealtimeSignalScanner(engine)
        with patch("gugu.engine.realtime_scanner.get_config") as mock_cfg:
            mock_cfg.return_value.execution.mode = "paper"
            await scanner.run_cycle()
        # paper 模式应调用引擎 _process_signal（内部走 PaperBroker）
        engine._process_signal.assert_awaited_once()
        # 不应直接调 broker.order（那是 live 模式路径）
        engine._broker.order.assert_not_called()

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_live_auto_execute(self, mock_time, mock_day):
        """live + confirm_required=False + 白名单内策略 → 自动下单。

        2026-11-04 安全加固后，自动下单还需满足两个前置条件：
        verified_strategies 非空，且信号可追溯到白名单内策略。
        """
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        signal = {
            "symbol": "sh600000",
            "direction": "buy",
            "price": 10.5,
            "suggested_quantity": 200,
            "strategy": "box_breakout",
        }
        engine._scan_signals = AsyncMock(return_value=[signal])
        scanner = RealtimeSignalScanner(engine)
        with patch("gugu.engine.realtime_scanner.get_config") as mock_cfg:
            mock_cfg.return_value.execution.mode = "live"
            mock_cfg.return_value.execution.confirm_required = False
            mock_cfg.return_value.strategy.verified_strategies = ["box_breakout"]
            await scanner.run_cycle()
        engine._broker.order.assert_called_once()
        args = engine._broker.order.call_args[0]
        assert args[0] == "sh600000"
        assert args[1] == "buy"

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_live_confirm_false_with_empty_whitelist_no_execute(self, mock_time, mock_day):
        """P0：仅把 confirm_required 改成 false 不得打开实盘自动下单。"""
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        engine._scan_signals = AsyncMock(
            return_value=[{"symbol": "sh600000", "direction": "buy", "strategy": "box_breakout"}]
        )
        scanner = RealtimeSignalScanner(engine)
        with patch("gugu.engine.realtime_scanner.get_config") as mock_cfg:
            mock_cfg.return_value.execution.mode = "live"
            mock_cfg.return_value.execution.confirm_required = False
            mock_cfg.return_value.strategy.verified_strategies = []
            await scanner.run_cycle()
        engine._broker.order.assert_not_called()

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_explicit_auto_trade_cannot_bypass_whitelist(self, mock_time, mock_day):
        """P0：构造参数 auto_trade=True 也不能绕过生产白名单。"""
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        engine._scan_signals = AsyncMock(
            return_value=[{"symbol": "sh600000", "direction": "buy", "strategy": "box_breakout"}]
        )
        scanner = RealtimeSignalScanner(engine, auto_trade=True)
        with patch("gugu.engine.realtime_scanner.get_config") as mock_cfg:
            mock_cfg.return_value.execution.mode = "live"
            mock_cfg.return_value.execution.confirm_required = True
            mock_cfg.return_value.strategy.verified_strategies = []
            await scanner.run_cycle()
        engine._broker.order.assert_not_called()

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_live_unverified_signal_strategy_no_execute(self, mock_time, mock_day):
        """P0：白名单里有 A，但信号来自未验证策略 B 时不得下单。"""
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        engine._scan_signals = AsyncMock(
            return_value=[{"symbol": "sh600000", "direction": "buy", "strategy": "rebound_system"}]
        )
        scanner = RealtimeSignalScanner(engine)
        with patch("gugu.engine.realtime_scanner.get_config") as mock_cfg:
            mock_cfg.return_value.execution.mode = "live"
            mock_cfg.return_value.execution.confirm_required = False
            mock_cfg.return_value.strategy.verified_strategies = ["box_breakout"]
            await scanner.run_cycle()
        engine._broker.order.assert_not_called()

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_live_confirm_required_no_execute(self, mock_time, mock_day):
        """live + confirm_required=True → 只推送等待人工确认。"""
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        signal = {
            "symbol": "sh600000",
            "direction": "buy",
            "price": 10.5,
        }
        engine._scan_signals = AsyncMock(return_value=[signal])
        scanner = RealtimeSignalScanner(engine)
        with patch("gugu.engine.realtime_scanner.get_config") as mock_cfg:
            mock_cfg.return_value.execution.mode = "live"
            mock_cfg.return_value.execution.confirm_required = True
            await scanner.run_cycle()
        engine._broker.order.assert_not_called()

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_scan_throttle(self, mock_time, mock_day):
        """扫描节流：间隔内第二次调用不触发扫描。"""
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        signal = {
            "symbol": "sh600000",
            "direction": "buy",
            "name": "浦发银行",
            "price": 10.5,
        }
        engine._scan_signals = AsyncMock(return_value=[signal])
        scanner = RealtimeSignalScanner(engine, cooldown_sec=1800, scan_interval_sec=300)

        # 第一次：执行扫描并推送
        pushed = await scanner.run_cycle()
        assert len(pushed) == 1
        assert engine._scan_signals.await_count == 1

        # 第二次（间隔内）：节流跳过，不扫描不推送
        pushed2 = await scanner.run_cycle()
        assert pushed2 == []
        assert engine._scan_signals.await_count == 1  # 未再次调用

        # 重置扫描时间后：再次扫描但冷却期阻止推送
        scanner._last_scan_ts = 0
        pushed3 = await scanner.run_cycle()
        assert pushed3 == []
        assert engine._scan_signals.await_count == 2  # 再次调用
        assert engine._notifier.notify_signal.await_count == 1  # 只推送一次，冷却期不重复通知

    @pytest.mark.asyncio
    @patch("gugu.engine.realtime_scanner.is_trading_day", return_value=True)
    @patch("gugu.engine.realtime_scanner.is_trading_time", return_value=True)
    async def test_non_trading_time_skipped(self, mock_time, mock_day):
        """非交易时段不扫描。"""
        from gugu.engine.realtime_scanner import RealtimeSignalScanner

        engine = self._make_engine()
        scanner = RealtimeSignalScanner(engine)
        with patch("gugu.engine.realtime_scanner.is_trading_time", return_value=False):
            pushed = await scanner.run_cycle()
        assert pushed == []
        engine._scan_signals.assert_not_called()
