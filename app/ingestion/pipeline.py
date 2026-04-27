"""Ingestion pipeline orchestrator."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import load_yaml
from app.db import RWSession
from app.ingestion import chunker, embedder, fetcher_filings, fetcher_xbrl, parser_html, parser_xbrl, summarizer
from app.models import Company, Document, DocumentSummary, FinancialLineItem, SectionSummary

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
    summary_method: str = "extractive",
) -> int:
    """
    Steps 5-8: chunk → embed → section summaries → doc summary → 'indexed'.
    Idempotent: skips chunk embedding if doc already 'normalized'; always
    regenerates summaries (delete + insert).
    Returns chunks inserted (0 on resume).
    """
    chunks = chunker.chunk_sections(sections, doc.doc_type)

    # Steps 5-6: embed only when not already done
    inserted = 0
    if doc.status not in ("normalized", "indexed"):
        inserted = embedder.embed_and_store(chunks, doc, company.name, session)
        doc.status = "normalized"
        session.commit()

    # Build section → chunk index ranges for linking summaries to chunks
    section_ranges: dict[str, tuple[int, int]] = {}
    for c in chunks:
        lo, hi = section_ranges.get(c.section_title, (c.chunk_index, c.chunk_index))
        section_ranges[c.section_title] = (min(lo, c.chunk_index), max(hi, c.chunk_index))

    # Step 7: section summaries — delete first for idempotency
    session.query(SectionSummary).filter_by(document_id=doc.document_id).delete()
    session.flush()

    period_str = str(doc.period_end)
    section_pairs: list[tuple[str, str]] = []

    for sec in sections:
        text = summarizer.summarize_section(
            sec.title, sec.text, company.name, doc.doc_type, period_str, summary_method
        )
        embed_in = f"{company.name}, {doc.doc_type}, {period_str}, {sec.title}: {text}"
        vec = embedder.embed_text(embed_in)
        chunk_range = section_ranges.get(sec.title)
        session.add(SectionSummary(
            document_id=doc.document_id,
            section_title=sec.title,
            summary_text=text,
            summary_embedding=vec,
            chunk_start_index=chunk_range[0] if chunk_range else None,
            chunk_end_index=chunk_range[1] if chunk_range else None,
        ))
        section_pairs.append((sec.title, text))

    session.flush()
    logger.info("%s: %d section summaries for doc %d", company.ticker, len(section_pairs), doc.document_id)

    # Step 8: document summary — delete first for idempotency
    session.query(DocumentSummary).filter_by(document_id=doc.document_id).delete()
    session.flush()

    try:
        doc_text, themes = summarizer.summarize_document(
            section_pairs, company.name, doc.doc_type, period_str
        )
    except Exception:
        logger.exception("Doc summary failed for doc %d — using fallback", doc.document_id)
        doc_text = " ".join(t for _, t in section_pairs[:4])
        themes = []

    doc_vec = embedder.embed_text(f"{company.name}, {doc.doc_type}, {period_str}: {doc_text}")
    session.add(DocumentSummary(
        document_id=doc.document_id,
        summary_text=doc_text,
        summary_embedding=doc_vec,
        key_themes=themes,
    ))

    # Step 9: mark indexed
    doc.status = "indexed"
    session.commit()
    logger.info("doc %d → indexed | themes: %s", doc.document_id, themes)
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


async def ingest_filings(
    company: Company,
    since: date,
    session: Session,
    summary_method: str = "extractive",
) -> tuple[int, int, int]:
    """
    Fetch, parse, chunk, embed, and summarize 10-K/10-Q filings.
    Returns (fetched_count, skipped_count, chunks_inserted).
    """
    filings = await fetcher_filings.list_filings(company.cik, since)
    fetched = skipped = total_chunks = 0

    for filing in filings:
        existing = session.query(Document).filter_by(source_url=filing.doc_url).first()

        if existing and existing.status == "indexed":
            logger.info("%s: %s %s already indexed — skipping", company.ticker, filing.form, filing.period_end)
            skipped += 1
            continue

        html = await fetcher_filings.fetch_filing_html(filing, company.cik)
        if html is None:
            skipped += 1
            continue

        # Resume: doc exists at 'parsed' or 'normalized' — re-use existing row
        if existing and existing.status in ("parsed", "normalized"):
            logger.info("%s: %s %s is '%s', resuming...", company.ticker, filing.form, filing.period_end, existing.status)
            doc = existing
        else:
            # New filing
            raw_hash = hashlib.sha256(html).hexdigest()
            if session.query(Document).filter_by(raw_hash=raw_hash).first():
                logger.info("%s: %s %s hash exists — skipping", company.ticker, filing.form, filing.period_end)
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
            if doc.status == "fetched":
                doc.status = "parsed"
                session.commit()
                logger.info("%s: %s %s parsed — %d sections", company.ticker, filing.form, filing.period_end, len(sections))
        except Exception:
            logger.exception("%s: parse failed for %s %s", company.ticker, filing.form, filing.period_end)
            skipped += 1
            continue

        try:
            n = ingest_document_sections(doc, sections, company, session, summary_method)
            total_chunks += n
            fetched += 1
        except Exception:
            logger.exception("%s: embed/summarize failed for %s %s", company.ticker, filing.form, filing.period_end)
            session.rollback()

    return fetched, skipped, total_chunks


async def ingest_company(
    company: Company,
    since: date,
    session: Session,
    summary_method: str = "extractive",
) -> IngestResult:
    """Full pipeline for one company."""
    result = IngestResult(ticker=company.ticker)

    try:
        result.xbrl_line_items = await ingest_xbrl(company, since, session)
    except Exception as exc:
        logger.exception("XBRL ingestion failed for %s", company.ticker)
        result.errors.append(f"xbrl: {exc}")

    try:
        result.filings_fetched, result.filings_skipped, result.chunks_inserted = (
            await ingest_filings(company, since, session, summary_method)
        )
    except Exception as exc:
        logger.exception("Filing ingestion failed for %s", company.ticker)
        result.errors.append(f"filings: {exc}")

    return result
