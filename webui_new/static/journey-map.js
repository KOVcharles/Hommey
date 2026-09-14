(function () {
    'use strict';
    const SVG = 'http://www.w3.org/2000/svg';
    // Level 3 shows China as a country; 15 is the street detail hotels need. The eager set is
    // what the card loads up front - the same four levels it always did - so widening the
    // range costs no extra AMap calls until someone actually zooms out.
    const MIN_LEVEL = 3, MAX_LEVEL = 15, EAGER_LEVELS = [12, 13, 14, 15], LOCAL_LEVEL = 13;
    const clampLevel = value => Math.max(MIN_LEVEL, Math.min(MAX_LEVEL, value));
    let loadMap = null;
    const instances = new Set();
    const el = (tag, cls, text) => { const n = document.createElement(tag); n.className = cls || ''; if (text !== undefined) n.textContent = text; return n; };
    const icon = (paths, size) => {
        const svg = document.createElementNS(SVG, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('width', size); svg.setAttribute('height', size);
        svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '1.8');
        svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round'); svg.setAttribute('aria-hidden', 'true');
        for (const d of paths) { const p = document.createElementNS(SVG, 'path'); p.setAttribute('d', d); svg.append(p); }
        return svg;
    };
    const ICONS = {
        in: ['M12 5v14M5 12h14'], out: ['M5 12h14'],
        open: ['M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M16 21h3a2 2 0 0 0 2-2v-3M8 21H5a2 2 0 0 1-2-2v-3'],
        shut: ['M3 8h3a2 2 0 0 0 2-2V3M21 8h-3a2 2 0 0 1-2-2V3M21 16h-3a2 2 0 0 0-2 2v3M3 16h3a2 2 0 0 1 2 2v3'],
        fit: ['M4 9V5a1 1 0 0 1 1-1h4M20 9V5a1 1 0 0 0-1-1h-4M4 15v4a1 1 0 0 0 1 1h4M20 15v4a1 1 0 0 1-1 1h-4'],
    };
    const button = (label, text, action) => { const n = el('button', '', text); n.type = 'button'; n.title = label; n.setAttribute('aria-label', label); n.onclick = action; return n; };
    const iconButton = (label, paths, action, size = 16) => { const n = button(label, '', action); n.append(icon(paths, size)); return n; };
    new MutationObserver(() => instances.forEach(item => { if (item.root.isConnected) item.attached = true; else if (item.attached) item.dispose(); })).observe(document.documentElement, {childList:true, subtree:true});
    function create({anchor = null, hotels = [], origin = null, city = '', onSelectHotel} = {}) {
        const root = el('div', 'journey-map');
        const head = el('div', 'journey-map-head');
        const title = el('span', '', '会场与附近酒店');
        const frame = el('div', 'journey-map-frame'); frame.setAttribute('aria-label', '会场与酒店位置地图');
        const links = document.createElementNS(SVG, 'svg');
        links.setAttribute('class', 'journey-map-links'); links.setAttribute('viewBox', '0 0 750 600'); links.setAttribute('aria-hidden', 'true');
        const routeLayer = document.createElementNS(SVG, 'g'); routeLayer.setAttribute('class', 'journey-map-route');
        const hotelLayer = document.createElementNS(SVG, 'g'); hotelLayer.setAttribute('class', 'journey-map-spokes');
        const defs = document.createElementNS(SVG, 'defs');
        links.append(defs, routeLayer, hotelLayer);
        const pinsLayer = el('div', 'journey-map-pins');
        const images = new Map();
        const state = el('div', 'journey-map-loading', '请选择准确地点'); state.setAttribute('role', 'status');
        const legend = el('div', 'journey-map-legend'); legend.append(el('span', '', '● 会场'), el('span', 'journey-map-legend-hotel', '● 酒店'));
        const attribution = el('span', 'journey-map-attribution', '© 高德地图 · GS(2018)1709号');
        const zoomControls = el('div', 'journey-map-zoom');
        const plus = iconButton('放大地图', ICONS.in, () => animate(target + 1));
        const minus = iconButton('缩小地图', ICONS.out, () => animate(target - 1));
        const expand = iconButton('展开地图', ICONS.open, () => setExpanded(!root.classList.contains('is-expanded')));
        const homeIcon = icon(ICONS.fit, 13);
        const home = button('显示全部地点', '', () => { selected = null; buildPins(); animate(fitZoom()); showDetail(); onSelectHotel?.(null); });
        home.className = 'journey-map-home'; home.append(homeIcon, el('span', '', '显示全部')); home.hidden = true;
        zoomControls.append(expand, plus, minus);
        frame.append(links, pinsLayer, legend, home, zoomControls, attribution, state);
        const detail = el('div', 'journey-map-detail');
        let selected = null, view = LOCAL_LEVEL, target = LOCAL_LEVEL, raf = 0, last = 0, generation = 0, controller = null;
        let urls = [], pins = [], spokes = [], disposed = false, visible = false, pendingLoad = false;
        let originPoint = origin, route = null, levelTimer = 0, essentialFailed = false;
        const loadedLevels = new Set(), pendingLevels = new Set(), failedLevels = new Set();
        const reduced = matchMedia('(prefers-reduced-motion: reduce)');
        function setExpanded(open) {
            root.classList.toggle('is-expanded', open);
            expand.setAttribute('aria-expanded', String(open));
            expand.setAttribute('aria-label', open ? '收起地图' : '展开地图');
            expand.replaceChildren(icon(open ? ICONS.shut : ICONS.open, 16));
        }
        expand.setAttribute('aria-expanded', 'false');
        root.addEventListener('keydown', e => { if (e.key === 'Escape') { setExpanded(false); expand.focus(); } });
        head.append(title); root.append(head, frame, detail);
        function xy(point, at = view) {
            const size = 512 * Math.pow(2, at);
            const merc = p => { const sin = Math.sin(p.lat * Math.PI / 180); return [size * (p.lng + 180) / 360, size * (.5 - Math.log((1 + sin)/(1 - sin))/(4*Math.PI))]; };
            const a = merc(anchor.location), b = merc(point); return [50 + (b[0]-a[0])/7.5, 50 + (b[1]-a[1])/6];
        }
        // Percent -> the 750x600 viewBox. Same aspect as the frame and the AMap image, so the
        // SVG scales uniformly and the maths stays a plain identity.
        function svgPoint(location, at = view) { const [x, y] = xy(location, at); return [x * 7.5, y * 6]; }
        function fitZoom() { return anchor && hotels.every(h => { const [x,y] = xy(h.hotel.location, LOCAL_LEVEL); return x > 5 && x < 95 && y > 10 && y < 95; }) ? LOCAL_LEVEL : LOCAL_LEVEL - 1; }
        function stop() { cancelAnimationFrame(raf); raf = 0; last = 0; }
        function layer(level) {
            let img = images.get(level);
            if (img) return img;
            img = el('img', 'journey-map-image'); img.alt = ''; img.dataset.level = level; img.setAttribute('aria-hidden', 'true');
            const higher = [...images.keys()].filter(other => other > level).sort((a, b) => a - b)[0];
            frame.insertBefore(img, higher === undefined ? links : images.get(higher));
            images.set(level, img);
            return img;
        }
        function setState(text) {
            if (text) { if (state.textContent !== text) state.textContent = text; state.hidden = false; }
            else state.hidden = true;
        }
        function paint() {
            if (!anchor) return;
            const loaded = [...loadedLevels];
            const base = loaded.length ? Math.min(...loaded) : null;
            images.forEach((img, level) => {
                img.style.transform = `scale(${2 ** (view-level)})`;
                img.style.opacity = base !== null && loadedLevels.has(level) && (level === base || view >= level - .001) ? '1' : '0';
            });
            pins.forEach(({node, point}) => { const [x,y] = xy(point); node.style.left = `${x}%`; node.style.top = `${y}%`; node.hidden = x < 1 || x > 99 || y < 5 || y > 98; });
            paintLinks();
            frame.dataset.zoom = view.toFixed(4); plus.disabled = target >= MAX_LEVEL; minus.disabled = target <= MIN_LEVEL;
            // Never magnify a coarse basemap past 4x; say it is loading instead of showing mush.
            if (base === null) setState(essentialFailed ? '地图暂时不可用，地点信息仍可查看' : '正在加载地图…');
            else if (view < base - 2) setState('正在加载地图…');
            else setState(null);
        }
        function tick(now) { const delta = last ? Math.min(now-last,48) : 16; last = now; view += (target-view)*(1-Math.exp(-delta/85)); if (Math.abs(target-view)<.0008) { view=target; paint(); stop(); } else { paint(); raf=requestAnimationFrame(tick); } }
        function animate(next) { if (!anchor) return; target=clampLevel(next); scheduleLevels(target); if (reduced.matches) { stop(); view=target; paint(); } else if (!raf) raf=requestAnimationFrame(tick); }
        frame.addEventListener('wheel', e => { if (!anchor || e.ctrlKey) return; e.preventDefault(); const pixels=e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?300:1); animate(target-Math.max(-200,Math.min(200,pixels))*.0025); }, {passive:false});
        function showDetail() {
            detail.replaceChildren(); if (!anchor) return;
            const place = selected?.hotel || anchor;
            const copy=el('div'); copy.append(el('strong','',place.name),el('span','',place.address || place.city || city));
            const link=el('a','','高德打开 ↗'); link.href=`https://uri.amap.com/marker?position=${place.location.lng},${place.location.lat}&name=${encodeURIComponent(place.name)}`; link.target='_blank'; link.rel='noopener noreferrer';
            detail.append(copy,link);
        }
        function choose(id, notify = true) { selected=hotels.find(h=>h.hotel.provider_place_id===id)||null; buildPins(); showDetail(); animate(fitZoom()); if (notify) onSelectHotel?.(selected?.hotel.provider_place_id || null); }
        function buildPins() {
            pinsLayer.replaceChildren(); pins=[]; if (!anchor) return;
            [{point:anchor, row:null},...hotels.map(row=>({point:row.hotel,row}))].forEach(({point,row},i)=> {
                const active = row ? selected?.hotel.provider_place_id===point.provider_place_id : !selected;
                const pin=button(`${row?'酒店':'会场'}：${point.name}`,'',()=>choose(row?point.provider_place_id:null)); pin.className=`journey-map-pin${row?' is-hotel':''}${active?' is-active':''}`;
                pin.setAttribute('aria-pressed',String(active));
                if (row) pin.append(el('span','journey-map-hotel-number',String(i)));
                else { const svg=document.createElementNS(SVG,'svg'); svg.setAttribute('viewBox','0 0 24 30'); svg.setAttribute('aria-hidden','true'); const p=document.createElementNS(SVG,'path'); p.setAttribute('d','M12 28.5C10 26 1.5 18.2 1.5 12a10.5 10.5 0 0 1 21 0C22.5 18.2 14 26 12 28.5Z'); const c=document.createElementNS(SVG,'circle'); c.setAttribute('cx','12'); c.setAttribute('cy','11.8'); c.setAttribute('r','3.4'); svg.append(p,c); pin.append(svg); }
                if (active) pin.append(el('span','journey-map-pin-label',point.name));
                pinsLayer.append(pin); pins.push({node:pin,point:point.location});
            });
            buildLinks(); home.hidden = !selected; paint();
        }
        // Spokes radiate from the venue to each hotel; the trip arc runs origin -> venue and only
        // shows once the origin falls inside the frame, which is exactly when it is zoomed out.
        function buildLinks() {
            defs.replaceChildren(); hotelLayer.replaceChildren(); routeLayer.replaceChildren(); spokes=[]; route=null;
            if (!anchor) return;
            hotels.forEach((row, i) => {
                const active = selected?.hotel.provider_place_id === row.hotel.provider_place_id;
                const id = `journey-map-fade-${i}`;
                const gradient = document.createElementNS(SVG, 'linearGradient');
                gradient.setAttribute('id', id); gradient.setAttribute('gradientUnits', 'userSpaceOnUse');
                const from = document.createElementNS(SVG, 'stop'); from.setAttribute('offset', '0');
                const to = document.createElementNS(SVG, 'stop'); to.setAttribute('offset', '1'); to.setAttribute('stop-opacity', '0');
                from.setAttribute('stop-color', active ? '#df775b' : '#286650');
                to.setAttribute('stop-color', active ? '#df775b' : '#286650');
                gradient.append(from, to); defs.append(gradient);
                const line = document.createElementNS(SVG, 'line');
                line.setAttribute('class', `journey-map-spoke${active ? ' is-active' : ''}`);
                line.setAttribute('stroke', `url(#${id})`);
                line.setAttribute('vector-effect', 'non-scaling-stroke');
                const dot = document.createElementNS(SVG, 'circle');
                dot.setAttribute('class', `journey-map-spoke-dot${active ? ' is-active' : ''}`);
                dot.setAttribute('r', '2.6'); dot.setAttribute('vector-effect', 'non-scaling-stroke');
                hotelLayer.append(line, dot);
                spokes.push({point: row.hotel.location, line, dot, gradient});
            });
            if (originPoint) {
                route = {path: document.createElementNS(SVG, 'path'), dot: document.createElementNS(SVG, 'circle'),
                    label: el('span', 'journey-map-origin-label', originPoint.name || '')};
                route.path.setAttribute('class', 'journey-map-route-path');
                route.path.setAttribute('vector-effect', 'non-scaling-stroke');
                route.dot.setAttribute('class', 'journey-map-route-dot');
                route.dot.setAttribute('r', '3.4'); route.dot.setAttribute('vector-effect', 'non-scaling-stroke');
                routeLayer.append(route.path, route.dot);
                pinsLayer.append(route.label);
            }
        }
        function paintLinks() {
            if (!anchor) return;
            const [ax, ay] = svgPoint(anchor.location);
            spokes.forEach(({point, line, dot, gradient}) => {
                const [hx, hy] = svgPoint(point);
                line.setAttribute('x1', ax); line.setAttribute('y1', ay); line.setAttribute('x2', hx); line.setAttribute('y2', hy);
                dot.setAttribute('cx', hx); dot.setAttribute('cy', hy);
                gradient.setAttribute('x1', ax); gradient.setAttribute('y1', ay);
                gradient.setAttribute('x2', hx); gradient.setAttribute('y2', hy);
            });
            if (!route || !originPoint) return;
            const [ox, oy] = svgPoint(originPoint);
            // Control point off the midpoint's perpendicular, bowed proportionally so short and
            // long trips both read as the same gentle curve rather than a kink or a balloon.
            const qx = (ox + ax) / 2 - (ay - oy) * 0.18, qy = (oy + ay) / 2 + (ax - ox) * 0.18;
            route.path.setAttribute('d', `M${ox} ${oy} Q${qx} ${qy} ${ax} ${ay}`);
            route.dot.setAttribute('cx', ox); route.dot.setAttribute('cy', oy);
            const inside = ox > 0 && ox < 750 && oy > 0 && oy < 600;
            routeLayer.style.opacity = inside ? '1' : '0';
            route.label.hidden = !inside; route.label.style.left = `${ox / 7.5}%`; route.label.style.top = `${oy / 6}%`;
        }
        function ensureSignal() { if (disposed || !anchor) return null; if (!controller) controller = new AbortController(); return controller.signal; }
        async function loadLevel(level, place, scope) {
            if (level < MIN_LEVEL || level > MAX_LEVEL) return;
            if (loadedLevels.has(level) || pendingLevels.has(level) || failedLevels.has(level)) return;
            const signal = ensureSignal(); if (!signal) return;
            const version = generation, img = layer(level);
            pendingLevels.add(level);
            try {
                if (!loadMap) throw new Error('地图暂不可用');
                const blob = await loadMap(scope, place.provider_place_id || place.place_id, level, signal);
                if (version !== generation || disposed) return;
                const url = URL.createObjectURL(blob); urls.push(url); img.src = url; await img.decode();
                if (version !== generation || disposed) return;
                loadedLevels.add(level); paint();
            } catch (error) {
                if (version !== generation || error.name === 'AbortError' || disposed) return;
                // A missing level degrades silently; the coarsest level that did load stays the backdrop.
                failedLevels.add(level); img.remove(); images.delete(level);
                if (EAGER_LEVELS.includes(level) && !loadedLevels.size) essentialFailed = true;
            } finally { pendingLevels.delete(level); }
        }
        // Coarse levels are only fetched once the traveller actually zooms out towards them.
        function scheduleLevels(center) {
            clearTimeout(levelTimer);
            levelTimer = setTimeout(() => {
                if (!anchor || !visible || disposed) return;
                const around = Math.floor(center);
                [around - 1, around, around + 1].filter(level => level < EAGER_LEVELS[0]).forEach(level => loadLevel(level, anchor, city));
            }, 160);
        }
        async function fetchImages() {
            if (!pendingLoad || !visible || !anchor || disposed) return;
            pendingLoad=false; const place=anchor, scope=city;
            setState('正在加载地图…');
            await Promise.all(EAGER_LEVELS.map(level => loadLevel(level, place, scope)));
        }
        function setData(next) {
            generation++; stop(); controller?.abort(); controller = null; clearTimeout(levelTimer);
            urls.forEach(URL.revokeObjectURL); urls=[]; loadedLevels.clear(); pendingLevels.clear(); failedLevels.clear(); essentialFailed = false;
            anchor=next.anchor; hotels=next.hotels||[]; city=next.city||anchor?.city||''; selected=null;
            originPoint = next.origin !== undefined ? next.origin : originPoint;
            images.forEach(img=>{img.removeAttribute('src'); img.style.opacity='0';});
            title.textContent=hotels.length?'会场与附近酒店':'地点预览'; legend.lastChild.hidden=!hotels.length;
            state.hidden=false; state.textContent=anchor?'正在加载地图…':'请从高德结果中选择准确地点';
            detail.hidden=!anchor; view=target=hotels.length?fitZoom():14;
            buildPins(); showDetail(); pendingLoad=!!anchor; fetchImages();
        }
        const observer=new IntersectionObserver(entries=> { if(entries.some(e=>e.isIntersecting)) {visible=true; fetchImages();} }); observer.observe(root);
        const instance={root, attached:false, dispose(){ disposed=true; generation++; stop(); clearTimeout(levelTimer); controller?.abort(); observer.disconnect(); urls.forEach(URL.revokeObjectURL); instances.delete(instance); }};
        instances.add(instance); root.setData=setData; root.selectHotel=id=>choose(id,false); root.dispose=()=>instance.dispose();
        setData({anchor,hotels,city,origin}); return root;
    }
    window.HommeyJourneyMap={create,configure(options){loadMap=options.loadMap;}};
})();
