"""Draft answers for a new RFP worksheet from the knowledge base, using Claude via Matcha.

Flow: guess_plan() proposes which columns to fill and how (reviewed in the dashboard) ->
create_job() snapshots the workbook + plan and lists the requirement rows -> run_job() retrieves
past answers per row and asks Claude for each batch -> reviewers edit in the dashboard ->
build_output() writes the answers into a copy of the original workbook, changing nothing else.

Drafts live in their own database (settings.DRAFTS_DB_PATH): unlike the knowledge base they are
user work, not derived data, so they must survive a knowledge-base rebuild.
"""

import io
import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import anthropic
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

from kb import db, modules, settings, store
from kb.detect import guess_sheet, read_rows
from kb.embed import Embedder, embedding_text
from kb.extract import _CHILD_ID, clean, iter_requirements
from kb.statuses import STATUSES
from kb.xlsx_patch import dropdowns, patch_workbook

MODEL = os.environ.get("KB_LLM_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("KB_LLM_EFFORT", "medium")
BATCH_SIZE = 10
WORKERS = 4
EVIDENCE_PER_ROW = 6

# Retrieval caps on confidence: the AI can't be more sure than its closest past answer allows.
SIM_HIGH, SIM_MEDIUM = 0.85, 0.75
SIM_SAME = 0.95  # past requirement is effectively the same one; its comment (or lack of one) is the model
COST_STATUSES = {"CUSTOM", "THIRD_PARTY"}
RED, YELLOW = "FFFFC7CE", "FFFFFF00"
_PLACEHOLDER = re.compile(r"\[[^\]]{2,}\]")
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


# ================================================================================================
# Plan: which columns to fill, and how
# ================================================================================================

def _col(letter: str) -> int:
    return column_index_from_string(letter) - 1


def _sheet_text(rows: list[tuple], limit: int = 6000) -> str:
    lines = []
    for row in rows:
        cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)[:limit]


def read_workbook(data: bytes) -> dict[str, list[tuple]]:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        return {ws.title: read_rows(ws) for ws in wb.worksheets}
    finally:
        wb.close()


def guess_plan(data: bytes, sheets: dict[str, list[tuple]] | None = None,
               header_rows: dict[str, int] | None = None) -> dict:
    """Propose a fill plan for a blank worksheet. Every part is meant to be reviewed by a person.
    `header_rows` overrides the detected header row per sheet."""
    sheets = sheets or read_workbook(data)
    lists = dropdowns(data, sheets)
    plan_sheets, instruction_parts = [], []

    for name, rows in sheets.items():
        guess = guess_sheet(name, rows, (header_rows or {}).get(name))
        if guess is None or "requirement" not in guess.columns:
            text = _sheet_text(rows)
            if text:
                instruction_parts.append(f"## Sheet '{name}'\n{text}")
            continue

        hdr = guess.header_row
        roles = guess.columns
        role_cols = set(roles.values())
        req_idx = _col(roles["requirement"])
        first_req = next((i for i in range(hdr, len(rows))
                          if req_idx < len(rows[i]) and clean(rows[i][req_idx])), hdr)
        hint_rows = rows[hdr:first_req]  # e.g. Bentonville's "Accepted Answers" rows
        hints = {}
        for letter in guess.headers:
            texts = [clean(r[_col(letter)]) for r in hint_rows if _col(letter) < len(r) and clean(r[_col(letter)])]
            if texts:
                hints[letter] = texts[-1]

        fields = []
        if guess.mark_columns:
            labels = {}
            for l in guess.mark_columns:
                head = guess.headers[l]
                meaning = guess.legend.get(head.upper())
                labels[l] = f"{head} - {meaning}" if meaning else head
            symbol = "header" if all(guess.headers[l].upper() in guess.legend for l in guess.mark_columns) else "X"
            fields.append({"key": "mark", "kind": "marks", "columns": guess.mark_columns, "labels": labels,
                           "symbol": symbol, "label": "Answer (mark one column)", "guidance": ""})

        # Rows that hold requirement text. A column's dropdown is the one covering most of them:
        # the first rows may be blocked out ("-") under a different, or broken, dropdown.
        req_rows = [i + 1 for i in range(first_req, len(rows)) if req_idx < len(rows[i]) and clean(rows[i][req_idx])]
        for letter, head in guess.headers.items():
            if letter in role_cols - {roles.get("comment")} or letter in guess.mark_columns:
                continue
            coverage = [(sum(d.covers(_col(letter) + 1, r) for r in req_rows), d.options) for d in lists.get(name, [])]
            options = max(((n, o) for n, o in coverage if n), default=(0, None), key=lambda c: c[0])[1]
            hint = hints.get(letter, "")
            if not options and hint and "list all" not in head.lower():
                parts = [p.strip() for p in hint.split("/") if p.strip()]
                if 2 <= len(parts) <= 8 and all(len(p) <= 30 for p in parts):
                    options = parts
            is_comment = letter == roles.get("comment")
            if options:
                fields.append({"key": letter, "kind": "choice", "columns": [letter], "label": head,
                               "options": options, "guidance": "", "include": True})
            elif is_comment or hint or letter in guess.answer_columns:
                fields.append({"key": letter, "kind": "text", "columns": [letter], "label": head,
                               "guidance": hint if hint and hint.lower() != "free form" else "",
                               "include": True})
            else:
                fields.append({"key": letter, "kind": "text", "columns": [letter], "label": head,
                               "guidance": "", "include": False})
        for f in fields:
            f.setdefault("include", True)

        notes = _sheet_text(rows[:hdr - 1], 3000)
        if notes:
            instruction_parts.append(f"## Notes above the table on sheet '{name}'\n{notes}")
        plan_sheets.append({
            "name": name, "header_row": hdr, "include": bool(fields),
            "columns": {k: v for k, v in roles.items() if k != "comment"},
            "fields": fields,
        })

    return {"sheets": plan_sheets, "instructions": "\n\n".join(instruction_parts)}


