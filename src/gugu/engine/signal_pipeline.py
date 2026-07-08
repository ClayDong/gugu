"""信号过滤流水线：从策略信号到 Wisdom 决策的全流程。

提取自 TradingEngine._scan_signals，形成独立可测试的 SignalPipeline 类。

过滤链顺序（实际执行顺序）：
  [步骤0]   四阶段判断（StageDetector，牛皮/升势/疯狂/最后）
  [步骤0.5] 危险信号检测（DangerSignalDetector，仅买入，medium+ 过滤）
  [步骤0.7] 量价背离检测（DivergenceDetector，仅买入，严重背离过滤）
  [步骤1]   基本面过滤（FundamentalFilter，仅买入）
  [步骤2]   资金流过滤（MoneyFlowFilter，仅买入）
  [步骤2.5] 板块轮动检查（SectorRotation，仅买入，热点/冷门板块调整）
  [步骤3]   向下摊平检查（NoAverageDownChecker，仅买入）
  [步骤3.5] 行业约束（IndustryConstraint，仅买入 + 新建仓位）
  [步骤4]   四阶段入场过滤（StageDetector，疯狂/最后阶段不入场）
  [步骤5]   市场状态仓位修正（RegimeDetector，已体现在 budget 中）
  [步骤6]   Wisdom 决策层（WisdomAdvisor，LLM + 15s 超时 fallback）

P0 优化：signal_only 模式下跳过 Wisdom 决策和日内不变过滤。
P0 优化：资金流数据缺失时默认放行。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd

from gugu import config as gugu_config
from gugu.analysis.danger_signal import DangerSignalDetector
from gugu.analysis.no_average_down import NoAverageDownChecker
from gugu.analysis.perspectives.engine import PerspectiveEngine
from gugu.analysis.perspectives.integrators import (
    attach_perspective_to_signal,
    build_context_from_signal,
)
from gugu.analysis.position_controller import PositionBudget, PositionController
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu.analysis.sector_rotation import SectorRotation
from gugu.analysis.stage_detector import MarketStage, StageDetector
from gugu.data.manager import DataManager
from gugu.data.stock_names import lookup_stock_name
from gugu.detectors.divergence_detector import DivergenceDetector
from gugu.engine.signal_router import SignalRouter
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.holder_concentration import HolderConcentrationFilter
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
        # v0.5.0 筹码集中度过滤器（季度慢变量）
        self._holder_concentration_filter: HolderConcentrationFilter | None = None
        try:
            cfg_hc = gugu_config.get_config()
            # AppConfig 有 holder_concentration_filter 字段时才启用
            if hasattr(cfg_hc, "holder_concentration_filter"):
                hc_cfg = cfg_hc.holder_concentration_filter
                if hc_cfg.enabled:
                    self._holder_concentration_filter = HolderConcentrationFilter()
                    logger.info(
                        "筹码集中度过滤器已启用（季度阈值 {:.1f}%）",
                        hc_cfg.quarterly_change_pct_threshold,
                    )
        except Exception as e:
            logger.debug(f"筹码过滤器初始化跳过: {e}")
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

        # 十大视角过滤引擎（可选，由配置控制）
        self._perspective_engine: PerspectiveEngine | None = None
        self._scan_with_perspectives = False
        self._perspective_min_composite = 6.0
        self._perspective_min_single = 4.0
        try:
            cfg = gugu_config.get_config()
            if cfg.perspective.enabled and cfg.perspective.scan_with_perspectives:
                self._perspective_engine = PerspectiveEngine()
                self._scan_with_perspectives = True
                self._perspective_min_composite = cfg.perspective.pass_threshold
                self._perspective_min_single = cfg.perspective.min_perspective_score
                logger.info(
                    "十大视角过滤已启用 (综合≥%.1f, 单项≥%.1f)",
                    self._perspective_min_composite,
                    self._perspective_min_single,
                )
        except Exception as e:
            logger.debug(f"十大视角引擎初始化跳过: {e}")

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
        regime: str | None = None,
    ) -> dict | None:
        try:
            signal = self._router.route(df, symbol, name=meta.get("name", ""), regime=regime)
            if not signal:
                return None

            signal["prev_close"] = float(df.iloc[-2]["close"]) if len(df) >= 2 else None
            signal["is_st"] = bool(meta.get("is_st", False))
            signal["is_suspended"] = bool(meta.get("is_suspended", False))
            # 保留 name：API 有效就用 API 的，API 脤数据（等于代码）就留给 formatter 的 fallback 表解析
            # 不再清空为 ""，避免下游 fallback 表查不到时只显示代码
            raw_name = signal.get("name") or meta.get("name", "")
            # 清洗 API 脤数据中的空白字符（如 '五 粮 液' → '五粮液'）
            if raw_name:
                raw_name = "".join(raw_name.split())
            if raw_name and raw_name != signal["symbol"]:
                signal["name"] = raw_name
            else:
                # API 脤数据/空：用本地映射表兜底（formatter 也会再查一次）
                from gugu.data.stock_names import lookup_stock_name

                local_name = lookup_stock_name(signal["symbol"])
                signal["name"] = local_name  # 查不到为 ""，formatter 会 fallback 到代码

            signal["price"] = float(df.iloc[-1]["close"])
            signal["price_is_realtime"] = False
            if rt_all is not None and not rt_all.empty:
                try:
                    row = rt_all[rt_all["symbol"] == symbol]
                    if not row.empty:
                        rt_price = float(row.iloc[0]["price"])
                        if rt_price > 0:
                            signal["price"] = rt_price
                            signal["price_is_realtime"] = True
                except Exception:
                    pass

            signal["suggested_position_ratio"] = budget.single_limit
            signal["has_position"] = symbol in portfolio

            # P0 修复：信号数据合理性校验（防止错误数据发送飞书）
            price = signal.get("price", 0)
            confidence = signal.get("confidence")
            price_is_realtime = signal.get("price_is_realtime", False)
            if price <= 1.0:
                logger.warning(
                    f"{symbol} 信号价格异常 price={price}，丢弃该信号。"
                    f" name={meta.get('name', '')} df_close={float(df.iloc[-1]['close']) if len(df) > 0 else 'N/A'}"
                    f" rt_available={rt_all is not None and not rt_all.empty}"
                )
                return None
            # 实时行情获取失败时，价格来自历史K线收盘价，不是真实市场价
            # 严格拒绝发送，避免飞书通知显示过期/错误价格
            if not price_is_realtime:
                logger.warning(
                    f"{symbol} 实时行情获取失败，价格={price}来自历史K线（非实时），丢弃该信号。"
                    f" name={meta.get('name', '')} df_close={float(df.iloc[-1]['close']) if len(df) > 0 else 'N/A'}"
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
                    logger.warning(f"{symbol} 量价背离检测: {', '.join(div_result['signals'])}")
                    if signal["direction"] == "buy":
                        # 量价背离说明上涨乏力，拦截买入
                        signal["wisdom_filtered"] = True
                        if not signal.get("filter_reason"):
                            signal["filter_reason"] = (
                                f"量价背离: {', '.join(div_result['signals'])}"
                            )
                    elif signal["direction"] == "sell":
                        # 量价背离增强卖出信号置信度
                        signal["confidence"] = min(1.0, float(signal.get("confidence", 0.5)) + 0.15)
                        if signal.get("reason"):
                            signal["reason"] += f"；量价背离({', '.join(div_result['signals'])})"
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

            # signal_only 模式：跳过 LLM 异步调用（降低 cost 和阻塞），但仍执行 Wisdom 硬编码 fallback
            # P0 修复：必须获取止损价/止盈价/仓位建议，否则新手买入信号无止损违反"强制止损"铁律
            if self._exec_mode == "signal_only":
                # signal_only 模式下，不对买入信号做硬过滤（让用户自行判断）
                # 但保留危险信号标记，用于通知时提醒用户
                # 买入和卖出信号都附加基本面数据，保证通知卡片数据完整
                try:
                    fund_result = self._fundamental_filter.check(symbol)
                    signal["fundamental"] = fund_result
                except Exception as e:
                    logger.debug(f"signal_only 基本面数据获取失败 {symbol}: {e}")
                # P0 修复：调用 Wisdom 硬编码 fallback 获取止损价/止盈价/仓位建议
                # 不调用 LLM 异步接口，避免 cost 和阻塞，但保留风控关键字段
                try:
                    fallback = self._wisdom._advise_fallback(signal)
                    _wisdom_keys = (
                        "wisdom_decision",
                        "wisdom",
                        "wisdom_filtered",
                        "filter_reason",
                        "suggested_position_ratio",
                        "stop_loss_price",
                        "take_profit",
                        "conflict_resolution",
                        "mentor_perspectives",
                    )
                    for _k in _wisdom_keys:
                        if _k in fallback:
                            signal[_k] = fallback[_k]
                    signal["llm_pending"] = False
                except Exception as e:
                    logger.warning(f"signal_only Wisdom fallback 失败 {symbol}: {e}")
                    signal["llm_pending"] = False
                # signal_only 模式：附加视角评分数据（不过滤，仅用于通知卡片展示）
                if self._scan_with_perspectives:
                    try:
                        ctx = build_context_from_signal(signal)
                        result = self._perspective_engine.analyze(ctx)  # type: ignore[union-attr]
                        attach_perspective_to_signal(signal, result)
                        signal["perspective_check"] = {
                            "composite_score": result.composite_score,
                            "min_score": result.min_score,
                            "passed": result.passed,
                            "fatal_count": result.fatal_count,
                            "verdict": result.overall_verdict,
                        }
                    except Exception as e:
                        logger.debug(f"signal_only 视角分析失败 {symbol}: {e}")
                logger.info(
                    f"信号(仅通知): {signal['symbol']} {signal['direction']} "
                    f"置信度 {signal['confidence']} 策略 {signal['strategies']} "
                    f"止损={signal.get('stop_loss_price', 0)}"
                )
                return signal

            # ===== 以下为 paper/live 模式完整过滤链 =====

            # 3.6 十大视角质量门禁（基于认知评分过滤低质量信号）
            # 只在非 signal_only 模式下生效：综合评分低于阈值或有致命缺陷的信号被拦截
            if self._scan_with_perspectives and not signal.get("wisdom_filtered"):
                try:
                    ctx = build_context_from_signal(signal)
                    result = self._perspective_engine.analyze(ctx)  # type: ignore[union-attr]
                    # 将视角结果附加到信号（用于通知卡片和历史记录）
                    attach_perspective_to_signal(signal, result)
                    signal["perspective_check"] = {
                        "composite_score": result.composite_score,
                        "min_score": result.min_score,
                        "passed": result.passed,
                        "fatal_count": result.fatal_count,
                        "verdict": result.overall_verdict,
                    }
                    if not result.passed:
                        logger.info(
                            f"{symbol} 视角过滤: 综合{result.composite_score:.1f} "
                            f"最低{result.min_score:.1f} 致命{result.fatal_count}条 — {result.overall_verdict}"
                        )
                        if result.fatal_count > 0:
                            signal["wisdom_filtered"] = True
                            signal["filter_reason"] = (
                                f"视角过滤: {result.overall_verdict} "
                                f"(致命{result.fatal_count}条/最低{result.min_score:.1f})"
                            )
                    else:
                        logger.debug(
                            f"{symbol} 视角通过: 综合{result.composite_score:.1f} "
                            f"最低{result.min_score:.1f}"
                        )
                except Exception as e:
                    logger.debug(f"视角质量检查失败 {symbol}: {e}")
                    # 检查失败不影响信号

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

            # 2.3 高低切换板块轮动过滤（v0.4.5 新增）
            # 高位板块（涨幅大+资金流出）→ 拒绝买入，不追高
            # 低位板块（跌幅深+资金流入）→ 放行并标记，适合低吸
            if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
                try:
                    hl_check = await self._sector_rotation.check_high_low_filter(symbol)
                    signal["high_low_check"] = hl_check
                    if not hl_check.get("allow_buy", True):
                        logger.warning(f"{symbol} 高低切换拦截: {hl_check.get('reason', '')}")
                        signal["wisdom_filtered"] = True
                        signal["filter_reason"] = f"高低切换: {hl_check.get('reason', '')}"
                    elif hl_check.get("is_undervalued"):
                        logger.info(
                            f"{symbol} 所属板块 {hl_check.get('sector', '?')} "
                            f"处于低位+资金流入，适合低吸"
                        )
                except Exception as e:
                    logger.debug(f"高低切换检查失败 {symbol}: {e}")

            # 2.4 筹码集中度过滤（v0.5.0 新增，仅买入信号）
            # 股东户数季度披露，作为慢变量过滤器：
            # - 避雷针信号（户数增 > 30%）→ 拒绝买入
            # - 潜伏者信号（户数降 > 20%）→ 置信度加成
            if (
                signal["direction"] == "buy"
                and not signal.get("wisdom_filtered")
                and self._holder_concentration_filter is not None
            ):
                try:
                    hc_result = await self._holder_concentration_filter.check(symbol)
                    signal["holder_concentration"] = hc_result
                    if not hc_result.get("pass", True):
                        logger.warning(
                            f"{symbol} 筹码集中度过滤: {hc_result.get('label', '')} — "
                            f"{', '.join(hc_result.get('reasons', []))}"
                        )
                        signal["wisdom_filtered"] = True
                        signal["filter_reason"] = (
                            f"筹码结构: {hc_result.get('label', '')} — "
                            f"{', '.join(hc_result.get('reasons', []))}"
                        )
                    elif hc_result.get("label") == "潜伏者":
                        # 潜伏者信号：置信度加成
                        boost = 0.15
                        signal["confidence"] = min(
                            1.0, float(signal.get("confidence", 0.5)) + boost
                        )
                        logger.info(f"{symbol} 筹码潜伏者信号：置信度 +{boost:.2f}")
                except Exception as e:
                    logger.debug(f"筹码集中度检查失败 {symbol}: {e}")

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
            # P0 修复：DOWNTREND 阶段也拦截买入（原逻辑只拦 FRENZY/FINAL）
            # 下跌趋势中接飞刀 = 违反《炒股的智慧》"不接飞刀"铁律
            if (
                signal["direction"] == "buy"
                and not signal.get("wisdom_filtered")
                and stage_result.stage
                in (MarketStage.FRENZY, MarketStage.FINAL, MarketStage.DOWNTREND)
            ):
                logger.warning(f"{symbol} 阶段过滤: {stage_result.stage.value}，不建议入场")
                signal["wisdom_filtered"] = True
                signal["filter_reason"] = (
                    f"阶段过滤: {stage_result.description}。{stage_result.suggestion}"
                )

            # 4. 市场状态仓位修正（已在 budget.single_limit 中体现）
            # 5. Wisdom 决策层（await + 超时保护，确保通知前决策就绪）
            # advise_async 内部已实现 LLM→冲突裁决→硬编码规则三级降级，
            # 此处仅加超时保护防止 LLM 长时间阻塞；超时则降级到硬编码 fallback。
            # advise_async 返回浅拷贝，需把 Wisdom 决策字段合并回原 signal。
            _wisdom_keys = (
                "wisdom_decision",
                "wisdom",
                "wisdom_filtered",
                "filter_reason",
                "suggested_position_ratio",
                "stop_loss_price",
                "take_profit",
                "conflict_resolution",
                "mentor_perspectives",
            )
            try:
                wisdom_signal = await asyncio.wait_for(
                    self._wisdom.advise_async(signal), timeout=15.0
                )
                for _k in _wisdom_keys:
                    if _k in wisdom_signal:
                        signal[_k] = wisdom_signal[_k]
                signal["llm_pending"] = False
            except TimeoutError:
                logger.warning(f"[wisdom] {symbol} LLM 决策超时(15s)，降级到硬编码 fallback")
                fallback = self._wisdom._advise_fallback(signal)
                for _k in _wisdom_keys:
                    if _k in fallback:
                        signal[_k] = fallback[_k]
                signal["llm_pending"] = False
            except Exception as e:
                logger.warning(f"[wisdom] {symbol} Wisdom 决策异常: {e}，降级到硬编码 fallback")
                fallback = self._wisdom._advise_fallback(signal)
                for _k in _wisdom_keys:
                    if _k in fallback:
                        signal[_k] = fallback[_k]
                signal["llm_pending"] = False

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


_signal_history_lock = threading.Lock()


def record_signal_history(signal: dict[str, Any], risk_result: Any, order_result: Any) -> None:
    try:
        with _signal_history_lock:
            hb_dir = gugu_config.PROJECT_ROOT / "data"
            hb_dir.mkdir(exist_ok=True)
            path = hb_dir / "signals_history.jsonl"
            _rotate_jsonl(path)

            decision_chain: list[dict] = []

            stage_info = signal.get("stage", {})
            if stage_info:
                decision_chain.append(
                    {
                        "step": 0,
                        "name": "四阶段判断",
                        "result": stage_info.get("stage", "unknown"),
                        "confidence": stage_info.get("confidence", 0),
                        "description": stage_info.get("description", ""),
                        "passed": True,
                    }
                )

            danger_info = signal.get("danger_signals", {})
            if danger_info:
                severity = danger_info.get("severity", "none")
                decision_chain.append(
                    {
                        "step": 0.5,
                        "name": "危险信号检测",
                        "result": severity,
                        "signals": danger_info.get("signals", []),
                        "description": danger_info.get("description", ""),
                        "passed": severity not in ("medium", "high"),
                    }
                )

            fund_info = signal.get("fundamental", {})
            if fund_info:
                decision_chain.append(
                    {
                        "step": 1,
                        "name": "基本面过滤",
                        "result": "pass" if fund_info.get("pass") else "fail",
                        "reasons": fund_info.get("reasons", []),
                        "passed": fund_info.get("pass", True),
                    }
                )

            flow_info = signal.get("money_flow", {})
            if flow_info:
                decision_chain.append(
                    {
                        "step": 2,
                        "name": "资金流过滤",
                        "result": "pass" if flow_info.get("pass") else "fail",
                        "reasons": flow_info.get("reasons", []),
                        "passed": flow_info.get("pass", True),
                    }
                )

            sector_info = signal.get("sector_check", {})
            if sector_info and "is_hot" in sector_info:
                decision_chain.append(
                    {
                        "step": 2.2,
                        "name": "板块轮动感知",
                        "result": "hot"
                        if sector_info.get("is_hot")
                        else ("cold" if sector_info.get("is_cold") else "neutral"),
                        "industry": sector_info.get("industry", ""),
                        "passed": True,
                    }
                )

            chip_info = signal.get("chip_check", {})
            if chip_info:
                decision_chain.append(
                    {
                        "step": 2.4,
                        "name": "筹码结构过滤",
                        "result": chip_info.get("label", "unknown"),
                        "chip_score": chip_info.get("chip_score", 0.5),
                        "passed": chip_info.get("label") != "避雷针",
                    }
                )

            avg_down_info = signal.get("avg_down_check", {})
            if avg_down_info:
                decision_chain.append(
                    {
                        "step": 2.5,
                        "name": "向下摊平检查",
                        "result": "allowed" if avg_down_info.get("allowed") else "blocked",
                        "reason": avg_down_info.get("reason", ""),
                        "profit_pct": avg_down_info.get("profit_pct", 0),
                        "passed": avg_down_info.get("allowed", True),
                    }
                )

            ind_info = signal.get("industry_check", {})
            if ind_info:
                decision_chain.append(
                    {
                        "step": 3,
                        "name": "行业约束",
                        "result": "allowed" if ind_info.get("allowed") else "blocked",
                        "reason": ind_info.get("reason", ""),
                        "passed": ind_info.get("allowed", True),
                    }
                )

            persp_info = signal.get("perspective_check", {})
            if persp_info:
                decision_chain.append(
                    {
                        "step": 3.6,
                        "name": "十大视角质量门禁",
                        "result": f"综合{persp_info.get('composite_score', 0):.1f} "
                        f"最低{persp_info.get('min_score', 0):.1f}",
                        "composite_score": persp_info.get("composite_score", 0),
                        "min_score": persp_info.get("min_score", 0),
                        "fatal_count": persp_info.get("fatal_count", 0),
                        "verdict": persp_info.get("verdict", ""),
                        "passed": persp_info.get("passed", True),
                    }
                )

            wisdom_dec = signal.get("wisdom_decision", {})
            if wisdom_dec:
                decision_chain.append(
                    {
                        "step": 5,
                        "name": "Wisdom决策",
                        "result": wisdom_dec.get("action", ""),
                        "position_ratio": wisdom_dec.get("position_ratio", 0),
                        "stop_loss_price": wisdom_dec.get("stop_loss_price", 0),
                        "reason": wisdom_dec.get("reason", ""),
                        "passed": not signal.get("wisdom_filtered", False),
                    }
                )

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
                "rank_score": signal.get("rank_score"),
                "factor_score": signal.get("factor_score"),
                "chip_score": signal.get("chip_score"),
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
                "order_stamp_tax": getattr(order_result, "stamp_tax", 0),
                "order_transfer_fee": getattr(order_result, "transfer_fee", 0),
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning(f"记录信号历史失败: {e}")
