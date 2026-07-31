(function () {
    'use strict';

    const form = document.getElementById('loginForm');
    const emailInput = document.getElementById('email');
    const passwordInput = document.getElementById('password');
    const submitBtn = document.getElementById('submitBtn');
    const errorMsg = document.getElementById('errorMsg');
    const loginModeBtn = document.getElementById('loginModeBtn');
    const registerModeBtn = document.getElementById('registerModeBtn');
    const authTitle = document.getElementById('authTitle');
    const authDescription = document.getElementById('authDescription');
    let authMode = 'login';

    function setMode(mode) {
        authMode = mode;
        const registering = mode === 'register';
        loginModeBtn.classList.toggle('active', !registering);
        registerModeBtn.classList.toggle('active', registering);
        loginModeBtn.setAttribute('aria-selected', String(!registering));
        registerModeBtn.setAttribute('aria-selected', String(registering));
        passwordInput.autocomplete = registering ? 'new-password' : 'current-password';
        authTitle.textContent = registering ? '创建账户' : '欢迎回来';
        authDescription.textContent = registering
            ? '注册后开始你的第一段差旅行程。'
            : '登录后继续你的差旅行程。';
        errorMsg.textContent = '';
    }

    function decodeJwtPayload(token) {
        const part = String(token || '').split('.')[1];
        if (!part) return null;
        const normalized = part.replace(/-/g, '+').replace(/_/g, '/');
        const padded = normalized + '='.repeat((4 - normalized.length % 4) % 4);
        try {
            return JSON.parse(decodeURIComponent(escape(atob(padded))));
        } catch (err) {
            return null;
        }
    }

    async function readError(response, fallback) {
        try {
            const body = await response.json();
            return body.error?.message || body.error || body.detail || fallback;
        } catch (err) {
            return fallback;
        }
    }

    async function login(email, password) {
        return fetch('/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password }),
        });
    }

    loginModeBtn.addEventListener('click', () => setMode('login'));
    registerModeBtn.addEventListener('click', () => setMode('register'));
    emailInput.addEventListener('input', () => { errorMsg.textContent = ''; });
    passwordInput.addEventListener('input', () => { errorMsg.textContent = ''; });

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const email = emailInput.value.trim();
        const password = passwordInput.value;
        if (!email || !password) {
            errorMsg.textContent = '请输入邮箱和密码';
            return;
        }

        submitBtn.disabled = true;
        submitBtn.textContent = authMode === 'register' ? '正在创建账户…' : '正在登录…';
        try {
            if (authMode === 'register') {
                const registerRes = await fetch('/auth/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, password }),
                });
                if (!registerRes.ok) {
                    errorMsg.textContent = await readError(registerRes, '注册失败，请重试');
                    return;
                }
            }

            const response = await login(email, password);
            if (!response.ok) {
                errorMsg.textContent = await readError(response, '登录失败，请重试');
                return;
            }
            const data = await response.json();
            const payload = decodeJwtPayload(data.access_token);
            const userId = payload && payload.sub;
            if (!userId) {
                errorMsg.textContent = '登录成功，但无法读取用户身份';
                return;
            }
            localStorage.setItem('hommey.access_token', data.access_token);
            localStorage.setItem('hommey.refresh_token', data.refresh_token);
            localStorage.setItem('hommey.user_id', String(userId));
            window.location.href = `/chat/${encodeURIComponent(userId)}`;
        } catch (err) {
            errorMsg.textContent = '网络错误，请检查连接后重试';
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = '继续';
        }
    });
})();
