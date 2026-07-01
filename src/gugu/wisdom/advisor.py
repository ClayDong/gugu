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

    def __init__(self) -> None:
        self._skills: dict[str, str] = {}
        self._skill_names: list[str] = get_config().wisdom.skill_names
        self._load_skills()

        # 加载仓颉蒸馏的多视角书籍认知
        self._book_router = BookPerspectiveRouter()

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

        Args:
            signal: 策略信号，Signal 数据类或 dict

        Returns:
            增强信号（dict 格式）。若输入为 Signal，可转为 Signal.from_dict(result) 恢复。
        """
        signal = self._as_dict(signal)

        if self._llm_available:
            try:
                return self._advise_llm(signal)
            except Exception as e:
                logger.warning(f"[wisdom] LLM 决策失败，降级到硬编码规则: {e}")

        return self._advise_fallback(signal)

    async def advise_async(self, signal: dict[str, Any] | Signal) -> dict[str, Any]:
        """异步版本：为策略信号提供交易智慧决策。

        与 advise() 行为完全一致，但 LLM 调用不阻塞事件循环。
        推荐 SignalPipeline 等异步代码路径使用此方法。

        优先使用冲突裁决器（多维权重融合），失败时降级到单LLM决策，
        再失败降级到硬编码规则。
        """
        signal = self._as_dict(signal)

        if self._llm_available:
            # 优先尝试冲突裁决器
            try:
                result = await self._advise_with_conflict_resolution(signal)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(f"[wisdom] 冲突裁决失败，降级到单LLM决策: {e}")

            # 降级到单 LLM 决策
            try:
                return await self._advise_llm_async(signal)
            except Exception as e:
                logger.warning(f"[wisdom] LLM 异步决策失败，降级到硬编码规则: {e}")

        return self._advise_fallback(signal)

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
        atr = signal.get("atr", 0)
        price = signal.get("price", 0)
        market_vol = (atr / price) if (atr > 0 and price > 0) else None
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
        """将 LLM 决策应用到信号。"""
        action = llm_decision["action"]
        reason = llm_decision.get("reason", "")
        position_ratio = float(llm_decision.get("position_ratio", 0))
        stop_loss_price = llm_decision.get("stop_loss_price")

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
            advice["entry_check"] = self._get_skill_advice("stock-entry-decision")
            advice["stop_loss"] = self._get_skill_advice("stock-stop-loss-decision")
            advice["position_sizing"] = self._get_skill_advice("stock-position-sizing")

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
            advice["profit_taking"] = self._get_skill_advice("stock-profit-taking-decision")
            advice["trailing_stop"] = self._get_skill_advice("stock-trailing-stop")

            # P1 修复：fallback 卖出信号校验——无持仓时过滤卖出信号
            has_position = signal.get("has_position", False)
            if not has_position:
                signal["wisdom_filtered"] = True
                decision["filter_reason"] = "无持仓，过滤卖出信号"
                logger.info(f"[wisdom-fallback] {signal.get('symbol', '')} 卖出过滤: 无持仓")
            elif price > 0:
                decision["sell_reason"] = "策略信号触发卖出"
                logger.info(f"[wisdom-fallback] {signal.get('symbol', '')} 卖出决策: 策略信号触发")

        advice["psychology_check"] = self._get_skill_advice("stock-psychology-check")

        signal["wisdom"] = advice
        signal["wisdom_decision"] = decision
        return signal

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
