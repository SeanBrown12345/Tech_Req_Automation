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
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import anthropic
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

from kb import settings, store
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

        first_data = first_req + 1
        for letter, head in guess.headers.items():
            if letter in role_cols - {roles.get("comment")} or letter in guess.mark_columns:
                continue
            options = next((d.options for d in lists.get(name, [])
                            if d.covers(_col(letter) + 1, first_data)), None)
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
    reviewed     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    PRIMARY KEY (job_id, sheet, row_num)
);
"""


def connect_drafts() -> sqlite3.Connection:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.DRAFTS_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # readers (the dashboard) don't block the drafting thread
    conn.executescript(DRAFTS_SCHEMA)
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
        if f["kind"] == "choice":
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
5. Comments and narratives: professional, concise, in our voice and consistent with the wording of our past comments. Never mention "evidence", other clients, or previous RFPs. Leave a comment field "" when the instructions don't call for one and a comment adds nothing.
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
        if f["kind"] == "choice":
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

        kb_conn = store.connect(settings.DB_PATH)
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
                reviewed: bool | None = None) -> None:
    conn = connect_drafts()
    with conn:
        if final is not None:
            conn.execute("UPDATE rows SET final = ? WHERE job_id = ? AND sheet = ? AND row_num = ?",
                         (json.dumps(final), job_id, sheet, row_num))
        if reviewed is not None:
            conn.execute("UPDATE rows SET reviewed = ? WHERE job_id = ? AND sheet = ? AND row_num = ?",
                         (int(reviewed), job_id, sheet, row_num))
    conn.close()


def delete_job(job_id: str) -> None:
    conn = connect_drafts()
    with conn:
        conn.execute("DELETE FROM rows WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    conn.close()
    workbook_path(job_id).unlink(missing_ok=True)


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
        answer = json.loads(r["final"] or r["ai"])
        written = []
        for key in json.loads(r["fill"]):
            f = fields_by_sheet[sheet][key]
            value = answer.get(key) or ""
            if f["kind"] == "marks":
                chosen = next((c for c in f["columns"] if f["labels"][c] == value), None)
                if chosen:
                    symbol = chosen and (f["labels"][chosen].split(" - ")[0] if f.get("symbol") == "header" else "X")
                    values.setdefault(sheet, {})[f"{chosen}{n}"] = symbol
                written += [f"{c}{n}" for c in f["columns"]]
            elif value:
                values.setdefault(sheet, {})[f"{f['columns'][0]}{n}"] = value
                written.append(f"{f['columns'][0]}{n}")
        if highlight_low and r["confidence"] == "low" and not r["reviewed"]:
            for ref in written:
                fills.setdefault(sheet, {})[ref] = RED
        if highlight_cost and r["cost_impact"]:
            fills.setdefault(sheet, {})[f"{req_col[sheet]}{n}"] = YELLOW

    return patch_workbook(workbook_path(job_id).read_bytes(), values, fills)
