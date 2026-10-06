"""Storage: raw history of every answered requirement, keyword search, and cached embeddings
for semantic search. SQLite (FTS5) locally; Postgres (tsvector) when hosted - see kb.db.

Re-ingesting a profile replaces that source's rows, so loading is idempotent. Embeddings are
cached by (model, text hash), so re-ingesting unchanged rows doesn't re-embed them.
"""

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from kb import db, settings
from kb.embed import Embedder, embedding_text, text_sha
from kb.extract import Record
from kb.profile import Profile

# Bump when the schema changes. The store is derived data, so an old database is rebuilt
# (re-run `python -m kb ingest`) rather than migrated.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,   -- profile file stem
    file         TEXT NOT NULL,
    file_sha256  TEXT NOT NULL,
    client       TEXT,
    rfp          TEXT,
    worksheet    TEXT,
    submitted    TEXT,               -- YYYY-MM-DD, used to prefer recent answers
    products     TEXT,               -- JSON list
    loaded_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    record_id        TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    sheet            TEXT NOT NULL,
    row_num          INTEGER NOT NULL,
    module           TEXT,
    req_id           TEXT,
    category         TEXT,
    subcategory      TEXT,
    section          TEXT,
    parent_text      TEXT,           -- lead-in this row only makes sense under
    requirement      TEXT NOT NULL,
    status           TEXT,           -- internal status (kb.statuses)
    answer_raw       TEXT NOT NULL,  -- JSON {signal: value as written}
    comment          TEXT,
    attributes       TEXT NOT NULL,  -- JSON of extra columns (module, cost, integration type, ...)
    warnings         TEXT NOT NULL,  -- JSON list
    embed_sha        TEXT NOT NULL,  -- sha256 of embedding_text(); key into embeddings
    client_specific  INTEGER NOT NULL DEFAULT 0,
    canonical_id     TEXT            -- set later when requirements are clustered
);
CREATE INDEX IF NOT EXISTS records_status ON records(status);
CREATE INDEX IF NOT EXISTS records_source ON records(source_id);

CREATE TABLE IF NOT EXISTS embeddings (
    model     TEXT NOT NULL,
    text_sha  TEXT NOT NULL,
    vector    BLOB NOT NULL,         -- float32, unit-normalized
    PRIMARY KEY (model, text_sha)
);

CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    record_id UNINDEXED, requirement, parent_text, comment, category,
    tokenize = 'porter unicode61'
);
"""

# Postgres: the same tables, with keyword search as a generated tsvector on records instead of
# an FTS5 table. Weights A-D follow the FTS5 bm25 column weights (requirement > lead-in > comment
# > category). The schema version lives in `meta`.
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    file         TEXT NOT NULL,
    file_sha256  TEXT NOT NULL,
    client       TEXT,
    rfp          TEXT,
    worksheet    TEXT,
    submitted    TEXT,
    products     TEXT,
    loaded_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    record_id        TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    sheet            TEXT NOT NULL,
    row_num          INTEGER NOT NULL,
    module           TEXT,
    req_id           TEXT,
    category         TEXT,
    subcategory      TEXT,
    section          TEXT,
    parent_text      TEXT,
    requirement      TEXT NOT NULL,
    status           TEXT,
    answer_raw       TEXT NOT NULL,
    comment          TEXT,
    attributes       TEXT NOT NULL,
    warnings         TEXT NOT NULL,
    embed_sha        TEXT NOT NULL,
    client_specific  INTEGER NOT NULL DEFAULT 0,
    canonical_id     TEXT,
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english'::regconfig, coalesce(requirement, '')), 'A') ||
        setweight(to_tsvector('english'::regconfig, coalesce(parent_text, '')), 'B') ||
        setweight(to_tsvector('english'::regconfig, coalesce(comment, '')), 'C') ||
        setweight(to_tsvector('english'::regconfig, coalesce(category, '') || ' ' || coalesce(subcategory, '')
                              || ' ' || coalesce(section, '')), 'D')
    ) STORED
);
CREATE INDEX IF NOT EXISTS records_status ON records(status);
CREATE INDEX IF NOT EXISTS records_source ON records(source_id);
CREATE INDEX IF NOT EXISTS records_tsv ON records USING GIN (tsv);

CREATE TABLE IF NOT EXISTS embeddings (
    model     TEXT NOT NULL,
    text_sha  TEXT NOT NULL,
    vector    BYTEA NOT NULL,
    PRIMARY KEY (model, text_sha)
);
"""

