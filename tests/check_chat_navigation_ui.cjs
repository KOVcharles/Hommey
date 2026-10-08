/* Production template/assets with controllable NDJSON streams. No business writes.
 * Run with Playwright on NODE_PATH: node tests/check_chat_navigation_ui.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium, expect } = require('playwright/test');
const root = path.resolve(__dirname, '..');
const assets = path.join(root, 'webui_new/static');
const output = path.join(root, 'tmp/chat-navigation-qa-20261004');
fs.mkdirSync(output, { recursive: true });
const sessions = [{ session_id: 'older', title: '已有会话' }];
const activations = [], interruptions = [], passed = [];
let attachmentCount = 0;
function pass(name) { passed.push(name); console.log('PASS', name); }

(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true });
    try {
        const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.addInitScript(() => {
            localStorage.setItem('hommey.token', 'qa-opaque-session');
            localStorage.setItem('hommey.theme', 'light');
            // Real ReadableStreams let replies arrive after navigation, independently.
            window.__streams = [];
            const fetchOriginal = window.fetch.bind(window);
            window.fetch = (url, options) => {
                if (!String(url).endsWith('/chat/stream')) return fetchOriginal(url, options);
                const payload = JSON.parse(options.body);
                const encoder = new TextEncoder();
                const stream = { payload, cancelled: false };
                window.__streams.push(stream);
                const body = new ReadableStream({
                    start(controller) {
                        stream.emit = event => controller.enqueue(encoder.encode(JSON.stringify(event) + '\n'));
                        stream.finish = () => { stream.emit({ type: 'done' }); controller.close(); };
                        stream.fail = () => controller.error(new Error('测试连接中断'));
                        stream.emit({ type: 'chunk', text: '已收到问题，正在整理。' });
                    },
                    cancel() { stream.cancelled = true; },
                });
                return Promise.resolve(new Response(body, { headers: { 'Content-Type': 'application/x-ndjson' } }));
            };
        });
        await page.route('**/*', async route => {
            const req = route.request(), url = new URL(req.url()), pathname = url.pathname;
            if (pathname.startsWith('/static/')) {
                const file = path.resolve(assets, '.' + pathname.slice('/static'.length));
                if (!file.startsWith(assets + path.sep) || !fs.existsSync(file)) return route.fulfill({ status: 404 });
                return route.fulfill({ body: fs.readFileSync(file), contentType: ({ '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream' });
            }
            if (pathname === '/chat/qa') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: fs.readFileSync(path.join(root, 'webui_new/templates/chat.html'), 'utf8').replaceAll('{{ user_id }}', 'qa').replaceAll("{{ user_id[0:1].upper() if user_id else 'U' }}", 'Q') });
            let body = {};
            if (pathname === '/api/me') body = {id: 'qa', role: 'user'};
            if (pathname.endsWith('/status')) body = { initialized: true };
            if (pathname.endsWith('/summary')) body = { name_display: '界面验证', role: 'user', preferences: [] };
            if (pathname.endsWith('/sessions')) {
                if (req.method() === 'POST') {
                    const session_id = 'session-' + sessions.length;
                    sessions.push({ session_id, title: session_id });
                    body = { session_id };
                } else body = { sessions };
            }
            if (pathname.endsWith('/activate')) {
                activations.push(pathname.split('/sessions/')[1].split('/')[0]);
                body = { messages: [{ role: 'user', content: '历史问题' }, { role: 'assistant', content: '历史回复' }] };
            }
            if (pathname.endsWith('/execution-plans')) body = { plans: [] };
            if (pathname.endsWith('/trip/active')) body = { active_trip: null };
            if (pathname.endsWith('/attachments')) body = req.method() === 'POST'
                ? { id: 'attachment-' + (++attachmentCount), filename: '草稿附件-' + attachmentCount + '.txt', kind: 'document', status: 'ready' }
                : { attachments: [] };
            if (pathname.endsWith('/interrupt')) interruptions.push(pathname);
            return route.fulfill({ json: body });
        });
        await page.goto('http://hommey.test/chat/qa');
        await expect(page.locator('#initOverlay')).toBeHidden();
        const home = page.locator('#homeInput'), chat = page.locator('#chatInput');
        const center = page.locator('#workspaceChatEntry');
        const openCenter = async () => {
            const view = await page.locator('#appShell').getAttribute('data-view');
            await page.locator(view === 'home' ? '#sidebarToggle' : '#workspaceChatEntry').click();
            await expect(page.locator('#sidebar')).toHaveAttribute('aria-hidden', 'false');
        };
        const openSession = async id => {
            await openCenter();
            await page.locator('#workspaceHistoryButton').click();
            await page.locator(`.session-row[data-session-id="${id}"] .session-open`).click();
            await expect(page.locator('#appShell')).toHaveAttribute('data-view', 'chat');
        };
        const upload = async input => {
            await input.setInputFiles({ name: '草稿.txt', mimeType: 'text/plain', buffer: Buffer.from('附件草稿') });
        };
        await page.screenshot({ path: path.join(output, 'home-desktop.png'), animations: 'disabled' });
        await page.locator('#homeComposer').screenshot({ path: path.join(output, 'composer.png'), animations: 'disabled' });
        await home.fill('第一段会话');
        await home.press('Enter');
        await page.waitForFunction(() => window.HommeySessionRuntime.isRunning('session-1'));
        await expect(center).toBeVisible();
        await chat.fill('原会话未发送的草稿');
        await upload(page.locator('.chat-composer-shell input[type="file"]'));
        await expect(page.locator('.chat-composer-shell .pending-chip')).toHaveCount(1);
        await page.evaluate(() => window.__firstCanvas = window.HommeySessionRuntime.get('session-1').container);
        await openCenter();
        await page.evaluate(() => window.__streams[0].emit({ type: 'chunk', text: '\n打开信息中心时继续生成。' }));
        await page.waitForFunction(() => window.__firstCanvas.textContent.includes('信息中心时继续生成'));
        await page.keyboard.press('Escape');
        await expect(center).toBeFocused();
        await expect(chat).toHaveValue('原会话未发送的草稿');
        assert.deepEqual(interruptions, []);
        pass('Information center opens during streaming, keeps the draft, and restores focus');

        await page.locator('#homeButton').click();
        await expect(page.locator('#appShell')).toHaveAttribute('data-view', 'home');
        await expect(home).toBeFocused();
        assert.equal(await page.evaluate(() => window.HommeySessionRuntime.activeId()), '');
        assert.ok(await page.evaluate(() => document.contains(window.__firstCanvas) && window.HommeySessionRuntime.isRunning('session-1')));
        await expect(page.locator('#homeComposer .pending-chip')).toHaveCount(0);
        await home.fill('首页独立草稿');
        await upload(page.locator('#homeComposer input[type="file"]'));
        await expect(page.locator('#homeComposer .pending-chip')).toHaveCount(1);
        await openSession('session-1');
        assert.ok(await page.evaluate(() => window.__firstCanvas === window.HommeySessionRuntime.active().container));
        await expect(chat).toHaveValue('原会话未发送的草稿');
        await expect(page.locator('.chat-composer-shell .pending-chip')).toHaveCount(1);
        await expect(page.locator('.chat-composer-shell .pending-chip')).toContainText('草稿附件-1.txt');
        assert.equal(activations.includes('session-1'), false);
        await page.locator('#homeButton').click();
        await expect(home).toHaveValue('首页独立草稿');
        await expect(page.locator('#homeComposer .pending-chip')).toContainText('草稿附件-2.txt');
        pass('Logo returns immediately; the live canvas and each composer draft/attachment stay separate');

        await home.fill('第二段会话');
        await home.press('Enter');
        await page.waitForFunction(() => window.HommeySessionRuntime.isRunning('session-2'));
        assert.equal(await page.evaluate(() => window.HommeySessionRuntime.runningCount()), 2);
        assert.deepEqual(await page.evaluate(() => window.__streams[1].payload.attachment_ids), ['attachment-2']);
        assert.deepEqual(await page.evaluate(() => window.HommeySessionRuntime.get('session-1').draft.attachments.map(item => item.id)), ['attachment-1']);
        await chat.fill('第二段的草稿');
        await openCenter();
        await page.evaluate(() => window.__streams[0].finish());
        await page.waitForFunction(() => !window.HommeySessionRuntime.isRunning('session-1'));
        await expect(page.locator('#sidebarClose')).toBeFocused();
        assert.equal(await page.evaluate(() => window.HommeySessionRuntime.isRunning('session-2')), true);
        await page.keyboard.press('Escape');
        await expect(chat).toHaveValue('第二段的草稿');
        await expect(page.locator('#sendBtn')).toHaveAttribute('aria-label', '停止生成');
        await openSession('session-1');
        await expect(chat).toHaveValue('原会话未发送的草稿');
        assert.ok(await page.evaluate(() => window.__firstCanvas === window.HommeySessionRuntime.active().container));
        assert.equal(activations.includes('session-1'), false);
        await page.screenshot({ path: path.join(output, 'chat-desktop.png'), animations: 'disabled' });
        pass('Homepage starts a second stream; completing the first keeps the second intact and restores the completed reply');

        // Dropdown remains usable after the compact composer redesign.
        await page.locator('.chat-composer-shell [data-retrieval-mode-trigger]').click();
        await page.locator('.chat-composer-shell [data-retrieval-mode-option="enhanced"]').click();
        await expect(page.locator('.chat-composer-shell [data-retrieval-mode-label]')).toHaveText('增强检索');
        await expect(page.locator('.composer [data-quick-trip-open]')).toHaveCount(0);
        await expect(page.locator('#homeComposer .composer-head, #homeComposer .capability-row')).toHaveCount(0);
        await page.locator('.chat-composer-shell [data-attachment-panel]').click();
        await expect(page.locator('#attachmentsLayer')).toHaveClass(/open/);
        await page.locator('#attachmentsClose').click();
        for (const width of [390, 320]) {
            await page.setViewportSize({ width, height: 844 });
            assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
            const entryBounds = await center.boundingBox();
            assert.ok(entryBounds.x >= 0 && entryBounds.x + entryBounds.width <= width);
            assert.ok(await page.locator('.chat-composer-shell .composer-tools').evaluate(el => el.scrollWidth <= el.clientWidth));
            await openCenter();
            await expect(page.locator('#sidebarClose')).toBeFocused();
            await page.keyboard.press('Escape');
            await page.screenshot({ path: path.join(output, `chat-${width}.png`), animations: 'disabled' });
            await page.locator('#homeButton').click();
            assert.ok(await page.locator('#homeComposer .composer-tools').evaluate(el => el.scrollWidth <= el.clientWidth));
            await page.screenshot({ path: path.join(output, `home-${width}.png`), animations: 'disabled' });
            await page.locator('#mobileHandle').click();
            await page.locator('#workspaceHistoryButton').click();
            await page.locator('.session-row[data-session-id="session-1"] .session-open').click();
        }
        await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
        await page.screenshot({ path: path.join(output, 'chat-dark.png'), animations: 'disabled' });
        pass('Retrieval/attachments still work; compact composers and center entry fit desktop, 390px, 320px and dark mode');

        // A failed background request must restore its text to its own draft.
        await page.locator('#homeButton').click();
        await expect(home).toBeFocused();
        await page.evaluate(() => window.__streams[1].fail());
        await page.waitForFunction(() => !window.HommeySessionRuntime.isRunning('session-2'));
        await expect(home).toBeFocused();
        await page.locator('#mobileHandle').click();
        await page.locator('#workspaceHistoryButton').click();
        await page.locator('.session-row[data-session-id="session-2"] .session-open').click();
        await expect(chat).toHaveValue('第二段会话');
        await expect(page.locator('.chat-composer-shell .pending-chip')).toContainText('草稿附件-2.txt');
        assert.deepEqual(interruptions, []);
        assert.equal(await page.evaluate(() => window.__streams.some(stream => stream.cancelled)), false);
        assert.deepEqual(errors, []);
        pass('Background failure restores the original text/attachment without stealing homepage focus or cancelling streams');
        fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ passed, errors, activations, interruptions }, null, 2));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
