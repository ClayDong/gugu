"""CXBR V1 扫描、模拟成交和通知编排。

编排层不猜测缺失的涨停价、历史换手率或元数据；调用方必须先提供完整
CXBR 数据契约。该模块可以被 scheduler 调用，但默认不自动注册调度任务。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

import pandas as pd

from gugu.execution.cxbr_paper import (
    CXBRMarketSnapshot,
    CXBRPaperExecution,
    CXBRPaperExecutor,
)
from gugu.research.cxbr_v1 import CXBRConfig, CXBRPosition, CXBRSignal, evaluate_exit, scan


class CXBRNotifier(Protocol):
    async def notify_cxbr_signal(self, signal: dict[str, Any]) -> bool: ...


@dataclass(frozen=True)
class CXBRCandidate:
    code: str
    history: pd.DataFrame
    metadata: dict[str, Any]
    snapshot: CXBRMarketSnapshot
    position: CXBRPosition | None = None


@dataclass(frozen=True)
class CXBRRunResult:
    signal: CXBRSignal
    execution: CXBRPaperExecution | None
    notified: bool


class CXBRRunner:
    """单次 CXBR 运行器，供盘后、盘前和盘中任务复用。"""

    def __init__(
        self,
        paper_executor: CXBRPaperExecutor,
        notifier: CXBRNotifier | None = None,
        config: CXBRConfig | None = None,
    ) -> None:
        self._paper = paper_executor
        self._notifier = notifier
        self._config = config or CXBRConfig()

    async def run_candidate(self, candidate: CXBRCandidate) -> CXBRRunResult:
        """先生成信号，再按信号类型执行纸面交易或发送观察提醒。"""
        if candidate.position is None:
            signal = scan(
                candidate.code,
                candidate.history,
                candidate.metadata,
                config=self._config,
            )
        else:
            signal = evaluate_exit(
                candidate.code,
                candidate.history,
                candidate.position,
                config=self._config,
            )

        execution: CXBRPaperExecution | None = None
        if signal.action in ("buy", "sell") and signal.data_quality == "fresh_valid":
            execution = self._paper.execute_signal(signal, candidate.snapshot)

        notified = await self._notify(signal, execution, candidate.snapshot)
        return CXBRRunResult(signal=signal, execution=execution, notified=notified)

    async def run_candidates(self, candidates: list[CXBRCandidate]) -> list[CXBRRunResult]:
        """批量运行；单个候选失败不应吞掉其它候选结果。"""
        results: list[CXBRRunResult] = []
        for candidate in candidates:
            results.append(await self.run_candidate(candidate))
        return results

    async def _notify(
        self,
        signal: CXBRSignal,
        execution: CXBRPaperExecution | None,
        snapshot: CXBRMarketSnapshot,
    ) -> bool:
        if self._notifier is None:
            return False
        payload = signal.as_dict()
        payload["price"] = snapshot.price
        if signal.action == "observe":
            payload["observation_only"] = True
            if signal.stage in {"wash", "protect", "waiting"}:
                payload["stage"] = "next_day_watch"
        if execution is not None:
            payload["execution"] = asdict(execution)
        return await self._notifier.notify_cxbr_signal(payload)


__all__ = ["CXBRCandidate", "CXBRNotifier", "CXBRRunResult", "CXBRRunner"]
