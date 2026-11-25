"""Hybrid, vector-only, and lexical-only retrieval. No paid embedding calls."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from tests.support import HARBOR_TENANT_ID, NORTHWIND_TENANT_ID

from resolveai.db.session import make_engine, make_session_factory
from resolveai.retrieval.corpus import (
    CANDIDATE_LIMIT,
    fuse_candidates,
    import_knowledge,
    search_knowledge,
)

NORTHWIND = UUID(NORTHWIND_TENANT_ID)
HARBOR = UUID(HARBOR_TENANT_ID)


def test_rrf_scores_follow_the_published_constant() -> None:
    vector = [_row("a", "a.md"), _row("b", "b.md")]
    lexical = [_row("b", "b.md"), _row("c", "c.md")]
    fused = fuse_candidates(vector, lexical, limit=3)
    scores = {str(item["row"]["id"]): item["score"] for item in fused}  # type: ignore[index]
    assert scores["b"] == (1 / 61) + (1 / 62)
    assert scores["a"] == 1 / 61
    assert scores["c"] == 1 / 62
    assert [str(item["row"]["id"]) for item in fused] == ["b", "a", "c"]  # type: ignore[index]

    tied = fuse_candidates([_row("later", "z.md")], [_row("earlier", "a.md")], limit=2)
    assert [str(item["row"]["source_path"]) for item in tied] == ["a.md", "z.md"]  # type: ignore[index]


def test_filters_apply_before_every_mode(settings, seeded) -> None:
    del seeded
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        for mode in ("hybrid", "vector", "lexical"):
            support = search_knowledge(
                session,
                tenant_id=NORTHWIND,
                role="support",
                query="approver accepts or rejects the exact proposal",
                mode=mode,
            )
            approver = search_knowledge(
                session,
                tenant_id=NORTHWIND,
                role="approver",
                query="approver accepts or rejects the exact proposal",
                mode=mode,
            )
            harbor = search_knowledge(
                session,
                tenant_id=HARBOR,
                role="approver",
                query="duplicate payment refund",
                mode=mode,
            )
            outsider = search_knowledge(
                session,
                tenant_id=NORTHWIND,
                role="auditor",
                query="duplicate payment refund",
                mode=mode,
            )
            assert all(item["source_path"] != "approver_review.md" for item in support)
            assert any(item["source_path"] == "approver_review.md" for item in approver)
            assert outsider == []
            assert harbor
            assert _tenants(session, [item["evidence_id"] for item in harbor]) == {HARBOR}
            assert _tenants(session, [item["evidence_id"] for item in approver]) == {NORTHWIND}
    engine.dispose()


def test_blank_and_unmatched_queries_leave_the_vector_order(settings, seeded) -> None:
    del seeded
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        for query in ("", "   ", "the", "zzzz-not-a-policy-term"):
            vector = _labels(
                search_knowledge(
                    session, tenant_id=NORTHWIND, role="support", query=query, mode="vector"
                )
            )
            hybrid = search_knowledge(
                session, tenant_id=NORTHWIND, role="support", query=query, mode="hybrid"
            )
            lexical = search_knowledge(
                session, tenant_id=NORTHWIND, role="support", query=query, mode="lexical"
            )
            assert lexical == []
            assert _labels(hybrid) == vector
            assert all(item["provenance"] == "vector" for item in hybrid)
    engine.dispose()


def test_tied_chunks_keep_the_same_logical_twenty_across_imports(
    settings, seeded, tmp_path: Path
) -> None:
    del seeded
    root = tmp_path / "ties"
    root.mkdir()
    for number in range(1, 22):
        (root / f"tie-{number:02d}.md").write_text(
            "---\n"
            "title: Tied policy\n"
            "version: tie-v1\n"
            "access: operator\n"
            "effective_from: 2026-10-01T00:00:00+00:00\n"
            "---\n\n"
            "## Same heading\n\n"
            "quorumtie token appears once.\n",
            encoding="utf-8",
        )
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        import_knowledge(session, NORTHWIND, root)
        session.commit()
        first = _candidate_labels(session, "quorumtie")
        first_ids = _candidate_ids(session, "quorumtie")
        import_knowledge(session, NORTHWIND, root)
        session.commit()
        second = _candidate_labels(session, "quorumtie")
        second_ids = _candidate_ids(session, "quorumtie")
    engine.dispose()
    expected = [(f"tie-{number:02d}.md", "tie-v1", 0) for number in range(1, 21)]
    assert first["vector"] == expected
    assert first["lexical"] == expected
    assert first["hybrid"] == expected[:3]
    assert second == first
    assert first_ids != second_ids
    assert len(first_ids["vector"]) == CANDIDATE_LIMIT
    assert ("tie-21.md", "tie-v1", 0) not in first["vector"]


def test_development_queries_record_hits_and_misses(settings, seeded) -> None:
    del seeded
    catalog = json.loads(
        (Path(__file__).resolve().parents[1] / "policies" / "retrieval_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        for case in catalog["queries"]:
            for mode, expected in case["top"].items():
                hits = search_knowledge(
                    session,
                    tenant_id=NORTHWIND,
                    role="support",
                    query=case["query"],
                    mode=mode,
                )
                assert [_label(item) for item in hits] == [tuple(item) for item in expected]
                assert all(item["evidence_id"] for item in hits)
    engine.dispose()


def test_hidden_chunks_stay_out_of_both_lists_and_evidence_lookup(
    settings, seeded
) -> None:
    del seeded
    from resolveai.clock import utcnow
    from resolveai.domain.embedding import embed_text, vector_literal
    from resolveai.retrieval.corpus import get_evidence

    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    hidden = {
        "not-current": _hidden_document(is_current=False, start="2020-01-01", end=None),
        "future": _hidden_document(is_current=True, start="2099-01-01", end=None),
        "closed": _hidden_document(is_current=True, start="2020-01-01", end="2020-02-01"),
    }
    with factory() as session:
        ids = {
            name: _insert_hidden(session, spec, utcnow, embed_text, vector_literal)
            for name, spec in hidden.items()
        }
        session.commit()
        params = _params("hiddenwindowtoken")
        params["candidate_limit"] = 500
        from resolveai.retrieval import corpus

        vector_ids = {str(row["id"]) for row in corpus._vector_candidates(session, params)}
        lexical_ids = {str(row["id"]) for row in corpus._lexical_candidates(session, params)}
        for evidence_id in ids.values():
            assert evidence_id not in vector_ids
            assert evidence_id not in lexical_ids
            for mode in ("hybrid", "vector", "lexical"):
                hits = search_knowledge(
                    session,
                    tenant_id=NORTHWIND,
                    role="support",
                    query="hiddenwindowtoken",
                    mode=mode,
                )
                assert all(item["evidence_id"] != evidence_id for item in hits)
            assert (
                get_evidence(
                    session,
                    tenant_id=NORTHWIND,
                    role="support",
                    evidence_id=UUID(evidence_id),
                )
                is None
            )
        approver_id = session.execute(
            text(
                """
                SELECT c.id
                FROM app.knowledge_chunk AS c
                JOIN app.knowledge_document AS d ON d.id = c.document_id
                WHERE d.tenant_id = :tenant_id
                  AND d.access_scope = 'approver'
                  AND d.is_current
                LIMIT 1
                """
            ),
            {"tenant_id": NORTHWIND},
        ).scalar_one()
        northwind_id = session.execute(
            text(
                """
                SELECT c.id
                FROM app.knowledge_chunk AS c
                WHERE c.tenant_id = :tenant_id
                LIMIT 1
                """
            ),
            {"tenant_id": NORTHWIND},
        ).scalar_one()
        harbor_id = session.execute(
            text(
                """
                SELECT c.id
                FROM app.knowledge_chunk AS c
                WHERE c.tenant_id = :tenant_id
                LIMIT 1
                """
            ),
            {"tenant_id": HARBOR},
        ).scalar_one()
        assert (
            get_evidence(
                session, tenant_id=NORTHWIND, role="support", evidence_id=approver_id
            )
            is None
        )
        assert (
            get_evidence(
                session, tenant_id=NORTHWIND, role="approver", evidence_id=approver_id
            )
            is not None
        )
        assert (
            get_evidence(
                session, tenant_id=HARBOR, role="support", evidence_id=northwind_id
            )
            is None
        )
        assert (
            get_evidence(
                session, tenant_id=NORTHWIND, role="support", evidence_id=harbor_id
            )
            is None
        )
        index = session.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'app' AND indexname = 'knowledge_chunk_lexical'
                """
            )
        ).scalar_one()
    engine.dispose()
    assert index == "knowledge_chunk_lexical"


