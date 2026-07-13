"""舆情分析系统：集成新闻采集 + 情感分析 + 报告生成 + 飞书推送。

使用方式（在 scheduler 中注册）：
    from gugu.analysis.sentiment import SentimentSystem
    system = SentimentSystem()
    scheduler.add_job(system.run_and_notify, ...)
"""

from __future__ import annotations

from typing import Any

from gugu.analysis.sentiment.fetcher import NewsFetcher
from gugu.analysis.sentiment.impact import ImpactAnalyzer
from gugu.analysis.sentiment.report import SentimentReport
from gugu.utils.log import get_logger

logger = get_logger()


class SentimentSystem:
    """完整的舆情分析系统。"""

    def __init__(
        self, symbol: str = "600519", stock_name: str = "", data_dir: str | None = None
    ) -> None:
        self.symbol = symbol
        self.stock_name = stock_name or f"股票 {symbol}"
        self._fetcher = NewsFetcher(cache_dir=data_dir)
        self._report = SentimentReport(symbol, self.stock_name, data_dir=data_dir)
        self._impact = ImpactAnalyzer(data_dir=data_dir)

    async def run_daily_report(self) -> dict[str, Any]:
        """运行舆情日报流程：采集 → 分析 → 保存。"""
        logger.info(f"== 舆情分析: {self.symbol} {self.stock_name} ==")
        news_list = self._fetcher.fetch_stock_news(self.symbol)
        logger.info(f"获取到 {len(news_list)} 条新闻")
        report = self._report.generate(news_list)
        for n in report.get("analyzed_news", []):
            self._impact.add_case(n, n.get("sentiment", {}))
        logger.info(
            f"舆情分析完成: {report['overall_sentiment']} (score={report['overall_sentiment_score']})"
        )
        return report

    async def run_and_notify(self, notifier=None) -> dict[str, Any]:
        """运行舆情分析并通过飞书推送。"""
        report = await self.run_daily_report()
        if notifier and report.get("news_count", 0) > 0:
            card = self._report.format_feishu_card(report)
            ok = await notifier.send_card(card)
            logger.info(f"舆情飞书推送: {'成功' if ok else '失败'}")
        return report

    def get_sentiment_factor(self, news_list: list[dict] | None = None) -> float:
        """获取舆情因子值 [-1.0, 1.0]，用于信号加权。"""
        if news_list is None:
            news_list = self._fetcher.fetch_stock_news(self.symbol, max_news=5)
        if not news_list:
            return 0.0
        report = self._report.generate(news_list)
        return report.get("overall_sentiment_score", 0.0)
