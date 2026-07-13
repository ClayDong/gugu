"""情感分析引擎：基于金融情感词典的 A 股新闻情感计算。

移植自 MakingMoney 项目的 sentiment_analyzer.py，适配 gugu 日志/配置体系。

核心算法：
1. 金融正向/负向情感词库匹配（含完整词匹配优先）
2. 程度副词权重倍增（"重大利好"→2.5x，"略微"→0.5x）
3. 否定词反转（"不看好"→负向）
4. 分数归一化到 [-1.0, 1.0]
5. 情感强度 1-5 级
"""

from __future__ import annotations

from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()


class SentimentAnalyzer:
    """基于金融情感词典的情感分析引擎。"""

    POS_WORDS: list[str] = [
        "重大利好",
        "重磅利好",
        "创新高",
        "业绩增长",
        "营收增长",
        "利润增长",
        "政策利好",
        "业绩利好",
        "利好消息",
        "大幅上涨",
        "超预期",
        "涨停",
        "暴涨",
        "利好",
        "上涨",
        "增长",
        "提升",
        "突破",
        "盈利",
        "优秀",
        "强劲",
        "向好",
        "乐观",
        "积极",
        "增持",
        "推荐",
        "买入",
        "看好",
        "成长",
        "发展",
        "新高",
        "市场份额",
        "竞争力",
        "领先",
    ]

    NEG_WORDS: list[str] = [
        "重大利空",
        "政策利空",
        "业绩利空",
        "利空消息",
        "大幅下跌",
        "低于预期",
        "业绩下滑",
        "营收下降",
        "利润下降",
        "负面新闻",
        "暴跌",
        "跌停",
        "爆雷",
        "利空",
        "下跌",
        "下降",
        "亏损",
        "下滑",
        "恶化",
        "悲观",
        "负面",
        "风险",
        "危机",
        "问题",
        "减持",
        "卖出",
        "不看好",
        "衰退",
        "停滞",
        "债务",
        "违约",
        "诉讼",
        "处罚",
        "调查",
    ]

    DEGREE_WORDS: dict[str, float] = {
        "非常": 2.0,
        "极其": 2.5,
        "十分": 1.8,
        "特别": 1.8,
        "大幅": 2.0,
        "明显": 1.5,
        "显著": 1.8,
        "略微": 0.5,
        "小幅": 0.5,
        "轻微": 0.3,
        "重大": 2.5,
        "重磅": 2.8,
        "超级": 3.0,
    }

    NEGATION_WORDS: list[str] = ["不", "没", "无", "非", "未", "别", "莫", "勿"]

    def analyze(self, text: str) -> dict[str, Any]:
        """对文本进行情感分析。

        Args:
            text: 新闻标题 + 内容

        Returns:
            dict with keys: sentiment, sentiment_score, sentiment_strength, key_words
        """
        if not text or len(text.strip()) < 5:
            return {
                "sentiment": "neutral",
                "sentiment_score": 0.0,
                "sentiment_strength": 1,
                "key_words": [],
            }

        score = 0.0
        key_words: list[str] = []

        # 正向词匹配（长词优先，避免短词误匹配）
        for word in sorted(self.POS_WORDS, key=lambda x: -len(x)):
            if word in text:
                score += 1.0 * text.count(word)
                key_words.append(word)

        # 负向词匹配（长词优先）
        for word in sorted(self.NEG_WORDS, key=lambda x: -len(x)):
            if word in text:
                score -= 1.0 * text.count(word)
                key_words.append(word)

        # 程度副词倍增
        degree = 1.0
        for dw, w in self.DEGREE_WORDS.items():
            if dw in text:
                degree *= w
        if degree > 1.0:
            degree = min(degree, 5.0)
            score *= degree

        # 否定词反转（关键词前3字内出现否定词）
        negation_count = 0
        for neg in self.NEGATION_WORDS:
            if neg in text:
                for kw in key_words:
                    idx = text.find(kw)
                    if idx > 0 and neg in text[max(0, idx - 3) : idx]:
                        negation_count += 1
                        break
        if negation_count % 2 == 1:
            score = -score

        # 归一化到 [-1.0, 1.0]
        normalized = max(-1.0, min(1.0, score / 5.0))

        sentiment = (
            "positive" if normalized > 0.1 else ("negative" if normalized < -0.1 else "neutral")
        )
        strength = min(5, max(1, int(abs(normalized) * 5 + 1)))

        return {
            "sentiment": sentiment,
            "sentiment_score": round(normalized, 4),
            "sentiment_strength": strength,
            "key_words": list(set(key_words)),
        }
