(function () {
    'use strict';
    let searchPlaces = null, sequence = 0;
    const el = (tag, cls, text) => {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = String(text);
        return node;
    };
    function button(text, action, cls = '') {
        const node = el('button', cls, text); node.type = 'button';
        node.addEventListener('click', action); return node;
    }
    function picker({ getCity, initialName = '', onSelect, onClear }) {
        const root = el('div', 'journey-place-picker');
        const input = el('input', 'journey-place-input');
        input.type = 'search'; input.autocomplete = 'off'; input.placeholder = '搜索会议中心、公司或详细地址';
        input.value = initialName; input.setAttribute('aria-label', '在目的地城市搜索工作地点');
        input.disabled = !getCity();
        input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-expanded', 'false');
        const list = el('div', 'journey-place-list'); list.id = `journey-places-${++sequence}`;
        list.setAttribute('role', 'listbox'); list.hidden = true; input.setAttribute('aria-controls', list.id);
        const status = el('p', 'journey-place-status', `仅搜索 ${getCity() || '所选目的地'} 内的地点，请从结果中选择`);
        status.setAttribute('role', 'status');
        let timer, controller, revision = 0, active = -1, options = [], selectedCity = '', selectedName = '';
        // 已确认的地点本身就是一次有效选择。不记下来的话，聚焦输入框会把已确认的
        // 名称当成新关键词重搜一遍，用户会以为自己的选择没有被接受。
        if (initialName) { selectedName = initialName; selectedCity = getCity(); }
        function close() { list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); active = -1; }
        function invalidate() { revision++; clearTimeout(timer); controller?.abort(); selectedName=''; onClear?.(); close(); }
        async function search(keyword, city, version) {
            controller = new AbortController();
            try {
                if (!searchPlaces) throw new Error('地点查询暂不可用，请刷新页面后重试');
                const items = await searchPlaces(city, keyword, controller.signal);
                if (version !== revision || city !== getCity() || input.value.trim() !== keyword || !root.isConnected) return;
                list.replaceChildren(); options = [];
                items.forEach((item, index) => {
                    const node = button('', () => {
                        if (city !== getCity() || version !== revision) { invalidate(); return; }
                        selectedCity = city; selectedName=item.name; input.value = item.name; close();
                        status.textContent = `${item.city} · ${item.district || ''} · 已选择`;
                        onSelect(item); input.focus();
                    }, 'journey-place-option');
                    node.id = `${list.id}-${index}`; node.setAttribute('role', 'option'); node.setAttribute('aria-selected', 'false');
                    node.append(el('strong', '', item.name), el('span', '', [item.city, item.district, item.address].filter(Boolean).join(' · ')));
                    options.push(node); list.appendChild(node);
                });
                status.textContent = items.length ? `${city}范围内找到 ${items.length} 个地点，请选择准确地址` : `${city}内没有找到匹配地点，请换一个关键词；不会推荐其他城市`;
                list.hidden = !items.length; input.setAttribute('aria-expanded', String(!!items.length));
            } catch (error) {
                if (version !== revision || error.name === 'AbortError') return;
                close(); status.textContent = '地点查询暂时不可用，请修改关键词重试。';
            }
        }
        input.addEventListener('input', () => {
            invalidate(); const city = getCity(), keyword = input.value.trim();
            if (!city) { status.textContent = '请先确定目的地城市'; return; }
            if (keyword.length < 2) { status.textContent = `仅搜索 ${city}，请输入至少两个字`; return; }
            status.textContent = `正在搜索 ${city}…`;
            const version = revision; timer = setTimeout(() => search(keyword, city, version), 300);
        });
        input.addEventListener('focus', () => {
            const city=getCity(), keyword=input.value.trim();
            if(!city || keyword.length<2 || (selectedName===keyword && selectedCity===city))return;
            clearTimeout(timer);controller?.abort();const version=++revision;
            timer=setTimeout(()=>search(keyword,city,version),150);
        });
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') { close(); return; }
            if (list.hidden || !options.length) return;
            if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
                event.preventDefault(); active = (active + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
                options.forEach((option, i) => option.setAttribute('aria-selected', String(i === active)));
                input.setAttribute('aria-activedescendant', options[active].id); options[active].scrollIntoView({ block: 'nearest' });
            } else if (event.key === 'Enter' && active >= 0) { event.preventDefault(); options[active].click(); }
        });
        root.addEventListener('focusout', () => setTimeout(() => { if (!root.contains(document.activeElement)) close(); }, 0));
        root.append(input, list, status);
        // 目的地换了，地点必须跟着换：清掉旧选择，并按新城市重新决定输入框能不能用。
        // 禁用状态不能只在构造时算一次，否则用户改完目的地就再也选不回办公地点。
        root.resetCity = () => {
            invalidate(); input.value = ''; selectedCity = '';
            const city = getCity();
            input.disabled = !city;
            status.textContent = city ? `仅搜索 ${city} 内的地点，请从结果中选择` : '请先确定目的地城市';
        };
        root.input = input;
        return root;
    }
    function mapLink(hotel) {
        const link = el('a', 'journey-map-link', '在高德查看 ↗');
        link.href = `https://uri.amap.com/marker?position=${hotel.location.lng},${hotel.location.lat}&name=${encodeURIComponent(hotel.name)}`;
        link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
    }
    function create(data) {
        const board = data.trip_options;
        const card = el('article', 'journey-card'); card.setAttribute('aria-label', '差旅出行选择');
        const header = el('header', 'journey-header');
        const brand = el('div', 'journey-eyebrow'); brand.append(el('span', 'hommey-card-logo'), el('span', '', 'HOMMEY / 差旅行程'));
        const incomplete = (data.sections || []).some(s => s.status !== 'success') || (data.notices || []).length;
        const state = [board.train_state, board.hotel_state].some(s => s.status === 'unavailable') ? '部分查询待重试' : !board.anchor && board.hotel_state.status !== 'excluded' ? '待确认工作地点' : incomplete ? '行程草案 · 有待确认项' : '候选信息已更新';
        brand.append(el('span', 'journey-overall-state', state));
        header.append(brand, el('h2', '', `${board.origin} → ${board.destination}`), el('p', 'journey-dates', `${board.start_date} — ${board.end_date} / ${board.duration_days} 天 / ${board.trip_purpose}`));
        if (board.preference_note) header.append(el('p', 'journey-preferences', board.preference_note));
        card.appendChild(header);
        let sectionNo = 0;
        function section(title, caption) {
            const node = el('section', 'journey-section');
            const head = el('div', 'journey-section-title'); head.append(el('span', 'journey-number', String(++sectionNo).padStart(2, '0')), el('h3', '', title));
            node.append(head, el('p', 'journey-caption', caption)); card.appendChild(node); return node;
        }
        const transport = section('去程车次', board.train_state.message);
        transport.classList.add('journey-compact-trains');
        if (board.trains.length) {
            const tools = el('div', 'journey-train-tools');
            const filter = el('select'); filter.setAttribute('aria-label', '筛选席别余票');
            ['全部席别', ...new Set(board.trains.flatMap(row => Object.keys(row.seats)))].forEach((seat, i) => {
                const option = el('option', '', i ? `${seat}有票` : seat); option.value = i ? seat : ''; filter.appendChild(option);
            });
            const count = el('span'); tools.append(count, filter); transport.appendChild(tools);
            const list = el('div', 'journey-trains'); transport.appendChild(list);
            const available = value => value && !['无', '--', '0', '候补'].includes(String(value));
            const COMPACT = 3;
            let expanded = false, trimTimer = 0;
            const more = button('展开全部车次', () => { expanded = !expanded; renderTrains(); }, 'journey-show-more');
            transport.append(more);
            // 高度用像素驱动，max-height 才有可过渡的起点和终点：紧凑态刚好放下三条，
            // 展开态跟着窗口长，大屏一次看更多，装不下的部分留在框内滚动。
            function compactHeight() {
                const rows = [...list.children].slice(0, COMPACT);
                if (!rows.length) return 0;
                const last = rows[rows.length - 1];
                // offsetTop/offsetHeight 不受容器裁剪影响，收起动画被打断时也算得准。
                return last.offsetTop - rows[0].offsetTop + last.offsetHeight;
            }
            function expandedHeight() {
                return Math.min(list.scrollHeight, window.innerHeight * .7, 760);
            }
            function applyHeight() {
                const target = expanded ? expandedHeight() : compactHeight();
                if (target) list.style.maxHeight = `${Math.round(target)}px`;
            }
            function ticketNode(row, enterIndex) {
                const ticket = el('div', 'journey-ticket');
                if (enterIndex >= 0) {
                    // 展开时补进来的车次错峰淡入，让人看得出多出来的是哪几条。
                    ticket.classList.add('is-entering');
                    ticket.style.animationDelay = `${Math.min(enterIndex, 8) * 26}ms`;
                }
                const service = el('div', 'journey-service'); service.append(el('strong', '', row.train_no), el('span', '', row.travel_date));
                const route = el('div', 'journey-ticket-route');
                const from = el('div'), to = el('div'); from.append(el('strong', '', row.depart_time), el('span', '', row.from_station));
                to.append(el('strong', '', row.arrive_time), el('span', '', `${row.to_station}${row.arrival_day_offset ? ` · +${row.arrival_day_offset}天` : ''}`));
                route.append(from, el('span', 'journey-duration', `${row.duration}\n────→`), to);
                const seats = el('div', 'journey-seats');
                Object.entries(row.seats).filter(([key]) => ['二等座', '一等座', '商务座', '软卧', '硬卧', '硬座'].includes(key)).forEach(([key,value]) => {
                    seats.appendChild(el('span', available(value) ? 'has-seats' : '', `${key} ${value}`));
                });
                ticket.append(service, route, seats);
                return ticket;
            }
            function renderRows(rows, showAll) {
                list.replaceChildren();
                if (!rows.length) { list.appendChild(el('p', 'journey-empty', '当前候选中没有该席别余票，可切回全部席别。')); return; }
                (showAll ? rows : rows.slice(0, COMPACT))
                    .forEach((row, index) => list.appendChild(ticketNode(row, showAll && index >= COMPACT ? index - COMPACT : -1)));
            }
            function renderTrains() {
                const rows = board.trains.filter(row => !filter.value || available(row.seats[filter.value]));
                count.textContent = `${rows.length} 个候选 · 有票优先，再按历时排序`;
                more.hidden = rows.length <= COMPACT;
                more.textContent = expanded ? '收起车次' : `查看其余 ${Math.max(0, rows.length - COMPACT)} 个车次`;
                clearTimeout(trimTimer);
                if (expanded) {
                    renderRows(rows, true);
                } else if (list.children.length > COMPACT) {
                    // 收起时框里还挂着展开后的全部车次：先缩高度，动画走完再裁掉，
                    // 否则下半截会在容器还没缩回去时就凭空消失。
                    trimTimer = setTimeout(() => { renderRows(rows, false); applyHeight(); }, 460);
                } else {
                    renderRows(rows, false);
                }
                applyHeight();
            }
            filter.addEventListener('change', renderTrains);
            renderTrains();
            // 卡片挂进文档之前量不到高度（offsetTop/offsetHeight 全是 0），首屏落位得等
            // 到下一帧；再等一帧才打开过渡，免得这次修正以动画形式滑给用户看。
            requestAnimationFrame(() => {
                applyHeight();
                requestAnimationFrame(() => list.classList.add('is-animated'));
            });
            // 窗口尺寸变了，展开高度要跟着重算；卡片被移除后顺手把监听摘掉。
            const onResize = () => {
                if (!list.isConnected) { window.removeEventListener('resize', onResize); return; }
                if (expanded) applyHeight();
            };
            window.addEventListener('resize', onResize);
        }
        const official = el('a', 'journey-map-link', '前往 12306 核实余票与票价 ↗'); official.href = 'https://www.12306.cn/'; official.target = '_blank'; official.rel = 'noopener noreferrer';
        if (board.train_state.status !== 'excluded') transport.appendChild(official);
        const location = section('会场与附近酒店', board.anchor ? '点击酒店可在地图定位 · 距离为直线参考，房价与房态待核实' : '选择目的地城市内的准确地点，再查询附近酒店');
        card.insertBefore(location, transport);
        const editor = el('details', 'journey-edit-place'); editor.open = !board.anchor;
        const editTitle = el('summary'); editTitle.append(el('strong', '', board.anchor?.name || '选择会议 / 办公地点'), el('span', '', board.anchor ? '修改地点 ›' : '请选择 ›'));
        const editBody = el('div', 'journey-place-editor'); editor.append(editTitle,editBody); location.append(editor);
        let selected = board.anchor ? { place_id: board.anchor.provider_place_id, name: board.anchor.name } : null;
        const update = button(board.anchor ? '确认修改，重新查询' : '确认地点，查询附近酒店', () => submit(), 'journey-primary');
        const editMap = window.HommeyJourneyMap?.create(); if (editMap) editMap.hidden=true;
        const placePicker = picker({ getCity: () => board.destination, initialName: board.anchor?.name || board.location_query,
            onSelect: item => { selected = item; update.disabled = false; if(editMap) {editMap.hidden=false; editMap.setData({anchor:item,city:board.destination});} },
            onClear: () => { selected = null; update.disabled = true; if(editMap) {editMap.hidden=true; editMap.setData({anchor:null});} } });
        update.disabled = !selected;
        const schedule = el('input', 'journey-schedule'); schedule.type = 'text'; schedule.value = board.work_schedule;
        schedule.placeholder = '会议时间（选填），如 9月12日 14:00'; schedule.maxLength = 300; schedule.setAttribute('aria-label', '会议或工作时间');
        editBody.append(placePicker); if(editMap) editBody.append(editMap); editBody.append(schedule,update);
        let submitting = false;
        function submit(retry = false) {
            if (submitting || (!selected && !retry)) return;
            if (retry && !selected && board.anchor) { editor.open=true; placePicker.input.focus(); return; }
            const trip = { origin: board.origin, destination: board.destination, start_date: board.start_date, end_date: board.end_date,
                duration_days: board.duration_days, trip_purpose: board.trip_purpose, work_schedule: schedule.value.trim(),
                work_location: selected?.name || '', work_location_place_id: selected?.place_id || '' };
            const detail = { text: selected ? `工作地点选择：${selected.name}（${board.destination}），请更新完整差旅行程及对应酒店。` : '重新查询并更新本次完整差旅行程。',
                requestPayload: { input_source: 'quick_trip_form', trip_input: trip, capability_selection: board.capability_selection },
                complete(success) { submitting=false; update.disabled = !selected; update.textContent = success ? '已生成新卡片' : '重试更新地点与酒店'; } };
            if (document.dispatchEvent(new CustomEvent('hommey:submit-message', { detail, cancelable: true }))) { submitting=true; update.disabled = true; update.textContent = '正在重新查询…'; }
        }
        if (board.hotel_state.status !== 'excluded') {
            const hotels = el('div','journey-place-board'); location.append(hotels);
            const hotelNodes = new Map();
            const highlight = id => hotelNodes.forEach((node,key)=>{node.classList.toggle('is-selected',key===id);node.setAttribute('aria-pressed',String(key===id));});
            const map = board.anchor && window.HommeyJourneyMap?.create({anchor:board.anchor,hotels:board.hotels,origin:board.origin_point,city:board.destination,onSelectHotel:highlight});
            if (board.hotels.length) {
                const grid = el('div', 'journey-hotels');
                board.hotels.forEach(({hotel, reason, matches_brand}, index) => {
                    const item = el('div', 'journey-hotel');
                    item.tabIndex=0; item.setAttribute('role','button'); item.setAttribute('aria-label',`在地图查看${hotel.name}`); item.setAttribute('aria-pressed','false');
                    hotelNodes.set(hotel.provider_place_id,item);
                    const show = ()=>{map?.selectHotel(hotel.provider_place_id);highlight(hotel.provider_place_id);};
                    item.addEventListener('click',event=>{if(!event.target.closest('a'))show();});
                    item.addEventListener('keydown',event=>{if(event.target===item && ['Enter',' '].includes(event.key)){event.preventDefault();show();}});
                    const top = el('div', 'journey-hotel-top'); top.append(el('span', '', String(index+1).padStart(2, '0')), el('span', matches_brand ? 'journey-brand-match' : '', reason));
                    item.append(top, el('h4', '', hotel.name));
                    const facts = el('div', 'journey-hotel-facts');
                    facts.append(el('strong', '', hotel.distance_m < 1000 ? `${hotel.distance_m} m` : `${(hotel.distance_m/1000).toFixed(1)} km`), el('span', '', '距会场'));
                    if (hotel.rating != null) facts.append(el('span', '', `评分 ${hotel.rating}`));
                    const bottom = el('div', 'journey-hotel-bottom'); bottom.append(el('span', '', '点击查看位置'), mapLink(hotel));
                    item.append(facts, bottom); grid.appendChild(item);
                }); hotels.appendChild(grid);
            } else hotels.appendChild(el('div', 'journey-empty', board.anchor ? '暂无可展示的酒店候选。' : '选择准确地点后，将按品牌偏好与距离展示附近酒店。'));
            if(map) hotels.append(map);
            if(board.hotel_state.status==='unavailable') location.append(el('p','journey-caption',board.hotel_state.message));
        } else if(board.anchor && window.HommeyJourneyMap) {
            location.append(window.HommeyJourneyMap.create({anchor:board.anchor,origin:board.origin_point,city:board.destination}));
        }
        const footer = el('footer', 'journey-footer');
        if (board.commute?.options?.length) {
            const commute = section('到站后的公共交通参考', `${board.commute.origin.name} → ${board.commute.destination.name}，请按实际乘坐车次的到达站核对`);
            board.commute.options.forEach(route => commute.appendChild(el('p', 'journey-caption', [route.lines.join(' → '), route.duration_sec == null ? '' : `约 ${Math.ceil(route.duration_sec / 60)} 分钟`, route.transit_fee_cny == null ? '' : `参考 ¥${route.transit_fee_cny}`].filter(Boolean).join(' · '))));
        }
        if (board.weather?.forecasts?.length) {
            const weather = el('div', 'journey-weather');
            board.weather.forecasts.filter(day => day.date >= board.start_date && day.date <= board.end_date).forEach(day => weather.appendChild(el('p', '', `${day.date} · ${day.day_condition} · ${day.low_c ?? '—'}–${day.high_c ?? '—'} °C`)));
            if (weather.childElementCount) footer.appendChild(weather);
        }
        const itinerary = (data.sections || []).filter(s => s.kind === 'trip' && s.title !== '出行选择' && s.items?.length);
        if (itinerary.length) {
            const plan = section('每日安排', '依据当前候选安排；会议时间和返程班次未确认时会明确标注');
            const days = el('div', 'journey-days');
            itinerary.forEach(s => s.items.forEach((item, i) => {
                const day = el('div', 'journey-day');
                const date = el('div', 'journey-day-date'); date.append(el('span', '', `DAY ${String(i + 1).padStart(2, '0')}`), el('strong', '', item.label));
                day.append(date);
                if (item.activities?.length) {
                    const activities = el('ul', 'journey-activities'); item.activities.forEach(text => activities.append(el('li', '', text))); day.append(activities);
                } else day.append(el('p', '', item.value));
                if (item.detail) day.append(el('small', '', item.detail)); days.append(day);
            })); plan.append(days);
        }
        const times = [board.train_state.retrieved_at, board.hotel_state.retrieved_at].filter(Boolean);
        if (times.length) footer.appendChild(el('small', '', `查询于 ${new Date(times.sort().at(-1)).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}（北京时间）`));
        footer.appendChild(button('重新查询', () => submit(true), 'journey-retry'));
        const policies = (data.sections || []).filter(s => s.kind === 'policy');
        if (policies.length) {
            const standards = section('差旅标准', '已核实的制度条款 · 具体档位结合职级与适用条件确认');
            if (window.HommeyPolicyCard) standards.append(HommeyPolicyCard.content(data));
            else {
            const list = el('div', 'journey-policy-list');
            policies.forEach(s => (s.items || []).forEach(item => {
                if (!item.value) return;
                const fact = el('div', 'journey-policy-fact');
                fact.append(el('span', 'journey-policy-label', item.label), el('p', 'journey-policy-value', item.value));
                list.appendChild(fact);
            }));
            standards.appendChild(list);
            const detailText = policies.map(s => s.body).filter(Boolean).join('\n');
            if (detailText) {
                const details = el('details', 'journey-policy-details');
                details.appendChild(el('summary', '', '查看完整标准与待确认项'));
                detailText.split(/\n+/).filter(Boolean).forEach(line => details.appendChild(el('p', 'journey-policy-detail', line)));
                standards.appendChild(details);
            }
            }
        }
        // Preserve specialist output beyond the map/choice board.
        (data.sections || []).filter(s => !['policy', 'notice'].includes(s.kind) && !itinerary.includes(s) && s.title !== '出行选择' && s.title !== '行程信息整理').forEach(s => {
            const detail = section(s.title, s.body || '');
            (s.items || []).forEach(item => detail.append(el('p', 'journey-caption', `${item.label}：${item.value}${item.detail ? ' · ' + item.detail : ''}`)));
            (s.days || []).forEach(day => detail.append(el('p', 'journey-caption', `${day.date} · ${day.condition} · ${day.low}–${day.high}`)));
        });
        const pending = [...new Set([...(data.notices || []), ...(board.next_actions || []), ...(data.sections || []).filter(s => s.kind === 'notice').flatMap(s => (s.body || '').split('\n'))].filter(Boolean))];
        if (pending.length) {
            const notes = section('还需确认', '补齐这些信息后，安排和费用适用范围会更准确'); notes.classList.add('journey-pending');
            const list = el('ul'); pending.forEach(text => list.append(el('li', '', text))); notes.append(list);
        }
        if (data.sources?.length && window.HommeyPolicyCard) footer.prepend(HommeyPolicyCard.sources(data));
        [...card.querySelectorAll(':scope > .journey-section .journey-number')].forEach((node, i) => node.textContent = String(i + 1).padStart(2, '0'));
        card.appendChild(footer);
        return card;
    }
    window.HommeyTripChoices = { create, picker, configure(options) { searchPlaces = options.searchPlaces; } };
})();
