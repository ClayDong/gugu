"""策略诊断脚本：对单个策略深度诊断，找出"为什么回测不通过"的根因。

诊断内容：
1. 分阶段回测（牛/熊/震荡）—— 用 HS300 指数 MA120 判断 regime
2. 信号分布分析 —— 信号集中在哪些价格区间 / 股票 / 阶段
3. 失败 trade 归因 —— 亏损 trade 的持仓天数 / 波动率 / 阶段共同特征
4. 输出诊断报告 —— 策略逻辑修正建议 + 适用场景建议

用法:
    uv run python scripts/diagnose_strategy.py --strategy turtle --stocks 600519,002714
    uv run python scripts/diagnose_strategy.py --strategy turtle --stocks 600519 --days 500
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest import BacktestEngine  # noqa: E402
from gugu.backtest.engine import BacktestResult, Trade  # noqa: E402
from gugu.data import data_manager  # noqa: E402
from gugu.data.stock_names import lookup_stock_name  # noqa: E402
from gugu.strategies.registry import get_strategy, list_strategies  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()

# HS300 指数代码
_HS300_CODE = "000300"
# regime 划分用的均线窗口（MA120：A股数据通常 250 天，留更多有效区间）
_REGIME_MA_WINDOW = 120
# 价格区间阈值（元）
_PRICE_TIER_LOW = 10.0
_PRICE_TIER_HIGH = 50.0


# === 市场阶段分类（基于 HS300 MA120） ===


def classify_hs300_regime(hs300_df: pd.DataFrame) -> pd.DataFrame:
    """基于 HS300 指数 MA120 划分每个交易日的市场阶段。

    bull:     close > MA120 * 1.05
    bear:     close < MA120 * 0.95
    sideways: 中间

    Args:
        hs300_df: HS300 指数日线数据，含 date/close 列。

    Returns:
        DataFrame，新增 regime 列和 ma 列，date 列统一为 date 对象。
    """
    df = hs300_df.copy()
    df["ma"] = df["close"].rolling(_REGIME_MA_WINDOW, min_periods=1).mean()
    df["regime"] = "sideways"
    df.loc[df["close"] > df["ma"] * 1.05, "regime"] = "bull"
    df.loc[df["close"] < df["ma"] * 0.95, "regime"] = "bear"
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def build_regime_map(regime_df: pd.DataFrame) -> dict:
    """构建 regime 查询结构：{mapping: {date: regime}, sorted_dates: [...]}。"""
    sorted_dates = sorted(regime_df["date"].tolist())
    mapping = {row["date"]: row["regime"] for _, row in regime_df.iterrows()}
    return {"mapping": mapping, "sorted_dates": sorted_dates}


def lookup_regime(date_val: Any, regime_map: dict | None) -> str:
    """查找某交易日的 regime。

    找不到精确日期时取最近交易日（A 股交易日与 HS300 可能不完全对齐）。
    regime_map 为 None 时返回 "未知"。
    """
    if regime_map is None:
        return "未知"
    try:
        d = pd.Timestamp(date_val).date()
    except Exception:
        return "sideways"
    mapping = regime_map["mapping"]
    if d in mapping:
        return mapping[d]
    # 二分查找最近日期
    sorted_dates = regime_map["sorted_dates"]
    if not sorted_dates:
        return "sideways"
    idx = bisect.bisect_left(sorted_dates, d)
    candidates: list = []
    if idx > 0:
        candidates.append(sorted_dates[idx - 1])
    if idx < len(sorted_dates):
        candidates.append(sorted_dates[idx])
    if not candidates:
        return "sideways"
    nearest = min(candidates, key=lambda x: abs((x - d).days))
    return mapping.get(nearest, "sideways")


def build_stock_regime_map(df: pd.DataFrame) -> dict:
    """降级方案：用个股自身 MA120 划分 regime（HS300 获取失败时使用）。

    与 backtest_by_regime.py 的逻辑一致。
    """
    df2 = df.copy()
    df2["ma"] = df2["close"].rolling(_REGIME_MA_WINDOW, min_periods=1).mean()
    df2["regime"] = "sideways"
    df2.loc[df2["close"] > df2["ma"] * 1.05, "regime"] = "bull"
    df2.loc[df2["close"] < df2["ma"] * 0.95, "regime"] = "bear"
    df2["date"] = pd.to_datetime(df2["date"]).dt.date
    return build_regime_map(df2)


# === 信号分布分析 ===


def classify_price_tier(price: float) -> str:
    """按价格区间分类（元）。"""
    if price < _PRICE_TIER_LOW:
        return f"低价股(<{_PRICE_TIER_LOW:.0f}元)"
    if price < _PRICE_TIER_HIGH:
        return f"中价股({_PRICE_TIER_LOW:.0f}-{_PRICE_TIER_HIGH:.0f}元)"
    return f"高价股(>{_PRICE_TIER_HIGH:.0f}元)"


def analyze_signal_distribution(
    strategy_name: str,
    df: pd.DataFrame,
    regime_map: dict | None,
) -> dict:
    """分析策略产生的买入信号分布。

    Returns:
        {buy_count, sell_count, buy_details: [{date, price, price_tier, regime}]}
    """
    strategy = get_strategy(strategy_name)
    signals_df = strategy.generate_signals(df, is_backtest=True)
    buy_signals = signals_df[signals_df["signal"] == 1]
    sell_signals = signals_df[signals_df["signal"] == -1]

    buy_details = []
    for _, row in buy_signals.iterrows():
        price = float(row["close"])
        try:
            d = pd.Timestamp(row["date"]).date()
        except Exception:
            d = None
        regime = lookup_regime(d, regime_map) if d else "未知"
        buy_details.append(
            {
                "date": str(row["date"]),
                "price": price,
                "price_tier": classify_price_tier(price),
                "regime": regime,
            }
        )

    return {
        "buy_count": len(buy_signals),
        "sell_count": len(sell_signals),
        "buy_details": buy_details,
    }


# === 失败 trade 归因 ===


def compute_holding_volatility(df: pd.DataFrame, buy_date: Any, sell_date: Any) -> float:
    """计算持仓期间股票日收益波动率（标准差）。

    用于判断亏损 trade 是否集中在高波动期。
    """
    try:
        buy_ts = pd.Timestamp(buy_date)
        sell_ts = pd.Timestamp(sell_date)
        df_dated = df.copy()
        df_dated["date_ts"] = pd.to_datetime(df_dated["date"])
        mask = (df_dated["date_ts"] >= buy_ts) & (df_dated["date_ts"] <= sell_ts)
        period = df_dated[mask]
        if len(period) < 2:
            return 0.0
        returns = period["close"].pct_change().dropna()
        if len(returns) < 1:
            return 0.0
        return float(returns.std())
    except Exception:
        return 0.0


def analyze_trades(
    result: BacktestResult,
    df: pd.DataFrame,
    symbol: str,
    regime_map: dict | None,
) -> dict:
    """配对 buy/sell，分析每笔交易的持仓天数/波动率/阶段。

    引擎在卖出时清仓全部持仓（position_qty=0），所以用首笔买入作为开仓点。
    """
    pending_buys: list[Trade] = []
    winning: list[dict] = []
    losing: list[dict] = []

    for trade in result.trades:
        if trade.direction == "buy":
            pending_buys.append(trade)
        elif trade.direction == "sell" and pending_buys:
            first_buy = pending_buys[0]
            entry_price = first_buy.price
            hold_days = (
                pd.Timestamp(trade.date) - pd.Timestamp(first_buy.date)
            ).days
            volatility = compute_holding_volatility(df, first_buy.date, trade.date)
            regime_at_buy = lookup_regime(first_buy.date, regime_map)
            regime_at_sell = lookup_regime(trade.date, regime_map)

            info = {
                "symbol": symbol,
                "buy_date": str(first_buy.date),
                "sell_date": str(trade.date),
                "entry_price": entry_price,
                "exit_price": trade.price,
                "profit": trade.profit,
                "hold_days": hold_days,
                "volatility": volatility,
                "regime_at_buy": regime_at_buy,
                "regime_at_sell": regime_at_sell,
                "price_tier": classify_price_tier(entry_price),
            }
            if trade.profit > 0:
                winning.append(info)
            else:
                losing.append(info)
            pending_buys = []

    return {"winning": winning, "losing": losing}


# === 报告生成 ===


def _fmt_pct(x: float) -> str:
    """格式化百分比。"""
    return f"{x * 100:+.2f}%"


def generate_report(
    strategy_name: str,
    symbols: list[str],
    days: int,
    hs300_status: str,
    hs300_regime_summary: dict,
    per_stock_results: list[dict],
    signal_distribution: dict,
    all_winning: list[dict],
    all_losing: list[dict],
) -> str:
    """生成 markdown 诊断报告。"""
    today = str(date.today())
    lines: list[str] = []

    lines.append(f"# 策略诊断报告：{strategy_name}")
    lines.append("")
    lines.append(f"- 生成日期: {today}")
    lines.append(f"- 诊断股票: {', '.join(symbols)}")
    lines.append(f"- 回测天数: {days}")
    lines.append(f"- Regime 数据源: {hs300_status}")
    lines.append("")

    # === 1. 总体回测结果 ===
    lines.append("## 1. 总体回测结果")
    lines.append("")
    lines.append("| 股票 | 总收益 | 年化 | 夏普 | 最大回撤 | 胜率 | 交易数 | 平均持仓天 |")
    lines.append("|------|--------|------|------|----------|------|--------|-----------|")
    for r in per_stock_results:
        m = r["overall_metrics"]
        name = lookup_stock_name(r["symbol"]) or r["symbol"]
        lines.append(
            f"| {name}({r['symbol']}) | {_fmt_pct(m.get('total_return', 0))} | "
            f"{_fmt_pct(m.get('annual_return', 0))} | "
            f"{m.get('sharpe', 0):.2f} | {_fmt_pct(m.get('max_drawdown', 0))} | "
            f"{m.get('win_rate', 0) * 100:.1f}% | {int(m.get('total_trades', 0))} | "
            f"{m.get('avg_hold_days', 0):.1f} |"
        )
    lines.append("")

    n_stocks = max(len(per_stock_results), 1)
    avg_return = sum(
        r["overall_metrics"].get("total_return", 0) for r in per_stock_results
    ) / n_stocks
    avg_sharpe = sum(
        r["overall_metrics"].get("sharpe", 0) for r in per_stock_results
    ) / n_stocks
    lines.append(f"**平均总收益**: {_fmt_pct(avg_return)} | **平均夏普**: {avg_sharpe:.2f}")
    lines.append("")
    if avg_return < 0 or avg_sharpe < 0:
        lines.append("> ⚠️ 策略整体表现不佳，需要诊断根因。")
    else:
        lines.append("> ✓ 策略整体表现可接受，但仍可优化。")
    lines.append("")

    # === 2. 分阶段回测 ===
    lines.append("## 2. 分阶段回测（基于 HS300 指数 MA120 划分）")
    lines.append("")
    if hs300_regime_summary:
        lines.append(
            f"HS300 样本区间阶段分布: 牛市 {hs300_regime_summary.get('bull', 0)} 天 / "
            f"熊市 {hs300_regime_summary.get('bear', 0)} 天 / "
            f"震荡 {hs300_regime_summary.get('sideways', 0)} 天"
        )
    else:
        lines.append("HS300 数据不可用，已降级为个股 MA120 划分")
    lines.append("")
    lines.append("| 阶段 | 交易数 | 胜率 | 平均收益 | 平均持仓天数 |")
    lines.append("|------|--------|------|----------|--------------|")

    # 汇总各阶段
    phase_summary: dict[str, dict] = {
        "bull": {"n": 0, "wins": 0, "profit": 0.0, "hold": 0.0},
        "bear": {"n": 0, "wins": 0, "profit": 0.0, "hold": 0.0},
        "sideways": {"n": 0, "wins": 0, "profit": 0.0, "hold": 0.0},
    }
    for r in per_stock_results:
        for regime, trades in r["trades_by_regime"].items():
            for t in trades:
                phase_summary[regime]["n"] += 1
                if t["profit"] > 0:
                    phase_summary[regime]["wins"] += 1
                phase_summary[regime]["profit"] += t["profit"]
                phase_summary[regime]["hold"] += t["hold_days"]

    regime_cn = {"bull": "牛市", "bear": "熊市", "sideways": "震荡市"}
    for regime in ["bull", "bear", "sideways"]:
        s = phase_summary[regime]
        if s["n"] == 0:
            lines.append(f"| {regime_cn[regime]} | 0 | - | - | - |")
        else:
            wr = s["wins"] / s["n"]
            avg_p = s["profit"] / s["n"]
            avg_h = s["hold"] / s["n"]
            lines.append(
                f"| {regime_cn[regime]} | {s['n']} | {wr * 100:.1f}% | "
                f"{avg_p:+.0f} | {avg_h:.1f} |"
            )
    lines.append("")

    # 找出最佳/最差阶段
    best_regime = None
    best_score = -999.0
    worst_regime = None
    worst_score = 999.0
    for regime in ["bull", "bear", "sideways"]:
        s = phase_summary[regime]
        if s["n"] == 0:
            continue
        score = s["profit"] / s["n"]
        if score > best_score:
            best_score = score
            best_regime = regime
        if score < worst_score:
            worst_score = score
            worst_regime = regime

    if best_regime:
        lines.append(f"**最佳阶段**: {regime_cn[best_regime]}（平均收益 {best_score:+.0f}/笔）")
    if worst_regime and worst_regime != best_regime:
        lines.append(f"**最差阶段**: {regime_cn[worst_regime]}（平均收益 {worst_score:+.0f}/笔）")
    lines.append("")

    # === 3. 信号分布分析 ===
    lines.append("## 3. 信号分布分析")
    lines.append("")
    total_buy = sum(s["buy_count"] for s in signal_distribution.values())
    total_sell = sum(s["sell_count"] for s in signal_distribution.values())
    lines.append(f"**总买入信号**: {total_buy} | **总卖出信号**: {total_sell}")
    lines.append("")

    # 按价格区间分布
    tier_counts: dict[str, int] = defaultdict(int)
    regime_signal_counts: dict[str, int] = defaultdict(int)
    for s in signal_distribution.values():
        for d in s["buy_details"]:
            tier_counts[d["price_tier"]] += 1
            regime_signal_counts[d["regime"]] += 1

    lines.append("买入信号价格区间分布:")
    lines.append("")
    lines.append("| 价格区间 | 信号数 | 占比 |")
    lines.append("|----------|--------|------|")
    tier_order = [
        f"低价股(<{_PRICE_TIER_LOW:.0f}元)",
        f"中价股({_PRICE_TIER_LOW:.0f}-{_PRICE_TIER_HIGH:.0f}元)",
        f"高价股(>{_PRICE_TIER_HIGH:.0f}元)",
    ]
    for tier in tier_order:
        n = tier_counts[tier]
        pct = n / max(total_buy, 1) * 100
        lines.append(f"| {tier} | {n} | {pct:.1f}% |")
    lines.append("")

    # 按阶段分布
    lines.append("买入信号阶段分布:")
    lines.append("")
    lines.append("| 阶段 | 信号数 | 占比 |")
    lines.append("|------|--------|------|")
    for regime in ["bull", "bear", "sideways", "未知"]:
        n = regime_signal_counts[regime]
        if n == 0:
            continue
        pct = n / max(total_buy, 1) * 100
        lines.append(f"| {regime_cn.get(regime, regime)} | {n} | {pct:.1f}% |")
    lines.append("")

    # 各股票信号数
    lines.append("各股票信号数:")
    lines.append("")
    lines.append("| 股票 | 买入信号数 | 卖出信号数 |")
    lines.append("|------|-----------|-----------|")
    for sym in symbols:
        s = signal_distribution.get(sym, {})
        name = lookup_stock_name(sym) or sym
        lines.append(
            f"| {name}({sym}) | {s.get('buy_count', 0)} | {s.get('sell_count', 0)} |"
        )
    lines.append("")

    # === 4. 失败 trade 归因 ===
    lines.append("## 4. 失败 trade 归因")
    lines.append("")
    lines.append(f"**盈利 trade 数**: {len(all_winning)} | **亏损 trade 数**: {len(all_losing)}")
    lines.append("")

    if not all_losing and not all_winning:
        lines.append("无交易记录，无法做归因分析。")
        lines.append("")
    else:
        if all_losing and all_winning:
            hold_lose = [t["hold_days"] for t in all_losing]
            hold_win = [t["hold_days"] for t in all_winning]
            vol_lose = [t["volatility"] for t in all_losing]
            vol_win = [t["volatility"] for t in all_winning]

            avg_h_lose = sum(hold_lose) / len(hold_lose)
            avg_h_win = sum(hold_win) / len(hold_win)
            avg_v_lose = sum(vol_lose) / len(vol_lose)
            avg_v_win = sum(vol_win) / len(vol_win)

            lines.append("亏损 trade vs 盈利 trade 特征对比:")
            lines.append("")
            lines.append("| 特征 | 亏损 trade 均值 | 盈利 trade 均值 | 差异 |")
            lines.append("|------|-----------------|-----------------|------|")
            lines.append(
                f"| 持仓天数 | {avg_h_lose:.1f} | {avg_h_win:.1f} | {avg_h_lose - avg_h_win:+.1f} |"
            )
            lines.append(
                f"| 持仓期波动率 | {avg_v_lose:.4f} | {avg_v_win:.4f} | {avg_v_lose - avg_v_win:+.4f} |"
            )
            lines.append("")

        if all_losing:
            # 亏损 trade 阶段分布
            regime_lose: dict[str, int] = defaultdict(int)
            tier_lose: dict[str, int] = defaultdict(int)
            for t in all_losing:
                regime_lose[t["regime_at_sell"]] += 1
                tier_lose[t["price_tier"]] += 1

            lines.append("亏损 trade 在各阶段的分布:")
            lines.append("")
            lines.append("| 阶段 | 亏损 trade 数 | 占比 |")
            lines.append("|------|--------------|------|")
            for regime in ["bull", "bear", "sideways", "未知"]:
                n = regime_lose[regime]
                if n == 0:
                    continue
                pct = n / max(len(all_losing), 1) * 100
                lines.append(f"| {regime_cn.get(regime, regime)} | {n} | {pct:.1f}% |")
            lines.append("")

            lines.append("亏损 trade 价格区间分布:")
            lines.append("")
            lines.append("| 价格区间 | 亏损 trade 数 | 占比 |")
            lines.append("|----------|--------------|------|")
            for tier in tier_order:
                n = tier_lose[tier]
                pct = n / max(len(all_losing), 1) * 100
                lines.append(f"| {tier} | {n} | {pct:.1f}% |")
            lines.append("")

    # === 5. 诊断结论与建议 ===
    lines.append("## 5. 诊断结论与建议")
    lines.append("")

    suggestions: list[str] = []

    # 阶段适应性建议
    if best_regime:
        suggestions.append(
            f"**适用场景**: 策略在 **{regime_cn[best_regime]}** 表现最佳"
            f"（平均收益 {best_score:+.0f}/笔），建议在{regime_cn[best_regime]}启用。"
        )
    if worst_regime and worst_regime != best_regime and worst_score < 0:
        suggestions.append(
            f"**禁用场景**: 策略在 **{regime_cn[worst_regime]}** 亏损严重"
            f"（平均收益 {worst_score:+.0f}/笔），建议在{regime_cn[worst_regime]}禁用或减仓。"
        )

    # 持仓天数诊断
    if all_losing and all_winning:
        avg_h_lose = sum(t["hold_days"] for t in all_losing) / len(all_losing)
        avg_h_win = sum(t["hold_days"] for t in all_winning) / len(all_winning)
        if avg_h_lose > avg_h_win * 1.5 and avg_h_lose > 0:
            suggestions.append(
                f"**逻辑修正**: 亏损 trade 持仓天数 ({avg_h_lose:.1f}) 显著长于盈利 trade "
                f"({avg_h_win:.1f})，建议加入时间止损（如持仓超过 N 天强制平仓）。"
            )

    # 波动率诊断
    if all_losing and all_winning:
        avg_v_lose = sum(t["volatility"] for t in all_losing) / len(all_losing)
        avg_v_win = sum(t["volatility"] for t in all_winning) / len(all_winning)
        if avg_v_lose > avg_v_win * 1.2 and avg_v_lose > 0:
            suggestions.append(
                f"**逻辑修正**: 亏损 trade 持仓期波动率 ({avg_v_lose:.4f}) 显著高于盈利 trade "
                f"({avg_v_win:.4f})，建议加入波动率过滤（高波动期不入场或缩小仓位）。"
            )

    # 信号数量诊断
    if total_buy < len(symbols) * 2:
        suggestions.append(
            f"**信号过少**: 总买入信号仅 {total_buy} 个，平均每只股票 "
            f"{total_buy / max(len(symbols), 1):.1f} 个。可能参数过严或策略不适合当前股票池。"
        )
    elif total_buy > len(symbols) * 50:
        suggestions.append(
            f"**信号过多**: 总买入信号 {total_buy} 个，平均每只股票 "
            f"{total_buy / max(len(symbols), 1):.1f} 个。可能参数过松，存在过度交易风险。"
        )

    # 整体表现诊断
    if avg_return < -0.05:
        suggestions.append(
            "**整体亏损**: 平均总收益为负，策略当前参数不适合该股票池。"
            "建议：(1) 检查参数是否过拟合；"
            "(2) 评估策略逻辑是否匹配股票特性；"
            "(3) 考虑使用 `optimize_params.py --multi-stock` 重新优化。"
        )

    # 信号阶段集中度诊断
    if total_buy > 0:
        for regime in ["bull", "bear", "sideways"]:
            n = regime_signal_counts.get(regime, 0)
            pct = n / total_buy
            if pct > 0.6 and regime == "bear" and best_regime != "bear":
                suggestions.append(
                    f"**信号集中风险**: {pct * 100:.0f}% 的买入信号集中在{regime_cn[regime]}，"
                    f"但策略在{regime_cn[regime]}表现不佳，建议加入 regime 过滤器。"
                )
                break

    if not suggestions:
        suggestions.append("未发现明显问题，策略在当前股票池上表现可接受。")

    for s in suggestions:
        lines.append(f"- {s}")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*本报告由 `scripts/diagnose_strategy.py` 自动生成，仅作决策参考。*")
    lines.append("")

    return "\n".join(lines)


# === 主流程 ===


async def diagnose(strategy_name: str, symbols: list[str], days: int) -> str:
    """主诊断流程：获取数据 → 回测 → 分析 → 生成报告。"""
    if strategy_name not in list_strategies():
        available = ", ".join(list_strategies())
        logger.error(f"策略 '{strategy_name}' 未注册，可用: {available}")
        return ""

    dm = data_manager()
    engine = BacktestEngine()

    # 1. 获取 HS300 数据并分类 regime
    logger.info("获取 HS300 指数日线...")
    hs300_df = await dm.fetch_stock_history(_HS300_CODE, days=days)
    regime_map: dict | None = None
    hs300_regime_summary: dict = {}
    hs300_status = "HS300 指数 MA120"

    if hs300_df.empty or len(hs300_df) < _REGIME_MA_WINDOW:
        logger.warning(
            f"HS300 数据不足（{len(hs300_df) if not hs300_df.empty else 0} 行），"
            f"降级为各股票自身 MA120 分类"
        )
        hs300_status = "个股 MA120（HS300 不可用降级）"
    else:
        regime_df = classify_hs300_regime(hs300_df)
        regime_map = build_regime_map(regime_df)
        hs300_regime_summary = regime_df["regime"].value_counts().to_dict()
        logger.info(f"HS300 阶段分布: {hs300_regime_summary}")

    # 2. 对每只股票跑回测 + 分析
    per_stock_results: list[dict] = []
    signal_distribution: dict[str, dict] = {}
    all_winning: list[dict] = []
    all_losing: list[dict] = []

    for symbol in symbols:
        symbol = symbol.strip().zfill(6)
        logger.info(f"获取 {symbol} 历史数据...")
        df = await dm.fetch_stock_history(symbol, days=days)
        if df.empty or len(df) < 60:
            logger.warning(
                f"{symbol} 数据不足（{len(df) if not df.empty else 0} 行），跳过"
            )
            continue

        # 降级：若 HS300 不可用，用个股自身 MA120
        stock_regime_map = regime_map
        if stock_regime_map is None:
            stock_regime_map = build_stock_regime_map(df)

        # 回测
        strategy = get_strategy(strategy_name)
        bt_result = engine.run(strategy, df, symbol)
        logger.info(
            f"{symbol} 回测完成: trades={len(bt_result.trades)} "
            f"return={bt_result.metrics.get('total_return', 0):.2%}"
        )

        # 信号分布分析
        sig_dist = analyze_signal_distribution(strategy_name, df, regime_map)
        signal_distribution[symbol] = sig_dist

        # 失败 trade 归因
        trades_analysis = analyze_trades(bt_result, df, symbol, stock_regime_map)

        # 按 regime 归类 trades（用买入时的 regime）
        trades_by_regime: dict[str, list[dict]] = {
            "bull": [],
            "bear": [],
            "sideways": [],
        }
        for t in trades_analysis["winning"] + trades_analysis["losing"]:
            regime = t["regime_at_buy"]
            if regime in trades_by_regime:
                trades_by_regime[regime].append(t)
            else:
                # "未知" 归入 sideways
                trades_by_regime["sideways"].append(t)

        per_stock_results.append(
            {
                "symbol": symbol,
                "overall_metrics": bt_result.metrics,
                "trades_by_regime": trades_by_regime,
            }
        )

        all_winning.extend(trades_analysis["winning"])
        all_losing.extend(trades_analysis["losing"])

    if not per_stock_results:
        logger.error("无有效回测结果，无法生成诊断报告")
        return ""

    # 3. 生成报告
    report = generate_report(
        strategy_name=strategy_name,
        symbols=symbols,
        days=days,
        hs300_status=hs300_status,
        hs300_regime_summary=hs300_regime_summary,
        per_stock_results=per_stock_results,
        signal_distribution=signal_distribution,
        all_winning=all_winning,
        all_losing=all_losing,
    )

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="策略诊断脚本：找出回测不通过的根因",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  uv run python scripts/diagnose_strategy.py --strategy turtle --stocks 600519,002714\n"
            "  uv run python scripts/diagnose_strategy.py --strategy turtle --stocks 600519 --days 500\n\n"
            "诊断内容：\n"
            "  1. 分阶段回测（HS300 MA120 划分牛/熊/震荡）\n"
            "  2. 信号分布分析（价格区间/股票/阶段）\n"
            "  3. 失败 trade 归因（持仓天数/波动率/阶段）\n"
            "  4. 策略逻辑修正建议 + 适用场景建议\n"
        ),
    )
    parser.add_argument(
        "--strategy",
        type=str,
        required=True,
        help="策略名（如 turtle / dual_ma / bollinger 等）",
    )
    parser.add_argument(
        "--stocks",
        type=str,
        required=True,
        help="逗号分隔的股票代码（如 600519,002714）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=500,
        help="回测历史天数（默认 500）",
    )
    args = parser.parse_args()

    symbols = [s.strip() for s in args.stocks.split(",") if s.strip()]
    symbols = [s.zfill(6) for s in symbols]

    if not symbols:
        logger.error("--stocks 不能为空")
        sys.exit(1)

    report = asyncio.run(diagnose(args.strategy, symbols, args.days))
    if not report:
        sys.exit(1)

    # 输出到 stdout
    print(report)

    # 输出到文件
    today = str(date.today())
    output_path = Path(f"data/diagnose_{args.strategy}_{today}.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    logger.info(f"诊断报告已保存: {output_path}")


if __name__ == "__main__":
    main()