# ================================================================================================
# Jobs (stored in the drafts database)
# ================================================================================================

DRAFTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    client      TEXT,
    filename    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,          -- ready | running | stopping | done | error
    plan        TEXT NOT NULL,          -- JSON
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    model       TEXT
);
CREATE TABLE IF NOT EXISTS rows (
    job_id       TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    sheet        TEXT NOT NULL,
    row_num      INTEGER NOT NULL,
    req_id       TEXT,
    requirement  TEXT NOT NULL,
    parent_text  TEXT,
    context      TEXT,                  -- category / subcategory / section
    fill         TEXT NOT NULL,         -- JSON list of field keys to fill
    prefilled    TEXT NOT NULL,         -- JSON {label: value} already in the template
    ai           TEXT,                  -- JSON {field key: value} as drafted
    final        TEXT,                  -- JSON {field key: value} after review edits
    status       TEXT,
    confidence   TEXT,                  -- high | medium | low (after retrieval caps)
    ai_confidence TEXT,
    best_similarity REAL,
    cost_impact  INTEGER,
    cost_note    TEXT,
    rationale    TEXT,
    evidence     TEXT,                  -- JSON list of past answers shown to the model
    reviewed     INTEGER NOT NULL DEFAULT 0,  -- approved by the SME (or whoever reviewed it)
    assignee     TEXT,                  -- SME responsible for the row; NULL = unassigned
    returned_by  TEXT,                  -- unassigned because this SME handed it back (the "returned" pool)
    return_note  TEXT,                  -- their reason, if they gave one
    module       TEXT,                  -- product module (kb/modules.py), or General; NULL = not detected yet
    partner_status TEXT,                -- rows assigned to a partner: NULL (not sent) | sent | received
    sent_at      TEXT,                  -- when the partner's trimmed worksheet was last exported
    received_at  TEXT,                  -- when their answer was imported
    error        TEXT,
    PRIMARY KEY (job_id, sheet, row_num)
);
CREATE TABLE IF NOT EXISTS modules (
    code        TEXT PRIMARY KEY,       -- as stored in rows.module, e.g. CIS
    description TEXT,
    position    INTEGER NOT NULL        -- display order
);
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
CREATE TABLE IF NOT EXISTS people (
    name        TEXT PRIMARY KEY,       -- as shown on the task board and stored in rows.assignee
    email       TEXT,
    areas       TEXT,                   -- what they review, to help whoever assigns rows
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'sme',  -- sme (uses the app) | partner (answers by email)
    modules     TEXT                    -- partners: JSON list of the modules they cover
);
"""


# Columns added after the first release: existing databases get them on connect.
_ADDED_COLUMNS = {
    "rows": {"assignee": "TEXT", "returned_by": "TEXT", "return_note": "TEXT", "module": "TEXT",
             "partner_status": "TEXT", "sent_at": "TEXT", "received_at": "TEXT"},
    "people": {"kind": "TEXT NOT NULL DEFAULT 'sme'", "modules": "TEXT"},
}

_drafts_ready = False  # Postgres: tables created once per process
_sqlite_ready: set = set()  # SQLite files already migrated in this process


def _migrate(conn) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        if db.is_postgres(conn):
            for name, kind in columns.items():
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {kind}")
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def _sync_people(conn) -> None:
    """Anyone rows are assigned to is an SME: covers rows assigned before the SME list existed."""
    with conn:  # commit now: an open write transaction would lock every other connection out
        conn.execute("INSERT INTO people (name, created_at) SELECT DISTINCT assignee, ? FROM rows"
                     " WHERE assignee IS NOT NULL AND assignee NOT IN (SELECT name FROM people)", (_now(),))


def _seed_modules(conn) -> None:
    """Load modules.txt into the modules table, once: after that the list is edited in Admin > Modules."""
    if conn.execute("SELECT 1 FROM app_settings WHERE key = 'modules_seeded'").fetchone():
        return
    with conn:
        if not conn.execute("SELECT 1 FROM modules").fetchone():
            conn.executemany("INSERT INTO modules (code, description, position) VALUES (?, ?, ?)",
                             [(code, description, i) for i, (code, description) in enumerate(modules.from_file())])
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('modules_seeded', ?)", (_now(),))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_drafts():
    """The drafts database: its own SQLite file locally, the shared Postgres database when hosted."""
    global _drafts_ready
    conn = db.connect(settings.DRAFTS_DB_PATH)
    if db.is_postgres(conn):
        if not _drafts_ready:
            conn.executescript(DRAFTS_SCHEMA)
            _migrate(conn)
            _sync_people(conn)
            _seed_modules(conn)
            _drafts_ready = True
        return conn
    conn.execute("PRAGMA journal_mode = WAL")  # readers (the dashboard) don't block the drafting thread
    conn.executescript(DRAFTS_SCHEMA)
    if settings.DRAFTS_DB_PATH not in _sqlite_ready:  # one-off upkeep, once per process
        _migrate(conn)
        _sync_people(conn)
        _seed_modules(conn)
        _sqlite_ready.add(settings.DRAFTS_DB_PATH)
    return conn


def workbook_path(job_id: str):
    return settings.DRAFTS_DIR / f"{job_id}.xlsx"


def _is_empty(value) -> bool:
    return value is None or str(value).strip() == ""


def collect_rows(plan: dict, sheets: dict[str, list[tuple]]) -> list[dict]:
    """Requirement rows with at least one empty field to fill."""
    out = []
    for sp in plan["sheets"]:
        if not sp.get("include", True):
            continue
        fields = [f for f in sp["fields"] if f.get("include", True)]
        if not fields:
            continue
        rows = sheets[sp["name"]]
        cols = {role: _col(letter) for role, letter in sp["columns"].items() if letter}
        hdr = sp["header_row"]
        header_req = clean(rows[hdr - 1][cols["requirement"]]) if hdr - 1 < len(rows) else None

        def cell(row, letter):
            i = _col(letter)
            return row[i] if i < len(row) else None

        found = list(iter_requirements(rows[hdr:], hdr + 1, cols, header_req))
        # Numbered headings like "4 Pre-paid Metering:" whose children (4.1, 4.2...) are the real rows.
        child_parents = {m.group("parent") for r in found if (m := _CHILD_ID.match(r[2] or ""))}
        for row_num, row, req_id, requirement, section, parent in found:
            if req_id in child_parents and requirement.rstrip().endswith(":"):
                continue
            fill, prefilled = [], {}
            for f in fields:
                current = [cell(row, l) for l in f["columns"]]
                if all(_is_empty(v) for v in current):
                    fill.append(f["key"])
                else:
                    prefilled[f["label"]] = " / ".join(str(v).strip() for v in current if not _is_empty(v))
            if not fill:
                continue
            context = {k: clean(row[cols[k]]) for k in ("category", "subcategory") if k in cols and cols[k] < len(row)}
            context["section"] = section
            out.append({"sheet": sp["name"], "row_num": row_num, "req_id": req_id, "requirement": requirement,
                        "parent_text": parent, "context": context, "fill": fill, "prefilled": prefilled})
    return out


def create_job(name: str, client: str | None, filename: str, data: bytes, plan: dict) -> tuple[str, int]:
    sheets = read_workbook(data)
    rows = collect_rows(plan, sheets)
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    settings.DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    workbook_path(job_id).write_bytes(data)
    conn = connect_drafts()
    with conn:
        conn.execute("INSERT INTO jobs (job_id, name, client, filename, created_at, status, plan, total, model)"
                     " VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?)",
                     (job_id, name, client, filename, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      json.dumps(plan), len(rows), MODEL))
        conn.executemany(
            "INSERT INTO rows (job_id, sheet, row_num, req_id, requirement, parent_text, context, fill, prefilled)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(job_id, r["sheet"], r["row_num"], r["req_id"], r["requirement"], r["parent_text"],
              json.dumps(r["context"]), json.dumps(r["fill"]), json.dumps(r["prefilled"])) for r in rows])
    conn.close()
    return job_id, len(rows)


# ================================================================================================
# Prompting
# ================================================================================================

def _products(kb_conn) -> list[str]:
    names = set()
    for (products,) in kb_conn.execute("SELECT products FROM sources"):
        names.update(json.loads(products or "[]"))
    return sorted(names)


def _system_prompt(plan: dict, sheet_plan: dict, client: str | None, products: list[str]) -> str:
    statuses = "\n".join(f"- {k}: {v}" for k, v in STATUSES.items())
    field_lines = []
    for f in sheet_plan["fields"]:
        if not f.get("include", True):
            continue
        guidance = f" Guidance: {f['guidance']}" if f.get("guidance") else ""
        if f["kind"] == "choice" and f.get("options"):
            field_lines.append(f'- "{f["key"]}" (column {f["columns"][0]}, "{f["label"]}"): choose exactly one of '
                               f'{json.dumps(f["options"], ensure_ascii=False)}.{guidance}')
        elif f["kind"] == "marks":
            opts = [f["labels"][c] for c in f["columns"]]
            field_lines.append(f'- "{f["key"]}" ({f["label"]}, columns {", ".join(f["columns"])}): choose exactly '
                               f'one of {json.dumps(opts, ensure_ascii=False)}; that column gets marked.{guidance}')
        else:
            field_lines.append(f'- "{f["key"]}" (column {f["columns"][0]}, "{f["label"]}"): free text.{guidance}')
    return f"""You are a senior proposal writer completing an RFP requirements worksheet on behalf of our company, a software vendor.
