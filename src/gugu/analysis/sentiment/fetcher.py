"""新闻采集器：通过 akshare 获取东方财富个股新闻与公告。

移植自 MakingMoney 项目的 news_fetcher.py，适配 gugu logger + 缓存机制。

数据源：
1. ak.stock_news_em(symbol) — 东方财富个股新闻（标题/内容/来源/时间）
2. ak.stock_notice_report(symbol) — 个股公告

缓存策略：2 小时文件缓存，避免盘中重复请求。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()

_CACHE_HOURS = 2
_MAX_NEWS = 10


class NewsFetcher:
    """新闻采集器。"""

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[4]
        self._cache_dir = (
            Path(cache_dir) if cache_dir else project_root / "data" / "sentiment" / "news_cache"
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._ak = None

    def _get_ak(self):
        if self._ak is None:
            try:
                import akshare as ak
                self._ak = ak
            except ImportError:
                logger.error("akshare 未安装，无法获取新闻")
                return None
        return self._ak

    def fetch_stock_news(self, symbol: str, max_news: int = _MAX_NEWS) -> list[dict[str, Any]]:
        """获取个股新闻，带 2 小时缓存。

        Args:
            symbol: 股票代码，如 "600519"（纯数字，无需前缀）
            max_news: 最大新闻数

        Returns:
            list[dict]: title, content, source, publish_time, type
        """
        cache_file = self._cache_dir / f"news_{symbol}.json"
        cached = self._read_cache(cache_file)
        if cached is not None:
            return cached[:max_news]

        news_list: list[dict] = []

        try:
            news_list.extend(self._fetch_em_news(symbol, max_news))
        except Exception as e:
            logger.warning(f"东方财富新闻获取失败 {symbol}: {e}")

        if len(news_list) < max_news:
            try:
                news_list.extend(self._fetch_notices(symbol, max_news - len(news_list)))
            except Exception as e:
                logger.warning(f"个股公告获取失败 {symbol}: {e}")

        news_list = news_list[:max_news]
        if news_list:
            self._write_cache(cache_file, news_list)
            logger.info(f"获取 {symbol} 新闻 {len(news_list)} 条")
        return news_list

    def fetch_market_news(self, max_news: int = 20) -> list[dict[str, Any]]:
        """获取市场要闻（用 000001 上证指数新闻代表全市场）。"""
        cache_file = self._cache_dir / "market_news.json"
        cached = self._read_cache(cache_file)
        if cached is not None:
            return cached[:max_news]

        news_list = self._fetch_em_news("000001", max_news)
        if news_list:
            for n in news_list:
                n["type"] = "market_news"
            self._write_cache(cache_file, news_list)
        return news_list[:max_news]

    def format_for_llm(self, news_list: list[dict], max_items: int = 5) -> str:
        """格式化新闻为 LLM 可读文本。"""
        if not news_list:
            return "暂无相关资讯"
        lines = []
        for i, n in enumerate(news_list[:max_items], 1):
            title = n.get("title", "无标题")
            source = n.get("source", "未知")
            pub = n.get("publish_time", "")
            content = n.get("content", "")
            lines.append(f"{i}. [{source} {pub}] {title}")
            if content:
                lines.append(f"   {content[:200]}")
        return "\n".join(lines)

    # ── 内部 ──

    def _fetch_em_news(self, code: str, max_news: int) -> list[dict]:
        ak = self._get_ak()
        if ak is None:
            return []
        df = ak.stock_news_em(symbol=code)
        if df is None or df.empty:
            return []
        news_list = []
        for _, row in df.head(max_news).iterrows():
            item = {
                "title": str(row.get("新闻标题", "")),
                "content": str(row.get("新闻内容", "")),
                "source": str(row.get("文章来源", "东方财富")),
                "publish_time": str(row.get("发布时间", "")),
                "url": str(row.get("新闻链接", "")),
                "type": "news",
            }
            if item["title"]:
                news_list.append(item)
        return news_list

    def _fetch_notices(self, code: str, max_news: int) -> list[dict]:
        ak = self._get_ak()
        if ak is None:
            return []
        df = ak.stock_notice_report(symbol=code)
        if df is None or df.empty:
            return []
        news_list = []
        for _, row in df.head(max_news).iterrows():
            item = {
                "title": str(row.get("公告标题", row.get("标题", ""))),
                "content": str(row.get("公告内容", row.get("内容", ""))),
                "source": "公告",
                "publish_time": str(row.get("公告日期", row.get("日期", ""))),
                "url": "",
                "type": "notice",
            }
            if item["title"]:
                news_list.append(item)
        return news_list

    def _read_cache(self, path: Path) -> list[dict] | None:
        if not path.exists():
            return None
        try:
            age = time.time() - path.stat().st_mtime
            if age < _CACHE_HOURS * 3600:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else None
        except Exception:
            pass
        return None

    def _write_cache(self, path: Path, data: list[dict]) -> None:
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass