"""多模态输入 P0：normalize（agent_query/display_message 分离）与附件绑定的单元测试。

用 stub repository 规避真实 Postgres，验证方案 §4.5 的核心约束：
- agent_query 含附件上下文、被不可信边界包裹；display_message 只含文件名清单。
- 归属/未就绪附件被拒绝；附件绑定到 message id。
"""
import pytest

from context.long_term_memory import FileLongTermMemory
from multimodal import context_builder
from multimodal.schemas import (
    ATTACHMENT_STATUS_READY,
    Attachment,
    Extraction,
)
from multimodal.service import AttachmentService
from webui_new.core.errors import BusinessError


def _attachment(aid, user_id="u1", status=ATTACHMENT_STATUS_READY, text="正文内容"):
    return Attachment(
        id=aid, user_id=user_id, filename=f"{aid}.docx", mime_type="x", kind="document",
        size_bytes=10, object_key=f"u1/{aid}", status=status,
    ), Extraction(attachment_id=aid, content_text=text, char_count=len(text), structured={"pages": [1, 2]})


class _StubRepo:
    """记录调用的内存 repository，供 AttachmentService.normalize/bind 使用。"""

    def __init__(self, attachments, extractions, user_id="u1"):
        self._att = {a.id: a for a in attachments}
        self._ext = {e.attachment_id: e for e in extractions}
        self._user_id = user_id
        self.binds = []  # (message_id, [ids], user_id)

    def get_many(self, ids, user_id):
        return [a for a in self._att.values() if a.id in set(ids) and a.user_id == user_id]

    def get_extraction(self, aid):
        return self._ext.get(aid)

    def bind(self, message_id, ids, user_id):
        self.binds.append((message_id, list(ids), user_id))
        return len(ids)


# ── context_builder ───────────────────────────────────────────────

def test_display_message_keeps_user_text_and_filename_only():
    att, _ = _attachment("att_1")
    msg = context_builder.build_display_message("总结住宿报销限制", [att])
    assert "总结住宿报销限制" in msg
    assert "att_1.docx" in msg
    # 清单里绝不出现附件正文
    assert "正文内容" not in msg


def test_agent_query_contains_attachment_text_and_untrusted_boundary():
    att, ext = _attachment("att_1", text="住宿每天上限 500 元")
    q, sources, warnings = context_builder.build_agent_query("总结限制", [(att, ext)])
    assert "住宿每天上限 500 元" in q
    assert "不可信内容" in q  # 提示注入隔离边界
    assert len(sources) == 1
    assert sources[0].page_count == 2


def test_agent_query_truncates_on_budget(monkeypatch):
    monkeypatch.setattr(context_builder, "ATTACHMENT_CONFIG", {"agent_query_char_budget": 20})
    att, ext = _attachment("att_1", text="A" * 500)
    q, sources, warnings = context_builder.build_agent_query("问", [(att, ext)])
    assert "截断" in " ".join(warnings)
    assert len(q) < 500


# ── AttachmentService.normalize ───────────────────────────────────

def test_normalize_separates_agent_query_and_display_message():
    a1, e1 = _attachment("att_1", text="住宿 500/天")
    a2, e2 = _attachment("att_2", text="交通 高铁二等座")
    service = AttachmentService(repository=_StubRepo([a1, a2], [e1, e2]))

    normalized = service.normalize("总结这两份", ["att_1", "att_2"], "u1")

    assert "住宿 500/天" in normalized.agent_query
    assert "交通 高铁二等座" in normalized.agent_query
    # display_message 只含文件名，绝不含正文
    assert "att_1.docx" in normalized.display_message
    assert "住宿 500/天" not in normalized.display_message
    assert len(normalized.sources) == 2


def test_normalize_rejects_not_ready_attachment():
    a1, e1 = _attachment("att_1", status="processing")
    service = AttachmentService(repository=_StubRepo([a1], [e1]))
    with pytest.raises(BusinessError):
        service.normalize("问", ["att_1"], "u1")


def test_normalize_rejects_foreign_attachment():
    a1, e1 = _attachment("att_1", user_id="u1")
    service = AttachmentService(repository=_StubRepo([a1], [e1]))
    # 请求者声明 att_1 但查的是 other_user —— stub 按 user_id 过滤后返回空 → NOT_FOUND
    with pytest.raises(BusinessError):
        service.normalize("问", ["att_1"], "other_user")


def test_normalize_without_attachments_returns_plain_text():
    service = AttachmentService(repository=_StubRepo([], []))
    normalized = service.normalize("你好", [], "u1")
    assert normalized.agent_query == "你好"
    assert normalized.display_message == "你好"


def test_bind_forwards_to_repository():
    repo = _StubRepo([], [])
    service = AttachmentService(repository=repo)
    service.bind(42, ["att_1", "att_2"], "u1")
    assert repo.binds == [(42, ["att_1", "att_2"], "u1")]


# ── 记忆层：add_chat_message 返回 id，且 get_chat_history 带回 id ──

def test_file_memory_returns_message_id_and_history_carries_id(tmp_path):
    memory = FileLongTermMemory("u1", storage_path=str(tmp_path))
    mid = memory.add_chat_message("user", "hello", "s1", {"request_id": "r1"})
    assert mid and mid is not False
    rows = memory.get_chat_history(session_id="s1")
    assert rows and rows[0].get("id") == mid
