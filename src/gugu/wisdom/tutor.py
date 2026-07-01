"""小白学堂：每日概念推送 + 学习进度追踪。

从 concepts.json 50个核心概念中按天轮换，
用 KB Agent 检索原书解释，生成教育卡片。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from gugu.config import PROJECT_ROOT
from gugu.utils.log import get_logger

logger = get_logger()

_CONCEPTS_FILE = Path(__file__).parent / "concepts.json"
_PROGRESS_FILE = PROJECT_ROOT / "data" / "concept_progress.json"

_concepts_cache: list[dict] | None = None


def _load_concepts() -> list[dict]:
    """加载概念库（带缓存）。"""
    global _concepts_cache
    if _concepts_cache is not None:
        return _concepts_cache
    try:
        _concepts_cache = json.loads(_CONCEPTS_FILE.read_text(encoding="utf-8"))
        return _concepts_cache
    except Exception as e:
        logger.exception(f"[tutor] 加载概念库失败: {e}")
        return []


def _load_progress() -> dict:
    """加载学习进度。"""
    if not _PROGRESS_FILE.exists():
        return {"index": 0, "shown": []}
    try:
        return json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"index": 0, "shown": []}


def _save_progress(progress: dict) -> None:
    """保存学习进度。"""
    _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def get_daily_concept() -> dict | None:
    """获取今日概念（按天轮换）。

    Returns:
        概念 dict 或 None（概念库为空时）
    """
    concepts = _load_concepts()
    if not concepts:
        return None

    progress = _load_progress()
    idx = progress.get("index", 0) % len(concepts)
    concept = concepts[idx]

    # 更新进度
    progress["index"] = (idx + 1) % len(concepts)
    shown = progress.get("shown", [])
    shown.append({"concept_id": concept.get("id"), "date": time.strftime("%Y-%m-%d")})
    progress["shown"] = shown[-50:]  # 保留最近50条
    _save_progress(progress)

    return concept


def get_concept_by_id(concept_id: str) -> dict | None:
    """按 ID 获取概念。"""
    concepts = _load_concepts()
    for c in concepts:
        if c.get("id") == concept_id:
            return c
    return None


def search_concepts(keyword: str) -> list[dict]:
    """按关键词搜索概念。"""
    concepts = _load_concepts()
    keyword = keyword.lower()
    results = []
    for c in concepts:
        if (
            keyword in c.get("name", "").lower()
            or keyword in c.get("definition", "").lower()
            or keyword in c.get("id", "").lower()
        ):
            results.append(c)
    return results


def get_progress_stats() -> dict:
    """获取学习进度统计。"""
    concepts = _load_concepts()
    progress = _load_progress()
    shown = progress.get("shown", [])
    return {
        "total_concepts": len(concepts),
        "shown_count": len(shown),
        "current_index": progress.get("index", 0),
        "recent_concepts": shown[-10:],
    }
