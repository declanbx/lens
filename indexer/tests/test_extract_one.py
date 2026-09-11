"""Tests for repo_index.extract_one — the stateless extract-one/extract-batch
helper (PHASE_0_1_SPEC.md §6, build-plan gate 10).

Drives :func:`extract_one.run_one` / :func:`extract_one.run_batch` directly
(not via a subprocess — the helper is pure Python called in-process, exactly
as ``cli.py`` calls it) over the shared ``fixture_tree`` (csv/py/md + a
resolving, a broken, and a directory symlink — tests/conftest.py). Asserts:
same ``(extractor, meta, error)`` triple as ``extract_meta`` directly; never
raises (nonexistent path / an internal extraction blowup -> in-band error,
exit 0); honors ``--no-columns`` (drops ``columns``, keeps ``n_columns``) and
a ``--max-csv-bytes`` size gate on BOTH subcommands; batch order preserved;
``input_path`` echoed; ``n_obs``/``n_vars`` denorm matches
``export_sqlite.py:142-143``'s ``isinstance(int)`` guard exactly; symlinks
(resolving AND broken) short-circuit to generic/{}/no-error without ever
reaching ``extract_meta``. A final section smoke-tests the ``cli.py``
dispatch wiring (flag parsing, the extra-allow-list, stdin wiring).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from repo_index import cli, extract_one
from repo_index.config import load_config
from repo_index.extractors.base import extract_meta


def _cfg(**overrides):
    try:
        return load_config(None, overrides or None)
    except NotImplementedError:
        pytest.skip("config.load_config not implemented yet (stub phase)")


# --------------------------------------------------------------------------- #
# extract_record / run_one — parity with extract_meta, never-raises, symlinks
# --------------------------------------------------------------------------- #

def test_run_one_matches_extract_meta_triple(fixture_tree, capsys):
    root, _markers = fixture_tree
    cfg = _cfg()
    target = root / "tables" / "cells.csv"

    rc = extract_one.run_one(str(target), root, cfg, path_fields=False)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())

    expected_extractor, expected_meta, expected_error = extract_meta(target)
    assert rec["v"] == 1
    assert rec["input_path"] == str(target)
    assert rec["extractor"] == expected_extractor
    assert rec["meta"] == expected_meta
    assert rec["error"] == expected_error


def test_run_one_accepts_root_relative_path(fixture_tree, capsys):
    root, _markers = fixture_tree
    cfg = _cfg()
    rc = extract_one.run_one("tables/cells.csv", root, cfg, path_fields=False)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())
    assert rec["input_path"] == str(root / "tables" / "cells.csv")
    assert rec["extractor"] == "tabular"


@pytest.mark.parametrize("rel", ["links/good_link.csv", "links/broken_link.csv"])
def test_symlinks_short_circuit_record_not_traverse(fixture_tree, capsys, rel):
    """A symlink (resolving OR broken) NEVER reaches extract_meta (§6.4)."""
    root, markers = fixture_tree
    if rel == "links/good_link.csv" and not markers.get("symlink_good"):
        pytest.skip("good symlink fixture unavailable on this filesystem")
    if rel == "links/broken_link.csv" and not markers.get("symlink_broken"):
        pytest.skip("broken symlink fixture unavailable on this filesystem")
    cfg = _cfg()
    target = root / rel

    rc = extract_one.run_one(str(target), root, cfg, path_fields=False)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())
    assert rec["extractor"] == "generic"
    assert rec["meta"] == {}
    assert rec["error"] is None
    assert rec["n_obs"] is None and rec["n_vars"] is None


def test_never_raises_on_nonexistent_path(fixture_tree, capsys):
    """A nonexistent .csv never crashes the helper. The tabular extractor
    itself swallows the OSError into an in-band ``meta.row_count_reason``
    rather than raising (CONTRACTS §2: extractors return a partial dict for an
    expected failure mode), so the top-level ``error`` stays None here — the
    exception-propagation case is covered separately below (h5ad)."""
    root, _markers = fixture_tree
    cfg = _cfg()
    rc = extract_one.run_one(str(root / "tables" / "does_not_exist.csv"), root, cfg,
                              path_fields=False)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())
    assert rec["extractor"] == "tabular"
    assert rec["meta"]["row_count_reason"] == "read_error"


def test_never_raises_when_extractor_itself_raises_on_nonexistent_path(
    fixture_tree, capsys
):
    """A nonexistent .h5ad DOES propagate a real exception out of h5py, up
    through extract_meta's try/except -> an in-band ``error`` string, exit 0 —
    the genuine "never raises" case (as opposed to the tabular extractor's own
    internal swallow, tested above)."""
    root, markers = fixture_tree
    if not markers.get("h5ad"):
        pytest.skip("h5py unavailable in this environment")
    cfg = _cfg()
    rc = extract_one.run_one(str(root / "data" / "does_not_exist.h5ad"), root, cfg,
                              path_fields=False)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())
    assert rec["error"] is not None


def test_never_raises_when_extraction_blows_up(fixture_tree, capsys, monkeypatch):
    """Directly proves extract_record's own try/except net (§6.2), independent
    of any one extractor's individual robustness (covered by test_extractors.py)."""
    root, _markers = fixture_tree
    cfg = _cfg()

    def _boom(_path):
        raise ValueError("simulated extractor blowup")

    monkeypatch.setattr(extract_one, "extract_meta", _boom)
    rc = extract_one.run_one(str(root / "tables" / "cells.csv"), root, cfg,
                              path_fields=False)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())
    assert rec["extractor"] == "generic"
    assert rec["meta"] == {}
    assert rec["n_obs"] is None and rec["n_vars"] is None
    assert "ValueError" in rec["error"]


