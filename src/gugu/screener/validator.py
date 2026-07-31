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
from typing import Any

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

# P2-3: 大盘开盘门控阈值（沪深300 开盘跌幅超过该值 → 放弃买入）
# 依据：7/30 大盘开盘 -0.74%，选中股票收盘平均 -9.58%（跌得比大盘狠 10 倍）
_MARKET_GATE_THRESHOLD = -0.5

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
    stop_loss_price: float = 0.0  # T 日建议止损价（用于次日止损命中率计算）


@dataclass
class ValidationResult:
    """T+1 日验证结果。"""

    symbol: str
    name: str
    close_price: float  # T 日收盘价
    open_price: float  # T+1 开盘价
    high_price: float  # T+1 最高价
    low_price: float  # T+1 最低价
    current_price: float  # T+1 当前价（收盘验证时为收盘价）
    open_premium: float  # 开盘溢价率
    intraday_high_premium: float  # 日内最高溢价率
    current_premium: float  # 当前价收益（%）
    close_premium: float  # 收盘价收益（%），盘中验证时与 current_premium 相同
    intraday_max_drawdown: float  # 盘中最低回撤（从开盘价算，%）
    stop_loss_hit: bool  # 是否触发止损（最低价 <= 选股止损价）
    screener_date: str
    validation_date: str
    passed: bool
    benchmark_open_change: float  # 沪深300同日开盘涨幅（用于计算超额收益）
    stop_loss_price: float = 0.0  # T 日选股建议止损价
    # P2-3: 交易模拟 + 大盘门控（开盘低吸→止盈/止损/收盘平仓闭环）
    sim_exit_reason: str = ""  # "take_profit" | "stop_loss" | "close" | ""（数据异常）
    sim_return_pct: float = 0.0  # 模拟交易收益率（%）
    market_gate_blocked: bool = False  # 大盘开盘跌超阈值 → 放弃买入


# ── 记录写入 ──────────────────────────────────────────


