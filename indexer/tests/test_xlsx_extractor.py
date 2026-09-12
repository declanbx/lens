"""Tests for the stdlib Excel extractor (repo_index.extractors.xlsx).

Workbooks are BUILT here with zipfile rather than openpyxl: the extractor's whole
point is that it needs no third-party spreadsheet library, so neither may its
tests. Each helper writes the minimum set of XML parts the extractor reads.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from repo_index.extractors import extract_meta, get_extractor
from repo_index.extractors.xlsx import XlsxExtractor

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
NSR = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _sheet_xml(rows, dimension=None):
    """One worksheet part. ``rows`` is a list of row cell-lists; each cell is
    ``("s", idx)`` for a shared string, ``("inline", text)``, ``("n", number)``
    or ``None`` for an empty cell."""
    out = [f"<worksheet {NS}>"]
    if dimension:
        out.append(f'<dimension ref="{dimension}"/>')
    out.append("<sheetData>")
    for ri, cells in enumerate(rows, start=1):
        out.append(f'<row r="{ri}">')
        for ci, cell in enumerate(cells):
            ref = f"{chr(ord('A') + ci)}{ri}"
            if cell is None:
                out.append(f'<c r="{ref}"/>')
            elif cell[0] == "s":
                out.append(f'<c r="{ref}" t="s"><v>{cell[1]}</v></c>')
            elif cell[0] == "inline":
                out.append(f'<c r="{ref}" t="inlineStr"><is><t>{cell[1]}</t></is></c>')
            else:
                out.append(f'<c r="{ref}"><v>{cell[1]}</v></c>')
        out.append("</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def _write_workbook(path: Path, sheets, shared=None):
    """``sheets`` = [(display name, part filename, rows, dimension)]."""
    wb = [f"<workbook {NS} {NSR}><sheets>"]
    rels = ['<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i, (name, part, _rows, _dim) in enumerate(sheets, start=1):
        wb.append(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(f'<Relationship Id="rId{i}" Target="worksheets/{part}" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/worksheet"/>')
    wb.append("</sheets></workbook>")
    rels.append("</Relationships>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", "".join(wb))
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        for _name, part, rows, dim in sheets:
            z.writestr(f"xl/worksheets/{part}", _sheet_xml(rows, dim))
        if shared:
            items = "".join(f"<si><t>{s}</t></si>" for s in shared)
            z.writestr("xl/sharedStrings.xml", f"<sst {NS}>{items}</sst>")


def test_xlsx_and_xlsm_route_to_the_xlsx_extractor():
    assert get_extractor(Path("a/b.xlsx")).name == "xlsx"
    assert get_extractor(Path("a/b.xlsm")).name == "xlsx"


def test_single_sheet_reports_names_headers_and_both_counts(tmp_path):
    shared = ["Donor ID", "Braak", "Sex"]
    rows = [[("s", 0), ("s", 1), ("s", 2)]] + [[("n", i), ("n", 3), ("inline", "F")] for i in range(5)]
    p = tmp_path / "one.xlsx"
    _write_workbook(p, [("Cohort", "sheet1.xml", rows, "A1:C6")], shared)

    name, meta, err = extract_meta(p)
    assert (name, err) == ("xlsx", None)
    assert meta["n_sheets"] == 1
    assert meta["sheet_names"] == ["Cohort"]
    assert meta["sheets"][0]["columns"] == ["Donor ID", "Braak", "Sex"]
    assert meta["sheets"][0]["n_columns"] == 3
    # 6 physical rows, 1 of them the header -> 5 data rows, counted not estimated.
    assert meta["sheets"][0]["row_count"] == 5
    assert meta["sheets"][0]["row_count_exact"] is True
    # A one-sheet workbook also reports its shape at the top level, like a csv.
    assert meta["columns"] == ["Donor ID", "Braak", "Sex"]
    assert meta["n_columns"] == 3 and meta["row_count"] == 5
    assert meta["primary_sheet"] == "Cohort"


def test_merged_title_banner_above_the_header_is_skipped(tmp_path):
    """A journal supplement opens with a merged one-cell title; the header is the
    row below it, and the banner must not be counted as a data row either."""
    shared = ["Supplementary Table 3. Donor traits", "gene", "log2FC", "padj"]
    rows = [
        [("s", 0), None, None],                       # the banner
        [("s", 1), ("s", 2), ("s", 3)],               # the real header
        [("inline", "APOE"), ("n", 1), ("n", 0)],
        [("inline", "CLU"), ("n", 2), ("n", 0)],
    ]
    p = tmp_path / "supp.xlsx"
    _write_workbook(p, [("Table S3", "sheet1.xml", rows, None)], shared)

    _n, meta, _e = extract_meta(p)
    sheet = meta["sheets"][0]
    assert sheet["columns"] == ["gene", "log2FC", "padj"]
    assert sheet["row_count"] == 2          # 4 rows - banner - header
    assert sheet["row_count_exact"] is True


def test_single_column_sheet_keeps_its_one_header(tmp_path):
    """The banner rule must not strip the header of a genuinely 1-column sheet
    that is shorter than the scan window."""
    p = tmp_path / "legend.xlsx"
    _write_workbook(
        p, [("Legend", "sheet1.xml", [[("s", 0)], [("s", 1)], [("s", 1)]], None)],
        ["Table S6. Legend", "text"],
    )
    _n, meta, _e = extract_meta(p)
    assert meta["sheets"][0]["columns"] == ["Table S6. Legend"]
    assert meta["sheets"][0]["n_columns"] == 1
    assert meta["sheets"][0]["row_count"] == 2


def test_multi_sheet_workbook_reports_no_single_shape(tmp_path):
    """Tab 1 of a supplement is often a legend. Mirroring it at the top level
    would label the whole workbook with the wrong shape, so there is none."""
    shared = ["legend", "gene", "log2FC"]
    p = tmp_path / "multi.xlsx"
    _write_workbook(p, [
        ("Legend", "sheet1.xml", [[("s", 0)], [("s", 0)]], None),
        ("Data", "sheet2.xml", [[("s", 1), ("s", 2)]] + [[("inline", "A"), ("n", 1)]] * 9, None),
    ], shared)

    _n, meta, _e = extract_meta(p)
    assert meta["n_sheets"] == 2
    assert meta["sheet_names"] == ["Legend", "Data"]
    assert "row_count" not in meta and "n_columns" not in meta and "columns" not in meta
    assert meta["sheets"][1]["columns"] == ["gene", "log2FC"]
    assert meta["sheets"][1]["row_count"] == 9


def test_sheet_order_follows_the_workbook_not_the_part_filename(tmp_path):
    """Reordering tabs in Excel leaves the part filenames alone, so reading parts
    in filename order would mislabel every sheet."""
    p = tmp_path / "reordered.xlsx"
    _write_workbook(p, [
        ("Second tab", "sheet2.xml", [[("inline", "b1"), ("inline", "b2")]], None),
        ("First tab", "sheet1.xml", [[("inline", "a1"), ("inline", "a2")]], None),
    ])
    _n, meta, _e = extract_meta(p)
    assert meta["sheet_names"] == ["Second tab", "First tab"]
    assert meta["sheets"][0]["columns"] == ["b1", "b2"]
    assert meta["sheets"][1]["columns"] == ["a1", "a2"]


def test_row_count_above_the_gate_falls_back_to_the_declared_range(tmp_path):
    """Above the size gate the sheet's own <dimension> is reported INSTEAD of a
    count, and is flagged inexact — it disagreed with the true count in 11 of 115
    real sheets, so it must never be presented as counted."""
    rows = [[("inline", "gene"), ("inline", "padj")]] + [[("inline", "X"), ("n", 1)]] * 50
    p = tmp_path / "big.xlsx"
    _write_workbook(p, [("Data", "sheet1.xml", rows, "A1:B4000")])

    ex = XlsxExtractor()
    ex.config = type("C", (), {"xlsx_rowcount_max_bytes": 10})()   # gate everything
    meta = ex.extract(p)
    sheet = meta["sheets"][0]
    assert sheet["row_count"] == 3999                  # declared range minus header
    assert sheet["row_count_exact"] is False
    assert sheet["row_count_reason"] == "size_gated"
    assert sheet["columns"] == ["gene", "padj"]        # the header is still read


def test_not_a_zip_degrades_to_empty_rather_than_erroring(tmp_path):
    """Excel's own ~$lock stubs share the extension and are not zips."""
    p = tmp_path / "~$locked.xlsx"
    p.write_bytes(b"not a zip at all")
    name, meta, err = extract_meta(p)
    assert (name, meta, err) == ("xlsx", {}, None)


def test_shared_strings_are_resolved_only_where_referenced(tmp_path):
    """The string table is workbook-wide and can run to tens of MB; only the
    indices the header rows use are materialised."""
    shared = [f"col{i}" for i in range(500)]
    p = tmp_path / "sparse.xlsx"
    _write_workbook(p, [("S", "sheet1.xml", [[("s", 7), ("s", 300)]], None)], shared)
    _n, meta, _e = extract_meta(p)
    assert meta["sheets"][0]["columns"] == ["col7", "col300"]
