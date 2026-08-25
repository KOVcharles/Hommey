"""Index version fingerprint (audit §6.1.5).

The index fingerprint is a sha256 over every factor that changes the meaning
of an embedding/chunk.  When the fingerprint is unchanged, incremental writes
can be skipped as idempotent; when any factor changes, a rebuild is forced.
The fingerprint is frozen at pipeline entry and stamped onto every chunk's
``index_version`` metadata plus the manifest ``index.version``.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from . import schemas

SCHEMA_VERSION = schemas.SCHEMA_VERSION
CHUNKER_VERSION = schemas.CHUNKER_VERSION
PARSER_VERSIONS = schemas.PARSER_VERSIONS


def code_revision() -> str:
    """Best-effort git revision; non-git deployments fall back to 'unknown'."""
    try:
        import subprocess

        revision = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                capture_output=True,
                text=True,
                timeout=3,
            )
            .stdout.strip()
        )
        return revision or "unknown"
    except Exception:
        return "unknown"


def compute_index_fingerprint(
    *,
    embedding_model: str,
    embedding_dimension: int,
    embedding_backend: str,
    chunk_min_tokens: int,
    chunk_max_tokens: int,
    chunk_overlap_tokens: int,
    parser_versions: Optional[Dict[str, str]] = None,
    query_instruction: str = "",
    tokenizer: str = "cjk-count",
    ocr_enabled: bool = False,
    ocr_confidence_threshold: float = 0.5,
    ocr_model: str = "",
) -> str:
    """Compute the deterministic index fingerprint (first 16 hex chars)."""
    parsers = dict(parser_versions or PARSER_VERSIONS)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "embedding_model": embedding_model,
        "embedding_dimension": int(embedding_dimension),
        "embedding_backend": embedding_backend,
        "query_instruction": query_instruction,
        "chunk_min_tokens": int(chunk_min_tokens),
        "chunk_max_tokens": int(chunk_max_tokens),
        "chunk_overlap_tokens": int(chunk_overlap_tokens),
        "chunker_version": CHUNKER_VERSION,
        "parser_versions": {k: parsers[k] for k in sorted(parsers)},
        "tokenizer": tokenizer,
        "code_revision": code_revision(),
        # Phase 3: OCR changes which pages become chunks, so the flag and its
        # confidence gate belong in the fingerprint (toggling forces a rebuild).
        "ocr_enabled": bool(ocr_enabled),
        "ocr_confidence_threshold": float(ocr_confidence_threshold),
        "ocr_model": str(ocr_model or "") if ocr_enabled else "",
    }
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def index_version_block(config: Any, parser_versions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Freeze the version block recorded in the manifest and chunk metadata."""
    fingerprint = compute_index_fingerprint(
        embedding_model=config.embedding_model,
        embedding_dimension=config.embedding_dimension,
        embedding_backend=config.embedding_backend,
        chunk_min_tokens=config.chunk_min_tokens,
        chunk_max_tokens=config.chunk_max_tokens,
        chunk_overlap_tokens=config.chunk_overlap_tokens,
        parser_versions=parser_versions,
        ocr_enabled=getattr(config, "ocr_enabled", False),
        ocr_confidence_threshold=getattr(config, "ocr_confidence_threshold", 0.5),
        ocr_model=getattr(config, "ocr_model", ""),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "index": {
            "version": fingerprint,
            "embedding": {
                "model": config.embedding_model,
                "dimension": config.embedding_dimension,
                "backend": config.embedding_backend,
            },
            "chunk": {
                "min_tokens": config.chunk_min_tokens,
                "max_tokens": config.chunk_max_tokens,
                "overlap_tokens": config.chunk_overlap_tokens,
                "chunker_version": CHUNKER_VERSION,
            },
            "parsers": dict(parser_versions or PARSER_VERSIONS),
            "ocr": {
                "enabled": bool(getattr(config, "ocr_enabled", False)),
                "model": str(getattr(config, "ocr_model", "") or ""),
            },
            "code_revision": code_revision(),
        },
    }
