"""统一事件研究框架（event-study v2）。

所有策略验证共享同一套事件定义、入场口径、成本模型、基准对齐和统计输出。

方法论：
1. 信号日与实际入场日分离，``next_open`` 不把信号日收盘当成成交价。
2. 持有期按实际入场交易日计算：h=1 表示入场后第一个交易日收盘。
3. 策略收益扣除显式交易成本，再与同一入场/退出窗口的基准收益比较。
4. 训练期、验证期、年份和股票集中度均输出，缺失基准不丢弃绝对收益样本。
5. 行情加载器可注入，测试和离线回放不依赖外部网络。

使用方式::

    from gugu.analysis.event_study import EventStudy, StudyConfig

    study = EventStudy(StudyConfig(strategy_name="my_strategy"))
    result = study.run_from_history("data/signals_history.jsonl")
    study.print_summary(result)
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from gugu.analysis.event_study_data import (
    KlineLoader,
    fetch_index_klines,
    fetch_stock_klines,
)
from gugu.analysis.event_study_data import (
    finite_float as _finite_float,
)
from gugu.analysis.event_study_data import (
    naive_timestamp as _naive_timestamp,
)
from gugu.analysis.event_study_data import (
    normalize_ohlcv as _normalize_ohlcv,
)
from gugu.analysis.event_study_data import (
    normalize_symbol as _normalize_symbol,
)
from gugu.analysis.event_study_data import (
    safe_timestamp as _safe_timestamp,
)
from gugu.analysis.event_study_models import (
    DEFAULT_DATA_DAYS,
    DEFAULT_INDEX_CODE,
    HorizonResult,
    StudyConfig,
    StudyEvent,
    StudyResult,
)

__all__ = [
    "EventStudy",
    "HorizonResult",
    "StudyConfig",
    "StudyEvent",
    "StudyResult",
    "welch_t",
    "fetch_index_klines",
    "fetch_stock_klines",
]
from gugu.analysis.event_study_report import EventStudyReportMixin
from gugu.analysis.event_study_runner import EventStudyRunnerMixin
from gugu.utils.log import get_logger

logger = get_logger()

# ── 统一数据/模型已拆分到 event_study_data.py 与 event_study_models.py ──


def welch_t(a: np.ndarray) -> tuple[float, float]:
    """对超额收益做单样本 t 检验（vs 零）。"""
    from scipy import stats

    values = np.asarray(a, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 3:
        return 0.0, 1.0
    result = stats.ttest_1samp(values, 0.0, nan_policy="omit")
    t_stat = float(result.statistic)
    p_value = float(result.pvalue)
    if not math.isfinite(t_stat) or not math.isfinite(p_value):
        return 0.0, 1.0
    return t_stat, p_value


def _is_success(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "success", "filled"}


def _strategy_names(value: Any, fallback: Any = None) -> list[str]:
    """兼容 list、JSON 字符串和单个策略名。"""
    candidate = value if value not in (None, "", []) else fallback
    if isinstance(candidate, str):
        text = candidate.strip()
        if text.startswith("["):
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError:
                candidate = [text]
        else:
            candidate = [text]
    if not isinstance(candidate, (list, tuple, set)):
        candidate = [candidate]
    return [str(item).strip() for item in candidate if str(item or "").strip()]


# ── 核心分析类 ───────────────────────────────────────────


class EventStudy(EventStudyRunnerMixin, EventStudyReportMixin):
    """统一事件研究分析器。

    ``stock_loader`` 和 ``index_loader`` 的签名均为 ``(symbol, days) -> DataFrame``，
    便于使用缓存、fixture 或 DataManager 替换默认网络数据源。
    """

    _FILL_SOURCES = {
        "fill",
        "fill_signal_date",
        "actual_fill",
        "execution_fill",
        "broker_fill",
    }

    def __init__(
        self,
        config: StudyConfig,
        *,
        stock_loader: KlineLoader | None = None,
        index_loader: KlineLoader | None = None,
    ) -> None:
        self.config = config
        self._stock_loader = stock_loader or fetch_stock_klines
        self._index_loader = index_loader or fetch_index_klines
        self._index_df: pd.DataFrame | None = None
        self._index_days: int = 0
        self._stock_cache: dict[tuple[str, int], pd.DataFrame] = {}

    # ── 入口方法 ─────────────────────────────────────────

    # ── 数据与事件定位 ───────────────────────────────────

    def _ensure_index_data(self, *, days: int | None = None) -> None:
        requested_days = int(days or self.config.data_days)
        if self._index_df is not None and self._index_days >= requested_days:
            return
        try:
            loaded = self._index_loader(self.config.index_code, requested_days)
            self._index_df = _normalize_ohlcv(loaded)
            self._index_days = requested_days
        except Exception as exc:
            logger.warning("指数 {} 数据加载失败: {}", self.config.index_code, exc)
            self._index_df = pd.DataFrame()
            self._index_days = requested_days

    def _get_stock_data(self, symbol: str, days: int | None = None) -> pd.DataFrame:
        normalized = _normalize_symbol(symbol)
        requested_days = int(days or self.config.data_days)
        key = (normalized, requested_days)
        if key not in self._stock_cache:
            try:
                loaded = self._stock_loader(normalized, requested_days)
                normalized_df = _normalize_ohlcv(loaded)
            except Exception as exc:
                logger.warning("行情加载失败 {}: {}", normalized, exc)
                normalized_df = pd.DataFrame()
            self._stock_cache[key] = normalized_df
        return self._stock_cache[key].copy()

    @staticmethod
    def _find_exact_index(stock_df: pd.DataFrame, target_date: pd.Timestamp) -> int | None:
        """只匹配指定交易日，供区分实际交易日与节假日信号。"""
        if stock_df.empty or "date" not in stock_df.columns:
            return None
        target = _naive_timestamp(target_date)
        matches = stock_df.index[stock_df["date"] == target]
        return int(matches[0]) if len(matches) else None

    @staticmethod
    def _find_signal_index(stock_df: pd.DataFrame, signal_date: pd.Timestamp) -> int | None:
        """优先匹配信号日；非交易日使用其后的第一个交易日。"""
        if stock_df.empty or "date" not in stock_df.columns:
            return None
        target = _naive_timestamp(signal_date)
        dates = stock_df["date"]
        exact = dates[dates == target]
        if not exact.empty:
            return int(exact.index[0])
        future = dates[dates > target]
        if not future.empty:
            return int(future.index[0])
        return None

    def _resolve_entry(
        self,
        stock_df: pd.DataFrame,
        signal_idx: int,
        signal_price: float,
        entry_source: str,
    ) -> tuple[int | None, float | None, pd.Timestamp | None, str]:
        source = entry_source.strip().lower()
        is_fill = source in self._FILL_SOURCES
        # 有实际成交价但没有可解析成交日期时，不能把成交价误当成 T+1 开盘价。
        # 调用方应尽量提供 entry_date；缺失时退回信号日对应的交易日并保留成交价。
        if is_fill and signal_price > 0:
            entry_date = stock_df.iloc[signal_idx]["date"]
            resolved_source = "fill_signal_date" if source == "fill_signal_date" else "fill"
            return signal_idx, signal_price, entry_date, resolved_source

        if self.config.entry_mode == "next_open":
            entry_idx = signal_idx + 1
            if entry_idx >= len(stock_df):
                return None, None, None, "next_open"
            entry_date = stock_df.iloc[entry_idx]["date"]
            entry_price = _finite_float(stock_df.iloc[entry_idx]["open"])
            return entry_idx, entry_price, entry_date, "next_open"

        entry_date = stock_df.iloc[signal_idx]["date"]
        # close 模式下优先使用调用方传入的信号/订单价格；缺失时才回退到 K 线收盘价。
        entry_price = (
            signal_price if signal_price > 0 else _finite_float(stock_df.iloc[signal_idx]["close"])
        )
        return signal_idx, entry_price, entry_date, "signal_close"

    @staticmethod
    def _buy_positions(signals: pd.DataFrame, market_df: pd.DataFrame) -> list[int]:
        """将策略输出安全转换为行情 positional index。

        优先使用策略输出中的日期列对齐；只有策略确实返回与行情等长的结果时，
        才使用位置索引。这样可以避免策略 reset/drop/filter 后的索引错位。
        """
        frame = signals.copy()
        if "signal" in frame.columns:
            values = pd.to_numeric(frame["signal"], errors="coerce").fillna(0)
            buy_mask = values.eq(1)
        elif "direction" in frame.columns:
            buy_mask = frame["direction"].astype(str).str.lower().eq("buy")
        else:
            return []

        market_dates = market_df["date"].map(_naive_timestamp)
        date_column = next(
            (name for name in ("date", "signal_date", "timestamp") if name in frame.columns),
            None,
        )
        positions: list[int] = []
        for pos in frame.index[buy_mask].tolist():
            data_pos: int | None = None
            if date_column is not None:
                signal_date = _safe_timestamp(frame.loc[pos, date_column])
                if not pd.isna(signal_date):
                    matches = market_dates[market_dates == signal_date]
                    if not matches.empty:
                        data_pos = int(matches.index[0])
                    else:
                        future = market_dates[market_dates > signal_date]
                        if not future.empty:
                            data_pos = int(future.index[0])
            if data_pos is None and len(frame) == len(market_df):
                data_pos = int(frame.index.get_loc(pos))
            if data_pos is not None and 0 <= data_pos < len(market_df):
                positions.append(data_pos)
        return positions

    def _classify_period(self, signal_date: pd.Timestamp | datetime | str) -> str:
        date_value = _naive_timestamp(signal_date)
        train_start = _naive_timestamp(self.config.train_start)
        train_end = _naive_timestamp(self.config.train_end)
        val_start = _naive_timestamp(self.config.val_start)
        val_end = _naive_timestamp(self.config.val_end)
        if train_start <= date_value <= train_end:
            return "train"
        if val_start <= date_value <= val_end:
            return "val"
        return "outside"

    def _forward_return(
        self,
        stock_df: pd.DataFrame,
        idx: int,
        horizon: int,
        entry_price: float,
        *,
        entry_idx: int | None = None,
    ) -> float | None:
        """计算净前瞻收益；horizon 从实际入场交易日向后计数。"""
        if horizon <= 0:
            return None
        actual_entry_idx = entry_idx
        if actual_entry_idx is None:
            actual_entry_idx = idx + 1 if self.config.entry_mode == "next_open" else idx
            if actual_entry_idx >= len(stock_df):
                return None
            if self.config.entry_mode == "next_open":
                entry_price = _finite_float(stock_df.iloc[actual_entry_idx]["open"]) or 0.0
        target_idx = actual_entry_idx + horizon
        if actual_entry_idx < 0 or target_idx >= len(stock_df) or entry_price <= 0:
            return None
        exit_price = _finite_float(stock_df.iloc[target_idx]["close"])
        if exit_price is None or exit_price <= 0:
            return None
        # cost_rate 是往返成本，使用与 generic_event_study 一致的乘法口径。
        net_return = (exit_price / entry_price) * (1 - self.config.cost_rate) - 1
        return round(net_return, 4)

    def _excess_return(
        self,
        entry_date: pd.Timestamp,
        horizon: int,
        strategy_return: float,
        *,
        entry_source: str = "signal_close",
    ) -> float | None:
        """计算相对基准同期收益的超额。

        策略若按 next_open 入场，基准也从同一交易日的 open 开始，
        避免把股票的开盘跳空 alpha 与指数隔夜收益错误混在一起。
        """
        if self._index_df is None or self._index_df.empty:
            return None
        start_idx = self._find_signal_index(self._index_df, entry_date)
        if start_idx is None:
            return None
        target_idx = start_idx + horizon
        if target_idx >= len(self._index_df):
            return None
        price_column = "open" if entry_source.strip().lower() == "next_open" else "close"
        index_entry = _finite_float(self._index_df.iloc[start_idx][price_column])
        index_exit = _finite_float(self._index_df.iloc[target_idx]["close"])
        if index_entry is None or index_exit is None or index_entry <= 0:
            return None
        benchmark_return = index_exit / index_entry - 1
        return round(strategy_return - benchmark_return, 4)

    # ── 聚合统计与结果输出由 EventStudyReportMixin 提供 ──


_DEFAULT_POOL = [
    "002472",
    "300033",
    "002230",
    "603501",
    "300308",
    "300502",
    "600519",
    "300750",
    "002594",
    "300059",
    "002475",
    "600050",
    "000977",
    "688041",
    "002371",
]


def cli() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="统一事件研究框架")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--history", default="data/signals_history.jsonl")
    parser.add_argument("--output", default=None)
    parser.add_argument("--entry-mode", default="next_open", choices=["close", "next_open"])
    parser.add_argument("--horizons", default="3,5,10")
    parser.add_argument("--index", default=DEFAULT_INDEX_CODE)
    parser.add_argument("--days", type=int, default=DEFAULT_DATA_DAYS)
    args = parser.parse_args()

    config = StudyConfig(
        strategy_name=args.strategy,
        horizons=[int(value) for value in args.horizons.split(",")],
        index_code=args.index,
        entry_mode=args.entry_mode,
        output_path=args.output,
        data_days=args.days,
    )
    study = EventStudy(config)
    result = study.run_from_history(args.history, max_days=args.days)
    study.print_summary(result)


if __name__ == "__main__":
    cli()
