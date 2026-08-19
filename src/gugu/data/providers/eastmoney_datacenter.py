"""东方财富数据中心 Provider。

龙虎榜、解禁、两融、大宗交易等另类数据共用该入口。
业务层只接收记录列表，字段映射和单位转换由具体 Collector 负责。
"""

from __future__ import annotations

from typing import Any

import requests

from gugu.data.providers.http_transport import HttpTransport, TransportConfig

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}


class EastmoneyDataCenter:
    """东财数据中心的共享、限流、可替换访问入口。"""

    def __init__(self, transport: HttpTransport | None = None) -> None:
        if transport is None:
            session = requests.Session()
            session.headers.update(_DEFAULT_HEADERS)
            transport = HttpTransport(
                "eastmoney-datacenter",
                config=TransportConfig(
                    min_interval_seconds=1.0,
                    max_retries=2,
                    backoff_base_seconds=1.0,
                    backoff_max_seconds=8.0,
                    timeout_seconds=15.0,
                    jitter_seconds=0.5,
                ),
                session=session,
            )
        self.transport = transport

    def query(
        self,
        report_name: str,
        *,
        columns: str = "ALL",
        filter_str: str = "",
        page_size: int = 50,
        sort_columns: str = "",
        sort_types: str = "-1",
    ) -> list[dict[str, Any]]:
        """查询数据中心记录，空结果表示上游无记录。"""
        if not report_name.strip():
            raise ValueError("report_name 不能为空")
        if page_size <= 0 or page_size > 5000:
            raise ValueError("page_size 必须在 1 到 5000 之间")
        params = {
            "reportName": report_name,
            "columns": columns,
            "filter": filter_str,
            "pageNumber": "1",
            "pageSize": str(page_size),
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
            "source": "WEB",
            "client": "WEB",
        }
        payload = self.transport.get_json(DATACENTER_URL, params=params)
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        data = result.get("data")
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]


_default_datacenter: EastmoneyDataCenter | None = None


def eastmoney_datacenter() -> EastmoneyDataCenter:
    """获取进程级共享数据中心 Provider。"""
    global _default_datacenter
    if _default_datacenter is None:
        _default_datacenter = EastmoneyDataCenter()
    return _default_datacenter
