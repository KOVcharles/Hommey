"""Browser regression checks. Run with a Playwright environment and installed Edge:

    python tests/check_trip_intake_calendar_ui.py
"""
from pathlib import Path
from tempfile import gettempdir

from playwright.sync_api import sync_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(gettempdir()) / "hommey-calendar-qa"
ARTIFACTS.mkdir(exist_ok=True)
FIELD = {"key": "start_date", "label": "出发日期", "input_type": "date",
         "help_text": "告诉我日期或相对时间", "examples": ["8月5日"]}


def render(page, now="2026-09-11T04:00:00Z", data=None):
    page.clock.install(time=now)
    page.set_content('<html lang="zh-CN"><main style="max-width:790px;margin:24px auto"></main></html>')
    for name in ("hommey.css", "trip-intake-card.css"):
        page.add_style_tag(path=str(ROOT / "webui_new/static" / name))
    page.add_script_tag(path=str(ROOT / "webui_new/static/trip-intake-card.js"))
    page.evaluate("""data => {
        window.submissions = [];
        document.addEventListener('hommey:submit-message', event => {
            submissions.push(event.detail.text); event.preventDefault();
        });
        document.querySelector('main').appendChild(HommeyTripIntakeCard.create(data));
    }""", data or {"missing_required": [FIELD], "route": {"origin": "北京", "destination": "重庆"}})


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    context = browser.new_context(viewport={"width": 1024, "height": 1000}, timezone_id="America/Los_Angeles")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    render(page)
    expect(page.locator('input[type="date"]')).to_have_count(0)
    expect(page.locator('[data-date="2026-09-10"]')).to_be_disabled()
    expect(page.locator('[data-date="2026-09-11"]')).to_be_enabled()
    expect(page.get_by_role("button", name="上个月", exact=True)).to_be_disabled()
    # Synthetic attempts must also fail to complete or submit the field.
    page.locator('[data-date="2026-09-10"]').dispatch_event("click")
    expect(page.get_by_role("button", name="提交补充信息")).to_be_disabled()
    expect(page.locator('.trip-intake-field-help')).to_contain_text("已过期")
    page.get_by_role("button", name="明天 ·").click()
    expect(page.locator('[data-date="2026-09-12"]')).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="提交补充信息").click()
    assert page.evaluate("submissions") == ["2026-09-12出发"]
    page.get_by_role("button", name="清除出发日期").click()
    expect(page.get_by_role("button", name="提交补充信息")).to_be_disabled()
    page.get_by_role("button", name="下周一 ·").click()
    expect(page.locator('[data-date="2026-09-14"]')).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="下个月", exact=True).click()
    expect(page.locator('.trip-intake-calendar-header strong')).to_have_text("2026年 10月")
    page.locator('[data-date="2026-10-01"]').focus()
    page.keyboard.press("ArrowLeft")
    expect(page.locator('[data-date="2026-09-30"]')).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator('[data-date="2026-09-30"]')).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="今天 ·").click()
    page.clock.run_for(500)
    page.screenshot(path=str(ARTIFACTS / "desktop.png"), full_page=True)

    # A selected date expires while the page is left open over Beijing midnight.
    page.clock.set_system_time("2026-09-11T16:00:01Z")
    page.get_by_role("button", name="提交补充信息").click()
    assert len(page.evaluate("submissions")) == 1
    expect(page.get_by_role("button", name="提交补充信息")).to_be_disabled()
    expect(page.locator('[data-date="2026-09-11"]')).to_be_disabled()
    page.get_by_role("button", name="今天 ·").click()
    expect(page.locator('[data-date="2026-09-12"]')).to_have_attribute("aria-pressed", "true")
    page.set_viewport_size({"width": 360, "height": 900})
    page.clock.run_for(500)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(ARTIFACTS / "mobile.png"), full_page=True)
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    page.clock.run_for(500)
    page.screenshot(path=str(ARTIFACTS / "dark.png"), full_page=True, animations="disabled")

    # Year transition and next Monday when today itself is Monday.
    render(page, "2026-12-31T04:00:00Z")
    page.get_by_role("button", name="明天 ·").click()
    expect(page.locator('[data-date="2027-01-01"]')).to_have_attribute("aria-pressed", "true")
    render(page, "2026-09-14T04:00:00Z")
    page.get_by_role("button", name="下周一 ·").click()
    expect(page.locator('[data-date="2026-09-21"]')).to_have_attribute("aria-pressed", "true")
    render(page, "2028-02-28T04:00:00Z")
    page.get_by_role("button", name="明天 ·").click()
    expect(page.locator('[data-date="2028-02-29"]')).to_have_attribute("aria-pressed", "true")

    # Conflicting dates have the same safe picker and a correction path.
    render(page, data={"missing_required": [], "conflicts": [
        {"key": "start_date", "message": "确认出发日期", "values": ["2026-09-01", "2026-09-12"]}
    ]})
    page.get_by_role("button", name="明天 ·").click()
    page.get_by_role("button", name="提交补充信息").click()
    assert page.evaluate("submissions") == ["2026-09-12出发"]
    assert errors == [], errors
    browser.close()
    print(f"Calendar UI checks passed; screenshots: {ARTIFACTS}")
