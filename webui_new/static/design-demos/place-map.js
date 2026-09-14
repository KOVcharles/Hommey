(async function () {
    'use strict';
    const $ = id => document.getElementById(id);
    let data, city, anchor, confirmed = null, hotel = null, mode = 'places', zoom = 14, pending = false;
    let viewZoom = 14, animation = 0, lastFrame = 0, mapAnchorId = '';
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const mapImages = [...document.querySelectorAll('.map-image')];
    let pins = [];
    const el = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; };
    const distance = meters => meters < 1000 ? `${meters} 米` : `${(meters / 1000).toFixed(1)} 公里`;
    function speak(text) { $('status').textContent = text; }
    function filtered() { const q = $('search').value.trim().toLowerCase(); return city.places.filter(p => `${p.name} ${p.address} ${p.district}`.toLowerCase().includes(q)); }
    function renderList() {
        const hotels = mode === 'hotels';
        const items = hotels ? anchor.hotels : filtered();
        $('results').replaceChildren();
        $('results').setAttribute('aria-label', hotels ? '附近酒店候选' : '地点候选');
        $('listHeading').textContent = hotels ? `${items.length} 家附近酒店 · 点击查看位置` : `${items.length} 个示例地点 · ${city.name}市`;
        items.forEach((item, i) => {
            const selected = hotels ? hotel?.provider_place_id === item.provider_place_id : pending && anchor?.provider_place_id === item.provider_place_id;
            const button = el('button', `result${selected ? ' selected' : ''}`);
            button.setAttribute('aria-pressed', String(selected));
            button.appendChild(el('span', 'result-number', String(i + 1).padStart(2, '0')));
            const copy = el('span', 'result-copy');
            copy.append(el('strong', '', item.name), el('small', '', hotels ? `距会场 ${distance(item.distance_m)}${item.rating ? ` · 评分 ${item.rating}` : ''}` : `${item.district} · ${item.address}`));
            if (hotels) copy.appendChild(el('span', 'result-tag', item.name.includes('汉庭') ? '符合汉庭偏好' : '附近备选'));
            button.appendChild(copy);
            button.addEventListener('click', () => hotels ? pickHotel(item) : pickPlace(item));
            $('results').appendChild(button);
        });
        if (!items.length) $('results').appendChild(el('p', 'empty', hotels ? '该会场暂无酒店快照，可更换地点查看。' : `未找到匹配的示例地点。仅搜索${city.name}的预置地点，不会显示其他城市。`));
    }
    function project(point) {
        // These static-map snapshots use scale=1. Calibrated against AMap's
        // native marker at zoom 13: hotel 3 lands near pixel (206, 106).
        const size = 512 * Math.pow(2, viewZoom);
        const mercator = p => { const sin = Math.sin(p.lat * Math.PI / 180); return [size * (p.lng + 180) / 360, size * (.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI))]; };
        const a = mercator(anchor.location), b = mercator(point);
        return [(375 + b[0] - a[0]) / 750 * 100, (300 + b[1] - a[1]) / 600 * 100];
    }
    function marker(item, index, isHotel = false) {
        const selected = isHotel ? hotel?.provider_place_id === item.provider_place_id : !hotel && anchor.provider_place_id === item.provider_place_id;
        const pin = el('button', `pin${isHotel ? ' hotel' : ''}${selected ? ' active' : ''}`);
        pin.setAttribute('aria-label', `${isHotel ? '酒店' : '会场'}：${item.name}`);
        pin.setAttribute('aria-pressed', String(selected));
        pin.appendChild(el('span', 'pin-label', item.name));
        if (isHotel) {
            pin.appendChild(el('span', 'hotel-number', String(index + 1)));
        } else {
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('viewBox', '0 0 24 30'); svg.setAttribute('class', 'pin-symbol'); svg.setAttribute('aria-hidden', 'true');
            const path = document.createElementNS(svg.namespaceURI, 'path');
            path.setAttribute('d', 'M12 28.5C10 26 1.5 18.2 1.5 12a10.5 10.5 0 0 1 21 0C22.5 18.2 14 26 12 28.5Z');
            const dot = document.createElementNS(svg.namespaceURI, 'circle');
            dot.setAttribute('cx', '12'); dot.setAttribute('cy', '11.8'); dot.setAttribute('r', '3.4');
            svg.append(path, dot); pin.appendChild(svg);
        }
        pin.addEventListener('click', () => isHotel ? pickHotel(item) : mode === 'hotels' ? pickHotel(null) : pickPlace(item));
        $('markers').appendChild(pin);
        pins.push({ node: pin, location: item.location });
    }
    function paintCamera() {
        // All levels share the same geographic center. The lowest-resolution
        // image always covers the frame; sharper images fade in only at 1:1
        // or larger, so zooming never exposes an empty map edge.
        for (const image of mapImages) {
            const level = Number(image.dataset.level);
            image.style.transform = `scale(${Math.pow(2, viewZoom - level)})`;
            image.style.opacity = level === 13 || (viewZoom >= level - .001 && image.complete && image.naturalWidth > 0) ? '1' : '0';
        }
        for (const {node, location} of pins) {
            const [x, y] = project(location);
            node.style.left = `${x}%`; node.style.top = `${y}%`;
            node.hidden = x < 1 || x > 99 || y < 5 || y > 97;
        }
        $('mapFrame').dataset.zoom = viewZoom.toFixed(4);
        $('mapFrame').dataset.targetZoom = String(zoom);
    }
    function stopCamera() { cancelAnimationFrame(animation); animation = 0; lastFrame = 0; }
    function cameraFrame(now) {
        const elapsed = lastFrame ? Math.min(now - lastFrame, 48) : 16;
        lastFrame = now;
        viewZoom += (zoom - viewZoom) * (1 - Math.exp(-elapsed / 85));
        if (Math.abs(zoom - viewZoom) < .0008) {
            viewZoom = zoom; paintCamera(); stopCamera(); return;
        }
        paintCamera(); animation = requestAnimationFrame(cameraFrame);
    }
    function animateZoom(next) {
        if (!anchor) return;
        zoom = Math.max(13, Math.min(15, next));
        $('zoomIn').disabled = zoom >= 15; $('zoomOut').disabled = zoom <= 13;
        if (reducedMotion.matches) { stopCamera(); viewZoom = zoom; paintCamera(); }
        else if (!animation) animation = requestAnimationFrame(cameraFrame);
    }
    function renderMap() {
        stopCamera(); viewZoom = zoom;
        if (mapAnchorId !== anchor.provider_place_id) {
            mapAnchorId = anchor.provider_place_id;
            $('mapFrame').classList.add('loading'); $('mapError').hidden = true;
            for (const image of mapImages) {
                image.src = anchor.maps[image.dataset.level];
                image.alt = image.id === 'basemap' ? `${city.name}市，${anchor.name}周边的高德地图快照` : '';
            }
        }
        $('markers').replaceChildren(); pins = [];
        if (mode === 'hotels') { marker(anchor, 0); anchor.hotels.forEach((p, i) => marker(p, i, true)); }
        else city.places.forEach((p, i) => marker(p, i));
        $('zoomIn').disabled = zoom >= 15; $('zoomOut').disabled = zoom <= 13;
        $('mapTitle').textContent = `${city.name} · ${mode === 'hotels' ? '会场与附近酒店' : '地点预览'}`;
        $('hotelLegend').hidden = mode !== 'hotels';
        paintCamera();
        renderDetail();
    }
    function renderDetail() {
        const item = hotel || anchor;
        $('detailType').textContent = hotel ? `酒店位置 · 距会场 ${distance(hotel.distance_m)} · 房价待核实` : confirmed ? '已确认会议地点' : pending ? '正在预览 · 尚未确认' : '地图浏览位置 · 请从搜索结果中选择';
        $('detailName').textContent = item.name;
        $('detailAddress').textContent = `${city.name}市 · ${item.district || anchor.district} · ${item.address}`;
        $('detailIcon').textContent = hotel ? '▥' : '⌖';
        $('amapLink').href = `https://uri.amap.com/marker?position=${item.location.lng},${item.location.lat}&name=${encodeURIComponent(item.name)}`;
    }
    function renderMode() {
        const hotels = mode === 'hotels';
        $('placesTab').classList.toggle('active', !hotels); $('placesTab').setAttribute('aria-pressed', String(!hotels));
        $('hotelsTab').classList.toggle('active', hotels); $('hotelsTab').setAttribute('aria-pressed', String(hotels)); $('hotelsTab').disabled = !confirmed;
        $('placeTools').hidden = hotels; $('hotelTools').hidden = !hotels;
        $('confirm').disabled = !hotels && !pending;
        $('confirm').replaceChildren(document.createTextNode(hotels ? '重新选择会议地点' : '确认此地点'), el('span', '', hotels ? '↶' : '→'));
        $('selectionHint').textContent = hotels ? '酒店编号与地图上的标记对应' : '确认后，查看会场周边的酒店';
        renderList(); renderMap();
    }
    function pickPlace(item) {
        anchor = item; pending = true; confirmed = null; hotel = null; mode = 'places'; zoom = 14;
        renderMode();
        $('mapFrame').classList.remove('just-moved'); requestAnimationFrame(() => $('mapFrame').classList.add('just-moved'));
        speak(`地图已定位到${item.name}。确认此地点后可查看附近酒店。`);
    }
    function pickHotel(item) { hotel = item; zoom = 13; renderList(); renderMap(); speak(item ? `已突出显示${item.name}，距会场${distance(item.distance_m)}。` : `已回到会议地点：${anchor.name}。`); }
    function backToPlaces() { mode = 'places'; hotel = null; renderMode(); speak('可更换会议地点；选择新地点会重新匹配酒店。'); }
    function setCity(name) {
        city = data.cities.find(c => c.name === name); confirmed = null; hotel = null; pending = true; mode = 'places'; zoom = 14; anchor = city.places[0];
        $('search').value = ''; $('scope').textContent = `仅限${city.name}市`; $('searchHint').textContent = `在${city.name}示例地点中搜索`;
        renderMode(); speak(`当前目的地为${city.name}，只显示该城市地点。已清除之前的会场和酒店选择。`);
    }
    for (const image of mapImages) image.addEventListener('load', () => {
        if (image.id === 'basemap') { $('mapFrame').classList.remove('loading'); $('mapError').hidden = true; }
        if (anchor) paintCamera();
    });
    $('basemap').addEventListener('error', () => { $('mapFrame').classList.remove('loading'); $('mapError').hidden = false; });
    $('city').addEventListener('change', () => setCity($('city').value));
    $('search').addEventListener('input', () => { pending = false; confirmed = null; hotel = null; renderMode(); speak('请从匹配结果中选择准确地点。'); });
    $('search').addEventListener('keydown', e => { if (e.key === 'Enter' && filtered().length) { e.preventDefault(); pickPlace(filtered()[0]); } });
    $('confirm').addEventListener('click', () => {
        if (mode === 'hotels') return backToPlaces();
        if (!pending) return;
        confirmed = anchor.provider_place_id; mode = 'hotels'; hotel = null; zoom = 13; renderMode();
        speak(`已确认${anchor.name}，展示${anchor.hotels.length}家附近酒店。本页仅演示，不保存行程。`);
    });
    $('placesTab').addEventListener('click', backToPlaces);
    $('hotelsTab').addEventListener('click', () => { if (confirmed) { mode = 'hotels'; zoom = 13; renderMode(); } });
    $('reset').addEventListener('click', () => { hotel = null; zoom = mode === 'hotels' ? 13 : 14; renderList(); renderMap(); speak(`已回到${anchor.name}。`); });
    $('zoomIn').addEventListener('click', () => animateZoom(zoom + 1));
    $('zoomOut').addEventListener('click', () => animateZoom(zoom - 1));
    $('mapFrame').addEventListener('wheel', event => {
        if (event.ctrlKey || !anchor) return;
        event.preventDefault();
        const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 300 : 1);
        animateZoom(zoom - Math.max(-200, Math.min(200, pixels)) * .0025);
    }, {passive: false});
    function expand(force) { const expanded = force === undefined ? !$('mapPanel').classList.contains('expanded') : force; $('mapPanel').classList.toggle('expanded', expanded); $('expand').setAttribute('aria-expanded', String(expanded)); $('expand').setAttribute('aria-label', expanded ? '收起地图' : '展开地图'); $('expand').title = expanded ? '收起地图' : '展开地图'; }
    $('expand').addEventListener('click', () => expand());
    document.addEventListener('keydown', e => { if (e.key === 'Escape') expand(false); });
    try {
        const response = await fetch('place-map-assets/data.json');
        if (!response.ok) throw new Error('地图示例数据尚未准备好');
        data = await response.json(); setCity($('city').value);
    } catch (error) { speak(error.message); $('confirm').disabled = true; }
})();
