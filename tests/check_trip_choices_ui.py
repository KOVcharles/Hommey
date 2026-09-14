"""Browser acceptance of real components, selection races and mobile layout."""
from pathlib import Path
from tempfile import gettempdir
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(gettempdir()) / "hommey-trip-choices-qa"
OUT.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1080, "height": 1100})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto('http://localhost:8000/static/design-demos/trip-choices.html')
    expect(page.locator('.journey-trains .journey-ticket')).to_have_count(12)
    expect(page.locator('.journey-hotels .journey-hotel')).to_have_count(4)
    assert not page.get_by_role('heading', name='重庆凯宾斯基酒店礼宾部').count()
    page.screenshot(path=str(OUT / 'desktop.png'), full_page=True)
    page.get_by_label('筛选席别余票').select_option('商务座')
    assert page.locator('.journey-ticket').count() <= 12
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(OUT / 'mobile.png'), full_page=True)
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    page.screenshot(path=str(OUT / 'dark.png'), full_page=True)

    # Reuse the actual picker, with controllable late responses.
    page.evaluate("""() => {
        document.querySelector('main').replaceChildren(); window.city = '重庆';
        window.pendingSearches = []; window.picked = []; window.cleared = 0;
        HommeyTripChoices.configure({searchPlaces: (city, keyword) => new Promise(resolve => pendingSearches.push({city, keyword, resolve}))});
        window.picker = HommeyTripChoices.picker({getCity: () => window.city, onSelect: x => picked.push(x), onClear: () => cleared++});
        document.querySelector('main').appendChild(picker);
    }""")
    field = page.get_by_role('combobox')
    field.fill('会议中心')
    page.wait_for_timeout(400)
    assert page.evaluate('pendingSearches.length') == 1
    page.evaluate("city = '南京'; picker.resetCity(); pendingSearches[0].resolve([{place_id:'OLD', name:'重庆旧结果', city:'重庆市'}])")
    expect(page.get_by_role('option')).to_have_count(0)
    field.fill('国际会议')
    page.wait_for_timeout(400)
    assert page.evaluate('pendingSearches.length') == 2
    page.evaluate("pendingSearches[1].resolve([{place_id:'NEW', name:'南京国际会议中心', city:'南京市', district:'玄武区',address:'四方城2号'}])")
    expect(page.get_by_role('option')).to_have_count(1)
    field.press('ArrowDown'); field.press('Enter')
    assert page.evaluate('picked[0].place_id') == 'NEW'
    field.fill('重新搜索')
    assert page.evaluate('cleared') >= 3
    page.wait_for_timeout(400)
    assert page.evaluate('pendingSearches.length') == 3
    page.evaluate('pendingSearches[2].resolve([])')
    expect(page.get_by_role('status')).to_contain_text('不会推荐其他城市')
    assert not errors, errors
    browser.close()
print(f'Browser checks passed. Screenshots: {OUT}')
