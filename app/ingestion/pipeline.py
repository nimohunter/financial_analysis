"""Ingestion pipeline orchestrator."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import load_yaml
from app.ingestion import chunker, embedder, fetcher_filings, fetcher_xbrl, parser_html, parser_xbrl
from app.models import Company, Document, FinancialLineItem

logger = logging.getLogger(__name__)


def seed_companies(session: Session) -> None:
    """Upsert companies from config.yaml — safe to call on every startup."""
    cfg = load_yaml()
    for c in cfg.get("companies", []):
        stmt = (
            pg_insert(Company)
            .values(
                ticker=c["ticker"],
                name=c["name"],
                cik=c["cik"],
                exchange=c.get("exchange"),
                sector=c.get("sector"),
            )
            .on_conflict_do_nothing(index_elements=["ticker"])
        )
        session.execute(stmt)
    session.commit()
    logger.info("Companies seeded from config.yaml")


@dataclass
class IngestResult:
    ticker: str
    xbrl_line_items: int = 0
    filings_fetched: int = 0
    filings_skipped: int = 0
    chunks_inserted: int = 0
    errors: list[str] = field(default_factory=list)


def ingest_document_sections(
    doc: Document,
    sections: list,
    company: Company,
    session: Session,
) -> int:
    """
    Steps 5-6: chunk + embed sections, update doc status to 'normalized'.
    Returns chunks inserted. Shared by ingest_filings() and the upload endpoint.
    """
    chunks = chunker.chunk_sections(sections, doc.doc_type)
    inserted = embedder.embed_and_store(chunks, doc, company.name, session)
    doc.status = "normalized"
    session.commit()
    return inserted


async def ingest_xbrl(company: Company, since: date, session: Session) -> int:
    """Fetch + parse XBRL companyfacts. Returns number of line items inserted."""
    raw = await fetcher_xbrl.fetch_companyfacts(company.cik)
    raw_hash = hashlib.sha256(raw).hexdigest()

    if session.query(Document).filter_by(raw_hash=raw_hash).first():
        logger.info("%s: XBRL unchanged (hash match) — skipping", company.ticker)
        return 0

    doc = Document(
        company_id=company.company_id,
        doc_type="XBRL-companyfacts",
        period_end=date.today(),
        raw_hash=raw_hash,
        status="indexed",
    )
    session.add(doc)
    session.flush()

    facts = parser_xbrl.parse(raw, since)
    inserted = 0
    if facts:
        rows = [
            {
                "company_id": company.company_id,
                "document_id": doc.document_id,
                "statement_type": f.statement_type,
                "period_end": f.period_end,
                "period_type": f.period_type,
                "line_item": f.line_item,
                "value": f.value,
                "currency": f.currency,
                "unit": f.unit,
                "as_reported_label": f.as_reported_label,
            }
            for f in facts
        ]
        stmt = pg_insert(FinancialLineItem.__table__).values(rows).on_conflict_do_nothing(
            index_elements=["company_id", "period_end", "period_type", "line_item", "statement_type"]
        )
        proxy = session.execute(stmt)
        inserted = proxy.rowcount if proxy.rowcount != -1 else len(rows)

    session.commit()
    logger.info("%s: XBRL — inserted %d line items", company.ticker, inserted)
    return inserted


async def ingest_filings(company: Company, since: date, session: Session) -> tuple[int, int, int]:
    """
    Fetch, parse, chunk and embed 10-K/10-Q HTML filings for one company.
    Returns (fetched_count, skipped_count, chunks_inserted).
    """
    filings = await fetcher_filings.list_filings(company.cik, since)
    fetched = skipped = total_chunks = 0

    for filing in filings:
        existing = session.query(Document).filter_by(source_url=filing.doc_url).first()

        # Already fully processed — skip
        if existing and existing.status in ("normalized", "indexed"):
            logger.info("%s: %s %s already %s — skipping", company.ticker, filing.form, filing.period_end, existing.status)
            skipped += 1
            continue

        # Partially processed (parsed but not yet embedded) — resume from embed step
        if existing and existing.status == "parsed":
            logger.info("%s: %s %s is parsed, resuming embed...", company.ticker, filing.form, filing.period_end)
            html = await fetcher_filings.fetch_filing_html(filing, company.cik)
            if html is None:
                skipped += 1
                continue
            try:
                sections = parser_html.extract_sections(html)
                n = ingest_document_sections(existing, sections, company, session)
                total_chunks += n
                fetched += 1
                logger.info("%s: %s %s — embedded %d chunks (resumed)", company.ticker, filing.form, filing.period_end, n)
            except Exception:
                logger.exception("%s: failed to embed (resumed) %s %s", company.ticker, filing.form, filing.period_end)
                session.rollback()
            continue

        # New filing — fetch, parse, embed
        html = await fetcher_filings.fetch_filing_html(filing, company.cik)
        if html is None:
            skipped += 1
            continue

        raw_hash = hashlib.sha256(html).hexdigest()
        if session.query(Document).filter_by(raw_hash=raw_hash).first():
            logger.info("%s: %s %s hash already exists — skipping", company.ticker, filing.form, filing.period_end)
            skipped += 1
            continue

        doc = Document(
            company_id=company.company_id,
            doc_type=filing.form,
            period_end=filing.period_end,
            filed_at=filing.filed_at,
            source_url=filing.doc_url,
            raw_hash=raw_hash,
            status="fetched",
        )
        session.add(doc)
        session.flush()

        try:
            sections = parser_html.extract_sections(html)
            doc.status = "parsed"
            session.commit()
            logger.info("%s: %s %s — parsed %d sections", company.ticker, filing.form, filing.period_end, len(sections))
        except Exception:
            logger.exception("%s: failed to parse %s %s", company.ticker, filing.form, filing.period_end)
            doc.status = "fetched"
            session.commit()
            skipped += 1
            continue

        try:
            n = ingest_document_sections(doc, sections, company, session)
            total_chunks += n
            fetched += 1
            logger.info("%s: %s %s — embedded %d chunks", company.ticker, filing.form, filing.period_end, n)
        except Exception:
            logger.exception("%s: failed to embed %s %s", company.ticker, filing.form, filing.period_end)
            session.rollback()

    return fetched, skipped, total_chunks


async def ingest_company(company: Company, since: date, session: Session) -> IngestResult:
    """Full pipeline for one company (Phases 2 + 3a so far)."""
    result = IngestResult(ticker=company.ticker)

    try:
        result.xbrl_line_items = await ingest_xbrl(company, since, session)
    except Exception as exc:
        logger.exception("XBRL ingestion failed for %s", company.ticker)
        result.errors.append(f"xbrl: {exc}")

    try:
        result.filings_fetched, result.filings_skipped, result.chunks_inserted = await ingest_filings(company, since, session)
    except Exception as exc:
        logger.exception("Filing ingestion failed for %s", company.ticker)
        result.errors.append(f"filings: {exc}")

    return result
