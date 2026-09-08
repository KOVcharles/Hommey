/* Two decoded image layers: the old photograph stays opaque until the new one covers it. */
(function () {
    'use strict';

    const scenery = document.getElementById('authScenery');
    const controls = document.getElementById('sceneControls');
    const choices = [...document.querySelectorAll('.auth-scene-choice')];
    const pauseButton = document.getElementById('scenePause');
    const label = document.getElementById('sceneLabel');
    const status = document.getElementById('sceneStatus');
    const panel = document.querySelector('.auth-panel');
    if (!scenery || !controls || !choices.length) return;

    const layers = [...scenery.querySelectorAll('.auth-scene-layer')];
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const connection = navigator.connection;
    const dwell = 9000;
    const transitionDuration = 1400;
    const cache = new Map();
    const failed = new Set();
    const zooms = new Map();
    let current = 0;
    let front = 0;
    let timer = null;
    let busy = false;
    let queued = null;
    let userPaused = false;
    let fade = null;

    function limitedMotion() {
        return reducedMotion.matches || document.documentElement.dataset.motion === 'off'
            || Boolean(connection?.saveData);
    }

    function isPaused() {
        return userPaused || limitedMotion() || document.hidden || panel.contains(document.activeElement);
    }

    function nextIndex() {
        for (let step = 1; step < choices.length; step += 1) {
            const index = (current + step) % choices.length;
            if (!failed.has(index)) return index;
        }
        return null;
    }

    function decodeScene(index) {
        if (cache.has(index)) return cache.get(index);
        const promise = new Promise((resolve, reject) => {
            const image = new Image();
            image.decoding = 'async';
            const timeout = setTimeout(() => done(new Error('Image load timed out')), 10000);
            let settled = false;
            function done(error) {
                if (settled) return;
                settled = true;
                clearTimeout(timeout);
                image.onload = null;
                image.onerror = null;
                if (error) reject(error);
                else resolve(image);
            }
            image.onload = () => image.decode().then(() => done(), done);
            image.onerror = () => done(new Error('Image unavailable'));
            image.src = choices[index].dataset.src;
        });
        cache.set(index, promise);
        promise.catch(() => cache.delete(index));
        return promise;
    }

    function warmNext() {
        if (limitedMotion() || document.hidden) return;
        const next = nextIndex();
        if (next !== null) decodeScene(next).catch(() => failed.add(next));
    }

    function schedule() {
        clearTimeout(timer);
        timer = null;
        if (busy || isPaused()) return;
        const next = nextIndex();
        if (next !== null) timer = setTimeout(() => select(next, false), dwell);
    }

    function startZoom(layer) {
        const oldZoom = zooms.get(layer);
        if (oldZoom) oldZoom.cancel();
        zooms.delete(layer);
        if (limitedMotion() || typeof layer.animate !== 'function') return;
        const zoom = layer.querySelector('img').animate([
            { transform: 'scale(1)' },
            { transform: 'scale(1.035)' },
        ], { duration: dwell + transitionDuration * 2, easing: 'linear', fill: 'forwards' });
        zooms.set(layer, zoom);
        if (isPaused()) zoom.pause();
    }

    function syncMotion() {
        const paused = isPaused();
        scenery.dataset.paused = String(paused);
        pauseButton.disabled = limitedMotion();
        pauseButton.setAttribute('aria-pressed', String(userPaused || limitedMotion()));
        pauseButton.setAttribute('aria-label', limitedMotion() ? '已按系统设置暂停背景轮换'
            : userPaused ? '继续背景轮换' : '暂停背景轮换');
        for (const zoom of zooms.values()) {
            if (limitedMotion()) zoom.cancel();
            else if (paused) zoom.pause();
            else if (zoom.playState === 'paused') zoom.play();
        }
        // A preference change is applied immediately, even during a crossfade.
        if (fade) {
            if (limitedMotion()) fade.finish();
            else if (userPaused || document.hidden) fade.pause();
            else if (fade.playState === 'paused') fade.play();
        }
        schedule();
    }

    async function select(index, manual) {
        if (!Number.isInteger(index) || index < 0 || index >= choices.length) return;
        if (busy) {
            if (manual) {
                queued = index;
                // A manual choice must also work when a previous dissolve was paused.
                if (fade?.playState === 'paused') fade.finish();
            }
            return;
        }
        if (index === current) return;
        clearTimeout(timer);
        busy = true;
        scenery.dataset.state = 'loading';
        status.textContent = '';
        const back = 1 - front;
        const incoming = layers[back];
        const outgoing = layers[front];
        try {
            const image = await decodeScene(index);
            // Focus or visibility may have changed while the network was loading.
            if (!manual && isPaused()) return;
            incoming.querySelector('img').src = image.src;
            await incoming.querySelector('img').decode();
            if (!manual && isPaused()) return;
            incoming.style.zIndex = '1';
            outgoing.style.zIndex = '0';
            startZoom(incoming);
            scenery.dataset.state = 'transitioning';
            if (!limitedMotion() && typeof incoming.animate === 'function') {
                fade = incoming.animate([{ opacity: 0 }, { opacity: 1 }], {
                    duration: transitionDuration, easing: 'cubic-bezier(.4, 0, .2, 1)', fill: 'forwards',
                });
                await fade.finished;
            }
            outgoing.classList.remove('is-current');
            incoming.classList.add('is-current');
            if (fade) fade.cancel();
            fade = null;
            zooms.get(outgoing)?.cancel();
            zooms.delete(outgoing);
            front = back;
            current = index;
            failed.delete(index);
            scenery.dataset.scene = String(index);
            choices.forEach((button, i) => button.setAttribute('aria-pressed', String(i === index)));
            label.textContent = `${String(index + 1).padStart(2, '0')} / ${choices[index].dataset.label}`;
            if (manual) status.textContent = `已切换至${choices[index].dataset.label}`;
            warmNext();
        } catch (error) {
            failed.add(index);
            if (fade) fade.cancel();
            fade = null;
            zooms.get(incoming)?.cancel();
            zooms.delete(incoming);
            if (manual) status.textContent = '这张背景暂时无法加载，已保留当前画面。';
        } finally {
            busy = false;
            scenery.dataset.state = 'idle';
            syncMotion();
            if (queued !== null) {
                const next = queued;
                queued = null;
                select(next, true);
            }
        }
    }

    choices.forEach((button, index) => button.addEventListener('click', () => select(index, true)));
    pauseButton.addEventListener('click', () => { userPaused = !userPaused; syncMotion(); });
    panel.addEventListener('focusin', syncMotion);
    panel.addEventListener('focusout', () => queueMicrotask(syncMotion));
    document.addEventListener('visibilitychange', syncMotion);
    reducedMotion.addEventListener('change', syncMotion);
    connection?.addEventListener('change', syncMotion);
    new MutationObserver(syncMotion).observe(document.documentElement, {
        attributes: true, attributeFilter: ['data-motion'],
    });
    window.addEventListener('pagehide', () => {
        clearTimeout(timer);
        for (const zoom of zooms.values()) zoom.pause();
    });
    window.addEventListener('pageshow', syncMotion);

    scenery.dataset.scene = '0';
    scenery.dataset.state = 'idle';
    controls.hidden = false;
    decodeScene(0).then(() => {
        startZoom(layers[front]);
        warmNext();
        syncMotion();
    }).catch(() => {
        failed.add(0);
        select(1, false);
    });
    syncMotion();
})();
