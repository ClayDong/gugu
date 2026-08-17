#!/usr/bin/env python3
"""「睿睿妈·隔夜套利法」事件研究验证（2026-08-01）。

战法原文量化（6 过滤条件 + 1 铁律）：
  1. 时间+涨幅：下午 2:30 后，涨幅 3%~5% 的主板票
  2. 股性：过去 20 天内有涨停板（主板 ≈10%）
  3. 人气：量比 > 1
  4. 市值：< 200 亿（剔除大象）
  5. 换手率：5%~10%（低于 5% 没人气，高于 10% 主力出货）
  6. 分时均价线：股价全天站上黄色均价线（日线近似：收盘 > 当日典型价）
  铁律：次日早盘必须清仓（涨停除外）

方法（gugu-strategy-backtest skill 铁律）：
- 腾讯源 ifzq.gtimg.cn 拉 24 只主板中小盘 + 4 只大盘对照 × 600 交易日
- 市值/换手从 qt.gtimg.cn 注入（当前值全区间近似，免费源无历史序列，诚实标注）
- 收益 = 隔夜收益（次日开盘/当日收盘-1），对照持有到次日收盘（检验"早盘必须走"铁律）
- 超额 = 事件收益 − 同期沪深300 隔夜收益（防 beta 污染）
- Welch t 检验 + 集中度 + 年份分桶 + train/val 切分 + 参数敏感性网格

用法: .venv/bin/python scripts/overnight_arb_event_study.py
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request
from collections import defaultdict
from math import erf, sqrt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

CTX = ssl._create_unverified_context()

# ── 股票池：24 只主板中小盘（40-200亿，8 行业）+ 4 只大盘对照 ──
POOL = [
    # 消费
    "sh600702",  # 舍得酒业 128.7亿 白酒
    "sz002507",  # 涪陵榨菜 140.3亿
    "sh600779",  # 水井坊 142.8亿 白酒
    "sh603848",  # 好太太 51.9亿 家居
    # 医药
    "sh603387",  # 基蛋生物 45.1亿
    "sz002901",  # 大博医疗 124.6亿
    # 科技/信创/支付
    "sh603666",  # 亿嘉和 45.5亿 机器人
    "sz002368",  # 太极股份 91.5亿 信创
    "sh600728",  # 佳都科技 103.6亿 AI
    "sz000555",  # 神州信息 108.6亿 金融IT
    "sz002268",  # 电科网安 123.2亿 安全
    "sh603236",  # 移远通信 182.5亿 物联网
    "sz000997",  # 新大陆 189.3亿 支付
    "sh600131",  # 国网信通 187.3亿
    "sh600271",  # 航天信息 129.5亿
    "sh603508",  # 思维列控 88.3亿 轨交
    # 军工
    "sz002519",  # 银河电子 58.2亿
    "sh601890",  # 亚星锚链 82.5亿
    # 化工/材料/电力设备
    "sh603026",  # 石大胜华 148.5亿 化工
    "sz000969",  # 安泰科技 164.5亿 新材料
    "sz000949",  # 新乡化纤 98.7亿 化工
    "sh603556",  # 海兴电力 131.2亿 电力设备
    "sh603690",  # 至纯科技 83.1亿 半导体设备
    # 交运/基建
    "sh600125",  # 铁龙物流 73.0亿 交运
    "sz002941",  # 新疆交建 76.8亿 基建
]
# 大盘对照（应被市值条件过滤，验证市值过滤必要性）
LARGE_CAP_CTRL = ["sh600519", "sh601318", "sh600036", "sz000858"]  # 茅台/平安/招行/五粮液

TRAIN_CUTOFF = "2025-01-01"
LIMIT_UP_TH = 0.098  # 主板涨停 ≈10%，qfq 容差
BASE_CHG = (0.03, 0.05)  # 涨幅 3%~5%
BASE_TURN = (0.05, 0.10)  # 换手 5%~10%
BASE_MCAP = 200e8  # 市值 < 200 亿


def fetch_kline(symbol: str, days: int = 600) -> pd.DataFrame:
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
    req = urllib.request.Request(url, headers={"Referer": "http://finance.qq.com/"})
    data = json.loads(urllib.request.urlopen(req, context=CTX, timeout=20).read())
    k = data["data"][symbol]
    rows = k.get("qfqday") or k.get("day")
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["date", "open", "close", "high", "low", "volume"]
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = df[c].astype(float)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_quote(symbol: str) -> dict:
    """腾讯实时行情：注入流通市值(亿)/换手率%/现价（当前值，全区间近似）。"""
    url = f"https://qt.gtimg.cn/q={symbol}"
    req = urllib.request.Request(url, headers={"Referer": "http://finance.qq.com/"})
    raw = urllib.request.urlopen(req, context=CTX, timeout=15).read().decode("gbk", errors="ignore")
    parts = raw.split("~")
    if len(parts) <= 46:
        raise RuntimeError(f"字段不足: {len(parts)}")
    return {
        "mcap_yi": float(parts[44]),  # 流通市值(亿)
        "turnover": float(parts[38]) / 100.0,  # 最新换手率(小数)
        "price": float(parts[3]),
    }


def welch_t(x: list[float]) -> tuple[float, float]:
    """单样本 Welch t 检验（vs 0）。"""
    n = len(x)
    if n < 2:
        return 0.0, 1.0
    mean = sum(x) / n
    var = sum((v - mean) ** 2 for v in x) / (n - 1)
    if var == 0:
        return (mean / 1e-9, 0.0) if mean != 0 else (0.0, 1.0)
    se = sqrt(var / n)
    t = mean / se
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return t, p


def build_features(df: pd.DataFrame, quote: dict) -> pd.DataFrame:
    """构造战法 6 条件所需的全部特征列。"""
    df = df.copy()
    df["prev_close"] = df["close"].shift(1)
    df["chg"] = df["close"] / df["prev_close"] - 1.0  # 当日涨幅
    # 量比：当日量 / 前5日均量
    df["vol_ma5"] = df["volume"].rolling(5).mean().shift(1)
    df["vol_ratio"] = df["volume"] / df["vol_ma5"]
    # 流通股本 = 流通市值 / 现价（当前值近似全区间）
    float_shares = quote["mcap_yi"] * 1e8 / quote["price"]
    # volume 单位校准：主板=手(×100)。factor = 流通股本×实时换手 / 最新K线volume → 取最接近{1,100}
    latest_vol = df["volume"].iloc[-1]
    raw_factor = float_shares * quote["turnover"] / latest_vol
    vol_factor = 100 if abs(raw_factor - 100) < abs(raw_factor - 1) else 1
    df["turnover"] = df["volume"] * vol_factor / float_shares  # 历史换手率(小数)
    df["market_cap"] = quote["mcap_yi"] * 1e8  # 流通市值（当前值近似）
    # 分时均价线日线近似：收盘 > 当日典型价 (H+L+C)/3
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3.0
    return df


def detect_events(
    df: pd.DataFrame, chg_lo: float, chg_hi: float, turn_lo: float, turn_hi: float
) -> pd.DataFrame:
    """6 条件 AND → 事件日（含各条件明细列供诊断）。"""
    d = df.copy()
    d["has_limit_20d"] = d["chg"].shift(1).rolling(20).max() >= LIMIT_UP_TH  # 前20日有涨停
    d["cond_1_chg"] = (d["chg"] >= chg_lo) & (d["chg"] <= chg_hi)
    d["cond_2_limit"] = d["has_limit_20d"]
    d["cond_3_vr"] = d["vol_ratio"] > 1.0
    d["cond_4_mcap"] = d["market_cap"] < BASE_MCAP
    d["cond_5_turn"] = (d["turnover"] >= turn_lo) & (d["turnover"] <= turn_hi)
    d["cond_6_above_avg"] = d["close"] > d["typical_price"]
    d["event"] = (
        d["cond_1_chg"]
        & d["cond_2_limit"]
        & d["cond_3_vr"]
        & d["cond_4_mcap"]
        & d["cond_5_turn"]
        & d["cond_6_above_avg"]
    )
    return d


def run_study(
    dfs: dict[str, pd.DataFrame],
    index_df: pd.DataFrame,
    chg_lo: float = BASE_CHG[0],
    chg_hi: float = BASE_CHG[1],
    turn_lo: float = BASE_TURN[0],
    turn_hi: float = BASE_TURN[1],
) -> dict:
    """事件研究主流程。"""
    idx = index_df.set_index("date")[["open", "close"]]
    idx["idx_overnight"] = idx["open"].shift(-1) / idx["close"] - 1.0  # 指数隔夜收益

    # 先对每只股票应用 6 条件检测（参数化）
    ev_dfs = {sym: detect_events(df, chg_lo, chg_hi, turn_lo, turn_hi) for sym, df in dfs.items()}

    events: list[dict] = []
    per_symbol = defaultdict(int)
    for sym, df in ev_dfs.items():
        ev = df[df["event"] == True]  # noqa: E712
        for _, row in ev.iterrows():
            i = df.index[df["date"] == row["date"]][0]
            if i + 1 >= len(df):
                continue
            nxt = df.iloc[i + 1]
            entry = row["close"]
            # 铁律：次日早盘清仓（次日开盘卖）
            overnight = nxt["open"] / entry - 1.0
            # 对照：持有到次日收盘（检验"早盘必须走"）
            hold_close = nxt["close"] / entry - 1.0
            # 涨停例外变体：次日开盘涨停 → 持有到收盘
            if nxt["open"] >= entry * (1 + LIMIT_UP_TH + 0.001):
                overnight_ex_limit = nxt["close"] / entry - 1.0
            else:
                overnight_ex_limit = overnight
            # 指数同期隔夜
            d0 = row["date"]
            idx_row = idx.loc[d0] if d0 in idx.index else None
            idx_ov = (
                idx_row["idx_overnight"]
                if idx_row is not None and pd.notna(idx_row["idx_overnight"])
                else float("nan")
            )
            events.append(
                {
                    "symbol": sym,
                    "date": d0,
                    "overnight": overnight,
                    "hold_close": hold_close,
                    "ov_ex_limit": overnight_ex_limit,
                    "excess": overnight - idx_ov if pd.notna(idx_ov) else float("nan"),
                    "chg": row["chg"],
                    "turn": row["turnover"],
                    "mcap": row["market_cap"],
                }
            )
            per_symbol[sym] += 1

    total = len(events)
    result: dict = {
        "total_signals": total,
        "params": {"chg": [chg_lo, chg_hi], "turn": [turn_lo, turn_hi]},
    }

    # 三种卖出模式
    modes = {
        "overnight_open": ("次日开盘卖(铁律)", [e["overnight"] for e in events]),
        "ov_ex_limit": ("开盘涨停则持有", [e["ov_ex_limit"] for e in events]),
        "hold_to_close": ("持有到次日收盘", [e["hold_close"] for e in events]),
    }
    for key, (label, rets) in modes.items():
        if not rets:
            result[key] = {"n": 0, "mean": None, "p": None, "win_rate": None}
            continue
        t, p = welch_t(rets)
        win = sum(1 for r in rets if r > 0) / len(rets)
        result[key] = {
            "label": label,
            "n": len(rets),
            "mean": round(sum(rets) / len(rets), 4),
            "p": round(p, 4),
            "win_rate": round(win, 4),
        }

    # 超额（vs 沪深300 隔夜）
    excess = [e["excess"] for e in events if pd.notna(e["excess"])]
    if excess:
        t_e, p_e = welch_t(excess)
        result["excess_overnight"] = {
            "n": len(excess),
            "mean": round(sum(excess) / len(excess), 4),
            "p": round(p_e, 4),
            "win_rate": round(sum(1 for r in excess if r > 0) / len(excess), 4),
        }

    # 集中度
    top_symbol = max(per_symbol.items(), key=lambda x: x[1]) if per_symbol else ("", 0)
    result["concentration"] = {
        "top_symbol": top_symbol[0],
        "top_count": top_symbol[1],
        "top_ratio": round(top_symbol[1] / total, 2) if total else 0,
    }

    # 年份分桶（隔夜超额）
    result["by_year"] = {}
    for year in sorted({e["date"].year for e in events}):
        yr_ret = [e["excess"] for e in events if e["date"].year == year and pd.notna(e["excess"])]
        if yr_ret:
            t_y, p_y = welch_t(yr_ret)
            result["by_year"][str(year)] = {
                "n": len(yr_ret),
                "mean": round(sum(yr_ret) / len(yr_ret), 4),
                "p": round(p_y, 4),
            }

    # train/val 切分（隔夜超额）
    val = [e for e in events if e["date"] >= pd.Timestamp(TRAIN_CUTOFF) and pd.notna(e["excess"])]
    train = [e for e in events if e["date"] < pd.Timestamp(TRAIN_CUTOFF) and pd.notna(e["excess"])]
    result["split"] = {"train_count": len(train), "val_count": len(val)}
    for name, grp in [("train", train), ("val", val)]:
        if grp:
            t_g, p_g = welch_t([e["excess"] for e in grp])
            result[f"{name}_excess"] = {
                "n": len(grp),
                "mean": round(sum(e["excess"] for e in grp) / len(grp), 4),
                "p": round(p_g, 4),
            }

    # 各条件单独命中率（诊断：哪个条件最严）
    cond_stats = {}
    for c in [
        "cond_1_chg",
        "cond_2_limit",
        "cond_3_vr",
        "cond_4_mcap",
        "cond_5_turn",
        "cond_6_above_avg",
    ]:
        hit = int(
            sum(1 for _, df in ev_dfs.items() if c in df.columns for _ in [1] if df[c].sum() > 0)
        )
        cond_stats[c] = hit
    result["cond_trigger_count"] = cond_stats

    # 剔除最大贡献者后重验（集中度审查）
    if total >= 5 and top_symbol[0]:
        rest = [e for e in events if e["symbol"] != top_symbol[0] and pd.notna(e["excess"])]
        if len(rest) >= 5:
            t_r, p_r = welch_t([e["excess"] for e in rest])
            result["excl_top_excess"] = {
                "n": len(rest),
                "mean": round(sum(e["excess"] for e in rest) / len(rest), 4),
                "p": round(p_r, 4),
            }
    return result


def fmt(r: dict) -> str:
    lines = [
        f"📊 隔夜套利法 — 总信号 {r['total_signals']}（参数: 涨幅{r['params']['chg']} 换手{r['params']['turn']}）"
    ]
    if r["total_signals"] == 0:
        lines.append("   ❌ 无信号触发（条件过严/池子不匹配）")
        return "\n".join(lines)
    for key in ["overnight_open", "ov_ex_limit", "hold_to_close"]:
        m = r[key]
        if m["n"]:
            lines.append(
                f"   {m['label']:14s} n={m['n']:4d} 均值{m['mean']:+.3%} 胜率{m['win_rate']:.0%} p={m['p']}"
            )
    e = r.get("excess_overnight")
    if e:
        lines.append(
            f"   超额(隔夜)      n={e['n']:4d} 均值{e['mean']:+.3%} 胜率{e['win_rate']:.0%} p={e['p']}"
        )
    c = r["concentration"]
    lines.append(f"   集中度: {c['top_symbol']} {c['top_count']}个 ({c['top_ratio']:.0%})")
    excl = r.get("excl_top_excess")
    if excl:
        lines.append(f"   剔除最大贡献者: n={excl['n']} 超额{excl['mean']:+.3%} p={excl['p']}")
    sp = r["split"]
    lines.append(f"   切分: train={sp['train_count']} val={sp['val_count']}")
    for name in ["train_excess", "val_excess"]:
        v = r.get(name)
        if v:
            lines.append(f"   {name}: n={v['n']} 超额{v['mean']:+.3%} p={v['p']}")
    if r.get("by_year"):
        yr = " ".join(
            f"{y}:{v['mean']:+.2%}(n={v['n']},p={v['p']})" for y, v in sorted(r["by_year"].items())
        )
        lines.append(f"   年份分桶: {yr}")
    return "\n".join(lines)


def main() -> None:
    print("拉取行情数据（28 只 + 沪深300）...")
    dfs: dict[str, pd.DataFrame] = {}
    quotes: dict[str, dict] = {}
    for sym in POOL + LARGE_CAP_CTRL:
        try:
            df = fetch_kline(sym)
            q = fetch_quote(sym)
            dfs[sym] = build_features(df, q)
            quotes[sym] = q
            print(f"  ✅ {sym} ({len(df)}d, 流通市值{q['mcap_yi']:.1f}亿)")
        except Exception as e:
            print(f"  ❌ {sym}: {e}")
    index_df = fetch_kline("sh000300")
    print(f"  ✅ sh000300 ({len(index_df)}d)")

    print("\n" + "=" * 95)
    print("【基础版】6 条件 AND + 铁律次日开盘卖")
    r = run_study(dfs, index_df)
    print(fmt(r))
    print("\n" + "=" * 95)
    print("【大盘对照】4 只大盘股应被市值条件全过滤")
    large_dfs = {s: dfs[s] for s in LARGE_CAP_CTRL if s in dfs}
    r_large = run_study(large_dfs, index_df)
    print(fmt(r_large))

    print("\n" + "=" * 95)
    print("【参数敏感性】涨幅 × 换手网格（方向对、数值需校准）")
    grid = [
        ((0.02, 0.04), (0.05, 0.10), "涨幅2-4%"),
        ((0.03, 0.05), (0.05, 0.10), "涨幅3-5% 基准"),
        ((0.04, 0.06), (0.05, 0.10), "涨幅4-6%"),
        ((0.03, 0.05), (0.04, 0.09), "换手4-9%"),
        ((0.03, 0.05), (0.06, 0.11), "换手6-11%"),
        ((0.02, 0.06), (0.04, 0.11), "双放宽"),
    ]
    for (cl, ch), (tl, th), label in grid:
        rg = run_study(dfs, index_df, cl, ch, tl, th)
        m = rg.get("overnight_open")
        e = rg.get("excess_overnight")
        v = rg.get("val_excess")
        line = (
            f"  {label:12s} n={m['n']:4d} 隔夜{m['mean']:+.3%} 胜率{m['win_rate']:.0%}"
            if m["n"]
            else f"  {label:12s} n=0"
        )
        if e:
            line += f" | 超额{e['mean']:+.3%}(p={e['p']})"
        if v:
            line += f" | 验证期{v['mean']:+.3%}(p={v['p']})"
        print(line)

    print("\n" + "=" * 95)
    # 条件诊断：逐条件过滤后信号数（用基准参数检测）
    ev_dfs_all = {sym: detect_events(df, *BASE_CHG, *BASE_TURN) for sym, df in dfs.items()}
    print("【条件漏斗】逐条加条件的信号数（诊断哪个条件最严）")
    d_first = next(iter(ev_dfs_all.values()))
    for cond, name in [
        ("cond_1_chg", "涨幅3-5%"),
        ("cond_2_limit", "+20日有涨停"),
        ("cond_3_vr", "+量比>1"),
        ("cond_4_mcap", "+市值<200亿"),
        ("cond_5_turn", "+换手5-10%"),
        ("cond_6_above_avg", "+收盘>均价"),
    ]:
        total_hit = sum(
            1 for _, df in ev_dfs_all.items() if cond in df.columns and bool(df[cond].any())
        )
        # 漏斗：逐条件累计
        funnel = 0
        for _, df in ev_dfs_all.items():
            cols = []
            all_cols = [
                "cond_1_chg",
                "cond_2_limit",
                "cond_3_vr",
                "cond_4_mcap",
                "cond_5_turn",
                "cond_6_above_avg",
            ]
            for col in all_cols[: all_cols.index(cond) + 1]:
                cols.append(df[col])
            if cols:
                m = pd.concat(cols, axis=1).all(axis=1)
                funnel += int(m.sum())
        print(f"  {name:16s} 触发股票数={total_hit:2d} | 漏斗累计事件={funnel:4d}")

    # 事件明细抽样
    print("\n" + "=" * 95)
    print("【事件明细抽样（最近 10 个）】")
    all_events = []
    for sym, df in ev_dfs_all.items():
        ev = df[df["event"] == True]  # noqa: E712
        for _, row in ev.iterrows():
            all_events.append(
                (row["date"], sym, row["chg"], row["turnover"], row["market_cap"] / 1e8)
            )
    all_events.sort(reverse=True)
    for date, sym, chg, turn, mcap in all_events[:10]:
        print(f"  {date.date()} {sym} 涨幅{chg:+.2%} 换手{turn:.1%} 市值{mcap:.0f}亿")


if __name__ == "__main__":
    main()