Products we have proposed in past RFPs: {", ".join(products) or "(not recorded)"}.
This worksheet is for: {client or "(client not specified)"}.

For each requirement row you receive our past answers to similar requirements from previous RFPs ("evidence"). Draft our answer.

Rules:
1. Base every answer on the evidence. Never claim a capability, module, integration, release, or certification the evidence does not support.
2. When past answers disagree, prefer the most recent one, and lower your confidence.
3. When no evidence covers the requirement, give the most cautious defensible answer and set confidence to "low".
4. Follow the worksheet instructions below exactly - for example, include release numbers and dates, cost estimates, or explanations wherever the instructions require them. If required information (a release number, a date, an hour estimate, a price) is not in the evidence, write a clear placeholder in square brackets, e.g. [Release # and date to be confirmed], and never invent it.
5. Comments and narratives: professional, concise, in our voice and consistent with the wording of our past comments. Never mention "evidence", other clients, or previous RFPs. Leave a comment field "" when the instructions don't call for one and a comment adds nothing. When none of the closest past answers (similarity {SIM_SAME} or higher) has a comment, leave the comment "" unless the worksheet instructions require one for the response you chose (for example a release number and date, or a modification estimate). If any of them has a comment, keep using it as the model for ours. Prefer not to write a comment that only restates the requirement; a comment should add a fact the requirement doesn't already contain.
6. Fill only the fields listed in each row's "fill"; return "" for every other field.
7. "status" is our internal classification of the answer:
{statuses}
8. "confidence": high = closely matching evidence answers this directly and consistently; medium = related evidence that needs inference or has minor gaps; low = little or no relevant evidence, conflicting evidence, or any placeholder in your answer.
9. "cost_impact": true when meeting the requirement likely adds implementation cost beyond the base proposal - customization or modification, a third-party product, an additional module or license, or extra services hours. Explain briefly in "cost_note" ("" when false).
10. "rationale": one or two sentences for the reviewer on why you answered this way; cite evidence IDs in "evidence_used".

FIELDS TO FILL on sheet "{sheet_plan["name"]}":
{chr(10).join(field_lines)}

WORKSHEET INSTRUCTIONS (from the client's workbook, as edited by our team):
{plan.get("instructions") or "(none provided)"}"""


def _output_schema(sheet_plan: dict, row_nums: list[int]) -> dict:
    values = {}
    for f in sheet_plan["fields"]:
        if not f.get("include", True):
            continue
        if f["kind"] == "choice" and f.get("options"):  # without options it's free text, not a forced blank
            values[f["key"]] = {"type": "string", "enum": [*f["options"], ""]}
        elif f["kind"] == "marks":
            values[f["key"]] = {"type": "string", "enum": [*(f["labels"][c] for c in f["columns"]), ""]}
        else:
            values[f["key"]] = {"type": "string"}
    row = {
        "type": "object",
        "properties": {
            "row": {"type": "integer", "enum": row_nums},
            "status": {"type": "string", "enum": list(STATUSES)},
            "values": {"type": "object", "properties": values, "required": list(values), "additionalProperties": False},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "cost_impact": {"type": "boolean"},
            "cost_note": {"type": "string"},
            "rationale": {"type": "string"},
            "evidence_used": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["row", "status", "values", "confidence", "cost_impact", "cost_note", "rationale", "evidence_used"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"rows": {"type": "array", "items": row}},
            "required": ["rows"], "additionalProperties": False}


def _evidence_brief(row_num: int, evidence: list[dict]) -> tuple[str, list[dict]]:
    lines, kept = [], []
    for i, e in enumerate(evidence, 1):
        eid = f"R{row_num}-E{i}"
        raw = json.loads(e["answer_raw"]) if isinstance(e["answer_raw"], str) else e["answer_raw"]
        attrs = json.loads(e["attributes"]) if isinstance(e["attributes"], str) else e["attributes"]
        req = f"{e['parent_text']} {e['requirement']}" if e.get("parent_text") else e["requirement"]
        sim = f"similarity {e['similarity']:.2f}, " if e.get("similarity") is not None else ""
        lines.append(f"  [{eid}] ({sim}{e['client']}, {e['submitted'] or 'date unknown'}) status {e['status']}; "
                     f"answered {json.dumps(raw, ensure_ascii=False)}\n"
                     f"     requirement: {req}\n"
                     f"     our comment: {e['comment'] or '(none)'}"
                     + (f"\n     other details: {json.dumps(attrs, ensure_ascii=False)}" if attrs else ""))
        kept.append({"id": eid, "record_id": e["record_id"], "client": e["client"], "submitted": e["submitted"],
                     "status": e["status"], "similarity": e.get("similarity"), "requirement": req,
                     "comment": e["comment"], "answer_raw": raw})
    return "\n".join(lines) or "  (no similar past answers found)", kept


def _user_message(rows: list[dict], evidence_text: dict[int, str]) -> str:
    parts = []
    for r in rows:
        ctx = " > ".join(v for v in json.loads(r["context"]).values() if v) if r["context"] else ""
        parts.append("\n".join(filter(None, [
            f"ROW {r['row_num']}" + (f" (ID {r['req_id']})" if r["req_id"] else ""),
            f"Context: {ctx}" if ctx else None,
            f"Under: {r['parent_text']}" if r["parent_text"] else None,
            f"Requirement: {r['requirement']}",
            f"fill: {', '.join(json.loads(r['fill']))}",
            f"Already filled in the template: {r['prefilled']}" if r["prefilled"] not in ("{}", None) else None,
            "Evidence:", evidence_text[r["row_num"]],
        ])))
    return "Draft our answers for these rows.\n\n" + "\n\n".join(parts)


# ================================================================================================
# Running a job
# ================================================================================================

_running: dict[str, threading.Thread] = {}
_lock = threading.Lock()


def start_job(job_id: str, exclude_sources: list[str] | None = None) -> bool:
    """Run (or resume) a job in a background thread. False if it's already running here."""
    with _lock:
        if job_id in _running and _running[job_id].is_alive():
            return False
        conn = connect_drafts()
        with conn:
            conn.execute("UPDATE jobs SET status = 'running', message = NULL WHERE job_id = ?", (job_id,))
        conn.close()
        thread = threading.Thread(target=_run_job, args=(job_id, exclude_sources), daemon=True,
                                  name=f"draft-{job_id}")
        _running[job_id] = thread
        thread.start()
        return True


def stop_job(job_id: str) -> None:
    conn = connect_drafts()
    with conn:
        conn.execute("UPDATE jobs SET status = 'stopping' WHERE job_id = ? AND status = 'running'", (job_id,))
    conn.close()


def is_running(job_id: str) -> bool:
    thread = _running.get(job_id)
    return bool(thread and thread.is_alive())


def reconcile(job_id: str) -> bool:
    """Mark a job interrupted if it says it's running but no thread in this process is drafting it
    (e.g. the server restarted). Returns True if it changed anything."""
    if is_running(job_id):
        return False
    conn = connect_drafts()
    with conn:
        n = conn.execute("UPDATE jobs SET status = 'ready', message = 'Drafting was interrupted; Resume to continue.'"
                         " WHERE job_id = ? AND status IN ('running', 'stopping')", (job_id,)).rowcount
    conn.close()
    return bool(n)


def _finalize(row: dict, result: dict, kept: list[dict], sheet_plan: dict) -> dict:
    best = max((e["similarity"] for e in kept if e["similarity"] is not None), default=0.0)
    cap = "high" if best >= SIM_HIGH else "medium" if best >= SIM_MEDIUM else "low"
    confidence = min(result["confidence"], cap, key=_CONF_RANK.get)
    fill = json.loads(row["fill"])
    values = {k: v for k, v in result["values"].items() if k in fill}
    if any(_PLACEHOLDER.search(v or "") for v in values.values()):
        confidence = "low"
    marks = [f for f in sheet_plan["fields"] if f["kind"] == "marks"]
    if any(f["key"] in fill and not values.get(f["key"]) for f in marks):
        confidence = "low"  # a required answer was left blank
    cost = bool(result["cost_impact"]) or result["status"] in COST_STATUSES
    note = result["cost_note"] or ("Custom development or third-party product." if cost else "")
    return {"ai": values, "status": result["status"], "confidence": confidence,
            "ai_confidence": result["confidence"], "best_similarity": best, "cost_impact": int(cost),
            "cost_note": note, "rationale": result["rationale"], "evidence": kept}


def _draft_batch(client: anthropic.Anthropic, system: str, sheet_plan: dict, rows: list[dict],
                 evidence: dict[int, list[dict]]) -> dict[int, dict]:
    texts, kept = {}, {}
    for r in rows:
        texts[r["row_num"]], kept[r["row_num"]] = _evidence_brief(r["row_num"], evidence[r["row_num"]])
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": EFFORT, "format": {"type": "json_schema",
                                                    "schema": _output_schema(sheet_plan, [r["row_num"] for r in rows])}},
        messages=[{"role": "user", "content": _user_message(rows, texts)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined this batch")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Response was cut off (max_tokens)")
    payload = json.loads(next(b.text for b in response.content if b.type == "text"))
    by_row = {res["row"]: res for res in payload["rows"]}
    return {r["row_num"]: _finalize(r, by_row[r["row_num"]], kept[r["row_num"]], sheet_plan)
            for r in rows if r["row_num"] in by_row}


def _save(conn, job_id: str, sheet: str, results: dict[int, dict], errors: dict[int, str]) -> None:
    with conn:
        for row_num, res in results.items():
            conn.execute(
                "UPDATE rows SET ai = ?, final = NULL, status = ?, confidence = ?, ai_confidence = ?,"
                " best_similarity = ?, cost_impact = ?, cost_note = ?, rationale = ?, evidence = ?, error = NULL"
                " WHERE job_id = ? AND sheet = ? AND row_num = ?",
                (json.dumps(res["ai"]), res["status"], res["confidence"], res["ai_confidence"],
                 res["best_similarity"], res["cost_impact"], res["cost_note"], res["rationale"],
                 json.dumps(res["evidence"]), job_id, sheet, row_num))
        for row_num, err in errors.items():
            conn.execute("UPDATE rows SET error = ? WHERE job_id = ? AND sheet = ? AND row_num = ?",
                         (err[:500], job_id, sheet, row_num))
        conn.execute("UPDATE jobs SET done = (SELECT COUNT(*) FROM rows WHERE job_id = ? AND ai IS NOT NULL)"
                     " WHERE job_id = ?", (job_id, job_id))


def _run_job(job_id: str, exclude_sources: list[str] | None) -> None:
    conn = connect_drafts()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        plan = json.loads(job["plan"])
        pending = [dict(r) for r in conn.execute(
            "SELECT * FROM rows WHERE job_id = ? AND ai IS NULL ORDER BY sheet, row_num", (job_id,))]
        if not pending:
            with conn:
                conn.execute("UPDATE jobs SET status = 'done' WHERE job_id = ?", (job_id,))
            return

        kb_conn = store.connect()
        retriever = store.Retriever(kb_conn, Embedder(), {"exclude_sources": exclude_sources or plan.get(
            "exclude_sources") or []})
        queries = [embedding_text(r["requirement"], r["parent_text"]) for r in pending]
        found = retriever.search_many(queries, EVIDENCE_PER_ROW)
        evidence = {(r["sheet"], r["row_num"]): ev for r, ev in zip(pending, found)}
        products = _products(kb_conn)
        kb_conn.close()

        llm = anthropic.Anthropic(max_retries=4)
        sheet_plans = {sp["name"]: sp for sp in plan["sheets"]}
        batches = []
        for sheet in dict.fromkeys(r["sheet"] for r in pending):
            sheet_rows = [r for r in pending if r["sheet"] == sheet]
            system = _system_prompt(plan, sheet_plans[sheet], job["client"], products)
            for i in range(0, len(sheet_rows), BATCH_SIZE):
                batches.append((sheet, system, sheet_rows[i:i + BATCH_SIZE]))

        def work(sheet, system, rows):
            if conn_status(job_id) == "stopping":
                return sheet, {}, {}
            ev = {r["row_num"]: evidence[(sheet, r["row_num"])] for r in rows}
            try:
                results = _draft_batch(llm, system, sheet_plans[sheet], rows, ev)
                missing = {r["row_num"]: "Not returned by the model" for r in rows if r["row_num"] not in results}
                return sheet, results, missing
            except Exception as exc:  # keep going; failed rows can be retried with Resume
                return sheet, {}, {r["row_num"]: f"{type(exc).__name__}: {exc}" for r in rows}

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(work, *b) for b in batches]
            for future in as_completed(futures):
                sheet, results, errors = future.result()
                _save(conn, job_id, sheet, results, errors)

        remaining = conn.execute("SELECT COUNT(*) FROM rows WHERE job_id = ? AND ai IS NULL", (job_id,)).fetchone()[0]
        stopped = conn_status(job_id) == "stopping"
        if not stopped:
            try:
                detect_modules(job_id, llm)
            except Exception:  # noqa: BLE001 - modules are a convenience; "Detect modules" can retry
                pass
        status = "done" if remaining == 0 else ("ready" if stopped else "error")
        message = None if remaining == 0 else (
            f"Stopped with {remaining} rows left." if stopped else f"{remaining} rows failed; use Resume to retry them.")
        with conn:
            conn.execute("UPDATE jobs SET status = ?, message = ? WHERE job_id = ?", (status, message, job_id))
    except Exception as exc:
        with conn:
            conn.execute("UPDATE jobs SET status = 'error', message = ? WHERE job_id = ?",
                         (f"{type(exc).__name__}: {exc}"[:500], job_id))
    finally:
        conn.close()
        with _lock:
            _running.pop(job_id, None)


