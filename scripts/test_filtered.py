"""体验测试：发送低置信度信号（应被入场过滤）。"""
import asyncio

from gugu.notifier import FeishuNotifier
from gugu.wisdom import WisdomAdvisor


async def main() -> None:
    advisor = WisdomAdvisor()
    signal = {
        "symbol": "000858",
        "name": "五粮液",
        "direction": "buy",
        "strategy": "rsi",
        "reason": "RSI 超卖反弹",
        "suggested_position_ratio": 0.15,
        "price": 180.0,
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