# --------------------------------------------------------------------------- #
# n_obs/n_vars denorm parity with export_sqlite.py:142-143
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "meta_n_obs,expected",
    [(12345, 12345), (12.5, None), (None, None), ("15000", None)],
)
def test_n_obs_denorm_matches_export_sqlite_isinstance_int_guard(
    fixture_tree, monkeypatch, meta_n_obs, expected
):
    root, _markers = fixture_tree
    cfg = _cfg()

    def _fake(_path):
        return "h5ad", {"n_obs": meta_n_obs, "n_vars": 99}, None

    monkeypatch.setattr(extract_one, "extract_meta", _fake)
    target = root / "tables" / "cells.csv"
    rec = extract_one.extract_record(target, root, cfg, path_fields=False)
    assert rec["n_obs"] == expected
    assert rec["n_vars"] == 99  # control: a real int always survives


# --------------------------------------------------------------------------- #
# --no-columns / --max-csv-bytes size gate — BOTH subcommands
# --------------------------------------------------------------------------- #

def test_no_columns_strips_columns_keeps_n_columns_run_one(fixture_tree, capsys):
    root, _markers = fixture_tree
    cfg = _cfg(index_columns=False)
    rc = extract_one.run_one(str(root / "tables" / "cells.csv"), root, cfg,
                              path_fields=False)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())
    assert "columns" not in rec["meta"]
    assert "n_columns" in rec["meta"] and rec["meta"]["n_columns"] > 0


def test_no_columns_strips_columns_keeps_n_columns_run_batch(fixture_tree):
    root, _markers = fixture_tree
    cfg = _cfg(index_columns=False)
    stdin = io.StringIO(json.dumps({"path": "tables/cells.csv"}) + "\n")
    stdout = io.StringIO()
    rc = extract_one.run_batch(root, cfg, path_fields=False, stdin=stdin, stdout=stdout)
    assert rc == 0
    rec = json.loads(stdout.getvalue().strip())
    assert "columns" not in rec["meta"]
    assert "n_columns" in rec["meta"]


def test_max_csv_bytes_size_gates_run_one(fixture_tree, capsys):
    root, _markers = fixture_tree
    cfg = _cfg(csv_rowcount_max_bytes=1)
    rc = extract_one.run_one(str(root / "tables" / "cells.csv"), root, cfg,
                              path_fields=False)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())
    assert rec["meta"]["row_count"] is None
    assert rec["meta"]["row_count_reason"] == "size_gated"
    # the header is a separate, always-cheap read: columns survive the gate.
    assert "columns" in rec["meta"]


def test_max_csv_bytes_size_gates_run_batch(fixture_tree):
    root, _markers = fixture_tree
    cfg = _cfg(csv_rowcount_max_bytes=1)
    stdin = io.StringIO(json.dumps({"path": "tables/cells.csv"}) + "\n")
    stdout = io.StringIO()
    rc = extract_one.run_batch(root, cfg, path_fields=False, stdin=stdin, stdout=stdout)
    assert rc == 0
    rec = json.loads(stdout.getvalue().strip())
    assert rec["meta"]["row_count_reason"] == "size_gated"


# --------------------------------------------------------------------------- #
# extract-batch: order preservation, blank-line skip, malformed-line defense
# --------------------------------------------------------------------------- #

def test_run_batch_preserves_input_order_and_echoes_input_path(fixture_tree):
    root, _markers = fixture_tree
    cfg = _cfg()
    rels = ["docs/README.md", "code/module.py", "tables/cells.csv"]  # deliberately unsorted
    stdin = io.StringIO("".join(json.dumps({"path": r}) + "\n" for r in rels))
    stdout = io.StringIO()

    rc = extract_one.run_batch(root, cfg, path_fields=False, stdin=stdin, stdout=stdout)
    assert rc == 0
    lines = stdout.getvalue().strip("\n").split("\n")
    assert len(lines) == 3
    got_paths = [json.loads(ln)["input_path"] for ln in lines]
    assert got_paths == [str(root / r) for r in rels]


