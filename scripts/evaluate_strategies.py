"""策略回测评估：逐策略回测沪深300成分股 + 真实持仓 ETF，输出诊断报告。

P0 修复（基于第一性原理 + 对抗性审查）：
- 移除 except Exception: pass 静默吞错，改为日志记录失败股票
- 新增 ETF 评估（用 ak.fund_etf_hist_em 专用接口，绕过网易 502 故障）
- 新增 JSON 报告输出（供后续参数微调决策）
- 区分"无信号"策略（避免误判为 0% 收益）
- 增加"样本外可用性"诊断（交易次数 <3 视为不可靠）
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import akshare as ak

from gugu.backtest.engine import BacktestEngine
from gugu.config import PROJECT_ROOT
from gugu.data import data_manager
from gugu.strategies.registry import get_enabled_strategies
from gugu.utils.log import get_logger

logger = get_logger()


async def _fetch_etf_history(symbol: str, days: int) -> "object":
    """P0 修复：ETF 专用数据接口，绕过网易 502 故障。

    ak.fund_etf_hist_em 返回的列名与个股接口不同，需统一为
    date/open/high/low/close/volume/amount 格式。
    """
    try:
        df = ak.fund_etf_hist_em(symbol=symbol, period="daily", adjust="qfq")
        if df is None or df.empty:
            return df
        # 取最近 days 行
        df = df.tail(days).copy()
        # 统一列名（akshare ETF 接口返回"日期/开盘/收盘/最高/最低/成交量/成交额"）
        col_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
        }
        df = df.rename(columns=col_map)
        # 确保 date 是 datetime
        df["date"] = df["date"].astype(str)
        return df
    except Exception as e:
        logger.warning(f"ETF {symbol} 数据获取失败: {e}")
        import pandas as pd
        return pd.DataFrame()


async def evaluate(top_n: int = 30, include_etfs: bool = True) -> None:
    """执行全市场策略评估。

    Args:
        top_n: 沪深300成分股取前 N 只
        include_etfs: 是否包含真实持仓的 4 只 ETF
    """
    dm = data_manager()
    strategies = get_enabled_strategies()
    engine = BacktestEngine()

    # 真实持仓的 ETF（从 paper_broker_state.json 读取）
    etf_symbols: list[str] = []
    if include_etfs:
        state_file = PROJECT_ROOT / "data" / "paper_broker_state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                for sym, pos in state.get("positions", {}).items():
                    code = sym.strip().zfill(6)
                    # ETF 代码前缀：51/52/56/58/15/16/18
                    if code.startswith(("51", "52", "56", "58", "15", "16", "18")):
                        etf_symbols.append(code)
            except Exception as e:
                logger.warning(f"读取 paper_broker_state 失败: {e}")

    # 获取沪深300成分股
    print("正在获取沪深300成分股...")
    codes: list[str] = []
    names: list[str] = []
    try:
        df_hs300 = ak.index_stock_cons_csindex(symbol="000300")
        codes = [str(c).strip().zfill(6) for c in df_hs300["成分券代码"].tolist()[:top_n]]
        names = [str(n) for n in df_hs300["成分券名称"].tolist()[:top_n]]
    except Exception as e:
        logger.error(f"获取沪深300失败: {e}")

    # 合并 ETF
    all_symbols: list[tuple[str, str]] = list(zip(codes, names))
    for etf in etf_symbols:
        if etf not in codes:
            all_symbols.append((etf, f"ETF-{etf}"))

    total = len(all_symbols)
    print(f"共 {total} 只标的（沪深300前{top_n} + {len(etf_symbols)} 只ETF），{len(strategies)} 个策略\n")

    # 收集结果
    all_results: dict[str, list[dict]] = {s.name: [] for s in strategies}
    failed_symbols: list[str] = []

    for i, (symbol, name) in enumerate(all_symbols):
        try:
            # P0 修复：ETF 用专用接口
            is_etf = symbol.startswith(("51", "52", "56", "58", "15", "16", "18"))
            if is_etf:
                df = await _fetch_etf_history(symbol, days=250)
            else:
                df = await dm.fetch_stock_history(symbol, days=250)

            if df is None or df.empty or len(df) < 60:
                failed_symbols.append(f"{symbol}({name})")
                continue

            for strategy in strategies:
                try:
                    result = engine.run(strategy, df, symbol=symbol)
                    all_results[strategy.name].append({
                        "symbol": symbol,
                        "name": name,
                        "is_etf": is_etf,
                        "total_return": float(result.metrics["total_return"]),
                        "sharpe": float(result.metrics["sharpe"]),
                        "max_drawdown": float(result.metrics["max_drawdown"]),
                        "win_rate": float(result.metrics["win_rate"]),
                        "profit_factor": float(result.metrics["profit_factor"]),
                        "total_trades": int(result.metrics["total_trades"]),
                    })
                except Exception as e:
                    logger.debug(f"{symbol}/{strategy.name} 回测失败: {e}")
        except Exception as e:
            logger.warning(f"{symbol}({name}) 整体失败: {e}")
            failed_symbols.append(f"{symbol}({name})")

        if (i + 1) % 10 == 0:
            print(f"  已完成 {i+1}/{total}")

    # 输出汇总
    print("\n" + "=" * 100)
    print(f"{'策略':<20} {'标的数':>5} {'平均收益':>8} {'胜率':>6} {'盈亏比':>6} {'夏普':>6} {'最大回撤':>8} {'平均交易':>6} {'诊断':<20}")
    print("-" * 100)

    report_data: list[dict] = []
    for strategy in strategies:
        results = all_results[strategy.name]
        if not results:
            print(f"{strategy.name:<20} {'无数据':>5}")
            report_data.append({"strategy": strategy.name, "status": "no_data"})
            continue

        # 区分"无信号"标的（total_trades=0）
        active_results = [r for r in results if r["total_trades"] > 0]
        no_signal_count = len(results) - len(active_results)

        avg_return = sum(r["total_return"] for r in results) / len(results)
        avg_win = sum(r["win_rate"] for r in results) / len(results)
        valid_pf = [r["profit_factor"] for r in results if r["profit_factor"] != float("inf")]
        avg_pf = sum(valid_pf) / max(1, len(valid_pf))
        avg_sharpe = sum(r["sharpe"] for r in results) / len(results)
        avg_dd = sum(r["max_drawdown"] for r in results) / len(results)
        avg_trades = sum(r["total_trades"] for r in results) / len(results)

        # P0 诊断标记
        flags: list[str] = []
        if avg_return < 0:
            flags.append("亏损")
        if avg_win < 0.4 and len(active_results) > 0:
            flags.append("低胜率")
        if avg_dd > 0.3:
            flags.append("高回撤")
        if avg_trades < 3:
            flags.append("信号过少")
        if no_signal_count > len(results) * 0.5:
            flags.append("半数无信号")
        if not flags:
            flags.append("可用")

        flag_str = " ".join(flags)
        print(
            f"{strategy.name:<20} {len(results):>5} {avg_return:>+7.2%} {avg_win:>5.1%} "
            f"{avg_pf:>5.2f} {avg_sharpe:>5.3f} {avg_dd:>7.2%} {avg_trades:>5.1f}  {flag_str}"
        )

        report_data.append({
            "strategy": strategy.name,
            "symbol_count": len(results),
            "active_count": len(active_results),
            "no_signal_count": no_signal_count,
            "avg_return": round(avg_return, 4),
            "avg_win_rate": round(avg_win, 4),
            "avg_profit_factor": round(avg_pf, 4),
            "avg_sharpe": round(avg_sharpe, 4),
            "avg_max_drawdown": round(avg_dd, 4),
            "avg_trades": round(avg_trades, 2),
            "diagnosis": flags,
        })

    print("-" * 100)
    if failed_symbols:
        print(f"数据获取失败 {len(failed_symbols)} 只: {failed_symbols[:5]}{'...' if len(failed_symbols) > 5 else ''}")
    print("\n诊断说明：亏损=平均收益<0 | 低胜率=<40% | 高回撤=>30% | 信号过少=平均交易<3 | 半数无信号=>50%标的未触发")

    # P0 修复：输出 JSON 报告供后续决策
    report = {
        "evaluated_at": datetime.now().isoformat(),
        "total_symbols": total,
        "failed_symbols": failed_symbols,
        "strategies": report_data,
    }
    report_path = PROJECT_ROOT / "data" / "strategy_evaluation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细报告已保存: {report_path}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    asyncio.run(evaluate(top_n=n))
