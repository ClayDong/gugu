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

import json
from pathlib import Path
from typing import Any, cast

import httpx

from gugu.config import PROJECT_ROOT, env, get_config
from gugu.models.signal import Signal
from gugu.utils.log import get_logger
from gugu.wisdom.book_router import BookPerspectiveRouter

logger = get_logger()

# ========== 硬编码 fallback 常量（LLM 不可用时使用） ==========
# 来源：stock-position-sizing / stock-stop-loss-decision / stock-entry-decision

FALLBACK_TRIAL_RATIO = 0.20  # 试仓比例 20%
FALLBACK_ADD_RATIO = 0.40  # 加码比例 40%
FALLBACK_STOP_LOSS_PCT = 0.08  # 默认止损比例 8%
FALLBACK_ENTRY_MIN_CONFIDENCE = 0.6  # 最低入场置信度
FALLBACK_TAKE_PROFIT_T1 = 0.10  # 第一止盈目标 +10%
FALLBACK_TAKE_PROFIT_T2 = 0.20  # 第二止盈目标 +20%

# ========== LLM 安全护栏 ==========
LLM_MAX_TRIAL_RATIO = 0.15  # LLM 首次试仓硬上限 15%
LLM_MIN_STOP_LOSS_PCT = 0.05  # LLM 止损最小幅度 5%
LLM_MAX_STOP_LOSS_PCT = 0.10  # LLM 止损最大幅度 10%

# ========== LLM 输出校验增强 ==========
# 价格合理性: 拒绝价格偏离市场均价超过 N 倍标准差
LLM_PRICE_DEVIATION_STD = 3.0
# 仓位一致性: LLM 建议仓位与信号方向不一致时拒绝
# 止损价位置: 止损价必须低于当前价 >= 2%（买入时）
LLM_STOP_LOSS_MIN_DISTANCE = 0.02


def _max_single_position() -> float:
    """从配置读取单股最大仓位上限，与风控 L1 保持一致。"""
    return get_config().risk.max_position_ratio


def _compute_take_profit(entry_price: float, atr: float = 0) -> dict:
    """计算止盈方案：第一目标 +10%、第二目标 +20%。

    若有 ATR 数据，用 ATR 校准止盈幅度；
    若无 ATR 则用固定百分比 fallback。

    Returns:
        {"target_1": float, "target_1_pct": float,
         "target_2": float, "target_2_pct": float,
         "reduce_at_target1": str, "note": str}
    """
    tp1_pct = FALLBACK_TAKE_PROFIT_T1
    tp2_pct = FALLBACK_TAKE_PROFIT_T2

    # ATR 校准：若 ATR/price > 3%，目标上调；若 < 1%，目标下调
    if atr > 0 and entry_price > 0:
        atr_ratio = atr / entry_price
        if atr_ratio > 0.03:
            tp1_pct = min(0.15, tp1_pct * 1.5)
            tp2_pct = min(0.30, tp2_pct * 1.5)
        elif atr_ratio < 0.01:
            tp1_pct = max(0.05, tp1_pct * 0.5)
            tp2_pct = max(0.10, tp2_pct * 0.5)

    return {
        "target_1": round(entry_price * (1 + tp1_pct), 2),
        "target_1_pct": tp1_pct,
        "target_2": round(entry_price * (1 + tp2_pct), 2),
        "target_2_pct": tp2_pct,
        "reduce_at_target1": "50%",
        "note": f"止损 {FALLBACK_STOP_LOSS_PCT:.0%} / 止盈 {tp1_pct:.0%}-{tp2_pct:.0%}，盈亏比 1:{tp2_pct / FALLBACK_STOP_LOSS_PCT:.1f}",
    }


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


# LLM 决策输出的 JSON Schema
_DECISION_SCHEMA = {
    "action": "buy|sell|hold|filter",
    "position_ratio": "0.0-1.0, 建议仓位占总资产比例",
    "stop_loss_price": "止损价（仅 buy 时必填）",
    "reason": "基于交易智慧的决策理由（1-2 句话）",
}


def _is_profitable_signal(signal_record: dict) -> bool:
    """根据信号记录判断是否为盈利信号。

    通过 order_result 中的成交价和成交量判断（简化版）：
    若 signal_record 中有 order_success=True，并且信号方向为 'buy'，
    在信号历史中查找后续卖出记录或根据 order_result 估算。

    简化实现：基于信号的建议方向和是否被 wisdom_filtered 判断。
    精确实现需要追踪每笔交易的实际平仓盈亏。

    Args:
        signal_record: 信号历史记录 dict

    Returns:
        是否盈利（简化版：buy 信号通常假设为盈利方向）
    """
    # 简化版：记录有 order_success=True 且 direction='buy' 的信号视为待验证的正向信号
    # 实际胜率依赖 AttributionAnalyzer 的精确追踪
    direction = signal_record.get("direction", "")
    success = signal_record.get("order_success", False)
    if not success:
        return False
    # 简化：buy 信号 + order_success 视为预期盈利
    # TODO: 接入 AttributionAnalyzer 追踪实际平仓盈亏
    return direction == "buy"


