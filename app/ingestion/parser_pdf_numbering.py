"""Split normalized PDF text on academic-style numbered major sections (1., 2., …)."""
from __future__ import annotations

import logging
import re

from app.ingestion.parser_html import Section

logger = logging.getLogger(__name__)

_MAJOR_NUM_ONLY = re.compile(r"^(\d+)\.(?!\d)\s*$")
_MAJOR_SAME_LINE = re.compile(r"^(\d+)\.(?!\d)\s+(.+)$")


def _find_chapter_starts(lines: list[str]) -> list[tuple[int, str]]:
    starts: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        m1 = _MAJOR_SAME_LINE.match(s)
        if m1:
            num_s, rest = m1.group(1), m1.group(2).strip()
            if not rest or not rest[0].isupper() or len(rest) > 150 or ". " in rest[:55]:
                i += 1
                continue
            try:
                n = int(num_s)
            except ValueError:
                i += 1
                continue
            if not 1 <= n <= 30 or rest[0].isdigit():
                i += 1
                continue
            starts.append((i, f"{num_s}. {rest}"))
            i += 1
            continue
        m0 = _MAJOR_NUM_ONLY.match(s)
        if m0:
            try:
                n = int(m0.group(1))
            except ValueError:
                i += 1
                continue
            if not 1 <= n <= 30:
                i += 1
                continue
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines):
                i += 1
                continue
            nxt = lines[j].strip()
            if not nxt or not nxt[0].isupper() or len(nxt) > 150:
                i += 1
                continue
            if ". " in nxt[:55] and not nxt.startswith("Abstract"):
                i += 1
                continue
            starts.append((i, f"{m0.group(1)}. {nxt}"))
            i = j + 1
            continue
        i += 1
    return starts


def split_by_numbered_chapters(text: str, label: str) -> list[Section] | None:
    lines = text.split("\n")
    starts = _find_chapter_starts(lines)
    if len(starts) < 2:
        return None

    def offset_of_line(li: int) -> int:
        return sum(len(lines[k]) + 1 for k in range(li))

    sections: list[Section] = []
    first_line = starts[0][0]
    if first_line > 0:
        preamble = "\n".join(lines[:first_line]).strip()
        if len(preamble) >= 120:
            sections.append(
                Section(
                    title=f"{label} (front matter)",
                    text=preamble,
                    start_char=0,
                    end_char=offset_of_line(first_line),
                )
            )
    for idx, (line_i, title) in enumerate(starts):
        start_off = offset_of_line(line_i)
        end_off = offset_of_line(starts[idx + 1][0]) if idx + 1 < len(starts) else len(text)
        body = text[start_off:end_off].strip()
        sections.append(Section(title=title, text=body, start_char=start_off, end_char=end_off))
    logger.info("PDF: %d section(s) from numbered chapter headings", len(sections))
    return sections
