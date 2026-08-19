"""Embedding interfaces for RAG vector stores."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from pathlib import Path
from typing import Any, List, Optional

import requests

logger = logging.getLogger(__name__)

# HTTP statuses worth retrying (rate-limited or upstream 5xx); auth/config
# errors (4xx except 429) are not transient and must fail fast.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_EMBEDDING_MODEL_CACHE: dict[str, Any] = {}


def resolve_embedding_model(model_name_or_path: str) -> str:
    """Resolve an existing local model path while preserving remote model IDs."""
    path = Path(model_name_or_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.exists():
        return str(path.resolve())
    return model_name_or_path


def _get_embedding_model(model_path: str) -> Any:
    """Load and cache a sentence-transformers model for the local backend."""
    cached = _EMBEDDING_MODEL_CACHE.get(model_path)
    if cached is not None:
        return cached

    from sentence_transformers import SentenceTransformer
    from sentence_transformers import models as st_models

    local_path = Path(model_path)
    if local_path.exists() and not (local_path / "modules.json").exists():
        transformer = st_models.Transformer(
            model_path,
            model_args={"local_files_only": True},
        )
        pooling = st_models.Pooling(
            transformer.get_word_embedding_dimension(),
            pooling_mode_mean_tokens=True,
        )
        model = SentenceTransformer(modules=[transformer, pooling])
    elif local_path.exists():
        model = SentenceTransformer(model_path, local_files_only=True)
    else:
        model = SentenceTransformer(model_path)
    _EMBEDDING_MODEL_CACHE[model_path] = model
    return model


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status in _RETRYABLE_STATUS
    return False


class TextEmbedder(ABC):
    @abstractmethod
    def dimension(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError

    @abstractmethod
    def embed_query(self, query: str) -> List[float]:
        raise NotImplementedError


class SentenceTransformerEmbedder(TextEmbedder):
    def __init__(self, model_name_or_path: str):
        self.model = _get_embedding_model(resolve_embedding_model(model_name_or_path))

    def dimension(self) -> int:
        return int(self.model.get_sentence_embedding_dimension())

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [self.model.encode(text).tolist() for text in texts]

    def embed_query(self, query: str) -> List[float]:
        return self.model.encode(query).tolist()


class SiliconFlowEmbedder(TextEmbedder):
    """OpenAI-compatible SiliconFlow embeddings client."""

    def __init__(
        self,
        api_key: str,
        model: str = "BAAI/bge-m3",
        base_url: str = "https://api.siliconflow.cn/v1",
        dimension: int = 1024,
        timeout_sec: float = 30.0,
        batch_size: int = 32,
        session: Any | None = None,
        # Phase 5 (audit §4.15): bounded retry/backoff for transient failures
        # and a process-local LRU cache so repeated texts (re-queries, duplicate
        # document versions) don't re-pay the embedding call.
        max_retries: int = 2,
        retry_base_delay_sec: float = 1.0,
        retry_max_delay_sec: float = 30.0,
        cache_size: int = 1024,
    ):
        if not api_key:
            raise RuntimeError("SiliconFlow embedding API key is missing")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._dimension = int(dimension)
        self.timeout_sec = float(timeout_sec)
        self.batch_size = max(1, int(batch_size))
        self.session = session or requests.Session()
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay_sec = float(retry_base_delay_sec)
        self.retry_max_delay_sec = float(retry_max_delay_sec)
        self.cache_size = max(0, int(cache_size))
        # text-hash → list[list[float]]; OrderedDict gives cheap LRU eviction.
        self._cache: "OrderedDict[str, List[List[float]]]" = OrderedDict()

    def dimension(self) -> int:
        return self._dimension

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        embeddings: List[List[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            embeddings.extend(self._cached_batch(batch))
        return embeddings

    def _cached_batch(self, texts: List[str]) -> List[List[float]]:
        key = _batch_cache_key(texts)
        if key in self._cache:
            self._cache.move_to_end(key)
            return [list(vector) for vector in self._cache[key]]
        embeddings = self._request_embeddings_with_retry(texts)
        if self.cache_size > 0:
            self._cache[key] = embeddings
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        # Return copies on both paths: the caller may mutate its vectors, and the
        # cache must never hand out a reference to the stored inner lists.
        return [list(vector) for vector in embeddings]

    def embed_query(self, query: str) -> List[float]:
        embeddings = self.embed_texts([query])
        return embeddings[0] if embeddings else []

    def _request_embeddings_with_retry(self, texts: List[str]) -> List[List[float]]:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._request_embeddings(texts)
            except Exception as exc:  # noqa: BLE001 - non-transient errors re-raise below
                last_error = exc
                if not _is_transient_error(exc) or attempt >= self.max_retries:
                    raise
                delay = min(
                    self.retry_base_delay_sec * (2 ** attempt),
                    self.retry_max_delay_sec,
                )
                logger.warning(
                    "Embedding request failed (attempt %d/%d): %s; retrying in %.2fs",
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                    delay,
                )
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def _request_embeddings(self, texts: List[str]) -> List[List[float]]:
        response = self.session.post(
            f"{self.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "input": texts,
                "encoding_format": "float",
            },
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        data = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
        embeddings = [item.get("embedding", []) for item in data]
        if len(embeddings) != len(texts):
            raise RuntimeError("SiliconFlow embedding response count mismatch")
        for embedding in embeddings:
            if len(embedding) != self._dimension:
                raise RuntimeError(
                    f"SiliconFlow embedding dimension mismatch: expected {self._dimension}, got {len(embedding)}"
                )
        return embeddings


def create_text_embedder(
    backend: str,
    model: str,
    api_key: str | None = None,
    base_url: str = "https://api.siliconflow.cn/v1",
    dimension: int = 1024,
    timeout_sec: float = 30.0,
    batch_size: int = 32,
    # Phase 5: retry/backoff + cache knobs (audit §4.15).
    max_retries: int = 2,
    retry_base_delay_sec: float = 1.0,
    retry_max_delay_sec: float = 30.0,
    cache_size: int = 1024,
) -> TextEmbedder:
    normalized = (backend or "siliconflow").lower()
    if normalized == "local":
        return SentenceTransformerEmbedder(model)
    if normalized == "siliconflow":
        return SiliconFlowEmbedder(
            api_key=api_key or "",
            model=model,
            base_url=base_url,
            dimension=dimension,
            timeout_sec=timeout_sec,
            batch_size=batch_size,
            max_retries=max_retries,
            retry_base_delay_sec=retry_base_delay_sec,
            retry_max_delay_sec=retry_max_delay_sec,
            cache_size=cache_size,
        )
    raise ValueError(f"Unsupported RAG embedding backend: {backend}")


def _batch_cache_key(texts: List[str]) -> str:
    """Deterministic cache key for a text batch (any order-independent use is
    the caller's responsibility — batches are sliced in document order)."""
    return hashlib.sha256(
        json.dumps(list(texts), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# TODO: Add multimodal embedders behind TextEmbedder or a sibling interface
# when image/table embeddings become part of the production dependency set.
