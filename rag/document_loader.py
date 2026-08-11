"""Load source documents for the RAG ingestion pipeline."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .schemas import SourceDocument


DEFAULT_CATEGORY_MAPPING = {
    "policy_authority": "policy_governance",
    "travel_standards": "travel_policy",
    "international_travel": "international_travel_policy",
    "exception_approval": "exception_policy",
    "rag_validation": "validation_scenarios",
    "reimbursement_policy": "reimbursement_policy",
    "booking_guide": "booking_guide",
    "faq": "faq",
    "emergency_procedures": "emergency_procedures",
    "platform_guide": "platform_guide",
    "city_specific_tips": "city_guide",
    "environmental_initiatives": "environmental_initiatives",
}


def infer_category(path: Path, mapping: Optional[Dict[str, str]] = None) -> str:
    mapping = mapping or DEFAULT_CATEGORY_MAPPING
    stem = path.stem
    for key, category in mapping.items():
        if key in stem:
            return category
    return "business_travel"


def load_text_documents(directory: str, pattern: str = "*.txt") -> List[SourceDocument]:
    root = Path(directory)
    if not root.exists():
        return []

    documents: List[SourceDocument] = []
    for path in sorted(root.glob(pattern)):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            continue
        title = content.splitlines()[0].strip() if content.splitlines() else path.stem
        documents.append(
            SourceDocument(
                content=content,
                source_path=str(path),
                title=title,
                category=infer_category(path),
                metadata={
                    "source": "business_travel_documents",
                    "parent_doc": path.name,
                },
            )
        )
    return documents


def iter_chunks(documents: Iterable[SourceDocument], max_chars: int = 600, overlap: int = 100):
    from .chunker import chunk_document

    for document in documents:
        yield from chunk_document(document, max_chars=max_chars, overlap=overlap)
