"""initial schema

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "companies",
        sa.Column("company_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.VARCHAR(10), unique=True, nullable=False),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("cik", sa.VARCHAR(10), nullable=False),
        sa.Column("exchange", sa.VARCHAR(20)),
        sa.Column("sector", sa.VARCHAR(100)),
        sa.Column("currency", sa.VARCHAR(3), server_default="USD"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "documents",
        sa.Column("document_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.company_id")),
        sa.Column("doc_type", sa.VARCHAR(10), nullable=False),
        sa.Column("period_end", sa.DATE(), nullable=False),
        sa.Column("filed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("source_url", sa.TEXT()),
        sa.Column("raw_hash", sa.VARCHAR(64), unique=True),
        sa.Column("status", sa.VARCHAR(20), server_default="fetched"),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "financial_line_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.company_id")),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.document_id"), nullable=True),
        sa.Column("statement_type", sa.VARCHAR(20), nullable=False),
        sa.Column("period_end", sa.DATE(), nullable=False),
        sa.Column("period_type", sa.VARCHAR(5), nullable=False),
        sa.Column("line_item", sa.VARCHAR(100), nullable=False),
        sa.Column("value", sa.NUMERIC()),
        sa.Column("currency", sa.VARCHAR(3), server_default="USD"),
        sa.Column("unit", sa.VARCHAR(20)),
        sa.Column("as_reported_label", sa.VARCHAR(255)),
        sa.UniqueConstraint("company_id", "period_end", "period_type", "line_item", "statement_type"),
    )

    op.create_table(
        "document_chunks",
        sa.Column("chunk_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.document_id")),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section_title", sa.VARCHAR(255)),
        sa.Column("text", sa.TEXT(), nullable=False),
        sa.Column("embedding_input", sa.TEXT()),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column("token_count", sa.Integer()),
    )

    op.create_table(
        "section_summaries",
        sa.Column("summary_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.document_id")),
        sa.Column("section_title", sa.VARCHAR(255), nullable=False),
        sa.Column("summary_text", sa.TEXT(), nullable=False),
        sa.Column("summary_embedding", Vector(384), nullable=False),
        sa.Column("chunk_start_index", sa.Integer()),
        sa.Column("chunk_end_index", sa.Integer()),
    )

    op.create_table(
        "document_summaries",
        sa.Column("summary_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.document_id"), unique=True),
        sa.Column("summary_text", sa.TEXT(), nullable=False),
        sa.Column("summary_embedding", Vector(384), nullable=False),
        sa.Column("key_themes", sa.ARRAY(sa.TEXT())),
    )

    # Indexes on vector columns (ivfflat — skip if row count is too small,
    # Postgres will use brute force automatically until lists is met)
    op.execute(
        "CREATE INDEX ON document_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    op.execute(
        "CREATE INDEX ON section_summaries USING ivfflat (summary_embedding vector_cosine_ops) WITH (lists = 50)"
    )
    op.execute(
        "CREATE INDEX ON document_summaries USING ivfflat (summary_embedding vector_cosine_ops) WITH (lists = 20)"
    )

    # Scalar indexes
    op.create_index("ix_fli_company_statement_period", "financial_line_items",
                    ["company_id", "statement_type", "period_end"])
    op.create_index("ix_chunks_document_section", "document_chunks",
                    ["document_id", "section_title"])

    # Grant SELECT on all tables to readonly user
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO readonly")


def downgrade() -> None:
    op.drop_table("document_summaries")
    op.drop_table("section_summaries")
    op.drop_table("document_chunks")
    op.drop_table("financial_line_items")
    op.drop_table("documents")
    op.drop_table("companies")
    op.execute("DROP EXTENSION IF EXISTS vector")
