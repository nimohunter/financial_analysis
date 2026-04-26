from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    DATE,
    NUMERIC,
    TEXT,
    TIMESTAMP,
    VARCHAR,
    ForeignKey,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    company_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(VARCHAR(10), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    cik: Mapped[str] = mapped_column(VARCHAR(10), nullable=False)
    exchange: Mapped[Optional[str]] = mapped_column(VARCHAR(20))
    sector: Mapped[Optional[str]] = mapped_column(VARCHAR(100))
    currency: Mapped[str] = mapped_column(VARCHAR(3), server_default="USD")
    created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), server_default="now()")

    documents: Mapped[list["Document"]] = relationship(back_populates="company")
    financial_line_items: Mapped[list["FinancialLineItem"]] = relationship(back_populates="company")


class Document(Base):
    __tablename__ = "documents"

    document_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.company_id"))
    doc_type: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    period_end: Mapped[date] = mapped_column(DATE, nullable=False)
    filed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    source_url: Mapped[Optional[str]] = mapped_column(TEXT)
    raw_hash: Mapped[Optional[str]] = mapped_column(VARCHAR(64), unique=True)
    status: Mapped[str] = mapped_column(VARCHAR(20), server_default="fetched")
    ingested_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), server_default="now()")

    company: Mapped["Company"] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(back_populates="document")
    section_summaries: Mapped[list["SectionSummary"]] = relationship(back_populates="document")
    document_summary: Mapped[Optional["DocumentSummary"]] = relationship(back_populates="document")
    financial_line_items: Mapped[list["FinancialLineItem"]] = relationship(back_populates="document")


class FinancialLineItem(Base):
    __tablename__ = "financial_line_items"
    __table_args__ = (
        UniqueConstraint("company_id", "period_end", "period_type", "line_item", "statement_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.company_id"))
    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.document_id"))
    statement_type: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    period_end: Mapped[date] = mapped_column(DATE, nullable=False)
    period_type: Mapped[str] = mapped_column(VARCHAR(5), nullable=False)
    line_item: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    value: Mapped[Optional[float]] = mapped_column(NUMERIC)
    currency: Mapped[str] = mapped_column(VARCHAR(3), server_default="USD")
    unit: Mapped[Optional[str]] = mapped_column(VARCHAR(20))
    as_reported_label: Mapped[Optional[str]] = mapped_column(VARCHAR(255))

    company: Mapped["Company"] = relationship(back_populates="financial_line_items")
    document: Mapped[Optional["Document"]] = relationship(back_populates="financial_line_items")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    chunk_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.document_id"))
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    section_title: Mapped[Optional[str]] = mapped_column(VARCHAR(255))
    text: Mapped[str] = mapped_column(TEXT, nullable=False)
    embedding_input: Mapped[Optional[str]] = mapped_column(TEXT)
    embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    token_count: Mapped[Optional[int]] = mapped_column(Integer)

    document: Mapped["Document"] = relationship(back_populates="chunks")


class SectionSummary(Base):
    __tablename__ = "section_summaries"

    summary_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.document_id"))
    section_title: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    summary_text: Mapped[str] = mapped_column(TEXT, nullable=False)
    summary_embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    chunk_start_index: Mapped[Optional[int]] = mapped_column(Integer)
    chunk_end_index: Mapped[Optional[int]] = mapped_column(Integer)

    document: Mapped["Document"] = relationship(back_populates="section_summaries")


class DocumentSummary(Base):
    __tablename__ = "document_summaries"

    summary_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.document_id"), unique=True)
    summary_text: Mapped[str] = mapped_column(TEXT, nullable=False)
    summary_embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    key_themes: Mapped[Optional[list[str]]] = mapped_column(ARRAY(TEXT))

    document: Mapped["Document"] = relationship(back_populates="document_summary")
