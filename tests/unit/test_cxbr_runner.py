"""CXBR 编排层测试。"""

from __future__ import annotations

import pandas as pd

from gugu.execution.cxbr_paper import CXBRMarketSnapshot, CXBRPaperExecutor
from gugu.research.cxbr_runner import CXBRCandidate, CXBRRunner
from gugu.research.cxbr_v1 import CXBRSignal


class _Notifier:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def notify_cxbr_signal(self, signal: dict) -> bool:
        self.payloads.append(signal)
        return True


def test_runner_keeps_observe_as_next_day_watch(tmp_path) -> None:
    notifier = _Notifier()
    runner = CXBRRunner(
        CXBRPaperExecutor(
            initial_capital=1_000_000,
            state_file=tmp_path / "state.json",
            ledger_file=tmp_path / "ledger.jsonl",
        ),
        notifier=notifier,
    )
    candidate = CXBRCandidate(
        code="600001",
        history=pd.DataFrame(),
        metadata={},
        snapshot=CXBRMarketSnapshot(price=10.0, prev_close=9.5),
    )
    result = __import__("asyncio").run(runner.run_candidate(candidate))
    assert result.signal.action == "none"
    assert result.execution is None
    assert notifier.payloads
    assert notifier.payloads[0]["data_quality"] == "unavailable"


def test_runner_does_not_execute_unavailable_buy(monkeypatch, tmp_path) -> None:
    class _Paper:
        def execute_signal(self, signal, snapshot):
            raise AssertionError("unavailable 信号不应进入模拟下单")

    runner = CXBRRunner(_Paper())
    signal = CXBRSignal(
        action="buy",
        stage="entry",
        code="600001",
        asof="2026-08-18",
        reason="测试",
        data_quality="unavailable",
    )
    monkeypatch.setattr("gugu.research.cxbr_runner.scan", lambda *args, **kwargs: signal)
    candidate = CXBRCandidate(
        code="600001",
        history=pd.DataFrame(),
        metadata={},
        snapshot=CXBRMarketSnapshot(price=10.0, prev_close=9.5),
    )
    result = __import__("asyncio").run(runner.run_candidate(candidate))
    assert result.execution is None
