"""Directory maintenance files never become policy evidence."""
import pytest

from rag.config import RAGPipelineConfig
from rag.pipeline import RAGPipeline
from rag.vector_store import InMemoryVectorStore
from webui_new.core.errors import BusinessError
from webui_new.knowledge_base_service import KnowledgeBaseManagementService
from webui_new.routes.knowledge_base import KnowledgeBaseLibrary


def test_source_listing_snapshot_and_ingestion_exclude_readme(tmp_path):
    root = tmp_path / "documents"
    branch = root / "cqu/finance"
    branch.mkdir(parents=True)
    (root / "README.md").write_text("# 维护说明\n过时的演示金额是 500 元。", encoding="utf-8")
    (branch / "ReadMe.txt").write_text("目录说明不作为制度。", encoding="utf-8")
    (branch / ".scratch.md").write_text("临时资料。", encoding="utf-8")
    (branch / "policy.md").write_text("# 报销制度\n电子票据需查重。", encoding="utf-8")
    config = RAGPipelineConfig(documents_dir=str(root), knowledge_base_path=str(tmp_path / "kb"))
    service = KnowledgeBaseManagementService(root, config.knowledge_base_path, config=config)
    library = KnowledgeBaseLibrary(root)
    expected = "cqu/finance/policy.md"
    assert [entry["document_id"] for entry in service.source_snapshot()] == [expected]
    assert [entry["id"] for entry in library.list_documents()] == [expected]
    with pytest.raises(BusinessError, match="文档不存在"):
        library.get_document("README.md")

    store = InMemoryVectorStore()
    pipeline = RAGPipeline(config=config, vector_store=store)
    try:
        report = pipeline.ingest(root, rebuild=True)
        assert report.status == "success" and report.documents_loaded == 1
        assert {chunk.document_id for chunk in store.rows} == {expected}
        manifest = service._write_manifest(report.to_dict())
        assert set(manifest["documents"]) == {expected}
    finally:
        pipeline.close()


@pytest.mark.parametrize("filename", ["README.md", "readme.txt", "ReadMe.pdf"])
def test_reserved_directory_documentation_cannot_be_uploaded(tmp_path, filename):
    service = KnowledgeBaseManagementService(tmp_path / "documents", tmp_path / "kb")
    with pytest.raises(BusinessError, match="README"):
        service.upload(filename, b"maintenance instructions")
    assert not list((tmp_path / "documents").rglob("*"))
