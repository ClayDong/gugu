"""Point-in-Time 盘中隔离：不允许把当日未完成日 K 当成已收盘数据使用。

2026-11-04 评审：``tests/unit/test_lookahead_fix.py`` 原先在测试文件内部自行
复刻 ``_should_trim()``，而生产代码中根本不存在对应逻辑——这是典型的
「测试自证」：测试永远绿，线上没有任何防线。

本模块把判定收敛为真实生产函数，并由 ``DataManager.fetch_stock_history``
在所有返回路径（实时请求 / 内存缓存 / Parquet 缓存）统一调用。
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# A 股收盘时刻；HH:MM 零填充字符串可直接比较
CLOSE_HM = "15:00"


def should_trim_intraday_bar(df: pd.DataFrame, *, now_hm: str, today: date) -> bool:
    """判断盘中是否应剔除最后一根未完成的当日 K 线。

    语义（2026-08-11 事故复盘）：
    - 收盘后（>= 15:00）：当日 K 线已定稿，不剔除；
    - 盘中且最后一根 bar 日期 == 今天：该 bar 未收盘，属于未来信息，剔除；
    - 盘中但最后一根 bar 是昨日已收盘：保留。A 股多数日 K 源盘中不含当日
      bar，无条件 ``iloc[:-1]`` 会让策略基于 2 个交易日前的数据计算；
    - 无 date 列 / 空数据 / 日期无法解析：不剔除，交由数据质量校验处理。
    """
    if now_hm >= CLOSE_HM:
        return False
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    if "date" not in df.columns:
        return False
    try:
        last_bar_date = pd.Timestamp(df["date"].iloc[-1]).date()
    except Exception:  # noqa: BLE001 - 脏数据不应让数据加载整体失败
        return False
    return last_bar_date == today


def trim_unfinished_intraday_bar(df: pd.DataFrame, *, now: datetime | None = None) -> pd.DataFrame:
    """盘中剔除当日未完成 bar，保留 ``df.attrs``，幂等可重复调用。

    ``now`` 仅供测试注入时间基准；生产调用不传，使用当前系统时间。
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df
    current = now or datetime.now()
    now_hm = current.strftime("%H:%M")
    if not should_trim_intraday_bar(df, now_hm=now_hm, today=current.date()):
        return df
    clean = df.iloc[:-1].copy()
    clean.attrs.update(df.attrs)
    logger.info(
        "PIT 隔离: 盘中剔除当日未完成 K 线（{} {}），剩余 {} 行",
        current.date(),
        now_hm,
        len(clean),
    )
    return clean
