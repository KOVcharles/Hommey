(function () {
    'use strict';
    const el = (tag, cls, text) => { const node = document.createElement(tag); node.className = cls || ''; if (text != null) node.textContent = text; return node; };
    const rich = (tag, cls, text, inline = true) => {
        const node = el(tag, cls, text);
        window.HommeyMarkdown?.render(node, text, {inline});
        return node;
    };
    // Highlight literal amounts only. Eligibility and conditions stay verbatim.
    function factText(value) {
        if (window.HommeyMarkdown) {
            const node = rich('div', 'policy-rule', value, false);
            // Apply existing amount emphasis only to text, preserving Markdown
            // links, code and explicit emphasis as already rendered.
            const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
            const texts = [];
            while (walker.nextNode()) texts.push(walker.currentNode);
            texts.forEach(text => {
                if (text.parentElement.closest('a,code,pre,strong')) return;
                const parts = text.textContent.split(/(\d+(?:\.\d+)?\s*元(?:\s*[/／]\s*(?:晚|天|人|次))?)/g);
                if (parts.length > 1) text.replaceWith(...parts.map((part, i) => i % 2 ? el('strong', 'policy-amount', part) : document.createTextNode(part)));
            });
            return node;
        }
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
            node.append(rich('h4', '', item.label), factText(item.value));
            const detail = String(item.detail || '').replace(/\n?来源[：:][\s\S]*/, '').trim();
            if (detail && !String(item.value || '').includes(detail)) node.append(rich('div', 'policy-applicability', detail, false));
            (cost ? costs : rules).append(node);
        });
        if (costs.childElementCount) root.append(costs);
        if (rules.childElementCount) root.append(rules);
        if (!items.length) root.append(el('p', 'policy-unavailable', '本次尚未取得可核实的制度条款。'));
        const bodies = [...new Set(sections.map(s => s.body).filter(Boolean))];
        if (bodies.length) {
            const more = el('details', 'policy-more');
            more.append(el('summary', '', '适用说明与待确认条件'));
            bodies.forEach(body => more.append(rich('div', '', body, false))); root.append(more);
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
        const card = el('article', 'policy-card'); card.setAttribute('aria-label', '报销与差旅制度');
        const header = el('header', 'policy-header');
        const partial = (data.sections || []).some(s => s.status !== 'success');
        header.append(el('div', 'policy-eyebrow', 'HOMMEY / 制度咨询'), rich('h2', '', ['企业差旅助手', '报销与差旅助手'].includes(data.title) ? '报销与差旅制度' : data.title), el('p', '', partial ? '部分要求或适用条件待确认 · 已核实条款如下' : '报销要求与适用条件随条款列示。'));
        card.append(header, content(data));
        if (data.sources?.length) card.append(sources(data));
        return card;
    }
    window.HommeyPolicyCard = { content, sources, create };
})();
