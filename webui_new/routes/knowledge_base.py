"""Authenticated RAG source browsing, upload, and refresh APIs."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile

from rag.document_loader import infer_category
from webui_new.auth import User, get_current_user
from webui_new.core.errors import BusinessError, InternalError
from webui_new.knowledge_base_service import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILES,
    KnowledgeBaseManagementService,
)


SUPPORTED_DOCUMENT_TYPES = {"txt", "md", "pdf"}
CATEGORY_LABELS = {
    "travel_policy": "差旅标准",
    "reimbursement_policy": "费用报销",
    "booking_guide": "预订指南",
    "faq": "常见问题",
    "emergency_procedures": "应急指南",
    "platform_guide": "平台使用",
    "city_guide": "城市指南",
    "environmental_initiatives": "绿色差旅",
    "business_travel": "差旅资料",
}


class KnowledgeBaseLibrary:
    """Expose display-safe metadata and content from one configured directory."""

    def __init__(self, documents_dir: str | Path):
        self.root = Path(documents_dir).resolve()

    def list_documents(self) -> list[dict]:
        if not self.root.exists():
            return []

        documents = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                path.resolve().relative_to(self.root)
            except ValueError:
                # Never follow a source-directory symlink outside the configured root.
                continue
            file_type = path.suffix.lower().lstrip(".")
            if file_type not in SUPPORTED_DOCUMENT_TYPES:
                continue
            try:
                content, page_count = self._read(path)
            except Exception:
                # One unreadable source must not make the whole library disappear.
                continue
            documents.append(self._metadata(path, content, page_count))
        return documents

    def get_document(self, document_id: str) -> dict:
        path = self._resolve_document(document_id)
        try:
            content, page_count = self._read(path)
        except Exception as exc:
            raise InternalError("KNOWLEDGE_DOCUMENT_UNREADABLE", "文档暂时无法读取，请稍后重试") from exc
        return {**self._metadata(path, content, page_count), "content": content}

    def _resolve_document(self, document_id: str) -> Path:
        candidate = (self.root / document_id).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise BusinessError("KNOWLEDGE_DOCUMENT_NOT_FOUND", "文档不存在", status_code=404) from exc

        file_type = candidate.suffix.lower().lstrip(".")
        if not candidate.is_file() or file_type not in SUPPORTED_DOCUMENT_TYPES:
            raise BusinessError("KNOWLEDGE_DOCUMENT_NOT_FOUND", "文档不存在", status_code=404)
        return candidate

    def _read(self, path: Path) -> tuple[str, int | None]:
        file_type = path.suffix.lower().lstrip(".")
        if file_type in {"txt", "md"}:
            return path.read_text(encoding="utf-8").strip(), None

        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF reading support is unavailable") from exc

        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(page for page in pages if page), len(reader.pages)

    def _metadata(self, path: Path, content: str, page_count: int | None) -> dict:
        relative_path = path.relative_to(self.root).as_posix()
        title = self._title(path, content)
        category = infer_category(path)
        stat = path.stat()
        compact_content = " ".join(content.split())
        return {
            "id": relative_path,
            "title": title,
            "filename": path.name,
            "file_type": path.suffix.lower().lstrip("."),
            "category": category,
            "category_label": CATEGORY_LABELS.get(category, "差旅资料"),
            "preview": compact_content[:108],
            "character_count": len(content),
            "read_minutes": max(1, round(len(content) / 500)),
            "page_count": page_count,
            "size_bytes": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }

    @staticmethod
    def _title(path: Path, content: str) -> str:
        for line in content.splitlines():
            title = line.strip().lstrip("#").strip()
            if title:
                return title[:120]
        return path.stem


async def _read_upload(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(min(1024 * 1024, MAX_UPLOAD_BYTES + 1)):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise BusinessError(
                "KNOWLEDGE_FILE_TOO_LARGE",
                f"单个文档不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
                status_code=400,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def create_knowledge_base_router(
    documents_dir: str | Path,
    knowledge_base_path: str | Path | None = None,
    *,
    management_service: KnowledgeBaseManagementService | None = None,
):
    library = KnowledgeBaseLibrary(documents_dir)
    management = management_service or KnowledgeBaseManagementService(
        documents_dir,
        knowledge_base_path or (Path(documents_dir).parent / "rag_knowledge"),
    )
    router = APIRouter()

    @router.get("/api/knowledge/documents")
    async def list_knowledge_documents(current_user: User = Depends(get_current_user)):
        documents = library.list_documents()
        for document in documents:
            path = library.root / document["id"]
            document["index_status"] = management.document_index_status(document["id"], path)
        return {"documents": documents, "total": len(documents)}

    @router.post("/api/knowledge/documents")
    async def upload_knowledge_documents(
        files: list[UploadFile] = File(...),
        current_user: User = Depends(get_current_user),
    ):
        # Testing phase: every authenticated user may manage the knowledge base.
        if not files or len(files) > MAX_UPLOAD_FILES:
            raise BusinessError(
                "KNOWLEDGE_FILE_COUNT_INVALID",
                f"每次最多上传 {MAX_UPLOAD_FILES} 份文档",
                status_code=400,
            )
        uploaded = []
        for file in files:
            content = await _read_upload(file)
            uploaded.append(management.upload(file.filename or "upload", content))
        return {"uploaded": uploaded, "total": len(uploaded), "refresh_required": True}

    @router.post("/api/knowledge/refresh")
    async def refresh_knowledge_base(current_user: User = Depends(get_current_user)):
        # Deliberately open to every authenticated user during the current test phase.
        return management.start_refresh(str(current_user.id))

    @router.get("/api/knowledge/refresh/status")
    async def knowledge_refresh_status(current_user: User = Depends(get_current_user)):
        return management.status()

    @router.get("/api/knowledge/documents/{document_id:path}")
    async def get_knowledge_document(
        document_id: str,
        current_user: User = Depends(get_current_user),
    ):
        return library.get_document(document_id)

    return router
