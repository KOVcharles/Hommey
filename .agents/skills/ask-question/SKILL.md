---
name: ask-question
description: Answer company business-travel policy, allowance, reimbursement, booking-rule, exception, and emergency-procedure questions using the internal RAG knowledge base. Use only for company travel questions; never use as general travel or city-guide search.
---

# Answer Company Travel Questions

Use `rag_retrieval` as the only authority for company rules.

## Workflow

1. Rewrite the question into the relevant policy concepts without removing destination, expense type, dates, or exception conditions.
2. Retrieve company travel documents.
3. Answer only from relevant evidence.
4. Return “知识库中没有相关信息” when evidence is missing or unrelated.
5. Preserve source metadata: file name, page, section, and excerpt when available.
6. Never infer numeric limits, approval requirements, allowed transport class, or reimbursement eligibility from model knowledge.

Return an answer and normalized sources. Do not execute booking, payment, approval, or reimbursement submission.
