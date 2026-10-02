"""Load and validate worksheet profiles and scale maps.

A *profile* (config/profiles/*.yaml) describes one workbook: which sheets to
read, where the header row is, which column holds what, and how the answer is
encoded. A *scale* (config/scales.yaml) maps one worksheet response vocabulary
onto the internal statuses in kb.statuses.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from openpyxl.utils import column_index_from_string

from kb.statuses import STATUSES

COLUMN_ROLES = {"req_id", "category", "subcategory", "requirement", "comment"}

_PUNCT = str.maketrans({"’": "'", "‘": "'", "–": "-", "—": "-", "‑": "-"})


def normalize_value(value) -> str:
    """Canonical form for matching responses: lowercase, plain quotes/dashes, single spaces."""
    return re.sub(r"\s+", " ", str(value).translate(_PUNCT)).strip().lower()


def col_index(letter: str) -> int:
    """Zero-based index for an Excel column letter."""
    return column_index_from_string(letter.strip().upper()) - 1


@dataclass
class Scale:
    name: str
    description: str
    values: dict[str, str | None]  # normalized response -> status (None = known value, no info)

    def lookup(self, raw: str) -> tuple[bool, str | None]:
        """Return (known, status) for a raw cell value."""
        key = normalize_value(raw)
        if key in self.values:
            return True, self.values[key]
        return False, None


@dataclass
class Signal:
    """One answer signal. Signals are applied in order; a later non-null status refines an earlier one."""
    name: str
    column: int | None = None          # value signal: cell text mapped through `scale`
    scale: Scale | None = None
    marks: dict[int, str] | None = None  # mark signal: any non-blank cell in column -> status
    refines: set[str] | None = None      # only override when the current status is unset or one of these


@dataclass
class SheetProfile:
    name: str
    module: str | None
    header_row: int
    first_data_row: int
    columns: dict[str, int]
    attributes: dict[str, int]
    signals: list[Signal]


@dataclass
class Profile:
    source_id: str
    path: Path
    file: Path
    source: dict
    sheets: list[SheetProfile] = field(default_factory=list)


def _parse_scale(name: str, spec: dict, where: str) -> Scale:
    values = {}
    for response, status in (spec.get("values") or {}).items():
        if status is not None and status not in STATUSES:
            raise ValueError(f"{where}: scale '{name}' maps {response!r} to unknown status {status!r}")
        values[normalize_value(response)] = status
    return Scale(name, spec.get("description", ""), values)


def load_scales(path: Path) -> dict[str, Scale]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {name: _parse_scale(name, spec, str(path)) for name, spec in raw.items()}


def _parse_signals(specs: list[dict], scales: dict[str, Scale], where: str) -> list[Signal]:
    signals = []
    for i, spec in enumerate(specs):
        name = spec.get("name") or f"answer_{i + 1}"
        refines = set(spec["refines"]) if "refines" in spec else None
        if refines and not refines <= STATUSES.keys():
            raise ValueError(f"{where}: 'refines' has unknown statuses {sorted(refines - STATUSES.keys())}")
        if "marks" in spec:
            marks = {}
            for letter, status in spec["marks"].items():
                if status not in STATUSES:
                    raise ValueError(f"{where}: mark column {letter} uses unknown status {status!r}")
                marks[col_index(letter)] = status
            signals.append(Signal(name, marks=marks, refines=refines))
        elif "column" in spec:
            if "values" in spec:  # scale defined inline (profiles created in the dashboard)
                scale = _parse_scale(f"{where}:{name}", {"values": spec["values"]}, where)
            elif spec.get("scale") in scales:
                scale = scales[spec["scale"]]
            else:
                raise ValueError(f"{where}: unknown scale {spec.get('scale')!r}")
            signals.append(Signal(name, column=col_index(spec["column"]), scale=scale, refines=refines))
        else:
            raise ValueError(f"{where}: answer entry needs 'column' + 'scale'/'values', or 'marks': {spec}")
    return signals


def load_profile(path: Path, scales: dict[str, Scale], root: Path) -> Profile:
    """Load a profile file. Its `file` is resolved against `root`."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_profile(raw, path.stem, scales, root, path)


def parse_profile(raw: dict, source_id: str, scales: dict[str, Scale], root: Path,
                  path: Path | None = None) -> Profile:
    """Build a Profile from its dict form (the YAML structure)."""
    where = str(path or source_id)
    file = root / raw["file"]
    if not file.exists():
        raise FileNotFoundError(f"{where}: workbook not found: {file}")

    defaults = raw.get("defaults") or {}
    profile = Profile(source_id=source_id, path=path or Path(source_id), file=file, source=raw.get("source") or {})

    for sheet in raw["sheets"]:
        spec = {**defaults, **sheet}
        columns = {role: col_index(letter) for role, letter in (spec.get("columns") or {}).items()}
        unknown = set(columns) - COLUMN_ROLES
        if unknown:
            raise ValueError(f"{where}: unknown column roles {sorted(unknown)} (allowed: {sorted(COLUMN_ROLES)})")
        if "requirement" not in columns:
            raise ValueError(f"{where}: sheet {spec['name']!r} has no 'requirement' column")
        header_row = int(spec["header_row"])
        profile.sheets.append(SheetProfile(
            name=spec["name"],
            module=spec.get("module"),
            header_row=header_row,
            first_data_row=int(spec.get("first_data_row", header_row + 1)),
            columns=columns,
            attributes={k: col_index(v) for k, v in (spec.get("attributes") or {}).items()},
            signals=_parse_signals(spec.get("answer") or [], scales, f"{where} [{spec['name']}]"),
        ))
    return profile
