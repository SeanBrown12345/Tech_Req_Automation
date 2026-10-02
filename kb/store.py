"""SQLite storage: raw history of every answered requirement, plus FTS5 keyword search.

Re-ingesting a profile replaces that source's rows, so loading is idempotent.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from kb.extract import Record
from kb.profile import Profile

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
    client_specific  INTEGER NOT NULL DEFAULT 0,
    canonical_id     TEXT            -- set later when requirements are clustered
);
CREATE INDEX IF NOT EXISTS records_status ON records(status);
CREATE INDEX IF NOT EXISTS records_source ON records(source_id);

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
    conn.executescript(SCHEMA)
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
            " section, parent_text, requirement, status, answer_raw, comment, attributes, warnings)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(r.record_id, profile.source_id, r.sheet, r.row_num, r.module, r.req_id, r.category, r.subcategory,
              r.section, r.parent_text, r.requirement, r.status, json.dumps(r.answer_raw), r.comment,
              json.dumps(r.attributes), json.dumps(r.warnings)) for r in records])
        conn.executemany(
            "INSERT INTO records_fts (record_id, requirement, parent_text, comment, category) VALUES (?, ?, ?, ?, ?)",
            [(r.record_id, r.requirement, r.parent_text, r.comment,
              " ".join(filter(None, [r.category, r.subcategory, r.section]))) for r in records])


def search(conn: sqlite3.Connection, query: str, limit: int = 10, status: str | None = None) -> list[sqlite3.Row]:
    # Quote each term so user text can't be parsed as FTS syntax; OR them so partial matches still rank.
    terms = [t.replace('"', "") for t in query.split() if t.strip('"')]
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    sql = (
        "SELECT r.*, s.client, s.submitted, bm25(records_fts, 0, 4.0, 2.0, 1.0, 1.0) AS score"
        " FROM records_fts JOIN records r USING (record_id) JOIN sources s USING (source_id)"
        " WHERE records_fts MATCH ?"
    )
    params: list = [match]
    if status:
        sql += " AND r.status = ?"
        params.append(status)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()
