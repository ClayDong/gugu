"""陈学长断板反包 CXBR V1 研究层实现。

本模块只实现可审计的日线硬规则，定位为 ``research_only``：
- 不注册到 ``gugu.strategies.registry``；
- 不修改 ``strategy_manifest.yaml``；
- 不调用下单接口；
- 缺少真实涨停价、流通市值或状态元数据时 fail-closed。

V1 不声称复刻分时承接、急跌拉回、尾盘偷袭等主观软条件。
``conditional_stop_price`` 仅表示券商条件单提醒参数，不是回测成交价。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

CXBRAction = Literal["buy", "sell", "observe", "none"]

_REQUIRED_SCAN_COLUMNS = {
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "limit_up_price",
    "turnover_pct",
}
_REQUIRED_EXIT_COLUMNS = {"date", "open", "high", "low", "close", "volume"}


class CXBRDataError(ValueError):
    """CXBR 输入数据不满足研究层契约。"""


@dataclass(frozen=True)
class CXBRConfig:
    """CXBR V1 参数；未验证参数不会自动进入生产。"""

    min_circulate_mv: float = 4_000_000_000.0
    max_circulate_mv: float = 5_000_000_000.0
    max_protect_window_day: int = 5
    min_listing_days: int = 60
    ma_period: int = 10
    effective_break_coeff: float = 0.97
    cond_order_offset_coeff: float = 0.96
    buy_b_vol_multiple: float = 1.3
    max_position: float = 0.20
    wash_window_days: int = 5
    wash_min_body_drop: float = 0.03
    wash_min_volume_multiple: float = 1.5
    limit_up_tolerance: float = 0.001
    turnover_min_pct: float | None = None
    turnover_max_pct: float | None = None
    take_profit_pct: float | None = None
    time_stop_days: int | None = None
    time_stop_min_return: float = 0.0

    def __post_init__(self) -> None:
        if self.min_circulate_mv <= 0 or self.min_circulate_mv >= self.max_circulate_mv:
            raise ValueError("流通市值范围必须为正且 min < max")
        if self.max_protect_window_day < 1 or self.wash_window_days < 1:
            raise ValueError("窗口天数必须大于 0")
        if self.min_listing_days < self.ma_period:
            raise ValueError("min_listing_days 不能小于 ma_period")
        if self.ma_period < 2:
            raise ValueError("ma_period 必须至少为 2")
        if not 0 < self.effective_break_coeff < 1:
            raise ValueError("effective_break_coeff 必须在 0 和 1 之间")
        if not 0 < self.cond_order_offset_coeff < 1:
            raise ValueError("cond_order_offset_coeff 必须在 0 和 1 之间")
        if self.buy_b_vol_multiple < 1:
            raise ValueError("buy_b_vol_multiple 必须 >= 1")
        if not 0 < self.max_position <= 1:
            raise ValueError("max_position 必须在 0 和 1 之间")
        if self.turnover_min_pct is not None and self.turnover_min_pct < 0:
            raise ValueError("turnover_min_pct 不能为负")
        if self.turnover_max_pct is not None and self.turnover_max_pct <= (
            self.turnover_min_pct or 0
        ):
            raise ValueError("turnover_max_pct 必须大于 turnover_min_pct")
        if self.take_profit_pct is not None and self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct 必须为正")
        if self.time_stop_days is not None and self.time_stop_days < 1:
            raise ValueError("time_stop_days 必须大于 0")


@dataclass(frozen=True)
class CXBRPosition:
    """供退出提醒使用的持仓事实，不代表真实券商持仓。"""

    entry_date: Any
    entry_price: float
    first_board_open: float


@dataclass(frozen=True)
class CXBRSignal:
    """可落库、可通知的 CXBR V1 结果。"""

    action: CXBRAction
    stage: str
    code: str
    asof: str | None
    reason: str
    buy_point: str | None = None
    first_board_date: str | None = None
    wash_date: str | None = None
    ma10: float | None = None
    strict_stop_price: float | None = None
    conditional_stop_price: float | None = None
    suggested_position: float | None = None
    data_quality: str = "fresh_valid"
    strategy_name: str = "cxbr_v1"
    rule_version: str = "cxbr-v1"
    entry_assumption: str = "next_session_open"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _date_text(value: Any) -> str:
    return str(pd.Timestamp(value).date())


def _signal(
    action: CXBRAction,
    stage: str,
    code: str,
    asof: Any | None,
    reason: str,
    **kwargs: Any,
) -> CXBRSignal:
    return CXBRSignal(
        action=action,
        stage=stage,
        code=code,
        asof=None if asof is None else _date_text(asof),
        reason=reason,
        **kwargs,
    )


def _validate_metadata(metadata: Mapping[str, Any]) -> tuple[float, str | None]:
    required = {"float_mv", "is_st", "is_suspended", "listing_date"}
    missing = sorted(required - set(metadata))
    if missing:
        raise CXBRDataError(f"缺少元数据: {','.join(missing)}")
    try:
        float_mv = float(metadata["float_mv"])
    except (TypeError, ValueError) as exc:
        raise CXBRDataError("float_mv 必须为有限正数") from exc
    if not np.isfinite(float_mv) or float_mv <= 0:
        raise CXBRDataError("float_mv 必须为有限正数")
    if bool(metadata["is_st"]):
        raise CXBRDataError("ST 股票不参与 CXBR V1")
    if bool(metadata["is_suspended"]):
        raise CXBRDataError("停牌股票不参与 CXBR V1")
    try:
        listing_date = _date_text(metadata["listing_date"])
    except (TypeError, ValueError) as exc:
        raise CXBRDataError("listing_date 无法解析") from exc
    return float_mv, listing_date


def _prepare_scan_frame(df: pd.DataFrame, config: CXBRConfig) -> pd.DataFrame:
    missing = sorted(_REQUIRED_SCAN_COLUMNS - set(df.columns))
    if missing:
        raise CXBRDataError(f"缺少日线字段: {','.join(missing)}")
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    if out["date"].isna().any() or out["date"].duplicated().any():
        raise CXBRDataError("date 必须全部有效且不可重复")
    out = out.sort_values("date").reset_index(drop=True)
    numeric = ["open", "high", "low", "close", "volume", "limit_up_price", "turnover_pct"]
    for column in numeric:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if not np.isfinite(out[numeric].to_numpy(dtype=float)).all():
        raise CXBRDataError("日线字段包含 NaN 或非有限值")
    if (out[["open", "high", "low", "close", "volume"]] <= 0).any().any():
        raise CXBRDataError("OHLCV 必须为正数")
    out["ma"] = out["close"].rolling(config.ma_period, min_periods=config.ma_period).mean()
    out["prior_vol_ma5"] = out["volume"].shift(1).rolling(5, min_periods=5).mean()
    out["is_limit_up"] = out["close"] >= out["limit_up_price"] * (1 - config.limit_up_tolerance)
    out["body_drop"] = (out["open"] - out["close"]) / out["open"]
    return out


def _check_turnover(value: float, config: CXBRConfig) -> bool:
    if config.turnover_min_pct is not None and value < config.turnover_min_pct:
        return False
    return not (config.turnover_max_pct is not None and value > config.turnover_max_pct)


def _is_one_word(row: pd.Series) -> bool:
    amplitude = row["high"] / row["low"] - 1
    return bool(amplitude < 0.01 and abs(row["open"] - row["high"]) <= row["high"] * 0.001)


def _first_board_candidates(frame: pd.DataFrame, config: CXBRConfig) -> list[int]:
    candidates: list[int] = []
    for index, row in frame.iterrows():
        if not row["is_limit_up"] or _is_one_word(row):
            continue
        if not _check_turnover(float(row["turnover_pct"]), config):
            continue
        previous = frame.iloc[max(0, index - 3) : index]
        if previous["is_limit_up"].any():
            continue
        next_two = frame.iloc[index + 1 : index + 3]
        if len(next_two) and next_two["is_limit_up"].any():
            continue
        candidates.append(index)
    return candidates


def _find_wash(frame: pd.DataFrame, first_index: int, config: CXBRConfig) -> int | None:
    first_volume = float(frame.iloc[first_index]["volume"])
    end = min(len(frame), first_index + config.wash_window_days + 1)
    for index in range(first_index + 1, end):
        row = frame.iloc[index]
        if (
            row["close"] < row["open"]
            and row["body_drop"] >= config.wash_min_body_drop
            and row["volume"] >= first_volume * config.wash_min_volume_multiple
        ):
            return index
    return None


def _effective_break(frame: pd.DataFrame, start: int, end: int, config: CXBRConfig) -> int | None:
    below_count = 0
    for index in range(start, end + 1):
        row = frame.iloc[index]
        ma = float(row["ma"])
        if not np.isfinite(ma):
            below_count = 0
            continue
        if row["close"] <= ma * config.effective_break_coeff:
            return index
        if row["close"] < ma:
            below_count += 1
            if below_count >= 2:
                return index
        else:
            below_count = 0
    return None


def scan(
    code: str,
    df: pd.DataFrame,
    metadata: Mapping[str, Any],
    config: CXBRConfig | None = None,
) -> CXBRSignal:
    """扫描截至最后一根日线的 CXBR V1 硬规则信号。

    任何数据契约失败都返回 ``data_quality=unavailable``，不会降级为买入。
    """
    config = config or CXBRConfig()
    try:
        float_mv, listing_date = _validate_metadata(metadata)
        frame = _prepare_scan_frame(df, config)
        asof = frame.iloc[-1]["date"]
        if (asof - pd.Timestamp(listing_date)).days < config.min_listing_days:
            raise CXBRDataError("上市时间不足，无法确认横盘基座")
        if str(code).startswith(("300", "688", "8", "4")):
            raise CXBRDataError("创业板、科创板或北交所代码不参与 V1")
        if not config.min_circulate_mv <= float_mv <= config.max_circulate_mv:
            raise CXBRDataError("流通市值不在 CXBR V1 范围")
    except CXBRDataError as exc:
        return _signal("none", "unavailable", str(code), None, str(exc), data_quality="unavailable")

    asof = frame.iloc[-1]["date"]
    if len(frame) < config.ma_period + 6:
        return _signal(
            "none", "insufficient_history", code, asof, "历史数据不足", data_quality="unavailable"
        )

    candidates = _first_board_candidates(frame, config)
    for first_index in reversed(candidates):
        if first_index >= len(frame) - 1:
            continue
        first = frame.iloc[first_index]
        wash_index = _find_wash(frame, first_index, config)
        if wash_index is None:
            continue
        last_index = len(frame) - 1
        protect_start = wash_index + 1
        if protect_start > last_index:
            return _signal(
                "observe",
                "wash",
                code,
                asof,
                "已识别手板和断板，等待护盘确认",
                first_board_date=_date_text(first["date"]),
                wash_date=_date_text(frame.iloc[wash_index]["date"]),
            )
        break_index = _effective_break(frame, protect_start, last_index, config)
        if break_index is not None:
            return _signal(
                "none",
                "broken",
                code,
                asof,
                "有效跌破 MA10，形态失效",
                first_board_date=_date_text(first["date"]),
                wash_date=_date_text(frame.iloc[wash_index]["date"]),
                ma10=float(frame.iloc[break_index]["ma"]),
            )
        if last_index - first_index > config.max_protect_window_day:
            return _signal(
                "none",
                "expired",
                code,
                asof,
                "手板后护盘窗口已过期且未出现买点",
                first_board_date=_date_text(first["date"]),
                wash_date=_date_text(frame.iloc[wash_index]["date"]),
            )
        row = frame.iloc[last_index]
        ma = float(row["ma"])
        if not np.isfinite(ma):
            return _signal("observe", "waiting", code, asof, "MA10 尚未形成")
        buy_a = bool(ma <= row["low"] <= ma * 1.02 and row["close"] > ma)
        buy_b = bool(
            row["close"] > first["close"]
            and np.isfinite(row["prior_vol_ma5"])
            and row["volume"] >= row["prior_vol_ma5"] * config.buy_b_vol_multiple
        )
        if buy_a or buy_b:
            buy_point = "+".join(
                point for point, enabled in (("A", buy_a), ("B", buy_b)) if enabled
            )
            return _signal(
                "buy",
                "entry",
                code,
                asof,
                "日线硬规则买点成立，等待人工/分时复核",
                buy_point=buy_point,
                first_board_date=_date_text(first["date"]),
                wash_date=_date_text(frame.iloc[wash_index]["date"]),
                ma10=ma,
                strict_stop_price=ma * config.effective_break_coeff,
                conditional_stop_price=ma * config.cond_order_offset_coeff,
                suggested_position=config.max_position,
            )
        return _signal(
            "observe",
            "protect",
            code,
            asof,
            "护盘窗口内，尚无 A/B 买点",
            first_board_date=_date_text(first["date"]),
            wash_date=_date_text(frame.iloc[wash_index]["date"]),
            ma10=ma,
            strict_stop_price=ma * config.effective_break_coeff,
            conditional_stop_price=ma * config.cond_order_offset_coeff,
        )
    return _signal("none", "no_pattern", code, asof, "未识别到有效 CXBR V1 形态")


def evaluate_exit(
    code: str,
    df: pd.DataFrame,
    position: CXBRPosition,
    config: CXBRConfig | None = None,
) -> CXBRSignal:
    """根据已给出的持仓事实生成卖出/观察提醒，不执行交易。"""
    config = config or CXBRConfig()
    try:
        missing = sorted(_REQUIRED_EXIT_COLUMNS - set(df.columns))
        if missing:
            raise CXBRDataError(f"缺少退出判断字段: {','.join(missing)}")
        frame = df.copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        for column in _REQUIRED_EXIT_COLUMNS - {"date"}:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if frame["date"].isna().any() or frame.duplicated("date").any():
            raise CXBRDataError("退出判断 date 无效或重复")
        frame = frame.sort_values("date").reset_index(drop=True)
        if not np.isfinite(
            frame[list(_REQUIRED_EXIT_COLUMNS - {"date"})].to_numpy(dtype=float)
        ).all():
            raise CXBRDataError("退出判断字段包含 NaN 或非有限值")
        frame["ma"] = frame["close"].rolling(config.ma_period, min_periods=config.ma_period).mean()
        entry_date = pd.Timestamp(position.entry_date)
        matches = frame.index[frame["date"] == entry_date].tolist()
        if not matches:
            raise CXBRDataError("持仓 entry_date 不在行情数据中")
        entry_index = matches[0]
        last_index = len(frame) - 1
        if last_index <= entry_index:
            raise CXBRDataError("持仓后尚无可判断行情")
    except (CXBRDataError, TypeError, ValueError) as exc:
        return _signal("none", "unavailable", code, None, str(exc), data_quality="unavailable")

    last = frame.iloc[last_index]
    ma = float(last["ma"])
    if not np.isfinite(ma):
        return _signal("observe", "waiting", code, last["date"], "MA10 尚未形成")
    below_ma = (
        frame.iloc[entry_index + 1 : last_index + 1]["close"]
        < frame.iloc[entry_index + 1 : last_index + 1]["ma"]
    )
    hard_stop = bool(last["close"] <= ma * config.effective_break_coeff)
    consecutive_break = bool(below_ma.iloc[-1] and len(below_ma) >= 2 and below_ma.iloc[-2])
    conditional_stop = ma * config.cond_order_offset_coeff
    if hard_stop or consecutive_break:
        reason = "单日收盘跌破 MA10×有效跌破系数" if hard_stop else "连续两日收盘低于 MA10"
        return _signal(
            "sell",
            "exit",
            code,
            last["date"],
            reason,
            ma10=ma,
            strict_stop_price=ma * config.effective_break_coeff,
            conditional_stop_price=conditional_stop,
        )
    return_pct = float(last["close"] / position.entry_price - 1)
    hold_days = last_index - entry_index
    if config.take_profit_pct is not None and return_pct >= config.take_profit_pct:
        return _signal("sell", "take_profit", code, last["date"], "达到配置的止盈阈值", ma10=ma)
    if (
        config.time_stop_days is not None
        and hold_days >= config.time_stop_days
        and return_pct < config.time_stop_min_return
    ):
        return _signal("sell", "time_stop", code, last["date"], "达到时间止损条件", ma10=ma)
    return _signal(
        "observe",
        "holding",
        code,
        last["date"],
        "未触发 V1 卖出条件",
        ma10=ma,
        strict_stop_price=ma * config.effective_break_coeff,
        conditional_stop_price=conditional_stop,
    )


__all__ = [
    "CXBRAction",
    "CXBRConfig",
    "CXBRDataError",
    "CXBRPosition",
    "CXBRSignal",
    "evaluate_exit",
    "scan",
]
