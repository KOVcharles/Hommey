/* 天气卡片改版 demo · 堆叠长条。
   renderWeatherPanel() / setupDeck() 是准备替换 answer-card.js:293 renderDepartureWeather() 的实现。 */

const SVG_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">';

const GLYPHS = {
    sun: SVG_OPEN + '<circle cx="12" cy="12" r="3.9"/><path d="M12 3.4v2.2M12 18.4v2.2M3.4 12h2.2M18.4 12h2.2M5.9 5.9l1.6 1.6M16.5 16.5l1.6 1.6M18.1 5.9l-1.6 1.6M7.5 16.5l-1.6 1.6"/></svg>',
    cloud: SVG_OPEN + '<path d="M7.6 18.4h9a4.1 4.1 0 0 0 .5-8.2 5.5 5.5 0 0 0-10.5 1.2 3.5 3.5 0 0 0 1 7Z"/></svg>',
    rain: SVG_OPEN + '<path d="M7.6 15.3h9a4.1 4.1 0 0 0 .5-8.2 5.5 5.5 0 0 0-10.5 1.2 3.5 3.5 0 0 0 1 7Z"/><path d="M9.5 18.1l-.8 2.3M13 18.1l-.8 2.3M16.5 18.1l-.8 2.3"/></svg>',
    snow: SVG_OPEN + '<path d="M7.6 15.3h9a4.1 4.1 0 0 0 .5-8.2 5.5 5.5 0 0 0-10.5 1.2 3.5 3.5 0 0 0 1 7Z"/><path d="M9.2 18.6h.01M12.6 18.6h.01M16 18.6h.01M10.9 21h.01M14.3 21h.01"/></svg>',
};

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const STACK_GAP = 6; // 展开时相邻长条的间距，要和 CSS 里的 margin-top 一致。

/* 「多云转晴」取转折后的那段，图标才跟得上当天的主导天气。 */
function weatherFamily(condition) {
    const text = String(condition || '').trim();
    const tail = text.includes('转') ? text.split('转').pop() : text;
    if (/雷|雨/.test(tail)) return 'rain';
    if (/雪|冰/.test(tail)) return 'snow';
    if (/晴/.test(tail)) return 'sun';
    return 'cloud';
}

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
}

function glyphNode(family) {
    const node = element('span', 'strip-glyph');
    node.setAttribute('aria-hidden', 'true');
    node.innerHTML = GLYPHS[family];
    return node;
}

/* 相对日期只给头三天，再往后报星期。 */
function dayLabel(date, index) {
    if (index === 0) return '今天';
    if (index === 1) return '明天';
    if (index === 2) return '后天';
    return WEEKDAYS[date.getDay()];
}

function shiftDate(base, offset) {
    const date = new Date(base.getTime());
    date.setDate(date.getDate() + offset);
    return date;
}

function renderSpine(day, index, date) {
    const spine = element('div', 'departure-weather-spine');
    const family = weatherFamily(day.condition);
    spine.appendChild(glyphNode(family));
    spine.appendChild(element('span', 'strip-when', dayLabel(date, index)));
    spine.appendChild(element('span', 'strip-date', `${date.getMonth() + 1}/${date.getDate()}`));
    spine.appendChild(element('span', 'strip-condition', day.condition));
    spine.appendChild(element('span', 'strip-temp', day.temperature));
    return spine;
}

/* 未来几天的详情来自 amap 的 forecasts（昼夜分别是两个字段），不是 LLM 写的建议。 */
function renderDetail(day) {
    const detail = element('div', 'departure-weather-detail');
    const parts = [];
    if (day.night) parts.push(`夜间 ${day.night} ${day.low}°C`);
    if (day.high !== undefined) parts.splice(0, 0, `白天 ${day.day || day.condition} ${day.high}°C`);
    if (day.wind) parts.push(`${day.wind}${day.power ? `${day.power} 级` : ''}`);
    parts.forEach((value) => detail.appendChild(element('span', '', value)));
    return detail;
}