def _hidden_document(*, is_current: bool, start: str, end: str | None) -> dict[str, object]:
    return {"is_current": is_current, "start": start, "end": end}


def _insert_hidden(session, spec: dict[str, object], utcnow, embed_text, vector_literal) -> str:
    document_id = uuid4()
    chunk_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO app.knowledge_document (
                id, tenant_id, source_path, title, version, content_hash,
                access_scope, effective_from, effective_to, is_current, created_at
            ) VALUES (
                :id, :tenant_id, :source_path, 'Hidden window', 'hidden-v1', 'hash',
                'operator', :effective_from, :effective_to, :is_current, :created_at
            )
            """
        ),
        {
            "id": document_id,
            "tenant_id": NORTHWIND,
            "source_path": f"hidden-{chunk_id}.md",
            "effective_from": f"{spec['start']}T00:00:00+00:00",
            "effective_to": None if spec["end"] is None else f"{spec['end']}T00:00:00+00:00",
            "is_current": spec["is_current"],
            "created_at": utcnow(),
        },
    )
    session.execute(
        text(
            """
            INSERT INTO app.knowledge_chunk (
                id, document_id, tenant_id, ordinal, heading, body,
                start_offset, end_offset, embedding
            ) VALUES (
                :id, :document_id, :tenant_id, 0, 'Hidden window',
                'hiddenwindowtoken labels a row outside the current window',
                0, 10, CAST(:embedding AS public.vector)
            )
            """
        ),
        {
            "id": chunk_id,
            "document_id": document_id,
            "tenant_id": NORTHWIND,
            "embedding": vector_literal(embed_text("hiddenwindowtoken")),
        },
    )
    return str(chunk_id)


def test_b1_uses_the_shared_search(client, api_app, restore_runtime, monkeypatch) -> None:
    del restore_runtime
    from tests.support import support_headers

    from resolveai.agent.runner import search_knowledge as runner_search
    from resolveai.evaluation.catalog import use_baseline

    seen: list[str] = []

    def wrapped(*args, **kwargs):
        seen.append(str(kwargs.get("mode", "")))
        return runner_search(*args, **kwargs)

    monkeypatch.setattr("resolveai.agent.runner.search_knowledge", wrapped)
    use_baseline(api_app)
    response = client.post(
        "/tickets/55555555-5555-4555-8555-555555555501/runs",
        headers=support_headers(client),
    )
    assert response.status_code == 200, response.text
    assert seen == ["hybrid"]
    citations: list[dict[str, str]] = []
    for run in response.json()["data"]["runs"]:
        for item in run["observations"]:
            if item["tool"] == "search_knowledge":
                citations.extend(item["data"]["citations"])
    assert citations
    assert citations[0]["title"] == "Duplicate payment refunds"
    assert "amount" not in citations[0]


def test_code_point_order_selects_the_same_twenty_before_the_limit(
    settings, seeded, tmp_path: Path
) -> None:
    del seeded
    names = [f"{number:02d}.md" for number in range(19)] + ["a-b.md", "aa.md"]
    root = tmp_path / "punctuation"
    root.mkdir()
    for name in names:
        (root / name).write_text(_same_policy("tie-v1"), encoding="utf-8")
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        import_knowledge(session, NORTHWIND, root)
        session.commit()
        first = _candidate_labels(session, "quorumtie")
        first_ids = _candidate_ids(session, "quorumtie")
        import_knowledge(session, NORTHWIND, root)
        session.commit()
        second = _candidate_labels(session, "quorumtie")
        second_ids = _candidate_ids(session, "quorumtie")
    engine.dispose()
    expected = [(name, "tie-v1", 0) for name in sorted(names)[:CANDIDATE_LIMIT]]
    assert first["vector"] == expected
    assert first["lexical"] == expected
    assert ("a-b.md", "tie-v1", 0) in expected
    assert ("aa.md", "tie-v1", 0) not in expected
    assert second == first
    assert first_ids != second_ids


def test_case_script_version_and_ordinal_use_the_same_order(settings, seeded) -> None:
    del seeded
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        _replace(session, [_doc("A.md", "v1"), _doc("a.md", "v1")])
        case_order = _candidate_labels(session, "quorumtie")
        _replace(session, [_doc("z.md", "v1"), _doc("é.md", "v1")])
        script_order = _candidate_labels(session, "quorumtie")
        _replace(
            session,
            [
                _doc(
                    "ordinal.md",
                    "v1",
                    chunks=((10, "Same"), (2, "Same")),
                )
            ],
        )
        ordinal_order = _candidate_labels(session, "quorumtie")
        session.execute(text("DROP INDEX app.knowledge_document_current_source"))
        try:
            _replace(
                session,
                [_doc("shared.md", "aa"), _doc("shared.md", "a-b")],
            )
            version_order = _candidate_labels(session, "quorumtie")
        finally:
            session.rollback()
    engine.dispose()
    assert case_order["vector"] == [("A.md", "v1", 0), ("a.md", "v1", 0)]
    assert case_order["lexical"] == case_order["vector"]
    assert script_order["vector"] == [("z.md", "v1", 0), ("é.md", "v1", 0)]
    assert script_order["lexical"] == script_order["vector"]
    assert ordinal_order["vector"] == [("ordinal.md", "v1", 2), ("ordinal.md", "v1", 10)]
    assert ordinal_order["lexical"] == ordinal_order["vector"]
    assert version_order["vector"] == [("shared.md", "a-b", 0), ("shared.md", "aa", 0)]
    assert version_order["lexical"] == version_order["vector"]


def test_equal_branch_scores_follow_the_candidate_order(settings, seeded) -> None:
    del seeded
    import zlib

    query = "pipelinelexneedle"
    bucket = zlib.crc32(query.encode()) % 32
    token = next(
        f"collision{i}" for i in range(10000) if zlib.crc32(f"collision{i}".encode()) % 32 == bucket
    )
    filler = (
        "amber bronze cedar delta ember flint grove ivory jasper kelp linen maple noble "
        "opal pebble quartz raven sable thorn violet willow yarrow zircon"
    )
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    rounds: list[tuple[list[str], list[str], set[str]]] = []
    with factory() as session:
        for _ in range(2):
            _clear(session)
            collision_paths = ["aa.md", *[f"zz{i:02d}.md" for i in range(19)]]
            for path in collision_paths:
                _insert(session, NORTHWIND, path, "v1", token)
            lexical_body = f"{query} {filler}"
            _insert(session, NORTHWIND, "a-b.md", "v1", lexical_body)
            for path in ("aa.md", "a-b.md"):
                _insert(session, HARBOR, path, "v1", token)
            session.commit()
            params = _params(query)
            from resolveai.retrieval import corpus

            vector = corpus._vector_candidates(session, params)
            lexical = corpus._lexical_candidates(session, params)
            hybrid = search_knowledge(
                session, tenant_id=NORTHWIND, role="support", query=query, mode="hybrid"
            )
            harbor_params = dict(params)
            harbor_params["tenant_id"] = HARBOR
            tie_order = [
                str(row["source_path"]) for row in corpus._vector_candidates(session, harbor_params)
            ]
            assert [str(row["source_path"]) for row in vector] == sorted(collision_paths)
            assert [str(row["source_path"]) for row in lexical] == ["a-b.md"]
            assert str(lexical[0]["id"]) not in {str(row["id"]) for row in vector}
            assert tie_order == sorted(["aa.md", "a-b.md"])
            assert [item["source_path"] for item in hybrid[:2]] == tie_order
            assert [item["provenance"] for item in hybrid[:2]] == ["lexical", "vector"]
            rounds.append(
                (
                    [item["source_path"] for item in hybrid],
                    [str(row["id"]) for row in vector],
                    {str(lexical[0]["id"])},
                )
            )
    engine.dispose()
    assert rounds[0][0] == rounds[1][0]
    assert set(rounds[0][1]).isdisjoint(rounds[1][1])
    assert rounds[0][2].isdisjoint(rounds[1][2])


def _row(identity: str, source: str) -> dict[str, object]:
    return {"id": identity, "source_path": source, "version": "v1", "ordinal": 0}


def _labels(hits: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    return [_label(item) for item in hits]


def _label(item: dict[str, str]) -> tuple[str, str, str]:
    return (item["source_path"], item["version"], item["ordinal"])


def _candidate_labels(session, query: str) -> dict[str, list[tuple[str, str, int]]]:
    from resolveai.retrieval import corpus

    params = _params(query)
    vector = corpus._vector_candidates(session, params)
    lexical = corpus._lexical_candidates(session, params)
    hybrid = search_knowledge(
        session, tenant_id=NORTHWIND, role="support", query=query, mode="hybrid"
    )
    return {
        "vector": [
            (str(row["source_path"]), str(row["version"]), int(row["ordinal"])) for row in vector
        ],
        "lexical": [
            (str(row["source_path"]), str(row["version"]), int(row["ordinal"])) for row in lexical
        ],
        "hybrid": [(item[0], item[1], int(item[2])) for item in _labels(hybrid)],
    }


def _candidate_ids(session, query: str) -> dict[str, list[str]]:
    from resolveai.retrieval import corpus

    params = _params(query)
    return {
        "vector": [str(row["id"]) for row in corpus._vector_candidates(session, params)],
        "lexical": [str(row["id"]) for row in corpus._lexical_candidates(session, params)],
    }


def _params(query: str) -> dict[str, object]:
    from resolveai.clock import utcnow

    return {
        "tenant_id": NORTHWIND,
        "scopes": ["operator"],
        "now": utcnow(),
        "query": query,
        "candidate_limit": CANDIDATE_LIMIT,
    }


def _same_policy(version: str) -> str:
    return (
        "---\n"
        "title: Tied policy\n"
        f"version: {version}\n"
        "access: operator\n"
        "effective_from: 2026-10-01T00:00:00+00:00\n"
        "---\n\n"
        "## Same heading\n\n"
        "quorumtie token appears once.\n"
    )


def _doc(
    path: str,
    version: str,
    *,
    chunks: tuple[tuple[int, str], ...] = ((0, ""),),
) -> dict[str, object]:
    return {"path": path, "version": version, "chunks": chunks}


def _replace(session, documents: list[dict[str, object]]) -> None:
    _clear(session)
    for document in documents:
        chunks = document["chunks"]
        assert isinstance(chunks, tuple)
        _insert(
            session,
            NORTHWIND,
            str(document["path"]),
            str(document["version"]),
            "quorumtie token appears once.",
            chunks=chunks,
        )


def _clear(session) -> None:
    for tenant_id in (NORTHWIND, HARBOR):
        session.execute(
            text("DELETE FROM app.knowledge_chunk WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        session.execute(
            text("DELETE FROM app.knowledge_document WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )


def _insert(
    session,
    tenant_id: UUID,
    path: str,
    version: str,
    body: str,
    *,
    chunks: tuple[tuple[int, str], ...] = ((0, ""),),
) -> None:
    from resolveai.clock import utcnow
    from resolveai.domain.embedding import embed_text, vector_literal

    document_id = uuid4()
    created_at = utcnow()
    session.execute(
        text(
            """
            INSERT INTO app.knowledge_document (
                id, tenant_id, source_path, title, version, content_hash,
                access_scope, effective_from, effective_to, is_current, created_at
            ) VALUES (
                :id, :tenant_id, :source_path, 'Tied policy', :version, 'hash',
                'operator', :effective_from, NULL, true, :created_at
            )
            """
        ),
        {
            "id": document_id,
            "tenant_id": tenant_id,
            "source_path": path,
            "version": version,
            "effective_from": created_at,
            "created_at": created_at,
        },
    )
    for ordinal, heading in chunks:
        session.execute(
            text(
                """
                INSERT INTO app.knowledge_chunk (
                    id, document_id, tenant_id, ordinal, heading, body,
                    start_offset, end_offset, embedding
                ) VALUES (
                    :id, :document_id, :tenant_id, :ordinal, :heading, :body,
                    0, 1, CAST(:embedding AS public.vector)
                )
                """
            ),
            {
                "id": uuid4(),
                "document_id": document_id,
                "tenant_id": tenant_id,
                "ordinal": ordinal,
                "heading": heading,
                "body": body,
                "embedding": vector_literal(embed_text(f"{heading}\n{body}")),
            },
        )


def _tenants(session, evidence_ids: list[str]) -> set[UUID]:
    if not evidence_ids:
        return set()
    rows = session.execute(
        text("SELECT DISTINCT tenant_id FROM app.knowledge_chunk WHERE id = ANY(:ids)"),
        {"ids": evidence_ids},
    ).scalars()
    return set(rows)
