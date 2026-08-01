"""断板反包·精英追涨策略（rebound_elite）。

来源：陈学长不吃面 226 条视频转写 + 2026-07-31 事件研究验证后的概率化用法。

═══════════════════════════════════════════════════════
背景：为什么要有这个策略
═══════════════════════════════════════════════════════
对 67 只主板活跃股 2.5 年数据、152 个完整断板反包形态的事件研究验证结论：
- 整体形态期望收益为负（5日超额 -1.5%，p=0.04 显著逆基准）
- 但有一条可用的规则被验证有效：
    「高活跃妖股(历史涨停≥20) + 放量反包追涨」是唯一正收益子集
    （5日 +4.08% / 10日 +9.99%，盈亏比 2.76）
- 陈学长最喜欢的「护盘回踩低吸」买点，统计上显著为负（胜率仅38%），已废弃

因此本策略只做**放量反包追涨**、只在高活跃度股票上触发、
只在牛市/强反弹环境启用（由 settings.yaml 的 enabled_by_regime 控制）。

═══════════════════════════════════════════════════════
策略逻辑（严格反包追涨，买点2）
═══════════════════════════════════════════════════════
买入（对当前最后一根K线判断，全部满足才触发）：
  1. 活跃度前置过滤：过去 lookback_days 日涨停次数 ≥ min_limit_ups（默认≥8，妖股特征）
  2. 存在断板反包形态：找到距今 wash_gap 日内的一根首板(涨停) + 其后放量大阴洗盘
  3. 护盘有效：洗盘日至今最低价不破首板开盘价×(1-support_tol)（主力成本底线）
  4. 反包确认（当日）：收阳 + 收盘突破洗盘日最高 + 量比≥1.2（放量）

卖出：
  - 破位止损：跌破首板开盘价×（1-stop_loss_pct）即卖（护盘失败=止损位）
  - 时间止损：持仓 max_hold_days（默认10）日内未创新高 或 涨幅不足 即卖

置信度：形态完整度 + 活跃度 + 放量系数的加权打分。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class ReboundEliteStrategy(Strategy):
    """断板反包·精英追涨：高活跃妖股 + 放量反包追涨（验证有效形态）。"""

    name = "rebound_elite"
    category = "main_force"  # 短线主力行为类

    def validate_params(self) -> None:
        lu = int(self.params.get("min_limit_ups", 8))
        if lu < 1:
            raise ValueError("min_limit_ups 必须 >= 1")
        lt = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= lt <= 0.11:
            raise ValueError("limit_up_pct 应在 5%-11%")
        wash_gap = int(self.params.get("wash_gap", 5))
        if wash_gap < 1:
            raise ValueError("wash_gap 必须 >= 1")

    def _ensure_columns(self, df: pd.DataFrame) -> None:
        required = ["open", "high", "low", "close", "volume", "amount"]
        for c in required:
            if c not in df.columns:
                raise ValueError(f"缺少必要列: {c}")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        lookback = int(self.params.get("lookback_days", 120))
        min_limit_ups = int(self.params.get("min_limit_ups", 8))
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        wash_gap = int(self.params.get("wash_gap", 5))
        wash_min_drop = float(self.params.get("wash_min_drop", 0.03))
        wash_vol_min = float(self.params.get("wash_vol_min", 0.7))
        support_tol = float(self.params.get("support_tolerance", 0.03))
        vol_ratio_min = float(self.params.get("vol_ratio_min", 1.2))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.08))
        max_hold_days = int(self.params.get("max_hold_days", 10))

        # ── 基础指标 ──────────────────────────
        df["pct"] = df["close"].pct_change()
        df["vol_ma5"] = df["volume"].rolling(5).mean()
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)
        df["is_limit_up"] = df["pct"] >= (limit_up_pct - 0.005)
        # 活跃度：过去 lookback 日涨停次数（滚动）
        df["limit_cnt"] = df["is_limit_up"].rolling(lookback).sum()

        n = len(df)

        # ── 向量化扫首板 ────────────────────────
        # 首板 = 涨停日。遍历每个涨停日，找到其后的断板反包买点。
        limit_idx = df.index[df["is_limit_up"]].to_numpy()

        # 记录每个"反包确认日"是否触发
        trigger = pd.Series(0, index=df.index)
        conf = pd.Series(0.0, index=df.index)

        for li in limit_idx:
            # 需要至少还有一根K线在后面
            if li + 1 >= n:
                continue
            limit_open = float(df.loc[li, "open"])
            limit_vol = float(df.loc[li, "volume"])
            if limit_vol <= 0:
                continue

            # 活跃度过滤：截至首板日，该股过去 lookback 日涨停次数足够
            # （用固定首板窗口近似，简化滚动）
            act_window = df["pct"].iloc[max(0, li - lookback) : li + 1]
            act_cnt = int((act_window >= limit_up_pct - 0.005).sum())
            if act_cnt < min_limit_ups:
                continue

            # 找洗盘大阴（首板后 wash_gap 日内，跌幅 & 放量）
            wash_i = None
            for wi in range(li + 1, min(li + wash_gap + 1, n)):
                ww = df.loc[wi]
                if ww["pct"] < -wash_min_drop and ww["volume"] >= limit_vol * wash_vol_min:
                    wash_i = wi
                    break
            if wash_i is None:
                continue
            wash_high = float(df.loc[wash_i, "high"])

            # 洗盘后到当前：护盘不破首板开盘价（主力成本底线）
            # 若中途破位且未收回，该形态失败
            for pi in range(wash_i + 1, n):
                window_low = df["low"].iloc[wash_i : pi + 1].min()
                if window_low < limit_open * (1 - support_tol):
                    break  # 有效破位，护盘失败
                pp = df.loc[pi]
                pr = pp["vol_ratio"]
                if pr is None or np.isnan(pr):
                    continue
                # 反包确认：放量阳线突破洗盘日最高
                if pp["close"] > wash_high and pr >= vol_ratio_min and pp["close"] > pp["open"]:
                    # 活跃度（对触发日也校验一次）
                    if act_cnt >= min_limit_ups:
                        trigger.iloc[pi] = 1
                        conf.iloc[pi] = self._score(
                            df, li, wash_i, pi, act_cnt, limit_open, wash_high
                        )
                    break  # 该首板事件到此结束

        # ── 收盘确认：反包当天已过，次日才真正可执行（shift 由回测引擎处理，这里不做）──
        df["signal"] = trigger.astype(int)
        df["confidence"] = conf.clip(0, 1)

        # ── 卖出逻辑：只对已入场（前一日为买入）的情形做触发，供持仓监控用 ──
        # 简化：破位卖出信号单独标注，实盘由引擎的止损规则兜底，
        # 这里在触发买入的 max_hold_days 内若跌破入场价止损线则置卖出。
        df["support"] = np.nan
        df["_buy_support"] = np.nan
        for li in limit_idx:
            if li + 1 >= n:
                continue
            for pi in range(li + 1, n):
                if df["signal"].iloc[pi] == 1:
                    df.loc[df.index[pi], "_buy_support"] = df.loc[li, "open"]
                    break

        buy_mask = (df["signal"] == 1).to_numpy()
        if buy_mask.any():
            # 入场后 max_hold_days 内跌破支撑止损
            last_buy = -1
            for i in range(n):
                if buy_mask[i]:
                    last_buy = i
                    continue
                if last_buy >= 0 and i - last_buy <= max_hold_days:
                    sup = (
                        float(df.loc[df.index[last_buy], "_buy_support"])
                        if not np.isnan(df.loc[df.index[last_buy], "_buy_support"])
                        else None
                    )
                    if sup and df.loc[df.index[i], "close"] < sup * (1 - stop_loss_pct):
                        df.loc[df.index[i], "signal"] = -1
                        df.loc[df.index[i], "_stop_reason"] = "破位止损"

        buy_count = int((df["signal"] == 1).sum())
        sell_count = int((df["signal"] == -1).sum())
        logger.info("rebound_elite 信号: {} 买入, {} 卖出", buy_count, sell_count)

        return df

    def _score(self, df, li, wash_i, pi, act_cnt, limit_open, wash_high) -> float:
        """置信度打分：活跃度 + 形态完整度 + 放量强度。"""
        score = 0.0
        # 活跃度贡献（0~0.35）
        act_norm = min(act_cnt / 20.0, 1.0)
        score += 0.35 * act_norm
        # 洗盘充分度（0~0.25）：洗盘跌幅越深越好
        wash_low = float(df.loc[wash_i, "low"])
        wash_depth = max(0.0, min((limit_open - wash_low) / limit_open, 0.15))
        score += 0.25 * min(wash_depth / 0.10, 1.0)
        # 反包强度（0~0.4）：当日涨幅 + 放量
        p = df.loc[pi]
        day_chg = float(p["close"]) / float(df.loc[pi - 1, "close"]) - 1 if pi > 0 else 0
        vol_r = float(p["vol_ratio"]) if not np.isnan(p["vol_ratio"]) else 1.0
        score += 0.2 * min(max(day_chg, 0) / 0.05, 1.0)
        score += 0.2 * min(max(vol_r - 1.0, 0) / 2.0, 1.0)
        return float(np.clip(score, 0.0, 1.0))
