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
from gugu.analysis.sector_rotation import SectorRotation
from gugu.analysis.stage_detector import StageDetector, MarketStage
from gugu.analysis.danger_signal import DangerSignalDetector
from gugu.analysis.no_average_down import NoAverageDownChecker
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
        sector_rotation: SectorRotation | None = None,
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
            sector_rotation: 板块轮动检测器，默认创建新实例
        """
        self._dm = data_manager
        self._router = signal_router
        self._wisdom = wisdom_advisor
        self._regime_detector = regime_detector
        self._position_controller = position_controller
        self._fundamental_filter = fundamental_filter or FundamentalFilter()
        self._money_flow_filter = money_flow_filter or MoneyFlowFilter()
        self._industry_constraint = industry_constraint or IndustryConstraint()
        self._sector_rotation = sector_rotation or SectorRotation()
        self._hot_sectors_cache: dict[str, Any] | None = None  # 当日热板块缓存

        # P0 新增：四阶段判断器、危险信号检测器、向下摊平检查器
        self._stage_detector = StageDetector()
        self._danger_detector = DangerSignalDetector()
        self._no_avg_down_checker = NoAverageDownChecker()

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
            # A-04 修复：确保 name 是有效的中文名而非代码
            raw_name = signal.get("name") or meta.get("name", "")
            if raw_name and raw_name != signal["symbol"] and raw_name != f"{signal['symbol']}":
                signal["name"] = raw_name
            else:
                signal["name"] = ""

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

            # 0. 四阶段判断（新增：入场前判断股票所处阶段）
            stage_result = self._stage_detector.detect(df)
            signal["stage"] = {
                "stage": stage_result.stage.value,
                "confidence": stage_result.confidence,
                "description": stage_result.description,
                "suggestion": stage_result.suggestion,
            }
            logger.info(
                f"{symbol} 阶段判断: {stage_result.stage.value} "
                f"(置信度 {stage_result.confidence:.2f}) — {stage_result.suggestion}"
            )

            # 0.5 危险信号检测（新增：检测量增价不涨/两天转头/坏消息）
            danger_result = self._danger_detector.detect(
                df, prev_close=signal.get("prev_close")
            )
            signal["danger_signals"] = {
                "signals": danger_result.signals,
                "severity": danger_result.severity,
                "description": danger_result.description,
                "action": danger_result.action,
            }
            if danger_result.has_signal and danger_result.severity in ("high", "medium"):
                logger.warning(
                    f"{symbol} 危险信号: {danger_result.signals}, "
                    f"严重程度={danger_result.severity}"
                )
                if signal["direction"] == "buy":
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = (
                        f"危险信号: {danger_result.description}"
                    )

            # 0.6 多周期趋势确认（仅买入信号，A-07 修复）
            # 日线信号 + 周线趋势向上 + 月线非下降 = 强入场信号
            # 周线趋势向下时：降低置信度但不过滤（让 wisdom 做最终决定）
            if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
                try:
                    weekly_trend = self._check_weekly_trend(df)
                    signal["multi_period"] = weekly_trend
                    if not weekly_trend.get("weekly_aligned", True):
                        logger.info(
                            f"{symbol} 周线趋势 {weekly_trend.get('weekly_trend', '?')} "
                            f"与日线信号方向不一致，标记为周线背离"
                        )
                        # 周线背离不作为硬过滤，仅降低置信度供 wisdom 参考
                        signal["weekly_misaligned"] = True
                        if not signal.get("filter_reason"):
                            signal["filter_reason"] = (
                                f"周线趋势 {weekly_trend.get('weekly_trend', '?')}"
                                f"与买入信号方向不一致"
                            )
                except Exception as e:
                    logger.debug(f"多周期趋势判断失败: {e}")
                    signal["multi_period"] = {"error": str(e)}

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

            # 2.2 板块轮动感知（仅买入信号，A-09 修复）
            # 热点板块内的个股获得置信度加成，冷门板块的个股被标记
            # 不过滤，仅作信息标记给 wisdom 决策参考
            if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
                try:
                    sector_check = await self._check_sector_rotation(symbol)
                    signal["sector_check"] = sector_check
                    if sector_check.get("is_hot"):
                        logger.info(f"{symbol} 所属板块 {sector_check.get('industry', '?')} 是热点板块")
                    elif sector_check.get("industry"):
                        logger.info(
                            f"{symbol} 所属板块 {sector_check.get('industry', '?')} "
                            f"{'是冷门板块' if sector_check.get('is_cold') else '非热点'}"
                        )
                except Exception as e:
                    logger.debug(f"板块轮动检查失败 {symbol}: {e}")
                    signal["sector_check"] = {"error": str(e)}

            # 2.5 向下摊平检查（新增：禁止在亏损仓位加码）
            if (
                signal["direction"] == "buy"
                and not signal.get("wisdom_filtered")
                and signal.get("has_position")
            ):
                pos = portfolio[symbol]
                avg_down_result = self._no_avg_down_checker.check(
                    symbol=symbol,
                    has_position=True,
                    cost_price=getattr(pos, "avg_cost", 0) or getattr(pos, "cost_price", 0),
                    current_price=signal.get("price", 0),
                    quantity=getattr(pos, "quantity", 0),
                )
                signal["avg_down_check"] = {
                    "allowed": avg_down_result.allowed,
                    "reason": avg_down_result.reason,
                    "profit_pct": avg_down_result.profit_pct,
                }
                if not avg_down_result.allowed:
                    logger.warning(f"{symbol} 向下摊平拦截: {avg_down_result.reason}")
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = f"向下摊平: {avg_down_result.reason}"

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

            # 3.5 四阶段入场过滤（新增：牛皮市不入场，疯狂阶段不追高）
            if (
                signal["direction"] == "buy"
                and not signal.get("wisdom_filtered")
                and stage_result.stage in (MarketStage.FRENZY, MarketStage.FINAL)
            ):
                logger.warning(
                    f"{symbol} 阶段过滤: {stage_result.stage.value}，不建议入场"
                )
                signal["wisdom_filtered"] = True
                signal["filter_reason"] = (
                    f"阶段过滤: {stage_result.description}。"
                    f"{stage_result.suggestion}"
                )

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

    @staticmethod
    def _check_weekly_trend(df: pd.DataFrame) -> dict:
        """检查周线趋势是否与日线方向一致（多周期共振）。

        对日线数据按周重采样，计算周线 MA5 方向。
        周线 MA 上升 + 日线买入信号 = 强信号（多周期共振）
        周线 MA 下降 + 日线买入信号 = 弱信号（周线背离）

        Args:
            df: 日线 DataFrame（含 close 列）

        Returns:
            dict: 周线趋势分析结果
        """
        if df.empty or len(df) < 10:
            return {"weekly_trend": "unknown", "weekly_aligned": True}

        try:
            # 按周重采样计算周收盘价
            df_copy = df.copy()
            df_copy["date"] = pd.to_datetime(df_copy["date"])
            df_copy = df_copy.set_index("date")
            weekly = df_copy.resample("W").last().dropna()

            if len(weekly) < 5:
                return {"weekly_trend": "unknown", "weekly_aligned": True}

            # 周线 MA5
            weekly_ma5 = weekly["close"].rolling(window=5, min_periods=1).mean()

            # 周线趋势：最近 3 根 MA5 是否上升
            recent = weekly_ma5.iloc[-3:].values
            if len(recent) >= 3:
                weekly_slope = (recent[-1] - recent[0]) / recent[0]
                if weekly_slope > 0.01:
                    trend = "up"
                elif weekly_slope < -0.01:
                    trend = "down"
                else:
                    trend = "sideways"
            else:
                trend = "unknown"

            return {
                "weekly_trend": trend,
                "weekly_slope": round(float(weekly_slope), 4) if len(recent) >= 3 else 0,
                "weekly_close": round(float(weekly["close"].iloc[-1]), 2) if len(weekly) > 0 else 0,
                "weekly_ma5": round(float(weekly_ma5.iloc[-1]), 2) if len(weekly_ma5) > 0 else 0,
                "weekly_aligned": trend != "down",  # 周线向下视为背离
            }
        except Exception:
            return {"weekly_trend": "unknown", "weekly_aligned": True}

    async def _check_sector_rotation(self, symbol: str) -> dict:
        """检查股票所属板块是否为当前热点。

        懒加载 SectorRotation 并缓存当日结果。
        通过 IndustryConstraint 获取股票行业，判断是否属于热点板块。

        Args:
            symbol: 股票代码

        Returns:
            dict: {is_hot, is_cold, industry, hot_sectors}
        """
        # 懒加载热板块数据（每日缓存）
        if self._hot_sectors_cache is None:
            try:
                result = await self._sector_rotation.detect(top_n=5)
                self._hot_sectors_cache = result
            except Exception as e:
                logger.debug(f"获取板块轮动数据失败: {e}")
                return {"is_hot": False, "is_cold": False, "industry": "", "hot_sectors": [], "error": str(e)}

        hot_sectors = self._hot_sectors_cache.get("hot_sectors", [])
        if not hot_sectors:
            return {"is_hot": False, "is_cold": False, "industry": "", "hot_sectors": []}

        # 获取股票所属行业
        industry = self._industry_constraint.get_industry(symbol)
        if not industry:
            return {"is_hot": False, "is_cold": False, "industry": "", "hot_sectors": hot_sectors}

        is_hot = industry in hot_sectors
        # 冷门板块：在板块热度排名后 1/3 的视为冷门（简化：不在 top5 且不在大类中）
        is_cold = False
        hot_categories = set()
        for s in hot_sectors:
            cat = self._sector_rotation.SW_INDUSTRY_MAP.get(s, "")
            if cat:
                hot_categories.add(cat)
        stock_cat = self._sector_rotation.SW_INDUSTRY_MAP.get(industry, "")
        if not is_hot and stock_cat and stock_cat not in hot_categories:
            is_cold = True

        return {
            "is_hot": is_hot,
            "is_cold": is_cold,
            "industry": industry,
            "hot_sectors": hot_sectors[:3],
        }


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

        # 构建决策链路（每一步的通过/过滤/原因）
        decision_chain: list[dict] = []

        # 步骤0: 四阶段判断
        stage_info = signal.get("stage", {})
        if stage_info:
            decision_chain.append({
                "step": 0,
                "name": "四阶段判断",
                "result": stage_info.get("stage", "unknown"),
                "confidence": stage_info.get("confidence", 0),
                "description": stage_info.get("description", ""),
                "passed": True,
            })

        # 步骤0.5: 危险信号检测
        danger_info = signal.get("danger_signals", {})
        if danger_info:
            severity = danger_info.get("severity", "none")
            decision_chain.append({
                "step": 0.5,
                "name": "危险信号检测",
                "result": severity,
                "signals": danger_info.get("signals", []),
                "description": danger_info.get("description", ""),
                "passed": severity not in ("medium", "high"),
            })

        # 步骤1: 基本面过滤
        fund_info = signal.get("fundamental", {})
        if fund_info:
            decision_chain.append({
                "step": 1,
                "name": "基本面过滤",
                "result": "pass" if fund_info.get("pass") else "fail",
                "reasons": fund_info.get("reasons", []),
                "passed": fund_info.get("pass", True),
            })

        # 步骤2: 资金流过滤
        flow_info = signal.get("money_flow", {})
        if flow_info:
            decision_chain.append({
                "step": 2,
                "name": "资金流过滤",
                "result": "pass" if flow_info.get("pass") else "fail",
                "reasons": flow_info.get("reasons", []),
                "passed": flow_info.get("pass", True),
            })

        # 步骤2.2: 板块轮动感知
        sector_info = signal.get("sector_check", {})
        if sector_info and "is_hot" in sector_info:
            decision_chain.append({
                "step": 2.2,
                "name": "板块轮动感知",
                "result": "hot" if sector_info.get("is_hot") else ("cold" if sector_info.get("is_cold") else "neutral"),
                "industry": sector_info.get("industry", ""),
                "passed": True,  # 板块不过滤，仅参考
            })

        # 步骤2.5: 向下摊平检查
        avg_down_info = signal.get("avg_down_check", {})
        if avg_down_info:
            decision_chain.append({
                "step": 2.5,
                "name": "向下摊平检查",
                "result": "allowed" if avg_down_info.get("allowed") else "blocked",
                "reason": avg_down_info.get("reason", ""),
                "profit_pct": avg_down_info.get("profit_pct", 0),
                "passed": avg_down_info.get("allowed", True),
            })

        # 步骤3: 行业约束
        ind_info = signal.get("industry_check", {})
        if ind_info:
            decision_chain.append({
                "step": 3,
                "name": "行业约束",
                "result": "allowed" if ind_info.get("allowed") else "blocked",
                "reason": ind_info.get("reason", ""),
                "passed": ind_info.get("allowed", True),
            })

        # 步骤5: Wisdom决策
        wisdom_dec = signal.get("wisdom_decision", {})
        if wisdom_dec:
            decision_chain.append({
                "step": 5,
                "name": "Wisdom决策",
                "result": wisdom_dec.get("action", ""),
                "position_ratio": wisdom_dec.get("position_ratio", 0),
                "stop_loss_price": wisdom_dec.get("stop_loss_price", 0),
                "reason": wisdom_dec.get("reason", ""),
                "passed": not signal.get("wisdom_filtered", False),
            })

        # 信号 name fallback（防止信号无 name 时的脏数据）
        _sym = signal.get("symbol", "")
        _raw_name = signal.get("name", "") or ""
        if not _raw_name or _raw_name == _sym:
            _local_names = {
                "600519": "贵州茅台", "300750": "宁德时代", "000858": "五粮液",
                "601318": "中国平安", "000333": "美的集团", "603259": "药明康德",
                "600600": "青岛啤酒", "002625": "光启技术", "600674": "川投能源",
            }
            _raw_name = _local_names.get(_sym, _raw_name)

        record = {
            "timestamp": pd.Timestamp.now().isoformat(),
            "symbol": signal.get("symbol"),
            "name": _raw_name,
            "direction": signal.get("direction"),
            "price": signal.get("price"),
            "confidence": signal.get("confidence"),
            "strategy": signal.get("strategy"),  # 字符串形式，如 "turtle"
            "strategies": signal.get("strategies"),
            "wisdom_filtered": signal.get("wisdom_filtered", False),
            "filter_reason": signal.get("filter_reason", ""),
            "wisdom_decision": wisdom_dec,
            "suggested_position_ratio": signal.get("suggested_position_ratio"),
            "stop_loss_price": signal.get("stop_loss_price"),
            "stage": stage_info,
            "danger_signals": danger_info,
            "decision_chain": decision_chain,
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