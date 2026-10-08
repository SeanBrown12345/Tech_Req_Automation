"""Surgical .xlsx editing: change only the cells we fill, copy every other byte as-is.

openpyxl rewrites the whole workbook on save and drops what it doesn't model (x14 dropdowns,
images, external links, threaded comments, custom XML), so the drafter never saves through it.
Instead we patch the cell XML of the affected sheets and, for highlights, append fill styles
to styles.xml. Everything else in the package is copied unchanged.
"""

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass

from lxml import etree
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
X14_NS = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM_NS = "http://schemas.microsoft.com/office/excel/2006/main"
q = lambda tag: f"{{{NS}}}{tag}"

_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


def split_ref(ref: str) -> tuple[str, int]:
    m = _CELL_REF.match(ref)
    return m.group(1), int(m.group(2))


def sheet_parts(z: zipfile.ZipFile) -> dict[str, str]:
    """Sheet name -> part path inside the package (e.g. 'xl/worksheets/sheet1.xml')."""
    wb = etree.fromstring(z.read("xl/workbook.xml"))
    rels = etree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels.iter(f"{{{PKG_REL_NS}}}Relationship")}
    parts = {}
    for sheet in wb.iter(q("sheet")):
        target = targets[sheet.get(f"{{{REL_NS}}}id")]
        parts[sheet.get("name")] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
            posixpath.join("xl", target))
    return parts


# ---- Reading dropdown lists --------------------------------------------------------------------

@dataclass
class Dropdown:
    ranges: list[tuple[int, int, int, int]]  # (min_col, min_row, max_col, max_row), 1-based
    options: list[str]

    def covers(self, col: int, row: int) -> bool:
        return any(c1 <= col <= c2 and r1 <= row <= r2 for c1, r1, c2, r2 in self.ranges)


def _ranges(sqref: str) -> list[tuple[int, int, int, int]]:
    out = []
    for part in sqref.split():
        try:
            out.append(range_boundaries(part))
        except ValueError:
            continue
    return out


def dropdowns(data: bytes, values: dict[str, list[tuple]]) -> dict[str, list[Dropdown]]:
    """List-type data validations per sheet, with their options resolved.

    `values` maps sheet name -> rows of cell values (as read by openpyxl), used to resolve
    options given as a range ("$A$4:$A$7", "Lists!$B$3:$B$6") or a defined name.
    """
    z = zipfile.ZipFile(io.BytesIO(data))
    wb = etree.fromstring(z.read("xl/workbook.xml"))
    names = {d.get("name"): d.text for d in wb.iter(q("definedName")) if d.text}

    def resolve(formula: str, sheet: str) -> list[str]:
        formula = (formula or "").strip()
        if formula.startswith('"'):
            return [o.strip() for o in formula.strip('"').split(",") if o.strip()]
        formula = names.get(formula, formula).lstrip("=")
        if "!" in formula:
            sheet, formula = formula.rsplit("!", 1)
            sheet = sheet.strip("'")
        rows = values.get(sheet)
        if rows is None or "#REF" in formula or "[" in sheet:
            return []
        try:
            c1, r1, c2, r2 = range_boundaries(formula.replace("$", ""))
        except ValueError:
            return []
        opts = []
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                if r - 1 < len(rows) and c - 1 < len(rows[r - 1]):
                    v = rows[r - 1][c - 1]
                    if v is not None and str(v).strip():
                        opts.append(str(v).strip())
        return opts

    out: dict[str, list[Dropdown]] = {}
    for sheet, part in sheet_parts(z).items():
        if part not in z.namelist():
            continue
        root = etree.fromstring(z.read(part))
        found = []
        for dv in root.iter(q("dataValidation")):
            f1 = dv.find(q("formula1"))
            if dv.get("type") == "list" and f1 is not None:
                found.append(Dropdown(_ranges(dv.get("sqref", "")), resolve(f1.text, sheet)))
        for dv in root.iter(f"{{{X14_NS}}}dataValidation"):
            f = dv.find(f"{{{X14_NS}}}formula1/{{{XM_NS}}}f")
            sq = dv.find(f"{{{XM_NS}}}sqref")
            if dv.get("type") == "list" and f is not None and sq is not None:
                found.append(Dropdown(_ranges(sq.text or ""), resolve(f.text, sheet)))
        out[sheet] = [d for d in found if d.options and d.ranges]
    return out


