"""知识库 Agent 单元测试。

覆盖：索引构建、检索、问答（含 LLM 降级）、用户画像影响。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gugu.wisdom.kb_agent import KBAnswer, Citation, KnowledgeBaseAgent
from gugu.wisdom.kb_indexer import (
    Chunk,
    chunk_text,
    extract_text_from_docx,
    tokenize_zh,
    build_index,
    load_index,
)


class TestChunkText:
    """文本切块测试。"""

    def test_chunk_basic(self):
        text = "A" * 1200
        chunks = chunk_text(text, "测试书", chunk_size=500, overlap=100)
        # 1200 字，500一块，步长400，预期 3 块
        assert len(chunks) >= 3
        assert all(c.book == "测试书" for c in chunks)
        assert chunks[0].id == 0
        assert chunks[0].char_offset == 0

    def test_chunk_overlap(self):
        text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 50  # 1300 字
        chunks = chunk_text(text, "书", chunk_size=500, overlap=100)
        # 验证相邻块有重叠
        if len(chunks) >= 2:
            overlap_text = chunks[0].text[-100:]
            assert chunks[1].text[:100] == overlap_text

    def test_chunk_short_text(self):
        text = "短文本"
        chunks = chunk_text(text, "书", chunk_size=500, overlap=100)
        assert len(chunks) == 1
        assert chunks[0].text == "短文本"


class TestTokenizeZh:
    """中文分词测试。"""

    def test_tokenize_chinese(self):
        tokens = tokenize_zh("止损是交易的最高准则")
        assert isinstance(tokens, list)
        assert len(tokens) > 0
        # 应包含"止损"
        assert any("止损" in t for t in tokens)

    def test_tokenize_filters_single_char(self):
        tokens = tokenize_zh("A B C 止损")
        # 单字符应被过滤
        assert all(len(t.strip()) > 1 for t in tokens)

    def test_tokenize_empty(self):
        assert tokenize_zh("") == []
        assert tokenize_zh("   ") == []


class TestBuildIndex:
    """索引构建测试（使用临时目录和 mock docx）。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.books_dir = Path(self.tmpdir) / "books"
        self.books_dir.mkdir()
        self.output_dir = Path(self.tmpdir) / "kb_index"

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_mock_docx(self, name: str, text: str):
        """创建 mock docx 文件（不真正生成 docx，而是 mock extract 函数）。"""
        # 实际测试中我们 mock extract_text_from_docx
        (self.books_dir / f"{name}.docx").write_bytes(b"mock docx content")

    def test_build_index_with_mocked_extraction(self):
        self._create_mock_docx("测试书1", "止损铁律是交易的核心")
        self._create_mock_docx("测试书2", "价值投资看重安全边际")

        mock_texts = {
            "测试书1": "止损是交易的最高准则。仓位管理要分层下注。" * 20,
            "测试书2": "安全边际是价值投资的核心。能力圈原则。" * 20,
        }

        with patch("gugu.wisdom.kb_indexer.extract_text_from_docx") as mock_extract:
            mock_extract.side_effect = lambda p: mock_texts.get(p.stem, "")
            chunk_count, books = build_index(
                books_dir=self.books_dir, output_dir=self.output_dir, force=True
            )

        assert chunk_count > 0
        assert len(books) == 2
        assert "测试书1" in books
        # 验证索引文件生成
        assert (self.output_dir / "chunks.json").exists()
        assert (self.output_dir / "tokens.pkl").exists()
        assert (self.output_dir / "meta.json").exists()

    def test_load_index(self):
        self.test_build_index_with_mocked_extraction()
        result = load_index(output_dir=self.output_dir)
        assert result is not None
        chunks, tokens, meta = result
        assert len(chunks) > 0
        assert len(tokens) == len(chunks)
        assert meta["book_count"] == 2

    def test_incremental_skip(self):
        """二次构建（无变更）应跳过。"""
        self.test_build_index_with_mocked_extraction()
        # 再次构建（不 force）
        with patch("gugu.wisdom.kb_indexer.extract_text_from_docx") as mock_extract:
            mock_extract.side_effect = lambda p: "应跳过"
            chunk_count, books = build_index(
                books_dir=self.books_dir, output_dir=self.output_dir, force=False
            )
        # 应保持原索引（未重新构建）
        assert mock_extract.call_count == 0

    def test_build_index_empty_dir(self):
        empty_dir = Path(self.tmpdir) / "empty"
        empty_dir.mkdir()
        result = build_index(books_dir=empty_dir, output_dir=self.output_dir, force=True)
        assert result == (0, [])


