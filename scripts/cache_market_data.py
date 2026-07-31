"""全市场股票数据批量缓存脚本。

使用新浪财经接口（免限流）批量获取全市场股票的日 K 线数据，
保存为 parquet 格式到 data/parquet_cache/，供回测脚本使用。

设计：
1. 从 akshare 获取全市场股票列表（如失败用缓存列表）
2. 用新浪接口并发获取每只股票 500 根日 K 线
3. 保存为 {code}_500d.parquet
4. 支持断点续传（已存在的跳过）

用法:
    uv run python scripts/cache_market_data.py
    uv run python scripts/cache_market_data.py --max-stocks 100  # 调试
    uv run python scripts/cache_market_data.py --datalen 1000    # 取 1000 根
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import akshare as ak
import httpx
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# 性能参数
_CONCURRENCY = 20  # 并发数（新浪接口较稳定，可以高一些）
_TIMEOUT = 8  # 单股超时秒
_BATCH_LOG = 50  # 每多少只打印进度
_SINA_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"


def get_stock_list(max_stocks: int | None = None) -> list[tuple[str, str, str]]:
    """获取全市场股票列表，返回 [(sina_symbol, code, name), ...]。

    新浪格式: sh600519 / sz000001 / bj430047
    优先用 akshare，失败时降级到东财 push2 接口。
    """
    stocks = []

    # 方式1: akshare stock_zh_a_spot_em（首选）
    try:
        spot = ak.stock_zh_a_spot_em()
        col_map = {"code": "代码", "name": "名称"}
        spot = spot.rename(columns={k: v for k, v in col_map.items() if k in spot.columns})
        spot["代码"] = spot["代码"].astype(str)
        mask = ~spot["代码"].str.startswith(("bj", "43", "83", "87", "88", "4", "8"))
        mask &= ~spot["名称"].str.contains("ST|退|N|^C", na=False, regex=True)
        codes = spot[mask]["代码"].tolist()
        names = spot[mask]["名称"].tolist()
        for code, name in zip(codes, names):
            sina_sym = _to_sina_symbol(code)
            if sina_sym:
                stocks.append((sina_sym, code, name))
        logger.info(f"获取股票列表: {len(stocks)} 只（akshare stock_zh_a_spot_em）")
    except Exception as e:
        logger.warning(f"akshare stock_zh_a_spot_em 失败: {e}")

    # 方式2: akshare stock_zh_a_spot（降级）
    if not stocks:
        try:
            spot = ak.stock_zh_a_spot()
            codes = spot["代码"].astype(str).tolist()
            names = spot["名称"].tolist()
            for code, name in zip(codes, names):
                if code.startswith(("4", "8")):
                    continue
                sina_sym = _to_sina_symbol(code)
                if sina_sym:
                    stocks.append((sina_sym, code, name))
            logger.info(f"获取股票列表: {len(stocks)} 只（akshare stock_zh_a_spot）")
        except Exception as e2:
            logger.warning(f"akshare stock_zh_a_spot 失败: {e2}")

    # 方式3: 东财 push2 批量实时行情接口（兜底）
    if not stocks:
        logger.info("akshare 全挂，尝试东财 push2 批量接口获取股票列表...")
        stocks = _fetch_stock_list_from_eastmoney()

    # 方式4: 生成全市场代码列表（最后兜底，name 为空）
    if not stocks:
        logger.info("所有在线接口失败，使用代码生成器（全市场 9997 个代码）")
        stocks = _generate_all_codes()

    if max_stocks and len(stocks) > max_stocks:
        stocks = stocks[:max_stocks]
        logger.info(f"限制为前 {max_stocks} 只（调试模式）")

    return stocks


def _to_sina_symbol(code: str) -> str | None:
    """股票代码转新浪格式。"""
    if code.startswith("6"):
        return f"sh{code}"
    elif code.startswith(("0", "3")):
        return f"sz{code}"
    return None


def _fetch_stock_list_from_eastmoney() -> list[tuple[str, str, str]]:
    """东财 push2 批量接口获取股票列表（最后兜底）。"""
    import httpx

    # 东财 push2 全市场列表: f12=代码, f14=名称
    # secids 不传，用 fields1=f3,f12,f14
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",  # 页码
        "pz": "10000",  # 每页大小
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",  # 沪深主板+创业板+科创板
        "fields": "f12,f14",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()
        diff = data.get("data", {}).get("diff", []) if data.get("data") else []
        stocks = []
        for item in diff:
            code = str(item.get("f12", ""))
            name = str(item.get("f14", ""))
            if not code or len(code) != 6:
                continue
            if name.startswith(("ST", "退", "N", "C")):
                continue
            sina_sym = _to_sina_symbol(code)
            if sina_sym:
                stocks.append((sina_sym, code, name))
        logger.info(f"东财 push2 获取股票列表: {len(stocks)} 只")
        return stocks
    except Exception as e:
        logger.error(f"东财 push2 获取股票列表失败: {e}")
        return []


def _generate_all_codes() -> list[tuple[str, str, str]]:
    """生成全市场股票代码列表（不依赖在线 API）。

    基于 A 股代码规则生成所有可能的代码，name 为空。
    新浪接口会自动过滤不存在的代码（返回空数据）。
    """
    codes: list[str] = []
    # 沪市主板 600000-603999
    codes.extend(str(n) for n in range(600000, 604000))
    # 沪市主板 605000-605999
    codes.extend(str(n) for n in range(605000, 606000))
    # 沪市科创板 688000-688999
    codes.extend(str(n) for n in range(688000, 689000))
    # 深市主板 000001-001999
    codes.extend(f"{n:06d}" for n in range(1, 2000))
    # 深市中小板 002001-002999
    codes.extend(f"{n:06d}" for n in range(2001, 3000))
    # 深市创业板 300001-300999
    codes.extend(str(n) for n in range(300001, 301000))

    # 转换为新浪格式
    result = []
    for code in codes:
        sina_sym = f"sh{code}" if code.startswith("6") else f"sz{code}"
        result.append((sina_sym, code, ""))
    return result


async def fetch_one(
    client: httpx.AsyncClient,
    sina_symbol: str,
    code: str,
    datalen: int,
    sem: asyncio.Semaphore,
) -> pd.DataFrame | None:
    """获取单股 K 线数据。"""
    params = {
        "symbol": sina_symbol,
        "scale": "240",  # 日K
        "ma": "no",
        "datalen": str(datalen),
    }
    async with sem:
        try:
            async with asyncio.timeout(_TIMEOUT):
                resp = await client.get(_SINA_URL, params=params)
                text = resp.text
                # 解析 =( [{...}] ) 格式
                start = text.find("[")
                end = text.rfind("]") + 1
                if start < 0 or end <= 0:
                    logger.debug(f"{sina_symbol} 响应格式异常")
                    return None
                data = json.loads(text[start:end])
                if not data:
                    return None
                df = pd.DataFrame(data)
                # 重命名列
                df = df.rename(columns={"day": "date"})
                # 转换类型
                for col in ["open", "high", "low", "close", "volume"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                # 添加 amount 字段（新浪不提供，填 0）
                if "amount" not in df.columns:
                    df["amount"] = 0.0
                # 按日期升序
                df = df.sort_values("date").reset_index(drop=True)
                return df
        except (TimeoutError, Exception) as e:
            logger.debug(f"{sina_symbol} 获取失败: {e}")
            return None


async def cache_batch(
    stocks: list[tuple[str, str, str]],
    cache_dir: Path,
    datalen: int,
    skip_existing: bool = True,
) -> tuple[int, int]:
    """批量缓存。返回 (成功数, 失败数)。"""
    sem = asyncio.Semaphore(_CONCURRENCY)
    cache_dir.mkdir(parents=True, exist_ok=True)

    success = 0
    failed = 0
    skipped = 0
    total = len(stocks)
    t0 = time.time()

    async with httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://finance.sina.com.cn/",
        }
    ) as client:
        # 先检查哪些需要下载
        to_download = []
        for sina_sym, code, name in stocks:
            f = cache_dir / f"{code}_{datalen}d.parquet"
            if skip_existing and f.exists():
                skipped += 1
            else:
                to_download.append((sina_sym, code, name))

        if skipped > 0:
            logger.info(f"跳过已缓存: {skipped} 只，待下载: {len(to_download)} 只")

        if not to_download:
            return (skipped, 0)

        # 并发下载
        async def _one(sina_sym: str, code: str, name: str) -> bool:
            df = await fetch_one(client, sina_sym, code, datalen, sem)
            if df is None or len(df) < 30:
                return False
            f = cache_dir / f"{code}_{datalen}d.parquet"
            df.to_parquet(f, index=False)
            return True

        tasks = [_one(*s) for s in to_download]
        completed = 0
        for coro in asyncio.as_completed(tasks):
            ok = await coro
            if ok:
                success += 1
            else:
                failed += 1
            completed += 1
            if completed % _BATCH_LOG == 0 or completed == len(to_download):
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(to_download) - completed) / rate if rate > 0 else 0
                logger.info(
                    f"缓存进度: {completed}/{len(to_download)} "
                    f"({completed/len(to_download)*100:.1f}%) "
                    f"成功={success} 失败={failed} "
                    f"速率={rate:.1f}/s "
                    f"ETA={eta/60:.1f}分钟"
                )

    return (success, failed)


async def main_async(args: argparse.Namespace) -> None:
    cache_dir = Path(args.cache_dir)

    # 1. 获取股票列表
    stocks = get_stock_list(args.max_stocks)
    if not stocks:
        logger.error("无法获取股票列表")
        return

    logger.info(f"开始缓存: {len(stocks)} 只股票 × {args.datalen} 根 K 线")

    # 2. 批量下载
    t0 = time.time()
    success, failed = await cache_batch(stocks, cache_dir, args.datalen, not args.force)

    elapsed = time.time() - t0
    logger.info(
        f"缓存完成: 成功 {success} + 失败 {failed} = {success+failed}, "
        f"耗时 {elapsed/60:.1f} 分钟"
    )

    # 3. 验证
    files = list(cache_dir.glob(f"_{args.datalen}d.parquet"))
    total_cache = list(cache_dir.glob("*.parquet"))
    logger.info(
        f"缓存目录: {len(files)} 个 _{args.datalen}d 文件, 总计 {len(total_cache)} 个 parquet"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="全市场数据批量缓存（新浪接口）")
    parser.add_argument("--max-stocks", type=int, default=None, help="限制股票数（调试用）")
    parser.add_argument("--datalen", type=int, default=500, help="K 线根数（默认 500）")
    parser.add_argument("--cache-dir", default="data/parquet_cache", help="缓存目录")
    parser.add_argument("--force", action="store_true", help="强制重新下载（不跳过已缓存）")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
