"""Parse SEC filing HTML into clean, section-structured text."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import warnings

from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from app.ingestion.config import SECTION_PATTERNS

logger = logging.getLogger(__name__)

_MIN_SECTION_CHARS = 300   # sections shorter than this are likely TOC entries
_TABLE_TOKEN_THRESHOLD = 300  # tables larger than this become standalone chunks


@dataclass
class Section:
    title: str
    text: str       # clean text, ready for chunking
    start_char: int
    end_char: int


def extract_sections(html: bytes) -> list[Section]:
    """
    Parse raw HTML bytes into a list of Section objects.
    Steps: strip boilerplate → flatten tables → extract text → detect sections.
    Returns an empty list only if no text can be extracted at all.
    """
    soup = BeautifulSoup(html, "lxml")

    _remove_noise_tags(soup)
    _replace_tables(soup)
    text = _extract_text(soup)
    sections = _detect_sections(text)

    if not sections:
        logger.warning("No sections detected — treating entire document as UNSTRUCTURED")
        sections = [Section(title="UNSTRUCTURED", text=text.strip(), start_char=0, end_char=len(text))]

    return sections


# ─── private helpers ──────────────────────────────────────────────────────────

def _remove_noise_tags(soup: BeautifulSoup) -> None:
    """Remove script/style/meta and unwrap XBRL inline tags."""
    for tag in soup.find_all(["script", "style", "meta", "link", "head"]):
        tag.decompose()

    # Unwrap iXBRL tags (keep text, drop the tag wrapper)
    for tag in soup.find_all(True):
        name = getattr(tag, "name", "") or ""
        if ":" in name:  # ix:nonfraction, xbrli:context, etc.
            tag.unwrap()


def _table_to_text(table: Tag) -> str:
    """Convert a <table> element to pipe-delimited plain text."""
    rows = []
    for tr in table.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _is_toc_table(table: Tag) -> bool:
    """Heuristic: a table that is mostly hyperlinks is a table of contents."""
    links = table.find_all("a")
    cells = table.find_all(["td", "th"])
    if not cells:
        return False
    link_ratio = len(links) / len(cells)
    return link_ratio > 0.5


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _replace_tables(soup: BeautifulSoup) -> None:
    """
    Replace each <table> with either:
    - A text block (small tables, or TOC → empty string)
    - A [TABLE]...[/TABLE] sentinel (large tables, preserved for chunker)
    """
    for table in soup.find_all("table"):
        if _is_toc_table(table):
            table.replace_with(soup.new_string(""))
            continue

        table_text = _table_to_text(table)
        if _estimate_tokens(table_text) >= _TABLE_TOKEN_THRESHOLD:
            replacement = f"\n[TABLE]\n{table_text}\n[/TABLE]\n"
        else:
            replacement = f"\n{table_text}\n"

        table.replace_with(soup.new_string(replacement))


def _extract_text(soup: BeautifulSoup) -> str:
    """Convert soup to clean plain text."""
    # Treat <br> and block elements as line breaks
    for br in soup.find_all("br"):
        br.replace_with(soup.new_string("\n"))
    for tag in soup.find_all(["p", "div", "tr", "li"]):
        tag.insert_before(soup.new_string("\n"))
        tag.insert_after(soup.new_string("\n"))

    text = soup.get_text(separator=" ")
    # Collapse runs of whitespace to single space, but preserve newlines
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    # Collapse 3+ consecutive blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _detect_sections(text: str) -> list[Section]:
    """
    Walk through the text and split at section headings.
    Drops sections whose content is shorter than _MIN_SECTION_CHARS
    (these are TOC entries or page-header artefacts).
    """
    compiled = [(re.compile(pat), title) for pat, title in SECTION_PATTERNS]

    # Collect (match_start, section_title) tuples
    hits: list[tuple[int, str]] = []
    for line_match in re.finditer(r"^.+$", text, re.MULTILINE):
        line = line_match.group(0).strip()
        for regex, title in compiled:
            if regex.fullmatch(line) or regex.search(line):
                hits.append((line_match.start(), title))
                break  # one title per line

    if not hits:
        return []

    # Build sections between consecutive hits
    sections: list[Section] = []
    for i, (start, title) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        body = text[start:end].strip()

        # Remove the heading line itself from the body text
        first_nl = body.find("\n")
        body = body[first_nl:].strip() if first_nl != -1 else ""

        if len(body) < _MIN_SECTION_CHARS:
            continue  # skip TOC entries

        # If the same section title appeared before (TOC duplicate), keep the longer one
        existing = next((s for s in sections if s.title == title), None)
        if existing:
            if len(body) > len(existing.text):
                sections.remove(existing)
            else:
                continue

        sections.append(Section(title=title, text=body, start_char=start, end_char=end))

    logger.info("Detected %d sections", len(sections))
    return sections
