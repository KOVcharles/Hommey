---
name: ask-question
description: Answer company business-travel policy, allowance, reimbursement, booking-rule, exception, and emergency-procedure questions using the internal RAG knowledge base. Use only for company travel questions; never use as general travel or city-guide search.
---

# 企业差旅制度问答

由 policy_rag 角色完成。先 search_policy 检索，再 read_source 回读，最后 report。
保留问题中的城市、日期、费用类型、职级和例外条件；不要混用制度版本。
所有确定的限额、审批要求和报销规则都要有 evidence_refs。资料不足时明确缺项。
不得凭模型常识补制度，不执行预订、付款或审批提交。
