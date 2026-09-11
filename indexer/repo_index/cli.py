"""repo_index.cli — argparse front-end + orchestration.

``main(argv)`` parses ``--root`` / ``--out`` / ``--config`` plus the build/query/
check surface, then orchestrates the pipeline:

    load_config -> walker.walk -> manifest.build_manifest -> manifest.write_outputs
    -> agent_map.write_agent_map -> render_html.write_html
    -> crosslinks.build_graph + write_crosslinks

``query`` delegates to :func:`repo_index.query.main`; ``check`` / ``--check``
delegates to :func:`repo_index.selfheal.check` (exit 1 on drift). Returns an int
exit code; ``__main__.py`` does ``sys.exit(main(...))``.

Space-safe (pathlib everywhere; the only subprocess is ``git`` inside
manifest.py, args as a list). Stdlib-only. See CONTRACTS.md §8/§11.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__

# The query.py verb names. A post-`query` token that is one of these (or a
# --flag) is forwarded verbatim to query.main; any other bare token keeps the
# historical `query <substr>` path-substring meaning.
_QUERY_VERBS = frozenset(
    {"find", "has-obs", "obsm", "by-type", "broken", "errors"}
)


# --------------------------------------------------------------------------- #
# Build pipeline (the public top-level entry, re-exported as build_index)
# --------------------------------------------------------------------------- #

def build_index(
    root: Path,
    out_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    *,
    overrides: Optional[Dict[str, Any]] = None,
    write_html_artifact: bool = True,
    write_dot_artifact: bool = True,
    write_agent_map_artifact: bool = True,
    incremental: bool = True,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Run the full build pipeline and write all artifacts; return the manifest.

    Steps (CONTRACTS.md §8/§11): ``load_config`` -> ``walker.walk(root)`` ->
    ``manifest.build_manifest`` -> ``manifest.write_outputs`` (INDEX.json +
    INDEX.jsonl) -> ``agent_map.write_agent_map`` -> ``render_html.write_html``
    -> ``crosslinks.build_graph`` + ``write_crosslinks``. ``out_dir`` defaults to
    ``root / config.out_dirname`` (``_repo_index``). ``overrides`` is an optional
    config-override dict; ``write_html_artifact`` / ``write_dot_artifact`` gate
    the HTML and crosslinks outputs (for ``--no-html`` / ``--no-dot``). Returns
    the assembled manifest mapping. Re-exported as ``repo_index.build_index``.
    """
    # Imported lazily so a bare ``import repo_index`` stays cheap and so this
    # module imports cleanly even while sibling modules are stubs.
    from . import config as config_mod
    from . import walker as walker_mod
    from . import manifest as manifest_mod
    from . import agent_map as agent_map_mod
    from . import render_html as render_html_mod
    from . import crosslinks as crosslinks_mod

    root = Path(root)

    # Resolve the OUT dir first (we need it to peek the prior index BEFORE config
    # is loaded so the prior index_columns can become the effective default).
    cfg_for_outdir = config_mod.load_config(config_path, overrides)
    if out_dir is None:
        out_dir = root / getattr(cfg_for_outdir, "out_dirname", "_repo_index")
    out_dir = Path(out_dir)

    # Make --no-columns (and --columns) PERSISTENT across the dominant read path.
    # `repo_index query`/`open`/`export-sqlite` auto-freshen via build_index with
    # overrides derived from CLI args; a PLAIN read (no --columns/--no-columns)
    # carries NO index_columns override, so cfg would fall back to the config
    # default (True) and the toggle-flip detector below would silently REVERSE a
    # prior --no-columns build — re-adding every wide `columns` list (the ~21.5 MB
    # the feature exists to remove) and paying a full re-extract. Fix: when the
    # user did NOT explicitly pass the flag, adopt the PRIOR index's index_columns
    # as the effective default (read from config_used.index_columns). Only an
    # explicit --columns/--no-columns overrides a prior setting. A MISSING prior
    # (legacy index / no prior build) leaves the config default in force.
    overrides = dict(overrides) if overrides else {}
    if "index_columns" not in overrides:
        prior_ic = manifest_mod._peek_prior_index_columns(out_dir)
        if prior_ic is not None:
            overrides["index_columns"] = prior_ic

    cfg = config_mod.load_config(config_path, overrides or None)

    def log(msg: str) -> None:
        if not quiet:
            print(msg, file=sys.stderr)

    # Force a FULL re-extract when the index_columns toggle flipped vs the prior
    # build. Unchanged files are size+mtime cache-reused (walker.make_entry), so
    # flipping index_columns ALONE would never re-read headers — re-indexing WITH
    # columns (or stripping them) requires bypassing the cache. The prior setting
    # is echoed in config_used.index_columns (config.raw); a MISSING prior is
    # treated as True (legacy builds always carried columns) so turning the toggle
    # OFF against an old index still forces a full. A --full build (incremental
    # already False) does not reach this and rewrites anyway. After the persistence
    # step above, a flip only fires on an EXPLICIT --columns/--no-columns that
    # differs from the prior — a plain freshen now inherits the prior and is a
    # no-op here (no spurious full re-extract).
    if incremental:
        prior_ic = manifest_mod._peek_prior_index_columns(out_dir)
        if prior_ic is None:
            prior_ic = True
        if bool(getattr(cfg, "index_columns", True)) != prior_ic:
            log("[repo_index] index_columns toggle changed — forcing full re-extract")
            incremental = False

    # Incremental: reuse the prior INDEX.json as a path->entry cache so unchanged
    # files (same size+mtime) are NOT re-extracted. Empty/missing prior => full.
    cache = manifest_mod.load_prior_entries(out_dir) if incremental else None
    if not cache:
        cache = None
    # Racy guard (CONTRACTS.md §9): the prior INDEX.json's mtime is the reference
    # "generation time" — any file whose mtime lands within racy_window_seconds of
    # it is re-extracted rather than cache-reused (closes the same-coarse-tick/
    # same-size blind spot of exFAT's 2s mtime granularity). Only meaningful when
    # a cache exists; None disables the rule (full builds, missing prior).
    reference_time: Optional[float] = None
    if cache is not None:
        try:
            reference_time = (Path(out_dir) / "INDEX.json").stat().st_mtime
        except OSError:
            reference_time = None
    log(f"[repo_index] walking {root} … ({'incremental' if cache else 'full'})")
    entries: List[Dict[str, Any]] = list(
        walker_mod.walk(
            root, cfg, out_dir=out_dir, cache=cache, reference_time=reference_time
        )
    )
    log(f"[repo_index] {len(entries)} entries; assembling manifest …")

    manifest = manifest_mod.build_manifest(root, entries, cfg, __version__)

    # Skip-write-on-unchanged (incremental only). The content_digest excludes
    # volatile fields (generated_at / git_commit / mtime_iso / error, §7), so an
    # equal digest means nothing STRUCTURAL changed — the on-disk artifacts are
    # already correct and re-serializing ~100 MB (INDEX.json + .jsonl + .html)
    # would be wasted. Only skip when the digest matches AND every artifact we'd
    # rewrite already exists (so a deleted artifact is always regenerated). A
    # --full build (incremental=False) always rewrites.
    if incremental:
        new_digest = manifest.get("content_digest")
        committed = manifest_mod._peek_committed_digest(out_dir)
        if committed is not None and committed == new_digest:
            have_json = (out_dir / "INDEX.json").exists()
            have_jsonl = (out_dir / "INDEX.jsonl").exists()
            # INDEX.html can fall BEHIND INDEX.json: a `query` freshen rewrites
            # json/jsonl with write_html_artifact=False, leaving INDEX.html at an
            # older digest. Skipping on mere EXISTENCE would then leave the page
            # stale — and the always-open app reloads from that page. So when we DO
            # own the HTML (write_html_artifact), require its OWN embedded digest to
            # match before skipping; otherwise fall through and regenerate it.
            have_html = (not write_html_artifact) or (
                (out_dir / "INDEX.html").exists()
                and manifest_mod._peek_html_digest(out_dir) == new_digest
            )
            if have_json and have_jsonl and have_html:
                log("[repo_index] no structural change — skipping artifact rewrite")
                return manifest

    paths = manifest_mod.write_outputs(manifest, out_dir)
    log(f"[repo_index] wrote {paths.get('index_json')}")
    log(f"[repo_index] wrote {paths.get('index_jsonl')}")

    if write_agent_map_artifact:
        agent_path = agent_map_mod.write_agent_map(manifest, cfg, out_dir)
        log(f"[repo_index] wrote {agent_path}")

    if write_html_artifact:
        html_path = render_html_mod.write_html(manifest, out_dir)
        log(f"[repo_index] wrote {html_path}")

    if write_dot_artifact:
        graph = crosslinks_mod.build_graph(manifest.get("entries", entries))
        xl = crosslinks_mod.write_crosslinks(graph, out_dir)
        log(f"[repo_index] wrote {xl.get('dot')}")
        log(f"[repo_index] wrote {xl.get('json')}")

    return manifest


