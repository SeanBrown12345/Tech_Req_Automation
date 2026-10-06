"""Database connections: SQLite files locally, Postgres when KB_DATABASE_URL is set (hosted).

Callers write SQLite-style SQL with `?` placeholders and use the connection like sqlite3's:
`conn.execute(...)`, `with conn:` for a transaction, rows readable by index or column name.
On Postgres the wrapper translates placeholders and runs every statement outside a transaction
block (autocommit) unless it's inside `with conn:`.
"""

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from kb import settings

POSTGRES = bool(settings.DATABASE_URL)


def connect(sqlite_path: Path) -> "sqlite3.Connection | PgConnection":
    """The knowledge base and drafts share one Postgres database; locally each is its own file."""
    if POSTGRES:
        return PgConnection(settings.DATABASE_URL)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlite_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def is_postgres(conn) -> bool:
    return isinstance(conn, PgConnection)


def read_sql(conn, sql: str, params=()) -> pd.DataFrame:
    """pd.read_sql_query for either backend (pandas only accepts sqlite3 or SQLAlchemy connections)."""
    cur = conn.execute(sql, params)
    columns = [d[0] for d in cur.description]
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=columns)


def _param(value):
    if isinstance(value, np.generic):  # numpy ints/floats from pandas
        value = value.item()
    if isinstance(value, bool):  # flag columns are INTEGER, as in SQLite
        return int(value)
    return value


class PgConnection:
    def __init__(self, url: str):
        import psycopg2  # only needed when hosted
        import psycopg2.extras

        self._raw = psycopg2.connect(url, cursor_factory=psycopg2.extras.DictCursor)
        self._raw.autocommit = True

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, params=()):
        cur = self._raw.cursor()
        params = [_param(p) for p in params]
        # Without parameters psycopg2 sends the SQL as written, so it must not be %-escaped.
        cur.execute(self._sql(sql), params) if params else cur.execute(sql)
        return cur

    def executemany(self, sql: str, seq):
        from psycopg2.extras import execute_batch  # many rows per round trip, unlike cursor.executemany

        cur = self._raw.cursor()
        execute_batch(cur, self._sql(sql), [[_param(p) for p in params] for params in seq], page_size=500)
        return cur

    def executescript(self, script: str) -> None:
        self._raw.cursor().execute(script)

    def __enter__(self):
        self._raw.cursor().execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        self._raw.cursor().execute("ROLLBACK" if exc_type else "COMMIT")
        return False

    def close(self) -> None:
        self._raw.close()
