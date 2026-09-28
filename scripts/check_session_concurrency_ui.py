"""Headless UI regression for concurrent sessions; APIs are deterministic stubs, no LLM calls.

Run: python scripts/check_session_concurrency_ui.py
Requires Playwright and Microsoft Edge. Backend overlap and isolation are
covered separately by tests/test_concurrent_sessions_integration.py.

Two scenarios:
  1. two tabs — independent conversations, list failure/retry, refresh retention
  2. one page — several conversations in flight at once, switching between them
"""
import asyncio
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from jinja2 import Template
from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
BASE = "http://hommey.test"
RUNNING = re.compile(r"\bis-running\b")


class Backend:
    """Deterministic stubs. Every chat stream is held open on its own gate, so the
    test decides exactly when each run finishes — that is what makes overlap
    observable without a real model."""

    def __init__(self):
        self.sessions = [{"session_id": "older", "title": "已有会话"}]
        self.streams = []        # [{"session_id": str, "gate": asyncio.Event}]
        self.activations = []    # session ids whose history was fetched
        self.trip_sessions = []
        self.fail_list = False
        self._mark = 0

    def mark(self):
        """Start a scenario: only streams opened after this point belong to it."""
        self._mark = len(self.streams)

    def scenario_streams(self):
        return self.streams[self._mark:]

    async def route(self, route):
        url = urlparse(route.request.url)
        path, method = url.path, route.request.method
        if path.startswith("/static/"):
            file = ROOT / "webui_new" / path.lstrip("/")
            await route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(str(file))[0] or "application/octet-stream")
        elif path == "/chat/qa":
            html = Template((ROOT / "webui_new/templates/chat.html").read_text(encoding="utf-8")).render(user_id="qa")
            await route.fulfill(body=html, content_type="text/html")
        elif path.endswith("/sessions"):
            if method == "POST":
                sid = "session-" + str(len(self.sessions))
                self.sessions.append({"session_id": sid, "title": sid})
                await route.fulfill(json={"session_id": sid})
            elif self.fail_list:
                await route.fulfill(status=503, json={"error": {"code": "UNAVAILABLE", "message": "暂时不可用"}})
            else:
                await route.fulfill(json={"sessions": self.sessions})
        elif path.endswith("/activate"):
            self.activations.append(path.split("/sessions/")[1].split("/")[0])
            await route.fulfill(json={"messages": [{"role": "user", "content": "历史内容"}]})
        elif path.endswith("/execution-plans"):
            await route.fulfill(json={"plans": []})
        elif path.endswith("/trip/active"):
            sid = parse_qs(url.query)["session_id"][0]
            self.trip_sessions.append(sid)
            await route.fulfill(json={"active_trip": {"destination": sid}})
        elif path.endswith("/chat/stream"):
            stream = {"session_id": route.request.post_data_json["session_id"], "gate": asyncio.Event()}
            self.streams.append(stream)
            await stream["gate"].wait()
            await route.fulfill(body=json.dumps({"type": "done"}) + "\n", content_type="application/x-ndjson")
        elif path.endswith("/status"):
            await route.fulfill(json={"initialized": True})
        elif path.endswith("/is-new"):
            await route.fulfill(json={"is_new": False})
        else:
            await route.fulfill(json={})

    async def wait_streams(self, count, timeout=5):
        async def poll():
            while len(self.scenario_streams()) < count:
                await asyncio.sleep(0.02)
        try:
            await asyncio.wait_for(poll(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError(
                f"expected {count} streams in this scenario, saw {[s['session_id'] for s in self.scenario_streams()]}"
            )
        return self.scenario_streams()

    def in_flight(self):
        return [s["session_id"] for s in self.scenario_streams() if not s["gate"].is_set()]

    def release(self, index):
        self.scenario_streams()[index]["gate"].set()

    def release_all(self):
        for stream in self.streams:
            stream["gate"].set()


async def send_in_composer(page, text):
    """Type into whichever composer is on screen and submit it."""
    composer = page.locator("#homeInput")
    await expect(composer).to_be_enabled()
    await composer.fill(text)
    await composer.press("Enter")


async def run_two_tabs(context, backend, errors):
    backend.mark()
    a, b = await context.new_page(), await context.new_page()
    for page in (a, b):
        page.on("pageerror", lambda error: errors.append(str(error)))
    await a.goto(BASE + "/chat/qa")
    await send_in_composer(a, "窗口 A 的任务")
    await backend.wait_streams(1)
    await b.goto(BASE + "/chat/qa")
    await b.locator("#sidebarToggle").click()
    await expect(b.get_by_role("button", name="已有会话", exact=True)).to_be_visible()
    await b.locator("#newChatButton").click()
    await b.wait_for_function("sessionStorage.getItem('hommey.session.qa') === 'session-2'")
    await expect(b.locator("#activeTrip")).to_contain_text("session-2")
    await send_in_composer(b, "窗口 B 的任务")
    await backend.wait_streams(2)
    assert backend.in_flight() == ["session-1", "session-2"]
    print("PASS: A remains in flight while B lists history, creates a conversation and sends independently")
    backend.release(0)
    backend.release(1)
    await expect(b.locator("#sendBtn")).to_be_enabled()
    # A failed list must show a retryable load state, not empty history.
    backend.fail_list = True
    await b.reload()
    await b.locator("#sidebarToggle").click()
    await expect(b.locator(".session-list-error")).to_contain_text("暂时无法加载历史会话")
    await expect(b.locator("#historyList")).not_to_contain_text("还没有历史会话")
    assert await b.evaluate("sessionStorage.getItem('hommey.session.qa')") == "session-2"
    backend.fail_list = False
    await b.locator(".session-list-error").get_by_role("button", name="重试").click()
    await expect(b.get_by_role("button", name="已有会话", exact=True)).to_be_visible()
    assert "session-2" in backend.trip_sessions
    assert not errors, errors
    print("PASS: session-scoped trip queries, list failure/retry, refresh retention; no JavaScript errors")
    await a.close()
    await b.close()


async def run_one_page_parallel(context, backend, errors):
    """The whole point: one page, several runs, none of them interrupting the others."""
    backend.mark()
    page = await context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(BASE + "/chat/qa")

    # 1. 会话 A 发消息，流挂住不放。
    await send_in_composer(page, "并行 A 的任务")
    await backend.wait_streams(1)
    assert backend.in_flight() == ["session-3"]
    # 记下 A 的画布节点：切回来时必须是同一个，而不是重新拉的。
    await page.evaluate("window.__aView = document.querySelector('.session-view')")
    assert await page.evaluate("window.__aView.dataset.sessionId") == "session-3"

    # 2. A 还在跑的时候新建会话 B。旧行为会在这里被 isProcessing 挡掉。
    await page.locator("#sidebarToggle").click()
    await page.locator("#newChatButton").click()
    await page.wait_for_function("sessionStorage.getItem('hommey.session.qa') === 'session-4'")
    await send_in_composer(page, "并行 B 的任务")
    await backend.wait_streams(2)

    # 3. 两个流同时在跑。
    assert backend.in_flight() == ["session-3", "session-4"]
    # 收起的会话只是隐藏，画布仍然连着文档——卡片脚本用 isConnected 判断生死
    # （journey-map.js 一脱离就 dispose 且不再复活），真摘下来会让切回来时画不出东西。
    assert await page.evaluate("document.contains(window.__aView)")
    assert not await page.evaluate("window.__aView.classList.contains('is-active')")
    print("PASS: a second conversation starts while the first is still streaming")

    # 4. 切回 A：画布是原来那一个，处理指示还在，也没有重拉历史。
    await page.locator("#sidebarToggle").click()
    await page.get_by_role("button", name="session-3", exact=True).click()
    await page.wait_for_function("document.querySelector('.session-view')?.dataset.sessionId === 'session-3'")
    assert await page.evaluate("window.__aView === document.querySelector('.session-view')")
    assert await page.evaluate("!!window.__aView.querySelector('.processing-indicator')")
    assert "session-3" not in backend.activations, backend.activations
    print("PASS: switching back shows the live canvas again without replaying history")

    # 5. 三个并发，第三次新建仍然可用。
    await page.locator("#sidebarToggle").click()
    await page.locator("#newChatButton").click()
    await page.wait_for_function("sessionStorage.getItem('hommey.session.qa') === 'session-5'")
    await send_in_composer(page, "并行 C 的任务")
    await backend.wait_streams(3)
    assert backend.in_flight() == ["session-3", "session-4", "session-5"]

    # 6. 第四个：立即提示，不发请求。
    await page.locator("#sidebarToggle").click()
    await page.locator("#newChatButton").click()
    await page.wait_for_function("sessionStorage.getItem('hommey.session.qa') === 'session-6'")
    await send_in_composer(page, "并行 D 的任务")
    await expect(page.locator("#toast")).to_contain_text("最多同时跑 3 个会话")
    assert len(backend.scenario_streams()) == 3, [s["session_id"] for s in backend.scenario_streams()]
    print("PASS: the fourth concurrent run is refused up front instead of queuing behind the global semaphore")

    # 7. A 跑完：它的运行指示消失，B、C 不受影响。
    backend.release(0)
    await page.locator("#sidebarToggle").click()
    s3 = page.locator('.session-row[data-session-id="session-3"]')
    await expect(s3).not_to_have_class(RUNNING)
    await expect(page.locator('.session-row[data-session-id="session-4"]')).to_have_class(RUNNING)
    await expect(page.locator('.session-row[data-session-id="session-5"]')).to_have_class(RUNNING)
    assert backend.in_flight() == ["session-4", "session-5"]
    print("PASS: a finished background run clears only its own indicator")

    assert not errors, errors
    print("PASS: no JavaScript errors across in-page parallel sessions")
    backend.release_all()
    await page.close()


async def main():
    backend = Backend()
    errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        context = await browser.new_context(viewport={"width": 1180, "height": 950})
        await context.add_init_script("localStorage.setItem('hommey.access_token', 'e30.' + btoa(JSON.stringify({sub:'qa'})) + '.qa');")
        await context.route("**/*", backend.route)
        try:
            await run_two_tabs(context, backend, errors)
            await run_one_page_parallel(context, backend, errors)
        finally:
            backend.release_all()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
