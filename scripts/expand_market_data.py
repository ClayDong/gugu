#!/usr/bin/env python3
"""全市场行情扩容（2026-11-06）。

为什么做这件事：两条主线已被实测证伪——K 线形态（三个在用策略的选股alpha 全为负）、
内部人交易（样本外 885 笔增持，选股差额 +0.20%，低于 0.4% 往返成本）。两次失败
指向同一个瓶颈，不是假设：

- 标的池 39 只，且是当年用另一个事件筛出来的（自带选择偏差）；
- 行情只覆盖 2023-07 起约 3 年，事件有三十年历史也只能用三年；
- 缓存 199 只标的，其中仅 40 只有一年以上历史。

所以先把数据做厚，再谈策略。

## 抓取口径（全部实测过，不是估计）

腾讯 `fqkline` 接口单次最多返回约 640 行，请求更多天数不但拿不到更多，还会退化成
固定的最近 640 行。但**带日期区间的请求可以翻页**：

    2 年窗口（约 486 行）→ 完整
    3 年窗口（约 729 行）→ 截断成最后 640 行

因此按「2 年一页、从今天往回翻」拼接。往回翻的锚点是**上一页实际返回的最早日期**，
不是窗口起点——这样即使某一页被意外截断，也不会在拼接处留下看不见的空洞。

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
# 拼接后中间出现超过这么多自然日的空洞，说明漏页了（春节休市最长约 20 天）
MAX_GAP_DAYS = 40
# 每笔请求之间的最小间隔（秒），全局共享。0.3 是实测安全值：0.15 跑约 1000 次
# 请求后腾讯开始返回 HTTP 501，整体被封比慢一点代价大得多。
MIN_INTERVAL = 0.30
MAX_RETRY = 3
# 这些状态码不是网络抖动，是限流/封禁信号。继续重试只会让封锁更久
BLOCK_CODES = frozenset({403, 429, 501})
# 连续失败这么多次就停：说明不是个别标的没数据，是整条链路出问题了
MAX_CONSECUTIVE_FAILURES = 15


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


def quality_check(frame: pd.DataFrame, years: int) -> list[str]:
    """返回问题列表；空列表表示通过。宁可报告问题，也不假装数据是干净的。"""
    problems: list[str] = []
    if frame.empty:
        return ["空数据"]
    if len(frame) < MIN_ROWS_OK:
        problems.append(f"行数偏少({len(frame)})")
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
) -> Progress:
    """批量扩容。已完成的标的直接跳过，所以可以随时中断再跑。

    一旦检测到限流（:class:`BlockedError`）或连续失败，就整体停下并记下原因：
    把剩下几千只逐个重试既浪费时间，也会把封禁期拖得更长。
    """
    output.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(min_interval)
    progress = Progress()
    todo = [s for s in symbols if not already_done(output, s, years)]
    progress.skipped = len(symbols) - len(todo)
    if limit:
        todo = todo[:limit]
    if verbose:
        print(f"待抓 {len(todo)} 只（跳过已完成 {progress.skipped} 只），年数={years}", flush=True)

    def work(symbol: str) -> None:
        if progress.blocked:
            return
        try:
            frame = fetch_history(symbol, years, limiter=limiter, today=today)
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


def estimate(symbols: int, years: int, per_page_seconds: float, workers: int) -> dict[str, Any]:
    """按实测的单页耗时外推整批的时间与存储。数字来自本次 pilot，不是拍脑袋。"""
    pages = max(1, math.ceil(years / PAGE_YEARS))
    requests = symbols * pages
    seconds = requests * per_page_seconds / max(1, workers)
    rows_per_symbol = years * 243
    bytes_per_symbol = rows_per_symbol * 50
    return {
        "symbols": symbols,
        "years": years,
        "pages_per_symbol": pages,
        "requests": requests,
        "seconds": seconds,
        "hours": seconds / 3600,
        "bytes": symbols * bytes_per_symbol,
        "gb": symbols * bytes_per_symbol / 1024**3,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 全市场行情扩容报告",
        "",
        f"- 标的清单：{report['universe_size']} 只"
        + (f"（含退市 {report['delisted']} 只）" if report.get("delisted") else ""),
        f"- 目标历史：{report['years']} 年；分页窗口：{PAGE_YEARS} 年/页",
        f"- 输出目录：`{report['output']}`",
        "",
        "## 本次结果",
        "",
        f"- 新写入 {report['written']} 只，跳过已完成 {report['skipped']} 只，"
        f"无数据 {report['empty']} 只，失败 {report['failed']} 只",
        f"- 数据质量告警 {len(report['problems'])} 只",
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
        f"- 单页实测 {report['per_page_seconds']:.2f} 秒，{report['workers']} 并发",
        "",
        "| 规模 | 请求数 | 预计耗时 | 预计存储 |",
        "| --- | --- | --- | --- |",
    ]
    for row in report["forecast"]:
        lines.append(
            f"| {row['symbols']} 只 × {row['years']} 年 | {row['requests']} "
            f"| {row['hours']:.1f} 小时 | {row['gb']:.2f} GB |"
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
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只，用于试跑")
    parser.add_argument("--min-interval", type=float, default=MIN_INTERVAL)
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

    pending_all = [s for s in symbols if not already_done(output, s, args.years)]
    pending = pending_all[: args.limit] if args.limit else pending_all
    delisted = len(symbols) - int(meta.get("listed", len(symbols)))

    if args.dry_run:
        # 用真实单页请求测吞吐：只看一个有代表性的 2 年窗口，取中位数，
        # 不用整批外推值反推（那样会把导入开销和冷启动都算进去）
        limiter = RateLimiter(args.min_interval)
        end = date.today()
        start = end - timedelta(days=365 * PAGE_YEARS)
        samples: list[float] = []
        for symbol in pending[:3]:
            probe_start = time.monotonic()
            try:
                fetch_page(symbol, start, end, limiter=limiter)
            except Exception:  # noqa: BLE001 — 探测失败也要给出外推，用保守值
                continue
            samples.append(time.monotonic() - probe_start)
        per_page = sorted(samples)[len(samples) // 2] if samples else 0.6
        report = {
            "universe_size": len(symbols),
            "delisted": delisted,
            "years": args.years,
            "output": str(output),
            "written": 0,
            "skipped": len(symbols) - len(pending_all),
            "empty": 0,
            "failed": 0,
            "problems": {},
            "rows": [],
            "elapsed": 0.0,
            "bytes": 0,
            "per_page_seconds": per_page,
            "workers": args.workers,
            "forecast": [
                estimate(len(pending), args.years, per_page, args.workers),
                estimate(len(symbols), args.years, per_page, args.workers),
                estimate(len(symbols), 10, per_page, args.workers),
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
        min_interval=args.min_interval,
    )
    elapsed = time.monotonic() - started
    per_page = elapsed / max(1, progress.written * math.ceil(args.years / PAGE_YEARS))

    report = {
        "universe_size": len(symbols),
        "delisted": delisted,
        "years": args.years,
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
        "per_page_seconds": per_page,
        "workers": args.workers,
        "forecast": [
            estimate(len(symbols), args.years, per_page, args.workers),
            estimate(len(symbols), 10, per_page, args.workers),
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
