/* Real Chromium checks with synthetic replies; never connects to the business API.
 * Run: NODE_PATH=<Playwright module directory> node tests/check_output_experience_ui.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const assets = path.join(root, 'webui_new/static');
const artifacts = path.join(root, 'tmp/output-experience-qa-20261003');
fs.mkdirSync(artifacts, {recursive: true});
const scripts = ['vendor/marked-18.0.14.js', 'vendor/dompurify-3.4.16.js', 'markdown.js', 'policy-card.js', 'answer-card.js', 'information-card.js'];
const styles = ['hommey.css', 'answer-card.css', 'policy-card.css', 'markdown.css', 'information-card.css'];
const question = {type: 'information_request', interaction_id: 'question-1', title: '确认适用的差旅条件',
    description: '查询范围已确定为**差旅住宿标准**。再补充以下条件，我会继续核对适用条款。',
    input_placeholder: '也可以直接回复补充信息…',
    fields: [
        {key: 'personnel', label: '人员类别', input_type: 'single_select', required: true, allow_custom: true, options: ['教职工', '学生', '暂不确定']},
        {key: 'title', label: '职称', input_type: 'single_select', required: false, allow_custom: true, help_text: '教职工可填写；学生或暂不确定时可以跳过。', options: ['正高', '副高', '中级', '初级', '无职称', '暂不确定']},
        {key: 'funding', label: '经费来源', input_type: 'single_select', required: true, allow_custom: true, options: ['科研经费', '非科研经费', '暂不确定']},
    ]};
const markdown = '## 已整理的查询条件\n\n下面是**展示示例**，实际标准仍需检索制度。\n\n| 项目 | 已知信息 | 下一步 |\n| --- | --- | --- |\n| 人员类别 | 教职工 | 核对职称条件 |\n| 经费来源 | 暂未确认 | 选择或自行填写 |\n\n- 已知信息不再重复询问\n- 未确认条件会明确保留\n\n> 选择仅用于本次查询，不会自动保存为长期偏好。';
const checks = [];
function pass(name) { checks.push(name); console.log('PASS', name); }
async function openHistory(page) {
    const view = await page.locator('#appShell').getAttribute('data-view');
    await page.locator(view === 'home' ? '#sidebarToggle' : '#workspaceChatEntry').click();
    await page.locator('#workspaceHistoryButton').click();
}
async function fixture(page, data = question) {
    await page.setContent('<html lang="zh-CN" data-theme="light"><body style="font-family:system-ui, sans-serif"><main style="max-width:760px;margin:28px auto;padding:0 16px"><div id="answer" class="msg-bubble ai" style="margin-bottom:24px"></div><div id="cards"></div></main></body></html>');
    for (const file of styles) await page.addStyleTag({path: path.join(assets, file)});
    for (const file of scripts) await page.addScriptTag({path: path.join(assets, file)});
    await page.evaluate(({data, markdown}) => {
        window.events = [];
        document.addEventListener('hommey:submit-message', event => { window.events.push(event.detail); if (window.cancelSubmit) event.preventDefault(); });
        HommeyMarkdown.render(document.querySelector('#answer'), markdown);
        document.querySelector('#cards').append(HommeyInformationCard.create(data));
    }, {data, markdown});
}
(async () => {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    try {
        const page = await browser.newPage({viewport: {width: 1120, height: 1100}});
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await fixture(page);
        assert.equal(await page.locator('#answer table tbody tr').count(), 2);
        assert.equal(await page.locator('#answer strong').innerText(), '展示示例');
        assert.equal(await page.locator('#answer li').count(), 2);
        assert.equal(await page.locator('input:checked').count(), 0);
        assert.equal(await page.locator('.information-submit').isDisabled(), true);
        await page.getByLabel('教职工', {exact: true}).check();
        await page.getByRole('combobox', {name: '职称'}).selectOption({label: '正高'});
        await page.getByLabel('科研经费', {exact: true}).check();
        assert.equal(await page.locator('.information-submit').isEnabled(), true);
        assert.equal(await page.evaluate(() => events.length), 0);
        await page.screenshot({path: path.join(artifacts, 'desktop.png'), fullPage: true});
        pass('Markdown headings, emphasis, lists and tables; chips/dropdown, no preselection or auto-submit');
        await page.locator('.information-submit').click();
        assert.equal(await page.evaluate(() => events.length), 1);
        assert.equal(await page.evaluate(() => events[0].text), '补充信息（确认适用的差旅条件）：\n人员类别：教职工\n职称：正高\n经费来源：科研经费');
        assert.equal(await page.locator('.information-submit').isDisabled(), true);
        await page.evaluate(() => events[0].complete(false));
        assert.equal(await page.getByLabel('教职工', {exact: true}).isChecked(), true);
        assert.equal(await page.locator('.information-submit').isEnabled(), true);
        await page.locator('.information-submit').click();
        await page.evaluate(() => events[1].complete(true));
        assert.equal(await page.locator('.information-card input,.information-card select,.information-card button').count(), 0);
        await page.locator('.information-archive summary').click();
        assert.match(await page.locator('.information-archive-body').innerText(), /职称：正高/);
        pass('Confirmed submission, duplicate-click lock, failure retains values, archive becomes read-only');

        const varied = {...question, fields: [
            {key: 'items', label: '查询内容', input_type: 'multi_select', options: ['交通', '酒店'], allow_custom: true, required: true},
            {key: 'date', label: '出发日期', input_type: 'date', required: true},
            {key: 'note', label: '补充说明', input_type: 'text', required: false}]};
        await fixture(page, varied);
        await page.getByLabel('交通', {exact: true}).check();
        await page.getByLabel('自行填写', {exact: true}).check();
        await page.getByLabel('出发日期', {exact: true}).fill('2026-10-15');
        assert.equal(await page.locator('.information-submit').isDisabled(), true);
        await page.getByLabel('查询内容：自行填写').fill('会场接驳');
        await page.getByLabel('补充说明', {exact: true}).fill('无需预订');
        await page.evaluate(() => { window.cancelSubmit = true; });
        await page.locator('.information-submit').click();
        assert.equal(await page.locator('.information-submit').isEnabled(), true);
        assert.match(await page.evaluate(() => events[0].text), /查询内容：交通、会场接驳\n出发日期：2026-10-15\n补充说明：无需预订/);
        pass('Multi-select, custom text, required validation, date, optional text and canceled event');

        await fixture(page);
        const combo = page.getByRole('combobox', {name: '职称'});
        await combo.selectOption('custom');
        assert.equal(await page.getByLabel('职称：自行填写').isVisible(), true);
        await page.getByLabel('职称：自行填写').fill('特聘岗位');
        await combo.selectOption({label: '副高'});
        assert.equal(await page.getByLabel('职称：自行填写').isVisible(), false);
        const radio = page.getByLabel('教职工', {exact: true});
        await radio.focus(); await page.keyboard.press('Space');
        assert.equal(await radio.isChecked(), true);
        pass('Dropdown custom-answer switch and keyboard selection');

        await page.setViewportSize({width: 390, height: 844});
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.screenshot({path: path.join(artifacts, 'mobile.png'), fullPage: true});
        await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
        await page.screenshot({path: path.join(artifacts, 'mobile-dark.png'), fullPage: true, animations: 'disabled'});
        pass('390px light/dark layout without page overflow');

        await page.evaluate(() => {
            window.xss = 0;
            HommeyMarkdown.render(document.querySelector('#answer'), '[安全链接](https://example.com/policy)\n\n[危险](javascript:window.xss=1) <img src=x onerror="window.xss=1"><svg onload="window.xss=1"></svg><script>window.xss=1</script><iframe src="https://example.com"></iframe><form><input></form>\n\n`**原样代码**`\n\n```html\n<img onerror="evil()">\n```');
        });
        assert.equal(await page.evaluate(() => window.xss), 0);
        assert.equal(await page.locator('#answer img,#answer script,#answer svg,#answer iframe,#answer form,#answer input').count(), 0);
        assert.equal(await page.locator('#answer a').count(), 1);
        assert.equal(await page.locator('#answer a').getAttribute('rel'), 'noopener noreferrer');
        assert.match(await page.locator('#answer code').first().innerText(), /\*\*原样代码\*\*/);
        pass('HTML/event/JavaScript URL injection removed; safe links and literal code preserved');

        await page.evaluate(() => HommeyMarkdown.render(document.querySelector('#answer'),
            '**职称：**正高；适用于**正高（教授）**人员\n\n- [x] 已确认\n- [ ] 待补充\n\n`**职称：**` 和 \\*\\*原样\\*\\*\n\n***强调***\n\n未闭合**内容'));
        assert.equal(await page.locator('#answer strong').first().innerText(), '职称：');
        assert.equal(await page.locator('#answer strong').nth(1).innerText(), '正高（教授）');
        assert.match(await page.locator('#answer').innerText(), /☑ 已确认/);
        assert.match(await page.locator('#answer').innerText(), /☐ 待补充/);
        assert.match(await page.locator('#answer').innerText(), /\*\*原样\*\*/);
        assert.match(await page.locator('#answer').innerText(), /未闭合\*\*内容/);
        assert.equal(await page.locator('#answer code').innerText(), '**职称：**');
        assert.equal(await page.locator('#answer em strong, #answer strong em').count(), 1);
        pass('CJK bold punctuation compatibility; task status, escapes, nested emphasis and incomplete text');

        await page.evaluate(() => {
            const card = HommeyAnswerCard.create({title: '**示例标题**', summary: '保留**必要条件**', sections: [
                {kind: 'general', title: '结果', status: 'success', body: '- **材料齐全**\n- 等待确认', items: [{label: '说明', value: '**示例值**', detail: '适用于**本次查询**'}]}]});
            document.querySelector('#cards').replaceChildren(card);
            const policy = HommeyPolicyCard.content({sections: [{kind: 'policy', items: [{label: '住宿示例', value: '**示例条件**下 100 元/晚', detail: '仅为**界面测试**'}], body: '| 项目 | 说明 |\n| --- | --- |\n| 示例 | 待确认 |'}]});
            document.querySelector('#cards').append(policy);
        });
        assert.doesNotMatch(await page.locator('#cards').innerText(), /\*\*/);
        assert.equal(await page.locator('.policy-amount').innerText(), '100 元/晚');
        assert.equal(await page.locator('.policy-more table').count(), 1);
        pass('General and policy cards share Markdown without losing amount highlighting');
        await fixture(page, {...question, archived: true, submitted_text: '人员类别：学生'});
        assert.equal(await page.locator('.information-card input').count(), 0);
        await page.locator('.information-archive summary').click();
        assert.match(await page.locator('.information-archive-body').innerText(), /人员类别：学生/);
        pass('Reloaded archive retains submitted answer and has no active controls');
        assert.deepEqual(errors, []);

        // Load the real chat template and app.js; only HTTP responses are fixtures.
        const integration = await browser.newPage({viewport: {width: 1200, height: 1000}});
        const integrationErrors = [];
        integration.on('pageerror', error => integrationErrors.push(error.message));
        const requests = [];
        let history = [{role: 'assistant', request_id: 'question-1', content: '补充信息', presentation_document: question}];
        await integration.addInitScript(() => {
            localStorage.setItem('hommey.access_token', 'test.' + btoa(JSON.stringify({sub: 'demo', exp: 9999999999})) + '.test');
        });
        await integration.route('**/*', async route => {
            const request = route.request(); const url = new URL(request.url());
            if (url.pathname.startsWith('/static/')) {
                const file = path.resolve(assets, '.' + url.pathname.slice('/static'.length));
                if (!file.startsWith(assets + path.sep) || !fs.existsSync(file)) return route.fulfill({status: 404, body: ''});
                const ext = path.extname(file);
                return route.fulfill({body: fs.readFileSync(file), contentType: ({'.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml'})[ext] || 'application/octet-stream'});
            }
            if (url.pathname === '/chat/demo') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'webui_new/templates/chat.html'), 'utf8').replaceAll('{{ user_id }}', 'demo').replaceAll("{{ user_id[0:1].upper() if user_id else 'U' }}", 'D')});
            if (url.pathname.endsWith('/chat/stream')) {
                requests.push(request.postDataJSON());
                const text = '## 已收到\n\n**教职工**，职称为**正高**。\n\n| 条件 | 当前值 |\n| --- | --- |\n| 经费 | 科研经费 |';
                history = [{...history[0], presentation_document: {...question, archived: true, submitted_text: requests.at(-1).message}},
                    {role: 'user', content_type: 'form_submission', content: requests.at(-1).message}, {role: 'assistant', content: text}];
                return route.fulfill({contentType: 'application/x-ndjson', body: [
                    {type: 'chunk', text: text.slice(0, 16)}, {type: 'chunk', text: text.slice(16)}, {type: 'done'}].map(e => JSON.stringify(e)).join('\n')});
            }
            let body = {};
            if (url.pathname.endsWith('/status')) body = {initialized: true};
            if (url.pathname.endsWith('/sessions')) body = {sessions: [{session_id: 'session-demo', title: '差旅条件示例'}]};
            if (url.pathname.endsWith('/activate')) body = {session_id: 'session-demo', messages: history};
            if (url.pathname.endsWith('/execution-plans')) body = {plans: []};
            return route.fulfill({contentType: 'application/json', body: JSON.stringify(body)});
        });
        await integration.goto('http://hommey.test/chat/demo');
        await openHistory(integration);
        await integration.locator('.session-open').click();
        await integration.getByLabel('教职工', {exact: true}).check();
        await integration.getByRole('combobox', {name: '职称'}).selectOption({label: '正高'});
        await integration.getByLabel('科研经费', {exact: true}).check();
        await integration.screenshot({path: path.join(artifacts, 'chat-form.png'), fullPage: true, animations: 'disabled'});
        await integration.locator('#chatInput').fill('这份草稿保留');
        await integration.locator('.information-submit').click();
        await integration.locator('.msg-bubble.ai table').waitFor();
        assert.equal(requests.length, 1);
        assert.equal(requests[0].session_id, 'session-demo');
        assert.equal(requests[0].intake_request_id, 'question-1');
        assert.deepEqual(requests[0].attachment_ids, []);
        assert.equal(await integration.locator('#chatInput').inputValue(), '这份草稿保留');
        assert.doesNotMatch(await integration.locator('.msg-bubble.ai').last().innerText(), /\*\*/);
        assert.equal(await integration.locator('.information-card[data-archived="true"]').count(), 1);
        await openHistory(integration);
        await integration.locator('.session-open').click();
        await integration.locator('.information-archive summary').click();
        assert.match(await integration.locator('.information-archive-body').innerText(), /人员类别：教职工/);
        assert.equal(await integration.locator('.msg-bubble.user').count(), 0);
        await integration.screenshot({path: path.join(artifacts, 'chat-integration.png'), fullPage: true});
        assert.deepEqual(integrationErrors, []);
        pass('Real chat integration: session-bound submission, draft retained, streaming Markdown, archived history');
        fs.writeFileSync(path.join(artifacts, 'browser-results.json'), JSON.stringify({passed: checks.length, checks}, null, 2));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