def conn_status(job_id: str) -> str | None:
    conn = connect_drafts()
    try:
        row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


# ================================================================================================
# Review helpers and output
# ================================================================================================

def save_review(job_id: str, sheet: str, row_num: int, final: dict | None = None,
                reviewed: bool | None = None, cost_impact: bool | None = None) -> None:
    """Save a reviewer's changes: edited answers, approval, and the pricing flag (which starts as the
    AI's "may add cost" judgement and drives the yellow highlight in the download)."""
    conn = connect_drafts()
    with conn:
        if cost_impact is not None:
            conn.execute("UPDATE rows SET cost_impact = ? WHERE job_id = ? AND sheet = ? AND row_num = ?",
                         (int(cost_impact), job_id, sheet, row_num))
        if final is not None:
            conn.execute("UPDATE rows SET final = ? WHERE job_id = ? AND sheet = ? AND row_num = ?",
                         (json.dumps(final), job_id, sheet, row_num))
        if reviewed is not None:
            conn.execute("UPDATE rows SET reviewed = ? WHERE job_id = ? AND sheet = ? AND row_num = ?",
                         (int(reviewed), job_id, sheet, row_num))
    conn.close()


def detect_modules(job_id: str, llm=None, progress=None) -> int:
    """Set the module of drafted rows that don't have one yet; returns how many are still missing."""
    conn = connect_drafts()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT sheet, row_num, requirement, parent_text, context, evidence FROM rows"
            " WHERE job_id = ? AND ai IS NOT NULL AND module IS NULL ORDER BY sheet, row_num", (job_id,))]
        if not rows or not modules.load():
            return len(rows)
        kb_conn = store.connect()
        try:
            found = modules.classify(llm or anthropic.Anthropic(max_retries=4), MODEL, kb_conn, rows, progress)
        finally:
            kb_conn.close()
        with conn:
            conn.executemany("UPDATE rows SET module = ? WHERE job_id = ? AND sheet = ? AND row_num = ?",
                             [(m, job_id, sheet, n) for (sheet, n), m in found.items()])
        return len(rows) - len(found)
    finally:
        conn.close()


