"""断板反包战法·形态规则唯一实现（Single Source of Truth）

2026-08-12 架构重构：消灭"三套规则漂移"问题。
- 之前: 扫描脚本(scan_rebound_market.py) / 回测引擎(backtest_rebound_full.py) /
       策略类(rebound_system.py) 各自实现一套形态判定, 参数与逻辑互相漂移
        (如回测引擎仍用 0.97 容忍线, 扫描已改严格铁律 → 验证结论无法落地)
- 现在: 本模块是唯一实现, 三方全部 import 本模块

本模块包含 2026-08-12 全部 5 条根源修复:
  ① 一字板(振幅<1% 且 开=高) 非首板 —— 无换手+量比分母≈0假象
  ② 首板后1-2日连板排除 —— 连板加速非"首次断板"
  ③ 洗盘必须实体阴线(close<open) —— pct口径漏天地板假阳
  ④ 破位判定回归 close<first_open 铁律 —— 0.97容忍线稀释铁律
  ⑤ 首板前20日已有涨停=二波异动 —— 非"横盘后首次异动"

用法:
    from gugu.strategies.rebound.rules import analyze_stock, enrich, RULE_VERSION
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ── 规则版本号: 信号落库时记录, 绩效按版本分层统计 ──
RULE_VERSION = "v2.0.0-20260812"

# ── 手册量化常量 ──────────────────────────────
LIMIT_UP_RATIO = 0.095  # 涨停判定（主板 9.5%，比率口径）
HOT_LIMIT_UPS = 3  # 题材热度：近250日涨停≥3次（近1年多次涨停=辨识度）
AMP_MIN = 0.03  # 股性：20日均振幅>3%（"心电图"横盘剔除）
WASH_TURNOVER_MIN = 1.5  # 洗盘量 ≥ 1.5×首板量（换手放大，手册/因子漏斗验证）
FIRST_PCT_MIN = 0.09  # 🔴 2026-08-12 修复⑦: 首板改为涨停口径(手册: 首板必须涨停, 非大阳>7%)
MA_CLUSTER_MAX = 0.10  # 横盘吸筹：首板前 MA5≈MA10≈MA20 最大偏离
PRE_RANGE_MAX = 0.30  # 🔴 2026-08-12 修复⑦: 振幅收紧至30%(手册: 横盘振幅收敛, 原50%过宽)
FIRST_VOL_MAX_RATIO = 3.0  # 首板量 < 3×20日均量（过度放量=情绪顶点）
WASH_BODY_DROP_MIN = 0.03  # 洗盘实体跌幅>3%
WASH_AMP_HEAVY = 0.10  # 天地板/巨震振幅阈值
WASH_VOL_VS_MA5_MIN = 1.0  # 洗盘量 ≥ 1×MA5
PROTECT_SHRINK_MAX = 0.5  # 护盘期量能萎缩 ≤ 0.5×洗盘量
TOUCH_OPEN_TOL = 1.03  # 回踩触及首板开盘价±3%
DIST_SUPPORT_MAX = 0.08  # 伏击圈：现价距支撑≤8%
BREAKOUT_VOL_MIN = 1.2  # 反包量 ≥ 1.2×MA5
REBOUND_WINDOW = 10  # 反包窗口：首板后≤10个交易日
REBOUND_FRESH_DAYS = 3  # 反包距今≤3日才算"已触发买点C"
FIND_WINDOW = 10  # 🔴 2026-08-12 修复⑦: 手册"筛选4日内涨停", 收紧至10日(4日+安全余量)
# 🔴 2026-08-12 修复⑦: 手册量能公式(基于流通市值)
FIRST_AMT_RATIO = 0.01  # 首板成交额 ≥ 流通市值×1%
REBOUND_AMT_MIN = 0.02  # 反包成交额 ∈ [流通市值×2%, 流通市值×3%]
REBOUND_AMT_MAX = 0.03
DANGER_AMT_RATIO = 2.0  # 爆量红线: 成交额 > 首板×2 = 倍量抛压
MV_MIN = 40.0  # 🔴 2026-08-12 修复⑦: 手册"优选40-50亿, 100亿以上排除"
MV_MAX = 100.0


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """计算形态识别所需的全部指标（与原 scan_rebound_market.enrich 一致）"""
    df = df.sort_values("date").reset_index(drop=True).copy()
    df["pct"] = df["close"].pct_change()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma5"]
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    # 成交额估算（腾讯K线无 amount）：volume(手)×100股×均价
    df["amount_est"] = df["volume"] * 100.0 * df["close"]
    df["amp"] = (df["high"] - df["low"]) / df["close"].shift(1)
    df["amp_ma20"] = df["amp"].rolling(20).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    # 近250日涨停次数（题材热度/辨识度：近1年多次涨停）
    df["is_limit_up"] = df["pct"] >= (LIMIT_UP_RATIO - 0.005)
    df["limit_250"] = df["is_limit_up"].rolling(250, min_periods=30).sum()
    return df


def _is_body_bear(close, open_) -> bool:
    """实体阴线：close < open"""
    return bool(close < open_)


def _is_one_word_board(open_, high, low) -> bool:
    """一字板/秒板：振幅<1% 且 开盘=最高（开盘即封板，无分歧无换手）"""
    amp = (high / low - 1.0) if low > 0 else 99.0
    return amp < 0.01 and open_ == high


# ── 原子判定谓词（回测引擎/扫描引擎共用，消灭规则漂移）────────────────
def is_valid_wash_day(open_, close, high, low, vol, first_vol, vol_ma5) -> bool:
    """洗盘日判定（修复③：必须实体阴线，pct口径漏天地板假阳）。

    - 实体阴线 close < open
    - 实体跌幅>3% 或 振幅>10%的天地板巨震
    - 放量 ≥ 1.5×首板量 且 ≥ 1×MA5
    """
    is_body_bear = close < open_
    amp = (high / low - 1.0) if low > 0 else 0.0
    body_drop = (open_ - close) / open_ if open_ > 0 else 0.0
    is_heavy_shake = amp > WASH_AMP_HEAVY and is_body_bear
    vol_ok = vol >= first_vol * WASH_TURNOVER_MIN
    if np.isfinite(vol_ma5):
        vol_ok = vol_ok and vol >= vol_ma5 * WASH_VOL_VS_MA5_MIN
    return bool(is_body_bear and (body_drop > WASH_BODY_DROP_MIN or is_heavy_shake) and vol_ok)


def is_broken_close(close: float, first_open: float) -> bool:
    """破位判定（修复④：收盘 < 首板开盘价 = 形态失败，无容忍线）"""
    return bool(close < first_open)


def is_valid_first_board(
    pct: float,
    prev3_pct,
    ma5: float,
    ma10: float,
    ma20: float,
    win_high: float,
    win_low: float,
    vol: float,
    vol_ma20: float,
    open_: float,
    high: float,
    low: float,
    next2_pct=None,
    prev20_pct=None,
    # 🔴 2026-08-12 修复⑥: 横盘吸筹质量检查参数（可选，调用方不传则跳过）
    close_price: float | None = None,
    ma60: float | None = None,
    ma60_20ago: float | None = None,
    prev20_close=None,
    prev20_high: float | None = None,
    prev20_low: float | None = None,
) -> bool:
    """首板候选判定（含修复①一字板/②连板/⑤二波异动排除/⑥横盘吸筹质量）。

    参数均为单个标量或切片。判定项：
    - 涨幅 ≥ 7%
    - 前3日无涨停（排除连板起点）
    - 均线粘合（MA5≈MA10≈MA20, 偏离≤10%）
    - 前20日振幅收敛（≤50%）
    - 首板量 < 3×20日均量（过度放量=情绪顶点）
    - 修复①: 非一字板
    - 修复②: 首板后1-2日不再涨停
    - 修复⑤: 前20日无涨停（首次异动，非二波）
    - 修复⑥: 横盘吸筹质量——趋势(MA60斜率)+时间(在MA60附近)+空间(振幅收敛)
    """
    if pct < FIRST_PCT_MIN:
        return False
    if (prev3_pct >= 0.09).any():
        return False
    if not (np.isfinite(ma5) and np.isfinite(ma10) and np.isfinite(ma20)):
        return False
    ma_max, ma_min = max(ma5, ma10, ma20), min(ma5, ma10, ma20)
    if ma_min <= 0 or (ma_max / ma_min - 1) > MA_CLUSTER_MAX:
        return False
    if win_low <= 0 or (win_high / win_low - 1) > PRE_RANGE_MAX:
        return False
    if np.isfinite(vol_ma20) and vol >= vol_ma20 * FIRST_VOL_MAX_RATIO:
        return False
    if _is_one_word_board(open_, high, low):  # 修复①
        return False
    if next2_pct is not None and (next2_pct >= LIMIT_UP_RATIO - 0.005).any():  # 修复②
        return False
    # 修复⑤: 前20日无涨停（首次异动，非二波）
    if prev20_pct is not None and (prev20_pct >= LIMIT_UP_RATIO - 0.005).any():
        return False
    # 修复⑥: 横盘吸筹质量检查（第一性原理）
    # 从趋势(MA60斜率)+时间(在MA60附近天数)+空间(振幅收敛)三维度判定
    return not (
        close_price is not None
        and not is_consolidation_base(
            close_price=close_price,
            ma60=ma60,
            ma60_20ago=ma60_20ago,
            prev20_close=prev20_close,
            prev20_high=prev20_high,
            prev20_low=prev20_low,
        )
    )


def is_consolidation_base(
    close_price: float,
    ma60: float,
    ma60_20ago: float,
    prev20_close,
    prev20_high: float,
    prev20_low: float,
) -> bool:
    """第一性原理检查：首板前是否有完整的横盘吸筹基座。

    断板反包战法的核心前提——首板必须是「横盘吸筹后的首次异动」。
    横盘吸筹 = 中期趋势走平 + 股价在MA60附近整理 + 振幅收敛。
    从三个正交维度判定：

    ① 趋势维度（中期趋势方向）
       MA60斜率(20日) > -5%（中期趋势走平或向上）
       或 股价已明显站上MA60（c/MA60 > 1.05，股价已突破中期趋势）
       → 排除：MA60快速下降且股价未站上（下跌趋势反弹）

    ② 时间维度（横盘持续时间）
       前20日中，至少50%的交易日收盘价在MA60的±15%范围内
       → 排除：最近才从低位反弹到MA60（V型反弹，无横盘基座）
       → 华东重机/兆新股份/咸亨国际：前20日收盘价大部分在MA60×0.85以下

    ③ 空间维度（振幅收敛）
       前20日最高最低振幅 ≤ 30%（原50%，收紧——50%振幅不是横盘）
       → 排除：大幅波动
    """
    # ① 趋势维度
    if np.isfinite(ma60) and np.isfinite(ma60_20ago) and ma60_20ago > 0:
        ma60_slope = ma60 / ma60_20ago - 1.0
        if ma60_slope < -0.05 and close_price < ma60 * 1.05:
            return False

    # ② 时间维度：前20日收盘价在MA60附近的占比
    if ma60 > 0 and prev20_close is not None and len(prev20_close) > 0:
        band_low = ma60 * 0.85
        band_high = ma60 * 1.15
        n_in_band = 0
        n_total = 0
        for c_val in prev20_close:
            if np.isfinite(c_val) and c_val > 0:
                n_total += 1
                if band_low <= c_val <= band_high:
                    n_in_band += 1
        if n_total >= 10:  # 至少有10个有效数据
            ratio = n_in_band / n_total
            if ratio < 0.50:
                return False

    # ③ 空间维度：前20日振幅收敛
    if prev20_low > 0 and prev20_high > 0:
        amp = prev20_high / prev20_low - 1.0
        if amp > 0.30:
            return False

    return True


def analyze_stock(code: str, df: pd.DataFrame, float_mv: float | None = None) -> dict | None:
    """识别当前是否处于断板反包形态, 返回阶段描述或 None。

    阶段判定(只关心最近未完成/进行中的形态, 手册口径):
      STAGE1 吸筹+首板刚出: 最近5日内出现首板(横盘后大阳>7%或涨停), 且首板后1-3日内
              必须出现放量大阴线(断板)或累计涨幅≤12%(待洗盘窗口) —— 无断板/加速=剔除
      STAGE2 洗盘中: 首板后出现放量大阴(5日内)
      STAGE3 护盘期(伏击圈): 洗盘后 收盘未破首板开盘价, 处于小阴小阳横盘+缩量,
              且现价距支撑≤8%(贴近回踩区); 反包>3日前=滞后剔除; 偏离支撑=剔除
      STAGE4 已触发买点C: 护盘后放量突破首板最高价(须在首板后≤10日内且距今≤3日)

    🔴 2026-08-12 根源修复(用户截图核验触发): ①一字板剔除 ②连板排除 ③实体阴洗盘
       ④破位回归铁律 ⑤二波异动排除。见模块 docstring。
    """
    ddf = enrich(df)
    n = len(ddf)
    if n < 60:
        return None
    pct = ddf["pct"].values
    vol = ddf["volume"].values
    high, low, close, open_ = (
        ddf["high"].values,
        ddf["low"].values,
        ddf["close"].values,
        ddf["open"].values,
    )
    dates = ddf["date"].values

    # 股性过滤：20日均振幅>3%（手册 F5）
    amp_ma20 = ddf["amp_ma20"].iloc[-1]
    if not np.isfinite(amp_ma20) or amp_ma20 <= AMP_MIN:
        return None
    # 题材热度：近250日涨停≥3次（手册 F4，近1年多次涨停）
    limit_250 = ddf["limit_250"].iloc[-1]
    if not np.isfinite(limit_250) or limit_250 < HOT_LIMIT_UPS:
        return None

    # 🔴 2026-08-12 修复⑥: 计算MA60用于横盘吸筹质量检查
    ddf["ma60"] = ddf["close"].rolling(60).mean()
    ma60_arr = ddf["ma60"].values

    # 只在最近 FIND_WINDOW 日内找首板(关注进行中的形态; 首板+洗盘5日+护盘12日≈18日)
    first_candidates = []
    for i in range(max(40, n - FIND_WINDOW), n):
        if pct[i] < FIRST_PCT_MIN:
            continue
        recent = pct[max(0, i - 3) : i]
        if (recent >= 0.09).any():
            continue  # 排除连板(前3日涨停); 老妖股频繁涨停是常态, 不排除
        ma5_ = ddf["ma5"].iloc[i - 1]
        ma10_ = ddf["ma10"].iloc[i - 1]
        ma20_ = ddf["ma20"].iloc[i - 1]
        if not (np.isfinite(ma5_) and np.isfinite(ma10_) and np.isfinite(ma20_)):
            continue
        ma_max, ma_min = max(ma5_, ma10_, ma20_), min(ma5_, ma10_, ma20_)
        if ma_min <= 0 or (ma_max / ma_min - 1) > MA_CLUSTER_MAX:
            continue
        win_high = high[max(0, i - 20) : i].max()
        win_low = low[max(0, i - 20) : i].min()
        if win_low <= 0 or (win_high / win_low - 1) > PRE_RANGE_MAX:
            continue
        # 缩量优先：首板量 < 3×20日均量（过度放量=情绪顶点, 因子漏斗负因子, 已验证）
        v20 = ddf["vol_ma20"].iloc[i]
        if np.isfinite(v20) and vol[i] >= v20 * FIRST_VOL_MAX_RATIO:
            continue
        # 修复①: 一字板/秒板剔除
        if _is_one_word_board(open_[i], high[i], low[i]):
            continue
        # 修复②: 首板后1-2日内再次涨停=连板加速
        nxt2 = pct[i + 1 : min(i + 3, n)]
        if (nxt2 >= LIMIT_UP_RATIO - 0.005).any():
            continue
        # 修复⑤: 首板前20日内已有涨停=二波异动, 非"横盘后首次异动"
        prev20 = pct[max(0, i - 20) : i]
        if (prev20 >= LIMIT_UP_RATIO - 0.005).any():
            continue
        # 修复⑥: 横盘吸筹质量检查（第一性原理：首板=横盘吸筹后首次异动）
        # 三维度：趋势(MA60斜率) + 时间(在MA60附近天数) + 空间(振幅收敛)
        # 替换之前的调参式MA60斜率检查（针对华东重机/兆新/咸亨国际的补丁）
        prev20_close_vals = close[max(0, i - 20) : i]
        prev20_high = high[max(0, i - 20) : i].max()
        prev20_low = low[max(0, i - 20) : i].min()
        if not is_consolidation_base(
            close_price=close[i],
            ma60=ma60_arr[i],
            ma60_20ago=ma60_arr[max(0, i - 20)],
            prev20_close=prev20_close_vals,
            prev20_high=prev20_high,
            prev20_low=prev20_low,
        ):
            continue
        first_candidates.append(i)
    if not first_candidates:
        return None
    i = first_candidates[-1]  # 最近一个首板

    first_open = open_[i]
    high[i]
    support = first_open * 0.97  # 护盘底线(3%容忍, 仅作盘中刺穿容差参考)
    gap_up = open_[i] > close[i - 1] * 1.005

    # 🔴 2026-08-12 修复⑦: 首板成交额硬性门槛（手册: 成交额≥流通市值×1%, 不达标直接放弃）
    amount_arr = ddf["amount_est"].values
    first_amount = amount_arr[i]
    if float_mv is not None and float_mv > 0:
        mv_yuan = float_mv * 1e8  # 流通市值(亿→元)
        if first_amount < mv_yuan * FIRST_AMT_RATIO:
            return None  # 首板成交额不足流通市值×1%, 放弃

    # 修复③: 找洗盘(首板后5日内放量大阴; 手册 S2: 量≥1.5×首板量)
    # 洗盘日必须"实体阴线"(close<open)——pct口径漏天地板假阳(利柏特8/5)
    wash_i = None
    for wi in range(i + 1, min(i + 6, n)):
        v5 = ddf["vol_ma5"].iloc[wi]
        is_body_bear = _is_body_bear(close[wi], open_[wi])
        amp_wi = (high[wi] / low[wi] - 1.0) if low[wi] > 0 else 0.0
        body_drop = (open_[wi] - close[wi]) / open_[wi] if open_[wi] > 0 else 0.0
        is_heavy_shake = amp_wi > WASH_AMP_HEAVY and is_body_bear  # 天地板/巨震假阴
        if (
            is_body_bear
            and (body_drop > WASH_BODY_DROP_MIN or is_heavy_shake)
            and vol[wi] >= vol[i] * WASH_TURNOVER_MIN
            and (not np.isfinite(v5) or vol[wi] >= v5 * WASH_VOL_VS_MA5_MIN)
        ):
            wash_i = wi
            break
    if wash_i is None:
        # 首板后未洗盘: 当前要么继续涨(连板/加速, 不符合战法), 要么刚首板
        last = n - 1
        days_since = last - i
        # 断板验证——首板后1-3日内必须有放量大阴线(断板)
        for di in range(i + 1, min(i + 4, n)):
            v5 = ddf["vol_ma5"].iloc[di]
            is_body_bear = _is_body_bear(close[di], open_[di])
            amp_di = (high[di] / low[di] - 1.0) if low[di] > 0 else 0.0
            body_drop = (open_[di] - close[di]) / open_[di] if open_[di] > 0 else 0.0
            if (
                is_body_bear
                and (body_drop > WASH_BODY_DROP_MIN or (amp_di > WASH_AMP_HEAVY and is_body_bear))
                and vol[di] >= vol[i] * WASH_TURNOVER_MIN
                and (not np.isfinite(v5) or vol[di] >= v5 * WASH_VOL_VS_MA5_MIN)
            ):
                wash_i = di  # 补识别: 首板后1-3日内有断板 → 按正常流程走
                break
        if wash_i is not None:
            pass  # 跳回正常洗盘处理
        elif days_since <= 2:
            # 首板后累计涨幅>12% = 加速/连板, 不是"待洗盘", 剔除
            cum_gain = close[last] / close[i] - 1.0
            if cum_gain > 0.12:
                return None
            return {
                "stage": "STAGE1_首板待洗盘",
                "first_open": first_open,
                "support": support,
                "gap_up": gap_up,
                "wash_high": None,
                "last_close": close[last],
                "last_date": str(dates[last])[:10],
            }
        else:
            # 首板后 >2日 且无断板 → 连板/加速, 不符合战法
            return None

    # 护盘期检查（手册: 三破三收 + 1日观察期 + 超10天剔除）
    protect_start = wash_i + 1
    last = n - 1
    # 🔴 修复⑦: 支撑底线 = 首板开盘价（去掉0.97容忍线, 手册禁止自定义容忍线）
    support = first_open
    # 洗盘日==今天(刚断板, 护盘期0天) → 直接返回"洗盘中"状态
    if protect_start > last:
        return {
            "stage": "STAGE2_洗盘中(刚断板)",
            "first_open": first_open,
            "support": support,
            "gap_up": gap_up,
            "wash_high": high[wash_i],
            "last_close": close[last],
            "last_date": str(dates[last])[:10],
            "days_since_wash": 0,
            "touching_support": False,
        }

    # 三破三收: 跌破支撑后3日内必须收回; 1日观察期: 单日跌破不判死
    # 手册: 首板后3天允许跌破(三破), 3日内必须收回(三收); 单日跌破给1日观察期
    # 同时检查: 超10个交易日无反包 = 剔除
    broken = False
    broken_day = None
    observation = False  # 正处观察期内（跌破后1日, 待明日确认）
    for d in range(protect_start, last + 1):
        if close[d] < support:
            # 检查之后3日内是否收回
            recovered = False
            for r in range(d + 1, min(d + 4, last + 1)):
                if close[r] >= support:
                    recovered = True
                    break
            if not recovered:
                # 三破未收: 若跌破日距今 <= 1日(观察期内), 不算失败
                if d >= last - 1:
                    observation = True
                    broken_day = str(dates[d])[:10]
                    break
                broken = True
                broken_day = str(dates[d])[:10]
                break
    if broken:
        return {
            "stage": "STAGE2_洗盘后破位(形态失败)",
            "first_open": first_open,
            "support": support,
            "gap_up": gap_up,
            "wash_high": high[wash_i],
            "last_close": close[last],
            "last_date": str(dates[last])[:10],
            "broken_day": broken_day,
            "fail": True,
        }

    # 超10天剔除: 洗盘后超过10个交易日无反包 → 剔除
    # 但若当前处于观察期内(刚跌破1日), 不剔除
    days_since_wash = last - wash_i
    if not observation and days_since_wash > 10:
        return {
            "stage": "STAGE3_护盘超10天(剔除)",
            "first_open": first_open,
            "support": support,
            "gap_up": gap_up,
            "wash_high": high[wash_i],
            "last_close": close[last],
            "last_date": str(dates[last])[:10],
            "days_since_wash": days_since_wash,
            "stale": True,
        }

    # 🔴 2026-08-12 修复⑧: 护盘期质量检查——排除假护盘/假反包/洗盘未完成
    # 用户在K线图核验中发现: 西部建设(护盘重心下移+假反弹)、益生股份(洗盘后继续放量)
    # 被误判为"伏击圈"。三点质量判定:
    # ① 洗盘未完成: 洗盘日后最近K线仍是放量阴线(量≥首板量) → 抛压还在释放
    # ② 假反弹: 护盘期内出现放量阳线(量≥1.5×首板量)但收盘已跌破该阳线开盘
    #    → 放量冲高回落=出货/诱多, 不是护盘(西部建设8-10放量5.31→8-11回落)
    # ③ 重心下移: 洗盘后横盘期间 最新收盘 < 洗盘日后最高收盘×0.98
    #    → 护盘高点持续下移=弱势, 不构成"缩量护盘"横盘
    # 恒宝(真信号) 8-10~8-12 缩量小阴小阳横盘: 无放量阳线、重心平稳 → 通过
    vol[wash_i]
    protect_high_close = (
        close[protect_start:last].max() if protect_start <= last - 1 else close[last]
    )
    # ① 洗盘未完成: 最后一根仍是放量阴线(量≥首板量, 对比首板量而非洗盘量)
    if close[last] < open_[last] and vol[last] >= vol[i] * WASH_TURNOVER_MIN:
        return {
            "stage": "STAGE3_护盘期·洗盘未完成(继续放量阴线)",
            "first_open": first_open,
            "support": support,
            "gap_up": gap_up,
            "wash_high": high[wash_i],
            "last_close": close[last],
            "last_date": str(dates[last])[:10],
            "days_since_wash": days_since_wash,
            "stale": True,
        }
    # ② 假反弹: 护盘期内出现放量阳线(量≥2×首板量, 涨幅≥3%), 之后收盘跌破该阳线收盘价
    # 收紧阈值: 1.5倍太宽(恒宝08-11小阳线+0.74%量1.57倍误触), 改为2倍+3%涨幅
    for d in range(protect_start, last + 1):
        pct_d = (close[d] - open_[d]) / open_[d] if open_[d] > 0 else 0
        if close[d] > open_[d] and vol[d] >= vol[i] * 2.0 and pct_d >= 0.03:
            # 找到放量阳线后, 检查最新收盘是否已跌破该阳线收盘(冲高回落)
            if last > d and close[last] < close[d]:
                return {
                    "stage": "STAGE3_护盘期·假反弹(放量冲高回落)",
                    "first_open": first_open,
                    "support": support,
                    "gap_up": gap_up,
                    "wash_high": high[wash_i],
                    "last_close": close[last],
                    "last_date": str(dates[last])[:10],
                    "days_since_wash": days_since_wash,
                    "stale": True,
                }
    # ③ 重心下移: 护盘高点持续下移(最新收盘 < 洗盘后最高收盘×0.98)
    # 恒宝 10.71/10.90=0.983>0.98 通过; 西部建设 4.91/5.09=0.965<0.98 剔除
    if days_since_wash >= 2 and protect_high_close > 0 and close[last] < protect_high_close * 0.98:
        return {
            "stage": "STAGE3_护盘期·重心下移(弱势)",
            "first_open": first_open,
            "support": support,
            "gap_up": gap_up,
            "wash_high": high[wash_i],
            "last_close": close[last],
            "last_date": str(dates[last])[:10],
            "days_since_wash": days_since_wash,
            "stale": True,
        }

    # 缩量检查(护盘期量能萎缩): 最近3日量 vs 洗盘量
    recent_vol = vol[max(protect_start, last - 2) : last + 1]
    shrink_ratio = recent_vol.min() / vol[wash_i] if vol[wash_i] > 0 else 1
    shrink_ok = shrink_ratio <= PROTECT_SHRINK_MAX

    # 当前是否回踩支撑(买点A候选): 收盘 ≥ 首板开盘价(铁律)
    touching = low[last] <= first_open * TOUCH_OPEN_TOL and close[last] >= first_open

    # 反包确认（手册 S4：实体大阳线完整覆盖洗盘阴线, 成交额=流通市值×2%~3%）
    breakout = False
    breakout_day = None
    for d in range(protect_start, last + 1):
        # 反包窗口约束——反包必须发生在首板后 ≤10 个交易日内
        if d - i > REBOUND_WINDOW:
            break
        v5 = ddf["vol_ma5"].iloc[d]
        is_rebound_pct = pct[d] > 0.05 or pct[d] >= (LIMIT_UP_RATIO - 0.005)
        if (
            close[d] > high[wash_i]  # 🔴 修复⑦: 覆盖洗盘阴线(手册), 非首板最高价
            and close[d] > open_[d]  # 阳线
            and is_rebound_pct  # 涨幅>5%或涨停
            and (np.isfinite(v5) and vol[d] >= v5 * BREAKOUT_VOL_MIN)  # 放量
        ):
            # 🔴 修复⑦: 成交额检查(手册: 流通市值×2%~3%)
            if float_mv is not None and float_mv > 0:
                mv_yuan = float_mv * 1e8
                amt = amount_arr[d]
                if not (mv_yuan * REBOUND_AMT_MIN <= amt <= mv_yuan * REBOUND_AMT_MAX):
                    continue  # 成交额不达标
                # 爆量红线: 成交额 > 首板×2 = 倍量抛压, 规避
                if amt > first_amount * DANGER_AMT_RATIO:
                    continue
            breakout = True
            breakout_day = d
            break

    # 反包时效约束——反包必须发生在最近3个交易日内才算"已触发买点C"
    if breakout and breakout_day is not None and last - breakout_day > REBOUND_FRESH_DAYS:
        return {
            "stage": "STAGE3_反包已走完(滞后剔除)",
            "first_open": first_open,
            "support": support,
            "gap_up": gap_up,
            "wash_high": high[wash_i],
            "last_close": close[last],
            "last_date": str(dates[last])[:10],
            "shrink_ratio": shrink_ratio,
            "days_since_wash": last - wash_i,
            "touching_support": touching,
            "breakout_days_ago": last - breakout_day,
            "stale": True,
        }

    # 伏击圈收紧——现价必须贴近支撑(距支撑≤8%)才算"等回踩", 且未突破洗盘高点
    dist_support = close[last] / support - 1.0 if support > 0 else 0.0
    above_wash_high = close[last] > high[wash_i]
    if not breakout and dist_support > DIST_SUPPORT_MAX:
        return {
            "stage": "STAGE3_伏击圈·偏离支撑(剔除)",
            "first_open": first_open,
            "support": support,
            "gap_up": gap_up,
            "wash_high": high[wash_i],
            "last_close": close[last],
            "last_date": str(dates[last])[:10],
            "shrink_ratio": shrink_ratio,
            "days_since_wash": last - wash_i,
            "touching_support": touching,
            "dist_support": round(dist_support, 3),
            "above_wash_high": bool(above_wash_high),
            "stale": True,
        }

    if breakout:
        stage = "STAGE4_已触发买点C(放量反包)"
    elif shrink_ok and touching:
        stage = "STAGE3_伏击圈·买点A(缩量回踩支撑)"
    elif shrink_ok:
        stage = "STAGE3_伏击圈·缩量护盘(等回踩/突破)"
    else:
        stage = "STAGE3_护盘中·量能未萎缩(等待)"

    return {
        "stage": stage,
        "first_open": first_open,
        "support": support,
        "gap_up": gap_up,
        "wash_high": high[wash_i],
        "last_close": close[last],
        "last_date": str(dates[last])[:10],
        "shrink_ratio": shrink_ratio,
        "days_since_wash": last - wash_i,
        "touching_support": touching,
        "rule_version": RULE_VERSION,
        # 🔴 2026-08-12 二次裁决修复: 返回索引, 消除 check_optimized_a 反匹配漂移
        "first_idx": int(i),
        "wash_idx": int(wash_i),
    }


def check_optimized_a(
    df: pd.DataFrame, res: dict, first_idx: int | None = None, wash_idx: int | None = None
) -> tuple[bool, dict]:
    """数据裁决版买点A 四条件（2026-08-11 因子漏斗实验，331事件裁决）：

      ① 首板涨停(≥9.5%)     ← 资金确认，非普通大阳
      ② 首板量 < 3×20日均量  ← 排除过度放量的情绪顶点(实测 -2.41% p=0.0002 显著为负)
      ③ 洗盘量 ≥ 1.5×首板量  ← 主力主动洗盘(放量大阴)
      ④ 护盘缩量 ≤ 0.5×洗盘量 ← 主力没跑(洗而不破)

    回测效果(买点A·5日): 全量 +0.01% → 4条件 +3.78%/胜率53.7%/超额+3.38%/p=0.0504,
    三年全正(2024+3.81/2025+3.30/2026+4.26)。探索性发现, 实盘前向验证中。

    🔴 2026-08-12 二次裁决修复: 调用方（scan_rebound_market）在 analyze_stock 同一份
    df 上已定位好 first_idx/wash_idx，直接传入用索引算 4 条件，**不再做价格反匹配**。
    旧版用 abs(open-first_open)<0.01 反定位首板日、abs(high-wash_high)<0.02 反定位洗盘日，
    qfq 前复权价格跨运行漂移 → 定位失败 → 四条件误判 False（恒宝/联创翻转根因）。已修。
    兼容性: 若调用方没传索引（旧回测脚本），仍走旧反匹配路径。

    返回 (is_optimized, factors_dict)；任何一步定位失败返回 (False, {})。
    """
    ddf = enrich(df)
    n = len(ddf)
    first_open = res.get("first_open")
    wash_high = res.get("wash_high")
    if first_open is None or wash_high is None or n < 40:
        return False, {}
    # 定位首板日（open≈首板开盘 且 涨幅≥6.7%）
    if first_idx is None:
        first_idx = None
        for i in range(max(0, n - 30), n):
            if abs(ddf["open"].iloc[i] - first_open) < 0.01 and ddf["pct"].iloc[i] >= 0.067 - 0.003:
                first_idx = i
                break
        if first_idx is None:
            return False, {}
    # 定位洗盘日（high≈洗盘日高 且 在首板之后）
    # 洗盘日必须实体阴线——防止 high 匹配把假阳/一字板当日当洗盘
    if wash_idx is None:
        wash_idx = None
        for i in range(first_idx + 1, min(n, first_idx + 8)):
            if abs(ddf["high"].iloc[i] - wash_high) < 0.02:
                body_bear = ddf["close"].iloc[i] < ddf["open"].iloc[i]
                amp_i = (
                    (ddf["high"].iloc[i] / ddf["low"].iloc[i] - 1.0)
                    if ddf["low"].iloc[i] > 0
                    else 0.0
                )
                if body_bear or amp_i > 0.10:
                    wash_idx = i
                    break
        if wash_idx is None:
            return False, {}
    # 索引合法性校验（防越界/防语义错位）
    if not (0 <= first_idx < n and 0 <= wash_idx < n and wash_idx > first_idx):
        return False, {}

    first_vol = ddf["volume"].iloc[first_idx]
    first_pct = ddf["pct"].iloc[first_idx]
    v20 = ddf["volume"].iloc[max(0, first_idx - 20) : first_idx].mean()
    first_vol_ratio = first_vol / v20 if v20 > 0 else 99.0
    wash_vol = ddf["volume"].iloc[wash_idx]
    wash_vol_ratio = wash_vol / first_vol if first_vol > 0 else 0.0
    protect_vols = ddf["volume"].iloc[wash_idx + 1 :]
    protect_shrink = protect_vols.min() / wash_vol if len(protect_vols) and wash_vol > 0 else 1.0

    cond1 = first_pct >= 0.095 - 0.003
    cond2 = first_vol_ratio < 3.0
    cond3 = wash_vol_ratio >= 1.5
    cond4 = protect_shrink <= 0.5
    # 显式 bool()：numpy bool 不是 JSON serializable，实盘写 signals 会崩
    return bool(cond1 and cond2 and cond3 and cond4), {
        "first_pct": round(float(first_pct), 4),
        "first_vol_ratio": round(float(first_vol_ratio), 2),
        "wash_vol_ratio": round(float(wash_vol_ratio), 2),
        "protect_shrink": round(float(protect_shrink), 3),
    }


def v2_visual_score(d: pd.DataFrame) -> float | None:
    """V2无前视视觉评分(0-10) —— 只用当日及之前信息(前视偏差铁律)
    事件日=最后一根K线(扫描日). 特征全部在收盘时刻已知. 详见 rebound-pattern-vision 技能.

    ⚠️ 2026-08-07 验证翻案: 高分(≥7)与低分(<5)无显著差异(-0.57% p=0.50),
    定位为"减亏过滤器"而非盈利信号——仅用于信号排序参考, 不作为入场依据.
    """
    idx = len(d) - 1
    if idx < 3:
        return None
    s = 0.0
    day = d.iloc[idx]
    # F1 事件日收阴: 买在回调日而非反弹日(唯一数据验证的正方向)
    if day["close"] < day["open"]:
        s += 1.5
    # F2 断板日缩量 (idx-1为断板日, 对比idx-2)
    brk = d.iloc[idx - 1]
    if brk["volume"] < d.iloc[idx - 2]["volume"]:
        s += 1.5
    # F3 断板日阴线
    if brk["close"] < brk["open"]:
        s += 1.2
    # F4 事件日缩量(回调末期地量)
    if day["volume"] < d.iloc[idx - 1]["volume"]:
        s += 1.2
    # F5 回调不破首板开盘价
    rets = d["close"].pct_change()
    fbs = d.index[(rets > 0.07) & (d.index < idx)].tolist()
    if fbs:
        fb = fbs[-1]
        between = d.iloc[fb + 1 : idx + 1]
        if len(between) and between["low"].min() > d.iloc[fb]["open"] * 0.97:
            s += 1.5
    # F6 有首板
    if fbs:
        s += 1.0
    # F7 首板新鲜度
    if fbs:
        gap = idx - fbs[-1]
        if 3 <= gap <= 8:
            s += 1.0
        elif gap <= 15:
            s += 0.5
    # F8 回调深度浅
    pre5 = d.iloc[max(0, idx - 5) : idx]
    if len(pre5) >= 2:
        drop = pre5["close"].iloc[0] / pre5["close"].iloc[-1] - 1
        if drop < 0.06:
            s += 1.1
    return round(min(s, 10), 1)
