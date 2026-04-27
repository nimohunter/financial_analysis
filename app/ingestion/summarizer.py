"""Section and document summarizers: extractive (free) and Groq LLM."""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def extractive_summary(section_text: str, max_sentences: int = 3) -> str:
    """First sentence of the section + first sentence of every 3rd paragraph."""
    paragraphs = [p.strip() for p in section_text.split("\n\n") if p.strip()]
    sentences: list[str] = []
    for i, para in enumerate(paragraphs):
        if i == 0 or i % 3 == 0:
            first = re.split(r'(?<=[.!?])\s+', para)[0]
            sentences.append(first)
        if len(sentences) >= max_sentences:
            break
    return " ".join(sentences) if sentences else section_text[:300].strip()


def summarize_section(
    section_title: str,
    section_text: str,
    company_name: str,
    doc_type: str,
    period_end: str,
    method: str = "extractive",
) -> str:
    if method == "llm":
        return _llm_section_summary(section_title, section_text, company_name, doc_type, period_end)
    return extractive_summary(section_text)


def _llm_section_summary(
    section_title: str,
    section_text: str,
    company_name: str,
    doc_type: str,
    period_end: str,
) -> str:
    from groq import Groq
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    resp = client.chat.completions.create(
        model="qwen/qwen3-32b",
        max_tokens=200,
        reasoning_effort="none",
        messages=[{
            "role": "user",
            "content": (
                f"Summarize this section from {company_name}'s {doc_type} "
                f"(period ending {period_end}) in exactly 2-3 sentences. "
                f"Focus on key facts, risks, or changes. Include names, numbers, topics.\n\n"
                f"Section: {section_title}\n\nText:\n{section_text[:6000]}"
            ),
        }],
    )
    return resp.choices[0].message.content.strip()


def summarize_document(
    section_pairs: list[tuple[str, str]],
    company_name: str,
    doc_type: str,
    period_end: str,
) -> tuple[str, list[str]]:
    """
    Groq LLM document summary from section summaries.
    Returns (summary_text, key_themes). Falls back if GROQ_API_KEY missing.
    """
    if not os.environ.get("GROQ_API_KEY"):
        logger.warning("GROQ_API_KEY not set — using extractive fallback for doc summary")
        return _fallback_doc_summary(section_pairs), []

    from groq import Groq
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    section_lines = "\n".join(f"- {title}: {text}" for title, text in section_pairs)

    resp = client.chat.completions.create(
        model="qwen/qwen3-32b",
        max_tokens=300,
        reasoning_effort="none",
        messages=[{
            "role": "user",
            "content": (
                f"Section summaries from {company_name}'s {doc_type} "
                f"(period ending {period_end}):\n\n{section_lines}\n\n"
                f"Write a single paragraph (3-4 sentences): financial position, key themes, "
                f"notable risks, major changes. Be specific — mention numbers and topics.\n\n"
                f'Then on a new line: THEMES: ["tag1", "tag2"] (3-5 lowercase_underscored tags).'
            ),
        }],
    )

    raw = resp.choices[0].message.content.strip()
    if "THEMES:" in raw:
        summary_text = raw[:raw.index("THEMES:")].strip()
        try:
            key_themes = json.loads(raw[raw.index("THEMES:") + 7:].strip())
        except json.JSONDecodeError:
            logger.warning("Could not parse key_themes JSON from Groq response")
            key_themes = []
    else:
        summary_text = raw
        key_themes = []

    return summary_text, key_themes


def _fallback_doc_summary(section_pairs: list[tuple[str, str]]) -> str:
    return " ".join(text for _, text in section_pairs[:4])