_pg_ready = False
_pg_lock = threading.Lock()


def connect(db_path: Path | None = None):
    """The knowledge base: `db_path` (default settings.DB_PATH) locally, Postgres when hosted."""
    conn = db.connect(db_path or settings.DB_PATH)
    if db.is_postgres(conn):
        _init_postgres(conn)
        return conn
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        has_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        if has_tables:
            print(f"Knowledge base schema changed (v{version} -> v{SCHEMA_VERSION}); rebuilding. "
                  "Re-run `python -m kb ingest` if this wasn't an ingest.")
            conn.executescript("DROP TABLE IF EXISTS records_fts; DROP TABLE IF EXISTS records;"
                               " DROP TABLE IF EXISTS sources; DROP TABLE IF EXISTS embeddings;")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _init_postgres(conn) -> None:
    """Create the tables once per process. Unlike the local file, the hosted knowledge base holds
    worksheets whose source files aren't in the container, so a schema change is never handled by
    dropping it: it stops here until the tables are migrated."""
    global _pg_ready
    with _pg_lock:
        if _pg_ready:
            return
        conn.executescript(PG_SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key = 'kb_schema'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta (key, value) VALUES ('kb_schema', ?)", (str(SCHEMA_VERSION),))
        elif row[0] != str(SCHEMA_VERSION):
            raise RuntimeError(f"Hosted knowledge base is schema v{row[0]}; this code expects v{SCHEMA_VERSION}. "
                               "Migrate the Postgres tables before deploying this version.")
        _pg_ready = True


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_source(conn, profile: Profile, records: list[Record]) -> None:
    src = profile.source
    fts = not db.is_postgres(conn)  # Postgres keeps its keyword index as a column of records
    with conn:
        if fts:
            conn.execute(
                "DELETE FROM records_fts WHERE record_id IN (SELECT record_id FROM records WHERE source_id = ?)",
                (profile.source_id,))
        conn.execute("DELETE FROM records WHERE source_id = ?", (profile.source_id,))
        conn.execute("DELETE FROM sources WHERE source_id = ?", (profile.source_id,))
        conn.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (profile.source_id, str(profile.file), _sha256(profile.file), src.get("client"), src.get("rfp"),
             src.get("worksheet"), str(src["submitted"]) if src.get("submitted") else None,
             json.dumps(src.get("products") or []), datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.executemany(
            "INSERT INTO records (record_id, source_id, sheet, row_num, module, req_id, category, subcategory,"
            " section, parent_text, requirement, status, answer_raw, comment, attributes, warnings, embed_sha)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(r.record_id, profile.source_id, r.sheet, r.row_num, r.module, r.req_id, r.category, r.subcategory,
              r.section, r.parent_text, r.requirement, r.status, json.dumps(r.answer_raw), r.comment,
              json.dumps(r.attributes), json.dumps(r.warnings),
              text_sha(embedding_text(r.requirement, r.parent_text))) for r in records])
        if fts:
            conn.executemany(
                "INSERT INTO records_fts (record_id, requirement, parent_text, comment, category)"
                " VALUES (?, ?, ?, ?, ?)",
                [(r.record_id, r.requirement, r.parent_text, r.comment,
                  " ".join(filter(None, [r.category, r.subcategory, r.section]))) for r in records])


