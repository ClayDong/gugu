"""决策层：基于 LLM + 炒股的智慧 skill 的交易决策引擎。

核心流程：
1. 加载《炒股的智慧》蒸馏的 6 个 SKILL.md 作为知识源
2. 将信号上下文 + 相关 skill 知识发送给 LLM
3. LLM 输出结构化 JSON 决策（action/position_ratio/stop_loss_price/reason）
4. LLM 不可用时降级到硬编码规则 fallback

LLM 决策优势：
- 真正理解 SKILL.md 中的交易原则（三层过滤、分层下注、止损铁律等）
- 根据具体信号上下文灵活决策，而非硬编码阈值
- 可解释：每次决策附带基于书中原则的理由
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, cast

import httpx

from gugu.config import PROJECT_ROOT, env, get_config
from gugu.models.signal import Signal
from gugu.utils.log import get_logger
from gugu.wisdom import perspective
from gugu.wisdom.book_router import BookPerspectiveRouter
from gugu.wisdom.fallback import (
    FALLBACK_ADD_RATIO,
    FALLBACK_ENTRY_MIN_CONFIDENCE,
    FALLBACK_STOP_LOSS_PCT,
    FALLBACK_TRIAL_RATIO,
    _compute_take_profit,
    _max_single_position,
    advise_fallback,
    get_strategy_win_rate,
)

logger = get_logger()

# ========== LLM 安全护栏 ==========
LLM_MAX_TRIAL_RATIO = 0.15  # LLM 首次试仓硬上限 15%
LLM_MIN_STOP_LOSS_PCT = 0.05  # LLM 止损最小幅度 5%
LLM_MAX_STOP_LOSS_PCT = 0.10  # LLM 止损最大幅度 10%

# ========== 决策对比日志 ==========
# P1-6: 记录 LLM 决策 vs 硬编码 fallback 决策的对比日志
WISDOM_COMPARISON_LOG = PROJECT_ROOT / "data" / "wisdom_comparison.jsonl"

# ========== LLM 输出校验增强 ==========
LLM_PRICE_DEVIATION_STD = 3.0
LLM_STOP_LOSS_MIN_DISTANCE = 0.02


def _wisdom_dirs() -> list[Path]:
    """返回所有可能的 skill 目录（项目内优先，外部 fallback）。"""
    wc = get_config().wisdom
    dirs: list[Path] = []

    project_dir = wc.skill_dir
    if project_dir:
        dirs.append(PROJECT_ROOT / project_dir)

    for d in wc.fallback_dirs:
        dirs.append(PROJECT_ROOT / d)

    return dirs


DECISION_SCHEMA = {
    "action": "buy|sell|hold|filter",
    "position_ratio": "0.0-1.0, 建议仓位占总资产比例",
    "stop_loss_price": "止损价（仅 buy 时必填）",
    "reason": "基于交易智慧的决策理由（1-2 句话）",
}


class WisdomAdvisor:
    """交易智慧顾问：LLM 决策 + SKILL.md 知识源。

    决策流程：
    1. 根据信号方向选择相关 skill 知识
    2. 构建包含信号上下文 + skill 知识的 prompt
    3. 调用 LLM 获取结构化决策
    4. 解析 LLM 输出并应用到信号
    5. LLM 失败时降级到硬编码规则
    """

    def __init__(self, book_router: BookPerspectiveRouter | None = None) -> None:
        self._skills: dict[str, str] = {}
        self._skill_names: list[str] = get_config().wisdom.skill_names
        self._load_skills()

        self._enable_multi_perspective = get_config().wisdom.enable_multi_perspective
        if book_router is not None:
            self._book_router = book_router
        elif self._enable_multi_perspective:
            self._book_router = BookPerspectiveRouter()
        else:
            self._book_router = None

        # async HTTP client 连接池
        self._async_client: httpx.AsyncClient | None = None

        # Circuit Breaker
        self._cb_failure_threshold = 5
        self._cb_recovery_timeout = 120.0
        self._cb_failure_count = 0
        self._cb_last_failure_time = 0.0
        self._cb_state = "closed"

        # LLM 超时与重试配置
        wc = get_config().wisdom
        self._llm_timeout = wc.llm_timeout
        self._llm_max_retries = wc.llm_max_retries
        self._llm_retry_delay = wc.llm_retry_delay

        # 重入防护
        self._in_pipeline = False

        # 检测 LLM 可用性
        cfg = env()
        self._llm_available = bool(cfg.llm_api_key and cfg.llm_base_url and cfg.llm_model)
        if self._llm_available:
            logger.info(
                "[wisdom] LLM 决策已启用: model={}, base_url={}", cfg.llm_model, cfg.llm_base_url
            )
        else:
            logger.warning(
                "[wisdom] LLM 未配置（需 .env 中设置 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL），"
                "将使用硬编码规则 fallback"
            )

    async def _get_async_client(self) -> httpx.AsyncClient:
        """获取 async HTTP client（懒加载，复用连接池）。"""
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=self._llm_timeout)
        return self._async_client

    def _cb_allow_request(self) -> bool:
        """Circuit Breaker: 判断是否允许调用LLM。"""
        now = time.monotonic()
        if self._cb_state == "open":
            if now - self._cb_last_failure_time >= self._cb_recovery_timeout:
                self._cb_state = "half_open"
                logger.info("[wisdom] CB: half-open, allowing probe request")
                return True
            return False
        return True

    def _cb_on_success(self) -> None:
        """LLM调用成功：重置失败计数，关闭熔断器。"""
        if self._cb_failure_count > 0:
            self._cb_failure_count = 0
            self._cb_state = "closed"

    def _cb_on_failure(self) -> None:
        """LLM调用失败：累加失败计数，达到阈值则熔断。"""
        self._cb_failure_count += 1
        self._cb_last_failure_time = time.monotonic()
        if self._cb_state == "half_open":
            self._cb_state = "open"
            logger.warning(
                "[wisdom] CB: probe failed, reopening circuit (failures={})", self._cb_failure_count
            )
        elif self._cb_failure_count >= self._cb_failure_threshold:
            self._cb_state = "open"
            logger.warning(
                "[wisdom] CB: circuit OPEN after {} failures, cooling down for {}s",
                self._cb_failure_count,
                self._cb_recovery_timeout,
            )

    async def close(self) -> None:
        """关闭 async HTTP client 连接池。"""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def _log_decision_comparison(
        self,
        signal: dict[str, Any],
        llm_result: dict[str, Any] | None,
        fallback_result: dict[str, Any],
        source: str,
    ) -> None:
        """记录 LLM 决策与硬编码 fallback 决策的对比日志到 wisdom_comparison.jsonl。

        Args:
            signal: 原始信号字典
            llm_result: LLM 决策结果（None 表示 LLM 不可用或失败）
            fallback_result: 硬编码 fallback 决策结果
            source: 日志来源标识（"advise" / "advise_async"）
        """
        try:
            record = {
                "timestamp": time.time(),
                "source": source,
                "symbol": signal.get("symbol", ""),
                "strategy": signal.get("strategy", ""),
                "direction": signal.get("direction", ""),
                "price": signal.get("price", 0),
                "confidence": signal.get("confidence", 0),
                "llm_decision": {
                    "action": llm_result.get("wisdom_decision", {}).get("action")
                    if llm_result
                    else None,
                    "position_ratio": llm_result.get("suggested_position_ratio", 0)
                    if llm_result
                    else None,
                    "stop_loss_price": llm_result.get("stop_loss_price") if llm_result else None,
                    "reason": llm_result.get("wisdom_decision", {}).get("llm_reason")
                    if llm_result
                    else None,
                    "filter_reason": llm_result.get("wisdom_decision", {}).get("filter_reason")
                    if llm_result
                    else None,
                }
                if llm_result
                else None,
                "fallback_decision": {
                    "action": fallback_result.get("wisdom_decision", {}).get("action"),
                    "position_ratio": fallback_result.get("suggested_position_ratio", 0),
                    "stop_loss_price": fallback_result.get("stop_loss_price"),
                    "reason": fallback_result.get("wisdom_decision", {}).get("llm_reason"),
                    "filter_reason": fallback_result.get("wisdom_decision", {}).get(
                        "filter_reason"
                    ),
                },
                "llm_available": self._llm_available,
                "llm_used": llm_result is not None,
            }

            WISDOM_COMPARISON_LOG.parent.mkdir(parents=True, exist_ok=True)
            with WISDOM_COMPARISON_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("[wisdom] 写入决策对比日志失败: {}", e)

    def _load_skills(self) -> None:
        """加载配置中指定的交易决策 skill。"""
        loaded_from: set[str] = set()
        for wisdom_dir in _wisdom_dirs():
            if not wisdom_dir.exists():
                continue
            for skill_dir in wisdom_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                skill_name = skill_dir.name
                if skill_name not in self._skill_names:
                    continue
                if skill_name in loaded_from:
                    continue
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    self._skills[skill_name] = skill_file.read_text(encoding="utf-8")
                    loaded_from.add(skill_name)

        if not self._skills:
            logger.warning(
                "未加载到任何交易智慧 skill。"
                "如需启用，请将 books2skill 的 skill 复制到 config/settings.yaml 中配置的 wisdom.skill_dir。"
            )
        logger.info(
            "[wisdom] 加载 {} 个交易智慧 skill: {}", len(self._skills), list(self._skills.keys())
        )

    @staticmethod
    def _get_strategy_win_rate(strategy_name: str) -> float | None:
        """获取策略的历史胜率（向后兼容委托到 fallback 模块）。"""
        return get_strategy_win_rate(strategy_name)

    def _inject_perspective_consensus(self, signal: dict[str, Any]) -> dict[str, Any]:
        """向后兼容委托到 perspective 模块。"""
        return perspective.inject_perspective_consensus(self._book_router, signal)

    def _build_data_driven_advice(self, signal: dict[str, Any], direction: str) -> dict[str, str]:
        """向后兼容委托到 fallback 模块。"""
        from gugu.wisdom.fallback import build_data_driven_advice

        return build_data_driven_advice(self._skills, signal, direction)

    def _get_skill_advice(self, skill_name: str) -> str:
        """向后兼容委托到 fallback 模块。"""
        from gugu.wisdom.fallback import get_skill_advice

        return get_skill_advice(self._skills, skill_name)

    def _get_skill_source(self, skill_name: str) -> str:
        """向后兼容委托到 fallback 模块。"""
        from gugu.wisdom.fallback import get_skill_source

        return get_skill_source(self._skills, skill_name)

    def _advise_fallback(self, signal: dict[str, Any]) -> dict[str, Any]:
        """向后兼容委托到 fallback 模块。"""
        from gugu.wisdom.fallback import advise_fallback

        return advise_fallback(self._skills, signal, get_strategy_win_rate)

    @staticmethod
    def _as_dict(signal: dict[str, Any] | Signal) -> dict[str, Any]:
        """将 Signal 或 dict 统一转为 dict。"""
        if isinstance(signal, Signal):
            return signal.to_dict()
        return dict(signal)

    def advise(self, signal: dict[str, Any] | Signal) -> dict[str, Any]:
        """为策略信号提供交易智慧决策（同步版）。"""
        signal = self._as_dict(signal)

        llm_result: dict[str, Any] | None = None
        fallback_result: dict[str, Any]

        if self._llm_available:
            try:
                llm_result = self._advise_llm(signal)
            except Exception as e:
                logger.warning("[wisdom] LLM 决策失败，降级到硬编码规则: {}", e)

        if llm_result is not None:
            result = llm_result
            fallback_result = advise_fallback(self._skills, signal, get_strategy_win_rate)
        else:
            result = advise_fallback(self._skills, signal, get_strategy_win_rate)
            fallback_result = result

        self._log_decision_comparison(signal, llm_result, fallback_result, "advise")
        return perspective.inject_perspective_consensus(self._book_router, result)

    async def advise_async(self, signal: dict[str, Any] | Signal) -> dict[str, Any]:
        """异步版本：为策略信号提供交易智慧决策。"""
        signal = self._as_dict(signal)

        if self._in_pipeline:
            logger.warning("[wisdom] 重入调用检测，降级到 fallback")
            fallback_result = advise_fallback(self._skills, signal, get_strategy_win_rate)
            self._log_decision_comparison(signal, None, fallback_result, "advise_async")
            return perspective.inject_perspective_consensus(self._book_router, fallback_result)
        self._in_pipeline = True

        llm_result: dict[str, Any] | None = None
        fallback_result: dict[str, Any]

        try:
            if self._llm_available and self._cb_allow_request():
                try:
                    llm_result = await self._advise_with_conflict_resolution(signal)
                    if llm_result is not None:
                        self._cb_on_success()
                except Exception as e:
                    logger.warning("[wisdom] 冲突裁决失败，降级到单LLM决策: {}", e)

                if llm_result is None:
                    try:
                        llm_result = await self._advise_llm_async(signal)
                        self._cb_on_success()
                    except Exception as e:
                        self._cb_on_failure()
                        logger.warning("[wisdom] LLM 决策失败，降级到 fallback: {}", e)
        finally:
            self._in_pipeline = False

        if llm_result is not None:
            result = llm_result
            fallback_result = advise_fallback(self._skills, signal, get_strategy_win_rate)
        else:
            result = advise_fallback(self._skills, signal, get_strategy_win_rate)
            fallback_result = result

        self._log_decision_comparison(signal, llm_result, fallback_result, "advise_async")
        return perspective.inject_perspective_consensus(self._book_router, result)

    def advise_fallback(self, signal: dict[str, Any]) -> dict[str, Any]:
        """公开的 fallback 决策接口。"""
        return advise_fallback(self._skills, signal, get_strategy_win_rate)

    async def _advise_with_conflict_resolution(
        self, signal: dict[str, Any]
    ) -> dict[str, Any] | None:
        """使用冲突裁决器进行多视角加权融合决策。"""
        from gugu.wisdom.conflict_resolver import get_conflict_resolver

        direction = signal.get("direction", "")
        scenario = (
            "entry"
            if direction == "buy"
            else "profit_taking"
            if direction == "sell"
            else "psychology_check"
        )

        resolver = get_conflict_resolver()
        if not resolver._llm_available:
            return None

        resolution = await resolver.resolve_conflict(scenario, signal, direction=direction)
        if resolution is None:
            return None

        llm_decision = {
            "action": resolution.final_action,
            "position_ratio": resolution.final_position_ratio,
            "stop_loss_price": resolution.final_stop_loss_price,
            "reason": (
                f"多视角加权裁决（{len(resolution.opinions)}视角，"
                f"冲突={'是' if resolution.is_conflict else '否'}，"
                f"分散度={resolution.dispersion:.2f}）"
            ),
        }

        signal["conflict_resolution"] = resolution.to_dict()
        signal["mentor_perspectives"] = [
            {
                "perspective": o.perspective,
                "action": o.action,
                "reason": o.reason,
                "weight": resolution.weights.get(o.perspective, 0),
            }
            for o in resolution.opinions[:2]
        ]

        return self._apply_llm_decision(signal, llm_decision)

    # ========== LLM 决策核心 ==========

    def _advise_llm(self, signal: dict[str, Any]) -> dict[str, Any]:
        """LLM 决策（同步版）。"""
        import httpx

        direction = signal.get("direction", "")
        cfg = env()

        skill_context = self._build_skill_context(direction)
        signal_context = self._build_signal_context(signal)
        book_context = self._build_book_context(signal)
        system_prompt = self._build_system_prompt()
        user_prompt = f"{signal_context}\n\n{skill_context}\n\n{book_context}"

        resp = httpx.post(
            f"{cfg.llm_base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json={
                "model": cfg.llm_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 800,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        llm_decision = self._parse_llm_response(content)
        if llm_decision is None:
            logger.warning("[wisdom] LLM 输出无法解析，降级到硬编码规则: {}", content[:200])
            return advise_fallback(self._skills, signal, get_strategy_win_rate)

        return self._apply_llm_decision(signal, llm_decision)

    async def _advise_llm_async(self, signal: dict[str, Any]) -> dict[str, Any]:
        """LLM 决策（异步版，推荐）。

        带重试逻辑：LLM 请求失败时自动重试，达到最大重试次数后降级到 fallback。
        """
        direction = signal.get("direction", "")
        cfg = env()

        skill_context = self._build_skill_context(direction)
        signal_context = self._build_signal_context(signal)
        book_context = self._build_book_context(signal)
        system_prompt = self._build_system_prompt()
        user_prompt = f"{signal_context}\n\n{skill_context}\n\n{book_context}"

        client = await self._get_async_client()
        for attempt in range(self._llm_max_retries + 1):
            try:
                resp = await client.post(
                    f"{cfg.llm_base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
                    json={
                        "model": cfg.llm_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": 800,
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]

                llm_decision = self._parse_llm_response(content)
                if llm_decision is None:
                    logger.warning(
                        "[wisdom] LLM 异步输出无法解析，降级到硬编码规则: {}", content[:200]
                    )
                    return advise_fallback(self._skills, signal, get_strategy_win_rate)

                return self._apply_llm_decision(signal, llm_decision)

            except Exception as e:
                if attempt < self._llm_max_retries:
                    logger.warning(
                        "[wisdom] LLM 请求失败 (尝试 {}/{}): {}，{}s 后重试",
                        attempt + 1,
                        self._llm_max_retries + 1,
                        e,
                        self._llm_retry_delay,
                    )
                    await asyncio.sleep(self._llm_retry_delay)
                else:
                    logger.error(
                        "[wisdom] LLM 请求失败，已达最大重试次数 ({})，降级到硬编码规则: {}",
                        self._llm_max_retries + 1,
                        e,
                    )

        return advise_fallback(self._skills, signal, get_strategy_win_rate)

    async def advise_macro_async(self, macro_summary: str, portfolio_summary: str = "") -> str:
        """生成大师兄视角的宏观市场解读（异步）。"""
        cfg = env()
        has_portfolio = bool(portfolio_summary and portfolio_summary.strip())
        system_prompt = (
            "你是「大师兄」——一位融合实战经验与宏观洞察的资深市场分析师。\n"
            "你的认知来自几十年实业和投资实战经验，核心框架是「五维宏观」：\n"
            "金（黄金）、油（石油）、汇（汇率）、债（国债收益率）、G（指数）。\n"
            "你相信「长期主义」和「留一口」的投资哲学。\n\n"
            "你的职责是根据当日宏观数据，输出一段有结构、有深度、可操作的大师视角解读。\n\n"
            "你必须用五维宏观框架来分析：先判断五个维度的综合方向，\n"
            "再解读资金流向和板块轮动背后的宏观逻辑，最后给出操作建议。\n\n"
            "输出格式（必须严格按以下 3 节输出，每节标题用###）：\n"
            "### 1. 五维宏观速览\n"
            "- 分别点评金/油/汇/债/G 五个维度的当日变化，用 1-2 句话说明每个维度的信号含义；\n"
            "- 给出综合判断：五维整体方向是（宽松/收紧/中性/分化）。\n"
            "### 2. 板块与资金逻辑\n"
            "- 结合资金流向判断主力在买什么/卖什么，市场风格及持续性；\n"
            "- 指出是否有板块轮动信号，哪些板块在吸筹、哪些在出货。\n"
            "### 3. 明日推演与操作建议\n"
            "- 2-3 个明日盘面关键观察点；\n"
            "- 具体仓位建议，必须说明止损纪律。\n"
        )
        if has_portfolio:
            system_prompt += (
                "### 4. 对你持仓的影响\n"
                "- 逐一点评持仓标的与当前宏观逻辑的共振/错配；\n"
                "- 给出针对每只票的具体调仓/止损/加仓建议，必须具体到盈亏和仓位。\n"
            )
        system_prompt += (
            "\n写作要求：\n"
            "- 总字数 500-800 字，每节必须有完整因果链，禁止流水账；\n"
            "- 拒绝空话套话，每句话都要传递增量信息；\n"
            "- 使用专业术语但要给出散户能懂的解释；\n"
            "- 不要重复数据，要分析数据背后的逻辑；\n"
            "- 语气像有经验的老交易员给同门师弟妹讲课。\n"
        )
        user_prompt = f"## 今日宏观数据\n\n{macro_summary}"
        if has_portfolio:
            user_prompt += f"\n\n## 我的当前持仓\n\n{portfolio_summary}"

        try:
            client = await self._get_async_client()
            resp = await client.post(
                f"{cfg.llm_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
                json={
                    "model": cfg.llm_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": 2500,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            resp_data = resp.json()
            content = resp_data["choices"][0]["message"]["content"]
            finish_reason = resp_data["choices"][0].get("finish_reason", "unknown")
            if finish_reason == "length":
                logger.warning(
                    "[wisdom-LLM] 大师兄宏观解读被 max_tokens 截断 "
                    "(已生成 {} 字，finish_reason=length)，内容可能不完整",
                    len(content),
                )
            else:
                logger.info(
                    "[wisdom-LLM] 大师兄宏观解读生成成功: {} 字 (finish_reason={})",
                    len(content),
                    finish_reason,
                )
            return content.strip()
        except Exception as e:
            logger.warning("[wisdom-LLM] 大师兄宏观解读失败: {}", e)
            return ""

    async def advise_portfolio_async(
        self,
        positions: list[dict[str, Any]],
        market_summary: str = "",
        macro_summary: str = "",
    ) -> str:
        """基于用户真实持仓生成个性化认知分析与建议（异步）。"""
        cfg = env()
        if not positions:
            return "当前无持仓，无需分析。"

        pos_lines = []
        for p in positions:
            profit_pct = p.get("profit_pct", 0)
            emoji = (
                "🔴"
                if profit_pct < -5
                else "🟡"
                if profit_pct < 0
                else "🟢"
                if profit_pct < 10
                else "🔥"
            )
            pos_lines.append(
                f"- {p.get('name', p.get('symbol', '?'))} ({p.get('symbol', '')}) | "
                f"成本 {p.get('entry_price', 0):.2f} | "
                f"现价 {p.get('current_price', 0):.2f} | "
                f"盈亏 {profit_pct:+.1f}% {emoji} | "
                f"市值 {p.get('market_value', 0):.1f} 元"
            )
        portfolio_text = "\n".join(pos_lines)

        system_prompt = (
            "你是「大师兄」——融合28本投资经典智慧的个性化持仓顾问。\n"
            "你的核心框架是三位一体：\n"
            "1. 技术面（趋势/均线/量价关系）\n"
            "2. 基本面（行业/估值/财务健康）\n"
            "3. 宏观面（五维：金油汇债G + 市场温度）\n\n"
            "你的职责是根据用户的真实持仓数据，输出一份个性化持仓分析报告。\n"
            "输出格式：\n"
            "### 1. 持仓健康度总评\n"
            "- 逐一评价每只持仓的当前状态，给出整体组合评分\n"
            "### 2. 风险信号（如有）\n"
            "- 逐只指出：止损是否触发、是否破位、是否趋势走坏\n"
            "- 给出具体建议：减仓/止损/持有观望\n"
            "### 3. 调仓机会（如有）\n"
            "- 如果某只持仓已大幅盈利，是否需要落袋为安\n"
            "- 如果某只持仓亏损但逻辑仍在，是否值得加仓做低成本\n"
            "- 如果当前仓位过重，是否需要降低风险暴露\n"
            "### 4. 明日操作计划\n"
            "- 开盘前该关注什么关键价位\n"
            "- 每只票的止损价应该定在哪里\n\n"
            "写作要求：\n"
            "- 总字数 400-800 字\n"
            "- 每条建议必须包含因果链：因为 X，所以建议 Y，预期结果 Z\n"
            "- 语气务实，像有经验的老交易员在给同门分析持仓\n"
            "- 拒绝空话套话\n"
        )
        user_prompt = f"## 我的持仓\n\n{portfolio_text}"
        if market_summary:
            user_prompt += f"\n\n## 当日市场概况\n\n{market_summary}"
        if macro_summary:
            user_prompt += f"\n\n## 宏观数据参考\n\n{macro_summary}"

        try:
            client = await self._get_async_client()
            resp = await client.post(
                f"{cfg.llm_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
                json={
                    "model": cfg.llm_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": 2500,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            resp_data = resp.json()
            content = resp_data["choices"][0]["message"]["content"]
            logger.info("[wisdom-LLM] 持仓认知报告生成成功: {} 字", len(content))
            return content.strip()
        except Exception as e:
            logger.warning("[wisdom-LLM] 持仓认知报告生成失败: {}", e)
            return ""

    def _build_book_context(self, signal: dict[str, Any]) -> str:
        """构建多视角书籍认知上下文。"""
        direction = signal.get("direction", "")
        scenario = (
            "entry"
            if direction == "buy"
            else "profit_taking"
            if direction == "sell"
            else "psychology_check"
        )
        atr = signal.get("atr", 0)
        price = signal.get("price", 0)
        market_vol = (atr / price) if (atr > 0 and price > 0) else None

        book_context = ""
        if self._book_router is not None:
            book_context = self._book_router.build_context(scenario, direction, signal)
            if market_vol is not None:
                dyn_perspectives = self._book_router.select_perspectives_dynamic(
                    scenario, direction, market_volatility=market_vol, signal=signal
                )
                if dyn_perspectives:
                    book_context += (
                        f"\n（波动率={market_vol:.2%}，动态选择视角: {dyn_perspectives[:2]}）"
                    )
        return book_context

    def _build_system_prompt(self) -> str:
        """构建 LLM 系统提示词。"""
        return (
            "你是一位融合28本股书智慧的交易决策顾问。\n"
            "你的认知来自陈江挺《炒股的智慧》、利弗莫尔《股票大作手回忆录》、\n"
            "海龟交易法则、范·撒普《通向财务自由之路》、邱国鹭《投资中最简单的事》等经典。\n"
            "你的职责是根据策略信号和多视角交易智慧，给出结构化的交易决策。\n\n"
            "你必须输出 JSON 格式，包含以下字段：\n"
            '- action: "buy" | "sell" | "hold" | "filter"\n'
            "  - buy: 建议买入\n"
            "  - sell: 建议卖出\n"
            "  - hold: 维持持仓不变\n"
            "  - filter: 过滤该信号，不建议操作\n"
            "- position_ratio: 建议仓位占总资产的比例（0.0-1.0），filter 时设为 0\n"
            "- stop_loss_price: 止损价（仅 buy 时必填，其他方向可为 null）\n"
            "- reason: 基于交易智慧的决策理由（1-2 句话）\n\n"
            "决策原则：\n"
            "1. 策略信号已经过技术面筛选，你的职责是评估风险和仓位，而非重复技术判断\n"
            "2. 仓位管理遵循分层下注：首次试仓用小仓位（5-10%），验证趋势后加码\n"
            "3. 信息不足时降低仓位而非否决——用试仓代替观望，用小仓位控制风险\n"
            "4. 止损是最高行为准则：入场前必须预设止损价（通常-5%到-10%），到点必执行\n"
            "5. 让利润奔跑：浮盈时用移动止损代替固定止盈，不要贪小便宜\n"
            "6. A股特殊规则：T+1、涨跌停板、单股仓位不超过30%\n"
            "7. 只在以下情况使用 filter：ST股、停牌股、涨跌停无法成交、或信号置信度极低（<0.3）\n"
        )

    def _build_signal_context(self, signal: dict[str, Any]) -> str:
        """构建信号上下文描述。"""
        direction = signal.get("direction", "")
        lines = [
            "## 当前交易信号",
            f"- 股票代码: {signal.get('symbol', '')}",
            f"- 股票名称: {signal.get('name', '')}",
            f"- 信号方向: {'买入' if direction == 'buy' else '卖出' if direction == 'sell' else direction}",
            f"- 触发策略: {signal.get('strategy', '')} ({', '.join(signal.get('strategies', []))})",
            f"- 信号理由: {signal.get('reason', '')}",
            f"- 当前价格: {signal.get('price', 0):.2f}",
            f"- 信号置信度: {signal.get('confidence', 0):.2f}",
            f"- 建议仓位比例: {signal.get('suggested_position_ratio', 0):.2%}",
        ]

        if direction == "buy":
            has_position = signal.get("has_position", False)
            current_ratio = signal.get("current_position_ratio", 0)
            lines.append(f"- 是否已有持仓: {'是' if has_position else '否'}")
            if has_position:
                lines.append(f"- 现有持仓占比: {current_ratio:.2%}")
            if signal.get("prev_close"):
                lines.append(f"- 昨收价: {signal['prev_close']:.2f}")
            if signal.get("is_st"):
                lines.append("- ⚠️ ST 股票")
            if signal.get("is_suspended"):
                lines.append("- ⚠️ 停牌中")
        elif direction == "sell":
            if signal.get("stop_loss_price"):
                lines.append(f"- 预设止损价: {signal['stop_loss_price']:.2f}")

        market_ctx = signal.get("market_context", {})
        if market_ctx:
            lines.append(f"- 数据源状态: {market_ctx.get('data_source', 'unknown')}")
            lines.append(f"- 当前持仓数: {market_ctx.get('portfolio_count', 0)}")
            lines.append(f"- 现金占比: {market_ctx.get('cash_ratio', 1.0):.1%}")

        return "\n".join(lines)

    def _build_skill_context(self, direction: str) -> str:
        """根据信号方向选择相关 skill 知识。"""
        buy_skills = ["stock-entry-decision", "stock-stop-loss-decision", "stock-position-sizing"]
        sell_skills = ["stock-profit-taking-decision", "stock-trailing-stop"]
        common_skills = ["stock-psychology-check"]

        if direction == "buy":
            relevant = buy_skills + common_skills
        elif direction == "sell":
            relevant = sell_skills + common_skills
        else:
            relevant = common_skills

        parts = ["## 交易智慧参考（来自《炒股的智慧》）\n"]
        for skill_name in relevant:
            content = self._skills.get(skill_name, "")
            if content:
                extracted = self._extract_decision_rules(content)
                if extracted:
                    parts.append(f"### {skill_name}\n{extracted}\n")

        return "\n".join(parts)

    def _extract_decision_rules(self, content: str) -> str:
        """从 SKILL.md 中提取决策相关的核心内容。"""
        lines = content.split("\n")
        result_lines: list[str] = []
        in_section = False

        for line in lines:
            if line.startswith("## "):
                if (
                    "I —" in line
                    or "I—" in line
                    or "方法论" in line
                    or "E —" in line
                    or "E—" in line
                    or "执行步骤" in line
                    or "B —" in line
                    or "B—" in line
                    or "边界" in line
                ):
                    in_section = True
                    result_lines.append(line)
                else:
                    in_section = False
                continue

            if in_section:
                result_lines.append(line)
            if len("\n".join(result_lines)) > 2000:
                break

        return "\n".join(result_lines)

    def _parse_llm_response(self, content: str) -> dict[str, Any] | None:
        """解析 LLM 输出的 JSON 决策。"""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            import re

            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                except json.JSONDecodeError:
                    return None
            else:
                return None

        if "action" not in data:
            return None
        if data["action"] not in ("buy", "sell", "hold", "filter"):
            return None

        return data

    def _apply_llm_decision(
        self, signal: dict[str, Any], llm_decision: dict[str, Any]
    ) -> dict[str, Any]:
        """将 LLM 决策应用到信号（安全墙）。"""
        action = llm_decision["action"]
        reason = llm_decision.get("reason", "")
        position_ratio = float(llm_decision.get("position_ratio", 0))
        stop_loss_price = llm_decision.get("stop_loss_price")

        # 安全墙：校验 action 与 direction 的兼容性
        direction = signal.get("direction", "")
        if direction == "buy" and action not in ("buy", "hold", "filter"):
            _original_action = action
            logger.warning(
                "[wisdom-LLM] {} 安全墙: 买入信号被 LLM 输出非买入 action={}，强制转为 filter",
                signal.get("symbol", ""),
                _original_action,
            )
            action = "filter"
            reason = f"安全墙: 买入信号不兼容 LLM action={_original_action}"
        elif direction == "sell" and action not in ("sell", "hold", "filter"):
            _original_action = action
            logger.warning(
                "[wisdom-LLM] {} 安全墙: 卖出信号被 LLM 输出非卖出 action={}，强制转为 filter",
                signal.get("symbol", ""),
                _original_action,
            )
            action = "filter"
            reason = f"安全墙: 卖出信号不兼容 LLM action={_original_action}"

        # 安全墙：校验 position_ratio 值域
        if not (0.0 <= position_ratio <= 1.0):
            logger.warning(
                "[wisdom-LLM] {} 安全墙: position_ratio={} 超出 0-1 范围，截断为 0",
                signal.get("symbol", ""),
                position_ratio,
            )
            position_ratio = max(0.0, min(1.0, position_ratio))

        # 安全墙：校验止损价位置
        price = signal.get("price", 0)
        if stop_loss_price is not None and float(stop_loss_price) > 0 and price > 0:
            sl = float(stop_loss_price)
            if sl >= price * (1 - LLM_STOP_LOSS_MIN_DISTANCE):
                logger.warning(
                    "[wisdom-LLM] {} 安全墙: 止损价 {:.2f} 距离当前价 {:.2f} 不足 2%，已修正",
                    signal.get("symbol", ""),
                    sl,
                    price,
                )
                stop_loss_price = round(price * (1 - LLM_STOP_LOSS_MIN_DISTANCE), 2)

        decision: dict[str, Any] = {
            "source": "llm",
            "action": action,
            "llm_reason": reason,
        }
        advice: dict[str, str] = {
            "llm_decision": f"LLM 建议: {action} — {reason}",
        }

        max_ratio_val = _max_single_position()

        if action == "filter":
            signal["wisdom_filtered"] = True
            decision["filter_reason"] = reason
            signal["suggested_position_ratio"] = 0.0
            logger.info("[wisdom-LLM] {} 过滤: {}", signal.get("symbol", ""), reason)

        elif action == "hold":
            signal["wisdom_filtered"] = True
            decision["filter_reason"] = f"建议持有观望: {reason}"
            signal["suggested_position_ratio"] = 0.0
            logger.info("[wisdom-LLM] {} 建议持有: {}", signal.get("symbol", ""), reason)

        elif direction == "buy" and action == "buy":
            current_ratio = float(signal.get("current_position_ratio", 0))
            remaining = max_ratio_val - current_ratio
            safe_ratio = min(position_ratio, remaining, max_ratio_val)
            safe_ratio = max(0.0, safe_ratio)

            is_trial = current_ratio <= 0
            if is_trial and safe_ratio > LLM_MAX_TRIAL_RATIO:
                logger.warning(
                    "[wisdom-LLM] {} 仓位护栏: LLM建议 {:.2%} > 试仓上限 {:.2%}，已截断",
                    signal.get("symbol", ""),
                    safe_ratio,
                    LLM_MAX_TRIAL_RATIO,
                )
                safe_ratio = LLM_MAX_TRIAL_RATIO

            if safe_ratio <= 0:
                signal["wisdom_filtered"] = True
                decision["filter_reason"] = (
                    f"现有持仓占比 {current_ratio:.2%} 已达上限 {max_ratio_val:.2%}，不再加仓"
                )
                signal["suggested_position_ratio"] = 0.0
                logger.info(
                    "[wisdom-LLM] {} 加仓过滤: 现有占比 {:.2%} >= 上限 {:.2%}",
                    signal.get("symbol", ""),
                    current_ratio,
                    max_ratio_val,
                )
            else:
                original_ratio = signal.get("suggested_position_ratio", 0)
                signal["suggested_position_ratio"] = safe_ratio
                decision["position_strategy"] = "add" if current_ratio > 0 else "trial"
                logger.info(
                    "[wisdom-LLM] {} 买入确认: 仓位 {:.2%} → {:.2%}, 理由: {}",
                    signal.get("symbol", ""),
                    original_ratio,
                    safe_ratio,
                    reason,
                )

            # 止损价安全护栏
            if stop_loss_price and float(stop_loss_price) > 0:
                sl = float(stop_loss_price)
                if price > 0:
                    sl_pct = (price - sl) / price
                    if sl_pct < LLM_MIN_STOP_LOSS_PCT:
                        sl = round(price * (1 - LLM_MIN_STOP_LOSS_PCT), 2)
                        logger.warning(
                            "[wisdom-LLM] {} 止损护栏: LLM止损幅度 {:.2%} < {:.2%}，调整到 {:.2f}",
                            signal.get("symbol", ""),
                            sl_pct,
                            LLM_MIN_STOP_LOSS_PCT,
                            sl,
                        )
                    elif sl_pct > LLM_MAX_STOP_LOSS_PCT:
                        sl = round(price * (1 - LLM_MAX_STOP_LOSS_PCT), 2)
                        logger.warning(
                            "[wisdom-LLM] {} 止损护栏: LLM止损幅度 {:.2%} > {:.2%}，调整到 {:.2f}",
                            signal.get("symbol", ""),
                            sl_pct,
                            LLM_MAX_STOP_LOSS_PCT,
                            sl,
                        )
                signal["stop_loss_price"] = round(sl, 2)
                decision["stop_loss_price"] = round(sl, 2)
            elif price > 0:
                default_stop = price * (1 - FALLBACK_STOP_LOSS_PCT)
                signal["stop_loss_price"] = round(default_stop, 2)
                decision["stop_loss_price"] = round(default_stop, 2)
                decision["stop_loss_source"] = "fallback"

            # 止盈方案
            if price > 0:
                tp = _compute_take_profit(price, signal.get("atr", 0))
                signal["take_profit"] = tp
                decision["take_profit"] = tp

        elif direction == "sell" and action == "sell":
            decision["sell_reason"] = reason
            logger.info("[wisdom-LLM] {} 卖出确认: {}", signal.get("symbol", ""), reason)

        elif direction == "buy" and action == "sell":
            signal["wisdom_filtered"] = True
            decision["filter_reason"] = f"LLM 建议卖出而非买入: {reason}"
            signal["suggested_position_ratio"] = 0.0
            logger.info("[wisdom-LLM] {} 买入被否决: {}", signal.get("symbol", ""), reason)

        elif direction == "sell" and action == "buy":
            signal["wisdom_filtered"] = True
            decision["filter_reason"] = f"LLM 建议持有而非卖出: {reason}"
            logger.info("[wisdom-LLM] {} 卖出被否决: {}", signal.get("symbol", ""), reason)

        else:
            if position_ratio > 0:
                signal["suggested_position_ratio"] = min(position_ratio, max_ratio_val)
            decision["action_detail"] = f"direction={direction}, llm_action={action}"

        signal["wisdom"] = advice
        signal["wisdom_decision"] = decision
        return signal

    def llm_interpret(self, signal: dict[str, Any]) -> str | None:
        """用 LLM 做自然语言解读。"""
        cfg = env()
        if not cfg.llm_api_key:
            return None

        try:
            import httpx

            prompt = self._build_interpret_prompt(signal)
            resp = httpx.post(
                f"{cfg.llm_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
                json={
                    "model": cfg.llm_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是交易决策助手，基于交易智慧给出简洁的买卖建议。",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 500,
                    "temperature": 0.3,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return cast(str | None, resp.json()["choices"][0]["message"]["content"])
        except Exception as e:
            logger.warning("[wisdom] LLM 解读失败: {}", e)
            return None

    @staticmethod
    def _build_interpret_prompt(signal: dict[str, Any]) -> str:
        """构建 LLM 自然语言解读 prompt。"""
        lines = [
            f"股票: {signal.get('symbol', '')} {signal.get('name', '')}",
            f"方向: {signal.get('direction', '')}",
            f"触发策略: {signal.get('strategy', '')}",
            f"触发理由: {signal.get('reason', '')}",
            f"当前价: {signal.get('price', '')}",
        ]
        wisdom_decision = signal.get("wisdom_decision", {})
        if wisdom_decision:
            lines.append(f"\n智慧决策: {wisdom_decision.get('action', '')}")
            lines.append(f"决策理由: {wisdom_decision.get('llm_reason', '')}")
            if wisdom_decision.get("stop_loss_price"):
                lines.append(f"止损价: {wisdom_decision['stop_loss_price']}")
        lines.append("\n请用简洁的中文给出交易建议（100字内）：")
        return "\n".join(lines)
