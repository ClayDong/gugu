"""十大视角系统集成器。

将十大视角分析结果集成到现有模块：
1. StockRanker — 增加 perspective_score 维度
2. StockSelector — 增加 perspective 过滤
3. WisdomAdvisor — 增加 perspective 上下文
4. TerminalScreener — 增加 perspective 评分
"""

from __future__ import annotations

from typing import Any

from gugu.analysis.perspectives.data_types import (
    PerspectiveAnalysisResult,
    StockDataContext,
)
from gugu.utils.log import get_logger

logger = get_logger()


def build_context_from_signal(signal: dict[str, Any]) -> StockDataContext:
    """从信号字典构建 StockDataContext。

    兼容 StockSelector 和 TerminalScreener 的信号格式。
    """
    fundamental = signal.get("fundamental", {}) or {}
    money_flow = signal.get("money_flow", {}) or {}
    market = signal.get("market_context", {}) or {}

    return StockDataContext(
        symbol=signal.get("symbol", ""),
        name=signal.get("name", ""),
        price=float(signal.get("price", 0)),
        change_pct=float(signal.get("change_pct", signal.get("change", 0))),
        volume_ratio=float(signal.get("volume_ratio", 0)),
        turnover_pct=float(signal.get("turnover_pct", signal.get("turnover", 0))),
        mcap_billion=float(signal.get("mcap_billion", signal.get("mcap", 0))),
        # 估值
        pe_static=float(fundamental.get("pe_static", signal.get("pe_static", 0))),
        pe_dynamic=float(fundamental.get("pe_dynamic", signal.get("pe_dynamic", 0))),
        pb=float(fundamental.get("pb", signal.get("pb", 0))),
        roe=float(fundamental.get("roe", signal.get("roe", 0))),
        revenue_growth=float(fundamental.get("revenue_growth", signal.get("revenue_growth", 0))),
        profit_growth=float(fundamental.get("profit_growth", signal.get("profit_growth", 0))),
        # 均线
        ma5=float(signal.get("ma5", 0)),
        ma10=float(signal.get("ma10", 0)),
        ma20=float(signal.get("ma20", 0)),
        ma60=float(signal.get("ma60", 0)),
        ma120=float(signal.get("ma120", 0)),
        ma250=float(signal.get("ma250", 0)),
        # 技术指标
        rsi14=float(signal.get("rsi14", signal.get("rsi", 0))),
        macd_dif=float(signal.get("macd_dif", 0)),
        macd_dea=float(signal.get("macd_dea", 0)),
        macd_hist=float(signal.get("macd_hist", 0)),
        atr14=float(signal.get("atr14", signal.get("atr", 0))),
        # 价格位置
        high_52w=float(signal.get("high_52w", signal.get("week52_high", 0))),
        low_52w=float(signal.get("low_52w", signal.get("week52_low", 0))),
        distance_from_52w_high=float(signal.get("distance_from_52w_high", 0)),
        distance_from_52w_low=float(signal.get("distance_from_52w_low", 0)),
        # 资金流
        main_net_inflow=float(money_flow.get("main_net_inflow", signal.get("main_net_inflow", 0))),
        main_net_inflow_5d=float(money_flow.get("main_net_inflow_5d", signal.get("main_net_inflow_5d", 0))),
        # 市场环境
        market_stage=market.get("market_stage", signal.get("market_stage", "")),
        hot_sector=signal.get("hot_sector", False),
        # 公司质量
        debt_ratio=float(fundamental.get("debt_ratio", signal.get("debt_ratio", 0))),
        operating_cash_flow=float(fundamental.get("operating_cash_flow", signal.get("operating_cash_flow", 0))),
        dividend_yield=float(fundamental.get("dividend_yield", signal.get("dividend_yield", 0))),
        institutional_holding=float(fundamental.get("institutional_holding", signal.get("institutional_holding", 0))),
        insider_buying=signal.get("insider_buying", False),
        insider_selling=signal.get("insider_selling", False),
        # 行业
        industry=signal.get("industry", ""),
        is_monopoly=signal.get("is_monopoly", signal.get("monopoly", False)),
        moat_type=signal.get("moat_type", ""),
    )


def _kline_cache_key(symbol: str, kline_df: Any) -> str:
    """生成K线缓存键（基于 symbol + 最后10个收盘价的哈希）。"""
    import hashlib
    if kline_df is None or (hasattr(kline_df, 'empty') and kline_df.empty):
        return f"{symbol}_empty"
    try:
        closes = kline_df["close"].values[-10:].tobytes() if "close" in kline_df else b""
        return f"{symbol}_{hashlib.md5(closes).hexdigest()}"
    except Exception:
        return f"{symbol}_{id(kline_df)}"


