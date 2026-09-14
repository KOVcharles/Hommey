"""Exercise the production chat page with deterministic HTTP fixtures in Edge."""
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Template
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.codex-resume-work/chat-polish-qa'
OUT.mkdir(parents=True, exist_ok=True)
CARD = {'type': 'trip_intake', 'title': '一起完善这一程', 'summary': '补充出差目的，即可继续安排。',
        'route': {'origin': '北京', 'destination': '重庆'}, 'progress': {'completed': 4, 'total': 5},
        'collected': [{'key': 'origin', 'label': '出发地', 'value': '北京'},
                      {'key': 'destination', 'label': '目的地', 'value': '重庆'}],
        'missing_required': [{'key': 'trip_purpose', 'label': '出差目的', 'input_type': 'choice',
                              'help_text': '这次出差主要安排什么工作', 'options': ['客户拜访', '参加会议']}],
        'interaction_id': 'source'}
history = [{'role': 'user', 'content': '帮我安排北京到重庆的出差'},
           {'role': 'assistant', 'request_id': 'source', 'presentation_document': CARD}]
requests = []
fail_next = False


def route_request(route):
    global fail_next
    path = urlparse(route.request.url).path
    if path.startswith('/static/'):
        file = ROOT / 'webui_new' / path.lstrip('/')
        if file.is_file():
            route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
        else:
            route.fulfill(status=404)
        return
    if path == '/chat/qa':
        html = Template((ROOT / 'webui_new/templates/chat.html').read_text(encoding='utf-8')).render(user_id='qa')
        route.fulfill(body=html, content_type='text/html')
        return
    if path.endswith('/chat/stream'):
        payload = route.request.post_data_json
        requests.append(payload)
        rid = payload['client_request_id']
        events = [{'type': 'execution_plan', 'run_id': rid, 'revision': 1, 'status': 'running', 'steps': []},
                  {'type': 'execution_plan', 'run_id': rid, 'revision': 3, 'status': 'completed', 'steps': [
                      {'title': '整理并保存出差信息', 'status': 'succeeded'}]},
                  {'type': 'chunk', 'text': '行程信息已更新，可以继续安排交通和住宿。'}, {'type': 'done'}]
        if fail_next:
            fail_next = False
            events = [events[0], {'type': 'error', 'error': {'code': 'UPSTREAM_FAILED', 'message': '暂时无法完成', 'retryable': True}}]
        route.fulfill(body='\n'.join(json.dumps(event, ensure_ascii=False) for event in events) + '\n',
                      content_type='application/x-ndjson')
        return
    data = {}
    if path.endswith('/status'): data = {'initialized': True}
    elif path.endswith('/is-new'): data = {'is_new': False}
    elif path.endswith('/sessions'): data = {'active_session_id': 'session', 'sessions': [{'session_id': 'session', 'title': '重庆出差'}]}
    elif path.endswith('/activate'): data = {'messages': history}
    elif path.endswith('/execution-plans'): data = {'plans': []}
    route.fulfill(json=data)


