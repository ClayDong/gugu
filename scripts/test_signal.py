"""体验测试：遍历自选股，用真实行情发送带 wisdom 决策的信号，并推送飞书通知。"""
import asyncio

from gugu.config import settings
from gugu.data import data_manager
from gugu.engine.signal_router import SignalRouter
from gugu.notifier import FeishuNotifier
from gugu.strategies.registry import get_enabled_strategies
from gugu.wisdom import WisdomAdvisor


async def main() -> None:
    dm = data_manager()
    advisor = WisdomAdvisor()
    notifier = FeishuNotifier()
    strategies = get_enabled_strategies()
    router = SignalRouter(strategies, fusion_rule="any", min_confidence=0.0)

    watchlist = [str(code).strip().zfill(6) for code in settings().get("watchlist", [])]
    print(f"自选股: {watchlist}")
    print(f"策略: {[s.name for s in strategies]}")
    print(f"决策模式: {'LLM' if advisor._llm_available else 'fallback'}")
    print(f"飞书通知: {'已配置' if notifier._is_configured() else '未配置（请检查 .env）'}")
    print("=" * 60)

    signal_count = 0
    notify_ok_count = 0

    for symbol in watchlist:
        print(f"\n--- {symbol} ---")
        try:
            df = await dm.fetch_stock_history(symbol, days=60)
            if df.empty:
                print("  历史数据为空，跳过")
                continue

            meta = await dm.fetch_stock_meta(symbol)
            name = meta.get("name", "")

            # 获取真实价格
            price = float(df.iloc[-1]["close"])
            prev_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else price
            try:
                rt = await dm.fetch_stock_realtime([symbol])
                if not rt.empty:
                    rt_price = float(rt.iloc[0]["price"])
                    if rt_price > 0:
                        price = rt_price
            except Exception:
                pass

            # 策略信号
            signal = router.route(df, symbol, name=name)
            if signal is None:
                print(f"  {name} ({symbol}): 无信号")
                continue

            # 补充信号上下文
            max_ratio = settings().get("risk", {}).get("max_position_ratio", 0.30)
            signal["suggested_position_ratio"] = max_ratio * 0.8
            signal["price"] = price
            signal["prev_close"] = prev_close
            signal["is_st"] = bool(meta.get("is_st", False))
            signal["is_suspended"] = bool(meta.get("is_suspended", False))
            signal["has_position"] = False
            signal["current_position_ratio"] = 0.0

            # wisdom 决策
            enhanced = advisor.advise(signal)
            decision = enhanced.get("wisdom_decision", {})

            direction_text = {"buy": "买入", "sell": "卖出", "hold": "持有"}.get(
                signal["direction"], signal["direction"]
            )
            filtered = enhanced.get("wisdom_filtered", False)

            print(f"  {name} ({symbol})")
            print(f"  当前价: {price:.2f}  昨收: {prev_close:.2f}")
            print(f"  策略信号: {direction_text} (置信度 {signal['confidence']:.2f}, 策略 {signal['strategies']})")
            print(f"  策略理由: {signal.get('reason', '')}")
            print(f"  Wisdom决策: {decision.get('action', '?').upper()}")
            print(f"  建议仓位: {enhanced.get('suggested_position_ratio', 0):.2%}")
            print(f"  止损价: {enhanced.get('stop_loss_price', 0):.2f}")
            print(f"  决策理由: {decision.get('llm_reason', decision.get('filter_reason', ''))}")
            if filtered:
                print(f"  ⚠️ 已过滤")

            # 推送飞书通知
            signal_count += 1
            notify_ok = await notifier.notify_signal(enhanced)
            if notify_ok:
                notify_ok_count += 1
                print(f"  ✅ 飞书通知已发送")
            else:
                print(f"  ❌ 飞书通知发送失败")

        except Exception as e:
            print(f"  处理失败: {e}")

    # 关闭通知器
    await notifier.close()

    print(f"\n{'=' * 60}")
    print(f"信号总数: {signal_count}, 飞书通知成功: {notify_ok_count}")


if __name__ == "__main__":
    asyncio.run(main())
