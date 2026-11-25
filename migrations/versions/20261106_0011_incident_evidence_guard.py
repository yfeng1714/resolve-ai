"""Confirmed incident handoffs must cite one full candidate.

Revision ID: 20261106_0011
Revises: 20261102_0010
Create Date: 2026-11-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261106_0011"
down_revision: str | None = "20261102_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app.incident_handoff_evidence_valid(
          kind text,
          incident_id uuid,
          incident_version integer,
          digest text,
          evidence jsonb
        ) RETURNS boolean
        LANGUAGE plpgsql
        IMMUTABLE
        AS $$
        DECLARE
          candidate jsonb;
          full_matches integer := 0;
          matched integer := 0;
        BEGIN
          IF evidence IS NULL OR jsonb_typeof(evidence) IS DISTINCT FROM 'object' THEN
            RETURN false;
          END IF;
          IF jsonb_typeof(evidence->'verified') IS DISTINCT FROM 'boolean'
             OR jsonb_typeof(evidence->'candidates') IS DISTINCT FROM 'array'
             OR jsonb_typeof(evidence->'context') IS DISTINCT FROM 'array' THEN
            RETURN false;
          END IF;
          IF kind IS DISTINCT FROM 'confirmed' THEN
            RETURN true;
          END IF;
          IF digest IS NULL OR incident_id IS NULL OR incident_version IS NULL THEN
            RETURN false;
          END IF;
          IF evidence->>'verified' IS DISTINCT FROM 'true' THEN
            RETURN false;
          END IF;
          FOR candidate IN
            SELECT value FROM jsonb_array_elements(evidence->'candidates') AS value
          LOOP
            IF jsonb_typeof(candidate) IS DISTINCT FROM 'object' THEN
              RETURN false;
            END IF;
            IF candidate->>'match_class' = 'full' THEN
              full_matches := full_matches + 1;
              IF candidate->>'id' = incident_id::text
                 AND jsonb_typeof(candidate->'resource_version') = 'number'
                 AND (candidate->>'resource_version')::integer = incident_version THEN
                matched := matched + 1;
              END IF;
            END IF;
          END LOOP;
          RETURN full_matches = 1 AND matched = 1;
        EXCEPTION
          WHEN others THEN
            RETURN false;
        END;
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE app.incident_handoff
          ADD CONSTRAINT incident_handoff_evidence_valid
          CHECK (
            app.incident_handoff_evidence_valid(
              kind,
              incident_id,
              incident_resource_version,
              snapshot_digest,
              evidence
            )
          ) NOT VALID
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app.incident_handoff DROP CONSTRAINT incident_handoff_evidence_valid"
    )
    op.execute("DROP FUNCTION app.incident_handoff_evidence_valid(text, uuid, integer, text, jsonb)")
