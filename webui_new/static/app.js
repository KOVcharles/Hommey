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
    const knowledgeWorkspace = document.getElementById('knowledgeWorkspace');
    const knowledgeList = document.getElementById('knowledgeList');
    const knowledgeSearch = document.getElementById('knowledgeSearch');
    const knowledgeEmpty = document.getElementById('knowledgeEmpty');
    const knowledgeDocument = document.getElementById('knowledgeDocument');
    const documentBody = document.getElementById('documentBody');
    const documentToc = document.getElementById('documentToc');
    const knowledgeFileInput = document.getElementById('knowledgeFileInput');
    const knowledgeUploadButton = document.getElementById('knowledgeUploadButton');
    const knowledgeRefreshButton = document.getElementById('knowledgeRefreshButton');
    const knowledgeAdminActions = document.getElementById('knowledgeAdminActions');
    const knowledgeSyncBar = document.getElementById('knowledgeSyncBar');
    const attachmentsLayer = document.getElementById('attachmentsLayer');
    const attachmentsList = document.getElementById('attachmentsList');

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
    let knowledgeDocuments = [];
    let activeKnowledgeDocumentId = '';
    let knowledgeLoaded = false;
    let knowledgeLoading = false;
    let knowledgeRefreshTimer;
    let knowledgeRefreshPollFailures = 0;
    let isKnowledgeAdmin = false;

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

    // 进度标签：优先用 GET /api/intents 动态填充，失败时退回本地保底 map。
    const progressAgentLabels = {
        rag_knowledge: '差旅标准',
        information_query: '天气与出行',
        memory_query: '差旅记录',
        preference: '偏好',
        event_collection: '行程信息',
        itinerary_planning: '行程规划',
        trip_compliance: '合规检查',
    };
    let dynamicAgentLabels = null;

    function getAgentLabel(intent) {
        if (dynamicAgentLabels && dynamicAgentLabels[intent]) return dynamicAgentLabels[intent];
        return progressAgentLabels[intent] || null;
    }

    async function loadIntentLabels() {
        try {
            const intents = await fetchJson('/api/intents');
            dynamicAgentLabels = {};
            for (const [key, meta] of Object.entries(intents)) {
                dynamicAgentLabels[key] = meta.display;
            }
        } catch (err) {
            // 保底：继续使用本地 progressAgentLabels。
        }
    }

    // 多模态附件：待发送的已上传附件 + 消息级 X-Request-ID（上传/聊天/重试共用）。
    let pendingAttachments = [];
    let currentRequestId = '';
    let retryRequestPending = false;
    let interruptPending = false;

    // 语音输入（Mode A）：MediaRecorder → 16kHz mono WAV → ASR 转写文本回填。
    let voiceRecorder = null;
    let voiceStream = null;
    let voiceChunks = [];
    let recordingButton = null;

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
        sendBtn.addEventListener('click', () => {
            if (isProcessing) interruptCurrentTurn();
            else submitCurrentInput();
        });
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
        document.getElementById('knowledgeButton').addEventListener('click', showKnowledge);
        document.getElementById('settingsButton').addEventListener('click', openSettings);
        document.getElementById('settingsClose').addEventListener('click', closeSettings);
        document.getElementById('clearHistoryButton').addEventListener('click', confirmClearHistory);
        document.getElementById('renameSessionButton').addEventListener('click', openRenameDialog);
        document.getElementById('deleteSessionButton').addEventListener('click', confirmDeleteSession);
        document.getElementById('renameForm').addEventListener('submit', renameSelectedSession);
        document.getElementById('confirmAction').addEventListener('click', runConfirmedAction);

        document.querySelectorAll('[data-voice-record]').forEach((button) => {
            button.addEventListener('click', () => toggleVoiceRecording(button));
        });
        document.querySelectorAll('[data-attachment-panel]').forEach((button) => {
            button.addEventListener('click', openAttachmentPanel);
        });
        document.getElementById('attachmentsClose').addEventListener('click', () => closeLayer('attachmentsLayer'));

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
        historySearch.addEventListener('keydown', (event) => {
            if (event.key !== 'Escape') return;
            event.preventDefault();
            setHistorySearchExpanded(false);
            document.getElementById('searchToggle').focus();
        });
        knowledgeSearch.addEventListener('input', renderKnowledgeList);
        document.getElementById('knowledgeBack').addEventListener('click', closeKnowledgeDocument);
        knowledgeUploadButton.addEventListener('click', () => {
            if (!isKnowledgeAdmin || knowledgeUploadButton.disabled) return;
            knowledgeFileInput.click();
        });
        knowledgeFileInput.addEventListener('change', () => {
            uploadKnowledgeDocuments(knowledgeFileInput.files);
            knowledgeFileInput.value = '';
        });
        knowledgeRefreshButton.addEventListener('click', startKnowledgeRefresh);
        document.getElementById('knowledgeSyncClose').addEventListener('click', () => {
            if (knowledgeSyncBar.dataset.status !== 'running') knowledgeSyncBar.hidden = true;
        });
        promptRotator.addEventListener('click', () => submitPrompt(promptRotator.dataset.prompt));
        promptRotator.addEventListener('mouseenter', stopPromptRotation);
        promptRotator.addEventListener('mouseleave', startPromptRotation);
        promptRotator.addEventListener('focusin', stopPromptRotation);
        promptRotator.addEventListener('focusout', startPromptRotation);
        document.addEventListener('hommey:fill-composer', handlePresentationFill);
        document.addEventListener('hommey:submit-message', handlePresentationSubmit);
        chatMessages.addEventListener('scroll', markConversationScrolling, { passive: true });

        document.addEventListener('click', (event) => {
            if (!sessionPopover.contains(event.target) && !event.target.closest('.session-more')) {
                sessionPopover.hidden = true;
            }
        });
        document.addEventListener('keydown', handleKnowledgeShortcut);
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

            loadIntentLabels();
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
        const hasReadyAttachments = pendingAttachments.some((attachment) => attachment.status === 'ready');
        if ((!text && !hasReadyAttachments) || isProcessing || isOnboarding) return;
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

    function handlePresentationSubmit(event) {
        const text = String(event.detail?.text || '').trim();
        if (!text || isProcessing || isOnboarding) {
            event.preventDefault();
            if (isProcessing) showToast('当前任务正在处理，请完成后再提交。');
            return;
        }
        // Card submissions use the same chat endpoint, request id, locking and
        // durable Turn path. Keep any composer draft/attachments untouched.
        sendMessage(text, {
            preserveComposer: true,
            includeAttachments: false,
            inlineSubmission: true,
        }).then((success) => {
            if (typeof event.detail?.complete === 'function') event.detail.complete(success);
        });
    }

    function enterChatView() {
        setMainView('chat');
        closeSidebar();
        requestAnimationFrame(scrollToBottom);
    }

    function showHome() {
        if (isOnboarding || isProcessing) return;
        setMainView('home');
        closeSidebar();
        setTimeout(() => homeInput.focus(), 180);
    }

    function showKnowledge() {
        if (isOnboarding) {
            showToast('完成首次设置后即可查阅知识库。');
            return;
        }
        setMainView('knowledge');
        closeSidebar();
        loadKnowledgeDocuments();
        if (isKnowledgeAdmin) loadKnowledgeRefreshStatus();
        setTimeout(() => knowledgeSearch.focus(), 180);
    }

    function setMainView(view) {
        appShell.dataset.view = view;
        document.getElementById('knowledgeButton').classList.toggle('active', view === 'knowledge');
        if (view !== 'knowledge') {
            clearTimeout(knowledgeRefreshTimer);
            knowledgeRefreshTimer = null;
            knowledgeRefreshPollFailures = 0;
        }
    }

    function handleKnowledgeShortcut(event) {
        if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'k') return;
        if (appShell.dataset.view !== 'knowledge') return;
        event.preventDefault();
        knowledgeSearch.focus();
    }

    async function loadKnowledgeDocuments(force = false) {
        if ((!force && knowledgeLoaded) || knowledgeLoading) return;
        knowledgeLoading = true;
        try {
            const data = await fetchJson('/api/knowledge/documents');
            knowledgeDocuments = Array.isArray(data.documents) ? data.documents : [];
            knowledgeLoaded = true;
            document.getElementById('knowledgeDocumentCount').textContent = String(data.total ?? knowledgeDocuments.length);
            renderKnowledgeList();
        } catch (err) {
            knowledgeList.innerHTML = '<div class="knowledge-list-state">知识库暂时无法载入。<br>请稍后再试。</div>';
            document.getElementById('knowledgeResultCount').textContent = '0';
            showToast(err.message || '知识库载入失败');
        } finally {
            knowledgeLoading = false;
        }
    }

    async function uploadKnowledgeDocuments(fileList) {
        if (!isKnowledgeAdmin) {
            showToast('仅知识库管理员可以上传制度文档。');
            return;
        }
        const files = Array.from(fileList || []);
        if (!files.length) return;
        if (files.length > 10) {
            showToast('每次最多上传 10 份文档。');
            return;
        }
        const unsupported = files.find((file) => !/\.(?:txt|md|pdf)$/i.test(file.name));
        if (unsupported) {
            showToast(`${unsupported.name} 不是支持的文档格式。`);
            return;
        }

        const formData = new FormData();
        files.forEach((file) => formData.append('files', file));
        setKnowledgeAdminBusy(true, '正在上传');
        try {
            const data = await fetchJson('/api/knowledge/documents', { method: 'POST', body: formData });
            knowledgeLoaded = false;
            await loadKnowledgeDocuments(true);
            showKnowledgeUploadResult(data.total || files.length);
            showToast(`已上传 ${data.total || files.length} 份文档`);
        } catch (err) {
            showToast(formatDisplayError(err, '文档上传失败'));
        } finally {
            setKnowledgeAdminBusy(false);
        }
    }

    async function startKnowledgeRefresh() {
        if (!isKnowledgeAdmin) {
            showToast('仅知识库管理员可以刷新数据库。');
            return;
        }
        if (knowledgeRefreshButton.classList.contains('busy')) return;
        setKnowledgeAdminBusy(true);
        try {
            const status = await fetchJson('/api/knowledge/refresh', { method: 'POST' });
            knowledgeRefreshPollFailures = 0;
            renderKnowledgeRefreshStatus(status);
            scheduleKnowledgeRefreshPoll();
        } catch (err) {
            setKnowledgeAdminBusy(false);
            showToast(formatDisplayError(err, '无法启动知识库刷新'));
            if (err.code === 'KNOWLEDGE_REFRESH_RUNNING') loadKnowledgeRefreshStatus();
        }
    }

    async function loadKnowledgeRefreshStatus() {
        if (!isKnowledgeAdmin) return;
        try {
            const status = await fetchJson('/api/knowledge/refresh/status');
            renderKnowledgeRefreshStatus(status);
            if (status.status === 'running') scheduleKnowledgeRefreshPoll();
        } catch (err) {
            // The document library remains usable even if status polling is unavailable.
        }
    }

    function scheduleKnowledgeRefreshPoll() {
        if (!isKnowledgeAdmin || appShell.dataset.view !== 'knowledge') return;
        clearTimeout(knowledgeRefreshTimer);
        knowledgeRefreshTimer = setTimeout(async () => {
            try {
                const status = await fetchJson('/api/knowledge/refresh/status');
                knowledgeRefreshPollFailures = 0;
                renderKnowledgeRefreshStatus(status);
                if (status.status === 'running') scheduleKnowledgeRefreshPoll();
                else {
                    knowledgeLoaded = false;
                    await loadKnowledgeDocuments(true);
                }
            } catch (err) {
                knowledgeRefreshPollFailures += 1;
                if (knowledgeRefreshPollFailures >= 6) {
                    setKnowledgeAdminBusy(false);
                    showToast('知识库状态暂时无法获取，请稍后重新打开知识库查看。');
                    return;
                }
                clearTimeout(knowledgeRefreshTimer);
                knowledgeRefreshTimer = setTimeout(
                    scheduleKnowledgeRefreshPoll,
                    Math.min(15000, 900 * (2 ** knowledgeRefreshPollFailures)),
                );
            }
        }, 900);
    }

    function renderKnowledgeRefreshStatus(status) {
        const state = String(status?.status || 'idle');
        if (state === 'idle' && !status?.finished_at) {
            knowledgeSyncBar.hidden = true;
            setKnowledgeAdminBusy(false);
            return;
        }

        const running = state === 'running';
        const success = state === 'success';
        const partial = state === 'partial_success';
        const report = status?.report || {};
        knowledgeSyncBar.hidden = false;
        knowledgeSyncBar.dataset.status = running ? 'running' : (success ? 'success' : (partial ? 'error' : state));
        document.getElementById('knowledgeSyncTitle').textContent = status?.stage || '知识库状态已更新';
        document.getElementById('knowledgeSyncProgress').style.width = `${Math.max(0, Math.min(100, Number(status?.progress || 0)))}%`;

        let detail = status?.message || '';
        if (running) detail = `${Number(status?.progress || 0)}% · 文档会在后台完成解析与向量化`;
        else if (success || partial) {
            detail = `${report.documents_loaded || 0} 份文档 · ${report.chunks_loaded || 0} 个检索片段${partial ? ` · ${report.errors?.length || 0} 项失败` : ''}`;
        } else if (!detail && status?.finished_at) {
            detail = `上次刷新于 ${formatDocumentDate(status.finished_at)}`;
        }
        document.getElementById('knowledgeSyncDetail').textContent = detail || '可以继续浏览当前知识库。';
        setKnowledgeAdminBusy(running);
    }

    function showKnowledgeUploadResult(count) {
        knowledgeSyncBar.hidden = false;
        knowledgeSyncBar.dataset.status = 'pending';
        document.getElementById('knowledgeSyncTitle').textContent = `已上传 ${count} 份文档，等待入库`;
        document.getElementById('knowledgeSyncDetail').textContent = '确认文档无误后，点击“刷新数据库”完成向量化。';
        document.getElementById('knowledgeSyncProgress').style.width = '0%';
    }

    function setKnowledgeAdminBusy(busy, uploadLabel) {
        knowledgeRefreshButton.classList.toggle('busy', busy);
        knowledgeRefreshButton.disabled = busy;
        knowledgeUploadButton.classList.toggle('busy', busy);
        knowledgeFileInput.disabled = busy;
        document.getElementById('knowledgeUploadLabel').textContent = uploadLabel || '上传文档';
    }

    function renderKnowledgeList() {
        if (!knowledgeLoaded) return;
        const query = knowledgeSearch.value.trim().toLocaleLowerCase('zh-CN');
        const visible = knowledgeDocuments.filter((doc) => {
            if (!query) return true;
            return [doc.title, doc.preview, doc.category_label, doc.filename]
                .some((value) => String(value || '').toLocaleLowerCase('zh-CN').includes(query));
        });

        document.getElementById('knowledgeResultCount').textContent = String(visible.length);
        knowledgeList.replaceChildren();
        if (!visible.length) {
            const empty = document.createElement('div');
            empty.className = 'knowledge-list-state';
            empty.textContent = query ? '没有找到匹配的文档。' : '知识库中还没有可查阅的文档。';
            knowledgeList.appendChild(empty);
            return;
        }

        visible.forEach((doc) => {
            const card = document.createElement('button');
            card.type = 'button';
            card.className = `knowledge-card${doc.id === activeKnowledgeDocumentId ? ' active' : ''}`;
            card.dataset.documentId = doc.id;
            card.setAttribute('aria-label', `阅读 ${doc.title}`);

            const top = document.createElement('div');
            top.className = 'knowledge-card-top';
            const category = document.createElement('span');
            category.className = 'knowledge-category';
            category.textContent = doc.category_label || '差旅资料';
            const tags = document.createElement('span');
            tags.className = 'knowledge-card-tags';
            if (doc.index_status === 'pending') {
                const indexState = document.createElement('span');
                indexState.className = 'knowledge-index-state';
                indexState.textContent = '待入库';
                tags.appendChild(indexState);
            }
            const type = document.createElement('span');
            type.className = 'knowledge-file-type';
            type.textContent = doc.file_type || 'TXT';
            tags.appendChild(type);
            top.append(category, tags);

            const title = document.createElement('h3');
            title.textContent = doc.title;
            const preview = document.createElement('p');
            preview.textContent = doc.preview || '打开查看文档内容';
            const meta = document.createElement('div');
            meta.className = 'knowledge-card-meta';
            appendMetaParts(meta, [`约 ${doc.read_minutes || 1} 分钟`, formatDocumentSize(doc.size_bytes)]);
            card.append(top, title, preview, meta);
            card.addEventListener('click', () => openKnowledgeDocument(doc.id));
            knowledgeList.appendChild(card);
        });
    }

    async function openKnowledgeDocument(documentId) {
        if (!documentId) return;
        activeKnowledgeDocumentId = documentId;
        renderKnowledgeList();
        knowledgeWorkspace.classList.add('has-document');
        knowledgeEmpty.hidden = false;
        knowledgeEmpty.querySelector('h2').textContent = '正在打开文档';
        knowledgeEmpty.querySelector('p').textContent = '正在整理章节与阅读目录。';
        knowledgeDocument.hidden = true;

        try {
            const encodedId = documentId.split('/').map(encodeURIComponent).join('/');
            const doc = await fetchJson(`/api/knowledge/documents/${encodedId}`);
            if (activeKnowledgeDocumentId !== documentId) return;
            renderKnowledgeDocument(doc);
        } catch (err) {
            if (activeKnowledgeDocumentId !== documentId) return;
            knowledgeEmpty.querySelector('h2').textContent = '文档暂时无法打开';
            knowledgeEmpty.querySelector('p').textContent = err.message || '请返回目录后重试。';
            showToast(err.message || '文档读取失败');
        }
    }

    function closeKnowledgeDocument() {
        knowledgeWorkspace.classList.remove('has-document');
        knowledgeSearch.focus();
    }

    function renderKnowledgeDocument(doc) {
        document.getElementById('documentCategory').textContent = doc.category_label || '差旅资料';
        document.getElementById('documentType').textContent = doc.file_type || 'TXT';
        document.getElementById('documentTitle').textContent = doc.title || doc.filename;
        const meta = document.getElementById('documentMeta');
        meta.replaceChildren();
        const parts = [
            doc.filename,
            `约 ${doc.read_minutes || 1} 分钟`,
            doc.page_count ? `${doc.page_count} 页` : `${Number(doc.character_count || 0).toLocaleString('zh-CN')} 字符`,
            `更新于 ${formatDocumentDate(doc.updated_at)}`,
        ];
        appendMetaParts(meta, parts);
        buildDocumentBody(doc.content || '', doc.title || '');
        knowledgeEmpty.hidden = true;
        knowledgeDocument.hidden = false;
        knowledgeDocument.closest('.knowledge-reader').scrollTop = 0;
    }

    function buildDocumentBody(content, title) {
        documentBody.replaceChildren();
        documentToc.replaceChildren();
        const fragment = document.createDocumentFragment();
        const tocEntries = [];
        let list = null;
        let titleSkipped = false;

        const flushList = () => {
            if (!list) return;
            fragment.appendChild(list);
            list = null;
        };

        String(content).replace(/\r\n?/g, '\n').split('\n').forEach((rawLine) => {
            const line = rawLine.trim();
            if (!line) {
                flushList();
                return;
            }
            const plainLine = line.replace(/^#{1,6}\s*/, '').trim();
            if (!titleSkipped && normalizeDocumentText(plainLine) === normalizeDocumentText(title)) {
                titleSkipped = true;
                return;
            }

            const markdownHeading = line.match(/^(#{1,6})\s+(.+)$/);
            const isPrimaryHeading = /^[一二三四五六七八九十百]+、\S+/.test(line);
            const isSecondaryHeading = /^\d+[.、]\s*\S+/.test(line);
            if (markdownHeading || isPrimaryHeading || isSecondaryHeading) {
                flushList();
                const level = (markdownHeading && markdownHeading[1].length >= 3) || isSecondaryHeading ? 3 : 2;
                const heading = document.createElement(level === 2 ? 'h2' : 'h3');
                heading.textContent = markdownHeading ? markdownHeading[2].trim() : line;
                if (level === 2) {
                    heading.id = `document-section-${tocEntries.length + 1}`;
                    tocEntries.push({ id: heading.id, title: heading.textContent });
                }
                fragment.appendChild(heading);
                titleSkipped = true;
                return;
            }

            const bullet = line.match(/^(?:[-*•]|[a-zA-Z][)）])\s*(.+)$/);
            if (bullet) {
                if (!list) list = document.createElement('ul');
                const item = document.createElement('li');
                item.textContent = bullet[1];
                list.appendChild(item);
                return;
            }

            flushList();
            const paragraph = document.createElement('p');
            paragraph.textContent = line;
            if (/^(?:Q\d*|A\d*|情况描述|处理步骤|特别提醒|温馨提示)[：:]/i.test(line)) {
                paragraph.className = 'document-lead';
            }
            fragment.appendChild(paragraph);
            titleSkipped = true;
        });
        flushList();
        documentBody.appendChild(fragment);

        tocEntries.slice(0, 12).forEach((entry) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = entry.title;
            button.addEventListener('click', () => document.getElementById(entry.id)?.scrollIntoView({ behavior: 'smooth' }));
            documentToc.appendChild(button);
        });
        documentToc.hidden = tocEntries.length < 2;
    }

    function normalizeDocumentText(value) {
        return String(value || '').replace(/\s+/g, '').replace(/[。；：:]/g, '').toLocaleLowerCase('zh-CN');
    }

    function appendMetaParts(container, parts) {
        parts.filter(Boolean).forEach((part, index) => {
            if (index) container.appendChild(document.createElement('i'));
            const span = document.createElement('span');
            span.textContent = part;
            container.appendChild(span);
        });
    }

    function formatDocumentSize(bytes) {
        const value = Number(bytes || 0);
        if (value < 1024) return `${value} B`;
        if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
        return `${(value / 1024 / 1024).toFixed(1)} MB`;
    }

    function formatDocumentDate(value) {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '未知日期';
        return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' }).format(date);
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
            applyKnowledgePermissions(data.role === 'admin');
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
            applyKnowledgePermissions(false);
            prefList.replaceChildren(createEmptyState('暂时无法读取偏好。'));
        }
    }

    function applyKnowledgePermissions(isAdmin) {
        isKnowledgeAdmin = !!isAdmin;
        knowledgeAdminActions.hidden = !isKnowledgeAdmin;
        if (!isKnowledgeAdmin) {
            clearTimeout(knowledgeRefreshTimer);
            knowledgeRefreshTimer = null;
            knowledgeSyncBar.hidden = true;
            setKnowledgeAdminBusy(false);
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
        setHistorySearchExpanded(!historySearchBox.classList.contains('visible'));
    }

    function setHistorySearchExpanded(expanded) {
        const toggle = document.getElementById('searchToggle');
        historySearchBox.classList.toggle('visible', expanded);
        historySearchBox.setAttribute('aria-hidden', String(!expanded));
        toggle.setAttribute('aria-expanded', String(expanded));
        toggle.classList.toggle('active', expanded);
        historySearch.tabIndex = expanded ? 0 : -1;
        if (expanded) {
            setTimeout(() => {
                if (historySearchBox.classList.contains('visible')) historySearch.focus();
            }, 140);
        } else {
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
                setMainView('home');
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
            setMainView('home');
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
        pendingContainers().forEach((c) => {
            c.replaceChildren();
            pendingAttachments.forEach((attachment) => {
                const chip = document.createElement('span');
                chip.className = attachment.status === 'failed' ? 'pending-chip failed' : 'pending-chip';
                chip.dataset.id = attachment.id || '';
                chip.dataset.tmpId = attachment.tmpId || '';
                chip.title = attachment.errorMessage
                    ? `${attachment.filename || '未命名附件'}：${attachment.errorMessage}`
                    : (attachment.filename || '');
                const label = document.createElement('span');
                label.className = 'pending-chip-label';
                label.textContent = `${attachment.filename || '未命名附件'}${attachment.status === 'failed' ? '（失败）' : ''}`;
                chip.appendChild(label);
                const remove = document.createElement('button');
                remove.type = 'button';
                remove.className = 'pending-chip-remove';
                remove.setAttribute('aria-label', `移除 ${attachment.filename || '附件'}`);
                remove.title = '移除附件';
                remove.textContent = '×';
                remove.addEventListener('click', () => {
                    pendingAttachments = pendingAttachments.filter((item) => {
                        const sameId = !!chip.dataset.id && (item.id || '') === chip.dataset.id;
                        const sameTemporaryId = !!chip.dataset.tmpId
                            && (item.tmpId || '') === chip.dataset.tmpId;
                        return !(sameId || sameTemporaryId);
                    });
                    if (retryRequestPending) {
                        resetRequestId();
                        retryRequestPending = false;
                    }
                    renderPendingAttachments();
                });
                chip.appendChild(remove);
                c.appendChild(chip);
            });
            c.style.display = pendingAttachments.length ? '' : 'none';
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
                if (entry.status === 'failed') {
                    entry.errorMessage = attachmentFailureMessage(res.error_code);
                }
            } catch (err) {
                entry.status = 'failed';
                entry.errorMessage = formatDisplayError(err, '附件上传失败');
                showToast(entry.errorMessage);
            }
            renderPendingAttachments();
        }
    }

    function attachmentFailureMessage(errorCode) {
        const messages = {
            VISION_DISABLED: '图片识别服务未开启',
            VISION_KEY_MISSING: '图片识别服务未配置',
            VISION_QUOTA_EXCEEDED: '今日图片识别次数已达上限',
            IMAGE_PARSE_FAILED: '图片识别失败',
            PARSE_FAILED: '附件解析失败',
            PERSIST_FAILED: '附件处理结果保存失败',
        };
        return messages[String(errorCode || '').toUpperCase()] || '附件处理失败';
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
            const icon = a.kind === 'image' ? '🖼' : '📎';
            card.innerHTML = `<span class="attachment-icon" aria-hidden="true">${icon}</span><span class="attachment-name">${escapeHtml(a.filename)}</span>`;
            wrap.appendChild(card);
        });
        stack.appendChild(wrap);
    }

    // ---- 附件面板：查看 / 下载 / 引用 / 删除 ---------------------------------

    async function openAttachmentPanel() {
        attachmentsLayer.classList.add('open');
        attachmentsList.innerHTML = '<div class="empty-state">正在读取附件…</div>';
        try {
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/attachments?limit=100`);
            renderAttachmentsList(data.attachments || []);
        } catch (err) {
            attachmentsList.innerHTML = '<div class="empty-state">读取附件失败，请重试。</div>';
            showToast(formatDisplayError(err, '读取附件失败'));
        }
    }

    function renderAttachmentsList(attachments) {
        attachmentsList.replaceChildren();
        if (!attachments || !attachments.length) {
            attachmentsList.innerHTML = '<div class="empty-state">还没有上传过附件。</div>';
            return;
        }
        attachments.forEach((att) => attachmentsList.appendChild(createAttachmentRow(att)));
    }

    function createAttachmentRow(att) {
        const row = document.createElement('div');
        row.className = 'attachment-item';
        row.dataset.id = att.id;

        const icon = document.createElement('span');
        icon.className = 'attachment-item-icon';
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = att.kind === 'image' ? '🖼' : '📄';

        const main = document.createElement('span');
        main.className = 'attachment-item-main';
        const name = document.createElement('span');
        name.className = 'attachment-item-name';
        name.textContent = att.filename || '未命名附件';
        name.title = att.filename || '';
        const meta = document.createElement('span');
        meta.className = 'attachment-item-meta';
        meta.textContent = `${attachmentStatusLabel(att)} · ${formatBytes(att.size_bytes)}`;
        main.append(name, meta);

        const actions = document.createElement('span');
        actions.className = 'attachment-item-actions';

        const downloadBtn = document.createElement('button');
        downloadBtn.type = 'button';
        downloadBtn.className = 'attachment-action';
        downloadBtn.textContent = '下载';
        downloadBtn.setAttribute('aria-label', `下载 ${att.filename}`);
        downloadBtn.addEventListener('click', () => downloadAttachment(att));
        actions.appendChild(downloadBtn);

        if (att.status === 'ready') {
            const attachBtn = document.createElement('button');
            attachBtn.type = 'button';
            attachBtn.className = 'attachment-action primary';
            attachBtn.textContent = '引用';
            attachBtn.setAttribute('aria-label', `引用 ${att.filename} 到新消息`);
            attachBtn.addEventListener('click', () => reAttachAttachment(att));
            actions.appendChild(attachBtn);
        }

        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.className = 'attachment-action danger';
        deleteBtn.textContent = '删除';
        deleteBtn.setAttribute('aria-label', `删除 ${att.filename}`);
        deleteBtn.addEventListener('click', () => deleteAttachment(att));
        actions.appendChild(deleteBtn);

        row.append(icon, main, actions);
        return row;
    }

    function attachmentStatusLabel(att) {
        const labels = { ready: '可用', failed: '解析失败', processing: '处理中', expired: '已过期' };
        return labels[att.status] || att.status || '未知';
    }

    function formatBytes(bytes) {
        const n = Number(bytes) || 0;
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    }

    function reAttachAttachment(att) {
        if (att.status !== 'ready') { showToast('该附件暂不可用'); return; }
        if (pendingAttachments.some((item) => item.id === att.id)) {
            showToast('该附件已在待发送列表中');
            return;
        }
        pendingAttachments.push({ id: att.id, filename: att.filename, kind: att.kind, status: 'ready' });
        if (retryRequestPending) {
            resetRequestId();
            retryRequestPending = false;
        }
        renderPendingAttachments();
        closeLayer('attachmentsLayer');
        enterChatView();
        showToast('已加入待发送附件');
    }

    async function downloadAttachment(att) {
        try {
            const response = await authFetch(
                `/api/${encodeURIComponent(userId)}/attachments/${encodeURIComponent(att.id)}/content`
            );
            if (!response.ok) {
                let data = null;
                try { data = await response.json(); } catch (e) { data = null; }
                throw createApiError(data, '下载失败', response.status);
            }
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = att.filename || 'attachment';
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            setTimeout(() => URL.revokeObjectURL(url), 4000);
        } catch (err) {
            showToast(formatDisplayError(err, '下载失败'));
        }
    }

    function deleteAttachment(att) {
        openConfirm(
            '删除附件？',
            `「${att.filename}」删除后不可恢复，已关联消息中的该附件也将一并移除。`,
            async () => {
                await fetchJson(
                    `/api/${encodeURIComponent(userId)}/attachments/${encodeURIComponent(att.id)}`,
                    { method: 'DELETE' }
                );
                showToast('附件已删除');
                await openAttachmentPanel();
            }
        );
    }

    async function sendMessage(explicitText, options = {}) {
        const hasExplicitText = typeof explicitText === 'string';
        const text = hasExplicitText ? explicitText.trim() : chatInput.value.trim();
        const includeAttachments = options.includeAttachments !== false;
        const hasAttachments = includeAttachments && pendingAttachments.some((a) => a.status === 'ready');
        if ((!text && !hasAttachments) || isProcessing || isOnboarding) return;
        const sendingEntries = includeAttachments
            ? pendingAttachments.filter((a) => a.status === 'ready')
            : [];
        const sendingAttachmentIds = sendingEntries.map((a) => a.id);
        const sendingAttachments = sendingEntries.map((a) => ({ filename: a.filename, kind: a.kind }));
        let requestCompleted = false;
        enterChatView();
        addMessage('user', text, undefined, sendingAttachments);
        if (!options.preserveComposer) {
            chatInput.value = '';
            resizeInput(chatInput);
        }
        if (includeAttachments) {
            pendingAttachments = [];
            renderPendingAttachments();
        }
        isProcessing = true;
        interruptPending = false;
        sendBtn.disabled = false;
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
            let turnInterrupted = false;
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
                    if (event.type === 'interrupted') {
                        turnInterrupted = true;
                        removeProcessingIndicator();
                        addMessage('ai', '已停止当前执行。输入“继续”可以从最近一次安全状态接着完成。');
                    }
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
            if (!streamMessage && !presentationRendered && !turnInterrupted) {
                addMessage('ai', '我收到了，但这次没有返回具体内容。');
            }
            if (streamMessage && responseSources.length) {
                renderAttachmentCards(streamMessage.stack, responseSources);
            }
            setComposerContext(nextPlaceholder);
            if (preferencesUpdated) await loadUserSummary();
            await Promise.all([loadActiveTrip(), loadSessions()]);
            requestCompleted = true;
        } catch (err) {
            removeProcessingIndicator();
            const errorText = formatDisplayError(err, '网络错误，请检查连接后重试。');
            if (options.inlineSubmission) showToast(errorText);
            else addMessage('ai', errorText);
            // Preserve the body and request ID so an explicit retry remains
            // idempotent. Inline card submissions retain their values in-card
            // and never overwrite an unrelated composer draft.
            if (!options.preserveComposer) chatInput.value = text;
            if (includeAttachments) {
                const sendingIds = new Set(sendingAttachmentIds);
                pendingAttachments = [
                    ...sendingEntries,
                    ...pendingAttachments.filter((entry) => !sendingIds.has(entry.id)),
                ];
            }
            retryRequestPending = true;
            if (includeAttachments) renderPendingAttachments();
            if (!options.preserveComposer) resizeInput(chatInput);
        } finally {
            isProcessing = false;
            interruptPending = false;
            sendBtn.disabled = false;
            setSendLoading(false);
            chatInput.placeholder = contextualPlaceholder || defaultPlaceholder;
            if (requestCompleted) {
                resetRequestId();
                retryRequestPending = false;
            }
            chatInput.focus();
        }
        return requestCompleted;
    }

    async function interruptCurrentTurn() {
        if (!isProcessing || interruptPending || !currentRequestId) return;
        interruptPending = true;
        setSendLoading(true, true);
        try {
            const response = await authFetch(
                `/api/${encodeURIComponent(userId)}/orchestration/interrupt`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        client_request_id: currentRequestId,
                        session_id: activeSessionId || null,
                    }),
                },
            );
            if (!response.ok) {
                const error = await response.json();
                throw createApiError(error, '停止失败，请重试', response.status);
            }
            chatInput.placeholder = '正在停止当前执行…';
        } catch (err) {
            interruptPending = false;
            setSendLoading(true);
            showToast(formatDisplayError(err, '停止失败，请重试。'));
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
            const label = getAgentLabel(event.intent);
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

    // ---- 语音输入（Mode A）--------------------------------------------------

    const MIC_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>';
    const REC_STOP_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>';

    async function toggleVoiceRecording(button) {
        if (recordingButton) {
            stopVoiceRecording();
            return;
        }
        if (isProcessing || isOnboarding) {
            showToast('当前正在处理，请稍后再试');
            return;
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            showToast('当前浏览器不支持录音');
            return;
        }
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            voiceStream = stream;
            voiceChunks = [];
            const recorder = new MediaRecorder(stream);
            voiceRecorder = recorder;
            recorder.ondataavailable = (e) => { if (e.data && e.data.size) voiceChunks.push(e.data); };
            recorder.onstop = () => finishVoiceRecording();
            recorder.start();
            recordingButton = button;
            setRecordingUI(button, true);
        } catch (err) {
            showToast('无法使用麦克风，请检查浏览器权限');
        }
    }

    function stopVoiceRecording() {
        const button = recordingButton;
        if (!button) return;
        if (voiceRecorder && voiceRecorder.state !== 'inactive') {
            voiceRecorder.stop(); // onstop → finishVoiceRecording
        } else {
            finishVoiceRecording();
        }
    }

    function setRecordingUI(button, recording) {
        if (!button) return;
        button.classList.toggle('is-recording', recording);
        button.setAttribute('aria-label', recording ? '停止录音' : '语音输入');
        button.title = recording ? '停止录音' : '语音输入';
        button.innerHTML = recording ? REC_STOP_ICON : MIC_ICON;
    }

    async function finishVoiceRecording() {
        const button = recordingButton;
        const recorder = voiceRecorder;
        recordingButton = null;
        voiceRecorder = null;
        if (voiceStream) {
            voiceStream.getTracks().forEach((track) => track.stop());
            voiceStream = null;
        }
        if (button) setRecordingUI(button, false);
        const blob = new Blob(voiceChunks, { type: (recorder && recorder.mimeType) || 'audio/webm' });
        voiceChunks = [];
        if (!blob.size) {
            showToast('没有录到声音，请重试');
            return;
        }
        try {
            const wav = await blobToWav16k(blob);
            const text = await transcribeAudio(wav);
            if (text) {
                backfillComposer(text);
            } else {
                showToast('没有识别到语音内容，请再试一次');
            }
        } catch (err) {
            showToast(formatDisplayError(err, '语音识别失败'));
        }
    }

    async function transcribeAudio(blob) {
        const form = new FormData();
        form.append('file', blob, 'voice.wav');
        const response = await authFetch(`/api/${encodeURIComponent(userId)}/asr/transcribe`, {
            method: 'POST',
            headers: { 'X-Request-ID': ensureRequestId() },
            body: form,
        });
        if (!response.ok) {
            let data = null;
            try { data = await response.json(); } catch (e) { data = null; }
            throw createApiError(data, '语音识别失败', response.status);
        }
        const data = await response.json();
        return String(data.text || '').trim();
    }

    async function blobToWav16k(blob) {
        const arrayBuffer = await blob.arrayBuffer();
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        const ctx = new AudioCtx();
        try {
            const decoded = await ctx.decodeAudioData(arrayBuffer);
            const targetRate = 16000;
            const length = Math.max(1, Math.ceil(decoded.duration * targetRate));
            const offline = new OfflineAudioContext(1, length, targetRate);
            const source = offline.createBufferSource();
            source.buffer = decoded;
            source.connect(offline.destination);
            source.start(0);
            const rendered = await offline.startRendering();
            return encodeWav(rendered.getChannelData(0), targetRate);
        } finally {
            if (ctx.close) ctx.close();
        }
    }

    function encodeWav(samples, sampleRate) {
        const dataSize = samples.length * 2;
        const buffer = new ArrayBuffer(44 + dataSize);
        const view = new DataView(buffer);
        const writeString = (offset, text) => {
            for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
        };
        writeString(0, 'RIFF');
        view.setUint32(4, 36 + dataSize, true);
        writeString(8, 'WAVE');
        writeString(12, 'fmt ');
        view.setUint32(16, 16, true);
        view.setUint16(20, 1, true);            // PCM
        view.setUint16(22, 1, true);            // mono
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * 2, true); // byte rate
        view.setUint16(32, 2, true);            // block align
        view.setUint16(34, 16, true);           // bits per sample
        writeString(36, 'data');
        view.setUint32(40, dataSize, true);
        let offset = 44;
        for (let i = 0; i < samples.length; i++) {
            const s = Math.max(-1, Math.min(1, samples[i]));
            view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
            offset += 2;
        }
        return new Blob([buffer], { type: 'audio/wav' });
    }

    function backfillComposer(text) {
        if (!text || isProcessing || isOnboarding) return;
        enterChatView();
        const current = chatInput.value.trim();
        chatInput.value = current ? `${current}，${text}` : text;
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

    function setSendLoading(loading, stopping = false) {
        sendBtn.classList.toggle('is-running', loading);
        sendBtn.classList.toggle('is-stopping', stopping);
        sendBtn.setAttribute('aria-label', loading ? (stopping ? '正在停止' : '停止生成') : '发送');
        sendBtn.title = loading ? (stopping ? '正在停止' : '停止生成') : '发送';
        sendBtn.innerHTML = loading
            ? (stopping
                ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8" stroke-dasharray="28 18" style="animation:logo-turn .8s linear infinite;transform-origin:center"/></svg>'
                : '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5"/></svg>')
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
