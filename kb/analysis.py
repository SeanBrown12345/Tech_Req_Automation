"""Read-only views over the knowledge base for the dashboard: summaries and answer conflicts."""

import numpy as np
import pandas as pd

from kb.db import read_sql

# How statuses group when checking whether two answers disagree.
STATUS_FAMILY = {
    "STANDARD": "yes", "SUPPORTED": "yes",
    "PARTIAL": "qualified", "CUSTOM": "qualified", "THIRD_PARTY": "qualified",
    "SCHEDULED": "qualified", "FUTURE": "qualified",
    "NOT_SUPPORTED": "no",
}  # NARRATIVE / NEEDS_DISCUSSION / NOT_APPLICABLE carry no comparable position


def sources(conn) -> pd.DataFrame:
    return read_sql(conn, 
        "SELECT s.source_id, s.client, s.rfp, s.worksheet, s.submitted, s.loaded_at,"
        " COUNT(r.record_id) AS records"
        " FROM sources s LEFT JOIN records r USING (source_id)"
        " GROUP BY s.source_id ORDER BY s.submitted DESC NULLS LAST, s.client")


def status_counts(conn, source_ids: list[str] | None = None) -> pd.DataFrame:
    sql = "SELECT COALESCE(status, 'NONE') AS status, COUNT(*) AS records FROM records"
    params: list = []
    if source_ids:
        sql += f" WHERE source_id IN ({','.join('?' * len(source_ids))})"
        params = list(source_ids)
    return read_sql(conn, sql + " GROUP BY status ORDER BY records DESC", params)


def module_status(conn) -> pd.DataFrame:
    """Records per module (rows) x status (columns)."""
    df = read_sql(
        conn,
        "SELECT COALESCE(module, '(none)') AS module, COALESCE(status, 'NONE') AS status, COUNT(*) AS n"
        " FROM records GROUP BY module, status")
    if df.empty:
        return df
    table = df.pivot_table(index="module", columns="status", values="n", fill_value=0, aggfunc="sum")
    by_volume = table.sum().sort_values(ascending=False).index.tolist()
    table.insert(0, "TOTAL", table.sum(axis=1))
    return table[["TOTAL", *by_volume]].sort_values("TOTAL", ascending=False)


def filter_options(conn) -> dict[str, list[str]]:
    col = lambda sql: [r[0] for r in conn.execute(sql) if r[0]]
    return {
        "clients": col("SELECT DISTINCT client FROM sources ORDER BY client"),
        "modules": col("SELECT DISTINCT module FROM records ORDER BY module"),
        "statuses": col("SELECT DISTINCT status FROM records ORDER BY status"),
    }


def conflicts(conn, model: str, threshold: float = 0.95,
              limit: int = 500) -> pd.DataFrame:
    """Pairs of near-identical requirements, from different sources, whose answers disagree.

    "Disagree" means different status families (yes / qualified / no); yes-vs-no pairs are
    ranked first. Similarity is cosine over the cached embeddings for `model`.
    """
    rows = conn.execute(
        "SELECT r.record_id, r.source_id, r.status, e.vector FROM records r"
        " JOIN embeddings e ON e.model = ? AND e.text_sha = r.embed_sha"
        f" WHERE r.status IN ({','.join('?' * len(STATUS_FAMILY))})",
        [model, *STATUS_FAMILY]).fetchall()
    if len(rows) < 2:
        return pd.DataFrame()

    vectors = np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    family = np.array([STATUS_FAMILY[r[2]] for r in rows])
    source = np.array([r[1] for r in rows])

    pairs = []
    chunk = 512
    for start in range(0, len(rows), chunk):
        sims = vectors[start:start + chunk] @ vectors.T
        i_idx, j_idx = np.nonzero(sims >= threshold)
        for i, j in zip(i_idx + start, j_idx):
            if j <= i or source[i] == source[j] or family[i] == family[j]:
                continue
            pairs.append((i, j, float(sims[i - start, j])))
    if not pairs:
        return pd.DataFrame()

    severity = lambda i, j: 2 if {family[i], family[j]} == {"yes", "no"} else 1
    pairs.sort(key=lambda p: (-severity(p[0], p[1]), -p[2]))
    pairs = pairs[:limit]

    ids = {rows[k][0] for p in pairs for k in p[:2]}
    details = {r["record_id"]: r for r in conn.execute(
        "SELECT r.record_id, r.requirement, r.parent_text, r.status, r.comment, r.req_id, r.sheet,"
        " s.client, s.submitted FROM records r JOIN sources s USING (source_id)"
        f" WHERE r.record_id IN ({','.join('?' * len(ids))})", list(ids))}

    def describe(rec):
        text = f"{rec['parent_text']} {rec['requirement']}" if rec["parent_text"] else rec["requirement"]
        return text, f"{rec['client']} ({rec['submitted'] or '?'}) {rec['req_id'] or ''}".strip()

    out = []
    for i, j, sim in pairs:
        a, b = details[rows[i][0]], details[rows[j][0]]
        # Show the more recent answer as "A".
        if (b["submitted"] or "") > (a["submitted"] or ""):
            a, b = b, a
        a_text, a_src = describe(a)
        b_text, b_src = describe(b)
        out.append({
            "severity": "high" if severity(i, j) == 2 else "medium",
            "similarity": round(sim, 3),
            "status_a": a["status"], "source_a": a_src, "requirement_a": a_text, "comment_a": a["comment"],
            "status_b": b["status"], "source_b": b_src, "requirement_b": b_text, "comment_b": b["comment"],
            "record_a": a["record_id"], "record_b": b["record_id"],
        })
    return pd.DataFrame(out)
