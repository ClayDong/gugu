"""Feishu notifier: async HTTP client for sending card messages to Feishu group chat.

Gracefully degrades when env config is missing: logs warning and skips send.
Never raises to caller to avoid blocking the main trading flow.

失败重试队列：信号通知失败时持久化到 data/notify_queue.jsonl，定期重试。
确保买卖信号不因飞书临时故障而丢失。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
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
# 重试队列条目最大存活时长（小时）：超过则视为过期信号自动丢弃。
# 6 小时覆盖单个交易日主要时段，避免昨日信号在今天重发（误导用户）。
MAX_QUEUE_AGE_HOURS = 6


class FeishuNotifier:
    """Feishu group chat notifier with async HTTP and tenacity retry.

    Reads credentials from env() (feishu_app_id, feishu_app_secret,
    feishu_chat_id, feishu_webhook). When required credentials are missing,
    all send operations log a warning and return False without raising.
    """

    def __init__(self) -> None:
        from gugu.config import get_config
        cfg = env()
        self._app_id: str = cfg.feishu_app_id
        self._app_secret: str = cfg.feishu_app_secret
        self._chat_id: str = cfg.feishu_chat_id
        self._webhook: str = cfg.feishu_webhook

        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=10.0)
        # P1-4 修复：重试队列并发安全锁，保护 _enqueue 和 retry_queued 的文件操作
        self._queue_lock: asyncio.Lock = asyncio.Lock()

        # 测试环境保护：通过显式环境变量将重试队列重定向到临时目录
        # 防止测试中的失败通知污染生产数据文件 data/notify_queue.jsonl
        # 不再使用 "pytest" in sys.modules 检测（避免生产代码依赖测试框架）
        import os
        if os.environ.get("GUGU_TEST_MODE"):
            import tempfile
            self._queue_path: Path = Path(tempfile.gettempdir()) / "gugu_test_notify_queue.jsonl"
        else:
            self._queue_path: Path = PROJECT_ROOT / "data" / "notify_queue.jsonl"

        # 读取通知开关配置
        feishu_cfg = get_config().feishu
        self._enabled: bool = feishu_cfg.enabled
        self._signal_enabled: bool = feishu_cfg.notify_signal
        self._daily_report_enabled: bool = feishu_cfg.notify_daily_report
        self._risk_alert_enabled: bool = feishu_cfg.notify_risk_alert
        self._backtest_enabled: bool = feishu_cfg.notify_backtest
        self._system_error_enabled: bool = feishu_cfg.notify_system_error

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
        if not self._enabled:
            logger.debug("Feishu notifications disabled by config")
            return False
        if not self._is_configured():
            logger.warning("Feishu not configured, skip card send")
            return False

        try:
            token = await self._get_tenant_token()
            await self._post_card(token, card)
            return True
        except Exception as e:
            logger.error(f"Feishu send_card failed: {e}")
            # 入队等待重试（P1-4: 使用异步加锁版本，避免与 retry_queued 并发写文件）
            await self._enqueue_async(card)
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

    async def _enqueue_async(self, card: dict[str, Any]) -> None:
        """异步入队（线程安全，与 retry_queued 互斥）。"""
        async with self._queue_lock:
            self._enqueue(card)

    async def retry_queued(self) -> int:
        """重试队列中的失败通知（per-item 追加模式，防丢失）。

        每次只读一条，重试成功则从队列删除（重写剩余队列），
        避免一次性读全部后进程崩溃导致丢失。

        P1-4 修复：分两段加锁——读取和写回分别在 _queue_lock 内，
        发送在锁外（避免长时间持锁阻塞 _enqueue_async）。
        写回时合并发送期间新入队的条目，防止覆盖丢失。

        Returns:
            成功重试的通知数
        """
        # P1-4: 第一段锁——读取 pending 列表
        async with self._queue_lock:
            if not self._queue_path.exists():
                return 0

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
            # 清空队列文件，让发送期间的 _enqueue_async 从空文件追加
            self._queue_path.write_text("", encoding="utf-8")

        # 第二段：锁外逐个重试发送（不阻塞新通知入队）
        success_count = 0
        still_failed: list[dict[str, Any]] = []

        for item in pending:
            retry_count = item.get("retry_count", 0)
            # 过期清理：超过 MAX_QUEUE_AGE_HOURS 的条目直接丢弃
            # （昨日信号今日已无意义，重发只会误导用户）
            item_ts = item.get("timestamp", "")
            if self._is_expired(item_ts):
                logger.warning(
                    f"通知队列条目过期(>{MAX_QUEUE_AGE_HOURS}h)，自动清理: "
                    f"timestamp={item_ts}"
                )
                continue
            if retry_count >= MAX_RETRY_COUNT:
                logger.error(
                    f"通知重试次数超限({retry_count}), 永久丢弃: "
                    f"{json.dumps(item.get('card', {}), ensure_ascii=False)[:200]}"
                )
                # 尝试通过飞书 webhook 发送一条告警（有 webhook 时）
                if self._webhook:
                    try:
                        token = await self._get_tenant_token()
                        alert_card = {
                            "msg_type": "interactive",
                            "card": {
                                "header": {"title": {"tag": "plain_text", "content": "⚠️ 通知重试超限告警"}, "template": "red"},
                                "elements": [
                                    {"tag": "div", "text": {"tag": "lark_md", "content": f"通知已重试 {retry_count} 次仍未成功，已永久丢弃。\n卡片摘要: {json.dumps(item.get('card', {}), ensure_ascii=False)[:200]}"}},
                                ],
                            },
                        }
                        await self._post_card(token, alert_card)
                    except Exception:
                        pass
                continue

            card = item.get("card", {})

            # 脏数据拦截：检查卡片中是否包含明显的测试/异常价格
            # 重试队列可能残留旧测试数据，重试前必须校验
            if self._is_stale_test_card(card):
                logger.error(
                    f"重试队列发现脏数据（测试价格或过期数据），永久丢弃: "
                    f"{json.dumps(card, ensure_ascii=False)[:200]}"
                )
                continue

            try:
                token = await self._get_tenant_token()
                await self._post_card(token, card)
                success_count += 1
                logger.info(f"重试通知成功: {success_count}")
            except Exception as e:
                logger.debug(f"重试通知失败: {e}")
                item["retry_count"] = retry_count + 1
                still_failed.append(item)

        # P1-4: 第三段锁——合并写回 still_failed（保留发送期间新入队的条目）
        if still_failed:
            async with self._queue_lock:
                # 读取发送期间 _enqueue_async 新追加的条目
                newly_queued: list[dict[str, Any]] = []
                if self._queue_path.exists():
                    try:
                        with self._queue_path.open("r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    newly_queued.append(json.loads(line))
                                except json.JSONDecodeError:
                                    continue
                    except Exception:
                        pass
                # 合并：新入队的在前，仍失败的在后
                all_items = newly_queued + still_failed
                tmp_path = self._queue_path.with_suffix(".jsonl.tmp")
                try:
                    with tmp_path.open("w", encoding="utf-8") as f:
                        for item in all_items:
                            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                    tmp_path.rename(self._queue_path)
                except Exception as e:
                    logger.error(f"写入重试队列失败: {e}")
                    with self._queue_path.open("w", encoding="utf-8") as f:
                        for item in all_items:
                            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        else:
            # 全部成功，清空队列（但保留发送期间新入队的条目）
            async with self._queue_lock:
                # 检查是否有新条目入队
                has_new = self._queue_path.exists() and self._queue_path.stat().st_size > 0
                if not has_new:
                    self._queue_path.write_text("", encoding="utf-8")

        if success_count > 0:
            logger.info(f"重试队列: {success_count} 成功, {len(still_failed)} 仍失败")
        return success_count

    @staticmethod
    def _is_expired(timestamp_str: str) -> bool:
        """判断队列条目是否已过期（超过 MAX_QUEUE_AGE_HOURS）。

        Args:
            timestamp_str: ISO 格式时间戳 ``%Y-%m-%dT%H:%M:%S``。

        Returns:
            True 表示已过期应清理；时间戳为空或解析失败时返回 False（保守不清理）。
        """
        if not timestamp_str:
            return False
        try:
            from datetime import datetime, timedelta

            ts = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S")
            return datetime.now() - ts > timedelta(hours=MAX_QUEUE_AGE_HOURS)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _is_stale_test_card(card: dict[str, Any]) -> bool:
        """检测重试队列中的脏数据卡片。

        判定规则：
        1. 提取卡片标题中的股票名称和价格
        2. 已知测试mock价格直接拦截（如茅台1500.0）
        3. 卡片内容包含明显的测试标记（如"测试"、"mock"、"test"）
        """
        try:
            # 提取卡片标题
            header = card.get("card", {}).get("header", {})
            title = ""
            if "title" in header:
                title = header["title"].get("content", "")

            # 提取卡片内容
            elements = card.get("card", {}).get("elements", [])
            content_text = ""
            for el in elements:
                if isinstance(el, dict) and "text" in el:
                    content_text += el["text"].get("content", "") + " "

            combined = (title + " " + content_text).lower()

            # 规则1：明确的测试标记
            test_markers = ["【测试】", "【mock】", "test_signal", "mock_signal"]
            for marker in test_markers:
                if marker.lower() in combined:
                    return True

            # 规则2：已知测试mock价格
            # 茅台真实价格约1000-2000区间，但1500.0是测试用的固定mock值
            # 通过正则提取价格，检查是否为已知的mock价格
            import re
            price_match = re.search(r"[￥¥]([0-9]+\.?[0-9]*)", combined)
            if price_match:
                price = float(price_match.group(1))
                # 茅台mock价格1500.0（与真实价格差异大时需更新）
                # 这里用更通用的方式：检查卡片是否包含"贵州茅台"且价格为1500.0
                if "贵州茅台" in content_text and abs(price - 1500.0) < 0.01:
                    return True
                if "600519" in content_text and abs(price - 1500.0) < 0.01:
                    return True

            # 规则3：价格为0或负数
            if price_match:
                price = float(price_match.group(1))
                if price <= 0:
                    return True

            return False
        except Exception:
            return False

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
        if not self._signal_enabled:
            return False
        # 对抗性防御：核心字段缺失时记录警告并修复 signal 完整性标记
        required_fields = ["symbol", "strategy", "strategies", "reason", "price"]
        missing = [f for f in required_fields if f not in signal or signal.get(f) is None]
        if missing:
            logger.warning(
                f"信号数据不完整，缺失字段: {missing}。"
                f" symbol={signal.get('symbol', 'N/A')} "
                f"direction={signal.get('direction', 'N/A')}"
            )
            # 补充默认值防止 formatter 崩溃，但保留缺失标记供 formatter 检测使用
            signal.setdefault("strategy", "")
            signal.setdefault("strategies", [])
            signal.setdefault("reason", "")
            signal.setdefault("price", 0.0)

        # 硬校验关键字段合法性：拒绝异常价格/置信度/空策略，避免脏数据发送到飞书
        price = signal.get("price", 0.0)
        confidence = signal.get("confidence")
        strategies = signal.get("strategies", [])
        try:
            price = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        if price <= 1.0:
            logger.error(
                f"信号价格异常: price={price} <= 1.0，拒绝发送。"
                f" symbol={signal.get('symbol', 'N/A')}"
            )
            return False
        if confidence is None or (isinstance(confidence, (int, float)) and confidence <= 0):
            logger.error(
                f"信号置信度异常: confidence={confidence}，拒绝发送。"
                f" symbol={signal.get('symbol', 'N/A')}"
            )
            return False
        if not strategies or (isinstance(strategies, list) and len(strategies) == 0):
            logger.error(
                f"信号策略为空: strategies={strategies}，拒绝发送。"
                f" symbol={signal.get('symbol', 'N/A')}"
            )
            return False

        return await self.send_card(format_signal(signal))

    async def notify_daily_report(self, period: str, data: dict[str, Any]) -> bool:
        """Format and send a daily report notification."""
        if not self._daily_report_enabled:
            return False
        return await self.send_card(format_daily_report(period, data))

    async def notify_risk_alert(self, alert: dict[str, Any]) -> bool:
        """Format and send a risk alert notification."""
        if not self._risk_alert_enabled:
            return False
        return await self.send_card(format_risk_alert(alert))

    async def notify_backtest(self, report: dict[str, Any]) -> bool:
        """Format and send a backtest report notification."""
        if not self._backtest_enabled:
            return False
        return await self.send_card(format_backtest_report(report))

    async def notify_screener(self, results: list, total_scanned: int) -> bool:
        """发送尾盘选股结果。"""
        if not self._signal_enabled:
            return False
        card = format_screener_report(results, total_scanned)
        return await self.send_card(card)

    async def notify_error(self, error: dict[str, Any]) -> bool:
        """Format and send a system error notification.

        P0 安全修复：自动对 error message 做密钥脱敏，
        防止异常信息中泄露 app_secret / api_key / token / password。
        """
        if not self._system_error_enabled:
            return False
        msg = error.get("message", "")
        # P0 脱敏：替换常见密钥模式
        for pattern in (
            r"(app_secret[\"\'\s]*[:=]\s*[\"\']?)[^\"\'\s,}]+",
            r"(api_key[\"\'\s]*[:=]\s*[\"\']?)[^\"\'\s,}]+",
            r"(api-key[\"\'\s]*[:=]\s*[\"\']?)[^\"\'\s,}]+",
            r"(Bearer\s+)[A-Za-z0-9._\-]{16,}",
        ):
            msg = re.sub(pattern, r"\1***REDACTED***", msg, flags=re.IGNORECASE)
        error = dict(error)
        error["message"] = msg
        return await self.send_card(format_system_error(error))

    async def notify_fund_monitor(self, result: dict[str, Any]) -> bool:
        """Format and send a fund monitor report."""
        return await self.send_card(build_fund_monitor_card(result))

    async def notify_flow_report(self, period: str, data: dict[str, Any]) -> bool:
        """Format and send a capital flow report.

        Args:
            period: "morning" (盘前复盘) / "afternoon" (14:30决策报告) / "close" (收盘日报).
            data: Result from run_morning_report() / run_afternoon_report() / run_close_report().
        """
        card = data["card"]
        return await self.send_card(card)

    async def notify_holdings_sell_alert(self, stocks: list[dict]) -> bool:
        """Format and send a holdings sell signal alert."""
        return await self.send_card(format_holdings_sell_alert(stocks))

    async def notify_take_profit_alert(
        self,
        symbol: str,
        name: str,
        signal: dict[str, Any],
        state: dict[str, Any] | None = None,
    ) -> bool:
        """发送财叔动态止盈减仓告警卡片。

        Args:
            symbol: 股票代码。
            name: 股票名称。
            signal: TakeProfitSignal 的 dict 形式。
            state: 可选的 TakeProfitState dict，用于展示减仓历史。
        """
        from gugu.notifier.formatter import format_take_profit_alert

        return await self.send_card(
            format_take_profit_alert(symbol, name, signal, state)
        )

    async def notify_strategy_dashboard(self) -> bool:
        """发送策略注册中心全景看板卡片。"""
        from gugu.strategies.manifest import get_manifest

        manifest = get_manifest()
        dashboard = manifest.format_dashboard()

        card = {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "📋 策略注册中心 · 全景看板",
                },
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": dashboard}},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "策略注册中心 · gugu · 策略治理",
                        }
                    ],
                },
            ],
        }
        return await self.send_card(card)


    async def notify_conflict(self, symbol: str, name: str, resolution: dict[str, Any]) -> bool:
        """发送多视角冲突研讨卡片。"""
        from gugu.notifier.formatter import format_conflict_card

        return await self.send_card(format_conflict_card(symbol, name, resolution))

    async def notify_concept(
        self, concept_name: str, definition: str, explanation: str, citations: list[dict[str, str]]
    ) -> bool:
        """发送每日概念卡（小白学堂）。"""
        from gugu.notifier.formatter import format_concept_card

        return await self.send_card(
            format_concept_card(concept_name, definition, explanation, citations)
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        with contextlib.suppress(RuntimeError):
            # Event loop may already be closed; safe to ignore
            await self._client.aclose()
