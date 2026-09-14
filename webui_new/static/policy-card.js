(function () {
    'use strict';
    const el = (tag, cls, text) => { const node = document.createElement(tag); node.className = cls || ''; if (text != null) node.textContent = text; return node; };
    // Highlight literal amounts only. Eligibility and conditions stay verbatim.
    function factText(value) {
        const node = el('p', 'policy-rule');
        String(value || '').split(/(\d+(?:\.\d+)?\s*元(?:\s*[/／]\s*(?:晚|天|人|次))?)/g).forEach((part, i) => node.append(i % 2 ? el('strong', 'policy-amount', part) : document.createTextNode(part)));
        return node;
    }
    function content(data) {
        const root = el('div', 'policy-content');
        const sections = (data.sections || []).filter(s => s.kind === 'policy');
        const items = sections.flatMap(s => s.items || []);
        const costs = el('div', 'policy-costs'), rules = el('div', 'policy-rules');
        items.forEach(item => {
            const cost = /住宿|餐补|餐费/.test(item.label) && !/报销|核算|通用/.test(item.label);
            const node = el('div', cost ? 'policy-cost' : 'policy-row');
            node.append(el('h4', '', item.label), factText(item.value));
            const detail = String(item.detail || '').replace(/\n?来源[：:][\s\S]*/, '').trim();
            if (detail && !item.value.includes(detail)) node.append(el('p', 'policy-applicability', detail));
            (cost ? costs : rules).append(node);
        });
        if (costs.childElementCount) root.append(costs);
        if (rules.childElementCount) root.append(rules);
        if (!items.length) root.append(el('p', 'policy-unavailable', '本次尚未取得可核实的差旅标准。'));
        const bodies = [...new Set(sections.map(s => s.body).filter(Boolean))];
        if (bodies.length) {
            const more = el('details', 'policy-more');
            more.append(el('summary', '', '适用说明与待确认条件'));
            bodies.forEach(body => more.append(el('p', '', body))); root.append(more);
        }
        return root;
    }
    function sources(data) {
        const root = el('details', 'policy-more policy-sources');
        const rows = [...new Map((data.sources || []).map(s => [s.title + s.detail, s])).values()];
        root.append(el('summary', '', `资料来源 · ${rows.length} 项`));
        rows.forEach(source => {
            const row = el('div', 'policy-source');
            row.append(el('strong', '', String(source.title).replace(/\s*·?\s*src_[a-z0-9]+/g, '')));
            if (source.detail) row.append(el('span', '', source.detail));
            root.append(row);
        });
        return root;
    }
    function create(data) {
        const card = el('article', 'policy-card'); card.setAttribute('aria-label', '差旅标准');
        const header = el('header', 'policy-header');
        const partial = (data.sections || []).some(s => s.status !== 'success');
        header.append(el('div', 'policy-eyebrow', 'HOMMEY / 差旅制度'), el('h2', '', data.title === '企业差旅助手' ? '差旅标准' : data.title), el('p', '', partial ? '部分标准或适用条件待确认 · 已核实条款如下' : '按费用查看标准，适用职级与例外条件随条款列示。'));
        card.append(header, content(data));
        if (data.sources?.length) card.append(sources(data));
        return card;
    }
    window.HommeyPolicyCard = { content, sources, create };
})();
