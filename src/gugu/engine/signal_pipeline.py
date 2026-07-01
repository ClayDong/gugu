"""信号过滤流水线：从策略信号到 Wisdom 决策的全流程。

提取自 TradingEngine._scan_signals，形成独立可测试的 SignalPipeline 类。

过滤链顺序：
1. 基本面过滤（FundamentalFilter）
2. 资金流过滤（MoneyFlowFilter）
3. 行业约束（IndustryConstraint）
4. 市场状态仓位修正（RegimeDetector 已体现在 budget 中）
5. Wisdom 决策层（WisdomAdvisor）

P0 优化：signal_only 模式下跳过 Wisdom 决策和日内不变过滤。
P0 优化：资金流数据缺失时默认放行。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pandas as pd

from gugu import config as gugu_config
from gugu.analysis.danger_signal import DangerSignalDetector
from gugu.analysis.no_average_down import NoAverageDownChecker
from gugu.analysis.position_controller import PositionBudget, PositionController
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu.analysis.sector_rotation import SectorRotation
from gugu.detectors.divergence_detector import DivergenceDetector
from gugu.analysis.stage_detector import MarketStage, StageDetector
from gugu.data.manager import DataManager
from gugu.data.stock_names import lookup_stock_name
from gugu.engine.signal_router import SignalRouter
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.industry_constraint import IndustryConstraint
from gugu.filters.money_flow import MoneyFlowFilter
from gugu.utils.log import get_logger
from gugu.wisdom import WisdomAdvisor

logger = get_logger()

_SECTOR_CACHE_TTL = 300
_sector_cache_lock: asyncio.Lock | None = None


class SignalPipeline:
    """信号过滤流水线。

    将原始策略信号依次经过多级过滤，输出完整的决策信号字典。

    signal_only 模式优化：
    - 跳过 Wisdom 决策层（LLM 调用和 fallback 硬编码规则）
    - 日内不变过滤（基本面/资金流）仅首次执行，后续用缓存
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
        exec_mode: str = "paper",
    ) -> None:
        self._dm = data_manager
        self._router = signal_router
        self._wisdom = wisdom_advisor
        self._regime_detector = regime_detector
        self._position_controller = position_controller
        self._fundamental_filter = fundamental_filter or FundamentalFilter()
        self._money_flow_filter = money_flow_filter or MoneyFlowFilter()
        self._industry_constraint = industry_constraint or IndustryConstraint()
        self._sector_rotation = sector_rotation or SectorRotation()
        self._hot_sectors_cache: dict[str, Any] | None = None
        self._hot_sectors_ts: float = 0.0
        self._exec_mode = exec_mode
        # signal_only 缓存：日内不变数据的检查结果（key=symbol, value=检查结果）
        self._daily_cache: dict[str, dict[str, Any]] = {}
        self._daily_cache_cleared = True

        self._stage_detector = StageDetector()
        self._danger_detector = DangerSignalDetector()
        self._no_avg_down_checker = NoAverageDownChecker()
        self._divergence_detector = DivergenceDetector()

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
        try:
            signal = self._router.route(df, symbol, name=meta.get("name", ""))
            if not signal:
                return None

            signal["prev_close"] = (
                float(df.iloc[-2]["close"]) if len(df) >= 2 else None
            )
            signal["is_st"] = bool(meta.get("is_st", False))
            signal["is_suspended"] = bool(meta.get("is_suspended", False))
            raw_name = signal.get("name") or meta.get("name", "")
            if raw_name and raw_name != signal["symbol"] and raw_name != f"{signal['symbol']}":
                signal["name"] = raw_name
            else:
                signal["name"] = ""

            signal["price"] = float(df.iloc[-1]["close"])
            if rt_all is not None and not rt_all.empty:
                try:
                    row = rt_all[rt_all["symbol"] == symbol]
                    if not row.empty:
                        rt_price = float(row.iloc[0]["price"])
                        if rt_price > 0:
                            signal["price"] = rt_price
                except Exception:
                    pass

            signal["suggested_position_ratio"] = budget.single_limit
            signal["has_position"] = symbol in portfolio

            # P0 修复：信号数据合理性校验（防止错误数据发送飞书）
            price = signal.get("price", 0)
            confidence = signal.get("confidence")
            if price <= 1.0:
                logger.warning(
                    f"{symbol} 信号价格异常 price={price}，丢弃该信号。"
                    f" name={meta.get('name', '')} df_close={float(df.iloc[-1]['close']) if len(df) > 0 else 'N/A'}"
                    f" rt_available={rt_all is not None and not rt_all.empty}"
                )
                return None
            if confidence is None or confidence <= 0.0:
                logger.warning(f"{symbol} 信号置信度无效 confidence={confidence}，丢弃该信号")
                return None
            if symbol in portfolio and account.total_value > 0:
                pos = portfolio[symbol]
                signal["current_position_ratio"] = (
                    pos.quantity * pos.current_price / account.total_value
                )
            else:
                signal["current_position_ratio"] = 0.0

            signal["market_context"] = {
                "data_source": "degraded" if self._dm.is_degraded else "normal",
                "portfolio_count": len(portfolio),
                "cash_ratio": (
                    account.cash / account.total_value if account.total_value > 0 else 1.0
                ),
                "regime": "",
            }

            # 阶段判断 + 危险信号（轻量，每次执行）
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

            danger_result = self._danger_detector.detect(df, prev_close=signal.get("prev_close"))
            signal["danger_signals"] = {
                "signals": danger_result.signals,
                "severity": danger_result.severity,
                "description": danger_result.description,
                "action": danger_result.action,
            }
            if danger_result.has_signal and danger_result.severity in ("medium", "high"):
                logger.warning(
                    f"{symbol} 危险信号: {danger_result.signals}, 严重程度={danger_result.severity}"
                )
                if signal["direction"] == "buy":
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = f"危险信号: {danger_result.description}"

            # 量价背离检测（《炒股的智慧》量价背离卖出）
            # 对所有信号检测：买入遇背离应拦截（上涨乏力），卖出遇背离应增强
            try:
                div_result = self._divergence_detector.detect(df)
                signal["divergence"] = div_result
                if div_result["signals"]:
                    logger.warning(
                        f"{symbol} 量价背离检测: {', '.join(div_result['signals'])}"
                    )
                    if signal["direction"] == "buy":
                        # 量价背离说明上涨乏力，拦截买入
                        signal["wisdom_filtered"] = True
                        if not signal.get("filter_reason"):
                            signal["filter_reason"] = (
                                f"量价背离: {', '.join(div_result['signals'])}"
                            )
                    elif signal["direction"] == "sell":
                        # 量价背离增强卖出信号置信度
                        signal["confidence"] = min(
                            1.0, float(signal.get("confidence", 0.5)) + 0.15
                        )
                        if signal.get("reason"):
                            signal["reason"] += (
                                f"；量价背离({', '.join(div_result['signals'])})"
                            )
            except Exception as e:
                logger.warning(f"量价背离检测失败 {symbol}: {e}")
                signal["divergence"] = {"error": str(e)}

            if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
                try:
                    weekly_trend = self._check_weekly_trend(df)
                    signal["multi_period"] = weekly_trend
                    if not weekly_trend.get("weekly_aligned", True):
                        logger.info(
                            f"{symbol} 周线趋势 {weekly_trend.get('weekly_trend', '?')} "
                            f"与日线信号方向不一致，标记为周线背离"
                        )
                        signal["weekly_misaligned"] = True
                        if not signal.get("filter_reason"):
                            signal["filter_reason"] = (
                                f"周线趋势 {weekly_trend.get('weekly_trend', '?')}"
                                f"与买入信号方向不一致"
                            )
                except Exception as e:
                    logger.debug(f"多周期趋势判断失败: {e}")
                    signal["multi_period"] = {"error": str(e)}
            elif signal["direction"] == "sell":
                # 卖出信号也计算周线趋势（用于通知卡片展示上下文，不做过滤）
                try:
                    weekly_trend = self._check_weekly_trend(df)
                    signal["multi_period"] = weekly_trend
                except Exception as e:
                    logger.debug(f"卖出信号多周期趋势判断失败: {e}")
                    signal["multi_period"] = {"error": str(e)}

            # signal_only 模式：跳过 Wisdom 决策层（降低 LLM cost 和阻塞时间）
            if self._exec_mode == "signal_only":
                # signal_only 模式下，不对买入信号做硬过滤（让用户自行判断）
                # 但保留危险信号标记，用于通知时提醒用户
                # 买入和卖出信号都附加基本面数据，保证通知卡片数据完整
                try:
                    fund_result = self._fundamental_filter.check(symbol)
                    signal["fundamental"] = fund_result
                except Exception as e:
                    logger.debug(f"signal_only 基本面数据获取失败 {symbol}: {e}")
                logger.info(
                    f"信号(仅通知): {signal['symbol']} {signal['direction']} "
                    f"置信度 {signal['confidence']} 策略 {signal['strategies']}"
                )
                return signal

            # ===== 以下为 paper/live 模式完整过滤链 =====

            # 1. 基本面过滤（仅买入信号）
            if signal["direction"] == "buy":
                fund_result = self._fundamental_filter.check(symbol)
                signal["fundamental"] = fund_result
                if not fund_result["pass"]:
                    logger.info(f"{symbol} 基本面过滤: {', '.join(fund_result['reasons'])}")
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = f"基本面: {', '.join(fund_result['reasons'])}"

            # 2. 资金流过滤（仅买入信号，数据缺失时默认放行）
            if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
                flow_result = await self._money_flow_filter.check(symbol)
                signal["money_flow"] = flow_result
                if not flow_result["pass"]:
                    if flow_result.get("data_missing"):
                        logger.debug(f"{symbol} 资金流数据缺失，默认放行")
                    else:
                        logger.info(f"{symbol} 资金流过滤: {', '.join(flow_result['reasons'])}")
                        signal["wisdom_filtered"] = True
                        signal["filter_reason"] = f"资金流: {', '.join(flow_result['reasons'])}"

            # 2.2 板块轮动感知
            if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
                try:
                    sector_check = await self._check_sector_rotation(symbol)
                    signal["sector_check"] = sector_check
                    if sector_check.get("is_hot"):
                        logger.info(
                            f"{symbol} 所属板块 {sector_check.get('industry', '?')} 是热点板块"
                        )
                    elif sector_check.get("industry"):
                        logger.info(
                            f"{symbol} 所属板块 {sector_check.get('industry', '?')} "
                            f"{'是冷门板块' if sector_check.get('is_cold') else '非热点'}"
                        )
                except Exception as e:
                    logger.debug(f"板块轮动检查失败 {symbol}: {e}")
                    signal["sector_check"] = {"error": str(e)}

            # 2.5 向下摊平检查
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
                    lowest_price_since_position=float(getattr(pos, "lowest_price", 0) or 0),
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

            # 3. 行业分散约束
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

            # 3.5 四阶段入场过滤
            if (
                signal["direction"] == "buy"
                and not signal.get("wisdom_filtered")
                and stage_result.stage in (MarketStage.FRENZY, MarketStage.FINAL)
            ):
                logger.warning(f"{symbol} 阶段过滤: {stage_result.stage.value}，不建议入场")
                signal["wisdom_filtered"] = True
                signal["filter_reason"] = (
                    f"阶段过滤: {stage_result.description}。{stage_result.suggestion}"
                )

            # 4. 市场状态仓位修正（已在 budget.single_limit 中体现）
            # 5. Wisdom 决策层（异步不阻塞，后台运行，主路径不等待 LLM）
            asyncio.create_task(self._wisdom.advise_async(signal))
            signal["llm_pending"] = True
            # LLM 完成前，使用基本的 fallback 决策
            if "wisdom_decision" not in signal:
                signal["wisdom_decision"] = {"source": "pending_llm"}
            if "wisdom" not in signal:
                signal["wisdom"] = {}

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
        if df.empty or len(df) < 10:
            return {"weekly_trend": "unknown", "weekly_aligned": True}
        try:
            df_copy = df.copy()
            df_copy["date"] = pd.to_datetime(df_copy["date"])
            df_copy = df_copy.set_index("date")
            weekly = df_copy.resample("W").last().dropna()
            if len(weekly) < 5:
                return {"weekly_trend": "unknown", "weekly_aligned": True}
            weekly_ma5 = weekly["close"].rolling(window=5, min_periods=1).mean()
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
                "weekly_aligned": trend != "down",
            }
        except Exception:
            return {"weekly_trend": "unknown", "weekly_aligned": True}

    async def _check_sector_rotation(self, symbol: str) -> dict:
        global _sector_cache_lock
        if _sector_cache_lock is None:
            _sector_cache_lock = asyncio.Lock()
        now = time.time()
        async with _sector_cache_lock:
            if self._hot_sectors_cache is None or now - self._hot_sectors_ts > _SECTOR_CACHE_TTL:
                try:
                    result = await self._sector_rotation.detect(top_n=5)
                    self._hot_sectors_cache = result
                    self._hot_sectors_ts = now
                except Exception as e:
                    logger.debug(f"获取板块轮动数据失败: {e}")
                    return {
                        "is_hot": False,
                        "is_cold": False,
                        "industry": "",
                        "hot_sectors": [],
                        "error": str(e),
                    }

        hot_sectors = self._hot_sectors_cache.get("hot_sectors", [])
        if not hot_sectors:
            return {"is_hot": False, "is_cold": False, "industry": "", "hot_sectors": []}

        industry = self._industry_constraint.get_industry(symbol)
        if not industry:
            return {"is_hot": False, "is_cold": False, "industry": "", "hot_sectors": hot_sectors}

        is_hot = industry in hot_sectors
        is_cold = False
        hot_categories = set()
        for s in hot_sectors:
            cat = SectorRotation.get_industry_category(s)
            if cat:
                hot_categories.add(cat)
        stock_cat = SectorRotation.get_industry_category(industry)
        if not is_hot and stock_cat and stock_cat not in hot_categories:
            is_cold = True

        return {
            "is_hot": is_hot,
            "is_cold": is_cold,
            "industry": industry,
            "hot_sectors": hot_sectors[:3],
        }


