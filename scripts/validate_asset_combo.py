"""asset_combo 认知资产组合策略·严格验证脚本。

目的：验证 risk_rules 五条认知资产叠加在「涨停首板入场 + 开盘溢价过滤」上是否产生正超额。
方法：多股事件研究——用 500 根日K长历史数据，对照有无 entry_gap_filter 的收益结构。

数据基础（2026-08-15 升级）：
- 使用新浪接口拉取的 500 根日K缓存（data/parquet_cache/*_500d.parquet）
- 107 只主板股票，覆盖 2024-2026 三个年度，足以做严格事件研究

根因诊断（500d 数据下挖出）：
- 首板次日开盘溢价均值 +2.39%、中位 +1.49%，25.9% 高开>5%
- 按5日收益呈「微笑曲线」：高开1-3% +1.18%（健康接力）、高开>5% -2.31%（透支）、
  低开>2% -3.30%（资金不看好）、平盘~小高开 -0.03%（中性）
- 「无条件次日开盘买」5日超额 -1.27%（亏钱），根因是 35% 高开>3% 吞掉 alpha
- 加 entry_gap_filter(0, 0.03) 后预期超额转正

用法:
    .venv/bin/python scripts/validate_asset_combo.py
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.strategies.asset_combo import AssetComboStrategy  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "parquet_cache"

# 严格参数（500d 数据下用完整 250 日 lookback，position_max=1.0 默认关闭位置过滤）
PARAMS = {
    "limit_up_pct": 0.095,
    "limit_up_lookback": 250,
    "min_limit_ups": 1,
    "position_window": 60,
    "position_max": 1.0,  # 默认关闭：500d 验证为负贡献
    "max_hold_days": 10,
    "gap_min": 0.0,
    "gap_max": 0.03,
}

STOCK_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")


def _is_stock(code: str) -> bool:
    return code.startswith(STOCK_PREFIXES)


def load_stocks(min_rows: int = 250) -> dict[str, pd.DataFrame]:
    """优先加载 500d 长历史数据；若无则降级到较短但明确标注的样本。

    统一走 data_gate：兼容 ``*_500d_none.parquet`` 命名，并拒绝短样本
    冒充长历史（旧实现直接 glob 全部 parquet 且静默跳过不足 min_rows 的文件）。
    """
    from gugu.backtest.data_gate import iter_cache_files, validate_stock_history

    stocks: dict[str, pd.DataFrame] = {}
    for f in iter_cache_files(CACHE_DIR):
        code = Path(f).stem.split("_")[0]
        if not _is_stock(code):
            continue
        try:
            df = pd.read_parquet(f)
            clean = validate_stock_history(df, symbol=code, min_rows=min_rows)
            if clean is None:
                continue
            # 同一代码优先保留更长的历史
            if code not in stocks or len(clean) > len(stocks[code]):
                stocks[code] = clean
        except Exception:
            continue
    return stocks


def run_signals(stocks, params, hold_days=(2, 5, 10)):
    """跑策略生成信号，返回 (signals_df, bench_5d_series)。"""
    strategy = AssetComboStrategy(params)
    signals = []
    bench_5d = []
    for code, df in stocks.items():
        try:
            out = strategy.generate_signals(df, is_backtest=True)
        except Exception:
            continue
        # 基准：所有交易日 5 日收益
        rolling5 = out["close"].pct_change(5).dropna()
        bench_5d.extend(rolling5.tolist())
        # 入场点
        for i in out.index[out["signal"] == 1]:
            if i + 1 >= len(out):
                continue
            entry_px = out.loc[out.index[i + 1], "open"]
            if entry_px <= 0 or np.isnan(entry_px):
                continue
            rec = {
                "code": code,
                "date": str(out.loc[out.index[i], "date"])[:10],
                "limit_cnt": out.loc[out.index[i], "limit_cnt"],
                "gap_pct": out.loc[out.index[i], "gap_pct"] if "gap_pct" in out.columns else np.nan,
            }
            for h in hold_days:
                j = i + 1 + h
                if j < len(out):
                    rec[f"ret_{h}d"] = out.loc[out.index[j], "close"] / entry_px - 1
                else:
                    rec[f"ret_{h}d"] = np.nan
            signals.append(rec)
    return pd.DataFrame(signals), pd.Series(bench_5d).dropna()


def print_stats(res: pd.DataFrame, bench: pd.Series, label: str):
    print(f"\n=== {label} ===")
    print(f"基准(全市场5日均值): N={len(bench)} 均值{bench.mean()*100:+.2f}%")
    print("-" * 70)
    if len(res) == 0:
        print("无信号")
        return
    for h in (2, 5, 10):
        col = f"ret_{h}d"
        if col not in res.columns:
            continue
        s = res[col].dropna()
        if len(s) == 0:
            continue
        win = (s > 0).mean() * 100
        avg = s.mean() * 100
        med = s.median() * 100
        ex = s.mean() - bench.mean()
        print(
            f"{h}日: N={len(s):3d} 胜率{win:.1f}% 均值{avg:+.2f}% 中位{med:+.2f}% 超额{ex*100:+.2f}%"
        )
        # 显著性检验
        if len(s) >= 5 and len(bench) >= 5:
            from scipy import stats as sp

            t, p = sp.ttest_ind(s, bench, equal_var=False)
            sig = "显著" if p < 0.05 else "不显著"
            print(f"     Welch t: t={t:.2f}, p={p:.4f} ({sig})")


def main() -> None:
    stocks = load_stocks(min_rows=250)
    print(f"加载个股缓存(>=250行): {len(stocks)} 只")
    if not stocks:
        print("无可用数据。请先运行: .venv/bin/python scripts/cache_market_data.py --datalen 500")
        return

    # ─── A. 主验证：五条组件全开 ───
    res, bench = run_signals(stocks, PARAMS)
    print(f"\n买入信号总数: {len(res)}，涉及股票 {res['code'].nunique() if len(res) else 0} 只")
    print_stats(res, bench, "A. 五条组件全开（含 entry_gap_filter）")

    # ─── B. 对照：关闭 entry_gap_filter（gap_max=1.0 表示允许任意高开）───
    params_b = {**PARAMS, "gap_min": -1.0, "gap_max": 1.0}
    res_b, _ = run_signals(stocks, params_b)
    print_stats(res_b, bench, "B. 对照：无 entry_gap_filter（原方案）")

    # ─── C. 对照：单变量 — 不同 gap_max ───
    print("\n" + "=" * 70)
    print("对照实验C：不同 gap_max 阈值 → 5日超额（验证 entry_gap_filter 单调贡献）")
    print("=" * 70)
    for gmax in (0.01, 0.02, 0.03, 0.05, 0.08, 1.0):
        p = {**PARAMS, "gap_max": gmax}
        r, _ = run_signals(stocks, p, hold_days=(5,))
        if len(r) == 0:
            print(f"  gap_max={gmax:.2f}: 无信号")
            continue
        s5 = r["ret_5d"].dropna()
        if len(s5) == 0:
            continue
        ex = s5.mean() - bench.mean()
        print(
            f"  gap_max={gmax:.2f}: N={len(s5):3d} 5日均值{s5.mean()*100:+.2f}% "
            f"超额{ex*100:+.2f}% 胜率{(s5>0).mean()*100:.1f}%"
        )

    # ─── D. 对照：不同活跃度阈值 ───
    print("\n" + "=" * 70)
    print("对照实验D：min_limit_ups（活跃度）→ 5日超额")
    print("=" * 70)
    for ml in (1, 3, 5, 8):
        p = {**PARAMS, "min_limit_ups": ml}
        r, _ = run_signals(stocks, p, hold_days=(5,))
        if len(r) == 0:
            print(f"  min_lu={ml}: 无信号")
            continue
        s5 = r["ret_5d"].dropna()
        if len(s5) == 0:
            continue
        ex = s5.mean() - bench.mean()
        print(
            f"  min_lu={ml}: N={len(s5):3d} 5日均值{s5.mean()*100:+.2f}% "
            f"超额{ex*100:+.2f}% 胜率{(s5>0).mean()*100:.1f}%"
        )

    print("\n" + "=" * 70)
    print("解读：")
    print("1. 对比 A vs B：entry_gap_filter 是否带来超额改善（核心验证）")
    print("2. 对照 C：寻找最优 gap_max（健康接力区间）")
    print("3. 对照 D：活跃度过滤是否仍有单调贡献")
    print("=" * 70)


if __name__ == "__main__":
    main()
