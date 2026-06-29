"""Feishu notifier: async HTTP client for sending card messages to Feishu group chat.

Gracefully degrades when env config is missing: logs warning and skips send.
Never raises to caller to avoid blocking the main trading flow.

失败重试队列：信号通知失败时持久化到 data/notify_queue.jsonl，定期重试。
确保买卖信号不因飞书临时故障而丢失。
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from gugu.config import PROJECT_ROOT, env
from gugu.notifier.formatter import (
    format_backtest_report,
    format_daily_report,
    format_holdings_sell_alert,
    format_risk_alert,
    format_signal,
    format_system_error,
)
from gugu.notifier.fund_monitor import build_fund_monitor_card
from gugu.screener.report import format_screener_report
from gugu.utils.log import get_logger

logger = get_logger()

FEISHU_BASE = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
MESSAGE_URL = f"{FEISHU_BASE}/im/v1/messages"
# Feishu tenant_access_token is valid for 2 hours; refresh slightly earlier.
TOKEN_TTL_SECONDS = 2 * 60 * 60
# 重试队列最大重试次数
MAX_RETRY_COUNT = 5


class FeishuNotifier:
    """Feishu group chat notifier with async HTTP and tenacity retry.

    Reads credentials from env() (feishu_app_id, feishu_app_secret,
    feishu_chat_id, feishu_webhook). When required credentials are missing,
    all send operations log a warning and return False without raising.
    """

    def __init__(self) -> None:
        cfg = env()
        self._app_id: str = cfg.feishu_app_id
        self._app_secret: str = cfg.feishu_app_secret
        self._chat_id: str = cfg.feishu_chat_id
        self._webhook: str = cfg.feishu_webhook

        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=10.0)
        self._queue_path: Path = PROJECT_ROOT / "data" / "notify_queue.jsonl"

        if not self._is_configured():
            logger.warning(
                "Feishu notifier not configured "
                "(missing app_id/app_secret/chat_id). "
                "All notifications will be skipped."
            )

    def __repr__(self) -> str:
        """屏蔽敏感凭证，防止日志/调试意外泄露 token（S-01 修复）。"""
        configured = self._is_configured()
        token_state = "set" if self._token else "empty"
        return f"FeishuNotifier(configured={configured}, token={token_state})"

    def _is_configured(self) -> bool:
        """Check if required Feishu credentials are present."""
        return bool(self._app_id and self._app_secret and self._chat_id)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, RuntimeError)),
        reraise=True,
    )
    async def _get_tenant_token(self) -> str:
        """Get cached tenant_access_token, refresh if expired.

        Token is cached for TOKEN_TTL_SECONDS. Retries on network/API errors.
        """
        if self._token and time.time() < self._token_expires_at:
            return self._token

        body = {"app_id": self._app_id, "app_secret": self._app_secret}
        resp = await self._client.post(TOKEN_URL, json=body)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"Feishu token error: {data.get('msg')}")

        self._token = data["tenant_access_token"]
        self._token_expires_at = time.time() + TOKEN_TTL_SECONDS
        logger.debug("Feishu tenant_access_token refreshed")
        return self._token

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, RuntimeError)),
        reraise=True,
    )
    async def _post_card(self, token: str, card: dict[str, Any]) -> None:
        """Post interactive card to Feishu chat. Raises on failure (will be retried)."""
        # Formatter returns {"msg_type": "interactive", "card": {...}}; extract card.
        card_payload = card.get("card", card)
        body = {
            "receive_id": self._chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card_payload, ensure_ascii=False),
        }
        headers = {"Authorization": f"Bearer {token}"}

        resp = await self._client.post(
            MESSAGE_URL,
            params={"receive_id_type": "chat_id"},
            json=body,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu API error: {data.get('msg')}")

    async def send_card(self, card: dict[str, Any]) -> bool:
        """Send an interactive card to the configured chat.

        Returns True on success, False on failure or when not configured.
        失败时自动入队，等待后续重试。Never raises to caller.
        """
        if not self._is_configured():
            logger.warning("Feishu not configured, skip card send")
            return False

        try:
            token = await self._get_tenant_token()
            await self._post_card(token, card)
            return True
        except Exception as e:
            logger.error(f"Feishu send_card failed: {e}")
            # 入队等待重试
            self._enqueue(card)
            return False

    def _enqueue(self, card: dict[str, Any]) -> None:
        """失败通知入队，持久化到 data/notify_queue.jsonl。"""
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "card": card,
                "retry_count": 0,
            }
            with self._queue_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            logger.info(f"通知入队等待重试: {self._queue_path}")
        except Exception as e:
            logger.error(f"通知入队失败: {e}")

    async def retry_queued(self) -> int:
        """重试队列中的失败通知。

        Returns:
            成功重试的通知数
        """
        if not self._queue_path.exists():
            return 0

        # 读取所有待重试记录
        pending: list[dict[str, Any]] = []
        try:
            with self._queue_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pending.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.error(f"读取重试队列失败: {e}")
            return 0

        if not pending:
            return 0

        # 清空队列文件（重试成功的移除，失败的重新写入）
        self._queue_path.write_text("", encoding="utf-8")

        success_count = 0
        still_failed: list[dict[str, Any]] = []

        for item in pending:
            retry_count = item.get("retry_count", 0)
            if retry_count >= MAX_RETRY_COUNT:
                logger.warning(
                    f"通知重试次数超限({retry_count}), 丢弃: "
                    f"{json.dumps(item.get('card', {}), ensure_ascii=False)[:200]}"
                )
                continue

            card = item.get("card", {})
            try:
                token = await self._get_tenant_token()
                await self._post_card(token, card)
                success_count += 1
                logger.info(f"重试通知成功: {success_count}")
            except Exception as e:
                logger.debug(f"重试通知失败: {e}")
                item["retry_count"] = retry_count + 1
                still_failed.append(item)

        # 重新写入仍失败的通知
        if still_failed:
            with self._queue_path.open("a", encoding="utf-8") as f:
                for item in still_failed:
                    f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")

        if success_count > 0:
            logger.info(f"重试队列: {success_count} 成功, {len(still_failed)} 仍失败")
        return success_count

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, RuntimeError)),
        reraise=True,
    )
    async def _post_webhook(self, text: str) -> None:
        """Post text to webhook. Raises on failure (will be retried)."""
        body = {"msg_type": "text", "content": {"text": text}}
        resp = await self._client.post(self._webhook, json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu webhook error: {data.get('msg')}")

    async def send_text(self, text: str) -> bool:
        """Send plain text via webhook (fallback).

        Returns True on success, False on failure or when webhook not configured.
        Never raises to caller.
        """
        if not self._webhook:
            logger.warning("Feishu webhook not configured, skip text send")
            return False

        try:
            await self._post_webhook(text)
            return True
        except Exception as e:
            logger.error(f"Feishu send_text failed: {e}")
            return False

    async def notify_signal(self, signal: dict[str, Any]) -> bool:
        """Format and send a trade signal notification."""
        return await self.send_card(format_signal(signal))

    async def notify_daily_report(self, period: str, data: dict[str, Any]) -> bool:
        """Format and send a daily report notification."""
        return await self.send_card(format_daily_report(period, data))

    async def notify_risk_alert(self, alert: dict[str, Any]) -> bool:
        """Format and send a risk alert notification."""
        return await self.send_card(format_risk_alert(alert))

    async def notify_backtest(self, report: dict[str, Any]) -> bool:
        """Format and send a backtest report notification."""
        return await self.send_card(format_backtest_report(report))

    async def notify_screener(self, results: list, total_scanned: int) -> bool:
        """发送尾盘选股结果。"""
        card = format_screener_report(results, total_scanned)
        return await self.send_card(card)

    async def notify_error(self, error: dict[str, Any]) -> bool:
        """Format and send a system error notification."""
        return await self.send_card(format_system_error(error))

    async def notify_fund_monitor(self, result: dict[str, Any]) -> bool:
        """Format and send a fund monitor report."""
        return await self.send_card(build_fund_monitor_card(result))

    async def notify_flow_report(self, period: str, data: dict[str, Any]) -> bool:
        """Format and send a capital flow report.

        Args:
            period: "morning" (盘前复盘) or "close" (收盘日报).
            data: Result from run_morning_report() or run_close_report().
        """
        card = data["card"]
        return await self.send_card(card)

    async def notify_holdings_sell_alert(self, stocks: list[dict]) -> bool:
        """Format and send a holdings sell signal alert."""
        return await self.send_card(format_holdings_sell_alert(stocks))

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        with contextlib.suppress(RuntimeError):
            # Event loop may already be closed; safe to ignore
            await self._client.aclose()
