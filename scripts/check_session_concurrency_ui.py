"""Headless two-tab UI regression; APIs are deterministic stubs, no LLM calls.

Run: python scripts/check_session_concurrency_ui.py
Requires Playwright and Microsoft Edge. Backend overlap and isolation are
covered separately by tests/test_concurrent_sessions_integration.py.
"""
import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from jinja2 import Template
from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
BASE = "http://hommey.test"


async def main():
    sessions = [{"session_id": "older", "title": "已有会话"}]
    entered = [asyncio.Event(), asyncio.Event()]
    finish = asyncio.Event()
    streams = []
    trip_sessions = []
    errors = []
    fail_list = False

    async def route_request(route):
        nonlocal fail_list
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
                sid = "session-" + str(len(sessions))
                sessions.append({"session_id": sid, "title": sid})
                await route.fulfill(json={"session_id": sid})
            elif fail_list:
                await route.fulfill(status=503, json={"error": {"code": "UNAVAILABLE", "message": "暂时不可用"}})
            else:
                await route.fulfill(json={"sessions": sessions})
        elif path.endswith("/activate"):
            await route.fulfill(json={"messages": [{"role": "user", "content": "历史内容"}]})
        elif path.endswith("/execution-plans"):
            await route.fulfill(json={"plans": []})
        elif path.endswith("/trip/active"):
            sid = parse_qs(url.query)["session_id"][0]
            trip_sessions.append(sid)
            await route.fulfill(json={"active_trip": {"destination": sid}})
        elif path.endswith("/chat/stream"):
            streams.append(route.request.post_data_json["session_id"])
            entered[len(streams) - 1].set()
            await finish.wait()
            await route.fulfill(body=json.dumps({"type": "done"}) + "\n", content_type="application/x-ndjson")
        elif path.endswith("/status"):
            await route.fulfill(json={"initialized": True})
        elif path.endswith("/is-new"):
            await route.fulfill(json={"is_new": False})
        else:
            await route.fulfill(json={})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        context = await browser.new_context(viewport={"width": 1180, "height": 950})
        await context.add_init_script("localStorage.setItem('hommey.access_token', 'e30.' + btoa(JSON.stringify({sub:'qa'})) + '.qa');")
        await context.route("**/*", route_request)
        a, b = await context.new_page(), await context.new_page()
        for page in (a, b):
            page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await a.goto(BASE + "/chat/qa")
            await expect(a.locator("#homeInput")).to_be_enabled()
            await a.locator("#homeInput").fill("窗口 A 的任务")
            await a.locator("#homeInput").press("Enter")
            await asyncio.wait_for(entered[0].wait(), 5)
            await b.goto(BASE + "/chat/qa")
            await expect(b.locator("#homeInput")).to_be_enabled()
            await b.locator("#sidebarToggle").click()
            await expect(b.get_by_role("button", name="已有会话", exact=True)).to_be_visible()
            await b.locator("#newChatButton").click()
            await b.wait_for_function("sessionStorage.getItem('hommey.session.qa') === 'session-2'")
            await expect(b.locator("#activeTrip")).to_contain_text("session-2")
            await b.locator("#homeInput").fill("窗口 B 的任务")
            await b.locator("#homeInput").press("Enter")
            await asyncio.wait_for(entered[1].wait(), 5)
            assert streams == ["session-1", "session-2"]
            assert not finish.is_set()
            print("PASS: A remains in flight while B lists history, creates a conversation and sends independently")
            finish.set()
            await expect(b.locator("#sendBtn")).to_be_enabled()
            # A failed list must show a retryable load state, not empty history.
            fail_list = True
            await b.reload()
            await b.locator("#sidebarToggle").click()
            await expect(b.locator(".session-list-error")).to_contain_text("暂时无法加载历史会话")
            await expect(b.locator("#historyList")).not_to_contain_text("还没有历史会话")
            assert await b.evaluate("sessionStorage.getItem('hommey.session.qa')") == "session-2"
            fail_list = False
            await b.locator(".session-list-error").get_by_role("button", name="重试").click()
            await expect(b.get_by_role("button", name="已有会话", exact=True)).to_be_visible()
            assert "session-2" in trip_sessions
            assert not errors, errors
            print("PASS: session-scoped trip queries, list failure/retry, refresh retention; no JavaScript errors")
        finally:
            finish.set()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