def save_screener_records(results: list[ScreenerRecord]) -> None:
    """将选股结果保存到历史 JSONL（T 日调用）。"""
    os.makedirs(os.path.dirname(_SCREENER_HISTORY_FILE), exist_ok=True)  # noqa: PTH103 兼容既有风格
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
                    "stop_loss_price": r.stop_loss_price,
                    "created_at": datetime.now().isoformat(),
                },
                ensure_ascii=False,
            )
            with open(_SCREENER_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            count += 1
        except Exception as e:
            logger.warning("保存选股记录失败 {}: {}", r.symbol, e)
    logger.info("尾盘选股记录已保存: {} 条", count)
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
        logger.info(f"JSONL 轮转: {os.path.basename(filepath)} {len(lines)}→{TRIM_TO}")  # noqa: PTH119 兼容既有风格
    except Exception as e:
        logger.debug("JSONL 轮转失败: {}", e)


# ── 验证执行 ──────────────────────────────────────────


async def validate_yesterday_screener(is_eod: bool = False) -> list[ValidationResult]:
    """验证昨日尾盘选股结果（T+1 日调用）。

    读取昨日选股记录，获取今日开盘价/最高价/最低价/当前价，计算溢价。

    Args:
        is_eod: 是否为收盘验证。收盘验证时 current_price 视为收盘价，
                并用覆盖方式保存当日记录，使 auto_tune 使用真实收盘收益。
    """
    # 修复：用上一交易日而非自然日昨天，周一/节后首日不漏验证
    yesterday_str = last_trading_day().isoformat()
    records = _load_records_by_date(yesterday_str)
    if not records:
        logger.info("尾盘验证: {} 无选股记录", yesterday_str)
        return []

    logger.info(f"尾盘验证: 昨日 {len(records)} 只选股记录，开始获取今日行情")
    results = await _fetch_today_prices(records)
    hs300_open_chg = await _fetch_hs300_open_change()

    validated: list[ValidationResult] = []
    for rec, today_data in zip(records, results, strict=True):
        if today_data is None:
            continue
        open_price, high_price, low_price, current_price = today_data

        def _pct(new: float, old: float) -> float:
            return (new - old) / old if old > 0 else 0.0

        open_premium = _pct(open_price, rec.close_price)
        high_premium = _pct(high_price, rec.close_price)
        current_premium = _pct(current_price, rec.close_price)
        # 收盘收益：收盘验证时 current_price 即收盘价；盘中验证时先用当前价占位
        close_premium = current_premium if is_eod else current_premium
        # 盘中最低回撤：从开盘价到最低价的跌幅（ negative 值）
        intraday_max_drawdown = _pct(low_price, open_price) * 100
        # 止损命中：最低价 <= 选股时建议止损价（且止损价有效）
        stop_loss_hit = rec.stop_loss_price > 0 and low_price <= rec.stop_loss_price

        # P2-3: 交易模拟（开盘低吸 → 止盈/止损/收盘平仓）+ 大盘门控
        from gugu.screener.trade_simulator import check_market_gate, simulate_trade

        sim = simulate_trade(
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=current_price if is_eod else high_price,
        )
        gate = check_market_gate(
            benchmark_open_change=hs300_open_chg,
            skip_threshold=_MARKET_GATE_THRESHOLD,
        )

        vr = ValidationResult(
            symbol=rec.symbol,
            name=rec.name,
            close_price=rec.close_price,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            current_price=current_price,
            open_premium=round(open_premium * 100, 2),  # 转为百分比
            intraday_high_premium=round(high_premium * 100, 2),
            current_premium=round(current_premium * 100, 2),
            close_premium=round(close_premium * 100, 2),
            intraday_max_drawdown=round(intraday_max_drawdown, 2),
            stop_loss_hit=stop_loss_hit,
            screener_date=yesterday_str,
            validation_date=date.today().isoformat(),
            passed=rec.passed,
            benchmark_open_change=hs300_open_chg,
            stop_loss_price=rec.stop_loss_price,
            sim_exit_reason=sim.exit_reason if sim else "",
            sim_return_pct=sim.return_pct if sim else 0.0,
            market_gate_blocked=gate.blocked,
        )
        validated.append(vr)

    # 保存验证结果
    if is_eod:
        _overwrite_validation_for_date(validated, date.today().isoformat())
    else:
        _save_validation_results(validated)

    # 输出摘要
    if validated:
        win_open = sum(1 for v in validated if v.open_premium > 0)
        win_high = sum(1 for v in validated if v.intraday_high_premium > 0)
        avg_open = sum(v.open_premium for v in validated) / len(validated)
        avg_high = sum(v.intraday_high_premium for v in validated) / len(validated)
        logger.info(
            f"尾盘验证完成: {len(validated)} 只 | "
            f"高开率 {win_open}/{len(validated)}={win_open / len(validated) * 100:.0f}% | "
            f"日内冲高率 {win_high}/{len(validated)}={win_high / len(validated) * 100:.0f}% | "
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
                            stop_loss_price=float(d.get("stop_loss_price", 0) or 0),
                        )
                    )
    except Exception as e:
        logger.warning("读取选股记录失败: {}", e)
    return records


