"""Exercise the standalone map demo in a browser; no employee data is used."""
from pathlib import Path
from tempfile import gettempdir
from playwright.sync_api import sync_playwright, expect

OUT = Path(gettempdir()) / "hommey-place-map-demo"
OUT.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1200}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://localhost:8000/static/design-demos/place-map.html")
    expect(page.locator(".result")).to_have_count(3)
    page.wait_for_function("document.getElementById('basemap').complete && document.getElementById('basemap').naturalWidth === 750")
    for image in page.locator('.map-image').all():
        expect(image).to_have_js_property('naturalWidth', 750)
    page.wait_for_timeout(250)
    page.screenshot(path=str(OUT / "desktop.png"), full_page=True)
    first_map = page.locator("#basemap").get_attribute("src")
    page.locator(".result").nth(1).click()
    expect(page.locator("#detailName")).to_contain_text("悦来")
    assert page.locator("#basemap").get_attribute("src") != first_map
    expect(page.locator(".pin.active")).to_have_count(1)
    page.locator(".result").first.click()
    page.locator("#confirm").click()
    expect(page.locator("#hotelsTab")).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".result")).to_have_count(3)
    page.locator(".result").first.click()
    expect(page.locator(".pin.hotel.active")).to_have_count(1)
    assert page.locator("#detailName").inner_text() == page.locator(".result strong").first.inner_text()
    # Compare with AMap's provider-rendered marker for this snapshot, not a
    # duplicate of the JS projection formula. A 256-pixel world misplaces it in the river.
    point = page.locator(".pin.hotel").nth(2).evaluate("n => ({x:parseFloat(n.style.left)*7.5,y:parseFloat(n.style.top)*6})")
    assert abs(point['x'] - 206) < 4 and abs(point['y'] - 106) < 4, point
    page.wait_for_function("!document.getElementById('mapFrame').classList.contains('loading')")
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "hotels.png"), full_page=True)
    # Buttons must produce intermediate frames, not swap immediately to a
    # discrete level. Marker size must stay fixed as its geographic position moves.
    size_before = page.locator(".pin.hotel.active").bounding_box()
    samples = page.evaluate("""() => new Promise(resolve => {
        const samples = []; document.getElementById('zoomIn').click();
        function frame() {
            samples.push(Number(document.getElementById('mapFrame').dataset.zoom));
            if (samples.length === 8) resolve(samples); else requestAnimationFrame(frame);
        } requestAnimationFrame(frame);
    })""")
    assert len(set(samples)) > 3 and all(13 < z < 14 for z in samples), samples
    expect(page.locator("#mapFrame")).to_have_attribute("data-zoom", "14.0000")
    size_after = page.locator(".pin.hotel.active").bounding_box()
    assert size_before['width'] == size_after['width'] and size_before['height'] == size_after['height']
    samples_out = page.evaluate("""() => new Promise(resolve => {
        const samples = []; document.getElementById('zoomOut').click();
        function frame() {
            samples.push(Number(document.getElementById('mapFrame').dataset.zoom));
            if (samples.length === 8) resolve(samples); else requestAnimationFrame(frame);
        } requestAnimationFrame(frame);
    })""")
    assert len(set(samples_out)) > 3 and all(13 < z < 14 for z in samples_out), samples_out
    expect(page.locator("#mapFrame")).to_have_attribute("data-zoom", "13.0000")
    page.locator('#zoomIn').click()
    expect(page.locator("#mapFrame")).to_have_attribute("data-zoom", "14.0000")
    # A wheel gesture should zoom only the map, and rapid reversals must settle
    # at the last requested value without launching competing animations.
    frame_box = page.locator("#mapFrame").bounding_box()
    page.mouse.move(frame_box['x'] + frame_box['width'] / 2, frame_box['y'] + frame_box['height'] / 2)
    scroll_before = page.evaluate("scrollY")
    page.mouse.wheel(0, -160)
    expect(page.locator("#mapFrame")).to_have_attribute("data-zoom", "14.4000")
    assert page.evaluate("scrollY") == scroll_before
    page.mouse.wheel(0, 80)
    expect(page.locator("#mapFrame")).to_have_attribute("data-zoom", "14.2000")
    page.evaluate("""() => {
        const frame = document.getElementById('mapFrame');
        for (const deltaY of [-80, 60, 40]) frame.dispatchEvent(new WheelEvent('wheel', {deltaY, cancelable:true}));
    }""")
    expect(page.locator("#mapFrame")).to_have_attribute("data-zoom", "14.1500")
    page.locator("#reset").click()
    expect(page.locator("#mapFrame")).to_have_attribute("data-zoom", "13.0000")
    # Wheel animation cannot repaint a previous city's map after a city switch.
    page.locator("#mapFrame").dispatch_event("wheel", {"deltaY": -180})
    page.get_by_role("button", name="展开地图", exact=True).click()
    expect(page.locator("#expand")).to_have_attribute("aria-expanded", "true")
    page.keyboard.press("Escape")
    expect(page.locator("#expand")).to_have_attribute("aria-expanded", "false")
    page.locator("#city").select_option("南京")
    page.wait_for_timeout(350)
    expect(page.locator("#mapFrame")).to_have_attribute("data-zoom", "14.0000")
    expect(page.locator("#scope")).to_have_text("仅限南京市")
    expect(page.locator("#hotelsTab")).to_be_disabled()
    assert "重庆" not in page.locator("#results").inner_text()
    expect(page.locator(".pin.hotel")).to_have_count(0)
    page.locator("#search").fill("重庆")
    expect(page.locator(".result")).to_have_count(0)
    expect(page.locator("#confirm")).to_be_disabled()
    expect(page.locator("#results")).to_contain_text("不会显示其他城市")
    page.locator("#search").fill("金陵")
    page.locator("#search").press("Enter")
    expect(page.locator("#detailName")).to_have_text("金陵饭店")
    expect(page.locator("#confirm")).to_be_enabled()
    page.locator("#city").select_option("重庆")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("document.getElementById('basemap').complete")
    page.wait_for_timeout(400)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(OUT / "mobile.png"), full_page=True)
    assert not errors, errors
    browser.close()
print(f"Map demo checks passed. Screenshots: {OUT}")
