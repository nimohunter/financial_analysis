"""Persist uploaded PDF bytes and create a documents row."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Company, Document

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
UPLOAD_DIR = _REPO_ROOT / "data" / "uploads"


def _safe_filename(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^\w.\-]+", "_", base, flags=re.UNICODE).strip("._") or "upload.pdf"
    return base[:200]


def save_upload(
    file_bytes: bytes,
    filename: str,
    company: Company,
    period_end: date,
    session: Session,
) -> Document | None:
    """
    Write bytes under data/uploads/, insert documents row with doc_type UPLOAD.
    Returns None if raw_hash already exists (duplicate).
    """
    raw_hash = hashlib.sha256(file_bytes).hexdigest()
    if session.query(Document).filter_by(raw_hash=raw_hash).first():
        logger.info("Upload duplicate hash %s — skipping", raw_hash[:16])
        return None

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(filename)
    dest = UPLOAD_DIR / f"{raw_hash[:16]}_{safe}"
    dest.write_bytes(file_bytes)
    logger.info("Saved upload to %s", dest)

    doc = Document(
        company_id=company.company_id,
        doc_type="UPLOAD",
        period_end=period_end,
        filed_at=None,
        source_url=str(dest),
        raw_hash=raw_hash,
        status="fetched",
    )
    session.add(doc)
    session.flush()
    return doc
