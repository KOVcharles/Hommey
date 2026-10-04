"""Command-line and compatibility ingestion entrypoints for the RAG pipeline."""
from __future__ import annotations

import argparse
import logging
from typing import Any, Dict

from .config import RAGPipelineConfig
from .pipeline import RAGPipeline

logger = logging.getLogger(__name__)


def ingest_documents(
    documents_dir: str,
    knowledge_base_path: str,
    collection_name: str,
    rebuild: bool = False,
) -> Dict[str, Any]:
    config = RAGPipelineConfig.from_settings(
        {
            "documents_dir": documents_dir,
            "knowledge_base_path": knowledge_base_path,
            "collection_name": collection_name,
        }
    )
    try:
        pipeline = RAGPipeline(config=config)
    except Exception as exc:
        logger.exception("Failed to initialize RAG ingestion pipeline")
        return {"status": "error", "message": str(exc)}

    try:
        return {
            **pipeline.ingest(documents_dir, rebuild=rebuild).to_dict(),
            "documents_dir": documents_dir,
        }
    except Exception as exc:
        logger.exception("RAG ingestion failed")
        return {"status": "error", "message": str(exc), "documents_dir": documents_dir}
    finally:
        pipeline.close()


def main() -> None:
    config = RAGPipelineConfig.from_settings()
    parser = argparse.ArgumentParser(description="Build or update the Hommey RAG knowledge base.")
    parser.add_argument("--documents-dir", default=config.documents_dir)
    parser.add_argument("--knowledge-base-path", default=config.knowledge_base_path)
    parser.add_argument("--collection", default=config.collection_name)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    result = ingest_documents(
        documents_dir=args.documents_dir,
        knowledge_base_path=args.knowledge_base_path,
        collection_name=args.collection,
        rebuild=args.rebuild,
    )
    print(result)


if __name__ == "__main__":
    main()
