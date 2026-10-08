"""Which product module each drafted requirement belongs to, so rows can be filtered and handed to the
SME or partner who owns that module.

The modules are kept in the drafts database and edited in Admin > Modules. modules.txt in the repo root
("CODE - Description" per line, e.g. "CIS - Customer Information System") is the starting list, loaded
once. Rows not tied to one module are GENERAL.

A sheet whose name names exactly one module (a "CIS Requirements" tab) puts all its rows in that module.
Other rows are classified by Claude from the sheet name, section headings, requirement text and the
module tags on the past answers used as evidence.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor

from kb import settings

GENERAL = "General"
BATCH_SIZE = 60
WORKERS = 4


def from_file() -> list[tuple[str, str]]:
    """(code, description) per line of modules.txt; empty when there's no file."""
    if not settings.MODULES_FILE.exists():
        return []
    found = []
    for line in settings.MODULES_FILE.read_text(encoding="utf-8-sig").splitlines():
        code, _, description = (part.strip() for part in line.partition(" - "))
        if code:
            found.append((code, description))
    return found


def load() -> list[tuple[str, str]]:
    """(code, description) per module, in display order."""
    from kb import draft  # draft imports this module

    conn = draft.connect_drafts()
    try:
        return [(r[0], r[1] or "") for r in conn.execute("SELECT code, description FROM modules ORDER BY position")]
    finally:
        conn.close()


def table():
    """Modules with how many drafted rows each has, for the admin page."""
    from kb import db, draft

    conn = draft.connect_drafts()
    try:
        return db.read_sql(conn, "SELECT m.code, m.description, COUNT(r.row_num) AS rows FROM modules m"
                                 " LEFT JOIN rows r ON r.module = m.code GROUP BY m.code, m.description, m.position"
                                 " ORDER BY m.position")
    finally:
        conn.close()


def _taken(conn, code: str, other_than: str | None = None) -> bool:
    if code.lower() == GENERAL.lower():
        return True
    return any(r[0].lower() == code.lower() and r[0] != other_than for r in conn.execute("SELECT code FROM modules"))


def add(code: str, description: str | None) -> bool:
    """Add a module at the end of the list; False if the code is taken (or is General)."""
    from kb import draft

    conn = draft.connect_drafts()
    try:
        with conn:
            if _taken(conn, code):
                return False
            position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM modules").fetchone()[0]
            conn.execute("INSERT INTO modules (code, description, position) VALUES (?, ?, ?)",
                         (code, description or None, position))
        return True
    finally:
        conn.close()


def update(old_code: str, code: str, description: str | None) -> bool:
    """Edit a module; a new code is carried onto its rows. False if the new code is taken."""
    from kb import draft

    conn = draft.connect_drafts()
    try:
        with conn:
            if _taken(conn, code, other_than=old_code):
                return False
            conn.execute("UPDATE modules SET code = ?, description = ? WHERE code = ?",
                         (code, description or None, old_code))
            if code != old_code:
                conn.execute("UPDATE rows SET module = ? WHERE module = ?", (code, old_code))
        return True
    finally:
        conn.close()


def move(code: str, step: int) -> None:
    """Move a module up (-1) or down (+1) the list."""
    from kb import draft

    conn = draft.connect_drafts()
    try:
        order = [r[0] for r in conn.execute("SELECT code FROM modules ORDER BY position")]
        i = order.index(code)
        j = min(max(i + step, 0), len(order) - 1)
        order.insert(j, order.pop(i))
        with conn:
            conn.executemany("UPDATE modules SET position = ? WHERE code = ?", [(n, c) for n, c in enumerate(order)])
    finally:
        conn.close()


def remove(code: str) -> None:
    """Remove a module. Its rows lose their module, so Detect modules can sort them again."""
    from kb import draft

    conn = draft.connect_drafts()
    try:
        with conn:
            conn.execute("UPDATE rows SET module = NULL WHERE module = ?", (code,))
            conn.execute("DELETE FROM modules WHERE code = ?", (code,))
    finally:
        conn.close()


def names() -> list[str]:
    """Module codes, then General: every value a row's module can take."""
    return [code for code, _ in load()] + [GENERAL]


def _keywords(code: str, description: str) -> list[str]:
    """What a sheet name might say for a module: each part of the code ("CEP", "CSS"), the description."""
    parts = [p.strip() for p in re.split(r"[/]", code) if p.strip()]
    return [*parts, *(p.strip() for p in description.split("/") if p.strip())]


def mentioned(text: str) -> list[str]:
    """Modules a piece of text names, by code or description ("Customer Engagement Portal (CEP)" -> CEP/CSS)."""
    return [code for code, description in load()
            if any(re.search(rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])", text or "", re.I)
                   for k in _keywords(code, description))]