with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True, args=['--disable-gpu'])
    context = browser.new_context(viewport={'width': 1180, 'height': 950})
    context.add_init_script("localStorage.setItem('hommey.access_token', 'e30.' + btoa(JSON.stringify({sub:'qa'})) + '.qa'); localStorage.setItem('hommey.theme','light');")
    context.route('**/*', route_request)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://hommey.test/chat/qa')
    page.locator('#sidebarToggle').click()
    page.get_by_role('button', name='重庆出差', exact=True).click()
    expect(page.locator('.trip-intake-submit')).to_be_disabled()
    page.get_by_role('button', name='客户拜访', exact=True).click()
    page.locator('#chatInput').fill('保留这段草稿')
    page.locator('.trip-intake-submit').click()
    expect(page.locator('.trip-intake-card')).to_have_attribute('data-archived', 'true')
    expect(page.locator('#chatInput')).to_have_value('保留这段草稿')
    expect(page.locator('.message-row.user')).to_have_count(1)
    assert requests[-1]['intake_request_id'] == 'source'
    assert requests[-1]['message'] == '出差目的：客户拜访'
    expect(page.locator('.execution-plan .hommey-card-logo')).to_be_visible()
    expect(page.locator('.message-row.ai .msg-avatar').last).to_be_visible()
    page.locator('.trip-intake-archive-heading').click()
    expect(page.locator('.trip-intake-card button, .trip-intake-card input')).to_have_count(0)
    expect(page.locator('.trip-intake-archive-body')).to_contain_text('客户拜访')
    page.locator('.execution-plan-heading').click()
    expect(page.locator('.execution-plan-step')).to_have_count(1)
    page.screenshot(path=str(OUT / 'desktop.png'), full_page=True, animations='disabled')
    for width, theme in [(390, 'light'), (390, 'dark')]:
        page.set_viewport_size({'width': width, 'height': 844})
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('#chatMessages').bounding_box()['x'] >= 0
        page.screenshot(path=str(OUT / f'mobile-{theme}.png'), full_page=True, animations='disabled')
    # Restore a hidden submission: no raw text bubble, no reactivated old form.
    history.append({'role': 'user', 'content': requests[-1]['message'], 'content_type': 'trip_submission'})
    page.locator('#sidebarToggle').click()
    page.get_by_role('button', name='重庆出差', exact=True).click()
    expect(page.locator('.message-row.user')).to_have_count(1)
    expect(page.locator('.trip-intake-card')).to_have_attribute('data-archived', 'true')
    # A normal new message also archives the current card, before the next answer.
    history.pop()
    page.locator('#sidebarToggle').click()
    page.get_by_role('button', name='重庆出差', exact=True).click()
    expect(page.locator('.trip-intake-submit')).to_be_visible()
    # A durable submission can fail after acceptance. Retry its exact body/id,
    # while the archived card remains read-only and the composer draft survives.
    fail_next = True
    page.get_by_role('button', name='客户拜访', exact=True).click()
    page.locator('#chatInput').fill('稍后再安排返程')
    page.locator('.trip-intake-submit').click()
    expect(page.locator('.submission-retry')).to_be_visible()
    failed = requests[-1]
    page.locator('.submission-retry button').click()
    expect(page.locator('.submission-retry')).to_have_count(0)
    expect(page.locator('#chatInput')).to_have_value('稍后再安排返程')
    expect(page.locator('.execution-plan-title')).to_have_text('已为你整理好')
    assert requests[-1] == failed
    expect(page.locator('.trip-intake-card button, .trip-intake-card input')).to_have_count(0)
    # Start a fresh current card to verify ordinary messages expire it too.
    page.locator('#sidebarToggle').click()
    page.get_by_role('button', name='重庆出差', exact=True).click()
    page.locator('#chatInput').fill('先帮我查天气')
    page.locator('#sendBtn').click()
    expect(page.locator('.trip-intake-card')).to_have_attribute('data-archived', 'true')
    page.emulate_media(reduced_motion='reduce')
    assert page.locator('.execution-plan').evaluate('(el) => getComputedStyle(el).animationName') == 'none'
    # Drive an actual streaming response and let the user scroll away mid-reply.
    page.evaluate("""() => {
        const original = window.fetch;
        window.fetch = (url, options) => String(url).endsWith('/chat/stream')
            ? Promise.resolve(new Response(new ReadableStream({start(controller) { window.qaStream = controller; }})))
            : original(url, options);
        window.emitQA = event => qaStream.enqueue(new TextEncoder().encode(JSON.stringify(event) + '\\n'));
    }""")
    page.locator('#chatInput').fill('继续安排行程')
    page.locator('#sendBtn').click()
    expect(page.locator('#processingIndicator .msg-avatar')).to_be_visible()
    page.wait_for_function('window.qaStream')
    page.evaluate("emitQA({type:'execution_plan',run_id:'stream-qa',revision:1,status:'running',steps:[]})")
    expect(page.locator('#processingIndicator')).to_have_count(0)
    expect(page.locator('.execution-plan').last.locator('.hommey-card-logo')).to_be_visible()
    page.screenshot(path=str(OUT / 'waiting-mobile.png'), full_page=True, animations='disabled')
    page.evaluate("emitQA({type:'chunk',text:'行程安排与会议准备。\\n'.repeat(150)})")
    expect(page.locator('.msg-bubble.ai').last).to_contain_text('会议准备')
    page.locator('#chatMessages').evaluate("el => el.scrollTo({top:0,behavior:'instant'})")
    page.wait_for_timeout(80)
    page.evaluate("emitQA({type:'chunk',text:'继续准备交通。'})")
    page.wait_for_timeout(80)
    assert page.locator('#chatMessages').evaluate('el => el.scrollTop') < 96
    page.evaluate("emitQA({type:'done'}); qaStream.close()")
    assert not errors, errors
    browser.close()
print(f'Production chat UI checks passed. Screenshots: {OUT}')
