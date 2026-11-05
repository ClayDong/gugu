"""市场环境比率指标的无样本语义测试（2026-11-05）。

背景：`market_env.assess()` 原先用 `else 0` 把「分母为 0」压成 0%，
再经 `can_relay = 0 >= 0.6` 判 False，最后由 `should_avoid_relay` 否决信号。
这是把「不知道」当成「不合格」。

实证据（修复前）：`data/trade_journal.jsonl` 144 条交易记录 100% 是
`gate_blocked`，一笔未成交；其中 45 条原因写着「接力成功率0%低于60%」。
自选股池仅 18 只，「前日涨停」候选长期为 0，于是该指标恒为 0、
拦截永久生效——策略信号永远进不了成交，`paper_signals` 也永远攒不到。

本文件锁住三条语义：
1. 分母为 0 → 比率为 None、`can_*` 为 None，不得伪造成 0/False；
2. `should_avoid_*` 遇到 None 不做否决（无法判断 ≠ 不合格）；
3. 有样本且低于阈值时仍然严格拦截（修 bug 不能顺手放宽风控）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from gugu.analysis.market_env import (
    MarketEnvironment,
    should_avoid_board,
    should_avoid_relay,
)


def _df(
    *,
    today_ret: float,
    prev_ret: float = 0.0,
    open_above_prev_close: bool = False,
    intraday_high_ret: float | None = None,
):
    """构造最小日线：三根 bar，用于控制今日涨幅/前日涨幅/是否高开/盘中最高。

    收盘价基准 100：
    - close[-3] = 100，close[-2] = 100*(1+prev_ret)，close[-1] = close[-2]*(1+today_ret)
    - open[-1]：按 open_above_prev_close 决定是否高于 close[-2]（高开判定用）
    - high[-1]：intraday_high_ret 给出时按 close[-2]*(1+该值) 设置，用于模拟炸板
    """
    c3 = 100.0
    c2 = c3 * (1 + prev_ret)
    c1 = c2 * (1 + today_ret)
    o1 = c2 * 1.01 if open_above_prev_close else c2 * 0.99
    h1 = c2 * (1 + intraday_high_ret) if intraday_high_ret is not None else max(o1, c1)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-11-03", periods=3, freq="D"),
            "open": [c3, c2, o1],
            "high": [c3, c2, max(h1, o1, c1)],
            "low": [c3, c2, min(o1, c1, h1)],
            "close": [c3, c2, c1],
            "volume": [1_000_000] * 3,
            "amount": [1e7] * 3,
        }
    )


# ========== 无样本：比率必须为 None，不得伪造成 0 ==========


def test_no_limit_up_yields_none_seal_rate() -> None:
    """池内无涨停 → 封板率无法计算，必须是 None 而不是 0。"""
    env = MarketEnvironment().assess({f"s{i}": _df(today_ret=0.01) for i in range(5)})

    assert env["limit_up_count"] == 0
    assert env["seal_rate"] is None
    assert env["can_board"] is None


def test_no_relay_candidate_yields_none_relay_rate() -> None:
    """池内无「前日涨停」股 → 接力成功率无法计算，必须是 None。

    这是导致 45 条信号被永久拦截的直接原因。
    """
    env = MarketEnvironment().assess({f"s{i}": _df(today_ret=0.01) for i in range(5)})

    assert env["relay_candidate_count"] == 0
    assert env["relay_rate"] is None
    assert env["can_relay"] is None


def test_none_rates_do_not_trigger_avoidance() -> None:
    """无法判断时不做否决——否则策略会被伪 0 分永久锁死。"""
    env = MarketEnvironment().assess({f"s{i}": _df(today_ret=0.01) for i in range(5)})

    assert should_avoid_board(env) is False
    assert should_avoid_relay(env) is False


def test_insufficient_samples_marked_insufficient() -> None:
    """样本不足时整体状态标记为 insufficient，由调用方走「仅观察」分支。"""
    env = MarketEnvironment().assess({"s0": _df(today_ret=0.01)})
    assert env["detector_status"] == "insufficient"


# ========== 有样本：必须仍然严格拦截 ==========


def test_relay_below_threshold_still_blocks() -> None:
    """有接力候选但成功率 0% → 仍然拦截，不能因为修 bug 而放宽风控。"""
    data = {
        "relay_fail": _df(today_ret=0.02, prev_ret=0.10, open_above_prev_close=False),
        **{f"flat{i}": _df(today_ret=0.01) for i in range(4)},
    }
    env = MarketEnvironment().assess(data)

    assert env["relay_candidate_count"] == 1
    assert env["relay_rate"] == pytest.approx(0.0)
    assert env["can_relay"] is False
    assert should_avoid_relay(env) is True


def test_relay_above_threshold_passes() -> None:
    """接力成功（高开）→ 允许接力。"""
    data = {
        "relay_ok": _df(today_ret=0.02, prev_ret=0.10, open_above_prev_close=True),
        **{f"flat{i}": _df(today_ret=0.01) for i in range(4)},
    }
    env = MarketEnvironment().assess(data)

    assert env["relay_rate"] == pytest.approx(1.0)
    assert env["can_relay"] is True
    assert should_avoid_relay(env) is False


def test_sealed_limit_up_still_passes_board_gate() -> None:
    """正常涨停（收盘封在涨停价）→ 封板率 100%，允许打板。"""
    data = {
        "sealed": _df(today_ret=0.10),
        **{f"flat{i}": _df(today_ret=0.01) for i in range(4)},
    }
    env = MarketEnvironment().assess(data)

    assert env["limit_up_count"] == 1
    assert env["touched_limit_count"] == 1
    assert env["seal_rate"] == pytest.approx(1.0)
    assert env["can_board"] is True
    assert should_avoid_board(env) is False


def test_yizi_board_counts_as_sealed_not_broken() -> None:
    """一字板（开=收=涨停价）必须算封板。

    原实现用 `close > open` 判封板，一字板 open == close 被判成「未封板」，
    导致最强形态反而被 can_board=False 否决——判定方向是反的。
    """
    yizi = _df(today_ret=0.10)
    yizi.loc[2, "open"] = yizi.loc[2, "close"]  # 一字板：开盘即涨停价
    yizi.loc[2, "high"] = yizi.loc[2, "close"]
    env = MarketEnvironment().assess(
        {"yizi": yizi, **{f"f{i}": _df(today_ret=0.01) for i in range(4)}}
    )

    assert env["limit_up_count"] == 1
    assert env["seal_rate"] == pytest.approx(1.0)
    assert env["can_board"] is True
    assert should_avoid_board(env) is False


def test_broken_board_blocks() -> None:
    """炸板（盘中触及涨停但收盘回落）→ 封板率 0%，仍然拦截打板。

    炸板股收盘涨幅不足 9.5%，不计入 limit_up_count，但计入 touched_limit_count，
    所以它是封板率的分母——这才是「封板率」应有的含义。
    """
    data = {
        "broke": _df(today_ret=0.03, intraday_high_ret=0.10),
        **{f"flat{i}": _df(today_ret=0.01) for i in range(4)},
    }
    env = MarketEnvironment().assess(data)

    assert env["limit_up_count"] == 0, "炸板收盘未涨停，不计入收盘涨停数"
    assert env["touched_limit_count"] == 1, "盘中触及涨停应计入封板率分母"
    assert env["seal_rate"] == pytest.approx(0.0)
    assert env["can_board"] is False
    assert should_avoid_board(env) is True


# ========== 兼容性：旧字典（缺字段）不得抛异常 ==========


def test_avoid_helpers_tolerate_legacy_env_without_keys() -> None:
    """旧环境字典没有 can_* 字段时，按「无法判断」处理而非 KeyError。"""
    assert should_avoid_board({}) is False
    assert should_avoid_relay({}) is False


def test_avoid_helpers_respect_explicit_false() -> None:
    """显式 False 必须继续拦截，不能被 None 分支吞掉。"""
    assert should_avoid_board({"can_board": False}) is True
    assert should_avoid_relay({"can_relay": False}) is True
