(function () {
    'use strict';

    const TOKEN_KEY = 'hommey.token';
    const USER_KEY = 'hommey.user_id';
    const LEGACY_KEYS = ['hommey.access_token', 'hommey.refresh_token'];

    function clear() {
        [TOKEN_KEY, USER_KEY, ...LEGACY_KEYS].forEach(key => localStorage.removeItem(key));
    }

    function save(data) {
        if (!data || typeof data.access_token !== 'string' || !data.access_token || !data.user?.id) {
            throw new Error('无法读取登录信息，请重新登录');
        }
        clear();
        localStorage.setItem(TOKEN_KEY, data.access_token);
        localStorage.setItem(USER_KEY, String(data.user.id));
        return String(data.user.id);
    }

    function token() {
        return localStorage.getItem(TOKEN_KEY) || '';
    }

    function expired() {
        clear();
        window.location.replace('/login');
    }

    async function authFetch(url, options = {}) {
        const headers = new Headers(options.headers || {});
        const value = token();
        if (value) headers.set('Authorization', `Bearer ${value}`);
        const response = await fetch(url, { ...options, headers });
        if (response.status === 401) expired();
        return response;
    }

    async function currentUser() {
        if (!token()) {
            expired();
            return null;
        }
        const response = await authFetch('/api/me');
        const body = await response.json();
        if (!response.ok) throw new Error(body.error?.message || '无法验证登录信息');
        localStorage.setItem(USER_KEY, String(body.id));
        return body;
    }

    async function logout(allDevices = false) {
        // Keep the token on network/server failures so the user can retry
        // revocation instead of silently leaving a live session behind.
        if (token()) {
            const headers = { Authorization: `Bearer ${token()}` };
            const response = await fetch(allDevices ? '/auth/logout-all' : '/auth/logout', {
                method: 'POST', headers,
            });
            if (!response.ok && response.status !== 401) {
                let body = null;
                try { body = await response.json(); } catch (ignored) { /* Use fallback below. */ }
                throw new Error(body?.error?.message || '退出失败，请稍后重试');
            }
        }
        clear();
        window.location.replace('/login');
    }

    window.HommeyAuth = Object.freeze({ save, clear, token, expired, fetch: authFetch, currentUser, logout });
}());
