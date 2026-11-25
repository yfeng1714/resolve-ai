# ADR 0012: Hybrid policy retrieval

Status: Implementation accepted locally on 2026-11-25, closing M3. The design
was accepted on 2026-11-06 and initially implemented on 2026-11-12. The accepted
repair gives textual tie keys one code-point order and places the rare
development token in the heading. Acceptance uses local fake-provider checks
and does not authorize a hosted embedding model or the M4 evaluation.

Problem: ADR 0004 accepts lexical plus vector retrieval with reciprocal rank
fusion for the Portfolio, and the product scope asks for a later ablation of
hybrid against vector-only. Before this slice, the running search had only
the vector order. A 32-dimensional token-bucket vector can miss a rare exact
policy term, and a lexical search can miss a paraphrase. There was no fused
result and no development check that separates those misses.

Alternatives: replace the vector path with lexical search; add a reranker;
buy a hosted embedding model and treat its neighbors as the policy; fuse
first and apply tenant or access filters afterward; tune fusion weights on
the frozen evaluation set.

Choice: keep the current filters first. Search the filtered chunks twice,
once with the existing vector order and once with Postgres full text. Fuse
those two ranked lists with reciprocal rank fusion, constant `k = 60`. That
constant is the usual published default. It is not a weight fitted on held-out
cases. Vector-only stays as an explicit mode so a later ablation can turn the
lexical list off without a second corpus. Hard financial rules stay
deterministic when retrieval returns nothing.

## What exists today

`search_knowledge` in `src/resolveai/retrieval/corpus.py` keeps a chunk only
when the document belongs to the tenant, is current, is inside its effective
window, and is visible to the caller's role. Both branches use that filtered
set before they rank or truncate. The vector comes from `embed_text` in
`src/resolveai/domain/embedding.py`: lowercase tokens are counted into 32
buckets with `zlib.crc32`, and the bucket counts are normalized. The same
function is stored on `app.knowledge_chunk.embedding`, which is `vector(32)`
from migration `20261013_0002`. The lexical branch uses the expression below.
Migration `20261112_0014` adds the expression GIN index and does not change
embeddings. Settings reject a non-fake embedding provider while the budget
is zero. `RETRIEVAL_MODE` accepts `hybrid`, `vector`, or `lexical`. Hybrid
is the default.

This is a deterministic local order over the synthetic policy files. It is
not a measurement of a hosted embedding model, and a neighbor in this space
is not evidence that a live model understood the policy. Citations carry the
chunk id, title, version, source, heading, and body, plus a provenance label
of `vector`, `lexical`, or `both`. Retrieved text does not set a refund
amount. The knowledge tables have tenant row-level security from the
two-tenant migration. An index is not the security boundary.

## Retrieval contract

The lexical document and the lexical query use one expression:

```text
document = to_tsvector('english', heading || ' ' || body)
parsed = websearch_to_tsquery('english', query_text)
```

A chunk is a lexical candidate only when `document @@ parsed` is true. Rank
is applied after that match. `ts_rank` never admits a nonmatching row.

For one query, in order:

1. Apply the current tenant, current-version, access-scope, and effective-date
   predicates. Role visibility stays `allowed_scopes`. An approver may see
   operator and approver text. Support may see operator text. Any other role
   receives no rows. Both candidate branches use this filtered set. Neither
   branch truncates before the predicate.
2. Vector branch: order the filtered chunks by `embedding <=> query_vector`
   ascending, then by `source_path` ascending, document `version` ascending,
   and chunk `ordinal` ascending. Text ascending means Unicode code-point
   order: PostgreSQL uses `COLLATE "C"` on `source_path` and `version`, which
   matches Python string order for UTF-8. `ordinal` is an integer, so 2
   precedes 10. Assign ranks 1, 2, 3, … in that order.
   Keep the first 20. The same order and the same 20 are the vector-only
   result; vector-only then returns the first 3. The runtime chunk id is not
   part of this order.
3. Lexical branch: if `parsed` is null or empty, including a blank query and
   a stopword-only query, the lexical list is empty. Otherwise keep rows
   that satisfy `document @@ parsed`, order them by `ts_rank(document, parsed)`
   descending, then by the same `source_path`, `version`, and `ordinal`, and
   keep the first 20. Fewer than 20 matches returns only those matches. A
   query with no matches returns an empty list. The list is never padded
   with nonmatching rows. Lexical-only mode uses this same order and returns
   its first 3.
