"""事件研究运行入口 mixin。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pandas as pd

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
from gugu.analysis.event_study_models import StudyEvent, StudyResult
from gugu.utils.log import get_logger

logger = get_logger()

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


def _is_success(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "success", "filled"}


def _strategy_names(value: Any, fallback: Any = None) -> list[str]:
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


class EventStudyRunnerMixin:
    def _read_history(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"信号历史文件不存在: {path}")
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def _history_row(self, record: dict[str, Any]) -> dict[str, Any] | None:
        if str(record.get("direction") or "").strip().lower() != "buy":
            return None
        if self.config.strategy_name not in _strategy_names(
            record.get("strategies"), record.get("strategy")
        ):
            return None
        signal_timestamp = _safe_timestamp(record.get("timestamp"))
        if pd.isna(signal_timestamp):
            return None

        order_result = record.get("order_result") or {}
        fill_price = (
            _finite_float(order_result.get("price"))
            if isinstance(order_result, dict) and _is_success(order_result.get("success"))
            else None
        )
        fill_timestamp = _safe_timestamp(
            (order_result.get("fill_time") if isinstance(order_result, dict) else None)
            or (order_result.get("executed_at") if isinstance(order_result, dict) else None)
            or record.get("fill_time")
            or record.get("executed_at")
            or record.get("order_time")
            or record.get("timestamp")
        )
        if fill_price is None and _is_success(
            record.get("execution_success") or record.get("order_success")
        ):
            fill_price = _finite_float(record.get("order_price"))
        signal_price = _finite_float(record.get("price"))
        has_fill = fill_price is not None and fill_price > 0
        entry = fill_price if has_fill else signal_price
        symbol = _normalize_symbol(record.get("symbol"))
        if not symbol or entry is None or entry <= 0:
            return None
        timestamped = has_fill and not pd.isna(fill_timestamp)
        row: dict[str, Any] = {
            "symbol": symbol,
            "date": signal_timestamp,
            "entry": entry,
            "entry_source": "fill"
            if timestamped
            else ("fill_signal_date" if has_fill else "signal"),
        }
        if timestamped:
            row["entry_date"] = fill_timestamp
        return row

    def _history_frame(self, records: list[dict[str, Any]]) -> pd.DataFrame:
        rows = [row for record in records if (row := self._history_row(record)) is not None]
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        priority = {"fill": 0, "fill_signal_date": 1, "signal": 2}
        frame["_source_priority"] = frame["entry_source"].map(priority).fillna(9)
        return (
            frame.sort_values(["date", "_source_priority"])
            .drop_duplicates(["symbol", "date"], keep="first")
            .drop(columns=["_source_priority"])
            .reset_index(drop=True)
        )

    def _history_days(self, signals: pd.DataFrame, max_days: int) -> int:
        days = max(int(max_days), self.config.data_days)
        if not signals.empty:
            span = (pd.Timestamp.now().normalize() - signals["date"].min()).days
            days = max(days, int(span * 1.45) + max(self.config.horizons) + 20)
        return days

    def run_from_history(
        self, history_path: str | Path = "data/signals_history.jsonl", max_days: int = 365
    ) -> StudyResult:
        signals = self._history_frame(self._read_history(Path(history_path)))
        if signals.empty:
            logger.warning("策略 {} 在历史中无买入信号", self.config.strategy_name)
            return self._empty_result()
        return self.run(signals, data_days=self._history_days(signals, max_days))

    def _prepare_strategy_frame(
        self, symbol: str, days: int, index: pd.DataFrame | None
    ) -> pd.DataFrame:
        frame = self._get_stock_data(symbol, days)
        if frame.empty or len(frame) < self.config.min_history_rows:
            return pd.DataFrame()
        if index is not None and not index.empty:
            index_close = index.set_index("date")["close"]
            frame["idx_close"] = frame["date"].map(index_close)
            frame["idx_ret"] = frame["idx_close"] / frame["idx_close"].shift(1) - 1
            frame["index_return"] = frame["idx_ret"].fillna(0.0)
        if self.config.enrich_func is not None:
            enriched = self.config.enrich_func(frame, symbol)
            frame = enriched[0] if isinstance(enriched, tuple) else enriched
            frame = _normalize_ohlcv(frame)
        return frame

    @staticmethod
    def _strategy_instance(strategy_class: type | Any) -> Any:
        if isinstance(strategy_class, type):
            return strategy_class()
        try:
            return copy.deepcopy(strategy_class)
        except Exception:
            return strategy_class.__class__(getattr(strategy_class, "params", None))

    def _strategy_events_for_symbol(
        self, strategy_class: type | Any, symbol: str, frame: pd.DataFrame
    ) -> list[dict[str, Any]]:
        try:
            signals = self._strategy_instance(strategy_class).generate_signals(
                frame.copy(), is_backtest=True
            )
        except Exception as exc:
            logger.debug("策略 {} 在 {} 上运行失败: {}", self.config.strategy_name, symbol, exc)
            return []
        if signals is None or signals.empty:
            return []
        events: list[dict[str, Any]] = []
        for position in self._buy_positions(signals, frame):
            events.append(
                {
                    "symbol": symbol,
                    "date": frame.iloc[position]["date"],
                    "entry": _finite_float(frame.iloc[position]["close"]) or 0.0,
                    "entry_source": "signal_close",
                }
            )
        return events

    def run_from_strategy(
        self,
        strategy_class: type | Any,
        stock_pool: list[str] | None = None,
        days: int | None = None,
    ) -> StudyResult:
        pool = stock_pool or self.config.stock_pool or _DEFAULT_POOL
        data_days = int(days or self.config.data_days)
        self._ensure_index_data(days=data_days)
        events: list[dict[str, Any]] = []
        for raw_symbol in pool:
            symbol = _normalize_symbol(raw_symbol)
            frame = self._prepare_strategy_frame(symbol, data_days, self._index_df)
            if frame.empty:
                continue
            events.extend(self._strategy_events_for_symbol(strategy_class, symbol, frame))
        if not events:
            logger.warning("策略 {} 在股票池中无买入信号", self.config.strategy_name)
            return self._empty_result()
        return self.run(pd.DataFrame(events), data_days=data_days)

    def _entry_for_row(
        self, row: pd.Series, stock_df: pd.DataFrame, signal_idx: int, exact_idx: int | None
    ) -> tuple[int | None, float | None, pd.Timestamp | None, str]:
        provided_date = _safe_timestamp(row.get("entry_date"))
        if not pd.isna(provided_date):
            entry_idx = self._find_signal_index(stock_df, provided_date)
            if entry_idx is None:
                return None, None, None, "fill"
            return entry_idx, float(row["entry"]), stock_df.iloc[entry_idx]["date"], "fill"
        if self.config.entry_mode == "next_open" and exact_idx is None:
            return (
                signal_idx,
                _finite_float(stock_df.iloc[signal_idx]["open"]),
                stock_df.iloc[signal_idx]["date"],
                "next_open",
            )
        return self._resolve_entry(
            stock_df, signal_idx, float(row["entry"]), str(row.get("entry_source", "signal"))
        )

    def _event_from_row(self, row: pd.Series, data_days: int) -> StudyEvent | None:
        symbol = str(row["symbol"])
        signal_timestamp = _naive_timestamp(row["date"])
        stock_df = self._get_stock_data(symbol, data_days)
        if stock_df.empty:
            return None
        exact_idx = self._find_exact_index(stock_df, signal_timestamp)
        signal_idx = (
            exact_idx
            if exact_idx is not None
            else self._find_signal_index(stock_df, signal_timestamp)
        )
        if signal_idx is None:
            return None
        entry_idx, entry_price, entry_date, entry_source = self._entry_for_row(
            row, stock_df, signal_idx, exact_idx
        )
        if entry_idx is None or entry_price is None or entry_date is None or entry_price <= 0:
            return None
        event = StudyEvent(
            symbol=symbol,
            date=signal_timestamp.strftime("%Y-%m-%d"),
            entry=entry_price,
            period=self._classify_period(signal_timestamp),
            year=signal_timestamp.year,
            entry_date=entry_date.strftime("%Y-%m-%d"),
            entry_source=entry_source,
        )
        for horizon in self.config.horizons:
            ret = self._forward_return(
                stock_df, signal_idx, horizon, entry_price, entry_idx=entry_idx
            )
            if ret is None:
                continue
            event.returns[str(horizon)] = ret
            excess = self._excess_return(entry_date, horizon, ret, entry_source=entry_source)
            if excess is not None:
                event.excess[str(horizon)] = excess
        return event if event.returns else None

    def run(self, signals_df: pd.DataFrame, *, data_days: int | None = None) -> StudyResult:
        required = {"symbol", "date", "entry"}
        missing = required - set(signals_df.columns)
        if missing:
            raise ValueError(f"信号 DataFrame 缺少列: {missing}")
        if signals_df.empty:
            return self._empty_result()
        frame = signals_df.copy()
        frame["symbol"] = frame["symbol"].map(_normalize_symbol)
        frame["date"] = frame["date"].map(_safe_timestamp)
        frame["entry"] = pd.to_numeric(frame["entry"], errors="coerce")
        frame = frame.dropna(subset=["symbol", "date", "entry"])
        frame = frame[frame["entry"] > 0].sort_values("date").reset_index(drop=True)
        if frame.empty:
            return self._empty_result()
        self._ensure_index_data(days=int(data_days or self.config.data_days))
        events = [
            event
            for _, row in frame.iterrows()
            if (event := self._event_from_row(row, int(data_days or self.config.data_days)))
            is not None
        ]
        if not events:
            return self._empty_result()
        concentration = self._check_concentration(
            events, threshold=self.config.concentration_threshold
        )
        return self._build_result(events, concentration)

    def _build_result(
        self, events: list[StudyEvent], concentration: list[dict[str, Any]]
    ) -> StudyResult:
        per_year = {
            year: self._aggregate_by_horizon([e for e in events if e.year == year])
            for year in sorted({e.year for e in events})
        }
        per_period = {
            period: self._aggregate_by_horizon([e for e in events if e.period == period])
            for period in ("train", "val", "outside")
        }
        return StudyResult(
            self.config.strategy_name,
            self.config,
            len(events),
            len({e.symbol for e in events}),
            concentration,
            self._aggregate_by_horizon(events),
            {k: v for k, v in per_year.items() if v},
            {k: v for k, v in per_period.items() if v},
            events,
            any(e.excess for e in events),
        )
