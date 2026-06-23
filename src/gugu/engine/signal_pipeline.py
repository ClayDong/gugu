"""信号过滤流水线：从策略信号到 Wisdom 决策的全流程。

提取自 TradingEngine._scan_signals，形成独立可测试的 SignalPipeline 类。

过滤链顺序（不可变）：
1. 基本面过滤（FundamentalFilter）
2. 资金流过滤（MoneyFlowFilter）
3. 行业约束（IndustryConstraint）
4. 市场状态仓位修正（RegimeDetector 已体现在 budget 中）
5. Wisdom 决策层（WisdomAdvisor）
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from gugu.analysis.position_controller import PositionBudget, PositionController
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu import config as gugu_config
from gugu.data.manager import DataManager
from gugu.engine.signal_router import SignalRouter
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.industry_constraint import IndustryConstraint
from gugu.filters.money_flow import MoneyFlowFilter
from gugu.utils.log import get_logger
from gugu.wisdom import WisdomAdvisor

logger = get_logger()


class SignalPipeline:
    """信号过滤流水线。

    将原始策略信号依次经过基本面、资金流、行业、市场状态、Wisdom 五层过滤，
    输出完整的决策信号字典。
    """

    def __init__(
        self,
        data_manager: DataManager,
        signal_router: SignalRouter,
        wisdom_advisor: WisdomAdvisor,
        regime_detector: MultiPeriodRegimeDetector,
        position_controller: PositionController,
        fundamental_filter: FundamentalFilter | None = None,
        money_flow_filter: MoneyFlowFilter | None = None,
        industry_constraint: IndustryConstraint | None = None,
    ) -> None:
        """初始化信号过滤流水线。

        Args:
            data_manager: 数据管理器，用于获取行情和元数据
            signal_router: 信号路由器，将行情数据转化为策略信号
            wisdom_advisor: Wisdom 决策层，LLM 或硬编码规则增强
            regime_detector: 多周期市场择时器
            position_controller: 仓位控制器，计算总/单股仓位上限
            fundamental_filter: 基本面过滤器，默认创建新实例
            money_flow_filter: 资金流过滤器，默认创建新实例
            industry_constraint: 行业分散约束器，默认创建新实例
        """
        self._dm = data_manager
        self._router = signal_router
        self._wisdom = wisdom_advisor
        self._regime_detector = regime_detector
        self._position_controller = position_controller
        self._fundamental_filter = fundamental_filter or FundamentalFilter()
        self._money_flow_filter = money_flow_filter or MoneyFlowFilter()
        self._industry_constraint = industry_constraint or IndustryConstraint()

    async def process(
        self,
        symbol: str,
        df: pd.DataFrame,
        meta: dict[str, Any],
        budget: PositionBudget,
        rt_all: pd.DataFrame | None,
        watchlist: list[str],
        portfolio: dict[str, Any],
        account: Any,
    ) -> dict | None:
        """对单个股票执行完整过滤链，返回决策信号或 None。

        Args:
            symbol: 股票代码（6 位字符串）
            df: 历史行情 DataFrame，含 'close' 列
            meta: 元数据字典（name, is_st, is_suspended 等）
            budget: 仓位预算（含 single_limit 等）
            rt_all: 批量实时行情 DataFrame，可 None
            watchlist: 自选股列表（用于查找实时价）
            portfolio: 当前持仓字典 {symbol: Position}
            account: 账户信息（含 total_value, cash 等）

        Returns:
            完整信号 dict，若过滤链丢弃则返回 None
        """
        try:
            signal = self._router.route(df, symbol, name=meta.get("name", ""))
            if not signal:
                return None

            # L3 元数据注入
            signal["prev_close"] = (
                float(df.iloc[-2]["close"])
                if len(df) >= 2
                else float(df.iloc[-1]["close"])
            )
            signal["is_st"] = bool(meta.get("is_st", False))
            signal["is_suspended"] = bool(meta.get("is_suspended", False))
            signal["name"] = signal.get("name") or meta.get("name", "")

            # 优先使用实时价，历史收盘价作为 fallback
            signal["price"] = float(df.iloc[-1]["close"])
            if rt_all is not None and not rt_all.empty:
                try:
                    row = rt_all[rt_all["symbol"] == symbol]
                    if not row.empty:
                        rt_price = float(row.iloc[0]["price"])
                        if rt_price > 0:
                            signal["price"] = rt_price
                except Exception:
                    pass  # 实时价解析失败，使用历史收盘价

            # 仓位预算
            signal["suggested_position_ratio"] = budget.single_limit

            # 持仓状态
            signal["has_position"] = symbol in portfolio
            if symbol in portfolio and account.total_value > 0:
                pos = portfolio[symbol]
                signal["current_position_ratio"] = (
                    pos.quantity * pos.current_price / account.total_value
                )
            else:
                signal["current_position_ratio"] = 0.0

            # 市场环境上下文
            signal["market_context"] = {
                "data_source": "degraded" if self._dm.is_degraded else "normal",
                "portfolio_count": len(portfolio),
                "cash_ratio": (
                    account.cash / account.total_value
                    if account.total_value > 0
                    else 1.0
                ),
                "regime": "",  # 由调用方填充，此处留空
            }

            # ===== 过滤链 =====

            # 1. 基本面过滤（仅买入信号）
            if signal["direction"] == "buy":
                fund_result = self._fundamental_filter.check(symbol)
                signal["fundamental"] = fund_result
                if not fund_result["pass"]:
                    logger.info(
                        f"{symbol} 基本面过滤: {', '.join(fund_result['reasons'])}"
                    )
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = (
                        f"基本面: {', '.join(fund_result['reasons'])}"
                    )

            # 2. 资金流过滤（仅买入信号）
            if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
                flow_result = await self._money_flow_filter.check(symbol)
                signal["money_flow"] = flow_result
                if not flow_result["pass"]:
                    logger.info(
                        f"{symbol} 资金流过滤: {', '.join(flow_result['reasons'])}"
                    )
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = (
                        f"资金流: {', '.join(flow_result['reasons'])}"
                    )

            # 3. 行业分散约束（仅买入信号 + 新建仓位）
            if (
                signal["direction"] == "buy"
                and not signal.get("wisdom_filtered")
                and not signal.get("has_position")
            ):
                ind_result = self._industry_constraint.check_buy(
                    symbol,
                    portfolio,
                    industry=signal.get("fundamental", {}).get("industry", ""),
                )
                signal["industry_check"] = ind_result
                if not ind_result["allowed"]:
                    logger.info(f"{symbol} 行业约束过滤: {ind_result['reason']}")
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = f"行业约束: {ind_result['reason']}"

            # 4. 市场状态仓位修正（已在 budget.single_limit 中体现，此处仅记录）
            # 无需额外动作，调用方负责传入正确的 budget

            # 5. 决策层增强（可能调整仓位比例、预设止损、过滤入场）
            signal = self._wisdom.advise(signal)

            logger.info(
                f"信号: {signal['symbol']} {signal['direction']} "
                f"置信度 {signal['confidence']} 策略 {signal['strategies']}"
            )
            return signal

        except Exception as e:
            logger.error(f"扫描 {symbol} 失败: {e}")
            return None


def record_signal_history(
    signal: dict[str, Any], risk_result: Any, order_result: Any
) -> None:
    """将信号、风控结果、下单结果持久化到 signals_history.jsonl。

    BIZ-01 修复。每行一个 JSON，含完整决策链路，便于回溯分析。

    Args:
        signal: 完整决策信号字典
        risk_result: RiskManager.check_order 返回值（含 allowed, message 等）
        order_result: Broker.order 返回值（含 success, price, quantity 等）
    """
    try:
        hb_dir = gugu_config.PROJECT_ROOT / "data"
        hb_dir.mkdir(exist_ok=True)
        path = hb_dir / "signals_history.jsonl"
        record = {
            "timestamp": pd.Timestamp.now().isoformat(),
            "symbol": signal.get("symbol"),
            "direction": signal.get("direction"),
            "price": signal.get("price"),
            "confidence": signal.get("confidence"),
            "strategies": signal.get("strategies"),
            "wisdom_filtered": signal.get("wisdom_filtered", False),
            "wisdom_decision": signal.get("wisdom_decision", {}),
            "suggested_position_ratio": signal.get("suggested_position_ratio"),
            "risk_allowed": getattr(risk_result, "allowed", None),
            "risk_message": getattr(risk_result, "message", ""),
            "order_success": getattr(order_result, "success", None),
            "order_quantity": getattr(order_result, "quantity", 0),
            "order_price": getattr(order_result, "price", 0),
            "order_commission": getattr(order_result, "commission", 0),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning(f"记录信号历史失败: {e}")