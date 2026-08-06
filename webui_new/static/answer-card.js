(function () {
    'use strict';

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function safeUrl(value) {
        try {
            const url = new URL(String(value || ''), window.location.origin);
            return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
        } catch (err) {
            return '';
        }
    }

    function fillComposer(text) {
        document.dispatchEvent(new CustomEvent('hommey:fill-composer', {
            detail: { text: String(text || '') },
        }));
    }

    function statusClass(status) {
        return ['ready', 'pending', 'required', 'optional'].includes(status) ? status : 'optional';
    }

    function splitLegacyList(value) {
        return String(value || '').split(/[；;\n]/).map((item) => item.replace(/^[\s•-]+/, '').trim()).filter(Boolean);
    }

    function legacyReimbursementItem(value, index) {
        const categories = [
            [['高铁', '火车', '机票', '车票', '航班'], '交通票据'],
            [['酒店', '住宿'], '住宿发票'],
            [['餐饮', '餐费', '餐补'], '餐饮凭证'],
            [['市内交通', '出租车', '地铁'], '市内交通'],
            [['会议', '签到'], '会议材料'],
            [['审批', '出差申请'], '出差审批'],
            [['报销单'], '报销单'],
        ];
        const match = categories.find(([keywords]) => keywords.some((keyword) => value.includes(keyword)));
        const optional = ['如需', '如有', '按需'].some((keyword) => value.includes(keyword));
        return {
            key: `legacy-reimbursement-${index}`,
            label: match ? match[1] : '报销材料',
            detail: value,
            status: optional ? 'optional' : 'required',
            status_label: optional ? '按需' : '需保留',
        };
    }

    function upgradeLegacyPreDeparture(documentData) {
        const data = { ...(documentData || {}) };
        const sections = Array.isArray(data.sections) ? [...data.sections] : [];
        if (data.pre_departure) return { ...data, sections };
        const index = sections.findIndex((section) => section?.title === '出发前确认');
        if (index < 0) return { ...data, sections };
        const legacy = sections[index];
        const noticeValue = legacy.items?.find((item) => item.label === '注意事项')?.value || '';
        const reimbursementValue = legacy.items?.find((item) => item.label === '报销准备')?.value || '';
        if (!noticeValue && !reimbursementValue) return { ...data, sections };

        const notes = splitLegacyList(noticeValue);
        const criticalItems = [];
        const addCritical = (key, label, detail, statusLabel, actionLabel, actionValue) => {
            if (!detail || criticalItems.some((item) => item.key === key)) return;
            criticalItems.push({
                key, label, detail, status: 'pending', status_label: statusLabel,
                action_label: actionLabel || '', action_value: actionValue || '',
            });
        };
        notes.forEach((note) => {
            if ((note.includes('会议') || note.includes('客户')) && note.includes('确认')) {
                addCritical('meeting', '会议安排', note, '待补充', '去补充', '会议地点和时间：');
            } else if (note.includes('住宿标准') || note.includes('酒店价格')) {
                addCritical('lodging', '酒店预订', note, '待核验', '补充标准', '公司住宿标准是：');
            } else if (note.includes('交通等级') || note.includes('商务座') || note.includes('商务舱')) {
                addCritical('transport', '交通票务', note, '待核验', '补充标准', '公司允许的交通等级是：');
            } else if (note.includes('预订')) {
                addCritical('booking', '预订进度', note, '待处理');
            }
        });

        const weatherNote = notes.find((note) => note.includes('天气')) || '';
        const weatherMatch = weatherNote.match(/天气[:：]\s*([^，,。]+)[，,]\s*(\d+)\s*[~～-]\s*(\d+)\s*°?C/);
        const preparation = [];
        if (/[雨降水]/.test(weatherNote)) preparation.push('雨具');
        if (weatherNote.includes('防晒')) preparation.push('防晒用品');
        if (weatherNote.includes('防暑')) preparation.push('补水防暑');
        if (weatherNote.includes('湿')) preparation.push('透气衣物');
        const weather = weatherNote ? {
            condition: weatherMatch?.[1] || '',
            temperature: weatherMatch ? `${weatherMatch[2]}–${weatherMatch[3]}°C` : '',
            humidity: '',
            advice: weatherNote,
            preparation: [...new Set(preparation)],
        } : null;
        const reimbursementItems = splitLegacyList(reimbursementValue).map(legacyReimbursementItem);
        sections.splice(index, 1);
        return {
            ...data,
            sections,
            pre_departure: {
                title: '出发前确认',
                summary: '关键事项确认后，行程和报销会更稳妥。',
                pending_count: criticalItems.length,
                critical_items: criticalItems,
                weather,
                reimbursement_items: reimbursementItems,
            },
        };
    }

    function renderCheckItem(item, compact) {
        const row = element('div', `departure-check-item is-${statusClass(item.status)}${compact ? ' is-compact' : ''}`);
        const marker = element('span', 'departure-check-marker');
        marker.setAttribute('aria-hidden', 'true');
        row.appendChild(marker);
        const content = element('div', 'departure-check-content');
        const title = element('div', 'departure-check-title');
        title.appendChild(element('strong', '', item.label));
        title.appendChild(element('span', 'departure-check-status', item.status_label));
        content.appendChild(title);
        if (item.detail) content.appendChild(element('p', '', item.detail));
        row.appendChild(content);
        if (item.action_label && item.action_value) {
            const button = element('button', 'departure-check-action', `${item.action_label} →`);
            button.type = 'button';
            button.addEventListener('click', () => fillComposer(item.action_value));
            row.appendChild(button);
        }
        return row;
    }

    function renderDepartureWeather(weather) {
        const panel = element('section', 'departure-weather-panel');
        panel.appendChild(element('h4', '', '天气与随行准备'));
        const metrics = element('div', 'departure-weather-metrics');
        [
            ['天气', weather.condition],
            ['温度', weather.temperature],
            ['体感', weather.humidity],
        ].forEach(([label, value]) => {
            if (!value) return;
            const metric = element('div', 'departure-weather-metric');
            metric.appendChild(element('span', '', label));
            metric.appendChild(element('strong', '', value));
            metrics.appendChild(metric);
        });
        if (metrics.childElementCount) panel.appendChild(metrics);
        if (Array.isArray(weather.preparation) && weather.preparation.length) {
            const tags = element('div', 'departure-preparation-tags');
            weather.preparation.forEach((value) => tags.appendChild(element('span', '', value)));
            panel.appendChild(tags);
        }
        if (weather.advice) panel.appendChild(element('p', 'departure-weather-advice', weather.advice));
        return panel;
    }

    function renderPreDeparture(checklist) {
        const block = element('section', 'answer-predeparture');
        const header = element('div', 'departure-header');
        const heading = element('div');
        heading.appendChild(element('h3', '', checklist.title || '出发前确认'));
        if (checklist.summary) heading.appendChild(element('p', '', checklist.summary));
        header.appendChild(heading);
        const pending = Number(checklist.pending_count || 0);
        header.appendChild(element('span', pending ? 'departure-pending-count has-pending' : 'departure-pending-count', pending ? `${pending} 项待处理` : '已检查'));
        block.appendChild(header);

        if (Array.isArray(checklist.critical_items) && checklist.critical_items.length) {
            const critical = element('div', 'departure-critical');
            critical.appendChild(element('h4', '', '必须确认'));
            const list = element('div', 'departure-critical-list');
            checklist.critical_items.forEach((item) => list.appendChild(renderCheckItem(item, false)));
            critical.appendChild(list);
            block.appendChild(critical);
        }

        const lower = element('div', 'departure-lower-grid');
        if (checklist.weather) lower.appendChild(renderDepartureWeather(checklist.weather));
        if (Array.isArray(checklist.reimbursement_items) && checklist.reimbursement_items.length) {
            const reimbursement = element('section', 'departure-reimbursement-panel');
            const title = element('div', 'departure-panel-title');
            title.appendChild(element('h4', '', '报销材料'));
            title.appendChild(element('span', '', `${checklist.reimbursement_items.length} 项`));
            reimbursement.appendChild(title);
            const list = element('div', 'departure-reimbursement-list');
            checklist.reimbursement_items.forEach((item) => list.appendChild(renderCheckItem(item, true)));
            reimbursement.appendChild(list);
            lower.appendChild(reimbursement);
        }
        if (lower.childElementCount) block.appendChild(lower);
        return block;
    }

    function renderItems(section, content) {
        if (!Array.isArray(section.items) || !section.items.length) return;
        const grid = element('div', 'answer-fact-grid');
        section.items.forEach((item) => {
            const fact = element('div', 'answer-fact');
            if (String(item.value || '').length + String(item.detail || '').length > 110) {
                fact.classList.add('is-wide');
            }
            fact.appendChild(element('span', 'answer-fact-label', item.label));
            fact.appendChild(element('strong', 'answer-fact-value', item.value));
            if (item.detail) fact.appendChild(element('span', 'answer-fact-detail', item.detail));
            grid.appendChild(fact);
        });
        content.appendChild(grid);
    }

    function renderWeather(section, content) {
        if (!Array.isArray(section.days) || !section.days.length) return;
        const days = element('div', 'answer-weather-days');
        section.days.forEach((day) => {
            const card = element('div', 'answer-weather-day');
            card.appendChild(element('span', 'answer-weather-date', day.date));
            card.appendChild(element('strong', 'answer-weather-condition', day.condition || '—'));
            const temperature = [day.low, day.high].filter(Boolean).join(' ～ ');
            if (temperature) card.appendChild(element('span', 'answer-weather-temperature', temperature));
            if (day.precipitation) card.appendChild(element('span', 'answer-weather-rain', day.precipitation));
            days.appendChild(card);
        });
        content.appendChild(days);
    }

    function renderSection(section, index) {
        const block = element('section', `answer-section answer-section-${section.kind || 'general'}`);
        if (section.status === 'error') block.classList.add('is-error');
        if (section.kind === 'trip' && index === 0) block.classList.add('is-overview');
        if (section.kind === 'trip' && /^第\s*\d+\s*天/.test(section.title || '')) block.classList.add('is-timeline');
        const heading = element('div', 'answer-section-heading');
        heading.appendChild(element('span', 'answer-section-mark'));
        heading.appendChild(element('h3', '', section.title));
        block.appendChild(heading);
        const content = element('div', 'answer-section-content');
        if (section.body) content.appendChild(element('p', 'answer-section-body', section.body));
        renderItems(section, content);
        renderWeather(section, content);
        block.appendChild(content);
        return block;
    }

    function renderDetails(label, body) {
        const details = element('details', 'answer-details');
        details.appendChild(element('summary', '', label));
        details.appendChild(body);
        return details;
    }

    function renderSources(sources) {
        const list = element('div', 'answer-source-list');
        sources.forEach((source) => {
            const row = element('div', 'answer-source-row');
            const url = safeUrl(source.url);
            const title = url ? element('a', 'answer-source-title', source.title) : element('span', 'answer-source-title', source.title);
            if (url) {
                title.href = url;
                title.target = '_blank';
                title.rel = 'noopener noreferrer';
            }
            row.appendChild(title);
            const meta = [source.detail, source.updated_at].filter(Boolean).join(' · ');
            if (meta) row.appendChild(element('span', 'answer-source-meta', meta));
            list.appendChild(row);
        });
        return list;
    }

    function create(documentData) {
        const data = upgradeLegacyPreDeparture(documentData);
        const card = element('article', 'answer-card');
        card.setAttribute('aria-label', data.title || '查询结果');

        const header = element('header', 'answer-card-header');
        const eyebrow = element('div', 'answer-card-eyebrow');
        eyebrow.appendChild(element('span', 'answer-card-route'));
        eyebrow.appendChild(element('span', '', 'Hommey · 已整理'));
        header.appendChild(eyebrow);
        header.appendChild(element('h2', '', data.title || '查询结果'));
        if (data.summary) header.appendChild(element('p', '', data.summary));
        card.appendChild(header);

        const sections = element('div', 'answer-card-sections');
        (data.sections || []).forEach((section, index) => sections.appendChild(renderSection(section, index)));
        card.appendChild(sections);

        if (data.pre_departure) card.appendChild(renderPreDeparture(data.pre_departure));

        if (Array.isArray(data.notices) && data.notices.length) {
            const notices = element('div', 'answer-notices');
            data.notices.forEach((notice) => notices.appendChild(element('p', '', notice)));
            card.appendChild(notices);
        }

        const footer = element('footer', 'answer-card-footer');
        if (data.plain_text) footer.appendChild(renderDetails('查看文字版', element('pre', 'answer-plain-text', data.plain_text)));
        if (Array.isArray(data.sources) && data.sources.length) {
            footer.appendChild(renderDetails(`来源与更新时间 · ${data.sources.length}`, renderSources(data.sources)));
        }
        if (footer.childElementCount) card.appendChild(footer);
        return card;
    }

    window.HommeyAnswerCard = { create };
})();
