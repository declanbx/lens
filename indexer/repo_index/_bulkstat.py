"""repo_index._bulkstat — Darwin-only batched directory listing via getattrlistbulk(2).

W4 (Axis A). The W1 ``os.scandir`` walk is correct but, on exFAT (this volume's
filesystem, via fskit), each ``DirEntry`` carries ``d_type == DT_UNKNOWN``, so
every ``is_symlink()`` / ``is_dir(follow_symlinks=False)`` / ``stat()`` forces a
per-entry ``lstat`` syscall — ~36.5k explicit ``lstat`` over the real tree. This
module collapses that per-entry stat storm into ONE ``getattrlistbulk(2)`` syscall
batch per directory, returning for ALL children at once: name + object type
(VREG/VDIR/VLNK) + modification time + (for files) data length.

Pure stdlib: ``ctypes`` against ``libSystem`` (``CDLL(find_library("System"))``,
``use_errno=True``) — no third-party dependency, no build step. The whole module
is import-guarded behind :data:`SUPPORTED` (only true on Darwin where the symbol
resolves); the caller (``walker._descend``) MUST fall back to ``os.scandir`` on
``not SUPPORTED``, on any :class:`OSError`, or on a per-record decode anomaly, so
behaviour is never worse than W1.

Identity / freshness key contract (CONTRACTS.md §9, and a hard W4 constraint):
exFAT returns a GARBAGE inode/FILEID here (a 2^64-ish value was observed by the
lead). This module therefore NEVER requests, parses, reads, or exposes
``ATTR_CMN_FILEID`` / any inode. The only freshness signals are ``st_mtime`` and
``st_size``, exactly like the rest of repo_index — the ``_StatLike`` shim carries
those two fields and nothing else (no ``st_ino``).

Buffer layout (verified this session against this exFAT volume, Python 3.11):
each record is ``[u32 reclen][attribute_set_t returned (5 x u32)][packed attrs in
ascending attribute-bit order]``. With ATTR_CMN_RETURNED_ATTRS|NAME|OBJTYPE|MODTIME
+ ATTR_FILE_DATALENGTH the packed order is: NAME (``attrreference_t`` =
``{int32 dataoffset; uint32 length}``, dataoffset relative to the start of the
attrreference field itself; the bytes are the NUL-terminated name) → OBJTYPE
(``fsobj_type_t`` as u32: VREG=1, VDIR=2, VLNK=5) → MODTIME (``struct timespec`` =
``{long tv_sec; long tv_nsec}`` = 16 bytes on 64-bit Darwin) → DATALENGTH
(``off_t`` = int64, present only for VREG). Attributes are packed back-to-back
with no extra inter-attribute alignment padding (confirmed: DATALENGTH and the
NAME offset both decode correctly with zero padding inserted).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
from typing import Iterator, Tuple

# --------------------------------------------------------------------------- #
# Capability guard: Darwin + a resolvable getattrlistbulk symbol.
# --------------------------------------------------------------------------- #

SUPPORTED = False
_getattrlistbulk = None  # type: ignore[assignment]

if sys.platform == "darwin":
    try:  # pragma: no cover - exercised on Darwin only
        _lib_name = ctypes.util.find_library("System")
        _libc = ctypes.CDLL(_lib_name, use_errno=True) if _lib_name else None
        if _libc is not None and hasattr(_libc, "getattrlistbulk"):
            _getattrlistbulk = _libc.getattrlistbulk
            _getattrlistbulk.restype = ctypes.c_int
            # int getattrlistbulk(int fd, struct attrlist *al, void *attrBuf,
            #                     size_t attrBufSize, uint64_t options);
            _getattrlistbulk.argtypes = [
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_ulong,
            ]
            SUPPORTED = True
    except Exception:  # noqa: BLE001 - any resolution failure => fall back
        SUPPORTED = False
        _getattrlistbulk = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Constants (from <sys/attr.h> / <sys/vnode.h>).
# --------------------------------------------------------------------------- #

_ATTR_BIT_MAP_COUNT = 5

# commonattr bits
_ATTR_CMN_RETURNED_ATTRS = 0x80000000
_ATTR_CMN_NAME = 0x00000001
_ATTR_CMN_OBJTYPE = 0x00000008
_ATTR_CMN_MODTIME = 0x00000400
# fileattr bits
_ATTR_FILE_DATALENGTH = 0x00000200
# options
_FSOPT_PACK_INVAL_ATTRS = 0x00000008

# fsobj_type_t values (sys/vnode.h)
_VREG = 1
_VDIR = 2
_VLNK = 5

# Buffer big enough that almost every real directory fits in one syscall, but the
# loop re-calls getattrlistbulk until it returns 0 regardless, so this is only a
# throughput knob, never a correctness one.
_BUFSIZE = 1 << 18  # 256 KiB


class _attrlist(ctypes.Structure):
    """``struct attrlist`` (sys/attr.h): u16 count, u16 reserved, 5 x u32 maps."""

    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort),
        ("commonattr", ctypes.c_uint),
        ("volattr", ctypes.c_uint),
        ("dirattr", ctypes.c_uint),
        ("fileattr", ctypes.c_uint),
        ("forkattr", ctypes.c_uint),
    ]


class _StatLike:
    """Tiny ``os.stat_result`` stand-in carrying ONLY ``st_mtime`` + ``st_size``.

    ``walker.make_entry`` reads only ``st.st_mtime`` and ``st.st_size`` from the
    stat it is handed, so this two-field ``__slots__`` shim is a complete,
    drop-in substitute for the bulk (non-symlink) path. It deliberately has NO
    ``st_ino`` / inode field: exFAT's FILEID is garbage here and inode must never
    enter identity or the freshness gate (CONTRACTS.md §9).
    """

    __slots__ = ("st_mtime", "st_size")

    def __init__(self, st_mtime: float, st_size: int) -> None:
        self.st_mtime = st_mtime
        self.st_size = st_size


def _classify(objtype: int) -> str:
    """Map an ``fsobj_type_t`` to {dir, symlink, file, other}."""
    if objtype == _VDIR:
        return "dir"
    if objtype == _VLNK:
        return "symlink"
    if objtype == _VREG:
        return "file"
    return "other"


def listdir_bulk(path: str) -> Iterator[Tuple[str, str, _StatLike]]:
    """Yield ``(name, kind, st_like)`` for every child of directory ``path``.

    ``kind`` is one of ``{"dir", "symlink", "file", "other"}`` (from
    ``ATTR_CMN_OBJTYPE``). ``st_like`` carries ``st_mtime`` (from
    ``ATTR_CMN_MODTIME``) and ``st_size`` (from ``ATTR_FILE_DATALENGTH`` for a
    regular file; ``0`` for dirs / others / symlinks — the caller re-stats
    symlinks via ``os.lstat`` so their size/target/resolve-status stay exactly
    W1-correct). ``name`` is decoded UTF-8 (``surrogateescape`` so an
    undecodable byte never raises).

    Implementation: open ``path`` ``O_RDONLY`` and loop ``getattrlistbulk(2)``
    until it returns 0, parsing the packed attribute buffer per record. Raises
    :class:`OSError` on the ``open`` failing or on a negative ``getattrlistbulk``
    return (e.g. ``ENOTSUP``/``EINVAL`` on a filesystem that does not implement
    it), and :class:`ValueError` on a per-record decode anomaly — the caller maps
    BOTH to the ``os.scandir`` fallback. NEVER requests/reads the inode/FILEID.
    """
    if not SUPPORTED or _getattrlistbulk is None:  # pragma: no cover
        raise OSError("getattrlistbulk not available on this platform")

    al = _attrlist()
    al.bitmapcount = _ATTR_BIT_MAP_COUNT
    al.commonattr = (
        _ATTR_CMN_RETURNED_ATTRS
        | _ATTR_CMN_NAME
        | _ATTR_CMN_OBJTYPE
        | _ATTR_CMN_MODTIME
    )
    al.volattr = 0
    al.dirattr = 0
    al.fileattr = _ATTR_FILE_DATALENGTH
    al.forkattr = 0

    fd = os.open(path, os.O_RDONLY)
    buf = ctypes.create_string_buffer(_BUFSIZE)
    try:
        while True:
            ctypes.set_errno(0)
            n = _getattrlistbulk(
                fd, ctypes.byref(al), buf, _BUFSIZE, _FSOPT_PACK_INVAL_ATTRS
            )
            if n < 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), path)
            if n == 0:
                break
            # buf is reused each batch; snapshot to bytes so struct.unpack_from
            # reads a stable, bounds-checked buffer (no raw pointer arithmetic).
            raw = buf.raw
            off = 0
            for _ in range(n):
                name, kind, st_like, reclen = _parse_record(raw, off)
                off += reclen
                yield name, kind, st_like
    finally:
        os.close(fd)


def _parse_record(raw: bytes, off: int) -> Tuple[str, str, _StatLike, int]:
    """Parse one packed getattrlistbulk record at ``raw[off:]``.

    Returns ``(name, kind, st_like, reclen)``. Raises :class:`ValueError` on any
    structural anomaly (zero/oversized record length, name slice out of bounds,
    a returned-attrs mask missing NAME or OBJTYPE) so a single malformed record
    degrades the WHOLE directory to the os.scandir fallback rather than emitting
    a corrupt entry. NEVER reads the inode/FILEID.
    """
    # [u32 reclen]
    (reclen,) = struct.unpack_from("<I", raw, off)
    if reclen <= 0 or off + reclen > len(raw):
        raise ValueError("bulk record length out of bounds: %r" % (reclen,))

    p = off + 4
    # attribute_set_t returned_attrs = 5 x u32 {common, vol, dir, file, fork}
    returned = struct.unpack_from("<5I", raw, p)
    p += 20
    ret_common = returned[0]
    ret_file = returned[3]

    # NAME (attrreference_t {int32 dataoffset; uint32 length}); dataoffset is
    # relative to the START of this attrreference field.
    if not (ret_common & _ATTR_CMN_NAME):
        raise ValueError("bulk record missing NAME attr")
    ref_at = p
    name_off, name_len = struct.unpack_from("<iI", raw, ref_at)
    p += 8
    name_start = ref_at + name_off
    name_end = name_start + name_len
    if name_start < off or name_end > off + reclen or name_len <= 0:
        raise ValueError("bulk NAME slice out of bounds")
    name_bytes = raw[name_start:name_end].split(b"\x00", 1)[0]
    name = name_bytes.decode("utf-8", "surrogateescape")

    # OBJTYPE (fsobj_type_t as u32).
    if not (ret_common & _ATTR_CMN_OBJTYPE):
        raise ValueError("bulk record missing OBJTYPE attr")
    (objtype,) = struct.unpack_from("<I", raw, p)
    p += 4
    kind = _classify(objtype)

    # MODTIME (struct timespec {long tv_sec; long tv_nsec} = 16 bytes on 64-bit).
    st_mtime = 0.0
    if ret_common & _ATTR_CMN_MODTIME:
        tv_sec, tv_nsec = struct.unpack_from("<qq", raw, p)
        p += 16
        st_mtime = tv_sec + tv_nsec / 1e9

    # DATALENGTH (off_t = int64) — present only for a regular file. For dirs /
    # symlinks / others it is invalidated (PACK_INVAL_ATTRS still reserves the
    # slot only when the bit is set in returned_attrs); size stays 0 and the
    # caller re-stats symlinks via os.lstat.
    st_size = 0
    if ret_file & _ATTR_FILE_DATALENGTH:
        (st_size,) = struct.unpack_from("<q", raw, p)
        p += 8

    return name, kind, _StatLike(st_mtime, int(st_size)), reclen
