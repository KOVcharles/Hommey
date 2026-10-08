// Exercise the actual chat handler with gated byte streams and a small DOM double.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { setImmediate: nextFrame } = require('node:timers/promises');

const source = readFileSync(resolve(__dirname, '../webui_new/static/app.js'), 'utf8');
function extract(name, nextName) {
    const start = source.indexOf(`    ${name}`);
    assert.ok(start >= 0);
    const end = nextName ? source.indexOf(`\n    function ${nextName}`, start) : source.indexOf('\n    function ', start + 5);
    assert.ok(end > start);
    return source.slice(start, end);
}

function fixture() {
    const rows = [];
    const runtime = {
        id: 'session', requestId: 'request', processing: false, draft: { text: '', attachments: [] },
        container: { querySelector: () => null, appendChild: row => rows.push(row) },
    };
    function shell(role) {
        const stack = { appendChild: bubble => { stack.bubble = bubble; }, remove: () => { throw new Error('Must remove the whole message row'); } };
        return { role, stack, querySelector: () => stack, remove() { this.removed = true; } };
    }
    function addMessage(target, role, text) {
        const row = shell(role);
        row.stack.bubble = { text };
        target.container.appendChild(row);
    }
    addMessage(runtime, 'ai', '已保存的上一轮回答');
    let controller;
    const body = new ReadableStream({ start(c) { controller = c; } });
    const noop = () => {};
    const context = vm.createContext({
        Response, TextDecoder, clearTimeout, userId: 'user', retrievalMode: 'standard', toastTimer: null,
        chatInput: { value: '', focus: noop }, sendBtn: {}, toast: { classList: { remove: noop } },
        document: { createElement: () => ({}) },
        window: { HommeySessionRuntime: { canStart: () => true, ensure: () => runtime, mount: noop, active: () => runtime } },
        authFetch: async () => new Response(body, { headers: { 'Content-Type': 'application/x-ndjson' } }),
        activeAttachments: () => [], activeSessionId: () => runtime.id, ensureActiveSession: async () => {},
        setActiveSession: noop, syncComposerToActive: noop, draftSlot: () => runtime.draft,
        enterChatView: noop, resizeInput: noop, renderPendingAttachments: noop, setSendLoading: noop,
        setRetrievalModeControlsDisabled: noop, showProcessingIndicator: noop, removeProcessingIndicator: noop,
        collapseTripIntakeCards: noop, ensureRequestId: () => runtime.requestId, showToast: noop,
        renderMessageInto: (bubble, text) => { bubble.text = text; }, scrollToBottom: noop,
        createMessageShell: shell, addMessage,
        addAnswerMessage: (target, document) => addMessage(target, 'ai', document.plain_text),
        addPresentationMessage: (target, document) => addMessage(target, 'ai', document.plain_text),
        parseStreamLine: line => line.trim() ? JSON.parse(line) : null,
        createApiError: data => Object.assign(new Error(data.message), data),
        formatDisplayError: error => error.message, ApiError: Error,
        setSessionPlaceholder: noop, loadActiveTrip: async () => {}, loadUserSummary: async () => {},
        isActiveProcessing: () => runtime.processing, refreshSessionList: noop,
        resetRequestId: () => { runtime.requestId = null; },
    });
    vm.runInContext(extract('async function sendMessage(', 'showSubmissionRetry') + '\n' + extract('function createStreamingMessage('), context);
    return {
        runtime,
        context,
        start: () => context.sendMessage('解释报销材料', { includeAttachments: false }),
        event: event => controller.enqueue(new TextEncoder().encode(JSON.stringify(event) + '\n')),
        close: () => controller.close(),
        visible: () => rows.filter(row => !row.removed && row.role === 'ai').map(row => row.stack.bubble.text),
    };
}

test('the browser handler paints the first delta while the request is still open', async () => {
    const f = fixture();
    const request = f.start();
    f.event({ type: 'chunk', text: '第一段' });
    await nextFrame();
    assert.deepEqual(f.visible(), ['已保存的上一轮回答', '第一段']);
    assert.equal(f.runtime.processing, true);
    f.event({ type: 'chunk', text: '第二段' });
    f.event({ type: 'done' });
    f.close();
    assert.equal(await request, true);
    assert.deepEqual(f.visible(), ['已保存的上一轮回答', '第一段第二段']);
});

test('tool reset removes the whole draft row and preserves previous messages', async () => {
    const f = fixture();
    const request = f.start();
    f.event({ type: 'chunk', text: '暂时的草稿' });
    f.event({ type: 'response_reset' });
    f.event({ type: 'chunk', text: '完整最终回答' });
    f.event({ type: 'done' });
    f.close();
    assert.equal(await request, true);
    assert.deepEqual(f.visible(), ['已保存的上一轮回答', '完整最终回答']);
});

test('EOF without done retracts the draft and preserves the request for retry', async () => {
    const f = fixture();
    const request = f.start();
    f.event({ type: 'chunk', text: '截断正文' });
    f.close();
    assert.equal(await request, false);
    assert.deepEqual(f.visible(), ['已保存的上一轮回答', '回复未完成，连接已中断，请重试']);
    assert.equal(f.runtime.requestId, 'request');
    assert.equal(f.runtime.retryRequestPending, true);
    assert.equal(f.runtime.draft.text, '解释报销材料');
});

test('an upstream error retracts the incomplete prose before showing the error', async () => {
    const f = fixture();
    const request = f.start();
    f.event({ type: 'chunk', text: '未完成正文' });
    f.event({ type: 'error', message: '模型连接中断', retryable: true });
    f.close();
    assert.equal(await request, false);
    assert.deepEqual(f.visible(), ['已保存的上一轮回答', '模型连接中断']);
});

test('interruption removes the draft and displays the existing stopped message', async () => {
    const f = fixture();
    const request = f.start();
    f.event({ type: 'chunk', text: '未完成正文' });
    f.event({ type: 'interrupted' });
    f.event({ type: 'done', interrupted: true });
    f.close();
    assert.equal(await request, true);
    assert.equal(f.visible().length, 2);
    assert.match(f.visible()[1], /已停止当前执行/);
});

test('a validated card replaces provisional prose without an empty message row', async () => {
    const f = fixture();
    const request = f.start();
    f.event({ type: 'chunk', text: '未完成正文' });
    f.event({ type: 'response_reset' });
    f.event({ type: 'answer_document', document: { plain_text: '完整的回答卡片' } });
    f.event({ type: 'done' });
    f.close();
    assert.equal(await request, true);
    assert.deepEqual(f.visible(), ['已保存的上一轮回答', '完整的回答卡片']);
});

test('failure to refresh a sidebar after done does not retract a completed answer', async () => {
    const f = fixture();
    f.context.loadActiveTrip = async () => { throw new Error('侧边栏刷新失败'); };
    const request = f.start();
    f.event({ type: 'chunk', text: '完整最终回答' });
    f.event({ type: 'done' });
    f.close();
    await request;
    assert.deepEqual(f.visible(), ['已保存的上一轮回答', '完整最终回答', '侧边栏刷新失败']);
});
