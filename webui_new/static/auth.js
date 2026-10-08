(function () {
    'use strict';

    const form = document.getElementById('loginForm');
    if (!form) return;
    const emailInput = document.getElementById('email');
    const passwordInput = document.getElementById('password');
    const submitBtn = document.getElementById('submitBtn');
    const submitLabel = document.getElementById('submitLabel');
    const errorMsg = document.getElementById('errorMsg');
    const fields = {
        email: { root: document.getElementById('emailField'), input: emailInput, error: document.getElementById('emailError') },
        password: { root: document.getElementById('passwordField'), input: passwordInput, error: document.getElementById('passwordError') },
    };
    let submitting = false;

    function setFieldError(name, message) {
        const field = fields[name];
        field.root.classList.toggle('has-error', Boolean(message));
        field.input.setAttribute('aria-invalid', String(Boolean(message)));
        field.error.textContent = message || '';
    }

    function validateField(name) {
        if (name === 'email') {
            if (!emailInput.value.trim()) return '请输入邮箱地址';
            if (!emailInput.validity.valid) return '邮箱格式不正确，请输入类似 name@example.com 的地址';
            return '';
        }
        return passwordInput.value ? '' : '请输入密码';
    }

    function validateForm() {
        const emailError = validateField('email');
        const passwordError = validateField('password');
        setFieldError('email', emailError);
        setFieldError('password', passwordError);
        const firstInvalid = emailError ? emailInput : passwordError ? passwordInput : null;
        if (firstInvalid) firstInvalid.focus();
        return !firstInvalid;
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

    Object.entries(fields).forEach(([name, field]) => {
        field.input.addEventListener('input', () => {
            errorMsg.textContent = '';
            if (field.root.classList.contains('has-error')) setFieldError(name, validateField(name));
        });
    });

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (submitting) return;
        errorMsg.textContent = '';
        if (!validateForm()) return;

        const email = emailInput.value.trim();
        const password = passwordInput.value;
        submitting = true;
        submitBtn.disabled = true;
        submitBtn.setAttribute('aria-busy', 'true');
        submitLabel.textContent = '正在登录…';
        try {
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
            const userId = window.HommeyAuth.save(data);
            if (!userId) {
                errorMsg.textContent = '登录成功，但无法读取用户身份';
                return;
            }
            window.location.href = `/chat/${encodeURIComponent(userId)}`;
        } catch (err) {
            errorMsg.textContent = '网络错误，请检查连接后重试';
        } finally {
            submitting = false;
            submitBtn.disabled = false;
            submitBtn.setAttribute('aria-busy', 'false');
            submitLabel.textContent = '登录';
        }
    });
})();
