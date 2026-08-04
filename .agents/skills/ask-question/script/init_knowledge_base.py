"""Initialize the ask-question RAG knowledge base.

This script is intentionally thin. The ingestion workflow lives in
``rag.ingestion`` so CLI scripts, tests, and future admin tools can share the
same document loading, chunking, embedding, and Milvus write path.
"""
from __future__ import annotations

import sys
from pathlib import Path

current_dir = Path(__file__).parent
project_root = current_dir.parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from rag.ingestion import ingest_documents
from settings import RAG_CONFIG


def main() -> None:
    documents_dir = RAG_CONFIG.get(
        "documents_dir",
        str(current_dir.parent / "data" / "documents"),
    )
    knowledge_base_path = RAG_CONFIG.get("knowledge_base_path", "data/rag_knowledge")
    collection_name = RAG_CONFIG.get("collection_name", "business_travel_knowledge")

    result = ingest_documents(
        documents_dir=documents_dir,
        knowledge_base_path=knowledge_base_path,
        collection_name=collection_name,
        rebuild=True,
    )

    print("=" * 70)
    print("RAG knowledge base initialization")
    print("=" * 70)
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
