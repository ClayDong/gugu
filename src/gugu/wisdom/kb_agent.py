"""知识库 Agent：基于 BM25 检索 + LLM 组织答案。

核心能力：
1. search(query, top_k) — 纯检索，返回最相关的文本块（供冲突裁决器引用原文）
2. ask(question, profile) — 检索 + LLM 组织答案（用于小白问答）

设计原则：
- 轻量：BM25 + jieba，无 PyTorch/GPU 依赖
- 优雅降级：索引缺失时返回空结果；LLM 不可用时返回原始片段
- 引用可追溯：每个答案附 [书名] 引用

性能：
- 首次加载索引约 1-2 秒（含 jieba 初始化）
- 单次检索 < 100ms
- LLM 调用约 2-5 秒
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from gugu.config import env
from gugu.models.user_profile import UserProfile, load_profile
from gugu.utils.log import get_logger
from gugu.wisdom.kb_indexer import Chunk, load_index, tokenize_zh

logger = get_logger()


@dataclass
class Citation:
    """答案引用。"""

    book: str
    snippet: str  # 原文片段（≤120字）


@dataclass
class KBAnswer:
    """KB Agent 问答结果。"""

    question: str
    answer: str
    citations: list[Citation]
    related_concepts: list[str]  # 相关概念名（可选）
    source: str  # "llm" | "raw_chunks" | "empty"

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "citations": [asdict(c) for c in self.citations],
            "related_concepts": self.related_concepts,
            "source": self.source,
        }


class KnowledgeBaseAgent:
    """知识库问答 Agent。"""

    def __init__(self) -> None:
        self._chunks: list[Chunk] | None = None
        self._tokens: list[list[str]] | None = None
        self._meta: dict = {}
        self._bm25: Any = None  # BM25Okapi 实例
        self._async_client: httpx.AsyncClient | None = None
        cfg = env()
        self._llm_available = bool(cfg.llm_api_key and cfg.llm_base_url and cfg.llm_model)
        self._llm_cfg = cfg

    def _ensure_loaded(self) -> bool:
        """懒加载索引（首次调用时加载）。"""
        if self._bm25 is not None:
            return True
        result = load_index()
        if result is None:
            logger.warning("[kb-agent] 索引未构建。请先运行: python scripts/build_kb_index.py")
            return False
        self._chunks, self._tokens, self._meta = result
        try:
            from rank_bm25 import BM25Okapi

            self._bm25 = BM25Okapi(self._tokens)
            logger.info(
                f"[kb-agent] 索引已加载: {len(self._chunks)} 块, "
                f"{self._meta.get('book_count', 0)} 本书"
            )
            return True
        except Exception as e:
            logger.exception(f"[kb-agent] BM25 初始化失败: {e}")
            return False

    async def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=30.0)
        return self._async_client

    async def close(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def search(self, query: str, top_k: int = 5) -> list[Chunk]:
        """纯检索：返回最相关的文本块。

        Args:
            query: 查询文本（自然语言）
            top_k: 返回前 K 个结果

        Returns:
            文本块列表（按相关性降序）
        """
        if not self._ensure_loaded():
            return []
        query_tokens = tokenize_zh(query)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        import numpy as np

        # 取 top_k 索引
        top_indices = np.argsort(scores)[::-1][:top_k]
        results: list[Chunk] = []
        for idx in top_indices:
            if scores[idx] > 0:  # 正分优先
                results.append(self._chunks[idx])

        # 回退：若 BM25 全零（极小语料 IDF 归零），用 token overlap 检索
        if not results:
            query_set = set(query_tokens)
            scored = []
            for i, toks in enumerate(self._tokens):
                overlap = len(query_set & set(toks))
                if overlap > 0:
                    scored.append((overlap, i))
            scored.sort(key=lambda x: -x[0])
            for _, idx in scored[:top_k]:
                results.append(self._chunks[idx])

        return results

    async def ask(
        self,
        question: str,
        profile: UserProfile | None = None,
        top_k: int = 5,
    ) -> KBAnswer:
        """问答：检索 + LLM 组织答案。

        Args:
            question: 用户问题
            profile: 用户画像（影响答案深度）
            top_k: 检索片段数

        Returns:
            KBAnswer
        """
        if profile is None:
            profile = load_profile()

        # 1. 检索相关片段
        chunks = self.search(question, top_k=top_k)
        if not chunks:
            return KBAnswer(
                question=question,
                answer="知识库中未找到相关内容。请先运行 `python scripts/build_kb_index.py` 构建索引。",
                citations=[],
                related_concepts=[],
                source="empty",
            )

        # 2. LLM 不可用 → 直接返回原始片段
        if not self._llm_available:
            answer_text = self._format_raw_chunks(chunks)
            return KBAnswer(
                question=question,
                answer=answer_text,
                citations=[Citation(book=c.book, snippet=c.text[:120]) for c in chunks],
                related_concepts=[],
                source="raw_chunks",
            )

        # 3. LLM 组织答案
        try:
            answer_text, citations = await self._llm_answer(question, chunks, profile)
            return KBAnswer(
                question=question,
                answer=answer_text,
                citations=citations,
                related_concepts=self._extract_concepts(answer_text),
                source="llm",
            )
        except Exception as e:
            logger.warning("[kb-agent] LLM 组织答案失败，降级为原始片段: {}", e)
            answer_text = self._format_raw_chunks(chunks)
            return KBAnswer(
                question=question,
                answer=answer_text,
                citations=[Citation(book=c.book, snippet=c.text[:120]) for c in chunks],
                related_concepts=[],
                source="raw_chunks",
            )

    async def _llm_answer(
        self, question: str, chunks: list[Chunk], profile: UserProfile
    ) -> tuple[str, list[Citation]]:
        """用 LLM 基于检索片段组织答案。"""
        # 构建上下文：每个片段标注 [书名]
        context_parts = []
        for i, c in enumerate(chunks):
            context_parts.append(f"[片段{i + 1}][{c.book}]\n{c.text}")
        context = "\n\n".join(context_parts)

        # 根据用户画像调整答案风格
        style_hint = ""
        if profile.is_novice():
            style_hint = "（用户是新手，请用通俗语言和类比解释，避免专业术语）"
        else:
            style_hint = "（用户有经验，可直接使用专业术语）"

        system_prompt = (
            "你是基于28本股书知识库的问答助手。请根据提供的书籍片段回答用户问题。\n"
            "规则：\n"
            "1. 答案必须基于提供的片段，不得编造\n"
            "2. 每个论点后用 [书名] 标注引用来源\n"
            "3. 若片段不足以回答，明确说明'根据现有资料无法完整回答'\n"
            f"4. {style_hint}\n"
            "5. 答案控制在 300 字以内"
        )
        user_prompt = f"## 问题\n{question}\n\n## 参考片段\n{context}\n\n## 请回答："

        client = await self._get_async_client()
        resp = await client.post(
            f"{self._llm_cfg.llm_base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._llm_cfg.llm_api_key}"},
            json={
                "model": self._llm_cfg.llm_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 800,
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"]

        # 提取引用（从片段中匹配）
        citations: list[Citation] = []
        seen_books: set[str] = set()
        for c in chunks:
            if c.book not in seen_books:
                citations.append(Citation(book=c.book, snippet=c.text[:120]))
                seen_books.add(c.book)
                if len(citations) >= 4:  # 最多4个引用
                    break

        return answer, citations

    @staticmethod
    def _format_raw_chunks(chunks: list[Chunk]) -> str:
        """LLM 不可用时，格式化原始片段作为答案。"""
        parts = ["以下是知识库中与您问题最相关的片段：\n"]
        for i, c in enumerate(chunks):
            parts.append(f"[{i + 1}] 《{c.book}》\n{c.text[:200]}...\n")
        return "\n".join(parts)

    @staticmethod
    def _extract_concepts(text: str) -> list[str]:
        """从答案文本中提取可能的概念（简单实现）。"""
        # 加载概念名列表，检查答案中是否包含
        concepts_file = Path(__file__).parent / "concepts.json"
        if not concepts_file.exists():
            return []
        try:
            concepts = json.loads(concepts_file.read_text(encoding="utf-8"))
            names = [c["name"] for c in concepts]
            return [n for n in names if n in text]
        except Exception:
            return []

    def get_stats(self) -> dict:
        """获取索引统计信息（用于 Web 展示）。"""
        if not self._ensure_loaded():
            return {"loaded": False}
        return {
            "loaded": True,
            "chunk_count": len(self._chunks) if self._chunks else 0,
            "book_count": self._meta.get("book_count", 0),
            "books": self._meta.get("books", []),
            "built_at": self._meta.get("built_at", ""),
        }


# 模块级单例（懒加载）
_agent: KnowledgeBaseAgent | None = None


def get_kb_agent() -> KnowledgeBaseAgent:
    """获取 KB Agent 单例。"""
    global _agent
    if _agent is None:
        _agent = KnowledgeBaseAgent()
    return _agent
