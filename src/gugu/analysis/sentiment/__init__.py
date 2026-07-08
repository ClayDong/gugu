"""舆情分析模块：新闻采集 + 情感分析 + 股价影响评估 + 报告生成。

通过 akshare 获取东方财富个股新闻和公告，基于金融情感词典进行情感分析，
计算舆情影响分数，生成每日舆情报告并推送到飞书。
"""

from gugu.analysis.sentiment.analyzer import SentimentAnalyzer
from gugu.analysis.sentiment.fetcher import NewsFetcher
from gugu.analysis.sentiment.impact import ImpactAnalyzer
from gugu.analysis.sentiment.report import SentimentReport
from gugu.analysis.sentiment.system import SentimentSystem

__all__ = [
    "ImpactAnalyzer",
    "NewsFetcher",
    "SentimentAnalyzer",
    "SentimentReport",
    "SentimentSystem",
]
