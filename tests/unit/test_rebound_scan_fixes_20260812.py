"""断板反包扫描器修复回归测试（2026-08-12 用户截图核验触发的 4 处修复）。

背景：8/12 推送 4 只标的（博迈科/柳钢/江河/南侨）经截图+数据双重核验全部不合格：
  ① 江河集团 首板后连涨 2 天(+15%) 从未断板 仍标 STAGE1 → 缺断板验证
  ② 博迈科 反包后 13 天仍每天推 STAGE4 → 缺反包时效约束
  ③ 柳钢/南侨 距支撑 13-20% 仍标"等回踩" → 伏击圈口径过宽
  ④ 推送把观察/滞后信号当"命中"写库 → 推送分级缺失

本测试用构造 K 线数据验证 4 处修复逻辑（不依赖网络）。
"""

import numpy as np
import pandas as pd
import pytest

from scripts.scan_rebound_market import analyze_stock


def make_df(close_list, opens=None, highs=None, lows=None, vols=None, start="2026-01-01"):
    """构造日K DataFrame（按 analyze_stock 期望的列）。"""
    n = len(close_list)
    opens = opens if opens is not None else [c * 0.99 for c in close_list]
    highs = highs if highs is not None else [max(o, c) * 1.02 for o, c in zip(opens, close_list)]
    lows = lows if lows is not None else [min(o, c) * 0.98 for o, c in zip(opens, close_list)]
    vols = vols if vols is not None else [1e6] * n
    dates = pd.date_range(start, periods=n, freq="B")
    return pd.DataFrame(
        {
            "date": dates,
            "open": opens,
            "close": close_list,
            "high": highs,
            "low": lows,
            "volume": vols,
        }
    )


class TestNoWashReject:
    """修复①：首板后无断板（连涨/加速）必须剔除。"""

    def test_first_board_then_acceleration_rejected(self):
        """首板涨停后连续两天大涨(+5%)，从未断板 → 应剔除(None)。"""
        # 横盘 30 日 → 首板涨停 → 次日+5% → 今日+3%（累计>12%）
        closes = [10.0] * 30 + [11.0, 11.55, 11.90]
        opens = [10.0] * 30 + [10.0, 11.0, 11.55]
        # 首板日放量涨停，后两日放量大涨（非断板）
        vols = [1e6] * 30 + [3e6, 3e6, 2.5e6]
        df = make_df(closes, opens=opens, vols=vols)
        assert analyze_stock("sh600000", df) is None, "首板后连涨无断板必须剔除"

    def test_first_board_recent_within_2d_allowed(self):
        """刚首板 1-2 天内（尚无断板机会）→ 仍标 STAGE1 观察（不写信号库）。"""
        closes = [10.0] * 30 + [11.0]
        opens = [10.0] * 30 + [10.0]
        vols = [1e6] * 30 + [3e6]
        df = make_df(closes, opens=opens, vols=vols)
        res = analyze_stock("sh600000", df)
        # 刚首板当天 → 允许 STAGE1（但不再写信号库，由 track 分级控制）
        assert res is None or "STAGE1" in res["stage"]