def set_module(job_id: str, sheet: str, row_nums: list[int], module: str) -> None:
    conn = connect_drafts()
    with conn:
        conn.executemany("UPDATE rows SET module = ? WHERE job_id = ? AND sheet = ? AND row_num = ?",
                         [(module, job_id, sheet, int(n)) for n in row_nums])
    conn.close()


def assign(job_id: str, sheet: str, row_nums: list[int], assignee: str | None) -> None:
    """Give rows to an SME (None = unassign). Either way they leave the returned pool."""
    conn = connect_drafts()
    with conn:
        # Partner tracking belongs to the current assignee: it's kept only if the row stays with them.
        conn.executemany("UPDATE rows SET assignee = ?, returned_by = NULL, return_note = NULL,"
                         " partner_status = CASE WHEN assignee = ? THEN partner_status END,"
                         " sent_at = CASE WHEN assignee = ? THEN sent_at END,"
                         " received_at = CASE WHEN assignee = ? THEN received_at END"
                         " WHERE job_id = ? AND sheet = ? AND row_num = ?",
                         [(assignee, assignee, assignee, assignee, job_id, sheet, int(n)) for n in row_nums])
    conn.close()


def return_row(job_id: str, sheet: str, row_num: int, by: str, note: str | None = None) -> None:
    """An SME hands a row back as not theirs: it goes to the returned pool (kept apart from rows never
    assigned) until someone reassigns it."""
    conn = connect_drafts()
    with conn:
        conn.execute("UPDATE rows SET assignee = NULL, returned_by = ?, return_note = ?, partner_status = NULL,"
                     " sent_at = NULL, received_at = NULL WHERE job_id = ? AND sheet = ? AND row_num = ?",
                     (by, note or None, job_id, sheet, row_num))
    conn.close()


