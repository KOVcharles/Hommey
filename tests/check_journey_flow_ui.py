"""Real production components, actual map snapshots and card submission roundtrip."""
from pathlib import Path
from tempfile import gettempdir
from playwright.sync_api import sync_playwright, expect

OUT=Path(gettempdir())/'hommey-journey-flow';OUT.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1120,'height':1050})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto('http://localhost:8000/static/design-demos/journey-flow.html')
    expect(page.locator('.trip-intake-place')).to_have_count(1)
    expect(page.locator('.trip-intake-submit')).to_be_disabled()
    page.get_by_role('combobox').fill('国际会议')
    expect(page.get_by_role('option')).to_have_count(2)
    page.get_by_role('option').first.click()
    expect(page.locator('.trip-intake-submit')).to_be_enabled()
    page.locator('.trip-intake-place').scroll_into_view_if_needed()
    expect(page.locator('.trip-intake-place .journey-map-image').first).to_have_js_property('naturalWidth',750)
    expect(page.locator('.trip-intake-place .journey-map-image').last).to_have_js_property('naturalWidth',750)
    page.wait_for_timeout(200)
    page.screenshot(path=str(OUT/'intake.png'),full_page=True)
    page.locator('.trip-intake-submit').click()
    expect(page.locator('.journey-card')).to_have_count(1)
    card=page.locator('.journey-card').first
    expect(card.locator('.journey-hotel')).to_have_count(4)
    card.locator('.journey-place-board').scroll_into_view_if_needed()
    expect(card.locator('.journey-place-board .journey-map-image').first).to_have_js_property('naturalWidth',750)
    first_names=card.locator('.journey-hotel h4').all_text_contents()
    card.locator('.journey-hotel').first.click()
    expect(card.locator('.journey-map-pin.is-hotel.is-active')).to_have_count(1)
    card.screenshot(path=str(OUT/'result-desktop.png'))
    # Capture actual wire payload as well as the demo's rendered next card.
    page.evaluate("document.addEventListener('hommey:submit-message', e => window.lastPayload=e.detail.requestPayload)")
    card.locator('.journey-edit-place>summary').click()
    card.locator('.journey-place-input').fill('悦来')
    expect(card.locator('.journey-place-option')).to_have_count(1)
    card.locator('.journey-place-option').click()
    card.get_by_role('button',name='确认修改，重新查询',exact=True).click()
    expect(page.locator('.journey-card')).to_have_count(2)
    latest=page.locator('.journey-card').last
    expect(latest.locator('.journey-edit-place>summary')).to_contain_text('悦来')
    assert latest.locator('.journey-hotel h4').all_text_contents()!=first_names
    assert page.evaluate('lastPayload.input_source')=='quick_trip_form'
    assert page.evaluate('lastPayload.trip_input.work_location').find('悦来')>=0
    assert page.evaluate('lastPayload.trip_input.work_location_place_id')
    assert card.locator('.journey-hotel h4').all_text_contents()==first_names
    # The old card remains an accurate old result, and the new one uses a new anchor.
    latest.locator('.journey-place-board').scroll_into_view_if_needed()
    expect(latest.locator('.journey-place-board .journey-map-image').first).to_have_js_property('naturalWidth',750)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    latest.screenshot(path=str(OUT/'result-mobile.png'))
    page.evaluate("document.documentElement.dataset.theme='dark'")
    latest.screenshot(path=str(OUT/'result-dark.png'))
    # Exercise the complete initial form too, including a destination change
    # after choosing a POI. Only server-verifiable fields may leave the card.
    page.evaluate("""() => {
        document.documentElement.dataset.theme='light';
        const fields=[['origin','出发地','text'],['destination','目的地','text'],['start_date','出发日期','date'],['trip_length','行程时长','text'],['trip_purpose','出差目的','text']];
        const data={place_selection_required:true,trip_input:{},route:{},missing_required:fields.map(([key,label,input_type])=>({key,label,input_type}))};
        document.getElementById('cards').replaceChildren(HommeyTripIntakeCard.create(data));
    }""")
    page.set_viewport_size({'width':1120,'height':1050})
    expect(page.locator('.journey-place-input')).to_be_disabled()
    for key,value in [('origin','北京'),('destination','重庆'),('trip_length','2天'),('trip_purpose','参加会议')]:
        row=page.locator(f'[data-field-key="{key}"]')
        if row.locator('.trip-intake-step-trigger').get_attribute('aria-expanded')!='true':row.locator('.trip-intake-step-trigger').click()
        row.locator('input').fill(value)
    row=page.locator('[data-field-key="start_date"]');row.locator('.trip-intake-step-trigger').click()
    row.locator('.trip-intake-date-shortcuts button').nth(1).click()
    page.locator('.journey-place-input').fill('国际会议')
    expect(page.locator('.journey-place-option')).to_have_count(2)
    page.locator('.journey-place-option').first.click()
    expect(page.locator('.trip-intake-submit')).to_be_enabled()
    row=page.locator('[data-field-key="destination"]');row.locator('.trip-intake-step-trigger').click();row.locator('input').fill('南京')
    expect(page.locator('.journey-place-input')).to_have_value('')
    expect(page.locator('.trip-intake-submit')).to_be_disabled()
    row.locator('input').fill('重庆')
    page.locator('.journey-place-input').fill('国际会议')
    expect(page.locator('.journey-place-option')).to_have_count(2)
    page.locator('.journey-place-option').first.click()
    page.locator('.trip-intake-submit').click()
    expect(page.locator('.journey-card')).to_have_count(1)
    payload=page.evaluate('lastPayload.trip_input')
    assert payload['origin']=='北京' and payload['destination']=='重庆' and payload['duration_days']==2
    assert payload['work_location_place_id'] and 'work_location_verified' not in payload
    assert not errors,errors
    browser.close()
print(f'Journey flow browser checks passed. Screenshots: {OUT}')