function renderWeatherPanel(weather) {
    const today = weather.days[0];
    const family = weatherFamily(today.condition);
    const panel = element('section', `departure-weather-panel is-${family}`);
    const base = new Date();
    base.setHours(0, 0, 0, 0);

    const head = element('div', 'departure-weather-head');
    head.appendChild(element('h4', 'departure-weather-heading', '天气与随行准备'));
    const toggle = element('button', 'departure-weather-toggle');
    toggle.type = 'button';
    const toggleLabel = element('span', '', '');
    toggle.appendChild(toggleLabel);
    toggle.appendChild(element('i', 'departure-weather-toggle-icon'));
    head.appendChild(toggle);
    panel.appendChild(head);

    const deck = element('div', 'departure-weather-deck');
    panel.appendChild(deck);

    const todayStrip = element('div', 'departure-weather-strip is-today');
    todayStrip.appendChild(renderSpine(today, 0, base));
    const body = element('div', 'departure-weather-today-body');
    if (weather.humidity) {
        const meta = element('div', 'departure-weather-meta');
        meta.appendChild(element('span', '', weather.humidity));
        body.appendChild(meta);
    }
    if (Array.isArray(weather.preparation) && weather.preparation.length) {
        const tags = element('div', 'departure-preparation-tags');
        weather.preparation.forEach((value) => tags.appendChild(element('span', '', value)));
        body.appendChild(tags);
    }
    if (weather.advice) body.appendChild(renderAdvice(weather.advice));
    todayStrip.appendChild(body);
    deck.appendChild(todayStrip);

    const upcoming = weather.days.slice(1);
    const stack = element('div', 'departure-weather-stack');
    upcoming.forEach((day, offset) => {
        const index = offset + 1;
        const strip = element('div', 'departure-weather-strip');
        strip.style.setProperty('--depth', String(offset));
        strip.appendChild(renderSpine(day, index, shiftDate(base, index)));
        strip.appendChild(renderDetail(day));
        stack.appendChild(strip);
    });
    if (upcoming.length) {
        deck.appendChild(stack);
    } else {
        toggle.hidden = true;
    }
    // 折叠要量真实高度，必须等面板进了文档再挂，所以把 setup 交给调用方。
    return {
        panel,
        setup: () => {
            if (upcoming.length) setupDeck(panel, stack, toggle, toggleLabel, upcoming.length);
        },
    };
}

function setupDeck(panel, stack, toggle, toggleLabel, count) {
    const strips = [...stack.querySelectorAll('.departure-weather-strip')];
    let folded = false; // 和初始 DOM 一致（还没有 is-folded），下面量完再折起来
    panel.style.setProperty('--count', String(count));

    // 收起时要正好露到脊线为止，所以 --peek 用脊线自身的高度。
    // 就算条被 max-height 裁着，脊线的 offsetHeight 仍是完整高度；
    // 整条的真实高度则用 scrollHeight 拿（max-height 不影响 scrollHeight）。
    function measure() {
        panel.style.setProperty('--peek', `${strips[0].querySelector('.departure-weather-spine').offsetHeight}px`);
        panel.style.setProperty('--strip-h', `${strips[0].scrollHeight}px`);
    }

    function setFolded(next) {
        if (next === folded) return;
        folded = next;
        panel.classList.toggle('is-folded', folded);
        toggle.setAttribute('aria-expanded', String(!folded));
        toggleLabel.textContent = folded ? `未来 ${count} 天` : '收起';
    }

    window.addEventListener('resize', measure);
    measure();
    setFolded(true); // 量完再折，首屏就是一次自然的收拢
    toggle.addEventListener('click', () => setFolded(!folded));
    stack.addEventListener('click', () => { if (folded) setFolded(false); });
}

/* 建议默认收在两行，没被截断就不给展开入口。 */
function renderAdvice(advice) {
    const block = element('div', 'departure-weather-advice');
    const head = element('div', 'departure-weather-advice-head');
    head.appendChild(element('span', 'departure-weather-advice-label', '建议'));

    const toggle = element('button', 'departure-weather-more');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.appendChild(element('span', '', '展开'));
    toggle.appendChild(element('i', 'departure-weather-more-icon'));
    head.appendChild(toggle);
    block.appendChild(head);

    const text = element('p', 'departure-weather-advice-text is-clamped', advice);
    block.appendChild(text);

    toggle.addEventListener('click', () => {
        const expanded = toggle.getAttribute('aria-expanded') === 'true';
        toggle.setAttribute('aria-expanded', String(!expanded));
        toggle.firstChild.textContent = expanded ? '展开' : '收起';
        text.classList.toggle('is-clamped', expanded);
    });

    // 是否真的被截断，要按折叠态量——展开后 scrollHeight 等于 clientHeight，量不出结果。
    requestAnimationFrame(() => {
        const expanded = !text.classList.contains('is-clamped');
        if (expanded) text.classList.add('is-clamped');
        const clipped = text.scrollHeight > text.clientHeight + 1;
        if (expanded) text.classList.remove('is-clamped');
        toggle.hidden = !clipped;
    });
    return block;
}

