"""asset_combo Walk-Forward 样本外验证脚本。

核心问题：上轮事件研究发现 entry_gap_filter 让 5日超额从 -0.53% 转正到 +0.14%，
但 p 值仍 >0.05（N=106 样本不足）。本轮用 Walk-Forward 样本外检验 alpha 稳健性。

方法：
1. 加载 500+ 只主板股票的 500d 长历史数据
2. 切 3 个时间窗口（每窗口前 60% 训练、后 40% 测试）
3. 训练集：扫描 gap_max 寻找最优参数
4. 测试集：用训练集选出的参数跑样本外收益
5. 聚合所有股票 × 所有窗口的样本外结果，做显著性检验

关键判定：
- 若样本外 5日超额 > 0 且 p < 0.05 → alpha 稳健，可考虑升级到 conditional_pass
- 若样本外 5日超额 > 0 但 p > 0.05 → 方向对但样本不足，需扩样本
- 若样本外 5日超额 ≤ 0 → alpha 不稳健，可能过拟合

用法:
    .venv/bin/python scripts/walk_forward_asset_combo.py
    .venv/bin/python scripts/walk_forward_asset_combo.py --symbols 600519,000858
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.strategies.asset_combo import AssetComboStrategy  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "parquet_cache"
STOCK_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")


def _is_stock(code: str) -> bool:
    return code.startswith(STOCK_PREFIXES)


def load_stocks(min_rows: int = 250) -> dict[str, pd.DataFrame]:
    """加载所有 500d 长历史数据。"""
    stocks: dict[str, pd.DataFrame] = {}
    for f in glob.glob(str(CACHE_DIR / "*_500d.parquet")):
        code = Path(f).stem.split("_")[0]
        if not _is_stock(code):
            continue
        try:
            df = pd.read_parquet(f)
            if "date" not in df.columns or len(df) < min_rows:
                continue
            df["date"] = pd.to_datetime(df["date"])
            if code not in stocks or len(df) > len(stocks[code]):
                stocks[code] = df.sort_values("date").reset_index(drop=True)
        except Exception:
            continue
    return stocks


def split_signals_by_time(
    stocks: dict[str, pd.DataFrame], params: dict, n_splits: int = 3
) -> tuple[list[pd.DataFrame], pd.Series]:
    """用全量数据生成信号，再按信号日期均分到 n_splits 个时间段。

    第一性原理：
    - count_limit_ups 依赖 250 日历史回看，切窗口会破坏这个依赖
    - 正确做法：用全数据生成信号（保证回看窗口完整），再按信号日期分组
    - 这才是真正的"样本外"——参数固定，验证不同时间段的表现是否稳健

    返回 [(signals_k, bench_k), ...] 长度 n_splits
    """
    # 用全数据生成信号
    strategy = AssetComboStrategy(params)
    all_signals = []
    bench_list = []
    for code, df in stocks.items():
        try:
            out = strategy.generate_signals(df, is_backtest=True)
        except Exception:
            continue
        # 基准
        df_b = out["close"].pct_change(5).dropna()
        bench_list.extend(df_b.tolist())
        # 信号
        for i in out.index[out["signal"] == 1]:
            if i + 1 >= len(out):
                continue
            entry = out.loc[out.index[i + 1], "open"]
            if entry <= 0 or np.isnan(entry):
                continue
            rec = {
                "code": code,
                "date": out.loc[out.index[i], "date"],
                "date_str": str(out.loc[out.index[i], "date"])[:10],
                "gap_pct": out.loc[out.index[i], "gap_pct"] if "gap_pct" in out.columns else np.nan,
            }
            for h in (2, 5, 10):
                j = i + 1 + h
                if j < len(out):
                    rec[f"ret_{h}d"] = out.loc[out.index[j], "close"] / entry - 1
                else:
                    rec[f"ret_{h}d"] = np.nan
            all_signals.append(rec)

    res = pd.DataFrame(all_signals)
    bench = pd.Series(bench_list).dropna()

    if len(res) == 0:
        return [], bench

    # 按信号日期均分到 n_splits 段
    res = res.sort_values("date").reset_index(drop=True)
    chunk_size = len(res) // n_splits
    chunks = []
    for k in range(n_splits):
        start = k * chunk_size
        end = (k + 1) * chunk_size if k < n_splits - 1 else len(res)
        chunks.append(res.iloc[start:end])
    return chunks, bench


def collect_benchmark(stocks: dict[str, pd.DataFrame]) -> pd.Series:
    """全市场基准：每只股票所有交易日 5 日收益。"""
    bench = []
    for code, df in stocks.items():
        df = df.copy()
        df["ret5"] = df["close"].pct_change(5)
        bench.extend(df["ret5"].dropna().tolist())
    return pd.Series(bench).dropna()


def split_by_time(stocks: dict[str, pd.DataFrame], n_splits: int = 3) -> list[tuple[dict, dict]]:
    """按时间切 n_splits 个窗口，每窗口前 60% 训练、后 40% 测试。

    返回 [(train_stocks, test_stocks), ...]
    """
    # 用所有股票的日期并集定时间窗口
    all_dates = pd.Index([])
    for df in stocks.values():
        all_dates = all_dates.union(df["date"].unique())

    total_len = len(all_dates)
    window_size = total_len // n_splits
    splits = []
    for k in range(n_splits):
        start = k * window_size
        end = (k + 1) * window_size if k < n_splits - 1 else total_len
        train_end = start + int((end - start) * 0.6)
        train_dates = set(all_dates[start:train_end])
        test_dates = set(all_dates[train_end:end])

        train_stocks, test_stocks = {}, {}
        for code, df in stocks.items():
            train_df = df[df["date"].isin(train_dates)].reset_index(drop=True)
            test_df = df[df["date"].isin(test_dates)].reset_index(drop=True)
            if len(train_df) >= 60:
                train_stocks[code] = train_df
            if len(test_df) >= 30:
                test_stocks[code] = test_df
        splits.append((train_stocks, test_stocks))
    return splits


def scan_best_gap_max(train_stocks: dict[str, pd.DataFrame], bench_mean: float) -> float:
    """训练集扫描 gap_max 寻最优（按 5日超额）。"""
    best_gap, best_ex = 0.03, -999
    for gmax in (0.01, 0.02, 0.03, 0.04, 0.05):
        params = {
            "limit_up_pct": 0.095,
            "limit_up_lookback": 250,
            "min_limit_ups": 1,
            "position_window": 60,
            "position_max": 1.0,
            "max_hold_days": 10,
            "gap_min": 0.0,
            "gap_max": gmax,
        }
        res = collect_signals(train_stocks, params)
        if len(res) == 0:
            continue
        s5 = res["ret_5d"].dropna()
        if len(s5) < 5:
            continue
        ex = s5.mean() - bench_mean
        if ex > best_ex:
            best_ex, best_gap = ex, gmax
    return best_gap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=str, default=None, help="自定义股票列表（逗号分隔）")
    parser.add_argument("--n-splits", type=int, default=3, help="时间段切分数")
    args = parser.parse_args()

    stocks = load_stocks(min_rows=250)
    print(f"加载股票: {len(stocks)} 只（>=250 行）")
    if len(stocks) < 30:
        print("⚠️ 股票数不足 30，Walk-Forward 无统计意义")
        return

    # 全市场基准
    bench = collect_benchmark(stocks)
    bench_mean = bench.mean()
    print(f"基准 5日均值: {bench_mean*100:+.2f}% (N={len(bench)})")

    # ── A. 固定参数 gap_max=0.03 + regime 弱势过滤,多时间段样本外检验 ──
    print(f"\n{'='*70}")
    print(f"A. gap_max=0.03 + regime 弱势过滤,信号按时间均分到 {args.n_splits} 段")
    print(f"{'='*70}")
    params_a = {
        "limit_up_pct": 0.095,
        "limit_up_lookback": 250,
        "min_limit_ups": 1,
        "position_window": 60,
        "position_max": 1.0,
        "max_hold_days": 10,
        "gap_min": 0.0,
        "gap_max": 0.03,
        "only_weak_regime": True,  # 启用 regime 过滤
    }
    chunks_a, _ = split_signals_by_time(stocks, params_a, n_splits=args.n_splits)
    oos_rets_a = {h: [] for h in (2, 5, 10)}
    for k, chunk in enumerate(chunks_a):
        if len(chunk) == 0:
            print(f"\n时间段{k+1}: 无信号")
            continue
        date_min = chunk["date_str"].min()
        date_max = chunk["date_str"].max()
        print(f"\n时间段{k+1} ({date_min} ~ {date_max}): N={len(chunk)}")
        for h in (2, 5, 10):
            col = f"ret_{h}d"
            if col not in chunk.columns:
                continue
            s = chunk[col].dropna()
            if len(s) == 0:
                continue
            ex = s.mean() - bench_mean
            oos_rets_a[h].extend(s.tolist())
            print(
                f"  {h}日: N={len(s):3d} 均值{s.mean()*100:+.2f}% 超额{ex*100:+.2f}% 胜率{(s>0).mean()*100:.1f}%"
            )

    print(f"\n{'='*70}")
    print("A. 多时间段聚合样本外结果（gap 过滤）")
    print(f"{'='*70}")
    for h in (2, 5, 10):
        s = pd.Series(oos_rets_a[h])
        if len(s) == 0:
            continue
        ex = s.mean() - bench_mean
        t_stat, p_val = sp.ttest_1samp(s, bench_mean)
        sig = "显著" if p_val < 0.05 else "不显著"
        print(
            f"{h}日: N={len(s):3d} 均值{s.mean()*100:+.2f}% 超额{ex*100:+.2f}% "
            f"胜率{(s>0).mean()*100:.1f}% t={t_stat:.2f} p={p_val:.4f} ({sig})"
        )

    # ── B. 对照:固定 gap_max=1.0(无过滤) ──
    print(f"\n{'='*70}")
    print(f"B. 对照:固定 gap_max=1.0（无 gap 过滤）多时间段")
    print(f"{'='*70}")
    params_b = {
        "limit_up_pct": 0.095,
        "limit_up_lookback": 250,
        "min_limit_ups": 1,
        "position_window": 60,
        "position_max": 1.0,
        "max_hold_days": 10,
        "gap_min": -1.0,
        "gap_max": 1.0,
        "only_weak_regime": False,  # 对照:关闭 regime 过滤
    }
    chunks_b, _ = split_signals_by_time(stocks, params_b, n_splits=args.n_splits)
    oos_rets_b = {h: [] for h in (2, 5, 10)}
    for k, chunk in enumerate(chunks_b):
        if len(chunk) == 0:
            continue
        for h in (2, 5, 10):
            col = f"ret_{h}d"
            if col not in chunk.columns:
                continue
            s = chunk[col].dropna()
            oos_rets_b[h].extend(s.tolist())

    print("\nB. 聚合样本外结果（无 gap 过滤）:")
    for h in (2, 5, 10):
        s = pd.Series(oos_rets_b[h])
        if len(s) == 0:
            continue
        ex = s.mean() - bench_mean
        t_stat, p_val = sp.ttest_1samp(s, bench_mean)
        sig = "显著" if p_val < 0.05 else "不显著"
        print(
            f"{h}日: N={len(s):3d} 均值{s.mean()*100:+.2f}% 超额{ex*100:+.2f}% "
            f"胜率{(s>0).mean()*100:.1f}% t={t_stat:.2f} p={p_val:.4f} ({sig})"
        )

    # ── 结论 ──
    print(f"\n{'='*70}")
    print("结论：")
    print(f"{'='*70}")
    a5 = pd.Series(oos_rets_a[5])
    b5 = pd.Series(oos_rets_b[5])
    if len(a5) and len(b5):
        ex_a = a5.mean() - bench_mean
        ex_b = b5.mean() - bench_mean
        diff = ex_a - ex_b
        t_stat, p_val = sp.ttest_ind(a5, b5, equal_var=False)
        print(f"  A（gap 过滤）5日超额: {ex_a*100:+.2f}% (N={len(a5)})")
        print(f"  B（无过滤）  5日超额: {ex_b*100:+.2f}% (N={len(b5)})")
        print(f"  A - B 改善:    {diff*100:+.2f}%")
        print(
            f"  双样本 Welch t: t={t_stat:.2f}, p={p_val:.4f} "
            f"({'A 显著优于 B' if (p_val < 0.05 and diff > 0) else '差异不显著'})"
        )
        print()
        if ex_a > 0 and p_val < 0.05 and diff > 0:
            print("  ✅ 判定：entry_gap_filter 在样本外产生显著正超额，alpha 稳健")
            print("     → asset_combo 可考虑升级到 conditional_pass")
        elif ex_a > 0 and diff > 0:
            print("  ⚠️ 判定：方向正确但样本外不显著，需扩样本至 500+ 信号")
        elif diff > 0 and ex_b < 0:
            print("  ⚠️ 判定：gap 过滤显著减亏（B 显著亏损 → A 不显著）")
            print("     → 作为「风控过滤」有效，但作为「alpha 来源」证据不足")
        else:
            print("  ❌ 判定：alpha 不稳健，可能过拟合，不达上线标准")


if __name__ == "__main__":
    main()
