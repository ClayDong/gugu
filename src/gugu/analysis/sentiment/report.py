"""舆情报告生成器：生成舆情报告 + 飞书卡片格式化。

报告内容：
1. 整体情感概览（正面/负面/中性 + 评分 + 风险等级）
2. 各新闻情感分析详情（标题 + 情感极性 + 关键词）
3. 最正面/最负面新闻突出展示
4. 操作建议（买入/持有/卖出 + 置信度）
"""

from __future__ import annotations

from datetime import date
from typing import Any

from gugu.analysis.sentiment.analyzer import SentimentAnalyzer
from gugu.analysis.sentiment.impact import ImpactAnalyzer
from gugu.utils.log import get_logger

logger = get_logger()

_SENTIMENT_EMOJI = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}
_ACTION_EMOJI = {"buy": "🟢", "sell": "🔴", "hold": "🟡"}

# A 股飞书卡片模板颜色：红涨绿跌
_TEMPLATE_MAP = {"positive": "red", "negative": "green", "neutral": "grey"}


class SentimentReport:
    """舆情报告生成器。"""

    def __init__(self, symbol: str, stock_name: str = "", data_dir: str | None = None) -> None:
        self.symbol = symbol
        self.stock_name = stock_name
        self._analyzer = SentimentAnalyzer()
        self._impact = ImpactAnalyzer(data_dir=data_dir)

    def generate(self, news_list: list[dict]) -> dict[str, Any]:
        """生成每日舆情报告。"""
        if not news_list:
            return {
                "date": str(date.today()),
                "stock_name": self.stock_name,
                "symbol": self.symbol,
                "news_count": 0,
                "summary": "今日无相关新闻",
                "overall_sentiment": "neutral",
                "risk_level": 1,
            }

        analyzed: list[dict] = []
        total = 0.0
        pos = neg = neu = 0

        for n in news_list:
            text = f"{n.get('title', '')} {n.get('content', '')}"
            sentiment = self._analyzer.analyze(text)
            impact = self._impact.calculate(sentiment, n.get("source", "未知"))
            analyzed.append({
                "title": n.get("title", ""),
                "content": n.get("content", ""),
                "source": n.get("source", ""),
                "publish_time": n.get("publish_time", ""),
                "url": n.get("url", ""),
                "sentiment": sentiment,
                "impact": impact,
            })
            s = sentiment["sentiment_score"]
            total += s
            if sentiment["sentiment"] == "positive":
                pos += 1
            elif sentiment["sentiment"] == "negative":
                neg += 1
            else:
                neu += 1

        avg = total / len(news_list)
        overall = "positive" if avg > 0.1 else ("negative" if avg < -0.1 else "neutral")
        risk = self._calc_risk(avg, neg, len(news_list))

        return {
            "date": str(date.today()),
            "stock_name": self.stock_name,
            "symbol": self.symbol,
            "news_count": len(news_list),
            "positive_news": pos,
            "negative_news": neg,
            "neutral_news": neu,
            "overall_sentiment_score": round(avg, 4),
            "overall_sentiment": overall,
            "risk_level": risk,
            "analyzed_news": analyzed,
            "suggestion": self._suggestion(overall, avg, risk),
        }

    def format_feishu_card(self, report: dict) -> dict:
        """格式化舆情报告为飞书卡片。"""
        sentiment = report.get("overall_sentiment", "neutral")
        se = _SENTIMENT_EMOJI.get(sentiment, "⚪")
        label = report.get("stock_name", "") or report.get("symbol", "")
        risk_stars = "⭐" * report.get("risk_level", 1)

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"📅 {report['date']}  |  📰 {report['news_count']} 条新闻\n\n"
                        f"{se} 整体情感：**{sentiment}**  |  评分：**{report['overall_sentiment_score']:.2f}**\n"
                        f"😊 正面 {report['positive_news']}  |  "
                        f"😟 负面 {report['negative_news']}  |  😐 中性 {report['neutral_news']}\n"
                        f"⚠️ 风险等级：{risk_stars}"
                    ),
                },
            },
        ]

        sug = report.get("suggestion", {})
        if sug:
            ae = _ACTION_EMOJI.get(sug.get("action", "hold"), "⚪")
            stars = "★" * sug.get("confidence", 1) + "☆" * (5 - sug.get("confidence", 1))
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**操作建议**\n{ae} {sug.get('message', '')}\n置信度：{stars}"},
            })

        analyzed = report.get("analyzed_news", [])
        if analyzed:
            sorted_n = sorted(analyzed, key=lambda x: abs(x["sentiment"]["sentiment_score"]), reverse=True)
            lines = []
            for i, n in enumerate(sorted_n[:5], 1):
                s = n["sentiment"]
                e = _SENTIMENT_EMOJI.get(s["sentiment"], "⚪")
                kw = ", ".join(s.get("key_words", [])[:3])
                lines.append(f"{i}. {e} {n['title']}")
                if kw:
                    lines.append(f"   🔑 {kw}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**重要新闻**\n" + "\n".join(lines)},
            })

        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "舆情日报 · gugu · 东方财富新闻 + 金融情感词典"}],
        })

        return {
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"📊 舆情日报 · {label}"},
                    "template": _TEMPLATE_MAP.get(sentiment, "grey"),
                },
                "elements": elements,
            }
        }

    @staticmethod
    def _calc_risk(avg: float, neg: int, total: int) -> int:
        if avg > 0.3:
            risk = 1
        elif avg > 0.1:
            risk = 2
        elif avg < -0.3:
            risk = 5
        elif avg < -0.1:
            risk = 4
        else:
            risk = 3
        if total > 0 and neg / total > 0.5:
            risk = min(5, risk + 1)
        return risk

    @staticmethod
    def _suggestion(sentiment: str, score: float, risk: int) -> dict:
        if sentiment == "positive" and score > 0.3:
            return {"action": "buy", "message": "舆情积极向好，可考虑加仓", "confidence": min(5, int(abs(score) * 5) + 1)}
        elif sentiment == "positive":
            return {"action": "hold", "message": "舆情偏正面，建议继续持有", "confidence": 3}
        elif sentiment == "negative" and score < -0.3:
            return {"action": "sell", "message": "舆情负面风险较大，建议减仓观望", "confidence": 4}
        elif sentiment == "negative":
            return {"action": "hold", "message": "舆情偏负面，建议保持观望", "confidence": 3}
        return {"action": "hold", "message": "舆情中性，维持原有策略", "confidence": 2}
