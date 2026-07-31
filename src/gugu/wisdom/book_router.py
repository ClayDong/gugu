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

from typing import Any

from gugu.config import PROJECT_ROOT
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
            logger.warning("[book-router] 书籍目录不存在: {}", books_dir)
            return

        for perspective_dir in books_dir.iterdir():
            if not perspective_dir.is_dir():
                continue
            skill_file = perspective_dir / "SKILL.md"
            if skill_file.exists():
                name = perspective_dir.name
                self._perspectives[name] = skill_file.read_text(encoding="utf-8")

        logger.info("[book-router] 加载 {} 个书籍视角", len(self._perspectives))

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

        return selected[: MAX_PERSPECTIVES_PER_SCENARIO + 1]

    def select_perspectives_dynamic(
        self,
        scenario: str,
        direction: str = "",
        market_volatility: float | None = None,
        signal: dict[str, Any] | None = None,
        custom_perspectives: list[str] | None = None,
    ) -> list[str]:
        """根据市场波动率动态调整书籍视角选择。

        高波动市场（ATR/price > 3%%）：
        - 加强交易系统（止损）和投资哲学（心理）视角
        - 减少价值投资和宏观视角

        低波动市场（ATR/price < 1%%）：
        - 加强价值投资和宏观视角
        - 减少技术分析视角

        正常波动市场：
        - 使用默认静态路由

        Args:
            scenario: 交易场景
            direction: 信号方向
            market_volatility: 市场波动率（ATR/price 比例，如 0.025）
            signal: 信号数据（可选，用于提取波动率）
            custom_perspectives: 自定义视角列表

        Returns:
            视角名称列表
        """
        if custom_perspectives:
            return [p for p in custom_perspectives if p in self._perspectives]

        # 从 signal 中提取波动率（如果未显式传入）
        if market_volatility is None and signal is not None:
            sig_atr = signal.get("atr", 0)
            sig_price = signal.get("price", 0)
            if sig_atr > 0 and sig_price > 0:
                market_volatility = sig_atr / sig_price

        # 使用动态或静态路由
        if market_volatility is not None:
            return self._dynamic_select(scenario, direction, market_volatility)
        else:
            return self.select_perspectives(scenario, direction)

    def _dynamic_select(self, scenario: str, direction: str, volatility: float) -> list[str]:
        """波动率感知的动态视角选择。"""
        # 基础类别从静态映射获取
        base_categories = list(SCENARIO_MAPPING.get(scenario, ["trading_system"]))

        if volatility > 0.03:
            # 高波动：加强交易系统（止损）和投资哲学（心理）
            if scenario == "entry":
                categories = ["trading_system", "technical_analysis"]
            elif scenario == "position_sizing":
                categories = ["trading_system", "investment_philosophy"]
            elif scenario == "stop_loss":
                categories = ["trading_system"]
            else:
                categories = base_categories
        elif volatility < 0.01:
            # 低波动：加强价值投资和宏观视角
            if scenario == "entry":
                categories = ["value_investing", "technical_analysis"]
            elif scenario == "stock_selection":
                categories = ["value_investing", "growth_selecting"]
            else:
                categories = base_categories
        else:
            categories = base_categories

        # 从类别中选取视角
        selected: list[str] = []
        max_perspectives = MAX_PERSPECTIVES_PER_SCENARIO
        for category in categories:
            perspectives_in_category = BOOK_CATEGORIES.get(category, [])
            for p in perspectives_in_category:
                if p in self._perspectives and p not in selected:
                    selected.append(p)
                if len(selected) >= max_perspectives:
                    break
            if len(selected) >= max_perspectives:
                break

        # 买卖方向补充
        if direction == "buy" and "chen-jiang-ting-perspective" not in selected:
            selected.append("chen-jiang-ting-perspective")
        elif direction == "sell" and "jesse-livermore-perspective" not in selected:
            selected.append("jesse-livermore-perspective")

        return selected[: max_perspectives + 1]

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
                if line.strip().startswith("- ") or line.strip().startswith(
                    ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")
                ):
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
                if len(" ".join(summary_lines)) > 300:
                    break

        return " ".join(summary_lines)[:300]

    # ========== 多视角信号分析 ==========

    # 视角中文显示名称映射
    _DISPLAY_NAMES: dict[str, str] = {
        "benjamin-graham-perspective": "本杰明·格雷厄姆",
        "warren-buffett-perspective": "沃伦·巴菲特",
        "charles-munger-perspective": "查理·芒格",
        "pat-dorsey-perspective": "帕特·多尔西",
        "qiu-guo-lu-perspective": "邱国鹭",
        "peter-lynch-perspective": "彼得·林奇",
        "william-oneil-perspective": "威廉·欧奈尔",
        "k-line-analysis-perspective": "K线分析",
        "richard-schabacker-perspective": "理查德·沙巴克",
        "technical-analysis-perspective": "技术分析",
        "steve-nison-perspective": "史蒂夫·尼森",
        "jesse-livermore-perspective": "杰西·利弗莫尔",
        "turtle-trader-perspective": "海龟交易",
        "mark-douglas-perspective": "马克·道格拉斯",
        "van-tharp-perspective": "范·撒普",
        "ghost-trader-perspective": "鬼才操盘手",
        "chen-jiang-ting-perspective": "陈江挺",
        "adam-smith-perspective": "亚当·斯密",
        "macroeconomics-perspective": "宏观经济学",
        "microeconomics-perspective": "微观经济学",
        "dashi-xiong-perspective": "大师兄",
        "common-prosperity-perspective": "共同富裕",
        "george-soros-perspective": "乔治·索罗斯",
        "nassim-taleb-perspective": "纳西姆·塔勒布",
        "burton-malkiel-perspective": "伯顿·麦基尔",
        "bernard-baruch-perspective": "伯纳德·巴鲁克",
        "zeng-shi-qiang-perspective": "曾仕强",
        "investing-habits-perspective": "投资习惯",
    }

    def load_all_perspectives(self) -> list[dict]:
        """返回所有已加载视角的元数据列表。

        Returns:
            list[dict]: [{name, category, display_name, summary, available}, ...]
        """
        result: list[dict] = []
        for name in self._perspectives:
            # 确定所属分类
            category = "unknown"
            for cat_name, cat_members in BOOK_CATEGORIES.items():
                if name in cat_members:
                    category = cat_name
                    break
            result.append(
                {
                    "name": name,
                    "display_name": self._DISPLAY_NAMES.get(name, name),
                    "category": category,
                    "summary": self.get_perspective_summary(name),
                    "available": True,
                }
            )
        return result

    def get_advice(self, signal: dict[str, Any]) -> dict[str, Any]:
        """为信号提供多视角分析建议。

        基于信号方向和特征，从已加载的书籍视角中选择相关视角，
        为每个视角预测其对该信号的交易建议。

        Args:
            signal: 信号 dict（需包含 direction, confidence 等字段）

        Returns:
            {
                "perspectives": [{name, action, reason, confidence}, ...],
                "consensus": {"buy_pct": 0.x, "sell_pct": 0.x, "hold_pct": 0.x},
                "conflicts": [],
            }
        """
        direction = signal.get("direction", "")
        scenario = (
            "entry"
            if direction == "buy"
            else "profit_taking"
            if direction == "sell"
            else "psychology_check"
        )

        # 动态选择相关视角
        selected = self.select_perspectives_dynamic(scenario, direction, signal=signal)
        if not selected:
            return {
                "perspectives": [],
                "consensus": {"buy_pct": 0, "sell_pct": 0, "hold_pct": 0},
                "conflicts": [],
            }

        signal_confidence = signal.get("confidence", 0.5)
        is_filtered = signal.get("wisdom_filtered", False)

        perspectives_advice: list[dict] = []
        for name in selected:
            content = self._perspectives.get(name, "")
            if not content:
                continue

            # 确定分类
            category = "unknown"
            for cat_name, cat_members in BOOK_CATEGORIES.items():
                if name in cat_members:
                    category = cat_name
                    break

            action, reason, conf = self._predict_perspective_action(
                name, category, direction, signal_confidence, signal, is_filtered
            )

            perspectives_advice.append(
                {
                    "name": self._DISPLAY_NAMES.get(name, name),
                    "action": action,
                    "reason": reason,
                    "confidence": round(conf, 2),
                }
            )

        # 计算共识
        total = len(perspectives_advice) if perspectives_advice else 1
        buy_count = sum(1 for p in perspectives_advice if p["action"] == "buy")
        sell_count = sum(1 for p in perspectives_advice if p["action"] == "sell")
        hold_count = sum(1 for p in perspectives_advice if p["action"] == "hold")
        filter_count = sum(1 for p in perspectives_advice if p["action"] == "filter")

        # filter 归到 hold（表示不建议当前方向操作）
        hold_count += filter_count

        return {
            "perspectives": perspectives_advice,
            "consensus": {
                "buy_pct": round(buy_count / total, 2),
                "sell_pct": round(sell_count / total, 2),
                "hold_pct": round(hold_count / total, 2),
            },
            "conflicts": [],
        }

    @staticmethod
    def _predict_perspective_action(
        name: str,
        category: str,
        signal_direction: str,
        signal_confidence: float,
        signal: dict[str, Any],
        is_filtered: bool,
    ) -> tuple[str, str, float]:
        """预测单视角对信号的交易建议。

        基于视角分类和信号特征的规则引擎：
        - 价值投资类：注重安全边际，低置信度时保守
        - 成长选股类：顺势而为，高置信度积极
        - 技术分析类：重趋势和形态
        - 交易系统类：强调止损和风控
        - 宏观经济类：关注大局
        - 投资哲学类：保守审慎

        Returns:
            (action, reason, confidence_score)
        """
        if signal_direction == "buy":
            if category == "value_investing":
                if is_filtered:
                    return ("hold", "存在风险信号，安全边际不足，建议观望", 0.35)
                if signal_confidence >= 0.7:
                    return ("buy", "优质标的估值合理，符合价值投资标准", 0.80)
                elif signal_confidence >= 0.5:
                    return ("buy", "估值合理，可小仓位试仓等待确认", 0.60)
                else:
                    return ("hold", "信号置信度不足，安全边际不够，观望", 0.35)

            elif category == "growth_selecting":
                if signal_confidence >= 0.65:
                    return ("buy", "成长趋势确认，符合选股标准，建议参与", 0.75)
                else:
                    return ("hold", "成长信号不够明确，等待更强确认", 0.40)

            elif category == "technical_analysis":
                if is_filtered:
                    return ("hold", "技术面存在危险信号，不宜入场", 0.30)
                return ("buy", "技术形态支持入场，按信号操作", 0.70)

            elif category == "trading_system":
                if is_filtered:
                    return ("filter", "多重风险信号叠加，应过滤该信号", 0.20)
                return ("buy", "信号符合交易系统规则，设好止损入场", 0.70)

            elif category == "macro_economics":
                return ("buy", "宏观环境未见系统性风险，可操作", 0.60)

            elif category == "investment_philosophy":
                return ("hold", "保持投资纪律，不因单一信号冲动操作", 0.50)

            # 默认
            if signal_confidence >= 0.6:
                return ("buy", "信号质量尚可，建议参与", 0.60)
            return ("hold", "信号强度不足，观望为宜", 0.40)

        elif signal_direction == "sell":
            if category == "value_investing":
                if is_filtered:
                    return ("sell", "基本面恶化，应果断离场", 0.75)
                return ("sell", "达到目标价或基本面变化，按规则卖出", 0.65)

            elif category == "growth_selecting":
                if signal_confidence >= 0.6:
                    return ("sell", "成长逻辑松动，应止盈/止损", 0.70)
                return ("hold", "卖出信号不够强，观察下个周期", 0.40)

            elif category == "technical_analysis":
                return ("sell", "技术形态走坏，应执行卖出", 0.75)

            elif category == "trading_system":
                return ("sell", "严格遵守交易系统的卖出规则", 0.80)

            elif category == "macro_economics":
                return ("sell", "宏观环境变化，减仓避险为妥", 0.65)

            elif category == "investment_philosophy":
                return ("sell", "保住本金是第一原则，建议卖出", 0.60)

            if signal_confidence >= 0.6:
                return ("sell", "卖出信号明确，建议执行", 0.65)
            return ("hold", "卖出信号偏弱，可再观察", 0.40)

        # 非买卖方向
        return ("hold", "当前信号方向不明，建议观望", 0.30)
