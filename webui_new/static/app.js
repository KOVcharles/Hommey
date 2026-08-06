/**
 * Hommey WebUI
 * Production interaction layer for authentication, onboarding, streaming chat,
 * session history, appearance settings, and responsive navigation.
 */
(function () {
    'use strict';

    const userId = String(document.body.dataset.userId || '');
    const appShell = document.getElementById('appShell');
    const chatMessages = document.getElementById('chatMessages');
    const chatInput = document.getElementById('chatInput');
    const sendBtn = document.getElementById('sendBtn');
    const homeComposer = document.getElementById('homeComposer');
    const homeInput = document.getElementById('homeInput');
    const homeSendBtn = document.getElementById('homeSendBtn');
    const initOverlay = document.getElementById('initOverlay');
    const sidebar = document.getElementById('sidebar');
    const scrim = document.getElementById('scrim');
    const historyList = document.getElementById('historyList');
    const historySearch = document.getElementById('historySearch');
    const historySearchBox = document.getElementById('historySearchBox');
    const settingsLayer = document.getElementById('settingsLayer');
    const renameLayer = document.getElementById('renameLayer');
    const confirmLayer = document.getElementById('confirmLayer');
    const sessionPopover = document.getElementById('sessionPopover');
    const renameInput = document.getElementById('renameInput');
    const panelName = document.getElementById('panelName');
    const panelLevel = document.getElementById('panelLevel');
    const prefList = document.getElementById('prefList');
    const activeTrip = document.getElementById('activeTrip');
    const toast = document.getElementById('toast');
    const promptRotator = document.getElementById('promptRotator');
    const rotatingQuestion = document.getElementById('rotatingQuestion');

    const ACCESS_TOKEN_KEY = 'hommey.access_token';
    const REFRESH_TOKEN_KEY = 'hommey.refresh_token';
    const USER_ID_KEY = 'hommey.user_id';
    const THEME_KEY = 'hommey.theme';
    const MOTION_KEY = 'hommey.motion';
    const defaultPlaceholder = '继续问 Hommey';

    let isProcessing = false;
    let isOnboarding = false;
    let onboardingIndex = 0;
    let customInputCallback = null;
    let activeSessionId = '';
    let selectedSessionId = '';
    let confirmCallback = null;
    let rotationTimer;
    let rotationIndex = 0;
    let toastTimer;
    let processingStatusTimer;
    let processingStatusQueue = [];
    let lastProcessingStatusAt = 0;
    let contextualPlaceholder = '';
    let scrollIdleTimer;

    const progressMessages = {
        request_analyzing: '正在理解你的需求',
        tasks_decomposing: '正在拆分差旅标准与外部信息',
        policy_searching: '正在检索适用的差旅制度',
        travel_info_searching: '正在查询目的地天气与出行信息',
        memory_searching: '正在查找相关差旅记录',
        preference_updating: '正在更新你的差旅偏好',
        trip_details_collecting: '正在整理行程信息',
        trip_planning: '正在生成行程安排',
        compliance_checking: '正在核对差旅合规性',
        task_completed: '一项信息已经准备好，继续整理中',
        task_failed: '部分信息暂时不可用，继续处理其他内容',
        answer_composing: '正在把结果整理成清晰的卡片',
        answer_ready: '信息已整理完成',
        task_running: '正在处理相关信息',
        queued: '任务已经排好，马上开始',
    };

    const progressAgentLabels = {
        rag_knowledge: '差旅标准',
        information_query: '天气与出行',
        memory_query: '差旅记录',
        preference: '偏好',
        event_collection: '行程信息',
        itinerary_planning: '行程规划',
        trip_compliance: '合规检查',
    };

    // 多模态附件：待发送的已上传附件 + 消息级 X-Request-ID（上传/聊天/重试共用）。
    let pendingAttachments = [];
    let currentRequestId = '';
    let retryRequestPending = false;

    const rotatingPrompts = [
        { label: '下周一去上海两天，帮我安排一下', prompt: '下周一去上海出差两天，帮我规划行程' },
        { label: '查一下北京的住宿和交通标准', prompt: '北京出差的住宿和交通标准是什么' },
        { label: '找到我上次去深圳的差旅行程', prompt: '查看我上次去深圳的差旅行程' },
        { label: '看看上海下周一的天气', prompt: '上海下周一的天气怎么样' },
        { label: '明早到虹桥，几点出门更稳妥？', prompt: '明早九点到上海虹桥站，建议我几点出门' },
        { label: '准备一条航班延误的备选路线', prompt: '如果航班延误，帮我准备一条备选路线' },
    ];

    const onboardingSteps = [
        {
            question: '先告诉我，你平时从哪个城市出发比较多？',
            key: 'home_location',
            hint: '之后规划行程时，我会优先按这个城市计算出发方案。',
            options: ['北京', '上海', '广州', '深圳', '成都', '杭州', '其他'],
        },
        {
            question: '出差路上，你更偏好哪种交通方式？',
            key: 'transportation_preference',
            hint: '我会在时间、舒适度和预算之间做更贴近偏好的取舍。',
            options: ['高铁', '飞机', '自驾', '都可以', '其他'],
        },
        {
            question: '住宿方面，有固定喜欢的酒店或风格吗？',
            key: 'hotel_brands',
            hint: '品牌、安静程度、通勤距离都可以告诉我。',
            options: ['汉庭', '如家', '全季', '亚朵', '锦江之星', '其他'],
        },
        {
            question: '最后，座位或舱位有什么偏好吗？',
            key: 'seat_preference',
            hint: '如果没有固定偏好，我会优先选择更稳妥的方案。',
            options: ['商务座', '一等座', '二等座', '经济舱', '不指定', '其他'],
        },
    ];

    applyStoredAppearance();
    bindEvents();
    document.addEventListener('DOMContentLoaded', initialize);

    function bindEvents() {
        chatInput.addEventListener('input', () => {
            resizeInput(chatInput);
            if (retryRequestPending) {
                resetRequestId();
                retryRequestPending = false;
            }
        });
        homeInput.addEventListener('input', () => resizeInput(homeInput));
        chatInput.addEventListener('keydown', handleComposerKeydown);
        homeInput.addEventListener('keydown', handleComposerKeydown);
        sendBtn.addEventListener('click', submitCurrentInput);
        homeComposer.addEventListener('submit', (event) => {
            event.preventDefault();
            submitHomeInput();
        });

        // 附件入口：每个 composer 的 .attach-button 内含一个隐藏 file input。
        document.querySelectorAll('.attach-button').forEach((label) => {
            const input = label.querySelector('input[type="file"]');
            if (!input) return;
            label.addEventListener('click', (e) => {
                // 让 label 自身只触发一次（避免与 input 默认行为重复）。
                if (e.target === input) return;
            });
            input.addEventListener('change', () => {
                handleFilePick(input.files);
                input.value = '';
            });
        });

        document.getElementById('sidebarToggle').addEventListener('click', openSidebar);
        document.getElementById('accountButton').addEventListener('click', openSettings);
        document.getElementById('accountRow').addEventListener('click', openSettings);
        document.getElementById('sidebarClose').addEventListener('click', closeSidebar);
        scrim.addEventListener('click', closeSidebar);
        document.getElementById('homeButton').addEventListener('click', showHome);
        document.getElementById('newChatButton').addEventListener('click', createNewSession);
        document.getElementById('searchToggle').addEventListener('click', toggleHistorySearch);
        document.getElementById('settingsButton').addEventListener('click', openSettings);
        document.getElementById('settingsClose').addEventListener('click', closeSettings);
        document.getElementById('clearHistoryButton').addEventListener('click', confirmClearHistory);
        document.getElementById('renameSessionButton').addEventListener('click', openRenameDialog);
        document.getElementById('deleteSessionButton').addEventListener('click', confirmDeleteSession);
        document.getElementById('renameForm').addEventListener('submit', renameSelectedSession);
        document.getElementById('confirmAction').addEventListener('click', runConfirmedAction);

        document.querySelectorAll('[data-close-layer]').forEach((button) => {
            button.addEventListener('click', () => closeLayer(button.dataset.closeLayer));
        });
        document.querySelectorAll('.modal-layer').forEach((layer) => {
            layer.addEventListener('click', (event) => {
                if (event.target === layer) layer.classList.remove('open');
            });
        });
        document.querySelectorAll('.logout-link').forEach((link) => {
            link.addEventListener('click', clearAuth);
        });
        document.querySelectorAll('[data-theme-option]').forEach((button) => {
            button.addEventListener('click', () => setTheme(button.dataset.themeOption));
        });
        document.getElementById('motionToggle').addEventListener('click', toggleMotion);

        historySearch.addEventListener('input', filterHistory);
        promptRotator.addEventListener('click', () => submitPrompt(promptRotator.dataset.prompt));
        promptRotator.addEventListener('mouseenter', stopPromptRotation);
        promptRotator.addEventListener('mouseleave', startPromptRotation);
        promptRotator.addEventListener('focusin', stopPromptRotation);
        promptRotator.addEventListener('focusout', startPromptRotation);
        document.addEventListener('hommey:fill-composer', handlePresentationFill);
        chatMessages.addEventListener('scroll', markConversationScrolling, { passive: true });

        document.addEventListener('click', (event) => {
            if (!sessionPopover.contains(event.target) && !event.target.closest('.session-more')) {
                sessionPopover.hidden = true;
            }
        });
    }

    function markConversationScrolling() {
        chatMessages.classList.add('is-scrolling');
        clearTimeout(scrollIdleTimer);
        scrollIdleTimer = setTimeout(() => {
            chatMessages.classList.remove('is-scrolling');
        }, 900);
    }

    async function initialize() {
        if (!ensureAuthenticatedPath()) return;
        try {
            const status = await fetchJson(`/api/${encodeURIComponent(userId)}/status`);
            if (!status.initialized) {
                const initData = await fetchJson(`/api/${encodeURIComponent(userId)}/init`, { method: 'POST' });
                if (!initData.success) throw createApiError(initData, '初始化失败');
            }

            await Promise.all([loadUserSummary(), loadActiveTrip(), loadSessions()]);
            hideInitOverlay();
            setInputEnabled(true);
            startPromptRotation();

            const newData = await fetchJson(`/api/${encodeURIComponent(userId)}/is-new`);
            if (newData.is_new) {
                enterChatView();
                startOnboarding();
            } else {
                showHome();
            }
        } catch (err) {
            showInitError(err.message || '无法连接到服务器，请检查网络后刷新页面');
        }
    }

    function handleComposerKeydown(event) {
        if (event.key !== 'Enter' || event.shiftKey) return;
        event.preventDefault();
        if (event.currentTarget === homeInput) submitHomeInput();
        else submitCurrentInput();
    }

    function submitHomeInput() {
        const text = homeInput.value.trim();
        if (!text || isProcessing || isOnboarding) return;
        homeInput.value = '';
        resizeInput(homeInput);
        chatInput.value = text;
        enterChatView();
        sendMessage();
    }

    function submitPrompt(prompt) {
        if (!prompt || isProcessing || isOnboarding) return;
        chatInput.value = prompt;
        enterChatView();
        sendMessage();
    }

    function submitCurrentInput() {
        if (customInputCallback) {
            const value = chatInput.value.trim();
            if (!value) return;
            const callback = customInputCallback;
            customInputCallback = null;
            chatInput.value = '';
            chatInput.placeholder = defaultPlaceholder;
            resizeInput(chatInput);
            callback(value);
            return;
        }
        sendMessage();
    }

    function enterChatView() {
        appShell.dataset.view = 'chat';
        closeSidebar();
        requestAnimationFrame(scrollToBottom);
    }

    function showHome() {
        if (isOnboarding || isProcessing) return;
        appShell.dataset.view = 'home';
        closeSidebar();
        setTimeout(() => homeInput.focus(), 180);
    }

    function setInputEnabled(enabled) {
        chatInput.disabled = !enabled;
        sendBtn.disabled = !enabled;
        homeInput.disabled = !enabled;
        homeSendBtn.disabled = !enabled;
    }

    function hideInitOverlay() {
        initOverlay.classList.add('hidden');
        setTimeout(() => { initOverlay.style.display = 'none'; }, 340);
    }

    function showInitError(message) {
        const status = initOverlay.querySelector('.init-status');
        const sub = initOverlay.querySelector('.init-sub');
        if (status) status.textContent = message;
        if (sub) {
            sub.replaceChildren();
            const link = document.createElement('a');
            link.href = '/';
            link.textContent = '重新登录';
            link.addEventListener('click', clearAuth);
            sub.appendChild(link);
        }
    }

    async function loadUserSummary() {
        try {
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/summary`);
            panelName.textContent = data.name_display || userId;
            panelLevel.textContent = data.member_level || '个人账户';
            prefList.replaceChildren();
            const preferences = Array.isArray(data.preferences) ? data.preferences : [];
            if (!preferences.length) {
                prefList.appendChild(createEmptyState('还没有偏好记录。'));
                return;
            }
            preferences.forEach((preference) => {
                const row = document.createElement('div');
                row.className = 'info-row';
                const label = document.createElement('span');
                label.textContent = preference.label || '';
                const value = document.createElement('span');
                value.textContent = preference.value || '-';
                row.append(label, value);
                prefList.appendChild(row);
            });
        } catch (err) {
            prefList.replaceChildren(createEmptyState('暂时无法读取偏好。'));
        }
    }

    async function loadActiveTrip() {
        try {
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/trip/active`);
            const trip = data.active_trip;
            activeTrip.replaceChildren();
            if (!trip) {
                activeTrip.appendChild(createEmptyState('当前没有进行中的出差任务。'));
                return;
            }
            const fields = [
                ['目的地', trip.destination],
                ['出发地', trip.origin],
                ['出发日期', trip.start_date],
                ['返程日期', trip.end_date],
                ['工作地点', trip.work_location],
            ];
            fields.filter(([, value]) => value).forEach(([label, value]) => {
                const row = document.createElement('div');
                row.className = 'trip-row';
                const key = document.createElement('span');
                key.textContent = label;
                const val = document.createElement('span');
                val.textContent = String(value);
                row.append(key, val);
                activeTrip.appendChild(row);
            });
        } catch (err) {
            activeTrip.replaceChildren(createEmptyState('暂时无法读取行程。'));
        }
    }

    function createEmptyState(text) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.textContent = text;
        return empty;
    }

    async function loadSessions() {
        try {
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/sessions`);
            activeSessionId = data.active_session_id || '';
            renderSessions(Array.isArray(data.sessions) ? data.sessions : []);
        } catch (err) {
            renderSessions([]);
        }
    }

    function renderSessions(sessions) {
        historyList.replaceChildren();
        const label = document.createElement('p');
        label.className = 'history-label';
        label.textContent = '最近';
        historyList.appendChild(label);
        if (!sessions.length) {
            historyList.appendChild(createEmptyState('还没有历史会话。发送第一条消息后会自动保存。'));
            return;
        }
        sessions.forEach((session) => {
            const row = document.createElement('div');
            row.className = `session-row${session.session_id === activeSessionId ? ' active' : ''}`;
            row.dataset.sessionId = session.session_id;
            row.dataset.title = session.title;

            const open = document.createElement('button');
            open.type = 'button';
            open.className = 'session-open';
            open.textContent = session.title || '未命名会话';
            open.title = session.preview || session.title || '';
            open.addEventListener('click', () => openSession(session.session_id));

            const more = document.createElement('button');
            more.type = 'button';
            more.className = 'session-more';
            more.setAttribute('aria-label', '会话操作');
            more.textContent = '•••';
            more.addEventListener('click', (event) => openSessionPopover(event, session));
            row.append(open, more);
            historyList.appendChild(row);
        });
        filterHistory();
    }

    async function createNewSession() {
        if (isProcessing || isOnboarding) return;
        try {
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/sessions`, { method: 'POST' });
            activeSessionId = data.session_id || '';
            chatMessages.replaceChildren();
            setComposerContext('');
            showHome();
            await loadSessions();
        } catch (err) {
            showToast(formatDisplayError(err, '无法创建新会话'));
        }
    }

    async function openSession(sessionId) {
        if (isProcessing || isOnboarding) return;
        try {
            const data = await fetchJson(
                `/api/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}/activate`,
                { method: 'POST' }
            );
            activeSessionId = sessionId;
            chatMessages.replaceChildren();
            const messages = data.messages || [];
            messages.forEach((message) => {
                const role = message.role === 'assistant' ? 'ai' : message.role;
                if (role === 'ai' || role === 'user') {
                    if (role === 'ai' && message.answer_document) {
                        addAnswerMessage(message.answer_document, message.timestamp);
                    } else if (role === 'ai' && message.presentation_document) {
                        addPresentationMessage(message.presentation_document, message.timestamp);
                    } else {
                        addMessage(role, message.content || '', message.timestamp, message.attachments);
                    }
                }
            });
            const latest = messages[messages.length - 1];
            setComposerContext(
                latest?.presentation_document?.type === 'trip_intake'
                    ? latest.presentation_document.input_placeholder
                    : ''
            );
            enterChatView();
            await loadSessions();
        } catch (err) {
            showToast(formatDisplayError(err, '无法打开会话'));
        }
    }

    function toggleHistorySearch() {
        historySearchBox.classList.toggle('visible');
        if (historySearchBox.classList.contains('visible')) historySearch.focus();
        else {
            historySearch.value = '';
            filterHistory();
        }
    }

    function filterHistory() {
        const query = historySearch.value.trim().toLowerCase();
        historyList.querySelectorAll('.session-row').forEach((row) => {
            row.hidden = !!query && !String(row.dataset.title || '').toLowerCase().includes(query);
        });
    }

    function openSessionPopover(event, session) {
        event.stopPropagation();
        selectedSessionId = session.session_id;
        renameInput.value = session.title || '';
        const rect = event.currentTarget.getBoundingClientRect();
        sessionPopover.style.left = `${Math.max(8, rect.right - 130)}px`;
        sessionPopover.style.top = `${Math.min(window.innerHeight - 90, rect.bottom + 4)}px`;
        sessionPopover.hidden = false;
    }

    function openRenameDialog() {
        sessionPopover.hidden = true;
        renameLayer.classList.add('open');
        setTimeout(() => renameInput.select(), 100);
    }

    async function renameSelectedSession(event) {
        event.preventDefault();
        const title = renameInput.value.trim();
        if (!selectedSessionId || !title) return;
        try {
            await fetchJson(
                `/api/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(selectedSessionId)}`,
                {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title }),
                }
            );
            renameLayer.classList.remove('open');
            await loadSessions();
            showToast('会话已重命名');
        } catch (err) {
            showToast(formatDisplayError(err, '重命名失败'));
        }
    }

    function confirmDeleteSession() {
        sessionPopover.hidden = true;
        openConfirm('删除这条会话？', '删除后无法恢复，但不会影响你的差旅偏好。', async () => {
            await fetchJson(
                `/api/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(selectedSessionId)}`,
                { method: 'DELETE' }
            );
            if (selectedSessionId === activeSessionId) {
                chatMessages.replaceChildren();
                appShell.dataset.view = 'home';
            }
            await loadSessions();
            showToast('会话已删除');
        });
    }

    function confirmClearHistory() {
        openConfirm('清空全部聊天记录？', '所有历史会话都会被删除，此操作无法恢复。', async () => {
            await fetchJson(`/api/${encodeURIComponent(userId)}/history`, { method: 'DELETE' });
            chatMessages.replaceChildren();
            closeSettings();
            appShell.dataset.view = 'home';
            await loadSessions();
            showToast('聊天记录已清空');
        });
    }

    function openConfirm(title, message, callback) {
        document.getElementById('confirmTitle').textContent = title;
        document.getElementById('confirmMessage').textContent = message;
        confirmCallback = callback;
        confirmLayer.classList.add('open');
    }

    async function runConfirmedAction() {
        const callback = confirmCallback;
        confirmCallback = null;
        confirmLayer.classList.remove('open');
        if (!callback) return;
        try {
            await callback();
        } catch (err) {
            showToast(formatDisplayError(err, '操作失败'));
        }
    }

    function startOnboarding() {
        isOnboarding = true;
        addMessage('ai', '你好，我是 Hommey。第一次见面，我想先了解几项偏好，让之后的差旅建议更贴近你。');
        setTimeout(() => showOnboardingQuestion(0), 360);
    }

    function showOnboardingQuestion(index) {
        if (index >= onboardingSteps.length) {
            finishOnboarding();
            return;
        }
        onboardingIndex = index;
        const step = onboardingSteps[index];
        addOptionsMessage(step.question, step.options, step.hint);
    }

    function addOptionsMessage(question, options, hint) {
        const row = createMessageShell('ai');
        const stack = row.querySelector('.msg-stack');
        const bubble = document.createElement('div');
        bubble.className = 'msg-bubble ai';
        bubble.append(createStrong(question));
        if (hint) bubble.append(document.createElement('br'), createMutedText(hint));
        const optionList = document.createElement('div');
        optionList.className = 'option-list';
        options.forEach((option) => {
            const pill = document.createElement('button');
            pill.type = 'button';
            pill.className = 'option-pill';
            pill.textContent = option;
            pill.addEventListener('click', () => handleOnboardingOption(option, optionList));
            optionList.appendChild(pill);
        });
        stack.append(bubble, optionList);
        chatMessages.appendChild(row);
        scrollToBottom();
    }

    function handleOnboardingOption(option, optionList) {
        optionList.querySelectorAll('button').forEach((button) => { button.disabled = true; });
        if (option === '其他') {
            chatInput.placeholder = '输入你的偏好';
            chatInput.focus();
            customInputCallback = (value) => {
                addMessage('user', value);
                sendOnboardingAnswer(value);
            };
            return;
        }
        addMessage('user', option);
        sendOnboardingAnswer(option);
    }

    async function sendOnboardingAnswer(value) {
        const step = onboardingSteps[onboardingIndex];
        isProcessing = true;
        showProcessingIndicator([]);
        try {
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/onboarding/preference`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key: step.key, value }),
            });
            removeProcessingIndicator();
            if (data.error || data.success === false) {
                addMessage('ai', getErrorMessage(data, '偏好保存失败，请再试一次。'));
                return;
            }
            addMessage('ai', data.message || `我记住了：${value}`);
            await loadUserSummary();
            setTimeout(() => showOnboardingQuestion(onboardingIndex + 1), 360);
        } catch (err) {
            removeProcessingIndicator();
            addMessage('ai', '偏好已先记录在当前对话里，稍后会继续尝试同步。');
            setTimeout(() => showOnboardingQuestion(onboardingIndex + 1), 360);
        } finally {
            isProcessing = false;
        }
    }

    function finishOnboarding() {
        isOnboarding = false;
        chatInput.placeholder = defaultPlaceholder;
        addMessage('ai', '偏好设置完成。现在可以把你的出行计划交给我。');
        loadSessions();
    }

    function ensureRequestId() {
        if (!currentRequestId) {
            // 简单 uuid v4，无需依赖外部库。
            currentRequestId = (crypto && crypto.randomUUID)
                ? crypto.randomUUID()
                : 'xxxxxxxxxxxx4xxx'.replace(/x/g, (c) => ((Math.random() * 16) | 0).toString(16));
        }
        return currentRequestId;
    }

    function resetRequestId() {
        currentRequestId = '';
    }

    async function uploadAttachment(file, onProgress) {
        const requestId = ensureRequestId();
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', `/api/${encodeURIComponent(userId)}/attachments`);
            const token = getAccessToken();
            if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
            xhr.setRequestHeader('X-Request-ID', requestId);
            // 注意：不设置 Content-Type——FormData 会自动带 multipart boundary。
            if (onProgress && xhr.upload) {
                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable) onProgress(e.loaded / e.total);
                };
            }
            xhr.onload = async () => {
                if (xhr.status === 401) {
                    const refreshed = await refreshAccessToken();
                    if (!refreshed) {
                        reject(new Error('登录已过期，请重新登录'));
                        return;
                    }
                    try { resolve(await uploadAttachment(file, onProgress)); } catch (e) { reject(e); }
                    return;
                }
                let body = null;
                try { body = JSON.parse(xhr.responseText); } catch (e) { body = null; }
                if (xhr.status >= 200 && xhr.status < 300 && body) {
                    resolve(body);
                } else {
                    const msg = (body && body.error && body.error.message) || '附件上传失败';
                    reject(new Error(msg));
                }
            };
            xhr.onerror = () => reject(new Error('网络错误，附件上传失败'));
            const form = new FormData();
            form.append('file', file);
            xhr.send(form);
        });
    }

    function pendingContainers() {
        return Array.from(document.querySelectorAll('.pending-attachments'));
    }

    function renderPendingAttachments() {
        const html = pendingAttachments.map((a) => {
            const cls = a.status === 'failed' ? 'pending-chip failed' : 'pending-chip';
            return `<span class="${cls}" data-id="${a.id || ''}" title="${a.filename}">${escapeHtml(a.filename)}${a.status === 'failed' ? '（失败）' : ''}<button class="pending-chip-remove" aria-label="移除">×</button></span>`;
        }).join('');
        pendingContainers().forEach((c) => { c.innerHTML = html; c.style.display = html ? '' : 'none'; });
        pendingContainers().forEach((c) => {
            c.querySelectorAll('.pending-chip-remove').forEach((btn) => {
                btn.addEventListener('click', (e) => {
                    const chip = e.target.closest('.pending-chip');
                    const id = chip && chip.dataset.id;
                    pendingAttachments = pendingAttachments.filter((a) => a.id !== id && a.tmpId !== chip.dataset.tmpid);
                    if (retryRequestPending) {
                        resetRequestId();
                        retryRequestPending = false;
                    }
                    renderPendingAttachments();
                });
            });
        });
    }

    async function handleFilePick(fileList) {
        const files = Array.from(fileList || []);
        for (const file of files) {
            const entry = { tmpId: 'tmp_' + Math.random().toString(36).slice(2), filename: file.name, status: 'uploading' };
            pendingAttachments.push(entry);
            renderPendingAttachments();
            try {
                const res = await uploadAttachment(file);
                entry.id = res.id;
                entry.filename = res.filename || file.name;
                entry.kind = res.kind;
                entry.status = res.status === 'ready' ? 'ready' : 'failed';
            } catch (err) {
                entry.status = 'failed';
                showToast(formatDisplayError(err, '附件上传失败'));
            }
            renderPendingAttachments();
        }
    }

    function escapeHtml(s) {
        return String(s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function renderAttachmentCards(stack, attachments) {
        if (!attachments || !attachments.length) return;
        const wrap = document.createElement('div');
        wrap.className = 'msg-attachments';
        attachments.forEach((a) => {
            const card = document.createElement('div');
            card.className = 'attachment-card';
            card.innerHTML = `<span class="attachment-icon" aria-hidden="true">📎</span><span class="attachment-name">${escapeHtml(a.filename)}</span>`;
            wrap.appendChild(card);
        });
        stack.appendChild(wrap);
    }

    async function sendMessage() {
        const text = chatInput.value.trim();
        const hasAttachments = pendingAttachments.some((a) => a.status === 'ready');
        if ((!text && !hasAttachments) || isProcessing || isOnboarding) return;
        const sendingEntries = pendingAttachments.filter((a) => a.status === 'ready');
        const sendingAttachmentIds = sendingEntries.map((a) => a.id);
        const sendingAttachments = sendingEntries.map((a) => ({ filename: a.filename, kind: a.kind }));
        let requestCompleted = false;
        enterChatView();
        addMessage('user', text, undefined, sendingAttachments);
        chatInput.value = '';
        resizeInput(chatInput);
        pendingAttachments = [];
        renderPendingAttachments();
        isProcessing = true;
        sendBtn.disabled = true;
        chatInput.placeholder = 'Hommey 正在整理…';
        setSendLoading(true);
        showProcessingIndicator([]);

        try {
            const response = await authFetch(`/api/${encodeURIComponent(userId)}/chat/stream`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Request-ID': ensureRequestId(),
                },
                body: JSON.stringify({
                    message: text,
                    attachment_ids: sendingAttachmentIds,
                    client_request_id: currentRequestId,
                }),
            });
            if (!response.ok) {
                const error = await response.json();
                throw createApiError(error, '请求失败，请重试', response.status);
            }
            if (!response.body) throw new Error('当前浏览器不支持流式响应');

            let streamMessage = null;
            let presentationRendered = false;
            let nextPlaceholder = '';
            let preferencesUpdated = false;
            let responseSources = [];
            const reader = response.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    const event = parseStreamLine(line);
                    if (!event) continue;
                    if (event.type === 'error') throw createApiError(event, '处理失败，请重试');
                    if (event.type === 'status' || event.type === 'task_status') updateProcessingStatus(event);
                    if (event.type === 'attachment_context') {
                        responseSources = event.sources || [];
                        if (event.warnings && event.warnings.length) {
                            showToast(event.warnings.join('；'));
                        }
                    }
                    if (event.type === 'agents') updateAgentTags(event.agents);
                    if (event.type === 'answer_document') {
                        removeProcessingIndicator();
                        addAnswerMessage(event.document);
                        presentationRendered = true;
                    }
                    if (event.type === 'presentation_document') {
                        removeProcessingIndicator();
                        addPresentationMessage(event.document);
                        presentationRendered = true;
                        nextPlaceholder = event.document?.input_placeholder || '';
                    }
                    if (event.type === 'chunk') {
                        if (!streamMessage) {
                            removeProcessingIndicator();
                            streamMessage = createStreamingMessage();
                        }
                        streamMessage.text += event.text || '';
                        renderMessageInto(streamMessage.bubble, streamMessage.text);
                        scrollToBottom();
                    }
                    if (event.type === 'done') preferencesUpdated = !!event.preferences_updated;
                }
            }

            const tail = parseStreamLine(buffer);
            if (tail && (tail.type === 'status' || tail.type === 'task_status')) updateProcessingStatus(tail);
            if (tail && tail.type === 'answer_document') {
                removeProcessingIndicator();
                addAnswerMessage(tail.document);
                presentationRendered = true;
            }
            if (tail && tail.type === 'presentation_document') {
                removeProcessingIndicator();
                addPresentationMessage(tail.document);
                presentationRendered = true;
                nextPlaceholder = tail.document?.input_placeholder || '';
            }
            if (tail && tail.type === 'chunk') {
                if (!streamMessage) {
                    removeProcessingIndicator();
                    streamMessage = createStreamingMessage();
                }
                streamMessage.text += tail.text || '';
                renderMessageInto(streamMessage.bubble, streamMessage.text);
            }
            if (tail && tail.type === 'done') preferencesUpdated = !!tail.preferences_updated;

            removeProcessingIndicator();
            if (!streamMessage && !presentationRendered) addMessage('ai', '我收到了，但这次没有返回具体内容。');
            if (streamMessage && responseSources.length) {
                renderAttachmentCards(streamMessage.stack, responseSources);
            }
            setComposerContext(nextPlaceholder);
            if (preferencesUpdated) await loadUserSummary();
            await Promise.all([loadActiveTrip(), loadSessions()]);
            requestCompleted = true;
        } catch (err) {
            removeProcessingIndicator();
            addMessage('ai', formatDisplayError(err, '网络错误，请检查连接后重试。'));
            // Preserve the body and request ID so an explicit retry remains idempotent.
            chatInput.value = text;
            const sendingIds = new Set(sendingAttachmentIds);
            pendingAttachments = [
                ...sendingEntries,
                ...pendingAttachments.filter((entry) => !sendingIds.has(entry.id)),
            ];
            retryRequestPending = true;
            renderPendingAttachments();
            resizeInput(chatInput);
        } finally {
            isProcessing = false;
            sendBtn.disabled = false;
            setSendLoading(false);
            chatInput.placeholder = contextualPlaceholder || defaultPlaceholder;
            if (requestCompleted) {
                resetRequestId();
                retryRequestPending = false;
            }
            chatInput.focus();
        }
    }

    function createMessageShell(role) {
        const row = document.createElement('div');
        row.className = `message-row ${role}`;
        const avatar = document.createElement('div');
        avatar.className = `msg-avatar ${role}`;
        const stack = document.createElement('div');
        stack.className = 'msg-stack';
        row.append(avatar, stack);
        return row;
    }

    function addMessage(role, text, timestamp, attachments) {
        if (role === 'ai') collapseTripIntakeCards();
        const row = createMessageShell(role);
        const stack = row.querySelector('.msg-stack');
        const bubble = document.createElement('div');
        bubble.className = `msg-bubble ${role}`;
        if (text) {
            if (role === 'user') renderUserMessageInto(bubble, text);
            else renderMessageInto(bubble, text);
        }
        stack.appendChild(bubble);
        if (role === 'user' && attachments && attachments.length) {
            renderAttachmentCards(stack, attachments);
        }
        if (timestamp) stack.appendChild(createTime(timestamp));
        chatMessages.appendChild(row);
        scrollToBottom();
        return row;
    }

    function addAnswerMessage(documentData, timestamp) {
        collapseTripIntakeCards();
        if (!window.HommeyAnswerCard || !documentData) {
            return addMessage('ai', documentData?.plain_text || '查询结果已生成。', timestamp);
        }
        const row = createMessageShell('ai');
        const stack = row.querySelector('.msg-stack');
        stack.appendChild(window.HommeyAnswerCard.create(documentData));
        if (timestamp) stack.appendChild(createTime(timestamp));
        chatMessages.appendChild(row);
        scrollToBottom();
        return row;
    }

    function addPresentationMessage(documentData, timestamp) {
        if (!documentData || documentData.type !== 'trip_intake' || !window.HommeyTripIntakeCard) {
            return addMessage('ai', documentData?.plain_text || '请补充行程信息。', timestamp);
        }
        collapseTripIntakeCards();
        const row = createMessageShell('ai');
        const stack = row.querySelector('.msg-stack');
        stack.appendChild(window.HommeyTripIntakeCard.create(documentData));
        if (timestamp) stack.appendChild(createTime(timestamp));
        chatMessages.appendChild(row);
        scrollToBottom();
        return row;
    }

    function collapseTripIntakeCards() {
        document.querySelectorAll('.trip-intake-card:not(.is-collapsed)').forEach((card) => {
            const title = card.querySelector('.trip-intake-heading h2');
            if (title && !card.dataset.originalTitle) card.dataset.originalTitle = title.textContent || '行程信息';
            card.classList.add('is-collapsed');
            if (title) title.textContent = '已更新行程信息';
            card.setAttribute('aria-label', '已更新行程信息，点击展开');
            const header = card.querySelector('.trip-intake-header');
            if (header && !header.dataset.toggleBound) {
                header.dataset.toggleBound = 'true';
                header.setAttribute('role', 'button');
                header.tabIndex = 0;
                const toggle = () => {
                    const collapsed = card.classList.toggle('is-collapsed');
                    if (title) title.textContent = collapsed ? '已更新行程信息' : card.dataset.originalTitle;
                    card.setAttribute('aria-label', collapsed ? '已更新行程信息，点击展开' : card.dataset.originalTitle);
                };
                header.addEventListener('click', toggle);
                header.addEventListener('keydown', (event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        toggle();
                    }
                });
            }
        });
    }

    function showProcessingIndicator(agents) {
        removeProcessingIndicator();
        const row = createMessageShell('ai');
        row.id = 'processingIndicator';
        const stack = row.querySelector('.msg-stack');
        const box = document.createElement('div');
        box.className = 'agent-indicator';
        const tags = document.createElement('div');
        tags.className = 'agent-tags';
        renderAgentTagsInto(tags, agents);
        const text = document.createElement('div');
        text.className = 'thinking-text';
        text.textContent = progressMessages.request_analyzing;
        const dots = document.createElement('div');
        dots.className = 'typing-dots';
        dots.innerHTML = '<i class="typing-dot"></i><i class="typing-dot"></i><i class="typing-dot"></i>';
        box.append(tags, text, dots);
        stack.appendChild(box);
        chatMessages.appendChild(row);
        scrollToBottom();
    }

    function removeProcessingIndicator() {
        clearTimeout(processingStatusTimer);
        processingStatusQueue = [];
        lastProcessingStatusAt = 0;
        document.getElementById('processingIndicator')?.remove();
    }

    function updateProcessingStatus(event) {
        const indicator = document.getElementById('processingIndicator');
        if (!indicator) return;
        const message = progressMessages[event.message_key] || event.message || '正在整理';
        const current = indicator.querySelector('.thinking-text')?.textContent;
        if (message && message !== current && !processingStatusQueue.includes(message)) {
            processingStatusQueue.push(message);
            flushProcessingStatus();
        }
        if (event.type === 'task_status' && event.intent && event.phase === 'running') {
            const label = progressAgentLabels[event.intent];
            if (label) updateAgentTags([{ name: event.intent, display: label }]);
        }
    }

    function flushProcessingStatus() {
        if (processingStatusTimer || !processingStatusQueue.length) return;
        const elapsed = Date.now() - lastProcessingStatusAt;
        const delay = Math.max(0, 650 - elapsed);
        processingStatusTimer = setTimeout(() => {
            processingStatusTimer = null;
            const text = document.querySelector('#processingIndicator .thinking-text');
            if (!text) return;
            const next = processingStatusQueue.shift();
            text.classList.add('is-changing');
            setTimeout(() => {
                if (!text.isConnected) return;
                text.textContent = next;
                text.classList.remove('is-changing');
                lastProcessingStatusAt = Date.now();
                flushProcessingStatus();
            }, 160);
        }, delay);
    }

    function updateAgentTags(agents) {
        const tags = document.querySelector('#processingIndicator .agent-tags');
        if (tags) renderAgentTagsInto(tags, agents);
    }

    function renderAgentTagsInto(container, agents) {
        container.replaceChildren();
        const values = Array.isArray(agents) && agents.length ? agents : [{ display: '分析中' }];
        values.forEach((agent) => {
            const tag = document.createElement('span');
            tag.className = 'agent-tag';
            tag.textContent = agent.display || agent.name || '处理中';
            container.appendChild(tag);
        });
    }

    function createStreamingMessage() {
        collapseTripIntakeCards();
        const row = createMessageShell('ai');
        const stack = row.querySelector('.msg-stack');
        const bubble = document.createElement('div');
        bubble.className = 'msg-bubble ai';
        stack.appendChild(bubble);
        chatMessages.appendChild(row);
        scrollToBottom();
        return { bubble, stack, text: '' };
    }

    function renderMessageInto(element, text) {
        element.classList.toggle(
            'structured-result',
            /(?:✈️|🚄|🏨|📋|✅|⚠️)\s*(?:\*\*)?|(?:交通建议|住宿建议|行程规划)/.test(String(text || ''))
        );
        element.replaceChildren();
        const fragment = document.createDocumentFragment();
        String(text || '').split(/(\*\*[^*]+\*\*|\n|•)/g).forEach((part) => {
            if (!part) return;
            if (part === '\n') fragment.appendChild(document.createElement('br'));
            else if (part.startsWith('**') && part.endsWith('**')) fragment.appendChild(createStrong(part.slice(2, -2)));
            else fragment.appendChild(document.createTextNode(part));
        });
        element.appendChild(fragment);
    }

    function renderUserMessageInto(element, text) {
        element.replaceChildren(document.createTextNode(String(text || '')));
    }

    function setComposerContext(placeholder) {
        contextualPlaceholder = String(placeholder || '');
        if (!isProcessing) chatInput.placeholder = contextualPlaceholder || defaultPlaceholder;
    }

    function handlePresentationFill(event) {
        const value = String(event.detail?.text || '').trim();
        if (!value || isProcessing || isOnboarding) return;
        enterChatView();
        const current = chatInput.value.trim();
        chatInput.value = current ? `${current}，${value}` : value;
        resizeInput(chatInput);
        chatInput.focus();
        chatInput.setSelectionRange(chatInput.value.length, chatInput.value.length);
    }

    function createStrong(text) {
        const strong = document.createElement('strong');
        strong.textContent = text;
        return strong;
    }

    function createMutedText(text) {
        const span = document.createElement('span');
        span.style.color = 'var(--ink-2)';
        span.style.fontSize = '11px';
        span.textContent = text;
        return span;
    }

    function createTime(value) {
        const time = document.createElement('div');
        time.className = 'msg-time';
        const date = value ? new Date(value) : new Date();
        time.textContent = Number.isNaN(date.getTime())
            ? ''
            : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        return time;
    }

    function resizeInput(input) {
        input.style.height = 'auto';
        input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
    }

    function setSendLoading(loading) {
        sendBtn.innerHTML = loading
            ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8" stroke-dasharray="28 18" style="animation:logo-turn .8s linear infinite;transform-origin:center"/></svg>'
            : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 19V5"/><path d="m6 11 6-6 6 6"/></svg>';
    }

    function scrollToBottom() {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function openSidebar() {
        sidebar.classList.add('open');
        scrim.classList.add('visible');
        loadSessions();
    }

    function closeSidebar() {
        sidebar.classList.remove('open');
        scrim.classList.remove('visible');
        sessionPopover.hidden = true;
    }

    function openSettings() {
        closeSidebar();
        settingsLayer.classList.add('open');
    }

    function closeSettings() {
        settingsLayer.classList.remove('open');
    }

    function closeLayer(id) {
        document.getElementById(id)?.classList.remove('open');
    }

    function applyStoredAppearance() {
        const theme = localStorage.getItem(THEME_KEY) || 'system';
        if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
        else document.documentElement.removeAttribute('data-theme');
        document.querySelectorAll('[data-theme-option]').forEach((button) => {
            button.classList.toggle('active', button.dataset.themeOption === theme);
        });

        const motionEnabled = localStorage.getItem(MOTION_KEY) !== 'off';
        document.getElementById('motionToggle').classList.toggle('on', motionEnabled);
        document.getElementById('motionToggle').setAttribute('aria-checked', String(motionEnabled));
        if (!motionEnabled) document.documentElement.dataset.motion = 'off';
    }

    function setTheme(theme) {
        localStorage.setItem(THEME_KEY, theme);
        if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
        else document.documentElement.removeAttribute('data-theme');
        document.querySelectorAll('[data-theme-option]').forEach((button) => {
            button.classList.toggle('active', button.dataset.themeOption === theme);
        });
    }

    function toggleMotion(event) {
        const enabled = event.currentTarget.classList.toggle('on');
        event.currentTarget.setAttribute('aria-checked', String(enabled));
        localStorage.setItem(MOTION_KEY, enabled ? 'on' : 'off');
        if (enabled) {
            document.documentElement.removeAttribute('data-motion');
            startPromptRotation();
        } else {
            document.documentElement.dataset.motion = 'off';
            stopPromptRotation();
        }
    }

    function rotatePrompt() {
        if (document.documentElement.dataset.motion === 'off') return;
        rotatingQuestion.classList.add('is-leaving');
        setTimeout(() => {
            rotationIndex = (rotationIndex + 1) % rotatingPrompts.length;
            const next = rotatingPrompts[rotationIndex];
            rotatingQuestion.classList.remove('is-leaving');
            rotatingQuestion.classList.add('is-entering');
            rotatingQuestion.textContent = next.label;
            promptRotator.dataset.prompt = next.prompt;
            setTimeout(() => rotatingQuestion.classList.remove('is-entering'), 70);
        }, 280);
    }

    function startPromptRotation() {
        stopPromptRotation();
        if (document.documentElement.dataset.motion !== 'off') {
            rotationTimer = setInterval(rotatePrompt, 3600);
        }
    }

    function stopPromptRotation() {
        clearInterval(rotationTimer);
    }

    function showToast(message) {
        clearTimeout(toastTimer);
        toast.textContent = message;
        toast.classList.add('visible');
        toastTimer = setTimeout(() => toast.classList.remove('visible'), 2200);
    }

    function parseStreamLine(line) {
        const trimmed = String(line || '').trim();
        if (!trimmed) return null;
        try {
            return JSON.parse(trimmed);
        } catch (err) {
            return null;
        }
    }

    async function fetchJson(url, options) {
        const response = await authFetch(url, options);
        const data = await response.json();
        if (!response.ok) throw createApiError(data, '请求失败', response.status);
        return data;
    }

    function getAccessToken() {
        return localStorage.getItem(ACCESS_TOKEN_KEY) || '';
    }

    function getRefreshToken() {
        return localStorage.getItem(REFRESH_TOKEN_KEY) || '';
    }

    function clearAuth() {
        localStorage.removeItem(ACCESS_TOKEN_KEY);
        localStorage.removeItem(REFRESH_TOKEN_KEY);
        localStorage.removeItem(USER_ID_KEY);
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

    function ensureAuthenticatedPath() {
        const token = getAccessToken();
        if (!token) {
            showInitError('请先登录');
            return false;
        }
        const payload = decodeJwtPayload(token);
        const tokenUserId = payload && payload.sub;
        if (!tokenUserId) {
            clearAuth();
            showInitError('登录信息无效');
            return false;
        }
        localStorage.setItem(USER_ID_KEY, String(tokenUserId));
        if (String(tokenUserId) !== userId) {
            window.location.replace(`/chat/${encodeURIComponent(tokenUserId)}`);
            return false;
        }
        return true;
    }

    async function authFetch(url, options) {
        const first = await fetchWithAccessToken(url, options);
        if (first.status !== 401) return first;
        const refreshed = await refreshAccessToken();
        if (!refreshed) {
            clearAuth();
            window.location.replace('/');
            return first;
        }
        return fetchWithAccessToken(url, options);
    }

    async function fetchWithAccessToken(url, options) {
        const headers = new Headers((options && options.headers) || {});
        const token = getAccessToken();
        if (token) headers.set('Authorization', `Bearer ${token}`);
        return fetch(url, { ...(options || {}), headers });
    }

    async function refreshAccessToken() {
        const refreshToken = getRefreshToken();
        if (!refreshToken) return false;
        try {
            const response = await fetch('/auth/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh_token: refreshToken }),
            });
            if (!response.ok) return false;
            const data = await response.json();
            const payload = decodeJwtPayload(data.access_token);
            if (!payload || String(payload.sub) !== userId) return false;
            localStorage.setItem(ACCESS_TOKEN_KEY, data.access_token);
            localStorage.setItem(REFRESH_TOKEN_KEY, data.refresh_token);
            localStorage.setItem(USER_ID_KEY, String(payload.sub));
            return true;
        } catch (err) {
            return false;
        }
    }

    class ApiError extends Error {
        constructor(status, code, message, requestId, retryable) {
            super(message);
            this.name = 'ApiError';
            this.status = status || 0;
            this.code = code || '';
            this.requestId = requestId || '';
            this.retryable = !!retryable;
        }
    }

    function createApiError(data, fallback, status) {
        const payload = getErrorPayload(data);
        return new ApiError(
            status || (data && data.status),
            payload && payload.code,
            getErrorMessage(data, fallback),
            payload && (payload.request_id || payload.requestId),
            payload && payload.retryable
        );
    }

    function getErrorPayload(data) {
        if (!data) return null;
        if (data.error && typeof data.error === 'object') return data.error;
        return data;
    }

    function getErrorMessage(data, fallback) {
        if (!data) return fallback;
        const payload = getErrorPayload(data);
        if (payload && payload.message) return payload.message;
        if (typeof data.error === 'string') return data.error;
        if (data.detail) return data.detail;
        if (data.message) return data.message;
        return fallback;
    }

    function formatDisplayError(error, fallback) {
        const message = (error && error.message) || fallback;
        return error && error.requestId ? `${message}（${error.requestId}）` : message;
    }
})();
