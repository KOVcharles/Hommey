from __future__ import annotations

import pytest

from multimodal.document_ocr_client import DocumentOcrClient, DocumentOcrError


class _Response:
    status_code = 200

    def json(self):
        return {
            "choices": [
                {"message": {"content": "```markdown\n# 标题\n\n正文\n```"}}
            ]
        }


def test_document_ocr_client_uses_deepseek_native_prompt_and_returns_markdown(monkeypatch):
    captured = {}

    class _Client:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, url, *, json, headers):
            captured.update(url=url, payload=json, headers=headers)
            return _Response()

    monkeypatch.setattr("multimodal.document_ocr_client.httpx.Client", _Client)
    client = DocumentOcrClient(
        enabled=True,
        api_key="test-key",
        base_url="https://ocr.example/v1",
        model="deepseek-ai/DeepSeek-OCR",
        timeout_sec=12,
        max_retries=0,
    )

    result = client.ocr_page(b"png", filename="scan.pdf", page_number=2)

    assert result.text == "# 标题\n\n正文"
    assert captured["url"] == "https://ocr.example/v1/chat/completions"
    assert captured["timeout"] == 12
    assert len(captured["payload"]["messages"]) == 1
    content = captured["payload"]["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["text"] == "<image>\n<|grounding|>Convert the document to markdown."
    assert "response_format" not in captured["payload"]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"enabled": False, "api_key": "key"}, "OCR_DISABLED"),
        ({"enabled": True, "api_key": ""}, "OCR_NOT_CONFIGURED"),
    ],
)
def test_document_ocr_client_validates_configuration(kwargs, code):
    client = DocumentOcrClient(max_retries=0, **kwargs)
    with pytest.raises(DocumentOcrError) as exc:
        client.ocr_page(b"png")
    assert exc.value.code == code


def test_document_ocr_client_rejects_empty_response(monkeypatch):
    class _EmptyResponse(_Response):
        def json(self):
            return {"choices": [{"message": {"content": ""}}]}

    class _Client:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, *_args, **_kwargs):
            return _EmptyResponse()

    monkeypatch.setattr("multimodal.document_ocr_client.httpx.Client", _Client)
    client = DocumentOcrClient(enabled=True, api_key="key", max_retries=0)

    with pytest.raises(DocumentOcrError) as exc:
        client.ocr_page(b"png")

    assert exc.value.code == "OCR_EMPTY_RESULT"
