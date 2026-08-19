"""共享 HTTP 传输层测试。"""

from __future__ import annotations

import requests
import pytest

from gugu.data.providers import HttpTransport, TransportConfig, TransportError


class FakeResponse:
    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


def test_get_json_success_and_reuses_session():
    session = FakeSession([FakeResponse({"data": {"ok": True}})])
    transport = HttpTransport(
        "test",
        session=session,
        config=TransportConfig(min_interval_seconds=0, max_retries=0),
    )

    assert transport.get_json("https://example.test/api", params={"a": 1}) == {"data": {"ok": True}}
    assert len(session.calls) == 1
    assert session.calls[0][1]["timeout"] == 15.0


def test_get_json_retries_request_failure():
    session = FakeSession(
        [
            FakeResponse(error=requests.ConnectionError("down")),
            FakeResponse({"data": "ok"}),
        ]
    )
    sleeps: list[float] = []
    transport = HttpTransport(
        "test",
        session=session,
        sleeper=sleeps.append,
        random_fn=lambda: 0.0,
        config=TransportConfig(
            min_interval_seconds=0,
            max_retries=1,
            backoff_base_seconds=0.25,
            jitter_seconds=0,
        ),
    )

    assert transport.get_json("https://example.test/api") == {"data": "ok"}
    assert len(session.calls) == 2
    assert sleeps == [0.25]


def test_get_json_rejects_non_object_payload():
    session = FakeSession([FakeResponse([1, 2, 3])])
    transport = HttpTransport(
        "test",
        session=session,
        config=TransportConfig(min_interval_seconds=0, max_retries=0),
    )

    with pytest.raises(TransportError, match="不是对象"):
        transport.get_json("https://example.test/api")


def test_rate_limit_waits_between_calls():
    session = FakeSession([FakeResponse({"ok": 1}), FakeResponse({"ok": 2})])
    now = [0.0]
    sleeps: list[float] = []

    def clock():
        return now[0]

    def sleeper(delay):
        sleeps.append(delay)
        now[0] += delay

    transport = HttpTransport(
        "test",
        session=session,
        clock=clock,
        sleeper=sleeper,
        config=TransportConfig(min_interval_seconds=1.0, max_retries=0),
    )
    transport.get_json("https://example.test/api")
    transport.get_json("https://example.test/api")

    assert sleeps == [1.0]
