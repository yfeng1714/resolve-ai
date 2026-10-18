"""Current policy documents for offline retrieval.

Revision ID: 20261013_0002
Revises: 20261012_0001
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261013_0002"
down_revision: str | None = "20261012_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.knowledge_document (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES app.tenant (id),
            source_path text NOT NULL,
            title text NOT NULL,
            version text NOT NULL,
            content_hash text NOT NULL,
            access_scope text NOT NULL,
            effective_from timestamptz NOT NULL,
            effective_to timestamptz,
            is_current boolean NOT NULL,
            created_at timestamptz NOT NULL,
            CONSTRAINT knowledge_document_access
                CHECK (access_scope IN ('operator', 'approver'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX knowledge_document_current_source
        ON app.knowledge_document (tenant_id, source_path)
        WHERE is_current
        """
    )
    op.execute(
        """
        CREATE TABLE app.knowledge_chunk (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES app.knowledge_document (id) ON DELETE CASCADE,
            tenant_id uuid NOT NULL REFERENCES app.tenant (id),
            ordinal integer NOT NULL,
            heading text NOT NULL,
            body text NOT NULL,
            start_offset integer NOT NULL,
            end_offset integer NOT NULL,
            embedding vector(32) NOT NULL,
            UNIQUE (document_id, ordinal)
        )
        """
    )
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON app.knowledge_document, app.knowledge_chunk
        TO resolveai_api
        """
    )
    op.execute(
        """
        GRANT SELECT
        ON app.knowledge_document, app.knowledge_chunk
        TO resolveai_oracle
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.knowledge_chunk")
    op.execute("DROP TABLE IF EXISTS app.knowledge_document")