def from_sheet_name(sheet: str) -> str | None:
    """The module a sheet's name names, if it names exactly one."""
    hits = mentioned(sheet)
    return hits[0] if len(hits) == 1 else None


def _system_prompt() -> str:
    lines = "\n".join(f"- {code}: {description}" for code, description in load())
    return f"""You sort requirements from utility-software RFP worksheets by the product module they belong to.

Modules:
{lines}
- {GENERAL}: not specific to one module - e.g. hosting, security, architecture, integration standards in
  general, implementation approach, training, support, company information, pricing, or requirements that
  apply to the whole solution.

Pick exactly one module per row: the one whose SME would answer it.
- When the sheet name or the section headings name a module, use it unless the requirement is clearly
  about something else.
- "Past answers were tagged" lists the modules of similar requirements we answered before - a hint, not
  a rule (ERP there may mean FMS, HCM or WMS).
- Use {GENERAL} only when no single module owns the requirement."""


def _schema(row_nums: list[int]) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["rows"], "properties": {"rows": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["row", "module"],
                                   "properties": {"row": {"type": "integer", "enum": row_nums},
                                                  "module": {"type": "string", "enum": names()}}}}}}


def _message(sheet: str, rows: list[dict], hints: dict[int, str]) -> str:
    out = [f"Sheet: {sheet}", ""]
    for r in rows:
        context = " > ".join(v for v in json.loads(r["context"] or "{}").values() if v)
        out.append(f"Row {r['row_num']}" + (f" [{context}]" if context else ""))
        if r.get("parent_text"):
            out.append(f"  Under: {r['parent_text']}")
        out.append(f"  {r['requirement']}")
        if hints.get(r["row_num"]):
            out.append(f"  Past answers were tagged: {hints[r['row_num']]}")
    return "\n".join(out)


def _evidence_hints(kb_conn, rows: list[dict]) -> dict[int, str]:
    """Per row, the module tags of the past answers it was drafted from, e.g. "CIS x4, ERP x1"."""
    ids = {r["row_num"]: [e["record_id"] for e in json.loads(r["evidence"] or "[]") if e.get("record_id")]
           for r in rows}
    wanted = sorted({i for v in ids.values() for i in v})
    module_of = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        module_of |= {rec: mod for rec, mod in kb_conn.execute(
            f"SELECT record_id, module FROM records WHERE record_id IN ({', '.join('?' * len(chunk))})", chunk)}
    hints = {}
    for row_num, recs in ids.items():
        counts: dict[str, int] = {}
        for rec in recs:
            if module_of.get(rec):
                counts[module_of[rec]] = counts.get(module_of[rec], 0) + 1
        hints[row_num] = ", ".join(f"{m} x{n}" for m, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return hints


def classify(llm, model: str, kb_conn, rows: list[dict], progress=None) -> dict[tuple[str, int], str]:
    """Module per (sheet, row_num) for `rows` (dicts from the rows table). Sheets named after a module
    need no AI call. `progress(done, total)` is called as batches finish. Batches that fail are left out,
    so their rows can be retried."""
    result, pending = {}, []
    for r in rows:
        if module := from_sheet_name(r["sheet"]):
            result[(r["sheet"], r["row_num"])] = module
        else:
            pending.append(r)
    if not pending:
        return result
    hints = _evidence_hints(kb_conn, pending)
    batches = []
    for sheet in dict.fromkeys(r["sheet"] for r in pending):
        sheet_rows = [r for r in pending if r["sheet"] == sheet]
        batches += [(sheet, sheet_rows[i:i + BATCH_SIZE]) for i in range(0, len(sheet_rows), BATCH_SIZE)]
    system = [{"type": "text", "text": _system_prompt(), "cache_control": {"type": "ephemeral"}}]

    def work(sheet, batch):
        response = llm.messages.create(
            model=model, max_tokens=8000, system=system,
            output_config={"effort": "low", "format": {"type": "json_schema",
                                                       "schema": _schema([r["row_num"] for r in batch])}},
            messages=[{"role": "user", "content": _message(sheet, batch, hints)}])
        payload = json.loads(next(b.text for b in response.content if b.type == "text"))
        return {(sheet, res["row"]): res["module"] for res in payload["rows"]}

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for future in [pool.submit(work, *b) for b in batches]:
            try:
                result |= future.result()
            except Exception:  # noqa: BLE001 - a failed batch stays unclassified and can be retried
                pass
            done += 1
            if progress:
                progress(done, len(batches))
    return result
