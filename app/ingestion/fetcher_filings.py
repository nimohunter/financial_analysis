"""Fetch 10-K/10-Q filing HTML from SEC EDGAR, with local file cache."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.ingestion.edgar_client import DATA_BASE_URL, FILINGS_BASE_URL, edgar_client

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "raw" / "filings"


@dataclass
class FilingMeta:
    form: str           # '10-K' or '10-Q'
    filed_at: date
    period_end: date
    accession_number: str   # "0000320193-24-000123" (with dashes)
    primary_document: str   # "aapl-20240928.htm"
    doc_url: str


async def list_filings(cik: str, since: date) -> list[FilingMeta]:
    """
    Return all 10-K and 10-Q filings for a company filed on or after `since`.
    Uses EDGAR submissions API. Covers filings.recent (~1000 most recent),
    which is sufficient for any company back to 2022.
    """
    cik_padded = cik.zfill(10)
    url = f"{DATA_BASE_URL}/submissions/CIK{cik_padded}.json"
    logger.info("Fetching submissions index for CIK %s", cik_padded)
    raw = await edgar_client.get(url)
    data = json.loads(raw)

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    cik_int = str(int(cik))  # strip leading zeros for URL
    results: list[FilingMeta] = []

    for form, filed_str, report_str, accession, primary_doc in zip(
        forms, filing_dates, report_dates, accessions, primary_docs
    ):
        if form not in ("10-K", "10-Q"):
            continue

        try:
            filed_at = date.fromisoformat(filed_str)
        except ValueError:
            continue

        if filed_at < since:
            continue

        try:
            period_end = date.fromisoformat(report_str) if report_str else filed_at
        except ValueError:
            period_end = filed_at

        accession_nodashes = accession.replace("-", "")
        doc_url = (
            f"{FILINGS_BASE_URL}/Archives/edgar/data"
            f"/{cik_int}/{accession_nodashes}/{primary_doc}"
        )

        results.append(FilingMeta(
            form=form,
            filed_at=filed_at,
            period_end=period_end,
            accession_number=accession,
            primary_document=primary_doc,
            doc_url=doc_url,
        ))

    logger.info("Found %d filings for CIK %s since %s", len(results), cik_padded, since)
    return results


async def fetch_filing_html(filing: FilingMeta, cik: str) -> bytes | None:
    """
    Download the primary HTML document for a filing.
    Saves to data/raw/filings/{cik}/{accession_nodashes}.html on first fetch.
    Returns None for non-HTML filings (older .txt format).
    """
    primary = filing.primary_document.lower()
    if not (primary.endswith(".htm") or primary.endswith(".html")):
        logger.warning("Skipping non-HTML filing %s (%s)", filing.accession_number, filing.primary_document)
        return None

    accession_nodashes = filing.accession_number.replace("-", "")
    cache_file = _CACHE_DIR / cik.zfill(10) / f"{accession_nodashes}.html"

    if cache_file.exists():
        logger.info("Loading filing from cache: %s", cache_file)
        return cache_file.read_bytes()

    logger.info("Fetching %s %s from %s", filing.form, filing.period_end, filing.doc_url)
    try:
        raw = await edgar_client.get(filing.doc_url)
    except Exception:
        logger.exception("Failed to fetch filing %s", filing.accession_number)
        return None

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(raw)
    logger.info("Cached %d bytes → %s", len(raw), cache_file)
    return raw
