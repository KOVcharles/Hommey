---
name: ask-question
description: Answer company business-travel policy, allowance, reimbursement, booking-rule, exception, and emergency-procedure questions using the internal RAG knowledge base. Use only for company travel questions; never use as general travel or city-guide search.
---

# 企业差旅制度问答

由 policy_rag 角色完成。先 search_policy 检索，再 read_source 回读，最后 report。
总共最多四轮：第一轮按需同轮检索不同费用类别，随后同轮回读相关来源，必要时继续回读，最后一轮提取报告。
检索、回读和报告阶段由运行器限制工具可见性，不在后续阶段重新检索。
report.data.findings 返回具体规则或金额（含币种、单位）、适用职级/城市/日期/例外及 evidence_refs。
summary 直接回答用户，不以“已查到/已确认”代替标准。不要把检索原文或日志粘贴进报告。
来源分页有 next_offset 时按需续读；尚未核实的条件标为未知，不把第一页当成完整制度。
保留问题中的城市、日期、费用类型、职级和例外条件；不要混用制度版本。
所有确定的限额、审批要求和报销规则都要有 evidence_refs。资料不足时明确缺项。
不得凭模型常识补制度，不执行预订、付款或审批提交。
