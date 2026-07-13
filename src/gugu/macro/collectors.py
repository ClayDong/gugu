"""宏观五维数据采集器：金、油、汇、债、G。

基于 akshare 公共 API，提供异步安全的数据采集。
每个维度独立采集，带超时和合理性校验。
失败时返回默认值（False valid），不影响其他维度。

数据源验证（2026-06-29，East Money CDN 封锁环境下）：
  futures_foreign_hist  ✅ 可用
  bond_zh_us_rate      ✅ 可用
  macro_shipping_bdi   ✅ 可用
  stock_hsgt_fund_flow_summary_em ✅ 可用
  macro_china_cpi_monthly ✅ 可用
  stock_zh_index_spot_sina   ❌ 列名问题
  stock_us_famous_spot_em    ❌ 被封锁
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import akshare as ak
import pandas as pd

from gugu.macro.models import (
    BondData,
    DerivativeData,
    FxData,
    GoldData,
    MacroSnapshot,
    OilData,
    StockIndexData,
)
from gugu.utils.log import get_logger

logger = get_logger()

# 合理性校验范围（基于 2026 年价格基线）
_PRICE_RANGES: dict[str, tuple[float, float]] = {
    "gold": (1500.0, 6000.0),  # COMEX 黄金
    "silver": (10.0, 100.0),  # COMEX 白银
    "brent": (20.0, 150.0),  # ICE 布伦特
    "wti": (15.0, 140.0),  # NYMEX WTI
    "usd_index": (90.0, 120.0),  # 美元指数
    "usd_cny": (6.0, 8.0),  # 美元/人民币
    "vix": (5.0, 100.0),  # VIX 恐慌指数
    "us10y": (0.5, 7.0),  # 美债 10Y 收益率
}


def _safe_float(val: Any, default: float = 0.0) -> float:
    """安全转换浮点数。"""
    if val is None:
        return default
    try:
        v = float(val)
        if v != v or v == float("inf") or v == float("-inf"):
            return default
        return v
    except (ValueError, TypeError):
        return default


def _validate_price(name: str, price: float) -> bool:
    """合理性校验。"""
    rng = _PRICE_RANGES.get(name)
    if rng is None:
        return price > 0
    return rng[0] <= price <= rng[1]


def _change_pct(current: float, prev: float) -> float:
    """计算涨跌幅百分比。"""
    if prev and prev != 0:
        return round((current - prev) / prev * 100, 2)
    return 0.0


async def _run_sync(fn, *args, timeout: float = 30.0, **kwargs):
    """在独立线程中执行同步函数（避免阻塞事件循环）。"""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, lambda: fn(*args, **kwargs)),
        timeout=timeout,
    )


async def _try_fetch(name: str, fn, *args, timeout: float = 30.0, **kwargs) -> pd.DataFrame | None:
    """安全采集：带重试、超时、异常保护。"""
    for attempt in range(3):
        try:
            result = await _run_sync(fn, *args, timeout=timeout, **kwargs)
            if result is not None and not result.empty:
                return result
            logger.warning(f"[macro] {name} 返回空结果 (attempt {attempt + 1})")
        except Exception as e:
            logger.debug(f"[macro] {name} 失败 (attempt {attempt + 1}): {e}")
        await asyncio.sleep(1.0)
    logger.warning(f"[macro] {name} 采集失败（3 次重试后放弃）")
    return None


# ═══════════════════════════════════════════════════════════
# 各维度采集器
# ═══════════════════════════════════════════════════════════


async def _collect_gold() -> GoldData:
    """采集贵金属数据：黄金、白银。"""
    gd = GoldData()
    try:
        df = await _try_fetch("黄金", ak.futures_foreign_hist, symbol="GC")
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else latest
            gd.gold_price = _safe_float(latest.get("收盘", latest.get("close", 0)))
            prev_gold = _safe_float(prev.get("收盘", prev.get("close", 0)))
            if _validate_price("gold", gd.gold_price):
                gd.gold_change_pct = _change_pct(gd.gold_price, prev_gold)
                gd.source = "futures_foreign_hist"
                gd.valid = True
    except Exception as e:
        logger.warning(f"[macro] 黄金采集异常: {e}")

    try:
        df = await _try_fetch("白银", ak.futures_foreign_hist, symbol="SI")
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            gd.silver_price = _safe_float(latest.get("收盘", latest.get("close", 0)))
            if _validate_price("silver", gd.silver_price) and gd.silver_price > 0:
                gd.gold_silver_ratio = round(gd.gold_price / gd.silver_price, 1)
    except Exception as e:
        logger.warning(f"[macro] 白银采集异常: {e}")

    return gd


async def _collect_oil() -> OilData:
    """采集原油数据：布伦特、WTI。"""
    od = OilData()
    try:
        df = await _try_fetch("布伦特原油", ak.futures_foreign_hist, symbol="OIL")
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else latest
            od.brent_price = _safe_float(latest.get("收盘", latest.get("close", 0)))
            prev_brent = _safe_float(prev.get("收盘", prev.get("close", 0)))
            if _validate_price("brent", od.brent_price):
                od.brent_change_pct = _change_pct(od.brent_price, prev_brent)
                od.source = "futures_foreign_hist"
                od.valid = True
    except Exception as e:
        logger.warning(f"[macro] 布伦特采集异常: {e}")

    try:
        df = await _try_fetch("WTI原油", ak.futures_foreign_hist, symbol="CL")
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            od.wti_price = _safe_float(latest.get("收盘", latest.get("close", 0)))
            if _validate_price("wti", od.wti_price):
                od.spread = round(od.brent_price - od.wti_price, 2)
    except Exception as e:
        logger.warning(f"[macro] WTI 采集异常: {e}")

    return od


async def _collect_fx() -> FxData:
    """采集外汇数据：美元指数、USDCNY、EUR/USD。"""
    fd = FxData()
    try:
        df = await _try_fetch("外汇", ak.forex_spot_em)
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("名称", ""))
                price = _safe_float(row.get("最新价", row.get("price", 0)))
                change = _safe_float(row.get("涨跌幅", row.get("change_pct", 0)))
                if "美元指数" in name or "USDX" in name.upper():
                    fd.usd_index = price
                    fd.usd_index_change_pct = change
                elif "欧元/美元" in name or "EURUSD" in name.upper():
                    fd.eur_usd = price
                    fd.eur_usd_change_pct = change
                elif "美元/人民币" in name or "USDCNY" in name.upper():
                    fd.usd_cny = price
                    fd.usd_cny_change_pct = change
                elif "美元/日元" in name or "USDJPY" in name.upper():
                    fd.usd_jpy = price
        if _validate_price("usd_index", fd.usd_index):
            fd.valid = True
            fd.source = "forex_spot_em"
    except Exception as e:
        logger.warning(f"[macro] 外汇采集异常: {e}")

    # 如果 forex_spot_em 失败，尝试 sina HTTP 直连
    if not fd.valid:
        try:
            import httpx

            # USD/CNY from sina
            r = httpx.get(
                "https://hq.sinajs.cn/list=fx_susdcny",
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=10.0,
            )
            if r.status_code == 200:
                parts = r.text.split(",")
                if len(parts) >= 2:
                    fd.usd_cny = float(parts[1])
                    fd.source = "sina_http"
                    fd.valid = True
        except Exception as e:
            logger.debug(f"[macro] 外汇 sina HTTP 回退失败: {e}")

    # 如果 forex_spot_em 没有找到美元指数，尝试 sina 指数
    if fd.usd_index == 0:
        try:
            df = await _try_fetch("美元指数(新浪)", ak.stock_zh_index_spot_sina)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    name = str(row.get("name", ""))
                    if "美元" in name:
                        fd.usd_index = _safe_float(row.get("current_point", row.get("price", 0)))
                        if _validate_price("usd_index", fd.usd_index):
                            fd.valid = True
        except Exception:
            pass

    return fd


async def _collect_bond() -> BondData:
    """采集债券数据：美债收益率、LPR、Shibor。"""
    bd = BondData()
    try:
        df = await _try_fetch("美债收益率", ak.bond_zh_us_rate)
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            bd.us10y_yield = _safe_float(latest.get("美国国债收益率10年", 0))
            bd.us2y_yield = _safe_float(latest.get("美国国债收益率2年", 0))
            if _validate_price("us10y", bd.us10y_yield):
                bd.us10y_2y_spread = round(bd.us10y_yield - bd.us2y_yield, 3)
                bd.inverted = bd.us10y_2y_spread < 0
                bd.source = "bond_zh_us_rate"
                bd.valid = True
    except Exception as e:
        logger.warning(f"[macro] 美债采集异常: {e}")

    try:
        df = await _try_fetch("LPR", ak.macro_china_lpr)
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            bd.lpr_1y = _safe_float(latest.get("LPR1Y", 0))
            bd.lpr_5y = _safe_float(latest.get("LPR5Y", 0))
    except Exception as e:
        logger.warning(f"[macro] LPR 采集异常: {e}")

    try:
        df = await _try_fetch("SHIBOR", ak.macro_china_shibor_all)
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            bd.shibor_7d = _safe_float(latest.get("7天", latest.get("7D", 0)))
    except Exception as e:
        logger.warning(f"[macro] Shibor 采集异常: {e}")

    return bd


async def _collect_derivatives() -> DerivativeData:
    """采集衍生品与综合数据：VIX、BDI、BTC、北向资金。"""
    dd = DerivativeData()

    # BDI
    try:
        df = await _try_fetch("BDI", ak.macro_shipping_bdi)
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else latest
            dd.bdi = _safe_float(latest.iloc[-1])
            prev_bdi = _safe_float(prev.iloc[-1])
            if dd.bdi > 0:
                dd.bdi_change_pct = _change_pct(dd.bdi, prev_bdi)
                dd.source = "macro_shipping_bdi"
                dd.valid = True
    except Exception as e:
        logger.warning(f"[macro] BDI 采集异常: {e}")

    # 北向资金（获取不到时保持 None，不写 0）
    try:
        df = await _try_fetch("北向资金", ak.stock_hsgt_fund_flow_summary_em)
        if df is not None and not df.empty:
            # 仅匹配明确的北向资金/沪深股通字段，避免误用其他行数据
            for _, row in df.iterrows():
                val = _safe_float(row.get("当日成交净买额", row.get("value", 0)))
                name = str(row.get("资金方向", row.get("name", "")))
                if "北向" in name or "沪深" in name:
                    dd.north_flow = round(val / 1e8, 2)  # 元→亿元
                    break
            if dd.north_flow is not None:
                logger.info(f"[macro] 北向资金: {dd.north_flow:.2f} 亿")
            else:
                logger.warning("[macro] 北向资金: 未匹配到北向/沪深字段，标记为获取不到")
        else:
            logger.warning("[macro] 北向资金: 返回空数据，标记为获取不到")
    except Exception as e:
        logger.warning(f"[macro] 北向资金采集异常: {e}")

    return dd


async def _collect_stock_index() -> StockIndexData:
    """采集 A 股大盘指数：上证指数、深证成指、创业板指。

    使用东方财富 push API 直连获取实时行情，可靠性高于 akshare。
    """
    si = StockIndexData()
    import httpx

    # 三大指数 secid: 上证=1.000001, 深证=0.399001, 创业板=0.399006
    indices = [
        ("1.000001", "sh"),
        ("0.399001", "sz"),
        ("0.399006", "cyb"),
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            for secid, prefix in indices:
                # 重试机制（最多 3 次，避免东方财富 API 限流导致间歇性失败）
                for attempt in range(3):
                    try:
                        url = "https://push2.eastmoney.com/api/qt/stock/get"
                        params = {
                            "secid": secid,
                            "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f169,f170",
                        }
                        resp = await client.get(url, params=params)
                        resp.raise_for_status()
                        data = resp.json()
                        if not data or not data.get("data"):
                            if attempt < 2:
                                await asyncio.sleep(0.5 * (attempt + 1))
                                continue
                            logger.warning(
                                f"[macro] 指数 {secid} 返回空数据 (attempt {attempt + 1})"
                            )
                            break

                        d = data["data"]
                        # f43=最新价(需/100转点), f44=最高, f45=最低, f46=开盘, f47=成交量, f48=成交额(元)
                        # f57=代码, f58=名称, f169=涨跌额, f170=涨跌幅(需/100转%)
                        price = _safe_float(d.get("f43", 0)) / 100.0
                        change_pct = _safe_float(d.get("f170", 0)) / 100.0
                        prev_close = _safe_float(d.get("f46", 0)) / 100.0  # 开盘作为参考
                        turnover = _safe_float(d.get("f48", 0))  # 成交额（元）

                        if price > 0:
                            if prefix == "sh":
                                si.sh_index = price
                                si.sh_change_pct = change_pct
                                si.sh_close = prev_close
                            elif prefix == "sz":
                                si.sz_index = price
                                si.sz_change_pct = change_pct
                                si.sz_close = prev_close
                            elif prefix == "cyb":
                                si.cyb_index = price
                                si.cyb_change_pct = change_pct
                                si.cyb_close = prev_close
                            # 累加成交额（元 → 亿元）
                            si.total_volume += round(turnover / 1e8, 2)
                            si.valid = True
                            si.source = "eastmoney_push_api"
                            break  # 成功则跳出重试循环
                        elif attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                        else:
                            logger.warning(f"[macro] 指数 {secid} price=0 (attempt {attempt + 1})")
                    except Exception as e:
                        logger.warning(
                            f"[macro] 指数 {secid} 采集异常 (attempt {attempt + 1}): {e}"
                        )
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
    except Exception as e:
        logger.warning(f"[macro] A 股指数采集异常: {e}")

    # 尝试通过 akshare 获取涨跌停家数（东方财富涨停板统计）
    try:
        df = await _try_fetch("涨跌停统计", ak.stock_zt_pool_em, date="", timeout=15.0)
        if df is not None and not df.empty:
            si.limit_up_count = len(df)
    except Exception:
        pass

    if si.valid:
        logger.info(
            f"[macro] A 股指数: 上证={si.sh_index:.2f}({si.sh_change_pct:+.2f}%) "
            f"深证={si.sz_index:.2f}({si.sz_change_pct:+.2f}%) "
            f"创业板={si.cyb_index:.2f}({si.cyb_change_pct:+.2f}%)"
        )
    return si


# ═══════════════════════════════════════════════════════════
# 主采集器
# ═══════════════════════════════════════════════════════════


class MacroCollector:
    """五维宏观数据采集器。

    用法：
        collector = MacroCollector()
        snapshot = await collector.collect()
        print(snapshot.gold.gold_price)
    """

    async def collect(self) -> MacroSnapshot:
        """采集所有六维数据。各维度并行执行。"""
        tasks = {
            "gold": _collect_gold(),
            "oil": _collect_oil(),
            "fx": _collect_fx(),
            "bond": _collect_bond(),
            "derivative": _collect_derivatives(),
            "stock_index": _collect_stock_index(),
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        snapshot = MacroSnapshot()
        snapshot.timestamp = datetime.now().isoformat()

        for name, result in zip(tasks.keys(), results, strict=False):
            if isinstance(result, Exception):
                logger.warning(f"[macro] {name} 采集异常: {result}")
            elif result is not None:
                setattr(snapshot, name, result)

        valid_count = sum(
            [
                snapshot.gold.valid,
                snapshot.oil.valid,
                snapshot.fx.valid,
                snapshot.bond.valid,
                snapshot.derivative.valid,
                snapshot.stock_index.valid,
            ]
        )
        snapshot.all_valid = valid_count >= 3  # 至少 3/6 维度有效
        logger.info(f"[macro] 六维数据采集完成: {valid_count}/6 维度有效")
        return snapshot


# 全局单例
_collector: MacroCollector | None = None


def get_collector() -> MacroCollector:
    global _collector
    if _collector is None:
        _collector = MacroCollector()
    return _collector
