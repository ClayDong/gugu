"""书籍多视角路由器：从28本股书蒸馏的Skill中选择最相关的认知视角。

基于仓颉认知植入术蒸馏的28本书籍SKILL.md，按交易场景智能选择最相关的2-4个视角，
为WisdomAdvisor提供更深层的多视角决策参考。

分类：
- A组 价值投资: Graham, Buffett, Munger, Dorsey, Qiu Guolu
- B组 成长选股: Peter Lynch, William O'Neil
- C组 技术分析: K-line, Schabacker, Technical Analysis, Steve Nison
- D组 交易系统: Livermore, Turtle, Mark Douglas, Van Tharp, Ghost Trader, Chen Jiangting
- E组 宏观经济: Adam Smith, Macro, Micro, Dashi Xiong, Common Prosperity
- F组 投资哲学: Soros, Taleb, Malkiel, Baruch, Zeng Shi Qiang, Investing Habits
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gugu.config import PROJECT_ROOT, settings
from gugu.utils.log import get_logger

logger = get_logger()


# 书籍视角分类
BOOK_CATEGORIES: dict[str, list[str]] = {
    "value_investing": [
        "benjamin-graham-perspective",
        "warren-buffett-perspective",
        "charles-munger-perspective",
        "pat-dorsey-perspective",
        "qiu-guo-lu-perspective",
    ],
    "growth_selecting": [
        "peter-lynch-perspective",
        "william-oneil-perspective",
    ],
    "technical_analysis": [
        "k-line-analysis-perspective",
        "richard-schabacker-perspective",
        "technical-analysis-perspective",
        "steve-nison-perspective",
    ],
    "trading_system": [
        "jesse-livermore-perspective",
        "turtle-trader-perspective",
        "mark-douglas-perspective",
        "van-tharp-perspective",
        "ghost-trader-perspective",
        "chen-jiang-ting-perspective",
    ],
    "macro_economics": [
        "adam-smith-perspective",
        "macroeconomics-perspective",
        "microeconomics-perspective",
        "dashi-xiong-perspective",
        "common-prosperity-perspective",
    ],
    "investment_philosophy": [
        "george-soros-perspective",
        "nassim-taleb-perspective",
        "burton-malkiel-perspective",
        "bernard-baruch-perspective",
        "zeng-shi-qiang-perspective",
        "investing-habits-perspective",
    ],
}

# 交易场景 → 相关书籍类别映射
SCENARIO_MAPPING: dict[str, list[str]] = {
    "entry": ["trading_system", "technical_analysis", "value_investing"],
    "stop_loss": ["trading_system", "investment_philosophy"],
    "position_sizing": ["trading_system", "investment_philosophy"],
    "profit_taking": ["trading_system", "technical_analysis"],
    "trailing_stop": ["trading_system", "technical_analysis"],
    "market_regime": ["macro_economics", "investment_philosophy", "technical_analysis"],
    "stock_selection": ["value_investing", "growth_selecting", "technical_analysis"],
    "risk_management": ["trading_system", "investment_philosophy"],
    "psychology_check": ["investment_philosophy", "trading_system"],
}

# 每个场景默认选取的视角数量
MAX_PERSPECTIVES_PER_SCENARIO = 3


class BookPerspectiveRouter:
    """书籍多视角路由器。

    加载28本股书蒸馏的SKILL.md，根据交易场景选择最相关的认知视角，
    为WisdomAdvisor提供多视角决策参考。

    核心理念：不同交易场景需要不同的智慧视角。
    - 入场决策需要利弗莫尔的关键点 + 陈江挺的临界点 + 技术分析确认
    - 止损决策需要陈江挺的止损铁律 + 海龟的止损成本观
    - 仓位管理需要范·撒普的R乘数 + 陈江挺的分层下注
    - 止盈决策需要陈江挺的移动止损 + 利弗莫尔的让利润奔跑
    """

    def __init__(self) -> None:
        self._perspectives: dict[str, str] = {}
        self._load_perspectives()

    def _load_perspectives(self) -> None:
        """加载所有书籍视角的SKILL.md。"""
        books_dir = PROJECT_ROOT / "src" / "gugu" / "wisdom" / "skills" / "books"
        if not books_dir.exists():
            logger.warning(f"[book-router] 书籍目录不存在: {books_dir}")
            return

        for perspective_dir in books_dir.iterdir():
            if not perspective_dir.is_dir():
                continue
            skill_file = perspective_dir / "SKILL.md"
            if skill_file.exists():
                name = perspective_dir.name
                self._perspectives[name] = skill_file.read_text(encoding="utf-8")

        logger.info(f"[book-router] 加载 {len(self._perspectives)} 个书籍视角")

    @property
    def available_perspectives(self) -> list[str]:
        """返回已加载的视角列表。"""
        return list(self._perspectives.keys())

    def select_perspectives(
        self,
        scenario: str,
        direction: str = "",
        custom_perspectives: list[str] | None = None,
    ) -> list[str]:
        """根据交易场景选择最相关的书籍视角。

        Args:
            scenario: 交易场景 (entry/stop_loss/position_sizing/profit_taking等)
            direction: 信号方向 (buy/sell)，辅助选择
            custom_perspectives: 自定义视角列表，覆盖默认选择

        Returns:
            视角名称列表
        """
        if custom_perspectives:
            return [p for p in custom_perspectives if p in self._perspectives]

        # 获取场景对应的书籍类别
        categories = SCENARIO_MAPPING.get(scenario, ["trading_system"])

        # 从每个类别中选取视角
        selected: list[str] = []
        for category in categories:
            perspectives_in_category = BOOK_CATEGORIES.get(category, [])
            for p in perspectives_in_category:
                if p in self._perspectives and p not in selected:
                    selected.append(p)
                if len(selected) >= MAX_PERSPECTIVES_PER_SCENARIO:
                    break
            if len(selected) >= MAX_PERSPECTIVES_PER_SCENARIO:
                break

        # 买卖方向补充：买入时加入价值投资视角，卖出时加入技术分析视角
        if direction == "buy" and "chen-jiang-ting-perspective" not in selected:
            selected.append("chen-jiang-ting-perspective")
        elif direction == "sell" and "jesse-livermore-perspective" not in selected:
            selected.append("jesse-livermore-perspective")

        return selected[:MAX_PERSPECTIVES_PER_SCENARIO + 1]

    def build_context(
        self,
        scenario: str,
        direction: str = "",
        signal: dict[str, Any] | None = None,
    ) -> str:
        """构建多视角认知上下文文本。

        从选中的书籍视角中提取核心认知要素（信念、决策原则、绝不做的事），
        形成多视角决策参考。

        Args:
            scenario: 交易场景
            direction: 信号方向
            signal: 信号上下文（可选，用于更精准的视角选择）

        Returns:
            多视角认知上下文文本
        """
        perspectives = self.select_perspectives(scenario, direction)
        if not perspectives:
            return ""

        parts = ["## 多视角认知参考（来自仓颉蒸馏的28本股书）\n"]

        for perspective_name in perspectives:
            content = self._perspectives.get(perspective_name, "")
            if not content:
                continue

            # 提取核心认知要素
            extracted = self._extract_core_wisdom(content, perspective_name)
            if extracted:
                parts.append(f"### {perspective_name}\n{extracted}\n")

        return "\n".join(parts)

    def _extract_core_wisdom(self, content: str, name: str) -> str:
        """从SKILL.md中提取核心认知要素。

        提取策略：
        1. 信念及其来源 → 取前3条核心信念
        2. 我绝不会做的事 → 全部提取（这是最硬的规则）
        3. 我看问题的方式 → 取前2条思维习惯
        """
        lines = content.split("\n")
        result_lines: list[str] = []
        current_section = ""
        belief_count = 0
        habit_count = 0

        for line in lines:
            # 检测段落标题
            if line.startswith("### ") or line.startswith("## "):
                lower_line = line.lower()

                if any(kw in lower_line for kw in ["信念", "信念及其来源", "核心理念"]):
                    current_section = "beliefs"
                    result_lines.append(line)
                    belief_count = 0
                    continue
                elif any(kw in lower_line for kw in ["绝不会做", "绝不"]):
                    current_section = "nevers"
                    result_lines.append(line)
                    continue
                elif any(kw in lower_line for kw in ["看问题的方式", "我看问题", "思维习惯"]):
                    current_section = "habits"
                    result_lines.append(line)
                    habit_count = 0
                    continue
                else:
                    current_section = ""
                    continue

            if current_section == "beliefs":
                if line.strip().startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
                    belief_count += 1
                    if belief_count <= 3:
                        result_lines.append(line)
                    elif belief_count == 4:
                        result_lines.append("...(更多信念详见完整SKILL.md)")
                elif belief_count > 0 and belief_count <= 3:
                    if line.strip() and not line.strip().startswith(("---", "```")):
                        result_lines.append(line)

            elif current_section == "nevers":
                if line.strip().startswith("- ") or line.strip().startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
                    result_lines.append(line)

            elif current_section == "habits":
                if line.strip().startswith(("#", "1.", "2.", "3.", "4.", "5.")):
                    habit_count += 1
                    if habit_count <= 2:
                        result_lines.append(line)
                elif habit_count > 0 and habit_count <= 2:
                    if line.strip() and not line.strip().startswith(("---", "```")):
                        result_lines.append(line)

            # 限制总长度
            if len("\n".join(result_lines)) > 1500:
                break

        return "\n".join(result_lines)

    def get_perspective_summary(self, perspective_name: str) -> str:
        """获取单个视角的摘要（用于Web展示）。"""
        content = self._perspectives.get(perspective_name, "")
        if not content:
            return ""

        lines = content.split("\n")
        summary_lines: list[str] = []
        in_intro = False

        for line in lines:
            if line.startswith("## 我是谁") or line.startswith("## 我是谁"):
                in_intro = True
                continue
            if in_intro:
                if line.startswith("## ") or line.startswith("---"):
                    break
                if line.strip():
                    summary_lines.append(line.strip())
                if len("\n".join(summary_lines)) > 300:
                    break

        return " ".join(summary_lines)[:300]
