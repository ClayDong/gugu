"""外部资源假设收件箱。

网络文章、书籍、视频只能生成待验证假设，不能直接生成可交易策略或
修改 verified_strategies。无法读取的来源也保留审计记录。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from gugu import config as gugu_config
from gugu.utils.atomic import append_jsonl

STATUS_VALUES = {"inbox", "source_unavailable", "researching", "validated", "rejected"}


def hypotheses_path() -> Path:
    return gugu_config.PROJECT_ROOT / "data" / "research_hypotheses.jsonl"


def record_hypothesis(
    *,
    source: str,
    title: str = "",
    author: str = "",
    text: str = "",
    status: str = "inbox",
    extracted_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record an external claim without granting strategy admission."""
    if status not in STATUS_VALUES:
        raise ValueError(f"invalid hypothesis status: {status}")
    record = {
        "hypothesis_id": uuid.uuid4().hex,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "title": title,
        "author": author,
        "text": text[:4000],
        "status": status,
        "extracted_rules": extracted_rules or [],
        "strategy_admission": "never_automatic",
    }
    append_jsonl(hypotheses_path(), record)
    return record


def record_source_unavailable(source: str, reason: str) -> dict[str, Any]:
    return record_hypothesis(source=source, status="source_unavailable", text=reason)