# ================================================================================================
# People rows can be assigned to: SMEs (use the app) and partners (answer by email)
# ================================================================================================

SME, PARTNER = "sme", "partner"


def people(kind: str | None = None) -> list[str]:
    """Names, alphabetically: everyone, or only SMEs or partners."""
    conn = connect_drafts()
    try:
        sql = "SELECT name FROM people" + (" WHERE kind = ?" if kind else "") + " ORDER BY LOWER(name)"
        return [r[0] for r in conn.execute(sql, (kind,) if kind else ())]
    finally:
        conn.close()


def partners() -> dict[str, list[str]]:
    """Partner name -> the modules they cover."""
    conn = connect_drafts()
    try:
        return {r[0]: json.loads(r[1] or "[]") for r in conn.execute(
            "SELECT name, modules FROM people WHERE kind = ? ORDER BY LOWER(name)", (PARTNER,))}
    finally:
        conn.close()


def people_table(kind: str = SME) -> "pd.DataFrame":
    """SMEs or partners with how many rows they have open and approved, across all drafts."""
    conn = connect_drafts()
    try:
        return db.read_sql(conn, """
            SELECT p.name, p.email, p.areas, p.modules,
                   COUNT(CASE WHEN r.reviewed = 0 THEN 1 END) AS open,
                   COUNT(CASE WHEN r.reviewed = 1 THEN 1 END) AS approved
            FROM people p LEFT JOIN rows r ON r.assignee = p.name
            WHERE p.kind = ?
            GROUP BY p.name, p.email, p.areas, p.modules ORDER BY LOWER(p.name)""", [kind])
    finally:
        conn.close()


