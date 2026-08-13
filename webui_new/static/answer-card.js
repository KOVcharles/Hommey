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

    const WEATHER_CONDITION_ZH = Object.freeze({
        'sunny': '晴朗',
        'clear': '晴',
        'partly cloudy': '局部多云',
        'cloudy': '多云',
        'overcast': '阴',
        'mist': '薄雾',
        'patchy rain possible': '局部可能有雨',
        'patchy rain nearby': '附近有零星降雨',
        'patchy snow possible': '局部可能有雪',
        'patchy snow nearby': '附近有零星降雪',
        'patchy sleet possible': '局部可能有雨夹雪',
        'patchy freezing drizzle possible': '局部可能有冻毛毛雨',
        'thundery outbreaks possible': '局部可能有雷暴',
        'blowing snow': '风吹雪',
        'blizzard': '暴风雪',
        'fog': '雾',
        'freezing fog': '冻雾',
        'patchy light drizzle': '局部小毛毛雨',
        'light drizzle': '小毛毛雨',
        'freezing drizzle': '冻毛毛雨',
        'heavy freezing drizzle': '强冻毛毛雨',
        'patchy light rain': '局部小雨',
        'light rain': '小雨',
        'moderate rain at times': '间歇性中雨',
        'moderate rain': '中雨',
        'heavy rain at times': '间歇性大雨',
        'heavy rain': '大雨',
        'light freezing rain': '小冻雨',
        'moderate or heavy freezing rain': '中到大冻雨',
        'light sleet': '小雨夹雪',
        'moderate or heavy sleet': '中到大雨夹雪',
        'patchy light snow': '局部小雪',
        'light snow': '小雪',
        'patchy moderate snow': '局部中雪',
        'moderate snow': '中雪',
        'patchy heavy snow': '局部大雪',
        'heavy snow': '大雪',
        'ice pellets': '冰粒',
        'light rain shower': '小阵雨',
        'moderate or heavy rain shower': '中到大阵雨',
        'torrential rain shower': '强阵雨',
        'light sleet showers': '小雨夹雪阵雨',
        'moderate or heavy sleet showers': '中到大雨夹雪阵雨',
        'light snow showers': '小阵雪',
        'moderate or heavy snow showers': '中到大阵雪',
        'light showers of ice pellets': '小冰粒阵雨',
        'moderate or heavy showers of ice pellets': '中到大冰粒阵雨',
        'patchy light rain with thunder': '局部小雨伴雷电',
        'moderate or heavy rain with thunder': '中到大雨伴雷电',
        'patchy light snow with thunder': '局部小雪伴雷电',
        'moderate or heavy snow with thunder': '中到大雪伴雷电',
    });
    const WEATHER_CONDITIONS_BY_LENGTH = Object.keys(WEATHER_CONDITION_ZH).sort((a, b) => b.length - a.length);

    function localizeWeatherCondition(value) {
        const raw = String(value || '').trim();
        return WEATHER_CONDITION_ZH[raw.toLowerCase().replace(/\s+/g, ' ')] || raw;
    }

    function localizeWeatherText(value) {
        let output = String(value || '');
        WEATHER_CONDITIONS_BY_LENGTH.forEach((condition) => {
            const escaped = condition.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            output = output.replace(new RegExp(`\\b${escaped}\\b`, 'gi'), WEATHER_CONDITION_ZH[condition]);
        });
        return output;
    }

    function localizeWeatherPresentation(documentData) {
        const data = { ...(documentData || {}) };
        const sections = Array.isArray(data.sections) ? data.sections : [];
        const hasWeather = sections.some((section) => section?.kind === 'weather') || !!data.pre_departure?.weather;
        if (!hasWeather) return data;
        data.summary = localizeWeatherText(data.summary);
        data.plain_text = localizeWeatherText(data.plain_text);
        data.sections = sections.map((section) => {
            if (section?.kind !== 'weather') return section;
            return {
                ...section,
                body: localizeWeatherText(section.body),
                items: (Array.isArray(section.items) ? section.items : []).map((item) => ({
                    ...item,
                    value: localizeWeatherText(item.value),
                    detail: localizeWeatherText(item.detail),
                })),
                days: (Array.isArray(section.days) ? section.days : []).map((day) => ({
                    ...day,
                    condition: localizeWeatherCondition(day.condition),
                })),
            };
        });
        if (data.pre_departure?.weather) {
            data.pre_departure = {
                ...data.pre_departure,
                weather: {
                    ...data.pre_departure.weather,
                    condition: localizeWeatherText(data.pre_departure.weather.condition),
                    advice: localizeWeatherText(data.pre_departure.weather.advice),
                },
            };
        }
        return data;
    }

    function splitLegacyList(value) {
        return String(value || '').split(/[；;\n]/).map((item) => item.replace(/^[\s•-]+/, '').trim()).filter(Boolean);
    }

    function normalizeMachinePlaceholders(documentData) {
        const data = { ...(documentData || {}) };
        const placeholder = (value) => ['unknown', 'none', 'null', 'n/a'].includes(String(value || '').trim().toLowerCase());
        const humanUnknown = '合规信息尚待核验，请以公司制度和人工审核结果为准。';
        data.notices = (Array.isArray(data.notices) ? data.notices : []).map((notice) => (
            placeholder(notice) ? humanUnknown : notice
        ));
        data.sections = (Array.isArray(data.sections) ? data.sections : []).map((section) => ({
            ...section,
            body: section?.kind === 'notice' && placeholder(section?.body) ? humanUnknown : section?.body,
            items: (Array.isArray(section?.items) ? section.items : []).map((item) => ({
                ...item,
                value: placeholder(item?.value) ? '待核验' : item?.value,
            })),
        }));
        return data;
    }

    function upgradeLegacyTripTimeline(documentData) {
        const data = { ...(documentData || {}) };
        const sections = Array.isArray(data.sections) ? data.sections : [];
        if (sections.some((section) => section?.kind === 'trip' && /^第\s*\d+\s*天/.test(section?.title || ''))) {
            return { ...data, sections };
        }
        const timeLabel = /^\d{1,2}:\d{2}(?:\s*[-–—~～]\s*\d{1,2}:\d{2})?$/;
        const upgraded = [];
        sections.forEach((section) => {
            const items = Array.isArray(section?.items) ? section.items : [];
            const scheduled = section?.kind === 'trip'
                ? items.filter((item) => timeLabel.test(String(item?.label || '').trim()))
                : [];
            if (scheduled.length < 2 || scheduled.length < Math.ceil(items.length * 0.6)) {
                upgraded.push(section);
                return;
            }
            const overviewItems = items.filter((item) => !scheduled.includes(item));
            if (section.body || overviewItems.length) {
                upgraded.push({ ...section, items: overviewItems, days: [] });
            }
            upgraded.push({
                ...section,
                title: '第 1 天',
                body: '',
                items: scheduled,
                days: [],
            });
        });
        return { ...data, sections: upgraded };
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

    function overviewFactClass(label) {
        const value = String(label || '').trim();
        if (/首选交通|交通建议/.test(value)) return ' is-transport';
        if (/备选交通/.test(value)) return ' is-alternative';
        if (/行程时长/.test(value)) return ' is-duration';
        if (/住宿建议/.test(value)) return ' is-lodging';
        if (/预算参考/.test(value)) return ' is-budget';
        return '';
    }

    function parseTransportLegs(value) {
        const source = String(value || '').trim();
        if (!source) return [];
        return source
            .split(/(?=(?:去程|返程)\s*[:：])/)
            .map((part) => part.replace(/^[；;\s]+/, '').trim())
            .map((part) => {
                const direction = part.match(/^(去程|返程)\s*[:：]\s*/);
                if (!direction) return null;
                const description = part.slice(direction[0].length).replace(/[；;\s]+$/, '').trim();
                const wrapped = description.match(/^([^（(]+?)\s*[（(]([\s\S]+)[）)]$/);
                const service = (wrapped?.[1] || '').trim();
                const details = (wrapped?.[2] || description).trim();
                const parts = details.split(/[，,]/).map((item) => item.trim()).filter(Boolean);
                const route = (parts.shift() || '').match(/^(.+?)\s+(\d{1,2}:\d{2})\s*[→–—-]\s*(.+?)\s+(\d{1,2}:\d{2})$/);
                return {
                    direction: direction[1],
                    service,
                    description,
                    origin: route?.[1] || '',
                    departure: route?.[2] || '',
                    destination: route?.[3] || '',
                    arrival: route?.[4] || '',
                    metadata: route ? parts : [],
                };
            })
            .filter(Boolean);
    }

    function renderTransportValue(item, fact) {
        const legs = parseTransportLegs(item.value);
        if (!legs.length) {
            fact.appendChild(element('strong', 'answer-fact-value', item.value));
            return;
        }

        const routes = element('div', 'answer-transport-routes');
        legs.forEach((leg) => {
            const card = element('div', 'answer-transport-leg');
            const head = element('div', 'answer-transport-head');
            head.appendChild(element('span', `answer-transport-direction is-${leg.direction === '返程' ? 'return' : 'outbound'}`, leg.direction));
            if (leg.service) head.appendChild(element('strong', 'answer-transport-service', leg.service));
            card.appendChild(head);

            if (leg.origin && leg.destination) {
                const route = element('div', 'answer-transport-route');
                const origin = element('div', 'answer-transport-point');
                origin.appendChild(element('strong', '', leg.departure));
                origin.appendChild(element('span', '', leg.origin));
                route.appendChild(origin);
                const line = element('span', 'answer-transport-line');
                line.setAttribute('aria-hidden', 'true');
                route.appendChild(line);
                const destination = element('div', 'answer-transport-point is-destination');
                destination.appendChild(element('strong', '', leg.arrival));
                destination.appendChild(element('span', '', leg.destination));
                route.appendChild(destination);
                card.appendChild(route);
            } else {
                card.appendChild(element('p', 'answer-transport-copy', leg.description));
            }

            if (leg.metadata.length) {
                const metadata = element('div', 'answer-transport-meta');
                leg.metadata.forEach((value) => metadata.appendChild(element('span', '', value)));
                card.appendChild(metadata);
            }
            routes.appendChild(card);
        });
        fact.appendChild(routes);
    }

    function cleanTimelineDetail(value) {
        const source = String(value || '').trim();
        if (!source) return '';
        const parts = source.split(/\s+[·•]\s+/).map((part) => part.trim()).filter(Boolean);
        if (parts.length > 1) return parts[0];
        const transportOnly = /^(?:步行|打车|出租车|网约车|地铁|公交|驾车|高铁|火车)(?:\s*[\/、或]\s*(?:步行|打车|出租车|网约车|地铁|公交|驾车|高铁|火车))*$/;
        return transportOnly.test(source) ? '' : source;
    }

    function renderItems(section, content, isOverview, isTimeline) {
        if (!Array.isArray(section.items) || !section.items.length) return;
        const grid = element('div', `answer-fact-grid${isOverview ? ' answer-overview-grid' : ''}`);
        section.items.forEach((item) => {
            const fact = element('div', `answer-fact${isOverview ? overviewFactClass(item.label) : ''}`);
            if (String(item.value || '').length + String(item.detail || '').length > 110) {
                fact.classList.add('is-wide');
            }
            fact.appendChild(element('span', 'answer-fact-label', item.label));
            if (isOverview && fact.classList.contains('is-transport')) {
                renderTransportValue(item, fact);
            } else {
                fact.appendChild(element('strong', 'answer-fact-value', item.value));
            }
            const detailValue = isTimeline ? cleanTimelineDetail(item.detail) : item.detail;
            if (detailValue) {
                const detail = element('span', 'answer-fact-detail', detailValue);
                if (isOverview && fact.classList.contains('is-transport')) {
                    detail.classList.add('answer-transport-note');
                }
                if (isTimeline) {
                    const detailShell = element('div', 'answer-fact-detail-shell');
                    detailShell.appendChild(detail);
                    fact.appendChild(detailShell);
                } else {
                    fact.appendChild(detail);
                }
            }
            grid.appendChild(fact);
        });
        content.appendChild(grid);
    }

    function renderWeather(section, content) {
        if (!Array.isArray(section.days) || !section.days.length) return;
        const days = element('div', 'answer-weather-days');
        (Array.isArray(section.items) ? section.items : []).forEach((item) => {
            const card = element('div', 'answer-weather-day is-current');
            card.appendChild(element('span', 'answer-weather-date', item.label));
            card.appendChild(element('strong', 'answer-weather-condition', item.value || '—'));
            if (item.detail) card.appendChild(element('span', 'answer-weather-temperature', item.detail));
            days.appendChild(card);
        });
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

    function cleanInlineMarkdown(value) {
        return String(value || '')
            .replace(/^#{1,6}\s+/, '')
            .replace(/\*\*(.*?)\*\*/g, '$1')
            .replace(/__(.*?)__/g, '$1')
            .replace(/`([^`]+)`/g, '$1')
            .trim();
    }

    function renderBody(value) {
        const body = element('div', 'answer-section-body');
        let list = null;
        String(value || '').split(/\n+/).forEach((rawLine) => {
            const line = rawLine.trim();
            if (!line) return;
            const bullet = line.match(/^[-*•]\s+(.+)/);
            if (bullet) {
                if (!list) {
                    list = element('ul', 'answer-section-body-list');
                    body.appendChild(list);
                }
                list.appendChild(element('li', '', cleanInlineMarkdown(bullet[1])));
                return;
            }
            list = null;
            const heading = /^#{1,6}\s+/.test(line) || /^\*\*.*\*\*[:：]?$/.test(line);
            body.appendChild(element('p', heading ? 'is-heading' : '', cleanInlineMarkdown(line)));
        });
        return body;
    }

    function disclosure(block, collapsedLabel, expandedLabel) {
        const button = element('button', 'answer-section-toggle');
        const label = element('span', 'answer-section-toggle-label', collapsedLabel);
        const icon = element('span', 'answer-section-toggle-icon');
        icon.setAttribute('aria-hidden', 'true');
        button.appendChild(label);
        button.appendChild(icon);
        button.type = 'button';
        button.setAttribute('aria-expanded', 'false');
        button.addEventListener('click', () => {
            const body = block.classList.contains('has-collapsible-body')
                ? block.querySelector('.answer-section-body')
                : null;
            if (body) {
                body.style.setProperty('--answer-body-expanded-height', `${body.scrollHeight}px`);
            }
            const expanded = block.classList.toggle('is-expanded');
            button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
            label.textContent = expanded ? expandedLabel : collapsedLabel;
        });
        return button;
    }

    function renderSection(section, index, documentTitle) {
        const block = element('section', `answer-section answer-section-${section.kind || 'general'}`);
        if (section.status === 'error') block.classList.add('is-error');
        if (section.kind === 'trip' && index === 0) block.classList.add('is-overview');
        if (section.kind === 'trip' && /^第\s*\d+\s*天/.test(section.title || '')) block.classList.add('is-timeline');
        const sectionTitle = String(section.title || '').trim();
        if (sectionTitle && sectionTitle !== String(documentTitle || '').trim()) {
            const heading = element('div', 'answer-section-heading');
            heading.appendChild(element('span', 'answer-section-mark'));
            heading.appendChild(element('h3', '', section.title));
            block.appendChild(heading);
        }
        const content = element('div', 'answer-section-content');
        const longBody = String(section.body || '').length > 220;
        const timelineDetails = section.kind === 'trip'
            && /^第\s*\d+\s*天/.test(section.title || '')
            && (section.items || []).some((item) => cleanTimelineDetail(item?.detail));
        const overviewDetails = block.classList.contains('is-overview')
            && (section.items || []).some((item) => String(item?.detail || '').length > 100);
        if (longBody || timelineDetails || overviewDetails) block.classList.add('is-collapsible');
        if (longBody) block.classList.add('has-collapsible-body');
        if (section.body) content.appendChild(renderBody(section.body));
        if (section.kind === 'weather' && Array.isArray(section.days) && section.days.length) {
            renderWeather(section, content);
        } else {
            renderItems(
                section,
                content,
                block.classList.contains('is-overview'),
                block.classList.contains('is-timeline')
            );
            renderWeather(section, content);
        }
        if (longBody) {
            content.appendChild(disclosure(block, '展开完整内容', '收起详细内容'));
        } else if (timelineDetails) {
            content.appendChild(disclosure(block, '查看行程细节', '收起行程细节'));
        } else if (overviewDetails) {
            content.appendChild(disclosure(block, '查看方案说明', '收起方案说明'));
        }
        block.appendChild(content);
        return block;
    }

    function renderNotices(values) {
        const notices = element('div', 'answer-notices');
        values.slice(0, 2).forEach((notice) => notices.appendChild(element('p', '', notice)));
        if (values.length > 2) {
            const more = element('details', 'answer-notice-details');
            more.appendChild(element('summary', '', `查看其余 ${values.length - 2} 项提醒`));
            values.slice(2).forEach((notice) => more.appendChild(element('p', '', notice)));
            notices.appendChild(more);
        }
        return notices;
    }

    function renderDetails(label, body) {
        const panel = element('div', 'answer-details');
        const trigger = element('button', 'answer-details-trigger');
        trigger.type = 'button';
        trigger.setAttribute('aria-expanded', 'false');
        trigger.appendChild(element('span', '', label));
        const icon = element('span', 'answer-details-icon');
        icon.setAttribute('aria-hidden', 'true');
        trigger.appendChild(icon);

        const content = element('div', 'answer-details-content');
        const inner = element('div', 'answer-details-content-inner');
        inner.appendChild(body);
        content.appendChild(inner);
        trigger.addEventListener('click', () => {
            const expanded = panel.classList.toggle('is-expanded');
            trigger.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        });
        panel.appendChild(trigger);
        panel.appendChild(content);
        return panel;
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
        const normalized = normalizeMachinePlaceholders(documentData);
        const timeline = upgradeLegacyTripTimeline(normalized);
        const data = localizeWeatherPresentation(upgradeLegacyPreDeparture(timeline));
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
        (data.sections || []).forEach((section, index) => {
            sections.appendChild(renderSection(section, index, data.title));
        });
        card.appendChild(sections);

        if (data.pre_departure) card.appendChild(renderPreDeparture(data.pre_departure));

        if (Array.isArray(data.notices) && data.notices.length) {
            card.appendChild(renderNotices(data.notices));
        }

        const footer = element('footer', 'answer-card-footer');
        if (data.plain_text) footer.appendChild(renderDetails('查看文字版', element('pre', 'answer-plain-text', data.plain_text)));
        if (Array.isArray(data.sources) && data.sources.length) {
            footer.appendChild(renderDetails(`来源与更新时间 · ${data.sources.length}`, renderSources(data.sources)));
        }
        if (footer.childElementCount) card.appendChild(footer);
        return card;
    }

    window.HommeyAnswerCard = { create, localizeWeatherCondition };
})();