# ---- Writing -----------------------------------------------------------------------------------

class _Styles:
    """Appends highlight fills to styles.xml, cloning each base cell format once per color."""

    def __init__(self, xml: bytes):
        self.root = etree.fromstring(xml)
        self.fills = self.root.find(q("fills"))
        self.xfs = self.root.find(q("cellXfs"))
        self._fill_ids: dict[str, int] = {}
        self._xf_ids: dict[tuple[int, str], int] = {}

    def fill_id(self, rgb: str) -> int:
        if rgb not in self._fill_ids:
            fill = etree.SubElement(self.fills, q("fill"))
            pattern = etree.SubElement(fill, q("patternFill"), patternType="solid")
            etree.SubElement(pattern, q("fgColor"), rgb=rgb)
            etree.SubElement(pattern, q("bgColor"), indexed="64")
            self.fills.set("count", str(len(self.fills)))
            self._fill_ids[rgb] = len(self.fills) - 1
        return self._fill_ids[rgb]

    def highlighted(self, base_xf: int, rgb: str) -> int:
        key = (base_xf, rgb)
        if key not in self._xf_ids:
            xfs = list(self.xfs)
            base = xfs[base_xf] if base_xf < len(xfs) else xfs[0]
            clone = etree.fromstring(etree.tostring(base))
            clone.set("fillId", str(self.fill_id(rgb)))
            clone.set("applyFill", "1")
            self.xfs.append(clone)
            self.xfs.set("count", str(len(self.xfs)))
            self._xf_ids[key] = len(self.xfs) - 1
        return self._xf_ids[key]

    def to_bytes(self) -> bytes:
        return etree.tostring(self.root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _row(sheet_data, rows: dict[int, etree._Element], r: int):
    """Find or create the <row> numbered `r`, keeping rows in document order."""
    row = rows.get(r)
    if row is None:
        row = etree.Element(q("row"), r=str(r))
        later = [n for n in rows if n > r]
        if later:
            rows[min(later)].addprevious(row)
        else:
            sheet_data.append(row)
        rows[r] = row
    return row


def _row_and_cell(sheet_data, rows: dict[int, etree._Element], ref: str):
    """Find or create the <row> and <c> for `ref`, keeping both in document order."""
    col, r = split_ref(ref)
    row = _row(sheet_data, rows, r)
    col_idx = column_index_from_string(col)
    for c in row.findall(q("c")):
        c_col, _ = split_ref(c.get("r"))
        c_idx = column_index_from_string(c_col)
        if c_idx == col_idx:
            return row, c
        if c_idx > col_idx:
            cell = etree.Element(q("c"), r=ref)
            c.addprevious(cell)
            break
    else:
        cell = etree.SubElement(row, q("c"), r=ref)
    if row.get("s") and row.get("customFormat") == "1":
        cell.set("s", row.get("s"))  # a new cell inherits the row's formatting
    return row, cell


def _set_value(cell, value: str | None) -> None:
    for child in list(cell):
        if child.tag in (q("v"), q("f"), q("is")):
            cell.remove(child)
    if value is None or value == "":
        cell.attrib.pop("t", None)
        return
    cell.set("t", "inlineStr")
    is_ = etree.SubElement(cell, q("is"))
    t = etree.SubElement(is_, q("t"))
    t.text = value
    if value != value.strip() or "\n" in value:
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _hide_sheets(xml: bytes, hidden: set[str]) -> tuple[bytes, list[str]]:
    """workbook.xml with `hidden` sheets set to hidden and the first visible sheet made the active tab.
    Returns the new XML and the sheet names in tab order."""
    root = etree.fromstring(xml)
    sheets = list(root.iter(q("sheet")))
    for sheet in sheets:
        if sheet.get("name") in hidden:
            sheet.set("state", "hidden")
    visible = [i for i, sheet in enumerate(sheets) if sheet.get("state") not in ("hidden", "veryHidden")]
    if visible:
        for view in root.iter(q("workbookView")):
            if int(view.get("activeTab", "0")) not in visible:
                view.set("activeTab", str(visible[0]))
            if int(view.get("firstSheet", "0")) not in visible:
                view.set("firstSheet", str(visible[0]))
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True),         [sheet.get("name") for sheet in sheets]


