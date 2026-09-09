(function () {
    'use strict';
    const token = localStorage.getItem('hommey.access_token');
    const app = document.getElementById('app');
    const error = document.getElementById('error');
    let state = { skills: [] };

    document.addEventListener('DOMContentLoaded', load);

    async function api(path, options = {}) {
        const response = await fetch(path, {
            ...options,
            headers: { 'Authorization': `Bearer ${token || ''}`, 'Content-Type': 'application/json', ...(options.headers || {}) },
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error?.message || '请求失败');
        return data;
    }

    async function load() {
        if (!token) return fail('请先以管理员身份登录。');
        try {
            state = await api('/api/admin/skills');
            app.hidden = false;
            error.textContent = '';
            renderList();
            if (state.skills.length) selectSkill(state.skills[0].name);
        } catch (exc) { fail(exc.message); }
    }

    function fail(message) { app.hidden = true; error.textContent = message; }

    function renderList() {
        const list = document.getElementById('skillList');
        list.replaceChildren();
        document.getElementById('skillCount').textContent = `${state.skills.length} 个`;
        state.skills.forEach((skill) => {
            const card = document.createElement('article');
            card.className = 'skill-card'; card.dataset.name = skill.name;
            const top = document.createElement('div'); top.className = 'skill-top';
            const title = document.createElement('strong'); title.textContent = skill.display_name;
            const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = '业务指南';
            const id = document.createElement('div'); id.className = 'meta'; id.textContent = `${skill.name} · v${skill.version} · ${skill.category}`;
            top.append(title, badge); card.append(top, id); card.addEventListener('click', () => selectSkill(skill.name)); list.appendChild(card);
        });
    }

    async function selectSkill(name) {
        document.querySelectorAll('.skill-card').forEach((item) => item.classList.toggle('active', item.dataset.name === name));
        const skill = await api(`/api/admin/skills/${encodeURIComponent(name)}`);
        const detail = document.getElementById('skillDetail'); detail.replaceChildren();
        const title = document.createElement('h2'); title.textContent = skill.display_name;
        const desc = document.createElement('p'); desc.className = 'meta'; desc.textContent = skill.description;
        const grid = document.createElement('div'); grid.className = 'detail-grid';
        grid.append(metric('标识', skill.name), metric('版本', skill.version), metric('使用角色', (skill.roles || []).join('、') || '独立评测器'));
        const heading = document.createElement('h3'); heading.textContent = '业务指导'; heading.style.marginTop = '18px';
        const pre = document.createElement('pre'); pre.textContent = skill.instructions || '无';
        detail.append(title, desc, grid, heading, pre);
    }

    function metric(label, value) { const box=document.createElement('div'); box.className='metric'; const small=document.createElement('span'); small.className='meta'; small.textContent=label; const strong=document.createElement('strong'); strong.textContent=value ?? '-'; box.append(small,strong); return box; }

}());
