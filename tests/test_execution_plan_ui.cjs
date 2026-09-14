/* DOM contract tests using only Node's standard library. */
const assert = require('node:assert/strict');
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.textContent = ''; this.open = false; }
    setAttribute(key, value) { this[key] = value; }
    addEventListener() {}
    append(...items) { this.children.push(...items); }
    appendChild(item) { this.append(item); }
    replaceChildren() { this.children = []; }
    querySelectorAll(selector) {
        return this.children.flatMap(item => [
            ...((selector.startsWith('.') ? item.className === selector.slice(1) : item.tag === selector) ? [item] : []),
            ...item.querySelectorAll(selector)
        ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0]; }
}
global.document = {createElement: tag => new Element(tag)};
const {update, connectionLost} = require('../webui_new/static/execution-plan.js');
const container = new Element('main');
const step = id => ({step_id: id, title: `<script>must stay text</script>${id}`, status: 'running', purpose: '天气或制度', summary: ''});
const initial = {run_id: 'one', revision: 1, status: 'running', steps: [step('a'), step('b')]};
update(container, initial);
assert.equal(container.children.length, 1);
assert.equal(container.querySelectorAll('li').length, 2);
assert.ok(container.querySelector('strong').textContent.includes('<script>'));
// The steps sit behind a grid-rows transition wrapper so open and close can animate every
// time; a CSS animation on the persistent <ol> only ever played once.
const body = container.children[0].querySelector('.execution-plan-body');
assert.ok(body, 'Collapse wrapper wraps the steps');
assert.equal(body.children[0].className, 'execution-plan-body-inner');
assert.equal(body.children[0].children[0].tag, 'ol');
assert.equal(body.inert, true, 'Collapsed steps stay out of the tab order');
const final = {...initial, revision: 3, status: 'degraded', steps: [
    {...step('a'), status: 'succeeded'}, {...step('b'), status: 'failed'}]};
update(container, final);
update(container, {...initial, revision: 2});
update(container, initial);
update(container, final);
assert.equal(container.children.length, 1);
assert.equal(container.children[0].planSnapshot.revision, 3);
assert.equal(container.children[0].open, false);
assert.equal(container.querySelectorAll('li')[1].dataset.status, 'failed');
update(container, {...initial, run_id: 'two'});
assert.equal(container.children.length, 2);
connectionLost(container, 'two');
assert.ok(container.children[1].querySelector('.execution-plan-title').textContent.includes('进度待确认'));
assert.equal(container.children[1].planSnapshot.status, 'running'); // transport cannot invent execution state
console.log('Execution plan UI: parallel steps, stale/replayed events, safe text and disconnect checks passed.');

container.children[1].open = true;
update(container, {...initial, run_id: 'two', revision: 2});
assert.equal(container.children[1].open, true, 'User-opened progress stays open');
update(container, {...initial, run_id: 'three', steps: [step('a'), {...step('a'), status: 'failed'}]});
assert.equal(container.children[2].querySelectorAll('li').length, 1, 'Retries collapse to one user action');
