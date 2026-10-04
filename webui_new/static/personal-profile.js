/* User-entered profile setup and settings. All edits use the authenticated profile API. */
(() => {
    'use strict';
    const layer = document.getElementById('personalProfileLayer');
    const form = document.getElementById('personalProfileForm');
    const content = document.getElementById('personalProfileContent');
    const tabs = document.getElementById('personalProfileTabs');
    const title = document.getElementById('personalProfileTitle');
    const subtitle = document.getElementById('personalProfileSubtitle');
    const error = document.getElementById('personalProfileError');
    const saveButton = document.getElementById('personalProfileSave');
    const backButton = document.getElementById('personalProfileBack');
    const skipButton = document.getElementById('personalProfileSkip');
    const closeButton = document.getElementById('personalProfileClose');
    let api, endpoint, stored, draft, firstRun = false, step = 'basic', busy = false, returnFocus, fromSettings = false;
    const categories = [['staff', '教职工'], ['student', '学生'], ['external', '校外人员'], ['other', '其他']];
    const titleLevels = [['senior', '正高级'], ['associate_senior', '副高级'], ['intermediate', '中级'], ['junior', '初级'], ['none', '无专业技术职称']];
    const clone = value => JSON.parse(JSON.stringify(value));
    const emptyIdentity = () => ({ professional_title_level: null, professional_position_grade: null, staff_grade: null, administrative_rank: null, academician_status: null, nationally_recognized_expert: null });
    const projectFields = { id: null, name: null, financial_project_code: null, funding_category: null, research_type: null, program_type: null, funding_source: null, is_military_project: null, user_project_role: null };
    const boolOptions = [['true', '是'], ['false', '否']];
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const activeMotion = new Map();
    const disclosureMotion = new WeakMap();
    const expandedSections = new Map();
    const expandedProjects = new Set();
    const ease = 'cubic-bezier(.22, 1, .36, 1)';
    let renderedStep = null;
    const indicator = document.createElement('span');
    indicator.className = 'profile-step-indicator';
    indicator.setAttribute('aria-hidden', 'true');

    function motionEnabled() {
        return !reducedMotion.matches && document.documentElement.dataset.motion !== 'off';
    }
    function move(element, frames, options = {}, onDone) {
        if (!motionEnabled()) { onDone?.(); return null; }
        const animation = element.animate(frames, { duration: 300, easing: ease, ...options });
        activeMotion.set(animation, element);
        const done = () => { activeMotion.delete(animation); onDone?.(); };
        animation.finished.then(done, done);
        return animation;
    }
    function settleMotion() {
        for (const animation of activeMotion.keys()) animation.finish();
    }
    const motionObserver = new MutationObserver(() => { if (!motionEnabled()) settleMotion(); });
    motionObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-motion'] });
    reducedMotion.addEventListener('change', () => { if (!motionEnabled()) settleMotion(); });

    function resizeModal(beforeHeight, afterHeight) {
        for (const [animation, element] of activeMotion) if (element === form) animation.cancel();
        afterHeight ??= form.offsetHeight;
        if (Math.abs(beforeHeight - afterHeight) < 1) return;
        // Animate the actual box, so centering and the scroll area follow its height.
        move(form, [{ height: `${beforeHeight}px` }, { height: `${afterHeight}px` }], { duration: 340 });
    }
    function geometry(page) {
        const result = new Map(), origin = content.getBoundingClientRect();
        page?.querySelectorAll('[data-motion-key]').forEach(element => {
            const rect = element.getBoundingClientRect();
            if (!rect.width || !rect.height || element.closest('[data-profile-ghost]')) return;
            result.set(element.dataset.motionKey, { element, x: rect.left - origin.left + content.scrollLeft,
                y: rect.top - origin.top + content.scrollTop, width: rect.width, height: rect.height,
                opacity: Number(getComputedStyle(element).opacity) });
        });
        return result;
    }
    function ghost(element, rect) {
        const copy = element.cloneNode(true);
        const originals = element.querySelectorAll('input, select');
        copy.querySelectorAll('input, select').forEach((control, index) => {
            control.value = originals[index].value;
            if (control.type === 'checkbox' || control.type === 'radio') control.checked = originals[index].checked;
        });
        copy.dataset.profileGhost = '';
        copy.removeAttribute('data-motion-key');
        copy.classList.add('profile-motion-ghost');
        copy.setAttribute('aria-hidden', 'true');
        copy.inert = true;
        [copy, ...copy.querySelectorAll('*')].forEach(item => {
            item.removeAttribute('id'); item.removeAttribute('name'); item.removeAttribute('data-motion-key');
            if (item.matches('input, select, button')) item.disabled = true;
        });
        Object.assign(copy.style, { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px`, margin: '0' });
        content.append(copy);
        return copy;
    }
    function reveal(page, opening = false) {
        const items = [page.querySelector('.profile-section-intro'), ...page.querySelectorAll(':scope > .profile-grid > .profile-field'),
            ...page.querySelectorAll(':scope > .profile-project, :scope > .profile-project-empty, :scope > .profile-more, :scope > .profile-hint')].filter(Boolean);
        items.forEach((item, index) => move(item,
            [{ opacity: 0, transform: 'translateY(9px)' }, { opacity: 1, transform: 'translateY(0)' }],
            { duration: 310, delay: (opening ? 70 : 25) + Math.min(index * 23, 130), fill: 'backwards' }));
    }
    function positionIndicator(before) {
        const current = tabs.querySelector('[aria-current="step"]');
        if (!current) return;
        const after = { left: current.offsetLeft, top: current.offsetTop, width: current.offsetWidth, height: current.offsetHeight };
        Object.assign(indicator.style, Object.fromEntries(Object.entries(after).map(([key, value]) => [key, `${value}px`])));
        if (before) move(indicator, [{ left: `${before.left}px`, width: `${before.width}px` }, { left: `${after.left}px`, width: `${after.width}px` }], { duration: 320 });
    }
    new ResizeObserver(() => positionIndicator()).observe(tabs);

    function toggleMore(details, body) {
        const previous = disclosureMotion.get(details);
        const target = !(previous?.target ?? details.open);
        const currentHeight = details.open ? body.getBoundingClientRect().height : 0;
        const currentOpacity = details.open ? Number(getComputedStyle(body).opacity) : 0;
        const beforeHeight = form.offsetHeight;
        const state = { target, animation: null };
        disclosureMotion.set(details, state);
        previous?.animation?.cancel();
        for (const [animation, element] of activeMotion) if (element === form) animation.cancel();
        details.open = target;
        const afterHeight = form.offsetHeight;
        details.open = true;
        const targetHeight = target ? body.scrollHeight : 0;
        details.querySelector('summary').setAttribute('aria-expanded', String(target));
        expandedSections.set(details.dataset.motionKey, target);
        resizeModal(beforeHeight, afterHeight);
        state.animation = move(body, [{ height: `${currentHeight}px`, opacity: currentOpacity },
            { height: `${targetHeight}px`, opacity: target ? 1 : 0 }], { duration: target ? 300 : 220 }, () => {
                if (disclosureMotion.get(details) !== state) return;
                details.open = target;
                disclosureMotion.delete(details);
            });
    }

    function toggleProject(card, body, button, project, refreshSummary) {
        const previous = disclosureMotion.get(card);
        const target = !expandedProjects.has(project.id);
        const currentHeight = body.hidden ? 0 : body.getBoundingClientRect().height;
        const currentOpacity = body.hidden ? 0 : Number(getComputedStyle(body).opacity);
        const beforeHeight = form.offsetHeight;
        const state = { target, animation: null };
        disclosureMotion.set(card, state);
        previous?.animation?.cancel();
        for (const [animation, element] of activeMotion) if (element === form) animation.cancel();
        if (target) expandedProjects.add(project.id); else expandedProjects.delete(project.id);
        refreshSummary();
        card.dataset.expanded = String(target);
        button.setAttribute('aria-expanded', String(target));
        button.firstChild.textContent = target ? '收起' : '展开编辑';
        if (!target && body.contains(document.activeElement)) button.focus({ preventScroll: true });
        body.inert = !target;
        body.hidden = !target;
        const afterHeight = form.offsetHeight;
        body.hidden = false;
        const targetHeight = target ? body.scrollHeight : 0;
        resizeModal(beforeHeight, afterHeight);
        state.animation = move(body, [{ height: `${currentHeight}px`, opacity: currentOpacity },
            { height: `${targetHeight}px`, opacity: target ? 1 : 0 }], { duration: target ? 320 : 260 }, () => {
                if (disclosureMotion.get(card) !== state) return;
                body.hidden = !target;
                disclosureMotion.delete(card);
            });
    }

    function node(tag, className, text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text) element.textContent = text;
        return element;
    }
    function message(text = '') {
        const before = form.offsetHeight;
        error.textContent = text; error.hidden = !text;
        if (layer.classList.contains('open')) {
            resizeModal(before);
            if (text) move(error, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'translateY(0)' }], { duration: 220 });
        }
    }
    function setBusy(value) {
        busy = value;
        form.setAttribute('aria-busy', String(value));
        form.querySelectorAll('button, input, select').forEach(element => { element.disabled = value; });
        if (!value && draft?.funding.projects.length >= 20) document.getElementById('profileAddProject')?.setAttribute('disabled', '');
    }
    function steps() {
        const category = draft.basic_info.personnel_category;
        return category === 'staff' || category === 'external' ? ['basic', 'identity', 'funding'] : ['basic', 'funding'];
    }
    function field(parent, object, key, label, options = {}) {
        const wrap = node('label', 'profile-field');
        wrap.append(node('span', '', label));
        const input = node(options.options ? 'select' : 'input');
        input.id = options.id || `profile-${key}`;
        wrap.dataset.motionKey = options.motionKey || input.id;
        input.name = key;
        if (options.options) {
            input.append(new Option(options.emptyLabel || '暂不填写', ''));
            options.options.forEach(([value, text]) => input.append(new Option(text, value)));
        } else {
            input.type = options.type || 'text';
            input.maxLength = options.maxLength || 120;
            if (options.placeholder) input.placeholder = options.placeholder;
            if (options.min !== undefined) input.min = options.min;
            if (options.max !== undefined) input.max = options.max;
            if (input.type === 'number') input.step = '1';
            if (input.type === 'date') { input.min = '1900-01-01'; input.max = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date()); }
        }
        input.value = object[key] === null || object[key] === undefined ? '' : String(object[key]);
        input.addEventListener(options.options ? 'change' : 'input', () => {
            object[key] = input.value === '' ? null : options.type === 'number' ? Number(input.value) : options.boolean ? input.value === 'true' : input.value;
            options.onChange?.();
        });
        wrap.append(input);
        if (options.note) wrap.append(node('small', '', options.note));
        parent.append(wrap);
        return wrap;
    }
    function intro(parent, heading, text) {
        const header = node('div', 'profile-section-intro');
        header.dataset.motionKey = 'intro';
        header.append(node('h3', '', heading), node('p', '', text));
        parent.append(header);
    }
    function advanced(parent, text) {
        const details = node('details', 'profile-more');
        details.dataset.motionKey = `more-${step}`;
        details.open = expandedSections.get(details.dataset.motionKey) || false;
        const summary = node('summary', '', text);
        summary.setAttribute('aria-expanded', String(details.open));
        details.append(summary);
        const body = node('div', 'profile-more-body');
        const grid = node('div', 'profile-grid');
        body.append(grid); details.append(body); parent.append(details);
        summary.addEventListener('click', event => { event.preventDefault(); toggleMore(details, body); });
        return grid;
    }
    function basicSection(parent) {
        const basic = draft.basic_info;
        intro(parent, '你的基本资料', '先选择人员类别，接下来只展示适用的资料。所有信息均可选填。');
        const grid = node('div', 'profile-grid profile-basic-grid'); parent.append(grid);
        field(grid, basic, 'personnel_category', '人员类别', { options: categories, emptyLabel: '请选择（可稍后补充）', onChange: () => {
            draft.policy_identity = emptyIdentity();
            if (basic.personnel_category !== 'student') basic.student_number = null;
            if (basic.personnel_category !== 'staff') basic.employee_number = null;
            render(); document.getElementById('profile-personnel_category').focus();
        }});
        field(grid, basic, 'real_name', '姓名', { placeholder: '你的称呼', maxLength: 120 });
        if (basic.personnel_category === 'student') field(grid, basic, 'student_number', '学号', { maxLength: 64, placeholder: '选填，保留前导零' });
        if (basic.personnel_category === 'staff') field(grid, basic, 'employee_number', '工号', { maxLength: 64, placeholder: '选填，保留前导零' });
        field(grid, basic, 'gender', '性别', { options: [['male', '男'], ['female', '女'], ['other', '其他'], ['undisclosed', '不愿透露']] });
        field(grid, basic, 'institution', '所属机构', { note: '当前制度知识库为重庆大学；修改机构不会切换知识库。' });
        field(grid, basic, 'department', '学院 / 二级单位', { placeholder: '例如：计算机学院' });
        const more = advanced(parent, '更多基本资料');
        field(more, basic, 'birth_date', '出生日期', { type: 'date', note: '选填，用于与年龄有关的报销条件判断。' });
        field(more, basic, 'nationality', '国籍', { placeholder: '选填' });
        field(more, draft.settlement, 'has_official_card', '是否持有公务卡', { options: boolOptions, boolean: true });
        if (basic.personnel_category === 'student') {
            const hint = node('p', 'profile-hint', '学生无需填写专业技术职称、岗位等级及职员等级。');
            hint.dataset.motionKey = 'student-hint'; parent.append(hint);
        }
    }
    function identitySection(parent) {
        const identity = draft.policy_identity;
        intro(parent, '报销身份', '填写你已确认的身份信息。不确定的等级可以留空，之后再补充。');
        const grid = node('div', 'profile-grid'); parent.append(grid);
        field(grid, identity, 'professional_title_level', '专业技术职称', { options: titleLevels, onChange: () => {
            identity.professional_position_grade = null;
            render(); document.getElementById('profile-professional_title_level').focus();
        }});
        if (identity.professional_title_level !== 'none') field(grid, identity, 'professional_position_grade', '专业技术岗位等级', { type: 'number', min: 1, max: 13, placeholder: '1–13，选填', note: '岗位等级与职称层级分别填写。' });
        if (draft.basic_info.personnel_category === 'staff') field(grid, identity, 'staff_grade', '职员等级', { type: 'number', min: 1, max: 10, placeholder: '1–10，选填', note: '与专业技术岗位等级不同。' });
        const more = advanced(parent, '更多报销身份');
        if (draft.basic_info.personnel_category === 'staff') field(more, identity, 'administrative_rank', '行政级别', { options: [['provincial_ministerial', '省部级'], ['department_bureau', '司局级'], ['other', '其他']] });
        field(more, identity, 'academician_status', '院士身份', { options: [['academician', '院士'], ['equivalent', '已确认的相当院士身份'], ['none', '非上述身份']] });
        field(more, identity, 'nationally_recognized_expert', '全国知名专家', { options: boolOptions, boolean: true });
    }
    function fundingSection(parent) {
        intro(parent, '常用经费', '可添加多个项目，并选择一个常用默认项目。经费号选填，每次报销仍可使用其他经费。');
        const none = node('label', 'profile-default-none');
        none.dataset.motionKey = 'default-none';
        const radio = node('input'); radio.type = 'radio'; radio.name = 'default_funding'; radio.value = ''; radio.checked = !draft.funding.default_project_id;
        radio.addEventListener('change', () => { draft.funding.default_project_id = null; });
        none.append(radio, document.createTextNode('暂不设置默认经费')); parent.append(none);
        if (!draft.funding.projects.length) {
            const empty = node('div', 'profile-project-empty');
            empty.dataset.motionKey = 'project-empty';
            empty.append(node('span', 'profile-project-symbol', '+'), node('strong', '', '添加一个常用项目'), node('p', '', '还没有确定经费？可以直接完成，稍后在个人信息中补充。'));
            parent.append(empty);
        }
        draft.funding.projects.forEach((project, index) => {
            const card = node('section', 'profile-project'); card.dataset.projectId = project.id;
            card.dataset.motionKey = `project-${project.id}`;
            const expanded = expandedProjects.has(project.id);
            card.dataset.expanded = String(expanded);
            const head = node('div', 'profile-project-head');
            const copy = node('div', 'profile-project-copy');
            const name = node('h4'), meta = node('p', 'profile-project-meta');
            copy.append(name, meta); head.append(copy);
            const refreshSummary = () => {
                name.textContent = project.name?.trim() || `项目 ${index + 1}`;
                name.title = name.textContent;
                const category = project.funding_category === 'research'
                    ? ({ vertical: '纵向科研', horizontal: '横向科研' }[project.research_type] || '科研经费')
                    : project.funding_category === 'non_research' ? '非科研经费' : '';
                const source = { fiscal: '财政拨款', non_fiscal: '非财政拨款' }[project.funding_source];
                const code = project.financial_project_code?.trim();
                meta.textContent = [category, source, code ? `经费号 ${code}` : ''].filter(Boolean).join(' · ') || '经费信息待补充';
                meta.title = meta.textContent;
            };
            refreshSummary();
            const actions = node('div', 'profile-project-actions'); head.append(actions);
            const select = node('label', 'profile-project-default');
            const radio = node('input'); radio.type = 'radio'; radio.name = 'default_funding'; radio.value = project.id; radio.checked = project.id === draft.funding.default_project_id;
            radio.addEventListener('change', () => { draft.funding.default_project_id = project.id; });
            select.append(radio, document.createTextNode('默认经费')); actions.append(select);
            const body = node('div', 'profile-project-body'); body.id = `profile-project-body-${index}`;
            body.hidden = !expanded; body.inert = !expanded;
            const toggle = node('button', 'profile-project-toggle'); toggle.type = 'button';
            toggle.append(document.createTextNode(expanded ? '收起' : '展开编辑'), node('span', 'profile-project-chevron'));
            toggle.setAttribute('aria-expanded', String(expanded)); toggle.setAttribute('aria-controls', body.id);
            toggle.addEventListener('click', () => toggleProject(card, body, toggle, project, refreshSummary));
            actions.append(toggle);
            const remove = node('button', 'profile-text-button', '移除'); remove.type = 'button'; remove.setAttribute('aria-label', `移除项目 ${index + 1}`);
            remove.addEventListener('click', () => {
                expandedProjects.delete(project.id);
                draft.funding.projects.splice(index, 1);
                if (draft.funding.default_project_id === project.id) draft.funding.default_project_id = null;
                render(); document.getElementById('profileAddProject').focus();
            }); actions.append(remove); card.append(head, body);
            const grid = node('div', 'profile-grid'); body.append(grid);
            const fieldFor = (key, label, options = {}) => field(grid, project, key, label, { ...options, id: `project-${index}-${key}`, motionKey: `${project.id}-${key}` });
            fieldFor('name', '项目名称 / 简称', { placeholder: '例如：课题 A' });
            fieldFor('financial_project_code', '经费号（财务项目号）', { maxLength: 64, placeholder: '选填' });
            fieldFor('funding_category', '经费类别', { options: [['research', '科研经费'], ['non_research', '非科研经费']], onChange: () => {
                if (project.funding_category !== 'research') {
                    project.research_type = null; project.program_type = null; project.is_military_project = null;
                }
                render(); document.getElementById(`project-${index}-funding_category`).focus();
            }});
            fieldFor('funding_source', '经费来源', { options: [['fiscal', '财政拨款'], ['non_fiscal', '非财政拨款']] });
            if (project.funding_category === 'research') {
                fieldFor('research_type', '科研经费类型', { options: [['vertical', '纵向'], ['horizontal', '横向']] });
                fieldFor('program_type', '项目计划类别', { options: [['national_science_technology', '国家科技计划'], ['national_social_science', '国家社会科学基金'], ['other', '其他']] });
                fieldFor('is_military_project', '是否军工项目', { options: boolOptions, boolean: true });
            }
            fieldFor('user_project_role', '我在项目中的角色', { options: [['principal', '负责人'], ['member', '成员'], ['handler', '经办人']] });
            const foot = node('div', 'profile-project-footer');
            const done = node('button', 'profile-project-done', '完成编辑'); done.type = 'button';
            done.addEventListener('click', () => toggleProject(card, body, toggle, project, refreshSummary));
            foot.append(done); body.append(foot);
            parent.append(card);
        });
        const add = node('button', 'profile-add-project', '+ 添加常用项目'); add.id = 'profileAddProject'; add.type = 'button'; add.disabled = draft.funding.projects.length >= 20;
        add.dataset.motionKey = 'add-project';
        add.addEventListener('click', () => {
            if (draft.funding.projects.length >= 20) return;
            // API accepts an opaque, user-scoped ID; users never enter the ID themselves.
            const id = crypto.randomUUID?.() || `project-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
            draft.funding.projects.push({ ...projectFields, id });
            expandedProjects.add(id);
            render(); document.getElementById(`project-${draft.funding.projects.length - 1}-name`).focus();
        }); parent.append(add);
    }
    function render({ opening = false } = {}) {
        const oldPage = content.querySelector('.profile-page:not([data-profile-ghost])');
        const oldBounds = oldPage?.getBoundingClientRect(), contentBounds = content.getBoundingClientRect();
        const beforeHeight = form.offsetHeight;
        const before = opening ? new Map() : geometry(oldPage);
        const oldIndicator = !opening && indicator.isConnected ? { left: indicator.offsetLeft, width: indicator.offsetWidth } : null;
        const oldStep = renderedStep;
        // Capture the current visual position before interrupting a previous transition.
        for (const animation of activeMotion.keys()) animation.cancel();
        content.querySelectorAll('[data-profile-ghost]').forEach(element => element.remove());
        const available = steps();
        if (!available.includes(step)) step = 'basic';
        tabs.replaceChildren(indicator);
        available.forEach((key, index) => {
            const button = node('button', 'profile-step', `${index + 1}  ${ {basic: '基本资料', identity: '报销身份', funding: '常用经费'}[key]}`);
            button.type = 'button'; button.dataset.step = key; button.setAttribute('aria-current', key === step ? 'step' : 'false');
            button.addEventListener('click', () => { if (step === key) return; step = key; render(); content.querySelector('input, select, button')?.focus({ preventScroll: true }); }); tabs.append(button);
        });
        const page = node('div', 'profile-page');
        ({ basic: basicSection, identity: identitySection, funding: fundingSection })[step](page);
        content.replaceChildren(page);
        if (opening || oldStep !== step) content.scrollTop = 0;
        backButton.hidden = !firstRun || available.indexOf(step) === 0;
        saveButton.textContent = firstRun && step !== available.at(-1) ? '下一步' : firstRun ? '完成设置' : '保存资料';
        skipButton.hidden = !firstRun;
        positionIndicator(oldIndicator);
        renderedStep = step;
        if (!motionEnabled()) return;
        if (opening || oldStep !== step) {
            if (!opening && oldPage) {
                const out = ghost(oldPage, { x: oldBounds.left - contentBounds.left, y: oldBounds.top - contentBounds.top + content.scrollTop, width: oldBounds.width, height: oldBounds.height });
                move(out, [{ opacity: 1, transform: 'translateX(0)' }, { opacity: 0, transform: `translateX(${available.indexOf(step) > available.indexOf(oldStep) ? -12 : 12}px)` }], { duration: 160 }, () => out.remove());
            }
            move(page, [{ opacity: 0, transform: `translateX(${opening ? 0 : available.indexOf(step) > available.indexOf(oldStep) ? 12 : -12}px)` }, { opacity: 1, transform: 'translateX(0)' }], { duration: 260 });
            reveal(page, opening);
        } else {
            const after = geometry(page);
            const delta = key => before.has(key) && after.has(key) ? { x: before.get(key).x - after.get(key).x, y: before.get(key).y - after.get(key).y } : { x: 0, y: 0 };
            after.forEach((rect, key) => {
                // Parent transforms already carry their children; only animate residual movement.
                const parentKey = rect.element.parentElement.closest('[data-motion-key]')?.dataset.motionKey;
                const ancestor = rect.element.parentElement.closest('.profile-more');
                if (ancestor) return;
                const parent = rect.element.parentElement.closest('.profile-project');
                if (parent && !before.has(parent.dataset.motionKey)) return;
                if (before.has(key)) {
                    const shift = delta(key), parentShift = delta(parentKey);
                    if (Math.abs(shift.x - parentShift.x) + Math.abs(shift.y - parentShift.y) > .5 || before.get(key).opacity < 1)
                        move(rect.element, [{ transform: `translate(${shift.x - parentShift.x}px, ${shift.y - parentShift.y}px)`, opacity: before.get(key).opacity }, { transform: 'translate(0, 0)', opacity: 1 }]);
                } else move(rect.element, [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'translateY(0)' }], { duration: 280, delay: 45, fill: 'backwards' });
            });
            before.forEach((rect, key) => {
                if (after.has(key)) return;
                const parentKey = rect.element.parentElement.closest('[data-motion-key]')?.dataset.motionKey;
                if (parentKey && !after.has(parentKey)) return;
                const out = ghost(rect.element, rect);
                move(out, [{ opacity: rect.opacity, transform: 'translateY(0)' }, { opacity: 0, transform: 'translateY(-5px)' }], { duration: 150 }, () => out.remove());
            });
        }
        if (!opening) resizeModal(beforeHeight);
    }
    function summary() {
        if (!stored?.profile) return;
        const basic = stored.profile.basic_info;
        const category = categories.find(([value]) => value === basic.personnel_category)?.[1];
        document.getElementById('profileBasicSummary').textContent = [category, basic.department].filter(Boolean).join(' · ') || '待补充';
        document.getElementById('profileIdentityEntry').hidden = basic.personnel_category === 'student' || basic.personnel_category === 'other';
        const identity = stored.profile.policy_identity;
        document.getElementById('profileIdentitySummary').textContent = titleLevels.find(([value]) => value === identity.professional_title_level)?.[1] || '待补充';
        const project = stored.profile.funding.projects.find(item => item.id === stored.profile.funding.default_project_id);
        const count = stored.profile.funding.projects.length;
        document.getElementById('profileFundingSummary').textContent = count > 1
            ? `${count} 个项目${project?.name ? ` · 默认 ${project.name}` : ''}` : project?.name || (count ? '1 个项目' : '待补充');
        api.onUpdate?.(stored.profile);
    }
    async function load() {
        stored = await api.fetchJson(endpoint);
        summary();
        return stored;
    }
    function show(onboarding, requestedStep = 'basic') {
        form.style.height = '';
        firstRun = onboarding; step = requestedStep; draft = clone(stored.profile);
        returnFocus = document.activeElement;
        fromSettings = document.getElementById('settingsLayer').classList.contains('open');
        document.getElementById('settingsLayer').classList.remove('open');
        title.textContent = onboarding ? '先认识一下你' : '个人信息';
        subtitle.textContent = onboarding ? '让报销与差旅建议更贴合你的情况，也可以稍后填写。' : '按实际情况补充资料，新的对话请求会使用更新后的信息。';
        expandedSections.clear();
        expandedProjects.clear();
        message(); render({ opening: true }); layer.classList.add('open'); layer.setAttribute('aria-hidden', 'false');
        document.getElementById('appShell').inert = true;
        document.getElementById('profile-personnel_category')?.focus();
        if (step !== 'basic') content.querySelector('input, select, button')?.focus();
    }
    function hide() {
        // Keep the current silhouette while closing, even if a resize is in flight.
        form.style.height = `${form.offsetHeight}px`;
        for (const animation of activeMotion.keys()) animation.cancel();
        layer.classList.remove('open'); layer.setAttribute('aria-hidden', 'true');
        document.getElementById('appShell').inert = false;
        if (fromSettings) document.getElementById('settingsLayer').classList.add('open');
        (returnFocus?.isConnected ? returnFocus : document.getElementById('homeInput'))?.focus();
    }
    form.addEventListener('transitionend', event => {
        if (event.target === form && event.propertyName === 'opacity' && !layer.classList.contains('open')) form.style.height = '';
    });
    async function skip() {
        if (busy) return;
        if (!firstRun) { hide(); return; }
        setBusy(true); message();
        try {
            stored = await api.fetchJson(`${endpoint}/skip`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ revision: stored.revision }) });
            summary(); hide();
        } catch (err) { hide(); api.onError?.('未能记录跳过状态，下次登录可重试填写。'); }
        finally { setBusy(false); }
    }
    form.addEventListener('submit', async event => {
        event.preventDefault(); if (busy) return;
        const available = steps(), index = available.indexOf(step);
        if (firstRun && index < available.length - 1) { step = available[index + 1]; render(); content.querySelector('input, select, button')?.focus({ preventScroll: true }); return; }
        // Trim editable strings; empty values remain explicitly unknown.
        const clean = value => typeof value === 'string' ? value.trim() || null : Array.isArray(value) ? value.map(clean) : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, clean(v)])) : value;
        setBusy(true); message();
        try {
            stored = await api.fetchJson(endpoint, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile: clean(draft), revision: stored.revision }) });
            summary(); hide(); api.onSaved?.();
        } catch (err) { message(err.message || '暂时无法保存，请稍后重试'); }
        finally { setBusy(false); }
    });
    backButton.addEventListener('click', () => { step = steps()[steps().indexOf(step) - 1]; render(); });
    skipButton.addEventListener('click', skip); closeButton.addEventListener('click', skip);
    layer.addEventListener('click', event => { if (event.target === layer) skip(); });
    layer.addEventListener('keydown', event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); skip(); }
        if (event.key !== 'Tab') return;
        const focusable = [...form.querySelectorAll('button, input, select, summary')].filter(el => !el.disabled && !el.closest('[data-profile-ghost]') && el.getClientRects().length);
        const first = focusable[0], last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    });
    document.querySelectorAll('[data-profile-step]').forEach(button => button.addEventListener('click', async () => {
        if (!api || busy) return;
        button.disabled = true;
        try { await load(); show(false, button.dataset.profileStep); }
        catch (err) { api.onError?.(err.message || '暂时无法读取个人资料'); }
        finally { button.disabled = false; }
    }));
    window.HommeyPersonalProfile = {
        async initialize(config) {
            api = config; endpoint = `/api/${encodeURIComponent(config.userId)}/profile`;
            try {
                await load();
                if (stored.onboarding_status === 'pending') { await api.ready; show(true); }
            }
            catch (_) { document.getElementById('profileBasicSummary').textContent = '点击重试'; }
        },
        applyDisplayName(fallback) { return stored?.profile?.basic_info?.real_name || fallback; },
    };
})();
