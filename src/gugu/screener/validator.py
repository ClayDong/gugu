"""尾盘选股次日验证：T+1 日验证尾盘选股信号的有效性。

工作原理：
  T日 14:30 → 尾盘选股 → 记录选股结果到 screener_history.jsonl
  T+1日 09:35 → 验证器启动 → 读取昨日记录 → 获取今日开盘价
  → 计算：开盘溢价、高开率、日均最高溢价、胜率

核心公式：
  open_premium = (T+1开盘价 - T收盘价) / T收盘价
  high_premium = (T+1盘中最高价 - T收盘价) / T收盘价

数据持久化：
  data/screener_history.jsonl: 选股记录（T日写入）
  data/screener_validation.jsonl: 验证结果（T+1日写入）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from gugu.utils.calendar import last_trading_day
from gugu.utils.log import get_logger

logger = get_logger()

# ── 路径 ──────────────────────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
_SCREENER_HISTORY_FILE = os.path.join(_DATA_DIR, "screener_history.jsonl")
_SCREENER_VALIDATION_FILE = os.path.join(_DATA_DIR, "screener_validation.jsonl")

# ── 参数 ──────────────────────────────────────────────
MAX_RECORDS = 5000  # JSONL 轮转上限
TRIM_TO = 2500  # 超限时保留最新数

# ── 东财日K线 URL ────────────────────────────────────
_EASTMONEY_KLINE_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={market}.{symbol}"
    "&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&klt=101&fqt=1"
    "&end={end_date}&lmt={limit}"
)

_STOCK_MARKET: dict[str, int] = {
    "6": 1,  # 沪市: sh=1
    "0": 0,  # 深市: sz=0
    "3": 0,  # 创业板: sz=0
    "4": 0,  # 北交: sz=0
    "8": 0,  # 北交: sz=0
}


def _get_market(symbol: str) -> int:
    """根据股票代码前缀返回东财市场编号。"""
    prefix = symbol[0]
    return _STOCK_MARKET.get(prefix, 1)


@dataclass
class ScreenerRecord:
    """T 日尾盘选股记录。"""

    symbol: str
    name: str
    close_price: float  # T 日收盘价
    change_pct: float  # T 日涨幅
    volume_ratio: float  # T 日量比
    screener_date: str  # "YYYY-MM-DD"
    passed: bool
    failed_conditions: list[str]


@dataclass
class ValidationResult:
    """T+1 日验证结果。"""

    symbol: str
    name: str
    close_price: float  # T 日收盘价
    open_price: float  # T+1 开盘价
    high_price: float  # T+1 最高价
    open_premium: float  # 开盘溢价率
    intraday_high_premium: float  # 日内最高溢价率
    screener_date: str
    validation_date: str
    passed: bool
    benchmark_open_change: float  # 沪深300同日开盘涨幅（用于计算超额收益）


# ── 记录写入 ──────────────────────────────────────────


def save_screener_records(results: list[ScreenerRecord]) -> None:
    """将选股结果保存到历史 JSONL（T 日调用）。"""
    os.makedirs(os.path.dirname(_SCREENER_HISTORY_FILE), exist_ok=True)
    count = 0
    for r in results:
        try:
            line = json.dumps(
                {
                    "symbol": r.symbol,
                    "name": r.name,
                    "close_price": r.close_price,
                    "change_pct": r.change_pct,
                    "volume_ratio": r.volume_ratio,
                    "screener_date": r.screener_date,
                    "passed": r.passed,
                    "failed_conditions": r.failed_conditions,
                    "created_at": datetime.now().isoformat(),
                },
                ensure_ascii=False,
            )
            with open(_SCREENER_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            count += 1
        except Exception as e:
            logger.warning(f"保存选股记录失败 {r.symbol}: {e}")
    logger.info(f"尾盘选股记录已保存: {count} 条")
    _rotate_file(_SCREENER_HISTORY_FILE)


def _rotate_file(filepath: str) -> None:
    """轮转 JSONL 文件，防止无限增长。"""
    try:
        if not os.path.exists(filepath):
            return
        with open(filepath, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_RECORDS:
            return
        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(lines[-TRIM_TO:])
        logger.info(f"JSONL 轮转: {os.path.basename(filepath)} {len(lines)}→{TRIM_TO}")
    except Exception as e:
        logger.debug(f"JSONL 轮转失败: {e}")


# ── 验证执行 ──────────────────────────────────────────


async def validate_yesterday_screener() -> list[ValidationResult]:
    """验证昨日尾盘选股结果（T+1 日调用）。

    读取昨日选股记录，获取今日开盘价/最高价，计算溢价。
    """
    # 修复：用上一交易日而非自然日昨天，周一/节后首日不漏验证
    yesterday_str = last_trading_day().isoformat()
    records = _load_records_by_date(yesterday_str)
    if not records:
        logger.info(f"尾盘验证: {yesterday_str} 无选股记录")
        return []

    logger.info(f"尾盘验证: 昨日 {len(records)} 只选股记录，开始获取今日行情")
    results = await _fetch_today_prices(records)

    validated: list[ValidationResult] = []
    for rec, today_data in zip(records, results, strict=True):
        if today_data is None:
            continue
        open_price, high_price, benchmark_open = today_data
        open_premium = (open_price - rec.close_price) / rec.close_price if rec.close_price > 0 else 0.0
        high_premium = (high_price - rec.close_price) / rec.close_price if rec.close_price > 0 else 0.0
        vr = ValidationResult(
            symbol=rec.symbol,
            name=rec.name,
            close_price=rec.close_price,
            open_price=open_price,
            high_price=high_price,
            open_premium=round(open_premium * 100, 2),  # 转为百分比
            intraday_high_premium=round(high_premium * 100, 2),
            screener_date=yesterday_str,
            validation_date=date.today().isoformat(),
            passed=rec.passed,
            benchmark_open_change=benchmark_open,
        )
        validated.append(vr)

    # 保存验证结果
    _save_validation_results(validated)

    # 输出摘要
    if validated:
        win_open = sum(1 for v in validated if v.open_premium > 0)
        win_high = sum(1 for v in validated if v.intraday_high_premium > 0)
        avg_open = sum(v.open_premium for v in validated) / len(validated)
        avg_high = sum(v.intraday_high_premium for v in validated) / len(validated)
        logger.info(
            f"尾盘验证完成: {len(validated)} 只 | "
            f"高开率 {win_open}/{len(validated)}={win_open/len(validated)*100:.0f}% | "
            f"日内冲高率 {win_high}/{len(validated)}={win_high/len(validated)*100:.0f}% | "
            f"平均开盘溢价 {avg_open:+.2f}% | "
            f"平均日内最高溢价 {avg_high:+.2f}%"
        )

    return validated


def _load_records_by_date(target_date: str) -> list[ScreenerRecord]:
    """从 JSONL 加载指定日期的选股记录。"""
    if not os.path.exists(_SCREENER_HISTORY_FILE):
        return []
    records: list[ScreenerRecord] = []
    try:
        with open(_SCREENER_HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("screener_date") == target_date and d.get("passed", False):
                    records.append(
                        ScreenerRecord(
                            symbol=d["symbol"],
                            name=d.get("name", ""),
                            close_price=float(d.get("close_price", 0)),
                            change_pct=float(d.get("change_pct", 0)),
                            volume_ratio=float(d.get("volume_ratio", 0)),
                            screener_date=d["screener_date"],
                            passed=d.get("passed", False),
                            failed_conditions=d.get("failed_conditions", []),
                        )
                    )
    except Exception as e:
        logger.warning(f"读取选股记录失败: {e}")
    return records


async def _fetch_today_prices(
    records: list[ScreenerRecord],
) -> list[tuple[float, float, float] | None]:
    """批量获取 T+1 日开盘价/最高价和沪深300开盘涨幅。

    P0 修复：改用东财 push2 批量实时行情接口（ulist.np/get），
    而非日K线接口。原因：
    1. 日K线接口在盘中没有当日数据（要收盘后才生成）
    2. 09:35 验证时需要实时开盘价/最高价，只有实时行情接口才有
    3. 批量接口一次获取所有选股记录，避免逐只请求
    """
    if not records:
        return []

    # 构造 secids 列表（如 0.300970,1.603190）
    secids = []
    symbol_to_idx: dict[str, int] = {}
    for i, rec in enumerate(records):
        clean = rec.symbol[-6:] if len(rec.symbol) > 6 else rec.symbol
        market = _get_market(clean)
        secids.append(f"{market}.{clean}")
        symbol_to_idx[clean] = i

    # 东财 push2 批量实时行情
    # f2=最新价, f12=代码, f14=名称, f15=最高, f17=今开, f18=昨收
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fields": "f2,f12,f14,f15,f17,f18",
        "secids": ",".join(secids),
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"尾盘验证: 东财批量实时行情获取失败: {e}")
        return [(None, None, None)] * len(records)

    # 解析返回数据
    diff_list = data.get("data", {}).get("diff", []) if data.get("data") else []
    if not diff_list:
        logger.warning("尾盘验证: 东财批量接口返回空数据")
        return [(None, None, None)] * len(records)

    # 建立代码 → (今开, 最高, 昨收) 映射
    price_map: dict[str, tuple[float, float, float]] = {}
    for item in diff_list:
        code = str(item.get("f12", ""))
        # 东财返回的价格是原始值÷100（如 2089 → 20.89 元）
        open_raw = item.get("f17")
        high_raw = item.get("f15")
        prev_close_raw = item.get("f18")
        if open_raw is not None and high_raw is not None:
            price_map[code] = (
                float(open_raw) / 100,
                float(high_raw) / 100,
                float(prev_close_raw) / 100 if prev_close_raw else 0.0,
            )

    # 获取沪深300今日开盘涨幅
    hs300_open_chg = await _fetch_hs300_open_change()

    # 按 records 顺序输出结果
    final: list[tuple[float, float, float] | None] = []
    for rec in records:
        clean = rec.symbol[-6:] if len(rec.symbol) > 6 else rec.symbol
        prices = price_map.get(clean)
        if prices:
            final.append((prices[0], prices[1], hs300_open_chg))
        else:
            final.append(None)

    return final


async def _fetch_hs300_open_change() -> float:
    """获取沪深300今日开盘涨幅（作为基准）。

    P0 修复：改用东财 push2 实时行情接口，而非日K线接口。
    原因：日K线在盘中没有当日数据。
    """
    try:
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        params = {
            "fields": "f2,f12,f15,f17,f18",
            "secids": "1.000300",  # 沪深300
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        diff_list = data.get("data", {}).get("diff", []) if data.get("data") else []
        if diff_list:
            item = diff_list[0]
            today_open = float(item.get("f17", 0)) / 100
            prev_close = float(item.get("f18", 0)) / 100
            if prev_close > 0:
                return round((today_open - prev_close) / prev_close * 100, 2)
    except Exception as e:
        logger.debug(f"获取沪深300开盘涨幅失败: {e}")
    return 0.0


def _save_validation_results(results: list[ValidationResult]) -> None:
    """保存验证结果到 validation JSONL。"""
    os.makedirs(os.path.dirname(_SCREENER_VALIDATION_FILE), exist_ok=True)
    count = 0
    for r in results:
        try:
            line = json.dumps(
                {
                    "symbol": r.symbol,
                    "name": r.name,
                    "close_price": r.close_price,
                    "open_price": r.open_price,
                    "high_price": r.high_price,
                    "open_premium": r.open_premium,
                    "intraday_high_premium": r.intraday_high_premium,
                    "screener_date": r.screener_date,
                    "validation_date": r.validation_date,
                    "benchmark_open_change": r.benchmark_open_change,
                    "created_at": datetime.now().isoformat(),
                },
                ensure_ascii=False,
            )
            with open(_SCREENER_VALIDATION_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            count += 1
        except Exception as e:
            logger.warning(f"保存验证记录失败 {r.symbol}: {e}")
    logger.info(f"尾盘验证结果已保存: {count} 条")
    _rotate_file(_SCREENER_VALIDATION_FILE)
