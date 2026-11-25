"""Lexical index for the current policy chunks.

Revision ID: 20261112_0014
Revises: 20261106_0013
Create Date: 2026-11-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261112_0014"
down_revision: str | None = "20261106_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX knowledge_chunk_lexical
        ON app.knowledge_chunk
        USING gin (to_tsvector('english', heading || ' ' || body))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.knowledge_chunk_lexical")
