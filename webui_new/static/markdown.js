(function () {
    'use strict';
    const inlineTags = ['strong', 'em', 'del', 'code', 'br', 'a'];
    const blockTags = [...inlineTags, 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li', 'blockquote', 'pre', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td'];
    let parser;

    function markdownParser() {
        if (!parser) parser = new window.marked.Marked({
            gfm: true, breaks: true,
            // Render task status without adding interactive form elements.
            renderer: {checkbox: ({checked}) => checked ? '☑ ' : '☐ '},
            extensions: [{
                name: 'cjkStrong', level: 'inline',
                start: source => source.indexOf('**'),
                tokenizer(source) {
                    // CommonMark flanking rules reject **职称：**正高 and
                    // **正高（教授）**人员. Accept paired CJK bold locally in
                    // the inline lexer, never by replacing raw source/HTML.
                    const match = /^\*\*(?!\*)((?:\\.|[^*\\\n]|\*(?!\*))+?)\*\*(?!\*)/.exec(source);
                    if (match && /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(match[1])
                        && match[1] === match[1].trim()) {
                        return {type: 'cjkStrong', raw: match[0], tokens: this.lexer.inlineTokens(match[1])};
                    }
                },
                renderer(token) { return `<strong>${this.parser.parseInline(token.tokens)}</strong>`; },
            }],
        });
        return parser;
    }

    function render(node, value, {inline = false} = {}) {
        const text = String(value ?? '');
        node.classList.add(inline ? 'hommey-markdown-inline' : 'hommey-markdown');
        // A missing asset must never turn model HTML into executable markup.
        if (!window.marked || !window.DOMPurify) {
            node.textContent = text;
            return node;
        }
        const html = inline ? markdownParser().parseInline(text) : markdownParser().parse(text);
        const fragment = window.DOMPurify.sanitize(html, {
            ALLOWED_TAGS: inline ? inlineTags : blockTags,
            ALLOWED_ATTR: ['href', 'title', 'start'],
            ALLOW_DATA_ATTR: false,
            ALLOW_ARIA_ATTR: false,
            RETURN_DOM_FRAGMENT: true,
        });
        fragment.querySelectorAll('a').forEach(link => {
            try {
                const url = new URL(link.getAttribute('href') || '', window.location.href);
                if (!link.hasAttribute('href') || !['http:', 'https:'].includes(url.protocol)) throw new Error('Unsupported link');
                link.href = url.href;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
            } catch (_) {
                link.replaceWith(...link.childNodes);
            }
        });
        fragment.querySelectorAll('table').forEach(table => {
            const wrapper = document.createElement('div');
            wrapper.className = 'markdown-table-scroll';
            wrapper.tabIndex = 0;
            wrapper.setAttribute('role', 'region');
            wrapper.setAttribute('aria-label', '表格，可横向滚动');
            table.replaceWith(wrapper);
            wrapper.appendChild(table);
        });
        node.replaceChildren(fragment);
        return node;
    }
    window.HommeyMarkdown = {render};
}());
