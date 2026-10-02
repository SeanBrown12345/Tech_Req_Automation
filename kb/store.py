"""SQLite storage: raw history of every answered requirement, FTS5 keyword search, and
cached embeddings for semantic search.

Re-ingesting a profile replaces that source's rows, so loading is idempotent. Embeddings are
cached by (model, text hash), so re-ingesting unchanged rows doesn't re-embed them.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

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


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_source(conn: sqlite3.Connection, profile: Profile, records: list[Record]) -> None:
    src = profile.source
    with conn:
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
        conn.executemany(
            "INSERT INTO records_fts (record_id, requirement, parent_text, comment, category) VALUES (?, ?, ?, ?, ?)",
            [(r.record_id, r.requirement, r.parent_text, r.comment,
              " ".join(filter(None, [r.category, r.subcategory, r.section]))) for r in records])


def embed_missing(conn: sqlite3.Connection, embedder: Embedder) -> int:
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
            "INSERT OR REPLACE INTO embeddings (model, text_sha, vector) VALUES (?, ?, ?)",
            [(embedder.model_name, sha, v.tobytes()) for sha, v in zip(unique, vectors)])
    return len(unique)


def _keyword_ids(conn: sqlite3.Connection, query: str, limit: int, status: str | None) -> list[str]:
    # Quote each term so user text can't be parsed as FTS syntax; OR them so partial matches still rank.
    terms = [t.replace('"', "") for t in query.split() if t.strip('"')]
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    sql = ("SELECT r.record_id FROM records_fts JOIN records r USING (record_id)"
           " WHERE records_fts MATCH ?")
    params: list = [match]
    if status:
        sql += " AND r.status = ?"
        params.append(status)
    sql += " ORDER BY bm25(records_fts, 0, 4.0, 2.0, 1.0, 1.0) LIMIT ?"
    params.append(limit)
    return [row[0] for row in conn.execute(sql, params)]


def _semantic_ids(conn: sqlite3.Connection, query: str, embedder: Embedder, limit: int,
                  status: str | None) -> tuple[list[str], dict[str, float]]:
    sql = ("SELECT r.record_id, e.vector FROM records r"
           " JOIN embeddings e ON e.model = ? AND e.text_sha = r.embed_sha")
    params: list = [embedder.model_name]
    if status:
        sql += " WHERE r.status = ?"
        params.append(status)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return [], {}
    # Brute-force cosine similarity: fine for tens of thousands of rows. Replace with
    # pgvector / Azure AI Search when hosted.
    matrix = np.frombuffer(b"".join(r["vector"] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    sims = matrix @ embedder.embed([query])[0]
    top = np.argsort(-sims)[:limit]
    return [rows[i]["record_id"] for i in top], {rows[i]["record_id"]: float(sims[i]) for i in top}


def search(conn: sqlite3.Connection, query: str, limit: int = 10, status: str | None = None,
           mode: str = "hybrid", embedder: Embedder | None = None, candidates: int = 50) -> list[dict]:
    """Find past records similar to `query`.

    mode: "keyword" (FTS5/BM25), "semantic" (embeddings), or "hybrid" (both, merged with
    reciprocal rank fusion so neither score scale dominates). Each result carries its cosine
    `similarity` (when semantic search ran) and `found_by`, the methods that returned it.
    """
    keyword: list[str] = []
    semantic: list[str] = []
    sims: dict[str, float] = {}
    if mode in ("keyword", "hybrid"):
        keyword = _keyword_ids(conn, query, candidates, status)
    if mode in ("semantic", "hybrid"):
        semantic, sims = _semantic_ids(conn, query, embedder or Embedder(), candidates, status)

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
