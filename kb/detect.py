"""Best-effort guesses for an unseen worksheet: header row, column roles, answer legend, statuses.

Everything here is a suggestion for a person to confirm in the dashboard - nothing is loaded
on a guess alone.
"""

import re
from collections import Counter
from itertools import islice
from dataclasses import dataclass, field

from openpyxl.utils import column_index_from_string, get_column_letter

from kb.extract import clean
from kb.profile import normalize_value

HEADER_SCAN_ROWS = 40
SAMPLE_ROWS = 300

_HEADER_WORDS = re.compile(
    r"requirement|description|response|comment|narrative|req\b|req\.|#|topic|function|process|"
    r"attribute|compl|status|module|category|group|notes|specification|availability", re.IGNORECASE)

_ROLE_PATTERNS = [  # checked in order; first role whose pattern matches a header wins that column
    ("req_id", re.compile(r"^(req(uirement)?\.?\s*(#|id|no\.?|number)|#|id|item\s*#?|ref(erence)?\s*#?)$", re.I)),
    ("comment", re.compile(r"comment|narrative|explanation|notes|remarks", re.I)),
    ("requirement", re.compile(r"requirement|description|attribute|specification", re.I)),
    ("category", re.compile(r"functional group|category|^function$|functional area|^area$", re.I)),
    ("subcategory", re.compile(r"topic|process|sub-?category", re.I)),
]
_ANSWER_HEADER = re.compile(r"response|compl|availability|status|support|meets|available", re.I)
_LEGEND_PAIR = re.compile(r"\b([A-Z][A-Z0-9-]{0,4})\s*[:=]\s*(.+?)(?=\s{2,}[A-Z][A-Z0-9-]{0,4}\s*[:=]|$)")

# Keyword rules for guessing a status from a response value or its legend text. Order matters.
_STATUS_RULES = [
    (re.compile(r"n/a|not applicable", re.I), "NOT_APPLICABLE"),
    (re.compile(r"cannot|can't|not met|not supported|not available|not provided|do not comply|"
                r"^(n|no|ns)$", re.I), "NOT_SUPPORTED"),
    (re.compile(r"partial|^p$", re.I), "PARTIAL"),
    (re.compile(r"third|3rd|^(tps|t)$", re.I), "THIRD_PARTY"),
    (re.compile(r"custom|modif|^(y-nd|c)$", re.I), "CUSTOM"),
    (re.compile(r"scheduled|upcoming|next release", re.I), "SCHEDULED"),
    (re.compile(r"future|roadmap|pilot|testing|^f$", re.I), "FUTURE"),
    (re.compile(r"more info|discuss|^i$", re.I), "NEEDS_DISCUSSION"),
    (re.compile(r"current|standard|base functionality|out of the box|production|^s$", re.I), "STANDARD"),
    (re.compile(r"^y(es)?$|comply|^x$|supported|meets?\b|met\b", re.I), "SUPPORTED"),
]


@dataclass
class SheetGuess:
    name: str
    header_row: int                     # 1-based
    headers: dict[str, str]             # column letter -> header text
    columns: dict[str, str]             # role -> column letter
    answer_columns: list[str]           # columns holding a response value
    mark_columns: list[str]             # columns where a mark ("X", a letter) selects the answer
    legend: dict[str, str] = field(default_factory=dict)  # code -> meaning, from rows above the header
    data_rows: int = 0


def guess_status(value: str, legend: dict[str, str] | None = None) -> str | None:
    """Suggest an internal status for a response value (or a mark column's header)."""
    text = clean(value)
    if not text:
        return None
    candidates = [text]
    if legend and text.upper() in legend:
        candidates.insert(0, legend[text.upper()])  # the legend's wording is more informative
    for candidate in candidates:
        for pattern, status in _STATUS_RULES:
            if pattern.search(candidate.strip()):
                return status
    return None


def read_rows(ws, limit: int | None = None) -> list[tuple]:
    return list(islice(ws.iter_rows(values_only=True), limit))


def _parse_legend(rows: list[tuple]) -> dict[str, str]:
    legend = {}
    for row in rows:
        cells = [clean(v) for v in row]
        texts = [c for c in cells if c]
        # Two-cell rows: "Y" | "Requirement Met and Proposed (...)"
        if len(texts) == 2 and re.fullmatch(r"[A-Z][A-Z0-9-]{0,4}", texts[0]) and len(texts[1]) > 3:
            legend[texts[0]] = texts[1]
            continue
        # Inline legends: "S: Standard     P: Partial     F: Future"
        for text in texts:
            for code, meaning in _LEGEND_PAIR.findall(text):
                legend.setdefault(code, meaning.strip())
    return legend