4. Hybrid fusion reads those two already limited lists. A chunk's score is
   the sum of `1 / (60 + rank)` across the lists that contain it. An empty
   list adds nothing. Absence from a list adds nothing. Order the fused
   chunks by score descending, then by `source_path`, `version`, and
   `ordinal`. Return the first 3. The runtime chunk id is not a tie key.
5. Each citation keeps the runtime chunk id, title, version, source, heading,
   and body, and records whether it came from the vector list, the lexical
   list, or both. That label is for development inspection. It is not a
   permission and not a financial fact.

The default mode is hybrid. The no-context
ablation is a run that does not call `search_knowledge`. B0, B1, and B2 call
this same function. They do not keep private copies of the corpus.

## Storage

One expression GIN index is enough:

```sql
CREATE INDEX knowledge_chunk_lexical
ON app.knowledge_chunk
USING gin (to_tsvector('english', heading || ' ' || body));
```

No embedding column changes. No corpus reset is required for the index.
Downgrade drops the index and does not delete documents or chunks. If a
later implementation needs a stored `tsvector` because the expression cannot
use the index, that column must be generated from `heading` and `body` and
dropped on the way back. It must not become a second place to edit policy
text.

## Development comparison

The importer in `src/resolveai/retrieval/corpus.py` assigns a new `uuid4()` to
every document and chunk on each import, then deletes the tenant's previous
rows. A checked-in runtime evidence UUID will not survive a clean reseed.
Development expectations therefore use a logical label:
`(source_path, document version, chunk ordinal)`. The test resolves that
label to the current chunk id after import. Citations and traces still return
that runtime id. No schema change is required to keep the opaque ids stable.

Reproducibility for this slice means a clean reimport of the same policy
files. The labels, the candidate order, and the top 3 must match again even
though the UUIDs differ. A database that has not been reimported stays
reproducible for the UUIDs it already stored. The acceptance check is the
clean reimport.

The comparison set is a small list of fixed queries checked into the
repository with those logical labels. It is for this slice's tests. It is
not the 40-case ablation, not the 160-case frozen set, and not a place to
tune `k`. Passing it does not establish retrieval quality for a hosted model
and does not establish an agent benefit.

Each query runs in hybrid, vector-only, and lexical-only modes. The vector
order inside hybrid is the vector-only order. The lexical order inside
hybrid is the lexical-only order. The test records the resolved chunk ids and
whether the expected label is inside the top 3. At least one query must be an
exact rare heading token that the 32-bucket hash can place below a colliding
chunk. At least one query must be a paraphrase whose exact lexemes are weak
and whose bucket overlap is stronger. A mode may miss. The test reports the
miss. It does not lower the safety checks to hide it.

Two tie fixtures are required. Chunks with equal vector distance are ordered
by `(source_path, document version, chunk ordinal)` before the limit of 20,
and the same logical key applies to equal lexical rank and to equal fused
scores. One fixture has more than 20 chunks tied for the boundary. The
included set is the 20 lowest logical keys, and the next key is absent.
Their ranks are then 1 through 20 in that logical order. Two imports that
assign fresh chunk UUIDs to the same files must keep that same set and that
same order. The citations still return each import's runtime ids.

No paid embedding call is part of this design. A hosted embedding experiment
needs its own reviewed budget and owner authorization. Until that exists,
published retrieval results from this repository describe the crc32 buckets
and the lexical index, not a live semantic model.

## Acceptance checks

These checks are in the local suite. Passing them is not a hosted-model
measurement and it is not an agent-benefit result.

- A support caller cannot read an approver-scoped chunk through hybrid,
  vector-only, or lexical-only search. A second tenant cannot read the first
  tenant's chunks. A document that is not current, or is outside its
  effective window, is absent from both candidate lists.
- The fused top 3 is reproducible after a clean reimport for the same
  files, query, and role. Expectations are logical labels, not stored UUIDs.
  The returned citations still carry the runtime chunk ids.
- Equal distance, equal lexical rank, and equal fused score use
  `(source_path, document version, chunk ordinal)` before `LIMIT`. A 21-way
  tie keeps the same 20 logical keys, in the same order, across two imports
  that assign fresh chunk UUIDs.
- A stopword-only query, an empty query, and a query that matches nothing
  produce an empty lexical list. That list does not change the vector order.
- The rare-term query and the paraphrase query behave as the development
  comparison requires, including an honest miss.
- Refund, cancellation, and incident decisions on an empty citation list
  match the decisions they make today. Retrieval text still cannot set an
  amount, choose a subscription, or name an incident.
- B1 uses the same search function and the same filters.

Do not start the 240-case evaluation or the three live ablations from this
slice. Those remain M4 work under a separate plan and spending limit.
