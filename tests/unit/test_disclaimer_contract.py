"""呈现层免责与诚实展示契约（2026-11-04 评审 P0-4）。

任何一条回归都会让未经验证的信号重新以「投资建议」面貌出现。
"""

from __future__ import annotations

import importlib
import json
import re
import urllib.request
from pathlib import Path

import pytest

from gugu.notifier.disclaimer import (
    DISCLAIMER_TEXT,
    MIN_PERFORMANCE_SAMPLES,
    performance_note,
    resolve_win_rate,
    with_disclaimer,
)
from gugu.notifier.formatter import format_signal

ROOT = Path(__file__).resolve().parents[2]


def _card_text(**overrides) -> str:
    signal = {
        "symbol": "000858",
        "name": "五粮液",
        "direction": "buy",
        "price": 150.0,
        "strategy": "box_breakout",
        "confidence": 0.7,
    }
    signal.update(overrides)
    return json.dumps(format_signal(signal), ensure_ascii=False)


def test_with_disclaimer_is_idempotent() -> None:
    once = with_disclaimer("观察信号")
    assert DISCLAIMER_TEXT in once
    assert with_disclaimer(once) == once


def test_with_disclaimer_handles_empty_text() -> None:
    assert with_disclaimer("") == DISCLAIMER_TEXT


def test_signal_card_always_has_disclaimer() -> None:
    assert DISCLAIMER_TEXT in _card_text()


def test_llm_source_still_has_disclaimer() -> None:
    """旧实现仅在 source != llm 时注入，LLM 解读会让免责静默消失。"""
    text = _card_text(wisdom_decision={"source": "llm"}, llm_pending=True)
    assert DISCLAIMER_TEXT in text


def test_signal_only_branch_is_not_required_for_disclaimer() -> None:
    text = _card_text(llm_pending=True)
    assert DISCLAIMER_TEXT in text


def test_small_sample_hides_win_rate() -> None:
    text = _card_text(
        performance_summary={
            "sample_count": 3,
            "forward_5d_signal_hit_rate": 0.8,
            "avg_return_5d": 0.05,
        }
    )
    assert "样本不足" in text
    assert "胜率：" not in text


def test_sufficient_sample_uses_correct_field_name() -> None:
    """engine_mixin 注入 forward_5d_signal_hit_rate，旧代码只读 win_rate 恒为 0%。"""
    text = _card_text(
        performance_summary={
            "sample_count": MIN_PERFORMANCE_SAMPLES + 10,
            "forward_5d_signal_hit_rate": 0.6,
            "avg_return_5d": 0.05,
        }
    )
    assert "胜率：60%" in text


def test_performance_note_and_win_rate_helpers() -> None:
    assert performance_note(3) is not None
    assert performance_note(30) is None
    assert resolve_win_rate({"forward_5d_signal_hit_rate": 0.42}) == pytest.approx(0.42)
    assert resolve_win_rate({}) == 0.0


def test_web_page_has_disclaimer() -> None:
    html = (ROOT / "src/gugu/web/static/index.html").read_text(encoding="utf-8")
    assert "不构成投资建议" in html
    assert "模拟盘" in html


def test_scan_push_includes_disclaimer(monkeypatch) -> None:
    """实验性选股观察直推业务群时也必须带免责。"""
    spv = importlib.import_module("scripts.scan_price_volume")
    captured: dict[str, str] = {}

    class _Resp:
        def read(self) -> bytes:
            return json.dumps({"code": 0}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, *args, **kwargs):  # noqa: ANN001, ANN002
        body = json.loads(req.data.decode())
        captured["text"] = json.loads(body["content"])["text"]
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(spv, "_fetch_tenant_token", lambda *a, **k: "t")
    monkeypatch.setattr(spv, "_load_env", lambda *a, **k: None)
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_CHAT_ID", "oc_test")

    assert spv._push_feishu_text("今日触发买入信号") is True
    assert DISCLAIMER_TEXT in captured["text"]


def test_conditional_pass_is_marked_observation_only() -> None:
    """conditional_pass 未通过 gate-v2，不得被当作可执行买入建议。"""
    from gugu.strategies.capability import CONDITIONAL

    assert CONDITIONAL == "conditional"
    source = (ROOT / "src/gugu/engine/signal_pipeline.py").read_text(encoding="utf-8")
    match = re.search(r"validation_status in \{([^}]+)\}", source)
    assert match is not None
    assert '"conditional"' in match.group(1)
    assert "observation_only" in source
