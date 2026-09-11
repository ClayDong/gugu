"""新闻采集器：通过 akshare 获取东方财富个股新闻与公告。

移植自 MakingMoney 项目的 news_fetcher.py，适配 gugu logger + 缓存机制。

数据源：
1. ak.stock_news_em(symbol) — 东方财富个股新闻（标题/内容/来源/时间）
2. ak.stock_notice_report(symbol="全部", date) — 沪深京全市场公告，按代码过滤

缓存策略：2 小时文件缓存，避免盘中重复请求。

2026-09-11 修复（告警评估员）：
- 新闻源 100% 失败（2026-09-07 起 148+ 次/日 WARNING）：
  pandas 3.x 默认 arrow-backed 字符串后端，akshare 内部
  ``df["新闻内容"].str.replace(r"\\u3000", "", regex=True)`` 走 pyarrow RE2，
  RE2 不支持 ``\\u`` 转义 → ArrowInvalid。调用期间临时切回 python 字符串后端规避。
- 公告源 100% 失败（KeyError '600519'）：``stock_notice_report`` 的 ``symbol``
  参数是「公告类型」（全部/重大事项/…），传股票代码必然 KeyError。
  改为按交易日拉全市场公告再按代码过滤（进程内按日缓存）。
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from gugu.utils.calendar import is_trading_day
from gugu.utils.log import get_logger

logger = get_logger()

_CACHE_HOURS = 2
_MAX_NEWS = 10
# 公告看板回溯交易日数（当日公告可能尚未入库，回看前一交易日）
_NOTICE_LOOKBACK_DAYS = 2
# 公告看板向后最多搜索的自然日数（覆盖周末/短假）
_NOTICE_SEARCH_SPAN = 6


@contextlib.contextmanager
def _python_string_storage():
    """临时将 pandas 字符串后端切回 python（规避 pyarrow RE2 不支持 ``\\u``）。

    akshare 部分接口用 ``str.replace(r"\\u3000", ...)``（Python re 语义），
    而 pandas 3.x 默认 arrow-backed 字符串会走 pyarrow RE2，遇 ``\\u`` 转义
    直接抛 ``ArrowInvalid``。仅在该调用期间切换，退出时无条件还原全局选项。
    """
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas 是强依赖，仅作防御
        yield
        return

    prev: Any = None
    try:
        prev = pd.options.mode.string_storage
        pd.options.mode.string_storage = "python"
    except Exception as e:  # pragma: no cover - 旧/新版本选项缺失时降级
        logger.debug("pandas string_storage 切换失败，按原样调用: {}", e)
        prev = None
    try:
        yield
    finally:
        if prev is not None:
            with contextlib.suppress(Exception):
                pd.options.mode.string_storage = prev


class NewsFetcher:
    """新闻采集器。"""

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[4]
        self._cache_dir = (
            Path(cache_dir) if cache_dir else project_root / "data" / "sentiment" / "news_cache"
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._ak = None
        # 公告看板进程内缓存（按日失效，避免每只股票/每轮扫描重复拉全市场）
        self._notice_board: dict[str, list[dict[str, Any]]] | None = None
        self._notice_board_key: str = ""

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
                logger.warning("个股公告获取失败 {}: {}", symbol, e)

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
        # 2026-09-11：pandas 3.x arrow 字符串后端会让 akshare 内部 RE2 正则
        # （r"\u3000"）报 ArrowInvalid，切回 python 后端调用。
        with _python_string_storage():
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
        """获取个股公告（按交易日全市场公告看板 + 代码过滤）。

        2026-09-11 修复：``ak.stock_notice_report`` 的 ``symbol`` 参数是
        「公告类型」（全部/重大事项/财务报告/…），旧实现传股票代码必然
        ``KeyError``（当日日志 148+ 次 WARNING，公告源 100% 失效）。
        正确用法是按日期取全市场公告，再按代码过滤。
        """
        board = self._load_notice_board()
        # 兼容带市场前后缀的代码（sz301588 / 600519.SH）→ 东财看板代码为 6 位纯数字
        digits = "".join(ch for ch in code if ch.isdigit())
        return (board.get(code) or board.get(digits) or [])[:max_news]

    def _load_notice_board(self) -> dict[str, list[dict]]:
        """按交易日拉取全市场公告并按股票代码分组（进程内按日缓存）。

        看板回溯最近 ``_NOTICE_LOOKBACK_DAYS`` 个交易日并合并（当日公告
        可能尚未入库），同一天内只请求一次，避免每只股票重复拉取。
        """
        key = date.today().isoformat()
        if self._notice_board is not None and self._notice_board_key == key:
            return self._notice_board

        board: dict[str, list[dict]] = {}
        ak = self._get_ak()
        if ak is not None:
            fetched = 0
            for offset in range(_NOTICE_SEARCH_SPAN):
                if fetched >= _NOTICE_LOOKBACK_DAYS:
                    break
                day = date.today() - timedelta(days=offset)
                if not is_trading_day(day):
                    continue
                fetched += 1
                try:
                    self._merge_notice_board(board, self._fetch_notice_board(ak, day))
                except Exception as e:
                    logger.warning("全市场公告获取失败 {}: {}", day.isoformat(), e)

        logger.info(
            "公告看板: {} 只股票 / {} 条公告（进程内缓存）",
            len(board),
            sum(len(v) for v in board.values()),
        )
        self._notice_board, self._notice_board_key = board, key
        return board

    def _fetch_notice_board(self, ak, day: date) -> dict[str, list[dict]]:
        """拉取某一交易日的全市场公告，返回 {代码: [公告, …]}。"""
        with _python_string_storage():
            df = ak.stock_notice_report(symbol="全部", date=day.strftime("%Y%m%d"))
        if df is None or df.empty:
            return {}
        board: dict[str, list[dict]] = {}
        for row in df.to_dict("records"):
            code = str(row.get("代码", "") or "").strip()
            title = str(row.get("公告标题", "") or "").strip()
            if not code or not title:
                continue
            board.setdefault(code, []).append(
                {
                    "title": title,
                    "content": "",
                    "source": "公告",
                    "publish_time": str(row.get("公告日期", "") or ""),
                    "url": str(row.get("网址", "") or ""),
                    "type": "notice",
                }
            )
        return board

    @staticmethod
    def _merge_notice_board(target: dict[str, list[dict]], extra: dict[str, list[dict]]) -> None:
        """把 extra 看板合并进 target（按标题去重，新公告在前）。"""
        for code, items in extra.items():
            bucket = target.setdefault(code, [])
            seen = {it["title"] for it in bucket}
            for item in items:
                if item["title"] not in seen:
                    bucket.append(item)
                    seen.add(item["title"])

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
        with contextlib.suppress(Exception):
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