def add_person(name: str, email: str | None = None, areas: str | None = None, kind: str = SME,
               modules_covered: list[str] | None = None) -> bool:
    """Add an SME or partner; False if the name is already taken (compared ignoring case) by anyone."""
    conn = connect_drafts()
    try:
        with conn:
            if conn.execute("SELECT 1 FROM people WHERE LOWER(name) = LOWER(?)", (name,)).fetchone():
                return False
            conn.execute("INSERT INTO people (name, email, areas, created_at, kind, modules) VALUES (?, ?, ?, ?, ?, ?)",
                         (name, email or None, areas or None, _now(), kind,
                          json.dumps(modules_covered) if modules_covered else None))
        return True
    finally:
        conn.close()


def update_person(old_name: str, name: str, email: str | None, areas: str | None,
                  modules_covered: list[str] | None = None) -> bool:
    """Edit someone. A new name follows them onto their rows. False if the new name is someone else's."""
    conn = connect_drafts()
    try:
        with conn:
            if name.lower() != old_name.lower() and conn.execute(
                    "SELECT 1 FROM people WHERE LOWER(name) = LOWER(?)", (name,)).fetchone():
                return False
            conn.execute("UPDATE people SET name = ?, email = ?, areas = ?, modules = ? WHERE name = ?",
                         (name, email or None, areas or None,
                          json.dumps(modules_covered) if modules_covered else None, old_name))
            if name != old_name:
                conn.execute("UPDATE rows SET assignee = ? WHERE assignee = ?", (name, old_name))
                conn.execute("UPDATE rows SET returned_by = ? WHERE returned_by = ?", (name, old_name))
        return True
    finally:
        conn.close()


def remove_person(name: str) -> None:
    """Remove an SME or partner. Their rows still to review go to the pool of rows unassigned by an SME,
    for the bid manager to reassign; rows already approved stay approved but no longer carry the name."""
    conn = connect_drafts()
    try:
        with conn:
            conn.execute("UPDATE rows SET assignee = NULL, returned_by = ?, return_note = 'Removed from the list',"
                         " partner_status = NULL, sent_at = NULL, received_at = NULL"
                         " WHERE assignee = ? AND reviewed = 0", (name, name))
            conn.execute("UPDATE rows SET assignee = NULL WHERE assignee = ?", (name,))
            conn.execute("DELETE FROM people WHERE name = ?", (name,))
    finally:
        conn.close()


