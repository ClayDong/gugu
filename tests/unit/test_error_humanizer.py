"""错误消息友好化测试。"""
from __future__ import annotations

from gugu.notifier.feishu import _humanize_error_message


class TestHumanizeErrorMessage:
    """_humanize_error_message 函数测试。"""

    def test_empty_message_returns_empty(self) -> None:
        assert _humanize_error_message("") == ""

    def test_connection_error_translated(self) -> None:
        result = _humanize_error_message("ConnectionError to sina API")
        assert "数据源连接失败" in result
        assert "自动重试" in result

    def test_http_500_translated(self) -> None:
        result = _humanize_error_message("HTTP 500: Internal Server Error")
        assert "数据源服务器错误" in result

    def test_http_502_translated(self) -> None:
        result = _humanize_error_message("HTTP 502 Bad Gateway")
        assert "数据源网关错误" in result

    def test_http_503_translated(self) -> None:
        result = _humanize_error_message("HTTP 503 Service Unavailable")
        assert "服务暂不可用" in result

    def test_http_429_translated(self) -> None:
        result = _humanize_error_message("HTTP 429 Too Many Requests")
        assert "请求过于频繁" in result

    def test_timeout_translated(self) -> None:
        result = _humanize_error_message("Timeout connecting to eastmoney")
        assert "请求超时" in result

    def test_unknown_error_unchanged(self) -> None:
        msg = "Some unknown error that has no matching pattern"
        result = _humanize_error_message(msg)
        assert result == msg

    def test_memory_error_translated(self) -> None:
        result = _humanize_error_message("OutOfMemoryError")
        assert "内存不足" in result