"""Embed chunks with all-MiniLM-L6-v2 (via fastembed) and write to document_chunks."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.ingestion.chunker import Chunk
from app.models import Document, DocumentChunk

logger = logging.getLogger(__name__)

_model = None
_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        logger.info("Loading %s (first call)...", _MODEL_NAME)
        _model = TextEmbedding(_MODEL_NAME)
        logger.info("Model loaded.")
    return _model


def _build_embedding_input(chunk: Chunk, company_name: str, doc_type: str) -> str:
    return f"{company_name}, {doc_type}, {chunk.section_title}: {chunk.text}"


def embed_and_store(
    chunks: list[Chunk],
    doc: Document,
    company_name: str,
    session: Session,
) -> int:
    """
    Embed all chunks for one document and bulk-insert into document_chunks.
    Returns the number of rows inserted.
    """
    if not chunks:
        logger.warning("No chunks to embed for document %d", doc.document_id)
        return 0

    model = _get_model()
    inputs = [_build_embedding_input(c, company_name, doc.doc_type) for c in chunks]

    logger.info("Embedding %d chunks for document %d...", len(chunks), doc.document_id)
    vectors = list(model.embed(inputs))  # fastembed returns a generator

    rows = [
        DocumentChunk(
            document_id=doc.document_id,
            chunk_index=c.chunk_index,
            section_title=c.section_title,
            text=c.text,
            embedding_input=inputs[i],
            embedding=vectors[i].tolist(),
            token_count=c.token_count,
        )
        for i, c in enumerate(chunks)
    ]

    session.bulk_save_objects(rows)
    session.flush()
    logger.info("Inserted %d chunks for document %d", len(rows), doc.document_id)
    return len(rows)
