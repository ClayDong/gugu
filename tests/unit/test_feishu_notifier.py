"""FeishuNotifier 单元测试。

覆盖：
- notify_signal 发送 HTTP POST（mock httpx，验证 URL 和 payload）
- notify_signal 在 HTTP 200 时返回 True
- notify_signal 在 HTTP 错误（非 200）时返回 False
- notify_signal 在网络异常（httpx.RequestError）时返回 False
- notify_risk_alert 发送风控告警
- notify_error 发送异常通知
- notify_daily_report 发送日报
- webhook URL 为空时 send_text 返回 False
- 重试逻辑：首次失败后重试至 max_retries（3 次）
- 未配置凭证时跳过发送并返回 False

Mock 策略：仅 mock httpx.AsyncClient（通过 httpx.MockTransport），
业务逻辑（formatter、tenacity retry）均使用真实实现。
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gugu.notifier.feishu import TOKEN_URL, FeishuNotifier


@pytest.fixture
def mock_env():
    """Patch env() 为已配置状态，返回 mock_env 可按需覆盖字段。"""
    with patch("gugu.notifier.feishu.env") as mock_env:
        mock_env.return_value.feishu_app_id = "app-id"
        mock_env.return_value.feishu_app_secret = "secret"
        mock_env.return_value.feishu_chat_id = "system-chat-id"
        mock_env.return_value.feishu_business_chat_id = "business-chat-id"
        mock_env.return_value.feishu_webhook = (
            "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
        )
        yield mock_env


@pytest.fixture
def no_sleep():
    """Patch asyncio.sleep 为 no-op，避免 tenacity 重试等待拖慢测试。"""
    with patch("asyncio.sleep", new_callable=AsyncMock):
        yield


@pytest.fixture(autouse=True)
def clear_notified_signals(tmp_path):
    """每个测试前清除通知记录，避免测试间状态污染。"""
    import os
    from pathlib import Path

    if os.environ.get("GUGU_TEST_MODE"):
        import tempfile

        notified_path = Path(tempfile.gettempdir()) / "gugu_test_notified_signals.json"
        if notified_path.exists():
            notified_path.unlink()
    yield


def _ok_handler(requests: list[httpx.Request] | None = None):
    """构造成功响应 handler：token 接口和消息接口均返回 code=0。

    可选收集请求列表用于断言 URL 和 payload。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "fake-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "msg": "ok"})

    return handler


# ========== notify_signal ==========


@pytest.mark.asyncio
async def test_notify_signal_sends_post_with_correct_url_and_payload(mock_env):
    """notify_signal 发送 HTTP POST 到飞书消息接口，验证 URL 和 payload。"""
    requests: list[httpx.Request] = []
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(requests)))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        signal = {
            "symbol": "600519",
            "name": "贵州茅台",
            "direction": "buy",
            "strategy": "turtle",
            "strategies": ["turtle"],
            "reason": "突破上轨",
            "price": 1500.0,
            "confidence": 0.85,
        }
        result = await notifier.notify_signal(signal)

        assert result is True
        assert len(requests) == 2  # token + message

        # 验证 token 请求 URL
        token_req = requests[0]
        assert str(token_req.url) == TOKEN_URL

        # 验证消息请求 URL 和 payload
        msg_req = requests[1]
        assert msg_req.url.path == "/open-apis/im/v1/messages"
        assert msg_req.url.params.get("receive_id_type") == "chat_id"
        assert msg_req.headers.get("Authorization") == "Bearer fake-token"

        body = json.loads(msg_req.content)
        assert body["receive_id"] == "business-chat-id"
        assert body["msg_type"] == "interactive"
        content = json.loads(body["content"])
        assert content["header"]["template"] == "red"  # buy = red (A 股红涨)
        assert "600519" in content["header"]["title"]["content"]
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_notify_signal_uses_business_chat(mock_env):
    notifier = FeishuNotifier()
    with patch.object(notifier, "send_card", new_callable=AsyncMock) as send_card:
        signal = {
            "symbol": "600519",
            "direction": "buy",
            "strategy": "price_volume_signal",
            "strategies": ["price_volume_signal"],
            "reason": "换手+量比观察",
            "price": 1500.0,
            "confidence": 0.8,
        }
        await notifier.notify_signal(signal)
        assert send_card.await_args.kwargs["receive_id"] == "business-chat-id"