class TestKBSearch:
    """KB Agent 检索测试。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output_dir = Path(self.tmpdir) / "kb_index"
        self.output_dir.mkdir()

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_test_index(self):
        """构建测试索引。"""
        chunks = [
            Chunk(id=0, book="聪明的投资者", text="安全边际是价值投资的核心原则", char_offset=0),
            Chunk(id=1, book="聪明的投资者", text="市场先生每天报价情绪不定", char_offset=100),
            Chunk(id=2, book="股票大作手回忆录", text="关键点是趋势开始加速的位置", char_offset=0),
            Chunk(id=3, book="炒股的智慧", text="止损铁律到点必执行绝不摊平", char_offset=0),
        ]
        tokens = [tokenize_zh(c.text) for c in chunks]

        (self.output_dir / "chunks.json").write_text(
            json.dumps([c.to_dict() for c in chunks], ensure_ascii=False), encoding="utf-8"
        )
        import pickle

        with (self.output_dir / "tokens.pkl").open("wb") as f:
            pickle.dump(tokens, f)
        meta = {
            "books": ["聪明的投资者", "股票大作手回忆录", "炒股的智慧"],
            "book_count": 3,
            "chunk_count": 4,
            "built_at": "2026-07-01 00:00:00",
        }
        (self.output_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load_test_index(self):
        """加载测试索引（替代 load_index 默认路径）。"""
        result = load_index(output_dir=self.output_dir)
        return result

    def _patch_loader(self):
        """返回 patch 上下文，使 KB Agent 加载测试索引。"""
        result = self._load_test_index()
        return patch("gugu.wisdom.kb_agent.load_index", return_value=result)

    def test_search_returns_relevant(self):
        self._build_test_index()
        with self._patch_loader():
            agent = KnowledgeBaseAgent()
            results = agent.search("安全边际", top_k=2)
            assert len(results) > 0
            # 安全边际应在聪明的投资者中
            assert any(r.book == "聪明的投资者" for r in results)

    def test_search_empty_query(self):
        self._build_test_index()
        with self._patch_loader():
            agent = KnowledgeBaseAgent()
            results = agent.search("", top_k=5)
            assert results == []

    def test_search_no_index(self):
        with patch("gugu.wisdom.kb_agent.load_index", return_value=None):
            agent = KnowledgeBaseAgent()
            results = agent.search("测试", top_k=5)
            assert results == []

    def test_search_unrelated_query(self):
        self._build_test_index()
        with self._patch_loader():
            agent = KnowledgeBaseAgent()
            results = agent.search("量子物理", top_k=5)
            # 不相关的查询应返回少量低分结果或空
            # BM25 可能返回零分结果，过滤后为空
            assert isinstance(results, list)


class TestKBAsk:
    """KB Agent 问答测试。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output_dir = Path(self.tmpdir) / "kb_index"
        self.output_dir.mkdir()
        self._build_test_index()

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_test_index(self):
        chunks = [
            Chunk(id=0, book="聪明的投资者", text="安全边际是价值投资的核心原则，以低于内在价值的价格买入", char_offset=0),
            Chunk(id=1, book="股票大作手回忆录", text="关键点是趋势开始加速的位置，确认后进场", char_offset=0),
        ]
        tokens = [tokenize_zh(c.text) for c in chunks]
        (self.output_dir / "chunks.json").write_text(
            json.dumps([c.to_dict() for c in chunks], ensure_ascii=False), encoding="utf-8"
        )
        import pickle

        with (self.output_dir / "tokens.pkl").open("wb") as f:
            pickle.dump(tokens, f)
        (self.output_dir / "meta.json").write_text(
            json.dumps({"books": ["聪明的投资者", "股票大作手回忆录"], "book_count": 2, "chunk_count": 2}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _patch_loader(self):
        result = load_index(output_dir=self.output_dir)
        return patch("gugu.wisdom.kb_agent.load_index", return_value=result)

    @pytest.mark.asyncio
    async def test_ask_no_index(self):
        with patch("gugu.wisdom.kb_agent.load_index", return_value=None):
            agent = KnowledgeBaseAgent()
            result = await agent.ask("什么是安全边际")
            assert result.source == "empty"
            assert "未找到" in result.answer or "构建索引" in result.answer

    @pytest.mark.asyncio
    async def test_ask_llm_unavailable_returns_raw(self):
        with self._patch_loader():
            agent = KnowledgeBaseAgent()
            agent._llm_available = False
            result = await agent.ask("安全边际是什么")
            assert result.source == "raw_chunks"
            assert len(result.citations) > 0
            assert any("聪明的投资者" in c.book for c in result.citations)

    @pytest.mark.asyncio
    async def test_ask_llm_success(self):
        with self._patch_loader():
            agent = KnowledgeBaseAgent()
            agent._llm_available = True

            # mock LLM 响应
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "choices": [{"message": {"content": "安全边际是格雷厄姆提出的概念[聪明的投资者]"}}]
            }
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            agent._async_client = mock_client
            agent._llm_cfg = MagicMock(
                llm_base_url="http://test", llm_api_key="key", llm_model="m"
            )

            result = await agent.ask("什么是安全边际")
            assert result.source == "llm"
            assert "安全边际" in result.answer
            assert len(result.citations) > 0

    @pytest.mark.asyncio
    async def test_ask_llm_failure_degrades(self):
        with self._patch_loader():
            agent = KnowledgeBaseAgent()
            agent._llm_available = True

            # mock LLM 抛异常
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("LLM down"))
            agent._async_client = mock_client
            agent._llm_cfg = MagicMock(
                llm_base_url="http://test", llm_api_key="key", llm_model="m"
            )

            result = await agent.ask("安全边际")
            # 应降级为原始片段
            assert result.source == "raw_chunks"


class TestKBAnswerDataclass:
    """KBAnswer 数据类测试。"""

    def test_to_dict(self):
        answer = KBAnswer(
            question="测试问题",
            answer="测试答案",
            citations=[Citation(book="书名", snippet="片段")],
            related_concepts=["安全边际"],
            source="llm",
        )
        d = answer.to_dict()
        assert d["question"] == "测试问题"
        assert d["source"] == "llm"
        assert len(d["citations"]) == 1
        assert d["citations"][0]["book"] == "书名"