class TestStaleBreakoutReject:
    """修复②：反包必须发生在最近 3 个交易日内，更早=滞后剔除。"""

    def _build_stale_breakout(self):
        """构造: 首板→洗盘→反包(10天前)→近期横盘 的滞后反包形态。"""
        closes = [10.0] * 30 + [11.0, 10.5, 10.2, 10.3, 10.4, 10.3, 10.2, 10.1, 10.2, 10.3]
        # 首板 11.0(+10%)，次日洗盘 10.5(-4.5%)，第三天反包 10.2 未过首板高...需构造真实反包
        # 简单做法：首板后 2 日大阴洗盘，随后一根大阳>首板最高价
        return None

    def test_stale_breakout_detected(self):
        """洗盘后 10 天才反包 → 剔除(stale)。"""
        # 30日横盘 @10 → 首板11.5(+15%大阳) → 次日10.8(-6%洗盘,放量) → 之后缓慢爬升
        closes = [10.0] * 30 + [11.5, 10.8]
        # 洗盘后连续 8 天小幅上行，第 9 天突破首板高点 11.5 → 距首板已超 10 日
        for i in range(8):
            closes.append(round(10.8 + 0.08 * i, 2))
        closes.append(11.6)  # 第 9 天突破(距首板 11 日 → 窗口外)
        opens = [10.0] * 30 + [10.5, 11.5] + [10.9] * 8 + [11.5]
        vols = [1e6] * 30 + [3e6, 4e6] + [8e5] * 8 + [2e6]
        df = make_df(closes, opens=opens, vols=vols)
        res = analyze_stock("sh600000", df)
        # 要么剔除(stale/None)，要么不标 STAGE4 买点C
        if res:
            assert "买点C" not in res["stage"], "窗口外反包不得标 STAGE4"


class TestAmbushTighten:
    """修复③：伏击圈必须贴近支撑(≤8%)，偏离=剔除。"""

    def test_far_from_support_rejected(self):
        """现价距支撑 >8% → 剔除。"""
        # 首板后洗盘，护盘期股价站在远离支撑的位置（>8%）
        closes = [10.0] * 30 + [11.5, 10.6, 10.7, 10.8, 10.9, 11.0, 11.1, 11.2]
        opens = [10.0] * 30 + [10.5, 11.4, 10.6, 10.7, 10.8, 10.9, 11.0, 11.1]
        vols = [1e6] * 30 + [3e6, 4e6] + [5e5] * 6
        df = make_df(closes, opens=opens, vols=vols)
        res = analyze_stock("sh600000", df)
        if res:
            assert (
                "剔除" in res["stage"] or "偏离" in res["stage"] or res.get("stale")
            ), f"偏离支撑必须剔除, 实际: {res['stage']}"


class TestTrackHierarchy:
    """修复④：信号库只写可交易信号（买点A/时效内买点C）。"""

    def test_ambush_not_tracked_as_signal(self):
        """伏击圈观察不再映射为 W_信号类型（代码级验证：无 W_ 分支）。"""
        import inspect
        import scripts.scan_rebound_market as srm

        src = inspect.getsource(srm.main)
        assert "W_伏击圈观察" not in src, "伏击圈观察不得再写入信号库"
        assert "滞后" in src and "剔除" in src, "滞后/剔除类型必须在信号分级中被过滤"


class TestAlphaAdmissionGate:
    """根源修复⑤：已验证 alpha 判据作为信号准入门槛（而非标签）。"""

    def test_optimized_a_is_gate_not_label(self):
        """代码级验证：买点A 必须通过 check_optimized_a 才入库，不通过则 continue。"""
        import inspect
        import scripts.scan_rebound_market as srm

        src = inspect.getsource(srm.main)
        # 准入门槛必须存在：不通过 optimized_a → 不入库
        assert 'if not rec.get("optimized_a"):' in src, "买点A 必须通过四条件才入库"
        # 门槛后 3 行内必须有 continue（跳过不达标的买点A）
        tail = src.split('if not rec.get("optimized_a"):')[1].split("\n")[1:4]
        assert any(
            "continue" in line for line in tail
        ), f"不通过四条件的买点A 必须跳过(不入库), 门槛后代码: {tail}"

    def test_buyc_disabled_from_signals(self):
        """代码级验证：买点C 已证伪(p<0.0001)，一律不入信号库。"""
        import inspect
        import scripts.scan_rebound_market as srm

        src = inspect.getsource(srm.main)
        # 买点C 分支必须被 continue 拦截
        assert 'sig_type == "C_放量反包"' in src
        assert "已证伪" in src or "证伪" in src, "买点C 禁用原因必须在代码注释中声明"

    def test_check_optimized_a_requires_wash(self):
        """check_optimized_a 缺少洗盘日(未洗盘)必须返回 False。"""
        import scripts.scan_rebound_market as srm

        # 构造: 只有首板没有洗盘
        closes = [10.0] * 30 + [11.0, 11.2, 11.3]
        opens = [10.0] * 30 + [10.0, 11.0, 11.2]
        df = make_df(closes, opens=opens)
        res = {"first_open": 10.0, "wash_high": 999.0}  # 洗盘高不存在
        ok, fac = srm.check_optimized_a(df, res)
        assert ok is False, "无洗盘日的形态不能通过四条件"