# --------------------------------------------------------------------------- #
# Argparse + dispatch
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the repo_index CLI (CONTRACTS.md §11).

    A flat (subcommand-less) surface is used: the action is selected by mutually
    informative flags. ``--check`` routes to selfheal; a positional ``query``
    expression (or ``--query``) routes to the query CLI; otherwise the default
    action is a full build.
    """
    p = argparse.ArgumentParser(
        prog="repo_index",
        description=(
            "Self-healing structural index of a large scientific repository. "
            "Walks the tree once, extracts CHEAP per-file metadata (h5ad obs "
            "columns + n_obs x n_vars without loading the matrix, etc.), and "
            "emits INDEX.json/.jsonl/.html + an agent map + crosslinks."
        ),
    )
    p.add_argument("--root", type=Path, default=Path.cwd(),
                   help="Repository root to index (default: cwd).")
    p.add_argument("--out", type=Path, default=None,
                   help="Output directory (default: <root>/_repo_index).")
    p.add_argument("--config", type=Path, default=None,
                   help="Optional YAML/TOML config override file.")
    p.add_argument("--include", action="append", default=None, metavar="GLOB",
                   help="Glob of paths to INCLUDE (repeatable). If given, only "
                        "matching relative paths are indexed.")
    p.add_argument("--exclude", action="append", default=None, metavar="GLOB",
                   help="Glob of paths to EXCLUDE (repeatable).")
    p.add_argument("--no-html", action="store_true",
                   help="Skip writing INDEX.html.")
    p.add_argument("--no-dot", action="store_true",
                   help="Skip writing crosslinks.dot/.json.")
    p.add_argument("--max-csv-bytes", type=int, default=None, metavar="N",
                   help="Override csv/tsv exact-rowcount size gate (bytes).")
    p.add_argument("--max-json-bytes", type=int, default=None, metavar="N",
                   help="Override json parse size gate (bytes).")
    p.add_argument("--check", action="store_true",
                   help="Verify the committed INDEX.json against the live FS; "
                        "exit 1 on drift (self-heal check).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress progress logging on stderr.")
    p.add_argument("--full", action="store_true",
                   help="Force a FULL re-extract (ignore the incremental cache).")
    p.add_argument("--columns", dest="index_columns",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="Capture the wide per-CSV/TSV/parquet `columns` list "
                        "(default on). Use --no-columns to DROP it (keeps the "
                        "n_columns count + h5ad obs/var columns) and shrink "
                        "INDEX.json/.jsonl; --columns re-indexes them. Flipping "
                        "this forces a full re-extract.")
    p.add_argument("--figure-text", dest="index_figure_text",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="Extract the text rendered into SVG figures (axis labels, "
                        "legends, gene symbols) so figures become searchable "
                        "(default on). --no-figure-text skips the read entirely, "
                        "which is the ~17 s full-build cost this lever avoids. "
                        "Flipping this forces a full re-extract.")
    p.add_argument("--path-fields", action="store_true",
                   help="For extract-one/extract-batch: additionally emit "
                        "ext/category/tags (computed from the --root-relative "
                        "path) so a caller can avoid embedding the ext->category "
                        "map / ontology regexes itself.")
    p.add_argument("--no-refresh", action="store_true",
                   help="For query/open: do NOT auto-refresh the index first.")
    p.add_argument("--port", type=int, default=8765,
                   help="Port for `serve` (default 8765; uses the next free port if busy).")
    p.add_argument("--no-open", action="store_true",
                   help="For `serve`: do not auto-open the browser.")
    p.add_argument("--reveal", default=None, metavar="REL",
                   help="For `app`: a repo-relative path (or '<dir>/__dir__') to jump "
                        "to once the page loads (used by `reveal-in-app`).")
    p.add_argument("--version", action="version",
                   version=f"repo_index {__version__}")
    p.add_argument("command", nargs="?", default=None,
                   help="Action: 'build' (default), 'refresh', 'serve', 'app', 'install-app', "
                        "'install-finder-action', 'reveal-in-app', 'export-sqlite', "
                        "'extract-one', 'extract-batch', 'check', 'query', or 'open'.")
    p.add_argument("expr", nargs="?", default=None,
                   help="For 'query': the search expression (path substring). "
                        "For 'reveal-in-app': the file/folder path to reveal. "
                        "For 'extract-one': the file path to extract.")
    return p


def _overrides_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    """Translate CLI flags that override config into an overrides dict.

    Only includes keys the user actually set so config defaults remain intact.
    ``--include`` / ``--exclude`` are passed through as ``include_globs`` /
    ``exclude_globs`` for the walker/config to honour.
    """
    ov: Dict[str, Any] = {}
    if args.max_csv_bytes is not None:
        ov["csv_rowcount_max_bytes"] = args.max_csv_bytes
    if args.max_json_bytes is not None:
        ov["json_parse_max_bytes"] = args.max_json_bytes
    if args.include:
        ov["include_globs"] = list(args.include)
    if args.exclude:
        ov["exclude_globs"] = list(args.exclude)
    # default=None means the flag only overrides when the user actually passed
    # --columns / --no-columns (preserving the "only set keys override" invariant).
    if getattr(args, "index_columns", None) is not None:
        ov["index_columns"] = args.index_columns
    # Same "only set keys override" invariant for --figure-text / --no-figure-text,
    # so a plain read never silently reverts a persisted --no-figure-text build.
    if getattr(args, "index_figure_text", None) is not None:
        ov["index_figure_text"] = args.index_figure_text
    return ov


_FRESHEN_TTL_SECONDS = 10


def _freshen(
    root: Path,
    out_dir: Optional[Path],
    config_path: Optional[Path],
    overrides: Optional[Dict[str, Any]],
    *,
    want_html: bool,
    no_refresh: bool,
    full: bool,
) -> None:
    """Incrementally refresh the index before a READ (query/open).

    This is what makes the index a usable locator without a manual rebuild step:
    *accessing it freshens it*. The refresh is a ~1s stat-walk that reuses
    unchanged entries (walker incremental cache) and writes only INDEX.json/.jsonl
    (plus INDEX.html when ``want_html``) — it skips the agent map + crosslinks to
    stay fast. Skipped when ``no_refresh`` is set or the index was refreshed within
    the last few seconds (TTL), so back-to-back queries don't each re-walk. A
    freshen failure is swallowed — a slightly-stale read beats a hard error.
    """
    if no_refresh:
        return
    from . import config as config_mod

    cfg = config_mod.load_config(config_path, overrides or None)
    od = (
        Path(out_dir)
        if out_dir is not None
        else root / getattr(cfg, "out_dirname", "_repo_index")
    )
    idx = od / "INDEX.json"
    try:
        if idx.exists() and (time.time() - idx.stat().st_mtime) < _FRESHEN_TTL_SECONDS:
            return  # refreshed very recently — skip the re-walk
    except OSError:
        pass
    try:
        build_index(
            root,
            od,
            config_path,
            overrides=overrides or None,
            write_html_artifact=want_html,
            write_dot_artifact=False,
            write_agent_map_artifact=False,
            incremental=not full,
            quiet=True,
        )
    except Exception as exc:  # noqa: BLE001 - a freshen failure must not block a read
        print(
            f"[repo_index] freshen skipped: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def main(argv: Optional[List[str]] = None) -> int:
    """Parse args and dispatch the chosen action; return an int exit code.

    Dispatch (CONTRACTS.md §11):
      * ``--check`` or ``command == "check"`` -> ``selfheal.check`` (exit 1 on
        drift);
      * ``command == "query"`` -> ``query.main`` over INDEX.jsonl;
      * otherwise -> :func:`build_index` (full build of all artifacts).

    Any uncaught error returns exit code 2 with a message on stderr (the walk's
    per-file errors are captured inside extractors and never reach here).
    """
    parser = _build_parser()
    # parse_known_args (not parse_args) so the rich `query` verb forms
    # (find/has-obs/obsm/by-type/broken/errors + their flags) are NOT rejected by
    # this top-level parser — they are captured in ``extra`` and forwarded
    # verbatim to query.main below. Without this, `query has-obs <col>` exits 2
    # because the top-level parser only declares [command] [expr].
    args, extra = parser.parse_known_args(argv)

    root = Path(args.root)
    out_dir = Path(args.out) if args.out is not None else None
    overrides = _overrides_from_args(args)

    cmd = (args.command or "build").lower()

    # If anything was left unparsed and we're NOT in a mode that forwards/uses the
    # leftovers, it's a genuine usage error — re-run strict parsing so argparse emits
    # the standard message and exits 2 (preserves the prior contract for build/check).
    # `query` forwards `extra` to query.main; `reveal-in-app` takes its file path from
    # `extra` when it follows `--root` (argparse matches positionals in groups split
    # by optionals, so `reveal-in-app --root <r> <path>` leaves <path> in extra).
    if extra and cmd not in ("query", "reveal-in-app", "extract-one", "extract-batch"):
        parser.parse_args(argv)

    # --- open (reveal the generated INDEX.html in the default browser) ---
    if cmd in ("open", "view"):
        import webbrowser

        _freshen(root, out_dir, args.config, overrides,
                 want_html=True, no_refresh=args.no_refresh, full=args.full)

        from . import config as config_mod

        cfg = config_mod.load_config(args.config, overrides or None)
        if out_dir is None:
            out_dir = root / getattr(cfg, "out_dirname", "_repo_index")
        html = Path(out_dir) / "INDEX.html"
        if not html.exists():
            print(
                f"[repo_index] no INDEX.html at {html} — run a build first "
                f"(python -m repo_index --root {root}).",
                file=sys.stderr,
            )
            return 1
        webbrowser.open(html.resolve().as_uri())
        print(f"[repo_index] opened {html}", file=sys.stderr)
        return 0

    # --- serve (localhost server with an in-page /refresh) ---
    if cmd == "serve":
        from . import config as config_mod
        from . import serve as serve_mod

        cfg = config_mod.load_config(args.config, overrides or None)
        if out_dir is None:
            out_dir = root / getattr(cfg, "out_dirname", "_repo_index")
        out_dir = Path(out_dir)

        def _serve_build() -> None:
            build_index(
                root, out_dir, args.config,
                overrides=overrides or None,
                incremental=not args.full,
                quiet=True,
            )

        try:
            return int(serve_mod.serve(
                root, out_dir, _serve_build,
                port=args.port, open_browser=not args.no_open,
            ))
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            print(f"[repo_index] serve failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    # --- app (always-open native pywebview window; no http server) ---
    if cmd == "app":
        from . import config as config_mod
        from . import app as app_mod  # lazy: pywebview is an optional extra

        cfg = config_mod.load_config(args.config, overrides or None)
        if out_dir is None:
            out_dir = root / getattr(cfg, "out_dirname", "_repo_index")
        out_dir = Path(out_dir)
        try:
            return int(app_mod.run_app(
                root, out_dir, args.config, overrides or None, quiet=args.quiet,
                reveal=args.reveal,
            ))
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            print(f"[repo_index] app failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    # --- install-app (build a double-clickable .app bundle in ~/Applications) ---
    if cmd == "install-app":
        from . import app as app_mod  # lazy

        try:
            # out_dir doubles as the optional destination dir for the .app
            # (default ~/Applications when --out is not given).
            return int(app_mod.install_app(root, dest=out_dir))
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            print(f"[repo_index] install-app failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    # --- install-finder-action (Finder right-click → "Reveal in Repo Index") ---
    if cmd == "install-finder-action":
        from . import finder_action as fa_mod  # lazy

        try:
            # out_dir doubles as the optional destination dir for the .workflow
            # (default ~/Library/Services when --out is not given).
            return int(fa_mod.install(root, dest=out_dir))
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            print(f"[repo_index] install-finder-action failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    # --- reveal-in-app (jump the running app to a file; launch one if none) ---
    if cmd == "reveal-in-app":
        from . import app as app_mod  # lazy
        from . import finder as finder_mod

        # The path may arrive as `expr` (before --root) or in `extra` (after --root,
        # which is how the Finder Quick Action invokes it).
        target_arg = args.expr or (extra[0] if extra else None)
        if not target_arg:
            print("[repo_index] reveal-in-app needs a file/folder path "
                  "(e.g. repo_index reveal-in-app --root <root> /abs/path).",
                  file=sys.stderr)
            return 2
        info = finder_mod.relative_under_root(root, target_arg)
        if not info.get("ok"):
            print(f"[repo_index] reveal-in-app: {info.get('error')} "
                  f"({target_arg!r} is not under {root}).", file=sys.stderr)
            return 1
        rel = info["rel"]
        rootp = Path(root).resolve()
        # The page navigates directories via the "<dir>/__dir__" sentinel; files by
        # their plain relative path (see render_html revealInTree).
        target = (rel + "/__dir__") if info.get("is_dir") else rel
        try:
            import subprocess

            running = app_mod.app_is_running(rootp)
            # Queue the reveal for the running app's watcher (the path used when the app
            # is already open). This is written UNCONDITIONALLY so it is there whether
            # `open` activates an existing window or launches a new one.
            app_mod.request_reveal(rootp, target)
            app_mod._log(rootp, "reveal-in-app rel=%r target=%r app_running=%s" % (rel, target, running))

            bundle = app_mod.installed_bundle()
            if bundle is not None:
                # ONE unified path: `open -a <bundle>` ACTIVATES the running app (brings
                # it to the front — reliable, no AppleScript/TCC) OR launches it; the
                # `--args --reveal <target>` makes a FRESH launch jump deterministically
                # via run_app(reveal=…), while an already-running app jumps via the
                # watcher + the reveal.json we just wrote. `open` never spawns a duplicate.
                r = subprocess.run(["open", "-a", str(bundle), "--args", "--reveal", target],
                                   capture_output=True, text=True, check=False)
                app_mod._log(rootp, "open -a bundle rc=%s err=%r" % (r.returncode, (r.stderr or "").strip()))
                if r.returncode != 0:
                    print(f"[repo_index] could not open Repo Index.app: {(r.stderr or '').strip()}",
                          file=sys.stderr)
                    return 2
                print(f"[repo_index] revealing {rel or '(root)'} in Repo Index", file=sys.stderr)
            else:
                # No installed bundle — fall back to the module (generic Python window;
                # run `install-app` for the branded one). Single-launch-locked.
                if not running and app_mod.acquire_launch_lock(rootp):
                    launch = [sys.executable, "-m", "repo_index", "app",
                              "--root", str(rootp), "--reveal", target]
                    subprocess.Popen(launch, start_new_session=True)
                    app_mod._log(rootp, "launched module (no bundle)")
                print(f"[repo_index] no Repo Index.app bundle — used the module for "
                      f"{rel or '(root)'} (run `install-app` for a branded window)",
                      file=sys.stderr)
            return 0
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            print(f"[repo_index] reveal-in-app failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                app_mod._log(Path(root).resolve(), "reveal-in-app EXC %s: %s" % (type(exc).__name__, exc))
            except Exception:  # noqa: BLE001
                pass
            return 2

    # --- extract-one / extract-batch (stateless per-file/batch metadata helper,
    #     PHASE_0_1_SPEC.md §6): no freshen — these never touch INDEX.json, they
    #     hand a caller (the Rust Lens writer) the extractor's (extractor, meta,
    #     error) triple for one or many paths, reusing extract_meta verbatim. ---
    if cmd in ("extract-one", "extract-batch"):
        from . import config as config_mod
        from . import extract_one as extract_one_mod

        cfg = config_mod.load_config(args.config, overrides or None)
        path_fields = bool(args.path_fields)
        try:
            if cmd == "extract-one":
                # The path may arrive as `expr` (before --root) or in `extra`
                # (after --root) — same positional-split quirk as reveal-in-app.
                target_arg = args.expr or (extra[0] if extra else None)
                if not target_arg:
                    print("[repo_index] extract-one needs a file path "
                          "(e.g. repo_index extract-one --root <root> <path>).",
                          file=sys.stderr)
                    return 2
                return int(extract_one_mod.run_one(
                    target_arg, root, cfg, path_fields=path_fields
                ))
            return int(extract_one_mod.run_batch(root, cfg, path_fields=path_fields))
        except Exception as exc:  # noqa: BLE001 - the helper itself failed to start
            print(f"[repo_index] {cmd} failed to start: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 2

    # --- export-sqlite (DERIVED read-cache: INDEX.sqlite, entries + FTS5) ---
    if cmd == "export-sqlite":
        import json as _json

        from . import config as config_mod
        from . import export_sqlite as sql_mod

        # Freshen first so the DB reflects the current tree, then read the (now
        # current) INDEX.json and project it to SQLite — no second extraction.
        _freshen(root, out_dir, args.config, overrides,
                 want_html=False, no_refresh=args.no_refresh, full=args.full)

        cfg = config_mod.load_config(args.config, overrides or None)
        if out_dir is None:
            out_dir = root / getattr(cfg, "out_dirname", "_repo_index")
        out_dir = Path(out_dir)
        index_json = out_dir / "INDEX.json"
        if not index_json.exists():
            print(
                f"[repo_index] no INDEX.json at {index_json} — run a build first "
                f"(python -m repo_index --root {root}).",
                file=sys.stderr,
            )
            return 1
        try:
            with index_json.open(encoding="utf-8") as fh:
                manifest = _json.load(fh)
            db = sql_mod.write_sqlite(manifest, out_dir)
            print(f"[repo_index] wrote {db}", file=sys.stderr)
            return 0
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            print(f"[repo_index] export-sqlite failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    # --- check (drift detection) ---
    if args.check or cmd == "check":
        from . import config as config_mod
        from . import selfheal as selfheal_mod

        cfg = config_mod.load_config(args.config, overrides or None)
        if out_dir is None:
            out_dir = root / getattr(cfg, "out_dirname", "_repo_index")
        index_path = Path(out_dir) / "INDEX.json"
        try:
            return int(selfheal_mod.check(root, index_path, cfg))
        except FileNotFoundError as exc:
            print(f"[repo_index] check: {exc}", file=sys.stderr)
            return 1

    # --- query ---
    if cmd == "query":
        _freshen(root, out_dir, args.config, overrides,
                 want_html=False, no_refresh=args.no_refresh, full=args.full)

        from . import config as config_mod
        from . import query as query_mod

        cfg = config_mod.load_config(args.config, overrides or None)
        if out_dir is None:
            out_dir = root / getattr(cfg, "out_dirname", "_repo_index")
        jsonl = Path(out_dir) / "INDEX.jsonl"
        # query.main owns its own argparse (verbs find/has-obs/obsm/by-type/
        # broken/errors + flags). Forward the post-`query` tokens VERBATIM so the
        # full verb surface advertised in INDEX.agent.md / README is reachable
        # through `python -m repo_index query ...`. ``args.expr`` holds the first
        # post-`query` token (the verb, or a bare substring); ``extra`` holds the
        # rest (the verb's argument and/or any --flags).
        first = args.expr
        if first is not None and first not in _QUERY_VERBS and not first.startswith("-"):
            # Back-compat: a bare `query <substr>` (not a verb, not a flag) keeps
            # its historical meaning — a path substring search.
            verb_argv: List[str] = ["--path", first] + list(extra)
        else:
            verb_argv = ([first] if first is not None else []) + list(extra)
        q_argv: List[str] = ["--jsonl", str(jsonl)] + verb_argv
        return int(query_mod.main(q_argv))

    # --- build (default) ---
    try:
        build_index(
            root,
            out_dir,
            args.config,
            overrides=overrides or None,
            write_html_artifact=not args.no_html,
            write_dot_artifact=not args.no_dot,
            incremental=not args.full,
            quiet=args.quiet,
        )
    except Exception as exc:  # noqa: BLE001 - top-level guard: report, don't crash
        print(f"[repo_index] build failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    return 0
