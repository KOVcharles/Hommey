(function () {
    'use strict';
    let nextId = 0;
    const el = (tag, cls, text) => {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = String(text);
        return node;
    };

    function create(data) {
        const card = el('article', 'information-card');
        const id = `information-${++nextId}`;
        const heading = el('h2', '', data.title || '补充信息');
        window.HommeyMarkdown?.render(heading, data.title || '补充信息', {inline: true});
        heading.id = `${id}-title`;
        card.setAttribute('aria-labelledby', heading.id);
        const header = el('header', 'information-header');
        header.append(el('span', 'information-eyebrow', 'HOMMEY / 补充信息'), heading);
        if (data.description) {
            const description = el('div', 'information-description', data.description);
            window.HommeyMarkdown?.render(description, data.description);
            header.append(description);
        }
        card.append(header);
        const form = el('form', 'information-form');
        form.noValidate = true;
        const fields = el('div', 'information-fields');
        const entries = [];
        let submitting = false;
        let archived = false;
        let archiveBadge;
        const controls = [];
        const register = control => { controls.push(control); return control; };

        (data.fields || []).slice(0, 5).forEach((field, index) => {
            const group = el('fieldset', 'information-field');
            const legend = el('legend');
            const label = el('span', '', field.label);
            window.HommeyMarkdown?.render(label, field.label, {inline: true});
            legend.append(label, el('small', '', field.required ? '必填' : '选填'));
            group.append(legend);
            const name = `${id}-${index}`;
            const labelId = `${name}-label`;
            label.id = labelId;
            let helpId;
            if (field.help_text) {
                const help = el('p', 'information-help', field.help_text);
                window.HommeyMarkdown?.render(help, field.help_text, {inline: true});
                helpId = `${name}-help`;
                help.id = helpId;
                group.append(help);
            }
            const associate = control => {
                control.setAttribute('aria-labelledby', labelId);
                control.setAttribute('aria-required', String(!!field.required));
                if (helpId) control.setAttribute('aria-describedby', helpId);
                return control;
            };
            let read;
            let valid = () => true;
            let customInput;
            let customSelected = () => false;
            const options = (field.options || []).slice(0, 8);
            if (['single_select', 'multi_select'].includes(field.input_type)) {
                const multiple = field.input_type === 'multi_select';
                let chosen;
                if (!multiple && options.length > 4) {
                    const select = register(associate(el('select', 'information-select')));
                    const placeholder = el('option', '', '请选择');
                    placeholder.value = '';
                    select.append(placeholder);
                    options.forEach((text, i) => {
                        const option = el('option', '', text);
                        option.value = String(i);
                        select.append(option);
                    });
                    if (field.allow_custom) {
                        const option = el('option', '', '自行填写'); option.value = 'custom'; select.append(option);
                    }
                    chosen = () => select.value && select.value !== 'custom' ? [options[Number(select.value)]] : [];
                    customSelected = () => select.value === 'custom';
                    group.append(select);
                } else {
                    const choices = el('div', 'information-choices');
                    const inputs = [];
                    const choice = (text, value) => {
                        const label = el('label', 'information-choice');
                        const input = register(el('input'));
                        input.type = multiple ? 'checkbox' : 'radio';
                        input.name = name;
                        input.value = value;
                        if (helpId) input.setAttribute('aria-describedby', helpId);
                        label.append(input, el('span', '', text));
                        choices.append(label);
                        return input;
                    };
                    options.forEach((text, i) => inputs.push(choice(text, String(i))));
                    chosen = () => inputs.filter(input => input.checked).map(input => options[Number(input.value)]);
                    if (field.allow_custom) {
                        const custom = choice('自行填写', 'custom');
                        customSelected = () => custom.checked;
                    }
                    group.append(choices);
                }
                if (field.allow_custom) {
                    customInput = register(el('input', 'information-input information-custom'));
                    customInput.type = 'text';
                    customInput.maxLength = 400;
                    customInput.placeholder = '填写你的答案';
                    customInput.setAttribute('aria-label', `${field.label}：自行填写`);
                    customInput.hidden = true;
                    group.append(customInput);
                }
                read = () => [...chosen(), ...(customSelected() && customInput?.value.trim() ? [customInput.value.trim()] : [])];
                valid = () => !customSelected() || !!customInput?.value.trim();
            } else {
                const input = register(associate(el('input', 'information-input')));
                input.type = field.input_type === 'date' ? 'date' : 'text';
                input.maxLength = 400;
                if (input.type === 'text') input.placeholder = '请填写';
                read = () => input.value.trim() ? [input.value.trim()] : [];
                valid = () => input.validity.valid;
                group.append(input);
            }
            entries.push({field, read, valid, group, customInput, customSelected});
            fields.append(group);
        });
        const footer = el('footer', 'information-footer');
        const copy = el('div');
        const status = el('p', 'information-status');
        status.setAttribute('role', 'status');
        copy.append(el('span', 'information-tip', '也可以直接在对话中回复'), status);
        const submit = el('button', 'information-submit', '确认并继续');
        submit.type = 'submit';
        footer.append(copy, submit);
        form.append(fields, footer);
        card.append(form);

        function update() {
            const missing = entries.filter(entry => !entry.valid() || (entry.field.required && !entry.read().length));
            submit.disabled = archived || submitting || missing.length > 0 || !entries.some(entry => entry.read().length);
            status.textContent = submitting ? '正在提交…' : missing.length ? `还有 ${missing.length} 项待补充` : '填写完成后，点击确认继续';
            controls.forEach(control => { control.disabled = archived || submitting; });
            entries.forEach(entry => {
                if (entry.customInput) {
                    entry.customInput.hidden = !entry.customSelected();
                    entry.customInput.disabled = archived || submitting || !entry.customSelected();
                }
            });
        }
        form.addEventListener('input', update);
        form.addEventListener('change', update);
        form.addEventListener('submit', event => {
            event.preventDefault();
            update();
            if (submit.disabled || !data.interaction_id) return;
            const lines = entries.filter(entry => entry.read().length)
                .map(entry => `${entry.field.label}：${entry.read().join('、')}`);
            const text = `补充信息（${data.title || '本次问题'}）：\n${lines.join('\n')}`;
            submitting = true;
            update();
            const detail = {text, source: 'information_request', card, interactionId: data.interaction_id,
                complete(success) {
                    submitting = false;
                    if (success) card.archive('已提交');
                    else if (!archived) {
                        update();
                        status.textContent = '提交未完成，内容已保留，可重试';
                    }
                }};
            if (!document.dispatchEvent(new CustomEvent('hommey:submit-message', {detail, cancelable: true}))) {
                submitting = false;
                update();
            }
        });
        card.archive = (label = '已归档') => {
            if (archived) {
                if (label === '已提交') archiveBadge.textContent = label;
                return;
            }
            archived = true;
            card.dataset.archived = 'true';
            card.classList.add('is-archived');
            const hadFocus = card.contains(document.activeElement);
            const details = el('details', 'information-archive');
            const summary = el('summary');
            const archivedHeading = el('span', '', data.title || '补充信息');
            archivedHeading.id = heading.id;
            archiveBadge = el('small', '', data.submitted_text ? '已提交' : label);
            summary.append(archivedHeading, archiveBadge);
            const body = el('div', 'information-archive-body');
            const selected = entries.filter(entry => entry.read().length)
                .map(entry => `${entry.field.label}：${entry.read().join('、')}`).join('\n');
            body.append(el('p', '', data.submitted_text || selected || (data.fields || []).map(field => field.label).join('、')),
                el('small', '', '此卡片仅供回看，继续补充请在对话中回复。'));
            details.append(summary, body);
            card.replaceChildren(details);
            if (hadFocus) summary.focus({preventScroll: true});
        };
        update();
        if (data.archived || !data.interaction_id) card.archive();
        return card;
    }
    window.HommeyInformationCard = {create};
}());