def test_run_batch_skips_blank_lines_and_defends_malformed_lines(fixture_tree):
    root, _markers = fixture_tree
    cfg = _cfg()
    stdin = io.StringIO(
        "\n"
        + json.dumps({"path": "tables/cells.csv"}) + "\n"
        + "   \n"
        + "not-json-at-all{{{\n"
        + json.dumps({"path": "code/module.py"}) + "\n"
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    rc = extract_one.run_batch(root, cfg, path_fields=False, stdin=stdin,
                                stdout=stdout, stderr=stderr)
    assert rc == 0
    lines = [json.loads(ln) for ln in stdout.getvalue().strip("\n").split("\n")]
    # blank/whitespace-only lines produce NO response; the malformed line
    # produces exactly one in-band-error response (never silently dropped).
    assert len(lines) == 3
    assert lines[0]["input_path"] == str(root / "tables" / "cells.csv")
    assert lines[1]["error"] is not None
    assert lines[1]["extractor"] == "generic"
    assert lines[2]["input_path"] == str(root / "code" / "module.py")
    assert "malformed" in stderr.getvalue()


@pytest.mark.parametrize("bad_path", [123, ["a"], {"nested": True}, 1.5, False])
def test_run_batch_never_crashes_on_non_string_path_value(fixture_tree, bad_path):
    """Regression: a well-formed JSON object whose "path" value is NOT a string
    (int/list/dict/float/bool) must NOT reach `_resolve_path` -> `Path(non_str)`,
    which raises TypeError OUTSIDE any per-line guard and previously aborted the
    WHOLE batch with a non-zero exit — violating "exit 0 whenever the process
    ran; per-file failure is in-band" (§6.2). Confirmed by adversarial review."""
    root, _markers = fixture_tree
    cfg = _cfg()
    rels = ["docs/README.md", "code/module.py"]
    stdin = io.StringIO(
        json.dumps({"path": rels[0]}) + "\n"
        + json.dumps({"path": bad_path}) + "\n"
        + json.dumps({"path": rels[1]}) + "\n"
    )
    stdout = io.StringIO()

    rc = extract_one.run_batch(root, cfg, path_fields=False, stdin=stdin, stdout=stdout)

    assert rc == 0  # the whole batch must survive
    lines = [json.loads(ln) for ln in stdout.getvalue().strip("\n").split("\n")]
    assert len(lines) == 3  # order/accounting preserved: one response per request
    assert lines[0]["input_path"] == str(root / rels[0])
    assert lines[0]["error"] is None
    # the bad request: exactly one in-band-error response, never dropped.
    assert lines[1]["error"] is not None
    assert lines[1]["extractor"] == "generic"
    assert lines[1]["meta"] == {}
    assert lines[2]["input_path"] == str(root / rels[1])
    assert lines[2]["error"] is None


# --------------------------------------------------------------------------- #
# --path-fields
# --------------------------------------------------------------------------- #

def test_path_fields_emits_ext_category_tags(fixture_tree, capsys):
    root, _markers = fixture_tree
    cfg = _cfg()
    rc = extract_one.run_one(str(root / "tables" / "cells.csv"), root, cfg,
                              path_fields=True)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())
    assert rec["ext"] == "csv"
    assert rec["category"] == cfg.category_for("csv")
    assert isinstance(rec["tags"], list)


def test_without_path_fields_omits_ext_category_tags(fixture_tree, capsys):
    root, _markers = fixture_tree
    cfg = _cfg()
    rc = extract_one.run_one(str(root / "tables" / "cells.csv"), root, cfg,
                              path_fields=False)
    assert rc == 0
    rec = json.loads(capsys.readouterr().out.strip())
    assert "ext" not in rec and "category" not in rec and "tags" not in rec


# --------------------------------------------------------------------------- #
# CLI dispatch wiring (cli.py: extract-one / extract-batch)
# --------------------------------------------------------------------------- #

def test_cli_extract_one_dispatch(fixture_tree, capsys):
    root, _markers = fixture_tree
    rc = cli.main([
        "extract-one", "--root", str(root), str(root / "tables" / "cells.csv"),
    ])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    rec = json.loads(out)
    assert rec["v"] == 1
    assert rec["extractor"] == "tabular"


def test_cli_extract_one_missing_path_arg_fails_to_start(fixture_tree, capsys):
    root, _markers = fixture_tree
    rc = cli.main(["extract-one", "--root", str(root)])
    assert rc == 2


def test_cli_extract_batch_dispatch_reads_stdin(fixture_tree, capsys, monkeypatch):
    root, _markers = fixture_tree
    ndjson = "".join(
        json.dumps({"path": r}) + "\n"
        for r in ["tables/cells.csv", "docs/README.md"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(ndjson))
    rc = cli.main(["extract-batch", "--root", str(root), "--no-columns"])
    assert rc == 0
    out = capsys.readouterr().out.strip("\n").split("\n")
    assert len(out) == 2
    recs = [json.loads(ln) for ln in out]
    assert recs[0]["input_path"] == str(root / "tables" / "cells.csv")
    assert recs[1]["input_path"] == str(root / "docs" / "README.md")
    assert "columns" not in recs[0]["meta"]  # --no-columns reached the helper
