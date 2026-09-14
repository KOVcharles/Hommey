/* Public runtime snapshots only. No model text or HTML is interpreted here. */
(function (root) {
    'use strict';
    const labels = {pending: '稍后', running: '进行中', succeeded: '已完成', partial: '待完善',
        needs_input: '待补充', failed: '暂未完成', skipped: '暂未开展', cancelled: '已停止',
        completed: '完成', degraded: '部分步骤未完成', waiting_input: '等待补充'};
    function accept(previous, next) {
        return !!next && typeof next.run_id === 'string' && Number.isSafeInteger(next.revision)
            && next.revision > 0 && Array.isArray(next.steps) && next.steps.length <= 32
            && (!previous || (previous.run_id === next.run_id && next.revision > previous.revision));
    }
    const titles = {'整理并保存出差信息': '确认行程信息', '查询差旅标准': '查阅差旅标准',
        '整理天气与交通信息': '查询天气与交通', '核对差旅记录与偏好': '核对出行偏好',
        '生成出差行程': '安排行程', '检查差旅合规': '核对出行标准'};
    function visibleSteps(plan) {
        const unique = new Map();
        for (const step of plan.steps) unique.set(titles[step.title] || step.title, step);
        return [...unique].map(([title, step]) => ({...step, title}));
    }
    function headline(plan) {
        if (plan.status === 'running') {
            const active = visibleSteps(plan).find(step => step.status === 'running');
            return active ? `正在${active.title}` : '正在整理你的需求';
        }
        return {completed: '已为你整理好', waiting_input: '补充后，继续安排', needs_input: '补充后，继续安排',
            partial: '部分信息待完善', degraded: '部分信息暂未查到', failed: '这次未能完成',
            cancelled: '已暂停'}[plan.status] || '进度已更新';
    }
    function node(tag, className, text) {
        const el = document.createElement(tag);
        el.className = className;
        if (text) el.textContent = text;
        return el;
    }
    function update(container, plan) {
        if (!container || !accept(null, plan)) return;
        let card = Array.from(container.querySelectorAll('.execution-plan')).find(el => el.dataset.runId === plan.run_id);
        if (card && !accept(card.planSnapshot, plan)) return;
        if (!card) {
            card = node('details', 'execution-plan');
            card.dataset.runId = plan.run_id;
            const heading = node('summary', 'execution-plan-heading');
            const logo = node('span', 'hommey-card-logo');
            logo.setAttribute('aria-hidden', 'true');
            const text = node('span', 'execution-plan-title');
            text.setAttribute('role', 'status');
            const indicator = node('span', 'execution-plan-dot');
            indicator.setAttribute('aria-hidden', 'true');
            heading.append(logo, text, indicator, node('span', 'execution-plan-toggle', '查看进展'));
            // The disclosure animates through a grid-rows transition, so the steps stay in the
            // DOM while collapsed. inert keeps them out of the tab order and the a11y tree,
            // which is what a closed <details> would otherwise do for us.
            const inner = node('div', 'execution-plan-body-inner');
            inner.appendChild(node('ol', 'execution-plan-steps'));
            const body = node('div', 'execution-plan-body');
            body.appendChild(inner);
            body.inert = true;
            card.append(heading, body);
            card.addEventListener('toggle', () => {
                card.querySelector('.execution-plan-toggle').textContent = card.open ? '收起进展' : '查看进展';
                body.inert = !card.open;
            });
            container.appendChild(card);
        }
        // Keep the user's disclosure choice through streaming updates.
        card.planSnapshot = plan;
        card.dataset.status = plan.status;
        card.querySelector('.execution-plan-title').textContent = headline(plan);
        const steps = visibleSteps(plan);
        const list = card.querySelector('ol');
        list.replaceChildren();
        if (!steps.length) list.appendChild(node('li', '', '正在确认需要准备的信息'));
        for (const step of steps) {
            const row = node('li', 'execution-plan-step');
            row.dataset.status = step.status;
            row.append(node('strong', '', step.title), node('span', '', labels[step.status] || '待确认'));
            list.appendChild(row);
        }
    }
    function connectionLost(container, runId) {
        const card = Array.from(container.querySelectorAll('.execution-plan')).find(el => el.dataset.runId === runId);
        if (card?.planSnapshot?.status === 'running') {
            card.dataset.status = 'disconnected';
            card.querySelector('.execution-plan-title').textContent = '连接中断 · 进度待确认';
        }
    }
    root.ExecutionPlan = {update, accept, connectionLost};
    if (typeof module !== 'undefined') module.exports = {accept, update, connectionLost};
})(typeof window === 'undefined' ? globalThis : window);
