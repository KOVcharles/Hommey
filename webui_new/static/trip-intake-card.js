(function () {
    'use strict';

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function fillComposer(text) {
        document.dispatchEvent(new CustomEvent('hommey:fill-composer', {
            detail: { text: String(text || '') },
        }));
    }

    function action(label, value, primary) {
        const button = element('button', primary ? 'trip-intake-action is-primary' : 'trip-intake-action', label);
        button.type = 'button';
        button.addEventListener('click', () => fillComposer(value));
        return button;
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

    function renderPlainText(text) {
        const details = element('details', 'trip-intake-text-details');
        details.appendChild(element('summary', '', '查看文字版'));
        details.appendChild(element('pre', 'trip-intake-plain-text', text));
        return details;
    }

    function renderPrompt(field, index) {
        const row = element('div', `trip-intake-field${field.error ? ' is-invalid' : ''}`);
        row.appendChild(element('span', 'trip-intake-field-index', index + 1));
        const content = element('div', 'trip-intake-field-content');
        content.appendChild(element('strong', 'trip-intake-field-label', field.label));
        content.appendChild(element(
            'span',
            field.error ? 'trip-intake-field-help is-error' : 'trip-intake-field-help',
            field.error || field.help_text || '',
        ));
        if (Array.isArray(field.examples) && field.examples.length) {
            content.appendChild(element('span', 'trip-intake-field-examples', `例如：${field.examples.join('、')}`));
        }
        if (Array.isArray(field.options) && field.options.length) {
            const options = element('div', 'trip-intake-options');
            field.options.forEach((option) => options.appendChild(action(option, option, false)));
            content.appendChild(options);
        }
        row.appendChild(content);
        return row;
    }

    function renderOptional(fields) {
        const details = element('details', 'trip-intake-optional');
        details.appendChild(element('summary', '', '补充后安排会更准确'));
        const list = element('div', 'trip-intake-optional-list');
        fields.forEach((field) => {
            const row = element('button', 'trip-intake-optional-row');
            row.type = 'button';
            row.appendChild(element('span', '', field.label));
            row.appendChild(element('small', '', field.help_text));
            row.addEventListener('click', () => fillComposer(`${field.label}：`));
            list.appendChild(row);
        });
        details.appendChild(list);
        return details;
    }

    function renderConflicts(conflicts) {
        const wrap = element('div', 'trip-intake-conflicts');
        conflicts.forEach((conflict) => {
            const block = element('div', 'trip-intake-conflict');
            block.appendChild(element('strong', '', '需要确认'));
            block.appendChild(element('p', '', conflict.message));
            if (Array.isArray(conflict.values) && conflict.values.length) {
                const options = element('div', 'trip-intake-options');
                conflict.values.forEach((value) => options.appendChild(action(value, value, false)));
                block.appendChild(options);
            }
            wrap.appendChild(block);
        });
        return wrap;
    }

    function create(documentData) {
        const data = documentData || {};
        const card = element('article', `trip-intake-card is-${data.status || 'collecting_required'}`);
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

        if (Array.isArray(data.conflicts) && data.conflicts.length) {
            card.appendChild(renderConflicts(data.conflicts));
        }

        if (Array.isArray(data.missing_required) && data.missing_required.length) {
            const required = element('section', 'trip-intake-required');
            required.appendChild(element('h3', '', '需要补充'));
            const list = element('div', 'trip-intake-field-list');
            data.missing_required.forEach((field, index) => list.appendChild(renderPrompt(field, index)));
            required.appendChild(list);
            card.appendChild(required);
        }

        if (Array.isArray(data.optional) && data.optional.length) {
            card.appendChild(renderOptional(data.optional));
        }

        if (data.suggested_reply) {
            const footer = element('footer', 'trip-intake-footer');
            const example = element('div');
            example.appendChild(element('span', '', '可以直接回复'));
            example.appendChild(element('p', '', `“${data.suggested_reply}”`));
            footer.appendChild(example);
            footer.appendChild(action('填入输入框', data.suggested_reply, true));
            card.appendChild(footer);
        }
        if (data.plain_text) card.appendChild(renderPlainText(data.plain_text));
        return card;
    }

    window.HommeyTripIntakeCard = { create };
}());
