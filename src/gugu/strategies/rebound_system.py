"""断板反包·陈学长实战体系（rebound_system）——手册完整版策略。

来源：陈学长不吃面「断板反包实战体系」系统提示词最终版（2026-08-12 用户手册）。

═══════════════════════════════════════════════════════════
第一性原理
═══════════════════════════════════════════════════════════
主力（批发商）在无人关注时悄悄收集筹码（吸筹），拉高吸引注意力（首板），
暴力洗盘清理浮筹（断板大阴），护盘确认成本底线（不破首板开盘价），
最后拉升出货（反包）。我们的任务：洗盘结束、拉升前夕跟随主力介入。

核心铁律：首板开盘价 = 主力短期成本底线。收盘有效跌破 = 形态失败。

═══════════════════════════════════════════════════════════
手册 → 代码映射（逐条对照，避免漏条件）
═══════════════════════════════════════════════════════════
一、硬性过滤（一票否决，全部通过才继续）：
  F1 总市值 50亿 ≤ MV ≤ 150亿          → mv_min / mv_max
  F2 非ST / 非创业板300 / 非科创板688   → 代码前缀（策略层不做，扫描器做）
  F3 收盘价 > 3元                       → price_min
  F4 题材热度：主流热点或历史辨识度      → 近250日涨停次数 ≥ hot_limit_ups（多次涨停=辨识度）
  F5 股性：历史涨停多次 + 日均振幅>3%   → 近250日涨停次数 + 振幅过滤 amp_min

二、形态四阶段（识别当前所处阶段）：
  S1 吸筹/异动（首板）：横盘后缩量涨停(≥9.5%)或大阳(>7%)，优先跳空
     量化：首板成交额 ≈ 流通市值×1%（±0.3%），缩量优先（首板量 < 20日均量×limit_vol_cap）
  S2 暴力洗盘（断板）：首板次日(或隔日)放量大阴线
     量化：跌幅 ≤ -wash_drop（-3%），成交额 > 首板成交额，换手放大至首板 1.5倍+
  S3 护盘（支撑确认）：小阴小阳横盘，不破首板开盘价（允许盘中刺穿，收盘收回）
     量化：收盘 ≥ 首板开盘价×（1-support_tol）；横盘期成交量持续萎缩
  S4 反包（启动确认）：放量大阳线（>5% 或涨停），收盘 > 首板最高价
     量化：反包日成交额 ≥ 流通市值×（rebound_amt_min ~ rebound_amt_max = 1.5%~2%）

三、买点（三选一，入场信号）：
  A 缩量回踩支撑：回踩首板开盘价/20日均线不破，分时缩量（日线近似：收盘守住支撑+缩量）
  B 分时底背离：盘中急跌创新低，MACD黄白线不创新低 + 缩量（日线近似：MACD底背离）
  C 有效放量追击：早盘30分钟首次上冲成交额达流通市值×0.5%（日线近似：放量突破）
     ⚠️ 历史验证（2026-08-11）：买点C 动态池/市值两档均显著为负 → 默认禁用，experimental

四、卖出（触发任一即执行）：
  TP1 反包即减仓（铁律）：买入后出现反包大阳线（>5%或涨停）→ 减仓50%
  TP2 分时顶背离：价创新高，MACD黄白线不创新高 + 缩量
  TP3 量价背离（涨停未遂）：拉到8-9%但实时成交额 < 流通市值×1%，上攻乏力回落 → 清仓
  TP4 到达压力位：触及前高或BOLL上轨 → 分批止盈（50%→剩余）
  SL1 破位止损：收盘跌破首板开盘价（或20日均线）且无法收回 → 次日开盘无条件清仓
  SL2 次日不及预期：买入次日低开-3%以上，前15分钟无快速拉升收复 → 早盘反抽清仓

五、仓位：单票≤4成，3-5只分散，分批2+2（策略层仅输出信号，仓位由风控层执行）

═══════════════════════════════════════════════════════════
历史验证结论（2026-08-11 裁决，必须遵守）
═══════════════════════════════════════════════════════════
- 市值 50-150亿：维持（30-50亿 p=0.031 显著为负，原文偏好证伪）✅ 手册一致
- 买点A（护盘低吸）：动态池 +0.73%/笔 p=0.53 存疑 → conditional_pass，实盘积累
- 买点A 优化四条件（首板涨停 + 首板量<3×20日均量 + 洗盘量≥1.5×首板 + 护盘缩量≤0.5×洗盘）
  → 回测 +3.78%/胜率53.7%/p=0.0504，三年全正。作为 buy_point_a_optimized 开关（默认 True）
- 买点C（放量反包追涨）：动态池 -3.63% p<0.0001 / 市值分层再证伪 → 默认禁用
- V2视觉评分：无 alpha → 仅作参考分，不参与入场过滤
"""

