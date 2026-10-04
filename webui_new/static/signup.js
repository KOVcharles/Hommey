(function () {
    'use strict';

    const form = document.getElementById('signupForm');
    if (!form) return;
    const emailInput = document.getElementById('email');
    const passwordInput = document.getElementById('password');
    const inviteInput = document.getElementById('inviteCode');
    const codeInput = document.getElementById('code');
    const sendCodeBtn = document.getElementById('sendCodeBtn');
    const submitBtn = document.getElementById('submitBtn');
    const submitLabel = document.getElementById('submitLabel');
    const passwordToggle = document.getElementById('passwordToggle');
    const capsLockHint = document.getElementById('capsLockHint');
    const altchaField = document.getElementById('altchaField');
    const altchaWidget = document.getElementById('altchaWidget');
    const altchaError = document.getElementById('altchaError');
    const errorMsg = document.getElementById('errorMsg');
    const fields = {
        email: { root: document.getElementById('emailField'), input: emailInput, error: document.getElementById('emailError') },
        password: { root: document.getElementById('passwordField'), input: passwordInput, error: document.getElementById('passwordError') },
        invite: { root: document.getElementById('inviteField'), input: inviteInput, error: document.getElementById('inviteError') },
        code: { root: document.getElementById('codeField'), input: codeInput, error: document.getElementById('codeError') },
    };
    let submitting = false;
    let sendingCode = false;
    let cooldownTimer = null;
    let altchaPayload = '';
    let altchaState = 'unverified';

    function setFieldError(name, message) {
        const field = fields[name];
        field.root.classList.toggle('has-error', Boolean(message));
        field.input.setAttribute('aria-invalid', String(Boolean(message)));
        field.error.textContent = message || '';
    }

    function validateField(name) {
        if (name === 'email') {
            if (!emailInput.value.trim()) return '请输入邮箱地址';
            return emailInput.validity.valid ? '' : '邮箱格式不正确';
        }
        if (name === 'password') {
            if (!passwordInput.value) return '请输入密码';
            return passwordInput.value.length < 8 ? '密码至少需要 8 个字符' : '';
        }
        if (name === 'invite') return inviteInput.value.trim() ? '' : '请输入邀请码';
        if (!codeInput.value.trim()) return '请输入邮箱验证码';
        return /^\d{6}$/.test(codeInput.value.trim()) ? '' : '请输入 6 位数字验证码';
    }

    function validateForm() {
        const names = ['email', 'password', 'invite', 'code'];
        let firstInvalid = null;
        for (const name of names) {
            const message = validateField(name);
            setFieldError(name, message);
            if (message && !firstInvalid) firstInvalid = fields[name].input;
        }
        if (firstInvalid) firstInvalid.focus();
        return !firstInvalid;
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

    function readAltchaPayload() {
        const input = altchaWidget.querySelector('input[name="altcha"]')
            || altchaWidget.shadowRoot?.querySelector('input[name="altcha"]');
        return input?.value || altchaPayload;
    }

    function startCodeCooldown() {
        let remaining = 60;
        sendCodeBtn.disabled = true;
        sendCodeBtn.textContent = `${remaining}s 后重发`;
        cooldownTimer = setInterval(() => {
            remaining -= 1;
            if (remaining <= 0) {
                clearInterval(cooldownTimer);
                cooldownTimer = null;
                sendCodeBtn.disabled = false;
                sendCodeBtn.textContent = '获取验证码';
            } else {
                sendCodeBtn.textContent = `${remaining}s 后重发`;
            }
        }, 1000);
    }

    async function sendVerificationCode() {
        if (sendingCode || cooldownTimer || submitting) return;
        const emailError = validateField('email');
        const inviteError = validateField('invite');
        setFieldError('email', emailError);
        setFieldError('invite', inviteError);
        if (emailError || inviteError) {
            (emailError ? emailInput : inviteInput).focus();
            return;
        }
        const payload = readAltchaPayload();
        if (!payload) {
            altchaField.classList.add('has-error');
            altchaError.textContent = altchaState === 'verifying' ? '人机验证进行中，请稍候' : '请先完成人机验证';
            return;
        }
        sendingCode = true;
        sendCodeBtn.disabled = true;
        sendCodeBtn.textContent = '发送中…';
        errorMsg.textContent = '';
        try {
            const response = await fetch('/auth/send-verification-code', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email: emailInput.value.trim(), invite_code: inviteInput.value.trim(), altcha: payload }),
            });
            if (!response.ok) {
                const { message, code } = await readErrorDetails(response, '验证码发送失败，请重试');
                if (code === 'INVALID_INVITE_CODE') {
                    setFieldError('invite', message);
                    inviteInput.focus();
                } else if (code === 'INVALID_ALTCHA') {
                    altchaWidget.reset();
                    altchaPayload = '';
                    altchaField.classList.add('has-error');
                    altchaError.textContent = '人机验证未通过，请重试';
                } else {
                    errorMsg.textContent = message;
                }
                return;
            }
            setFieldError('email', '');
            setFieldError('invite', '');
            startCodeCooldown();
        } catch (err) {
            errorMsg.textContent = '网络错误，请检查连接后重试';
        } finally {
            sendingCode = false;
            if (!cooldownTimer) {
                sendCodeBtn.disabled = false;
                sendCodeBtn.textContent = '获取验证码';
            }
        }
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

    passwordToggle.addEventListener('click', () => {
        const visible = passwordInput.type === 'password';
        passwordInput.type = visible ? 'text' : 'password';
        passwordToggle.setAttribute('aria-pressed', String(visible));
        passwordToggle.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
    });
    function updateCapsLock(event) { capsLockHint.hidden = !event.getModifierState('CapsLock'); }
    passwordInput.addEventListener('keydown', updateCapsLock);
    passwordInput.addEventListener('keyup', updateCapsLock);
    passwordInput.addEventListener('blur', () => { capsLockHint.hidden = true; });
    sendCodeBtn.addEventListener('click', sendVerificationCode);
    Object.entries(fields).forEach(([name, field]) => {
        field.input.addEventListener('input', () => {
            errorMsg.textContent = '';
            if (field.root.classList.contains('has-error')) setFieldError(name, validateField(name));
        });
    });

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (submitting || sendingCode) return;
        errorMsg.textContent = '';
        if (!validateForm()) return;
        submitting = true;
        submitBtn.disabled = true;
        submitBtn.setAttribute('aria-busy', 'true');
        submitLabel.textContent = '正在创建账户…';
        try {
            const email = emailInput.value.trim();
            const password = passwordInput.value;
            const response = await fetch('/auth/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password, code: codeInput.value.trim(), invite_code: inviteInput.value.trim() }),
            });
            if (!response.ok) {
                const { message, code } = await readErrorDetails(response, '注册失败，请重试');
                if (response.status === 409) {
                    setFieldError('email', message);
                    emailInput.focus();
                } else if (code === 'INVALID_INVITE_CODE') {
                    setFieldError('invite', message);
                    inviteInput.focus();
                } else if (code === 'INVALID_VERIFICATION_CODE' || code === 'VERIFICATION_ATTEMPTS_EXCEEDED') {
                    setFieldError('code', message);
                    codeInput.focus();
                } else {
                    errorMsg.textContent = message;
                }
                return;
            }
            const loginResponse = await fetch('/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password }),
            });
            if (!loginResponse.ok) {
                errorMsg.textContent = '账户已创建，请前往登录页面登录';
                return;
            }
            const data = await loginResponse.json();
            const userId = decodeJwtPayload(data.access_token)?.sub;
            if (!userId) {
                errorMsg.textContent = '账户已创建，请前往登录页面登录';
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
            submitLabel.textContent = '创建账户';
        }
    });

    customElements.whenDefined('altcha-widget').then(() => {
        const i18n = globalThis.$altcha?.i18n;
        if (!i18n) return;
        i18n.set('zh-cn', {
            ...i18n.get('en'),
            label: '点击完成人机验证',
            verifying: '正在验证，请稍候…',
            verified: '验证已通过',
            verificationRequired: '请先完成人机验证',
            error: '验证失败，请重试',
            expired: '验证已过期，请重试',
            loading: '正在加载…',
            reload: '重新加载',
            verify: '验证',
            cancel: '取消',
        });
    });
    altchaWidget.addEventListener('statechange', (event) => {
        const detail = event.detail || {};
        altchaState = detail.state || 'unverified';
        altchaPayload = detail.payload || '';
        if (altchaState === 'verified') {
            altchaField.classList.remove('has-error');
            altchaError.textContent = '';
        }
    });
})();