class TestWashTodayBoundary:
    """修复⑥：洗盘日==今天(护盘期0天) 不得崩溃(凯龙股份 002783 zero-size array)。"""

    def test_wash_day_is_today_no_crash(self):
        """首板后次日即放量大阴(洗盘日=今天) → 返回'洗盘中'而非崩溃。"""
        # 横盘 → 首板涨停 → 今天放量大阴(-5%, 量≥1.5×首板)
        closes = [10.0] * 30 + [11.0, 10.45]
        opens = [10.0] * 30 + [10.0, 11.0]
        highs = [10.4] * 30 + [11.0, 10.9]
        lows = [9.8] * 30 + [10.0, 10.2]
        vols = [1e6] * 30 + [3e6, 5e6]  # 洗盘量 5e6 ≥ 1.5×3e6
        df = make_df(closes, opens=opens, highs=highs, lows=lows, vols=vols)
        res = analyze_stock("sh600000", df)
        # 不崩溃且返回合理状态(刚断板/洗盘中/剔除均可, 但不能抛异常)
        assert res is None or isinstance(res.get("stage"), str)
        if res:
            assert "买点A" not in res["stage"], "刚断板日不可能已回踩支撑"


class TestRootCauseAmountFilter:
    """根源修复⑦：首板/反包成交额校验不得一票否决(恒宝/利柏特/联创被误杀案)。"""

    def test_amount_filter_not_veto(self):
        """代码级验证：analyze_stock 不再用 float_mv 做首板/反包成交额一票否决。"""
        import inspect
        import scripts.scan_rebound_market as srm

        src = inspect.getsource(srm.analyze_stock)
        # 首板成交额校验已被移除：不得再出现 "amount[i] <= target + tol" 式硬过滤
        assert "target - tol <= amount[i]" not in src, "首板成交额校验必须移除(误杀真信号)"
        assert "float_mv * 1e8 * 0.015" not in src, "反包成交额校验必须移除"

    def test_float_mv_does_not_change_stage(self):
        """同一股票 传/不传 float_mv 判定必须一致(此前恒宝传则被拒)。"""
        import scripts.scan_rebound_market as srm

        # 构造: 首板涨停(缩量) → 放量断板 → 缩量护盘回踩 (完整买点A形态)
        closes = [10.0] * 30 + [11.0, 10.4, 10.3, 10.35, 10.32]
        opens = [10.0] * 30 + [10.0, 10.9, 10.35, 10.30, 10.35]
        highs = [10.4] * 30 + [11.0, 10.9, 10.5, 10.5, 10.45]
        lows = [9.8] * 30 + [10.0, 10.2, 10.15, 10.20, 10.15]
        # 首板 3e6(缩量) → 断板 5e6(≥1.5×) → 护盘 5e5(缩量≤0.5×)
        vols = [1e6] * 30 + [3e6, 5e6, 5e5, 5e5, 5e5]
        df = make_df(closes, opens=opens, highs=highs, lows=lows, vols=vols)
        r1 = analyze_stock("sh600000", df, float_mv=None)
        r2 = analyze_stock("sh600000", df, float_mv=100.0)  # 任意流通市值
        s1 = r1["stage"] if r1 else "None"
        s2 = r2["stage"] if r2 else "None"
        assert s1 == s2, f"float_mv 不得改变判定: {s1} vs {s2} (根因: 成交额校验误杀)"
