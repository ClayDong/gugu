"""野哥主线爆发战法的可审计事件规则。

该模块只处理结构化证据，不联网、不猜测缺失字段。缺少板块、首板、
消息或中军数据时，结果必须是 insufficient/unavailable，而不是通过。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

RULE_VERSION = "yege-mainline-v1"
_VALID_MESSAGE_LEVELS = {"high", "medium", "low"}


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

    def to_dict(self) -> dict[str, Any]:
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in self.__dict__.items()
        }


def _missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_context(raw: dict[str, Any] | None) -> dict[str, Any]:
    """标准化策略上下文，保留证据状态并拒绝隐式通过。"""
    context = dict(raw or {})
    context.setdefault("rule_version", RULE_VERSION)
    context.setdefault("asof", datetime.now().isoformat(timespec="seconds"))
    context.setdefault("entry_assumption", "next_session_open")
    context.setdefault("evidence_status", "unavailable")
    context.setdefault("data_quality", "unknown")
    return context


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
    level = str(ctx.get("message_level", "")).lower()
    first_count = _as_int(ctx.get("first_board_count"))
    window = _as_int(ctx.get("first_board_window_minutes"))
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
        "anchor_count": ctx.get("anchor_count"),
        "max_anchor_market_cap_yi": ctx.get("max_anchor_market_cap_yi"),
        "old_dragon_anomaly": ctx.get("old_dragon_anomaly"),
        "candidate_symbol": candidate_symbol,
        "board_role": candidate_role,
    }
    missing = [key for key, value in required.items() if _missing(value)]
    if missing:
        reasons.append(f"缺少证据: {','.join(missing)}")

    if ctx.get("evidence_status") != "available":
        reasons.append(f"证据状态={ctx.get('evidence_status')}")
    message_rank = {"low": 1, "medium": 2, "high": 3}
    if level not in _VALID_MESSAGE_LEVELS or message_rank.get(level, 0) < message_rank.get(
        min_message_level, 3
    ):
        reasons.append(f"消息级别不足: {level or 'unknown'}")
    if not min_first_board_count <= first_count <= max_first_board_count:
        reasons.append(f"早盘首板数不在{min_first_board_count}-{max_first_board_count}范围")
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

    status = (
        "available"
        if ctx.get("evidence_status") == "available"
        else str(ctx.get("evidence_status"))
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
) -> dict[str, Any]:
    """用 T+1 可执行行情确认主线，不把缺失值当作通过。"""
    result = dict(event)
    entry_price = next_open
    signal_close = _as_float(event.get("signal_close"), 0.0)
    missing = any(value is None for value in (next_open, next_high, next_low, next_close))
    open_premium = None
    close_premium = None
    if entry_price and entry_price > 0 and signal_close > 0:
        open_premium = round(entry_price / signal_close - 1, 6)
        close_premium = round(_as_float(next_close) / signal_close - 1, 6)
    reasons = list(event.get("reasons", []))
    if missing:
        reasons.append("T+1行情缺失")
    if open_premium is not None and open_premium < min_open_premium:
        reasons.append("次日开盘溢价不足")
    if first_board_survival_rate is None:
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
            "first_board_survival_rate": first_board_survival_rate,
            "anchor_negative_feedback_t1": anchor_negative_feedback,
            "validated": not reasons,
            "validation_status": "available" if not missing else "insufficient_evidence",
            "reasons": reasons,
        }
    )
    return result
