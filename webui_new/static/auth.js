(function () {
    'use strict';

    const form = document.getElementById('loginForm');
    const emailInput = document.getElementById('email');
    const passwordInput = document.getElementById('password');
    const codeInput = document.getElementById('code');
    const codeField = document.getElementById('codeField');
    const sendCodeBtn = document.getElementById('sendCodeBtn');
    const submitBtn = document.getElementById('submitBtn');
    const errorMsg = document.getElementById('errorMsg');
    const loginModeBtn = document.getElementById('loginModeBtn');
    const registerModeBtn = document.getElementById('registerModeBtn');
    const authTitle = document.getElementById('authTitle');
    const authDescription = document.getElementById('authDescription');
    const authModeCopy = document.getElementById('authModeCopy');
    const authTabs = document.getElementById('authTabs');
    const authFootnote = document.getElementById('authFootnote');
    const altchaField = document.getElementById('altchaField');
    const altchaWidget = document.getElementById('altchaWidget');
    const altchaError = document.getElementById('altchaError');
    let altchaPayload = '';
    let altchaState = 'unverified';
    const fields = {
        email: {
            root: document.getElementById('emailField'),
            input: emailInput,
            error: document.getElementById('emailError'),
        },
        password: {
            root: document.getElementById('passwordField'),
            input: passwordInput,
            error: document.getElementById('passwordError'),
        },
        code: {
            root: codeField,
            input: codeInput,
            error: document.getElementById('codeError'),
        },
    };
    let authMode = 'login';
    let modeAnimation = null;
    let codeCooldownTimer = null;

    function setMode(mode) {
        if (mode === authMode) return;
        authMode = mode;
        const registering = mode === 'register';
        loginModeBtn.classList.toggle('active', !registering);
        registerModeBtn.classList.toggle('active', registering);
        loginModeBtn.setAttribute('aria-selected', String(!registering));
        registerModeBtn.setAttribute('aria-selected', String(registering));
        authTabs.dataset.mode = mode;
        passwordInput.autocomplete = registering ? 'new-password' : 'current-password';
        authTitle.textContent = registering ? '创建账户' : '欢迎回来';
        authDescription.textContent = registering
            ? '注册后开始你的第一段差旅行程。'
            : '登录后继续你的差旅行程。';
        authFootnote.textContent = registering
            ? '创建账户即表示你同意仅将账户用于 Hommey 差旅服务。'
            : '登录即表示你同意仅将账户用于 Hommey 差旅服务。';
        codeField.hidden = !registering;
        if (!registering) resetCodeCooldown();
        clearAllErrors();

        const motionDisabled = document.documentElement.dataset.motion === 'off'
            || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (!motionDisabled && typeof authModeCopy.animate === 'function') {
            if (modeAnimation) modeAnimation.cancel();
            modeAnimation = authModeCopy.animate([
                { opacity: 0, transform: registering ? 'translateX(8px)' : 'translateX(-8px)' },
                { opacity: 1, transform: 'translateX(0)' },
            ], {
                duration: 260,
                easing: 'cubic-bezier(.2, .85, .25, 1)',
            });
        }
    }

    function setFieldError(name, message) {
        const field = fields[name];
        field.root.classList.toggle('has-error', Boolean(message));
        field.input.setAttribute('aria-invalid', String(Boolean(message)));
        field.error.textContent = message || '';
    }

    function clearAllErrors() {
        setFieldError('email', '');
        setFieldError('password', '');
        setFieldError('code', '');
        altchaField.classList.toggle('has-error', false);
        altchaError.textContent = '';
        errorMsg.textContent = '';
    }

    function validateField(name) {
        if (name === 'email') {
            const email = emailInput.value.trim();
            if (!email) return '请输入邮箱地址';
            if (!emailInput.validity.valid) return '邮箱格式不正确，请输入类似 name@example.com 的地址';
            return '';
        }

        if (name === 'code') {
            if (authMode !== 'register') return '';
            if (!codeInput.value.trim()) return '请输入验证码';
            return '';
        }

        const password = passwordInput.value;
        if (!password) return '请输入密码';
        if (authMode === 'register' && password.length < 8) return '密码至少需要 8 个字符';
        return '';
    }

    function validateForm() {
        const errors = {
            email: validateField('email'),
            password: validateField('password'),
            code: validateField('code'),
        };
        setFieldError('email', errors.email);
        setFieldError('password', errors.password);
        setFieldError('code', errors.code);
        const firstInvalid = errors.email
            ? emailInput
            : (errors.password ? passwordInput : (errors.code ? codeInput : null));
        if (firstInvalid) firstInvalid.focus();
        return !firstInvalid;
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

    async function readErrorDetails(response, fallback) {
        try {
            const body = await response.json();
            return {
                message: body.error?.message || body.error || body.detail || fallback,
                code: body.error?.code || '',
            };
        } catch (err) {
            return { message: fallback, code: '' };
        }
    }

    function resetCodeCooldown() {
        if (codeCooldownTimer) {
            clearInterval(codeCooldownTimer);
            codeCooldownTimer = null;
        }
        sendCodeBtn.disabled = false;
        sendCodeBtn.textContent = '获取验证码';
    }

    function readAltchaPayload() {
        // 组件把 base64 payload 写入 shadow DOM 内 name=altcha 的隐藏 input；
        // 优先读隐藏 input，读不到时回退到 statechange 事件缓存的值。
        const input = altchaWidget.querySelector('input[name="altcha"]')
            || (altchaWidget.shadowRoot && altchaWidget.shadowRoot.querySelector('input[name="altcha"]'));
        return input && input.value ? input.value : altchaPayload;
    }

    function startCodeCooldown() {
        resetCodeCooldown();
        let remaining = 60;
        sendCodeBtn.disabled = true;
        sendCodeBtn.textContent = `${remaining}s 后重发`;
        codeCooldownTimer = setInterval(() => {
            remaining -= 1;
            if (remaining <= 0) {
                resetCodeCooldown();
            } else {
                sendCodeBtn.textContent = `${remaining}s 后重发`;
            }
        }, 1000);
    }

    async function sendVerificationCode() {
        const email = emailInput.value.trim();
        if (!email) {
            setFieldError('email', '请先输入邮箱地址');
            emailInput.focus();
            return;
        }
        if (!emailInput.validity.valid) {
            setFieldError('email', '邮箱格式不正确，请输入类似 name@example.com 的地址');
            emailInput.focus();
            return;
        }
        const payload = readAltchaPayload();
        if (!payload) {
            altchaField.classList.toggle('has-error', true);
            altchaError.textContent = altchaState === 'verifying'
                ? '人机验证进行中，请稍候'
                : '请先完成人机验证';
            return;
        }
        sendCodeBtn.disabled = true;
        sendCodeBtn.textContent = '发送中…';
        try {
            const res = await fetch('/auth/send-verification-code', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, altcha: payload }),
            });
            if (!res.ok) {
                const { message, code } = await readErrorDetails(res, '验证码发送失败，请重试');
                if (code === 'INVALID_ALTCHA') {
                    // payload 已失效（过期/被篡改）：重置组件，重新勾选即可再验证。
                    altchaWidget.reset();
                    altchaPayload = '';
                    altchaField.classList.toggle('has-error', true);
                    altchaError.textContent = '人机验证未通过，请重试';
                } else {
                    errorMsg.textContent = message;
                }
                return;
            }
            setFieldError('email', '');
            errorMsg.textContent = '';
            startCodeCooldown();
        } catch (err) {
            errorMsg.textContent = '网络错误，请检查连接后重试';
        } finally {
            if (!codeCooldownTimer) {
                sendCodeBtn.disabled = false;
                sendCodeBtn.textContent = '获取验证码';
            }
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
    sendCodeBtn.addEventListener('click', sendVerificationCode);
    Object.entries(fields).forEach(([name, field]) => {
        field.input.addEventListener('input', () => {
            errorMsg.textContent = '';
            if (field.root.classList.contains('has-error')) {
                setFieldError(name, validateField(name));
            }
        });
    });

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        errorMsg.textContent = '';
        if (!validateForm()) return;

        const email = emailInput.value.trim();
        const password = passwordInput.value;

        submitBtn.disabled = true;
        submitBtn.textContent = authMode === 'register' ? '正在创建账户…' : '正在登录…';
        try {
            if (authMode === 'register') {
                const registerRes = await fetch('/auth/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, password, code: codeInput.value.trim() }),
                });
                if (!registerRes.ok) {
                    const { message, code } = await readErrorDetails(registerRes, '注册失败，请重试');
                    if (registerRes.status === 409) {
                        setFieldError('email', message);
                        emailInput.focus();
                    } else if (code === 'INVALID_VERIFICATION_CODE' || code === 'VERIFICATION_ATTEMPTS_EXCEEDED') {
                        setFieldError('code', message);
                        codeInput.focus();
                    } else {
                        errorMsg.textContent = message;
                    }
                    return;
                }
            }

            const response = await login(email, password);
            if (!response.ok) {
                const message = await readError(response, '登录失败，请重试');
                if (response.status === 401) {
                    setFieldError('email', '请检查邮箱地址');
                    setFieldError('password', message);
                    passwordInput.focus();
                } else {
                    errorMsg.textContent = message;
                }
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

    // ALTCHA 自托管组件（login.html 引入 /static/vendor/altcha.min.js）。
    // 勾选组件触发 PoW 验证；statechange 事件携带 {state, payload}，
    // payload 随「获取验证码」请求提交，服务端离线校验。
    altchaWidget.addEventListener('statechange', (event) => {
        const detail = event.detail || {};
        altchaState = detail.state || 'unverified';
        altchaPayload = detail.payload || '';
        if (altchaState === 'verified') {
            altchaField.classList.toggle('has-error', false);
            altchaError.textContent = '';
        }
    });
})();
