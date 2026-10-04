/**
 * Hommey WebUI
 * Production interaction layer for authentication, streaming chat,
 * session history, appearance settings, and responsive navigation.
 */
(function () {
    'use strict';

    const userId = String(document.body.dataset.userId || '');
    let userSummaryName = userId;
    const appShell = document.getElementById('appShell');
    const chatMessages = document.getElementById('chatMessages');
    const chatInput = document.getElementById('chatInput');
    const sendBtn = document.getElementById('sendBtn');
    const homeComposer = document.getElementById('homeComposer');
    const homeInput = document.getElementById('homeInput');
    const homeSendBtn = document.getElementById('homeSendBtn');
    const initOverlay = document.getElementById('initOverlay');
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
    let settingsReturnFocus = null;
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
    const knowledgeExitButton = document.getElementById('knowledgeExitButton');
    const knowledgeExitLabel = document.getElementById('knowledgeExitLabel');
    const attachmentsLayer = document.getElementById('attachmentsLayer');
    const attachmentsList = document.getElementById('attachmentsList');
    const quickTripLayer = document.getElementById('quickTripLayer');
    const quickTripForm = document.getElementById('quickTripForm');
    const quickTripWorkLocation = document.getElementById('quickTripWorkLocation');
    const quickTripWorkLocationId = document.getElementById('quickTripWorkLocationId');
    const quickTripPlaceSuggestions = document.getElementById('quickTripPlaceSuggestions');
    const quickTripPlaceStatus = document.getElementById('quickTripPlaceStatus');
    const retrievalModeControls = Array.from(document.querySelectorAll('[data-retrieval-mode-control]'));

    const ACCESS_TOKEN_KEY = 'hommey.access_token';
    const REFRESH_TOKEN_KEY = 'hommey.refresh_token';
    const USER_ID_KEY = 'hommey.user_id';
    const THEME_KEY = 'hommey.theme';
    const MOTION_KEY = 'hommey.motion';
    const RETRIEVAL_MODE_KEY_PREFIX = 'hommey.retrieval_mode';
    // 会话边界跟着“这一次浏览”走（见 loadSessions），不跟空闲时间走。
    const SESSION_MEMORY_KEY_PREFIX = 'hommey.session';
    const defaultPlaceholder = '继续问 Hommey';

    // 当前显示哪个会话的指针。会话自己的全部状态都在 HommeySessionRuntime 里，
    // 这里只回答"现在看的是哪一个"，不再承载会话状态。
    function activeSessionId() { return window.HommeySessionRuntime.activeId(); }
    function setActiveSession(sessionId) {
        window.HommeySessionRuntime.setActive(sessionId || '');
        return sessionId || '';
    }
    function activeRuntime() { return window.HommeySessionRuntime.active(); }
    // 取代原来的全局 isProcessing：问的是"当前查看的会话是否在跑"。
    // 别的会话在跑不影响这里。
    function isActiveProcessing() {
        const runtime = activeRuntime();
        return !!(runtime && runtime.processing);
    }
    let selectedSessionId = '';
    let confirmCallback = null;
    let rotationTimer;
    let rotationIndex = 0;
    let toastTimer;
    let scrollIdleTimer;
    let knowledgeDocuments = [];
    let activeKnowledgeDocumentId = '';
    let knowledgeLoaded = false;
    let knowledgeLoading = false;
    let knowledgeRefreshTimer;
    let knowledgeRefreshPollFailures = 0;
    let isKnowledgeAdmin = false;
    let knowledgeReturnView = 'home';
    let retrievalMode = 'standard';
    let routeMotionController = null;
    let quickTripSearchTimer = null;
    let quickTripSearchController = null;
    let quickTripMap = null;

    const progressMessages = {
        request_analyzing: '正在理解你的需求',
        tasks_decomposing: '正在准备这次出行所需的信息',
        policy_searching: '正在检索适用的报销与差旅制度',
        travel_info_searching: '正在查询目的地天气与出行信息',
        train_query_searching: '正在查询高铁车次与余票',
        memory_searching: '正在查找相关差旅记录',
        preference_updating: '正在更新你的差旅偏好',
        trip_details_collecting: '正在整理行程信息',
        trip_planning: '正在生成行程安排',
        compliance_checking: '正在核对差旅合规性',
        task_completed: '一项信息已经准备好，继续整理中',
        task_failed: '部分信息暂时不可用，继续处理其他内容',
        answer_composing: '正在整理出行建议',
        answer_ready: '信息已整理完成',
        task_running: '正在处理相关信息',
        queued: '任务已经排好，马上开始',
    };

    // 进度标签：优先用 GET /api/intents 动态填充，失败时退回本地保底 map。
    const progressAgentLabels = {
        rag_knowledge: '制度咨询',
        information_query: '天气与出行',
        train_query: '高铁车次',
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

    // 多模态附件：待发送的已上传附件，属于草稿的一部分，跟着会话走。
    // 请求 ID、重试状态、中断状态都在各自的 SessionRuntime 上，不在这里。
    // 首页（还没进任何会话）也有一个输入区，它的草稿先记在这个匿名草稿位，
    // 进会话时由 mountSession 搬过去。
    const looseDraft = { text: '', attachments: [], placeholder: '' };

    function draftSlot(runtime) {
        if (!runtime) return looseDraft;
        if (!runtime.draft.attachments) runtime.draft.attachments = [];
        return runtime.draft;
    }

    // 当前输入区正在编辑的那份草稿。附件卡片、上传回执都写到这里。
    function activeAttachments() {
        return draftSlot(window.HommeySessionRuntime.active()).attachments;
    }

    // 语音输入（Mode A）：MediaRecorder → 16kHz mono WAV → ASR 转写文本回填。
    let voiceRecorder = null;
    let voiceStream = null;
    let voiceChunks = [];
    let recordingButton = null;

    const rotatingPrompts = [
        { label: '版面费报销需要什么材料？', prompt: '版面费报销需要什么材料？' },
        { label: '电子发票可以上传截图吗？', prompt: '电子发票可以上传截图报销吗？' },
        { label: '下周一去上海两天，帮我安排一下', prompt: '下周一去上海出差两天，帮我规划行程' },
        { label: '查一下北京的住宿和交通标准', prompt: '北京出差的住宿和交通标准是什么' },
        { label: '找到我上次去深圳的差旅行程', prompt: '查看我上次去深圳的差旅行程' },
        { label: '看看上海下周一的天气', prompt: '上海下周一的天气怎么样' },
        { label: '明早到虹桥，几点出门更稳妥？', prompt: '明早九点到上海虹桥站，建议我几点出门' },
        { label: '准备一条航班延误的备选路线', prompt: '如果航班延误，帮我准备一条备选路线' },
    ];

    applyStoredAppearance();
    // 会话运行时要往 #chatMessages 里挂各会话自己的画布。
    window.HommeySessionRuntime.configure({ mountPoint: chatMessages });
    bindEvents();
    routeMotionController = initializeInteractiveRoute();
    document.addEventListener('DOMContentLoaded', initialize);

    function initializeInteractiveRoute() {
        const svg = document.querySelector('.route-intro');
        const path = svg?.querySelector('.route-track');
        const progressPath = svg?.querySelector('.route-progress');
        const progressGradient = svg?.querySelector('#route-progress-gradient');
        const traveller = svg?.querySelector('.traveller');
        const hitArea = svg?.querySelector('.route-hit-area');
        if (!svg || !path || !progressPath || !traveller || !hitArea) return null;

        const travelDuration = 5200;
        const startHoldDuration = 420;
        const arrivalHoldDuration = 1100;
        const fadeDuration = 340;
        const restartPauseDuration = 260;
        const totalLength = path.getTotalLength();
        const trailLength = Math.min(150, totalLength * .32);
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
        const state = {
            enabled: true,
            mode: 'auto',
            progress: 0,
            targetProgress: 0,
            opacity: 0,
            travelStartedAt: performance.now() + startHoldDuration,
            frame: 0,
            pausedAt: 0,
        };

        progressPath.style.strokeDasharray = `${trailLength} ${totalLength + trailLength}`;

        function motionEnabled() {
            return state.enabled
                && document.documentElement.dataset.motion !== 'off'
                && !reducedMotion.matches;
        }

        function draw() {
            const distance = Math.max(0, Math.min(totalLength, state.progress * totalLength));
            const point = path.getPointAtLength(distance);
            traveller.setAttribute('cx', point.x.toFixed(2));
            traveller.setAttribute('cy', point.y.toFixed(2));
            traveller.style.opacity = String(state.opacity);
            progressPath.style.opacity = String(state.opacity);
            // The original used two independent animations. Varying the
            // traveller's position inside the dash restores that layered motion.
            const travellerAnchor = Math.max(.07, Math.pow(1 - state.progress, 2.2));
            const segmentStart = distance - trailLength * travellerAnchor;
            progressPath.style.strokeDashoffset = String(-segmentStart);
            if (progressGradient) {
                const startPoint = path.getPointAtLength(Math.max(0, segmentStart));
                const endPoint = path.getPointAtLength(Math.min(totalLength, segmentStart + trailLength));
                progressGradient.setAttribute('x1', startPoint.x.toFixed(2));
                progressGradient.setAttribute('y1', startPoint.y.toFixed(2));
                progressGradient.setAttribute('x2', endPoint.x.toFixed(2));
                progressGradient.setAttribute('y2', endPoint.y.toFixed(2));
            }
            svg.classList.toggle('is-arrived', state.progress >= .997 && state.opacity > .02);
        }

        function resetCycle(now) {
            state.progress = 0;
            state.opacity = 0;
            state.travelStartedAt = now + startHoldDuration;
        }

        function updateAuto(now) {
            const elapsed = now - state.travelStartedAt;
            if (elapsed < 0) {
                state.progress = 0;
                state.opacity = Math.max(0, 1 + elapsed / fadeDuration);
                return;
            }
            if (elapsed <= travelDuration) {
                state.progress = elapsed / travelDuration;
                state.opacity = Math.min(1, elapsed / fadeDuration);
                return;
            }
            const afterArrival = elapsed - travelDuration;
            state.progress = 1;
            if (afterArrival <= arrivalHoldDuration) {
                state.opacity = 1;
                return;
            }
            const fading = afterArrival - arrivalHoldDuration;
            if (fading <= fadeDuration) {
                state.opacity = Math.max(0, 1 - fading / fadeDuration);
                return;
            }
            if (fading <= fadeDuration + restartPauseDuration) {
                state.opacity = 0;
                return;
            }
            resetCycle(now);
        }

        function tick(now) {
            if (!motionEnabled() || document.hidden) {
                state.frame = 0;
                return;
            }
            if (state.mode === 'pointer') {
                state.progress += (state.targetProgress - state.progress) * .24;
                if (Math.abs(state.targetProgress - state.progress) < .0005) {
                    state.progress = state.targetProgress;
                }
                state.opacity += (1 - state.opacity) * .28;
            } else {
                updateAuto(now);
            }
            draw();
            state.frame = requestAnimationFrame(tick);
        }

        function ensureRunning() {
            if (!state.frame && motionEnabled() && !document.hidden) {
                state.frame = requestAnimationFrame(tick);
            }
        }

        function closestProgress(clientX, clientY) {
            const matrix = svg.getScreenCTM();
            if (!matrix) return state.progress;
            const cursor = svg.createSVGPoint();
            cursor.x = clientX;
            cursor.y = clientY;
            const localCursor = cursor.matrixTransform(matrix.inverse());
            const samples = 56;
            let bestLength = 0;
            let bestDistance = Infinity;
            for (let index = 0; index <= samples; index += 1) {
                const length = totalLength * index / samples;
                const point = path.getPointAtLength(length);
                const distance = (point.x - localCursor.x) ** 2 + (point.y - localCursor.y) ** 2;
                if (distance < bestDistance) {
                    bestDistance = distance;
                    bestLength = length;
                }
            }
            let step = totalLength / samples;
            for (let index = 0; index < 6; index += 1) {
                [-step, step].forEach((offset) => {
                    const length = Math.max(0, Math.min(totalLength, bestLength + offset));
                    const point = path.getPointAtLength(length);
                    const distance = (point.x - localCursor.x) ** 2 + (point.y - localCursor.y) ** 2;
                    if (distance < bestDistance) {
                        bestDistance = distance;
                        bestLength = length;
                    }
                });
                step *= .5;
            }
            return bestLength / totalLength;
        }

        function takeControl(event) {
            if (event.pointerType && event.pointerType !== 'mouse') return;
            if (!motionEnabled()) return;
            state.mode = 'pointer';
            state.targetProgress = closestProgress(event.clientX, event.clientY);
            svg.classList.add('is-interacting');
            ensureRunning();
        }

        function movePointer(event) {
            if (state.mode !== 'pointer') return;
            state.targetProgress = closestProgress(event.clientX, event.clientY);
        }

        function resumeAutoFromCurrent(now = performance.now()) {
            state.mode = 'auto';
            state.travelStartedAt = state.progress >= .997
                ? now - travelDuration
                : now - state.progress * travelDuration;
            svg.classList.remove('is-interacting');
        }

        function releaseControl() {
            if (state.mode !== 'pointer') return;
            resumeAutoFromCurrent();
            ensureRunning();
        }

        function setEnabled(enabled) {
            state.enabled = enabled;
            if (!motionEnabled()) {
                if (state.frame) cancelAnimationFrame(state.frame);
                state.frame = 0;
                state.mode = 'auto';
                state.progress = .08;
                state.opacity = 1;
                svg.classList.remove('is-interacting', 'is-arrived');
                draw();
                return;
            }
            resetCycle(performance.now());
            ensureRunning();
        }

        hitArea.addEventListener('pointerenter', takeControl);
        hitArea.addEventListener('pointermove', movePointer);
        hitArea.addEventListener('pointerleave', releaseControl);
        window.addEventListener('blur', releaseControl);
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                const now = performance.now();
                if (state.mode === 'pointer') resumeAutoFromCurrent(now);
                state.pausedAt = now;
                if (state.frame) cancelAnimationFrame(state.frame);
                state.frame = 0;
                return;
            }
            if (state.pausedAt && state.mode === 'auto') {
                state.travelStartedAt += performance.now() - state.pausedAt;
            }
            state.pausedAt = 0;
            ensureRunning();
        });
        reducedMotion.addEventListener?.('change', () => setEnabled(state.enabled));
        setEnabled(true);
        return { setEnabled };
    }

    function bindEvents() {
        chatInput.addEventListener('input', () => {
            resizeInput(chatInput);
            const runtime = activeRuntime();
            if (runtime?.retryRequestPending) {
                resetRequestId(runtime);
                runtime.retryRequestPending = false;
            }
        });
        homeInput.addEventListener('input', () => resizeInput(homeInput));
        chatInput.addEventListener('keydown', handleComposerKeydown);
        homeInput.addEventListener('keydown', handleComposerKeydown);
        sendBtn.addEventListener('click', () => {
            if (isActiveProcessing()) interruptCurrentTurn();
            else submitCurrentInput();
        });
        homeComposer.addEventListener('submit', (event) => {
            event.preventDefault();
            submitHomeInput();
        });

        retrievalModeControls.forEach((control) => {
            const trigger = control.querySelector('[data-retrieval-mode-trigger]');
            const menu = control.querySelector('[data-retrieval-mode-menu]');
            trigger?.addEventListener('click', (event) => {
                event.stopPropagation();
                if (trigger.disabled) return;
                const opening = !menu.classList.contains('is-open');
                closeRetrievalModeMenus();
                if (opening) {
                    clearTimeout(menu._retrievalCloseTimer);
                    menu.hidden = false;
                    menu.setAttribute('aria-hidden', 'false');
                    trigger.setAttribute('aria-expanded', 'true');
                    requestAnimationFrame(() => {
                        menu.classList.add('is-open');
                        menu.querySelector(`[data-retrieval-mode-option="${retrievalMode}"]`)?.focus({ preventScroll: true });
                    });
                }
            });
            control.querySelectorAll('[data-retrieval-mode-option]').forEach((option) => {
                option.addEventListener('click', (event) => {
                    event.stopPropagation();
                    setRetrievalMode(option.dataset.retrievalModeOption, { persist: true });
                    closeRetrievalModeMenus();
                    trigger?.focus({ preventScroll: true });
                });
            });
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

        window.HommeyWorkspace.configure({
            open: openSidebar, openSession, startTrip: openQuickTrip,
            search: setHistorySearchExpanded, refresh: refreshSessionList,
        });
        document.getElementById('sidebarToggle').addEventListener('click', event => openSidebar(event.currentTarget));
        document.getElementById('accountButton').addEventListener('click', openSettings);
        document.getElementById('sidebarClose').addEventListener('click', closeSidebar);
        scrim.addEventListener('click', closeSidebar);
        document.getElementById('homeButton').addEventListener('click', showHome);
        document.getElementById('newChatButton').addEventListener('click', createNewSession);
        document.getElementById('workspaceNewChat').addEventListener('click', createNewSession);
        document.getElementById('searchToggle').addEventListener('click', () => window.HommeyWorkspace.showHistory(true));
        document.getElementById('knowledgeButton').addEventListener('click', showKnowledge);
        knowledgeExitButton.addEventListener('click', returnFromKnowledge);
        document.getElementById('settingsButton').addEventListener('click', openSettings);
        document.getElementById('settingsClose').addEventListener('click', closeSettings);
        bindSettingsNavigation();
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
        document.querySelectorAll('[data-quick-trip-open]').forEach((button) => {
            button.addEventListener('click', openQuickTrip);
        });
        quickTripForm.addEventListener('submit', submitQuickTrip);
        quickTripWorkLocation.addEventListener('input', handleQuickTripPlaceInput);

        document.querySelectorAll('[data-close-layer]').forEach((button) => {
            button.addEventListener('click', () => closeLayer(button.dataset.closeLayer));
        });
        document.querySelectorAll('.modal-layer').forEach((layer) => {
            layer.addEventListener('click', (event) => {
                if (event.target === layer && layer.id !== 'personalProfileLayer') closeLayer(layer.id);
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
        window.HommeyTripChoices?.configure({ searchPlaces: async (city, keyword, signal) => {
            const params = new URLSearchParams({ city, keyword });
            const response = await authFetch(`/api/${encodeURIComponent(userId)}/places/suggest?${params}`, { signal });
            const data = await response.json();
            if (!response.ok) throw createApiError(data, '地点查询失败', response.status);
            return data.items || [];
        } });
        window.HommeyJourneyMap?.configure({ loadMap: async (city, place_id, zoom, signal) => {
            const params = new URLSearchParams({city, place_id, zoom});
            const response = await authFetch(`/api/${encodeURIComponent(userId)}/places/map?${params}`, {signal});
            if (!response.ok) throw new Error('地图暂时不可用');
            return response.blob();
        } });
        const quickMapMount = document.getElementById('quickTripMapPreview');
        if (quickMapMount && window.HommeyJourneyMap) { quickTripMap=window.HommeyJourneyMap.create(); quickMapMount.append(quickTripMap); }
        quickTripWorkLocation.closest('.place-picker').addEventListener('keydown', event => {
            if (event.key==='Escape') {hideQuickTripSuggestions();quickTripWorkLocation.focus();return;}
            if (!['ArrowDown','ArrowUp'].includes(event.key) || quickTripPlaceSuggestions.hidden) return;
            const options=[...quickTripPlaceSuggestions.querySelectorAll('button')]; if(!options.length)return;
            event.preventDefault(); const index=options.indexOf(document.activeElement);
            options[(index+(event.key==='ArrowDown'?1: index<0?0:-1)+options.length)%options.length].focus();
        });
        document.getElementById('quickTripDestination').addEventListener('input', () => {
            clearTimeout(quickTripSearchTimer);
            quickTripSearchController?.abort();
            quickTripWorkLocationId.value = '';
            quickTripWorkLocation.value = '';
            quickTripMap?.setData({anchor:null}); if(quickMapMount)quickMapMount.hidden=true;
            hideQuickTripSuggestions();
            setQuickTripPlaceStatus('目的地已改变，请在新的城市范围内重新选择工作地点。');
        });
        // 滚动发生在各会话自己的 .session-view 上，切走再切回时滚动位置天然保留。
        // scroll 不冒泡，所以在挂载点用捕获阶段接。
        chatMessages.addEventListener('scroll', (event) => {
            const view = event.target.closest?.('.session-view');
            if (!view) return;
            const runtime = window.HommeySessionRuntime.get(view.dataset.sessionId);
            if (!runtime) return;
            markConversationScrolling(view);
            runtime.followConversation = view.scrollHeight - view.scrollTop - view.clientHeight < 96;
        }, { passive: true, capture: true });

        document.addEventListener('click', (event) => {
            if (!event.target.closest('[data-retrieval-mode-control]')) closeRetrievalModeMenus();
            if (!event.target.closest('.place-picker')) hideQuickTripSuggestions();
            if (!sessionPopover.contains(event.target) && !event.target.closest('.session-more')) {
                sessionPopover.hidden = true;
            }
        });
        document.addEventListener('keydown', handleKnowledgeShortcut);
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') closeRetrievalModeMenus();
            if (event.key === 'Escape' && quickTripLayer.classList.contains('open')) closeLayer('quickTripLayer');
        });
    }

    function retrievalModeStorageKey(sessionId = activeSessionId()) {
        return `${RETRIEVAL_MODE_KEY_PREFIX}.${userId}.${sessionId || 'pending'}`;
    }

    function restoreRetrievalMode() {
        let stored = 'standard';
        try {
            stored = localStorage.getItem(retrievalModeStorageKey()) || 'standard';
        } catch (err) {
            stored = 'standard';
        }
        setRetrievalMode(stored, { persist: false });
    }

    function setRetrievalMode(mode, { persist = false } = {}) {
        retrievalMode = mode === 'enhanced' ? 'enhanced' : 'standard';
        retrievalModeControls.forEach((control) => {
            const trigger = control.querySelector('[data-retrieval-mode-trigger]');
            const label = control.querySelector('[data-retrieval-mode-label]');
            if (label) label.textContent = retrievalMode === 'enhanced' ? '增强检索' : '标准检索';
            trigger?.classList.toggle('is-enhanced', retrievalMode === 'enhanced');
            trigger?.setAttribute(
                'aria-label',
                retrievalMode === 'enhanced'
                    ? '当前为增强检索，点击切换检索模式'
                    : '当前为标准检索，点击切换检索模式'
            );
            control.querySelectorAll('[data-retrieval-mode-option]').forEach((option) => {
                option.setAttribute(
                    'aria-checked',
                    String(option.dataset.retrievalModeOption === retrievalMode)
                );
            });
            if (persist && retrievalMode === 'enhanced') {
                trigger?.classList.remove('is-newly-enhanced');
                requestAnimationFrame(() => trigger?.classList.add('is-newly-enhanced'));
                setTimeout(() => trigger?.classList.remove('is-newly-enhanced'), 520);
            }
        });
        if (persist) {
            try {
                localStorage.setItem(retrievalModeStorageKey(), retrievalMode);
            } catch (err) {
                // Current-page state remains usable when storage is unavailable.
            }
        }
    }

    function closeRetrievalModeMenus() {
        retrievalModeControls.forEach((control) => {
            const trigger = control.querySelector('[data-retrieval-mode-trigger]');
            const menu = control.querySelector('[data-retrieval-mode-menu]');
            if (menu) {
                clearTimeout(menu._retrievalCloseTimer);
                menu.classList.remove('is-open');
                menu.setAttribute('aria-hidden', 'true');
                menu._retrievalCloseTimer = setTimeout(() => {
                    if (!menu.classList.contains('is-open')) menu.hidden = true;
                }, 240);
            }
            trigger?.setAttribute('aria-expanded', 'false');
        });
    }

    function setRetrievalModeControlsDisabled(disabled) {
        retrievalModeControls.forEach((control) => {
            const trigger = control.querySelector('[data-retrieval-mode-trigger]');
            if (trigger) trigger.disabled = !!disabled;
        });
        if (disabled) closeRetrievalModeMenus();
    }

    function markConversationScrolling(view) {
        view.classList.add('is-scrolling');
        clearTimeout(scrollIdleTimer);
        scrollIdleTimer = setTimeout(() => {
            view.classList.remove('is-scrolling');
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
            // 先定下这一轮浏览用哪个会话，再拉依赖会话的数据：进行中的行程是按会话存的。
            await loadSessions();
            await Promise.all([loadUserSummary(), loadActiveTrip()]);
            const readyForProfile = hideInitOverlay();
            setInputEnabled(true);
            startPromptRotation();

            showHome();
            window.HommeyPersonalProfile?.initialize({ userId, fetchJson, ready: readyForProfile,
                onUpdate: applySettingsProfile,
                onSaved: () => { showToast('个人资料已保存'); },
                onError: message => showToast(message),
            });
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

    // 首页是"新对话的起点"，不该续写上一次的会话。例外是刚点过「新建会话」的那块
    // 空画布——它还一条消息都没有，复用它，否则每问一句都会多出一条空会话。
    function homeStartsNewConversation() {
        const runtime = activeRuntime();
        return !runtime || runtime.container.children.length > 0;
    }

    function submitHomeInput() {
        const text = homeInput.value.trim();
        const hasReadyAttachments = activeAttachments().some((attachment) => attachment.status === 'ready');
        if ((!text && !hasReadyAttachments) || isActiveProcessing()) return;
        homeInput.value = '';
        resizeInput(homeInput);
        chatInput.value = text;
        enterChatView();
        sendMessage(undefined, { newConversation: homeStartsNewConversation() });
    }

    function submitPrompt(prompt) {
        if (!prompt || isActiveProcessing()) return;
        chatInput.value = prompt;
        enterChatView();
        sendMessage(undefined, { newConversation: homeStartsNewConversation() });
    }

    function submitCurrentInput() {
        sendMessage();
    }

    function handlePresentationSubmit(event) {
        const text = String(event.detail?.text || '').trim();
        if (!text || isActiveProcessing() || event.detail?.card?.dataset.archived === 'true') {
            event.preventDefault();
            if (isActiveProcessing()) showToast('当前任务正在处理，请完成后再提交。');
            return;
        }
        // Card submissions use the same chat endpoint, request id, locking and
        // durable Turn path. Keep any composer draft/attachments untouched.
        sendMessage(text, {
            preserveComposer: true,
            includeAttachments: false,
            inlineSubmission: true,
            silentSubmission: ['trip_intake', 'information_request'].includes(event.detail?.source),
            requestPayload: {...(event.detail?.requestPayload || {}),
                ...(event.detail?.interactionId ? {intake_request_id: event.detail.interactionId} : {})},
        }).then((success) => {
            if (typeof event.detail?.complete === 'function') event.detail.complete(success);
        });
    }

    function enterChatView() {
        setMainView('chat');
        closeSidebar();
        requestAnimationFrame(() => {
            const runtime = activeRuntime();
            if (runtime) scrollToBottom(runtime);
        });
    }

    // 返回首页只切换视图。保留会话画布、流和草稿，首页使用独立草稿位。
    function showHome() {
        const runtime = activeRuntime();
        if (runtime && runtime.container.children.length > 0) {
            runtime.draft.text = chatInput.value;
            runtime.container.classList.remove(window.HommeySessionRuntime.ACTIVE_CLASS);
            setActiveSession('');
            chatInput.value = '';
            resizeInput(chatInput);
            syncComposerToActive();
            restoreRetrievalMode();
            renderPendingAttachments();
            loadActiveTrip();
        }
        setMainView('home');
        closeSidebar();
        setTimeout(() => {
            if (appShell.dataset.view === 'home' && !document.body.classList.contains('workspace-open')) homeInput.focus();
        }, 180);
    }

    function showKnowledge() {
        const currentView = appShell.dataset.view;
        if (currentView === 'home' || currentView === 'chat') {
            knowledgeReturnView = currentView;
        }
        const returnLabel = knowledgeReturnView === 'chat' ? '返回对话' : '返回首页';
        knowledgeExitLabel.textContent = returnLabel;
        knowledgeExitButton.setAttribute('aria-label', returnLabel);
        setMainView('knowledge');
        closeSidebar();
        loadKnowledgeDocuments();
        if (isKnowledgeAdmin) loadKnowledgeRefreshStatus();
        setTimeout(() => knowledgeSearch.focus(), 180);
    }

    function returnFromKnowledge() {
        if (appShell.dataset.view !== 'knowledge') return;
        const targetView = knowledgeReturnView === 'chat' ? 'chat' : 'home';
        setMainView(targetView);
        const targetInput = targetView === 'chat' ? chatInput : homeInput;
        setTimeout(() => targetInput.focus(), 180);
    }

    function setMainView(view) {
        if (quickTripLayer.classList.contains('is-inline')) closeLayer('quickTripLayer');
        appShell.dataset.view = view;
        document.getElementById('knowledgeButton').classList.toggle('active', view === 'knowledge');
        if (view !== 'knowledge') {
            clearTimeout(knowledgeRefreshTimer);
            knowledgeRefreshTimer = null;
            knowledgeRefreshPollFailures = 0;
        }
    }

    function handleKnowledgeShortcut(event) {
        if (event.key === 'Escape' && appShell.dataset.view === 'knowledge') {
            event.preventDefault();
            returnFromKnowledge();
            return;
        }
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
            const scopes = Array.isArray(data.search_scopes) ? data.search_scopes : [];
            document.getElementById('knowledgeScopeNote').textContent = scopes.length
                ? `当前回答使用 ${scopes.join('、')} 分区的资料；其他分区仅供查阅。新上传资料进入 ${scopes[0]}。`
                : '当前回答可检索全部制度资料。';
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
            if (status.status === 'running' || status.status === 'queued') scheduleKnowledgeRefreshPoll();
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
                if (status.status === 'running' || status.status === 'queued') scheduleKnowledgeRefreshPoll();
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

        const running = state === 'running' || state === 'queued';
        const success = state === 'success';
        const partial = state === 'partial_success';
        const report = status?.report || {};
        knowledgeSyncBar.hidden = false;
        knowledgeSyncBar.dataset.status = running ? 'running' : (success ? 'success' : (partial ? 'error' : state));
        document.getElementById('knowledgeSyncTitle').textContent = status?.stage || '知识库状态已更新';
        document.getElementById('knowledgeSyncProgress').style.width = `${Math.max(0, Math.min(100, Number(status?.progress || 0)))}%`;

        let detail = status?.message || '';
        if (state === 'queued') detail = '任务已持久化，正在等待刷新 worker 认领';
        else if (running) detail = `${Number(status?.progress || 0)}% · 文档会在后台完成解析与向量化`;
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
        setRetrievalModeControlsDisabled(!enabled);
    }

    function hideInitOverlay() {
        return new Promise(resolve => {
            let fallback;
            const finish = () => {
                clearTimeout(fallback);
                initOverlay.removeEventListener('transitionend', onTransition);
                initOverlay.style.display = 'none';
                resolve();
            };
            const onTransition = event => {
                if (event.target === initOverlay && event.propertyName === 'opacity') finish();
            };
            initOverlay.addEventListener('transitionend', onTransition);
            initOverlay.classList.add('hidden');
            if (document.documentElement.dataset.motion === 'off' || window.matchMedia('(prefers-reduced-motion: reduce)').matches) finish();
            else fallback = setTimeout(finish, 360);
        });
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
            userSummaryName = data.name_display || userId;
            panelName.textContent = window.HommeyPersonalProfile?.applyDisplayName(data.name_display || userId) || data.name_display || userId;
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
        const sessionId = activeSessionId();
        window.HommeyWorkspace.setTripState('正在读取行程…');
        try {
            // 没有选择会话时，不读取任何其他会话的行程。
            if (!activeSessionId()) {
                window.HommeyWorkspace.setTrip(null, '');
                activeTrip.replaceChildren();
                activeTrip.appendChild(createEmptyState('当前没有进行中的出差任务。'));
                return;
            }
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/trip/active?session_id=${encodeURIComponent(sessionId)}`);
            if (activeSessionId() !== sessionId) return;
            const trip = data.active_trip;
            window.HommeyWorkspace.setTrip(trip, sessionId);
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
            if (activeSessionId() !== sessionId) return;
            window.HommeyWorkspace.setTripState('暂时无法读取行程');
            activeTrip.replaceChildren(createEmptyState('暂时无法读取行程。'));
        }
    }

    function createEmptyState(text) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.textContent = text;
        return empty;
    }

    function sessionMemoryKey() {
        return `${SESSION_MEMORY_KEY_PREFIX}.${userId}`;
    }

    function rememberSession(sessionId) {
        try {
            if (sessionId) sessionStorage.setItem(sessionMemoryKey(), sessionId);
            else sessionStorage.removeItem(sessionMemoryKey());
        } catch (_) { /* 隐私模式下不可用，退化为每次进入都开新会话。 */ }
    }

    function rememberedSession() {
        try {
            return sessionStorage.getItem(sessionMemoryKey()) || '';
        } catch (_) { return ''; }
    }

    // 会话边界由“这一次浏览”决定：刷新要接着当前会话，关掉标签页再进来才算新的一轮。
    // sessionStorage 正好是这个语义——刷新保留、标签页一关闭就清空。空闲时长不参与判断：
    // 挂着一个页面多久都不该把用户的对话换掉。
    async function loadSessions() {
        try {
            const remembered = rememberedSession();
            setActiveSession('');
            if (remembered) {
                try {
                    await fetchJson(
                        `/api/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(remembered)}/activate`,
                        { method: 'POST' }
                    );
                    setActiveSession(remembered);
                } catch (err) {
                    if (err instanceof ApiError && err.code === 'SESSION_NOT_FOUND') {
                        rememberSession('');
                    } else {
                        setActiveSession(remembered);
                        throw err;
                    }
                }
            }
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/sessions`);
            restoreRetrievalMode();
            renderSessions(Array.isArray(data.sessions) ? data.sessions : []);
        } catch (err) {
            showSessionListError();
        }
    }

    function showSessionListError() {
        window.HommeyWorkspace.setSessionsError();
        if (!historyList.querySelector('.session-row')) historyList.replaceChildren();
        let error = historyList.querySelector('.session-list-error');
        if (!error) {
            error = createEmptyState('暂时无法加载历史会话。');
            error.classList.add('session-list-error');
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.textContent = '重试';
            retry.addEventListener('click', refreshSessionList);
            error.appendChild(retry);
            historyList.appendChild(error);
        }
    }

    // 只重画侧边栏列表，不碰当前会话：切会话、新建、删除之后用它。
    // loadSessions 仅在初始化时恢复标签页选择。
    async function refreshSessionList() {
        try {
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/sessions`);
            renderSessions(Array.isArray(data.sessions) ? data.sessions : []);
        } catch (_) { showSessionListError(); }
    }

    // 这一轮浏览还没开过会话（刚进入，或上次那个已经不可用）：现在建一个，
    // 每个聊天请求都必须携带明确的会话 ID。startNew 为真时，手上那条会话不用，
    // 另开一条——首页的提问要用这个语义，见 homeStartsNewConversation。
    async function ensureActiveSession(startNew = false) {
        const previous = window.HommeySessionRuntime.active();
        if (!startNew && activeSessionId()) return activeSessionId();
        const data = await fetchJson(`/api/${encodeURIComponent(userId)}/sessions`, { method: 'POST' });
        const sessionId = data.session_id || '';
        const runtime = window.HommeySessionRuntime.ensure(sessionId);
        // 两个输入区共用一份草稿，附件是在"认领时活动的那个会话"下记的（见 handleFilePick）。
        // 换会话时它们必须跟着这条消息走，否则首页上看得见的 chips 会留在旧会话里发不出去。
        runtime.draft.attachments = [
            ...(previous?.draft.attachments || []),
            ...looseDraft.attachments,
            ...(runtime.draft.attachments || []),
        ];
        if (previous) {
            previous.draft.attachments = [];
            // 和切会话一样：丢内存态，下次从库里重建，避免首页每问一句就留下一个容器。
            window.HommeySessionRuntime.release(previous.id);
        }
        looseDraft.attachments = [];
        setActiveSession(sessionId);
        rememberSession(sessionId);
        window.HommeySessionRuntime.mount(runtime);
        syncComposerToActive();
        renderPendingAttachments();
        return sessionId;
    }

    function renderSessions(sessions) {
        historyList.replaceChildren();
        const label = document.createElement('p');
        label.className = 'history-label';
        label.textContent = '最近';
        historyList.appendChild(label);

        // 服务端的列表是从消息推出来的，刚建、还一条消息都没发出去的会话不在里面。
        // 正在跑的那种必须补进来，否则用户切走之后就没有入口回去了。
        const listed = new Set(sessions.map((session) => session.session_id));
        const runningOnly = window.HommeySessionRuntime.all()
            .filter((runtime) => runtime.processing && !listed.has(runtime.id))
            .map((runtime) => ({ session_id: runtime.id, title: '新会话', preview: '' }));
        const rows = [...sessions, ...runningOnly];
        window.HommeyWorkspace.setSessions(rows);

        if (!rows.length) {
            historyList.appendChild(createEmptyState('还没有历史会话。发送第一条消息后会自动保存。'));
            return;
        }
        rows.forEach((session) => {
            const running = window.HommeySessionRuntime.isRunning(session.session_id);
            const row = document.createElement('div');
            row.className = `session-row${session.session_id === activeSessionId() ? ' active' : ''}`;
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
            if (running) {
                row.classList.add('is-running');
                const dot = document.createElement('span');
                dot.className = 'session-running-dot';
                dot.setAttribute('aria-label', '正在运行');
                row.insertBefore(dot, open);
            }
            historyList.appendChild(row);
        });
        filterHistory();
    }

    // 把一个会话的画布挂到 #chatMessages，并交换草稿。
    // 切走的上一个会话交给保留规则：还在跑就留在内存里继续往自己的容器写，
    // 跑完了就丢掉，下次打开从库里重建。这里不再有"正在生成就不许切"的限制。
    // 换会话才交换草稿：停在同一个会话上重复调用（比如发消息前确保画布挂上）
    // 不能动用户正在输入的草稿。
    function mountSession(sessionId, { useHomeDraft = false } = {}) {
        const next = window.HommeySessionRuntime.ensure(sessionId);
        const previous = window.HommeySessionRuntime.active();
        if (previous !== next) {
            if (previous) {
                previous.draft.text = chatInput.value;
                window.HommeySessionRuntime.release(previous.id);
            } else if (useHomeDraft) {
                // 只有新会话接收首页附件；打开已有会话时保留首页草稿。
                next.draft.attachments = [
                    ...looseDraft.attachments,
                    ...(next.draft.attachments || []),
                ];
                looseDraft.attachments = [];
            }
            chatInput.value = next.draft.text || '';
            resizeInput(chatInput);
        }
        setActiveSession(sessionId);
        renderPendingAttachments();
        window.HommeySessionRuntime.mount(next);
        syncComposerToActive();
        // 收起时布局为 0，滚动位置只能在重新显示之后落。
        requestAnimationFrame(() => scrollToBottom(next));
        return next;
    }

    // 把历史画进会话自己的容器。只做 DOM、不碰网络，所以中途不会被切换打断。
    function buildHistory(runtime, messages, plans) {
        messages.forEach((message) => {
            const role = message.role === 'assistant' ? 'ai' : message.role;
            if (role === 'user' && ['trip_submission', 'form_submission'].includes(message.content_type)) {
                collapseTripIntakeCards(runtime);
                return;
            }
            if (role === 'ai') {
                const plan = plans.find(item => item.run_id === message.request_id);
                if (plan) window.ExecutionPlan?.update(runtime.container, plan);
            }
            if (role === 'ai' || role === 'user') {
                if (role === 'ai' && message.answer_document) {
                    addAnswerMessage(runtime, message.answer_document, message.timestamp);
                } else if (role === 'ai' && message.presentation_document) {
                    addPresentationMessage(runtime, {...message.presentation_document, interaction_id: message.presentation_document.interaction_id || message.request_id}, message.timestamp);
                } else {
                    addMessage(runtime, role, message.content || '', message.timestamp, message.attachments);
                }
            }
        });
        plans.forEach(plan => window.ExecutionPlan?.update(runtime.container, plan));
        const latest = messages[messages.length - 1];
        setSessionPlaceholder(
            runtime,
            ['trip_intake', 'information_request'].includes(latest?.presentation_document?.type)
                ? latest.presentation_document.input_placeholder
                : ''
        );
    }

    async function createNewSession() {
        try {
            const data = await fetchJson(`/api/${encodeURIComponent(userId)}/sessions`, { method: 'POST' });
            const runtime = mountSession(data.session_id || '', { useHomeDraft: true });
            runtime.container.replaceChildren();
            runtime.followConversation = true;
            rememberSession(activeSessionId());
            setRetrievalMode('standard', { persist: true });
            setSessionPlaceholder(runtime, '');
            showHome();
            await Promise.all([refreshSessionList(), loadActiveTrip()]);
        } catch (err) {
            showToast(formatDisplayError(err, '无法创建新会话'));
        }
    }

    async function openSession(sessionId) {
        // 运行中或返回首页后保留的会话直接挂回来，保留回复和未发送草稿。
        const live = window.HommeySessionRuntime.get(sessionId);
        if (live && (live.processing || (live !== activeRuntime() && live.container.children.length > 0))) {
            mountSession(sessionId);
            rememberSession(sessionId);
            restoreRetrievalMode();
            enterChatView();
            await Promise.all([refreshSessionList(), loadActiveTrip()]);
            return;
        }
        try {
            const data = await fetchJson(
                `/api/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}/activate`,
                { method: 'POST' }
            );
            let plans = [];
            try {
                plans = (await fetchJson(`/api/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}/execution-plans`)).plans || [];
            } catch (_) { /* Chat history remains usable if progress recovery is unavailable. */ }
            // 数据取完才开始建 DOM，中间没有 await，不会被切换打断。
            const runtime = mountSession(sessionId);
            runtime.container.replaceChildren();
            runtime.followConversation = true;
            buildHistory(runtime, data.messages || [], plans);
            rememberSession(sessionId);
            restoreRetrievalMode();
            enterChatView();
            await Promise.all([refreshSessionList(), loadActiveTrip()]);
        } catch (err) {
            showToast(formatDisplayError(err, '无法打开会话'));
        }
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
        closeSidebar();
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
            await refreshSessionList();
            showToast('会话已重命名');
        } catch (err) {
            showToast(formatDisplayError(err, '重命名失败'));
        }
    }

    function confirmDeleteSession() {
        sessionPopover.hidden = true;
        openConfirm('删除这条会话？', '删除后无法恢复，但不会影响你的差旅偏好。', async () => {
            const target = selectedSessionId;
            // 正在跑的会话先停掉：后端这条会话一旦删除，那个流就没有落点了。
            const runtime = window.HommeySessionRuntime.get(target);
            if (runtime && runtime.processing) await interruptTurn(runtime);
            await fetchJson(
                `/api/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(target)}`,
                { method: 'DELETE' }
            );
            window.HommeySessionRuntime.discard(target);
            if (target === activeSessionId()) {
                setActiveSession('');
                rememberSession('');
                setMainView('home');
            }
            await Promise.all([refreshSessionList(), loadActiveTrip()]);
            showToast('会话已删除');
        });
    }

    function confirmClearHistory() {
        openConfirm('清空全部聊天记录？', '所有历史会话都会被删除，此操作无法恢复。', async () => {
            await Promise.all(
                window.HommeySessionRuntime.all()
                    .filter(runtime => runtime.processing)
                    .map(runtime => interruptTurn(runtime))
            );
            await fetchJson(`/api/${encodeURIComponent(userId)}/history`, { method: 'DELETE' });
            window.HommeySessionRuntime.discardAll();
            setActiveSession('');
            rememberSession('');
            closeSettings();
            setMainView('home');
            await Promise.all([refreshSessionList(), loadActiveTrip()]);
            showToast('聊天记录已清空');
        });
    }

    function openConfirm(title, message, callback) {
        closeSidebar();
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

    function newRequestId() {
        // 简单 uuid v4，无需依赖外部库。
        return (crypto && crypto.randomUUID)
            ? crypto.randomUUID()
            : 'xxxxxxxxxxxx4xxx'.replace(/x/g, (c) => ((Math.random() * 16) | 0).toString(16));
    }

    function ensureRequestId(runtime) {
        if (!runtime.requestId) runtime.requestId = newRequestId();
        return runtime.requestId;
    }

    function resetRequestId(runtime) {
        runtime.requestId = '';
    }

    async function uploadAttachment(file, onProgress) {
        // 附件上传有它自己的幂等键，与会话的聊天轮次无关。
        const requestId = newRequestId();
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
        // 两个输入区（首页 / 会话）共用一份草稿，所以两边画的是同一组附件。
        const attachments = activeAttachments();
        pendingContainers().forEach((c) => {
            c.replaceChildren();
            attachments.forEach((attachment) => {
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
                    const draft = draftSlot(window.HommeySessionRuntime.active());
                    draft.attachments = draft.attachments.filter((item) => {
                        const sameId = !!chip.dataset.id && (item.id || '') === chip.dataset.id;
                        const sameTemporaryId = !!chip.dataset.tmpId
                            && (item.tmpId || '') === chip.dataset.tmpId;
                        return !(sameId || sameTemporaryId);
                    });
                    const runtime = activeRuntime();
                    if (runtime?.retryRequestPending) {
                        resetRequestId(runtime);
                        runtime.retryRequestPending = false;
                    }
                    renderPendingAttachments();
                });
                chip.appendChild(remove);
                c.appendChild(chip);
            });
            c.style.display = attachments.length ? '' : 'none';
        });
    }

    async function handleFilePick(fileList) {
        const files = Array.from(fileList || []);
        // 上传是异步的：在这里先认下草稿位，上传期间用户切走也不会把附件落到别人头上。
        const draft = draftSlot(window.HommeySessionRuntime.active());
        for (const file of files) {
            const entry = { tmpId: 'tmp_' + Math.random().toString(36).slice(2), filename: file.name, status: 'uploading' };
            draft.attachments.push(entry);
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
            OCR_DISABLED: 'PDF 包含扫描页，但 OCR 服务未开启',
            OCR_NOT_CONFIGURED: 'PDF OCR 服务未配置',
            OCR_PAGE_LIMIT_EXCEEDED: 'PDF 扫描页超过 10 页处理上限',
            OCR_TIMEOUT: 'PDF OCR 超时，请稍后重试',
            OCR_EMPTY_RESULT: 'PDF 扫描页未识别到文字',
            OCR_FAILED: 'PDF 扫描页识别失败',
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
        closeSidebar();
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
        const attachments = activeAttachments();
        if (attachments.some((item) => item.id === att.id)) {
            showToast('该附件已在待发送列表中');
            return;
        }
        attachments.push({ id: att.id, filename: att.filename, kind: att.kind, status: 'ready' });
        const runtime = activeRuntime();
        if (runtime?.retryRequestPending) {
            resetRequestId(runtime);
            runtime.retryRequestPending = false;
        }
        renderPendingAttachments();
        closeLayer('attachmentsLayer');
        const activeComposer = appShell.dataset.view === 'chat' ? chatInput : homeInput;
        requestAnimationFrame(() => activeComposer.focus());
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

    function localDateValue(offsetDays = 0) {
        const value = new Date();
        value.setDate(value.getDate() + offsetDays);
        const year = value.getFullYear();
        const month = String(value.getMonth() + 1).padStart(2, '0');
        const day = String(value.getDate()).padStart(2, '0');
        return `${year}-${month}-${day}`;
    }

    function openQuickTrip() {
        if (isActiveProcessing()) {
            showToast('当前任务完成后即可使用快速差旅。');
            return;
        }
        closeSidebar();
        const start = document.getElementById('quickTripStartDate');
        const end = document.getElementById('quickTripEndDate');
        const today = localDateValue();
        start.min = today;
        end.min = today;
        if (!start.value) start.value = today;
        if (!end.value) end.value = localDateValue(2);
        if (appShell.dataset.view === 'home') {
            homeComposer.after(quickTripLayer);
            quickTripLayer.classList.add('is-inline');
            homeComposer.hidden = true;
            document.body.classList.add('quick-planning');
        }
        quickTripLayer.classList.add('open');
        setTimeout(() => document.getElementById('quickTripOrigin').focus(), 120);
    }

    function hideQuickTripSuggestions() {
        quickTripPlaceSuggestions.hidden = true;
        quickTripPlaceSuggestions.replaceChildren();
    }

    function setQuickTripPlaceStatus(message, state = '') {
        quickTripPlaceStatus.textContent = message;
        quickTripPlaceStatus.classList.toggle('is-error', state === 'error');
        quickTripPlaceStatus.classList.toggle('is-verified', state === 'verified');
    }

    function handleQuickTripPlaceInput() {
        quickTripWorkLocationId.value = '';
        setQuickTripPlaceStatus('请从目的地城市的高德结果中选择准确地点。');
        quickTripMap?.setData({anchor:null}); document.getElementById('quickTripMapPreview').hidden=true;
        clearTimeout(quickTripSearchTimer);
        quickTripSearchController?.abort();
        const keyword = quickTripWorkLocation.value.trim();
        if (!document.getElementById('quickTripDestination').value.trim()) {
            hideQuickTripSuggestions();
            setQuickTripPlaceStatus('请先填写目的地城市，再搜索工作地点。');
            return;
        }
        if (keyword.length < 2) {
            hideQuickTripSuggestions();
            return;
        }
        quickTripSearchTimer = setTimeout(() => searchQuickTripPlaces(keyword), 320);
    }

    async function searchQuickTripPlaces(keyword) {
        quickTripSearchController = new AbortController();
        const city = document.getElementById('quickTripDestination').value.trim();
        const params = new URLSearchParams({ keyword });
        if (city) params.set('city', city);
        try {
            const response = await authFetch(
                `/api/${encodeURIComponent(userId)}/places/suggest?${params.toString()}`,
                { signal: quickTripSearchController.signal }
            );
            const data = await response.json();
            if (!response.ok) throw createApiError(data, '地点查询失败', response.status);
            if (quickTripWorkLocation.value.trim() !== keyword || document.getElementById('quickTripDestination').value.trim() !== city) return;
            renderQuickTripPlaces(data.items || []);
        } catch (err) {
            if (err.name === 'AbortError') return;
            hideQuickTripSuggestions();
            setQuickTripPlaceStatus(formatDisplayError(err, '地点查询暂时不可用'), 'error');
        }
    }

    function renderQuickTripPlaces(items) {
        const searchCity=document.getElementById('quickTripDestination').value.trim();
        quickTripPlaceSuggestions.replaceChildren();
        if (!items.length) {
            const empty = document.createElement('div');
            empty.className = 'place-suggestions-empty';
            empty.textContent = '没有找到匹配地点，请补充城市或完整名称。';
            quickTripPlaceSuggestions.appendChild(empty);
        } else {
            items.slice(0, 5).forEach((item) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'place-suggestion';
                button.setAttribute('role', 'option');
                const name = document.createElement('strong');
                name.textContent = item.name || '未命名地点';
                const address = document.createElement('small');
                address.textContent = [item.city, item.district, item.address].filter(Boolean).join(' · ');
                button.append(name, address);
                button.addEventListener('click', () => {
                    if(searchCity!==document.getElementById('quickTripDestination').value.trim() || String(item.city).replace(/市$/,'')!==searchCity.replace(/市$/,''))return;
                    quickTripWorkLocation.value = item.name || '';
                    quickTripWorkLocationId.value = item.place_id || '';
                    if(quickTripMap){document.getElementById('quickTripMapPreview').hidden=false;quickTripMap.setData({anchor:item,city:searchCity});}
                    setQuickTripPlaceStatus(`已通过高德选择：${item.name || ''}`, 'verified');
                    hideQuickTripSuggestions();
                });
                quickTripPlaceSuggestions.appendChild(button);
            });
        }
        quickTripPlaceSuggestions.hidden = false;
    }

    function quickTripDuration(startValue, endValue) {
        const start = new Date(`${startValue}T00:00:00Z`);
        const end = new Date(`${endValue}T00:00:00Z`);
        if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) return 0;
        return Math.floor((end - start) / 86400000) + 1;
    }

    function submitQuickTrip(event) {
        event.preventDefault();
        if (isActiveProcessing() || !quickTripForm.reportValidity()) return;
        const origin = document.getElementById('quickTripOrigin').value.trim();
        const destination = document.getElementById('quickTripDestination').value.trim();
        const startDate = document.getElementById('quickTripStartDate').value;
        const endDate = document.getElementById('quickTripEndDate').value;
        const purpose = document.getElementById('quickTripPurpose').value.trim();
        const workLocation = quickTripWorkLocation.value.trim();
        const placeId = quickTripWorkLocationId.value.trim();
        const duration = quickTripDuration(startDate, endDate);
        if (duration < 1 || duration > 60) {
            showToast('返程日期不能早于出发日期，且行程最多 60 天。');
            return;
        }
        if (!workLocation || !placeId) {
            setQuickTripPlaceStatus('请从高德候选中选择具体工作地点。', 'error');
            quickTripWorkLocation.focus();
            return;
        }
        const capabilityInputs = Array.from(
            quickTripForm.querySelectorAll('input[name="quick_capability"]')
        );
        const include = capabilityInputs.filter((item) => item.checked).map((item) => item.value);
        const exclude = capabilityInputs.filter((item) => !item.checked).map((item) => item.value);
        const tripInput = {
            origin,
            destination,
            start_date: startDate,
            end_date: endDate,
            duration_days: duration,
            trip_purpose: purpose,
            work_location: workLocation,
            work_location_note: document.getElementById('quickTripWorkNote').value.trim(),
            work_location_place_id: placeId,
        };
        const summary = `${startDate} 从${origin}前往${destination}出差${duration}天，目的：${purpose}`
            + (workLocation ? `，工作地点：${workLocation}` : '');
        closeLayer('quickTripLayer');
        hideQuickTripSuggestions();
        sendMessage(summary, {
            preserveComposer: true,
            includeAttachments: false,
            requestPayload: {
                input_source: 'quick_trip_form',
                trip_input: tripInput,
                capability_selection: { include, exclude },
            },
        });
    }

    async function sendMessage(explicitText, options = {}) {
        const hasExplicitText = typeof explicitText === 'string';
        const text = hasExplicitText ? explicitText.trim() : chatInput.value.trim();
        const includeAttachments = options.includeAttachments !== false;
        const hasAttachments = includeAttachments && activeAttachments().some((a) => a.status === 'ready');
        if ((!text && !hasAttachments)) return;
        // 上限按"整个页面同时在跑几个"算。撞到后端全局信号量的失败模式是无反馈地
        // 等 120s 再报 GLOBAL_CONCURRENCY_LIMIT，这里直接说清楚。
        if (!window.HommeySessionRuntime.canStart()) {
            showToast(`最多同时跑 ${window.HommeySessionRuntime.MAX_CONCURRENT} 个会话，等一个完成再开。`);
            return;
        }
        // 这一轮浏览还没有会话就先建一个，否则这条消息会落进上一次的对话里。
        // 首页来的提交（options.newConversation）即使有会话也要另开一条。
        try {
            await ensureActiveSession(options.newConversation === true);
        } catch (err) {
            showToast(formatDisplayError(err, '无法开始新会话，请重试'));
            return;
        }
        // 会话锁定在这里：后面的 await 期间用户可能切走，甚至把这个会话删掉。
        // 流、请求 ID 和渲染目标都从 runtime 取，不再读"当前是哪个会话"。
        const runtime = window.HommeySessionRuntime.ensure(activeSessionId());
        // 同一个会话内并发是后端设计性拒绝的（409 SESSION_BUSY），这里先挡掉。
        if (runtime.processing) return;
        // 保证这一屏挂着画布（从首页直接发第一条消息时可能还没有）。
        // 这里不交换草稿——卡片内提交（preserveComposer）会带用户正在写的输入。
        setActiveSession(runtime.id);
        window.HommeySessionRuntime.mount(runtime);
        syncComposerToActive();
        if (options.retryRequestId) runtime.requestId = options.retryRequestId;
        else if (runtime.submissionRetry) resetRequestId(runtime);
        runtime.submissionRetry = null;
        runtime.container.querySelector('.submission-retry')?.remove();
        clearTimeout(toastTimer);
        toast.classList.remove('visible');
        // 附件从本会话的草稿里取，不从"当前输入区"取：上面的 await 期间可能切过会话。
        const sendingEntries = includeAttachments
            ? draftSlot(runtime).attachments.filter((a) => a.status === 'ready')
            : [];
        const sendingAttachmentIds = sendingEntries.map((a) => a.id);
        const sendingAttachments = sendingEntries.map((a) => ({ filename: a.filename, kind: a.kind }));
        let requestCompleted = false;
        let submissionAccepted = false;
        enterChatView();
        runtime.followConversation = true;
        const silentSubmission = options.silentSubmission || options.requestPayload?.input_source === 'quick_trip_form';
        if (!silentSubmission) addMessage(runtime, 'user', text, undefined, sendingAttachments);
        if (!options.preserveComposer) {
            chatInput.value = '';
            resizeInput(chatInput);
        }
        if (includeAttachments) {
            draftSlot(runtime).attachments = [];
            renderPendingAttachments();
        }
        runtime.processing = true;
        setRetrievalModeControlsDisabled(true);
        runtime.interruptPending = false;
        sendBtn.disabled = false;
        chatInput.placeholder = 'Hommey 正在整理…';
        setSendLoading(true);
        showProcessingIndicator(runtime, []);

        try {
            const response = await authFetch(`/api/${encodeURIComponent(userId)}/chat/stream`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Request-ID': ensureRequestId(runtime),
                },
                body: JSON.stringify({
                    message: text,
                    attachment_ids: sendingAttachmentIds,
                    client_request_id: runtime.requestId,
                    session_id: runtime.id || null,
                    retrieval_mode: retrievalMode,
                    ...(options.requestPayload || {}),
                }),
            });
            if (!response.ok) {
                const error = await response.json();
                throw createApiError(error, '请求失败，请重试', response.status);
            }
            if (!response.body) throw new Error('当前浏览器不支持流式响应');
            submissionAccepted = true;
            collapseTripIntakeCards(runtime);

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
                    if (event.type === 'execution_plan') {
                        window.ExecutionPlan?.update(runtime.container, event);
                        removeProcessingIndicator(runtime);
                    }
                    if (event.type === 'done' && event.public_plan) window.ExecutionPlan?.update(runtime.container, event.public_plan);
                    if (event.type === 'status' || event.type === 'task_status') updateProcessingStatus(runtime, event);
                    if (event.type === 'attachment_context') {
                        responseSources = event.sources || [];
                        if (event.warnings && event.warnings.length) {
                            showToast(event.warnings.join('；'));
                        }
                    }
                    if (event.type === 'agents') updateAgentTags(runtime, event.agents);
                    if (event.type === 'interrupted') {
                        turnInterrupted = true;
                        removeProcessingIndicator(runtime);
                        addMessage(runtime, 'ai', '已停止当前执行。输入“继续”可以从最近一次安全状态接着完成。');
                    }
                    if (event.type === 'answer_document') {
                        removeProcessingIndicator(runtime);
                        addAnswerMessage(runtime, event.document);
                        presentationRendered = true;
                    }
                    if (event.type === 'presentation_document') {
                        removeProcessingIndicator(runtime);
                        addPresentationMessage(runtime, {...event.document, interaction_id: event.document?.interaction_id || runtime.requestId});
                        presentationRendered = true;
                        nextPlaceholder = event.document?.input_placeholder || '';
                    }
                    if (event.type === 'chunk') {
                        if (!streamMessage) {
                            removeProcessingIndicator(runtime);
                            streamMessage = createStreamingMessage(runtime);
                        }
                        streamMessage.text += event.text || '';
                        renderMessageInto(streamMessage.bubble, streamMessage.text);
                        scrollToBottom(runtime);
                    }
                    if (event.type === 'done') preferencesUpdated = !!event.preferences_updated;
                }
            }

            const tail = parseStreamLine(buffer);
            if (tail?.type === 'error') throw createApiError(tail, '处理失败，请重试');
            if (tail?.type === 'execution_plan') window.ExecutionPlan?.update(runtime.container, tail);
            if (tail?.type === 'done' && tail.public_plan) window.ExecutionPlan?.update(runtime.container, tail.public_plan);
            if (tail && (tail.type === 'status' || tail.type === 'task_status')) updateProcessingStatus(runtime, tail);
            if (tail && tail.type === 'answer_document') {
                removeProcessingIndicator(runtime);
                addAnswerMessage(runtime, tail.document);
                presentationRendered = true;
            }
            if (tail && tail.type === 'presentation_document') {
                removeProcessingIndicator(runtime);
                addPresentationMessage(runtime, {...tail.document, interaction_id: tail.document?.interaction_id || runtime.requestId});
                presentationRendered = true;
                nextPlaceholder = tail.document?.input_placeholder || '';
            }
            if (tail && tail.type === 'chunk') {
                if (!streamMessage) {
                    removeProcessingIndicator(runtime);
                    streamMessage = createStreamingMessage(runtime);
                }
                streamMessage.text += tail.text || '';
                renderMessageInto(streamMessage.bubble, streamMessage.text);
            }
            if (tail && tail.type === 'done') preferencesUpdated = !!tail.preferences_updated;

            removeProcessingIndicator(runtime);
            if (!streamMessage && !presentationRendered && !turnInterrupted) {
                addMessage(runtime, 'ai', '我收到了，但这次没有返回具体内容。');
            }
            if (streamMessage && responseSources.length) {
                renderAttachmentCards(streamMessage.stack, responseSources);
            }
            setSessionPlaceholder(runtime, nextPlaceholder);
            if (preferencesUpdated) await loadUserSummary();
            // 侧栏的运行指示在 finally 里统一重画，这里不重复拉一次。
            await loadActiveTrip();
            requestCompleted = true;
        } catch (err) {
            removeProcessingIndicator(runtime);
            window.ExecutionPlan?.connectionLost(runtime.container, runtime.requestId);
            const errorText = formatDisplayError(err, '网络错误，请检查连接后重试。');
            // 错误落进这个会话自己的容器。用户可能已经切到别的会话去了，
            // 那里不该被这条流的结果影响。
            if (options.inlineSubmission) showToast(errorText);
            else addMessage(runtime, 'ai', errorText);
            if (options.inlineSubmission && err instanceof ApiError && err.code === 'INTAKE_CARD_EXPIRED') {
                collapseTripIntakeCards(runtime);
            }
            if (options.inlineSubmission && submissionAccepted && (!(err instanceof ApiError) || err.retryable)) {
                showSubmissionRetry(runtime, text, options, runtime.requestId);
            }
            // Preserve the body and request ID so an explicit retry remains
            // idempotent. Inline card submissions retain their values in-card
            // and never overwrite an unrelated composer draft.
            if (!options.preserveComposer) {
                runtime.draft.text = text;
                if (runtime === window.HommeySessionRuntime.active()) chatInput.value = text;
            }
            if (includeAttachments) {
                // 还原到发起这次请求的那个会话，不是用户此刻正看着的那个。
                const draft = draftSlot(runtime);
                const sendingIds = new Set(sendingAttachmentIds);
                draft.attachments = [
                    ...sendingEntries,
                    ...draft.attachments.filter((entry) => !sendingIds.has(entry.id)),
                ];
            }
            runtime.retryRequestPending = true;
            if (includeAttachments && runtime === window.HommeySessionRuntime.active()) {
                renderPendingAttachments();
            }
            if (!options.preserveComposer && runtime === window.HommeySessionRuntime.active()) resizeInput(chatInput);
        } finally {
            runtime.processing = false;
            // 检索模式是按会话存的（retrievalModeStorageKey），后台会话跑完不能把
            // 用户正在看的那个会话的控件解开。
            setRetrievalModeControlsDisabled(isActiveProcessing());
            runtime.interruptPending = false;
            sendBtn.disabled = false;
            if (requestCompleted) {
                resetRequestId(runtime);
                runtime.retryRequestPending = false;
            }
            // 共享输入区只跟随当前会话：后台会话跑完不该动用户正在用的输入框。
            if (runtime === window.HommeySessionRuntime.active()) {
                syncComposerToActive();
                chatInput.focus();
            }
            // 侧边栏的运行指示要跟着变。
            refreshSessionList();
        }
        return requestCompleted;
    }

    function showSubmissionRetry(runtime, text, options, requestId) {
        const pending = {text, options, requestId, sessionId: runtime.id};
        runtime.submissionRetry = pending;
        const row = document.createElement('div');
        row.className = 'submission-retry';
        const copy = document.createElement('span');
        copy.textContent = '本次提交暂未完成，填写内容已保留';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.textContent = '重试';
        retry.addEventListener('click', () => {
            if (runtime.processing || runtime.submissionRetry !== pending) return;
            // 重试要把用户带回到这个会话，否则消息会落进当前看的那个。
            if (activeSessionId() !== pending.sessionId) mountSession(pending.sessionId);
            sendMessage(text, {...options, retryRequestId: requestId});
        });
        row.append(copy, retry);
        runtime.container.appendChild(row);
        scrollToBottom(runtime);
    }

    // 停掉指定会话的运行。与当前查看的是哪一个无关——后台会话也能被停。
    async function interruptTurn(runtime) {
        if (!runtime || !runtime.processing || runtime.interruptPending || !runtime.requestId) return;
        runtime.interruptPending = true;
        if (runtime === window.HommeySessionRuntime.active()) setSendLoading(true, true);
        try {
            const response = await authFetch(
                `/api/${encodeURIComponent(userId)}/orchestration/interrupt`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        client_request_id: runtime.requestId,
                        session_id: runtime.id || null,
                    }),
                },
            );
            if (!response.ok) {
                const error = await response.json();
                throw createApiError(error, '停止失败，请重试', response.status);
            }
            setSessionPlaceholder(runtime, '正在停止当前执行…');
            if (runtime === window.HommeySessionRuntime.active()) chatInput.placeholder = '正在停止当前执行…';
        } catch (err) {
            runtime.interruptPending = false;
            if (runtime === window.HommeySessionRuntime.active()) setSendLoading(true);
            showToast(formatDisplayError(err, '停止失败，请重试。'));
        }
    }

    function interruptCurrentTurn() {
        return interruptTurn(window.HommeySessionRuntime.active());
    }

    function createMessageShell(role) {
        const row = document.createElement('div');
        row.className = `message-row ${role}`;
        const avatar = document.createElement('div');
        avatar.className = `msg-avatar ${role}`;
        avatar.setAttribute('aria-label', 'Hommey');
        avatar.setAttribute('role', 'img');
        const stack = document.createElement('div');
        stack.className = 'msg-stack';
        row.append(avatar, stack);
        return row;
    }

    // 渲染函数一律显式接收目标 runtime。后台会话的流要继续写进它自己的容器，
    // "当前查看的会话"不是渲染目标。
    function addMessage(runtime, role, text, timestamp, attachments) {
        collapseTripIntakeCards(runtime);
        const row = createMessageShell(role);
        const stack = row.querySelector('.msg-stack');
        const visibleText = role === 'user' ? userMessageText(text, attachments) : String(text || '');
        if (visibleText) {
            const bubble = document.createElement('div');
            bubble.className = `msg-bubble ${role}`;
            if (role === 'user') renderUserMessageInto(bubble, visibleText);
            else renderMessageInto(bubble, visibleText);
            stack.appendChild(bubble);
        }
        if (role === 'user' && attachments && attachments.length) {
            renderAttachmentCards(stack, attachments);
        }
        if (timestamp) stack.appendChild(createTime(timestamp));
        runtime.container.appendChild(row);
        scrollToBottom(runtime);
        return row;
    }

    function userMessageText(text, attachments) {
        const value = String(text || '').trim();
        if (!value || !attachments || !attachments.length) return value;
        const manifestStart = value.lastIndexOf('（附件：');
        if (manifestStart < 0 || !value.endsWith('）')) return value;
        const manifest = value.slice(manifestStart);
        const names = attachments
            .map((attachment) => String(attachment.filename || '').trim())
            .filter(Boolean);
        if (!names.length || !names.some((name) => manifest.includes(name))) return value;
        return value.slice(0, manifestStart).trim();
    }

    function addAnswerMessage(runtime, documentData, timestamp) {
        collapseTripIntakeCards(runtime);
        if (!window.HommeyAnswerCard || !documentData) {
            return addMessage(runtime, 'ai', documentData?.plain_text || '查询结果已生成。', timestamp);
        }
        const row = createMessageShell('ai');
        const stack = row.querySelector('.msg-stack');
        stack.appendChild(window.HommeyAnswerCard.create(documentData));
        if (timestamp) stack.appendChild(createTime(timestamp));
        runtime.container.appendChild(row);
        scrollToBottom(runtime);
        return row;
    }

    function addPresentationMessage(runtime, documentData, timestamp) {
        const renderer = documentData?.type === 'trip_intake' ? window.HommeyTripIntakeCard
            : documentData?.type === 'information_request' ? window.HommeyInformationCard : null;
        if (!renderer) {
            return addMessage(runtime, 'ai', documentData?.plain_text || '请补充信息。', timestamp);
        }
        collapseTripIntakeCards(runtime);
        const row = createMessageShell('ai');
        const stack = row.querySelector('.msg-stack');
        const card = renderer.create(documentData);
        stack.appendChild(card);
        if (documentData.archived) card.archive?.();
        if (timestamp) stack.appendChild(createTime(timestamp));
        runtime.container.appendChild(row);
        scrollToBottom(runtime);
        return row;
    }

    function collapseTripIntakeCards(runtime) {
        runtime.container.querySelectorAll('.trip-intake-card, .information-card').forEach(card => card.archive?.());
    }

    function showProcessingIndicator(runtime, agents) {
        removeProcessingIndicator(runtime);
        const row = createMessageShell('ai');
        // 用 class 而不是 id：多个会话同时跑时会有多个指示器共存。
        row.classList.add('processing-indicator');
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
        box.append(text, dots);
        stack.appendChild(box);
        runtime.container.appendChild(row);
        scrollToBottom(runtime);
        return row;
    }

    function processingIndicatorOf(runtime) {
        return runtime.container.querySelector('.processing-indicator');
    }

    function removeProcessingIndicator(runtime) {
        if (!runtime) return;
        clearTimeout(runtime.statusTimer);
        runtime.statusTimer = null;
        runtime.statusQueue = [];
        runtime.lastStatusAt = 0;
        processingIndicatorOf(runtime)?.remove();
    }

    function updateProcessingStatus(runtime, event) {
        const indicator = processingIndicatorOf(runtime);
        if (!indicator) return;
        const message = progressMessages[event.message_key] || event.message || '正在整理';
        const current = indicator.querySelector('.thinking-text')?.textContent;
        if (message && message !== current && !runtime.statusQueue.includes(message)) {
            runtime.statusQueue.push(message);
            flushProcessingStatus(runtime);
        }
        if (event.type === 'task_status' && event.intent && event.phase === 'running') {
            const label = event.display || getAgentLabel(event.intent);
            if (label) updateAgentTags(runtime, [{ name: event.intent, display: label }]);
        }
    }

    function flushProcessingStatus(runtime) {
        if (runtime.statusTimer || !runtime.statusQueue.length) return;
        const elapsed = Date.now() - runtime.lastStatusAt;
        const delay = Math.max(0, 650 - elapsed);
        runtime.statusTimer = setTimeout(() => {
            runtime.statusTimer = null;
            const text = processingIndicatorOf(runtime)?.querySelector('.thinking-text');
            if (!text) return;
            const next = runtime.statusQueue.shift();
            text.classList.add('is-changing');
            setTimeout(() => {
                if (!text.isConnected) return;
                text.textContent = next;
                text.classList.remove('is-changing');
                runtime.lastStatusAt = Date.now();
                flushProcessingStatus(runtime);
            }, 160);
        }, delay);
    }

    function updateAgentTags(runtime, agents) {
        const tags = processingIndicatorOf(runtime)?.querySelector('.agent-tags');
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

    function createStreamingMessage(runtime) {
        collapseTripIntakeCards(runtime);
        const row = createMessageShell('ai');
        const stack = row.querySelector('.msg-stack');
        const bubble = document.createElement('div');
        bubble.className = 'msg-bubble ai';
        stack.appendChild(bubble);
        runtime.container.appendChild(row);
        scrollToBottom(runtime);
        return { bubble, stack, text: '' };
    }

    function renderMessageInto(element, text) {
        if (window.HommeyMarkdown) {
            window.HommeyMarkdown.render(element, text);
            return;
        }
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

    // 输入区是共享的一个，它的显示状态跟着当前会话走。
    function setSessionPlaceholder(runtime, placeholder) {
        runtime.draft.placeholder = String(placeholder || '');
        if (runtime === window.HommeySessionRuntime.active() && !runtime.processing) {
            chatInput.placeholder = runtime.draft.placeholder || defaultPlaceholder;
        }
    }

    // 切会话之后把共享输入区同步到新会话：占位文案、发送键语义。
    function syncComposerToActive() {
        const runtime = window.HommeySessionRuntime.active();
        const processing = !!(runtime && runtime.processing);
        chatInput.placeholder = processing
            ? (runtime.interruptPending ? '正在停止当前执行…' : 'Hommey 正在整理…')
            : (runtime?.draft.placeholder || defaultPlaceholder);
        setSendLoading(processing, !!(runtime && runtime.interruptPending));
        // 检索模式是按会话存的，控件可用性也得跟着当前会话，而不是跟着"有没有人在跑"。
        setRetrievalModeControlsDisabled(processing);
    }

    function handlePresentationFill(event) {
        const value = String(event.detail?.text || '').trim();
        if (!value || isActiveProcessing()) return;
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
        if (isActiveProcessing()) {
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
            headers: { 'X-Request-ID': newRequestId() },
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
        if (!text || isActiveProcessing()) return;
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

    // runtime 的容器在后台时是脱离 DOM 的，脱离的节点没有 scrollHeight，滚动也无意义。
    function scrollToBottom(runtime) {
        const container = runtime.container;
        if (runtime.followConversation && container.isConnected) {
            container.scrollTo({ top: container.scrollHeight, behavior: 'instant' });
        }
    }

    function openSidebar(source) {
        window.HommeyWorkspace.open(source);
        refreshSessionList();
        loadActiveTrip();
    }

    function closeSidebar() {
        window.HommeyWorkspace.close();
        sessionPopover.hidden = true;
    }

    const settingsSections = {
        appearance: ['偏好设置', '外观', '让工作界面更合你的习惯。'],
        basic: ['个人信息', '基本资料', '用于完善出差与报销信息。'],
        identity: ['个人信息', '报销身份', '按实际情况补充适用的报销身份。'],
        funding: ['个人信息', '常用经费', '管理日常使用的经费项目。'],
        preferences: ['个人信息', '差旅偏好', '查看已记录的住宿与交通习惯。'],
        trip: ['个人信息', '当前出差任务', '查看当前对话中的出差安排。'],
        account: ['账户管理', '数据与账户', '管理聊天记录与登录状态。'],
    };

    function selectSettingsSection(section) {
        const selected = settingsLayer.querySelector(`[data-settings-section="${section}"]`);
        if (!selected || selected.hidden) return;
        settingsLayer.querySelector('.settings-modal').dataset.section = section;
        settingsLayer.querySelectorAll('[data-settings-section]').forEach(button => {
            const active = button === selected;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', String(active));
            button.tabIndex = active ? 0 : -1;
            document.getElementById(button.getAttribute('aria-controls')).hidden = !active;
        });
        const [group, title, description] = settingsSections[section];
        document.getElementById('settingsPanelGroup').textContent = group;
        document.getElementById('settingsPanelTitle').textContent = title;
        document.getElementById('settingsPanelDescription').textContent = description;
        settingsLayer.querySelector('.settings-panels').scrollTop = 0;
        if (section === 'trip') loadActiveTrip();
    }

    function applySettingsProfile(profile) {
        const basic = profile.basic_info;
        const name = basic.real_name || userSummaryName;
        panelName.textContent = name;
        settingsLayer.querySelector('.account-avatar').textContent = Array.from(name)[0]?.toUpperCase() || 'U';
        document.getElementById('settingsRealName').textContent = basic.real_name || '待补充';
        document.getElementById('settingsInstitution').textContent = basic.institution || '待补充';
        if (document.getElementById('profileIdentityEntry').hidden
            && settingsLayer.querySelector('.settings-modal').dataset.section === 'identity') {
            selectSettingsSection('basic');
        }
    }

    function bindSettingsNavigation() {
        const navigation = settingsLayer.querySelector('.settings-nav');
        const compactNavigation = window.matchMedia('(max-width: 440px)');
        const syncOrientation = () => navigation.setAttribute('aria-orientation', compactNavigation.matches ? 'horizontal' : 'vertical');
        syncOrientation();
        compactNavigation.addEventListener('change', syncOrientation);
        navigation.addEventListener('click', event => {
            const button = event.target.closest('[data-settings-section]');
            if (button) selectSettingsSection(button.dataset.settingsSection);
        });
        navigation.addEventListener('keydown', event => {
            const horizontal = compactNavigation.matches;
            const previous = horizontal ? 'ArrowLeft' : 'ArrowUp';
            const next = horizontal ? 'ArrowRight' : 'ArrowDown';
            if (![previous, next, 'Home', 'End'].includes(event.key)) return;
            const buttons = [...navigation.querySelectorAll('[data-settings-section]')].filter(button => !button.hidden);
            const index = buttons.indexOf(document.activeElement);
            if (index < 0) return;
            event.preventDefault();
            const target = event.key === 'Home' ? buttons[0] : event.key === 'End' ? buttons.at(-1)
                : buttons[(index + (event.key === next ? 1 : -1) + buttons.length) % buttons.length];
            selectSettingsSection(target.dataset.settingsSection);
            target.focus();
        });
        settingsLayer.addEventListener('keydown', event => {
            if (!settingsLayer.classList.contains('open') || confirmLayer.classList.contains('open')) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                closeSettings();
            }
            if (event.key !== 'Tab') return;
            const controls = [...settingsLayer.querySelectorAll('button:not([tabindex="-1"]), a[href], input, select')]
                .filter(element => !element.disabled && element.getClientRects().length);
            const first = controls[0], last = controls.at(-1);
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        });
        const sync = () => {
            const open = settingsLayer.classList.contains('open');
            settingsLayer.setAttribute('aria-hidden', String(!open));
            appShell.inert = open || document.getElementById('personalProfileLayer').classList.contains('open');
        };
        new MutationObserver(sync).observe(settingsLayer, { attributes: true, attributeFilter: ['class'] });
    }

    function openSettings(event) {
        settingsReturnFocus = event?.currentTarget || document.activeElement;
        closeSidebar();
        settingsLayer.classList.add('open');
        settingsLayer.setAttribute('aria-hidden', 'false');
        appShell.inert = true;
        document.getElementById('settingsClose').focus({ preventScroll: true });
    }

    function closeSettings() {
        settingsLayer.classList.remove('open');
        settingsLayer.setAttribute('aria-hidden', 'true');
        appShell.inert = false;
        const target = settingsReturnFocus?.getClientRects().length && !settingsReturnFocus.closest('[inert]')
            ? settingsReturnFocus : document.getElementById('accountButton');
        target?.focus({ preventScroll: true });
    }

    function closeLayer(id) {
        if (id === 'settingsLayer') { closeSettings(); return; }
        document.getElementById(id)?.classList.remove('open');
        if (id === 'quickTripLayer' && quickTripLayer.classList.contains('is-inline')) {
            quickTripLayer.classList.remove('is-inline');
            document.body.append(quickTripLayer);
            homeComposer.hidden = false;
            document.body.classList.remove('quick-planning');
            homeInput.focus({ preventScroll: true });
        }
    }

    function applyStoredAppearance() {
        const theme = localStorage.getItem(THEME_KEY) || 'system';
        if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
        else document.documentElement.removeAttribute('data-theme');
        document.querySelectorAll('[data-theme-option]').forEach((button) => {
            button.classList.toggle('active', button.dataset.themeOption === theme);
            button.setAttribute('aria-pressed', String(button.dataset.themeOption === theme));
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
            button.setAttribute('aria-pressed', String(button.dataset.themeOption === theme));
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
        routeMotionController?.setEnabled(enabled);
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
