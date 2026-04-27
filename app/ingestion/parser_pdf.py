"""Extract sections from uploaded PDFs (reading order + font-size headings)."""
from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass

import fitz  # pymupdf

from app.ingestion.parser_html import Section
from app.ingestion.parser_pdf_numbering import split_by_numbered_chapters

logger = logging.getLogger(__name__)

_Y_GROUP_TOL = 3.0
_HEADING_SIZE_RATIO = 1.18
_HEADING_MAX_CHARS = 130
_MIN_BODY_SPAN_CHARS = 35


def _merge_hyphenated_linebreaks(text: str) -> str:
    """Join words split across lines with soft hyphens (common in justified PDFs)."""
    return re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)


def _normalize_whitespace(text: str) -> str:
    """Strip layout padding: leading spaces per line, runs of spaces, excess blank lines."""
    text = _merge_hyphenated_linebreaks(text)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        line = re.sub(r"[\t\xa0\u2000-\u200b]+", " ", line)
        line = re.sub(r" {2,}", " ", line)
        lines.append(line)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _extract_text_blocks_sorted(doc: fitz.Document) -> str:
    """
    MuPDF reading order + block boundaries — avoids per-span stream order and
    newline-between-every-span artifacts.
    """
    page_parts: list[str] = []
    for page in doc:
        blocks = page.get_text("blocks", sort=True)
        texts: list[str] = []
        for b in blocks:
            if len(b) < 7 or b[6] != 0:
                continue
            raw = (b[4] or "").strip()
            if raw:
                texts.append(raw)
        if texts:
            page_parts.append("\n\n".join(texts))
    joined = "\n\n".join(page_parts)
    return _normalize_whitespace(joined)


@dataclass
class _LineSpan:
    y: float
    x: float
    size: float
    text: str


def _collect_rows_sorted(doc: fitz.Document) -> list[tuple[float, float, str]]:
    """
    Return list of (y, max_font_size, line_text) in reading order.
    """
    spans: list[_LineSpan] = []
    for page in doc:
        data = page.get_text("dict", sort=True)
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                bbox = line.get("bbox") or (0, 0, 0, 0)
                y0, x0 = float(bbox[1]), float(bbox[0])
                for span in line.get("spans", []):
                    t = (span.get("text") or "").strip()
                    if not t:
                        continue
                    sz = float(span.get("size", 10))
                    spans.append(_LineSpan(y=y0, x=x0, size=sz, text=t))

    spans.sort(key=lambda s: (s.y, s.x))
    if not spans:
        return []

    line_groups: list[list[_LineSpan]] = []
    cur: list[_LineSpan] = [spans[0]]
    anchor_y = spans[0].y
    for s in spans[1:]:
        if abs(s.y - anchor_y) <= _Y_GROUP_TOL:
            cur.append(s)
        else:
            cur.sort(key=lambda z: z.x)
            line_groups.append(cur)
            cur = [s]
            anchor_y = s.y
    cur.sort(key=lambda z: z.x)
    line_groups.append(cur)

    rows: list[tuple[float, float, str]] = []
    for grp in line_groups:
        grp.sort(key=lambda z: z.x)
        y = sum(s.y for s in grp) / len(grp)
        max_sz = max(s.size for s in grp)
        merged = " ".join(s.text for s in grp)
        rows.append((y, max_sz, merged))
    return rows


def _split_sections_by_headings(
    rows: list[tuple[float, float, str]],
    label: str,
) -> list[Section]:
    """Use font size vs median body size to split sections (spec heuristic, geometry-aware)."""
    body_sizes: list[float] = []
    for _y, sz, text in rows:
        if len(text) >= _MIN_BODY_SPAN_CHARS and 6 <= sz <= 30:
            body_sizes.append(sz)
    if len(body_sizes) < 3:
        body_text = "\n".join(t for _, _, t in rows)
        return [Section(title=label, text=body_text, start_char=0, end_char=len(body_text))]

    med = float(statistics.median(body_sizes))
    thresh = med * _HEADING_SIZE_RATIO
    logger.debug("PDF heading threshold: median body font=%.2f thresh=%.2f", med, thresh)

    sections: list[Section] = []
    cur_title = label
    cur_lines: list[str] = []
    offset = 0

    for _y, sz, text in rows:
        if not text.strip():
            continue
        short = len(text) < _HEADING_MAX_CHARS
        not_sentence = ". " not in text[: min(60, len(text))]
        # Headings are larger than body, but cap size to ignore rare superscript spikes.
        looks_heading = short and not_sentence and sz >= thresh and sz <= max(med + 14, 40)

        if looks_heading and cur_lines:
            body = "\n".join(cur_lines)
            sections.append(
                Section(title=cur_title, text=body, start_char=offset, end_char=offset + len(body))
            )
            offset += len(body) + 2
            cur_title = text.strip()
            cur_lines = []
        else:
            cur_lines.append(text.strip())

    if cur_lines:
        body = "\n".join(cur_lines)
        sections.append(
            Section(title=cur_title, text=body, start_char=offset, end_char=offset + len(body))
        )

    if len(sections) <= 1:
        return []

    return sections


def extract_sections(pdf_bytes: bytes, label: str = "UPLOADED DOCUMENT") -> list[Section]:
    """Extract text in reading order; split on numbered chapters (1., 2., …) if present, else font headings."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        block_text = _extract_text_blocks_sorted(doc)
        if not block_text:
            logger.warning("No text extracted from PDF — single empty section")
            return [Section(title=label, text="", start_char=0, end_char=0)]

        numbered = split_by_numbered_chapters(block_text, label)
        if numbered:
            return numbered

        rows = _collect_rows_sorted(doc)
        heading_split = _split_sections_by_headings(rows, label)
        if heading_split:
            return heading_split

        return [Section(title=label, text=block_text, start_char=0, end_char=len(block_text))]
    finally:
        doc.close()
