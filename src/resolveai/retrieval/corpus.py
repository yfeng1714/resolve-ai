"""Import and search the current synthetic policy corpus.

Scope and effective-date filters run before vector ranking. Retrieved text is
context for the operator, not authority for a refund amount.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.domain.embedding import embed_text, vector_literal
from resolveai.settings import repo_root

ACCESS_SCOPES = ("operator", "approver")
_HEADING = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def corpus_dir() -> Path:
    return repo_root() / "policies" / "knowledge"


def allowed_scopes(role: str) -> tuple[str, ...]:
    if role == "approver":
        return ("operator", "approver")
    if role == "support":
        return ("operator",)
    return ()


def import_knowledge(session: Session, tenant_id: UUID, root: Path | None = None) -> int:
    """Replace the tenant's current corpus with the files on disk."""
    root = root or corpus_dir()
    session.execute(
        text("DELETE FROM app.knowledge_chunk WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    session.execute(
        text("DELETE FROM app.knowledge_document WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    count = 0
    for path in sorted(root.glob("*.md")):
        count += _import_file(session, tenant_id, path, root)
    return count


def search_knowledge(
    session: Session,
    *,
    tenant_id: UUID,
    role: str,
    query: str,
    limit: int = 3,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    scopes = allowed_scopes(role)
    if not scopes or not query.strip():
        return []
    now = now or utcnow()
    embedding = vector_literal(embed_text(query))
    rows = session.execute(
        text(
            """
            SELECT c.id, d.title, d.version, d.source_path, d.access_scope,
                   c.heading, c.body
            FROM app.knowledge_chunk AS c
            JOIN app.knowledge_document AS d ON d.id = c.document_id
            WHERE d.tenant_id = :tenant_id
              AND c.tenant_id = :tenant_id
              AND d.is_current
              AND d.access_scope = ANY(:scopes)
              AND d.effective_from <= :now
              AND (d.effective_to IS NULL OR d.effective_to > :now)
            ORDER BY c.embedding OPERATOR(public.<=>) CAST(:embedding AS public.vector)
            LIMIT :limit
            """
        ),
        {
            "tenant_id": tenant_id,
            "scopes": list(scopes),
            "now": now,
            "embedding": embedding,
            "limit": limit,
        },
    ).mappings()
    return [_citation(row) for row in rows]


def get_evidence(
    session: Session,
    *,
    tenant_id: UUID,
    role: str,
    evidence_id: UUID,
    now: datetime | None = None,
) -> dict[str, str] | None:
    scopes = allowed_scopes(role)
    if not scopes:
        return None
    now = now or utcnow()
    row = session.execute(
        text(
            """
            SELECT c.id, d.title, d.version, d.source_path, d.access_scope,
                   c.heading, c.body
            FROM app.knowledge_chunk AS c
            JOIN app.knowledge_document AS d ON d.id = c.document_id
            WHERE c.id = :evidence_id
              AND d.tenant_id = :tenant_id
              AND c.tenant_id = :tenant_id
              AND d.is_current
              AND d.access_scope = ANY(:scopes)
              AND d.effective_from <= :now
              AND (d.effective_to IS NULL OR d.effective_to > :now)
            """
        ),
        {
            "evidence_id": evidence_id,
            "tenant_id": tenant_id,
            "scopes": list(scopes),
            "now": now,
        },
    ).mappings().first()
    if row is None:
        return None
    citation = _citation(row)
    citation["body"] = str(row["body"])
    return citation


def _import_file(session: Session, tenant_id: UUID, path: Path, root: Path) -> int:
    meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
    access = meta.get("access", "operator")
    if access not in ACCESS_SCOPES:
        raise ValueError(f"{path.name} has an unknown access scope")
    document_id = uuid4()
    source = str(path.relative_to(root))
    session.execute(
        text(
            """
            INSERT INTO app.knowledge_document (
                id, tenant_id, source_path, title, version, content_hash,
                access_scope, effective_from, effective_to, is_current, created_at
            ) VALUES (
                :id, :tenant_id, :source_path, :title, :version, :content_hash,
                :access_scope, :effective_from, NULL, true, :created_at
            )
            """
        ),
        {
            "id": document_id,
            "tenant_id": tenant_id,
            "source_path": source,
            "title": meta.get("title", path.stem),
            "version": meta.get("version", "unversioned"),
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "access_scope": access,
            "effective_from": meta.get("effective_from", "2026-10-01T00:00:00+00:00"),
            "created_at": utcnow(),
        },
    )
    chunks = _chunks(body)
    for ordinal, (heading, chunk, start, end) in enumerate(chunks):
        session.execute(
            text(
                """
                INSERT INTO app.knowledge_chunk (
                    id, document_id, tenant_id, ordinal, heading, body,
                    start_offset, end_offset, embedding
                ) VALUES (
                    :id, :document_id, :tenant_id, :ordinal, :heading, :body,
                    :start_offset, :end_offset, CAST(:embedding AS public.vector)
                )
                """
            ),
            {
                "id": uuid4(),
                "document_id": document_id,
                "tenant_id": tenant_id,
                "ordinal": ordinal,
                "heading": heading,
                "body": chunk,
                "start_offset": start,
                "end_offset": end,
                "embedding": vector_literal(embed_text(f"{heading}\n{chunk}")),
            },
        )
    return 1


def _chunks(body: str) -> list[tuple[str, str, int, int]]:
    matches = list(_HEADING.finditer(body))
    if not matches:
        text_body = body.strip()
        return [("Document", text_body, 0, len(body))] if text_body else []
    chunks: list[tuple[str, str, int, int]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        heading = match.group(1).strip()
        chunk = body[start:end].strip()
        if chunk:
            chunks.append((heading, chunk, start, end))
    return chunks


def _split_front_matter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end < 0:
        return {}, raw
    meta: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, raw[end + 5 :]


def _citation(row: object) -> dict[str, str]:
    mapping = row if isinstance(row, dict) else dict(row)  # type: ignore[arg-type]
    body = str(mapping["body"])
    return {
        "evidence_id": str(mapping["id"]),
        "title": str(mapping["title"]),
        "version": str(mapping["version"]),
        "source_path": str(mapping["source_path"]),
        "access_scope": str(mapping["access_scope"]),
        "heading": str(mapping["heading"]),
        "snippet": body[:480],
    }
