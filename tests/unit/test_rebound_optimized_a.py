"""优化版买点A 四条件判定单元测试（check_optimized_a，合成K线）

2026-08-11 因子漏斗实验裁决出的规则：
  ① 首板涨停(≥9.5%)  ② 首板量<3×20日均量  ③ 洗盘量≥1.5×首板量  ④ 护盘缩量≤0.5×洗盘量
"""

import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from scan_rebound_market import check_optimized_a  # noqa: E402


def make_df(days):
    df = pd.DataFrame(days, columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def base_rows(first_vol=20000, first_close=22.0, wash_vol=40000, protect_vols=(10000, 8000)):
    """标准形态链: 横盘36日 → 首板涨停 → 放量洗盘 → 缩量护盘2日（总40行）"""
    import datetime as dt

    rows = []
    d0 = dt.date(2026, 6, 15)
    prev = 20.0
    for i in range(36):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        rows.append((d, prev, prev, prev + 0.02, prev - 0.02, 10000))
    # 首板: 涨停 20.0→22.0
    rows.append(
        ((d0 + dt.timedelta(days=36)).isoformat(), 20.5, first_close, first_close, 20.5, first_vol)
    )
    # 洗盘: 放量大阴
    rows.append(((d0 + dt.timedelta(days=37)).isoformat(), 21.5, 20.0, 21.8, 19.9, wash_vol))
    # 护盘
    for i, (c, v) in enumerate([(20.2, protect_vols[0]), (20.1, protect_vols[1])]):
        rows.append(((d0 + dt.timedelta(days=38 + i)).isoformat(), 20.2, c, c + 0.1, c - 0.2, v))
    return rows


def check(rows, res=None):
    df = make_df(rows)
    res = res or {"first_open": 20.5, "wash_high": 21.8}
    return check_optimized_a(df, res)


class TestOptimizedA:
    def test_all_conditions_met(self):
        ok, fac = check(base_rows())
        assert ok is True
        assert fac["first_pct"] >= 0.095
        assert fac["first_vol_ratio"] < 3.0
        assert fac["wash_vol_ratio"] >= 1.5
        assert fac["protect_shrink"] <= 0.5

    def test_first_board_not_limit_up(self):
        # 首板仅 +7.5% 大阳（非涨停）→ 条件①不满足
        ok, _ = check(base_rows(first_close=21.5))
        assert ok is False

    def test_first_board_over_volume(self):
        # 首板量 40000 = 4×20日均量 → 条件②不满足（情绪顶点）
        ok, fac = check(base_rows(first_vol=40000))
        assert ok is False
        assert fac["first_vol_ratio"] >= 3.0

    def test_wash_not_volume(self):
        # 洗盘量 15000 < 1.5×首板量 → 条件③不满足（不是暴力洗盘）
        ok, _ = check(base_rows(wash_vol=15000))
        assert ok is False

    def test_protect_not_shrink(self):
        # 护盘量 30000/35000，min/洗盘量 = 0.75 > 0.5 → 条件④不满足
        ok, _ = check(base_rows(protect_vols=(35000, 30000)))
        assert ok is False

    def test_missing_res_fields(self):
        # 缺 wash_high → 安全返回 False
        df = make_df(base_rows())
        ok, fac = check_optimized_a(df, {"first_open": 20.5})
        assert ok is False
        assert fac == {}

    def test_short_history(self):
        # 历史不足 40 日 → 安全返回 False
        rows = base_rows()[:25]
        ok, _ = check(rows)
        assert ok is False
