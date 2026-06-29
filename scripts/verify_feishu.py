"""飞书消息验证脚本。

构造一个完整信号，经过 SignalPipeline 所有过滤层 + formatter，
输出格式化后的飞书卡片 JSON，然后发送到飞书。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.analysis.position_controller import PositionController
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu.data import data_manager
from gugu.engine.signal_pipeline import SignalPipeline
from gugu.engine.signal_router import SignalRouter
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.industry_constraint import IndustryConstraint
from gugu.filters.money_flow import MoneyFlowFilter
from gugu.notifier import FeishuNotifier
from gugu.notifier.formatter import format_signal
from gugu.strategies.registry import get_enabled_strategies
from gugu.wisdom import WisdomAdvisor


async def main() -> None:
    dm = data_manager()

    # 构造完整 SignalPipeline（真实组件，非 mock）
    strategies = get_enabled_strategies()
    router = SignalRouter(strategies, fusion_rule="any", min_confidence=0.0)
    wisdom = WisdomAdvisor()
    regime = MultiPeriodRegimeDetector()
    pos_ctrl = PositionController()
    fund_filter = FundamentalFilter()
    flow_filter = MoneyFlowFilter()
    ind_constraint = IndustryConstraint()

    pipeline = SignalPipeline(
        data_manager=dm,
        signal_router=router,
        wisdom_advisor=wisdom,
        regime_detector=regime,
        position_controller=pos_ctrl,
        fundamental_filter=fund_filter,
        money_flow_filter=flow_filter,
        industry_constraint=ind_constraint,
    )

    # 选择一个信号——贵州茅台
    symbol = "600519"
    df = await dm.fetch_stock_history(symbol, days=60)
    if df.empty:
        print(f"❌ {symbol} 行情数据为空")
        return
    meta = await dm.fetch_stock_meta(symbol)
    print(f"✅ {symbol} 行情 {len(df)} 条 | meta name={meta.get('name', '?')}")

    # 构造仓位预算
    regime_result = await regime.detect()
    from gugu.execution import PaperBroker
    broker = PaperBroker()
    account = broker.get_account()
    budget = pos_ctrl.calculate(
        regime=regime_result, account=account, is_halted=False,
    )

    # 通过 SignalPipeline 获取完整信号
    signal = await pipeline.process(
        symbol=symbol,
        df=df,
        meta=meta,
        budget=budget,
        rt_all=None,
        watchlist=[symbol],
        portfolio={},
        account=account,
    )

    if signal is None:
        print(f"❌ {symbol} 未产生信号（所有策略无触发）")
        # 用 router 构造一个手动信号来验证 formatter 链路
        route_result = router.route(df, symbol, name=meta.get("name", ""))
        if route_result is None:
            print("  路由也无信号，构造手动信号")
            route_result = {
                "symbol": symbol,
                "name": meta.get("name", ""),
                "direction": "buy",
                "confidence": 0.7,
                "strategy": "manual",
                "strategies": ["manual"],
                "reason": "手动验证",
            }
        # 补全必要字段，走 wisdom 决策
        route_result["price"] = float(df.iloc[-1]["close"])
        route_result["prev_close"] = float(df.iloc[-2]["close"]) if len(df) >= 2 else route_result["price"]
        route_result["suggested_position_ratio"] = budget.single_limit
        route_result["has_position"] = False
        route_result["current_position_ratio"] = 0.0
        route_result["market_context"] = {"regime": "test", "portfolio_count": 0}
        # 手动补充板块轮动感知（模拟 SignalPipeline 步骤 2.2）
        try:
            from gugu.analysis.sector_rotation import SectorRotation as _SR
            from gugu.filters.industry_constraint import IndustryConstraint as _IC
            sr = _SR()
            sr_result = await sr.detect(top_n=5)
            ic = _IC()
            industry = ic.get_industry(symbol)
            hot = sr_result.get("hot_sectors", [])
            hot_cats = set()
            for s in hot:
                cat = sr.SW_INDUSTRY_MAP.get(s, "")
                if cat:
                    hot_cats.add(cat)
            stock_cat = sr.SW_INDUSTRY_MAP.get(industry, "")
            is_hot = industry in hot if industry else False
            is_cold = bool(not is_hot and stock_cat and stock_cat not in hot_cats) if industry else False
            route_result["sector_check"] = {
                "is_hot": is_hot,
                "is_cold": is_cold,
                "industry": industry or "",
                "hot_sectors": hot[:3],
            }
            hot_status = "hot" if is_hot else ("cold" if is_cold else "neutral")
            print(f"  ✅ 板块轮动: {industry} → {hot_status} (热点={hot[:3]})")
        except Exception as e:
            print(f"  ⚠️ 板块轮动获取失败: {e}")
            route_result["sector_check"] = {"is_hot": False, "is_cold": False, "industry": "", "hot_sectors": []}

        # 手动补多周期趋势（模拟 SignalPipeline 步骤 0.6）
        try:
            weekly = SignalPipeline._check_weekly_trend(df)
            route_result["multi_period"] = weekly
            print(f"  ✅ 多周期共振: 周线={weekly.get('weekly_trend', '?')} aligned={weekly.get('weekly_aligned', '?')}")
        except Exception as e:
            print(f"  ⚠️ 多周期趋势失败: {e}")
            route_result["multi_period"] = {"weekly_trend": "unknown", "weekly_aligned": True}

        # 手动补 decision_chain 测试 formatter 渲染
        chain = [
            {"step": 0, "name": "四阶段判断", "result": "normal_up", "passed": True},
            {"step": 0.5, "name": "危险信号检测", "result": "none", "passed": True},
            {"step": 1, "name": "基本面过滤", "result": "pass", "passed": True},
            {"step": 2, "name": "资金流过滤", "result": "pass", "passed": True},
        ]
        sc = route_result.get("sector_check", {})
        if sc.get("industry"):
            hot_status = "hot" if sc.get("is_hot") else ("cold" if sc.get("is_cold") else "neutral")
            chain.append({"step": 2.2, "name": "板块轮动感知", "result": hot_status,
                          "industry": sc.get("industry", ""), "passed": True})
        chain.extend([
            {"step": 2.5, "name": "向下摊平检查", "result": "allowed", "passed": True},
            {"step": 3, "name": "行业约束", "result": "allowed", "passed": True},
            {"step": 5, "name": "Wisdom决策", "result": "buy", "passed": True},
        ])
        route_result["decision_chain"] = chain
        enhanced = wisdom.advise(route_result)
        signal = enhanced
        print("  使用手动信号 + Wisdom 决策")
    else:
        print("✅ SignalPipeline 完整流程产生信号")
        # 补 decision_chain（pipeline 中 record_signal_history 才写，这里手动补）
        if not signal.get("decision_chain"):
            signal["decision_chain"] = [
                {"step": 0, "name": "四阶段判断", "result": signal.get("stage", {}).get("stage", "?")},
                {"step": 0.5, "name": "危险信号检测", "result": signal.get("danger_signals", {}).get("severity", "none")},
            ]
            if signal.get("fundamental"):
                signal["decision_chain"].append({"step": 1, "name": "基本面过滤", "result": "pass" if signal["fundamental"].get("pass") else "fail"})
            if signal.get("sector_check"):
                hot_str = "hot" if signal["sector_check"].get("is_hot") else ("cold" if signal["sector_check"].get("is_cold") else "neutral")
                signal["decision_chain"].append({"step": 2.2, "name": "板块轮动感知", "result": hot_str, "industry": signal["sector_check"].get("industry", "")})
            if signal.get("wisdom_decision"):
                signal["decision_chain"].append({"step": 5, "name": "Wisdom决策", "result": signal["wisdom_decision"].get("action", "?")})

    print(f"\n{'=' * 60}")
    print("信号内容:")
    print(f"  symbol:      {signal.get('symbol', '?')}")
    print(f"  name:        {signal.get('name', '?')}")
    print(f"  direction:   {signal.get('direction', '?')}")
    print(f"  confidence:  {signal.get('confidence', 0):.2f}")
    print(f"  price:       {signal.get('price', 0):.2f}")
    print(f"  strategies:  {signal.get('strategies', [])}")
    print(f"  wis_filtered:{signal.get('wisdom_filtered', False)}")
    print(f"  decision_chain: {len(signal.get('decision_chain', []))} steps")
    print(f"  sector_check:{signal.get('sector_check', {})}")
    print(f"  multi_period:{signal.get('multi_period', {}).get('weekly_trend', 'N/A')}")

    # 飞书卡片格式化
    card = format_signal(signal)
    card_content = card.get("card", {})
    header = card_content.get("header", {})
    title = header.get("title", {}).get("content", "?")
    template = header.get("template", "?")
    elements = card_content.get("elements", [])

    print(f"\n{'=' * 60}")
    print(f"飞书卡片预览:")
    print(f"  标题:   {title}")
    print(f"  模板:   {template}")
    print(f"  段落数: {len(elements)}")

    # 打印段落内容摘要
    for i, elem in enumerate(elements):
        if elem.get("tag") == "div":
            text = elem.get("text", {}).get("content", "")
            preview = text[:200].replace("\n", " | ")
            print(f"  段{i}: {preview}...")
        elif elem.get("tag") == "hr":
            print(f"  段{i}: ---")
        elif elem.get("tag") == "note":
            note = elem.get("elements", [{}])[0].get("content", "")
            print(f"  段{i}: [note] {note}")

    # 关键验证点
    print(f"\n{'=' * 60}")
    print("验证:")
    issues = []

    # 1. 名称
    if "未知" in title:
        issues.append("❌ 标题包含 '未知'")
    elif signal.get("name"):
        print(f"  ✅ 股票名称: {signal['name']} 显示正确")
    else:
        issues.append("❌ name 为空")

    # 2. decision_chain 渲染
    chain_found = any(
        "决策链路" in (e.get("text", {}).get("content", "") if e.get("tag") == "div" else "")
        for e in elements
    )
    if chain_found:
        print(f"  ✅ 决策链路已渲染")
    else:
        issues.append("❌ 决策链路未渲染")

    # 3. 价格
    price = signal.get("price", 0)
    if price > 0:
        print(f"  ✅ 价格有效: {price:.2f}")
    else:
        issues.append(f"❌ 价格异常: {price}")

    # 4. 检查板块轮动
    if signal.get("sector_check"):
        print(f"  ✅ 板块轮动感知: {signal['sector_check']}")

    # 5. 检查多周期
    if signal.get("multi_period"):
        wp = signal["multi_period"]
        print(f"  ✅ 多周期共振: 周线趋势={wp.get('weekly_trend', '?')} aligned={wp.get('weekly_aligned', '?')}")

    # 打印问题
    if issues:
        for i in issues:
            print(f"  {i}")
        print("\n⚠️  存在上述问题，请修复后再发送")
        return

    # 发送到飞书
    notifier = FeishuNotifier()
    if notifier._is_configured():
        print(f"\n🚀 发送到飞书...")
        ok = await notifier.send_card(card)
        print(f"  {'✅ 已发送成功' if ok else '❌ 发送失败'}")
        await notifier.close()
    else:
        print(f"\nℹ️  飞书未配置，卡片 JSON 已打印到控制台（不发送）")
        print("\n完整卡片 JSON:")
        print(json.dumps(card, ensure_ascii=False, indent=2)[:3000])
        print("...(截断)")


if __name__ == "__main__":
    asyncio.run(main())