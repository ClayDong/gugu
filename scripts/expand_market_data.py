#!/usr/bin/env python3
"""全市场行情扩容（2026-11-06）。

为什么做这件事：两条主线已被实测证伪——K 线形态（三个在用策略的选股alpha 全为负）、
内部人交易（样本外 885 笔增持，选股差额 +0.20%，低于 0.4% 往返成本）。两次失败
指向同一个瓶颈，不是假设：

- 标的池 39 只，且是当年用另一个事件筛出来的（自带选择偏差）；
- 行情只覆盖 2023-07 起约 3 年，事件有三十年历史也只能用三年；
- 缓存 199 只标的，其中仅 40 只有一年以上历史。

所以先把数据做厚，再谈策略。

## 两个数据源（东财为主，腾讯备）

东财 `push2his` 一次请求返回整段历史，5 年窗口实测 **0.28 秒/只**，5 并发 20 只
1.16 秒，外推 5945 只约 6 分钟，零失败。腾讯 `fqkline` 单次上限约 640 行，5 年要翻
3 页，约 0.9 秒/只且高频会封禁。所以默认走东财。

腾讯通道保留为备用，它需要自己解决分页：带日期区间的请求**可以**翻页，2 年窗口
（约 486 行）完整、3 年窗口（约 729 行）被截断成最后 640 行。因此按「2 年一页、从
今天往回翻」拼接，锚点取**上一页实际返回的最早日期**而不是窗口起点——这样即使某页
被意外截断，拼接处也不会留下看不见的空洞。

## 两条通道的口径差异（实测，不是推断）

同一只票、同一段重叠日期逐列比对：

    开高低收   最大差 0.005（腾讯保留 3 位小数）
    成交量     完全一致（两者都是「手」）
    成交额     不一致——腾讯给的是复权后的成交额，东财是原始成交额

本脚本统一写**原始成交额**（真实换了多少钱），并且全市场都用同一口径。混着存比
选哪一个都危险：按成交额筛流动性的策略，会在新旧文件之间拿到系统性不同的数值。
`volume` 是原始「手」、`close` 是前复权价，因此 `成交额 / (成交量×100)` 能还原原始
均价，这个自洽关系已写成测试。

## 幸存者偏差

只在市标的会系统性地漏掉退市股，而退市股恰恰是策略最该避开的那一类。所以清单
默认取「在市 5578 只 + 沪深已退市 373 只」，退市股占 6.3%，不纳入等于先赢一半。

## 限流

腾讯对同 IP 的高频请求会直接返回 HTTP 501。实测 0.15 秒间隔跑约 1000 次请求后就被封，
所以默认间隔放到 0.3 秒，并且**一旦见到 403/429/501 就整体停下**，不拿剩下几千只
逐个重试——重试只会把封禁期拖得更长。已写入的文件按行数判定完成，重跑自动续上。

## 用法

    python scripts/expand_market_data.py --build-universe        # 刷新全市场清单
    python scripts/expand_market_data.py --years 5 --dry-run      # 先看规模与耗时
    python scripts/expand_market_data.py --years 5 --workers 4    # 正式跑（可中断续跑）
    python scripts/expand_market_data.py --years 10 --limit 40    # 小样本试跑

退出码：0 = 完成（允许个别失败）；1 = 前置条件不满足
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gugu.analysis.event_study_data import (  # noqa: E402
    _http_json,
    parse_tencent_kline,
    tencent_symbol,
)

DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "parquet_cache"
DEFAULT_UNIVERSE = PROJECT_ROOT / "data" / "universe_a_share.json"

# 单页窗口：2 年约 486 个交易日，能完整取回；3 年会超过接口上限被截断
PAGE_YEARS = 2
PAGE_ROWS = 640
# 少于这个行数视为「没抓全」，下次重跑会补
MIN_ROWS_OK = 250
# 历史不足的告警统一用这个前缀，报告据此与真实数据问题分开统计
SHORT_HISTORY_PREFIX = "历史不足"
# 拼接后中间出现超过这么多自然日的空洞，说明漏页了（春节休市最长约 20 天）
MAX_GAP_DAYS = 40
# 每笔请求之间的最小间隔（秒），全局共享。0.3 是实测安全值：0.15 跑约 1000 次
# 请求后腾讯开始返回 HTTP 501，整体被封比慢一点代价大得多。
MIN_INTERVAL = 0.30
# 东财单次请求实测 0.28 秒、5 并发零失败，间隔可以比腾讯松得多；
# 但仍保留一个下限，避免把并发打到接口上。
EM_MIN_INTERVAL = 0.05
MAX_RETRY = 3
# 这些状态码不是网络抖动，是限流/封禁信号。继续重试只会让封锁更久
BLOCK_CODES = frozenset({403, 429, 501})
# 连续失败这么多次就停：说明不是个别标的没数据，是整条链路出问题了
MAX_CONSECUTIVE_FAILURES = 15

# 东财日 K 接口。klt=101 日线，fqt=1 前复权，lmt 给足上限一次取完
EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_FIELDS1 = "f1,f2,f3,f4,f5,f6"
EM_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57"
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


class BlockedError(RuntimeError):
    """接口开始返回限流/封禁信号。

    单独建一个异常类型，是为了让上层能立刻整体停下——把 5600 只标的逐个重试
    失败既浪费时间，也会把封禁期拖长。
    """


class RateLimiter:
    """全局共享的最小间隔限流器，多线程下也成立。

    注意 sleep 必须在锁外：在锁内 sleep 会让所有线程排队等同一把锁，
    实际吞吐被压成「一次一笔」，并发就白设了。
    """

    def __init__(self, min_interval: float = MIN_INTERVAL) -> None:
        self._interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._last + self._interval - now
            self._last = max(now, self._last + self._interval)
        if delay > 0:
            time.sleep(delay)


def fetch_page(
    symbol: str,
    start: date,
    end: date,
    *,
    limiter: RateLimiter,
    rows: int = PAGE_ROWS,
    retries: int = MAX_RETRY,
) -> pd.DataFrame:
    """取 [start, end] 区间的一页日线（前复权）。失败重试后返回空表，不抛。

    限流类状态码直接抛 :class:`BlockedError`，不重试——重试只是把封锁期拖长。
    """
    code = tencent_symbol(symbol)
    factor = 1 if code.startswith(("sh688", "sh689", "bj")) else 100
    param = f"{code},day,{start.isoformat()},{end.isoformat()},{rows},qfq"
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"

    last_error: Exception | None = None
    for attempt in range(retries):
        limiter.wait()
        try:
            payload = _http_json(url)
            node = payload.get("data", {}).get(code, {})
            if not isinstance(node, dict):
                return pd.DataFrame()  # 该标的无此区间数据（未上市/已退市）
            arr = node.get("qfqday") or node.get("day") or []
            return parse_tencent_kline(arr, volume_factor=factor)
        except HTTPError as exc:
            if exc.code in BLOCK_CODES:
                raise BlockedError(f"接口返回 HTTP {exc.code}，疑似限流") from exc
            last_error = exc
        except Exception as exc:  # noqa: BLE001 — 网络抖动只重试，不中断
            last_error = exc
        time.sleep(0.4 * (attempt + 1))
    if last_error is not None:
        raise ConnectionError(str(last_error)) from last_error
    return pd.DataFrame()


def fetch_history(
    symbol: str,
    years: int,
    *,
    limiter: RateLimiter,
    today: date | None = None,
    page_years: int = PAGE_YEARS,
    fetcher: Callable[..., pd.DataFrame] = fetch_page,
) -> pd.DataFrame:
    """从今天往回按页翻，拼出指定年数的完整日线。

    锚点用「上一页实际返回的最早日期」而不是窗口起点：某一页即使被接口截断，
    下一页也会紧贴着它继续往回取，拼接处不会出现看不见的空洞。
    """
    end = today or date.today()
    floor = end - timedelta(days=365 * years + 30)
    pages: list[pd.DataFrame] = []
    while end > floor:
        start = max(end - timedelta(days=365 * page_years), floor)
        page = fetcher(symbol, start, end, limiter=limiter)
        if page is None or page.empty:
            break
        pages.append(page)
        earliest = pd.to_datetime(page["date"]).min().date()
        if earliest <= floor:
            break
        next_end = earliest - timedelta(days=1)
        if next_end >= end:
            break  # 没有前进，防止死循环
        end = next_end

    if not pages:
        return pd.DataFrame()
    full = (
        pd.concat(pages, ignore_index=True)
        .drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )
    return full


def em_secid(symbol: str) -> str:
    """股票代码 → 东财 secid（如 600000 → 1.600000）。

    6/9 开头归沪市，其余归深市；但 920xxx 是北交所新代码段，虽然 9 开头却属深市侧，
    必须特判，否则整段北交所都取不到。
    """
    if symbol.startswith("920"):
        return f"0.{symbol}"
    return f"{'1' if symbol[0] in '69' else '0'}.{symbol}"


def parse_em_kline(rows: list[str]) -> pd.DataFrame:
    """把东财 klines 的逗号串解析成规范列。

    列序：日期,开,收,高,低,成交量(手),成交额(元)。注意开收高低里**收在第三位**，
    直接按 OHLC 顺序摆会把收盘价当最高价，这种错不会报错、只会让回测悄悄变样。
    """
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    records: list[tuple[Any, ...]] = []
    for line in rows:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        records.append(
            (
                parts[0],
                float(parts[1]),  # 开
                float(parts[3]),  # 高
                float(parts[4]),  # 低
                float(parts[2]),  # 收——东财把收盘放在第三列
                float(parts[5]),  # 成交量（手）
                float(parts[6]),  # 成交额（原始，元）
            )
        )
    frame = pd.DataFrame(
        records, columns=["date", "open", "high", "low", "close", "volume", "amount"]
    )
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.sort_values("date").reset_index(drop=True)


def fetch_eastmoney_history(
    symbol: str,
    years: int,
    *,
    limiter: RateLimiter,
    today: date | None = None,
    retries: int = MAX_RETRY,
) -> pd.DataFrame:
    """一次请求取回整段历史（东财单次就能返回 6000+ 行，不需要翻页）。

    限流类状态码直接抛 :class:`BlockedError`；其余异常重试，用尽后抛
    :class:`ConnectionError`，让上层把这支记为失败而不是「无数据」。
    """
    import httpx

    end = today or date.today()
    begin = end - timedelta(days=365 * years + 30)
    params = {
        "secid": em_secid(symbol),
        "fields1": EM_FIELDS1,
        "fields2": EM_FIELDS2,
        "klt": "101",  # 日线
        "fqt": "1",  # 前复权
        "beg": begin.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "lmt": "100000",
    }

    last_error: Exception | None = None
    for attempt in range(retries):
        limiter.wait()
        try:
            response = httpx.get(EM_KLINE_URL, params=params, headers=EM_HEADERS, timeout=20.0)
            if response.status_code in BLOCK_CODES:
                raise BlockedError(f"东财返回 HTTP {response.status_code}，疑似限流")
            response.raise_for_status()
            payload = response.json()
            node = payload.get("data")
            if not isinstance(node, dict):
                return pd.DataFrame()  # 未上市 / 已退市且窗口内无数据
            return parse_em_kline(node.get("klines") or [])
        except BlockedError:
            raise
        except Exception as exc:  # noqa: BLE001 — 网络抖动只重试，不中断
            last_error = exc
        time.sleep(0.4 * (attempt + 1))
    if last_error is not None:
        raise ConnectionError(str(last_error)) from last_error
    return pd.DataFrame()


def quality_check(frame: pd.DataFrame, years: int) -> list[str]:
    """返回问题列表；空列表表示通过。宁可报告问题，也不假装数据是干净的。

    「历史不足」与「数据有问题」必须分开说：次新股只有几十行是正常的，把它和
    「中间有 70 天空洞」混在一个计数里，281 条告警会把真正该看的十几条淹掉。
    所以历史不足统一用 :data:`SHORT_HISTORY_PREFIX` 开头，由报告单独统计。
    """
    problems: list[str] = []
    if frame.empty:
        return ["空数据"]
    if len(frame) < MIN_ROWS_OK:
        first = pd.to_datetime(frame["date"]).min().strftime("%Y-%m-%d")
        problems.append(f"{SHORT_HISTORY_PREFIX}(自 {first} 起，{len(frame)} 行)")
    dates = pd.to_datetime(frame["date"])
    if dates.duplicated().any():
        problems.append("日期重复")
    if not dates.is_monotonic_increasing:
        problems.append("日期未升序")
    gaps = dates.diff().dt.days.dropna()
    if len(gaps) and gaps.max() > MAX_GAP_DAYS:
        problems.append(f"中间有 {int(gaps.max())} 天空洞")
    for column in ("open", "high", "low", "close"):
        if column not in frame.columns:
            problems.append(f"缺列 {column}")
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or (values <= 0).any():
            problems.append(f"{column} 有非法值")
    return problems


def target_path(output: Path, symbol: str, years: int) -> Path:
    return output / f"{symbol}_{years * 250}d_qfq.parquet"


def already_done(output: Path, symbol: str, years: int) -> bool:
    path = target_path(output, symbol, years)
    if not path.is_file():
        return False
    try:
        return len(pd.read_parquet(path)) >= MIN_ROWS_OK
    except Exception:  # noqa: BLE001 — 坏文件当作没做，重抓
        return False


def build_universe(path: Path) -> list[str]:
    """在市 + 已退市的全部 A 股代码，去掉重复。

    只取在市标的会系统性漏掉退市股，而退市股正是策略最该躲开的那一类。
    """
    import akshare as ak

    codes: set[str] = set()
    listed = ak.stock_info_a_code_name()
    codes.update(str(c).zfill(6) for c in listed["code"])

    for fn_name, column in (
        ("stock_info_sh_delist", "公司代码"),
        ("stock_info_sz_delist", "证券代码"),
    ):
        try:
            frame = getattr(ak, fn_name)()
            codes.update(str(c).zfill(6) for c in frame[column])
        except Exception:  # noqa: BLE001 — 退市清单拿不到就只用在市，但要在报告里说明
            print(f"警告：{fn_name} 获取失败，退市股未纳入清单", file=sys.stderr)

    ordered = sorted(c for c in codes if len(c) == 6 and c.isdigit())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"codes": ordered, "listed": len(listed)}, ensure_ascii=False), encoding="utf-8"
    )
    return ordered


def load_universe(path: Path) -> tuple[list[str], dict[str, Any]]:
    if not path.is_file():
        return [], {}
    data = json.loads(path.read_text(encoding="utf-8"))
    codes = [str(c).zfill(6) for c in data.get("codes", [])]
    return [c for c in codes if len(c) == 6 and c.isdigit()], data


@dataclass
class Progress:
    done: int = 0
    written: int = 0
    skipped: int = 0
    empty: int = 0
    failed: int = 0
    problems: dict[str, list[str]] = field(default_factory=dict)
    bytes_written: int = 0
    rows: list[int] = field(default_factory=list)
    blocked: bool = False
    abort_reason: str | None = None
    consecutive_failures: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self, symbol: str, frame: pd.DataFrame | None, path: Path, issues: list[str]
    ) -> None:
        with self.lock:
            self.done += 1
            self.consecutive_failures = 0
            if frame is None:
                self.empty += 1
                return
            self.written += 1
            self.rows.append(len(frame))
            try:
                self.bytes_written += path.stat().st_size
            except OSError:
                pass
            if issues:
                self.problems[symbol] = issues

    def mark_failed(self, symbol: str, reason: str, *, blocked: bool = False) -> None:
        with self.lock:
            self.done += 1
            self.failed += 1
            self.consecutive_failures += 1
            self.problems[symbol] = [reason]
            if blocked:
                self.blocked = True
                self.abort_reason = reason
            elif self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self.blocked:
                self.blocked = True
                self.abort_reason = f"连续 {self.consecutive_failures} 次失败，已中止"


FETCHERS: dict[str, Callable[..., pd.DataFrame]] = {
    "eastmoney": fetch_eastmoney_history,
    "tencent": fetch_history,
}


def requests_for(source: str, years: int) -> int:
    """每只标的要发几次请求：东财一次取全段，腾讯按 2 年一页翻。"""
    if source == "eastmoney":
        return 1
    return max(1, math.ceil(years / PAGE_YEARS))


def expand(
    symbols: list[str],
    output: Path,
    years: int,
    *,
    workers: int = 4,
    limit: int = 0,
    min_interval: float = MIN_INTERVAL,
    today: date | None = None,
    verbose: bool = True,
    source: str = "eastmoney",
    force: bool = False,
) -> Progress:
    """批量扩容。已完成的标的直接跳过，所以可以随时中断再跑。

    一旦检测到限流（:class:`BlockedError`）或连续失败，就整体停下并记下原因：
    把剩下几千只逐个重试既浪费时间，也会把封禁期拖得更长。
    """
    if source not in FETCHERS:
        raise ValueError(f"未知数据源：{source}")
    fetcher = FETCHERS[source]
    output.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(min_interval)
    progress = Progress()
    todo = symbols if force else [s for s in symbols if not already_done(output, s, years)]
    progress.skipped = len(symbols) - len(todo)
    if limit:
        todo = todo[:limit]
    if verbose:
        print(f"待抓 {len(todo)} 只（跳过已完成 {progress.skipped} 只），年数={years}", flush=True)

    def work(symbol: str) -> None:
        if progress.blocked:
            return
        try:
            frame = fetcher(symbol, years, limiter=limiter, today=today)
        except BlockedError as exc:
            progress.mark_failed(symbol, f"限流中止: {exc}", blocked=True)
            return
        except Exception as exc:  # noqa: BLE001 — 单只失败不影响整批
            progress.mark_failed(symbol, f"抓取失败: {type(exc).__name__}")
            return
        if frame.empty:
            progress.record(symbol, None, target_path(output, symbol, years), [])
            return
        issues = quality_check(frame, years)
        path = target_path(output, symbol, years)
        frame.to_parquet(path, index=False)
        progress.record(symbol, frame, path, issues)

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(work, s): s for s in todo}
            step = max(1, len(futures) // 10)
            for count, future in enumerate(as_completed(futures), start=1):
                future.result()
                if verbose and (count % step == 0 or count == len(futures)):
                    state = "，已中途中止" if progress.blocked else ""
                    print(
                        f"  {count}/{len(futures)} 完成，已写 {progress.written} 只{state}",
                        flush=True,
                    )
                if progress.blocked:
                    for pending in futures:
                        pending.cancel()
                    break
    return progress


def estimate(
    symbols: int,
    years: int,
    per_page_seconds: float,
    workers: int,
    *,
    source: str = "eastmoney",
) -> dict[str, Any]:
    """按实测的单页耗时外推整批的时间与存储。数字来自本次 pilot，不是拍脑袋。"""
    pages = requests_for(source, years)
    requests = symbols * pages
    seconds = requests * per_page_seconds / max(1, workers)
    rows_per_symbol = years * 243
    bytes_per_symbol = rows_per_symbol * 50
    return {
        "symbols": symbols,
        "years": years,
        "source": source,
        "pages_per_symbol": pages,
        "requests": requests,
        "seconds": seconds,
        "hours": seconds / 3600,
        "bytes": symbols * bytes_per_symbol,
        "gb": symbols * bytes_per_symbol / 1024**3,
    }


def _human_seconds(seconds: float) -> str:
    """把秒数说成人话。换源后整批只要几分钟，按「小时」显示会变成 0.0，读起来像没跑。"""
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def _split_warnings(problems: dict[str, list[str]]) -> tuple[int, int]:
    """把告警拆成（历史不足, 真实问题）两类只数。

    一只标的同时有两种问题时计入真实问题——历史短是背景，不是故障。
    """
    short = 0
    real = 0
    for issues in problems.values():
        if any(not str(i).startswith(SHORT_HISTORY_PREFIX) for i in issues):
            real += 1
        elif issues:
            short += 1
    return short, real


def render_markdown(report: dict[str, Any]) -> str:
    source = report.get("source", "eastmoney")
    source_note = "东财单发取全段" if source == "eastmoney" else f"腾讯分页，{PAGE_YEARS} 年/页"
    lines = [
        "# 全市场行情扩容报告",
        "",
        f"- 标的清单：{report['universe_size']} 只"
        + (f"（含退市 {report['delisted']} 只）" if report.get("delisted") else ""),
        f"- 目标历史：{report['years']} 年；数据源：{source}（{source_note}）",
        f"- 输出目录：`{report['output']}`",
        "",
        "## 本次结果",
        "",
        f"- 新写入 {report['written']} 只，跳过已完成 {report['skipped']} 只，"
        f"无数据 {report['empty']} 只，失败 {report['failed']} 只",
        f"- 数据质量告警 {len(report['problems'])} 只"
        + (
            f"（历史不足 {_split_warnings(report['problems'])[0]} 只，"
            f"多为次新股；需关注的 {_split_warnings(report['problems'])[1]} 只）"
            if report["problems"]
            else ""
        ),
        f"- 耗时 {report['elapsed']:.1f} 秒；写入 {report['bytes'] / 1024 / 1024:.1f} MB",
    ]
    if report.get("aborted"):
        lines += [
            "",
            f"> **本次中途中止**：{report.get('abort_reason')}",
            "> 已写入的文件保留（按行数判定完成，重跑会自动跳过），"
            "换个时段重跑即可续上；间隔调大或并发调小，别硬刚限流。",
        ]
    if report["rows"]:
        rows = sorted(report["rows"])
        lines.append(f"- 行数：中位 {rows[len(rows) // 2]}，最少 {rows[0]}，最多 {rows[-1]}")
    if report["problems"]:
        lines += ["", "### 质量告警（前 10 只）", ""]
        for symbol, issues in list(report["problems"].items())[:10]:
            lines.append(f"- {symbol}: {'、'.join(issues)}")

    lines += [
        "",
        "## 外推（按本次实测吞吐）",
        "",
        f"- 单只实测 {report['per_page_seconds']:.2f} 秒，{report['workers']} 并发",
        "",
        "| 规模 | 请求数 | 预计耗时 | 预计存储 |",
        "| --- | --- | --- | --- |",
    ]
    for row in report["forecast"]:
        lines.append(
            f"| {row['symbols']} 只 × {row['years']} 年 | {row['requests']} "
            f"| {_human_seconds(row['seconds'])} | {row['gb']:.2f} GB |"
        )
    lines += [
        "",
        "> 翻页锚点取「上一页实际返回的最早日期」，所以接口提前截断也不会在拼接处留空洞；",
        "> 已完成标的按行数判断并跳过，中断后重跑即可续上。",
        "> 退市股必须纳入：只取在市标的等于先剔掉最该躲开的那批。",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="全市场行情扩容")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--build-universe", action="store_true", help="刷新全市场清单（含退市）")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--source",
        choices=sorted(FETCHERS),
        default="eastmoney",
        help="行情源：eastmoney 单发取全段（默认，快且不易限流）；tencent 需翻页",
    )
    parser.add_argument("--force", action="store_true", help="已完成的也重抓（换源后统一口径用）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只，用于试跑")
    parser.add_argument("--min-interval", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true", help="只打印规模与外推，不抓取")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    universe_path = Path(args.universe)
    output = Path(args.output)

    if args.build_universe:
        codes = build_universe(universe_path)
        print(f"清单已写入 {universe_path}：{len(codes)} 只", flush=True)
        # 只建清单就退出：否则会顺手开抓全市场，几小时的活不该由一次清单刷新触发
        return 0

    symbols, meta = load_universe(universe_path)
    if not symbols:
        print(f"标的清单为空：{universe_path}（先跑 --build-universe）", file=sys.stderr)
        return 1

    source = args.source
    # 间隔默认跟着源走：东财单发实测宽松，腾讯翻页高频会被封
    min_interval = (
        args.min_interval
        if args.min_interval is not None
        else (EM_MIN_INTERVAL if source == "eastmoney" else MIN_INTERVAL)
    )
    pending_all = (
        list(symbols)
        if args.force
        else [s for s in symbols if not already_done(output, s, args.years)]
    )
    pending = pending_all[: args.limit] if args.limit else pending_all
    delisted = len(symbols) - int(meta.get("listed", len(symbols)))

    if args.dry_run:
        # 用真实请求测吞吐，取中位数，不用整批外推值反推（那会把导入和冷启动算进去）
        limiter = RateLimiter(min_interval)
        end = date.today()
        start = end - timedelta(days=365 * PAGE_YEARS)
        samples: list[float] = []
        for symbol in pending[:3]:
            probe_start = time.monotonic()
            try:
                if source == "tencent":
                    fetch_page(symbol, start, end, limiter=limiter)
                else:
                    fetch_eastmoney_history(symbol, args.years, limiter=limiter)
            except Exception:  # noqa: BLE001 — 探测失败也要给出外推，用保守值
                continue
            samples.append(time.monotonic() - probe_start)
        per_request = sorted(samples)[len(samples) // 2] if samples else 0.6
        report = {
            "universe_size": len(symbols),
            "delisted": delisted,
            "years": args.years,
            "source": source,
            "output": str(output),
            "written": 0,
            "skipped": len(symbols) - len(pending_all),
            "empty": 0,
            "failed": 0,
            "problems": {},
            "rows": [],
            "elapsed": 0.0,
            "bytes": 0,
            "per_page_seconds": per_request,
            "workers": args.workers,
            "forecast": [
                estimate(len(pending), args.years, per_request, args.workers, source=source),
                estimate(len(symbols), args.years, per_request, args.workers, source=source),
                estimate(len(symbols), 10, per_request, args.workers, source=source),
            ],
        }
        print(
            json.dumps(report, ensure_ascii=False, indent=1)
            if args.json
            else render_markdown(report)
        )
        return 0

    started = time.monotonic()
    progress = expand(
        symbols,
        output,
        args.years,
        workers=args.workers,
        limit=args.limit,
        min_interval=min_interval,
        source=source,
        force=args.force,
    )
    elapsed = time.monotonic() - started
    per_request = elapsed / max(1, progress.written * requests_for(source, args.years))

    report = {
        "universe_size": len(symbols),
        "delisted": delisted,
        "years": args.years,
        "source": source,
        "output": str(output),
        "written": progress.written,
        "skipped": progress.skipped,
        "empty": progress.empty,
        "failed": progress.failed,
        "problems": progress.problems,
        "rows": progress.rows,
        "elapsed": elapsed,
        "bytes": progress.bytes_written,
        "aborted": progress.blocked,
        "abort_reason": progress.abort_reason,
        "per_page_seconds": per_request,
        "workers": args.workers,
        "forecast": [
            estimate(len(symbols), args.years, per_request, args.workers, source=source),
            estimate(len(symbols), 10, per_request, args.workers, source=source),
        ],
    }
    print(
        json.dumps(report, ensure_ascii=False, indent=1, default=str)
        if args.json
        else render_markdown(report)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
