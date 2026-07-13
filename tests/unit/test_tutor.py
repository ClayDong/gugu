"""小白学堂 tutor.py 单元测试。

覆盖 4 个公开函数：get_daily_concept, get_concept_by_id, search_concepts, get_progress_stats。
测试隔离：用 monkeypatch 重定向 _PROGRESS_FILE 到 tmp_path，清理 _concepts_cache。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gugu.wisdom import tutor


@pytest.fixture
def isolated_progress(tmp_path: Path, monkeypatch):
    """隔离进度文件，避免污染真实 data/concept_progress.json。"""
    progress_file = tmp_path / "concept_progress.json"
    monkeypatch.setattr(tutor, "_PROGRESS_FILE", progress_file)
    return progress_file


@pytest.fixture
def fresh_cache(monkeypatch):
    """每个测试前清空概念缓存，确保 _load_concepts 重新读取。"""
    monkeypatch.setattr(tutor, "_concepts_cache", None)
    yield
    monkeypatch.setattr(tutor, "_concepts_cache", None)


class TestGetDailyConcept:
    """get_daily_concept 测试。"""

    def test_returns_concept_with_required_fields(self, isolated_progress, fresh_cache):
        """返回的 dict 含 id/name/definition 等必需字段。"""
        concept = tutor.get_daily_concept()
        assert concept is not None
        assert "id" in concept
        assert "name" in concept
        assert "definition" in concept
        assert "related_books" in concept

    def test_increments_progress_index(self, isolated_progress, fresh_cache):
        """连续调用使 index 递增，且概念不同。"""
        c1 = tutor.get_daily_concept()
        c2 = tutor.get_daily_concept()
        assert c1 is not None and c2 is not None
        assert c1["id"] != c2["id"]

    def test_saves_progress_to_file(self, isolated_progress, fresh_cache):
        """调用后进度文件被写入磁盘。"""
        assert not isolated_progress.exists()
        tutor.get_daily_concept()
        assert isolated_progress.exists()
        data = json.loads(isolated_progress.read_text(encoding="utf-8"))
        assert "index" in data
        assert "shown" in data
        assert len(data["shown"]) == 1

    def test_wraps_around_concepts(self, isolated_progress, fresh_cache):
        """index 超过概念数时回绕（取模）。"""
        concepts = tutor._load_concepts()
        n = len(concepts)
        # 调用 n+1 次，第 n+1 次应回到第 0 个
        last_id = None
        for _ in range(n + 1):
            c = tutor.get_daily_concept()
            last_id = c["id"]
        # 回绕后应等于第一个概念
        assert last_id == concepts[0]["id"]

    def test_returns_none_when_concepts_empty(self, isolated_progress, monkeypatch):
        """概念库为空时返回 None。"""
        monkeypatch.setattr(tutor, "_concepts_cache", [])
        result = tutor.get_daily_concept()
        assert result is None


class TestGetConceptById:
    """get_concept_by_id 测试。"""

    def test_returns_concept_for_valid_id(self, fresh_cache):
        """已知 id 返回概念。"""
        concepts = tutor._load_concepts()
        first_id = concepts[0]["id"]
        concept = tutor.get_concept_by_id(first_id)
        assert concept is not None
        assert concept["id"] == first_id

    def test_returns_none_for_unknown_id(self, fresh_cache):
        """未知 id 返回 None。"""
        result = tutor.get_concept_by_id("nonexistent_id_12345")
        assert result is None


class TestSearchConcepts:
    """search_concepts 测试。"""

    def test_matches_name(self, fresh_cache):
        """关键词匹配 name 字段。"""
        results = tutor.search_concepts("安全边际")
        assert len(results) >= 1
        assert any(r["id"] == "safety_margin" for r in results)

    def test_matches_definition(self, fresh_cache):
        """关键词匹配 definition 字段。"""
        results = tutor.search_concepts("格雷厄姆")
        assert len(results) >= 1

    def test_case_insensitive(self, fresh_cache):
        """大写关键词也能匹配（中文不区分大小写，验证英文 id 匹配）。"""
        results_lower = tutor.search_concepts("safety")
        results_upper = tutor.search_concepts("SAFETY")
        assert len(results_lower) > 0
        assert len(results_upper) > 0

    def test_returns_empty_for_no_match(self, fresh_cache):
        """无匹配返回空列表。"""
        results = tutor.search_concepts("不存在的关键词xyz123")
        assert results == []


class TestGetProgressStats:
    """get_progress_stats 测试。"""

    def test_returns_stats_dict(self, isolated_progress, fresh_cache):
        """返回含 total_concepts/shown_count/current_index/recent_concepts 的统计。"""
        # 先调用一次产生进度
        tutor.get_daily_concept()
        stats = tutor.get_progress_stats()
        assert "total_concepts" in stats
        assert "shown_count" in stats
        assert "current_index" in stats
        assert "recent_concepts" in stats
        assert stats["total_concepts"] > 0
        assert stats["shown_count"] == 1
        assert len(stats["recent_concepts"]) == 1

    def test_stats_with_no_progress(self, isolated_progress, fresh_cache):
        """无进度文件时返回零值统计。"""
        stats = tutor.get_progress_stats()
        assert stats["shown_count"] == 0
        assert stats["current_index"] == 0
        assert stats["recent_concepts"] == []
        assert stats["total_concepts"] > 0