async def _fetch_today_prices(
    records: list[ScreenerRecord],
) -> list[tuple[float, float, float, float] | None]:
    """批量获取 T+1 日开盘价/最高价/最低价/当前价和沪深300开盘涨幅。

    P0 修复：改用东财 push2 批量实时行情接口（ulist.np/get），
    而非日K线接口。原因：
    1. 日K线接口在盘中没有当日数据（要收盘后才生成）
    2. 09:35 验证时需要实时开盘价/最高价，只有实时行情接口才有
    3. 批量接口一次获取所有选股记录，避免逐只请求

    P2 扩展：fields 增加 f2=最新价/f16=最低价，支持计算
    当前收益、盘中最低回撤、止损命中率。
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
    # f2=最新价, f12=代码, f14=名称, f15=最高, f16=最低, f17=今开, f18=昨收
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fields": "f2,f12,f14,f15,f16,f17,f18",
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
        return [(None, None, None, None)] * len(records)

    # 解析返回数据
    diff_list = data.get("data", {}).get("diff", []) if data.get("data") else []
    if not diff_list:
        logger.warning("尾盘验证: 东财批量接口返回空数据")
        return [(None, None, None, None)] * len(records)

    # 建立代码 → (今开, 最高, 最低, 当前价) 映射
    price_map: dict[str, tuple[float, float, float, float]] = {}
    for item in diff_list:
        code = str(item.get("f12", ""))
        # 东财返回的价格是原始值÷100（如 2089 → 20.89 元）
        open_raw = item.get("f17")
        high_raw = item.get("f15")
        low_raw = item.get("f16")
        current_raw = item.get("f2")
        if open_raw is not None and high_raw is not None:
            price_map[code] = (
                float(open_raw) / 100,
                float(high_raw) / 100,
                float(low_raw) / 100 if low_raw is not None else 0.0,
                float(current_raw) / 100 if current_raw is not None else 0.0,
            )

    # 按 records 顺序输出结果
    final: list[tuple[float, float, float, float] | None] = []
    for rec in records:
        clean = rec.symbol[-6:] if len(rec.symbol) > 6 else rec.symbol
        prices = price_map.get(clean)
        if prices:
            final.append((prices[0], prices[1], prices[2], prices[3]))
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
    """保存验证结果到 validation JSONL（追加模式）。"""
    _write_validation_lines(results, mode="a")


def _overwrite_validation_for_date(results: list[ValidationResult], date_str: str) -> None:
    """覆盖保存指定日期的验证结果（用于收盘验证覆盖盘中验证）。"""
    _write_validation_lines(results, mode="w", filter_date=date_str)


def _validation_result_to_dict(r: ValidationResult) -> dict[str, Any]:
    """将 ValidationResult 序列化为 dict。"""
    return {
        "symbol": r.symbol,
        "name": r.name,
        "close_price": r.close_price,
        "open_price": r.open_price,
        "high_price": r.high_price,
        "low_price": r.low_price,
        "current_price": r.current_price,
        "open_premium": r.open_premium,
        "intraday_high_premium": r.intraday_high_premium,
        "current_premium": r.current_premium,
        "close_premium": r.close_premium,
        "intraday_max_drawdown": r.intraday_max_drawdown,
        "stop_loss_hit": r.stop_loss_hit,
        "screener_date": r.screener_date,
        "validation_date": r.validation_date,
        "benchmark_open_change": r.benchmark_open_change,
        "stop_loss_price": r.stop_loss_price,
        "sim_exit_reason": r.sim_exit_reason,
        "sim_return_pct": r.sim_return_pct,
        "market_gate_blocked": r.market_gate_blocked,
        "created_at": datetime.now().isoformat(),
    }


def _write_validation_lines(
    results: list[ValidationResult],
    mode: str = "a",
    filter_date: str | None = None,
) -> None:
    """底层写入函数：追加或覆盖指定日期的验证记录（幂等去重）。

    P2-3 修复（对抗性审查发现的生产 bug）：
    7/30 收盘验证被调度两次 → 同日同股重复 2 条，污染 auto_tune 统计。
    现在按 (validation_date, symbol) 幂等去重：同键只保留最新一条。

    实现：无论 append 还是 overwrite，都重写整个文件——
    - 保留与本次写入无键冲突的旧记录
    - 本次 results 覆盖同键旧记录
    """
    os.makedirs(os.path.dirname(_SCREENER_VALIDATION_FILE), exist_ok=True)  # noqa: PTH103 兼容既有风格

    # 1. 读取现有记录，建立 (validation_date, symbol) → 行 索引
    kept_lines: list[str] = []
    if os.path.exists(_SCREENER_VALIDATION_FILE):
        try:
            with open(_SCREENER_VALIDATION_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # 覆盖模式：剔除 filter_date 当日的全部旧记录（整日重写）
                    if filter_date and d.get("validation_date") == filter_date:
                        continue
                    kept_lines.append(line)
        except Exception as e:
            logger.warning("读取旧验证记录失败: {}", e)

    # 2. 本次写入的新记录（去重：同键只保留最后一条）
    new_keys: set[tuple[str, str]] = set()
    new_lines: list[str] = []
    for r in results:
        d = _validation_result_to_dict(r)
        key = (d.get("validation_date", ""), d.get("symbol", ""))
        if key in new_keys:
            continue  # 本次批量内去重
        new_keys.add(key)
        new_lines.append(json.dumps(d, ensure_ascii=False))

    # 3. 重写文件：保留与本次写入无键冲突的旧记录 + 新记录
    count = 0
    try:
        with open(_SCREENER_VALIDATION_FILE, "w", encoding="utf-8") as f:
            for line in kept_lines:
                try:
                    d = json.loads(line)
                    key = (d.get("validation_date", ""), d.get("symbol", ""))
                except json.JSONDecodeError:
                    key = None
                # 幂等去重：旧记录与本次新写入同键 → 丢弃（以新为准）
                if key is not None and key in new_keys:
                    continue
                f.write(line + "\n")
            for line in new_lines:
                f.write(line + "\n")
                count += 1
    except Exception as e:
        logger.warning("保存验证记录失败: {}\n{}", e, results)
    logger.info("尾盘验证结果已保存: {} 条（旧记录保留 {} 条）", count, len(kept_lines))
    _rotate_file(_SCREENER_VALIDATION_FILE)