class WisdomAdvisor:
    """交易智慧顾问：LLM 决策 + SKILL.md 知识源。

    决策流程：
    1. 根据信号方向选择相关 skill 知识
    2. 构建包含信号上下文 + skill 知识的 prompt
    3. 调用 LLM 获取结构化决策
    4. 解析 LLM 输出并应用到信号
    5. LLM 失败时降级到硬编码规则

    Async 支持：
    - advise() 同步方法（向后兼容）
    - advise_async() 异步方法（推荐，避免阻塞事件循环）
    - _advise_llm_async() 使用 AsyncClient 调用 LLM
    """

    def __init__(self, book_router: BookPerspectiveRouter | None = None) -> None:
        self._skills: dict[str, str] = {}
        self._skill_names: list[str] = get_config().wisdom.skill_names
        self._load_skills()

        # 加载仓颉蒸馏的多视角书籍认知
        # P0 修复：根据 enable_multi_perspective 开关决定是否启用多视角注入
        # 原逻辑无条件创建 BookPerspectiveRouter，导致 settings.yaml 的
        # enable_multi_perspective: false 配置完全不生效
        self._enable_multi_perspective = get_config().wisdom.enable_multi_perspective
        if book_router is not None:
            self._book_router = book_router
        elif self._enable_multi_perspective:
            self._book_router = BookPerspectiveRouter()
        else:
            self._book_router = None

        # async HTTP client 连接池（复用，避免每次创建新连接）
        self._async_client: httpx.AsyncClient | None = None

        # 检测 LLM 可用性
        cfg = env()
        self._llm_available = bool(cfg.llm_api_key and cfg.llm_base_url and cfg.llm_model)
        if self._llm_available:
            logger.info(
                f"[wisdom] LLM 决策已启用: model={cfg.llm_model}, base_url={cfg.llm_base_url}"
            )
        else:
            logger.warning(
                "[wisdom] LLM 未配置（需 .env 中设置 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL），"
                "将使用硬编码规则 fallback"
            )

    async def _get_async_client(self) -> httpx.AsyncClient:
        """获取 async HTTP client（懒加载，复用连接池）。"""
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=30.0)
        return self._async_client

    async def close(self) -> None:
        """关闭 async HTTP client 连接池。"""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    # ========== 多视角共识注入 ==========

    def _inject_perspective_consensus(self, signal: dict[str, Any]) -> dict[str, Any]:
        """将多视角分析结果注入信号。

        调用 book_router.get_advice(signal) 获取多视角分析，
        计算共识指标并更新 mentor_perspectives 字段。
        高共识增强置信度，低共识降低置信度。

        Args:
            signal: 信号 dict（需包含 direction, confidence 等字段）

        Returns:
            更新后的信号 dict（含 mentor_perspectives 字段）
        """
        if self._book_router is None:
            return signal

        try:
            advice = self._book_router.get_advice(signal)
            perspectives = advice.get("perspectives", [])
            if not perspectives:
                return signal

            consensus = advice.get("consensus", {})
            buy_pct = consensus.get("buy_pct", 0)
            sell_pct = consensus.get("sell_pct", 0)
            hold_pct = consensus.get("hold_pct", 0)

            # 分类视角
            buy_perspectives: list[str] = []
            sell_perspectives: list[str] = []
            hold_perspectives: list[str] = []
            top_reasons: list[dict] = []

            for p in perspectives:
                action = p.get("action", "")
                name = p.get("name", "")
                reason = p.get("reason", "")
                if action == "buy":
                    buy_perspectives.append(name)
                elif action == "sell":
                    sell_perspectives.append(name)
                else:
                    hold_perspectives.append(name)

                # 收集置信度最高的视角理由
                top_reasons.append({
                    "perspective": name,
                    "reason": reason,
                })

            # 取 top-3 理由
            top_reasons = sorted(
                top_reasons,
                key=lambda x: next(
                    (p.get("confidence", 0) for p in perspectives if p.get("name") == x["perspective"]),
                    0,
                ),
                reverse=True,
            )[:3]

            # 确定共识标签
            if buy_pct >= 0.7:
                consensus_label = "strong_buy"
            elif buy_pct >= 0.5 and buy_pct > sell_pct:
                consensus_label = "buy"
            elif sell_pct >= 0.7:
                consensus_label = "strong_sell"
            elif sell_pct >= 0.5 and sell_pct > buy_pct:
                consensus_label = "sell"
            elif abs(buy_pct - sell_pct) < 0.2 and buy_pct > 0 and sell_pct > 0:
                consensus_label = "conflict"
            else:
                consensus_label = "conflict"

            mentor_perspectives = {
                "analyzed_count": len(perspectives),
                "buy_agreement_pct": buy_pct,
                "sell_agreement_pct": sell_pct,
                "hold_agreement_pct": hold_pct,
                "buy_perspectives": buy_perspectives,
                "sell_perspectives": sell_perspectives,
                "hold_perspectives": hold_perspectives,
                "top_reasons": top_reasons,
                "consensus": consensus_label,
            }

            signal["mentor_perspectives"] = mentor_perspectives

            # 高共识（>70%同意买入）→ 增强置信度
            if buy_pct > 0.70:
                current_conf = float(signal.get("confidence", 0.5))
                signal["confidence"] = min(1.0, current_conf + 0.1)
                logger.info(
                    f"[wisdom-perspective] 高共识({buy_pct:.0%}买入)增强置信度: "
                    f"{current_conf:.2f} → {signal['confidence']:.2f}"
                )
            # 低共识（<30%同意买入）→ 降低置信度
            elif buy_pct < 0.30 and signal.get("direction") == "buy":
                current_conf = float(signal.get("confidence", 0.5))
                signal["confidence"] = max(0.1, current_conf - 0.1)
                logger.info(
                    f"[wisdom-perspective] 低共识({buy_pct:.0%}买入)降低置信度: "
                    f"{current_conf:.2f} → {signal['confidence']:.2f}"
                )

        except Exception as e:
            logger.warning(f"[wisdom-perspective] 多视角分析异常，跳过: {e}")

        return signal

    def _load_skills(self) -> None:
        """加载配置中指定的交易决策 skill（按配置目录顺序，后加载的不覆盖先加载的）。

        只加载 settings.yaml 中 wisdom.skill_names 列出的 skill，
        忽略目录中存在但未配置的 skill。
        """
        loaded_from: set[str] = set()
        for wisdom_dir in _wisdom_dirs():
            if not wisdom_dir.exists():
                continue
            for skill_dir in wisdom_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                skill_name = skill_dir.name
                # 只加载配置中指定的 skill
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
            f"[wisdom] 加载 {len(self._skills)} 个交易智慧 skill: {list(self._skills.keys())}"
        )

    @staticmethod
    def _as_dict(signal: dict[str, Any] | Signal) -> dict[str, Any]:
        """将 Signal 或 dict 统一转为 dict（兼容新旧接口）。"""
        if isinstance(signal, Signal):
            return signal.to_dict()
        return dict(signal)

    def advise(self, signal: dict[str, Any] | Signal) -> dict[str, Any]:
        """为策略信号提供交易智慧决策。

        优先使用 LLM 决策，LLM 不可用时降级到硬编码规则。
        同时接受 Signal 数据类和 dict 作为输入。

        决策完成后注入多视角共识分析。

        Args:
            signal: 策略信号，Signal 数据类或 dict

        Returns:
            增强信号（dict 格式）。若输入为 Signal，可转为 Signal.from_dict(result) 恢复。
        """
        signal = self._as_dict(signal)

        if self._llm_available:
            try:
                result = self._advise_llm(signal)
                return self._inject_perspective_consensus(result)
            except Exception as e:
                logger.warning(f"[wisdom] LLM 决策失败，降级到硬编码规则: {e}")

        result = self._advise_fallback(signal)
        return self._inject_perspective_consensus(result)

    async def advise_async(self, signal: dict[str, Any] | Signal) -> dict[str, Any]:
        """异步版本：为策略信号提供交易智慧决策。

        与 advise() 行为完全一致，但 LLM 调用不阻塞事件循环。
        推荐 SignalPipeline 等异步代码路径使用此方法。

        优先使用冲突裁决器（多维权重融合），失败时降级到单LLM决策，
        再失败降级到硬编码规则。
        所有路径完成后注入多视角共识分析。
        """
        signal = self._as_dict(signal)

        if self._llm_available:
            # 优先尝试冲突裁决器
            try:
                result = await self._advise_with_conflict_resolution(signal)
                if result is not None:
                    return self._inject_perspective_consensus(result)
            except Exception as e:
                logger.warning(f"[wisdom] 冲突裁决失败，降级到单LLM决策: {e}")

            # 降级到单 LLM 决策
            try:
                result = await self._advise_llm_async(signal)
                return self._inject_perspective_consensus(result)
            except Exception as e:
                logger.warning(f"[wisdom] LLM 异步决策失败，降级到硬编码规则: {e}")

        result = self._advise_fallback(signal)
        return self._inject_perspective_consensus(result)

    async def _advise_with_conflict_resolution(
        self, signal: dict[str, Any]
    ) -> dict[str, Any] | None:
        """使用冲突裁决器进行多视角加权融合决策。

        Returns:
            增强后的信号 dict，或 None（裁决器不可用时）
        """
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

        # 将裁决结果应用到信号（复用 _apply_llm_decision 的逻辑）
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

        # 附加裁决详情到信号（供通知层展示"导师视角"栏）
        signal["conflict_resolution"] = resolution.to_dict()
        signal["mentor_perspectives"] = [
            {
                "perspective": o.perspective,
                "action": o.action,
                "reason": o.reason,
                "weight": resolution.weights.get(o.perspective, 0),
            }
            for o in resolution.opinions[:2]  # 仅取 top-2 供通知展示
        ]

        return self._apply_llm_decision(signal, llm_decision)

    # ========== LLM 决策核心 ==========

    def _advise_llm(self, signal: dict[str, Any]) -> dict[str, Any]:
        """LLM 决策（同步版，向后兼容）：将信号上下文 + skill 知识发送给 LLM。"""
        import httpx

        direction = signal.get("direction", "")
        cfg = env()

        # 1. 选择相关 skill 知识
        skill_context = self._build_skill_context(direction)

        # 2. 构建信号上下文
        signal_context = self._build_signal_context(signal)

        # 2.5 构建多视角书籍认知上下文
        scenario = (
            "entry"
            if direction == "buy"
            else "profit_taking"
            if direction == "sell"
            else "psychology_check"
        )
        # P2 修复：使用波动率感知的动态路由
        # P0 修复：enable_multi_perspective=false 时跳过多视角注入
        atr = signal.get("atr", 0)
        price = signal.get("price", 0)
        market_vol = (atr / price) if (atr > 0 and price > 0) else None
        book_context = ""
        if self._book_router is not None:
            book_context = self._book_router.build_context(scenario, direction, signal)
            # 若 market_vol 可用，额外附上动态视角选择说明
            if market_vol is not None:
                dyn_perspectives = self._book_router.select_perspectives_dynamic(
                    scenario, direction, market_volatility=market_vol, signal=signal
                )
                if dyn_perspectives:
                    book_context += (
                        f"\n（波动率={market_vol:.2%}，动态选择视角: {dyn_perspectives[:2]}）"
                )

        # 3. 构建 prompt
        system_prompt = self._build_system_prompt()
        user_prompt = f"{signal_context}\n\n{skill_context}\n\n{book_context}"

        # 4. 调用 LLM
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

        # 5. 解析 LLM 输出
        llm_decision = self._parse_llm_response(content)
        if llm_decision is None:
            logger.warning(f"[wisdom] LLM 输出无法解析，降级到硬编码规则: {content[:200]}")
            return self._advise_fallback(signal)

        # 6. 应用 LLM 决策到信号
        return self._apply_llm_decision(signal, llm_decision)

    async def _advise_llm_async(self, signal: dict[str, Any]) -> dict[str, Any]:
        """LLM 决策（异步版，推荐）：使用 AsyncClient 避免阻塞事件循环。"""
        direction = signal.get("direction", "")
        cfg = env()

        # 1. 选择相关 skill 知识
        skill_context = self._build_skill_context(direction)

        # 2. 构建信号上下文
        signal_context = self._build_signal_context(signal)

        # 2.5 构建多视角书籍认知上下文
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
        book_context = self._book_router.build_context(scenario, direction, signal)
        if market_vol is not None:
            dyn_perspectives = self._book_router.select_perspectives_dynamic(
                scenario, direction, market_volatility=market_vol, signal=signal
            )
            if dyn_perspectives:
                book_context += (
                    f"\n（波动率={market_vol:.2%}，动态选择视角: {dyn_perspectives[:2]}）"
                )

        # 3. 构建 prompt
        system_prompt = self._build_system_prompt()
        user_prompt = f"{signal_context}\n\n{skill_context}\n\n{book_context}"

        # 4. 调用 LLM（异步，不阻塞事件循环）
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
                "max_tokens": 800,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # 5. 解析 LLM 输出
        llm_decision = self._parse_llm_response(content)
        if llm_decision is None:
            logger.warning(f"[wisdom] LLM 异步输出无法解析，降级到硬编码规则: {content[:200]}")
            return self._advise_fallback(signal)

        # 6. 应用 LLM 决策到信号
        return self._apply_llm_decision(signal, llm_decision)

    async def advise_macro_async(
        self, macro_summary: str, portfolio_summary: str = ""
    ) -> str:
        """生成大师兄视角的宏观市场解读（异步）。

        与个股决策不同，宏观解读不输出 action/position_ratio，
        而是直接返回一段市场分析文本。

        Args:
            macro_summary: 宏观数据摘要文本（金油汇债G + A股指数 + 资金流）
            portfolio_summary: 用户当前持仓摘要（可选，用于个性化建议）

        Returns:
            大师兄解读文本（纯文本，非 JSON）
        """
        cfg = env()
        has_portfolio = bool(portfolio_summary and portfolio_summary.strip())
        system_prompt = (
            "你是「大师兄」——一位融合28本股书智慧的资深市场分析师。\n"
            "你的认知来自陈江挺《炒股的智慧》、利弗莫尔《股票大作手回忆录》、\n"
            "海龟交易法则、邱国鹭《投资中最简单的事》、达利欧《原则》等经典。\n\n"
            "你的职责是根据当日宏观数据（金/油/汇/债/G + A股指数 + 资金流向），\n"
            "输出一段有结构、有深度、可操作的大师视角解读。\n\n"
            "P0 修复：6节精简为3节，每节必须有完整因果链而非数据罗列。\n"
            "输出格式（必须严格按以下 3 节输出，每节标题用###）：\n"
            "### 1. 关键变化与含义\n"
            "- 用 2-3 条说明今日最重要、最反常或最具指示意义的数据变化；\n"
            "- 必须解释'这个数据意味着什么'，禁止只报数字不复述涨跌。\n"
            "### 2. 对A股的逻辑传导\n"
            "- 解释宏观数据到A股的传导链路（如：实际利率→估值→成长股）；\n"
            "- 结合资金流向判断主力在买什么/卖什么，市场风格（成长/价值/防御）及持续性。\n"
            "### 3. 明日推演与操作建议\n"
            "- 2-3 个明日盘面关键观察点（价格/量能/资金/事件）；\n"
            "- 具体仓位建议（加仓/减仓/观望/调仓），必须说明止损纪律。\n"
        )
        if has_portfolio:
            system_prompt += (
                "### 4. 对你持仓的影响（仅持仓时输出）\n"
                "- 逐一点评持仓标的与当前宏观逻辑的共振/错配；\n"
                "- 给出针对每只票的具体调仓/止损/加仓建议，必须具体到盈亏和仓位。\n"
            )
        system_prompt += (
            "\n写作要求：\n"
            "- 总字数 500-800 字，每节必须有完整因果链，禁止流水账；\n"
            "- 拒绝空话套话，每句话都要传递增量信息；\n"
            "- 使用专业术语但要给出散户能懂的解释；\n"
            "- 不要重复数据，要分析数据背后的逻辑；\n"
            "- 语气像有经验的老交易员给同门师弟师妹讲课。\n"
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
                    # v0.5.2 修复：原 max_tokens=1200 不足以生成4节内容
                    # （有持仓时第4节"对你持仓的影响"逐一点评持仓标的），
                    # 导致 LLM 输出在生成中途被截断（finish_reason=length），
                    # 用户收到的飞书通知内容不完整。
                    # 1192 字中文 ≈ 1800 tokens，提高到 2500 确保完整输出
                    "max_tokens": 2500,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            resp_data = resp.json()
            content = resp_data["choices"][0]["message"]["content"]

            # v0.5.2 修复：检查 finish_reason，如果被截断则记录警告
            finish_reason = resp_data["choices"][0].get("finish_reason", "unknown")
            if finish_reason == "length":
                logger.warning(
                    f"[wisdom-LLM] 大师兄宏观解读被 max_tokens 截断 "
                    f"(已生成 {len(content)} 字，finish_reason=length)，"
                    f"内容可能不完整"
                )
            else:
                logger.info(
                    f"[wisdom-LLM] 大师兄宏观解读生成成功: {len(content)} 字 "
                    f"(finish_reason={finish_reason})"
                )
            return content.strip()
        except Exception as e:
            logger.warning(f"[wisdom-LLM] 大师兄宏观解读失败: {e}")
            return ""

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

        # 市场环境上下文
        market_ctx = signal.get("market_context", {})
        if market_ctx:
            lines.append(f"- 数据源状态: {market_ctx.get('data_source', 'unknown')}")
            lines.append(f"- 当前持仓数: {market_ctx.get('portfolio_count', 0)}")
            lines.append(f"- 现金占比: {market_ctx.get('cash_ratio', 1.0):.1%}")

        return "\n".join(lines)

    def _build_skill_context(self, direction: str) -> str:
        """根据信号方向选择相关 skill 知识。"""
        # 买入相关 skill
        buy_skills = ["stock-entry-decision", "stock-stop-loss-decision", "stock-position-sizing"]
        # 卖出相关 skill
        sell_skills = ["stock-profit-taking-decision", "stock-trailing-stop"]
        # 通用 skill
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
                # 截取方法论骨架（I 段）和执行步骤（E 段），这是最核心的决策依据
                extracted = self._extract_decision_rules(content)
                if extracted:
                    parts.append(f"### {skill_name}\n{extracted}\n")

        return "\n".join(parts)

    def _extract_decision_rules(self, content: str) -> str:
        """从 SKILL.md 中提取决策相关的核心内容（I 段方法论 + E 段执行步骤 + B 段边界）。

        跳过 R 段（原文引用）和 A1/A2 段（历史案例/触发场景），聚焦可操作的规则。
        """
        lines = content.split("\n")
        result_lines: list[str] = []
        in_section = False

        for line in lines:
            # 检测段落标题
            if line.startswith("## "):
                # 只保留 I 段（方法论骨架）、E 段（执行步骤）、B 段（边界）
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
                    # R 段、A1 段、A2 段等跳过
                    in_section = False
                continue

            if in_section:
                result_lines.append(line)

            # 限制长度，避免 prompt 过长
            if len("\n".join(result_lines)) > 2000:
                break

        return "\n".join(result_lines)

    def _parse_llm_response(self, content: str) -> dict[str, Any] | None:
        """解析 LLM 输出的 JSON 决策。"""
        try:
            # 尝试直接解析
            data = json.loads(content)
        except json.JSONDecodeError:
            # 尝试从 markdown code block 中提取
            import re

            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                except json.JSONDecodeError:
                    return None
            else:
                return None

        # 验证必要字段
        if "action" not in data:
            return None
        if data["action"] not in ("buy", "sell", "hold", "filter"):
            return None

        return data

    def _apply_llm_decision(
        self, signal: dict[str, Any], llm_decision: dict[str, Any]
    ) -> dict[str, Any]:
        """将 LLM 决策应用到信号。

        安全墙（新增）：
        1. Action 一致性校验：LLM action 必须与信号场景匹配
        2. 止损价位置校验：止损价不能高于入场价的 2%（买入时）
        3. 仓位数值域校验：position_ratio 必须 0-1
        """
        action = llm_decision["action"]
        reason = llm_decision.get("reason", "")
        position_ratio = float(llm_decision.get("position_ratio", 0))
        stop_loss_price = llm_decision.get("stop_loss_price")

        # 安全墙：校验 action 与 direction 的兼容性
        direction = signal.get("direction", "")
        if direction == "buy" and action not in ("buy", "hold", "filter"):
            logger.warning(
                f"[wisdom-LLM] {signal.get('symbol', '')} 安全墙: "
                f"买入信号被 LLM 输出非买入 action={action}，强制转为 filter"
            )
            action = "filter"
            reason = f"安全墙: 买入信号不兼容 LLM action={action}"
        elif direction == "sell" and action not in ("sell", "hold", "filter"):
            logger.warning(
                f"[wisdom-LLM] {signal.get('symbol', '')} 安全墙: "
                f"卖出信号被 LLM 输出非卖出 action={action}，强制转为 filter"
            )
            action = "filter"
            reason = f"安全墙: 卖出信号不兼容 LLM action={action}"

        # 安全墙：校验 position_ratio 值域
        if not (0.0 <= position_ratio <= 1.0):
            logger.warning(
                f"[wisdom-LLM] {signal.get('symbol', '')} 安全墙: "
                f"position_ratio={position_ratio} 超出 0-1 范围，截断为 0"
            )
            position_ratio = max(0.0, min(1.0, position_ratio))

        # 安全墙：校验止损价位置（买入时必须低于当前价至少 2%）
        price = signal.get("price", 0)
        if stop_loss_price is not None and float(stop_loss_price) > 0 and price > 0:
            sl = float(stop_loss_price)
            if sl >= price * (1 - LLM_STOP_LOSS_MIN_DISTANCE):
                logger.warning(
                    f"[wisdom-LLM] {signal.get('symbol', '')} 安全墙: "
                    f"止损价 {sl:.2f} 距离当前价 {price:.2f} 不足 2%，已修正"
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

        # 根据信号方向 + LLM action 综合决策
        direction = signal.get("direction", "")
        max_ratio = _max_single_position()

        if action == "filter":
            # LLM 建议过滤
            signal["wisdom_filtered"] = True
            decision["filter_reason"] = reason
            signal["suggested_position_ratio"] = 0.0
            logger.info(f"[wisdom-LLM] {signal.get('symbol', '')} 过滤: {reason}")

        elif action == "hold":
            # LLM 建议持有不动（不买也不卖）
            signal["wisdom_filtered"] = True
            decision["filter_reason"] = f"建议持有观望: {reason}"
            signal["suggested_position_ratio"] = 0.0
            logger.info(f"[wisdom-LLM] {signal.get('symbol', '')} 建议持有: {reason}")

        elif direction == "buy" and action == "buy":
            # LLM 同意买入，应用仓位和止损
            # 安全校验：仓位不超过风控上限
            current_ratio = float(signal.get("current_position_ratio", 0))
            remaining = max_ratio - current_ratio
            safe_ratio = min(position_ratio, remaining, max_ratio)
            safe_ratio = max(0.0, safe_ratio)

            # 安全护栏：首次试仓硬上限
            is_trial = current_ratio <= 0
            if is_trial and safe_ratio > LLM_MAX_TRIAL_RATIO:
                logger.warning(
                    f"[wisdom-LLM] {signal.get('symbol', '')} 仓位护栏: "
                    f"LLM建议 {safe_ratio:.2%} > 试仓上限 {LLM_MAX_TRIAL_RATIO:.2%}，已截断"
                )
                safe_ratio = LLM_MAX_TRIAL_RATIO

            if safe_ratio <= 0:
                signal["wisdom_filtered"] = True
                decision["filter_reason"] = (
                    f"现有持仓占比 {current_ratio:.2%} 已达上限 {max_ratio:.2%}，不再加仓"
                )
                signal["suggested_position_ratio"] = 0.0
                logger.info(
                    f"[wisdom-LLM] {signal.get('symbol', '')} 加仓过滤: "
                    f"现有占比 {current_ratio:.2%} >= 上限 {max_ratio:.2%}"
                )
            else:
                original_ratio = signal.get("suggested_position_ratio", 0)
                signal["suggested_position_ratio"] = safe_ratio
                decision["position_strategy"] = "add" if current_ratio > 0 else "trial"
                logger.info(
                    f"[wisdom-LLM] {signal.get('symbol', '')} 买入确认: "
                    f"仓位 {original_ratio:.2%} → {safe_ratio:.2%}, 理由: {reason}"
                )

            # 止损价安全护栏
            price = signal.get("price", 0)
            if stop_loss_price and float(stop_loss_price) > 0:
                sl = float(stop_loss_price)
                # 校验止损幅度在 5%-10% 之间
                if price > 0:
                    sl_pct = (price - sl) / price
                    if sl_pct < LLM_MIN_STOP_LOSS_PCT:
                        # 止损太近，调整到最小幅度
                        sl = round(price * (1 - LLM_MIN_STOP_LOSS_PCT), 2)
                        logger.warning(
                            f"[wisdom-LLM] {signal.get('symbol', '')} 止损护栏: "
                            f"LLM止损幅度 {sl_pct:.2%} < {LLM_MIN_STOP_LOSS_PCT:.2%}，"
                            f"调整到 {sl:.2f}"
                        )
                    elif sl_pct > LLM_MAX_STOP_LOSS_PCT:
                        # 止损太远，调整到最大幅度
                        sl = round(price * (1 - LLM_MAX_STOP_LOSS_PCT), 2)
                        logger.warning(
                            f"[wisdom-LLM] {signal.get('symbol', '')} 止损护栏: "
                            f"LLM止损幅度 {sl_pct:.2%} > {LLM_MAX_STOP_LOSS_PCT:.2%}，"
                            f"调整到 {sl:.2f}"
                        )
                signal["stop_loss_price"] = round(sl, 2)
                decision["stop_loss_price"] = round(sl, 2)
            elif price > 0:
                # LLM 未给出止损价，用默认规则
                default_stop = price * (1 - FALLBACK_STOP_LOSS_PCT)
                signal["stop_loss_price"] = round(default_stop, 2)
                decision["stop_loss_price"] = round(default_stop, 2)
                decision["stop_loss_source"] = "fallback"

            # 止盈方案（基于入场价 + 固定百分比）
            if price > 0:
                tp = _compute_take_profit(price, signal.get("atr", 0))
                signal["take_profit"] = tp
                decision["take_profit"] = tp

        elif direction == "sell" and action == "sell":
            # LLM 同意卖出
            decision["sell_reason"] = reason
            logger.info(f"[wisdom-LLM] {signal.get('symbol', '')} 卖出确认: {reason}")

        elif direction == "buy" and action == "sell":
            # 策略建议买入但 LLM 建议卖出 → 过滤买入信号
            signal["wisdom_filtered"] = True
            decision["filter_reason"] = f"LLM 建议卖出而非买入: {reason}"
            signal["suggested_position_ratio"] = 0.0
            logger.info(f"[wisdom-LLM] {signal.get('symbol', '')} 买入被否决: {reason}")

        elif direction == "sell" and action == "buy":
            # 策略建议卖出但 LLM 建议买入 → 过滤卖出信号
            signal["wisdom_filtered"] = True
            decision["filter_reason"] = f"LLM 建议持有而非卖出: {reason}"
            logger.info(f"[wisdom-LLM] {signal.get('symbol', '')} 卖出被否决: {reason}")

        else:
            # 其他情况：应用 LLM 的仓位建议
            if position_ratio > 0:
                signal["suggested_position_ratio"] = min(position_ratio, max_ratio)
            decision["action_detail"] = f"direction={direction}, llm_action={action}"

        signal["wisdom"] = advice
        signal["wisdom_decision"] = decision
        return signal

    # ========== 硬编码 fallback ==========

    def _advise_fallback(self, signal: dict[str, Any]) -> dict[str, Any]:
        """硬编码规则 fallback（LLM 不可用时使用）。

        历史胜率注入：若该策略历史胜率 < 30%，降低入场置信度阈值（从 0.6 降至 0.4）。
        """
        signal = dict(signal)
        direction = signal.get("direction", "")
        price = signal.get("price", 0.0)
        confidence = signal.get("confidence", 1.0)

        # P2 修复：历史胜率注入 — 对历史表现差的策略降低置信度
        strategies = signal.get("strategies", []) or [signal.get("strategy", "")]
        if isinstance(strategies, str):
            strategies = [strategies]
        for s_name in strategies:
            if s_name:
                hist_win_rate = self._get_strategy_win_rate(s_name)
                if hist_win_rate is not None and hist_win_rate < 0.30:
                    logger.info(
                        f"[wisdom-fallback] 策略 {s_name} 历史胜率 {hist_win_rate:.1%} < 30%%，"
                        f"降低入场置信度阈值（0.6 → 0.4）"
                    )
                    # 降低置信度阈值：原 0.6 降至 0.4
                    min_conf = 0.4
                    if confidence < min_conf:
                        logger.info(
                            f"[wisdom-fallback] 策略 {s_name} 低胜率 {hist_win_rate:.1%}，"
                            f"置信度 {confidence:.2f} < {min_conf}，过滤信号"
                        )
                        signal["wisdom_filtered"] = True
                        signal["filter_reason"] = (
                            f"策略 {s_name} 历史胜率 {hist_win_rate:.1%} 过低，信号被过滤"
                        )
                        return signal

        advice: dict[str, str] = {}
        decision: dict[str, Any] = {"source": "fallback"}

        if direction == "buy":
            # 数据驱动建议（替代格言摘录）
            advice = self._build_data_driven_advice(signal, "buy")

            # 入场过滤——低置信度信号降级
            if confidence < FALLBACK_ENTRY_MIN_CONFIDENCE:
                decision["entry_filtered"] = True
                decision["filter_reason"] = (
                    f"信心度 {confidence:.2f} 低于入场阈值 {FALLBACK_ENTRY_MIN_CONFIDENCE}"
                )
                signal["wisdom_filtered"] = True
                logger.info(
                    f"[wisdom-fallback] {signal.get('symbol', '')} 入场过滤: "
                    f"信心度 {confidence:.2f} < {FALLBACK_ENTRY_MIN_CONFIDENCE}"
                )

            # 仓位调整——分层下注（三档：试仓/加仓/满仓）
            original_ratio = signal.get("suggested_position_ratio", 0.0)
            if original_ratio > 0:
                has_position = signal.get("has_position", False)
                profit_pct = signal.get("profit_pct", 0.0)
                if has_position:
                    # 持仓盈利 >5% → 趋势确认，允许加码更多
                    if profit_pct > 0.05:
                        adjusted_ratio = original_ratio * FALLBACK_ADD_RATIO * 1.5
                        strategy_label = "add_confirmed"
                    else:
                        adjusted_ratio = original_ratio * FALLBACK_ADD_RATIO
                        strategy_label = "add"
                else:
                    adjusted_ratio = original_ratio * FALLBACK_TRIAL_RATIO
                    strategy_label = "trial"
                current_ratio = signal.get("current_position_ratio", 0.0)
                max_ratio = _max_single_position()
                remaining = max_ratio - float(current_ratio)
                if remaining <= 0:
                    adjusted_ratio = 0.0
                    decision["filter_reason"] = (
                        f"现有持仓占比 {current_ratio:.2%} 已达上限 {max_ratio:.2%}，不再加仓"
                    )
                    signal["wisdom_filtered"] = True
                else:
                    adjusted_ratio = min(adjusted_ratio, remaining)
                decision["adjusted_position_ratio"] = adjusted_ratio
                decision["position_strategy"] = strategy_label
                signal["suggested_position_ratio"] = adjusted_ratio
                logger.info(
                    f"[wisdom-fallback] {signal.get('symbol', '')} 仓位调整: "
                    f"{original_ratio:.2%} → {adjusted_ratio:.2%} ({strategy_label})"
                )

            # 止损价预设
            if price > 0:
                stop_price = price * (1 - FALLBACK_STOP_LOSS_PCT)
                decision["stop_loss_price"] = round(stop_price, 2)
                signal["stop_loss_price"] = round(stop_price, 2)
                # 止盈方案
                tp = _compute_take_profit(price, signal.get("atr", 0))
                signal["take_profit"] = tp
                decision["take_profit"] = tp
                logger.info(
                    f"[wisdom-fallback] {signal.get('symbol', '')} 止损预设: "
                    f"{stop_price:.2f} (-{FALLBACK_STOP_LOSS_PCT:.0%})"
                    f" | 止盈目标: +{tp.get('target_1_pct', 0):.0%}/+{tp.get('target_2_pct', 0):.0%}"
                )

        elif direction == "sell":
            # 数据驱动建议（替代格言摘录）
            advice = self._build_data_driven_advice(signal, "sell")

            # P1 修复：fallback 卖出信号校验——无持仓时过滤卖出信号
            has_position = signal.get("has_position", False)
            if not has_position:
                signal["wisdom_filtered"] = True
                decision["filter_reason"] = "无持仓，过滤卖出信号"
                logger.info(f"[wisdom-fallback] {signal.get('symbol', '')} 卖出过滤: 无持仓")
            elif price > 0:
                decision["sell_reason"] = "策略信号触发卖出"
                # P0-6 修复：sell 信号补充 stop_loss_price（语义为"立即在当前价退出"）
                # 原遗漏导致所有 sell 信号 stop_loss_price=null，下游 NULL 检查可能失败
                # 对抗性审查结论：sell 信号触发后持仓应已清空，止损价无实际意义
                # 但保持字段一致性可避免下游分支异常
                exit_price = round(price, 2)
                decision["stop_loss_price"] = exit_price
                signal["stop_loss_price"] = exit_price
                logger.info(
                    f"[wisdom-fallback] {signal.get('symbol', '')} 卖出决策: 策略信号触发, "
                    f"退出价: {exit_price:.2f}"
                )

        signal["wisdom"] = advice
        signal["wisdom_decision"] = decision

        # 注入多视角共识分析（signal_only 模式下也填充 mentor_perspectives）
        return self._inject_perspective_consensus(signal)

    @staticmethod
    def _get_strategy_win_rate(strategy_name: str) -> float | None:
        """获取策略的历史胜率（从 signals_history.jsonl 计算）。

        胜率定义：该策略发出的信号中，5 日后收益 > 0 的比例。
        若历史记录不足（< 5 条），返回 None（表示数据不足，不调整）。

        Args:
            strategy_name: 策略名称

        Returns:
            历史胜率 (0-1) 或 None（数据不足）
        """
        import json

        from gugu.config import PROJECT_ROOT

        path = PROJECT_ROOT / "data" / "signals_history.jsonl"
        if not path.exists():
            return None

        # 读取最近 100 条信号
        records: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i >= 500:  # 最多读取 500 行
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        records.append(rec)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return None

        if not records:
            return None

        # 过滤该策略的信号
        strategy_signals = []
        for r in records:
            strategies = r.get("strategies", []) or [r.get("strategy", "")]
            if isinstance(strategies, str):
                strategies = [strategies]
            if strategy_name in strategies and r.get("order_success"):
                strategy_signals.append(r)

        if len(strategy_signals) < 5:
            return None  # 数据不足

        # 胜率 = 执行后盈利的信号 / 总执行信号
        wins = sum(1 for r in strategy_signals if _is_profitable_signal(r))
        return wins / len(strategy_signals)

    def _get_skill_advice(self, skill_name: str) -> str:
        """获取指定 skill 的核心建议（截取关键段落，用于 fallback 展示）。"""
        content = self._skills.get(skill_name, "")
        if not content:
            return ""

        lines = content.split("\n")
        summary_lines = []
        for line in lines:
            if line.startswith("#") or line.startswith("- ") or line.startswith("  "):
                summary_lines.append(line)
            if len("\n".join(summary_lines)) > 500:
                break
        return "\n".join(summary_lines)

    def _get_skill_source(self, skill_name: str) -> str:
        """从 SKILL.md 的 YAML frontmatter 提取 source_book 字段。"""
        content = self._skills.get(skill_name, "")
        if not content:
            return ""
        # 解析 YAML frontmatter
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                frontmatter = content[3:end]
                for line in frontmatter.split("\n"):
                    if line.strip().startswith("source_book:"):
                        return line.split(":", 1)[1].strip()
        return ""

    def _build_data_driven_advice(
        self, signal: dict[str, Any], direction: str
    ) -> dict[str, str]:
        """基于信号实际数据生成交易智慧建议（替代格言摘录）。

        每条建议包含：
        1. 基于当前信号数据的具体结论
        2. 应用的策略原则（含出处）
        """
        advice: dict[str, str] = {}
        symbol = signal.get("symbol", "")
        name = signal.get("name", "") or symbol
        price = signal.get("price", 0.0)
        confidence = signal.get("confidence", 1.0)
        atr = signal.get("atr", 0.0)
        has_position = signal.get("has_position", False)
        profit_pct = signal.get("profit_pct", 0.0)
        strategies = signal.get("strategies", []) or [signal.get("strategy", "")]
        if isinstance(strategies, str):
            strategies = [strategies]

        # 市场分析数据（从 signal_pipeline 注入）
        stage = signal.get("stage", "")
        danger_signals = signal.get("danger_signals", [])
        divergence = signal.get("divergence", {})
        money_flow = signal.get("money_flow", {})
        fundamental = signal.get("fundamental", {})
        _multi_period = signal.get("multi_period", {})

        if direction == "buy":
            # ── 入场建议 ──
            entry_parts: list[str] = []
            entry_parts.append(f"**{name}({symbol})** 当前价 ¥{price:.2f}，置信度 {confidence:.0%}")

            # 阶段判断
            if stage:
                stage_map = {
                    "bullish": "升势",
                    "crazy": "疯狂",
                    "final": "最后",
                    "flat": "牛皮市",
                }
                stage_text = stage_map.get(stage, stage)
                entry_parts.append(f"市场阶段：{stage_text}")
                if stage in ("crazy", "final"):
                    entry_parts.append("⚠️ 疯狂/最后阶段禁止入场（P0 规则）")

            # 危险信号
            if danger_signals:
                entry_parts.append(
                    f"⚠️ 检测到 {len(danger_signals)} 个危险信号：{', '.join(danger_signals[:2])}"
                )

            # 量价背离
            if divergence and isinstance(divergence, dict) and divergence.get("diverged"):
                entry_parts.append("⚠️ 量价背离：价涨量缩，警惕主力出货")

            # 资金流
            if money_flow and isinstance(money_flow, dict):
                net_inflow = money_flow.get("net_inflow", 0)
                if net_inflow < 0:
                    entry_parts.append(f"⚠️ 资金净流出 {abs(net_inflow):.2f}亿，主力在撤")

            # 基本面
            if fundamental and isinstance(fundamental, dict):
                pe = fundamental.get("pe")
                if pe and pe > 0:
                    entry_parts.append(f"PE={pe:.1f}")

            # 策略出处
            source = self._get_skill_source("stock-entry-decision")
            entry_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
            advice["entry_check"] = "\n".join(entry_parts)

            # ── 止损建议 ──
            stop_parts: list[str] = []
            stop_price = signal.get("stop_loss_price", 0.0)
            if stop_price and price > 0:
                stop_pct = (price - stop_price) / price
                stop_parts.append(f"建议止损价：¥{stop_price:.2f}（-{stop_pct:.1%}）")
                if atr > 0:
                    stop_parts.append(f"ATR={atr:.2f}（占价 {atr/price:.1%}），止损幅度参考 ATR")
                else:
                    stop_parts.append("无 ATR 数据，使用默认 8% 止损")
            else:
                stop_parts.append(f"建议止损价：¥{price * 0.92:.2f}（-8%）" if price > 0 else "价格无效")
            stop_parts.append("原则：止损是铁律，不可违反，跌破立即执行")
            source = self._get_skill_source("stock-stop-loss-decision")
            stop_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
            advice["stop_loss"] = "\n".join(stop_parts)

            # ── 仓位建议 ──
            pos_parts: list[str] = []
            ratio = signal.get("suggested_position_ratio", 0.0)
            if has_position:
                pos_parts.append(f"已持仓，当前盈利 {profit_pct:+.1%}")
                if profit_pct > 0.05:
                    pos_parts.append(f"盈利确认，可加码至 {ratio:.1%}（加仓策略）")
                else:
                    pos_parts.append(f"建议加码比例：{ratio:.1%}")
            else:
                pos_parts.append(f"未持仓，建议试仓 {ratio:.1%}（首次建仓 20%）")
                pos_parts.append("分层下注：试仓 20% → 盈利确认加码 40% → 满仓 30%")
            source = self._get_skill_source("stock-position-sizing")
            pos_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
            advice["position_sizing"] = "\n".join(pos_parts)

        elif direction == "sell":
            # ── 止盈建议 ──
            profit_parts: list[str] = []
            profit_parts.append(f"**{name}({symbol})** 卖出信号触发")
            profit_parts.append(f"当前价 ¥{price:.2f}，持仓盈利 {profit_pct:+.1%}")
            if profit_pct > 0.30:
                profit_parts.append("⚠️ 已达 +30%，按财叔策略应减仓 1/3")
            elif profit_pct > 0.15:
                profit_parts.append("💡 盈利 >15%，考虑部分止盈")
            elif profit_pct < -0.08:
                profit_parts.append("⚠️ 亏损 >8%，应执行止损")
            source = self._get_skill_source("stock-profit-taking-decision")
            profit_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
            advice["profit_taking"] = "\n".join(profit_parts)

            # ── 追踪止损 ──
            trail_parts: list[str] = []
            if has_position and profit_pct > 0:
                trail_parts.append(f"持仓盈利 {profit_pct:+.1%}，启用追踪止损")
                trail_parts.append("止损价随浪谷上移，只上不下")
            else:
                trail_parts.append("无持仓或亏损中，不适用追踪止损")
            source = self._get_skill_source("stock-trailing-stop")
            trail_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
            advice["trailing_stop"] = "\n".join(trail_parts)

        # ── 心态检查 ──
        psych_parts: list[str] = []
        if direction == "buy":
            if confidence < 0.6:
                psych_parts.append("⚠️ 置信度偏低，警惕 FOMO 情绪驱动入场")
            if stage in ("crazy", "final"):
                psych_parts.append("⚠️ 市场过热，警惕贪婪情绪")
        elif direction == "sell":
            if profit_pct > 0.30:
                psych_parts.append("💡 盈利丰厚，警惕贪婪不愿卖")
            elif profit_pct < -0.05:
                psych_parts.append("💡 亏损中，警惕侥幸心理不止损")
        psych_parts.append("原则：纪律 > 情绪，按策略执行而非凭感觉")
        source = self._get_skill_source("stock-psychology-check")
        psych_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
        advice["psychology_check"] = "\n".join(psych_parts)

        return advice

    # ========== LLM 自然语言解读（保留，用于飞书通知） ==========

    def llm_interpret(self, signal: dict[str, Any]) -> str | None:
        """用 LLM 做自然语言解读（用于飞书通知展示）。

        Args:
            signal: 含 wisdom 字段的信号

        Returns:
            自然语言解读文本，无 LLM 配置返回 None
        """
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
            logger.warning(f"[wisdom] LLM 解读失败: {e}")
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
