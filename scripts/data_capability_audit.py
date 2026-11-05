#!/usr/bin/env python3
"""数据能力审计：换方向之前，先确认手里到底有什么数据。

背景（2026-11-05）：K 线形态这条路已被证伪——三个在用策略的选股alpha 全部为负，
任一正超额都是池子同期涨幅。换方向（事件驱动 / 资金流 / 内部人交易）之前必须先
回答一个更基础的问题：**数据够不够支撑那个方向？**

经验上，换方向失败极少是因为"假设不好"，多数是因为：
- 数据只有几个快照，做不出时间序列；
- 数据很长但缺公告日，只能用财报期 → 前视偏差；
- 数据覆盖全市场，但池子里一只都没有。

所以本脚本对每个数据集只回答五件事，且全部可复算：
1. 有多少个**不同日期**（不是文件数——快照文件互相重叠，文件数会骗人）；
2. 时间跨度到哪儿（新鲜度）；
3. 覆盖多少只股票，其中多少只在我们自己的池子里；
4. 有没有可用于对齐的时间列（没有就只能放弃事件研究）；
5. 总行数是不是"同一份快照重复 N 次"（重复因子）——行数多不等于覆盖广。

三条容易骗人的口径，本脚本显式拆穿：

- **日期退化**：缺失值被填 0 的列会被 pandas 压成 ``1970-01-01``，看上去"有日期"，
  实际是脏数据。低于 :data:`MIN_VALID_YEAR` 的日期一律不认；此时回退到文件名里的
  快照日期，并把来源标成「文件名」，绝不假装是列里的日期。
- **日期列冗余**：``announcement_date`` 可能只是 ``change_date`` 的复制（实测
  insider_trading 145471 行滞后恒为 0 天）。此时它**不是**独立公告日，不能据此
  声称"无前视"。逐行比对完全相同即判为冗余。
- **池内零命中一票否决**：数据再长、再干净，如果标的池里一只都没有，这个方向现在
  就用不上。

用法：
    python scripts/data_capability_audit.py
    python scripts/data_capability_audit.py --as-of 2026-11-05 --json
退出码：0 = 审计完成；1 = 数据目录不存在
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

DEFAULT_ALTALPHA = PROJECT_ROOT / "data" / "altalpha"
DEFAULT_HOLDER = PROJECT_ROOT / "data" / "holder_structure"
DEFAULT_KLINE = PROJECT_ROOT / "data" / "parquet_cache"
DEFAULT_POOL = PROJECT_ROOT / "data" / "linjudaye_event_study_40.json"

# 已知数据集前缀：文件名常被追加查询参数（_stock_lhb_detail_em_end_date=...），
# 必须归并回同一个数据集，否则"18 个文件"会被误读成 18 份独立数据。
KNOWN_DATASETS = (
    "earnings_forecast",
    "insider_trading",
    "holder_structure",
    "dragon_tiger",
    "margin_trade",
    "block_trade",
    "share_unlock",
)

# 时间列按可信度排序：交易/变动日 > 公告日 > 期末日。
# 事件研究必须用"当时可知"的日期，所以这里只做识别，不做替换。
DATE_COLUMNS = (
    "trade_date",
    "change_date",
    "announcement_date",
    "announce_date",
    "unlock_date",
    "end_date",
    "date",
)
CODE_COLUMNS = ("code", "symbol", "股票代码")
DIRECTION_COLUMNS = ("direction", "变动方向", "增减持")

# A 股最早数据在 1990 年之后；数值 0 / 缺失被压成 1970-01-01，低于此线一律判退化。
MIN_VALID_YEAR = 1990
# 重复因子达到这个倍数，就说明文件是滚动全量快照而非增量，行数不代表覆盖广度。
DUPLICATE_WARN = 3.0
# 缺日期行占比达到这个比例，才值得单独告警（零星缺失不必刷屏）。
NULL_DATE_WARN = 0.10

FRESH_DAYS = 7
STALE_DAYS = 30
COLD_DAYS = 90

# 年交易日约 250；不足一年做不出可检验的策略，只能做可行性判断
ONE_YEAR_DAYS = 250
MIN_EVENT_DAYS = 60
# 池内命中低于这个数，事件研究只能得到个例，不构成统计证据
MIN_POOL_HITS = 5
# 行情窗口内的池内事件数低于这个量级，样本撑不起检验（项目既有前向验证用 N=41）
MIN_POOL_EVENTS = 30

FILENAME_DATE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")


@dataclass
class DatasetAudit:
    """一个数据集的体检结果。"""

    name: str
    files: int = 0
    rows: int = 0
    date_column: str | None = None
    date_columns: list[str] = field(default_factory=list)
    redundant_date_columns: list[str] = field(default_factory=list)
    date_min: str | None = None
    date_max: str | None = None
    distinct_dates: int = 0
    codes: int = 0
    pool_hits: int = 0
    pool_events: int = 0
    # None = 未统计（日期退化或无行情覆盖），与"统计结果为 0"必须区分
    pool_events_in_window: int | None = None
    date_source: str = "列"
    degenerate_date: bool = False
    date_null_rows: int = 0
    rows_largest_file: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def duplicate_factor(self) -> float | None:
        """总行数 / 单文件最大行数。≈1 是增量，≈文件数 是同一份快照重复。"""
        if not self.rows_largest_file:
            return None
        return round(self.rows / self.rows_largest_file, 1)

    def as_dict(self, as_of: date) -> dict[str, Any]:
        stale = None
        if self.date_max:
            stale = (as_of - date.fromisoformat(self.date_max)).days
        return {
            "dataset": self.name,
            "files": self.files,
            "rows": self.rows,
            "date_column": self.date_column,
            "date_columns": self.date_columns,
            "redundant_date_columns": self.redundant_date_columns,
            "date_min": self.date_min,
            "date_max": self.date_max,
            "distinct_dates": self.distinct_dates,
            "codes": self.codes,
            "pool_hits": self.pool_hits,
            "pool_events": self.pool_events,
            "pool_events_in_window": self.pool_events_in_window,
            "date_source": self.date_source,
            "degenerate_date": self.degenerate_date,
            "date_null_rows": self.date_null_rows,
            "date_null_ratio": (round(self.date_null_rows / self.rows, 3) if self.rows else 0.0),
            "duplicate_factor": self.duplicate_factor,
            "days_stale": stale,
            "freshness": freshness_label(stale, self.date_max is not None),
            "adequacy": adequacy_label(self.distinct_dates),
            "errors": self.errors,
        }


def canonical_dataset(filename: str, default: str | None = None) -> str:
    """把 ``20260715_dragon_tiger_stock_lhb_detail_em_end_date=...`` 归回 ``dragon_tiger``。

    纯日期文件名（``2026-07-28.parquet``）剥完前缀什么都不剩，此时用 ``default``
    ——目录本身就是数据集身份，否则 17 个快照会被当成 17 个数据集。
    """
    stem = Path(filename).stem
    stem = FILENAME_DATE.sub("", stem, count=1).lstrip("_-")
    for base in KNOWN_DATASETS:
        if stem == base or stem.startswith(f"{base}_"):
            return base
    if not stem:
        return default or "unknown"
    return stem.split("_")[0]


def filename_date(filename: str) -> str | None:
    """从文件名里取快照日期，取不到或非法返回 None。"""
    match = FILENAME_DATE.search(Path(filename).stem)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_code(value: Any) -> str | None:
    """把 sh600030 / 600030.SH / 600030 / 600030.0 统一成 6 位数字。"""
    text = str(value).strip()
    if text.endswith(".0"):  # 整型列被读成 float 时会出现 600030.0
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    if len(digits) < 6:
        return None
    return digits[-6:]


def pick_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lower = {str(c).lower(): str(c) for c in columns}
    for name in candidates:
        if name in lower:
            return lower[name]
    return None


def present_date_columns(columns: list[str]) -> list[str]:
    """按可信度优先级列出数据里真实存在的时间列。"""
    lower = {str(c).lower() for c in columns}
    return [c for c in DATE_COLUMNS if c in lower]


def freshness_label(days_stale: int | None, has_data: bool) -> str:
    if not has_data or days_stale is None:
        return "无数据"
    if days_stale <= FRESH_DAYS:
        return "新鲜"
    if days_stale <= STALE_DAYS:
        return "偏旧"
    if days_stale <= COLD_DAYS:
        return "陈旧"
    return "过期"


def adequacy_label(distinct_dates: int) -> str:
    """够不够做统计。阈值写死在这里，避免报告里各说各话。"""
    if distinct_dates >= ONE_YEAR_DAYS:
        return "够一年"
    if distinct_dates >= MIN_EVENT_DAYS:
        return "仅够事件研究"
    if distinct_dates > 0:
        return "只够可行性"
    return "不可用"


def audit_dataset(
    name: str,
    files: list[Path],
    pool_codes: set[str],
    kline_cover: dict[str, tuple[str, str]] | None = None,
) -> DatasetAudit:
    """读取一组文件，统计日期覆盖 / 股票覆盖 / 池子命中 / 池内事件样本。

    ``kline_cover`` 给的是每只标的行情实际覆盖的起止日。事件发生得再早，没有对应
    行情也算不出收益，所以"池内事件数"必须限定在这个窗口内——否则会把三十年的
    历史事件当成可用样本。
    """
    audit = DatasetAudit(name=name, files=len(files))
    column_dates: set[str] = set()
    file_dates: set[str] = set()
    codes: set[str] = set()
    # 逐行比对低优先级时间列是否等于最高优先级列：全等即冗余，不能当独立信息用
    equal_counts: dict[str, list[int]] = {}
    # 池内行的（标的, 日期, 方向）三元组，跨文件汇总后统一去重
    pool_frames: list[pd.DataFrame] = []

    for path in files:
        snapshot = filename_date(path.name)
        if snapshot:
            file_dates.add(snapshot)
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001 — 坏文件只记录，不中断整表
            audit.errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        audit.rows += len(frame)
        audit.rows_largest_file = max(audit.rows_largest_file, len(frame))

        schema_hit = present_date_columns(list(frame.columns))
        if not audit.date_columns:
            audit.date_columns = schema_hit
        audit.date_column = audit.date_column or pick_column(list(frame.columns), DATE_COLUMNS)

        if audit.date_column:
            parsed = pd.to_datetime(frame[audit.date_column], errors="coerce")
            valid = parsed[parsed.dt.year >= MIN_VALID_YEAR]
            audit.date_null_rows += int(len(parsed) - len(valid))
            column_dates.update(valid.dt.strftime("%Y-%m-%d").tolist())

        # 只在 schema 一致的文件之间做冗余比对，避免拿不同含义的列互相比
        if schema_hit == audit.date_columns and len(schema_hit) > 1:
            resolved = {c: pick_column(list(frame.columns), (c,)) for c in schema_hit}
            base = pd.to_datetime(frame[resolved[schema_hit[0]]], errors="coerce")
            base_ok = base.notna() & (base.dt.year >= MIN_VALID_YEAR)
            for other in schema_hit[1:]:
                series = pd.to_datetime(frame[resolved[other]], errors="coerce")
                pair = base_ok & series.notna() & (series.dt.year >= MIN_VALID_YEAR)
                same = int((pair & (base == series)).sum())
                bucket = equal_counts.setdefault(other, [0, 0])
                bucket[0] += same
                bucket[1] += int(pair.sum())

        code_column = pick_column(list(frame.columns), CODE_COLUMNS)
        direction_column = pick_column(list(frame.columns), DIRECTION_COLUMNS)
        if code_column:
            # 只对唯一值做规范化再映射回来：350 万行逐行跑正则会慢到不可用
            mapping = {v: normalize_code(v) for v in frame[code_column].dropna().unique()}
            codes.update(c for c in mapping.values() if c)
            if pool_codes and audit.date_column:
                normalized_series = frame[code_column].map(mapping)
                in_pool = normalized_series.isin(pool_codes)
                if int(in_pool.sum()):
                    pool_frames.append(
                        pd.DataFrame(
                            {
                                "code": normalized_series[in_pool].to_numpy(),
                                "dt": pd.to_datetime(
                                    frame.loc[in_pool, audit.date_column], errors="coerce"
                                ).to_numpy(),
                                "direction": (
                                    frame.loc[in_pool, direction_column].to_numpy()
                                    if direction_column
                                    else None
                                ),
                            }
                        )
                    )

    if column_dates:
        dates = column_dates
        audit.date_source = "列"
    elif file_dates:
        dates = file_dates
        audit.date_source = "文件名"
        # 列里有值但全被判退化：那是 0 填充，不是日期。此时事件窗口无法按列日期算
        audit.degenerate_date = audit.date_null_rows > 0
        audit.pool_events_in_window = None
    else:
        dates = set()

    if dates:
        audit.date_min = min(dates)
        audit.date_max = max(dates)
        audit.distinct_dates = len(dates)
    audit.date_null_rows = min(audit.date_null_rows, audit.rows)
    audit.redundant_date_columns = sorted(
        col for col, (same, total) in equal_counts.items() if total and same == total
    )
    audit.codes = len(codes)
    audit.pool_hits = len(codes & pool_codes)
    if pool_frames:
        events = pd.concat(pool_frames, ignore_index=True).dropna(subset=["dt"])
        # 同一事件会在 24 份快照里各出现一次，且一次公告会因多个变动人拆成多行；
        # 只有（标的, 日期, 方向）三元组去重后的数字才是真实样本量
        events = events.drop_duplicates(subset=["code", "dt", "direction"])
        audit.pool_events = int(len(events))
        if kline_cover and audit.date_source == "列":
            audit.pool_events_in_window = _count_in_window(events, kline_cover)
    return audit


def _count_in_window(events: pd.DataFrame, kline_cover: dict[str, tuple[str, str]]) -> int:
    """统计落在各标的自身行情覆盖区间内的事件数。

    事件有三十年历史、行情只有三年，可用样本就是这三年里的那些，不是全部。
    """
    total = 0
    for code, group in events.groupby("code"):
        window = kline_cover.get(code)
        if not window:
            continue
        low, high = pd.Timestamp(window[0]), pd.Timestamp(window[1])
        total += int(((group["dt"] >= low) & (group["dt"] <= high)).sum())
    return total


def _window_event_stats(
    date_values: pd.Series,
    code_values: pd.Series,
    direction_values: pd.Series,
    kline_cover: dict[str, tuple[str, str]],
) -> tuple[int, int]:
    """返回 (落在行情窗口内的行数, 去重后的独立事件数)。

    一行不等于一个事件：同一笔增减持会因多个变动人拆成多行，一次公告可能出十几行。
    直接用行数当样本量会系统性高估 N，所以按（标的, 日期, 方向）去重后再数。
    """
    if date_values.empty:
        return 0, 0
    events = pd.DataFrame(
        {
            "code": code_values.to_numpy(),
            "dt": pd.to_datetime(date_values, errors="coerce").to_numpy(),
            "direction": direction_values.to_numpy(),
        }
    ).dropna(subset=["dt"])
    if events.empty:
        return 0, 0
    rows = 0
    unique = 0
    for code, group in events.groupby("code"):
        window = kline_cover.get(code)
        if not window:
            continue
        low, high = pd.Timestamp(window[0]), pd.Timestamp(window[1])
        inside = group[(group["dt"] >= low) & (group["dt"] <= high)]
        rows += len(inside)
        unique += len(inside.drop_duplicates(subset=["dt", "direction"]))
    return rows, unique


def kline_coverage(directory: Path) -> dict[str, tuple[str, str]]:
    """每只标的行情实际覆盖的起止日。

    事件研究真正的窗口由行情决定：有 30 年事件但只有 3 年行情，可用样本就是 3 年
    内的那些，而不是 30 年。这份 map 用来把"池内事件行数"压到"真正可回测的行数"。
    """
    cover: dict[str, tuple[str, str]] = {}
    for path in sorted(directory.glob("*.parquet")):
        symbol = normalize_code(path.stem.split("_")[0])
        if not symbol:
            continue
        try:
            frame = pd.read_parquet(path)
        except Exception:  # noqa: BLE001 — 坏文件在 audit_kline_cache 里报，这里跳过
            continue
        if "date" not in frame.columns:
            continue
        stamps = pd.to_datetime(frame["date"], errors="coerce").dropna()
        if stamps.empty:
            continue
        low, high = stamps.min().strftime("%Y-%m-%d"), stamps.max().strftime("%Y-%m-%d")
        previous = cover.get(symbol)
        if previous:
            low, high = min(low, previous[0]), max(high, previous[1])
        cover[symbol] = (low, high)
    return cover


def audit_kline_cache(directory: Path, pool_codes: set[str]) -> dict[str, Any]:
    """行情缓存单独盘：只关心行数分布与最新日期。"""
    files = sorted(directory.glob("*.parquet"))
    if not files:
        return {
            "files": 0,
            "symbols": 0,
            "median_rows": 0,
            "min_rows": 0,
            "latest": None,
            "pool_hits": 0,
            "errors": [],
        }
    rows: list[int] = []
    symbols: set[str] = set()
    latest: str | None = None
    errors: list[str] = []
    for path in files:
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        rows.append(len(frame))
        symbol = normalize_code(path.stem.split("_")[0])
        if symbol:
            symbols.add(symbol)
        if "date" in frame.columns:
            maximum = pd.to_datetime(frame["date"], errors="coerce").max()
            if pd.notna(maximum):
                text = maximum.strftime("%Y-%m-%d")
                latest = text if latest is None else max(latest, text)
    rows.sort()
    return {
        "files": len(files),
        "symbols": len(symbols),
        "median_rows": rows[len(rows) // 2] if rows else 0,
        "min_rows": rows[0] if rows else 0,
        "latest": latest,
        "pool_hits": len(symbols & pool_codes),
        "errors": errors,
    }


def load_pool_codes(pool_file: Path) -> set[str]:
    if not pool_file.exists():
        return set()
    data = json.loads(pool_file.read_text(encoding="utf-8"))
    codes = {normalize_code(event.get("code", "")) for event in data.get("events", [])}
    return {c for c in codes if c}


def collect_families(directory: Path, default_name: str | None = None) -> dict[str, list[Path]]:
    families: dict[str, list[Path]] = {}
    if not directory.exists():
        return families
    for path in sorted(directory.glob("*.parquet")):
        families.setdefault(canonical_dataset(path.name, default_name), []).append(path)
    return families


def verdict_for(target: dict[str, Any], pool_size: int) -> tuple[str, str]:
    """给出"现在能不能做"的判断，附加一句短理由。

    顺序即优先级：

    1. 池内零命中一票否决——数据再长，这个池子也用不上，谈时间跨度毫无意义；
    2. 日期不可解析 / 已过期，都是硬伤；
    3. 池内命中太少或观测天数太少，结论是**数据不够**，不是**方向不行**。
       这两件事必须分开说：前者补数据就能推进，后者才该放弃。
    """
    if target["pool_hits"] == 0:
        return "不可做", "标的池零命中，需先扩池"
    if target["adequacy"] == "不可用":
        return "不可做", "无可解析日期"
    if target["freshness"] == "过期":
        return "不可做", "数据已过期"
    hits = target["pool_hits"]
    if hits < MIN_POOL_HITS:
        return "需补数据", f"池内仅 {hits}/{pool_size} 只命中，事件样本只够个例"
    window_events = target.get("pool_events_in_window")
    if window_events is not None and window_events < MIN_POOL_EVENTS:
        return "需补数据", f"行情窗口内池内仅 {window_events} 条样本，撑不起检验"
    if target["adequacy"] == "只够可行性":
        return "需补数据", "观测天数不足，只能判断方向是否存在"
    return "可做", ""


def direction_feasibility(
    audits: dict[str, dict[str, Any]], pool_size: int = 0
) -> list[dict[str, Any]]:
    """把数据集映射到策略方向，并给出"现在能不能做"的判断。

    只写有依据的映射：每个方向都标出依赖哪几个数据集，缺一个就不是"能做"。
    """

    def best(*names: str) -> dict[str, Any] | None:
        present = [audits[n] for n in names if n in audits]
        if not present:
            return None
        return max(present, key=lambda a: a["distinct_dates"])

    plans = [
        (
            "内部人交易（增减持）",
            ("insider_trading",),
            "变动日可得 → 可对齐；需确认是否有独立公告日",
        ),
        ("事件驱动（业绩预告 PEAD）", ("earnings_forecast",), "需要足够预告样本 + 公告日"),
        ("资金流（龙虎榜）", ("dragon_tiger",), "需要连续交易日，快照窗口重叠会虚增"),
        ("资金流（融资余额）", ("margin_trade",), "需要连续交易日"),
        ("筹码结构（股东户数）", ("holder_structure",), "季度披露，稀疏"),
        ("大宗交易", ("block_trade",), "事件稀疏，需长历史"),
        ("解禁压力", ("share_unlock",), "事件稀疏，需长历史"),
    ]
    rows: list[dict[str, Any]] = []
    for direction, needed, note in plans:
        found = [audits[n] for n in needed if n in audits]
        if not found:
            rows.append(
                {
                    "direction": direction,
                    "datasets": "、".join(needed),
                    "distinct_dates": 0,
                    "adequacy": "缺数据",
                    "freshness": "无数据",
                    "pool_hits": 0,
                    "pool_events_in_window": None,
                    "verdict": "不可做",
                    "note": note,
                }
            )
            continue
        target = best(*needed)
        assert target is not None
        verdict, reason = verdict_for(target, pool_size)
        rows.append(
            {
                "direction": direction,
                "datasets": "、".join(needed),
                "distinct_dates": target["distinct_dates"],
                "adequacy": target["adequacy"],
                "freshness": target["freshness"],
                "pool_hits": target["pool_hits"],
                "pool_events_in_window": target.get("pool_events_in_window"),
                "verdict": verdict,
                "note": f"{note}；{reason}" if reason else note,
            }
        )
    return rows


def _null_pct(row: dict[str, Any]) -> str:
    if not row["rows"] or not row["date_null_rows"]:
        return "—"
    return f"{100 * row['date_null_rows'] / row['rows']:.0f}%"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 数据能力审计",
        "",
        f"- 审计基准日：{report['as_of']}",
        f"- 标的池：{report['pool_size']} 只",
        "",
        "## 数据集",
        "",
        "| 数据集 | 文件数 | 行数 | 重复因子 | 时间列 | 日期来源 | 起 | 止 | 不同日期 "
        "| 股票数 | 池内命中 | 缺日期行 | 距基准日 | 新鲜度 | 可用性 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["datasets"]:
        dates = "、".join(row["date_columns"]) or "—"
        if row["redundant_date_columns"]:
            dates += "（冗余：" + "、".join(row["redundant_date_columns"]) + "）"
        factor = "—" if row["duplicate_factor"] is None else f"{row['duplicate_factor']}×"
        lines.append(
            f"| {row['dataset']} | {row['files']} | {row['rows']} | {factor} "
            f"| {dates} | {row['date_source']} | {row['date_min'] or '—'} "
            f"| {row['date_max'] or '—'} | {row['distinct_dates']} | {row['codes']} "
            f"| {row['pool_hits']} | {_null_pct(row)} | "
            f"{'—' if row['days_stale'] is None else str(row['days_stale']) + ' 天'} "
            f"| {row['freshness']} | {row['adequacy']} |"
        )
    kline = report["kline"]
    lines += [
        "",
        "## 行情缓存",
        "",
        f"- 文件 {kline['files']} 个，覆盖 {kline['symbols']} 只标的，池内命中 {kline['pool_hits']} 只",
        f"- 行数中位数 {kline['median_rows']}，最短 {kline['min_rows']}",
        f"- 最新行情日期 {kline['latest'] or '—'}",
        "",
        "## 方向可行性",
        "",
        "| 方向 | 依赖数据集 | 不同日期 | 可用性 | 新鲜度 | 池内命中 | 池内样本 | 判断 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["directions"]:
        events = row.get("pool_events_in_window")
        lines.append(
            f"| {row['direction']} | {row['datasets']} | {row['distinct_dates']} "
            f"| {row['adequacy']} | {row['freshness']} | {row['pool_hits']} "
            f"| {'—' if events is None else events} "
            f"| **{row['verdict']}** | {row['note']} |"
        )
    lines += [
        "",
        "> 不同日期 ≠ 文件数：快照文件是重叠的滚动窗口，按文件数会系统性虚增覆盖。",
        f"> 重复因子 = 总行数 / 单文件最大行数。≈{DUPLICATE_WARN:.0f}× 以上说明文件是"
        "滚动全量快照而非增量，行数是同一份数据重复，不代表覆盖广度。",
        f"> 可用性阈值：≥{ONE_YEAR_DAYS} 个日期 = 够一年；≥{MIN_EVENT_DAYS} = 仅够事件研究；"
        "再少只能做可行性判断。",
        f"> 新鲜度阈值：≤{FRESH_DAYS} 天=新鲜；≤{STALE_DAYS}=偏旧；≤{COLD_DAYS}=陈旧；更久=过期。",
        "> 池内命中 = 该数据集覆盖的股票里，有多少只属于我们自己的标的池。"
        "为 0 说明数据再长也用不上这个池子，得先扩池——这是不可做的一票否决项。",
        f"> 判断口径：「可做」= 够一年且池内命中 ≥{MIN_POOL_HITS} 只、"
        f"行情窗口内池内样本 ≥{MIN_POOL_EVENTS} 条；"
        f"「需补数据」= 方向没被证伪，但天数、池内命中或样本量不够，补历史/扩池后可复审；"
        "「不可做」= 池内零命中、无日期或已过期。",
        "> 池内样本 = 池内**独立事件数**（按标的+日期+方向去重）中，落在各标的"
        "**自身行情覆盖区间**内的部分。一行不等于一个事件：一次公告会因多个变动人"
        "拆成多行，24 份快照又各存一份，去重后才是真实样本量；事件有三十年、"
        "行情只有三年时，可用样本就是三年内的那些。",
        "> 日期来源「文件名」= 列里的日期不可用（缺失或全为 0 被压成 1970-01-01），"
        "退回用快照文件名日期，仅代表观测日，不代表事件发生日。",
    ]
    if report.get("notes"):
        lines += ["", "## 备注", ""] + [f"- {n}" for n in report["notes"]]
    return "\n".join(lines)


def build_report(
    altdir: Path,
    holderdir: Path,
    klinedir: Path,
    pool_file: Path,
    as_of: date,
) -> dict[str, Any]:
    pool_codes = load_pool_codes(pool_file)
    families = collect_families(altdir)
    # 股东户数目录用日期当文件名，目录本身即数据集身份
    families.update(collect_families(holderdir, default_name="holder_structure"))
    # 事件样本必须限定在行情能算收益的区间内，否则会把三十年历史当成可用样本
    cover = kline_coverage(klinedir)

    audits: dict[str, dict[str, Any]] = {}
    for name, files in sorted(families.items()):
        audits[name] = audit_dataset(name, files, pool_codes, cover).as_dict(as_of)

    notes: list[str] = []
    for name, row in audits.items():
        if row["errors"]:
            notes.append(f"{name} 有 {len(row['errors'])} 个文件读取失败（已跳过）")
        if row["degenerate_date"]:
            notes.append(
                f"{name} 的时间列取值全被判退化（0 填充被压成 1970-01-01），"
                f"已回退到文件名日期：该列本身不可用于对齐"
            )
        elif row["date_column"] is None:
            notes.append(
                f"{name} 未识别到时间列"
                + (
                    "，已回退到文件名日期"
                    if row["date_source"] == "文件名"
                    else "，无法用于事件研究"
                )
            )
        if row["redundant_date_columns"]:
            notes.append(
                f"{name} 的 {'、'.join(row['redundant_date_columns'])} 与 "
                f"{row['date_columns'][0]} 逐行完全相同：不是独立信息来源，"
                "不能据此声称「有公告日 → 无前视」"
            )
        if row["duplicate_factor"] and row["duplicate_factor"] >= DUPLICATE_WARN:
            notes.append(
                f"{name} 总行数约为单文件的 {row['duplicate_factor']} 倍："
                f"{row['files']} 个文件是滚动全量快照，"
                f"有效信息量接近 1 份而非 {row['files']} 份"
            )
        if row["rows"] and row["date_null_rows"] / row["rows"] >= NULL_DATE_WARN:
            # 已回退到文件名日期时，这些行仍能按快照日对齐，不必重复告警
            if row["date_source"] == "列":
                notes.append(
                    f"{name} 有 {row['date_null_rows']} 行（{_null_pct(row)}）缺时间列取值，"
                    "这些行无法按日对齐"
                )
        if row["date_max"] and row["date_max"] > as_of.isoformat():
            notes.append(
                f"{name} 的 {row['date_column']} 最大值 {row['date_max']} 晚于审计基准日："
                "该列含**预告**事件（如解禁计划），做事件研究必须按基准日截断，"
                "否则就是直接使用未来信息"
            )
        if row["distinct_dates"] and row["files"] and row["distinct_dates"] < row["files"]:
            notes.append(
                f"{name} 的不同日期（{row['distinct_dates']}）少于文件数（{row['files']}）："
                "多个快照覆盖同一批日期，按文件数会虚增覆盖"
            )
        if row["codes"] and row["pool_hits"] == 0:
            notes.append(f"{name} 覆盖 {row['codes']} 只股票但与标的池零交集")

    return {
        "as_of": as_of.isoformat(),
        "pool_size": len(pool_codes),
        "datasets": list(audits.values()),
        "kline": audit_kline_cache(klinedir, pool_codes),
        "directions": direction_feasibility(audits, len(pool_codes)),
        "notes": notes,
    }


def _parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="数据能力审计")
    parser.add_argument("--altalpha-dir", default=str(DEFAULT_ALTALPHA))
    parser.add_argument("--holder-dir", default=str(DEFAULT_HOLDER))
    parser.add_argument("--kline-dir", default=str(DEFAULT_KLINE))
    parser.add_argument("--pool-file", default=str(DEFAULT_POOL))
    parser.add_argument("--as-of", default=date.today().isoformat(), help="审计基准日 YYYY-MM-DD")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    altdir = Path(args.altalpha_dir)
    if not altdir.exists():
        print(f"另类数据目录不存在：{altdir}", file=sys.stderr)
        return 1
    try:
        as_of = _parse_date(args.as_of)
    except ValueError:
        print(f"--as-of 格式应为 YYYY-MM-DD：{args.as_of}", file=sys.stderr)
        return 1

    report = build_report(
        altdir, Path(args.holder_dir), Path(args.kline_dir), Path(args.pool_file), as_of
    )
    print(
        json.dumps(report, ensure_ascii=False, indent=1) if args.json else render_markdown(report)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