from __future__ import annotations

from typing import TypedDict

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class _BuyInfo(TypedDict):
    """买入触发信息。"""

    type: str
    first_open: float
    support: float
    first_i: int
    wash_i: int


class ReboundSystemStrategy(Strategy):
    """断板反包·陈学长实战体系完整版（手册逐条量化）。"""

    name = "rebound_system"
    category = "main_force"  # 短线主力行为类

    # ── 手册硬性过滤 ──────────────────────────
    def validate_params(self) -> None:
        mv_min = float(self.params.get("mv_min", 50))
        mv_max = float(self.params.get("mv_max", 150))
        if mv_min >= mv_max:
            raise ValueError("mv_min 必须 < mv_max")
        price_min = float(self.params.get("price_min", 3.0))
        if price_min <= 0:
            raise ValueError("price_min 必须 > 0")
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= limit_up_pct <= 0.11:
            raise ValueError("limit_up_pct 应在 5%-11%")
        amp_min = float(self.params.get("amp_min", 0.03))
        if amp_min <= 0:
            raise ValueError("amp_min 必须 > 0")

    def _ensure_columns(self, df: pd.DataFrame) -> None:
        required = ["open", "high", "low", "close", "volume"]
        for c in required:
            if c not in df.columns:
                raise ValueError(f"缺少必要列: {c}")
        # amount 可选：没有则用 volume×100×close 估算
        if "amount" not in df.columns:
            df["amount"] = df["volume"] * 100.0 * df["close"]

    @staticmethod
    def _est_amount(df: pd.DataFrame) -> pd.Series:
        """成交额估算（元）：volume(手)×100股×均价。腾讯K线只有 volume 无 amount。"""
        if "amount" in df.columns and df["amount"].abs().sum() > 0:
            return df["amount"]
        return df["volume"] * 100.0 * df["close"]

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """手册完整版信号生成。

        输出列（除 signal/confidence 外附加诊断列）：
          - stage:        当前形态阶段（STAGE1~4 / NONE）
          - first_open:   首板开盘价（主力成本底线）
          - support:      护盘支撑价
          - buy_point:    触发买点类型（A/B/C）
          - exit_reason:  卖出触发原因（TP1~4 / SL1~2）
        """
        self._ensure_columns(df)
        df = df.copy()
        n = len(df)
        if n < 60:
            df["signal"] = 0
            df["confidence"] = 0.0
            return df

        # ── 参数 ──────────────────────────────
        mv_min = float(self.params.get("mv_min", 50))
        mv_max = float(self.params.get("mv_max", 150))
        price_min = float(self.params.get("price_min", 3.0))
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        big_yang_pct = float(self.params.get("big_yang_pct", 0.07))
        hot_limit_ups = int(self.params.get("hot_limit_ups", 3))  # 近250日涨停≥3次=辨识度
        amp_min = float(self.params.get("amp_min", 0.03))  # 日均振幅>3%
        first_amt_ratio = float(self.params.get("first_amt_ratio", 0.01))  # 首板成交额≈流通市值1%
        first_amt_tol = float(self.params.get("first_amt_tol", 0.003))  # ±0.3%
        limit_vol_cap = float(
            self.params.get("limit_vol_cap", 3.0)
        )  # 首板量<3×20日均量（过度放量=出货）
        support_tol = float(
            self.params.get("support_tol", 0.0)
        )  # 护盘容忍（手册：收盘必须守住，0容忍）
        shrink_ratio = float(self.params.get("shrink_ratio", 0.5))  # 缩量：≤前一日50%
        rebound_amt_min = float(
            self.params.get("rebound_amt_min", 0.015)
        )  # 反包成交额≥流通市值1.5%
        rebound_amt_max = float(self.params.get("rebound_amt_max", 0.02))  # ≤2%
        valid_vol_ratio = float(self.params.get("valid_vol_ratio", 0.005))  # 有效放量=流通市值0.5%
        danger_amt_ratio = float(self.params.get("danger_amt_ratio", 0.02))  # 倍量危险=流通市值2%
        search_window = int(self.params.get("search_window", 25))  # 首板回溯窗口
        wash_gap = int(self.params.get("wash_gap", 5))  # 首板后找洗盘最大间隔
        a_optimized = bool(self.params.get("buy_point_a_optimized", True))  # 优化四条件
        b_enabled = bool(self.params.get("buy_point_b_enabled", True))  # 买点B
        c_enabled = bool(self.params.get("buy_point_c_enabled", False))  # 买点C（历史证伪，默认关）
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.03))  # SL2 次日低开-3% 判定
        max_hold_days = int(self.params.get("max_hold_days", 15))

        # ── 基础指标 ──────────────────────────
        df["pct"] = df["close"].pct_change()
        df["amount_est"] = self._est_amount(df)
        df["amp"] = (df["high"] - df["low"]) / df["close"].shift(1)  # 当日振幅
        df["amp_ma20"] = df["amp"].rolling(20).mean()  # 20日均振幅
        df["vol_ma20"] = df["volume"].rolling(20).mean()
        df["vol_ma5"] = df["volume"].rolling(5).mean()  # 2026-08-12: rules.is_valid_wash_day 需要
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_ma20"].replace(0, np.nan)
        df["is_limit_up"] = df["pct"] >= (limit_up_pct - 0.005)

        # 流通市值（亿）列（可选，扫描器传入；策略层没有则跳过成交额绝对值判断）
        has_mv = bool("float_mv" in df.columns and df["float_mv"].notna().any())
        # 总市值列（可选）：启用 F1 市值过滤
        has_total_mv = bool("total_mv" in df.columns and df["total_mv"].notna().any())
        mv_arr = df["total_mv"].to_numpy(dtype=float) if has_total_mv else None

        # MACD（买点B/顶背离用）
        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["dif"] = ema12 - ema26
        df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = (df["dif"] - df["dea"]) * 2

        # BOLL（止盈4压力位）
        df["boll_mid"] = df["close"].rolling(20).mean()
        boll_std = df["close"].rolling(20).std()
        df["boll_up"] = df["boll_mid"] + 2 * boll_std

        # ── 信号列初始化 ──────────────────────
        df["signal"] = 0
        df["confidence"] = 0.0
        df["stage"] = "NONE"
        df["first_open"] = np.nan
        df["support"] = np.nan
        df["buy_point"] = ""
        df["exit_reason"] = ""

        # ══════════════════════════════════════
        # 形态识别：逐日扫描（无前视：只用当日及之前信息）
        # ══════════════════════════════════════
        triggers_buy: dict[int, _BuyInfo] = {}  # 买入触发日 → 详情

        # 近250日涨停次数（滚动，辨识度/股性过滤）
        df["limit_250"] = df["is_limit_up"].rolling(250, min_periods=30).sum()

        # 20日均振幅（股性）
        df["amp_ok"] = df["amp_ma20"] > amp_min

        for i in range(40, n):  # 从第40根开始（需要20日均线等前置）
            row = df.iloc[i]

            # ── 硬性过滤（逐项一票否决）──────────
            # 价格
            if row["close"] <= price_min:
                continue
            # 市值（若有 total_mv 列）
            if mv_arr is not None:
                mv_t = float(mv_arr[i])
                if not (mv_min <= mv_t <= mv_max):
                    continue
            # 振幅/股性
            if not row["amp_ok"] or np.isnan(row["amp_ok"]):
                continue
            # 题材热度/辨识度：近250日涨停≥hot_limit_ups（含当日）
            if np.isnan(row["limit_250"]) or row["limit_250"] < hot_limit_ups:
                continue

            # ── 找最近一次首板（search_window 内）──
            # 首板 = 涨停 或 大阳（>7%），且之前横盘（前5日无大阳/无涨停）
            first_i = None
            for j in range(i, max(i - search_window, 40), -1):
                rj = df.iloc[j]
                if not (rj["is_limit_up"] or rj["pct"] > big_yang_pct):
                    continue
                # 排除连板：前3日无涨停
                if df["is_limit_up"].iloc[max(0, j - 3) : j].any():
                    continue
                # 横盘前置：前20日区间窄幅（首板前低位）
                win_high = df["high"].iloc[max(0, j - 20) : j].max()
                win_low = df["low"].iloc[max(0, j - 20) : j].min()
                if win_low <= 0 or (win_high / win_low - 1) > 0.50:
                    continue
                first_i = j
                break
            if first_i is None:
                continue

            first_open = float(df.loc[df.index[first_i], "open"])
            first_high = float(df.loc[df.index[first_i], "high"])
            first_amt = float(df.loc[df.index[first_i], "amount_est"])
            support = first_open * (1 - support_tol)

            # ── 首板成交额校验（流通市值×1% ±0.3%）──
            if has_mv:
                mv = float(row["float_mv"])
                target = mv * 1e8 * first_amt_ratio
                if not (
                    target * (1 - first_amt_tol / first_amt_ratio)
                    <= first_amt
                    <= target * (1 + first_amt_tol / first_amt_ratio)
                ):
                    continue  # 首板成交额不达标 → 不是真实封板
            # 缩量优先：首板量 < limit_vol_cap × 20日均量（过度放量=情绪顶点，因子漏斗验证负因子）
            v20 = df["vol_ma20"].iloc[first_i]
            if np.isfinite(v20) and df["volume"].iloc[first_i] >= v20 * limit_vol_cap:
                continue

            # ── 找洗盘（首板后 wash_gap 日内放量大阴）──
            # 🔴 2026-08-12: 改用 rules.py 的 is_valid_wash_day (实体阴线, 消灭三套规则漂移)
            wash_i = None
            first_vol = df["volume"].iloc[first_i]
            from gugu.strategies.rebound.rules import is_valid_wash_day

            for wi in range(first_i + 1, min(first_i + wash_gap + 1, i + 1)):
                wrow = df.iloc[wi]
                if is_valid_wash_day(
                    float(wrow["open"]),
                    float(wrow["close"]),
                    float(wrow["high"]),
                    float(wrow["low"]),
                    float(wrow["volume"]),
                    float(first_vol),
                    float(df["vol_ma5"].iloc[wi]),
                ):
                    wash_i = wi
                    break
            if wash_i is None:
                # 首板后未洗盘：仅首板刚出时标记 STAGE1
                if i - first_i <= 2:
                    df.loc[df.index[i], "stage"] = "STAGE1_首板待洗盘"
                    df.loc[df.index[i], "first_open"] = first_open
                    df.loc[df.index[i], "support"] = support
                continue

            # ── 护盘检查：洗盘后至今，收盘不破首板开盘价 ──
            protect_start = wash_i + 1
            broken = False
            for d in range(protect_start, i + 1):
                if df["close"].iloc[d] < support:
                    broken = True
                    break
            if broken:
                # 破位 = 形态失败
                df.loc[df.index[i], "stage"] = "STAGE2_破位(形态失败)"
                df.loc[df.index[i], "first_open"] = first_open
                df.loc[df.index[i], "support"] = support
                continue

            # 洗盘后至今天数
            days_since_wash = i - wash_i
            # 横盘期量能是否持续萎缩：最近3日量 ≤ 洗盘量 × shrink_ratio
            recent_vol = df["volume"].iloc[max(protect_start, i - 2) : i + 1]
            shrink_ok = recent_vol.max() <= df["volume"].iloc[wash_i] * shrink_ratio

            # ── 反包判定（S4）：放量大阳 >5% 或涨停，收盘 > 首板最高价 ──
            rebound_pct = float(row["pct"]) if np.isfinite(row["pct"]) else 0.0
            is_rebound = (
                (rebound_pct > 0.05 or row["is_limit_up"])
                and row["close"] > first_high
                and row["close"] > row["open"]
            )
            # 反包成交额 ≥ 流通市值×1.5%~2%（若可判断）
            rebound_amt_ok = True
            if has_mv:
                mv = float(row["float_mv"])
                rebound_amt_ok = (
                    row["amount_est"] >= mv * 1e8 * rebound_amt_min
                    and row["amount_est"] <= mv * 1e8 * rebound_amt_max
                )

            # ── 买点判定 ──────────────────────
            buy_signal = 0
            buy_type = ""
            confidence = 0.0

            # 买点A：缩量回踩支撑（收盘守住首板开盘价/20日均线 + 缩量）
            touching = row["low"] <= first_open * 1.03 and row["close"] >= support
            vol_shrink_today = row["volume"] <= df["volume"].iloc[i - 1] * 1.05
            if touching and vol_shrink_today and not is_rebound:
                # 优化版买点A四条件（因子漏斗验证有效）：
                #   ① 首板涨停≥9.5%  ② 首板量<3×20日均量  ③ 洗盘量≥1.5×首板  ④ 护盘缩量≤0.5×洗盘
                if a_optimized:
                    first_is_limit = df["is_limit_up"].iloc[first_i]
                    cond2 = not (np.isfinite(v20) and df["volume"].iloc[first_i] >= v20 * 3.0)
                    cond3 = df["volume"].iloc[wash_i] >= df["volume"].iloc[first_i] * 1.5
                    cond4 = shrink_ok  # 最近3日量 ≤ 洗盘量×0.5
                    if first_is_limit and cond2 and cond3 and cond4:
                        buy_signal, buy_type = 1, "A"
                        confidence = 0.55 + 0.15 * min(days_since_wash / 8.0, 1.0)
                    else:
                        buy_signal, buy_type = 0, ""  # 不满足优化四条件 → 不触发（验证结论）
                else:
                    buy_signal, buy_type = 1, "A"
                    confidence = 0.5

            # 买点B：底背离（价创新低，MACD DIF 不创新低 + 缩量）——日线近似
            if buy_signal == 0 and b_enabled and i >= 5:
                recent_low = (
                    df["low"].iloc[protect_start:i].min() if protect_start < i else row["low"]
                )
                dif_now = row["dif"]
                dif_min_5 = df["dif"].iloc[max(protect_start, i - 5) : i].min()
                # 接近区间低点 + DIF 不创新低 + 缩量
                if (
                    row["low"] <= recent_low * 1.01
                    and np.isfinite(dif_now)
                    and np.isfinite(dif_min_5)
                    and dif_now > dif_min_5
                    and row["volume"] <= df["volume"].iloc[i - 1] * 0.8
                ):
                    buy_signal, buy_type = 1, "B"
                    confidence = 0.45

            # 买点C：有效放量追击（手册：早盘30分钟首次上冲成交额≥流通市值×0.5%；
            # 日线近似：放量反包 + 成交额≥流通市值×valid_vol_ratio）——历史证伪，默认关闭
            if buy_signal == 0 and c_enabled and is_rebound and rebound_amt_ok:
                amt_now = float(row["amount_est"])
                mv_c = float(row["float_mv"]) if has_mv else 0.0
                valid_ok = amt_now >= mv_c * 1e8 * valid_vol_ratio
                if valid_ok:
                    buy_signal, buy_type = 1, "C"
                    confidence = 0.5

            # ── 阶段标记 ──────────────────────
            if is_rebound and rebound_amt_ok:
                stage = "STAGE4_反包启动"
            elif shrink_ok:
                stage = "STAGE3_护盘缩量"
            else:
                stage = "STAGE3_护盘中"
            df.loc[df.index[i], "stage"] = stage
            df.loc[df.index[i], "first_open"] = first_open
            df.loc[df.index[i], "support"] = support

            if buy_signal == 1:
                triggers_buy[i] = {
                    "type": buy_type,
                    "first_open": first_open,
                    "support": support,
                    "first_i": first_i,
                    "wash_i": wash_i,
                }
                df.loc[df.index[i], "signal"] = 1
                df.loc[df.index[i], "confidence"] = float(np.clip(confidence, 0, 1))
                df.loc[df.index[i], "buy_point"] = buy_type

        # ══════════════════════════════════════
        # 卖出逻辑（持仓后触发）
        # ══════════════════════════════════════
        # 简化：对每个买入触发日，在 max_hold_days 内检查卖出条件
        buy_days = sorted(triggers_buy.keys())
        for bi in buy_days:
            info = triggers_buy[bi]
            first_open = float(info["first_open"])
            entry_close = float(df.loc[df.index[bi], "close"])
            for d in range(bi + 1, min(bi + max_hold_days + 1, n)):
                drow = df.iloc[d]
                pct_d = float(drow["pct"]) if np.isfinite(drow["pct"]) else 0.0
                reason = ""

                # SL1 破位止损：收盘 < 首板开盘价（手册铁律）
                if drow["close"] < first_open:
                    reason = "SL1_破位止损"
                # SL2 次日不及预期：买入次日低开 -3% 以上，前15分钟无拉升（日线近似）
                elif d == bi + 1 and drow["open"] < entry_close * (1 - stop_loss_pct):
                    reason = "SL2_次日不及预期"
                # TP1 反包减仓：涨幅>5% 或涨停（铁律：反包即减仓50%）
                elif pct_d > 0.05 or drow["is_limit_up"]:
                    reason = "TP1_反包减仓"
                # TP3 量价背离（涨停未遂）：拉到8%-9%但成交额 < 流通市值×1%，上攻乏力 → 清仓
                elif 0.08 <= pct_d < (limit_up_pct - 0.005):
                    if has_mv:
                        amt_d = float(drow["amount_est"])
                        mv_d = float(drow["float_mv"])
                        if amt_d < mv_d * 1e8 * first_amt_ratio:
                            reason = "TP3_量价背离(涨停未遂)"
                    else:
                        # 无市值列：用缩量近似（量 < 前日×1.1 且涨幅8-9%）
                        if drow["volume"] <= df["volume"].iloc[d - 1] * 1.1:
                            reason = "TP3_量价背离(涨停未遂)"
                # 倍量危险（手册：成交额≥流通市值×2% = 高位爆量出货预警）
                elif has_mv:
                    amt_d = float(drow["amount_est"])
                    mv_d = float(drow["float_mv"])
                    if amt_d >= mv_d * 1e8 * danger_amt_ratio:
                        reason = "TP3_倍量出货预警"
                # TP2 顶背离：价创新高，DIF 不创新高 + 缩量
                elif d >= 1:
                    pre_high = df["high"].iloc[bi:d].max() if d > bi else drow["high"]
                    if drow["high"] > pre_high:
                        dif_now = drow["dif"]
                        dif_prev = df["dif"].iloc[d - 1]
                        if (
                            np.isfinite(dif_now)
                            and np.isfinite(dif_prev)
                            and dif_now < dif_prev
                            and drow["volume"] <= df["volume"].iloc[d - 1] * 0.9
                        ):
                            reason = "TP2_顶背离"
                # TP4 压力位：触及前高或 BOLL 上轨
                elif np.isfinite(drow["boll_up"]) and drow["high"] >= drow["boll_up"]:
                    reason = "TP4_压力位止盈"

                if reason:
                    if df.loc[df.index[d], "signal"] != 1:  # 不覆盖买入信号
                        df.loc[df.index[d], "signal"] = -1
                        df.loc[df.index[d], "exit_reason"] = reason
                        df.loc[df.index[d], "confidence"] = 0.9
                    break  # 首个卖出信号后该笔结束

        buy_count = int((df["signal"] == 1).sum())
        sell_count = int((df["signal"] == -1).sum())
        logger.info("rebound_system 信号: {} 买入, {} 卖出", buy_count, sell_count)
        return df