const SAMPLES = [
    {
        label: '晴热 · 4 天',
        weather: {
            humidity: '湿度 46%',
            preparation: ['防晒用品', '补水防暑', '透气衣物'],
            advice: '重庆午后体感可达 41°C，建议把客户拜访安排在 10:00 前或 16:00 后，随身带水。会场到酒店步行约 8 分钟，全程无遮挡。',
            days: [
                { condition: '晴', temperature: '31–38°C', high: 38, low: 31, night: '晴' },
                { condition: '多云转雷阵雨', day: '多云', temperature: '18–26°C', high: 26, low: 18, night: '雷阵雨', wind: '东南风', power: '3' },
                { condition: '阴', day: '阴', temperature: '9–14°C', high: 14, low: 9, night: '阴', wind: '北风', power: '2' },
                { condition: '晴', temperature: '12–21°C', high: 21, low: 12, night: '晴', wind: '西南风', power: '2' },
            ],
        },
    },
    {
        label: '雨天 · 3 天',
        weather: {
            humidity: '湿度 82%',
            preparation: ['雨具', '透气衣物'],
            advice: '午后有雷阵雨，建议 14:00 前抵达会场，并为从高铁站到酒店的 3 公里预留 20 分钟机动时间。',
            days: [
                { condition: '多云转雷阵雨', day: '多云', temperature: '18–26°C', high: 26, low: 18, night: '雷阵雨', wind: '东南风', power: '3' },
                { condition: '中雨', day: '中雨', temperature: '16–21°C', high: 21, low: 16, night: '小雨', wind: '东风', power: '4' },
                { condition: '阴', day: '阴', temperature: '15–22°C', high: 22, low: 15, night: '多云', wind: '东北风', power: '2' },
            ],
        },
    },
    {
        label: '阴冷 · 2 天 · 短建议',
        weather: {
            humidity: '湿度 63%',
            preparation: ['保暖外套'],
            advice: '早晚温差 5°C，备一件外套即可。',
            days: [
                { condition: '阴', temperature: '9–14°C', high: 14, low: 9, night: '阴', wind: '北风', power: '2' },
                { condition: '多云', temperature: '7–16°C', high: 16, low: 7, night: '晴', wind: '西北风', power: '2' },
            ],
        },
    },
];

/* 支持 ?theme=dark&expand=1，方便直接抓某一状态的截图。
   主题要在渲染前定下来——折叠高度是按布局量的，先上主题再量才准。 */
const params = new URLSearchParams(location.search);
const toggleButton = document.getElementById('themeToggle');

function applyTheme(dark) {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    toggleButton.textContent = dark ? '浅色' : '深色';
}
applyTheme(params.get('theme') === 'dark');
toggleButton.addEventListener('click', () => {
    applyTheme(document.documentElement.getAttribute('data-theme') !== 'dark');
});

const mount = document.getElementById('weatherMount');
SAMPLES.forEach((sample) => {
    const frame = element('div', 'wd-frame');
    const label = element('div', 'wd-frame-label');
    label.appendChild(element('span', '', sample.label));
    label.appendChild(element('i'));
    frame.appendChild(label);
    const { panel, setup } = renderWeatherPanel(sample.weather);
    frame.appendChild(panel);
    mount.appendChild(frame);
    setup();
});

if (params.get('expand') === '1') {
    document.querySelectorAll('.departure-weather-toggle:not([hidden])').forEach((button) => button.click());
    document.querySelectorAll('.departure-weather-more:not([hidden])').forEach((button) => button.click());
}
