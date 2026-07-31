"""Multimodal chat input (P0): document attachments + input normalization.

附件先规范化、模型按能力消费：附件被同步解析为文本，经 context_builder 拼入
agent_query 喂给既有 Agent/LLM；记忆与前端只保留简短的 display_message。
详见 docs/plans/2026-07-26-multimodal-input-plan.md §1.1 / §4.5。
"""
