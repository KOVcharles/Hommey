/* 页内并行会话的运行时状态。
 *
 * 一个 SessionRuntime 是一段对话在浏览器里的全部易失状态：它自己的 DOM 容器、
 * 进行中的流、草稿、滚动跟随。`#chatMessages` 退化为挂载点——同时只挂一个 view，
 * 但每个 runtime 的容器对象始终活着，所以后台会话的 chunk 会继续写进它自己的容器里，
 * 切回来不需要重放。
 *
 * 这个文件只管状态与生命周期，不管渲染：DOM 由 app.js 的渲染函数生产，这个模块
 * 只决定它们写到哪里、什么时候保留、什么时候丢弃。
 *
 * 刷新即丢是有意的：runtime 是纯内存态，只有已落库的历史能重建。
 */
(function () {
    'use strict';

    // 后端全局信号量 8（跨用户共享）、I/O 池 16 workers / 32 pending。
    // 不设前端上限的失败模式是撞上信号量后无反馈地等 120s 再报 GLOBAL_CONCURRENCY_LIMIT。
    const MAX_CONCURRENT = 3;
    const VIEW_CLASS = 'session-view';
    const ACTIVE_CLASS = 'is-active';

    const runtimes = new Map();
    let mountPoint = null;
    let activeId = '';

    function configure(options) {
        if (options && options.mountPoint) mountPoint = options.mountPoint;
    }

    function create(id) {
        const container = document.createElement('div');
        container.className = VIEW_CLASS;
        container.dataset.sessionId = id;
        const runtime = {
            id,
            container,
            processing: false,
            requestId: '',
            interruptPending: false,
            followConversation: true,
            retryRequestPending: false,
            submissionRetry: null,
            statusQueue: [],
            statusTimer: null,
            lastStatusAt: 0,
            // 这一屏输入区里属于本会话的东西：文本、待发附件、占位文案。
            draft: { text: '', attachments: [], placeholder: '' },
        };
        runtimes.set(id, runtime);
        return runtime;
    }

    function get(id) {
        return id ? runtimes.get(id) || null : null;
    }

    function ensure(id) {
        return get(id) || create(id);
    }

    function activeId_() {
        return activeId;
    }

    function active() {
        return get(activeId);
    }

    function setActive(id) {
        activeId = id || '';
        return active();
    }

    function all() {
        return Array.from(runtimes.values());
    }

    function runningCount() {
        let count = 0;
        runtimes.forEach((runtime) => { if (runtime.processing) count += 1; });
        return count;
    }

    function canStart() {
        return runningCount() < MAX_CONCURRENT;
    }

    function isRunning(id) {
        const runtime = get(id);
        return !!(runtime && runtime.processing);
    }

    // 显示某个 view，其余的收起来。收起只是隐藏，不从 DOM 摘下来：
    // 卡片脚本用 isConnected 判断生死（journey-map.js 一脱离就 dispose 且不再复活），
    // 真把节点摘掉会让切走的会话里那些卡片退休，切回来时画不出东西。
    function mount(runtime) {
        if (!mountPoint) throw new Error('session runtime mount point is not configured');
        runtimes.forEach((item) => item.container.classList.toggle(ACTIVE_CLASS, item === runtime));
        if (runtime.container.parentNode !== mountPoint) mountPoint.appendChild(runtime.container);
        return runtime;
    }

    // 切走。processing 为真的是活会话，收起来但容器和流都留着；否则连同容器一起丢掉，
    // 下次打开从库里重建（结果在运行结束时已经落库）。
    function release(id) {
        const runtime = get(id);
        if (!runtime) return;
        runtime.container.classList.remove(ACTIVE_CLASS);
        if (runtime.processing) return;
        discard(id);
    }

    // 把还没有会话 ID 的画布（首次设置那一屏）认领成真实会话。
    // 不这么做的话，第一条消息发出时会换成一个空画布，用户刚看到的内容会当场消失。
    function adopt(fromId, toId) {
        const runtime = get(fromId);
        if (!runtime || !toId || get(toId)) return null;
        runtimes.delete(fromId);
        runtime.id = toId;
        runtime.container.dataset.sessionId = toId;
        runtimes.set(toId, runtime);
        if (activeId === fromId) activeId = toId;
        return runtime;
    }

    function discard(id) {
        const runtime = get(id);
        if (!runtime) return;
        if (runtime.statusTimer) clearTimeout(runtime.statusTimer);
        runtime.container.remove();
        runtimes.delete(id);
        if (activeId === id) activeId = '';
    }

    function discardAll() {
        all().forEach((runtime) => discard(runtime.id));
    }

    window.HommeySessionRuntime = {
        MAX_CONCURRENT,
        VIEW_CLASS,
        ACTIVE_CLASS,
        configure,
        create,
        get,
        ensure,
        active,
        activeId: activeId_,
        setActive,
        all,
        runningCount,
        canStart,
        isRunning,
        mount,
        release,
        adopt,
        discard,
        discardAll,
    };
})();