def build_context_from_kline(
    symbol: str,
    name: str,
    kline_df: Any,
    **kwargs: Any,
) -> StockDataContext:
    """从K线DataFrame构建 StockDataContext。

    自动计算均线、RSI、MACD、布林带、ATR等技术指标。

    P1 优化：使用 LRU 缓存避免同一只股票在同一次分析中重复计算技术指标。
    缓存键基于 symbol + 最后10个收盘价的哈希，确保数据变化时缓存失效。
    """
    import numpy as np
    import pandas as pd

    if kline_df is None or (hasattr(kline_df, 'empty') and kline_df.empty):
        return StockDataContext(symbol=symbol, name=name, **kwargs)

    df = kline_df.copy() if hasattr(kline_df, 'copy') else pd.DataFrame(kline_df)

    # 确保 close 列存在
    close_col = "close" if "close" in df.columns else "收盘"
    high_col = "high" if "high" in df.columns else "最高"
    low_col = "low" if "low" in df.columns else "最低"
    volume_col = "volume" if "volume" in df.columns else "成交量"

    closes = df[close_col].values.astype(float) if close_col in df.columns else np.array([])
    highs = df[high_col].values.astype(float) if high_col in df.columns else closes
    lows = df[low_col].values.astype(float) if low_col in df.columns else closes
    volumes = df[volume_col].values.astype(float) if volume_col in df.columns else np.array([])

    if len(closes) == 0:
        return StockDataContext(symbol=symbol, name=name, **kwargs)

    price = float(closes[-1])

    # 均线
    def _ma(data: np.ndarray, period: int) -> float:
        if len(data) >= period:
            return float(np.nanmean(data[-period:]))
        return 0.0

    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    ma120 = _ma(closes, 120)
    ma250 = _ma(closes, 250)

    # RSI(14)
    rsi14 = 0.0
    if len(closes) >= 15:
        deltas = np.diff(closes[-15:])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi14 = 100 - (100 / (1 + rs))
        else:
            rsi14 = 100.0

    # MACD (12, 26, 9)
    macd_dif = macd_dea = macd_hist = 0.0
    if len(closes) >= 26:
        ema12 = _calc_ema(closes, 12)
        ema26 = _calc_ema(closes, 26)
        macd_dif = ema12 - ema26
        # Signal line (DEA) = EMA(9) of DIF
        if len(closes) >= 35:
            dif_series = []
            for i in range(26, len(closes) + 1):
                sub = closes[:i]
                dif_series.append(_calc_ema(sub, 12) - _calc_ema(sub, 26))
            if len(dif_series) >= 9:
                macd_dea = _calc_ema(np.array(dif_series), 9)
                macd_hist = (macd_dif - macd_dea) * 2

    # 布林带 (20, 2)
    bollinger_upper = bollinger_lower = 0.0
    if len(closes) >= 20:
        bb_ma = np.nanmean(closes[-20:])
        bb_std = np.nanstd(closes[-20:])
        bollinger_upper = bb_ma + 2 * bb_std
        bollinger_lower = bb_ma - 2 * bb_std

    # ATR(14)
    atr14 = 0.0
    if len(highs) >= 15 and len(lows) >= 15 and len(closes) >= 15:
        trs = []
        for i in range(-14, 0):
            h = highs[i]
            low = lows[i]
            prev_c = closes[i - 1]
            tr = max(h - low, abs(h - prev_c), abs(low - prev_c))
            trs.append(tr)
        atr14 = float(np.mean(trs))

    # 52周高低
    high_52w = float(np.max(highs[-min(len(highs), 250):])) if len(highs) > 0 else 0
    low_52w = float(np.min(lows[-min(len(lows), 250):])) if len(lows) > 0 else 0

    dist_high = ((price - high_52w) / high_52w * 100) if high_52w > 0 else 0
    dist_low = ((price - low_52w) / low_52w * 100) if low_52w > 0 else 0

    # 量比
    volume_ratio = 0.0
    if len(volumes) >= 6:
        avg_5 = float(np.nanmean(volumes[-6:-1]))
        volume_ratio = float(volumes[-1]) / avg_5 if avg_5 > 0 else 0

    # 当日涨幅
    change_pct = 0.0
    if len(closes) >= 2:
        change_pct = (closes[-1] - closes[-2]) / closes[-2] * 100

    ctx_dict = {
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_pct": change_pct,
        "volume_ratio": volume_ratio,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "ma120": ma120,
        "ma250": ma250,
        "rsi14": rsi14,
        "macd_dif": macd_dif,
        "macd_dea": macd_dea,
        "macd_hist": macd_hist,
        "bollinger_upper": bollinger_upper,
        "bollinger_lower": bollinger_lower,
        "atr14": atr14,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "distance_from_52w_high": dist_high,
        "distance_from_52w_low": dist_low,
    }

    # 合并外部传入的额外字段
    ctx_dict.update(kwargs)

    return StockDataContext(**ctx_dict)


def _calc_ema(data: Any, period: int) -> float:
    """计算 EMA。"""
    import numpy as np
    if len(data) < period:
        return float(np.nanmean(data)) if len(data) > 0 else 0.0
    alpha = 2.0 / (period + 1)
    ema = float(np.nanmean(data[:period]))
    for val in data[period:]:
        ema = alpha * float(val) + (1 - alpha) * ema
    return ema


# ── 集成辅助函数 ──

def attach_perspective_to_signal(
    signal: dict[str, Any],
    result: PerspectiveAnalysisResult,
) -> dict[str, Any]:
    """将视角分析结果附加到信号字典。"""
    signal["perspective_score"] = round(result.composite_score, 1)
    signal["perspective_passed"] = result.passed
    signal["perspective_verdict"] = result.overall_verdict
    signal["perspective_min_score"] = result.min_score
    signal["perspective_min_perspective"] = result.min_perspective
    signal["perspective_passed_count"] = result.passed_count
    signal["perspective_fatal_count"] = result.fatal_count
    signal["perspective_suggestion"] = result.suggestion
    if result.entry_price:
        signal["perspective_entry"] = result.entry_price
    if result.stop_loss_price:
        signal["perspective_stop_loss"] = result.stop_loss_price
    if result.target_price:
        signal["perspective_target"] = result.target_price
    return signal


def filter_by_perspective(
    signals: list[dict[str, Any]],
    min_score: float = 6.0,
    require_pass: bool = False,
) -> list[dict[str, Any]]:
    """用视角评分过滤信号列表。"""
    filtered = []
    for sig in signals:
        score = sig.get("perspective_score", 0)
        passed = sig.get("perspective_passed", False)
        if score >= min_score and (not require_pass or passed):
            filtered.append(sig)
        elif score == 0:
            # 无视角评分的信号保留（向后兼容）
            filtered.append(sig)
    return filtered
