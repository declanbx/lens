"""repo_index.extractors.xlsx — Excel workbook structure (sheet names + headers).

An .xlsx/.xlsm workbook is a ZIP of XML parts, so this reads it with the STDLIB
only (``zipfile`` + ``xml.etree.ElementTree``) — no openpyxl, no pandas, nothing
to bundle or version-match. See CONTRACTS.md §4.5.

What it reads, per sheet: the sheet's NAME, its HEADER ROW, the column count, and
the row count. THE HEADER RULE is stated once and applied everywhere: the header
is the first row carrying at least two non-empty cells, looked for within the
first eight rows, falling back to the first non-empty row. That skips the merged
title banner journal supplementary tables put above the real headers. What it never
reads: any data cell below the header. The one exception is row COUNTING, which
streams row start-tags (values discarded) and is size-gated exactly like the
csv row count — above the gate the sheet's declared ``<dimension>`` range is
reported instead, flagged inexact.

Shared strings are resolved LAZILY: header cells carry an INDEX into a workbook-
wide string table that can run to tens of MB, so the table is streamed once and
only the indices the header rows actually reference are kept. Peak memory is the
header text, not the string table.

extract() return-dict keys
--------------------------
    { "n_sheets": int,
      "sheet_names": list[str],                    # document order
      "sheets": [ { "name": str, "columns": list[str], "n_columns": int,
                    "row_count": int|None, "row_count_exact": bool,
                    "row_count_reason": str|None }, ... ],
      # SINGLE-SHEET WORKBOOKS ONLY — a one-sheet workbook is a csv with tabs,
      # so it also reports its shape at the top level the way a csv does. A
      # multi-sheet workbook reports no top-level shape at all (there isn't one):
      "primary_sheet": str,
      "columns": list[str], "n_columns": int,
      "row_count": int|None, "row_count_exact": bool,
      "row_count_reason": str|None }

``row_count_reason`` is present only when the count is missing or inexact:
"size_gated" (fell back to the declared dimension) | "no_dimension" (gated and
the sheet declares no range) | "read_error" | "empty".
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from xml.etree import ElementTree as ET

from .base import Extractor, register

#: Uncompressed bytes of ONE worksheet part above which rows are not counted by
#: streaming. Mirrors the csv/tsv 100 MB row-count gate (CONTRACTS.md §6).
_DEFAULT_SHEET_MAX = 100 * 1024 * 1024

#: Hard cap on how many header cells are kept from one row. A worksheet may
#: legally declare 16,384 columns; a header that wide is a pivot dump, not a
#: table, and storing it would dominate the catalogue.
_MAX_HEADER_CELLS = 4096

#: Hard cap on sheets inspected per workbook (document order). Beyond this the
#: names are still listed; only the per-sheet header/row work is skipped.
_MAX_SHEETS_INSPECTED = 64

#: How many leading rows may be skipped while looking for the header. Journal
#: supplementary tables routinely open with a merged title banner ("Supplementary
#: Table 3. ...") occupying row 1 alone, which would otherwise be recorded as the
#: sheet's only column name and would bury the real ones.
_HEADER_SCAN_ROWS = 8

#: The SpreadsheetML + relationship namespaces, as they appear in every .xlsx.
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

_DIM_ROW = re.compile(r"[A-Z]*(\d+)\s*$")


def _tag(elem_tag: str) -> str:
    """Strip the ``{namespace}`` prefix ElementTree prepends to every tag."""
    return elem_tag.rsplit("}", 1)[-1]


class XlsxExtractor(Extractor):
    """Extractor for Excel workbooks (.xlsx/.xlsm), stdlib zip + XML only."""

    name = "xlsx"
    extensions = ("xlsx", "xlsm")

    def __init__(self, config: Optional[Any] = None) -> None:
        """Hold an optional config carrying the per-worksheet row-count gate."""
        self.config = config

    def _sheet_max(self) -> int:
        return int(getattr(self.config, "xlsx_rowcount_max_bytes", _DEFAULT_SHEET_MAX))

    # ------------------------------------------------------------------ #
    # entry point
    # ------------------------------------------------------------------ #
    def extract(self, path: Path) -> Dict[str, Any]:
        """Return the §4.5 workbook metadata dict; ``{}`` if it is not a zip.

        A file that is not a readable ZIP is not an error worth failing the walk
        over — Excel's own ``~$lock`` stubs share the extension and are 165-byte
        fragments — so it degrades to ``{}`` like any other unreadable file.
        """
        try:
            with zipfile.ZipFile(path) as z:
                return self._read_workbook(z)
        except (zipfile.BadZipFile, KeyError, OSError):
            return {}

    # ------------------------------------------------------------------ #
    # workbook -> sheets
    # ------------------------------------------------------------------ #
    def _read_workbook(self, z: zipfile.ZipFile) -> Dict[str, Any]:
        sheets_meta = self._sheet_directory(z)            # [(name, part_path|None)]
        if not sheets_meta:
            return {}
        names = [n for n, _ in sheets_meta]

        # Pass 1: header cells per sheet, shared-string INDICES left unresolved.
        raw: List[Tuple[str, Optional[str], List[Tuple[bool, str]], Optional[int], bool, Optional[str]]] = []
        needed: Set[int] = set()
        for name, part in sheets_meta[:_MAX_SHEETS_INSPECTED]:
            if part is None or part not in z.namelist():
                raw.append((name, part, [], None, False, "read_error"))
                continue
            cells, n_rows, exact, reason = self._read_sheet(z, part)
            for is_shared, value in cells:
                if is_shared:
                    try:
                        needed.add(int(value))
                    except ValueError:
                        pass
            raw.append((name, part, cells, n_rows, exact, reason))

        # Pass 2: resolve ONLY the shared strings the headers referenced.
        table = self._shared_strings(z, needed)

        sheets: List[Dict[str, Any]] = []
        for name, _part, cells, n_rows, exact, reason in raw:
            columns = [
                (table.get(int(v), "") if is_shared and v.lstrip("-").isdigit() else v)
                for is_shared, v in cells
            ]
            columns = [c.strip() for c in columns]
            entry: Dict[str, Any] = {
                "name": name,
                "columns": columns,
                "n_columns": len(columns),
                "row_count": n_rows,
                "row_count_exact": exact,
            }
            if reason:
                entry["row_count_reason"] = reason
            sheets.append(entry)
        # Sheets past the inspection cap are still NAMED, so search finds them.
        for name in names[_MAX_SHEETS_INSPECTED:]:
            sheets.append({"name": name, "columns": [], "n_columns": 0,
                           "row_count": None, "row_count_exact": False,
                           "row_count_reason": "sheet_cap"})

        out: Dict[str, Any] = {
            "n_sheets": len(names),
            "sheet_names": names,
            "sheets": sheets,
        }
        # A workbook has ONE shape only when it has one sheet. Mirroring the first
        # sheet of a multi-sheet workbook would have labelled the whole file with
        # whatever happened to be on tab 1 — for a paper's supplement that is a
        # one-column legend tab, so a 198 x 5 table would have read "3 x 1". Where
        # there are several sheets the per-sheet block carries the shapes instead.
        if len(sheets) == 1:
            first = sheets[0]
            out["primary_sheet"] = first["name"]
            out["columns"] = first["columns"]
            out["n_columns"] = first["n_columns"]
            out["row_count"] = first["row_count"]
            out["row_count_exact"] = first["row_count_exact"]
            if first.get("row_count_reason"):
                out["row_count_reason"] = first["row_count_reason"]
        return out

    @staticmethod
    def _sheet_directory(z: zipfile.ZipFile) -> List[Tuple[str, Optional[str]]]:
        """Return ``[(sheet name, worksheet part path)]`` in WORKBOOK order.

        The order and the name live in ``xl/workbook.xml``; the part each sheet
        maps to lives in the relationship file beside it. Resolving the
        relationship matters: ``sheet1.xml`` is NOT reliably the first sheet, so
        reading parts in filename order would mislabel every sheet in a workbook
        whose tabs were reordered.
        """
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
        except (KeyError, ET.ParseError, OSError):
            return []
        rels: Dict[str, str] = {}
        try:
            rel_root = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            for rel in rel_root:
                rid = rel.get("Id")
                target = rel.get("Target") or ""
                if not rid:
                    continue
                target = target.split("/xl/", 1)[-1].lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target.lstrip("./")
                rels[rid] = target
        except (KeyError, ET.ParseError, OSError):
            rels = {}
        out: List[Tuple[str, Optional[str]]] = []
        for elem in wb.iter():
            if _tag(elem.tag) != "sheet":
                continue
            name = elem.get("name") or ""
            rid = elem.get(f"{{{_NS_REL_DOC}}}id") or elem.get(f"{{{_NS_REL_PKG}}}id")
            part = rels.get(rid or "")
            if part is None:
                # No usable relationship: fall back to positional naming, which
                # is right for the common single-sheet, never-reordered case.
                guess = f"xl/worksheets/sheet{len(out) + 1}.xml"
                part = guess if guess in z.namelist() else None
            out.append((name, part))
        return out

    def _read_sheet(
        self, z: zipfile.ZipFile, part: str
    ) -> Tuple[List[Tuple[bool, str]], Optional[int], bool, Optional[str]]:
        """Return ``(header cells, row count, count is exact, reason)`` for one sheet.

        Header cells come back as ``(is_shared_string, raw value)`` pairs so the
        caller can batch the shared-string lookups. Rows are counted by streaming
        start-tags when the part is under the gate; above it, the sheet's own
        declared ``<dimension>`` range is reported instead and flagged inexact.
        """
        try:
            info = z.getinfo(part)
        except KeyError:
            return [], None, False, "read_error"
        gated = info.file_size > self._sheet_max()

        header: List[Tuple[bool, str]] = []
        header_candidate: Optional[List[Tuple[bool, str]]] = None
        header_row_offset = 0
        rows_scanned = 0
        seen_first_row = False
        row_total = 0
        dim_rows: Optional[int] = None

        try:
            with z.open(part) as fh:
                for event, elem in ET.iterparse(fh, events=("start", "end")):
                    name = _tag(elem.tag)
                    if event == "start":
                        if name == "dimension":
                            ref = elem.get("ref") or ""
                            end = ref.split(":")[-1]
                            m = _DIM_ROW.match(end)
                            if m:
                                dim_rows = int(m.group(1))
                        elif name == "row":
                            row_total += 1
                            if gated and seen_first_row:
                                # Nothing left to learn from this part.
                                break
                        continue
                    # end events
                    if name == "row":
                        if not seen_first_row:
                            cells = self._header_cells(elem)
                            if header_candidate is None and cells:
                                header_candidate = cells
                            # THE HEADER RULE: the first row carrying at least two
                            # non-empty cells, looked for within the first
                            # _HEADER_SCAN_ROWS rows. A merged title banner is one
                            # non-empty cell padded with blanks, so it is skipped;
                            # a genuine single-column sheet has no such row and
                            # falls back to the first non-empty row seen.
                            if self._filled(cells) >= 2:
                                header = cells
                                seen_first_row = True
                                header_row_offset = rows_scanned
                            elif rows_scanned + 1 >= _HEADER_SCAN_ROWS:
                                header = header_candidate or cells
                                seen_first_row = True
                                header_row_offset = 0
                            rows_scanned += 1
                        elem.clear()
                    elif name in ("c", "v", "is", "t") and seen_first_row:
                        elem.clear()
        except (ET.ParseError, OSError, KeyError):
            return header, None, False, "read_error"

        # A sheet SHORTER than the header scan window exits the loop before the
        # fallback fires (a 3-row, one-column legend tab), so settle it here.
        if not seen_first_row:
            header = header_candidate or []
            header_row_offset = 0

        # Data rows = every row below the header row, so any banner rows ABOVE
        # the header come off the total as well as the header row itself.
        consumed = header_row_offset + 1
        if gated:
            if dim_rows is not None:
                return header, max(dim_rows - consumed, 0), False, "size_gated"
            return header, None, False, "no_dimension"
        if row_total == 0:
            return [], 0, True, "empty"
        return header, max(row_total - consumed, 0), True, None

    @staticmethod
    def _filled(cells: List[Tuple[bool, str]]) -> int:
        """Count cells carrying any text. A shared-string cell is non-empty by
        construction (it holds a table index), so only inline/literal blanks and
        cells with no value child count as empty here."""
        return sum(1 for is_shared, v in cells if is_shared or v.strip())

    @staticmethod
    def _header_cells(row_elem: ET.Element) -> List[Tuple[bool, str]]:
        """Pull ``(is_shared_string, raw text)`` for every cell of the header row."""
        cells: List[Tuple[bool, str]] = []
        for c in row_elem:
            if _tag(c.tag) != "c":
                continue
            ctype = c.get("t")
            text = ""
            if ctype == "inlineStr":
                text = "".join(
                    (t.text or "")
                    for t in c.iter()
                    if _tag(t.tag) == "t"
                )
            else:
                for child in c:
                    if _tag(child.tag) == "v":
                        text = child.text or ""
                        break
            cells.append((ctype == "s", text))
            if len(cells) >= _MAX_HEADER_CELLS:
                break
        return cells

    @staticmethod
    def _shared_strings(z: zipfile.ZipFile, needed: Set[int]) -> Dict[int, str]:
        """Stream the workbook string table, keeping only the ``needed`` indices.

        The table is shared by every cell in the workbook and routinely runs to
        tens of MB; a header row references a few dozen of its entries. Streaming
        and discarding keeps peak memory at the header text rather than the table.
        """
        if not needed or "xl/sharedStrings.xml" not in z.namelist():
            return {}
        want_max = max(needed)
        table: Dict[int, str] = {}
        idx = 0
        try:
            with z.open("xl/sharedStrings.xml") as fh:
                for event, elem in ET.iterparse(fh, events=("end",)):
                    if _tag(elem.tag) != "si":
                        continue
                    if idx in needed:
                        table[idx] = "".join(
                            (t.text or "") for t in elem.iter() if _tag(t.tag) == "t"
                        )
                    elem.clear()
                    idx += 1
                    if idx > want_max:
                        break
        except (ET.ParseError, OSError, KeyError):
            return table
        return table


register(XlsxExtractor)