def delete_source(conn, source_id: str) -> None:
    with conn:
        if not db.is_postgres(conn):
            conn.execute(
                "DELETE FROM records_fts WHERE record_id IN (SELECT record_id FROM records WHERE source_id = ?)",
                (source_id,))
        conn.execute("DELETE FROM records WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))


def embed_missing(conn, embedder: Embedder) -> int:
    """Embed every record text that has no cached vector for this model. Returns how many were added."""
    rows = conn.execute(
        "SELECT DISTINCT r.embed_sha, r.requirement, r.parent_text FROM records r"
        " LEFT JOIN embeddings e ON e.model = ? AND e.text_sha = r.embed_sha"
        " WHERE e.text_sha IS NULL", (embedder.model_name,)).fetchall()
    # Identical texts share one sha; embed each once.
    unique = {r["embed_sha"]: embedding_text(r["requirement"], r["parent_text"]) for r in rows}
    if not unique:
        return 0
    vectors = embedder.embed(list(unique.values()))
    with conn:
        conn.executemany(
            "INSERT INTO embeddings (model, text_sha, vector) VALUES (?, ?, ?)"
            " ON CONFLICT (model, text_sha) DO UPDATE SET vector = excluded.vector",
            [(embedder.model_name, sha, v.tobytes()) for sha, v in zip(unique, vectors)])
    return len(unique)


def data_version(conn) -> str:
    """Changes whenever records or embeddings change; use as a cache key for derived data."""
    sources, loaded = conn.execute("SELECT COUNT(*), MAX(loaded_at) FROM sources").fetchone()
    vectors = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    return f"{sources}|{loaded}|{vectors}"


# Every record's embedding, kept per process and model: loading them pulls every vector out of the
# database (megabytes over the network when hosted), so do it once per data change, not per search.
_vectors: dict[str, tuple[str, list[str], np.ndarray]] = {}
_vectors_lock = threading.Lock()


def _vector_matrix(conn, model: str, filters: dict | None) -> tuple[list[str], np.ndarray]:
    version = data_version(conn)
    with _vectors_lock:
        cached = _vectors.get(model)
        if not cached or cached[0] != version:
            rows = conn.execute(
                "SELECT r.record_id, e.vector FROM records r"
                " JOIN embeddings e ON e.model = ? AND e.text_sha = r.embed_sha ORDER BY r.record_id",
                (model,)).fetchall()
            matrix = (np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
                      if rows else np.zeros((0, 1), dtype=np.float32))
            cached = _vectors[model] = (version, [r[0] for r in rows], matrix)
    _, ids, matrix = cached
    where, params = _filter_sql(filters)
    if not where:
        return ids, matrix
    allowed = {r[0] for r in conn.execute(
        f"SELECT r.record_id FROM records r JOIN sources s USING (source_id) WHERE 1=1{where}", params)}
    keep = [i for i, rid in enumerate(ids) if rid in allowed]
    return [ids[i] for i in keep], matrix[keep]


def _filter_sql(filters: dict | None) -> tuple[str, list]:
    """AND-clauses over records r / sources s for {"statuses": [...], "clients": [...], "modules": [...],
    "exclude_sources": [...]}."""
    clauses, params = [], []
    for key, column in (("statuses", "r.status"), ("clients", "s.client"), ("modules", "r.module")):
        values = (filters or {}).get(key)
        if values:
            clauses.append(f"{column} IN ({','.join('?' * len(values))})")
            params.extend(values)
    excluded = (filters or {}).get("exclude_sources")
    if excluded:
        clauses.append(f"r.source_id NOT IN ({','.join('?' * len(excluded))})")
        params.extend(excluded)
    return "".join(f" AND {c}" for c in clauses), params


def _keyword_ids(conn, query: str, limit: int, filters: dict | None) -> list[str]:
    # Quote each term so user text can't be parsed as FTS syntax; OR them so partial matches still rank.
    terms = [t.replace('"', "") for t in query.split() if t.strip('"')]
    if not terms:
        return []
    where, params = _filter_sql(filters)
    if db.is_postgres(conn):
        # One plainto_tsquery per term (user text is never parsed as query syntax), OR-ed together.
        tsquery = " || ".join(["plainto_tsquery('english', ?)"] * len(terms))
        sql = (f"SELECT r.record_id FROM records r JOIN sources s USING (source_id), (SELECT {tsquery}) AS q(q)"
               f" WHERE r.tsv @@ q.q{where} ORDER BY ts_rank(r.tsv, q.q) DESC LIMIT ?")
        return [row[0] for row in conn.execute(sql, [*terms, *params, limit])]
    match = " OR ".join(f'"{t}"' for t in terms)
    sql = ("SELECT r.record_id FROM records_fts JOIN records r USING (record_id) JOIN sources s USING (source_id)"
           f" WHERE records_fts MATCH ?{where}"
           " ORDER BY bm25(records_fts, 0, 4.0, 2.0, 1.0, 1.0) LIMIT ?")
    return [row[0] for row in conn.execute(sql, [match, *params, limit])]


def _semantic_ids(conn, query: str, embedder: Embedder, limit: int,
                  filters: dict | None) -> tuple[list[str], dict[str, float]]:
    ids, matrix = _vector_matrix(conn, embedder.model_name, filters)
    if not ids:
        return [], {}
    # Brute-force cosine similarity: fine for tens of thousands of rows.
    sims = matrix @ embedder.embed([query])[0]
    top = np.argsort(-sims)[:limit]
    return [ids[i] for i in top], {ids[i]: float(sims[i]) for i in top}


def search(conn, query: str, limit: int = 10, status: str | None = None,
           mode: str = "hybrid", embedder: Embedder | None = None, candidates: int = 50,
           filters: dict | None = None) -> list[dict]:
    """Find past records similar to `query`.

    filters: {"statuses": [...], "clients": [...], "modules": [...]}; `status` is shorthand
    for a single status.

    mode: "keyword" (FTS5/BM25), "semantic" (embeddings), or "hybrid" (both, merged with
    reciprocal rank fusion so neither score scale dominates). Each result carries its cosine
    `similarity` (when semantic search ran) and `found_by`, the methods that returned it.
    """
    filters = dict(filters or {})
    if status:
        filters["statuses"] = [status]
    keyword: list[str] = []
    semantic: list[str] = []
    sims: dict[str, float] = {}
    if mode in ("keyword", "hybrid"):
        keyword = _keyword_ids(conn, query, candidates, filters)
    if mode in ("semantic", "hybrid"):
        semantic, sims = _semantic_ids(conn, query, embedder or Embedder(), candidates, filters)

    fused: dict[str, float] = {}
    for ranked in (keyword, semantic):
        for rank, record_id in enumerate(ranked):
            fused[record_id] = fused.get(record_id, 0.0) + 1.0 / (60 + rank)
    top = sorted(fused, key=fused.get, reverse=True)[:limit]
    if not top:
        return []

    rows = {r["record_id"]: dict(r) for r in conn.execute(
        "SELECT r.*, s.client, s.submitted FROM records r JOIN sources s USING (source_id)"
        f" WHERE r.record_id IN ({','.join('?' * len(top))})", top)}
    keyword_set, semantic_set = set(keyword), set(semantic)
    results = []
    for record_id in top:
        row = rows[record_id]
        row["similarity"] = sims.get(record_id)
        row["found_by"] = [name for name, ids in (("keyword", keyword_set), ("semantic", semantic_set))
                           if record_id in ids]
        results.append(row)
    return results


class Retriever:
    """Hybrid search for many queries at once (the drafter's workload).

    Loads the filtered embedding matrix once and embeds all queries in one batch, instead of
    reloading vectors per query like `search()` does.
    """

    def __init__(self, conn, embedder: Embedder, filters: dict | None = None, candidates: int = 50):
        self.conn, self.embedder, self.filters, self.candidates = conn, embedder, filters, candidates
        self.ids, self.matrix = _vector_matrix(conn, embedder.model_name, filters)

    def search_many(self, queries: list[str], limit: int = 6) -> list[list[dict]]:
        if not queries:
            return []
        vectors = self.embedder.embed(queries) if len(self.ids) else None
        ranked: list[tuple[list[str], dict[str, float], list[str]]] = []
        for i, query in enumerate(queries):
            keyword = _keyword_ids(self.conn, query, self.candidates, self.filters)
            semantic, sims = [], {}
            if vectors is not None:
                scores = self.matrix @ vectors[i]
                top = np.argsort(-scores)[:self.candidates]
                semantic = [self.ids[j] for j in top]
                sims = {self.ids[j]: float(scores[j]) for j in top}
            fused: dict[str, float] = {}
            for ids in (keyword, semantic):
                for rank, record_id in enumerate(ids):
                    fused[record_id] = fused.get(record_id, 0.0) + 1.0 / (60 + rank)
            ranked.append((sorted(fused, key=fused.get, reverse=True)[:limit], sims, keyword))

        wanted = {rid for ids, _, _ in ranked for rid in ids}
        details = {}
        wanted_list = list(wanted)
        for start in range(0, len(wanted_list), 900):  # stay under SQLite's variable limit
            chunk = wanted_list[start:start + 900]
            for r in self.conn.execute(
                    "SELECT r.*, s.client, s.submitted FROM records r JOIN sources s USING (source_id)"
                    f" WHERE r.record_id IN ({','.join('?' * len(chunk))})", chunk):
                details[r["record_id"]] = dict(r)
        out = []
        for ids, sims, keyword in ranked:
            results = []
            for rid in ids:
                row = dict(details[rid])
                row["similarity"] = sims.get(rid)
                row["found_by"] = [n for n, hit in (("keyword", rid in keyword), ("semantic", rid in sims)) if hit]
                results.append(row)
            out.append(results)
        return out
