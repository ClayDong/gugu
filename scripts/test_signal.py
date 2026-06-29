"""体验测试：完整决策链路验证——择时→因子→板块→评分→策略→wisdom→飞书通知。"""
import asyncio

from gugu.config import settings
from gugu.data import data_manager
from gugu.engine.signal_router import SignalRouter
from gugu.notifier import FeishuNotifier
from gugu.strategies.registry import get_enabled_strategies
from gugu.wisdom import WisdomAdvisor
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu.analysis.position_controller import PositionController
from gugu.analysis.alpha_factory import AlphaFactory
from gugu.analysis.sector_rotation import SectorRotation
from gugu.analysis.stock_ranker import StockRanker


async def main() -> None:
    import time as _time
    _start_ts = _time.time()

    def _elapsed():
        return f"({_time.time() - _start_ts:.0f}s)"

    print("初始化...")
    dm = data_manager()
    print(f"  数据管理器 ✓ {_elapsed()}")
    advisor = WisdomAdvisor()
    print(f"  智慧顾问 ✓ {_elapsed()}")
    notifier = FeishuNotifier()
    strategies = get_enabled_strategies()
    router = SignalRouter(strategies, fusion_rule="any", min_confidence=0.0)
    print(f"  策略({len(strategies)}个) ✓ {_elapsed()}")
    print(f"  LLM决策: {'已启用' if advisor._llm_available else '未启用（将使用硬编码规则）'} {_elapsed()}")
    print(f"  飞书: {'已配置' if notifier._is_configured() else '未配置，仅输出到控制台'} {_elapsed()}")

    # 分析层
    regime_detector = MultiPeriodRegimeDetector()
    position_ctrl = PositionController()
    alpha_factory = AlphaFactory()
    sector_rotation = SectorRotation()
    stock_ranker = StockRanker()

    watchlist = [str(code).strip().zfill(6) for code in settings().get("watchlist", [])]
    print(f"自选股: {len(watchlist)} 只")
    print(f"策略: {[s.name for s in strategies]}")
    print(f"决策模式: {'LLM' if advisor._llm_available else 'fallback'}")
    print(f"飞书通知: {'已配置' if notifier._is_configured() else '未配置'}")
    print("=" * 70)

    # === 1. 大盘择时 ===
    print("\n【大盘择时】")
    try:
        regime = await regime_detector.detect()
        print(f"  市场状态: {regime['regime']} (总仓位上限={regime['total_position_limit']:.0%})")
        print(f"  允许买入: {regime['buy_signal_allowed']} | 强制卖出: {regime.get('sell_signal_required', False)}")
        print(f"  理由: {regime['reason']}")
    except Exception as e:
        print(f"  择时失败: {e}")
        regime = {"regime": "sideways", "total_position_limit": 0.40,
                  "buy_signal_allowed": True, "sell_signal_required": False, "reason": "降级"}

    # === 2. 板块轮动 ===
    print("\n【板块轮动】")
    try:
        sector_result = await sector_rotation.detect(top_n=5)
        hot = sector_result.get("hot_sectors", [])
        print(f"  强势板块: {hot[:3] if hot else '获取失败'}")
        print(f"  理由: {sector_result.get('reason', '')}")
    except Exception as e:
        print(f"  板块轮动失败: {e}")
        hot = []

    # === 3. 个股评分排名 ===
    print("\n【个股评分排名 Top5】")
    rankings = []
    try:
        rankings = await asyncio.wait_for(
            stock_ranker.rank(watchlist, hot_sectors=hot, top_n=5), timeout=120.0
        )
        for i, r in enumerate(rankings):
            print(f"  #{i+1} {r['name']}({r['symbol']}) 总分={r['total_score']:.3f} "
                  f"因子={r['factor_score']:.2f} 基本面={r['fundamental_score']:.2f} "
                  f"资金流={r['money_flow_score']:.2f} 板块={r['sector_score']:.2f} "
                  f"PE={r.get('pe', '?')}")
    except asyncio.TimeoutError:
        print("  评分超时，跳过")
    except Exception as e:
        print(f"  评分失败: {e}")

    # === 4. 仓位预算 ===
    print("\n【仓位预算】")
    from gugu.execution import PaperBroker
    broker = PaperBroker()
    account = broker.get_account()
    budget = position_ctrl.calculate(
        regime=regime, account=account, is_halted=False, total_pnl_pct=0.0
    )
    print(f"  总仓位上限: {budget.total_limit:.0%}")
    print(f"  单股仓位上限: {budget.single_limit:.0%}")
    print(f"  可用资金: ¥{budget.available_budget:,.0f}")
    print(f"  剩余仓位槽: {budget.max_positions}")
    print(f"  理由: {budget.reason}")

    # === 5. 策略信号扫描 ===
    print(f"\n{'=' * 70}")
    print("【策略信号扫描 + Wisdom决策】")
    print("=" * 70)

    signal_count = 0
    buy_count = 0
    sell_count = 0
    filter_count = 0
    notify_ok_count = 0

    for symbol in watchlist:
        try:
            df = await dm.fetch_stock_history(symbol, days=60)
            if df.empty:
                continue

            meta = await dm.fetch_stock_meta(symbol)
            name = meta.get("name", "")

            # 真实价格
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
                continue

            # Alpha因子增强
            factors = alpha_factory.compute_all(df)
            factor_score = alpha_factory.composite_score(factors)
            signal["alpha_score"] = factor_score["score"]
            signal["alpha_buy"] = factor_score["buy_signal"]
            signal["alpha_top_factors"] = factor_score["top_factors"][:3]

            # 补充信号上下文
            signal["suggested_position_ratio"] = budget.single_limit
            signal["price"] = price
            signal["prev_close"] = prev_close
            signal["is_st"] = bool(meta.get("is_st", False))
            signal["is_suspended"] = bool(meta.get("is_suspended", False))
            signal["has_position"] = symbol in broker.get_portfolio()
            signal["current_position_ratio"] = 0.0
            signal["market_context"] = {
                "regime": regime["regime"],
                "total_position_limit": regime["total_position_limit"],
                "budget_single_limit": budget.single_limit,
            }

            # Wisdom决策
            enhanced = advisor.advise(signal)
            decision = enhanced.get("wisdom_decision", {})
            action = decision.get("action", "?")

            direction_text = {"buy": "买入", "sell": "卖出", "hold": "持有"}.get(
                signal["direction"], signal["direction"]
            )
            filtered = enhanced.get("wisdom_filtered", False)

            # 仓位显示逻辑修正
            if action in ("sell",):
                position_display = "清仓"
            elif action == "filter":
                position_display = "0%"
            else:
                position_display = f"{enhanced.get('suggested_position_ratio', 0):.0%}"

            print(f"\n  {name} ({symbol})")
            print(f"  当前价: {price:.2f}  昨收: {prev_close:.2f}")
            print(f"  策略信号: {direction_text} (置信度={signal['confidence']:.2f}, 策略={signal['strategies']})")
            print(f"  Alpha因子: 综合评分={factor_score['score']:.3f} Top={[(f[0], f[1]) for f in factor_score['top_factors'][:3]]}")
            print(f"  Wisdom决策: {action.upper()}")
            print(f"  建议仓位: {position_display}")
            if enhanced.get("stop_loss_price", 0) > 0:
                print(f"  止损价: {enhanced['stop_loss_price']:.2f} ({(enhanced['stop_loss_price']/price-1)*100:.1f}%)")
            print(f"  决策理由: {decision.get('llm_reason', decision.get('filter_reason', ''))}")
            if filtered:
                print(f"  [已过滤]")

            signal_count += 1
            if action == "buy":
                buy_count += 1
            elif action == "sell":
                sell_count += 1
            elif action == "filter":
                filter_count += 1

            # 推送飞书通知
            notify_ok = await notifier.notify_signal(enhanced)
            if notify_ok:
                notify_ok_count += 1
                print(f"  -> 飞书通知已发送")

        except Exception as e:
            print(f"  {symbol} 处理失败: {e}")

    await notifier.close()

    # === 汇总 ===
    print(f"\n{'=' * 70}")
    print(f"【扫描汇总】")
    print(f"  扫描股票: {len(watchlist)} 只")
    print(f"  信号触发: {signal_count} 个 (买入={buy_count} 卖出={sell_count} 过滤={filter_count})")
    print(f"  飞书通知: {notify_ok_count}/{signal_count} 成功")
    print(f"  市场状态: {regime['regime']} (仓位上限={regime['total_position_limit']:.0%})")
    print(f"  强势板块: {hot[:3] if hot else 'N/A'}")
    if rankings:
        print(f"  推荐关注: {rankings[0]['name']}({rankings[0]['symbol']}) 评分={rankings[0]['total_score']:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
