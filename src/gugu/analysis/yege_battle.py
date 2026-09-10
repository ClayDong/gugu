"""野哥主线爆发战法的可审计事件规则。

该模块只处理结构化证据，不联网、不猜测缺失字段。缺少板块、首板、
消息或中军数据时，结果必须是 insufficient/unavailable，而不是通过。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

RULE_VERSION = "yege-mainline-v1"
_VALID_MESSAGE_LEVELS = {"high", "medium", "low"}
_REQUIRED_CONTEXT_FIELDS = (
    "sector",
    "message_level",
    "message_source",
    "message_impact_score",
    "first_board_count",
    "first_board_window_minutes",
    "sector_index_new_high",
    "sector_volume_ratio",
    "sector_limit_up_count",
    "anchor_count",
    "max_anchor_market_cap_yi",
    "old_dragon_symbol",
    "old_dragon_anomaly",
)


@dataclass(frozen=True)
class YegeBurstEvent:
    """T 日主线爆发事件及其证据快照。"""

    event_id: str
    rule_version: str
    asof: str
    signal_date: str
    entry_assumption: str
    sector: str
    candidate_symbol: str
    message_level: str
    message_source: str
    message_impact_score: float
    sector_limit_up_count: int
    first_board_count: int
    first_board_window_minutes: int
    sector_index_new_high: bool
    sector_volume_ratio: float
    breakout_window: int
    anchor_count: int
    max_anchor_market_cap_yi: float
    anchor_negative_feedback: bool
    old_dragon_symbol: str
    old_dragon_anomaly: bool
    board_role: str
    evidence_status: str
    data_quality: str
    passed: bool
    reasons: tuple[str, ...]
    signal_close: float | None = None
    rule_status: str = "insufficient_evidence"

    def to_dict(self) -> dict[str, Any]:
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in self.__dict__.items()
        }


def _missing(value: Any) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return True
    try:
        marker = pd.isna(value)
        if not hasattr(marker, "__len__"):
            return bool(marker)
    except (TypeError, ValueError):
        pass
    return False


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def build_context(raw: dict[str, Any] | None) -> dict[str, Any]:
    """标准化策略上下文，保留证据状态并拒绝隐式通过。"""
    context = dict(raw or {})
    context.setdefault("rule_version", RULE_VERSION)
    context.setdefault("asof", datetime.now().isoformat(timespec="seconds"))
    context.setdefault("entry_assumption", "next_session_open")
    context.setdefault("evidence_status", "unavailable")
    context.setdefault("data_quality", "unknown")
    return context


def audit_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """审计野哥事件输入，不将字段存在误认为证据有效。"""
    normalized = build_context(context)
    missing = [field for field in _REQUIRED_CONTEXT_FIELDS if _missing(normalized.get(field))]
    quality = str(normalized.get("data_quality", "unknown"))
    status = str(normalized.get("evidence_status", "unavailable"))
    reasons: list[str] = []
    if missing:
        reasons.append(f"缺少字段: {','.join(missing)}")
    if status != "available":
        reasons.append(f"证据状态={status}")
    if quality not in {"fresh_valid", "verified", "available"}:
        reasons.append(f"数据质量={quality}")
    if normalized.get("entry_assumption") != "next_session_open":
        reasons.append("入场假设不是次日开盘")
    if normalized.get("rule_version") != RULE_VERSION:
        reasons.append("规则版本不匹配")
    return {
        "evidence_status": "available" if not reasons else "insufficient_evidence",
        "data_quality": quality,
        "missing_fields": missing,
        "reasons": reasons,
        "ready": not reasons,
    }


def derive_burst_context(
    *,
    sector: str,
    message: dict[str, Any] | None,
    first_board_rows: list[dict[str, Any]] | None,
    sector_index: pd.DataFrame | None,
    anchor_rows: list[dict[str, Any]] | None,
    old_dragon: dict[str, Any] | None,
    signal_date: str,
    candidate_symbol: str,
    breakout_window: int = 20,
) -> dict[str, Any]:
    """从原始结构化证据推导野哥事件上下文。

    所有输入都必须来自 T 日及之前；板块指数 DataFrame 需按日期升序，
    且包含 ``date``, ``close``, ``volume``。缺列或无历史窗口时返回
    ``insufficient_evidence``，不使用单股价格替代板块证据。
    """
    rows = first_board_rows or []
    signal_dt = pd.to_datetime(signal_date, errors="coerce")
    future_evidence = pd.isna(signal_dt)
    for row in rows:
        row_date = pd.to_datetime(row.get("date"), errors="coerce")
        if pd.notna(row_date) and row_date > signal_dt:
            future_evidence = True
    index = sector_index.copy() if sector_index is not None else pd.DataFrame()
    required_index = {"date", "close", "volume"}
    index_ok = required_index <= set(index.columns)
    if index_ok:
        index["date"] = pd.to_datetime(index["date"], errors="coerce")
        future_evidence = future_evidence or bool((index["date"] > signal_dt).any())
        index = index[index["date"] <= signal_dt]
        index = index.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    index_ok = index_ok and len(index) >= breakout_window + 1 and not future_evidence
    sector_volume_ratio = None
    sector_index_new_high = None
    if index_ok:
        prior = index["close"].iloc[-breakout_window - 1 : -1]
        latest_close = float(index["close"].iloc[-1])
        prior_high = float(prior.max()) if len(prior) else 0.0
        volume_ma = float(index["volume"].iloc[-breakout_window - 1 : -1].mean())
        sector_index_new_high = latest_close >= prior_high > 0
        sector_volume_ratio = (
            latest_close and float(index["volume"].iloc[-1]) / volume_ma if volume_ma > 0 else None
        )
    message = message or {}
    anchor_rows = anchor_rows or []
    max_cap = max(
        (
            float(row.get("market_cap_yi", row.get("float_market_cap_yi", 0)) or 0)
            for row in anchor_rows
        ),
        default=0.0,
    )
    context = {
        "evidence_status": "available"
        if index_ok and message and rows and anchor_rows and old_dragon
        else "insufficient_evidence",
        "data_quality": "fresh_valid" if index_ok else "insufficient",
        "sector": sector,
        "message_level": message.get("message_level", ""),
        "message_source": message.get("source", message.get("message_source", "")),
        "message_impact_score": message.get("impact_score", message.get("message_impact_score")),
        "first_board_count": len(rows),
        "sector_limit_up_count": int(message.get("sector_limit_up_count", 0) or 0),
        "first_board_window_minutes": max(
            (int(row.get("window_minutes", 0) or 0) for row in rows), default=0
        ),
        "sector_index_new_high": sector_index_new_high,
        "sector_volume_ratio": sector_volume_ratio,
        "anchor_count": len(anchor_rows),
        "max_anchor_market_cap_yi": max_cap,
        "anchor_negative_feedback": any(bool(row.get("negative_feedback")) for row in anchor_rows),
        "old_dragon_symbol": str((old_dragon or {}).get("symbol", "")),
        "old_dragon_anomaly": bool((old_dragon or {}).get("anomaly", False)),
        "candidate_symbol": candidate_symbol,
        "signal_date": signal_date,
        "breakout_window": breakout_window,
        "board_role": (old_dragon or {}).get("candidate_role", ""),
        "entry_assumption": "next_session_open",
        "rule_version": RULE_VERSION,
    }
    return context


def audit_event_records(records: list[dict[str, Any]] | None) -> dict[str, Any]:
    """批量审计事件证据覆盖率，不计算收益也不改变准入状态。"""
    rows = records or []
    audits = [audit_context(row) for row in rows]
    missing_counts: dict[str, int] = {}
    for result in audits:
        for field in result["missing_fields"]:
            missing_counts[field] = missing_counts.get(field, 0) + 1
    return {
        "event_count": len(rows),
        "ready_count": sum(1 for result in audits if result["ready"]),
        "insufficient_evidence_count": sum(
            1 for result in audits if result["evidence_status"] != "available"
        ),
        "missing_field_counts": missing_counts,
        "status": "available"
        if rows and all(result["ready"] for result in audits)
        else "insufficient_evidence",
    }


def detect_mainline_burst(
    context: dict[str, Any] | None,
    *,
    candidate_symbol: str = "",
    signal_date: str = "",
    event_id: str = "",
    min_first_board_count: int = 3,
    max_first_board_count: int = 5,
    min_message_level: str = "high",
    min_anchor_market_cap_yi: float = 100.0,
    min_sector_volume_ratio: float = 1.2,
    breakout_window: int = 20,
) -> dict[str, Any]:
    """检测 T 日主线爆发五关，缺证据时 fail-closed。"""
    ctx = build_context(context)
    context_audit = audit_context(ctx)
    level = str(ctx.get("message_level", "")).lower()
    first_count = _as_int(ctx.get("first_board_count"))
    window = _as_int(ctx.get("first_board_window_minutes"))
    sector_count = _as_int(ctx.get("sector_limit_up_count"))
    sector = str(ctx.get("sector", ""))
    source = str(ctx.get("message_source", ""))
    candidate_role = str(ctx.get("board_role", ""))
    reasons: list[str] = []

    required = {
        "sector": sector,
        "message_level": level,
        "message_source": source,
        "message_impact_score": ctx.get("message_impact_score"),
        "first_board_count": ctx.get("first_board_count"),
        "first_board_window_minutes": ctx.get("first_board_window_minutes"),
        "sector_index_new_high": ctx.get("sector_index_new_high"),
        "sector_volume_ratio": ctx.get("sector_volume_ratio"),
        "sector_limit_up_count": ctx.get("sector_limit_up_count"),
        "anchor_count": ctx.get("anchor_count"),
        "max_anchor_market_cap_yi": ctx.get("max_anchor_market_cap_yi"),
        "old_dragon_symbol": ctx.get("old_dragon_symbol"),
        "old_dragon_anomaly": ctx.get("old_dragon_anomaly"),
        "candidate_symbol": candidate_symbol,
        "board_role": candidate_role,
    }
    missing = [key for key, value in required.items() if _missing(value)]
    if missing:
        reasons.append(f"缺少证据: {','.join(missing)}")

    if context_audit["reasons"]:
        reasons.extend(str(reason) for reason in context_audit["reasons"])
    if str(ctx.get("entry_assumption", "next_session_open")) != "next_session_open":
        reasons.append("入场假设不是次日开盘")
    if _as_float(ctx.get("message_impact_score")) <= 0:
        reasons.append("消息影响分数无效")
    if breakout_window <= 0:
        reasons.append("突破观察窗口必须大于0")
    message_rank = {"low": 1, "medium": 2, "high": 3}
    if level not in _VALID_MESSAGE_LEVELS or message_rank.get(level, 0) < message_rank.get(
        min_message_level, 3
    ):
        reasons.append(f"消息级别不足: {level or 'unknown'}")
    if not min_first_board_count <= first_count <= max_first_board_count:
        reasons.append(f"早盘首板数不在{min_first_board_count}-{max_first_board_count}范围")
    if sector_count < first_count:
        reasons.append("板块涨停总数少于首板数，数据不一致")
    if not 3 <= window <= 60:
        reasons.append("首板爆发不在开盘3-60分钟窗口")
    if not bool(ctx.get("sector_index_new_high")):
        reasons.append("板块指数未突破观察窗口新高")
    if _as_float(ctx.get("sector_volume_ratio")) < min_sector_volume_ratio:
        reasons.append("板块指数量能不足")
    if _as_int(ctx.get("anchor_count")) < 1:
        reasons.append("缺少容量中军")
    if _as_float(ctx.get("max_anchor_market_cap_yi")) < min_anchor_market_cap_yi:
        reasons.append("中军市值低于百亿门槛")
    if bool(ctx.get("anchor_negative_feedback")):
        reasons.append("中军出现负反馈")
    if not bool(ctx.get("old_dragon_anomaly")):
        reasons.append("老龙异动证据不足")
    if candidate_role not in {"space_dragon", "core", "leader_candidate"}:
        reasons.append("候选股不是核心/空间龙")

    raw_status = str(ctx.get("evidence_status", "unavailable"))
    status = (
        raw_status
        if raw_status in {"unavailable", "source_unavailable"}
        else "available"
        if context_audit["ready"]
        else "insufficient_evidence"
    )
    passed = not reasons
    event = YegeBurstEvent(
        event_id=event_id or f"{RULE_VERSION}:{signal_date}:{candidate_symbol}",
        rule_version=str(ctx.get("rule_version", RULE_VERSION)),
        asof=str(ctx.get("asof", "")),
        signal_date=signal_date or str(ctx.get("signal_date", "")),
        entry_assumption=str(ctx.get("entry_assumption", "next_session_open")),
        sector=sector,
        candidate_symbol=candidate_symbol,
        message_level=level,
        message_source=source,
        message_impact_score=_as_float(ctx.get("message_impact_score")),
        sector_limit_up_count=_as_int(ctx.get("sector_limit_up_count")),
        first_board_count=first_count,
        first_board_window_minutes=window,
        sector_index_new_high=bool(ctx.get("sector_index_new_high")),
        sector_volume_ratio=_as_float(ctx.get("sector_volume_ratio")),
        breakout_window=_as_int(ctx.get("breakout_window"), breakout_window),
        anchor_count=_as_int(ctx.get("anchor_count")),
        max_anchor_market_cap_yi=_as_float(ctx.get("max_anchor_market_cap_yi")),
        anchor_negative_feedback=bool(ctx.get("anchor_negative_feedback")),
        old_dragon_symbol=str(ctx.get("old_dragon_symbol", "")),
        old_dragon_anomaly=bool(ctx.get("old_dragon_anomaly")),
        board_role=candidate_role,
        evidence_status=status,
        data_quality=str(ctx.get("data_quality", "unknown")),
        passed=passed,
        reasons=tuple(reasons),
        signal_close=_positive_float(ctx.get("signal_close")),
        rule_status=(
            "passed"
            if passed
            else "insufficient_evidence"
            if not context_audit["ready"]
            else "failed"
        ),
    )
    return event.to_dict()


def validate_next_day(
    event: dict[str, Any],
    *,
    next_open: float | None,
    next_high: float | None,
    next_low: float | None,
    next_close: float | None,
    first_board_survival_rate: float | None,
    anchor_negative_feedback: bool = False,
    min_open_premium: float = 0.0,
    min_survival_rate: float = 0.5,
    benchmark_open_return: float | None = None,
    benchmark_close_return: float | None = None,
) -> dict[str, Any]:
    """用 T+1 可执行行情确认主线，不把缺失值当作通过。"""
    result = dict(event)
    entry_price = next_open
    signal_close = _as_float(event.get("signal_close"), 0.0)
    missing = any(value is None for value in (next_open, next_high, next_low, next_close))
    invalid_prices = any(
        value is not None and _positive_float(value) is None
        for value in (next_open, next_high, next_low, next_close)
    )
    if _positive_float(event.get("signal_close")) is None:
        invalid_prices = True
    open_premium = None
    close_premium = None
    next_session_return = None
    if entry_price and entry_price > 0 and signal_close > 0:
        open_premium = round(entry_price / signal_close - 1, 6)
        close_premium = round(_as_float(next_close) / signal_close - 1, 6)
    if _positive_float(next_open) is not None and _positive_float(next_close) is not None:
        next_session_return = round(float(next_close) / float(next_open) - 1, 6)
    excess_open_return = (
        round(open_premium - benchmark_open_return, 6)
        if open_premium is not None and benchmark_open_return is not None
        else None
    )
    excess_close_return = (
        round(close_premium - benchmark_close_return, 6)
        if close_premium is not None and benchmark_close_return is not None
        else None
    )
    reasons = list(event.get("reasons", []))
    event_passed = event.get("passed") is True
    event_evidence_insufficient = (
        event.get("rule_status") == "insufficient_evidence"
        or event.get("evidence_status") != "available"
    )
    if not event_passed:
        reasons.append("T日主线爆发规则未通过")
    if missing:
        reasons.append("T+1行情缺失")
    if invalid_prices:
        reasons.append("T日或T+1价格无效")
    if open_premium is not None and open_premium < min_open_premium:
        reasons.append("次日开盘溢价不足")
    survival_missing = first_board_survival_rate is None
    if survival_missing:
        reasons.append("首板活口留存率缺失")
    elif first_board_survival_rate < min_survival_rate:
        reasons.append("首板活口留存率不足")
    if anchor_negative_feedback:
        reasons.append("次日中军负反馈")
    result.update(
        {
            "next_date": result.get("next_date", ""),
            "entry_assumption": "next_session_open",
            "entry_price_source": "next_open" if next_open is not None else "missing",
            "next_open": next_open,
            "next_high": next_high,
            "next_low": next_low,
            "next_close": next_close,
            "open_premium": open_premium,
            "close_premium": close_premium,
            "next_session_return": next_session_return,
            "benchmark_open_return": benchmark_open_return,
            "benchmark_close_return": benchmark_close_return,
            "excess_open_return": excess_open_return,
            "excess_close_return": excess_close_return,
            "first_board_survival_rate": first_board_survival_rate,
            "anchor_negative_feedback_t1": anchor_negative_feedback,
            "validated": not reasons,
            "validation_status": (
                "available"
                if (
                    not missing
                    and not invalid_prices
                    and not survival_missing
                    and event_passed
                    and not reasons
                )
                else "insufficient_evidence"
                if missing or invalid_prices or survival_missing or event_evidence_insufficient
                else "rule_failed"
            ),
            "reasons": reasons,
        }
    )
    return result
