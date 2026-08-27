const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Small DOM fixture for renderer contracts; real layout/scrolling is browser-tested.
class Element {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.attributes = {};
        this.className = '';
        this.text = '';
        this.classList = {
            add: (...names) => { this.className = [...new Set([...this.className.split(' ').filter(Boolean), ...names])].join(' '); },
            contains: (name) => this.className.split(' ').includes(name),
        };
    }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text + this.children.map((child) => child.textContent).join(''); }
    get childElementCount() { return this.children.length; }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener() {}
}
const context = vm.createContext({
    window: { location: { origin: 'http://localhost' } },
    document: { createElement: (tag) => new Element(tag) },
    URL,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../webui_new/static/answer-card.js'), 'utf8'), context);
const { sortTrainItems, create } = context.window.HommeyAnswerCard;
const train = (label, departure, date = '') => ({
    label, value: `${departure} 上海虹桥 → 杭州东 23:59`, detail: '',
    transport_legs: [{ service: label, origin: '上海虹桥', departure_time: departure, destination: '杭州东', arrival_time: '23:59', travel_date: date, duration: '00:36', availability: {} }],
});
const labels = (items) => Array.from(sortTrainItems(items), (item) => item.label);
const nodes = (root) => [root, ...root.children.flatMap(nodes)];
const byClass = (root, name) => nodes(root).filter((node) => node.classList.contains(name));
const render = (items, kind = 'train') => create({ title: '测试结果', sections: [{ kind, title: '车次信息', items }] });

test('sorts by departure, not duration or arrival, including single-digit hours', () => {
    assert.deepEqual(labels([train('G21', '21:01'), train('G15', '15:20'), train('G8', '8:05')]), ['G8', 'G15', 'G21']);
});
test('sorts dated results chronologically across midnight', () => {
    assert.deepEqual(labels([train('next', '00:10', '2026-08-28'), train('late', '23:40', '2026-08-27'), train('early', '00:05', '2026-08-27')]), ['early', 'late', 'next']);
});
test('invalid or missing clocks go last; unknown dates are not guessed', () => {
    assert.deepEqual(labels([train('unknown', ''), train('undated', '08:00'), train('invalid', '24:20'), train('dated', '21:00', '2026-08-27')]), ['dated', 'undated', 'unknown', 'invalid']);
});
test('rejects impossible dates and keeps tied rows in original order', () => {
    assert.deepEqual(labels([train('invalid-date', '08:00', '2026-02-30'), train('same-a', '09:00', '2026/8/27'), train('same-b', '09:00', '2026-08-27')]), ['same-a', 'same-b', 'invalid-date']);
});
test('sorting is non-mutating', () => {
    const input = Object.freeze([train('late', '23:00'), train('early', '01:00')]);
    const before = JSON.stringify(input);
    assert.deepEqual(labels(input), ['early', 'late']);
    assert.equal(JSON.stringify(input), before);
});
test('legacy text-only train results sort and render without inventing seats', () => {
    const items = [{ label: 'G21', value: '21:01 金山北 → 杭州东 21:35 00:34', detail: '二等座 有' }, { label: 'G15', value: '15:20 上海松江 → 杭州东 15:56 00:36', detail: '' }];
    assert.deepEqual(labels(items), ['G15', 'G21']);
    const card = render(items);
    assert.equal(byClass(card, 'answer-transport-leg').length, 2);
    assert.equal(byClass(card, 'answer-transport-seats').length, 0);
    assert.ok(card.textContent.includes('二等座 有'));
});
test('renders every result in one focusable scroll region without pagination', () => {
    const card = render(Array.from({ length: 12 }, (_, i) => train(`G${i}`, `${23 - i}:00`)));
    assert.equal(byClass(card, 'answer-transport-leg').length, 12);
    assert.equal(byClass(card, 'answer-train-scroll')[0].tabIndex, 0);
    assert.equal(byClass(card, 'answer-train-scroll')[0].attributes.role, 'region');
    assert.equal(byClass(card, 'answer-train-pagination').length, 0);
    assert.equal(byClass(card, 'answer-transport-service')[0].textContent, 'G11');
});
test('does not repeat labels/availability, keeps extra notes and all seat classes including zero', () => {
    const item = train('G123', '08:00');
    item.transport_legs[0].availability = { 商务座: '6', 一等座: '无', 二等座: '有', 无座: '有', 硬卧: 0 };
    item.detail = '商务座 6 · 一等座 无 · 二等座 有 · 无座 有 · 硬卧 0 · 请提前进站';
    const card = render([item]);
    assert.equal(byClass(card, 'answer-fact-label').length, 0);
    assert.equal(byClass(card, 'answer-transport-seat').length, 5);
    assert.equal(byClass(card, 'answer-transport-seat').filter((node) => node.classList.contains('is-unavailable')).length, 2);
    assert.equal(byClass(card, 'answer-fact-detail')[0].textContent, '请提前进站');
    assert.equal(byClass(card, 'answer-transport-duration')[0].textContent, '36 分钟');
});
test('empty results create no misleading count or scroll container', () => {
    assert.equal(byClass(render([]), 'answer-train-scroll').length, 0);
});
test('normalizes single-digit hours and keeps explicit overnight notes separate', () => {
    const item = train('night', '8:05');
    item.transport_legs[0].arrival_time = '00:35 +1天';
    const card = render([item]);
    assert.equal(byClass(card, 'answer-transport-time')[0].textContent, '08:05');
    assert.equal(byClass(card, 'answer-transport-time-note')[0].textContent, '+1天');
});
test('itinerary legs retain their planned order and do not become a scroll list', () => {
    const card = render([train('outbound', '15:00'), train('return', '08:00')], 'trip');
    assert.equal(byClass(card, 'answer-train-scroll').length, 0);
    assert.deepEqual(byClass(card, 'answer-transport-service').map((node) => node.textContent), ['outbound', 'return']);
});
