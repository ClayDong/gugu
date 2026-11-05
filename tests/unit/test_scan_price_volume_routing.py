"""scripts/scan_price_volume.py 推送路由契约测试（2026-10-20）。

背景：换手+量比观察消息曾误发 Hermes 机器人 P2P 对话框
（.env FEISHU_CHAT_ID = 系统通知目标），应发"开悟之路"业务群
（.env FEISHU_BUSINESS_CHAT_ID）。

契约（与 docs/DOMAIN_CONVENTIONS.md 呈现完整性一致）：
    --chat 显式参数 > FEISHU_BUSINESS_CHAT_ID（业务群）> FEISHU_CHAT_ID（系统兜底）
任何路由回归在此直接失败。
"""

from __future__ import annotations

import importlib
import json
import urllib.request

import pytest

spv = importlib.import_module("scripts.scan_price_volume")

BUSINESS = "oc_business_sentinel"
SYSTEM = "oc_system_sentinel"


class _FakeResp:
    def read(self) -> bytes:
        return json.dumps({"code": 0}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def captured_receive_id(monkeypatch):
    """拦截飞书 HTTP 调用与环境加载，返回记录 receive_id 的闭包。

    _load_env 置为 no-op：由 monkeypatch 完全控制环境变量，
    避免真实 .env（含 FEISHU_BUSINESS_CHAT_ID）渗入断言。
    """
    holder: dict[str, str] = {}

    def _fake_urlopen(req, *args, **kwargs):  # noqa: ANN001, ANN002
        body = json.loads(req.data.decode())
        if "receive_id" in body:
            holder["receive_id"] = body["receive_id"]
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(spv, "_fetch_tenant_token", lambda *a, **k: "fake-token")
    monkeypatch.setattr(spv, "_load_env", lambda *a, **k: None)
    return holder


def test_push_defaults_to_business_chat(monkeypatch, captured_receive_id):
    """默认目标必须是业务群（开悟之路），而非系统 chat（Hermes 对话框）。"""
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_BUSINESS_CHAT_ID", BUSINESS)
    monkeypatch.setenv("FEISHU_CHAT_ID", SYSTEM)

    assert spv._push_feishu_text("test") is True
    assert captured_receive_id["receive_id"] == BUSINESS


def test_push_falls_back_to_system_chat_when_no_business(monkeypatch, captured_receive_id):
    """未配置业务群时回退系统 chat（保证不丢通知）。"""
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.delenv("FEISHU_BUSINESS_CHAT_ID", raising=False)
    monkeypatch.setenv("FEISHU_CHAT_ID", SYSTEM)

    assert spv._push_feishu_text("test") is True
    assert captured_receive_id["receive_id"] == SYSTEM


def test_push_explicit_chat_wins(monkeypatch, captured_receive_id):
    """--chat 显式参数优先级最高。"""
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_BUSINESS_CHAT_ID", BUSINESS)

    assert spv._push_feishu_text("test", chat_id="oc_explicit") is True
    assert captured_receive_id["receive_id"] == "oc_explicit"
