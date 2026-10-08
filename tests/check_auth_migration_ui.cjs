/* Real production pages/scripts with deterministic HTTP fixtures; no business writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium, expect } = require('playwright/test');

const root = path.resolve(__dirname, '..');
const assets = path.join(root, 'webui_new/static');

(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true });
    try {
        const page = await browser.newPage();
        const errors = [], requests = [];
        let sessionValid = false, logoutFails = false;
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', async route => {
            const request = route.request();
            const pathname = new URL(request.url()).pathname;
            if (pathname.startsWith('/static/')) {
                const file = path.resolve(assets, '.' + pathname.slice('/static'.length));
                if (!file.startsWith(assets + path.sep) || !fs.existsSync(file)) {
                    return route.fulfill({ status: 404 });
                }
                return route.fulfill({ body: fs.readFileSync(file), contentType: {
                    '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2',
                }[path.extname(file)] || 'application/octet-stream' });
            }
            if (pathname === '/login' || pathname === '/chat/1') {
                const template = pathname === '/login' ? 'signin.html' : 'chat.html';
                const html = fs.readFileSync(path.join(root, 'webui_new/templates', template), 'utf8')
                    .replaceAll('{{ user_id }}', '1')
                    .replaceAll("{{ user_id[0:1].upper() if user_id else 'U' }}", '1');
                return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
            }
            requests.push(pathname);
            if (pathname === '/auth/login') {
                assert.equal(request.postDataJSON().email, 'one@example.com');
                sessionValid = true;
                return route.fulfill({ json: {
                    access_token: 'opaque-session-without-jwt-parts', token_type: 'bearer',
                    expires_in: 604800, user: { id: 1, role: 'user' },
                } });
            }
            if (pathname === '/auth/logout') {
                assert.equal(request.headers().authorization, 'Bearer opaque-session-without-jwt-parts');
                if (logoutFails) return route.fulfill({ status: 503,
                    json: { error: { message: '认证服务暂不可用，请稍后重试' } } });
                sessionValid = false;
                return route.fulfill({ json: { logged_out: true } });
            }
            if (!sessionValid) return route.fulfill({ status: 401,
                json: { error: { message: '登录已失效，请重新登录' } } });
            assert.equal(request.headers().authorization, 'Bearer opaque-session-without-jwt-parts');
            let body = {};
            if (pathname === '/api/me') body = { id: 1, role: 'user' };
            if (pathname.endsWith('/status')) body = { initialized: true };
            if (pathname.endsWith('/summary')) body = { role: 'user', preferences: [] };
            if (pathname.endsWith('/sessions')) body = { sessions: [] };
            if (pathname.endsWith('/trip/active')) body = { active_trip: null };
            if (pathname.endsWith('/profile')) body = { profile: {}, onboarding_status: 'completed', revision: 0 };
            return route.fulfill({ json: body });
        });

        await page.goto('http://hommey.test/login');
        await page.evaluate(() => {
            localStorage.setItem('hommey.access_token', 'old-jwt');
            localStorage.setItem('hommey.refresh_token', 'old-refresh');
        });
        await page.locator('#email').fill('one@example.com');
        await page.locator('#password').fill('password123');
        await page.locator('#submitBtn').click();
        await expect(page).toHaveURL('http://hommey.test/chat/1');
        await expect(page.locator('#initOverlay')).toBeHidden();
        assert.equal(await page.evaluate(() => localStorage.getItem('hommey.token')),
            'opaque-session-without-jwt-parts');
        assert.equal(await page.evaluate(() => localStorage.getItem('hommey.refresh_token')), null);
        assert.ok(requests.includes('/api/me'));
        console.log('PASS Login and chat initialization work with an opaque token and server identity');

        await page.locator('#sidebarToggle').click();
        await page.locator('#settingsButton').click();
        await page.locator('#settingsAccountTab').click();
        logoutFails = true;
        await page.locator('.logout-link').click();
        await expect(page.locator('#toast')).toContainText('认证服务暂不可用');
        assert.equal(await page.evaluate(() => localStorage.getItem('hommey.token')),
            'opaque-session-without-jwt-parts');
        logoutFails = false;
        await page.locator('.logout-link').click();
        await expect(page).toHaveURL('http://hommey.test/login');
        assert.equal(await page.evaluate(() => localStorage.getItem('hommey.token')), null);
        assert.equal(sessionValid, false);
        console.log('PASS Logout failure can be retried; successful logout revokes the server session');

        await page.evaluate(() => localStorage.setItem('hommey.token', 'expired-session'));
        await page.goto('http://hommey.test/chat/1');
        await expect(page).toHaveURL('http://hommey.test/login');
        assert.equal(await page.evaluate(() => localStorage.getItem('hommey.token')), null);
        assert.equal(requests.includes('/auth/refresh'), false);
        assert.deepEqual(errors, []);
        console.log('PASS Expired sessions return to login without refresh requests or JavaScript errors');
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
