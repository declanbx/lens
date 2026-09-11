"""repo_index.extractors.tabular — tabular file metadata.

.csv/.tsv/.csv.gz/.tsv.gz: header columns ALWAYS read cheaply (first line; gz via
streaming gzip). Exact row count only when size-gated (CONTRACTS.md §6). .parquet:
pyarrow FOOTER metadata only (no row-group data). See CONTRACTS.md §4.4.

extract() return-dict keys
--------------------------
    csv/tsv/csv.gz/tsv.gz ->
        { "columns": list[str], "n_columns": int, "delimiter": str,
          "row_count": int|None, "row_count_exact": bool,
          "row_count_reason": str }   # reason present only when null/inexact:
                                       #   "size_gated" | "read_error" | "empty"
    parquet ->
        { "columns": [ {"name": str, "type": str}, ... ], "n_columns": int,
          "num_rows": int|None, "num_row_groups": int|None }

Thresholds come from config (raw csv 100MB / compressed gz 25MB / parquet always
footer-only). pyarrow absent -> parquet extract() returns {}.
"""

from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Extractor, register

try:  # optional enhancer (parquet only)
    import pyarrow.parquet as _pq  # type: ignore
except Exception:  # noqa: BLE001
    _pq = None  # type: ignore[assignment]

# §6 defaults, used when no config is attached to the singleton.
_DEFAULT_CSV_MAX = 100 * 1024 * 1024      # 100 MB raw csv/tsv
_DEFAULT_CSVGZ_MAX = 25 * 1024 * 1024     # 25 MB compressed csv.gz/tsv.gz

# Hard cap on how many bytes of the FIRST line we ever pull into memory. A
# pathological single-line wide matrix (or a binary file mis-extensioned as .csv)
# with no early newline must not force an unbounded readline() allocation, so we
# read at most this prefix and take the header up to the first newline within it.
_HEADER_READ_CAP = 1 << 20  # 1 MB


