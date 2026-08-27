(function () {
    'use strict';

    let nextCardId = 0;
    let nextDisclosureId = 0;

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
        const completed = [...state.requiredKeys].filter((key) => String(state.values.get(key) || '').trim()).length;
        if (state.status) {
            state.status.textContent = state.requiredKeys.size
                ? `已填写 ${completed} / ${state.requiredKeys.size} 项`
                : '可直接提交';
        }
        if (state.submit) state.submit.disabled = state.submitting || completed < state.requiredKeys.size;
        state.steps.forEach((step, key) => {
            const value = state.values.get(key) || '';
            const filled = !!String(value).trim();
            step.row.classList.toggle('is-complete', filled);
            step.row.classList.toggle('is-invalid', !filled && !!step.field.error);
            step.summary.textContent = value || '点击补充';
            step.summary.title = value;
            step.badge.textContent = filled ? '已填写' : (step.field.error ? '待确认' : '待填写');
            step.index.textContent = filled ? '✓' : step.number;
            step.next.disabled = state.submitting || !filled;
            step.help.textContent = (!filled && step.field.error) || step.field.help_text || '';
            step.help.classList.toggle('is-error', !filled && !!step.field.error);
            step.input.setAttribute('aria-invalid', !filled && step.field.error ? 'true' : 'false');
        });
        if (state.stepStatus) {
            const filled = [...state.steps.keys()].filter((key) => state.values.has(key)).length;
            state.stepStatus.textContent = `${filled} / ${state.steps.size} 项已填`;
        }
    }

    function openStep(state, key, focus = false) {
        if (state.submitting) return;
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
        const clean = String(value || '').trim();
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
        updateSelectionState(state);
    }

    function optionAction(label, key, value, state) {
        const button = element('button', 'trip-intake-action', label);
        button.type = 'button';
        button.setAttribute('aria-pressed', 'false');
        registerControl(state, key, button);
        button.addEventListener('click', () => selectValue(state, key, value, button));
        return button;
    }

    function inlineInput(field, state, optional) {
        const input = element('input', 'trip-intake-inline-input');
        input.type = field.input_type === 'date' ? 'date' : 'text';
        input.autocomplete = 'off';
        input.placeholder = optional
            ? (field.help_text || '可选填写')
            : (field.options?.length ? '或自行填写' : (field.examples?.[0] || field.help_text || '请填写'));
        input.setAttribute('aria-label', field.label);
        registerControl(state, field.key, input);
        input.addEventListener('input', () => selectValue(state, field.key, input.value, input));
        return input;
    }

    function renderRoute(route) {
        const row = element('div', 'trip-intake-route');
        row.appendChild(element('strong', '', route.origin || '出发地待补充'));
        const line = element('span', 'trip-intake-route-line');
        line.setAttribute('aria-hidden', 'true');
        row.appendChild(line);
        row.appendChild(element('strong', '', route.destination || '目的地待补充'));
        return row;
    }

    function renderCollected(fields) {
        const wrap = element('div', 'trip-intake-collected');
        wrap.appendChild(element('span', 'trip-intake-collected-label', '已确认'));
        const list = element('div', 'trip-intake-collected-list');
        fields.forEach((field) => {
            const item = element('div', 'trip-intake-collected-item');
            item.appendChild(element('span', '', field.label));
            item.appendChild(element('strong', '', field.value));
            list.appendChild(item);
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
        if (Array.isArray(field.examples) && field.examples.length) {
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
        const input = inlineInput(field, state, false);
        input.setAttribute('aria-describedby', help.id);
        content.appendChild(input);
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

    function renderConflicts(conflicts, state) {
        const wrap = element('div', 'trip-intake-conflicts');
        conflicts.forEach((conflict) => {
            const block = element('div', 'trip-intake-conflict');
            block.appendChild(element('strong', '', '需要确认'));
            block.appendChild(element('p', '', conflict.message));
            if (Array.isArray(conflict.values) && conflict.values.length) {
                const options = element('div', 'trip-intake-options');
                conflict.values.forEach((value) => options.appendChild(optionAction(value, conflict.key, value, state)));
                block.appendChild(options);
            }
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
        const submit = element('button', 'trip-intake-action is-primary trip-intake-submit', '提交补充信息');
        submit.type = 'button';
        submit.disabled = true;
        state.submit = submit;
        submit.addEventListener('click', () => {
            const text = buildReply(data, state);
            if (!text || state.submitting) return;
            const detail = {
                text,
                source: 'trip_intake',
                complete(success) {
                    state.submitting = false;
                    card.classList.remove('is-submitting');
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
            controls: new Map(),
            requiredKeys: new Set([
                ...(data.missing_required || []).map((field) => field.key),
                ...(data.conflicts || []).map((conflict) => conflict.key),
            ]),
            submit: null,
            status: null,
            submitting: false,
        };
        card.setAttribute('aria-label', data.title || '补充行程信息');

        const header = element('header', 'trip-intake-header');
        const heading = element('div', 'trip-intake-heading');
        const text = element('div');
        text.appendChild(element('span', 'trip-intake-eyebrow', 'TRIP INTAKE'));
        text.appendChild(element('h2', '', data.title || '行程信息'));
        heading.appendChild(text);
        const progress = data.progress || { completed: 0, total: 5 };
        heading.appendChild(element('strong', 'trip-intake-progress', `${progress.completed} / ${progress.total}`));
        header.appendChild(heading);
        if (data.route) header.appendChild(renderRoute(data.route));
        if (data.summary) header.appendChild(element('p', 'trip-intake-summary', data.summary));
        if (Array.isArray(data.collected) && data.collected.length) {
            header.appendChild(renderCollected(data.collected));
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

        if (Array.isArray(data.optional) && data.optional.length) {
            card.appendChild(renderOptional(data.optional, state));
        }

        if (state.requiredKeys.size) {
            card.appendChild(renderSubmission(data, state, card));
        }
        if (data.plain_text) card.appendChild(renderPlainText(data.plain_text));
        return card;
    }

    window.HommeyTripIntakeCard = { create };
}());
