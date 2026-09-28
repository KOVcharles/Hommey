(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const panel = $('#controlPanel');
  const views = ['dashboardView', 'flowView', 'detailView', 'toolView'];
  const storageKey = 'hommey.quiet-departure.v1';
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const date = (offset = 0) => {
    const value = new Date(); value.setDate(value.getDate() + offset);
    return `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,'0')}-${String(value.getDate()).padStart(2,'0')}`;
  };
  const sampleTrips = [
    {id:'sample-shanghai',origin:'北京',destination:'上海',start:date(3),end:date(5),purpose:'客户拜访',place:'上海 · 陆家嘴',services:['交通方案','附近住宿','每日安排'],status:'待出发',sample:true},
    {id:'sample-hangzhou',origin:'上海',destination:'杭州',start:date(8),end:date(9),purpose:'项目沟通',place:'杭州 · 滨江区',services:['交通方案','每日安排'],status:'草稿',sample:true},
    {id:'sample-shenzhen',origin:'北京',destination:'深圳',start:date(-10),end:date(-8),purpose:'行业交流',place:'深圳 · 南山区',services:['交通方案','附近住宿','每日安排'],status:'已结束',sample:true}
  ];
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(storageKey) || '{}') || {}; } catch { /* Storage can be unavailable in private browsing. */ }
  const validTrip = (item) => item && typeof item.id === 'string' && typeof item.origin === 'string' && typeof item.destination === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(item.start) && /^\d{4}-\d{2}-\d{2}$/.test(item.end) && Array.isArray(item.services);
  let trips = Array.isArray(saved.trips) ? saved.trips.filter(validTrip).slice(0,50) : [];
  let preferences = {origin:'北京',transport:'高铁优先',hotel:'安静，靠近工作地点',...(saved.preferences && typeof saved.preferences === 'object' ? saved.preferences : {})};
  let checked = Array.isArray(saved.checked) ? saved.checked : [];
  let draft = saved.draft && typeof saved.draft === 'object' && Array.isArray(saved.draft.services) ? saved.draft : null;
  let current = null, step = 0, returnFocus = null, allTrips = false, toastTimer;
  const checklistItems = ['确认会议时间与工作地点','检查身份证件和出行凭证','准备电脑、充电器与转接头','确认住宿位置与入住时间','收好报销所需的票据'];
  const persist = () => { try { localStorage.setItem(storageKey, JSON.stringify({trips,preferences,checked,draft})); return true; } catch { return false; } };
  const toast = (message) => { clearTimeout(toastTimer); $('#toast').textContent = message; $('#toast').classList.add('visible'); toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 3100); };
  function showView(id, focus = true) {
    views.forEach((view) => { $(`#${view}`).hidden = view !== id; });
    $('#panelScroll').scrollTop = 0;
    if (focus) { const title = $(`#${id} h2`); if (title) { title.tabIndex = -1; requestAnimationFrame(() => { if (!panel.inert && !title.closest('[hidden]')) title.focus({preventScroll:true}); }); } }
  }
  function openPanel(target = 'dashboard') {
    if (!document.body.classList.contains('panel-open')) returnFocus = document.activeElement;
    document.body.classList.add('panel-open');
    $('#page').inert = true; panel.inert = false;
    $('#edgeTrigger').setAttribute('aria-expanded','true');
    $('#edgeTrigger').tabIndex = -1;
    if (target === 'quick') startFlow();
    else if (target === 'preferences') showTool('preferences');
    else { renderTrips(); showView('dashboardView'); if (target === 'recent') $('.recent-section').scrollIntoView({block:'nearest'}); }
    panel.setAttribute('aria-labelledby', target === 'quick' ? 'flowTitle' : target === 'preferences' ? 'toolTitle' : 'panelTitle');
  }
  function closePanel() {
    document.body.classList.remove('panel-open'); $('#page').inert = false; panel.inert = true;
    $('#edgeTrigger').setAttribute('aria-expanded','false'); $('#edgeTrigger').tabIndex = 0;
    (returnFocus?.isConnected ? returnFocus : $('#edgeTrigger')).focus({preventScroll:true});
  }
  $('#edgeTrigger').addEventListener('click', () => openPanel());
  panel.addEventListener('transitionend', (event) => {
    if (event.target === panel && event.propertyName === 'transform' && !panel.inert && !panel.contains(document.activeElement)) {
      panel.querySelector('.panel-view:not([hidden]) h2')?.focus({preventScroll:true});
    }
  });
  $('#closePanel').addEventListener('click', closePanel); $('#scrim').addEventListener('click', closePanel);
  document.querySelectorAll('[data-open]').forEach((button) => button.addEventListener('click', () => openPanel(button.dataset.open)));
  document.addEventListener('keydown', (event) => {
    if (!document.body.classList.contains('panel-open')) return;
    if (event.key === 'Escape') { event.preventDefault(); closePanel(); }
    if (event.key === 'Tab') {
      const items = [...panel.querySelectorAll('button,a,input,select,textarea,[tabindex="0"]')].filter((item) => !item.disabled && item.getClientRects().length);
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || !items.includes(document.activeElement))) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  const backButton = '<button class="back-button" data-back>← <span>返回差旅主控</span></button>';
  function bindBack(container) { container.querySelector('[data-back]').addEventListener('click', () => { renderTrips(); showView('dashboardView'); panel.setAttribute('aria-labelledby','panelTitle'); }); }
  function renderTrips() {
    const list = [...trips,...sampleTrips]; $('#tripCount').textContent = String(list.length).padStart(2,'0');
    $('#viewAll').textContent = allTrips ? '收起 ↑' : '查看全部 ↗';
    $('#recentTrips').innerHTML = (allTrips ? list : list.slice(0,3)).map((trip) => `<button class="trip-row" data-trip="${escape(trip.id)}"><span class="trip-date"><small>${Number(trip.start.slice(5,7))} 月</small><b>${trip.start.slice(8)}</b></span><span><strong>${escape(trip.origin)}<span class="route-arrow">→</span>${escape(trip.destination)}</strong><span class="trip-sub">${escape(trip.purpose)} · ${days(trip)} 天${trip.sample ? ' · 示例行程' : ' · 本机草稿'}</span></span><span class="trip-status"><span class="status-badge ${trip.status === '草稿' ? 'draft' : trip.status === '已结束' ? 'done' : ''}">${escape(trip.status)}</span><span>›</span></span></button>`).join('');
    $('#recentTrips').querySelectorAll('[data-trip]').forEach((button) => button.addEventListener('click', () => showDetail(list.find((item) => item.id === button.dataset.trip))));
    $('#checklistSubtitle').textContent = checked.length ? `${checked.length} / ${checklistItems.length} 项准备就绪` : '让准备少一点遗漏';
    $('#startQuick .feature-description').textContent = draft ? '上次的准备已保留，接着把旅程理清。' : '几项简单信息，一份清晰的出发准备。';
    $('#startQuick .feature-bottom>span:last-child').innerHTML = draft ? '继续填写 <b>→</b>' : '即刻开始 <b>→</b>';
  }
  function days(trip) { return Math.max(1,Math.round((Date.parse(trip.end)-Date.parse(trip.start))/86400000)+1); }
  $('#viewAll').addEventListener('click', () => { allTrips = !allTrips; renderTrips(); });
  $('#startQuick').addEventListener('click', () => startFlow());
  function freshTrip() { return {origin:preferences.origin,destination:'',start:date(1),end:date(3),purpose:'客户拜访',place:'',note:'',services:['交通方案','附近住宿','每日安排']}; }
  function startFlow(trip = null) { current = structuredClone(trip || draft || freshTrip()); if (current.sample) { delete current.id; delete current.sample; } step = 0; renderFlow(); }
  function rememberDraft() { draft = structuredClone(current); persist(); }
  const stepNames = ['去哪里，什么时候','照顾好每一个细节','确认这一次出发'];
  function renderFlow() {
    const view = $('#flowView');
    const captions = ['先定下目的地，其余的慢慢来。','让安排，更贴合这次出差。','看一眼，就可以收好这份出发准备。'];
    let content;
    if (step === 0) content = `<div class="route-fields"><label class="field">从这里出发<input name="origin" value="${escape(current.origin)}" placeholder="出发城市" maxlength="30" required></label><button class="swap-button" type="button" id="swapCities" aria-label="交换出发地和目的地">⇄</button><label class="field">抵达这里<input name="destination" value="${escape(current.destination)}" placeholder="目的地城市" maxlength="30" required></label></div><div class="city-chips"><span class="eyebrow" style="align-self:center">常去</span>${['上海','杭州','深圳','成都'].map((city) => `<button type="button" data-city="${city}">${city}</button>`).join('')}</div><div class="date-fields"><label class="field">出发日期<input type="date" name="start" value="${escape(current.start)}" min="${date()}" required></label><label class="field">返程日期<input type="date" name="end" value="${escape(current.end)}" min="${escape(current.start)}" required></label></div><div class="confirmation-note">不必一次想好全部细节。填写内容会保留在当前浏览器，随时回来继续。</div>`;
    else if (step === 1) content = `<label class="field">这次出发，为了什么？<input name="purpose" maxlength="80" value="${escape(current.purpose)}" placeholder="例如：客户拜访、项目沟通" required></label><label class="field">工作地点 <span class="eyebrow">选填</span><input name="place" maxlength="120" value="${escape(current.place)}" placeholder="公司、园区或会议地点"><small>先记下地点，后续规划时再核对准确地址。</small></label><label class="field">补充一点要求 <span class="eyebrow">选填</span><textarea name="note" maxlength="300" placeholder="例如：周一上午到达，酒店尽量安静">${escape(current.note)}</textarea></label><p class="field">想一起安排的事</p><div class="selection-list">${[['交通方案','梳理往返路线与时间','↗'],['附近住宿','以工作地点为中心，减少奔波','⌂'],['每日安排','给会议和路程留出合适的时间','◷']].map(([name,desc,icon]) => `<label class="selection"><input type="checkbox" name="services" value="${name}" ${current.services.includes(name) ? 'checked' : ''}><span><strong>${name}</strong><small>${desc}</small></span><span class="selection-symbol">${icon}</span></label>`).join('')}</div>`;
    else content = `${ticket(current)}<div class="confirmation-note">这一步会保存差旅草稿，方便继续规划。此 demo 不查询实时车票、酒店，也不提交预订。</div>`;
    view.innerHTML = `${backButton}<div class="eyebrow">QUICK DEPARTURE</div><h2 id="flowTitle">${stepNames[step]}</h2><p class="view-intro">${captions[step]}</p><div class="step-track">${[0,1,2].map((index) => `<span class="${index<=step?'active':''}"></span>`).join('')}</div><div class="step-caption"><span>0${step+1} / 03</span><span>路线与日期 → 差旅需求 → 出发准备</span></div><form id="tripForm">${content}<p class="error-message" id="flowError" role="alert" hidden></p><div class="flow-bottom">${step ? '<button class="secondary-button" id="previousStep" type="button">← 上一步</button>' : '<small>随时可以退出<br>下次从这里继续</small>'}<button class="primary-button" type="submit">${step === 2 ? '保存出发准备' : '下一步'} <span>→</span></button></div></form>`;
    bindBack(view); showView('flowView'); panel.setAttribute('aria-labelledby','flowTitle');
    const form = $('#tripForm');
    const collect = () => { const data = new FormData(form); ['origin','destination','start','end','purpose','place','note'].forEach((key) => { if (data.has(key)) current[key] = String(data.get(key)).trim(); }); if (step === 1) current.services = data.getAll('services'); rememberDraft(); };
    form.addEventListener('input', () => { collect(); if (step === 0) form.elements.end.min = current.start; });
    form.querySelectorAll('[data-city]').forEach((button) => button.addEventListener('click', () => { form.elements.destination.value = button.dataset.city; collect(); form.elements.destination.focus(); }));
    $('#swapCities')?.addEventListener('click', () => { const origin = form.elements.origin.value; form.elements.origin.value = form.elements.destination.value; form.elements.destination.value = origin; collect(); });
    $('#previousStep')?.addEventListener('click', () => { collect(); step--; renderFlow(); });
    form.addEventListener('submit', (event) => {
      event.preventDefault(); collect(); let error = '';
      if (step === 0) {
        if (!current.origin || !current.destination) error = '请填写出发城市和目的地。';
        else if (current.origin === current.destination) error = '出发地与目的地相同，请再确认一下。';
        else if (current.start < date() || current.end < current.start) error = '请确认出发和返程日期。';
        else if (days(current) > 60) error = '单次差旅请控制在 60 天以内。';
      } else if (step === 1 && !current.purpose) error = '请简单写下这次出差的目的。';
      else if (step === 1 && !current.services.length) error = '至少选一项需要安排的事。';
      if (error) { $('#flowError').textContent = error; $('#flowError').hidden = false; return; }
      if (step < 2) { step++; renderFlow(); } else saveTrip();
    });
  }
  function ticket(trip) {
    return `<div class="summary-ticket"><div class="summary-route"><div><small>DEPARTURE</small><strong>${escape(trip.origin)}</strong></div><span>╌╌ ↗ ╌╌</span><div><small>ARRIVAL</small><strong>${escape(trip.destination)}</strong></div></div><dl class="summary-meta"><dt>出行日期</dt><dd>${escape(trip.start)} — ${escape(trip.end)}<br>共 ${days(trip)} 天</dd><dt>出差目的</dt><dd>${escape(trip.purpose)}</dd><dt>工作地点</dt><dd>${escape(trip.place || '待补充')}</dd><dt>需要安排</dt><dd>${escape(trip.services.join(' · ') || '待补充')}</dd>${trip.note ? `<dt>补充要求</dt><dd>${escape(trip.note)}</dd>` : ''}</dl></div>`;
  }
  function saveTrip() {
    const trip = {...current,id:current.id || `trip-${Date.now()}`,status:'草稿',sample:false};
    trips = [trip,...trips.filter((item) => item.id !== trip.id)].slice(0,50); draft = null;
    const stored = persist(); renderTrips(); showDetail(trip,true,stored);
  }
  function showDetail(trip, justSaved = false, stored = true) {
    const view = $('#detailView');
    view.innerHTML = `${backButton}${justSaved ? '<div class="success-symbol">✓</div>' : '<div class="eyebrow">JOURNEY NOTES</div>'}<h2 id="detailTitle">${justSaved ? '出发准备，已收好。' : `${escape(trip.destination)}，${trip.status === '已结束' ? '旅途回顾。' : '下一站见。'}`}</h2><p class="view-intro">${justSaved ? (stored ? '草稿已保存到当前浏览器，也放进了最近差旅。' : '草稿已保留在本页；浏览器暂不允许持久保存，刷新会丢失。') : trip.sample ? '示例行程 · 用于体验浏览和复用交互。' : '本机草稿 · 你可以继续调整这份出发准备。'}</p>${ticket(trip)}<div class="confirmation-note">${trip.sample ? '示例数据不代表已预订的交通或住宿。可以复用这份准备，开启自己的差旅。' : '草稿已整理好。正式规划时，还需要核对地点、差旅标准和实时交通住宿信息。'}</div><div class="detail-actions"><button class="secondary-button" id="copyTrip">复制行程摘要</button><button class="primary-button" id="editTrip">${trip.sample ? '复用这次行程' : '继续编辑'} ↗</button></div>`;
    bindBack(view); showView('detailView'); panel.setAttribute('aria-labelledby','detailTitle');
    $('#editTrip').addEventListener('click', () => { const next = {...trip}; if (next.sample || next.start < date()) { next.start = date(1); next.end = date(3); } startFlow(next); });
    $('#copyTrip').addEventListener('click', async () => { const summary = `${trip.origin} → ${trip.destination}\n${trip.start} 至 ${trip.end}，共 ${days(trip)} 天\n${trip.purpose}\n工作地点：${trip.place || '待补充'}\n安排：${trip.services.join('、')}${trip.note ? `\n备注：${trip.note}` : ''}`; try { await navigator.clipboard.writeText(summary); toast('行程摘要已复制'); } catch { const area = document.createElement('textarea'); area.value = summary; area.readOnly = true; area.setAttribute('aria-label','行程摘要，请手动复制'); view.append(area); area.focus(); area.select(); toast('请选中摘要，手动复制'); } });
  }
  document.querySelectorAll('[data-tool]').forEach((button) => button.addEventListener('click', () => showTool(button.dataset.tool)));
  function showTool(tool) {
    const view = $('#toolView'); let content;
    if (tool === 'preferences') content = `<h2 id="toolTitle">记住你的习惯。</h2><p class="view-intro">少一次重复填写，多一点恰到好处。偏好保存在本机。</p><form id="preferencesForm"><label class="field">常用出发地<input name="origin" value="${escape(preferences.origin)}" maxlength="30" required></label><label class="field">交通偏好<select name="transport">${['高铁优先','飞机优先','按时间和费用综合选择'].map((option) => `<option ${preferences.transport === option ? 'selected' : ''}>${option}</option>`).join('')}</select></label><label class="field">住宿偏好<input name="hotel" value="${escape(preferences.hotel)}" maxlength="120" placeholder="例如：安静，靠近工作地点"></label><div class="confirmation-note">新建差旅会预填常用出发地。交通与住宿偏好会保留，供后续正式规划使用。</div><div class="flow-bottom"><small>让每一次出发更熟悉</small><button class="primary-button">保存偏好 ✓</button></div></form>`;
    else if (tool === 'checklist') content = `<h2 id="toolTitle">出发前，再看一眼。</h2><p class="view-intro">把零碎的小事放在这里，带着轻松的心情出发。</p><div class="progress-summary" id="checkProgress"></div><div class="checklist">${checklistItems.map((item,index) => `<label class="check-row"><input type="checkbox" data-check="${index}" ${checked.includes(index) ? 'checked' : ''}><span>${item}</span></label>`).join('')}</div><button class="quiet-button" id="resetChecklist">重置清单，准备下一次出发 ↗</button>`;
    else content = `<h2 id="toolTitle">出发前，心中有数。</h2><p class="view-intro">先把需要确认的事情列清楚。正式标准，以企业最新制度为准。</p><div class="confirmation-note">这是制度入口的交互示例，尚未接入企业知识库；以下为待核对项目，不是报销标准。</div>${[['交通与舱位','核对可选交通方式、舱位或席别，以及超标时的审批要求。'],['住宿与补助','按目的地、人员类别和出差日期，核对适用的住宿与补助标准。'],['审批与报销','确认出发前所需审批，以及行程结束后需要保留的凭证。']].map(([title,desc]) => `<article class="policy-card"><h3>${title}</h3><p>${desc}</p></article>`).join('')}`;
    view.innerHTML = `${backButton}<div class="eyebrow">TRAVEL COMPANION</div>${content}`;
    bindBack(view); showView('toolView'); panel.setAttribute('aria-labelledby','toolTitle');
    $('#preferencesForm')?.addEventListener('submit', (event) => { event.preventDefault(); const data = new FormData(event.target); if (!String(data.get('origin')).trim()) { event.target.elements.origin.focus(); return; } preferences = Object.fromEntries([...data].map(([key,value]) => [key,String(value).trim()])); toast(persist() ? '偏好已保存，下次出发会记得' : '偏好已保留在本页，浏览器不允许持久保存'); });
    if (tool === 'checklist') {
      const update = () => { $('#checkProgress').textContent = `${checked.length} / ${checklistItems.length} 项已准备${checked.length === checklistItems.length ? ' · 可以从容出发了 ✓' : ''}`; };
      update(); view.querySelectorAll('[data-check]').forEach((input) => input.addEventListener('change', () => { const index = Number(input.dataset.check); checked = input.checked ? [...new Set([...checked,index])] : checked.filter((item) => item !== index); persist(); update(); }));
      $('#resetChecklist').addEventListener('click', () => { checked = []; persist(); showTool('checklist'); toast('清单已重置'); });
    }
  }
  $('#homeComposer').addEventListener('submit', (event) => {
    event.preventDefault(); const thought = $('#travelThought').value.trim();
    if (thought) {
      const next = draft ? structuredClone(draft) : freshTrip(); next.note = thought;
      const destination = thought.match(/(?:去|前往|到)(上海|北京|杭州|深圳|广州|成都|南京|武汉|重庆|苏州)/);
      if (destination) next.destination = destination[1];
      draft = next; persist();
    }
    openPanel('quick'); if (thought) toast('需求已带入草稿，请确认城市和具体日期');
  });
  renderTrips();

  // A proximity field: quiet at rest, a gathering ribbon as the pointer approaches.
  const canvas = $('#edgeParticles'), context = canvas.getContext('2d');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const finePointer = matchMedia('(pointer: fine)');
  let width = 260, height = innerHeight, pointerY = height / 2, target = 0, strength = 0, frame = 0, last = 0;
  const particles = Array.from({length:68}, (_,i) => ({phase:i*2.39996,radius:18+(i%11)*5,seed:i/68,size:i%7 === 0 ? 1.8 : .85}));
  function resize() { height = innerHeight; const ratio = Math.min(devicePixelRatio || 1,2); canvas.width = width*ratio; canvas.height = height*ratio; context?.setTransform(ratio,0,0,ratio,0,0); }
  function tick(time) {
    frame = 0;
    if (!context || document.hidden || reducedMotion.matches || !finePointer.matches) return;
    const elapsed = Math.min((time-last)/16.67 || 1,3); last = time;
    const open = document.body.classList.contains('panel-open'); const desired = open ? 0 : target;
    strength += (desired-strength)*Math.min(.09*elapsed,1);
    context.clearRect(0,0,width,height);
    $('#edgeTrigger').style.setProperty('--proximity',strength.toFixed(3));
    if (strength > .005) {
      const center = height/2 + (pointerY-height/2)*.3;
      for (const particle of particles) {
        const angle = particle.phase + time*.00018;
        const spread = 27 + (1-strength)*42;
        const x = width-14 - Math.abs(Math.sin(angle))*particle.radius*(.8+strength*.7) - particle.seed*spread;
        const y = center + Math.cos(angle*.73+particle.seed)*((82+particle.seed*85)*(1.4-strength*.4));
        context.beginPath(); context.arc(x,y,particle.size*(.7+strength*.5),0,Math.PI*2);
        context.fillStyle = particle.seed>.88 ? `rgba(196,136,88,${strength*.65})` : `rgba(111,137,81,${strength*(.22+particle.seed*.55)})`;
        context.fill();
      }
      context.beginPath(); context.ellipse(width+13,center,48+strength*26,95, -.14, Math.PI*.5,Math.PI*1.5); context.strokeStyle = `rgba(132,152,105,${strength*.14})`; context.lineWidth = .8; context.stroke();
    }
    if (strength>.005 || desired>0) frame = requestAnimationFrame(tick);
  }
  function wake() { if (!frame && !document.hidden && !reducedMotion.matches && finePointer.matches) { last = performance.now(); frame=requestAnimationFrame(tick); } }
  document.addEventListener('pointermove', (event) => { if (event.pointerType !== 'mouse') return; target = Math.max(0,1-(innerWidth-event.clientX)/300); pointerY = event.clientY; wake(); },{passive:true});
  document.documentElement.addEventListener('pointerleave', () => { target=0; wake(); });
  window.addEventListener('blur', () => { target=0; wake(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { cancelAnimationFrame(frame); frame=0; } else wake(); });
  reducedMotion.addEventListener('change', () => { context?.clearRect(0,0,width,height); wake(); });
  window.addEventListener('resize', resize,{passive:true}); resize();
})();
