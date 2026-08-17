"""止损告警升级状态机契约测试（PR1 根治）。

覆盖（2026-08-14 评审实证的缺陷族）：
- 同日/同轮重复触发止损不得重置 escalation_level（旧实现每轮重置 → 永远升不到 level3）
- 升级必须"发送确认后"才推进，未送达不推进（旧实现 check 即推进 → 发送失败也升级）
- 升级节奏基于 last_escalation_ts 平滑推进（旧实现基于 first_alert_ts 累计 → 每轮扫描狂升）
- ack 后终止、clear 后重建、旧状态文件向后兼容
"""

from __future__ import annotations

import json
import time

import pytest

from gugu.analysis.stop_loss_escalator import StopLossAlertEscalator


@pytest.fixture
def escalator(tmp_path):
    return StopLossAlertEscalator(
        state_file=tmp_path / "stop_loss_alerts.json",
        escalation_intervals=[30, 60],
        max_escalation=3,
    )


def test_repeat_trigger_keeps_level(escalator):
    """回归：持续破位期间重复触发不得重置升级级别（旧实现重置为 0 → 振荡）。"""
    e = escalator
    e.record_stop_loss_triggered("510500")
    a = e._alerts["510500"]
    now = time.time()
    a.first_alert_ts = now - 3600  # 1 小时前首次告警
    a.last_escalation_ts = now - 3600
    a.escalation_level = 2

    # 破位持续，下一轮扫描再次触发
    e.record_stop_loss_triggered("510500")

    assert e._alerts["510500"].escalation_level == 2  # 旧实现重置为 0（bug）


def test_escalation_progresses_only_after_confirm(escalator):
    """check 只报告、不推进；发送确认后级别才推进（旧实现 check 即推进 → 发送失败也升级）。"""
    e = escalator
    now = time.time()
    e.record_stop_loss_triggered("510500")
    a = e._alerts["510500"]
    a.first_alert_ts = now - 7200  # 2 小时前
    a.last_escalation_ts = now - 7200

    needs, level, label = e.check_escalation("510500")
    assert needs is True
    assert level == 1
    assert "紧急" in label
    # 未确认发送前，状态不得推进（旧实现在这里直接推进 → 发送失败也升级）
    assert e._alerts["510500"].escalation_level == 0

    # 确认发送成功后才推进
    e.confirm_escalation_sent("510500", 1)
    assert e._alerts["510500"].escalation_level == 1

    # 刚确认完立刻再 check：距上次确认间隔不足 → 不再重复升级
    needs2, _, _ = e.check_escalation("510500")
    assert needs2 is False


def test_upgrade_waits_for_interval_after_confirm(escalator):
    """升级节奏基于 last_escalation_ts：level1 确认后需间隔 [30] 分钟才升 level2。"""
    e = escalator
    now = time.time()
    e.record_stop_loss_triggered("600519")
    a = e._alerts["600519"]
    a.first_alert_ts = now - 7200
    a.last_escalation_ts = now - 7200

    needs1, level1, _ = e.check_escalation("600519")
    assert needs1 and level1 == 1
    e.confirm_escalation_sent("600519", 1)

    # 模拟 10 分钟后（< 30min）：不应升 level2
    a.last_escalation_ts = now - 600
    needs2, level2, _ = e.check_escalation("600519")
    assert needs2 is False

    # 模拟 70 分钟后（>= 30min 间隔，距上次确认 70min）：升 level2
    a.last_escalation_ts = now - 4200
    needs3, level3, _ = e.check_escalation("600519")
    assert needs3 and level3 == 2
    e.confirm_escalation_sent("600519", 2)


def test_ack_stops_escalation(escalator):
    e = escalator
    e.record_stop_loss_triggered("600519")
    e.acknowledge("600519")
    needs, _, _ = e.check_escalation("600519")
    assert needs is False
    assert e._alerts["600519"].acknowledged is True


def test_clear_removes_state(escalator):
    e = escalator
    e.record_stop_loss_triggered("510500")
    e.clear("510500")
    assert "510500" not in e._alerts


def test_max_escalation_cap(escalator):
    """达到 max_escalation 后停止升级（不无限刷屏）。"""
    e = escalator
    now = time.time()
    e.record_stop_loss_triggered("600519")
    a = e._alerts["600519"]
    a.first_alert_ts = now - 999999
    a.last_escalation_ts = now - 999999
    e.confirm_escalation_sent("600519", 3)

    needs, _, _ = e.check_escalation("600519")
    assert needs is False
    assert e._alerts["600519"].escalation_level == 3


def test_from_dict_backward_compat(tmp_path):
    """旧状态文件无 last_escalation_ts → 兼容为 first_alert_ts（不崩溃、节奏从首次告警起算）。"""
    p = tmp_path / "stop_loss_alerts.json"
    p.write_text(
        json.dumps(
            {
                "510500": {
                    "symbol": "510500",
                    "first_alert_ts": 111.0,
                    "last_alert_ts": 222.0,
                    "escalation_level": 1,
                    "acknowledged": False,
                }
            }
        ),
        encoding="utf-8",
    )
    e = StopLossAlertEscalator(state_file=p)
    assert e._alerts["510500"].last_escalation_ts == 111.0


def test_to_dict_roundtrip(tmp_path):
    """状态序列化/反序列化完整（含新字段 last_escalation_ts）。"""
    e = StopLossAlertEscalator(state_file=tmp_path / "sl.json")
    e.record_stop_loss_triggered("510500")
    e.confirm_escalation_sent("510500", 1)
    e2 = StopLossAlertEscalator(state_file=tmp_path / "sl.json")
    a = e2._alerts["510500"]
    assert a.escalation_level == 1
    assert a.last_escalation_ts > 0
