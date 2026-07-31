"""知识库索引构建脚本。

用法：
    python scripts/build_kb_index.py             # 增量构建（无变更则跳过）
    python scripts/build_kb_index.py --force     # 强制重建
    python scripts/build_kb_index.py --stats     # 查看索引统计
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 支持直接运行（无需安装）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.wisdom.kb_agent import get_kb_agent
from gugu.wisdom.kb_indexer import DEFAULT_BOOKS_DIR, INDEX_DIR, build_index


def main() -> None:
    parser = argparse.ArgumentParser(description="构建股书知识库 BM25 索引")
    parser.add_argument("--force", action="store_true", help="强制重建索引")
    parser.add_argument("--stats", action="store_true", help="查看索引统计")
    parser.add_argument("--books-dir", default=str(DEFAULT_BOOKS_DIR), help="docx 目录")
    args = parser.parse_args()

    if args.stats:
        agent = get_kb_agent()
        stats = agent.get_stats()
        print("=== 知识库索引统计 ===")
        for k, v in stats.items():
            if k == "books":
                print(f"  books: {len(v)} 本")
                for b in v:
                    print(f"    - {b}")
            else:
                print(f"  {k}: {v}")
        return

    books_dir = Path(args.books_dir)
    print(f"数据源: {books_dir}")
    print(f"输出到: {INDEX_DIR}")
    print()

    chunk_count, book_names = build_index(books_dir=books_dir, force=args.force)
    print()
    print(f"=== 构建完成 ===")
    print(f"书籍数: {len(book_names)}")
    print(f"文本块: {chunk_count}")
    print(f"索引目录: {INDEX_DIR}")


if __name__ == "__main__":
    main()
