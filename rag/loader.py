"""Document loading interfaces and filesystem implementation.

Loader writes only the document-layer identity fields (audit §6.1.1): the
content version fingerprint and the lineage ``document_id`` (the path relative
to the ingestion root, aligned with the manifest's ``documents`` keys).  The
old ``source``/``parent_doc`` metadata keys are gone — citation identity now
comes from the ``RawDocument`` fields.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, List

from .schemas import RawDocument, _document_version_from_bytes


class DocumentLoader(ABC):
    @abstractmethod
    def load(self, path: str | Path) -> List[RawDocument]:
        raise NotImplementedError


class FileSystemDocumentLoader(DocumentLoader):
    def __init__(self, supported_file_types: Iterable[str] = ("txt", "pdf")):
        self.supported_file_types = {item.lower().lstrip(".") for item in supported_file_types}

    def load(self, path: str | Path) -> List[RawDocument]:
        root = Path(path)
        if not root.exists():
            raise FileNotFoundError(f"Document path does not exist: {root}")

        if root.is_file():
            return [self._load_file(root, root)]

        documents: List[RawDocument] = []
        for file_path in sorted(item for item in root.rglob("*") if item.is_file()):
            file_type = file_path.suffix.lower().lstrip(".")
            if file_type not in self.supported_file_types:
                continue
            documents.append(self._load_file(file_path, root))
        return documents

    def _load_file(self, path: Path, root: Path) -> RawDocument:
        file_type = path.suffix.lower().lstrip(".")
        content = path.read_bytes()
        try:
            document_id = path.relative_to(root).as_posix()
        except ValueError:
            # A single-file ingest (or a path outside the root) has no
            # sub-directory identity; the filename is the stable anchor.
            document_id = path.name
        return RawDocument(
            content=content,
            source_path=str(path),
            filename=path.name,
            file_type=file_type,
            metadata={
                "document_version": _document_version_from_bytes(content),
                "document_id": document_id,
            },
        )