def delete_job(job_id: str) -> None:
    conn = connect_drafts()
    with conn:
        conn.execute("DELETE FROM rows WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    conn.close()
    workbook_path(job_id).unlink(missing_ok=True)


def _answer_cells(r, fields: dict[str, dict]) -> tuple[dict[str, str], list[str]]:
    """The cells one row's answer writes ({ref: value}) and every answer cell it covers."""
    n, answer = r["row_num"], json.loads(r["final"] or r["ai"])
    values, covered = {}, []
    for key in json.loads(r["fill"]):
        f = fields[key]
        value = answer.get(key) or ""
        if f["kind"] == "marks":
            chosen = next((c for c in f["columns"] if f["labels"][c] == value), None)
            if chosen:
                values[f"{chosen}{n}"] = f["labels"][chosen].split(" - ")[0] if f.get("symbol") == "header" else "X"
            covered += [f"{c}{n}" for c in f["columns"]]
        elif value:
            values[f"{f['columns'][0]}{n}"] = value
            covered.append(f"{f['columns'][0]}{n}")
    return values, covered


def build_output(job_id: str, highlight_low: bool = True, highlight_cost: bool = True) -> bytes:
    """The original workbook with drafted answers written in; nothing else changes.

    Red fills the answer cells of low-confidence rows not yet marked reviewed; yellow fills the
    requirement cell of rows that may add implementation cost.
    """
    conn = connect_drafts()
    job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    plan = json.loads(job["plan"])
    rows = conn.execute("SELECT * FROM rows WHERE job_id = ? AND ai IS NOT NULL", (job_id,)).fetchall()
    conn.close()
    fields_by_sheet = {sp["name"]: {f["key"]: f for f in sp["fields"]} for sp in plan["sheets"]}
    req_col = {sp["name"]: sp["columns"]["requirement"] for sp in plan["sheets"]}

    values: dict[str, dict[str, str | None]] = {}
    fills: dict[str, dict[str, str]] = {}
    for r in rows:
        sheet, n = r["sheet"], r["row_num"]
        cells, written = _answer_cells(r, fields_by_sheet[sheet])
        values.setdefault(sheet, {}).update(cells)
        if highlight_low and r["confidence"] == "low" and not r["reviewed"]:
            for ref in written:
                fills.setdefault(sheet, {})[ref] = RED
        if highlight_cost and r["cost_impact"]:
            fills.setdefault(sheet, {})[f"{req_col[sheet]}{n}"] = YELLOW

    return patch_workbook(workbook_path(job_id).read_bytes(), values, fills)


# ================================================================================================
# Partners: a trimmed copy of the client's worksheet goes out by email, their answers come back
# ================================================================================================

def build_partner_export(job_id: str, partner: str) -> bytes:
    """The client's workbook as a partner should see it: their rows filled with our drafted answers,
    every other requirement row hidden, and requirement sheets with none of their rows hidden.
    Nothing is deleted, so their reply lines up row for row. No highlights, no pricing."""
    conn = connect_drafts()
    try:
        plan = json.loads(conn.execute("SELECT plan FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0])
        rows = conn.execute("SELECT * FROM rows WHERE job_id = ? AND assignee = ? AND ai IS NOT NULL",
                            (job_id, partner)).fetchall()
        last_row = {r[0]: r[1] for r in conn.execute(
            "SELECT sheet, MAX(row_num) FROM rows WHERE job_id = ? GROUP BY sheet", (job_id,))}
    finally:
        conn.close()
    fields_by_sheet = {sp["name"]: {f["key"]: f for f in sp["fields"]} for sp in plan["sheets"]}
    values: dict[str, dict[str, str | None]] = {}
    theirs: dict[str, set[int]] = {}
    for r in rows:
        values.setdefault(r["sheet"], {}).update(_answer_cells(r, fields_by_sheet[r["sheet"]])[0])
        theirs.setdefault(r["sheet"], set()).add(r["row_num"])
    # Hide the requirement rows that aren't theirs: everything between the header and the last requirement
    # row (title, instructions and header above, and any notes below, stay visible).
    hidden_rows = {sp["name"]: set(range(sp["header_row"] + 1, last_row[sp["name"]] + 1)) - theirs[sp["name"]]
                   for sp in plan["sheets"] if sp["name"] in theirs}
    hidden_sheets = {sp["name"] for sp in plan["sheets"]
                     if sp.get("include", True) and sp["name"] in last_row and sp["name"] not in theirs}
    return patch_workbook(workbook_path(job_id).read_bytes(), values,
                          hidden_rows=hidden_rows, hidden_sheets=hidden_sheets)


def mark_sent(job_id: str, partner: str) -> None:
    """Record that the partner's rows went out (rows already answered stay received)."""
    conn = connect_drafts()
    with conn:
        conn.execute("UPDATE rows SET sent_at = ?, partner_status = CASE WHEN partner_status = 'received'"
                     " THEN 'received' ELSE 'sent' END WHERE job_id = ? AND assignee = ? AND ai IS NOT NULL",
                     (_now(), job_id, partner))
    conn.close()


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


ANSWERED, SAME, BLANK, MISMATCH, INVALID, MISSING = (
    "Changed", "Same as our draft", "Left blank", "Requirement doesn't match", "Not an allowed option", "Not in file")
IMPORTABLE = (ANSWERED, SAME)


def read_partner_answers(job_id: str, partner: str, data: bytes) -> list[dict]:
    """Match a partner's returned workbook to their rows: per row our current answer, theirs, and a status
    (see the constants above). Rows are found by sheet and row number, then checked by requirement text in
    case rows were inserted or deleted."""
    conn = connect_drafts()
    try:
        plan = json.loads(conn.execute("SELECT plan FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0])
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM rows WHERE job_id = ? AND assignee = ? AND ai IS NOT NULL ORDER BY sheet, row_num",
            (job_id, partner))]
    finally:
        conn.close()
    sheets = read_workbook(data)
    plans = {sp["name"]: sp for sp in plan["sheets"]}
    # Requirement text -> line, per sheet, to find rows that moved (a partner inserted or deleted rows).
    by_text: dict[str, dict[str, list]] = {}
    for name, sp in plans.items():
        index: dict[str, list] = {}
        req = _col(sp["columns"]["requirement"])
        for line in sheets.get(name) or []:
            if req < len(line) and _norm(line[req]):
                index.setdefault(_norm(line[req]), []).append(line)
        by_text[name] = index
    out = []
    for r in rows:
        sp = plans[r["sheet"]]
        fields = {f["key"]: f for f in sp["fields"]}
        ours = json.loads(r["final"] or r["ai"])
        item = {"sheet": r["sheet"], "row_num": r["row_num"], "requirement": r["requirement"], "ours": ours,
                "theirs": {}, "fill": json.loads(r["fill"])}
        grid = sheets.get(r["sheet"])
        line = grid[r["row_num"] - 1] if grid and r["row_num"] - 1 < len(grid) else None
        if line is None:
            out.append(item | {"status": MISSING})
            continue

        def cell(letter):
            i = _col(letter)
            return line[i] if i < len(line) else None

        found = _norm(cell(sp["columns"]["requirement"]))
        expected = _norm(r["requirement"])
        if not found or not (found[:60] in expected or expected[:60] in found):
            moved = by_text[r["sheet"]].get(expected) or []
            if len(moved) != 1:
                out.append(item | {"status": MISMATCH})
                continue
            line = moved[0]
        status = None
        for key in item["fill"]:
            f = fields[key]
            if f["kind"] == "marks":
                marked = [f["labels"][c] for c in f["columns"] if not _is_empty(cell(c))]
                value = marked[0] if len(marked) == 1 else ""
                if len(marked) > 1:
                    status = INVALID
            else:
                raw = cell(f["columns"][0])
                value = "" if _is_empty(raw) else str(raw).strip()
                if value and f["kind"] == "choice":
                    canonical = next((o for o in f.get("options") or [] if _norm(o) == _norm(value)), None)
                    if canonical is None:
                        status = INVALID
                    value = canonical or value
            item["theirs"][key] = value
        if status is None:
            if not any(item["theirs"].values()):
                status = BLANK
            elif all((item["theirs"].get(k) or "") == (ours.get(k) or "") for k in item["fill"]):
                status = SAME
            else:
                status = ANSWERED
        out.append(item | {"status": status})
    return out


def approve_partner_answers(job_id: str, partner: str) -> int:
    """Approve every answer the partner sent back that isn't approved yet; returns how many."""
    conn = connect_drafts()
    try:
        with conn:
            return conn.execute("UPDATE rows SET reviewed = 1 WHERE job_id = ? AND assignee = ?"
                                " AND partner_status = 'received' AND reviewed = 0", (job_id, partner)).rowcount
    finally:
        conn.close()


def apply_partner_answers(job_id: str, items: list[dict]) -> int:
    """Save the importable answers from read_partner_answers as received (not approved); returns how many.
    A changed answer needs approving again even if the row was approved before."""
    now, done = _now(), 0
    conn = connect_drafts()
    try:
        with conn:
            for item in items:
                if item["status"] not in IMPORTABLE:
                    continue
                answer = item["ours"] | item["theirs"]
                conn.execute("UPDATE rows SET final = ?, partner_status = 'received', received_at = ?,"
                             " reviewed = CASE WHEN ? THEN 0 ELSE reviewed END"
                             " WHERE job_id = ? AND sheet = ? AND row_num = ?",
                             (json.dumps(answer), now, item["status"] == ANSWERED, job_id, item["sheet"],
                              item["row_num"]))
                done += 1
        return done
    finally:
        conn.close()
