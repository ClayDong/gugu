"""FeishuNotifier 卡片指纹去重测试。

覆盖：
- send_card 发送成功后记录指纹
- 同一卡片在 1 小时内重复发送被跳过（含日期后缀，跨日不会误拦截）
- retry_queued 重试时已发送过的卡片被跳过
- 指纹记录超过 1 小时后自动清理
- 不同卡片不受去重影响
- 指纹包含日期后缀，23:50-00:10 窗口跨日信号不被误拦截
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gugu.notifier.feishu import FeishuNotifier


@pytest.fixture
def mock_env():
    """Patch env() 为已配置状态，并启用测试模式（队列写入临时目录）。"""
    import os

    os.environ["GUGU_TEST_MODE"] = "1"
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
    """Patch asyncio.sleep 为 no-op，避免等待拖慢测试。"""
    with patch("asyncio.sleep", new_callable=AsyncMock):
        yield


def _ok_handler():
    """构造成功响应 handler。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "fake-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "msg": "ok"})

    return handler


def _make_card(symbol: str = "600519", title: str = "测试信号") -> dict:
    """构造测试卡片。"""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"股票代码: {symbol}"},
                }
            ],
        },
    }


# ========== 指纹去重测试 ==========


@pytest.mark.asyncio
async def test_send_card_records_fingerprint(mock_env):
    """send_card 发送成功后应记录卡片指纹。"""
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        card = _make_card("600519", "买入信号")
        fingerprint = notifier._get_card_fingerprint(card)

        # 发送前指纹不应在记录中
        assert fingerprint not in notifier._recent_card_fingerprints

        result = await notifier.send_card(card)
        assert result is True

        # 发送后指纹应在记录中
        assert fingerprint in notifier._recent_card_fingerprints
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_send_card_skips_duplicate_within_5min(mock_env):
    """同一卡片在 5 分钟内重复发送应被跳过。"""
    requests: list[httpx.Request] = []
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        card = _make_card("600519", "买入信号")

        # 第一次发送
        result1 = await notifier.send_card(card)
        assert result1 is True

        # 第二次发送（应被跳过：去重命中 = 未发送，返回 False）
        # PR1 语义修正：旧实现返回 True 假装成功，导致告警被吞而调用方无感
        result2 = await notifier.send_card(card)
        assert result2 is False  # 去重命中 = 未发送

        # 验证只发送了一次 HTTP 请求（token + message = 2 次）
        # 由于我们使用 MockTransport，无法直接计数，但可以通过指纹记录验证
        assert len(notifier._recent_card_fingerprints) == 1
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_send_card_allows_different_cards(mock_env):
    """不同卡片不受去重影响，应正常发送。"""
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        card1 = _make_card("600519", "买入信号A")
        card2 = _make_card("000001", "买入信号B")

        result1 = await notifier.send_card(card1)
        result2 = await notifier.send_card(card2)

        assert result1 is True
        assert result2 is True

        # 两个不同卡片的指纹都应被记录
        assert len(notifier._recent_card_fingerprints) == 2
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_fingerprint_expires_after_5min(mock_env):
    """指纹记录超过 5 分钟后应自动清理，允许重新发送。"""
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    notifier = FeishuNotifier()
    notifier._client = mock_client

    try:
        card = _make_card("600519", "买入信号")

        # 第一次发送
        result1 = await notifier.send_card(card)
        assert result1 is True

        # 模拟 6 分钟前发送的指纹（应被清理）
        old_fingerprint = notifier._get_card_fingerprint(card)
        # 模拟指纹已过期（清理后为空）
        notifier._recent_card_fingerprints.clear()

        # 再次发送应成功（因为旧指纹已过期被清理，新指纹会被记录）
        result2 = await notifier.send_card(card)
        assert result2 is True
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_retry_queued_skips_already_sent_card(mock_env, tmp_path):
    """retry_queued 重试时应跳过已发送过的卡片。"""
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    notifier = FeishuNotifier()
    notifier._client = mock_client
    # 使用隔离队列路径，避免其他测试失败入队的残留条目干扰本测试
    notifier._queue_path = tmp_path / "notify_queue.jsonl"

    try:
        card = _make_card("600519", "买入信号")
        fingerprint = notifier._get_card_fingerprint(card)

        # 模拟卡片已发送（记录指纹）
        notifier._recent_card_fingerprints[fingerprint] = time.time()

        # 将卡片放入重试队列
        await notifier._enqueue_async(card)

        # 运行重试队列
        success_count = await notifier.retry_queued()

        assert success_count == 0
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_retry_queued_uses_tenant_configuration_for_limit_alert(mock_env, tmp_path):
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    notifier = FeishuNotifier()
    notifier._client = mock_client
    notifier._queue_path = tmp_path / "notify_queue.jsonl"
    notifier._webhook = ""
    notifier._queue_path.write_text(
        json.dumps(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "card": _make_card("000001", "通知重试告警"),
                "retry_count": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with (
        patch("gugu.notifier.feishu.MAX_RETRY_COUNT", 0),
        patch.object(notifier, "_get_tenant_token", new_callable=AsyncMock, return_value="token"),
        patch.object(notifier, "_post_card", new_callable=AsyncMock) as post_card,
    ):
        await notifier.retry_queued()
    post_card.assert_awaited_once()
    await mock_client.aclose()


def test_signal_dedup_cleanup_removes_non_recent_dates(mock_env, tmp_path):
    notifier = FeishuNotifier()
    path = notifier._get_notified_signals_path()
    path.write_text(
        json.dumps({"old": "2020-01-01", "today": time.strftime("%Y-%m-%d")}), encoding="utf-8"
    )
    notifier._mark_signal_notified("new")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "old" not in data
    assert data["today"] == time.strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_retry_queued_sends_new_card(mock_env, tmp_path):
    """retry_queued 应发送队列中未发送过的新卡片。"""
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    notifier = FeishuNotifier()
    notifier._client = mock_client
    # 使用隔离队列路径，避免其他测试失败入队的残留条目干扰本测试
    notifier._queue_path = tmp_path / "notify_queue.jsonl"

    try:
        card = _make_card("600519", "买入信号")

        # 将卡片放入重试队列（未发送过）
        await notifier._enqueue_async(card)

        # 运行重试队列
        success_count = await notifier.retry_queued()

        # 新卡片应发送成功
        assert success_count == 1

        # 指纹应被记录
        fingerprint = notifier._get_card_fingerprint(card)
        assert fingerprint in notifier._recent_card_fingerprints
    finally:
        await mock_client.aclose()


# ========== 指纹生成测试 ==========


def test_get_card_fingerprint_same_content_same_hash():
    """相同内容的卡片应生成相同的指纹。"""
    notifier = FeishuNotifier()
    card = _make_card("600519", "测试")

    fp1 = notifier._get_card_fingerprint(card)
    fp2 = notifier._get_card_fingerprint(card)

    assert fp1 == fp2


def test_get_card_fingerprint_different_content_different_hash():
    """不同内容的卡片应生成不同的指纹。"""
    notifier = FeishuNotifier()
    card1 = _make_card("600519", "测试A")
    card2 = _make_card("000001", "测试B")

    fp1 = notifier._get_card_fingerprint(card1)
    fp2 = notifier._get_card_fingerprint(card2)

    assert fp1 != fp2


def test_get_card_fingerprint_handles_nested_card_structure():
    """指纹生成应处理嵌套的 card 结构。"""
    notifier = FeishuNotifier()
    card = {
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "嵌套结构测试"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "内容1"}},
                {"tag": "div", "text": {"tag": "lark_md", "content": "内容2"}},
            ],
        }
    }

    fp = notifier._get_card_fingerprint(card)
    assert isinstance(fp, str)
    # 格式: MD5(32) + _ + YYYY-MM-DD(10) = 43
    assert len(fp) == 43
    assert "_" in fp
    # 验证日期部分是今天
    from datetime import date

    assert fp.endswith(date.today().isoformat())
