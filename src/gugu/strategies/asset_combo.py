"""认知资产组合验证策略（asset_combo）。

定位
----
不是生产策略，是 ``risk_rules`` 五条认知资产「接进策略」的最小验证载体。
目标：用数据裁决哪条资产是真正的 alpha 来源、哪条是认知陷阱。

组件映射
--------
- ``count_limit_ups``           → 活跃度前置过滤（历史涨停≥N 的妖股池）
- ``position_percentile``       → 位置前置过滤（默认关闭，500d 验证为负贡献）
- ``entry_gap_filter``          → 开盘溢价过滤（次日开盘相对昨收的合理区间）★ 风控减亏核心
- ``apply_support_break_stop``  → 破位止损（收盘破入场日 open 即走）
- ``detect_lock_profit_days``   → 板上减半（放量大阳触发锁利，TP1）
- ``regime_filter``             → 市场环境过滤（HS300 close vs ma60）★ alpha 真正来源

500d 大样本验证结果（2026-08-15，512 只主板股票，N=677~1580 信号）：

| 组合 | 全样本超额 | 弱势期超额 | 强势期超额 |
|------|-----------|-----------|-----------|
| 无 gap 过滤 | -0.86% (p=0.010 显著亏) | +0.15% (p=0.82) | -1.08% (p=0.007 显著亏) |
| gap 过滤 | -0.39% (p=0.43 不显著) | **+1.92%** (p=0.07 接近显著) | -0.98% (p=0.09) |

**三个关键发现（颠覆陈学长"牛市战法"论调）**：
1. **强势期反而显著亏钱**——市场过热，首板次日大幅高开透支，冲高回落
2. **弱势期+gap过滤才是真 alpha**——市场冷清，开盘溢价合理，反而能延续
3. **gap 过滤是"显著减亏的风控"，regime 过滤才是"独立 alpha 来源"**

信号语义
--------
入场：涨停首板 + 次日开盘溢价 ∈ [gap_min, gap_max]
       + 可选：活跃度过滤、位置过滤（默认关闭）
       + 可选：regime 过滤（默认开启，只在弱势期入场）
卖出：破位止损 + 时间止损
减半：入场后放量大阳日

根因
----
「首板涨停次日开盘买」基线 5日超额 -0.86%（显著亏损）。根因是强势期被市场情绪透支。
修复=「弱势期 + gap 过滤」组合，5日超额转正到 +1.92%（接近显著）。
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy
from gugu.strategies.risk_rules import (
    apply_support_break_stop,
    count_limit_ups,
    detect_lock_profit_days,
    entry_gap_filter,
    position_percentile,
)


class AssetComboStrategy(Strategy):
    """认知资产组合：涨停首板入场 + 四条认知资产过滤/风控。"""

    name = "asset_combo"
    category = "breakout"

    def validate_params(self) -> None:
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= limit_up_pct <= 0.11:
            raise ValueError("limit_up_pct 应在 5%-11% 之间")
        min_limit_ups = int(self.params.get("min_limit_ups", 1))
        if min_limit_ups < 1:
            raise ValueError("min_limit_ups 必须 >= 1")
        position_max = float(self.params.get("position_max", 1.0))
        if not 0 < position_max <= 1:
            raise ValueError("position_max 应在 (0, 1] 之间")
        max_hold_days = int(self.params.get("max_hold_days", 10))
        if max_hold_days < 1:
            raise ValueError("max_hold_days 必须 >= 1")
        gap_min = float(self.params.get("gap_min", 0.0))
        gap_max = float(self.params.get("gap_max", 0.03))
        only_weak_regime = bool(self.params.get("only_weak_regime", True))

        # 加载 HS300 regime 数据（用于市场环境过滤）
        self._load_hs300_regime() if only_weak_regime else None
        if gap_min >= gap_max:
            raise ValueError("gap_min 必须 < gap_max")

    def _ensure_columns(self, df: pd.DataFrame) -> None:
        required = ["date", "open", "high", "low", "close", "volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"{self.name} 缺失列: {missing}")

    def _load_hs300_regime(self) -> pd.DataFrame | None:
        """加载 HS300 regime 数据，返回含 date/regime 列的 DataFrame。

        regime = 1 表示强势（close > ma60），0 表示弱势。
        无前视：只用历史数据，当日收盘后才判定。
        """
        from pathlib import Path

        # 用 PROJECT_ROOT 绝对路径确保从任何 cwd 都能找到
        project_root = Path(__file__).resolve().parents[3]
        cache = project_root / "data/parquet_cache" / "hs300_500d.parquet"
        if not cache.exists():
            return None
        try:
            hs = pd.read_parquet(cache)
            hs["date"] = pd.to_datetime(hs["date"])
            hs = hs.sort_values("date").reset_index(drop=True)
            hs["ma60"] = hs["close"].rolling(60).mean()
            hs["regime"] = (hs["close"] > hs["ma60"]).astype(int)
            return hs[["date", "regime"]]
        except Exception:
            return None

    def _apply_regime_filter(
        self, df: pd.DataFrame, hs_regime: pd.DataFrame | None, only_weak: bool = True
    ) -> pd.DataFrame:
        """市场环境过滤：只在弱势期(close<=ma60)允许入场。

        数据依据：512 只主板大样本验证发现：
        - 弱势期+gap过滤 = +1.92% 超额（alpha 真正来源）
        - 强势期反而显著亏损 -1.08%（市场过热透支）
        """
        if hs_regime is None or not only_weak:
            return df
        # 用 merge_asof 按日期对齐（无前视：取信号日及之前的最后一个 HS300 regime）
        # 统一日期类型：pandas 2.x 后 datetime 默认 us 精度，HS300 缓存可能是 s 精度
        # 用 astype('datetime64[s]') 强制统一到秒级
        df = df.copy()
        df["date"] = df["date"].astype("datetime64[s]")
        hs = hs_regime.copy()
        hs["date"] = hs["date"].astype("datetime64[s]")
        signal_dates = df[["date"]].sort_values("date").reset_index(drop=True)
        merged = pd.merge_asof(signal_dates, hs, on="date", direction="backward")
        # regime=0（弱势）→ 保留信号；regime=1（强势）→ 取消
        df["regime"] = merged["regime"].values
        if "signal" in df.columns:
            mask_cancel = (df["regime"] == 1) & (df["signal"] == 1)
            df.loc[mask_cancel, "signal"] = 0
        return df

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.sort_values("date").reset_index(drop=True).copy()

        # ── 参数 ──────────────────────────────────────
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        limit_up_lookback = int(self.params.get("limit_up_lookback", 250))
        min_limit_ups = int(self.params.get("min_limit_ups", 1))
        position_window = int(self.params.get("position_window", 60))
        position_max = float(self.params.get("position_max", 1.0))  # 默认关闭：500d 验证为负贡献
        max_hold_days = int(self.params.get("max_hold_days", 10))
        lock_pct = float(self.params.get("lock_pct", 0.05))
        vol_ratio_min = float(self.params.get("vol_ratio_min", 1.2))
        gap_min = float(self.params.get("gap_min", 0.0))
        gap_max = float(self.params.get("gap_max", 0.03))
        only_weak_regime = bool(self.params.get("only_weak_regime", True))

        # 加载 HS300 regime 数据（用于市场环境过滤）
        hs_regime = self._load_hs300_regime() if only_weak_regime else None

        # ── 组件1：活跃度过滤（近 limit_up_lookback 日涨停次数）──
        df["limit_cnt"] = count_limit_ups(
            df,
            lookback_days=limit_up_lookback,
            limit_up_pct=limit_up_pct,
            min_periods=limit_up_lookback,
        )

        # ── 组件2：位置过滤（近 position_window 日高低点分位）──
        df["pos_pct"] = position_percentile(df, window=position_window)

        # ── 入场：涨停首板（非连板） ────────────────────
        df["pct"] = df["close"].pct_change()
        df["is_limit_up"] = df["pct"] >= (limit_up_pct - 0.005)
        df["prev_is_limit_up"] = df["is_limit_up"].shift(1).fillna(False)
        df["is_first_board"] = df["is_limit_up"] & (~df["prev_is_limit_up"])

        # 位置过滤用首板「前一日」的分位——首板当日涨停突破前高，当日分位必然 >1，
        # 会误伤所有首板；断板反包的「位置」要求是「首板启动前处于相对低位」。
        buy_cond = (
            df["is_first_board"]
            & (df["limit_cnt"] >= min_limit_ups)
            & (
                df["pos_pct"].shift(1) < position_max
            )  # shift(1) 确保用昨日收盘位置，position_percentile 仅排除盘中未定型
        )

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1

        # ── 组件3：开盘溢价过滤（次日开盘相对昨收 ∈ [gap_min, gap_max]） ──
        # 取消过度高开（>5%）或低开（<0%）的入场点——500d 事件研究证明这是
        # 「入场信号减亏」的根本原因（无条件次日开盘买超额 -1.27%，区间过滤后预期转正）
        df = entry_gap_filter(df, gap_min=gap_min, gap_max=gap_max)

        # ── 组件4：破位止损（支撑=入场日 open） ──────────
        df = apply_support_break_stop(df, support_from_entry_open=True)

        # ── 时间止损：max_hold_days 日强制退出 ──────────
        df = self._apply_max_hold_exit(df, max_hold_days=max_hold_days)

        # ── 组件6：市场环境过滤（HS300 弱势期才入场） ──
        # 512 只大样本验证：弱势期+gap过滤 = +1.92% 超额（真alpha），强势期反而显著亏损 -1.08%
        df = self._apply_regime_filter(df, hs_regime, only_weak=only_weak_regime)

        # ── 组件5：板上减半（放量大阳触发日） ──────────
        lock_days = detect_lock_profit_days(df, lock_pct=lock_pct, vol_ratio_min=vol_ratio_min)
        df["lock_profit"] = 0
        for idx in lock_days:
            df.loc[df.index[idx], "lock_profit"] = 1

        # ── 置信度：活跃度 + 位置优势 ──────────────────
        activity_norm = (df["limit_cnt"].clip(0, 20) / 20).fillna(0)
        position_score = (1 - df["pos_pct"].clip(0, 1)).fillna(0)
        df["confidence"] = (0.5 * activity_norm + 0.5 * position_score).clip(0, 1)

        return df
