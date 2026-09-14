(function () {
    'use strict';

    let nextCardId = 0;
    let nextDisclosureId = 0;

    // Built-in city list for departure/destination selection (short names, no 市 suffix).
    const CITY_LIST = [
        '北京', '上海', '天津', '重庆',
        '石家庄', '唐山', '秦皇岛', '邯郸', '邢台', '保定', '张家口', '承德', '沧州', '廊坊', '衡水',
        '太原', '大同', '阳泉', '长治', '晋城', '朔州', '晋中', '运城', '忻州', '临汾', '吕梁',
        '呼和浩特', '包头', '乌海', '赤峰', '通辽', '鄂尔多斯', '呼伦贝尔', '巴彦淖尔', '乌兰察布',
        '沈阳', '大连', '鞍山', '抚顺', '本溪', '丹东', '锦州', '营口', '阜新', '辽阳', '盘锦', '铁岭', '朝阳', '葫芦岛',
        '长春', '吉林', '四平', '辽源', '通化', '白山', '松原', '白城', '延边',
        '哈尔滨', '齐齐哈尔', '鸡西', '鹤岗', '双鸭山', '大庆', '伊春', '佳木斯', '七台河', '牡丹江', '黑河', '绥化',
        '南京', '无锡', '徐州', '常州', '苏州', '南通', '连云港', '淮安', '盐城', '扬州', '镇江', '泰州', '宿迁',
        '杭州', '宁波', '温州', '嘉兴', '湖州', '绍兴', '金华', '衢州', '舟山', '台州', '丽水',
        '合肥', '芜湖', '蚌埠', '淮南', '马鞍山', '淮北', '铜陵', '安庆', '黄山', '滁州', '阜阳', '宿州', '六安', '亳州', '池州', '宣城',
        '福州', '厦门', '莆田', '三明', '泉州', '漳州', '南平', '龙岩', '宁德',
        '南昌', '景德镇', '萍乡', '九江', '新余', '鹰潭', '赣州', '吉安', '宜春', '抚州', '上饶',
        '济南', '青岛', '淄博', '枣庄', '东营', '烟台', '潍坊', '济宁', '泰安', '威海', '日照', '临沂', '德州', '聊城', '滨州', '菏泽',
        '郑州', '开封', '洛阳', '平顶山', '安阳', '鹤壁', '新乡', '焦作', '濮阳', '许昌', '漯河', '三门峡', '南阳', '商丘', '信阳', '周口', '驻马店', '济源',
        '武汉', '黄石', '十堰', '宜昌', '襄阳', '鄂州', '荆门', '孝感', '荆州', '黄冈', '咸宁', '随州', '恩施',
        '长沙', '株洲', '湘潭', '衡阳', '邵阳', '岳阳', '常德', '张家界', '益阳', '郴州', '永州', '怀化', '娄底', '湘西',
        '广州', '深圳', '珠海', '汕头', '佛山', '韶关', '湛江', '肇庆', '江门', '茂名', '惠州', '梅州', '汕尾', '河源', '阳江', '清远', '东莞', '中山', '潮州', '揭阳', '云浮',
        '南宁', '柳州', '桂林', '梧州', '北海', '防城港', '钦州', '贵港', '玉林', '百色', '贺州', '河池', '来宾', '崇左',
        '海口', '三亚', '儋州',
        '成都', '自贡', '攀枝花', '泸州', '德阳', '绵阳', '广元', '遂宁', '内江', '乐山', '南充', '眉山', '宜宾', '广安', '达州', '雅安', '巴中', '资阳',
        '贵阳', '六盘水', '遵义', '安顺', '毕节', '铜仁',
        '昆明', '曲靖', '玉溪', '保山', '昭通', '丽江', '普洱', '临沧', '楚雄', '红河', '文山', '西双版纳', '大理', '德宏', '怒江', '迪庆',
        '拉萨', '日喀则', '昌都', '林芝', '山南', '那曲',
        '西安', '铜川', '宝鸡', '咸阳', '渭南', '延安', '汉中', '榆林', '安康', '商洛',
        '兰州', '嘉峪关', '金昌', '白银', '天水', '武威', '张掖', '平凉', '酒泉', '庆阳', '定西', '陇南', '临夏', '甘南',
        '西宁', '海东', '海西', '海北', '黄南', '果洛', '玉树',
        '银川', '石嘴山', '吴忠', '固原', '中卫',
        '乌鲁木齐', '克拉玛依', '吐鲁番', '哈密', '昌吉', '博尔塔拉', '巴音郭楞', '阿克苏', '克孜勒苏', '喀什', '和田', '伊犁', '塔城', '阿勒泰',
        '香港', '澳门', '台北', '高雄', '台中', '台南', '新北', '桃园', '新竹', '基隆', '嘉义',
    ];

    // Opened dropdowns must offer something to click; an empty hint list dead-ends the user.
    const POPULAR_CITIES = ['北京', '上海', '广州', '深圳', '杭州', '成都', '南京', '武汉'];

    function normalizeCity(name) {
        return String(name || '').replace(/\s+/g, '').replace(/(市|地区|自治州|特别行政区)$/g, '');
    }

    // Calendar arithmetic uses UTC; the business date always follows Beijing time.
    function todayDate() {
        return new Intl.DateTimeFormat('en-CA', {
            timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
        }).format(new Date());
    }

    function dateObject(value) { return new Date(`${value}T00:00:00Z`); }
    function dateString(value) { return value.toISOString().slice(0, 10); }
    function addDays(value, days) {
        const date = dateObject(value);
        date.setUTCDate(date.getUTCDate() + days);
        return dateString(date);
    }
    function dateError(value) {
        if (!value) return '';
        const parsed = dateObject(value);
        if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(parsed.getTime()) || dateString(parsed) !== value) {
            return '请选择有效的出发日期';
        }
        return value < todayDate() ? '出发日期已过期，请选择今天或之后的日期' : '';
    }

    function validateDates(state) {
        state.dateKeys.forEach((key) => {
            (state.controls.get(key) || []).forEach((control) => {
                if (control.dataset.dateValue) control.disabled = !!dateError(control.dataset.dateValue);
            });
            const error = dateError(state.values.get(key));
            if (error) {
                state.values.delete(key);
                state.errors.set(key, error);
            }
        });
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function registerControl(state, key, control) {
        if (!state.controls.has(key)) state.controls.set(key, []);
        state.controls.get(key).push(control);
    }

    function updateSelectionState(state) {
        if (state.archived) return;
        validateDates(state);
        // 有值不等于可用：出发地与目的地相同的选择会被记下来，但不计入完成度，
        // 否则提交按钮会在冲突还没解决时就亮起来。
        const completed = [...state.requiredKeys].filter(
            (key) => String(state.values.get(key) || '').trim() && !state.errors.has(key),
        ).length;
        if (state.status) {
            state.status.textContent = state.requiredKeys.size
                ? `已填写 ${completed} / ${state.requiredKeys.size} 项`
                : '可直接提交';
        }
        if (state.submit) state.submit.disabled = state.submitting || completed < state.requiredKeys.size;
        state.steps.forEach((step, key) => {
            const value = state.values.get(key) || '';
            const filled = !!String(value).trim();
            // 字段填了也可能不可用（例如出发地和目的地相同），错误优先于“已填写”。
            const error = state.errors.get(key) || (filled ? '' : step.field.error);
            step.row.classList.toggle('is-complete', filled && !error);
            step.row.classList.toggle('is-invalid', !!error);
            step.summary.textContent = value || '点击补充';
            step.summary.title = value;
            step.badge.textContent = error ? '待确认' : (filled ? '已填写' : '待填写');
            step.index.textContent = filled && !error ? '✓' : step.number;
            step.next.disabled = state.submitting || !filled || !!error;
            step.help.textContent = error || (state.dateKeys.has(key) ? '选择出发日期，仅可选择今天及之后（北京时间）' : step.field.help_text) || '';
            step.help.classList.toggle('is-error', !!error);
            step.input.setAttribute('aria-invalid', error ? 'true' : 'false');
        });
        // 冲突块一旦被改好就该消失，不能让它停在那里继续指责一个已经不存在的值。
        state.conflictBlocks?.forEach((block, key) => { block.hidden = !state.errors.has(key); });
        state.datePickers.forEach((refresh) => refresh());
        if (state.stepStatus) {
            const filled = [...state.steps.keys()].filter((key) => state.values.has(key)).length;
            state.stepStatus.textContent = `${filled} / ${state.steps.size} 项已填`;
        }
    }

    function openStep(state, key, focus = false) {
        if (state.submitting || state.archived) return;
        updateSelectionState(state);
        state.activeKey = key;
        state.steps.forEach((step, stepKey) => {
            const expanded = stepKey === key;
            step.row.classList.toggle('is-expanded', expanded);
            step.trigger.setAttribute('aria-expanded', String(expanded));
            step.panel.setAttribute('aria-hidden', String(!expanded));
            step.panel.inert = !expanded;
        });
        if (focus) state.steps.get(key)?.trigger.focus({ preventScroll: true });
    }

    function advanceStep(state, key) {
        updateSelectionState(state);
        if (!state.values.has(key) || state.errors.has(key)) return;
        const keys = [...state.steps.keys()];
        const current = keys.indexOf(key);
        const next = [...keys.slice(current + 1), ...keys.slice(0, current)]
            .find((candidate) => !state.values.has(candidate));
        openStep(state, next || null, !!next);
        if (!next) {
            const unresolved = [...state.requiredKeys].find((candidate) => !state.values.has(candidate));
            const target = state.controls.get(unresolved)?.[0] || state.submit;
            target?.focus({ preventScroll: true });
        }
    }

    function selectValue(state, key, value, activeControl) {
        if (state.submitting || state.archived) return;
        const clean = String(value || '').trim();
        const error = state.dateKeys.has(key) ? dateError(clean) : '';
        if (error) {
            state.values.delete(key);
            state.errors.set(key, error);
            updateSelectionState(state);
            return;
        }
        state.errors.delete(key);
        (state.controls.get(key) || []).forEach((control) => {
            if (control.tagName === 'BUTTON') {
                const selected = control === activeControl && !!clean;
                control.classList.toggle('is-selected', selected);
                control.setAttribute('aria-pressed', selected ? 'true' : 'false');
            } else if (control !== activeControl && activeControl?.tagName === 'BUTTON') {
                control.value = '';
            }
        });
        if (activeControl?.tagName !== 'BUTTON') {
            (state.controls.get(key) || []).forEach((control) => {
                if (control.tagName === 'BUTTON') {
                    control.classList.remove('is-selected');
                    control.setAttribute('aria-pressed', 'false');
                }
            });
        }
        if (clean) state.values.set(key, clean);
        else state.values.delete(key);
        if (key === 'destination') state.placePicker?.resetCity();
        updateSelectionState(state);
    }

    function optionAction(label, key, value, state) {
        const button = element('button', 'trip-intake-action', label);
        button.type = 'button';
        button.setAttribute('aria-pressed', 'false');
        if (state.dateKeys.has(key)) {
            button.dataset.dateValue = value;
            button.disabled = !!dateError(value);
        }
        registerControl(state, key, button);
        button.addEventListener('click', () => selectValue(state, key, value, button));
        return button;
    }

    function inlineInput(field, state, optional) {
        if (field.input_type === 'date') return datePicker(field, state);
        const input = element('input', 'trip-intake-inline-input');
        input.type = 'text';
        input.autocomplete = 'off';
        input.placeholder = optional
            ? (field.help_text || '可选填写')
            : (field.options?.length ? '或自行填写' : (field.examples?.[0] || field.help_text || '请填写'));
        input.setAttribute('aria-label', field.label);
        registerControl(state, field.key, input);
        input.addEventListener('input', () => selectValue(state, field.key, input.value, input));
        return input;
    }

    // 另一座城市当前已知的值：本卡片刚选的优先，其次是后端已确认的事实。仍在等待
    // 填写的字段不算已知，否则会拿空值去比较，把任何选择都判成不冲突。
    function knownCity(state, key) {
        if (state.values.has(key)) return state.values.get(key);
        if (state.steps.has(key)) return '';
        const data = state.document || {};
        return data.trip_input?.[key] || data.route?.[key] || '';
    }

    // 出发地和目的地是同一座城市时这一程没有意义，必须在选中的当下就讲清楚。
    function cityConflict(state, key, city) {
        const other = knownCity(state, key === 'origin' ? 'destination' : 'origin');
        if (!other || normalizeCity(city) !== normalizeCity(other)) return '';
        const name = normalizeCity(city);
        return key === 'destination'
            ? `目的地与出发地都是${name}，请选择本次出差的实际目的地。`
            : `出发地与目的地都是${name}，请选择实际的出发城市。`;
    }

    function cityPicker(field, state, initialValue, emptyHints, onChange, validate) {
        const wrap = element('div', 'trip-intake-city');
        const input = element('input', 'trip-intake-city-input');
        input.type = 'text';
        input.autocomplete = 'off';
        input.spellcheck = false;
        input.placeholder = field.key === 'origin' ? '搜索或选择出发城市' : '搜索或选择目的地城市';
        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-label', field.label);
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', 'false');

        // Collapse via grid-template-rows so opening and closing read as motion,
        // matching the step panels and disclosures instead of snapping into place.
        const popover = element('div', 'trip-intake-city-popover');
        const inner = element('div', 'trip-intake-city-popover-inner');
        const list = element('div', 'trip-intake-city-list');
        list.setAttribute('role', 'listbox');
        inner.appendChild(list);
        popover.appendChild(inner);
        popover.inert = true;

        let selected = '';
        let options = [];
        let activeIndex = -1;

        function setActive(index) {
            activeIndex = index;
            options.forEach((option, position) => {
                const active = position === index;
                option.classList.toggle('is-active', active);
                option.setAttribute('aria-selected', active ? 'true' : 'false');
            });
        }

        function closeList() {
            wrap.classList.remove('is-open');
            popover.inert = true;
            input.setAttribute('aria-expanded', 'false');
            setActive(-1);
        }

        function apply(city) {
            selected = city;
            input.value = city;
            state.values.set(field.key, city);
            // 跨字段冲突在选中的当下就标出来。值仍然保留，用户看得见自己选了什么，
            // 而不是等到整张表填完、提交后才被后端退回一次。
            const error = validate ? validate(city) : '';
            if (error) state.errors.set(field.key, error);
            else state.errors.delete(field.key);
            closeList();
            updateSelectionState(state);
            if (onChange) onChange(city);
        }

        function appendHighlighted(option, city, query) {
            const at = query ? normalizeCity(city).indexOf(query) : -1;
            if (at < 0) {
                option.textContent = city;
                return;
            }
            if (at) option.appendChild(document.createTextNode(city.slice(0, at)));
            option.appendChild(element('mark', 'trip-intake-city-match', city.slice(at, at + query.length)));
            option.appendChild(document.createTextNode(city.slice(at + query.length)));
        }

        function renderOptions(query) {
            const q = normalizeCity(query);
            // 输入框里还停着上次选定的城市时，用户是“打开列表挑一个”，不是在搜索：
            // 该给他一份能挑的推荐，而不是把列表过滤成已经选中的那一条。
            const browsing = !q || q === normalizeCity(selected);
            const matches = (browsing ? emptyHints || [] : CITY_LIST.filter((city) => normalizeCity(city).includes(q))).slice(0, 12);
            list.replaceChildren();
            options = [];
            setActive(-1);
            if (!matches.length) {
                list.appendChild(element('div', 'trip-intake-city-empty',
                    browsing ? '输入城市名称进行筛选' : '没有匹配的城市，请换个关键词'));
            } else {
                matches.forEach((city) => {
                    const option = element('button', 'trip-intake-city-option');
                    option.type = 'button';
                    option.setAttribute('role', 'option');
                    option.setAttribute('aria-selected', 'false');
                    option.dataset.city = city;
                    appendHighlighted(option, city, q);
                    option.addEventListener('mousedown', (event) => event.preventDefault());
                    option.addEventListener('click', () => apply(city));
                    list.appendChild(option);
                    options.push(option);
                });
            }
            wrap.classList.add('is-open');
            popover.inert = false;
            input.setAttribute('aria-expanded', 'true');
        }

        input.addEventListener('focus', () => renderOptions(input.value));
        // 选定之后输入框会保持聚焦，这时再点它不会再触发 focus，列表就打不开了；
        // 用户明明想换一个城市，点上去却毫无反应。点击也要能展开列表。
        input.addEventListener('click', () => {
            if (!wrap.classList.contains('is-open')) renderOptions(input.value);
        });
        input.addEventListener('input', () => {
            if (input.value.trim() !== selected) {
                selected = '';
                state.values.delete(field.key);
                state.errors.delete(field.key);
            }
            renderOptions(input.value);
            updateSelectionState(state);
        });
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') { closeList(); return; }
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                if (!wrap.classList.contains('is-open')) { renderOptions(input.value); return; }
                if (!options.length) return;
                event.preventDefault();
                const step = event.key === 'ArrowDown' ? 1 : -1;
                setActive((activeIndex + step + options.length) % options.length);
                return;
            }
            if (event.key === 'Enter') {
                event.preventDefault();
                const active = options[activeIndex];
                if (wrap.classList.contains('is-open') && active) apply(active.dataset.city);
                else closeList();
            }
        });
        input.addEventListener('focusout', (event) => {
            if (!wrap.contains(event.relatedTarget)) closeList();
        });

        registerControl(state, field.key, input);

        if (initialValue) {
            selected = initialValue;
            input.value = initialValue;
            state.values.set(field.key, initialValue);
        }

        wrap.append(input, popover);
        return { input, node: wrap };
    }

    function datePicker(field, state) {
        state.dateKeys.add(field.key);
        const wrap = element('div', 'trip-intake-date-picker');
        wrap.setAttribute('role', 'group');
        wrap.setAttribute('aria-label', field.label);
        const shortcuts = element('div', 'trip-intake-date-shortcuts');
        const calendar = element('div', 'trip-intake-calendar');
        const header = element('div', 'trip-intake-calendar-header');
        const monthLabel = element('strong');
        monthLabel.id = `${state.id}-${field.key}-month`;
        monthLabel.setAttribute('aria-live', 'polite');
        const nav = element('div', 'trip-intake-calendar-nav');
        function navButton(label, text, action) {
            const button = element('button', '', text);
            button.type = 'button';
            button.setAttribute('aria-label', label);
            button.addEventListener('click', action);
            nav.appendChild(button);
            return button;
        }
        let month = todayDate().slice(0, 7);
        let focusDate = todayDate();
        function showMonth(offset) {
            const date = dateObject(`${month}-01`);
            date.setUTCMonth(date.getUTCMonth() + offset);
            month = dateString(date).slice(0, 7);
            refresh();
        }
        const previous = navButton('上个月', '‹', () => showMonth(-1));
        navButton('回到本月', '本月', () => { month = todayDate().slice(0, 7); refresh(); });
        navButton('下个月', '›', () => showMonth(1));
        header.append(monthLabel, nav);
        const weekdays = element('div', 'trip-intake-calendar-weekdays');
        weekdays.setAttribute('aria-hidden', 'true');
        ['一', '二', '三', '四', '五', '六', '日'].forEach((day) => weekdays.appendChild(element('span', '', day)));
        const days = element('div', 'trip-intake-calendar-days');
        days.setAttribute('role', 'group');
        days.setAttribute('aria-labelledby', monthLabel.id);
        const selection = element('div', 'trip-intake-date-selection');
        const selectedText = element('span');
        selectedText.setAttribute('role', 'status');
        const clear = element('button', '', '清除');
        clear.type = 'button';
        clear.setAttribute('aria-label', '清除出发日期');
        clear.addEventListener('click', () => selectValue(state, field.key, '', clear));
        selection.append(selectedText, clear);
        calendar.append(header, weekdays, days, selection);
        wrap.append(shortcuts, calendar);
        const quickButtons = ['今天', '明天', '下周一'].map((label, index) => {
            const button = element('button', 'trip-intake-action');
            button.type = 'button';
            button.addEventListener('click', () => {
                const today = todayDate();
                const offset = index < 2 ? index : 7 - ((dateObject(today).getUTCDay() + 6) % 7);
                const value = addDays(today, offset);
                month = value.slice(0, 7);
                focusDate = value;
                selectValue(state, field.key, value, button);
            });
            shortcuts.appendChild(button);
            return { button, label, index };
        });
        function refresh() {
            const today = todayDate();
            const currentMonth = today.slice(0, 7);
            if (month < currentMonth) month = currentMonth;
            const selected = state.values.get(field.key);
            monthLabel.textContent = `${Number(month.slice(0, 4))}年 ${Number(month.slice(5))}月`;
            previous.disabled = month <= currentMonth;
            quickButtons.forEach(({ button, label, index }) => {
                const offset = index < 2 ? index : 7 - ((dateObject(today).getUTCDay() + 6) % 7);
                const value = addDays(today, offset);
                button.textContent = `${label} · ${Number(value.slice(5, 7))}/${Number(value.slice(8))}`;
                button.classList.toggle('is-selected', value === selected);
                button.setAttribute('aria-pressed', String(value === selected));
            });
            // Preserve keyboard focus when a selection refreshes the day buttons.
            const focused = days.contains(document.activeElement) ? document.activeElement.dataset.date : null;
            days.replaceChildren();
            const first = `${month}-01`;
            const leading = (dateObject(first).getUTCDay() + 6) % 7;
            const count = new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5)), 0)).getUTCDate();
            const tabDate = [focusDate, selected, today, first].find((value) => value && value.startsWith(month) && value >= today);
            for (let i = 0; i < Math.ceil((leading + count) / 7) * 7; i++) {
                const value = addDays(first, i - leading);
                if (!value.startsWith(month)) {
                    const empty = element('span', 'trip-intake-calendar-empty');
                    empty.setAttribute('aria-hidden', 'true');
                    days.appendChild(empty);
                    continue;
                }
                const button = element('button', 'trip-intake-calendar-day', Number(value.slice(8)));
                button.type = 'button';
                button.dataset.date = value;
                button.disabled = value < today;
                button.tabIndex = value === tabDate ? 0 : -1;
                button.setAttribute('aria-label', `${value}${value === today ? '，今天' : ''}${button.disabled ? '，不可选' : ''}`);
                button.setAttribute('aria-pressed', String(value === selected));
                button.classList.toggle('is-selected', value === selected);
                button.classList.toggle('is-today', value === today);
                if (value === today) button.setAttribute('aria-current', 'date');
                button.addEventListener('click', () => {
                    focusDate = value;
                    selectValue(state, field.key, value, button);
                });
                button.addEventListener('keydown', (event) => {
                    const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
                    if (!(event.key in offsets)) return;
                    event.preventDefault();
                    focusDate = addDays(value, offsets[event.key]);
                    if (focusDate < todayDate()) focusDate = todayDate();
                    month = focusDate.slice(0, 7);
                    refresh();
                    days.querySelector(`[data-date="${focusDate}"]`)?.focus({ preventScroll: true });
                });
                days.appendChild(button);
            }
            if (focused) days.querySelector(`[data-date="${focused}"]`)?.focus({ preventScroll: true });
            selectedText.textContent = selected
                ? `${Number(selected.slice(5, 7))}月${Number(selected.slice(8))}日 · ${['周日', '周一', '周二', '周三', '周四', '周五', '周六'][dateObject(selected).getUTCDay()]}出发`
                : (state.errors.get(field.key) || '尚未选择出发日期');
            selection.classList.toggle('is-error', !!state.errors.get(field.key));
            clear.hidden = !selected;
        }
        state.datePickers.set(field.key, refresh);
        refresh();
        return wrap;
    }

    function renderRoute(route, state) {
        const row = element('div', 'trip-intake-route');
        const origin = element('strong', '', route.origin || '出发地待补充');
        row.appendChild(origin);
        const line = element('span', 'trip-intake-route-line');
        line.setAttribute('aria-hidden', 'true');
        row.appendChild(line);
        const destination = element('strong', '', route.destination || '目的地待补充');
        row.appendChild(destination);
        state.routeNodes = { origin, destination };
        return row;
    }

    // A value we already know is shown as settled, not re-asked; editing stays one click away.
    function collectedEditor(field, state) {
        const editor = element('div', 'trip-intake-collected-editor');
        const edit = element('button', 'trip-intake-collected-edit', '修改');
        edit.type = 'button';
        edit.setAttribute('aria-label', `修改${field.label}`);
        edit.setAttribute('aria-expanded', 'false');
        const collapse = () => {
            editor.classList.remove('is-open');
            edit.classList.remove('is-active');
            edit.setAttribute('aria-expanded', 'false');
        };
        const picker = cityPicker(
            { key: field.key, label: field.label },
            state,
            '',
            [...new Set([state.homeLocation, ...POPULAR_CITIES].filter(Boolean))],
            (city) => {
                if (state.routeNodes) state.routeNodes.origin.textContent = city;
                collapse();
            },
        );
        const inner = element('div', 'trip-intake-collected-editor-inner');
        inner.appendChild(picker.node);
        editor.appendChild(inner);
        edit.addEventListener('click', () => {
            const open = !editor.classList.contains('is-open');
            if (!open) { collapse(); return; }
            editor.classList.add('is-open');
            edit.classList.add('is-active');
            edit.setAttribute('aria-expanded', 'true');
            picker.input.focus({ preventScroll: true });
        });
        return { editor, edit };
    }

    function renderCollected(fields, state) {
        const wrap = element('div', 'trip-intake-collected');
        wrap.appendChild(element('span', 'trip-intake-collected-label', '已确认'));
        const list = element('div', 'trip-intake-collected-list');
        fields.forEach((field) => {
            const item = element('div', 'trip-intake-collected-item');
            item.appendChild(element('span', '', field.label));
            const value = element('strong', '', field.value);
            item.appendChild(value);
            list.appendChild(item);
            if (field.source !== 'memory') return;
            item.classList.add('is-from-memory');
            item.appendChild(element('span', 'trip-intake-collected-source', '常驻城市'));
            const { editor, edit } = collectedEditor(field, state);
            item.appendChild(edit);
            list.appendChild(editor);
        });
        wrap.appendChild(list);
        return wrap;
    }

    function renderDisclosure(className, label, body) {
        const panel = element('div', className);
        const trigger = element('button', 'trip-intake-disclosure-trigger');
        trigger.type = 'button';
        trigger.setAttribute('aria-expanded', 'false');
        trigger.appendChild(element('span', '', label));
        const icon = element('span', 'trip-intake-disclosure-icon');
        icon.setAttribute('aria-hidden', 'true');
        trigger.appendChild(icon);
        const content = element('div', 'trip-intake-disclosure-content');
        content.id = `trip-intake-disclosure-${++nextDisclosureId}`;
        content.inert = true;
        content.setAttribute('aria-hidden', 'true');
        trigger.setAttribute('aria-controls', content.id);
        const inner = element('div', 'trip-intake-disclosure-inner');
        inner.appendChild(body);
        content.appendChild(inner);
        trigger.addEventListener('click', () => {
            const expanded = panel.classList.toggle('is-expanded');
            trigger.setAttribute('aria-expanded', expanded ? 'true' : 'false');
            content.inert = !expanded;
            content.setAttribute('aria-hidden', String(!expanded));
        });
        panel.appendChild(trigger);
        panel.appendChild(content);
        return panel;
    }

    function renderPlainText(text) {
        return renderDisclosure(
            'trip-intake-text-details',
            '查看文字版',
            element('pre', 'trip-intake-plain-text', text),
        );
    }

    function renderPrompt(field, index, state) {
        const row = element('div', `trip-intake-field${field.error ? ' is-invalid' : ''}`);
        row.dataset.fieldKey = field.key;
        row.style.setProperty('--step-index', Math.min(index, 3));
        row.style.setProperty('--step-layer', state.stepCount - index);
        const heading = element('h4', 'trip-intake-step-heading');
        const trigger = element('button', 'trip-intake-step-trigger');
        trigger.type = 'button';
        trigger.id = `${state.id}-step-${index}`;
        trigger.setAttribute('aria-label', field.label);
        trigger.setAttribute('aria-expanded', 'false');
        const number = element('span', 'trip-intake-field-index', index + 1);
        number.setAttribute('aria-hidden', 'true');
        trigger.appendChild(number);
        const title = element('span', 'trip-intake-step-title');
        title.appendChild(element('span', 'trip-intake-field-label', field.label));
        const summary = element('span', 'trip-intake-step-summary', '点击补充');
        summary.id = `${state.id}-summary-${index}`;
        trigger.setAttribute('aria-describedby', summary.id);
        title.appendChild(summary);
        trigger.appendChild(title);
        const badge = element('span', 'trip-intake-step-badge', '待填写');
        trigger.appendChild(badge);
        const chevron = element('span', 'trip-intake-disclosure-icon');
        chevron.setAttribute('aria-hidden', 'true');
        trigger.appendChild(chevron);
        trigger.addEventListener('click', () => {
            openStep(state, state.activeKey === field.key ? null : field.key);
        });
        heading.appendChild(trigger);
        row.appendChild(heading);
        const panel = element('div', 'trip-intake-step-panel');
        panel.id = `${state.id}-panel-${index}`;
        panel.setAttribute('role', 'region');
        panel.setAttribute('aria-labelledby', trigger.id);
        panel.setAttribute('aria-hidden', 'true');
        panel.inert = true;
        trigger.setAttribute('aria-controls', panel.id);
        const inner = element('div', 'trip-intake-step-inner');
        const content = element('div', 'trip-intake-field-content');
        const help = element(
            'span',
            field.error ? 'trip-intake-field-help is-error' : 'trip-intake-field-help',
            field.error || field.help_text || '',
        );
        help.id = `${state.id}-help-${index}`;
        content.appendChild(help);
        if (field.input_type !== 'date' && Array.isArray(field.examples) && field.examples.length) {
            content.appendChild(element('span', 'trip-intake-field-examples', `例如：${field.examples.join('、')}`));
        }
        if (field.suggested_value) {
            const suggestion = element('div', 'trip-intake-suggestion');
            suggestion.appendChild(element(
                'span',
                'trip-intake-field-help',
                `${field.suggestion_reason || '根据已有信息'}，请确认`,
            ));
            suggestion.appendChild(optionAction(
                `确认 ${field.suggested_value}`,
                field.key,
                field.suggested_value,
                state,
            ));
            content.appendChild(suggestion);
        }
        if (Array.isArray(field.options) && field.options.length) {
            const options = element('div', 'trip-intake-options');
            field.options.forEach((option) => options.appendChild(optionAction(option, field.key, option, state)));
            content.appendChild(options);
        }
        let input;
        if (field.key === 'origin' || field.key === 'destination') {
            const picker = cityPicker(
                field,
                state,
                field.key === 'origin' ? state.homeLocation : '',
                field.key === 'origin'
                    ? [...new Set([state.homeLocation, ...POPULAR_CITIES].filter(Boolean))]
                    : POPULAR_CITIES,
                // 目的地一改，已确认的会议地点就失效了：清空旧选择并重新启用搜索框。
                // 少了这一步，地点输入框会停在禁用状态，用户再也改不回办公地点。
                field.key === 'destination' ? () => state.placePicker?.resetCity() : undefined,
                (city) => cityConflict(state, field.key, city),
            );
            input = picker.input;
            content.appendChild(picker.node);
        } else {
            input = inlineInput(field, state, false);
            content.appendChild(input);
        }
        input.setAttribute('aria-describedby', help.id);
        const navigation = element('div', 'trip-intake-step-navigation');
        navigation.appendChild(element('span', '', '填写后可随时修改'));
        const next = element('button', 'trip-intake-step-next', index === state.stepCount - 1 ? '完成填写' : '下一步');
        next.type = 'button';
        next.disabled = true;
        next.addEventListener('click', () => advanceStep(state, field.key));
        navigation.appendChild(next);
        content.appendChild(navigation);
        inner.appendChild(content);
        panel.appendChild(inner);
        row.appendChild(panel);
        state.steps.set(field.key, { row, trigger, panel, summary, badge, index: number, number: index + 1, next, help, input, field });
        return row;
    }

    function renderOptional(fields, state) {
        const list = element('div', 'trip-intake-optional-list');
        fields.forEach((field) => {
            if (field.key === 'work_location') {
                list.appendChild(element('p', 'trip-intake-field-help', '下一步将在目的地城市内搜索并选择准确的会议或办公地点。'));
                return;
            }
            const row = element('div', 'trip-intake-optional-row');
            const copy = element('div');
            copy.appendChild(element('span', '', field.label));
            copy.appendChild(element('small', '', field.help_text));
            row.appendChild(copy);
            row.appendChild(inlineInput(field, state, true));
            list.appendChild(row);
        });
        return renderDisclosure('trip-intake-optional', '补充后安排会更准确', list);
    }

    function renderPlace(data, state) {
        const wrap = element('section', 'trip-intake-place');
        wrap.appendChild(element('h3', '', '确认会议 / 办公地点'));
        const getCity = () => state.steps.has('destination') ? (state.values.get('destination') || '') : (data.trip_input?.destination || data.route?.destination || '');
        const saved = data.trip_input?.work_location_verified;
        const preview = window.HommeyJourneyMap?.create({anchor:saved || null, city:getCity()});
        if (preview) preview.hidden = !saved;
        const choose = item => {
            state.values.set('work_location',item.name); state.values.set('work_location_place_id',item.place_id || item.provider_place_id);
            if (preview) { preview.hidden=false; preview.setData({anchor:item,city:getCity()}); }
            updateSelectionState(state);
        };
        state.placePicker = window.HommeyTripChoices.picker({getCity, initialName:data.trip_input?.work_location || '',
            onSelect:choose, onClear:()=>{state.values.delete('work_location'); state.values.delete('work_location_place_id'); if(preview) {preview.hidden=true;preview.setData({anchor:null});} updateSelectionState(state);}});
        state.requiredKeys.add('work_location_place_id');
        if (saved) { state.values.set('work_location',saved.name); state.values.set('work_location_place_id',saved.provider_place_id); }
        wrap.append(state.placePicker); if(preview) wrap.append(preview);
        return wrap;
    }

    function renderConflicts(conflicts, state) {
        const wrap = element('div', 'trip-intake-conflicts');
        conflicts.forEach((conflict) => {
            const block = element('div', 'trip-intake-conflict');
            block.appendChild(element('strong', '', '需要确认'));
            block.appendChild(element('p', '', conflict.message));
            if (state.dateKeys.has(conflict.key)) {
                block.appendChild(datePicker({ key: conflict.key, label: '出发日期' }, state));
            } else if (Array.isArray(conflict.values) && conflict.values.length) {
                const options = element('div', 'trip-intake-options');
                conflict.values.forEach((value) => options.appendChild(optionAction(value, conflict.key, value, state)));
                block.appendChild(options);
            }
            state.conflictBlocks.set(conflict.key, block);
            wrap.appendChild(block);
        });
        return wrap;
    }

    function replyPart(key, value) {
        const clean = String(value || '').trim();
        if (!clean) return '';
        if (key === 'origin') return `从${clean}出发`;
        if (key === 'destination') return `目的地：${clean}`;
        if (key === 'start_date') return `${clean}出发`;
        if (key === 'trip_length') {
            if (/^\d+\s*天$/.test(clean)) return `出差${clean.replace(/\s+/g, '')}`;
            if (clean.includes('返程')) return clean;
            return `行程时长：${clean}`;
        }
        if (key === 'trip_purpose') return `出差目的：${clean}`;
        if (key === 'work_location') return `工作地点：${clean}`;
        if (key === 'work_schedule') return `工作时间：${clean}`;
        return `${key}：${clean}`;
    }

    function buildReply(data, state) {
        const order = [];
        // Values settled from memory are not prompted, but still belong in the sentence we send.
        (data.collected || []).forEach((field) => {
            if (field.source === 'memory' && state.values.has(field.key)) order.push(field.key);
        });
        (data.missing_required || []).forEach((field) => order.push(field.key));
        (data.conflicts || []).forEach((field) => order.push(field.key));
        (data.optional || []).forEach((field) => order.push(field.key));
        return [...new Set(order)]
            .map((key) => replyPart(key, state.values.get(key)))
            .filter(Boolean)
            .join('，');
    }

    function renderSubmission(data, state, card) {
        const footer = element('footer', 'trip-intake-footer');
        const copy = element('div');
        copy.appendChild(element('span', '', '在卡片内完成补充'));
        state.status = element('p', 'trip-intake-submit-status', '');
        state.status.setAttribute('role', 'status');
        copy.appendChild(state.status);
        footer.appendChild(copy);
        const submit = element('button', 'trip-intake-action is-primary trip-intake-submit', data.place_selection_required ? '确认行程并查询' : '提交补充信息');
        submit.type = 'button';
        submit.disabled = true;
        state.submit = submit;
        submit.addEventListener('click', () => {
            updateSelectionState(state);
            if (state.archived || submit.disabled || state.errors.size) return;
            let text = buildReply(data, state);
            let requestPayload;
            if (data.place_selection_required && window.HommeyTripChoices) {
                const trip = {...data.trip_input}; delete trip.work_location_verified;
                for (const key of ['origin','destination','start_date','trip_purpose','work_location','work_location_place_id','work_schedule']) {
                    if (state.values.has(key)) trip[key]=state.values.get(key);
                }
                const length = state.values.get('trip_length');
                if (length) {
                    const count = String(length).match(/^(\d+)\s*天$/);
                    const end = String(length).match(/^(\d{4}-\d{2}-\d{2})\s*返程$/);
                    if (count) {trip.duration_days=Number(count[1]);delete trip.end_date;}
                    else if (end) {trip.end_date=end[1];delete trip.duration_days;}
                    else {state.status.textContent='请用“2天”或“2026-09-15返程”填写行程时长';return;}
                }
                if (trip.duration_days && trip.start_date) trip.end_date=addDays(trip.start_date,trip.duration_days-1);
                if (!trip.end_date || !trip.work_location_place_id) {state.status.textContent='请补齐日期并从高德结果中选择地点';return;}
                text = [`从${trip.origin}出发`,`目的地：${trip.destination}`,`${trip.start_date}出发`,`${trip.end_date}返程`,`出差目的：${trip.trip_purpose}`,`工作地点：${trip.work_location}`].join('，');
                requestPayload={input_source:'quick_trip_form',trip_input:trip,capability_selection:data.capability_selection || {}};
            }
            if (!text || state.submitting) return;
            const detail = {
                text,
                ...(requestPayload ? {requestPayload} : {}),
                source: 'trip_intake',
                card,
                interactionId: data.interaction_id,
                complete(success) {
                    state.submitting = false;
                    card.classList.remove('is-submitting');
                    if (success) card.archive?.('已提交');
                    submit.textContent = success ? '已提交' : '重新提交';
                    if (!success) updateSelectionState(state);
                },
            };
            const event = new CustomEvent('hommey:submit-message', { detail, cancelable: true });
            if (!document.dispatchEvent(event)) return;
            state.submitting = true;
            card.classList.add('is-submitting');
            submit.textContent = '正在提交…';
            updateSelectionState(state);
        });
        footer.appendChild(submit);
        updateSelectionState(state);
        return footer;
    }

    function create(documentData) {
        const data = documentData || {};
        const card = element('article', `trip-intake-card is-${data.status || 'collecting_required'}`);
        const missingKeys = new Set((data.missing_required || []).map((field) => field.key));
        const standaloneConflicts = (data.conflicts || []).filter(
            (conflict) => !missingKeys.has(conflict.key),
        );
        const state = {
            id: `trip-intake-${++nextCardId}`,
            steps: new Map(),
            stepCount: (data.missing_required || []).length,
            activeKey: null,
            stepStatus: null,
            values: new Map(),
            errors: new Map(),
            dateKeys: new Set(['start_date', ...(data.missing_required || []).filter((field) => field.input_type === 'date').map((field) => field.key)]),
            datePickers: new Map(),
            controls: new Map(),
            requiredKeys: new Set([
                ...(data.missing_required || []).map((field) => field.key),
                ...(data.conflicts || []).map((conflict) => conflict.key),
            ]),
            submit: null,
            status: null,
            submitting: false,
            homeLocation: data.home_location || '',
            document: data,
            conflictBlocks: new Map(),
        };
        card.setAttribute('aria-label', data.title || '补充行程信息');
        // Memory-sourced values seed the form so submitting never depends on whether the chip was touched.
        (data.collected || []).forEach((field) => {
            if (field.source === 'memory') state.values.set(field.key, field.value);
        });

        const header = element('header', 'trip-intake-header');
        const heading = element('div', 'trip-intake-heading');
        const text = element('div');
        const eyebrow = element('span', 'trip-intake-eyebrow');
        const logo = element('span', 'hommey-card-logo');
        logo.setAttribute('aria-hidden', 'true');
        eyebrow.append(logo, element('span', '', 'Hommey · 行程信息'));
        text.appendChild(eyebrow);
        text.appendChild(element('h2', '', data.title || '行程信息'));
        heading.appendChild(text);
        const progress = data.progress || { completed: 0, total: 5 };
        heading.appendChild(element('strong', 'trip-intake-progress', `${progress.completed} / ${progress.total}`));
        header.appendChild(heading);
        if (data.route) header.appendChild(renderRoute(data.route, state));
        if (data.summary) header.appendChild(element('p', 'trip-intake-summary', data.summary));
        if (Array.isArray(data.collected) && data.collected.length) {
            header.appendChild(renderCollected(data.collected, state));
        }
        card.appendChild(header);

        if (standaloneConflicts.length) {
            card.appendChild(renderConflicts(standaloneConflicts, state));
        }

        if (Array.isArray(data.missing_required) && data.missing_required.length) {
            const required = element('section', 'trip-intake-required');
            const heading = element('div', 'trip-intake-required-heading');
            heading.appendChild(element('h3', '', '补全这一程'));
            state.stepStatus = element('span', 'trip-intake-step-count');
            heading.appendChild(state.stepStatus);
            required.appendChild(heading);
            const list = element('div', 'trip-intake-field-list');
            data.missing_required.forEach((field, index) => list.appendChild(renderPrompt(field, index, state)));
            required.appendChild(list);
            card.appendChild(required);
            const first = data.missing_required.find((field) => field.error) || data.missing_required[0];
            openStep(state, first.key);
        }

        if (data.place_selection_required && window.HommeyTripChoices) card.appendChild(renderPlace(data, state));
        const optional = (data.optional || []).filter(field => field.key !== 'work_location');
        if (optional.length) {
            card.appendChild(renderOptional(optional, state));
        }

        if (state.requiredKeys.size) {
            card.appendChild(renderSubmission(data, state, card));
        }
        // Archive to a genuinely read-only snapshot, without retaining live form controls.
        card.archive = (label = '已归档') => {
            if (state.archived) return;
            state.archived = true;
            card.dataset.archived = 'true';
            card.classList.add('is-archived');
            const snapshot = new Map((data.collected || []).map(field => [field.key, {label: field.label, value: field.value}]));
            const fields = [...(data.missing_required || []), ...(data.optional || [])];
            state.values.forEach((value, key) => {
                const field = fields.find(item => item.key === key);
                if (field) snapshot.set(key, {label: field.label, value});
            });
            const disclosure = element('details', 'trip-intake-archive');
            const summary = element('summary', 'trip-intake-archive-heading');
            const logo = element('span', 'hommey-card-logo');
            logo.setAttribute('aria-hidden', 'true');
            const route = [data.route?.origin, data.route?.destination].filter(Boolean).join(' → ');
            summary.append(logo, element('span', '', route || '行程信息'), element('small', '', label));
            const body = element('div', 'trip-intake-archive-body');
            body.appendChild(element('p', '', '此卡片仅供回看，继续补充请使用当前对话。'));
            const list = element('dl', 'trip-intake-archive-values');
            snapshot.forEach(field => {
                if (field.value) list.append(element('dt', '', field.label), element('dd', '', field.value));
            });
            body.appendChild(list);
            disclosure.append(summary, body);
            if (card.contains(document.activeElement)) summary.tabIndex = 0;
            const hadFocus = card.contains(document.activeElement);
            card.replaceChildren(disclosure);
            if (hadFocus) summary.focus({preventScroll: true});
        };
        return card;
    }

    window.HommeyTripIntakeCard = { create };
}());
