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
        mock_env.return_value.feishu_chat_id = "chat-id"
        mock_env.return_value.feishu_webhook = (
            "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
        )
        yield mock_env


@pytest.fixture
def no_sleep():
    """Patch asyncio.sleep 为 no-op，避免 tenacity 重试等待拖慢测试。"""
    with patch("asyncio.sleep", new_callable=AsyncMock):
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
            "reason": "突破上轨",
            "price": 1500.0,
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
        assert body["receive_id"] == "chat-id"
        assert body["msg_type"] == "interactive"
        content = json.loads(body["content"])
        assert content["header"]["template"] == "green"  # buy = green
        assert "600519" in content["header"]["title"]["content"]
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_notify_signal_returns_true_on_http_200(mock_env):
    """notify_signal 在 HTTP 200 响应时返回 True。"""
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        result = await notifier.notify_signal(
            {"symbol": "600519", "direction": "buy", "price": 1.0}
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
            {"symbol": "600519", "direction": "buy", "price": 1.0}
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
            {"symbol": "600519", "direction": "buy", "price": 1.0}
        )
        assert result is False
    finally:
        await mock_client.aclose()


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
    """重试逻辑：首次失败后重试至 max_retries（3 次）。"""
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
            {"symbol": "600519", "direction": "buy", "price": 1.0}
        )

        assert result is False
        # _get_tenant_token has @retry(stop=stop_after_attempt(3))
        assert call_count == 3
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

    result = await notifier.notify_signal(
        {"symbol": "600519", "direction": "buy", "price": 1.0}
    )
    assert result is False
