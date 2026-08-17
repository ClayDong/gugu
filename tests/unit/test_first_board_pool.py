"""首板观察池状态机单元测试（合成K线，不依赖网络）。

场景覆盖：
  1. is_limit_up 涨停判定（涨幅+收盘贴顶）
  2. is_first_board 首板判定（近20日无涨停+当日涨停）
  3. advance_state: 待断板(连板) / 断板→洗盘中 / 断板→缩量→伏击圈 / 破位失败 / 反包涨停 / 超时
  4. 状态文件 load/save 持久化 + 事件日志
  5. 幂等：同状态重复 update 不误报变更
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from first_board_pool import (  # noqa: E402
    SUPPORT_TOL,
    advance_state,
    is_first_board,
    is_limit_up,
    load_state,
    log_event,
    save_state,
)

START = "2026-07-01"  # 横盘起点；首板日 = iso(START, 12) = 2026-07-13


# ---------------------------------------------------------------- helpers
def make_df(days):
    df = pd.DataFrame(days, columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def flat_days(n, base=20.0, vol=10000, start=START):
    import datetime as dt

    d0 = dt.date.fromisoformat(start)
    prev = base
    rows = []
    for i in range(n):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        rows.append((d, prev, prev, prev + 0.02, prev - 0.02, vol))
    return rows


def limit_day(d, prev_close, open_pct=0.025):
    o = prev_close * (1 + open_pct)
    c = prev_close * 1.10
    return (d, round(o, 2), round(c, 2), round(c, 2), round(o, 2), 50000)


def base_item(code="sz001229", first_date=None, first_open=20.5, first_pct=10.0):
    first_date = first_date or iso(START, 12)
    return {
        "code": code,
        "name": "测试股",
        "first_board_date": first_date,
        "first_open": first_open,
        "first_close": round(first_open * 1.10, 2),
        "first_pct": first_pct,
        "support": round(first_open * SUPPORT_TOL, 2),
        "total_mv": 80.0,
        "status": "待断板",
    }


def iso(d, i):
    import datetime as dt

    return (dt.date.fromisoformat(d) + dt.timedelta(days=i)).isoformat()


# ---------------------------------------------------------------- 1. 涨停判定
class TestLimitUp:
    def test_standard_limit_up(self):
        assert is_limit_up({"pct": 0.10, "high": 22.0, "close": 22.0})

    def test_close_near_high_ok(self):
        assert is_limit_up({"pct": 0.099, "high": 22.05, "close": 22.0})

    def test_close_far_from_high_rejected(self):
        # 尾盘炸板：close 远离 high → 不算涨停
        assert not is_limit_up({"pct": 0.099, "high": 23.0, "close": 22.0})

    def test_low_pct_rejected(self):
        assert not is_limit_up({"pct": 0.06, "high": 22.0, "close": 22.0})

    def test_any_pct_above_threshold_counts(self):
        # 语义：pct>=9.7% 即涨停（20cm 股由 all_codes 主板白名单排除，函数不分板块）
        assert is_limit_up({"pct": 0.198, "high": 22.0, "close": 22.0})


# ---------------------------------------------------------------- 2. 首板判定
class TestFirstBoard:
    def test_pure_first_board(self):
        rows = flat_days(25)
        rows.append(limit_day(iso(START, 25), rows[-1][2]))
        assert is_first_board(make_df(rows))

    def test_limit_up_in_past_20d_rejected(self):
        rows = flat_days(10)
        rows.append(limit_day(iso(START, 10), rows[-1][2]))  # 旧涨停
        prev = rows[-1][2]
        for i in range(5, 0, -1):  # 涨停后回落横盘
            rows.append(flat_days(1, base=prev * 0.98, start=iso(START, 11))[0])
        rows.append(limit_day(iso(START, 16), rows[-1][2]))  # 当日涨停
        df = make_df(rows)
        assert not is_first_board(df)

    def test_no_limit_up_today_rejected(self):
        rows = flat_days(15)
        rows.append(flat_days(1, base=21.0, start=iso(START, 15))[0])
        assert not is_first_board(make_df(rows))

    def test_short_history_rejected(self):
        assert not is_first_board(make_df(flat_days(10)))


# ---------------------------------------------------------------- 3. 状态机
class TestAdvanceState:
    def test_waiting_break_consecutive_boards(self):
        """首板后继续涨停 → 待断板"""
        rows = flat_days(12)
        rows.append(limit_day(iso(START, 12), rows[-1][2]))  # 首板
        rows.append(limit_day(iso(START, 13), rows[-1][2]))  # 连板
        rows.append(limit_day(iso(START, 14), rows[-1][2]))  # 连板
        df = make_df(rows)
        status, changed, detail = advance_state("sz001229", base_item(), df)
        assert status == "待断板"
        assert not changed

    def test_broken_then_washing(self):
        """断板后量未萎缩 → 洗盘中"""
        rows = flat_days(12)
        rows.append(limit_day(iso(START, 12), rows[-1][2]))  # 首板
        rows.append((iso(START, 13), 22.0, 20.9, 22.2, 20.8, 80000))  # 断板放量阴
        rows.append((iso(START, 14), 20.9, 20.6, 21.0, 20.4, 70000))  # 量未萎缩
        df = make_df(rows)
        status, changed, detail = advance_state("sz001229", base_item(), df)
        assert status == "洗盘中"
        assert changed  # 首次断板变更

    def test_broken_shrink_vicinity(self):
        """断板→缩量护盘不破位 → 伏击圈（核心场景）"""
        rows = flat_days(12)
        rows.append(limit_day(iso(START, 12), rows[-1][2]))  # 首板 开20.5 收22.55
        rows.append((iso(START, 13), 22.0, 20.9, 22.2, 20.8, 80000))  # 断板放量阴
        rows.append((iso(START, 14), 20.9, 20.7, 21.0, 20.5, 40000))  # 缩量
        rows.append((iso(START, 15), 20.7, 20.6, 20.9, 20.4, 25000))  # 继续缩量
        df = make_df(rows)
        item = base_item(first_open=20.5)
        status, changed, detail = advance_state("sz001229", item, df)
        assert status == "伏击圈"
        assert changed
        # 幂等：再跑一次不报变更
        item["status"] = "伏击圈"
        status2, changed2, _ = advance_state("sz001229", item, df)
        assert status2 == "伏击圈"
        assert not changed2

    def test_broken_then_break_support_failed(self):
        """断板后收盘跌破首板开盘价×0.97 → 破位失败"""
        rows = flat_days(12)
        rows.append(limit_day(iso(START, 12), rows[-1][2]))  # 首板 开20.5
        rows.append((iso(START, 13), 22.0, 20.9, 22.2, 20.8, 80000))  # 断板
        rows.append((iso(START, 14), 20.9, 19.5, 21.0, 19.3, 60000))  # 破位 19.5 < 19.885
        df = make_df(rows)
        status, changed, detail = advance_state("sz001229", base_item(first_open=20.5), df)
        assert status == "破位失败"
        assert changed

    def test_broken_then_rebound_limit_up(self):
        """断板后再涨停 → 反包涨停（移交 scan 买点C）"""
        rows = flat_days(12)
        rows.append(limit_day(iso(START, 12), rows[-1][2]))  # 首板
        rows.append((iso(START, 13), 22.0, 20.9, 22.2, 20.8, 80000))  # 断板
        rows.append(limit_day(iso(START, 14), 20.9))  # 反包涨停
        df = make_df(rows)
        status, changed, detail = advance_state("sz001229", base_item(), df)
        assert status == "反包涨停"
        assert changed

    def test_timeout_no_break(self):
        """首板后 26 日仍连板（未断板）→ 超时"""
        rows = flat_days(12)
        rows.append(limit_day(iso(START, 12), rows[-1][2]))  # 首板
        prev = rows[-1][2]
        for i in range(26):
            rows.append(limit_day(iso(START, 13 + i), prev))  # 连续26个涨停日
            prev = rows[-1][2]
        df = make_df(rows)
        status, changed, detail = advance_state("sz001229", base_item(), df)
        assert status == "超时"

    def test_first_date_missing(self):
        """首板日不在K线内 → 不崩溃，保持原状态"""
        df = make_df(flat_days(15))
        item = base_item(first_date="2025-01-01")
        status, changed, _ = advance_state("sz001229", item, df)
        assert status == "待断板"
        assert not changed


# ---------------------------------------------------------------- 4. 持久化
class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        p = tmp_path / "pool.json"
        state = {"updated": "2026-08-11T15:05:00", "active": {"sz001229": {"code": "sz001229"}}}
        save_state(p, state)
        loaded = load_state(p)
        assert loaded["active"]["sz001229"]["code"] == "sz001229"

    def test_load_missing_file(self, tmp_path):
        assert load_state(tmp_path / "nope.json") == {"updated": None, "active": {}}

    def test_log_event_appends(self, tmp_path):
        ev = tmp_path / "events.jsonl"
        log_event(ev, "sz001229", "测试", "NEW_FIRST_BOARD", "首板")
        log_event(ev, "sz001229", "测试", "VICINITY", "伏击圈")
        lines = [json.loads(l) for l in ev.read_text(encoding="utf-8").splitlines()]
        assert [x["event"] for x in lines] == ["NEW_FIRST_BOARD", "VICINITY"]
