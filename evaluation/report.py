"""Dependency-free Markdown summary for the first shadow-evaluation rollout."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def build_markdown(data: dict[str, Any]) -> str:
    rows = data.get("rows") or []
    assistant_turns = int(data.get("assistant_turns") or 0)
    captured_subjects = len({str(row.get("subject_id")) for row in rows})
    statuses = Counter(str(row.get("status") or "unknown") for row in rows)
    verdicts = Counter(str(row.get("verdict") or "unscored") for row in rows if row.get("status") == "completed")
    reason_codes = Counter(
        str(code)
        for row in rows
        for code in _list(row.get("reason_codes"))
    )
    by_skill: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        skills = _list(row.get("selected_skills")) or ["unclassified"]
        for skill in skills:
            by_skill[str(skill)][str(row.get("verdict") or row.get("status") or "unknown")] += 1

    coverage = captured_subjects / assistant_turns if assistant_turns else 0.0
    lines = [
        f"# Turn Evaluation Report ({int(data.get('days') or 7)} days)",
        "",
        f"- Assistant turns: {assistant_turns}",
        f"- Captured subjects: {captured_subjects}",
        f"- Capture coverage: {coverage:.1%}",
        f"- Run status: {_format_counter(statuses)}",
        f"- Verdicts: {_format_counter(verdicts)}",
        "",
        "## Skills",
        "",
        "| Skill | Pass | Warning | Fail | Critical | Unscored |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for skill, counts in sorted(by_skill.items()):
        lines.append(
            f"| {skill} | {counts['pass']} | {counts['warning']} | {counts['fail']} | "
            f"{counts['critical_fail']} | {counts['unscored']} |"
        )
    lines.extend(["", "## Top reason codes", ""])
    lines.extend(
        f"- `{code}`: {count}" for code, count in reason_codes.most_common(10)
    )
    if not reason_codes:
        lines.append("- None")

    failures = [
        row for row in rows
        if row.get("verdict") in {"fail", "critical_fail"} or row.get("review_required")
    ][:20]
    lines.extend(["", "## Review queue", ""])
    lines.extend(
        f"- subject `{row.get('subject_id')}` / evaluation `{row.get('evaluation_id')}`: "
        f"{row.get('verdict') or row.get('error_code') or row.get('status')}"
        for row in failures
    )
    if not failures:
        lines.append("- Empty")
    return "\n".join(lines) + "\n"


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _format_counter(counter: Counter) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(counter.items())) or "none"
