"""Deterministic merge of rules and an optional Judge result."""
from __future__ import annotations

from evaluation.models import FinalEvaluationResult, JudgeResult, RuleResult


def decide(
    rules: list[RuleResult],
    judge: JudgeResult | None,
    *,
    judge_expected: bool,
) -> FinalEvaluationResult:
    critical = [item for item in rules if item.severity == "critical"]
    hard = [item for item in rules if item.hard]
    incomplete = any(item.code == "METADATA_INCOMPLETE" for item in rules)
    rule_codes = [item.code for item in rules]

    if critical:
        verdict = "critical_fail"
        score = min(judge.score, 20) if judge and judge.score is not None else 0
    elif hard:
        verdict = "fail"
        score = min(judge.score, 49) if judge and judge.score is not None else 40
    elif incomplete:
        verdict = "unscored"
        score = None
    elif judge is None and judge_expected:
        verdict = "unscored"
        score = None
    elif judge is None:
        verdict = "warning" if rules else "pass"
        score = 75 if rules else 100
    else:
        verdict = judge.verdict
        score = judge.score

    judge_codes = judge.reason_codes if judge else []
    critical_errors = [item.code for item in critical]
    if judge:
        critical_errors.extend(judge.critical_errors)
    explanation = (
        judge.summary if judge else
        "确定性规则检查完成。" if not rules else
        "；".join(item.message for item in rules)
    )
    return FinalEvaluationResult(
        verdict=verdict,
        score=score,
        dimension_scores=dict(judge.dimensions) if judge else {},
        reason_codes=list(dict.fromkeys(rule_codes + judge_codes)),
        critical_errors=list(dict.fromkeys(critical_errors)),
        rule_results=rules,
        explanation=explanation,
        review_required=bool(critical or incomplete or (judge is None and judge_expected))
        or bool(judge and judge.review_required),
    )
