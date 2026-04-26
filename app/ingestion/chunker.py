"""Split Section objects into overlapping Chunk objects."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingestion.config import NARRATIVE_SECTIONS

_TARGET_TOKENS = 800
_OVERLAP_TOKENS = 100
_NON_NARRATIVE_CAP_CHARS = 4800  # ~1200 tokens — cap for non-narrative sections


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _last_n_tokens(text: str, n: int) -> str:
    chars = n * 4
    return text[-chars:] if len(text) > chars else text


@dataclass
class Chunk:
    text: str
    section_title: str
    chunk_index: int
    token_count: int
    is_table: bool = False


def _split_at_sentences(text: str, target: int, overlap: int) -> list[str]:
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        candidate = (current + " " + sent).strip() if current else sent
        if _estimate_tokens(candidate) > target and current:
            chunks.append(current.strip())
            current = _last_n_tokens(current, overlap) + " " + sent
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _chunk_normal_text(text: str, target: int, overlap: int) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para_tokens = _estimate_tokens(para)
        current_tokens = _estimate_tokens(current)

        if current_tokens + para_tokens > target and current:
            chunks.append(current.strip())
            current = _last_n_tokens(current, overlap) + "\n\n" + para
        elif para_tokens > target:
            if current:
                chunks.append(current.strip())
            sub = _split_at_sentences(para, target, overlap)
            chunks.extend(sub[:-1])
            current = sub[-1] if sub else ""
        else:
            current = (current + "\n\n" + para).strip() if current else para

    if current.strip():
        chunks.append(current.strip())
    return chunks


def _chunk_section_text(text: str, target: int, overlap: int) -> list[tuple[str, bool]]:
    """
    Split section text into (chunk_text, is_table) pairs.
    [TABLE]...[/TABLE] sentinels are extracted as atomic table chunks.
    """
    result: list[tuple[str, bool]] = []
    table_re = re.compile(r'\[TABLE\].*?\[/TABLE\]', re.DOTALL)
    pos = 0
    pending = ""

    for m in table_re.finditer(text):
        pending += text[pos:m.start()]
        if pending.strip():
            for raw in _chunk_normal_text(pending.strip(), target, overlap):
                result.append((raw, False))
            pending = ""
        table_clean = re.sub(r'\[/?TABLE\]', '', m.group(0)).strip()
        if table_clean:
            result.append((table_clean, True))
        pos = m.end()

    pending += text[pos:]
    if pending.strip():
        for raw in _chunk_normal_text(pending.strip(), target, overlap):
            result.append((raw, False))

    return result


def chunk_sections(sections: list, doc_type: str = "") -> list[Chunk]:
    """
    Convert Section objects into Chunk objects with continuous chunk_index.

    Narrative sections (ITEM 1A, MD&A, etc.) are split at paragraph/sentence
    boundaries with 100-token overlap.  Non-narrative sections produce a single
    truncated chunk so they still get an embedding for section-level search.
    """
    all_chunks: list[Chunk] = []
    index = 0

    for section in sections:
        if section.title in NARRATIVE_SECTIONS:
            pairs = _chunk_section_text(section.text, _TARGET_TOKENS, _OVERLAP_TOKENS)
        else:
            truncated = section.text[:_NON_NARRATIVE_CAP_CHARS].strip()
            pairs = [(truncated, False)] if truncated else []

        for text, is_table in pairs:
            all_chunks.append(Chunk(
                text=text,
                section_title=section.title,
                chunk_index=index,
                token_count=_estimate_tokens(text),
                is_table=is_table,
            ))
            index += 1

    return all_chunks
