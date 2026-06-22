"""决策层：接入 books2skill 交易智慧 skill。

从 books2skill 的"炒股的智慧"加载交易决策 skill，为策略信号提供知识增强。
可选接入 LLM 做自然语言解读。
"""
from __future__ import annotations

from typing import Any, cast

from gugu.config import PROJECT_ROOT, env
from gugu.utils.log import get_logger

logger = get_logger()

# books2skill 参考路径
WISDOM_DIR = PROJECT_ROOT / "_refs" / "books2skill" / "library" / "books" / "炒股的智慧" / "skills"


class WisdomAdvisor:
    """交易智慧顾问。

    加载 books2skill 的交易决策 skill，为策略信号提供知识增强。
    """

    def __init__(self) -> None:
        self._skills: dict[str, str] = {}
        self._load_skills()

    def _load_skills(self) -> None:
        """加载所有交易决策 skill。"""
        if not WISDOM_DIR.exists():
            logger.warning(f"交易智慧目录不存在: {WISDOM_DIR}")
            return

        for skill_dir in WISDOM_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                self._skills[skill_dir.name] = skill_file.read_text(encoding="utf-8")
        logger.info(f"加载 {len(self._skills)} 个交易智慧 skill: {list(self._skills.keys())}")

    def advise(self, signal: dict[str, Any]) -> dict[str, Any]:
        """为策略信号提供知识增强建议。

        Args:
            signal: 策略信号，含 symbol, direction, strategy, reason, price

        Returns:
            增强 signal，加入 wisdom 字段（入场/止损/仓位建议）
        """
        signal = dict(signal)
        direction = signal.get("direction", "")

        advice = {}

        if direction == "buy":
            advice["entry_check"] = self._get_skill_advice("stock-entry-decision")
            advice["stop_loss"] = self._get_skill_advice("stock-stop-loss-decision")
            advice["position_sizing"] = self._get_skill_advice("stock-position-sizing")

        elif direction == "sell":
            advice["profit_taking"] = self._get_skill_advice("stock-profit-taking-decision")
            advice["trailing_stop"] = self._get_skill_advice("stock-trailing-stop")

        # 心理检查（买卖都要）
        advice["psychology_check"] = self._get_skill_advice("stock-psychology-check")

        signal["wisdom"] = advice
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
