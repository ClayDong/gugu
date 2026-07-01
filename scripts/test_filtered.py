"""体验测试：发送低置信度信号（应被入场过滤）。"""
import asyncio

from gugu.data import data_manager
from gugu.notifier import FeishuNotifier
from gugu.wisdom import WisdomAdvisor


async def main() -> None:
    advisor = WisdomAdvisor()

    # 从真实行情获取五粮液最新收盘价，不使用硬编码价格
    dm = data_manager()
    symbol = "000858"
    df = await dm.fetch_stock_history(symbol, days=5)
    if df.empty:
        print(f"❌ 无法获取 {symbol} 行情数据")
        return
    real_price = float(df.iloc[-1]["close"])
    print(f"✅ {symbol} 真实收盘价: {real_price:.2f}")

    signal = {
        "symbol": symbol,
        "name": "五粮液",
        "direction": "buy",
        "strategy": "rsi",
        "reason": "RSI 超卖反弹",
        "suggested_position_ratio": 0.15,
        "price": real_price,
        "confidence": 0.4,  # 低置信度，应被过滤
    }
    enhanced = advisor.advise(signal)
    print(f"决策: {enhanced.get('wisdom_decision', {})}")
    print(f"入场过滤: {enhanced.get('wisdom_filtered', False)}")

    notifier = FeishuNotifier()
    ok = await notifier.notify_signal(enhanced)
    print(f"信号发送: {ok}")
    await notifier.close()


if __name__ == "__main__":
    asyncio.run(main())
