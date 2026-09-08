(function () {
    'use strict';

    const form = document.getElementById('loginForm');
    const emailInput = document.getElementById('email');
    const passwordInput = document.getElementById('password');
    const codeInput = document.getElementById('code');
    const codeField = document.getElementById('codeField');
    const sendCodeBtn = document.getElementById('sendCodeBtn');
    const submitBtn = document.getElementById('submitBtn');
    const submitLabel = document.getElementById('submitLabel');
    const passwordToggle = document.getElementById('passwordToggle');
    const passwordHint = document.getElementById('passwordHint');
    const capsLockHint = document.getElementById('capsLockHint');
    const authFormPanel = document.getElementById('authFormPanel');
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
    let codeAnimation = null;
    let codeCooldownTimer = null;
    let submitting = false;
    let sendingCode = false;

    function setPasswordVisible(visible) {
        passwordInput.type = visible ? 'text' : 'password';
        passwordToggle.setAttribute('aria-pressed', String(visible));
        passwordToggle.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
    }

    function updateSubmitLabel() {
        submitLabel.textContent = authMode === 'register' ? '创建账户' : '登录';
    }

    function animateCodeField(visible, motionDisabled) {
        // Read the in-flight geometry before cancelling so rapid reversals stay continuous.
        const gap = parseFloat(getComputedStyle(form).rowGap) || 0;
        const previous = codeField.hidden
            ? { height: '0px', opacity: 0, marginBottom: `${-gap}px` }
            : {
                height: `${codeField.getBoundingClientRect().height}px`,
                opacity: getComputedStyle(codeField).opacity,
                marginBottom: getComputedStyle(codeField).marginBottom,
            };
        if (codeAnimation) codeAnimation.cancel();
        codeAnimation = null;
        codeField.inert = !visible;
        codeField.setAttribute('aria-hidden', String(!visible));
        codeField.style.overflow = '';
        if (motionDisabled || typeof codeField.animate !== 'function') {
            codeField.hidden = !visible;
            return;
        }
        codeField.hidden = false;
        const expandedHeight = codeField.getBoundingClientRect().height;
        codeField.style.overflow = 'hidden';
        const animation = codeField.animate([
            previous,
            { height: visible ? `${expandedHeight}px` : '0px', opacity: visible ? 1 : 0, marginBottom: visible ? '0px' : `${-gap}px` },
        ], { duration: 360, easing: 'cubic-bezier(.22, .7, .25, 1)', fill: 'both' });
        codeAnimation = animation;
        animation.finished.then(() => {
            if (codeAnimation !== animation) return;
            codeField.hidden = !visible;
            codeField.style.overflow = '';
            animation.cancel();
            codeAnimation = null;
        }).catch(() => {}); // A new mode takes ownership when the user reverses mid-transition.
    }

    function setMode(mode) {
        if (mode === authMode || submitting || sendingCode) return;
        authMode = mode;
        const registering = mode === 'register';
        loginModeBtn.classList.toggle('active', !registering);
        registerModeBtn.classList.toggle('active', registering);
        loginModeBtn.setAttribute('aria-selected', String(!registering));
        registerModeBtn.setAttribute('aria-selected', String(registering));
        loginModeBtn.tabIndex = registering ? -1 : 0;
        registerModeBtn.tabIndex = registering ? 0 : -1;
        authFormPanel.setAttribute('aria-labelledby', registering ? 'registerModeBtn' : 'loginModeBtn');
        authTabs.dataset.mode = mode;
        document.title = registering ? '注册 · Hommey' : '登录 · Hommey';
        passwordInput.autocomplete = registering ? 'new-password' : 'current-password';
        passwordHint.hidden = !registering;
        setPasswordVisible(false);
        updateSubmitLabel();
        authTitle.textContent = registering ? '创建账户' : '欢迎回来';
        authDescription.textContent = registering
            ? '注册后开始你的第一段差旅行程。'
            : '登录后继续你的差旅行程。';
        authFootnote.textContent = registering
            ? '创建账户即表示你同意仅将账户用于 Hommey 差旅服务。'
            : '登录即表示你同意仅将账户用于 Hommey 差旅服务。';
        codeInput.required = registering;
        clearAllErrors();

        const motionDisabled = document.documentElement.dataset.motion === 'off'
            || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        animateCodeField(registering, motionDisabled);
        if (modeAnimation) modeAnimation.cancel();
        if (!motionDisabled && typeof authModeCopy.animate === 'function') {
            modeAnimation = authModeCopy.animate([
                { opacity: .3, transform: 'translateY(4px)' },
                { opacity: 1, transform: 'translateY(0)' },
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
            if (!/^\d{6}$/.test(codeInput.value.trim())) return '请输入 6 位数字验证码';
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
        if (sendingCode || codeCooldownTimer || submitting || authMode !== 'register') return;
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
        sendingCode = true;
        loginModeBtn.disabled = true;
        registerModeBtn.disabled = true;
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
            sendingCode = false;
            loginModeBtn.disabled = submitting;
            registerModeBtn.disabled = submitting;
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
    authTabs.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        if (submitting || sendingCode) return;
        const nextMode = event.key === 'Home' ? 'login' : event.key === 'End' ? 'register'
            : authMode === 'login' ? 'register' : 'login';
        setMode(nextMode);
        (nextMode === 'login' ? loginModeBtn : registerModeBtn).focus();
    });
    passwordToggle.addEventListener('click', () => {
        setPasswordVisible(passwordInput.type === 'password');
    });
    function updateCapsLock(event) {
        capsLockHint.hidden = !event.getModifierState('CapsLock');
    }
    passwordInput.addEventListener('keydown', updateCapsLock);
    passwordInput.addEventListener('keyup', updateCapsLock);
    passwordInput.addEventListener('blur', () => { capsLockHint.hidden = true; });
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
        if (submitting || sendingCode) return;
        errorMsg.textContent = '';
        if (!validateForm()) return;

        const email = emailInput.value.trim();
        const password = passwordInput.value;

        submitting = true;
        submitBtn.disabled = true;
        submitBtn.setAttribute('aria-busy', 'true');
        loginModeBtn.disabled = true;
        registerModeBtn.disabled = true;
        sendCodeBtn.disabled = true;
        submitLabel.textContent = authMode === 'register' ? '正在创建账户…' : '正在登录…';
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
            submitting = false;
            submitBtn.disabled = false;
            submitBtn.setAttribute('aria-busy', 'false');
            loginModeBtn.disabled = false;
            registerModeBtn.disabled = false;
            sendCodeBtn.disabled = Boolean(codeCooldownTimer);
            updateSubmitLabel();
        }
    });

    // ALTCHA 自托管组件（login.html 引入 /static/vendor/altcha.min.js）。
    // 勾选组件触发 PoW 验证；statechange 事件携带 {state, payload}，
    // payload 随「获取验证码」请求提交，服务端离线校验。
    customElements.whenDefined('altcha-widget').then(() => {
        const i18n = globalThis.$altcha?.i18n;
        if (!i18n) return;
        i18n.set('zh-cn', {
            ...i18n.get('en'),
            ariaLinkLabel: 'ALTCHA 官方网站',
            label: '点击完成人机验证',
            verifying: '正在验证，请稍候…',
            verified: '验证已通过',
            verificationRequired: '请先完成人机验证',
            waitAlert: '正在验证，请稍候',
            error: '验证失败，请重试',
            expired: '验证已过期，请重试',
            loading: '正在加载…',
            reload: '重新加载',
            verify: '验证',
            cancel: '取消',
            enterCode: '请输入验证码',
            enterCodeAria: '输入听到的验证码，按空格键播放音频。',
            enterCodeFromImage: '请输入下方图片中的验证码。',
            getAudioChallenge: '获取语音验证码',
            footer: '由 <a href="https://altcha.org/" tabindex="-1" target="_blank" rel="noopener noreferrer" aria-label="ALTCHA 官方网站">ALTCHA</a> 提供验证',
        });
    });
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
