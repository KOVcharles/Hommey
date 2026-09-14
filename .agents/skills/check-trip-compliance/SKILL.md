---
name: check-trip-compliance
description: Check a proposed or current company business trip against retrieved internal travel-policy evidence. Use for requests about whether transport class, lodging cost, allowance, approval, itinerary, or reimbursement preparation complies with company rules. Return unknown rather than guessing when RAG evidence is missing.
---

# 检查差旅合规

由 compliance 角色检查明确传入的方案和 policy_rag 证据。
按人员、城市、日期、交通等级、住宿金额和审批条件逐项核对。
使用 report，data.verdict 为 compliant / non_compliant / partial / unknown。
data.checks 每项包含 item、status、reason、evidence_refs。
缺少适用制度或关键条件时使用 unknown，不能以常识补限额或审批结论。
只提供核对和材料建议，不批准、提交审批或报销。
