"""小圈圈儿《反包策略》战法规则模块 V2.0——原书完整修正版。

对应《反包策略》广东经济出版社，2025年6月
覆盖 5+1 种战法 + 风险红线 + 情绪周期 + 通用前置条件。

来源：用户提供的反包策略执行框架 V2.0（原书完整修正版）
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

RULE_VERSION = "v2.0.0-20260815"

# 涨跌停判定
LIMIT_UP_PCT = 0.095  # 涨停
TOUCH_LIMIT_PCT = 0.09  # 盘中触及涨停
DIETING_PCT = -0.095  # 跌停

# 通用前置条件
MV_MIN = 50.0  # 最小流通市值（亿）
HOT_LIMIT_UPS = 3  # 题材热度：近250日涨停≥3次（作为活跃度代理）
LOOKBACK_DAYS = 10  # K线回溯天数

# 炸板反包模式
ZHABAN_MIN_CLOSE = 0.06  # 炸板日收盘涨幅≥6%

# 跌停板战法
DIETING_LIANBAN_MIN = 2  # 高位跌停最小连板数

# 一字板反包
YIZI_LOOKBACK = 250  # 一字板位置回看窗口
YIZI_MA_LONG = 250  # 年线窗口

# 反包回调战法
CALLBACK_MAX_DAYS = 10  # 最大回调天数
CALLBACK_MIN_DAYS = 3  # 最小回调天数

# 暴力洗盘
BAOLI_DROP_PCT = -0.07  # 暴力洗盘最小跌幅

# 跳空涨停
TIAOKONG_GAP_MIN = 0.0  # 缺口阈值

# 性价比评分权重
SCORE_MV_WEIGHT = 0.4  # 市值权重
SCORE_LIANBAN_WEIGHT = 0.4  # 连板高度权重
SCORE_TOPIC_WEIGHT = 0.2  # 题材权重


# ══════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════


@dataclass
class FanbaoSignal:
    """反包信号。

    Attributes:
        strategy: 战法名称
        code: 股票代码
        signal_type: 信号类型（买入/观察）
        confidence: 置信度 [0,1]
        score: 性价比评分 [0,100]
        reason: 信号原因
        operation: 操作建议
        risk_note: 风险提示
        meta: 额外数据
    """

    strategy: str
    code: str
    signal_type: str = "买入"
    confidence: float = 0.5
    score: float = 50.0
    reason: str = ""
    operation: str = ""
    risk_note: str = ""
    meta: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════
# 通用工具函数
# ══════════════════════════════════════════════════════════════


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """计算通用指标。"""
    df = df.sort_values("date").reset_index(drop=True).copy()
    df["pct"] = df["close"].pct_change()
    df["high_pct"] = df["high"] / df["close"].shift(1) - 1.0
    df["low_pct"] = df["low"] / df["close"].shift(1) - 1.0
    df["open_pct"] = df["open"] / df["close"].shift(1) - 1.0
    df["vol_ma5"] = df["volume"].rolling(5, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma5"]
    df["ma5"] = df["close"].rolling(5, min_periods=1).mean()
    df["ma10"] = df["close"].rolling(10, min_periods=1).mean()
    df["ma20"] = df["close"].rolling(20, min_periods=1).mean()
    df["ma60"] = df["close"].rolling(60, min_periods=1).mean()
    df["ma250"] = df["close"].rolling(250, min_periods=20).mean()
    df["amp"] = (df["high"] - df["low"]) / df["close"].shift(1)
    df["amp_ma20"] = df["amp"].rolling(20, min_periods=1).mean()
    df["is_limit_up"] = df["pct"] >= (LIMIT_UP_PCT - 0.005)
    df["is_dieting"] = df["pct"] <= (DIETING_PCT + 0.005)
    df["limit_250"] = df["is_limit_up"].rolling(250, min_periods=30).sum()
    # 一字板检测：开盘即涨停（open ≈ 涨停价）且全天封死
    limit_price_col = df["close"].shift(1) * (1 + LIMIT_UP_PCT)
    df["is_yizi"] = (
        df["is_limit_up"]
        & (df["open"] >= limit_price_col * 0.99)
        & (df["low"] >= limit_price_col * 0.99)
    )
    return df


def _is_board_ok(code: str) -> bool:
    """检查板块是否允许（主板/中小板，排除创业板/科创板/北交所）。"""
    normalized = str(code).strip().lower()
    if normalized.startswith(("sh688", "sh689", "bj", "sh8")):
        return False
    return not normalized.startswith(("sz300", "sz301", "sz4"))


def _is_st_name(name: str | None) -> bool:
    """检查股票名称是否为 ST/*ST。"""
    normalized = str(name or "").strip().upper()
    return normalized.startswith("ST") or normalized.startswith("*ST") or " ST" in normalized


def _check_today_gap(open_pct: float) -> bool:
    """检查今日是否高开（统一进场信号）。"""
    return open_pct > 0.0  # 高开 > 0%


def calc_score(
    float_mv: float | None,
    lianban_height: int,
    is_hot_topic: bool = False,
) -> float:
    """计算性价比评分 [0, 100]。

    Args:
        float_mv: 流通市值（亿），越大越好
        lianban_height: 连板高度
        is_hot_topic: 是否当前主流题材

    Returns:
        评分
    """
    # 市值分 [0, 40]（对应权重 0.4）
    if float_mv is None:
        mv_score = 20
    elif float_mv >= 200:
        mv_score = 40
    elif float_mv >= 100:
        mv_score = 30
    elif float_mv >= MV_MIN:
        mv_score = 20
    else:
        mv_score = 0

    # 连板高度分 [0, 40]（对应权重 0.4）
    if lianban_height >= 7:
        lb_score = 40
    elif lianban_height >= 5:
        lb_score = 35
    elif lianban_height >= 3:
        lb_score = 25
    elif lianban_height >= 2:
        lb_score = 15
    else:
        lb_score = 5

    # 题材分 [0, 20]（对应权重 0.2）
    topic_score = 20 if is_hot_topic else 5

    return mv_score + lb_score + topic_score


def _find_max_consecutive_limit_ups(pct_arr: np.ndarray, start: int, end: int) -> int:
    """计算 [start, end) 区间的最大连续涨停数。"""
    max_cons = 0
    cur = 0
    for i in range(start, end):
        if pct_arr[i] >= (LIMIT_UP_PCT - 0.005):
            cur += 1
            max_cons = max(max_cons, cur)
        else:
            cur = 0
    return max_cons


# ══════════════════════════════════════════════════════════════
# 第一步：风险红线检查
# ══════════════════════════════════════════════════════════════


@dataclass
class RedLineResult:
    """红线检查结果。"""

    passed: bool = True
    reasons: list[str] = field(default_factory=list)


def check_red_lines(
    code: str,
    df: pd.DataFrame,
    name: str | None = None,
    float_mv: float | None = None,
    close_price: float | None = None,
) -> RedLineResult:
    """6条红线检查。

    Returns:
        RedLineResult: passed=False 表示触犯红线，应停止分析
    """
    result = RedLineResult(passed=True, reasons=[])

    # 红线1：板块和 ST/*ST 过滤必须在规则层执行，不能只依赖扫描器。
    if not _is_board_ok(code):
        result.passed = False
        result.reasons.append("红线1: 科创板/创业板/北交所")
    if _is_st_name(name):
        result.passed = False
        result.reasons.append("红线1: ST/*ST")

    # 红线2：立案/问询（需要在数据层传入 extra 信息，此处仅提供检查接口）
    # 实际应用中需传入 extra 数据

    # 红线3：股价低于3元且业绩亏损（用 close 价格近似）
    if close_price is not None and close_price < 3.0:
        result.passed = False
        result.reasons.append(f"红线3: 股价{close_price:.2f}<3元")

    # 红线4：总市值<10亿（用流通市值近似）
    if float_mv is not None and float_mv < 10.0:
        result.passed = False
        result.reasons.append(f"红线4: 市值{float_mv:.1f}亿<10亿")

    # 红线5：大趋势长期阴跌（通过 ma60 与 ma250 的关系判断）
    ddf = enrich(df)
    if len(ddf) >= 60:
        ma60 = ddf["ma60"].iloc[-1]
        close = ddf["close"].iloc[-1]
        if np.isfinite(ma60) and close < ma60 * 0.7:
            result.passed = False
            result.reasons.append("红线5: K线长期阴跌(close<ma60*0.7)")

    # 红线6：退潮旧题材（由扫描器层提供 extra.topic 信息，此处仅做接口）
    # 需要外部传入 current_hot_topic 判断

    return result


# ══════════════════════════════════════════════════════════════
# 第二步：情绪周期判断（简化版）
# ══════════════════════════════════════════════════════════════


def detect_sentiment_cycle(
    market_df: pd.DataFrame | None = None,
    daily_limit_up_count: pd.Series | None = None,
    zhangting_high_open_ratio: float | None = None,
) -> dict:
    """判断市场情绪周期。

    7阶段：冰点期 → 混沌期 → 启动期 → 发酵期 → 主升期 → 高潮期 → 退潮期

    Args:
        market_df: 大盘（HS300）K线数据，含 close/ma60
        daily_limit_up_count: 近5日每日涨停家数
        zhangting_high_open_ratio: 涨停股次日高开溢价比例(None=无法判断)

    Returns:
        dict 含 stage, is_operatable, reason
    """
    result = {
        "stage": "unknown",
        "is_operatable": False,
        "best_window": False,
        "reason": "",
    }

    # 默认：无数据时保守判断
    if market_df is None and daily_limit_up_count is None:
        result["stage"] = "unknown"
        result["is_operatable"] = False
        result["reason"] = "无市场数据，保守判定不可操作"
        return result

    # 用 HS300 regime 作为市场环境的代理指标
    if market_df is not None and len(market_df) >= 60:
        market = market_df.copy()
        market["date"] = pd.to_datetime(market["date"])
        market = market.sort_values("date").reset_index(drop=True)
        market["ma60"] = market["close"].rolling(60).mean()
        market["pct"] = market["close"].pct_change()
        last_5d_ret = market["pct"].tail(5).sum() if len(market) >= 5 else 0
        close_over_ma60 = market["close"].iloc[-1] > market["ma60"].iloc[-1]
        ma60_slope = (
            (market["ma60"].iloc[-1] / market["ma60"].iloc[-5] - 1) if len(market) >= 5 else 0
        )

        if close_over_ma60 and ma60_slope > 0.005 and last_5d_ret > 0.03:
            result["stage"] = "主升期"
            result["is_operatable"] = True
            result["best_window"] = True
            result["reason"] = "大盘>ma60且ma60向上，近5日涨幅>3%"
        elif close_over_ma60 and last_5d_ret > 0:
            result["stage"] = "发酵期"
            result["is_operatable"] = True
            result["best_window"] = True
            result["reason"] = "大盘>ma60且近5日为正"
        elif close_over_ma60:
            result["stage"] = "启动期"
            result["is_operatable"] = True
            result["reason"] = "大盘站上ma60，可能启动"
        elif not close_over_ma60 and last_5d_ret > 0.02:
            result["stage"] = "混沌期"
            result["is_operatable"] = False
            result["reason"] = "大盘<ma60但短期反弹，方向不明"
        elif not close_over_ma60 and last_5d_ret < -0.03:
            result["stage"] = "退潮期"
            result["is_operatable"] = False
            result["reason"] = "大盘<ma60且近5日大跌>3%"
        else:
            result["stage"] = "冰点期" if not close_over_ma60 else "退潮期"
            result["is_operatable"] = False
            result["reason"] = "大盘<ma60，成交低迷"

    # 涨停家数修正：如果有涨停家数数据，可进一步精化
    return result


# ══════════════════════════════════════════════════════════════
# 第三步：通用前置条件检查
# ══════════════════════════════════════════════════════════════


def _check_position(df_enriched: pd.DataFrame, lookback: int = 250) -> bool:
    """位置判断：通过250日高度分位判断是否处于低位启动区。

    原书："缩图看势"，拒绝参与已经大幅翻倍的高位股。
    """
    if len(df_enriched) < 60:
        return False
    close = df_enriched["close"].values
    recent = close[-min(lookback, len(close)) :]
    pct_rank = (close[-1] - recent.min()) / (recent.max() - recent.min() + 1e-10)
    # 位置分位 < 0.7 = 处于相对低位（拒绝已大幅翻倍的高位）
    return pct_rank < 0.7


def _check_float_mv(float_mv: float | None) -> bool:
    """流通市值检查。"""
    if float_mv is None:
        return True  # 无法判断时放行
    return float_mv >= MV_MIN


def _check_topic_heat(df_enriched: pd.DataFrame) -> bool:
    """题材热度检查（以250日涨停次数作为活跃度代理）。"""
    limit_250 = df_enriched["limit_250"].iloc[-1]
    return bool(np.isfinite(limit_250) and limit_250 >= HOT_LIMIT_UPS)


def check_general_conditions(
    df: pd.DataFrame,
    float_mv: float | None = None,
) -> dict:
    """通用前置条件检查。

    Returns:
        dict: {passed, reason, details}
    """
    ddf = enrich(df)
    n = len(ddf)
    if n < 60:
        return {"passed": False, "reason": "数据不足60行", "details": {}}

    mv_ok = _check_float_mv(float_mv)
    pos_ok = _check_position(ddf)
    topic_ok = _check_topic_heat(ddf)

    details = {
        "float_mv_ok": mv_ok,
        "position_ok": pos_ok,
        "topic_ok": topic_ok,
        "position_pct_rank": round(
            (ddf["close"].iloc[-1] - ddf["close"].tail(250).min())
            / (ddf["close"].tail(250).max() - ddf["close"].tail(250).min() + 1e-10),
            3,
        )
        if n >= 250
        else None,
    }

    passed = mv_ok and pos_ok and topic_ok
    reasons = []
    if not mv_ok:
        reasons.append("市值不足50亿")
    if not pos_ok:
        reasons.append("股价处于高位")
    if not topic_ok:
        reasons.append("题材热度不足")
    return {
        "passed": passed,
        "reason": "，".join(reasons) if reasons else "通过",
        "details": details,
    }


# ══════════════════════════════════════════════════════════════
# 模式一：跌停板战法（极端反包）
# ══════════════════════════════════════════════════════════════
# 观察点：涨停板→断板→跌停板
# 分类：高位（连板≥2） vs 低位（仅一板断）
# 核心：市值越大越好 × 连板高度越高越好 = 性价比
# 进场：跌停板次日"高开"
# 禁忌：跌停板当日绝对不能买！


def detect_dieting_fanbao(
    code: str,
    df: pd.DataFrame,
    float_mv: float | None = None,
    is_hot_topic: bool = False,
    skip_checks: bool = False,
    today_open: float | None = None,
) -> FanbaoSignal | None:
    """检测跌停板战法（模式一）。

    区分高位跌停板（原M04升级版）和低位跌停板。

    Args:
        today_open: 今日开盘涨幅（None 时从 df 最后一行读，用于单元测试兼容）
    """
    ddf = enrich(df)
    n = len(ddf)
    if n < 3:
        return None

    pct = ddf["pct"].values
    ddf["high_pct"].values
    open_pct = ddf["open_pct"].values
    ddf["close"].values
    ddf["date"].values
    last = n - 1

    # 高开检查：优先用外部传入的 today_open（消除前视）
    today_open_pct = today_open if today_open is not None else open_pct[last]
    if not _check_today_gap(today_open_pct):
        return None
    ddf = enrich(df)
    n = len(ddf)
    if n < 3:
        return None

    pct = ddf["pct"].values
    ddf["high_pct"].values
    open_pct = ddf["open_pct"].values
    ddf["close"].values
    ddf["date"].values
    last = n - 1

    # 今日必须高开（统一进场信号）
    if not _check_today_gap(open_pct[last]):
        return None

    # 今日不能是跌停（高开已排除跌停）
    # 昨日必须是跌停
    yesterday = last - 1
    if yesterday < 0:
        return None
    if pct[yesterday] > DIETING_PCT:
        return None
    # 昨日跌停前是断板日（昨天之前至少有一天是涨停/断板状态）
    # 再前一日：至少是断板（非涨停）
    day_before = yesterday - 1
    if day_before < 0:
        return None

    # 计算跌停前的连板高度（回看跌停前30日）
    lookback_start = max(0, yesterday - 30)
    max_cons = _find_max_consecutive_limit_ups(pct, lookback_start, yesterday)

    # 高/低位分类
    is_high = max_cons >= DIETING_LIANBAN_MIN
    strategy_name = "跌停板战法-高位" if is_high else "跌停板战法-低位"

    # 性价比评分
    score = calc_score(float_mv, max_cons, is_hot_topic)

    # 市值检查
    if not skip_checks and float_mv is not None and float_mv < MV_MIN and is_high:
        return None  # 高位跌停必须大市值
    # 低位跌停对市值要求可适当放宽

    # 位置检查
    if not skip_checks and not _check_position(ddf):
        return None

    reason = (
        f"昨日跌停({pct[yesterday]*100:.1f}%),"
        f"今日高开({today_open_pct*100:.1f}%)弱转强,"
        f"前期连板{max_cons}日"
    )
    operation = "竞价高开确认后在9:30前买入" if is_high else "低位跌停高开可小仓位试错"

    return FanbaoSignal(
        strategy=strategy_name,
        code=code,
        signal_type="买入",
        confidence=min(0.8, 0.3 + max_cons * 0.08),
        score=score,
        reason=reason,
        operation=operation,
        risk_note="跌停板次日高开是唯一买点，若当日再次跌停则放弃",
        meta={
            "max_consecutive_limit_ups": max_cons,
            "yesterday_pct": round(pct[yesterday] * 100, 2),
            "today_open_pct": round(today_open_pct * 100, 2),
        },
    )


# ══════════════════════════════════════════════════════════════
# 模式二：炸板反包（新手首选，最常见）
# ══════════════════════════════════════════════════════════════
# 观察点：涨停板→炸板(盘中打开且未封回)→次日高开
# 条件：①流通市值大 ②炸板日高开 ③炸板日高位收盘(≥6%)
# 进场：次日"高开"


def detect_zhaban_fanbao(
    code: str,
    df: pd.DataFrame,
    float_mv: float | None = None,
    is_hot_topic: bool = False,
    skip_checks: bool = False,
    today_open: float | None = None,
) -> FanbaoSignal | None:
    """检测炸板反包（模式二）。

    时序（V2.0 统一进场信号）：
      T-2: 涨停
      T-1: 炸板（盘中触板、收盘未封、收盘≥6%、高开）
      T:  高开 → 买入信号

    Args:
        code: 股票代码
        df: K线数据（不含今日，最后一行=昨日）
        float_mv: 流通市值（亿）
        is_hot_topic: 是否主流题材
        skip_checks: 跳过位置/题材检查（单元测试用）
        today_open: 今日开盘涨幅（None 时从 df 最后一行读）
    """
    ddf = enrich(df)
    n = len(ddf)
    if n < 60:
        return None

    pct = ddf["pct"].values
    high_pct = ddf["high_pct"].values
    open_pct = ddf["open_pct"].values
    ddf["close"].values
    ddf["date"].values
    last = n - 1

    # 高开检查：优先用外部传入的 today_open（消除前视）
    today_open_pct = today_open if today_open is not None else open_pct[last]
    if not _check_today_gap(today_open_pct):
        return None

    # 昨日（Day T-1）：炸板日
    yesterday = last - 1
    if yesterday < 0:
        return None

    # 前日（Day T-2）：涨停日
    day_before = yesterday - 1
    if day_before < 0:
        return None

    # C01：前日涨停
    if pct[day_before] < LIMIT_UP_PCT:
        return None

    # C02：昨日盘中触及涨停（炸板特征）
    if high_pct[yesterday] < TOUCH_LIMIT_PCT:
        return None

    # C03：昨日收盘未涨停（炸板特征）
    if pct[yesterday] >= LIMIT_UP_PCT:
        return None

    # C04：昨日收盘维持高位（≥6%）
    if pct[yesterday] < ZHABAN_MIN_CLOSE:
        return None

    # C05：流通市值≥50亿
    if not skip_checks and float_mv is not None and float_mv < MV_MIN:
        return None

    if not skip_checks:
        # 位置检查
        if not _check_position(ddf):
            return None

    # 成交量确认
    vol_ok = (
        np.isfinite(ddf["vol_ma5"].iloc[yesterday])
        and ddf["volume"].iloc[yesterday] >= ddf["vol_ma5"].iloc[yesterday]
    )

    score = calc_score(float_mv, 1, is_hot_topic)

    return FanbaoSignal(
        strategy="炸板反包",
        code=code,
        signal_type="买入",
        confidence=0.7,
        score=score,
        reason=(
            f"前日涨停({pct[day_before]*100:.1f}%)→"
            f"昨日炸板(最高{high_pct[yesterday]*100:.1f}%/收盘{pct[yesterday]*100:.1f}%)→"
            f"今日高开({today_open_pct*100:.1f}%)弱转强"
        ),
        operation="竞价高开确认后买入",
        risk_note="炸板次日若低开则放弃，止损设炸板日最低价",
        meta={
            "day_before_pct": round(pct[day_before] * 100, 2),
            "yesterday_pct": round(pct[yesterday] * 100, 2),
            "yesterday_high": round(high_pct[yesterday] * 100, 2),
            "today_open_pct": round(today_open_pct * 100, 2),
            "vol_confirmed": bool(vol_ok),
        },
    )


# ══════════════════════════════════════════════════════════════
# 模式三：一字涨停板反包
# ══════════════════════════════════════════════════════════════
# 观察点：第一个/第二个涨停是一字板→断板→整理→高开
# 排除：第3/4板才出现一字加速(已高位，做不了反包)
# 进场：断板整理后再次高开放量


def detect_yizi_fanbao(
    code: str,
    df: pd.DataFrame,
    float_mv: float | None = None,
    is_hot_topic: bool = False,
    skip_checks: bool = False,
    today_open: float | None = None,
) -> FanbaoSignal | None:
    """检测一字涨停板反包（模式三）。"""
    ddf = enrich(df)
    n = len(ddf)
    if n < 100:
        return None

    pct = ddf["pct"].values
    open_pct = ddf["open_pct"].values
    ddf["high_pct"].values
    closes = ddf["close"].values
    is_yizi = ddf["is_yizi"].values
    ddf["is_limit_up"].values
    ddf["date"].values
    vols = ddf["volume"].values
    last = n - 1

    # 今日必须高开；优先使用实时/竞价输入，避免把历史最后一根 K 线误当成今日。
    today_open_pct = today_open if today_open is not None else open_pct[last]
    if not _check_today_gap(today_open_pct):
        return None

    # 今日有放量迹象
    vol_ma5 = ddf["vol_ma5"].iloc[last]
    np.isfinite(vol_ma5) and vols[last] >= vol_ma5 * 1.2

    # 找最近的一字板位置
    yizi_indices = np.where(is_yizi)[0]
    recent_yizi = [i for i in yizi_indices if i < last]

    if not recent_yizi:
        return None

    # 取最近的一字板
    last_yizi_idx = recent_yizi[-1]
    # 检查一字板是否在断板整理阶段（不是当天）
    days_since_yizi = last - last_yizi_idx
    if days_since_yizi < CALLBACK_MIN_DAYS or days_since_yizi > 30:
        return None

    # 判断一字板位置：检查一字板前是否已有连板（避免第3/4板加速一字）
    lookback_start = max(0, last_yizi_idx - 10)
    cons_before_yizi = _find_max_consecutive_limit_ups(pct, lookback_start, last_yizi_idx)
    # 如果一字板前已有3板以上，说明是加速一字（排除）
    if cons_before_yizi >= 3:
        return None

    # 位置检查：是否在年线附近或底部
    ma250 = ddf["ma250"].iloc[-1]
    pos_ok = np.isfinite(ma250) and closes[last] >= ma250 * 0.9

    if not skip_checks:
        if not pos_ok:
            return None

        # 市值检查
        if float_mv is not None and float_mv < MV_MIN:
            return None

    score = calc_score(float_mv, 1, is_hot_topic)

    return FanbaoSignal(
        strategy="一字涨停板反包",
        code=code,
        signal_type="买入",
        confidence=0.6,
        score=score,
        reason=(
            f"前{days_since_yizi}日出现一字板，断板整理{days_since_yizi}天，"
            f"今日高开({today_open_pct*100:.1f}%)放量({ddf['vol_ratio'].iloc[-1]:.1f}x)"
        ),
        operation="高开放量确认后买入，止损设涨停前平台低点",
        risk_note="一字板后若长时间不启动则放弃，注意是否是第3板后加速一字",
        meta={
            "yizi_idx": int(last_yizi_idx),
            "days_since_yizi": int(days_since_yizi),
            "today_open_pct": round(today_open_pct * 100, 2),
            "vol_ratio": round(ddf["vol_ratio"].iloc[last], 2),
        },
    )


# ══════════════════════════════════════════════════════════════
# 模式四：反包回调战法
# ══════════════════════════════════════════════════════════════
# 观察点：涨停→断板→反包涨停的股票(有反包基因)→等待回调→企稳再进
# 回调企稳信号：①阴线实体变小 ②出现阳线 ③回调在涨停板上方
# 进场：回调后再次高开或放量突破平台


def detect_callback_fanbao(
    code: str,
    df: pd.DataFrame,
    float_mv: float | None = None,
    is_hot_topic: bool = False,
    skip_checks: bool = False,
    today_open: float | None = None,
) -> FanbaoSignal | None:
    """检测反包回调战法（模式四）。"""
    ddf = enrich(df)
    n = len(ddf)
    if n < 60:
        return None

    pct = ddf["pct"].values
    open_pct = ddf["open_pct"].values
    closes = ddf["close"].values
    opens = ddf["open"].values
    highs = ddf["high"].values
    lows = ddf["low"].values
    vols = ddf["volume"].values
    is_limit_up = ddf["is_limit_up"].values
    ddf["date"].values
    last = n - 1

    # 今日必须高开；优先使用显式竞价/开盘输入。
    today_open_pct = today_open if today_open is not None else open_pct[last]
    if not _check_today_gap(today_open_pct):
        return None

    # 今日有放量迹象
    vol_ma5 = ddf["vol_ma5"].iloc[last]
    np.isfinite(vol_ma5) and vols[last] >= vol_ma5 * 1.2

    # 寻找历史的"涨停→断板→反包涨停"模式
    # 回看最近60日
    lookback_start = max(0, last - 60)
    fanbao_indices = []
    i = lookback_start
    while i < last - 2:
        if is_limit_up[i]:
            # 找到涨停
            j = i + 1
            # 断板
            if j < last and not is_limit_up[j]:
                # 找反包
                k = j + 1
                while k < last:
                    if is_limit_up[k]:
                        # 反包涨停成立！记录反包日
                        fanbao_indices.append(k)
                        i = k + 1
                        break
                    k += 1
                else:
                    i += 1
            else:
                i += 1
        else:
            i += 1

    if not fanbao_indices:
        return None

    # 取最近的反包涨停日
    last_fanbao_idx = fanbao_indices[-1]
    days_since_fanbao = last - last_fanbao_idx

    # 回调持续3-10天
    if days_since_fanbao < CALLBACK_MIN_DAYS or days_since_fanbao > CALLBACK_MAX_DAYS:
        return None

    # 检查回调期间的企稳信号
    callback_slice = slice(last_fanbao_idx + 1, last + 1)
    cb_closes = closes[callback_slice]
    cb_opens = opens[callback_slice]
    cb_lows = lows[callback_slice]
    highs[callback_slice]
    pct[callback_slice]

    # 信号1：阴线实体变小（最后几日实体比前几日小）
    # 信号2：出现阳星（小阳线）
    has_little_yang = any(
        cb_opens[i] < cb_closes[i] and (cb_closes[i] - cb_opens[i]) < cb_closes[i] * 0.02
        for i in range(len(cb_closes))
    )
    # 信号3：回调幅度不宜过大——最低在涨停板上方
    fanbao_close = closes[last_fanbao_idx]
    callback_max_drop = (cb_lows.min() - fanbao_close) / fanbao_close
    drop_ok = callback_max_drop > -0.05  # 回调不超过5%

    if not has_little_yang and not drop_ok:
        return None

    if not skip_checks:
        # 市值检查
        if float_mv is not None and float_mv < MV_MIN:
            return None

        # 位置检查
        if not _check_position(ddf):
            return None

    score = calc_score(float_mv, 1, is_hot_topic)

    return FanbaoSignal(
        strategy="反包回调战法",
        code=code,
        signal_type="买入",
        confidence=0.6,
        score=score,
        reason=(
            f"前{last_fanbao_idx}日反包涨停后回调{days_since_fanbao}天，"
            f"回调幅度{callback_max_drop*100:.1f}%，"
            f"出现企稳阳星，今日高开({today_open_pct*100:.1f}%)放量"
        ),
        operation="高开放量突破回调平台时买入",
        risk_note="回调幅度不宜超过5%，若跌破反包涨停日最低价则放弃",
        meta={
            "fanbao_idx": int(last_fanbao_idx),
            "days_since_fanbao": int(days_since_fanbao),
            "callback_max_drop": round(callback_max_drop * 100, 2),
            "today_open_pct": round(today_open_pct * 100, 2),
            "vol_ratio": round(ddf["vol_ratio"].iloc[last], 2),
        },
    )


# ══════════════════════════════════════════════════════════════
# 模式五：跳空涨停式反包
# ══════════════════════════════════════════════════════════════
# 观察点：跳空高开涨停（开盘价>昨日最高价）→断板→高开再进
# 核心：大市值跳空高开=资金合力强劲
# 只做一板断


def detect_tiaokong_fanbao(
    code: str,
    df: pd.DataFrame,
    float_mv: float | None = None,
    is_hot_topic: bool = False,
    skip_checks: bool = False,
    today_open: float | None = None,
) -> FanbaoSignal | None:
    """检测跳空涨停式反包（模式五）。"""
    ddf = enrich(df)
    n = len(ddf)
    if n < 10:
        return None

    ddf["pct"].values
    open_pct = ddf["open_pct"].values
    ddf["high_pct"].values
    ddf["close"].values
    opens = ddf["open"].values
    highs = ddf["high"].values
    is_limit_up = ddf["is_limit_up"].values
    ddf["date"].values
    ddf["volume"].values
    last = n - 1

    # 今日必须高开；优先使用显式竞价/开盘输入。
    today_open_pct = today_open if today_open is not None else open_pct[last]
    if not _check_today_gap(today_open_pct):
        return None

    yesterday = last - 1
    if yesterday < 0:
        return None

    # 昨天：跳空高开涨停 → 一板断
    # 跳空：开盘价 > 昨日最高价
    day_before = yesterday - 1
    if day_before < 0:
        return None
    gap_open = opens[yesterday] > highs[day_before]

    if not gap_open:
        return None

    # 昨天涨停
    if not is_limit_up[yesterday]:
        return None

    # 一板断：昨天涨停后今日断板（不涨停）
    if is_limit_up[last]:
        return None

    if not skip_checks:
        # 大市值优先
        if float_mv is not None and float_mv < MV_MIN:
            return None

        # 位置检查
        if not _check_position(ddf):
            return None

    score = calc_score(float_mv, 1, is_hot_topic)

    return FanbaoSignal(
        strategy="跳空涨停式反包",
        code=code,
        signal_type="买入",
        confidence=0.65,
        score=score,
        reason=(
            f"昨日跳空高开涨停(开盘>{highs[day_before]:.2f})，"
            f"今日断板但高开({today_open_pct*100:.1f}%)，资金合力强劲"
        ),
        operation="高开确认后买入，大市值跳空反包优先",
        risk_note="一板断后若持续低开则放弃，说明资金接力不足",
        meta={
            "yesterday_gap_open": round(open_pct[yesterday] * 100, 2),
            "today_open_pct": round(today_open_pct * 100, 2),
            "yesterday_high": round(highs[yesterday], 2),
        },
    )


# ══════════════════════════════════════════════════════════════
# 补充模式：暴力洗盘战法
# ══════════════════════════════════════════════════════════════
# 观察点：涨停板后→暴力急跌(跌幅>7%甚至跌停)→高开
# 条件：主流题材+热门股+大市值优先
# 核心逻辑："跌得越快越好，跌得越急越好"
# 进场：下跌次日高开


def detect_baoli_xipan(
    code: str,
    df: pd.DataFrame,
    float_mv: float | None = None,
    is_hot_topic: bool = True,
    skip_checks: bool = False,
    today_open: float | None = None,
) -> FanbaoSignal | None:
    """检测暴力洗盘战法（补充模式）。"""
    ddf = enrich(df)
    n = len(ddf)
    if n < 10:
        return None

    pct = ddf["pct"].values
    open_pct = ddf["open_pct"].values
    ddf["high_pct"].values
    ddf["low_pct"].values
    ddf["close"].values
    ddf["open"].values
    ddf["high"].values
    ddf["low"].values
    is_limit_up = ddf["is_limit_up"].values
    ddf["is_dieting"].values
    ddf["date"].values
    last = n - 1

    # 今日必须高开；优先使用显式竞价/开盘输入。
    today_open_pct = today_open if today_open is not None else open_pct[last]
    if not _check_today_gap(today_open_pct):
        return None

    yesterday = last - 1
    if yesterday < 0:
        return None

    # 昨日暴力急跌（跌幅>7%或跌停）
    if pct[yesterday] > BAOLI_DROP_PCT:
        return None

    # 检查大跌前(前几日)是否有涨停
    prev = yesterday - 1
    if prev < 0:
        return None

    # 近5日内是否有涨停
    recent_start = max(0, yesterday - 5)
    has_recent_limit = any(is_limit_up[recent_start:yesterday])
    if not has_recent_limit:
        return None

    if not skip_checks:
        # 大市值优先
        if float_mv is not None and float_mv < MV_MIN:
            return None

        # 必须是主流题材
        if not is_hot_topic:
            return None

        # 位置检查
        if not _check_position(ddf):
            return None

    score = calc_score(float_mv, 1, is_hot_topic)

    return FanbaoSignal(
        strategy="暴力洗盘战法",
        code=code,
        signal_type="买入",
        confidence=0.65,
        score=score,
        reason=(
            f"前期涨停后昨日暴力急跌({pct[yesterday]*100:.1f}%)，"
            f"主流题材热门股，今日高开({today_open_pct*100:.1f}%)弱转强"
        ),
        operation="高开确认后买入，暴力洗盘后反弹力度大",
        risk_note="如果不是主流题材股，暴力急跌可能是真出货；大市值暴力下跌更能确认是洗盘",
        meta={
            "yesterday_pct": round(pct[yesterday] * 100, 2),
            "today_open_pct": round(today_open_pct * 100, 2),
            "has_recent_limit": bool(has_recent_limit),
        },
    )


# ══════════════════════════════════════════════════════════════
# 全战法检测入口
# ══════════════════════════════════════════════════════════════


def detect_all(
    code: str,
    df: pd.DataFrame,
    float_mv: float | None = None,
    is_hot_topic: bool = False,
    skip_red_lines: bool = False,
    sentiment_ok: bool = True,
    skip_checks: bool = False,
    today_open: float | None = None,
    today_prev_close: float | None = None,
) -> list[FanbaoSignal]:
    """运行全部战法检测。

    Args:
        code: 股票代码
        df: K线数据（不含今日，形态判定只用此 df 的最后一根作为昨日）
        float_mv: 流通市值（亿）
        is_hot_topic: 是否当前主流题材
        skip_red_lines: 跳过红线检查（扫描器层已检查时用）
        sentiment_ok: 情绪周期是否适合操作
        skip_checks: 跳过位置/题材检查（单元测试用）
        today_open: 今日开盘价（None 时从 df 最后一行读）
        today_prev_close: 昨日收盘价（None 时从 df 倒数第二行读）

    Returns:
        信号列表（按性价比评分降序排列）
    """
    # 提取今日开盘信息：形态判定用 df[:-1]，高开用 today_open
    if today_open is not None and today_prev_close is not None:
        _today_open_pct = today_open / today_prev_close - 1.0
    else:
        _today_open_pct = None  # 让各 detector 从 df 自己读
    if not skip_red_lines:
        red = check_red_lines(code, df, float_mv=float_mv)
        if not red.passed:
            return []

    if not sentiment_ok:
        return []

    signals: list[FanbaoSignal] = []

    detectors = [
        ("跌停板战法", detect_dieting_fanbao),
        ("炸板反包", detect_zhaban_fanbao),
        ("一字涨停板反包", detect_yizi_fanbao),
        ("反包回调战法", detect_callback_fanbao),
        ("跳空涨停式反包", detect_tiaokong_fanbao),
        ("暴力洗盘战法", detect_baoli_xipan),
    ]

    for _name, detector in detectors:
        try:
            signal = detector(
                code,
                df,
                float_mv,
                is_hot_topic,
                skip_checks,
                today_open=_today_open_pct,
            )
            if signal is not None:
                signals.append(signal)
        except Exception:
            continue

    # 按评分降序排列
    signals.sort(key=lambda s: s.score, reverse=True)
    return signals
