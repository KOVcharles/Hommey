"""Heading recognition rule registry (audit §7 原则 P4).

Topic detection is a *registry*, not a handful of hard-coded regexes buried in
the chunker.  Each rule declares its regex, the heading level it implies, and
which document formats it applies to.  Adding a new format or a new numbering
scheme means registering a rule, never editing the chunker or block parser
control flow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class HeadingRule:
    name: str
    level: int
    pattern: re.Pattern
    formats: Tuple[str, ...] = ("txt", "md")

    def match(self, line: str, file_type: str) -> Optional[str]:
        """Return the heading *text* (without markers) if the line is a match."""
        if file_type not in self.formats:
            return None
        matched = self.pattern.match(line.strip())
        if not matched:
            return None
        return matched.group(1).strip()


# Ordered by specificity: more specific rules are consulted first.
HEADING_RULES: List[HeadingRule] = [
    HeadingRule(
        name="markdown_atx",
        level=1,
        pattern=re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$"),
        formats=("md",),
    ),
    HeadingRule(
        name="chinese_chapter",
        level=1,
        pattern=re.compile(r"^第[一二三四五六七八九十百零〇\d]+[章节部分篇]\s*(.*)$"),
        formats=("txt", "md", "pdf", "docx"),
    ),
    # The dot separators must be followed by whitespace so a decimal amount
    # like "12.5 元" or "1.5倍" is never misread as a section number.  The
    # enumeration comma "、" needs no space ("一、住宿标准").
    HeadingRule(
        name="chinese_section",
        level=1,
        pattern=re.compile(r"^(?:[一二三四五六七八九十百]+[、]|[一二三四五六七八九十百]+[.．]\s+)(.+)$"),
        formats=("txt", "md", "pdf", "docx"),
    ),
    HeadingRule(
        name="chinese_subsection",
        level=2,
        pattern=re.compile(r"^[（(][一二三四五六七八九十百]+[）)]\s*(.+)$"),
        formats=("txt", "md", "pdf", "docx"),
    ),
    HeadingRule(
        name="numbered_heading",
        level=3,
        pattern=re.compile(r"^(?:\d+[、]|\d+[.．]\s+)(.+)$"),
        formats=("txt", "md", "pdf", "docx"),
    ),
    HeadingRule(
        name="numbered_parenthesized",
        level=4,
        pattern=re.compile(r"^[（(]\d+[）)]\s*(.+)$"),
        formats=("txt", "md", "pdf", "docx"),
    ),
]

# Setext headings need a lookahead at the next line, so they cannot be a plain
# line rule.  Level is decided by the underline: "===" -> 1, "---" -> 2.
SETEXT_LEVEL_RE = {
    "=": 1,
    "-": 2,
}
SETEXT_UNDERLINE_RE = re.compile(r"^\s*(=+|-+)\s*$")


def match_heading(line: str, file_type: str) -> Optional[Tuple[int, str]]:
    """Return (level, heading_text) or None for a heading line."""
    for rule in HEADING_RULES:
        text = rule.match(line, file_type)
        if text is not None:
            return rule.level, text
    return None


def match_setext_underline(line: str) -> Optional[int]:
    """Return the setext level if ``line`` is an underline (=== or ---)."""
    matched = SETEXT_UNDERLINE_RE.match(line)
    if not matched:
        return None
    return SETEXT_LEVEL_RE[matched.group(1)[0]]
