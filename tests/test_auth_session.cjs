const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');

const source = readFileSync(resolve(__dirname, '../webui_new/static/auth-session.js'), 'utf8');

function session(respond) {
    const storage = new Map();
    const calls = [], redirects = [];
    const context = {
        Headers,
        localStorage: {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, String(value)),
            removeItem: key => storage.delete(key),
        },
        window: { location: { replace: url => redirects.push(url) } },
        fetch: async (url, options) => {
            calls.push({ url, options });
            return respond ? respond(url, options) : Response.json({});
        },
    };
    vm.runInNewContext(source, context);
    return { auth: context.window.HommeyAuth, storage, calls, redirects };
}

const login = { access_token: 'opaque-random-token', user: { id: 12, role: 'user' } };

test('opaque tokens use the response identity and replace legacy credentials', () => {
    const { auth, storage } = session();
    storage.set('hommey.access_token', 'old-jwt');
    storage.set('hommey.refresh_token', 'old-refresh');
    assert.equal(auth.save(login), '12');
    assert.equal(auth.token(), login.access_token);
    assert.equal(storage.get('hommey.user_id'), '12');
    assert.equal(storage.has('hommey.access_token'), false);
    assert.equal(storage.has('hommey.refresh_token'), false);
    assert.throws(() => auth.save({ access_token: 'token' }), /登录信息/);
});

test('identity comes from the authenticated server rather than stored user id', async () => {
    const { auth, storage, calls } = session(() => Response.json({ id: 12, role: 'user' }));
    auth.save(login);
    storage.set('hommey.user_id', '999');
    assert.equal((await auth.currentUser()).id, 12);
    assert.equal(storage.get('hommey.user_id'), '12');
    assert.equal(calls[0].url, '/api/me');
    assert.equal(calls[0].options.headers.get('Authorization'), 'Bearer opaque-random-token');
});

test('401 clears credentials and returns to login without a refresh request', async () => {
    const { auth, storage, calls, redirects } = session(() => Response.json({}, { status: 401 }));
    auth.save(login);
    const response = await auth.fetch('/api/12/sessions');
    assert.equal(response.status, 401);
    assert.equal(storage.size, 0);
    assert.deepEqual(redirects, ['/login']);
    assert.equal(calls.length, 1);
});

test('logout revokes the server token before clearing local credentials', async () => {
    const { auth, storage, calls, redirects } = session((url, options) => {
        assert.equal(storage.get('hommey.token'), login.access_token);
        assert.equal(url, '/auth/logout');
        assert.equal(options.method, 'POST');
        assert.equal(options.headers.Authorization, 'Bearer opaque-random-token');
        return Response.json({ logged_out: true });
    });
    auth.save(login);
    await auth.logout();
    assert.equal(storage.size, 0);
    assert.deepEqual(redirects, ['/login']);
    assert.equal(calls.length, 1);
});

test('failed logout keeps credentials so revocation can be retried', async () => {
    const { auth, redirects } = session(() => Response.json(
        { error: { message: '认证服务暂不可用' } }, { status: 503 }));
    auth.save(login);
    await assert.rejects(auth.logout(), /认证服务暂不可用/);
    assert.equal(auth.token(), login.access_token);
    assert.equal(redirects.length, 0);
});

test('network failure during logout preserves the live token', async () => {
    const { auth } = session(() => { throw new Error('offline'); });
    auth.save(login);
    await assert.rejects(auth.logout(), /offline/);
    assert.equal(auth.token(), login.access_token);
});

test('logout-all uses its own endpoint and already expired sessions can exit', async () => {
    const { auth, calls } = session(() => Response.json({}, { status: 401 }));
    auth.save(login);
    await auth.logout(true);
    assert.equal(calls[0].url, '/auth/logout-all');
    assert.equal(auth.token(), '');
});

test('403 and service failures keep the session and preserve request options', async () => {
    const { auth, redirects, calls } = session(() => Response.json({}, { status: 403 }));
    auth.save(login);
    await auth.fetch('/api/admin/skills', {
        method: 'POST', headers: { 'X-Request-ID': 'req-test' }, body: '{}',
    });
    assert.equal(auth.token(), login.access_token);
    assert.equal(redirects.length, 0);
    assert.equal(calls[0].options.headers.get('X-Request-ID'), 'req-test');
    assert.equal(calls[0].options.body, '{}');
});

test('legacy credentials alone require a new login', async () => {
    const { auth, storage, calls, redirects } = session();
    storage.set('hommey.access_token', 'old-jwt');
    assert.equal(await auth.currentUser(), null);
    assert.equal(storage.size, 0);
    assert.equal(calls.length, 0);
    assert.deepEqual(redirects, ['/login']);
});
