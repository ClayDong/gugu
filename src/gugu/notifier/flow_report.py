"""资金流日报 — 大盘/行业/个股资金流向飞书推送。

移植自 realtime-flow 项目，在 gugu 中提供两个时段推送：
- 08:00 盘前 → 昨日(T-1)资金流复盘
- 15:10 收盘 → 今日资金流日报

数据源（双通道冗余）：
1. akshare 库（东方财富后端，约 1-3 分钟延迟）
2. 东方财富 push API 直连 HTTP（akshare 失败时自动降级）
"""

from __future__ import annotations

from typing import Any

from gugu.notifier.flow_collector import (
    _collect_fallback_market_data,
    collect_all,
)
from gugu.notifier.flow_formatter import (
    build_afternoon_card,
    build_close_card,
    build_morning_card,
    build_unavailable_card,
)
from gugu.utils.log import get_logger

logger = get_logger()


async def run_morning_report() -> dict[str, Any]:
    """运行盘前资金流复盘（08:00）。"""
    data = await collect_all()
    market = data.get("market")
    has_valid_data = market is not None and (
        market.get("north_only", False)
        or market.get("sh_close")
        or market.get("sz_close")
        or market.get("main_net_inflow")
    )
    if not has_valid_data and not data.get("sectors") and not data.get("strong_stocks"):
        logger.warning("资金流复盘: 全部数据源不可用，跳过推送")
        card = build_unavailable_card("资金流复盘")
    else:
        card = build_morning_card(data)
    return {"card": card, "data": data}


async def run_close_report() -> dict[str, Any]:
    """运行收盘资金流日报（15:10）。"""
    data = await collect_all()
    market = data.get("market")
    has_valid_data = market is not None and (
        market.get("north_only", False)
        or market.get("sh_close")
        or market.get("sz_close")
        or market.get("main_net_inflow")
    )
    if not has_valid_data and not data.get("sectors") and not data.get("strong_stocks"):
        logger.warning("资金流日报: 全部数据源不可用，跳过推送")
        card = build_unavailable_card("资金流日报")
    else:
        card = build_close_card(data)
    return {"card": card, "data": data}


async def run_afternoon_report() -> dict[str, Any]:
    """运行盘后资金流决策报告（14:30）。"""
    data = await collect_all()
    market = data.get("market")
    has_valid_data = market is not None and (
        market.get("sh_close")
        or market.get("sz_close")
        or (
            market.get("main_net_inflow") not in (None, 0, 0.0)
            and not market.get("north_only", False)
        )
    )
    has_sector_data = bool(data.get("sectors"))
    has_stock_data = bool(data.get("strong_stocks"))

    if not has_valid_data and not has_sector_data and not has_stock_data:
        logger.warning("盘后资金流决策报告: 资金流数据源不可用，尝试使用行情数据替代...")
        try:
            fallback_data = await _collect_fallback_market_data()
            if fallback_data:
                data.update(fallback_data)
                data["data_source"] = "fallback_price"
                card = build_afternoon_card(data)
                return {"card": card, "data": data}
        except Exception as e:
            logger.error("行情数据替代分析失败: {}", e)
        card = build_unavailable_card("盘后资金流决策报告")
    else:
        data["data_source"] = "capital_flow"
        card = build_afternoon_card(data)
    return {"card": card, "data": data}


# Backward compatibility re-exports (from submodules)
from gugu.notifier.flow_collector import (  # noqa: F401
    _normalize,
    _safe_float,
    _yuan_to_yi,
    analyze_concepts,
    analyze_key_industries,
    analyze_market_overview,
    analyze_sectors,
    analyze_strong_stocks,
)
from gugu.notifier.flow_formatter import (  # noqa: F401
    _fmt,
    _fmt_flow,
    _format_decision_guidance,
    _format_key_industries_section,
    _rotation_label,
)
