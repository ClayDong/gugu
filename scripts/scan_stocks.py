"""全市场扫描：从 A 股主要指数成分股中筛选当前有策略信号的股票。"""
from __future__ import annotations

import asyncio
import sys

import akshare as ak

from gugu.data import data_manager
from gugu.engine.signal_router import SignalRouter
from gugu.strategies.registry import get_enabled_strategies


async def scan(top_n: int = 30) -> None:
    dm = data_manager()
    strategies = get_enabled_strategies()
    # 用 any 规则 + 低置信度阈值，尽可能多捕获信号
    router = SignalRouter(strategies, fusion_rule="any", min_confidence=0.3)

    # 获取沪深300成分股
    print("正在获取沪深300成分股...")
    try:
        df_hs300 = ak.index_stock_cons_csindex(symbol="000300")
        codes = df_hs300["成分券代码"].tolist()
        names = df_hs300["成分券名称"].tolist()
    except Exception as e:
        print(f"获取沪深300失败: {e}，尝试上证50...")
        try:
            df_sz50 = ak.index_stock_cons_csindex(symbol="000016")
            codes = df_sz50["成分券代码"].tolist()
            names = df_sz50["成分券名称"].tolist()
        except Exception as e2:
            print(f"获取上证50也失败: {e2}")
            return

    print(f"共 {len(codes)} 只成分股，开始扫描...")

    results: list[dict] = []
    total = min(len(codes), top_n) if top_n > 0 else len(codes)

    for i, (code, name) in enumerate(zip(codes, names)):
        if i >= total:
            break
        symbol = str(code).strip().zfill(6)
        try:
            df = await dm.fetch_stock_history(symbol, days=60)
            if df.empty or len(df) < 30:
                continue

            signal = router.route(df, symbol, name=name)
            if signal is not None:
                results.append({
                    "symbol": symbol,
                    "name": name,
                    "direction": signal["direction"],
                    "confidence": signal["confidence"],
                    "strategies": signal["strategies"],
                })
                direction_text = {"buy": "买入", "sell": "卖出"}.get(
                    signal["direction"], signal["direction"]
                )
                print(f"  [{len(results)}] {name}({symbol}) {direction_text} "
                      f"置信度={signal['confidence']:.2f} 策略={signal['strategies']}")
        except Exception as e:
            pass  # 跳过失败的

        if (i + 1) % 20 == 0:
            print(f"  已扫描 {i+1}/{total}，发现 {len(results)} 个信号")

    print(f"\n扫描完成: {total} 只中 {len(results)} 只有信号")
    print("=" * 60)

    # 按方向分组输出
    buy_signals = [r for r in results if r["direction"] == "buy"]
    sell_signals = [r for r in results if r["direction"] == "sell"]

    if buy_signals:
        print(f"\n买入信号 ({len(buy_signals)}):")
        for r in sorted(buy_signals, key=lambda x: -x["confidence"]):
            print(f"  {r['name']}({r['symbol']}) 置信度={r['confidence']:.2f} 策略={r['strategies']}")

    if sell_signals:
        print(f"\n卖出信号 ({len(sell_signals)}):")
        for r in sorted(sell_signals, key=lambda x: -x["confidence"]):
            print(f"  {r['name']}({r['symbol']}) 置信度={r['confidence']:.2f} 策略={r['strategies']}")

    # 输出可复制的代码列表
    all_codes = [r["symbol"] for r in sorted(results, key=lambda x: -x["confidence"])]
    if all_codes:
        print(f"\n推荐添加的自选股代码:")
        print(all_codes)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    asyncio.run(scan(top_n=n))
