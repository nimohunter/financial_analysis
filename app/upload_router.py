"""HTTP upload endpoint for custom PDF documents (Phase 3d)."""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import load_yaml
from app.db import get_rw_session
from app.ingestion import parser_pdf
from app.ingestion.fetcher_upload import save_upload
from app.ingestion.pipeline import ingest_document_sections
from app.models import Company

logger = logging.getLogger(__name__)

router = APIRouter(tags=["upload"])


def _summary_method_from_config() -> str:
    raw = (load_yaml().get("ingestion") or {}).get("summary_method", "extractive")
    m = str(raw).lower().strip()
    return m if m in ("extractive", "llm") else "extractive"


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    ticker: str = Form(...),
    label: str = Form(""),
    period_end: date | None = Form(default=None),
    session: Session = Depends(get_rw_session),
) -> dict[str, str | int]:
    pdf_bytes = await file.read()
    if len(pdf_bytes) < 5 or not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    company = session.query(Company).filter(Company.ticker == ticker.upper()).first()
    if not company:
        raise HTTPException(
            status_code=404,
            detail=f"Ticker {ticker!r} not found. Add it in config.yaml and seed companies.",
        )

    doc_label = (label or file.filename or "UPLOADED DOCUMENT").strip() or "UPLOADED DOCUMENT"
    p_end = period_end or date.today()
    filename = file.filename or "upload.pdf"

    doc = save_upload(pdf_bytes, filename, company, p_end, session)
    if doc is None:
        return {"status": "duplicate", "message": "This file has already been ingested."}

    sections = parser_pdf.extract_sections(pdf_bytes, doc_label)
    doc.status = "parsed"
    session.commit()
    logger.info("Upload doc %d parsed — %d sections", doc.document_id, len(sections))

    summary_method = _summary_method_from_config()
    stats = ingest_document_sections(doc, sections, company, session, summary_method)
    return {
        "status": "indexed",
        "document_id": doc.document_id,
        "chunks": stats.chunks_inserted,
        "sections": stats.sections_count,
    }
