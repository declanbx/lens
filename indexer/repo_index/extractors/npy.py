"""repo_index.extractors.npy — .npy / .npz header-only metadata (STUB).

.npy: parse the .npy header ONLY (do not load the array). .npz: open as a zip
and read each member's .npy header via zipfile — NO data decompression. Both
paths are stdlib-only (manual .npy header parse), so they work WITHOUT numpy.
See CONTRACTS.md §4.3.

extract() return-dict keys
--------------------------
    .npy -> { "shape": list[int], "dtype": str, "fortran_order": bool }
    .npz -> { "n_members": int,
              "members": { name: { "shape": list[int], "dtype": str } } }

If a member header is unparseable, OMIT that member (do not raise).
"""

from __future__ import annotations

import ast
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from .base import Extractor, register

# numpy is OPTIONAL: only used as a nicety if present. The stdlib parser below is
# the authoritative path and works WITHOUT numpy. We never import numpy at module
# load in a way that could raise; the guarded import keeps the bare-stdlib core.
try:  # optional enhancer
    import numpy  # type: ignore
    from numpy.lib import format as _np_format  # type: ignore
except Exception:  # noqa: BLE001
    numpy = None  # type: ignore[assignment]
    _np_format = None  # type: ignore[assignment]

_NPY_MAGIC = b"\x93NUMPY"


def _normalize_header_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a parsed .npy header dict into the §4.3 return shape.

    Header dict keys: ``descr`` (dtype string), ``fortran_order`` (bool),
    ``shape`` (tuple of ints). Returns the contract dict with a list shape and a
    plain-str dtype.
    """
    shape = d.get("shape", ())
    return {
        "shape": [int(x) for x in shape],
        "dtype": str(d.get("descr", "")),
        "fortran_order": bool(d.get("fortran_order", False)),
    }


def _parse_npy_header(fobj) -> Dict[str, Any]:  # noqa: ANN001
    """Parse a .npy magic+header from an open binary stream positioned at byte 0.

    Returns {"shape": [...], "dtype": str, "fortran_order": bool}. Stdlib-only:
    reads the magic string, version, header length, and the literal header dict
    (a Python literal) WITHOUT importing numpy. Raises on a malformed header (the
    caller / extract_meta turns that into an error or omits the member).
    """
    magic = fobj.read(6)
    if magic != _NPY_MAGIC:
        raise ValueError("not a .npy stream (bad magic)")
    version = fobj.read(2)
    if len(version) != 2:
        raise ValueError("truncated .npy version")
    major = version[0]
    # v1.0: 2-byte uint16 LE header length; v2.0+: 4-byte uint32 LE.
    if major == 1:
        raw_len = fobj.read(2)
        if len(raw_len) != 2:
            raise ValueError("truncated .npy header length")
        header_len = int.from_bytes(raw_len, "little")
    else:
        raw_len = fobj.read(4)
        if len(raw_len) != 4:
            raise ValueError("truncated .npy header length")
        header_len = int.from_bytes(raw_len, "little")
    header_bytes = fobj.read(header_len)
    if len(header_bytes) != header_len:
        raise ValueError("truncated .npy header")
    header_str = header_bytes.decode("latin1").strip()
    # The header is a Python dict literal, e.g.
    #   {'descr': '<f4', 'fortran_order': False, 'shape': (100, 50), }
    header_dict = ast.literal_eval(header_str)
    if not isinstance(header_dict, dict):
        raise ValueError("npy header is not a dict")
    return _normalize_header_dict(header_dict)


class NpyExtractor(Extractor):
    """Extractor for NumPy ``.npy`` and ``.npz`` files (header-only, stdlib)."""

    name = "npy"
    extensions = ("npy", "npz")

    def extract(self, path: Path) -> Dict[str, Any]:
        """Return the §4.3 npy/npz metadata dict.

        For ``.npy`` parse the single header; for ``.npz`` open the zip and parse
        each member's .npy header (no decompression of data). Stdlib-only.
        """
        # .npz is itself a zip archive, so detect by extension on the basename.
        if path.name.lower().endswith(".npz"):
            return self._extract_npz(path)
        return self._extract_npy(path)

    @staticmethod
    def _extract_npy(path: Path) -> Dict[str, Any]:
        """Parse the single .npy header at the front of the file (no array read)."""
        if _np_format is not None:
            # numpy nicety: use the library's own header reader when available.
            try:
                with open(path, "rb") as fobj:
                    _np_format.read_magic(fobj)
                    shape, fortran_order, dtype = _np_format.read_array_header_1_0(
                        fobj
                    )
                return {
                    "shape": [int(x) for x in shape],
                    "dtype": str(dtype),
                    "fortran_order": bool(fortran_order),
                }
            except ValueError:
                # 1.0 reader rejects 2.0 headers; fall through to stdlib parser
                # which handles both versions.
                pass
        with open(path, "rb") as fobj:
            return _parse_npy_header(fobj)

    @staticmethod
    def _extract_npz(path: Path) -> Dict[str, Any]:
        """List .npz members and parse each member's .npy header (no data read).

        Opens the archive with ``zipfile`` and, for each member, reads ONLY the
        leading header bytes from the member stream. ``ZipFile.open`` returns an
        incremental (decompressing-on-read) stream, so reading the small header
        prefix never decompresses the full data payload.
        """
        members: Dict[str, Dict[str, Any]] = {}
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                member_name = info.filename
                # numpy stores arrays as "<name>.npy" inside the zip.
                key = member_name
                if key.lower().endswith(".npy"):
                    key = key[: -len(".npy")]
                try:
                    with zf.open(info, "r") as fobj:
                        hdr = _parse_npy_header(fobj)
                except Exception:  # noqa: BLE001 - unparseable member: omit it
                    continue
                members[key] = {"shape": hdr["shape"], "dtype": hdr["dtype"]}
        return {"n_members": len(members), "members": members}


register(NpyExtractor)
