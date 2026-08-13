"""Line-level block builder shared by the TXT/MD and PDF text parsers.

Implements the audit's P1/P2/P4 principles:
- P1: parsers produce a ``Block`` sequence (never leave raw text to the chunker).
- P2: headings are *paths*, not leaves — a heading block carries the heading
  stack as ``heading_path`` and every following content block inherits it.
- P4: heading recognition is the registry in ``rag/heading_rules.py``, not a
  set of regexes buried in the chunker.

FAQ records (``Q1: … A1: …``) are treated as complete self-contained content
blocks (P2's explicit exception: a record that is itself answerable).
"""
from __future__ import annotations

import re
from typing import List, Optional

from .heading_rules import match_heading, match_setext_underline
from .schemas import Block, BLOCK_TYPE_CODE, BLOCK_TYPE_FAQ, BLOCK_TYPE_HEADING, BLOCK_TYPE_LIST, BLOCK_TYPE_PARAGRAPH, BLOCK_TYPE_TABLE, _block_id

class HeadingStack:
    """Reusable heading-path accumulator for structured parsers.

    DOCX paragraphs arrive in document order (not as flat text lines), so the
    line-walker's closure-based stack is not reusable.  This class implements
    the same P2 semantics: a heading of level N pops any open heading of level
    >= N, then pushes itself, and every following content block reads the
    accumulated path.
    """

    def __init__(self) -> None:
        self._stack: List[List[object]] = []  # list of (level:int, name:str)

    def current_path(self) -> List[str]:
        return [str(item[1]) for item in self._stack]

    def push(self, level: int, name: str) -> None:
        while self._stack and int(self._stack[-1][0]) >= level:
            self._stack.pop()
        self._stack.append([level, name])


_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FAQ_Q_RE = re.compile(r"^Q\d+\s*[:：]", re.IGNORECASE)
_LIST_MARKER_RE = re.compile(r"^\s*[-*•]\s+\S")
_INDENT_RE = re.compile(r"^\s+")

_STRUCTURAL_BOUNDARY_RE = re.compile(
    r"^(?:```|```\w*|\s*\|)|^#{1,6}\s+"
)


def parse_text_blocks(text: str, *, page_number: int = 1, file_type: str = "txt") -> List[Block]:
    """Split decoded ``text`` into structural blocks for one page."""
    return _parse_blocks(text.splitlines(), [], 0, page_number, file_type)


def _parse_blocks(
    lines: List[str],
    blocks: List[Block],
    seq: int,
    page_number: int,
    file_type: str,
) -> List[Block]:
    """Iterative line walker.  ``stack`` holds (level:int, name:str) pairs."""
    stack: List[List[object]] = []  # list of (level:int, name:str)
    total = len(lines)
    index = 0

    def flush_stack_level(level: int) -> None:
        while stack and int(stack[-1][0]) >= level:
            stack.pop()

    def current_path() -> List[str]:
        return [str(item[1]) for item in stack]

    def add_block(block_type: str, text: str, level: int = 0) -> None:
        nonlocal seq
        seq += 1
        blocks.append(
            Block(
                block_id=_block_id(page_number, seq),
                block_type=block_type,
                text=text,
                level=level,
                heading_path=current_path(),
            )
        )

    def add_heading(level: int, name: str) -> None:
        nonlocal seq
        flush_stack_level(level)
        stack.append([level, name])
        seq += 1
        blocks.append(
            Block(
                block_id=_block_id(page_number, seq),
                block_type=BLOCK_TYPE_HEADING,
                text=name,
                level=level,
                heading_path=current_path(),
            )
        )

    def is_heading_line(line: str, ftype: str) -> bool:
        return match_heading(line, ftype) is not None

    while index < total:
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        # Markdown ATX heading (needs real depth).
        if file_type == "md":
            atx = _ATX_RE.match(stripped)
            if atx:
                add_heading(len(atx.group(1)), atx.group(2).strip())
                index += 1
                continue

        # Setext heading: current line + underline on the next line.  Setext
        # is a Markdown construct; a TXT/PDF line followed by "----" is a
        # table/section separator, not a heading.
        if file_type == "md" and index + 1 < total:
            underline_level = match_setext_underline(lines[index + 1])
            if underline_level is not None:
                add_heading(underline_level, stripped)
                index += 2
                continue

        # Fenced code block.
        if stripped.startswith("```"):
            code_lines = [line]
            index += 1
            while index < total and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            if index < total:
                code_lines.append(lines[index])
                index += 1
            add_block(BLOCK_TYPE_CODE, "\n".join(code_lines).strip())
            continue

        # Registry heading.
        heading = match_heading(line, file_type)
        if heading is not None:
            level, name = heading
            add_heading(level, name)
            index += 1
            continue

        # FAQ record: Q line plus its answer until the next Q/heading/boundary.
        if _FAQ_Q_RE.match(stripped):
            faq_lines = [line]
            index += 1
            while index < total:
                nxt = lines[index].strip()
                if not nxt:
                    index += 1
                    continue
                if _FAQ_Q_RE.match(nxt) or is_heading_line(nxt, file_type):
                    break
                if nxt.startswith("```"):
                    break
                faq_lines.append(lines[index])
                index += 1
            add_block(BLOCK_TYPE_FAQ, "\n".join(faq_lines).strip())
            continue

        # Markdown table run.
        if stripped.startswith("|") and file_type == "md":
            table_lines = [line]
            index += 1
            while index < total:
                nxt = lines[index].strip()
                if not nxt:
                    break
                if nxt.startswith("|"):
                    table_lines.append(lines[index])
                    index += 1
                    continue
                break
            add_block(BLOCK_TYPE_TABLE, "\n".join(table_lines).strip())
            continue

        # List run: dash/star items plus indented continuations.
        if _LIST_MARKER_RE.match(stripped):
            list_lines = [line]
            index += 1
            while index < total:
                nxt_raw = lines[index]
                nxt = nxt_raw.strip()
                if not nxt:
                    break
                if is_heading_line(nxt, file_type) or nxt.startswith("```") or nxt.startswith("|"):
                    break
                if _LIST_MARKER_RE.match(nxt):
                    list_lines.append(nxt_raw)
                    index += 1
                    continue
                if _INDENT_RE.match(nxt_raw):
                    list_lines.append(nxt_raw)
                    index += 1
                    continue
                break
            add_block(BLOCK_TYPE_LIST, "\n".join(list_lines).strip())
            continue

        # Paragraph: accumulate until a structural boundary or blank line.
        para_lines = [line]
        index += 1
        while index < total:
            nxt_raw = lines[index]
            nxt = nxt_raw.strip()
            if not nxt:
                break
            if is_heading_line(nxt, file_type):
                break
            if nxt.startswith("```") or (nxt.startswith("|") and file_type == "md"):
                break
            if _FAQ_Q_RE.match(nxt) or _LIST_MARKER_RE.match(nxt):
                break
            para_lines.append(nxt_raw)
            index += 1
        add_block(BLOCK_TYPE_PARAGRAPH, "\n".join(para_lines).strip())

    return blocks
