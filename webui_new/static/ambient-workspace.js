/* Interaction shell for the supplied Ambient Workspace design.
 * Data and conversation execution remain owned by app.js / SessionRuntime.
 */
(function () {
    'use strict';
    const root = document.documentElement;
    const body = document.body;
    const shell = document.getElementById('appShell');
    const workspace = document.getElementById('sidebar');
    const backdrop = document.getElementById('scrim');
    const entry = document.getElementById('sidebarToggle');
    const mobileEntry = document.getElementById('mobileHandle');
    const chatEntry = document.getElementById('workspaceChatEntry');
    const closeButton = document.getElementById('sidebarClose');
    const dragHandle = document.getElementById('dragHandle');
    const overview = document.getElementById('workspaceOverview');
    const history = document.getElementById('workspaceHistory');
    const tripButton = document.getElementById('workspaceTrip');
    const tripEmpty = document.getElementById('workspaceTripEmpty');
    const tripDetails = document.getElementById('workspaceTripDetails');
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
    const finePointer = matchMedia('(hover: hover) and (pointer: fine)');
    let callbacks = {};
    let previousFocus = null;
    let tripSession = '';
    let dragStart = null;
    let dragOffset = 0;
    const entries = [entry, mobileEntry, chatEntry];
    const proximity = { target: 0, current: 0, pointerX: innerWidth / 2, hovering: false, frame: 0 };
    const particleSpecs = [
        [10,67,1.2,-.8,-10,.34,0], [16,41,1.6,.5,-13,.28,1],
        [23,77,1.1,1.2,-8,.42,0], [31,51,1.5,-.9,-15,.3,0],
        [38,70,1.2,.6,-11,.38,1], [44,37,1.1,-.4,-9,.29,0],
        [52,60,1.7,.8,-14,.35,1], [59,79,1.1,-.7,-7,.4,0],
        [66,45,1.4,.4,-13,.3,0], [73,68,1.2,-1.1,-10,.38,1],
        [81,54,1.6,.7,-16,.31,0], [89,75,1.1,-.5,-9,.4,0],
        [94,34,1.3,.3,-12,.26,1], [6,88,1.1,.5,-7,.34,0],
        [27,91,1.4,-.7,-11,.3,0], [76,91,1.3,.9,-8,.36,0],
    ];
    const particles = particleSpecs.map(([x,y,size,dx,dy,opacity,orange], index) => {
        const dot = document.createElement('span');
        dot.className = 'particle';
        dot.style.setProperty('--x', `${x}%`);
        dot.style.setProperty('--y', `${y}%`);
        dot.style.setProperty('--size', `${size}px`);
        dot.style.setProperty('--particle-color', orange ? 'rgba(255,104,70,.7)' : 'rgba(96,92,84,.58)');
        document.getElementById('particles').append(dot);
        return {dot,x,dx,dy,opacity,phase:index*.73};
    });
    const clamp = (value) => Math.max(0, Math.min(1, value));
    const motionEnabled = () => !reducedMotion.matches && root.dataset.motion !== 'off';
    const isOpen = () => body.classList.contains('workspace-open');

    function easeProximity(now) {
        proximity.frame = 0;
        const awake = !document.hidden && shell.dataset.view === 'home' && !isOpen();
        const target = awake ? proximity.target : 0;
        const delta = target - proximity.current;
        proximity.current += delta * (delta > 0 ? .16 : .075);
        if (Math.abs(delta) < .001) proximity.current = target;
        const intensity = proximity.current;
        root.style.setProperty('--proximity', intensity.toFixed(3));
        root.style.setProperty('--entry-y', `${((1-intensity)*10).toFixed(2)}px`);
        root.style.setProperty('--line-scale', (.18+intensity*.82).toFixed(3));
        root.style.setProperty('--line-opacity', (.16+intensity*.58).toFixed(3));
        root.style.setProperty('--glow-opacity', (intensity*.72).toFixed(3));
        const animated = motionEnabled() && awake;
        particles.forEach(particle => {
            const drift = Math.sin(now/1800+particle.phase);
            const gather = proximity.hovering ? (50-particle.x)*.025 : 0;
            particle.dot.style.setProperty('--particle-x', `${((particle.dx*drift+gather+(proximity.pointerX/Math.max(innerWidth,1)-.5)*.8)*intensity).toFixed(2)}px`);
            particle.dot.style.setProperty('--particle-y', `${(particle.dy*intensity+Math.cos(now/2100+particle.phase)*1.8*intensity).toFixed(2)}px`);
            particle.dot.style.setProperty('--particle-scale', (.68+intensity*.34).toFixed(3));
            particle.dot.style.setProperty('--particle-opacity', animated ? (particle.opacity*intensity).toFixed(3) : '0');
        });
        if (!document.hidden && (Math.abs(target-intensity)>.001 || (animated && intensity>.01))) scheduleProximity();
    }
    function scheduleProximity() {
        if (!proximity.frame) proximity.frame = requestAnimationFrame(easeProximity);
    }
    function resetDrag() {
        dragStart = null;
        dragOffset = 0;
        workspace.classList.remove('is-dragging');
        workspace.style.removeProperty('transform');
        backdrop.style.removeProperty('opacity');
    }
    function showOverview() {
        overview.hidden = false;
        history.hidden = true;
        callbacks.search?.(false);
        document.getElementById('workspaceTitle').textContent = '信息中心';
    }
    function open(source) {
        if (isOpen()) return;
        previousFocus = source instanceof HTMLElement ? source : document.activeElement;
        showOverview();
        body.classList.add('workspace-open');
        workspace.inert = false;
        workspace.setAttribute('aria-hidden', 'false');
        entries.forEach(button => button.setAttribute('aria-expanded', 'true'));
        shell.inert = true;
        document.getElementById('proximityZone').inert = true;
        requestAnimationFrame(() => { if (isOpen()) closeButton.focus({preventScroll:true}); });
        proximity.target = 0;
        scheduleProximity();
    }
    function close() {
        if (!isOpen()) return;
        body.classList.remove('workspace-open');
        shell.inert = false;
        document.getElementById('proximityZone').inert = false;
        entries.forEach(button => button.setAttribute('aria-expanded', 'false'));
        resetDrag();
        const target = previousFocus?.isConnected && previousFocus.getClientRects().length ? previousFocus : (shell.dataset.view === 'home' ? (finePointer.matches ? entry : mobileEntry) : chatEntry);
        target?.focus({preventScroll:true});
        workspace.inert = true;
        workspace.setAttribute('aria-hidden', 'true');
    }
    function showHistory(search = false) {
        overview.hidden = true;
        history.hidden = false;
        document.getElementById('workspaceTitle').textContent = '历史记录';
        callbacks.search?.(search);
        if (!search) document.getElementById('workspaceBack').focus({preventScroll:true});
    }
    function setTrip(trip, sessionId) {
        tripSession = trip && sessionId ? sessionId : '';
        tripButton.disabled = false;
        tripEmpty.hidden = !!tripSession;
        tripDetails.hidden = !tripSession;
        document.getElementById('workspaceTripEyebrow').textContent = tripSession ? 'Current trip · 当前行程' : 'Your next trip';
        tripEmpty.querySelector('strong').textContent = '下一程，从这里开始。';
        tripEmpty.querySelector('p').textContent = '告诉 Hommey 地点和日期，慢慢补齐出发前的细节。';
        tripEmpty.lastElementChild.hidden = false;
        tripButton.setAttribute('aria-label', tripSession ? '继续当前会话的行程' : '开始规划行程');
        if (!tripSession) return;
        document.getElementById('workspaceTripOrigin').textContent = trip.origin || '待补充';
        document.getElementById('workspaceTripDestination').textContent = trip.destination || '待补充';
        document.getElementById('workspaceTripDates').textContent = `${trip.start_date || '待确认'} — ${trip.end_date || '待确认'}`;
        document.getElementById('workspaceTripPurpose').textContent = trip.trip_purpose || trip.purpose || '待补充';
    }
    function setTripState(message) {
        tripSession = '';
        tripDetails.hidden = true;
        tripEmpty.hidden = false;
        tripButton.disabled = true;
        tripButton.setAttribute('aria-label', message);
        tripEmpty.querySelector('strong').textContent = message;
        tripEmpty.querySelector('p').textContent = '当前会话的行程会显示在这里。';
        tripEmpty.lastElementChild.hidden = true;
    }
    function setSessions(sessions) {
        const recent = document.getElementById('workspaceRecent');
        recent.replaceChildren();
        document.getElementById('workspaceRecentCount').textContent = String(sessions.length).padStart(2,'0');
        if (!sessions.length) {
            const empty = document.createElement('div');
            empty.className = 'empty-state';
            empty.textContent = '还没有最近记录，开始一段新的对话吧。';
            recent.append(empty);
        }
        sessions.slice(0,3).forEach(session => {
            const button = document.createElement('button');
            button.className = 'recent-row';
            button.type = 'button';
            const label = document.createElement('span');
            label.textContent = session.title || '未命名会话';
            const date = document.createElement('small');
            const parsed = new Date(session.updated_at);
            const running = window.HommeySessionRuntime?.isRunning(session.session_id);
            date.textContent = running ? '正在生成' : (Number.isNaN(parsed.getTime()) ? '' : `${parsed.getMonth()+1}月${parsed.getDate()}日`);
            const arrow = document.createElement('span');
            arrow.textContent = '↗';
            arrow.setAttribute('aria-hidden','true');
            button.append(label,date,arrow);
            button.addEventListener('click', () => callbacks.openSession?.(session.session_id));
            recent.append(button);
        });
    }
    function setSessionsError() {
        document.getElementById('workspaceRecentCount').textContent = '—';
        const recent = document.getElementById('workspaceRecent');
        recent.replaceChildren();
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'recent-row';
        retry.textContent = '暂时无法读取记录，点击重试';
        retry.addEventListener('click', () => callbacks.refresh?.());
        recent.append(retry);
    }

    [mobileEntry,chatEntry].forEach(button => button.addEventListener('click', () => callbacks.open?.(button)));
    document.getElementById('workspaceHistoryButton').addEventListener('click', () => showHistory(false));
    document.getElementById('workspaceBack').addEventListener('click', () => {showOverview();document.getElementById('workspaceHistoryButton').focus();});
    tripButton.addEventListener('click', () => tripSession ? callbacks.openSession?.(tripSession) : callbacks.startTrip?.());
    document.getElementById('settingsButton').addEventListener('click', () => document.getElementById('settingsPreferencesTab').click());
    window.addEventListener('pointermove', event => {
        if (!finePointer.matches || isOpen() || shell.dataset.view !== 'home') return;
        proximity.pointerX = event.clientX;
        proximity.target = clamp(1-(innerHeight-event.clientY)/190);
        scheduleProximity();
    }, {passive:true});
    const rest = () => {proximity.target=0;scheduleProximity();};
    document.addEventListener('pointerleave', rest);
    window.addEventListener('blur', rest);
    document.addEventListener('visibilitychange', rest);
    new MutationObserver(scheduleProximity).observe(root,{attributes:true,attributeFilter:['data-motion']});
    new MutationObserver(rest).observe(shell,{attributes:true,attributeFilter:['data-view']});
    reducedMotion.addEventListener('change', scheduleProximity);
    entry.addEventListener('pointerenter', () => {proximity.hovering=true;scheduleProximity();});
    entry.addEventListener('pointerleave', () => {proximity.hovering=false;});
    entry.addEventListener('focus', () => {proximity.target=1;scheduleProximity();});
    entry.addEventListener('blur', rest);
    document.addEventListener('keydown', event => {
        if (!isOpen()) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopImmediatePropagation();
            const popover = document.getElementById('sessionPopover');
            if (!popover.hidden) {popover.hidden=true;workspace.querySelector('.session-more:focus')?.focus();return;}
            close();
            return;
        }
        if (event.key !== 'Tab') return;
        const popover = document.getElementById('sessionPopover');
        const focusables = [...workspace.querySelectorAll('button:not(:disabled),input:not(:disabled),a[href]'), ...(!popover.hidden ? popover.querySelectorAll('button') : [])].filter(el=>el.tabIndex>=0 && el.getClientRects().length && getComputedStyle(el).visibility!=='hidden');
        const first = focusables[0], last = focusables[focusables.length-1];
        if (!first) return;
        if (event.shiftKey && (document.activeElement === first || !focusables.includes(document.activeElement))) {event.preventDefault();last.focus();}
        else if (!event.shiftKey && (document.activeElement === last || !focusables.includes(document.activeElement))) {event.preventDefault();first.focus();}
    }, true);
    dragHandle.addEventListener('pointerdown', event => {
        if (!isOpen() || event.button !== 0) return;
        dragStart = event.clientY;
        dragOffset = 0;
        workspace.classList.add('is-dragging');
        dragHandle.setPointerCapture(event.pointerId);
    });
    dragHandle.addEventListener('pointermove', event => {
        if (dragStart === null) return;
        dragOffset = Math.max(0,event.clientY-dragStart);
        const distance = Math.min(dragOffset,workspace.offsetHeight*.68);
        workspace.style.transform = `translate3d(0,${distance}px,0)`;
        backdrop.style.opacity = String(Math.max(.18,1-distance/workspace.offsetHeight));
    });
    dragHandle.addEventListener('pointerup', () => {const dismiss=dragOffset>Math.min(150,workspace.offsetHeight*.2);resetDrag();if(dismiss)close();});
    dragHandle.addEventListener('pointercancel', resetDrag);
    window.HommeyWorkspace = {configure:options=>{callbacks=options;},open,close,showHistory,setTrip,setTripState,setSessions,setSessionsError};
})();
