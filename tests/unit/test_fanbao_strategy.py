"""Tests for 小圈圈儿《反包策略》V2.0 完整版。

覆盖 5+1 种战法 + 风险红线 + 情绪周期。
"""

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.fanbao.rules import (
    RULE_VERSION,
    FanbaoSignal,
    enrich,
    check_red_lines,
    detect_sentiment_cycle,
    detect_zhaban_fanbao,
    detect_dieting_fanbao,
    detect_yizi_fanbao,
    detect_callback_fanbao,
    detect_tiaokong_fanbao,
    detect_baoli_xipan,
    detect_all,
    check_general_conditions,
)


# ── 辅助：生成测试K线数据 ──────────────────────────────


def _make_kline(
    closes: list[float],
    *,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
    base_date: str = "2025-01-01",
) -> pd.DataFrame:
    n = len(closes)
    if opens is None:
        opens = [c * 0.99 for c in closes]
    if highs is None:
        highs = [max(o, c) * 1.02 for o, c in zip(opens, closes)]
    if lows is None:
        lows = [min(o, c) * 0.98 for o, c in zip(opens, closes)]
    if volumes is None:
        volumes = [1_000_000.0] * n
    dates = pd.date_range(base_date, periods=n, freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


# ── 基础功能测试 ──────────────────────────────────────


def test_rule_version_exists():
    assert RULE_VERSION
    assert isinstance(RULE_VERSION, str)


def test_enrich_adds_columns():
    n = 260
    df = _make_kline(closes=[10.0 + i * 0.1 for i in range(n)])
    enriched = enrich(df)
    for col in [
        "pct",
        "high_pct",
        "low_pct",
        "open_pct",
        "vol_ma5",
        "vol_ratio",
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "ma250",
        "amp",
        "amp_ma20",
        "is_limit_up",
        "is_dieting",
        "is_yizi",
        "limit_250",
    ]:
        assert col in enriched.columns, f"enrich() 缺失列: {col}"


def test_fanbao_signal_dataclass():
    sig = FanbaoSignal(
        strategy="测试战法",
        code="sh600519",
        reason="测试信号",
        operation="买入",
    )
    assert sig.strategy == "测试战法"
    assert sig.signal_type == "买入"
    assert sig.confidence == 0.5


# ── 风险红线测试 ──────────────────────────────────────


def test_red_lines_passed():
    df = _make_kline(closes=[10.0] * 100)
    red = check_red_lines("sh600519", df, float_mv=200, close_price=15.0)
    assert red.passed
    assert len(red.reasons) == 0


def test_red_lines_low_price():
    df = _make_kline(closes=[10.0] * 100)
    red = check_red_lines("sh600519", df, close_price=2.5)
    assert not red.passed
    assert any("红线3" in r for r in red.reasons)


def test_red_lines_small_mv():
    df = _make_kline(closes=[10.0] * 100)
    red = check_red_lines("sh600519", df, float_mv=5.0)
    assert not red.passed
    assert any("红线4" in r for r in red.reasons)


def test_red_lines_board_filter():
    df = _make_kline(closes=[10.0] * 100)
    red = check_red_lines("sh688001", df, float_mv=200)
    assert not red.passed


# ── 情绪周期测试 ──────────────────────────────────────


def test_sentiment_no_data():
    sent = detect_sentiment_cycle()
    assert sent["stage"] == "unknown"
    assert not sent["is_operatable"]


def test_sentiment_weak_market():
    dates = pd.date_range("2025-01-01", periods=100, freq="D")
    closes = 3000 + np.arange(100) * -5  # 持续下跌
    hs = pd.DataFrame({"date": dates, "close": closes.astype(float)})
    sent = detect_sentiment_cycle(market_df=hs)
    assert not sent["is_operatable"]


# ── 炸板反包（模式二）测试 ──────────────────────────────


def _build_active_stock() -> pd.DataFrame:
    """构建250日活跃股行情，精确满足所有前置条件。

    保证：
    - 位置分位 < 0.7（低位震荡）
    - limit_250 >= 3（活跃度）
    - amp_ma20 > 3%（股性活跃）
    """
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=250, freq="D")

    # 构造震荡行情：价格围绕 10 元波动，振幅 4%
    base = 10.0
    closes = []
    for i in range(250):
        noise = np.random.randn() * 0.15
        revert = (10.0 - (closes[-1] if closes else base)) * 0.03
        price = (closes[-1] if closes else base) + noise + revert
        price = max(price, 8.0)
        price = min(price, 12.0)
        # 第 50, 100, 150, 200 天构造涨停（明确 10%）
        if i in (50, 100, 150, 200):
            price = (closes[-1] if closes else base) * 1.10
        closes.append(price)

    # 确保最后一天处于低位（< 250日高位的70%）
    recent_max = max(closes)
    recent_min = min(closes)
    if (closes[-1] - recent_min) / (recent_max - recent_min + 1e-10) >= 0.7:
        closes[-1] = recent_min + (recent_max - recent_min) * 0.5

    # 高开低开构造（确保振幅>3%）
    opens = [c * (0.985 + 0.03 * np.random.rand()) for c in closes]
    highs = [max(c, o) * (1.0 + 0.02 * np.random.rand()) for c, o in zip(closes, opens)]
    lows = [min(c, o) * (1.0 - 0.02 * np.random.rand()) for c, o in zip(closes, opens)]
    volumes = [int(1_000_000 + 500_000 * np.random.rand()) for _ in range(250)]

    return pd.DataFrame(
        {
            "date": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def _make_zhaban_data(base_df: pd.DataFrame) -> pd.DataFrame:
    """在 base_df 上添加炸板反包的最后 3 根 K 线。"""
    df = base_df.copy()
    n = len(df)
    last_close = df["close"].iloc[-1]
    # T-2: 涨停
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_close,
                        "high": last_close * 1.10,
                        "low": last_close * 0.998,
                        "close": last_close * 1.10,
                        "volume": 5_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    last_close = df["close"].iloc[-1]
    # T-1: 炸板（高开+触板+回落+收盘>6%）
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_close * 1.02,  # 高开2%
                        "high": last_close * 1.095,  # 触板9.5%
                        "low": last_close * 0.99,
                        "close": last_close * 1.07,  # 收盘涨7%
                        "volume": 8_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    last_close = df["close"].iloc[-1]
    # T: 高开3%（买入信号日）
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_close * 1.03,
                        "high": last_close * 1.04,
                        "low": last_close * 0.99,
                        "close": last_close * 1.02,
                        "volume": 6_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    return df


def test_zhaban_fanbao_standard():
    df = _build_active_stock()
    df = _make_zhaban_data(df)
    sig = detect_zhaban_fanbao("sh600519", df, float_mv=100, skip_checks=True)
    assert sig is not None, f"炸板反包应被检测到，实际: {sig}"
    assert "炸板反包" in sig.strategy


def test_zhaban_no_gap():
    """今日没有高开，不应触发。"""
    df = _build_active_stock()
    df = _make_zhaban_data(df)
    # 把最后一天的 open 改成平开
    df.iloc[-1, df.columns.get_loc("open")] = df["close"].iloc[-2]
    sig = detect_zhaban_fanbao("sh600519", df, float_mv=100)
    assert sig is None, "没有高开不应触发"


def test_zhaban_too_small_cap():
    df = _build_active_stock()
    df = _make_zhaban_data(df)
    sig = detect_zhaban_fanbao("sh600519", df, float_mv=30)
    assert sig is None, "市值不足50亿不应触发"


# ── 跌停板战法（模式一）测试 ──────────────────────────────


def _make_dieting_data(base_df: pd.DataFrame) -> pd.DataFrame:
    """在 base_df 上添加跌停反包的最后 2 根 K 线。"""
    df = base_df.copy()
    last_close = df["close"].iloc[-1]
    # T-1: 跌停
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_close * 0.95,
                        "high": last_close,
                        "low": last_close * 0.90,
                        "close": last_close * 0.90,
                        "volume": 3_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    last_close = df["close"].iloc[-1]
    # T: 高开2%
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_close * 1.02,
                        "high": last_close * 1.03,
                        "low": last_close * 0.98,
                        "close": last_close * 1.01,
                        "volume": 4_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    return df


def test_dieting_fanbao_standard():
    df = _build_active_stock()
    df = _make_dieting_data(df)
    sig = detect_dieting_fanbao("sh600519", df, float_mv=100)
    assert sig is not None, f"跌停反包应被检测到，实际: {sig}"
    assert "跌停板" in sig.strategy


def test_dieting_no_gap():
    df = _build_active_stock()
    df = _make_dieting_data(df)
    df.iloc[-1, df.columns.get_loc("open")] = df["close"].iloc[-2]
    sig = detect_dieting_fanbao("sh600519", df, float_mv=100)
    assert sig is None, "没有高开不应触发"


# ── 暴力洗盘（补充模式）测试 ──────────────────────────────


def _make_baoli_data(base_df: pd.DataFrame) -> pd.DataFrame:
    """在 base_df 上添加暴力洗盘的 K 线。"""
    df = base_df.copy()
    last_close = df["close"].iloc[-1]
    # T-2: 涨停（暴力洗盘前需要有涨停）
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_close,
                        "high": last_close * 1.10,
                        "low": last_close * 0.998,
                        "close": last_close * 1.10,
                        "volume": 5_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    last_close = df["close"].iloc[-1]
    # T-1: 暴力急跌 -8%
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_close * 0.96,
                        "high": last_close,
                        "low": last_close * 0.90,
                        "close": last_close * 0.92,
                        "volume": 7_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    last_close = df["close"].iloc[-1]
    # T: 高开2%
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_close * 1.02,
                        "high": last_close * 1.03,
                        "low": last_close * 0.98,
                        "close": last_close * 1.01,
                        "volume": 5_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    return df


def test_baoli_xipan_standard():
    df = _build_active_stock()
    df = _make_baoli_data(df)
    sig = detect_baoli_xipan("sh600519", df, float_mv=100, is_hot_topic=True)
    assert sig is not None, f"暴力洗盘应被检测到，实际: {sig}"
    assert "暴力洗盘" in sig.strategy


def test_baoli_not_hot_topic():
    df = _build_active_stock()
    df = _make_baoli_data(df)
    sig = detect_baoli_xipan("sh600519", df, float_mv=100, is_hot_topic=False)
    assert sig is None, "非热点股票不应触发暴力洗盘"


# ── 跳空涨停式反包（模式五）测试 ──────────────────────────


def _make_tiaokong_data(base_df: pd.DataFrame) -> pd.DataFrame:
    """跳空涨停（开盘>昨日最高）→一板断→高开。"""
    df = base_df.copy()
    last_close = df["close"].iloc[-1]
    last_high = df["high"].iloc[-1]

    # T-1: 跳空高开涨停（开盘>昨日最高）
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_high * 1.01,  # 跳空
                        "high": last_high * 1.10,
                        "low": last_high * 0.99,
                        "close": last_high * 1.10,  # 涨停
                        "volume": 5_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    last_close = df["close"].iloc[-1]
    # T: 断板+高开
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].iloc[-1] + pd.Timedelta(days=1),
                        "open": last_close * 1.02,
                        "high": last_close * 1.03,
                        "low": last_close * 0.98,
                        "close": last_close * 1.01,
                        "volume": 4_000_000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    return df


def test_tiaokong_fanbao_standard():
    df = _build_active_stock()
    df = _make_tiaokong_data(df)
    sig = detect_tiaokong_fanbao("sh600519", df, float_mv=100, skip_checks=True)
    assert sig is not None, f"跳空涨停反包应被检测到，实际: {sig}"
    assert "跳空" in sig.strategy


# ── 全战法检测测试 ──────────────────────────────────────


def test_detect_all_returns_list():
    df = _build_active_stock()
    sigs = detect_all("sh600519", df, float_mv=100, skip_red_lines=True, sentiment_ok=True)
    assert isinstance(sigs, list)


# ── 通用前置条件测试 ──────────────────────────────────────


def test_general_conditions_not_enough_data():
    df = _make_kline(closes=[10.0] * 30)
    res = check_general_conditions(df)
    assert not res["passed"]
    assert "数据不足" in res["reason"]


# ── FanbaoStrategy 端到端测试 ──────────────────────────────


def test_fanbao_strategy_importable():
    from gugu.strategies.fanbao_strategy import FanbaoStrategy

    strat = FanbaoStrategy()
    assert strat.name == "fanbao"
    assert strat.category == "short_term"


def test_fanbao_strategy_returns_signal_on_zhaban():
    from gugu.strategies.fanbao_strategy import FanbaoStrategy

    strat = FanbaoStrategy(params={"skip_red_lines": True, "use_gate_open": True})

    df = _build_active_stock()
    df = _make_zhaban_data(df)
    df["code"] = "sh600519"
    df["float_mv"] = 100.0

    result = strat.generate_signals(df)
    assert "signal" in result.columns
    assert "confidence" in result.columns


def test_fanbao_strategy_no_signal_without_gap():
    """use_gate_open=True 时，无高开不应发信号。"""
    from gugu.strategies.fanbao_strategy import FanbaoStrategy

    strat = FanbaoStrategy(params={"skip_red_lines": True, "use_gate_open": True})

    df = _build_active_stock()
    df = _make_zhaban_data(df)
    df.iloc[-1, df.columns.get_loc("open")] = df["close"].iloc[-2]  # 平开
    df["code"] = "sh600519"
    df["float_mv"] = 100.0

    result = strat.generate_signals(df)
    assert result["signal"].iloc[-1] != 1, "无高开不应有买入信号"


def test_fanbao_strategy_zhaban_high_open_gives_signal():
    """炸板反包 + 高开 → 买入信号。"""
    from gugu.strategies.fanbao_strategy import FanbaoStrategy

    strat = FanbaoStrategy(
        params={
            "skip_red_lines": True,
            "use_gate_open": True,
            "skip_sentiment": True,
            "skip_checks": True,
        }
    )

    df = _build_active_stock()
    df = _make_zhaban_data(df)
    df["code"] = "sh600519"
    df["float_mv"] = 100.0

    result = strat.generate_signals(df)
    # 最后一天的 open_pct=3% > 0，应有信号
    ddf = enrich(df)
    open_pct = ddf["open_pct"].iloc[-1]
    assert open_pct > 0, f"测试数据构造错误，open_pct应为正: {open_pct*100:.1f}%"
    assert result["signal"].iloc[-1] == 1, f"高开({open_pct*100:.1f}%)应有买入信号"


# ── 板块过滤测试 ──────────────────────────────────────


def test_board_filter_rejects_kechuang():
    df = _build_active_stock()
    sig = detect_zhaban_fanbao("sh688001", df, float_mv=100)
    assert sig is None


def test_board_filter_rejects_chuangyeban():
    df = _build_active_stock()
    sig = detect_zhaban_fanbao("sz300001", df, float_mv=100)
    assert sig is None


def test_board_filter_accepts_main():
    df = _build_active_stock()
    df = _make_zhaban_data(df)
    sig = detect_zhaban_fanbao("sh600519", df, float_mv=100, skip_checks=True)
    assert sig is not None, "主板应通过过滤"