@pytest.mark.asyncio
async def test_notify_signal_returns_true_on_http_200(mock_env):
    """notify_signal 在 HTTP 200 响应时返回 True。"""
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        result = await notifier.notify_signal(
            {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.85,
                "strategies": ["turtle"],
                "reason": "突破",
            }
        )
        assert result is True
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_notify_signal_returns_false_on_http_error(mock_env, no_sleep):
    """notify_signal 在 HTTP 错误（非 200）时返回 False。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "fake-token", "expire": 7200},
            )
        return httpx.Response(500, text="Internal Server Error")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        result = await notifier.notify_signal(
            {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.85,
                "strategies": ["turtle"],
                "reason": "突破",
            }
        )
        assert result is False
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_notify_signal_returns_false_on_network_exception(mock_env, no_sleep):
    """notify_signal 在网络异常（httpx.RequestError）时返回 False。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RequestError("network error", request=request)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        result = await notifier.notify_signal(
            {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.85,
                "strategies": ["turtle"],
                "reason": "突破",
            }
        )
        assert result is False
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_notify_signal_rejects_invalid_price(mock_env):
    """价格异常（<=1.0）时拒绝发送，不调用 HTTP。"""
    notifier = FeishuNotifier()
    result = await notifier.notify_signal(
        {
            "symbol": "600519",
            "direction": "buy",
            "price": 1.0,
            "confidence": 0.85,
            "strategies": ["turtle"],
            "reason": "突破",
        }
    )
    assert result is False


@pytest.mark.asyncio
async def test_notify_signal_rejects_invalid_confidence(mock_env):
    """置信度异常（None 或 <=0）时拒绝发送，不调用 HTTP。"""
    notifier = FeishuNotifier()
    result = await notifier.notify_signal(
        {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": None,
            "strategies": ["turtle"],
            "reason": "突破",
        }
    )
    assert result is False

    result = await notifier.notify_signal(
        {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": 0.0,
            "strategies": ["turtle"],
            "reason": "突破",
        }
    )
    assert result is False


@pytest.mark.asyncio
async def test_notify_signal_rejects_empty_strategies(mock_env):
    """策略为空时拒绝发送，不调用 HTTP。"""
    notifier = FeishuNotifier()
    result = await notifier.notify_signal(
        {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": 0.85,
            "strategies": [],
            "reason": "突破",
        }
    )
    assert result is False


# ========== notify_risk_alert ==========


@pytest.mark.asyncio
async def test_notify_risk_alert_sends_alert(mock_env):
    """notify_risk_alert 发送风控告警到飞书。"""
    requests: list[httpx.Request] = []
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(requests)))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        alert = {"level": "halt", "message": "日亏5%熔断", "suggestion": "减仓"}
        result = await notifier.notify_risk_alert(alert)

        assert result is True
        assert len(requests) == 2  # token + message

        msg_req = requests[1]
        body = json.loads(msg_req.content)
        content = json.loads(body["content"])
        assert content["header"]["template"] == "red"  # halt = red
        assert "熔断" in content["header"]["title"]["content"]
    finally:
        await mock_client.aclose()


@patch("asyncio.sleep", new_callable=AsyncMock)
@pytest.mark.asyncio
async def test_notify_risk_alert_routes_to_business_chat(mock_sleep, mock_env):
    """P0 修复：风控告警（止损/熔断）属于交易业务 → 必须发到业务群（开悟之路）。"""
    notifier = FeishuNotifier()
    with patch.object(
        notifier, "send_card", new_callable=AsyncMock, return_value=True
    ) as send_card:
        alert = {"level": "halt", "message": "日亏5%熔断", "suggestion": "减仓"}
        result = await notifier.notify_risk_alert(alert)

        assert result is True
        assert send_card.await_args.kwargs["receive_id"] == "business-chat-id"


@pytest.mark.asyncio
async def test_notify_error_routes_to_system_chat(mock_env):
    """系统异常 → 必须发到系统对话（Hermes 对话），与业务群隔离。"""
    notifier = FeishuNotifier()
    with patch.object(
        notifier, "send_card", new_callable=AsyncMock, return_value=True
    ) as send_card:
        error = {"module": "data", "message": "采集失败", "suggestion": "重试"}
        result = await notifier.notify_error(error)

        assert result is True
        assert send_card.await_args.kwargs["receive_id"] == "system-chat-id"


