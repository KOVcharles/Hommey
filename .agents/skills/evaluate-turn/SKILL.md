---
name: evaluate-turn
description: Evaluate one frozen business-travel assistant turn from supplied metadata and evidence. This post-processing skill is never a user intent and must not call business tools or mutate state.
---

# Turn quality evaluation

## Procedure

1. Treat the supplied metadata as the complete and immutable evaluation record.
2. Evaluate understanding, task progress, groundedness, safety, and clarity from 0 to 4.
3. Judge a `waiting_user` turn by whether its question is necessary and accurate, not by whether a final itinerary exists.
4. Ground policy and compliance findings only in `metadata.evidence.items` and `metadata.answer.sources`.
5. If the snapshot is insufficient or truncated in a material way, return `unscored` with `review_required=true`.
6. Attach every finding to a metadata field and, when relevant, to evidence identifiers.

## Boundaries

- Do not use general knowledge as company policy evidence.
- Do not call RAG, memory, MCP, web, or any business Skill.
- Do not modify a conversation, trip, preference, orchestration run, prompt, or knowledge base.
- Return only the output schema. Do not include chain-of-thought or hidden reasoning.