class TabularExtractor(Extractor):
    """Extractor for delimited text tables and parquet (cheap header/footer)."""

    name = "tabular"
    extensions = ("csv", "tsv", "csv.gz", "tsv.gz", "parquet")

    def __init__(self, config: Optional[Any] = None) -> None:
        """Hold an optional config carrying the row-count size thresholds.

        ``config`` may be None at registration time (singleton); the walker/CLI
        sets thresholds via config when invoking. Implementations read
        ``config.csv_rowcount_max_bytes`` etc., falling back to the §6 defaults.
        """
        self.config = config

    # ------------------------------------------------------------------ #
    # threshold helpers (defensive: config may be None or partial)
    # ------------------------------------------------------------------ #
    def _csv_max(self) -> int:
        return int(getattr(self.config, "csv_rowcount_max_bytes", _DEFAULT_CSV_MAX))

    def _csvgz_max(self) -> int:
        return int(getattr(self.config, "csvgz_rowcount_max_bytes", _DEFAULT_CSVGZ_MAX))

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def extract(self, path: Path) -> Dict[str, Any]:
        """Return the §4.4 tabular metadata dict.

        Dispatches on extension: delimited (csv/tsv/+.gz) -> header + size-gated
        exact row count; parquet -> pyarrow footer metadata (or {} if pyarrow
        absent).
        """
        lname = path.name.lower()
        if lname.endswith(".parquet"):
            return self._extract_parquet(path)
        if lname.endswith(".csv.gz") or lname.endswith(".tsv.gz"):
            return self._extract_delimited(path, gz=True)
        if lname.endswith(".csv") or lname.endswith(".tsv"):
            return self._extract_delimited(path, gz=False)
        # Should not be reached given the registered extensions, but stay safe.
        return {}

    # ------------------------------------------------------------------ #
    # delimited text
    # ------------------------------------------------------------------ #
    def _extract_delimited(self, path: Path, *, gz: bool) -> Dict[str, Any]:
        # Read the first (header) line cheaply and sniff the delimiter.
        header_line = self._read_header_line(path, gz=gz)
        if header_line is None:
            # Could not read even the header (corrupt/locked/empty).
            return {
                "columns": [],
                "n_columns": 0,
                "delimiter": self._delim_by_ext(path),
                "row_count": None,
                "row_count_exact": False,
                "row_count_reason": "read_error",
            }
        if header_line == "":
            # File exists but is empty (no first line).
            return {
                "columns": [],
                "n_columns": 0,
                "delimiter": self._delim_by_ext(path),
                "row_count": 0,
                "row_count_exact": True,
                "row_count_reason": "empty",
            }

        delimiter = self._sniff_delimiter(header_line, path)
        columns = self._parse_header(header_line, delimiter)
        result: Dict[str, Any] = {
            "columns": columns,
            "n_columns": len(columns),
            "delimiter": delimiter,
        }

        # Size gate for exact row count.
        try:
            size = path.stat().st_size  # follows symlink to the real target by design
        except OSError:
            result.update(
                row_count=None, row_count_exact=False, row_count_reason="read_error"
            )
            return result

        limit = self._csvgz_max() if gz else self._csv_max()
        if size > limit:
            result.update(
                row_count=None,
                row_count_exact=False,
                row_count_reason="size_gated",
            )
            return result

        # Within the gate: stream-count data records via csv.reader (respects
        # quoted embedded newlines), so a free-text annotation column with an
        # embedded newline does not inflate the count past the true record count.
        row_count = self._count_rows(path, delimiter, gz=gz)
        if row_count is None:
            result.update(
                row_count=None,
                row_count_exact=False,
                row_count_reason="read_error",
            )
            return result
        result["row_count"] = row_count
        result["row_count_exact"] = True
        return result

    @staticmethod
    def _delim_by_ext(path: Path) -> str:
        lname = path.name.lower()
        if lname.endswith(".tsv") or lname.endswith(".tsv.gz"):
            return "\t"
        return ","

    def _sniff_delimiter(self, header_line: str, path: Path) -> str:
        """Sniff the delimiter from the header line; fall back to by-extension.

        TSV files default to tab and CSV to comma; csv.Sniffer refines when the
        header is ambiguous but never overrides a clearly-extension-typed file in
        a way that contradicts the contract ("," or "\\t").
        """
        ext_delim = self._delim_by_ext(path)
        try:
            dialect = csv.Sniffer().sniff(header_line, delimiters=",\t")
            sniffed = dialect.delimiter
            if sniffed in (",", "\t"):
                return sniffed
        except Exception:  # noqa: BLE001 - ambiguous/short header: use extension
            pass
        return ext_delim

    @staticmethod
    def _parse_header(header_line: str, delimiter: str) -> List[str]:
        try:
            reader = csv.reader([header_line], delimiter=delimiter)
            for row in reader:
                return [c.strip() for c in row]
        except Exception:  # noqa: BLE001 - quoting edge case: naive split
            return [c.strip() for c in header_line.split(delimiter)]
        return []

    @staticmethod
    def _read_header_line(path: Path, *, gz: bool) -> Optional[str]:
        """Read just the first line, BOUNDED. Returns None on error, "" if empty.

        Reads at most ``_HEADER_READ_CAP`` bytes (never an unbounded
        ``readline()``) and takes the header as the text up to the first newline
        within that prefix. If no newline is found within the cap, the file has a
        pathologically long first "line" (a single-row wide matrix exported as CSV
        with no early newline, or a corrupt/binary file mis-typed as .csv): we
        parse only the capped prefix as the header rather than materializing the
        whole line. Constant peak memory regardless of file size.
        """
        try:
            if gz:
                with gzip.open(
                    path, "rt", encoding="utf-8", errors="replace", newline=""
                ) as fh:
                    chunk = fh.read(_HEADER_READ_CAP)
            else:
                with open(
                    path, "rt", encoding="utf-8", errors="replace", newline=""
                ) as fh:
                    chunk = fh.read(_HEADER_READ_CAP)
        except OSError:
            return None
        except Exception:  # noqa: BLE001 - e.g. truncated gzip
            return None
        if chunk == "":
            return ""
        nl = chunk.find("\n")
        line = chunk if nl == -1 else chunk[:nl]
        return line.rstrip("\r\n")

    @staticmethod
    def _count_rows(path: Path, delimiter: str, *, gz: bool) -> Optional[int]:
        """Stream-count DATA records (total CSV records minus the header).

        Uses ``csv.reader`` so a quoted field containing an embedded newline is
        treated as ONE record rather than several physical lines — the previous
        ``for _ in fh`` newline count over-reported in that case while still
        claiming ``row_count_exact=True``. ``csv.reader`` streams its underlying
        iterator one (possibly multi-physical-line) record at a time, so peak
        memory stays bounded by the widest single record, not the file size.
        Returns None on a read error mid-stream.
        """
        try:
            opener = (
                (lambda: gzip.open(path, "rt", encoding="utf-8", errors="replace", newline=""))
                if gz
                else (lambda: open(path, "rt", encoding="utf-8", errors="replace", newline=""))
            )
            total = 0
            with opener() as fh:
                reader = csv.reader(fh, delimiter=delimiter)
                for _ in reader:
                    total += 1
        except OSError:
            return None
        except Exception:  # noqa: BLE001 - truncated/corrupt stream or csv field-size limit
            return None
        # Subtract the header record; an empty file would have total==0.
        return max(total - 1, 0)

    # ------------------------------------------------------------------ #
    # parquet (footer metadata only)
    # ------------------------------------------------------------------ #
    def _extract_parquet(self, path: Path) -> Dict[str, Any]:
        if _pq is None:
            return {}
        # ParquetFile reads only the footer metadata, not row-group data.
        pf = _pq.ParquetFile(str(path))
        meta = pf.metadata
        schema = pf.schema_arrow  # arrow schema -> name + type strings
        columns: List[Dict[str, str]] = []
        try:
            for field in schema:
                columns.append({"name": str(field.name), "type": str(field.type)})
        except Exception:  # noqa: BLE001 - fall back to the parquet schema view
            columns = []
            try:
                pq_schema = meta.schema
                for i in range(pq_schema.names.__len__() if hasattr(pq_schema, "names") else len(pq_schema)):
                    col = pq_schema.column(i)
                    columns.append({"name": str(col.name), "type": str(col.physical_type)})
            except Exception:  # noqa: BLE001
                columns = []
        num_rows = None
        num_row_groups = None
        try:
            num_rows = int(meta.num_rows)
        except Exception:  # noqa: BLE001
            num_rows = None
        try:
            num_row_groups = int(meta.num_row_groups)
        except Exception:  # noqa: BLE001
            num_row_groups = None
        return {
            "columns": columns,
            "n_columns": len(columns),
            "num_rows": num_rows,
            "num_row_groups": num_row_groups,
        }


register(TabularExtractor)
