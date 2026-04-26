"""widen documents.doc_type to varchar(30)

Revision ID: 0002
Revises: 0001
Create Date: 2024-01-02 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("documents", "doc_type", type_=sa.VARCHAR(30), existing_nullable=False)


def downgrade() -> None:
    op.alter_column("documents", "doc_type", type_=sa.VARCHAR(10), existing_nullable=False)
