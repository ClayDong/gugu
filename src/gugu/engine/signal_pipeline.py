"""信号过滤流水线：从策略信号到 Wisdom 决策的全流程。

提取自 TradingEngine._scan_signals，形成独立可测试的 SignalPipeline 类。

过滤链顺序（signal_only 模式）：
  [步骤0]   四阶段判断（StageDetector）
  [步骤0.5] 危险信号检测（DangerSignalDetector）
  [步骤0.7] 量价背离检测（DivergenceDetector）
  [步骤1]   基本面过滤（FundamentalFilter，仅买入）
  [步骤2]   资金流过滤（MoneyFlowFilter，仅买入）
  [步骤2.5] 板块轮动检查（SectorRotation，仅买入）
  [步骤3]   筹码集中度（HolderConcentrationFilter，仅买入）
  [步骤3.5] 另类Alpha因子（AltAlphaFilter，仅买入）
  [步骤4]   向下摊平检查（NoAverageDownChecker，仅买入）
  [步骤4.5] 行业约束（IndustryConstraint，仅买入）
  [步骤5]   Wisdom 决策层（signal_only 仅 fallback）

过滤链顺序（paper/live 模式）：
  [步骤0]   四阶段判断（StageDetector）
  [步骤0.5] 危险信号检测（DangerSignalDetector）
  [步骤0.7] 量价背离检测（DivergenceDetector）
  [步骤1]   基本面过滤（FundamentalFilter，仅买入）
  [步骤2]   资金流过滤（MoneyFlowFilter，仅买入）
  [步骤2.5] 板块轮动检查（SectorRotation，仅买入）
  [步骤3]   筹码集中度（HolderConcentrationFilter，仅买入）
  [步骤3.5] 另类Alpha因子（AltAlphaFilter，仅买入）
  [步骤4]   向下摊平检查（NoAverageDownChecker，仅买入）
  [步骤4.5] 行业约束（IndustryConstraint，仅买入）
  [步骤5]   四阶段入场过滤（StageDetector）
  [步骤6]   Wisdom 决策层（LLM 或 fallback）
  [步骤7]   十大视角评估（PerspectiveEngine）
  [步骤8]   综合入场判定（evaluate_entry_gate）

P0 优化：signal_only 模式下跳过 Wisdom 决策和日内不变过滤。
数据质量门禁：资金流及其他关键数据缺失时，paper/live 阻断新增买入；signal_only/track_live 仅保留观察信号。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict
from contextlib import suppress
from pathlib import Path
from typing import Any

import pandas as pd

from gugu import config as gugu_config
from gugu.analysis.danger_signal import DangerSignalDetector
from gugu.analysis.golden_ratio import calc_golden_ratio
from gugu.analysis.no_average_down import NoAverageDownChecker
from gugu.analysis.perspectives.engine import PerspectiveEngine
from gugu.analysis.perspectives.integrators import (
    attach_perspective_to_signal,
    build_context_from_signal,
)
from gugu.analysis.position_controller import PositionBudget, PositionController
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu.analysis.sector_rotation import SectorRotation
from gugu.analysis.stage_detector import StageDetector
from gugu.data.manager import DataManager
from gugu.data.quality_state import resolve_signal_data_quality, should_block_buy
from gugu.data.stock_names import lookup_stock_name
from gugu.detectors.divergence_detector import DivergenceDetector
from gugu.engine.signal_router import SignalRouter
from gugu.filters.altalpha import AltAlphaFilter
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.holder_concentration import HolderConcentrationFilter
from gugu.filters.industry_constraint import IndustryConstraint
from gugu.filters.money_flow import MoneyFlowFilter
from gugu.models.decision import evaluate_entry_gate
from gugu.utils.atomic import append_jsonl
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
        altalpha_filter: AltAlphaFilter | None = None,
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
            logger.debug("筹码过滤器初始化跳过: {}", e)
        # v0.6.2 另类 Alpha 因子过滤器（事件驱动信号增强）
        self._altalpha_filter: AltAlphaFilter | None = None
        if altalpha_filter is not None:
            self._altalpha_filter = altalpha_filter
        else:
            try:
                cfg_aa = gugu_config.get_config()
                if hasattr(cfg_aa, "altalpha_filter"):
                    aa_cfg = cfg_aa.altalpha_filter
                    if getattr(aa_cfg, "enabled", True):
                        self._altalpha_filter = AltAlphaFilter()
                        logger.info("另类 Alpha 因子过滤器已启用")
            except Exception as e:
                logger.debug("另类 Alpha 过滤器初始化跳过: {}", e)

        self._industry_constraint = industry_constraint or IndustryConstraint()
        self._sector_rotation = sector_rotation or SectorRotation()
        self._hot_sectors_cache: dict[str, Any] | None = None
        self._hot_sectors_ts: float = 0.0
        self._exec_mode = exec_mode

        # LLM 超时配置（用于 asyncio.wait_for，比 LLM 超时稍大以容纳重试）
        self._llm_timeout = gugu_config.get_config().wisdom.llm_timeout + 5.0

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
                    "十大视角过滤已启用 (综合≥{}, 单项≥{})",
                    self._perspective_min_composite,
                    self._perspective_min_single,
                )
        except Exception as e:
            logger.debug("十大视角引擎初始化跳过: {}", e)
            self._scan_with_perspectives = False  # P0-2 修复：异常时重置标志

        # 过滤链拒绝统计（每轮扫描重置，用于日志输出）
        self._filter_stats: dict[str, int] = defaultdict(int)
        self._filter_scan_count: int = 0

    def _inc_filter(self, step_name: str) -> None:
        """记录某过滤步骤的拒绝次数。"""
        self._filter_stats[step_name] += 1

    def _log_filter_stats(self) -> None:
        """输出本轮扫描的过滤链拒绝统计。"""
        if self._filter_scan_count == 0:
            return
        total_rejected = sum(self._filter_stats.values())
        if total_rejected == 0:
            return
        logger.info(
            "过滤链统计: 扫描{}只, {}步拒绝{}次",
            self._filter_scan_count,
            len(self._filter_stats),
            total_rejected,
        )
        for step, cnt in sorted(self._filter_stats.items(), key=lambda x: -x[1]):
            pct = cnt / self._filter_scan_count * 100
            logger.info("   {:<20s}: {:>3d}次 ({:.1f}%)", step, cnt, pct)

    def _entry_gate(
        self,
        signal: dict[str, Any],
        symbol: str,
        portfolio: dict[str, Any],
        account: Any,
        regime: str | None,
        df: pd.DataFrame,
    ) -> str | None:
        """入场前检查清单（交易纪律）。

        返回 None 表示通过，返回字符串表示拦截原因。
        所有买入信号必须通过此门禁，不可绕过。
        """
        # ── 1. 市场状态不允许买入 ──────────────────────
        if regime in ("bear", "crash"):
            return f"市场状态为 {regime}，禁止买入"

        # ── 2. 风控熔断中 ──────────────────────────────
        from gugu.risk.manager import RiskManager

        risk = RiskManager(scope="live")
        # is_halted 是 @property（bool），不是方法——误加 () 会抛
        # TypeError: 'bool' object is not callable（2026-09-03 实证，v0.9.42 修复）
        if risk.is_halted:
            return "风控熔断中，禁止买入"

        # ── 3. 总持仓数已达上限 ─────────────────────────
        positions = portfolio.get("positions", {})
        cfg = gugu_config.get_config()
        max_positions = cfg.risk.max_total_positions
        active_positions = {k: v for k, v in positions.items() if v.get("quantity", 0) > 0}
        if len(active_positions) >= max_positions:
            return f"持仓数已达上限 {max_positions}，禁止新增买入"

        # ── 4. 剧烈波动日（单日涨跌 > 5%） ──────────────
        if len(df) >= 2:
            today_ret = (
                (df["close"].iloc[-1] / df["close"].iloc[-2] - 1) if df["close"].iloc[-2] > 0 else 0
            )
            if abs(today_ret) > 0.05:
                direction = "暴涨" if today_ret > 0 else "暴跌"
                return f"今日{direction} {today_ret:.1%}，不追涨杀跌"

        # ── 5. 涨停环境（钱多多：每天只有几只涨停就空仓） ──
        # 冰点除外——冰点是试探性买入的窗口
        strategy_name = str(signal.get("strategy", ""))
        from gugu.analysis.market_env import (
            should_avoid_board,
            should_avoid_relay,
            should_empty_position,
        )

        # 简化：用单股数据近似涨停环境（全市场数据需从引擎层注入）
        # 生产环境由引擎层预计算 market_env 并注入到 signal context
        precomputed_env = signal.get("_market_env")
        if precomputed_env:
            if precomputed_env.get("detector_status") != "available":
                return "市场环境数据不足，仅观察"
            if should_empty_position(precomputed_env):
                return f"涨停稀少({precomputed_env.get('limit_up_count', 0)}只)，空仓等机会"
            if "ghost_breakout" in strategy_name and should_avoid_board(precomputed_env):
                return f"封板率{precomputed_env.get('seal_rate', 0):.0%}低于70%，不打板"
            if "dragon_head" in strategy_name and should_avoid_relay(precomputed_env):
                return f"接力成功率{precomputed_env.get('relay_rate', 0):.0%}低于60%，不接力"

        return None

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
        dragon_head_info: dict[str, Any] | None = None,
        market_env: dict[str, Any] | None = None,
    ) -> dict | None:
        try:
            self._filter_scan_count += 1
            signal = self._router.route(df, symbol, name=meta.get("name", ""), regime=regime)
            if not signal:
                self._inc_filter("信号路由")
                return None

            # 龙头策略在跨股证据不可用时 fail-closed，不静默当成真龙头
            _signal_strategies = signal.get("strategies", []) or []
            _is_dragon_signal = (
                "dragon_head" in _signal_strategies or signal.get("strategy") == "dragon_head"
            )
            if signal.get("direction") == "buy" and _is_dragon_signal and not dragon_head_info:
                signal["observation_only"] = True
                signal["gate_blocked"] = True
                signal["gate_reason"] = "跨股龙头证据不可用，仅观察"

            # 市场环境由引擎批量预计算；缺失/不足只允许观察，不能升级买入资格
            if market_env:
                signal["_market_env"] = market_env
                signal["market_env"] = market_env
                if (
                    signal.get("direction") == "buy"
                    and market_env.get("detector_status") != "available"
                ):
                    signal["observation_only"] = True
                    signal["gate_blocked"] = True
                    signal["gate_reason"] = "市场环境数据不足，仅观察"

            # ═══════════════════════════════════════════════════════
            # 道①：入场前检查清单（交易纪律，不可绕过）
            # "耐心等待"——只在胜率最高时出手
            # ═══════════════════════════════════════════════════════
            if signal.get("direction") == "buy":
                gate_result = self._entry_gate(signal, symbol, portfolio, account, regime, df)
                if gate_result:
                    signal["observation_only"] = True
                    signal["gate_blocked"] = True
                    signal["gate_reason"] = gate_result
                    logger.info("入场门禁拦截 %s: %s", symbol, gate_result)
                    from gugu.journal.trade_log import log_signal_blocked

                    log_signal_blocked(
                        symbol=symbol,
                        strategy=str(signal.get("strategy", "")),
                        reason=gate_result,
                        price=float(signal.get("price", 0)),
                        regime=regime,
                    )

            # P2 能力圈标注：注入策略可解释性/验证等级/仓位上限
            # （巴菲特：知道自己在圈内还是圈外）
            try:
                from gugu.strategies.capability import annotate_signal

                signal = annotate_signal(signal)
            except Exception:
                pass  # 标注失败不阻塞信号
            capability = signal.get("capability") or {}
            validation_status = str(capability.get("validation_status", "")).lower()
            if signal.get("direction") == "buy" and (
                not validation_status
                or validation_status in {"experimental", "in_review", "unknown"}
            ):
                signal["observation_only"] = True
                signal["validation_note"] = "策略尚未完成生产验证，仅供观察，不执行买入"

            # 跨股龙头纪律（钱多多：只做市场选出的真龙头）
            # 引擎层已用批量数据识别板块龙头/中军/跟随龙并注入 dragon_head_info
            if dragon_head_info:
                signal["dragon_head"] = dragon_head_info
                is_dragon = bool(dragon_head_info.get("is_dragon"))
                dragon_sector = str(dragon_head_info.get("sector", ""))
                # dragon_head 策略只对板块真龙头放行；非龙头信号降级观察
                strategies_list = signal.get("strategies", []) or []
                from_dragon_strategy = (
                    "dragon_head" in strategies_list
                    or str(signal.get("strategy", "")) == "dragon_head"
                )
                if signal.get("direction") == "buy" and from_dragon_strategy and not is_dragon:
                    signal["observation_only"] = True
                    signal["gate_reason"] = (
                        f"非板块真龙头（{dragon_sector}板块），只做市场选出的龙头"
                    )
                    signal["dragon_head_role"] = (
                        dragon_head_info.get("is_anchor")
                        and "中军"
                        or (dragon_head_info.get("is_follower") and "跟随龙" or "非龙头")
                    )
                    logger.info(
                        "龙头纪律拦截 {}: {} 板块非龙头（角色={}），dragon_head 信号降级观察",
                        symbol,
                        dragon_sector,
                        signal.get("dragon_head_role", "?"),
                    )
                    from gugu.journal.trade_log import log_signal_blocked

                    log_signal_blocked(
                        symbol=symbol,
                        strategy="dragon_head",
                        reason=signal.get("gate_reason", ""),
                        price=float(signal.get("price", 0)),
                        regime=regime,
                    )

            # 基础信号验证（无持仓卖出、停牌、ST股票）
            if self._validate_signal_basic(signal, symbol, portfolio, meta):
                return None

            # V5框架集成：Golden-Ratio军规检查（买入信号专用）
            # 军规线0.5破位 → 禁止新建仓（趋势已破坏）
            if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
                try:
                    market_cap = meta.get("market_cap")
                    gr = calc_golden_ratio(df, market_cap=market_cap)
                    if gr and gr.military_rule_broken:
                        self._inc_filter("gr_military_rule")
                        signal["wisdom_filtered"] = True
                        signal["filter_reason"] = (
                            f"GR军规破位({gr.current_callback_pct:.1f}%回调)，禁止新建仓"
                        )
                        signal["golden_ratio"] = gr.to_dict()
                        logger.info(
                            "{} GR军规破位: {}, 回调{:.1f}%，拦截买入",
                            symbol,
                            gr.action_zone,
                            gr.current_callback_pct,
                        )
                    elif gr:
                        signal["golden_ratio"] = gr.to_dict()
                        if gr.discount_factor < 1.0:
                            signal["confidence"] = (
                                signal.get("confidence", 0.7) * gr.discount_factor
                            )
                except Exception:
                    logger.debug("{} GR计算异常，跳过军规检查", symbol, exc_info=True)

            #  enrich signal with price, name, and basic context
            if self._enrich_signal_with_price_and_name(
                signal, symbol, df, meta, rt_all, budget, portfolio, account
            ):
                return None

            # 初始化统一三态数据质量和市场上下文。
            # fresh_valid 才允许新增买入；stale_degraded 只观察；unavailable 阻断。
            quality_state = resolve_signal_data_quality(
                signal,
                dm_degraded=bool(self._dm.is_degraded),
            )
            signal["data_quality"] = quality_state.value
            if quality_state.value != "fresh_valid":
                signal["observation_only"] = True
                signal["data_quality_note"] = (
                    "数据源降级或非实时，信号仅供观察，禁止新增买入"
                    if quality_state.value == "stale_degraded"
                    else "关键数据不可用，禁止新增买入"
                )
            if signal["direction"] == "buy":
                blocked, reason = should_block_buy(
                    signal,
                    dm_degraded=bool(self._dm.is_degraded),
                )
                if blocked:
                    self._inc_filter("data_quality_buy_gate")
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = reason
                    signal["rejected"] = True
                    signal["reject_reason"] = reason
                    signal["observation_only"] = True
                    # 提醒模式保留审计信号；paper/live 不允许新增买入继续流转。
                    if self._exec_mode not in ("signal_only", "track_live"):
                        return None
            signal["data_timestamp"] = signal.get("data_timestamp", pd.Timestamp.now().isoformat())
            signal["data_sources"] = signal.get(
                "data_sources", ["realtime"] if signal.get("price_is_realtime") else ["history"]
            )
            self._init_market_context(signal, regime, portfolio, account)

            # P1修复：盘中前视偏差防护
            # ⚠️ 2026-08-11 修复（与 engine_mixin AUDIT_LOOKAHEAD 同根因）:
            # 原实现 is_intraday 时无条件 iloc[:-1]，但 A股日K源盘中不含当日bar
            # （最后一行=昨日已收盘bar）→ 分析阶段也丢掉昨日完整bar。
            # 正确语义: 仅当最后一行日期==今日才剔除当日未完成K线。
            is_intraday = rt_all is not None and not rt_all.empty and pd.Timestamp.now().hour < 15
            if is_intraday and len(df) > 60 and "date" in df.columns:
                _last_bar_date = pd.Timestamp(df["date"].iloc[-1]).date()
                if _last_bar_date == pd.Timestamp.now().date():
                    df_for_analysis = df.iloc[:-1]
                else:
                    df_for_analysis = df
            else:
                df_for_analysis = df

            # 阶段判断 + 危险信号 + 量价背离 + 周线趋势
            self._detect_stage_and_danger(signal, symbol, df, df_for_analysis)

            perspective_result = None

            # signal_only / track_live 模式：跳过 LLM 异步调用（降低 cost 和阻塞），
            # 但仍执行 Wisdom 硬编码 fallback。track_live 与 signal_only 同属"提醒"
            # 语义（只读跟踪、不自动交易）——2026-08-05 修复：track_live 此前走
            # paper/live 链被 evaluate_entry_gate 拦截（数据降级 data_quality!=fresh
            # + 持仓满 5 只预算 0%），导致"行情好但 0 买入信号"；持仓已满时用户
            # 仍应看到建议信号（决定是否调仓），而不是被静默过滤。
            if self._exec_mode in ("signal_only", "track_live"):
                return await self._run_signal_only_chain(
                    signal, symbol, portfolio, perspective_result
                )

            # ===== 以下为 paper/live 模式完整过滤链 =====
            # 原则：保留安全保护过滤（ST/停牌/危险信号/四阶段），
            # 放宽数据依赖和分析类过滤，让更多信号到达执行层
            perspective_result = await self._run_paper_live_chain(
                signal, symbol, portfolio, perspective_result
            )

            # 4. 市场状态仓位修正（已在 budget.single_limit 中体现）
            # 5. Wisdom 决策层（await + 超时保护，确保通知前决策就绪）
            # advise_async 内部已实现 LLM→冲突裁决→硬编码规则三级降级，
            # 此处仅加超时保护防止 LLM 长时间阻塞；超时则降级到硬编码 fallback。
            await self._run_wisdom_decision(signal, symbol, perspective_result)

            logger.info(
                "信号: {} {} 置信度 {} 策略 {}",
                signal["symbol"],
                signal["direction"],
                signal["confidence"],
                signal["strategies"],
            )
            self._log_filter_stats()
            return signal

        except Exception as e:
            logger.error("扫描 {} 失败: {}", symbol, e)
            return None

    def _validate_signal_basic(
        self, signal: dict[str, Any], symbol: str, portfolio: dict[str, Any], meta: dict[str, Any]
    ) -> bool:
        """基础信号验证：无持仓卖出、停牌、ST股票。

        Returns:
            True if signal should be discarded, False if it passes validation.
        """
        # P0 修复：无持仓卖出硬检查（任何模式下都拦截，防止对空仓发卖出信号）
        if signal["direction"] == "sell" and signal["symbol"] not in portfolio:
            self._inc_filter("sell_no_position")
            logger.debug("{} 无持仓，过滤卖出信号", symbol)
            return True

        # P0 修复：停牌股票硬拦截（任何模式、任何方向都不发信号）
        is_suspended = bool(meta.get("is_suspended", False))
        if is_suspended:
            self._inc_filter("suspended")
            logger.debug("{} 停牌，过滤信号", symbol)
            return True

        # P0 修复：ST股票买入拦截
        is_st = bool(meta.get("is_st", False))
        signal["is_st"] = is_st
        signal["is_suspended"] = is_suspended
        if is_st and signal["direction"] == "buy":
            self._inc_filter("st_stock")
            logger.info("{} 是ST股票，拦截买入信号", symbol)
            signal["wisdom_filtered"] = True
            signal["filter_reason"] = "ST股票不建议买入"

        return False

    def _enrich_signal_with_price_and_name(
        self,
        signal: dict[str, Any],
        symbol: str,
        df: pd.DataFrame,
        meta: dict[str, Any],
        rt_all: pd.DataFrame | None,
        budget: PositionBudget,
        portfolio: dict[str, Any],
        account: Any,
    ) -> bool:
        """enrich signal with price, name, and basic context.

        Returns:
            True if signal should be discarded (invalid data), False if it passes.
        """
        signal["prev_close"] = float(df.iloc[-2]["close"]) if len(df) >= 2 else None

        # 保留 name：API 有效就用 API 的，API 胖数据（等于代码）就留给 formatter 的 fallback 表解析
        raw_name = signal.get("name") or meta.get("name", "")
        if raw_name:
            raw_name = "".join(raw_name.split())
        if raw_name and raw_name != signal["symbol"]:
            signal["name"] = raw_name
        else:
            from gugu.data.stock_names import lookup_stock_name

            signal["name"] = lookup_stock_name(signal["symbol"])

        signal["price"] = float(df.iloc[-1]["close"])
        signal["price_is_realtime"] = False
        signal["price_note"] = ""
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

        if not signal["price_is_realtime"]:
            signal["price_note"] = "⚠️ 非实时价格（实时行情获取失败，使用最新收盘价）"

        signal["suggested_position_ratio"] = budget.single_limit
        signal["has_position"] = symbol in portfolio

        # P0 修复：信号数据合理性校验
        price = signal.get("price", 0)
        confidence = signal.get("confidence")
        if price <= 1.0:
            self._inc_filter("price_invalid")
            logger.warning("{} 信号价格异常 price={}，丢弃该信号。", symbol, price)
            # P2-10: 记录被拒绝的信号到历史
            signal["rejected"] = True
            signal["reject_reason"] = f"价格异常 price={price}"
            with suppress(Exception):
                record_signal_history(signal, None, None)
            return True
        if confidence is None or confidence <= 0.0:
            self._inc_filter("confidence_zero")
            logger.warning("{} 信号置信度无效 confidence={}，丢弃该信号", symbol, confidence)
            # P2-10: 记录被拒绝的信号到历史
            signal["rejected"] = True
            signal["reject_reason"] = f"置信度无效 confidence={confidence}"
            with suppress(Exception):
                record_signal_history(signal, None, None)
            return True

        if symbol in portfolio and account.total_value > 0:
            pos = portfolio[symbol]
            signal["current_position_ratio"] = (
                pos.quantity * pos.current_price / account.total_value
            )
        else:
            signal["current_position_ratio"] = 0.0

        return False

    def _init_market_context(
        self, signal: dict[str, Any], regime: str | None, portfolio: dict[str, Any], account: Any
    ) -> None:
        """初始化市场上下文信息。"""
        signal["market_context"] = {
            "data_source": "degraded" if self._dm.is_degraded else "normal",
            "data_quality": signal.get("data_quality", "fresh"),
            "data_timestamp": signal.get("data_timestamp", pd.Timestamp.now().isoformat()),
            "data_sources": signal.get(
                "data_sources", ["realtime"] if signal.get("price_is_realtime") else ["history"]
            ),
            "portfolio_count": len(portfolio),
            "cash_ratio": account.cash / account.total_value if account.total_value > 0 else 1.0,
            "regime": regime or "",
        }

    def _detect_stage_and_danger(
        self,
        signal: dict[str, Any],
        symbol: str,
        df: pd.DataFrame,
        df_for_analysis: pd.DataFrame,
    ) -> None:
        """阶段判断 + 危险信号 + 量价背离检测 + 周线趋势。"""
        # 阶段判断 + 危险信号（轻量，每次执行）
        stage_result = self._stage_detector.detect(df_for_analysis)
        signal["stage"] = {
            "stage": stage_result.stage.value,
            "confidence": stage_result.confidence,
            "description": stage_result.description,
            "suggestion": stage_result.suggestion,
        }
        logger.info(
            "{} 阶段判断: {} (置信度 {:.2f}) — {}",
            symbol,
            stage_result.stage.value,
            stage_result.confidence,
            stage_result.suggestion,
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
                "{} 危险信号: {}, 严重程度={}",
                symbol,
                danger_result.signals,
                danger_result.severity,
            )
            if signal["direction"] == "buy":
                self._inc_filter("danger_signal")
                signal["wisdom_filtered"] = True
                signal["filter_reason"] = f"危险信号: {danger_result.description}"

        # 量价背离检测
        try:
            div_result = self._divergence_detector.detect(df)
            signal["divergence"] = div_result
            if div_result["signals"]:
                logger.warning("{} 量价背离检测: {}", symbol, ", ".join(div_result["signals"]))
                if signal["direction"] == "buy":
                    self._inc_filter("divergence")
                    signal["wisdom_filtered"] = True
                    if not signal.get("filter_reason"):
                        signal["filter_reason"] = f"量价背离: {', '.join(div_result['signals'])}"
                elif signal["direction"] == "sell":
                    signal["confidence"] = min(1.0, float(signal.get("confidence", 0.5)) + 0.15)
                    if signal.get("reason"):
                        signal["reason"] += f"；量价背离({', '.join(div_result['signals'])})"
        except Exception as e:
            logger.warning("量价背离检测失败 {}: {}", symbol, e)
            signal["divergence"] = {"error": str(e)}

        # 周线趋势判断
        if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
            try:
                weekly_trend = self._check_weekly_trend(df_for_analysis)
                signal["multi_period"] = weekly_trend
                if not weekly_trend.get("weekly_aligned", True):
                    logger.info(
                        "{} 周线趋势 {} 与日线信号方向不一致，标记为周线背离",
                        symbol,
                        weekly_trend.get("weekly_trend", "?"),
                    )
                    signal["weekly_misaligned"] = True
                    if not signal.get("filter_reason"):
                        signal["filter_reason"] = (
                            f"周线趋势 {weekly_trend.get('weekly_trend', '?')}与买入信号方向不一致"
                        )
            except Exception as e:
                logger.debug("多周期趋势判断失败: {}", e)
                signal["multi_period"] = {"error": str(e)}
        elif signal["direction"] == "sell":
            try:
                weekly_trend = self._check_weekly_trend(df)
                signal["multi_period"] = weekly_trend
            except Exception as e:
                logger.debug("卖出信号多周期趋势判断失败: {}", e)
                signal["multi_period"] = {"error": str(e)}

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

    async def _run_signal_only_chain(
        self,
        signal: dict[str, Any],
        symbol: str,
        portfolio: dict[str, Any],
        perspective_result: Any,
    ) -> dict[str, Any]:
        """提醒模式的过滤链：保留信号用于观察，不产生新增买入执行资格。

        特点：
        - signal_only/track_live 保留候选信号，供通知和人工判断
        - 数据质量、危险信号和过滤结果明确写入信号，不得被当作可执行买入
        - 买入和卖出信号都附加基本面数据，保证通知卡片数据完整
        - P0 修复：调用 Wisdom 硬编码 fallback 获取止损价/止盈价/仓位建议
        - v0.7.5 P0-2修复：signal_only 模式下十大视角不拦截信号
        """
        # 买入和卖出信号都附加基本面数据，保证通知卡片数据完整
        try:
            fund_result = self._fundamental_filter.check(symbol)
            signal["fundamental"] = fund_result
        except Exception as e:
            logger.debug("signal_only 基本面数据获取失败 {}: {}", symbol, e)

        # P0 修复：调用 Wisdom 硬编码 fallback 获取止损价/止盈价/仓位建议
        # 不调用 LLM 异步接口，避免 cost 和阻塞，但保留风控关键字段
        try:
            signal["_exec_mode"] = self._exec_mode
            fallback = self._wisdom.advise_fallback(signal)
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
            if fallback.get("wisdom_filtered"):
                self._inc_filter("wisdom_block")
            signal["llm_pending"] = False
        except Exception as e:
            logger.warning("signal_only Wisdom fallback 失败 {}: {}", symbol, e)
            signal["llm_pending"] = False

        # P1修复：signal_only模式补齐过滤器（仅附加数据，不拦截信号）
        if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
            # 资金流过滤（仅附加数据）
            try:
                flow_result = await self._money_flow_filter.check(symbol)
                signal["money_flow"] = flow_result
            except Exception as e:
                logger.debug("signal_only 资金流数据获取失败 {}: {}", symbol, e)
            # 板块轮动（仅附加数据）
            try:
                sector_check = await self._check_sector_rotation(symbol)
                signal["sector_check"] = sector_check
            except Exception as e:
                logger.debug("signal_only 板块轮动检查失败 {}: {}", symbol, e)
            # 筹码集中度（仅附加数据）
            if self._holder_concentration_filter is not None:
                try:
                    hc_result = await self._holder_concentration_filter.check(symbol)
                    signal["holder_concentration"] = hc_result
                except Exception as e:
                    logger.debug("signal_only 筹码检查失败 {}: {}", symbol, e)
            # 另类 Alpha 因子仅附加证据，不提高买入置信度。
            if self._altalpha_filter is not None:
                try:
                    signal["altalpha"] = await self._altalpha_filter.check(symbol)
                except Exception as e:
                    logger.debug("signal_only 另类因子检查失败 {}: {}", symbol, e)
            # 向下摊平检查（仅附加数据）
            if signal.get("has_position"):
                try:
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
                except Exception as e:
                    logger.debug("signal_only 向下摊平检查失败 {}: {}", symbol, e)
            # 行业约束（仅附加数据）
            if not signal.get("has_position"):
                try:
                    ind_result = self._industry_constraint.check_buy(
                        symbol,
                        portfolio,
                        industry=signal.get("fundamental", {}).get("industry", ""),
                    )
                    signal["industry_check"] = ind_result
                except Exception as e:
                    logger.debug("signal_only 行业约束检查失败 {}: {}", symbol, e)

        # signal_only 模式：附加视角评分数据（不过滤，仅用于通知卡片展示）
        # v0.7.5 P0-2修复：signal_only 模式下十大视角不拦截信号
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
                logger.debug("signal_only 视角分析失败 {}: {}", symbol, e)
                # v0.7.5 P0-2修复：signal_only 模式下视角分析失败不拦截
                signal["perspective_check"] = {
                    "composite_score": None,
                    "min_score": None,
                    "passed": None,
                    "fatal_count": 0,
                    "verdict": "分析失败（signal_only 模式不拦截）",
                }

        logger.info(
            "信号(仅通知): {} {} 置信度 {} 策略 {} 止损={}",
            signal["symbol"],
            signal["direction"],
            signal["confidence"],
            signal["strategies"],
            signal.get("stop_loss_price", 0),
        )

        # P1修复：signal_only模式止损价有效性校验（强制止损铁律）
        sl = signal.get("stop_loss_price", 0)
        price = signal.get("price", 0)
        if price > 0 and (sl <= 0 or sl >= price):
            signal["stop_loss_price"] = round(price * 0.93, 2)
            logger.warning(
                "signal_only {} 止损价无效(={})，已设为默认7%: {}",
                signal["symbol"],
                sl,
                signal["stop_loss_price"],
            )

        # v0.7.5 P0-2修复：signal_only 模式不调用 evaluate_entry_gate（不拦截）
        self._log_filter_stats()
        return signal

    async def _run_paper_live_chain(
        self,
        signal: dict[str, Any],
        symbol: str,
        portfolio: dict[str, Any],
        perspective_result: Any,
    ) -> None:
        """paper/live 模式的过滤链（实盘精简版）。

        原则：
        - 保留安全保护过滤（ST/停牌/危险信号/四阶段）
        - 数据依赖过滤统一 fail-closed：缺失/过期数据不得新增买入
        """
        # 3.5 十大视角（仅附加数据，不过滤信号）
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
                if result.fatal_count > 0:
                    logger.info(
                        "{} 视角警告(已放行): 综合{:.1f} 致命{}条 — {}",
                        symbol,
                        result.composite_score,
                        result.fatal_count,
                        result.overall_verdict,
                    )
            except Exception as e:
                logger.debug("视角分析失败 {}: {}", symbol, e)
                if signal["direction"] == "buy":
                    self._inc_filter("perspective")
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = "十大视角分析失败，缺少可信视角评分"

        # 1. 基本面过滤（仅买入信号，数据缺失也必须阻断）
        if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
            fund_result = self._fundamental_filter.check(symbol)
            signal["fundamental"] = fund_result
            if not fund_result.get("pass", False):
                reasons = ", ".join(fund_result.get("reasons", [])) or "基本面数据不可用"
                logger.info("{} 基本面过滤: {}", symbol, reasons)
                self._inc_filter("fundamental")
                signal["wisdom_filtered"] = True
                signal["filter_reason"] = f"基本面: {reasons}"

        # 2. 资金流过滤（仅买入信号，数据缺失也必须阻断）
        if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
            flow_result = await self._money_flow_filter.check(symbol)
            signal["money_flow"] = flow_result
            if not flow_result["pass"]:
                logger.info("{} 资金流过滤: {}", symbol, ", ".join(flow_result["reasons"]))
                self._inc_filter("money_flow")
                signal["wisdom_filtered"] = True
                signal["filter_reason"] = f"资金流: {', '.join(flow_result['reasons'])}"

        # 2.2 板块轮动感知（仅附加数据，不过滤信号）
        if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
            try:
                sector_check = await self._check_sector_rotation(symbol)
                signal["sector_check"] = sector_check
                if sector_check.get("is_hot"):
                    logger.info(
                        "{} 所属板块 {} 是热点板块",
                        symbol,
                        sector_check.get("industry", "?"),
                    )
            except Exception as e:
                logger.debug("板块轮动检查失败 {}: {}", symbol, e)

        # 2.3 高低切换（改为警告不拦截）
        if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
            try:
                hl_check = await self._sector_rotation.check_high_low_filter(symbol)
                signal["high_low_check"] = hl_check
                if not hl_check.get("allow_buy", True):
                    logger.info("{} 高低切换建议(已放行): {}", symbol, hl_check.get("reason", ""))
                elif hl_check.get("is_undervalued"):
                    logger.info("{} 板块低位+资金流入，适合低吸", symbol)
            except Exception as e:
                logger.debug("高低切换检查失败 {}: {}", symbol, e)

        # 2.4 筹码集中度过滤（v0.5.0 新增，仅买入信号）
        # 股东户数季度披露，作为慢变量过滤器：
        # - 避雷针信号（户数增 > 30%）→ 拒绝买入
        # - 潜伏者信号（户数降 > 20%）→ 仅作辅助证据，不调整置信度
        if (
            signal["direction"] == "buy"
            and not signal.get("wisdom_filtered")
            and self._holder_concentration_filter is not None
        ):
            try:
                hc_result = await self._holder_concentration_filter.check(symbol)
                signal["holder_concentration"] = hc_result
                if not hc_result.get("pass", True):
                    self._inc_filter("holder_concentration")
                    logger.warning(
                        "{} 筹码集中度过滤: {} — {}",
                        symbol,
                        hc_result.get("label", ""),
                        ", ".join(hc_result.get("reasons", [])),
                    )
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = (
                        f"筹码结构: {hc_result.get('label', '')} — "
                        f"{', '.join(hc_result.get('reasons', []))}"
                    )
                elif hc_result.get("label") == "潜伏者":
                    # 潜伏者只作为可解释证据展示，不提高买入置信度。
                    logger.info("{} 筹码潜伏者信号：作为辅助证据保留", symbol)
            except Exception as e:
                logger.debug("筹码集中度检查失败 {}: {}", symbol, e)

        # 2.45 另类 Alpha 因子过滤（v0.6.2 新增，仅买入信号）
        # 事件驱动/行为金融信号增强：高评分提升 confidence，极低评分标记过滤
        # 数据缺失时默认放行，避免阻塞已有策略信号
        if (
            signal["direction"] == "buy"
            and not signal.get("wisdom_filtered")
            and self._altalpha_filter is not None
        ):
            try:
                aa_result = await self._altalpha_filter.check(symbol)
                signal["altalpha"] = aa_result
                if not aa_result.get("pass", True):
                    self._inc_filter("altalpha")
                    logger.info(
                        "{} 另类因子过滤: {}", symbol, ", ".join(aa_result.get("reasons", []))
                    )
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = f"另类因子: {', '.join(aa_result.get('reasons', []))}"
                elif aa_result.get("confidence_boost", 0) > 0:
                    # 增强因子只作为证据展示；置信度只由策略本身和风险门禁决定。
                    logger.info("{} 另类因子支撑：保留为辅助证据，不调整置信度", symbol)
            except Exception as e:
                logger.debug("另类因子检查失败 {}: {}", symbol, e)

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
                self._inc_filter("no_avg_down")
                logger.warning("{} 向下摊平拦截: {}", symbol, avg_down_result.reason)
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
                self._inc_filter("industry_constraint")
                logger.info("{} 行业约束过滤: {}", symbol, ind_result["reason"])
                signal["wisdom_filtered"] = True
                signal["filter_reason"] = f"行业约束: {ind_result['reason']}"

        # 3.5 四阶段入场过滤
        # P0 修复：DOWNTREND 阶段也拦截买入（原逻辑只拦 FRENZY/FINAL）
        # 下跌趋势中接飞刀 = 违反《炒股的智慧》"不接飞刀"铁律
        stage_result = signal.get("stage", {})
        stage_stage = stage_result.get("stage", "")
        if (
            signal["direction"] == "buy"
            and not signal.get("wisdom_filtered")
            and stage_stage in ("frenzy", "final", "downtrend")
        ):
            self._inc_filter("stage_filter")
            logger.warning("{} 阶段过滤: {}，不建议入场", symbol, stage_stage)
            signal["wisdom_filtered"] = True
            signal["filter_reason"] = (
                f"阶段过滤: {stage_result.get('description', '')}。"
                f"{stage_result.get('suggestion', '')}"
            )

    async def _run_wisdom_decision(
        self,
        signal: dict[str, Any],
        symbol: str,
        perspective_result: Any,
    ) -> None:
        """Wisdom 决策层：LLM 决策 + 超时保护 + 入场判定。

        功能：
        - advise_async 内部已实现 LLM→冲突裁决→硬编码规则三级降级
        - 此处仅加超时保护防止 LLM 长时间阻塞；超时则降级到硬编码 fallback
        - advise_async 返回浅拷贝，需把 Wisdom 决策字段合并回原 signal
        - 买入信号额外执行 evaluate_entry_gate 综合入场判定
        """
        # Wisdom 决策字段列表
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
        already_filtered = bool(signal.get("wisdom_filtered", False))
        signal["_exec_mode"] = self._exec_mode

        try:
            if not already_filtered:
                # LLM 异步决策（带超时保护）
                wisdom_signal = await asyncio.wait_for(
                    self._wisdom.advise_async(signal), timeout=self._llm_timeout
                )
                for _k in _wisdom_keys:
                    if _k in wisdom_signal:
                        if _k == "wisdom_filtered" and already_filtered:
                            continue
                        signal[_k] = wisdom_signal[_k]
                if wisdom_signal.get("wisdom_filtered") and not already_filtered:
                    self._inc_filter("wisdom_block")
            else:
                # 已过滤信号仅获取止损/止盈/仓位建议
                fallback = self._wisdom.advise_fallback(signal)
                for _k in ("stop_loss_price", "take_profit", "suggested_position_ratio"):
                    if _k in fallback:
                        signal[_k] = fallback[_k]
            signal["llm_pending"] = False
        except TimeoutError:
            # 超时降级到硬编码 fallback
            logger.warning(
                "[wisdom] {} LLM 决策超时({:.0f}s)，降级到硬编码 fallback",
                symbol,
                self._llm_timeout,
            )
            fallback = self._wisdom.advise_fallback(signal)
            for _k in _wisdom_keys:
                if _k in fallback:
                    if _k == "wisdom_filtered" and already_filtered:
                        continue
                    signal[_k] = fallback[_k]
            if fallback.get("wisdom_filtered") and not already_filtered:
                self._inc_filter("wisdom_block")
            signal["llm_pending"] = False
        except Exception as e:
            # 异常降级到硬编码 fallback
            logger.warning("[wisdom] {} Wisdom 决策异常: {}，降级到硬编码 fallback", symbol, e)
            fallback = self._wisdom.advise_fallback(signal)
            for _k in _wisdom_keys:
                if _k in fallback:
                    if _k == "wisdom_filtered" and already_filtered:
                        continue
                    signal[_k] = fallback[_k]
            if fallback.get("wisdom_filtered") and not already_filtered:
                self._inc_filter("wisdom_block")
            signal["llm_pending"] = False

        # 买入信号：综合入场判定
        if signal["direction"] == "buy":
            decision = evaluate_entry_gate(signal, perspective_result)
            if decision.status == "blocked" and signal.get("filter_reason"):
                existing_reason = signal["filter_reason"]
                if existing_reason not in decision.block_reasons:
                    decision.block_reasons.insert(0, existing_reason)
            signal["decision"] = decision.to_dict()
            if decision.status == "blocked":
                self._inc_filter("entry_gate")
                signal["filter_reason"] = "；".join(decision.block_reasons)
                signal["wisdom_filtered"] = True

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
                    logger.debug("获取板块轮动数据失败: {}", e)
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
                logger.info("历史文件轮转: {} {}→{} 行", path.name, len(lines), len(keep))
    except Exception as e:
        logger.warning("历史文件轮转失败: {}", e)


_signal_history_lock = threading.Lock()


def record_signal_history(signal: dict[str, Any], risk_result: Any, order_result: Any) -> None:
    try:
        with _signal_history_lock:
            hb_dir = gugu_config.PROJECT_ROOT / "data"
            hb_dir.mkdir(exist_ok=True)
            path = hb_dir / "signals_history.jsonl"
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

            chip_info = signal.get("holder_concentration", {})
            if chip_info:
                decision_chain.append(
                    {
                        "step": 2.4,
                        "name": "筹码结构过滤",
                        "result": chip_info.get("label", "unknown"),
                        "chip_score": chip_info.get("chip_score", 0.5),
                        "passed": chip_info.get("pass", True),
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
                        "result": f"综合{persp_info.get('composite_score') or 0:.1f} "
                        f"最低{persp_info.get('min_score') or 0:.1f}",
                        "composite_score": persp_info.get("composite_score") or 0,
                        "min_score": persp_info.get("min_score") or 0,
                        "fatal_count": persp_info.get("fatal_count") or 0,
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
                "signal_id": signal.get("signal_id"),
                "parent_signal_id": signal.get("parent_signal_id"),
                "symbol": signal.get("symbol"),
                "name": _raw_name,
                "direction": signal.get("direction"),
                "price": signal.get("price"),
                "confidence": signal.get("confidence"),
                "reason": signal.get("reason", ""),
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
                "execution_success": getattr(order_result, "success", None),
                "execution_status": (
                    getattr(order_result, "fill_status", "filled")
                    if getattr(order_result, "success", False)
                    else "rejected"
                    if order_result is not None
                    else "not_submitted"
                ),
                "fill_status": getattr(order_result, "fill_status", "unknown"),
                "position_status": getattr(order_result, "position_status", "unknown"),
                "position_closed": getattr(order_result, "position_closed", False),
                "outcome_status": (
                    "closed"
                    if getattr(order_result, "success", False)
                    and signal.get("direction") == "sell"
                    and getattr(order_result, "position_closed", False)
                    else "partial"
                    if getattr(order_result, "success", False) and signal.get("direction") == "sell"
                    else "open"
                    if getattr(order_result, "success", False)
                    else "pending"
                ),
                "order_quantity": getattr(order_result, "quantity", 0),
                "order_price": getattr(order_result, "price", 0),
                "order_commission": getattr(order_result, "commission", 0),
                "order_stamp_tax": getattr(order_result, "stamp_tax", 0),
                "order_transfer_fee": getattr(order_result, "transfer_fee", 0),
                "execution_id": getattr(order_result, "execution_id", None),
                "entry_execution_id": getattr(order_result, "entry_execution_id", None),
                "realized_pnl": getattr(order_result, "realized_pnl", None),
                "attribution_method": signal.get("attribution_method", "primary"),
                "requested_quantity": signal.get("requested_quantity")
                or signal.get("quantity")
                or signal.get("suggested_quantity"),
                "exit_reason": signal.get("exit_reason") or signal.get("reason", "")
                if signal.get("direction") == "sell"
                else signal.get("exit_reason", ""),
            }
            # 2026-09-04 优化（数据专家 review）：校验策略名在注册表中，
            # 已删除策略（历史残留 dual_ma/turtle/bollinger）不再写入信号历史，
            # 避免污染策略统计。信号本身仍照常处理，只是不记录到 history。
            try:
                from gugu.strategies.registry import list_strategies as _list_strategies

                _registered = set(_list_strategies())
                # strategy 字段可能是逗号拼接的多策略名，需拆分后逐个校验
                _sig_strategy = str(record.get("strategy") or "")
                _raw_names = set()
                for _part in _sig_strategy.split(",") if _sig_strategy else []:
                    if _part.strip():
                        _raw_names.add(_part.strip())
                for _s in record.get("strategies") or []:
                    if str(_s).strip():
                        _raw_names.add(str(_s).strip())
                # 只要有一个策略名未注册（且非空），视为残留信号，跳过记录
                if _raw_names and _raw_names - _registered:
                    _stale = sorted(_raw_names - _registered)
                    logger.info(
                        "信号历史跳过残留策略名 {}（{} {}），不记录",
                        _stale,
                        record.get("symbol"),
                        record.get("direction"),
                    )
                    return
            except Exception:
                pass  # 校验失败不阻塞记录
            append_jsonl(path, record, max_lines=_SIGNAL_HISTORY_MAX_LINES)
    except Exception as e:
        logger.warning("记录信号历史失败: {}", e)
        # P0 watchdog：信号历史写入失败是"系统失忆"前兆，必须飞书告警
        _alert_signal_history_write_failure(signal, e)


def _alert_signal_history_write_failure(signal: dict, exc: Exception) -> None:
    """信号历史写入失败 → 飞书最高优先级告警（P0 静默失效防线）。

    异步触发不阻塞主流程；通过模块级函数隔离，避免循环依赖。
    飞书未配置/禁用时静默降级（仅日志）。

    测试环境保护：GUGU_TEST_MODE 下不真实外发（防止测试副作用泄漏到生产通知），
    仅记录 debug 日志——测试环境隔离是 P0 防线，测试本身通过 patch 断言告警触发。
    """
    import os

    if os.environ.get("GUGU_TEST_MODE"):
        logger.debug("测试模式：跳过信号历史写入失败飞书告警（仅记录日志）")
        return
    try:
        import threading

        def _do_alert() -> None:
            try:
                import asyncio

                from gugu.notifier.feishu import FeishuNotifier

                notifier = FeishuNotifier()
                if not notifier._system_error_enabled:
                    logger.warning("信号历史写入失败但系统错误告警未开启，跳过告警")
                    return
                symbol = signal.get("symbol", "?")
                direction = signal.get("direction", "?")
                msg = (
                    f"⚠️ 信号历史写入失败（P0 数据完整性）\n"
                    f"股票: {symbol} ({direction})\n"
                    f"错误: {exc}\n"
                    f"信号未落盘，绩效验证闭环将缺失该记录！"
                )
                asyncio.run(notifier.notify_error({"message": msg}))
            except Exception as e2:
                logger.warning("信号历史写入失败告警发送失败: {}", e2)

        threading.Thread(target=_do_alert, daemon=True).start()
    except Exception:
        # 告警链路本身故障不掩盖原始问题
        pass
