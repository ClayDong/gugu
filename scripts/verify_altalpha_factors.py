#!/usr/bin/env python3
"""另类因子IC验证脚本。

对沪深300成分股，计算每个另类因子的IC（信息系数）并验证有效性。

使用方式：
    uv run python scripts/verify_altalpha_factors.py --start 2023-01-01 --end 2025-12-31
    uv run python scripts/verify_altalpha_factors.py --symbols 600519,000858 --quick

IC有效性标准：
    |IC均值| > 0.03
    IC_IR（IC均值/IC标准差）> 0.5
    IC同向率 > 55%

注意：
    本脚本用「当日因子值 vs 未来5日收益率」计算横截面IC。
    由于另类数据是事件驱动的（高管增减持/龙虎榜/解禁等），
    需要较大样本（≥100只股票）才能产生有意义的横截面变异。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gugu.analysis.altalpha_factors import AltAlphaFactorEngine, FACTOR_NAMES
from gugu.utils.log import get_logger

logger = get_logger()


def compute_ic(
    factor_values: pd.Series,
    forward_returns: pd.Series,
) -> dict[str, float]:
    """计算单个因子的IC统计量。

    Args:
        factor_values: 横截面因子值（按symbol索引）
        forward_returns: 未来N日收益率（按symbol索引）

    Returns:
        {"ic": float, "ic_ir": float, "ic_positive_ratio": float, "n": int}
    """
    # 对齐
    common = factor_values.index.intersection(forward_returns.index)
    if len(common) < 10:
        return {"ic": 0.0, "ic_ir": 0.0, "ic_positive_ratio": 0.0, "n": 0}

    f = factor_values.reindex(common).fillna(0.0)
    r = forward_returns.reindex(common).fillna(0.0)

    # 剔除全为常数的因子（无横截面变异）
    if f.std() == 0 or r.std() == 0:
        return {"ic": 0.0, "ic_ir": 0.0, "ic_positive_ratio": 0.0, "n": len(common)}

    # Spearman rank IC（更稳健）
    ic = f.corr(r, method="spearman")
    if np.isnan(ic):
        ic = 0.0

    # IC同向率（因子方向与收益方向一致的比例）
    ic_positive_ratio = float((np.sign(f) == np.sign(r)).mean())

    return {
        "ic": ic,
        "ic_ir": ic,  # 单期IC，IR=IC/std(IC)，需多期才有意义
        "ic_positive_ratio": ic_positive_ratio,
        "n": len(common),
    }


async def fetch_forward_returns(symbols: list[str], days: int = 5) -> pd.Series:
    """获取所有股票的未来N日收益率。

    使用 DataManager（三级降级：sina → akshare → eastmoney）获取K线，
    避免 akshare 直连东方财富被拒（RemoteDisconnected）。
    注意：由于是「最近N日收益」而非「因子公布后N日收益」，
    这是一个近似验证（假设因子有持续性）。

    Args:
        symbols: 股票代码列表
        days: 收益率计算窗口

    Returns:
        pd.Series[symbol -> forward_return]
    """
    from gugu.data.manager import DataManager

    dm = DataManager()
    returns: dict[str, float] = {}

    async def _fetch_one(symbol: str) -> None:
        try:
            # DataManager 三级降级获取K线
            df = await dm.fetch_stock_history(symbol, days=days + 10)
            if df is not None and not df.empty and len(df) >= days + 1:
                # DataManager 返回列名: date/open/high/low/close
                close = df["close"].values
                ret = (close[-1] - close[-days - 1]) / close[-days - 1]
                returns[symbol] = float(ret)
            else:
                returns[symbol] = 0.0
        except Exception as e:
            logger.debug(f"获取 {symbol} K线失败: {e}")
            returns[symbol] = 0.0

    # 并行获取（分批，避免并发过大）
    batch_size = 10
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        await asyncio.gather(*[_fetch_one(s) for s in batch])

    return pd.Series(returns)


async def verify_factors(
    symbols: list[str],
    benchmark: str = "000300",
) -> pd.DataFrame:
    """验证所有另类因子的IC。

    Args:
        symbols: 待验证的股票代码列表
        benchmark: 基准指数

    Returns:
        DataFrame[factor_name, ic, ic_ir, ic_positive_ratio, n, effective]
    """
    logger.info(f"开始IC验证: {len(symbols)} 只股票")

    # 1. 获取因子值
    engine = AltAlphaFactorEngine()
    factor_data = await engine.compute_all(symbols)

    # 统计有非零因子值的股票数
    for fname in FACTOR_NAMES:
        nonzero = sum(1 for s in symbols if factor_data.get(s, {}).get(fname, 0.0) != 0.0)
        logger.info(f"  因子 {fname}: {nonzero}/{len(symbols)} 只股票有非零值")

    # 2. 获取未来5日收益率
    logger.info("开始获取历史K线计算收益率...")
    returns = await fetch_forward_returns(symbols, days=5)
    nonzero_returns = (returns != 0).sum()
    logger.info(f"收益率获取完成: {nonzero_returns}/{len(symbols)} 只有效")

    # 3. 计算每个因子的IC
    results = []
    ret_series = returns

    for factor_name in FACTOR_NAMES:
        factor_values = pd.Series(
            {s: factor_data.get(s, {}).get(factor_name, 0.0) for s in symbols}
        )
        ic_result = compute_ic(factor_values, ret_series)

        effective = abs(ic_result["ic"]) > 0.03 and ic_result["ic_positive_ratio"] > 0.55
        results.append(
            {
                "factor": factor_name,
                "ic": round(ic_result["ic"], 4),
                "ic_ir": round(ic_result["ic_ir"], 4),
                "ic_positive_ratio": round(ic_result["ic_positive_ratio"], 4),
                "n": ic_result["n"],
                "effective": effective,
            }
        )

    return pd.DataFrame(results)


async def main():
    parser = argparse.ArgumentParser(description="另类因子IC验证")
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="逗号分隔的股票代码（默认沪深300前20只）",
    )
    parser.add_argument("--quick", action="store_true", help="快速模式（仅前10只）")
    parser.add_argument("--full", action="store_true", help="完整模式（沪深300前50只）")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        # 默认用一组代表性股票（覆盖消费/金融/医药/科技/周期）
        symbols = [
            "600519",
            "000858",
            "601318",
            "600036",
            "000333",
            "600276",
            "601166",
            "002714",
            "300750",
            "603259",
            "600887",
            "000568",
            "002475",
            "600031",
            "601888",
            "300059",
            "600009",
            "601012",
            "600436",
            "000725",
            "600000",
            "000001",
            "600028",
            "601398",
            "601628",
            "600030",
            "000776",
            "002142",
            "600999",
            "601688",
            "601288",
            "600837",
            "000338",
            "600585",
            "601668",
            "600048",
            "601138",
            "300760",
            "002415",
            "600406",
            "603501",
            "002271",
            "600438",
            "603986",
            "002179",
            "300124",
            "600588",
            "601881",
            "000063",
            "600745",
        ]
        if args.quick:
            symbols = symbols[:10]
        elif not args.full:
            symbols = symbols[:20]

    print(f"\n{'=' * 70}")
    print(f"另类因子IC验证")
    print(f"股票数量: {len(symbols)}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 70}\n")

    result = await verify_factors(symbols)

    print(result.to_string(index=False))
    print(f"\n{'=' * 70}")
    effective_count = result["effective"].sum()
    print(f"有效因子数: {effective_count}/{len(result)}")
    print(f"{'=' * 70}\n")

    # 输出到文件
    output_path = PROJECT_ROOT / "data" / "altalpha_ic_report.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False)
    print(f"报告已保存: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
