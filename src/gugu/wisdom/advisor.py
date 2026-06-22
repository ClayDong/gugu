"""决策层：接入 books2skill 交易智慧 skill。

从 books2skill 的"炒股的智慧"加载交易决策 skill，为策略信号提供知识增强。
解析 skill 中的交易规则，真正参与仓位/止损/入场决策。
可选接入 LLM 做自然语言解读。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from gugu.config import PROJECT_ROOT, env, settings
from gugu.utils.log import get_logger

logger = get_logger()

# 仓位管理规则常量（来自 stock-position-sizing skill）
POSITION_TRIAL_RATIO = 0.20  # 试仓比例 20%
POSITION_ADD_RATIO = 0.40  # 加码比例 40%
POSITION_FULL_RATIO = 1.0  # 满仓比例 100%
MAX_SINGLE_POSITION = 0.20  # 单股最大占总资金 20%（分散原则）

# 止损规则常量（来自 stock-stop-loss-decision skill）
STOP_LOSS_DEFAULT_PCT = 0.08  # 默认止损比例 8%（入场价的 8%）
STOP_LOSS_MAX_PCT = 0.20  # 最大止损比例 20%（铁律）

# 入场决策规则常量（来自 stock-entry-decision skill）
ENTRY_MIN_CONFIDENCE = 0.6  # 最低入场置信度


def _wisdom_dirs() -> list[Path]:
    """返回所有可能的 skill 目录（项目内优先，外部 fallback）。"""
    cfg = settings().get("wisdom", {})
    dirs: list[Path] = []

    project_dir = cfg.get("skill_dir")
    if project_dir:
        dirs.append(PROJECT_ROOT / project_dir)

    for d in cfg.get("fallback_dirs", []):
        dirs.append(PROJECT_ROOT / d)

    return dirs


class WisdomAdvisor:
    """交易智慧顾问。

    加载交易决策 skill，解析规则并真正参与决策：
    - 仓位管理：分层下注（试仓 20% → 加码 40% → 满仓）
    - 止损决策：入场前预设止损价
    - 入场决策：三层过滤（基础分析/阶段判断/临界点）
    """

    def __init__(self) -> None:
        self._skills: dict[str, str] = {}
        self._skill_names: list[str] = settings().get("wisdom", {}).get(
            "skill_names",
            [
                "stock-entry-decision",
                "stock-stop-loss-decision",
                "stock-position-sizing",
                "stock-profit-taking-decision",
                "stock-trailing-stop",
                "stock-psychology-check",
            ],
        )
        self._load_skills()

    def _load_skills(self) -> None:
        """加载所有交易决策 skill（按配置目录顺序，后加载的不覆盖先加载的）。"""
        loaded_from: set[str] = set()
        for wisdom_dir in _wisdom_dirs():
            if not wisdom_dir.exists():
                continue
            for skill_dir in wisdom_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                skill_name = skill_dir.name
                if skill_name in loaded_from:
                    continue
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    self._skills[skill_name] = skill_file.read_text(encoding="utf-8")
                    loaded_from.add(skill_name)

        if not self._skills:
            logger.warning(
                "未加载到任何交易智慧 skill；飞书信号中的 wisdom 字段将为空。"
                "如需启用，请将 books2skill 的 skill 复制到 config/settings.yaml 中配置的 wisdom.skill_dir。"
            )
        logger.info(f"加载 {len(self._skills)} 个交易智慧 skill: {list(self._skills.keys())}")

    def advise(self, signal: dict[str, Any]) -> dict[str, Any]:
        """为策略信号提供知识增强建议，并真正参与决策。

        决策参与点：
        1. 仓位调整：根据分层下注原则，首次买入仅用建议仓位的 20-30%
        2. 止损价预设：入场前根据止损规则计算止损价
        3. 入场过滤：低置信度信号被降级或拒绝

        Args:
            signal: 策略信号，含 symbol, direction, strategy, reason, price

        Returns:
            增强 signal，加入 wisdom 字段 + 决策调整字段
        """
        signal = dict(signal)
        direction = signal.get("direction", "")
        price = signal.get("price", 0.0)
        confidence = signal.get("confidence", 1.0)

        advice: dict[str, str] = {}
        decision: dict[str, Any] = {}

        if direction == "buy":
            advice["entry_check"] = self._get_skill_advice("stock-entry-decision")
            advice["stop_loss"] = self._get_skill_advice("stock-stop-loss-decision")
            advice["position_sizing"] = self._get_skill_advice("stock-position-sizing")

            # 决策1：入场过滤——低置信度信号降级
            if confidence < ENTRY_MIN_CONFIDENCE:
                decision["entry_filtered"] = True
                decision["filter_reason"] = f"置信度 {confidence:.2f} 低于入场阈值 {ENTRY_MIN_CONFIDENCE}"
                signal["wisdom_filtered"] = True
                logger.info(
                    f"[wisdom] {signal.get('symbol', '')} 入场过滤: 置信度 {confidence:.2f} < {ENTRY_MIN_CONFIDENCE}"
                )

            # 决策2：仓位调整——分层下注（首次仅 20-30%）
            original_ratio = signal.get("suggested_position_ratio", 0.0)
            if original_ratio > 0:
                # 首次买入仅用目标仓位的 20-30%（取 25%）
                adjusted_ratio = original_ratio * POSITION_TRIAL_RATIO / MAX_SINGLE_POSITION
                # 不超过单股最大 20%
                adjusted_ratio = min(adjusted_ratio, MAX_SINGLE_POSITION)
                decision["adjusted_position_ratio"] = adjusted_ratio
                decision["position_strategy"] = "trial"  # 试仓
                signal["suggested_position_ratio"] = adjusted_ratio
                logger.info(
                    f"[wisdom] {signal.get('symbol', '')} 仓位调整: "
                    f"{original_ratio:.2%} → {adjusted_ratio:.2%} (试仓)"
                )

            # 决策3：止损价预设
            if price > 0:
                stop_price = price * (1 - STOP_LOSS_DEFAULT_PCT)
                decision["stop_loss_price"] = round(stop_price, 2)
                decision["stop_loss_pct"] = STOP_LOSS_DEFAULT_PCT
                signal["stop_loss_price"] = round(stop_price, 2)
                logger.info(
                    f"[wisdom] {signal.get('symbol', '')} 止损预设: "
                    f"{stop_price:.2f} (-{STOP_LOSS_DEFAULT_PCT:.0%})"
                )

        elif direction == "sell":
            advice["profit_taking"] = self._get_skill_advice("stock-profit-taking-decision")
            advice["trailing_stop"] = self._get_skill_advice("stock-trailing-stop")

        # 心理检查（买卖都要）
        advice["psychology_check"] = self._get_skill_advice("stock-psychology-check")

        signal["wisdom"] = advice
        signal["wisdom_decision"] = decision
        return signal

    def _get_skill_advice(self, skill_name: str) -> str:
        """获取指定 skill 的核心建议（截取关键段落）。"""
        content = self._skills.get(skill_name, "")
        if not content:
            return ""

        # 截取 SKILL.md 的核心内容（前 500 字符作为摘要）
        lines = content.split("\n")
        summary_lines = []
        for line in lines:
            if line.startswith("#") or line.startswith("- ") or line.startswith("  "):
                summary_lines.append(line)
            if len("\n".join(summary_lines)) > 500:
                break
        return "\n".join(summary_lines)

    def llm_interpret(self, signal: dict[str, Any]) -> str | None:
        """用 LLM 做自然语言解读（可选，需配置 LLM API）。

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

            prompt = self._build_prompt(signal)
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
            logger.warning(f"LLM 解读失败: {e}")
            return None

    @staticmethod
    def _build_prompt(signal: dict[str, Any]) -> str:
        """构建 LLM prompt。"""
        lines = [
            f"股票: {signal.get('symbol', '')}",
            f"方向: {signal.get('direction', '')}",
            f"触发策略: {signal.get('strategy', '')}",
            f"触发理由: {signal.get('reason', '')}",
            f"当前价: {signal.get('price', '')}",
        ]
        wisdom = signal.get("wisdom", {})
        if wisdom:
            lines.append("\n交易智慧参考:")
            for key, val in wisdom.items():
                if val:
                    lines.append(f"[{key}]\n{val[:200]}")
        lines.append("\n请给出简洁的买卖建议（100字内）：")
        return "\n".join(lines)