def _header_score(row: tuple) -> float:
    texts = [str(v).strip() for v in row if clean(v)]
    if len(texts) < 3:
        return 0
    hits = sum(1 for t in texts if len(t) <= 80 and _HEADER_WORDS.search(t))
    return hits + 0.1 * min(len(texts), 10) if hits >= 2 else 0


def guess_sheet(name: str, rows: list[tuple], header_row: int | None = None) -> SheetGuess | None:
    """Guess the layout of one sheet from its rows (values only). None if no header row is found.
    Pass `header_row` (1-based) to skip header detection and use that row."""
    if header_row:
        if header_row > len(rows):
            return None
        header_idx = header_row - 1
    else:
        scores = [(_header_score(r), i) for i, r in enumerate(rows[:HEADER_SCAN_ROWS])]
        best, header_idx = max(scores, default=(0, 0))
        if best == 0:
            return None
    header = rows[header_idx]
    headers = {get_column_letter(i + 1): str(v).strip() for i, v in enumerate(header) if clean(v)}
    data = [r for r in rows[header_idx + 1: header_idx + 1 + SAMPLE_ROWS] if any(clean(v) for v in r)]
    legend = _parse_legend(rows[:header_idx])

    def values(letter):
        idx = _col_idx(letter)
        return [clean(r[idx]) for r in data if idx < len(r) and clean(r[idx])]

    def avg_len(letter):
        vals = values(letter)
        return sum(len(v) for v in vals) / len(vals) if vals else 0

    # Mark columns: short headers whose cells only ever hold "X" or their own header text, plus
    # empty siblings whose header is a legend code (a UWU tab may never use its "F" column).
    marks = []
    for letter, text in headers.items():
        vals = {v.strip('"').upper() for v in values(letter)}
        if len(text) <= 30 and vals and (vals <= {"X"} or vals <= {text.upper()}):
            marks.append(letter)
    if marks:
        marks += [l for l, t in headers.items() if l not in marks and not values(l) and t.upper() in legend]
        marks.sort(key=_col_idx)
    else:  # blank template: two or more adjacent empty columns headed by legend codes
        coded = [l for l, t in headers.items() if len(t) <= 5 and t.upper() in legend and not values(l)]
        if len(coded) >= 2 and all(_col_idx(b) - _col_idx(a) == 1 for a, b in zip(coded, coded[1:])):
            marks = coded

    columns: dict[str, str] = {}
    candidates: dict[str, list[str]] = {}
    for letter, text in headers.items():
        if letter in marks:
            continue
        for role, pattern in _ROLE_PATTERNS:
            if pattern.search(text):
                candidates.setdefault(role, []).append(letter)
                break
    for role, letters in candidates.items():
        # Prefer the candidate with the most (and longest) content, e.g. the real requirement text.
        columns[role] = max(letters, key=lambda l: (len(values(l)) > 0, avg_len(l)))
    if "requirement" not in columns:
        text_cols = [l for l in headers if l not in marks]
        if text_cols:
            columns["requirement"] = max(text_cols, key=avg_len)

    # Answer columns: an answer-like header, few distinct values, and most of them read as a status.
    used = set(columns.values()) | set(marks)
    answers = []
    for letter, text in headers.items():
        vals = values(letter)
        if letter in used or not vals or not _ANSWER_HEADER.search(text):
            continue
        distinct = {normalize_value(v) for v in vals}
        readable = sum(1 for v in vals if guess_status(v, legend))
        if len(distinct) <= 15 and readable >= 0.6 * len(vals):
            answers.append(letter)
    if marks:  # the marks are the answer; only an availability/status column can sharpen them
        answers = [l for l in answers if re.search(r"availability|status", headers[l], re.I)]

    return SheetGuess(
        name=name,
        header_row=header_idx + 1,
        headers=headers,
        columns=columns,
        answer_columns=answers,
        mark_columns=marks,
        legend=legend,
        data_rows=len([r for r in rows[header_idx + 1:] if any(clean(v) for v in r)]),
    )


def distinct_values(rows: list[tuple], letter: str, first_row: int,
                    requirement_letter: str | None = None) -> Counter:
    """Response values in one column from `first_row` (1-based) down, normalized-deduplicated.
    With `requirement_letter`, rows without requirement text (headings, legends) are skipped."""
    idx = _col_idx(letter)
    req_idx = _col_idx(requirement_letter) if requirement_letter else None
    seen: dict[str, str] = {}
    counts: Counter = Counter()
    for row in rows[first_row - 1:]:
        if req_idx is not None and not (req_idx < len(row) and clean(row[req_idx])):
            continue
        value = clean(row[idx]) if idx < len(row) else None
        if value:
            key = normalize_value(value)
            seen.setdefault(key, value)
            counts[seen[key]] += 1
    return counts


def _col_idx(letter: str) -> int:
    return column_index_from_string(letter) - 1
