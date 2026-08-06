---
name: check-trip-compliance
description: Check a proposed or current company business trip against retrieved internal travel-policy evidence. Use for requests about whether transport class, lodging cost, allowance, approval, itinerary, or reimbursement preparation complies with company rules. Return unknown rather than guessing when RAG evidence is missing.
---

# Check Trip Compliance

Check only against evidence returned from the company travel knowledge base.

## Workflow

1. Read the structured trip facts from `event_collection` or the active trip context.
2. Read policy evidence from `rag_knowledge`; never replace missing evidence with model knowledge.
3. Check transport, lodging, allowance, approval, schedule, and reimbursement only when relevant facts exist.
4. Mark each item `compliant`, `non_compliant`, or `unknown`.
5. Include the supporting source for every definite conclusion.
6. Put missing trip facts or policy evidence in `missing_info` and `unknown_items`.
7. Give advice only. Do not approve, book, pay, or submit anything.

Read [references/evidence-rules.md](references/evidence-rules.md) when normalizing RAG sources or resolving conflicting evidence.

Return JSON matching `schemas/output.json`.
