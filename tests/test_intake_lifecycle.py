"""A saved conversation, rather than browser state, owns intake validity."""
import asyncio
from types import SimpleNamespace

import pytest

from context.long_term_memory import FileLongTermMemory
from webui_new.manager import HommeyWebInstance
from webui_new.core.errors import BusinessError


def instance_with(rows):
    instance = HommeyWebInstance('employee')
    instance.initialized = True
    instance.memory_manager = SimpleNamespace(long_term=SimpleNamespace(
        get_chat_history=lambda **kwargs: list(rows), get_chat_session_titles=lambda: {}))
    return instance


def source():
    return {'role': 'assistant', 'request_id': 'source', 'content': '补充行程',
            'presentation_document': {'type': 'trip_intake', 'interaction_id': 'source'}}


def test_card_expires_on_any_later_message_and_cannot_write():
    instance = instance_with([source(), {'role': 'user', 'request_id': 'later', 'content': '先查天气'}])
    with pytest.raises(BusinessError, match='归档'):
        asyncio.run(instance._process_message_impl('从北京出发', request_id='new', intake_request_id='source', session_id='session', request_memory=instance.memory_manager))


def test_current_card_and_exact_retry_allowed_but_changed_payload_rejected():
    instance = instance_with([source()])
    instance._validate_intake_submission('source', 'submit', '从北京出发', 'session')
    submitted = {'role': 'user', 'request_id': 'submit', 'content': '从北京出发', 'content_type': 'trip_submission'}
    instance = instance_with([source(), submitted])
    instance._validate_intake_submission('source', 'submit', '从北京出发', 'session')
    with pytest.raises(BusinessError):
        instance._validate_intake_submission('source', 'submit', '从上海出发', 'session')
    with pytest.raises(BusinessError):
        instance._validate_intake_submission('source', 'new', '从北京出发', 'session')


def test_foreign_or_unknown_card_rejected():
    with pytest.raises(BusinessError):
        instance_with([source()])._validate_intake_submission('foreign', 'new', '从北京出发', 'session')


def test_history_restores_archive_and_submission_marker_without_losing_content(tmp_path):
    memory = FileLongTermMemory('employee', storage_path=str(tmp_path))
    memory.add_chat_message('assistant', '补充行程', 'session',
                            {'request_id': 'source', 'presentation_document': source()['presentation_document']})
    memory.add_chat_message('user', '从北京出发', 'session',
                            {'request_id': 'submit', 'content_type': 'trip_submission'})
    restored = FileLongTermMemory('employee', storage_path=str(tmp_path))
    rows = restored.get_chat_history(session_id='session')
    assert rows[-1]['content'] == '从北京出发'
    assert rows[-1]['content_type'] == 'trip_submission'
    result = instance_with(rows).get_chat_session('session')
    assert result['messages'][0]['presentation_document']['archived'] is True
    assert result['messages'][0]['presentation_document']['interaction_id'] == 'source'


def test_hidden_submission_still_reaches_memory_and_supervisor():
    instance = instance_with([source()])
    saved = []
    instance.memory_manager.add_message = lambda role, text, metadata: saved.append((role, text, metadata)) or 'id'
    async def run(scope, text, **kwargs):
        assert text == '从北京出发'
        return {'response': '收到'}
    instance.supervisor = SimpleNamespace(run=run)
    asyncio.run(instance._process_message_impl('从北京出发', request_id='new', intake_request_id='source', session_id='session', request_memory=instance.memory_manager))
    assert saved[0][1] == '从北京出发'
    assert saved[0][2]['content_type'] == 'trip_submission'
    assert saved[1][2]['content_type'] == 'text'


def test_unidentifiable_legacy_card_is_read_only():
    row = source()
    row.pop('request_id')
    row['presentation_document'].pop('interaction_id')
    assert instance_with([row]).get_chat_session('session')['messages'][0]['presentation_document']['archived']


@pytest.mark.parametrize('suffix', ['chat', 'chat/stream'])
def test_both_http_entrypoints_forward_card_ownership(suffix):
    import httpx
    from fastapi import FastAPI
    from webui_new.auth import require_path_user
    from webui_new.routes.chat import create_chat_router

    calls = []
    class Manager:
        async def process_message(self, user_id, message, **kwargs):
            calls.append(kwargs)
            return {'response': 'ok'}

        async def stream_message(self, user_id, message, **kwargs):
            calls.append(kwargs)
            yield {'type': 'done'}

    async def exercise():
        app = FastAPI()
        app.dependency_overrides[require_path_user] = lambda: object()
        app.include_router(create_chat_router(Manager()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.post('/api/employee/' + suffix, json={
                'message': '从北京出发', 'session_id': 'session', 'intake_request_id': 'source'})

    assert asyncio.run(exercise()).status_code == 200
    assert calls[0]['intake_request_id'] == 'source'
    assert calls[0]['session_id'] == 'session'