@pytest.mark.asyncio
async def test_enqueue_preserves_receive_id(mock_env, tmp_path):
    """P0 修复：入队必须记录 receive_id，重试时发回原目标群。"""
    notifier = FeishuNotifier()
    notifier._queue_path = tmp_path / "notify_queue.jsonl"

    card = {
        "msg_type": "interactive",
        "card": {"header": {"title": {"tag": "plain_text", "content": "业务卡片"}}},
    }
    await notifier._enqueue_async(card, receive_id="business-chat-id")

    import json as _json

    lines = tmp_path.joinpath("notify_queue.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = _json.loads(lines[0])
    assert record["receive_id"] == "business-chat-id"


@pytest.mark.asyncio
async def test_retry_queued_sends_to_recorded_receive_id(mock_env, tmp_path):
    """P0 修复：重试队列中的业务卡片必须发回业务群，而非系统对话。"""
    notifier = FeishuNotifier()
    notifier._queue_path = tmp_path / "notify_queue.jsonl"
    notifier._queue_path.write_text(
        json.dumps(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "card": {
                    "msg_type": "interactive",
                    "card": {"header": {"title": {"tag": "plain_text", "content": "业务信号"}}},
                },
                "receive_id": "business-chat-id",
                "retry_count": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with (
        patch.object(notifier, "_post_card", new_callable=AsyncMock) as post_card,
        patch.object(notifier, "_get_tenant_token", new_callable=AsyncMock, return_value="token"),
    ):
        success_count = await notifier.retry_queued()

    assert success_count == 1
    # 重试必须带 receive_id=business-chat-id（不能回退到系统对话）
    assert post_card.await_args.args[2] == "business-chat-id"


@pytest.mark.asyncio
async def test_retry_queued_fallback_system_chat_for_legacy_items(mock_env, tmp_path):
    """兼容：旧队列条目（无 receive_id）重试时回退系统对话，与旧行为一致。"""
    notifier = FeishuNotifier()
    notifier._queue_path = tmp_path / "notify_queue.jsonl"
    notifier._queue_path.write_text(
        json.dumps(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "card": {
                    "msg_type": "interactive",
                    "card": {"header": {"title": {"tag": "plain_text", "content": "旧条目"}}},
                },
                "retry_count": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with (
        patch.object(notifier, "_post_card", new_callable=AsyncMock) as post_card,
        patch.object(notifier, "_get_tenant_token", new_callable=AsyncMock, return_value="token"),
    ):
        success_count = await notifier.retry_queued()

    assert success_count == 1
    assert post_card.await_args.args[2] is None  # 无 receive_id → 默认系统对话


# ========== notify_error ==========


@pytest.mark.asyncio
async def test_notify_error_sends_error_notification(mock_env):
    """notify_error 发送系统异常通知。"""
    requests: list[httpx.Request] = []
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(requests)))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        error = {"module": "data", "message": "采集失败", "suggestion": "重试"}
        result = await notifier.notify_error(error)

        assert result is True
        assert len(requests) == 2

        msg_req = requests[1]
        body = json.loads(msg_req.content)
        content = json.loads(body["content"])
        assert content["header"]["template"] == "red"  # error = red
        assert "系统异常" in content["header"]["title"]["content"]
        assert "data" in content["header"]["title"]["content"]
    finally:
        await mock_client.aclose()


# ========== notify_daily_report ==========


@pytest.mark.asyncio
async def test_notify_daily_report_sends_report(mock_env):
    """notify_daily_report 发送每日日报。"""
    requests: list[httpx.Request] = []
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(requests)))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        data = {
            "market_summary": {
                "total_value": 1_050_000,
                "cash": 500_000,
                "positions_count": 3,
            },
            "signals": [],
        }
        result = await notifier.notify_daily_report("close", data)

        assert result is True
        assert len(requests) == 2

        msg_req = requests[1]
        body = json.loads(msg_req.content)
        content = json.loads(body["content"])
        assert "收盘" in content["header"]["title"]["content"]
    finally:
        await mock_client.aclose()


# ========== webhook URL validation ==========


@pytest.mark.asyncio
async def test_send_text_returns_false_when_webhook_empty(mock_env):
    """webhook URL 为空时 send_text 返回 False，不发送任何请求。"""
    mock_env.return_value.feishu_webhook = ""
    notifier = FeishuNotifier()

    result = await notifier.send_text("test message")
    assert result is False


# ========== retry logic ==========


@pytest.mark.asyncio
async def test_retry_logic_retries_up_to_max_attempts(mock_env, no_sleep):
    """重试逻辑：首次失败后刷新 token 再试一轮，每轮 tenacity 重试 3 次，共 6 次。"""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # token endpoint always fails with network error
        raise httpx.RequestError("network error", request=request)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        result = await notifier.notify_signal(
            {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.85,
                "strategies": ["turtle"],
                "reason": "突破",
            }
        )

        assert result is False
        # send_card 首轮 _get_tenant_token tenacity 重试 3 次失败，
        # 等待 2 秒刷新 token 后再试一轮（3 次），共 6 次
        assert call_count == 6
    finally:
        await mock_client.aclose()


# ========== disabled notifier ==========


@pytest.mark.asyncio
async def test_disabled_notifier_skips_sending(mock_env):
    """未配置凭证时 send_card 跳过发送并返回 False。"""
    mock_env.return_value.feishu_app_id = ""
    mock_env.return_value.feishu_app_secret = ""
    mock_env.return_value.feishu_chat_id = ""

    notifier = FeishuNotifier()
    assert notifier._is_configured() is False

    result = await notifier.notify_signal({"symbol": "600519", "direction": "buy", "price": 1.0})
    assert result is False
