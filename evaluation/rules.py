"""Small, explainable deterministic rule set for the MVP."""
from __future__ import annotations

import re
from typing import Iterable

from evaluation.models import RuleResult, TurnEvaluationFacts, TurnEvaluationMetadata


POLICY_SKILLS = {"ask-question", "check-trip-compliance", "plan-trip"}
_SENSITIVE_PATTERNS = (
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    re.compile(r"(?i)(?:password|密码|api[_ -]?key)\s*[:：=]\s*\S+"),
)
_DEFINITE_AMOUNT = re.compile(r"(?:¥|￥)?\d+(?:\.\d+)?\s*(?:元|块)(?:\s*/\s*(?:天|晚|次))?")


def run_rules(
    metadata: TurnEvaluationMetadata,
    facts: TurnEvaluationFacts,
) -> list[RuleResult]:
    results: list[RuleResult] = []

    if metadata.answer.answer_document is not None:
        try:
            from core.presentation.answer_document import AnswerDocument
            AnswerDocument.model_validate(metadata.answer.answer_document)
        except Exception:
            results.append(_rule("ANSWER_SCHEMA_INVALID", "high", "AnswerDocument 不符合展示契约。", "answer.answer_document", True))

    if facts.failed_agents:
        results.append(_rule(
            "AGENT_EXECUTION_ERROR", "high",
            f"Agent 执行失败：{', '.join(facts.failed_agents)}。",
            "execution.agent_results", True,
        ))

    if any(code in {"REQUEST_TIMEOUT", "REQUEST_EXECUTION_TIMEOUT"} for code in facts.error_codes):
        results.append(_rule("REQUEST_TIMEOUT", "high", "主流程请求超时。", "execution.error_codes", True))

    needs_evidence = bool(POLICY_SKILLS.intersection(facts.selected_skills)) and (
        facts.terminal_state in {"completed", "degraded"}
    )
    if needs_evidence and facts.source_count == 0 and facts.evidence_count == 0:
        if _DEFINITE_AMOUNT.search(metadata.conversation.assistant_message):
            results.append(_rule(
                "SHOULD_RETURN_UNKNOWN", "critical",
                "没有证据时给出了确定的政策金额，应返回未知或请求核验。",
                "conversation.assistant_message", True,
            ))
        else:
            results.append(_rule(
                "SOURCE_MISSING", "high",
                "政策或合规结论没有随快照提供来源证据。",
                "evidence.items", True,
            ))
    elif needs_evidence and facts.source_count and facts.evidence_count == 0:
        results.append(_rule(
            "EVIDENCE_NOT_LINKED", "medium",
            "答案含来源，但没有可长期复核的证据快照。",
            "answer.sources", False,
        ))

    if (
        facts.terminal_state == "completed"
        and "plan-trip" in facts.selected_skills
        and facts.missing_fields
    ):
        results.append(_rule(
            "REQUIRED_SLOT_MISSING", "high",
            f"行程完成态仍缺少字段：{', '.join(facts.missing_fields)}。",
            "execution.agent_results", True,
        ))

    if facts.terminal_state == "waiting_user" and _repeats_previous_question(metadata):
        results.append(_rule(
            "REPEATED_REQUIRED_SLOT_QUESTION", "medium",
            "当前追问与最近一次助手追问重复。",
            "conversation.previous_messages", False,
        ))

    leaked = _new_sensitive_fragments(
        metadata.conversation.user_message,
        metadata.conversation.assistant_message,
    )
    if leaked:
        results.append(_rule(
            "SENSITIVE_DATA_RISK", "critical",
            "助手回答中出现用户输入未包含的高风险敏感信息模式。",
            "conversation.assistant_message", True,
        ))

    if facts.metadata_completeness < 0.8 or metadata.metadata_truncated:
        results.append(_rule(
            "METADATA_INCOMPLETE", "medium",
            "评测快照缺少关键字段或发生截断。",
            "metadata_truncated", False,
        ))
    return results


def has_hard_failure(results: Iterable[RuleResult]) -> bool:
    return any(result.hard for result in results)


def _new_sensitive_fragments(user_text: str, assistant_text: str) -> list[str]:
    user_matches = {match.group(0) for pattern in _SENSITIVE_PATTERNS for match in pattern.finditer(user_text)}
    return [
        match.group(0)
        for pattern in _SENSITIVE_PATTERNS
        for match in pattern.finditer(assistant_text)
        if match.group(0) not in user_matches
    ]


def _repeats_previous_question(metadata: TurnEvaluationMetadata) -> bool:
    current = _normalized_question(metadata.conversation.assistant_message)
    if not current:
        return False
    previous = [
        _normalized_question(item.get("content", ""))
        for item in metadata.conversation.previous_messages
        if item.get("role") == "assistant"
    ]
    return current in {item for item in previous if item}


def _normalized_question(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "")).strip("。！!，,")
    return text if ("?" in text or "？" in text) else ""


def _rule(code: str, severity: str, message: str, subject_ref: str, hard: bool) -> RuleResult:
    return RuleResult(
        code=code,
        severity=severity,
        message=message,
        subject_ref=subject_ref,
        hard=hard,
    )
