"""Parse XBRL companyfacts JSON into deduplicated financial line items."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.ingestion.config import STATEMENT_TYPES, XBRL_TAXONOMY

logger = logging.getLogger(__name__)

# Preserve insertion order for tie-breaking duplicate concepts
_TAXONOMY_ORDER = list(XBRL_TAXONOMY.keys())

_UNIT_MAP = {"USD": "USD", "USD/shares": "per_share", "shares": "shares"}


@dataclass
class ParsedFact:
    statement_type: str
    period_end: date
    period_type: str
    line_item: str
    value: Optional[float]
    currency: str
    unit: str
    as_reported_label: str


def parse(raw: bytes, since: date) -> list[ParsedFact]:
    """
    Parse companyfacts JSON bytes into a deduplicated list of facts.

    Dedup rules:
    - Only us-gaap namespace, only taxonomy-mapped concepts
    - Only 10-K and 10-Q forms
    - Only periods ending >= since
    - For (canonical_name, end_date, fp) duplicates: most recent filed date wins;
      tie-break by taxonomy dict position (earlier = preferred)
    """
    data = json.loads(raw)
    us_gaap = data.get("facts", {}).get("us-gaap", {})

    if not us_gaap:
        logger.warning("No us-gaap facts found in companyfacts JSON")
        return []

    # key = (canonical, end_str, fp)
    # val = (filed_date, taxonomy_pos, raw_value, unit_type, label)
    best: dict[tuple[str, str, str], tuple] = {}

    for xbrl_concept, canonical in XBRL_TAXONOMY.items():
        concept_data = us_gaap.get(xbrl_concept)
        if not concept_data:
            continue

        label = concept_data.get("label", xbrl_concept)
        taxonomy_pos = _TAXONOMY_ORDER.index(xbrl_concept)

        for unit_type, facts in concept_data.get("units", {}).items():
            for fact in facts:
                if fact.get("form") not in ("10-K", "10-Q"):
                    continue

                end_str = fact.get("end", "")
                fp = fact.get("fp", "")
                if not end_str or not fp:
                    continue

                try:
                    end_date = date.fromisoformat(end_str)
                except ValueError:
                    continue

                if end_date < since:
                    continue

                try:
                    filed_date = date.fromisoformat(fact.get("filed", "2000-01-01"))
                except ValueError:
                    filed_date = date(2000, 1, 1)

                key = (canonical, end_str, fp)
                existing = best.get(key)

                if existing is None:
                    best[key] = (filed_date, taxonomy_pos, fact.get("val"), unit_type, label)
                else:
                    ex_filed, ex_pos, _, _, _ = existing
                    if filed_date > ex_filed or (filed_date == ex_filed and taxonomy_pos < ex_pos):
                        best[key] = (filed_date, taxonomy_pos, fact.get("val"), unit_type, label)

    results: list[ParsedFact] = []
    for (canonical, end_str, fp), (_, _, raw_val, unit_type, label) in best.items():
        period_type = "FY" if fp == "FY" else "Q"
        results.append(
            ParsedFact(
                statement_type=STATEMENT_TYPES.get(canonical, "other"),
                period_end=date.fromisoformat(end_str),
                period_type=period_type,
                line_item=canonical,
                value=float(raw_val) if raw_val is not None else None,
                currency="USD",
                unit=_UNIT_MAP.get(unit_type, unit_type),
                as_reported_label=label,
            )
        )

    logger.info("Parsed %d deduplicated facts from companyfacts JSON", len(results))
    return results