def patch_workbook(data: bytes, values: dict[str, dict[str, str | None]],
                   highlights: dict[str, dict[str, str]] | None = None,
                   hidden_rows: dict[str, set[int]] | None = None, hidden_sheets: set[str] | None = None) -> bytes:
    """Return a copy of the workbook with cell values set and cells highlighted.

    values:        {sheet: {"F7": "1 - Current Functionality", ...}}; None/"" clears the value.
    highlights:    {sheet: {"E7": "FFFFC7CE", ...}} ARGB fill colors.
    hidden_rows:   {sheet: {12, 13, ...}} rows to hide (nothing is deleted, so references stay intact).
    hidden_sheets: sheet names to hide; the active tab moves to the first visible sheet.
    Cell styles (fonts, borders, number formats) are kept; highlighted cells get a copy of their
    style with only the fill changed.
    """
    highlights, hidden_rows, hidden_sheets = highlights or {}, hidden_rows or {}, hidden_sheets or set()
    src = zipfile.ZipFile(io.BytesIO(data))
    parts = sheet_parts(src)
    styles = _Styles(src.read("xl/styles.xml")) if any(highlights.values()) else None

    patched: dict[str, bytes] = {}
    if hidden_sheets:
        patched["xl/workbook.xml"], _ = _hide_sheets(src.read("xl/workbook.xml"), hidden_sheets)
    for sheet in set(values) | set(highlights) | {s for s, rows in hidden_rows.items() if rows} | hidden_sheets:
        if sheet not in parts:
            raise KeyError(f"Sheet {sheet!r} not in workbook")
        root = etree.fromstring(src.read(parts[sheet]))
        sheet_data = root.find(q("sheetData"))
        rows = {int(r.get("r")): r for r in sheet_data.findall(q("row"))}
        for ref, value in (values.get(sheet) or {}).items():
            _, cell = _row_and_cell(sheet_data, rows, ref)
            _set_value(cell, value)
        for ref, rgb in (highlights.get(sheet) or {}).items():
            _, cell = _row_and_cell(sheet_data, rows, ref)
            cell.set("s", str(styles.highlighted(int(cell.get("s", "0")), rgb)))
        for n in sorted(hidden_rows.get(sheet) or ()):
            _row(sheet_data, rows, n).set("hidden", "1")
        if sheet in hidden_sheets:  # a hidden sheet can't stay selected, or Excel groups it with the active one
            for view in root.iter(q("sheetView")):
                view.attrib.pop("tabSelected", None)
        patched[parts[sheet]] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    if styles:
        patched["xl/styles.xml"] = styles.to_bytes()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            body = patched.get(info.filename)
            if body is None:
                dst.writestr(info, src.read(info.filename))  # byte-for-byte copy
            else:
                dst.writestr(info, body, compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue()


def cell_ref(col_letter: str, row: int) -> str:
    return f"{col_letter}{row}"


__all__ = ["Dropdown", "dropdowns", "patch_workbook", "sheet_parts", "cell_ref", "get_column_letter"]
