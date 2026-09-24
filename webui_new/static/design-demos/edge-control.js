(() => {
    'use strict';
    const $ = (selector) => document.querySelector(selector);
    const panel = $('#edgePanel');
    const edge = $('#edgeOpen');
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const fine = matchMedia('(pointer: fine)');
    const key = 'hommey.edge-control.demo.v1';
    const escape = (text) => String(text ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
    const localDate = (offset = 0) => {
        const day = new Date();
        day.setDate(day.getDate() + offset);
        return `${day.getFullYear()}-${String(day.getMonth()+1).padStart(2,'0')}-${String(day.getDate()).padStart(2,'0')}`;
    };
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch { /* Local-only demo also works without persistent storage. */ }
    const validTrip = trip => trip && typeof trip.origin === 'string' && typeof trip.destination === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(trip.start) && /^\d{4}-\d{2}-\d{2}$/.test(trip.end) && Array.isArray(trip.services);
    let trips = Array.isArray(saved.trips) ? saved.trips.filter(validTrip).slice(0,30) : [];
    let draft = saved.draft && Array.isArray(saved.draft.services) ? saved.draft : null;
    let origin = typeof saved.origin === 'string' ? saved.origin : '北京';
    let checklist = Array.isArray(saved.checklist) ? [...new Set(saved.checklist.filter(index => Number.isInteger(index) && index >= 0 && index < 5))] : [];
    let current = null;
    let restoreFocus = null;
    let expanded = false;
    let toastTimer;
    const examples = [
        {id:'shanghai', origin:'北京', destination:'上海', start:localDate(3), end:localDate(5), purpose:'客户拜访', place:'陆家嘴', services:['交通','住宿','日程'], status:'待出发', example:true},
        {id:'hangzhou', origin:'上海', destination:'杭州', start:localDate(7), end:localDate(8), purpose:'项目沟通', place:'滨江区', services:['交通','日程'], status:'草稿', example:true},
        {id:'shenzhen', origin:'北京', destination:'深圳', start:localDate(-8), end:localDate(-6), purpose:'行业交流', place:'南山区', services:['交通','住宿'], status:'已结束', example:true}
    ];
    const items = ['确认会议时间和工作地点','检查证件与出行凭证','准备电脑和充电器','确认住宿与入住时间','保留报销所需票据'];
    function store() {
        try { localStorage.setItem(key, JSON.stringify({trips,draft,origin,checklist})); return true; }
        catch { return false; }
    }
    function toast(message) {
        clearTimeout(toastTimer);
        $('#toast').textContent = message;
        $('#toast').classList.add('visible');
        toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 2700);
    }
    function show(id, title = '主控') {
        for (const view of panel.querySelectorAll('.ec-view')) view.hidden = view.id !== id;
        $('#ecTitle').textContent = title;
        $('#ecTitle').tabIndex = -1;
        $('#ecScroll').scrollTop = 0;
        requestAnimationFrame(() => { if (!panel.inert) $('#ecTitle').focus({preventScroll:true}); });
    }
    function open(destination = 'home') {
        if (panel.inert) restoreFocus = document.activeElement;
        closeSidebar();
        document.body.classList.add('ec-open');
        panel.inert = false;
        $('#appShell').inert = true;
        $('#sidebar').inert = true;
        $('#edgeShade').hidden = false;
        edge.inert = true;
        edge.setAttribute('aria-expanded', 'true');
        if (destination === 'quick') openForm();
        else if (destination === 'preferences') openTool('preferences');
        else { renderTrips(); show('ecHome'); }
    }
    function close() {
        document.body.classList.remove('ec-open');
        panel.inert = true;
        $('#appShell').inert = false;
        $('#sidebar').inert = false;
        $('#edgeShade').hidden = true;
        edge.inert = false;
        edge.setAttribute('aria-expanded','false');
        if (restoreFocus?.isConnected && restoreFocus.getClientRects().length && getComputedStyle(restoreFocus).visibility !== 'hidden') restoreFocus.focus({preventScroll:true});
        else edge.focus({preventScroll:true});
        strengthTarget = 0;
        wake();
    }
    edge.addEventListener('click', () => open());
    $('#edgeClose').addEventListener('click', close);
    $('#edgeShade').addEventListener('click', close);
    panel.addEventListener('transitionend', event => {
        if (event.target === panel && !panel.inert && !panel.contains(document.activeElement)) $('#ecTitle').focus({preventScroll:true});
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            if (!panel.inert) close();
            else closeSidebar();
        }
        if (event.key !== 'Tab' || panel.inert) return;
        const controls = [...panel.querySelectorAll('button,input,select,textarea')].filter(node => !node.disabled && node.getClientRects().length);
        if (event.shiftKey && (document.activeElement === controls[0] || !controls.includes(document.activeElement))) {
            event.preventDefault(); controls.at(-1)?.focus();
        } else if (!event.shiftKey && document.activeElement === controls.at(-1)) {
            event.preventDefault(); controls[0]?.focus();
        }
    });
    function home() { renderTrips(); show('ecHome'); }
    function bindBack(view) { view.querySelector('[data-ec-back]')?.addEventListener('click', home); }
    const back = '<button class="ec-back" data-ec-back type="button">← 返回</button>';
    bindBack($('#ecFormView'));
    function renderTrips() {
        const list = [...trips, ...examples];
        $('#ecTrips').innerHTML = (expanded ? list : list.slice(0,3)).map((trip, index) => `<button class="ec-trip" data-trip="${index}"><span><strong>${escape(trip.destination)} · ${escape(trip.purpose)}</strong><small>${escape(trip.start.slice(5).replace('-','.'))} — ${escape(trip.end.slice(5).replace('-','.'))} · ${trip.example ? '示例' : '本机草稿'}</small></span><span class="ec-trip-status">${escape(trip.status || '草稿')}</span></button>`).join('');
        $('#ecTrips').querySelectorAll('[data-trip]').forEach(button => button.addEventListener('click', () => detail(list[Number(button.dataset.trip)])));
        $('#ecAll').textContent = expanded ? '收起 ↑' : '全部 ↗';
        $('#ecCheckCount').textContent = `${checklist.length} / ${items.length}`;
        $('#ecQuickNote').textContent = draft ? '继续上次未完成的填写' : '填好信息，开始安排';
    }
    $('#ecAll').addEventListener('click', () => { expanded = !expanded; renderTrips(); });
    $('#ecQuick').addEventListener('click', () => openForm());
    function openForm(trip) {
        current = structuredClone(trip || draft || {origin, destination:'', start:localDate(1), end:localDate(3), purpose:'', place:'', services:['交通','住宿','日程']});
        if (current.example) {
            delete current.id; delete current.example;
            current.start = localDate(1); current.end = localDate(3);
        }
        const form = $('#ecForm');
        for (const name of ['origin','destination','start','end','purpose','place']) form.elements[name].value = current[name] || '';
        form.elements.start.min = localDate();
        form.elements.end.min = current.start;
        form.querySelectorAll('[name="services"]').forEach(input => { input.checked = current.services.includes(input.value); });
        $('#ecError').hidden = true;
        show('ecFormView');
    }
    function collect() {
        const data = new FormData($('#ecForm'));
        for (const name of ['origin','destination','start','end','purpose','place']) current[name] = String(data.get(name) || '').trim();
        current.services = data.getAll('services');
        draft = structuredClone(current);
        $('#ecSaveNote').textContent = store() ? '填写已自动保留' : '暂存于本页，刷新将丢失';
        $('#ecForm').elements.end.min = current.start;
    }
    $('#ecForm').addEventListener('input', collect);
    $('#ecForm').addEventListener('submit', event => {
        event.preventDefault(); collect();
        let error = '';
        if (!current.origin || !current.destination || !current.purpose) error = '请填写出发地、目的地和出差目的。';
        else if (current.origin === current.destination) error = '请确认出发地与目的地，当前填写的城市相同。';
        else if (current.start < localDate() || current.end < current.start) error = '请确认出行日期。';
        else if ((Date.parse(current.end)-Date.parse(current.start))/86400000 >= 60) error = '单次行程请控制在 60 天以内。';
        else if (!current.services.length) error = '请选择至少一项需要安排的内容。';
        if (error) { $('#ecError').textContent = error; $('#ecError').hidden = false; return; }
        const trip = {...current, id:current.id || `draft-${Date.now()}`, status:'草稿'};
        trips = [trip, ...trips.filter(item => item.id !== trip.id)].slice(0,30);
        draft = null;
        const stored = store();
        detail(trip);
        toast(stored ? '草稿已保存到最近差旅' : '草稿暂存于本页，刷新将丢失');
    });
    function detail(trip) {
        const view = $('#ecDetail');
        view.innerHTML = `${back}<h3 class="ec-view-title">${escape(trip.destination)} · ${escape(trip.purpose)}</h3><p class="ec-description">${trip.example ? '示例行程' : '差旅草稿'}</p><dl class="ec-details"><dt>往返城市</dt><dd>${escape(trip.origin)} → ${escape(trip.destination)}</dd><dt>出行日期</dt><dd>${escape(trip.start)}<br>至 ${escape(trip.end)}</dd><dt>工作地点</dt><dd>${escape(trip.place || '待补充')}</dd><dt>需要安排</dt><dd>${escape(trip.services.join('、'))}</dd></dl><p class="ec-detail-note">当前为界面演示，草稿仅保存在本机。尚未查询实时交通、酒店或提交预订。</p><div class="ec-detail-actions"><button class="ec-text-button" id="ecCopy" type="button">复制摘要</button><button class="ec-primary" id="ecEdit" type="button">${trip.example ? '复用行程' : '继续编辑'} <span>→</span></button></div>`;
        bindBack(view); show('ecDetail');
        $('#ecEdit').addEventListener('click', () => openForm(trip));
        $('#ecCopy').addEventListener('click', async () => {
            const text = `${trip.origin} → ${trip.destination}\n${trip.start} 至 ${trip.end}\n${trip.purpose}\n地点：${trip.place || '待补充'}\n安排：${trip.services.join('、')}`;
            try { await navigator.clipboard.writeText(text); toast('摘要已复制'); }
            catch {
                let area = view.querySelector('textarea');
                if (!area) { area = document.createElement('textarea'); area.className = 'ec-copy-fallback'; area.readOnly = true; area.setAttribute('aria-label','行程摘要'); view.append(area); }
                area.value = text; area.focus(); area.select(); toast('请手动复制已选中的摘要');
            }
        });
    }
    function openTool(tool) {
        const view = $('#ecTool');
        let html;
        if (tool === 'preferences') html = `<h3 class="ec-view-title">差旅偏好</h3><p class="ec-description">新建差旅时，自动填写常用出发地。</p><form id="ecPreferences"><label>常用出发地<input name="origin" value="${escape(origin)}" maxlength="40" required></label><div class="ec-form-foot"><small>保存在当前浏览器</small><button class="ec-primary" type="submit">保存</button></div></form>`;
        else if (tool === 'checklist') html = `<h3 class="ec-view-title">出发清单</h3><p class="ec-description" id="ecProgress">已准备 ${checklist.length} / ${items.length} 项</p>${items.map((item,index) => `<label class="ec-check"><input type="checkbox" data-check="${index}" ${checklist.includes(index) ? 'checked' : ''}><span>${item}</span></label>`).join('')}`;
        else html = `<h3 class="ec-view-title">差旅制度</h3><p class="ec-description">演示入口，尚未接入企业知识库。</p>${[['交通标准','核对适用的交通方式、舱位与席别。'],['住宿与补助','按目的地和人员类别确认适用标准。'],['审批与报销','确认审批流程和应保留的报销凭证。']].map(([name,description]) => `<div class="ec-policy-row"><h4>${name}</h4><p>${description}</p></div>`).join('')}<p class="ec-demo-notice">以上为待核对项目，具体要求以企业制度为准。</p>`;
        view.innerHTML = back + html;
        bindBack(view); show('ecTool');
        $('#ecPreferences')?.addEventListener('submit', event => {
            event.preventDefault();
            const value = event.target.elements.origin.value.trim();
            if (!value) { event.target.elements.origin.focus(); return; }
            origin = value; toast(store() ? '偏好已保存' : '偏好暂存于本页');
        });
        view.querySelectorAll('[data-check]').forEach(input => input.addEventListener('change', () => {
            const index = Number(input.dataset.check);
            checklist = input.checked ? [...new Set([...checklist,index])] : checklist.filter(item => item !== index);
            store(); $('#ecProgress').textContent = `已准备 ${checklist.length} / ${items.length} 项`;
        }));
    }
    panel.querySelectorAll('[data-ec-tool]').forEach(button => button.addEventListener('click', () => openTool(button.dataset.ecTool)));

    // Original homepage controls: preserve their layout, with explicit demo boundaries.
    document.querySelectorAll('[data-quick-trip-open]').forEach(button => button.addEventListener('click', () => open('quick')));
    $('#accountButton').addEventListener('click', () => open('preferences'));
    $('#homeButton').addEventListener('click', () => { closeSidebar(); if (!panel.inert) close(); });
    $('#homeComposer').addEventListener('submit', event => { event.preventDefault(); toast('本页仅预览界面；请用快速差旅体验填写流程。'); });
    $('#promptRotator').addEventListener('click', () => { $('#homeInput').value = $('#promptRotator').dataset.prompt; $('#homeInput').focus(); });
    document.querySelectorAll('[data-voice-record],[data-attachment-panel]').forEach(button => button.addEventListener('click', () => toast('此入口沿用原版，本 demo 未接入业务服务。')));
    document.querySelectorAll('input[type="file"]').forEach(input => input.addEventListener('change', () => { toast('界面预览不会上传附件'); input.value = ''; }));
    const retrieval = $('[data-retrieval-mode-trigger]');
    retrieval.addEventListener('click', () => { const menu = $('[data-retrieval-mode-menu]'); menu.hidden = !menu.hidden; retrieval.setAttribute('aria-expanded',String(!menu.hidden)); });
    document.querySelectorAll('[data-retrieval-mode-option]').forEach(button => button.addEventListener('click', () => {
        $('[data-retrieval-mode-label]').textContent = button.dataset.retrievalModeOption === 'enhanced' ? '增强检索' : '标准检索';
        document.querySelectorAll('[data-retrieval-mode-option]').forEach(item => item.setAttribute('aria-checked',String(item === button)));
        $('[data-retrieval-mode-menu]').hidden = true; retrieval.setAttribute('aria-expanded','false');
    }));
    function closeSidebar() { $('#sidebar').classList.remove('open'); $('#scrim').classList.remove('visible'); }
    $('#sidebarToggle').addEventListener('click', () => { $('#sidebar').classList.add('open'); $('#scrim').classList.add('visible'); });
    $('#sidebarClose').addEventListener('click', closeSidebar);
    $('#scrim').addEventListener('click', closeSidebar);
    $('#newChatButton').addEventListener('click', () => { closeSidebar(); $('#homeInput').value = ''; $('#homeInput').focus(); });
    $('#searchToggle').addEventListener('click', () => toast('演示页没有实际会话记录'));
    $('#knowledgeButton').addEventListener('click', () => { open(); openTool('policy'); });
    for (const id of ['settingsButton','accountRow']) $(`#${id}`).addEventListener('click', () => open('preferences'));
    renderTrips();

    // Gaussian edge field. Every row belongs to the edge; the cursor's Y is the
    // Gaussian mean. Only nearby rows move inward; there are no orbital particles.
    const canvas = $('#edgeField');
    const context = canvas.getContext('2d');
    const fieldWidth = 132;
    let height = innerHeight;
    let pointerY = height / 2;
    let centerY = pointerY;
    let strengthTarget = 0;
    let strength = 0;
    let frame = 0;
    let previousTime = 0;
    let rows = [];
    const lanes = [.05, .14, .25, .38, .53, .69, .85, 1];
    function resize() {
        height = innerHeight;
        const ratio = Math.min(devicePixelRatio || 1, 2);
        canvas.width = fieldWidth * ratio;
        canvas.height = height * ratio;
        context?.setTransform(ratio,0,0,ratio,0,0);
        rows = Array.from({length:Math.ceil(height/3.6)}, (_,i) => ({y:i*3.6, jitter:Math.sin(i*127.1)*.75}));
        draw(performance.now());
    }
    function draw(time) {
        if (!context) return;
        context.clearRect(0,0,fieldWidth,height);
        const sigma = 82;
        const amplitude = 82 * strength;
        for (let row = 0; row < rows.length; row++) {
            for (let lane = 0; lane < lanes.length; lane++) {
                const y = rows[row].y + (lane%2)*1.65;
                const normalized = (y-centerY)/sigma;
                const gaussian = Math.exp(-.5*normalized*normalized);
                const drift = reduced.matches ? 0 : Math.sin(time*.00065+row*.42+lane)*.48*strength;
                const fromEdge = 1.4 + lane*.68 + amplitude*gaussian*lanes[lane] + rows[row].jitter + drift;
                const alpha = .085 + lane*.009 + gaussian*strength*(.15+lanes[lane]*.09);
                context.beginPath();
                context.arc(fieldWidth-fromEdge,y,.58+(row%7===0?.13:0),0,Math.PI*2);
                context.fillStyle = `rgba(94,99,93,${alpha})`;
                context.fill();
            }
        }
        edge.style.width = `${Math.round(27+amplitude)}px`;
        const active = strength > .22;
        edge.classList.toggle('is-near',active);
        $('#edgeHint').classList.toggle('visible',active && panel.inert);
        $('#edgeHint').style.top = `${Math.max(100,Math.min(height-65,centerY))}px`;
    }
    function tick(time) {
        frame = 0;
        if (document.hidden) return;
        const elapsed = Math.min((time-previousTime)/16.67 || 1,3);
        previousTime = time;
        const target = panel.inert ? strengthTarget : 0;
        const ease = reduced.matches ? 1 : 1-Math.pow(.82,elapsed);
        strength += (target-strength)*ease;
        centerY += (pointerY-centerY)*ease;
        draw(time);
        if ((target > .005 && !reduced.matches && panel.inert) || Math.abs(strength-target)>.002 || Math.abs(centerY-pointerY)>.2) frame = requestAnimationFrame(tick);
    }
    function wake() { if (!frame && !document.hidden) { previousTime = performance.now(); frame = requestAnimationFrame(tick); } }
    document.addEventListener('pointermove', event => {
        if (event.pointerType !== 'mouse' || !fine.matches) return;
        const distance = innerWidth-event.clientX;
        strengthTarget = Math.max(0, Math.min(1, 1-distance/240));
        pointerY = event.clientY;
        if (strengthTarget > 0 || strength > .002) wake();
    }, {passive:true});
    function rest() { strengthTarget=0; wake(); }
    document.documentElement.addEventListener('pointerleave',rest);
    window.addEventListener('blur',rest);
    edge.addEventListener('focus', () => { pointerY=height/2; strengthTarget=.65; wake(); });
    edge.addEventListener('blur',rest);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) { cancelAnimationFrame(frame); frame=0; }
        else wake();
    });
    reduced.addEventListener('change',wake);
    window.addEventListener('resize',resize,{passive:true});
    resize();
})();