# signals_history.jsonl 最大行数，超过时截断保留后半部分
_SIGNAL_HISTORY_MAX_LINES = 5000


def _rotate_jsonl(path: Path, max_lines: int = _SIGNAL_HISTORY_MAX_LINES) -> None:
    """JSONL 文件行数超过 max_lines 时截断，保留最新 max_lines//2 行。"""
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                keep = lines[-max_lines // 2 :]
                with path.open("w", encoding="utf-8") as f:
                    f.writelines(keep)
                logger.info(f"历史文件轮转: {path.name} {len(lines)}→{len(keep)} 行")
    except Exception as e:
        logger.warning(f"历史文件轮转失败: {e}")


def record_signal_history(signal: dict[str, Any], risk_result: Any, order_result: Any) -> None:
    try:
        hb_dir = gugu_config.PROJECT_ROOT / "data"
        hb_dir.mkdir(exist_ok=True)
        path = hb_dir / "signals_history.jsonl"
        _rotate_jsonl(path)

        decision_chain: list[dict] = []

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

        fund_info = signal.get("fundamental", {})
        if fund_info:
            decision_chain.append({
                "step": 1,
                "name": "基本面过滤",
                "result": "pass" if fund_info.get("pass") else "fail",
                "reasons": fund_info.get("reasons", []),
                "passed": fund_info.get("pass", True),
            })

        flow_info = signal.get("money_flow", {})
        if flow_info:
            decision_chain.append({
                "step": 2,
                "name": "资金流过滤",
                "result": "pass" if flow_info.get("pass") else "fail",
                "reasons": flow_info.get("reasons", []),
                "passed": flow_info.get("pass", True),
            })

        sector_info = signal.get("sector_check", {})
        if sector_info and "is_hot" in sector_info:
            decision_chain.append({
                "step": 2.2,
                "name": "板块轮动感知",
                "result": "hot" if sector_info.get("is_hot") else ("cold" if sector_info.get("is_cold") else "neutral"),
                "industry": sector_info.get("industry", ""),
                "passed": True,
            })

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

        ind_info = signal.get("industry_check", {})
        if ind_info:
            decision_chain.append({
                "step": 3,
                "name": "行业约束",
                "result": "allowed" if ind_info.get("allowed") else "blocked",
                "reason": ind_info.get("reason", ""),
                "passed": ind_info.get("allowed", True),
            })

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

        _sym = signal.get("symbol", "")
        _raw_name = signal.get("name", "") or ""
        if not _raw_name or _raw_name == _sym:
            _raw_name = lookup_stock_name(_sym) or _raw_name

        record = {
            "timestamp": pd.Timestamp.now().isoformat(),
            "symbol": signal.get("symbol"),
            "name": _raw_name,
            "direction": signal.get("direction"),
            "price": signal.get("price"),
            "confidence": signal.get("confidence"),
            "strategy": signal.get("strategy"),
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