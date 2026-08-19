"""共享的 HTTP GET 传输层。

该模块只负责传输治理，不负责业务字段解析：
- 复用 requests.Session；
- 对幂等 GET 做有限重试；
- 按 provider 维度做最小请求间隔；
- 处理 HTTP/网络/JSON 层错误；
- 不记录可能包含敏感参数的完整 URL。

业务采集器应在拿到 payload 后自行做字段、单位、日期和质量校验。
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from gugu.utils.log import get_logger

logger = get_logger()


class TransportError(RuntimeError):
    """共享传输层无法获得有效响应。"""


@dataclass(frozen=True)
class TransportConfig:
    """单个 provider 的请求治理参数。"""

    min_interval_seconds: float = 1.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    timeout_seconds: float = 15.0
    jitter_seconds: float = 0.2


class HttpTransport:
    """可复用、可测试的 GET JSON 传输客户端。"""

    def __init__(
        self,
        provider: str,
        *,
        config: TransportConfig | None = None,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        if not provider.strip():
            raise ValueError("provider 不能为空")
        self.provider = provider
        self.config = config or TransportConfig()
        if self.config.max_retries < 0:
            raise ValueError("max_retries 不能为负数")
        if self.config.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds 不能为负数")
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.clock = clock
        self.random_fn = random_fn
        self._last_request_at: float | None = None
        self._rate_lock = threading.Lock()

    def _wait_for_rate_limit(self) -> None:
        """在 provider 级别串行控制请求间隔。"""
        with self._rate_lock:
            now = self.clock()
            if self._last_request_at is not None:
                wait_seconds = self.config.min_interval_seconds - (now - self._last_request_at)
                if wait_seconds > 0:
                    self.sleeper(wait_seconds)
            self._last_request_at = self.clock()

    def _retry_delay(self, attempt: int) -> float:
        base = min(
            self.config.backoff_max_seconds,
            self.config.backoff_base_seconds * (2**attempt),
        )
        return float(base + self.random_fn() * self.config.jitter_seconds)

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET JSON 对象；失败后抛出 TransportError。"""
        if not url.startswith(("http://", "https://")):
            raise ValueError("url 必须是 HTTP(S) 地址")

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._wait_for_rate_limit()
            try:
                response = self.session.get(url, params=params, timeout=self.config.timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TransportError("响应 JSON 不是对象")
                return payload
            except (requests.RequestException, ValueError, TransportError) as exc:
                last_error = exc
                retryable = isinstance(exc, (requests.RequestException, ValueError))
                if not retryable or attempt >= self.config.max_retries:
                    break
                delay = self._retry_delay(attempt)
                logger.warning(
                    "{} GET 失败，第 {}/{} 次重试，等待 {:.2f}s：{}",
                    self.provider,
                    attempt + 1,
                    self.config.max_retries,
                    delay,
                    type(exc).__name__,
                )
                self.sleeper(delay)

        raise TransportError(
            f"{self.provider} GET 请求失败: {type(last_error).__name__}: {last_error}"
        )

    def close(self) -> None:
        """释放底层 Session。"""
        self.session.close()
