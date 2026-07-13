"""知识库索引构建器：从 docx 原书构建 BM25 检索索引。

性能考量（用户强调电脑性能）：
- 使用 BM25（rank-bm25）+ jieba 分词，无需 PyTorch/GPU
- 索引文件轻量（chunks.json + tokens.pkl，约 5-15MB）
- 34 本 docx 索引构建约 30-60 秒
- 单次查询 < 100ms

索引结构：
- data/kb_index/chunks.json：[{id, book, text, char_offset}]
- data/kb_index/tokens.pkl：[[token, ...], ...] 与 chunks 一一对应
- data/kb_index/meta.json：{books, built_at, chunk_count, config}

数据源：/Users/dong/workspace/pdf2word/知识库_docx/*.docx
"""

from __future__ import annotations

import json
import pickle
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from gugu.config import PROJECT_ROOT
from gugu.utils.log import get_logger

logger = get_logger()

# 默认数据源（用户提供的 word 版本）
DEFAULT_BOOKS_DIR = Path("/Users/dong/workspace/pdf2word/知识库_docx")
# 索引输出目录
INDEX_DIR = PROJECT_ROOT / "data" / "kb_index"

# 切块参数
CHUNK_SIZE = 500  # 每块字符数
CHUNK_OVERLAP = 100  # 重叠字符数（保留上下文）


@dataclass
class Chunk:
    """文本块。"""

    id: int
    book: str  # 书名（不含扩展名）
    text: str
    char_offset: int  # 在原书中的字符偏移

    def to_dict(self) -> dict:
        return asdict(self)


def extract_text_from_docx(docx_path: Path) -> str:
    """从 docx 提取纯文本。"""
    from docx import Document  # python-docx

    doc = Document(str(docx_path))
    parts: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def chunk_text(
    text: str, book: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[Chunk]:
    """将文本切分为重叠块。"""
    chunks: list[Chunk] = []
    start = 0
    chunk_id = 0
    while start < len(text):
        end = start + chunk_size
        piece = text[start:end]
        chunks.append(Chunk(id=chunk_id, book=book, text=piece, char_offset=start))
        chunk_id += 1
        # 下一个块起点（考虑重叠）
        next_start = start + chunk_size - overlap
        if next_start <= start:  # 防止死循环（overlap >= chunk_size 时）
            next_start = start + 1
        start = next_start
    return chunks


def tokenize_zh(text: str) -> list[str]:
    """中文分词（jieba），保留长度>1的词。"""
    import jieba

    tokens = jieba.lcut(text)
    # 过滤单字（多为标点/虚词）和纯空白
    return [t for t in tokens if len(t.strip()) > 1]


def build_index(
    books_dir: Path = DEFAULT_BOOKS_DIR,
    output_dir: Path = INDEX_DIR,
    force: bool = False,
) -> tuple[int, list[str]]:
    """构建 BM25 索引。

    Args:
        books_dir: docx 文件目录
        output_dir: 索引输出目录
        force: 强制重建（忽略增量检测）

    Returns:
        (chunk_count, book_names)
    """
    if not books_dir.exists():
        logger.error(f"[kb-indexer] 书籍目录不存在: {books_dir}")
        return 0, []

    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_file = output_dir / "chunks.json"
    tokens_file = output_dir / "tokens.pkl"
    meta_file = output_dir / "meta.json"

    # 增量检测：若索引已存在且 books_dir 无变更，跳过
    if not force and meta_file.exists():
        try:
            old_meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if _is_index_fresh(old_meta, books_dir):
                logger.info(
                    f"[kb-indexer] 索引已是最新（{old_meta.get('chunk_count')} 块，"
                    f"建于 {old_meta.get('built_at')}），跳过重建。使用 force=True 强制重建。"
                )
                return old_meta.get("chunk_count", 0), old_meta.get("books", [])
        except Exception:
            pass  # meta 损坏，重建

    docx_files = sorted(books_dir.glob("*.docx"))
    if not docx_files:
        logger.warning(f"[kb-indexer] 目录中无 docx 文件: {books_dir}")
        return 0, []

    logger.info(f"[kb-indexer] 开始构建索引：{len(docx_files)} 本书籍")
    t0 = time.time()

    all_chunks: list[Chunk] = []
    all_tokens: list[list[str]] = []
    book_names: list[str] = []

    for docx_path in docx_files:
        book_name = docx_path.stem
        try:
            text = extract_text_from_docx(docx_path)
            if not text:
                logger.warning(f"[kb-indexer] {book_name} 文本为空，跳过")
                continue
            chunks = chunk_text(text, book_name)
            for chunk in chunks:
                all_chunks.append(chunk)
                all_tokens.append(tokenize_zh(chunk.text))
            book_names.append(book_name)
            logger.info(f"[kb-indexer] {book_name}: {len(chunks)} 块, {len(text)} 字")
        except Exception as e:
            logger.exception(f"[kb-indexer] 解析 {book_name} 失败: {e}")

    if not all_chunks:
        logger.error("[kb-indexer] 无有效文本块，索引构建失败")
        return 0, []

    # 持久化
    chunks_file.write_text(
        json.dumps([c.to_dict() for c in all_chunks], ensure_ascii=False), encoding="utf-8"
    )
    with tokens_file.open("wb") as f:
        pickle.dump(all_tokens, f)

    meta = {
        "books": book_names,
        "book_count": len(book_names),
        "chunk_count": len(all_chunks),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "build_seconds": round(time.time() - t0, 2),
        "config": {"chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP},
        "source_dir": str(books_dir),
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    elapsed = time.time() - t0
    logger.info(
        f"[kb-indexer] 索引构建完成: {len(all_chunks)} 块, {len(book_names)} 本书, "
        f"耗时 {elapsed:.1f}s, 输出至 {output_dir}"
    )
    return len(all_chunks), book_names


def _is_index_fresh(meta: dict, books_dir: Path) -> bool:
    """检测索引是否新鲜（源文件未变更）。"""
    try:
        source_dir = Path(meta.get("source_dir", ""))
        if source_dir != books_dir:
            return False
        # 检查文件数量和最新 mtime
        current_files = sorted(books_dir.glob("*.docx"))
        if len(current_files) != meta.get("book_count", 0):
            return False
        current_max_mtime = max(f.stat().st_mtime for f in current_files)
        # 用 built_at 反推时间戳（粗略对比）
        built_str = meta.get("built_at", "")
        if built_str:
            built_ts = time.mktime(time.strptime(built_str, "%Y-%m-%d %H:%M:%S"))
            return current_max_mtime <= built_ts + 60  # 60秒容差
    except Exception:
        pass
    return False


def load_index(output_dir: Path = INDEX_DIR) -> tuple[list[Chunk], list[list[str]], dict] | None:
    """加载已构建的索引。

    Returns:
        (chunks, tokens, meta) 或 None（索引不存在）
    """
    chunks_file = output_dir / "chunks.json"
    tokens_file = output_dir / "tokens.pkl"
    meta_file = output_dir / "meta.json"

    if not chunks_file.exists() or not tokens_file.exists():
        return None

    try:
        chunks_data = json.loads(chunks_file.read_text(encoding="utf-8"))
        chunks = [Chunk(**c) for c in chunks_data]
        with tokens_file.open("rb") as f:
            tokens = pickle.load(f)
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        return chunks, tokens, meta
    except Exception as e:
        logger.exception(f"[kb-indexer] 加载索引失败: {e}")
        return None
