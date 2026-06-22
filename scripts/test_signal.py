"""体验测试：发送带 wisdom 决策的真实信号到飞书。"""
import asyncio

from gugu.notifier import FeishuNotifier
from gugu.wisdom import WisdomAdvisor


async def main() -> None:
    advisor = WisdomAdvisor()
    signal = {
        "symbol": "600519",
        "name": "贵州茅台",
        "direction": "buy",
        "strategy": "turtle",
        "reason": "突破20日高点，成交量放大",
        "suggested_position_ratio": 0.24,
        "price": 1500.0,
        "confidence": 0.85,
    }
    enhanced = advisor.advise(signal)
    print(f"决策: {enhanced.get('wisdom_decision', {})}")
    print(f"调整后仓位: {enhanced.get('suggested_position_ratio', 0):.2%}")
    print(f"止损价: {enhanced.get('stop_loss_price', 0):.2f}")

    notifier = FeishuNotifier()
    ok = await notifier.notify_signal(enhanced)
    print(f"信号发送: {ok}")
    await notifier.close()


if __name__ == "__main__":
    asyncio.run(main())
