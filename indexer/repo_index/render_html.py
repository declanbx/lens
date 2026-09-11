"""repo_index.render_html — single self-contained INDEX.html.

Emits ONE self-contained HTML file: inline CSS + JS, the manifest embedded as a
JSON ``<script type="application/json">`` blob (no external assets, no network).
The page provides:

  * a collapsible directory tree of all entries;
  * a category filter (checkboxes, one per §3 category present);
  * a client-side substring search over path + meta (incl. h5ad ``obs_columns``
    and ``obsm`` keys);
  * a click-to-inspect metadata panel that pretty-prints the selected entry's
    meta (h5ad obs columns, obsm keys/shapes, etc.);
  * a Health tab listing broken symlinks (``symlink_ok == false``) and entries
    carrying an ``error``.

Stdlib-only. See CONTRACTS.md §8.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


# --------------------------------------------------------------------------- #
# Manifest projection (keep the embedded blob lean but lossless for the UI)
# --------------------------------------------------------------------------- #

def _project_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON-serializable projection of ``manifest`` for embedding.

    Carries the summary, a small header block, and the full entry list (entries
    are already cheap structural metadata, so the whole list is embedded so the
    page can search/filter entirely client-side with no network). Falls back to
    sensible empties if a field is missing so a partial manifest still renders.
    """
    summary = manifest.get("summary") or {}
    entries = manifest.get("entries") or []
    return {
        "schema_version": manifest.get("schema_version", "1.0"),
        "generated_at": manifest.get("generated_at"),
        "root": manifest.get("root"),
        "git_commit": manifest.get("git_commit"),
        "tool_version": manifest.get("tool_version"),
        "content_digest": manifest.get("content_digest"),
        # Whether the wide per-CSV/TSV/parquet `columns` list was captured. Read
        # from config_used (echoed config.raw); default True for legacy manifests
        # predating the toggle. Surfaced to JS as DATA.index_columns so the COLUMNS
        # inspector card can distinguish "not indexed" from "genuinely 0 columns".
        "index_columns": bool(
            (manifest.get("config_used") or {}).get("index_columns", True)
        ),
        "summary": summary,
        # Long meta arrays (wide-table `columns`, etc.) are truncated to a head +
        # an `n_<key>_total` count FOR THE EMBED ONLY (see _project_entry_for_html).
        # The FULL arrays stay in INDEX.json / INDEX.jsonl, and the app's
        # Api.meta() bridge serves them on demand. This is the single biggest lever
        # on the always-open WebView's memory while keeping the page self-contained
        # (CONTRACTS §8).
        "entries": [_project_entry_for_html(e) for e in entries],
    }


# --------------------------------------------------------------------------- #
# HTML-embed metadata truncation (memory: keep the parsed JS graph small)
# --------------------------------------------------------------------------- #

# Meta keys whose values are string lists that can grow very large. The whole
# embed is ~80% `meta`, and ~90% of THAT is wide-table `columns` arrays (a single
# counts matrix can carry ~39k column names ≈ 1.1 MB), so truncating these to a
# head keeps the WebView parsing a few-MB blob instead of ~28 MB. Deliberately
# EXCLUDES `obs_columns` / `var_columns`: they are tiny (~0.15 MB total) and are
# the scientifically important inline-search target, so they stay full in the page.
_HTML_TRUNCATE_LIST_KEYS = (
    "columns", "defs", "classes", "imports", "uns_keys",
    "layers", "obsp", "varm", "members", "datasets", "outbound_refs",
    "top_level_keys",
)
_HTML_HEAD = 48
_HTML_FIRST_PARAGRAPH_MAX = 600


def _truncate_meta_for_html(meta: Dict[str, Any], head: int = _HTML_HEAD) -> Dict[str, Any]:
    """Return ``meta`` with long list values truncated to a ``head`` + a sibling
    ``n_<key>_total`` integer, for the HTML embed ONLY. Returns the SAME object
    (no copy) when nothing needs truncating, so the common small-meta entry is
    zero-cost. Never mutates the input. ``first_paragraph`` is capped to
    ``_HTML_FIRST_PARAGRAPH_MAX`` chars with an ellipsis."""
    if not isinstance(meta, dict):
        return meta
    out = meta
    changed = False
    for key in _HTML_TRUNCATE_LIST_KEYS:
        val = meta.get(key)
        if isinstance(val, list) and len(val) > head:
            if not changed:
                out = dict(meta)
                changed = True
            out[key] = val[:head]
            out["n_%s_total" % key] = len(val)
    fp = meta.get("first_paragraph")
    if isinstance(fp, str) and len(fp) > _HTML_FIRST_PARAGRAPH_MAX:
        if not changed:
            out = dict(meta)
            changed = True
        out["first_paragraph"] = fp[:_HTML_FIRST_PARAGRAPH_MAX] + "…"
    return out


#: SVG figure-text keys, dropped wholesale from the HTML embed. The static page
#: has no opt-in control, so carrying them would (a) silently fold every figure's
#: legend into the foundation's default `hayOf()` haystack — diverging it from
#: `db.rs`'s HAYSTACK, which by construction cannot see figure text — and (b) add
#: ~2.8 MB to a WKWebView string graph that is never recycled. Figure-text search
#: lives in the Lens app, backed by the `entries.figure_text` SQLite column.
_HTML_DROP_META_KEYS = ("figure_text", "figure_text_mode", "figure_text_truncated")


def _project_entry_for_html(entry: Dict[str, Any], head: int = _HTML_HEAD) -> Dict[str, Any]:
    """Return ``entry`` with its ``meta`` truncated for the HTML embed (see
    :func:`_truncate_meta_for_html`) and the figure-text keys dropped (see
    :data:`_HTML_DROP_META_KEYS`). Returns the SAME entry object when neither was
    needed (no copy), so most entries pass through untouched."""
    meta = entry.get("meta")
    if not isinstance(meta, dict):
        return entry
    new_meta = _truncate_meta_for_html(meta, head)
    if any(k in new_meta for k in _HTML_DROP_META_KEYS):
        new_meta = {k: v for k, v in new_meta.items() if k not in _HTML_DROP_META_KEYS}
    if new_meta is meta:
        return entry
    e = dict(entry)
    e["meta"] = new_meta
    return e


def _embed_json(obj: Any) -> str:
    """Serialize ``obj`` to a string safe to embed inside an HTML ``<script>``.

    Uses compact separators and ``ensure_ascii=False`` (the document declares
    UTF-8). Critically, the only sequence that can terminate a script element is
    ``</script`` (case-insensitive) and ``<!--``/``-->`` HTML comment markers, so
    those are defanged by inserting a backslash that JSON tolerates inside string
    values and that we also neutralise structurally. We escape ``<`` to the JSON
    unicode escape ``\\u003c`` which JSON.parse decodes back to ``<`` — this makes
    the blob impossible to break out of regardless of content.
    """
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    # Escape every '<' so neither </script> nor <!-- can appear literally; '>'
    # and '&' escaped too for completeness inside the script context.
    raw = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return raw


# --------------------------------------------------------------------------- #
# Static assets (inline CSS + JS). No CDN, no external fonts.
# --------------------------------------------------------------------------- #

_CSS = """
/* ===== repo_index — Locator. Dark default; light via :root[data-theme=light]. ===== */
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1f2630; --panel3:#11161d;
  --fg:#e6edf3; --fg2:#c9d1d9; --muted:#8b98a5; --faint:#6a7681;
  --border:#2a313c; --border2:#3a434f;
  --accent:#58a6ff; --accent-ink:#06101f; --accent-soft:#1b2c4a;
  --hit:#e3b341; --hit-bg:#3a2d12;
  --ok:#3fb950; --warn:#e3a008; --err:#f85149;
  /* 11 categories + other (dark) */
  --c-code:#79c0ff; --c-data_matrix:#d2a8ff; --c-data_table:#7ee787; --c-config:#ffa657;
  --c-doc:#a5d6ff; --c-notebook:#f0883e; --c-figure:#ff7b72; --c-figure_pdf:#d29922;
  --c-model:#e3b341; --c-log:#9aa5b1; --c-archive:#bc8cff; --c-other:#6e7681;
  --row-h:22px; --row-py:2px;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --ui:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
:root[data-theme=light]{
  --bg:#ffffff; --panel:#f6f8fa; --panel2:#eef1f4; --panel3:#f0f3f6;
  --fg:#1f2328; --fg2:#424a53; --muted:#59636e; --faint:#818b96;
  --border:#d1d9e0; --border2:#afb8c1;
  --accent:#0969da; --accent-ink:#ffffff; --accent-soft:#ddeeff;
  --hit:#9a6700; --hit-bg:#fff8c5;
  --ok:#1a7f37; --warn:#9a6700; --err:#cf222e;
  --c-code:#0550ae; --c-data_matrix:#8250df; --c-data_table:#1a7f37; --c-config:#bc4c00;
  --c-doc:#0550ae; --c-notebook:#bc4c00; --c-figure:#cf222e; --c-figure_pdf:#9a6700;
  --c-model:#7d4e00; --c-log:#59636e; --c-archive:#8250df; --c-other:#59636e;
}
:root[data-density=comfortable]{ --row-h:28px; --row-py:5px; }

*{box-sizing:border-box;}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
  font-family:var(--ui);font-size:13px;line-height:1.45;
  -webkit-font-smoothing:antialiased;overflow:hidden;}
a{color:var(--accent);text-decoration:none;}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums;}
.tnum{font-variant-numeric:tabular-nums;}

/* ===== app grid: header / strip / toolbar / body — only panes scroll ===== */
#app{display:grid;grid-template-rows:40px auto auto 1fr 22px;height:100vh;}

/* ----- HEADER ----- */
#hdr{height:40px;background:var(--panel);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:14px;padding:0 12px;overflow:hidden;}
#hdr .word{font-size:14px;font-weight:600;display:inline-flex;align-items:center;gap:6px;
  white-space:nowrap;}
#hdr .word .g{color:var(--accent);}
#hdr .stat{font-family:var(--mono);font-size:11px;color:var(--muted);
  font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
#hdr .right{margin-left:auto;display:flex;align-items:center;gap:10px;white-space:nowrap;}
#hdr .hmeta{font-family:var(--mono);font-size:11px;color:var(--muted);cursor:default;}
#hdr .hmeta.click{cursor:pointer;}
#hdr .hmeta.click:hover{color:var(--fg);}
.brokenchip{font-size:11px;border-radius:10px;padding:1px 8px;cursor:pointer;
  color:var(--err);border:1px solid var(--err);background:transparent;}
.brokenchip.zero{color:var(--muted);border-color:var(--border);cursor:default;}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:6px;overflow:hidden;}
.seg button{background:var(--panel2);color:var(--fg);border:0;border-right:1px solid var(--border);
  padding:3px 11px;cursor:pointer;font-size:12px;font-family:var(--ui);}
.seg button:last-child{border-right:0;}
.seg button.active{background:var(--accent);color:var(--accent-ink);font-weight:600;}
.iconbtn{background:transparent;border:1px solid var(--border);border-radius:6px;color:var(--fg);
  cursor:pointer;font-size:13px;width:26px;height:24px;line-height:1;padding:0;}
.iconbtn:hover{border-color:var(--accent);}
.iconbtn.off{opacity:.4;text-decoration:line-through;}

/* ----- KEY MATRICES STRIP ----- */
#strip{height:28px;background:var(--panel3);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:8px;padding:0 10px;overflow:hidden;}
#strip.hidden{display:none;}
#strip .lbl{font-size:10px;font-weight:600;letter-spacing:.04em;color:var(--muted);
  text-transform:uppercase;cursor:pointer;white-space:nowrap;user-select:none;}
#stripPills{display:flex;gap:6px;overflow-x:auto;overflow-y:hidden;flex:1;
  scrollbar-width:thin;}
.kmpill{display:inline-flex;align-items:center;gap:5px;white-space:nowrap;
  background:var(--panel2);border:1px solid var(--border);border-radius:12px;
  padding:1px 9px;font-size:11px;cursor:pointer;font-family:var(--mono);
  font-variant-numeric:tabular-nums;}
.kmpill:hover{border-color:var(--c-data_matrix);}
.kmpill .g{color:var(--c-data_matrix);}
.kmpill .dim{color:var(--muted);}

/* ----- TOOLBAR ----- */
#toolbar{background:var(--panel);border-bottom:1px solid var(--border);
  padding:8px 12px 6px;display:flex;flex-direction:column;gap:6px;}
.searchwrap{position:relative;display:flex;align-items:center;}
.searchwrap .lead{position:absolute;left:10px;color:var(--muted);font-size:13px;pointer-events:none;}
#search{width:100%;height:32px;padding:0 70px 0 28px;border-radius:8px;
  border:1px solid var(--border);background:var(--bg);color:var(--fg);font-size:13.5px;
  font-family:var(--ui);}
#search:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-soft);}
.searchwrap .trail{position:absolute;right:8px;display:flex;align-items:center;gap:8px;}
.searchwrap .scount{font-family:var(--mono);font-size:11px;color:var(--muted);
  font-variant-numeric:tabular-nums;}
.searchwrap .clr{cursor:pointer;color:var(--muted);font-size:13px;display:none;}
.searchwrap .clr.show{display:inline;}
.searchwrap .clr:hover{color:var(--fg);}
.shint{font-size:11px;color:var(--muted);margin-top:-2px;}
#tbrow2{display:flex;flex-wrap:wrap;align-items:center;gap:6px;}
.sortbtn{background:var(--panel2);color:var(--fg);border:1px solid var(--border);
  border-radius:8px;padding:2px 9px;font-size:11.5px;cursor:pointer;font-family:var(--ui);
  display:inline-flex;align-items:center;gap:5px;}
.sortbtn:hover{border-color:var(--accent);}
.sortbtn .arr{color:var(--muted);}
.sortmenu{position:absolute;z-index:30;background:var(--panel);border:1px solid var(--border2);
  border-radius:8px;padding:4px;box-shadow:0 6px 24px rgba(0,0,0,.35);display:none;}
.sortmenu.open{display:block;}
.sortmenu .opt{padding:4px 12px;font-size:12px;cursor:pointer;border-radius:5px;white-space:nowrap;}
.sortmenu .opt:hover{background:var(--panel2);}
.sortmenu .opt.cur{color:var(--accent);font-weight:600;}
.filterbtn{background:var(--panel2);color:var(--fg);border:1px solid var(--border);
  border-radius:8px;padding:2px 9px;font-size:11.5px;cursor:pointer;font-family:var(--ui);}
.filterbtn:hover{border-color:var(--accent);}
.filterchip{display:inline-flex;align-items:center;gap:4px;border-radius:12px;
  padding:1px 8px;font-size:11.5px;font-weight:500;cursor:pointer;user-select:none;
  border:1px solid var(--border);background:var(--panel2);position:relative;}
.filterchip .g{font-family:var(--mono);}
.filterchip .cnt{color:var(--muted);font-variant-numeric:tabular-nums;font-size:11px;}
.filterchip .only{font-size:9px;color:var(--muted);border:1px solid var(--border);
  border-radius:5px;padding:0 3px;margin-left:2px;display:none;}
.filterchip:hover .only{display:inline;}
.filterchip .only:hover{color:var(--accent);border-color:var(--accent);}
.filterchip.off{opacity:.4;}
/* active chip tint via per-cat var, set inline as --cc */
.filterchip.on{border-color:var(--cc);}
.filterchip.on .g{color:var(--cc);}
.iconbtn.spin{animation:ri-spin .8s linear infinite;}
@keyframes ri-spin{to{transform:rotate(360deg);}}
@media (prefers-reduced-motion:reduce){.iconbtn.spin{animation:none;}}
.grp>ul{list-style:none;margin:0;padding:0;}
.grp.collapsed>ul{display:none;}
.grphd{display:flex;align-items:center;gap:8px;padding:5px 8px;cursor:pointer;
  border-bottom:1px solid var(--border2);background:var(--panel);}
.grphd:hover{background:var(--panel2);}
.grphd .tw{width:12px;color:var(--muted);}
.grphd .gnm{font-weight:600;color:var(--fg2);}
.grphd .gcnt{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums;
  background:var(--panel2);border:1px solid var(--border);border-radius:9px;padding:0 7px;font-size:11px;}
.mrtag{font-size:9px;color:var(--muted);border:1px solid var(--border);border-radius:5px;
  padding:0 3px;margin-left:6px;vertical-align:middle;}
.grphd .gpath{font-family:var(--mono);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;}

/* ----- BREADCRUMB ----- */
#crumb{display:flex;align-items:center;gap:2px;font-family:var(--mono);font-size:11px;
  color:var(--muted);overflow:hidden;white-space:nowrap;}
#crumb .seg-c{cursor:pointer;overflow:hidden;text-overflow:ellipsis;}
#crumb .seg-c:hover{color:var(--accent);}
#crumb .sep{color:var(--faint);}
#crumb .cpy{margin-left:auto;cursor:pointer;color:var(--muted);padding-left:8px;}
#crumb .cpy:hover{color:var(--accent);}

/* ----- BODY ----- */
#body{display:grid;grid-template-columns:minmax(360px,1.6fr) 6px minmax(340px,1fr);
  min-height:0;overflow:hidden;}
#gutter{background:var(--border);}
.pane{overflow:auto;min-height:0;}
#left{border-right:0;}
#right{background:var(--panel);padding:0;}

/* column ruler */
#ruler{position:sticky;top:0;z-index:6;background:var(--panel);
  border-bottom:1px solid var(--border);display:grid;
  grid-template-columns:14px 16px minmax(0,1fr) auto 64px 56px 24px;
  align-items:center;gap:0;height:22px;padding:0 6px;
  font-size:11px;color:var(--muted);font-family:var(--mono);user-select:none;}
#ruler .r-name{padding-left:2px;}
#ruler .r-sz,#ruler .r-mt{text-align:right;cursor:pointer;}
#ruler .r-sz:hover,#ruler .r-mt:hover{color:var(--fg);}
#ruler .r-cp{text-align:center;}
#ruler .act{color:var(--accent);}

#tree{padding:2px 0 60px 0;}
#tree ul{list-style:none;margin:0;padding:0;}

/* ===== GRID ROWS (depth via padding-left, NOT nested margins) ===== */
.row{display:grid;
  grid-template-columns:14px 16px minmax(0,1fr) auto 64px 56px 24px;
  align-items:center;gap:0;height:var(--row-h);
  padding:var(--row-py) 6px var(--row-py) calc(8px + var(--depth,0) * 14px);
  white-space:nowrap;cursor:pointer;position:relative;border-radius:0;}
.row:hover{background:var(--panel2);}
.row:hover .cp{opacity:1;}
.row:focus-visible{outline:none;box-shadow:inset 0 0 0 2px var(--accent);}
.row.active{background:var(--panel2);}
.row.active::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--accent);}
.row.sel{background:var(--accent-soft);}
.row.sel::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent);}
.row.sel .nm{color:var(--fg);font-weight:550;}
.row.sel .cp{opacity:1;}
/* category rail: 3px bar just right of glyph using box-shadow on glyph cell */
.tw{font-size:11px;color:var(--muted);text-align:center;transition:transform .12s;}
.dir.collapsed > .row .tw{}
.glyph{font-family:var(--mono);font-size:13px;text-align:center;position:relative;}
.glyph::after{content:"";position:absolute;left:-3px;top:2px;bottom:2px;width:3px;
  border-radius:2px;background:var(--rail,transparent);}
.nm{overflow:hidden;text-overflow:ellipsis;color:var(--fg);font-size:12.5px;font-weight:450;
  padding-left:4px;}
.dir .nm{font-weight:600;}
.nm.sym{font-style:italic;}
.nm mark{background:var(--hit-bg);color:var(--hit);border-radius:2px;padding:0 1px;}
.shape{font-family:var(--mono);font-size:11px;font-variant-numeric:tabular-nums;
  padding:0 6px;color:var(--muted);text-align:right;}
.shape .flags{color:var(--muted);margin-left:3px;}
.sz{font-family:var(--mono);font-size:11px;color:var(--muted);text-align:right;
  font-variant-numeric:tabular-nums;}
.sz.heavy{color:var(--warn);}
.mt{font-family:var(--mono);font-size:11px;color:var(--muted);text-align:right;
  font-variant-numeric:tabular-nums;display:inline-flex;align-items:center;justify-content:flex-end;gap:3px;}
.mt .dot{font-size:8px;line-height:1;}
.cp{font-size:12px;color:var(--muted);text-align:center;opacity:0;}
.cp:hover{color:var(--accent);}
.cp.ok{color:var(--ok);opacity:1;}
.rowacts{position:absolute;right:4px;top:0;bottom:0;display:flex;align-items:center;gap:7px;
  opacity:0;padding-left:16px;background:linear-gradient(to right,transparent,var(--panel2) 40%);}
.row:hover .rowacts,.row.sel .rowacts{opacity:1;}
.rowacts .cp{opacity:1;font-size:13px;}
.grphd .cp{opacity:0;font-size:12px;}
.grphd:hover .cp{opacity:1;}
.cnt{font-family:var(--mono);font-size:11px;color:var(--muted);text-align:right;
  font-variant-numeric:tabular-nums;}
.collapsed > ul{display:none;}
@media (prefers-reduced-motion: reduce){ *{transition-duration:0ms !important;} }

/* ===== INSPECTOR ===== */
#panel{display:flex;flex-direction:column;height:100%;}
.insp-empty{color:var(--muted);text-align:center;padding:60px 24px;line-height:1.7;}
.insp-empty .big{font-size:28px;color:var(--c-data_matrix);display:block;margin-bottom:10px;}
.hero{position:sticky;top:0;z-index:4;background:var(--panel);padding:12px 14px 0;}
.hero .top{display:flex;align-items:flex-start;gap:10px;}
.hero .hg{font-family:var(--mono);font-size:24px;line-height:1;}
.hero .hname{font-size:16px;font-weight:600;word-break:break-all;flex:1;}
.hero .hbtns{display:flex;gap:4px;flex-shrink:0;}
.cbtn{background:var(--panel2);border:1px solid var(--border);border-radius:6px;color:var(--fg);
  cursor:pointer;font-size:11px;padding:2px 7px;font-family:var(--ui);white-space:nowrap;}
.cbtn:hover{border-color:var(--accent);}
.cbtn.ok{color:var(--ok);border-color:var(--ok);}
.hero .sub{font-family:var(--mono);font-size:11.5px;color:var(--muted);margin:6px 0 8px;
  font-variant-numeric:tabular-nums;}
.hero .rule{height:2px;border-radius:2px;background:var(--rail,var(--accent));}
.cards{padding:0 14px 40px;overflow:auto;}
.card{border-top:1px solid var(--border);padding:10px 0;}
.card:first-child{border-top:0;}
.sechdr{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
  color:var(--muted);margin:0 0 6px;display:flex;align-items:center;gap:8px;}
.sechdr-r{margin-left:auto;display:flex;gap:6px;align-items:center;}
.card .muted{font-family:var(--mono);font-size:11.5px;color:var(--faint);}
.bigstat{font-family:var(--mono);font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;}
.bigstat .sub{font-size:11.5px;color:var(--muted);font-weight:400;margin-left:8px;}
.metagrid{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-family:var(--mono);
  font-size:11.5px;margin-top:6px;font-variant-numeric:tabular-nums;}
.metagrid .k{color:var(--muted);}
.metagrid .v{color:var(--fg2);word-break:break-all;}
.pathline{font-family:var(--mono);font-size:11.5px;display:flex;align-items:center;gap:6px;
  word-break:break-all;}
.pathline code{background:var(--bg);border:1px solid var(--border);border-radius:4px;
  padding:1px 5px;cursor:pointer;flex:1;}
.pathline code:hover{border-color:var(--accent);}
.pathline.dim{color:var(--muted);margin-top:4px;}
.pathline .pc{cursor:pointer;color:var(--muted);}
.pathline .pc:hover{color:var(--accent);}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin:2px 0;}
.chip{background:var(--panel2);border:1px solid var(--border);border-radius:10px;
  padding:1px 8px;font-size:11.5px;font-family:var(--mono);font-weight:450;cursor:pointer;}
.chip:hover{border-color:var(--accent);}
.chip.hit{background:var(--hit-bg);color:var(--hit);border-color:var(--hit);}
.chip.umap{color:var(--accent);border-color:var(--accent);}
.chip.hidden{display:none;}
.loadall{font-size:11px;color:var(--muted);margin-left:8px;font-weight:400;text-transform:none;letter-spacing:0;}
.loadall-link{color:var(--accent);cursor:pointer;}
.loadall-link:hover{text-decoration:underline;}
.obsfilter{background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg);
  font-size:11px;padding:1px 6px;font-family:var(--ui);width:110px;}
.obsfilter:focus{outline:none;border-color:var(--accent);}
details.raw{margin-top:4px;}
details.raw>summary{cursor:pointer;font-size:11px;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase;color:var(--muted);list-style:none;}
details.raw>summary::-webkit-details-marker{display:none;}
details.raw>summary::before{content:"\\25B8 ";}
details.raw[open]>summary::before{content:"\\25BE ";}
pre.raw{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;
  overflow:auto;font-size:11px;font-family:var(--mono);white-space:pre-wrap;word-break:break-word;
  margin:6px 0 0;max-height:340px;}
.docpara{font-size:12px;line-height:1.5;color:var(--fg2);margin:4px 0;}
.docpara.title{font-weight:600;color:var(--fg);font-size:13px;}

/* ===== HEALTH ===== */
#health{display:none;padding:14px;overflow:auto;}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px;}
.tile{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;}
.tile .n{font-family:var(--mono);font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;}
.tile .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}
.tile.err .n{color:var(--err);}
.htable{width:100%;border-collapse:collapse;font-size:11.5px;font-family:var(--mono);margin-bottom:18px;}
.htable th,.htable td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--border);
  vertical-align:top;}
.htable th{color:var(--muted);position:sticky;top:0;background:var(--panel);text-transform:uppercase;
  font-size:10px;letter-spacing:.04em;}
.htable tr{cursor:pointer;}
.htable tr:hover td{background:var(--panel2);}
.htable .broken{color:var(--err);} .htable .errc{color:var(--warn);}
.htable code{background:var(--bg);padding:1px 4px;border-radius:4px;word-break:break-all;}
.allclear{color:var(--ok);font-size:13px;padding:20px 0;}

/* ----- STATUS BAR ----- */
#statusbar{height:22px;background:var(--panel);border-top:1px solid var(--border);
  display:flex;align-items:center;gap:14px;padding:0 12px;font-size:11px;color:var(--muted);
  font-family:var(--mono);font-variant-numeric:tabular-nums;overflow:hidden;}
#statusbar .keys{margin-left:auto;color:var(--faint);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;}

/* ----- TOAST + HELP OVERLAY ----- */
#toast{position:fixed;left:50%;bottom:40px;transform:translateX(-50%);
  background:var(--panel);border:1px solid var(--ok);color:var(--ok);border-radius:8px;
  padding:6px 14px;font-size:12px;box-shadow:0 4px 18px rgba(0,0,0,.4);z-index:100;
  opacity:0;pointer-events:none;transition:opacity .15s;}
#toast.show{opacity:1;}
#help{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:200;display:none;
  align-items:center;justify-content:center;}
#help.open{display:flex;}
#help .box{background:var(--panel);border:1px solid var(--border2);border-radius:12px;
  padding:18px 22px;max-width:560px;max-height:80vh;overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,.5);}
#help h3{margin:0 0 12px;font-size:14px;}
#help .kg{display:grid;grid-template-columns:auto 1fr;gap:4px 16px;font-size:12px;}
#help kbd{font-family:var(--mono);background:var(--panel2);border:1px solid var(--border);
  border-radius:4px;padding:0 5px;font-size:11px;color:var(--accent);}
#help .note{margin-top:12px;font-size:11px;color:var(--muted);line-height:1.5;}
#help .close{float:right;cursor:pointer;color:var(--muted);}
"""


def _js() -> str:
    """Return the inline client-side JavaScript (no external deps).

    Reads the embedded ``application/json`` blob, builds the directory tree,
    wires the category filter + substring search (over path + flattened meta +
    obs_columns + obsm keys), renders the click-to-inspect metadata panel, and
    populates the Health view. Pure DOM, no frameworks.
    """
    return r"""
"use strict";
const DATA = JSON.parse(document.getElementById("repo-index-data").textContent);
const ENTRIES = DATA.entries || [];
const SUMMARY = DATA.summary || {};
const ROOT = DATA.root || "";
// content_digest of the index state our in-memory ENTRIES currently reflect. A
// refresh delta is computed by the backend against the ON-DISK prior index; it is
// only valid to APPLY to ENTRIES when this baseline equals that prior (r.before).
// If another process advanced the on-disk index since we last synced (a `query`
// freshen, a commit, a manual build), the baseline diverges and the delta does not
// compose with our state — applyResult() then does a full reload instead. Updated
// after each successful in-place apply; re-initialised from DATA on every (re)load.
let BASELINE_DIGEST = DATA.content_digest || "";
// Free the embedded source text: once parsed into DATA we never touch the raw
// <script> blob again, and leaving it in the DOM keeps WebKit holding the full
// (multi-MB) source string for the life of the always-open window. Drop it. DATA
// (the parsed object, incl. git_commit) is unaffected.
(function(){ const _s=document.getElementById("repo-index-data"); if(_s){ _s.textContent=""; _s.remove(); } })();
// Cap applied to SEARCH RESULTS ONLY: a query matching thousands of files would
// freeze the browser if every hit became a live DOM row, so the flat search list
// is capped at RENDER_CAP with a "showing N of M — narrow your search" note. The
// BROWSE tree is NOT capped — it renders lazily (a folder's children are built
// into the DOM only when it is expanded), so the ENTIRE repository is always
// present and reachable, nothing hidden.
const RENDER_CAP = 3000;

// ---- in-memory prefs (NO localStorage — strict zero-state self-containment) ----
const PREFS = { theme:"dark", density:"compact", dateAbs:false, groupBy:"folder" };

// ---- per-category glyph map (mixed-shape, §1/§3) ----
const GLYPHS = {
  code:"❴", data_matrix:"▦", data_table:"▤", config:"⚙",
  doc:"¶", notebook:"◆", figure:"◑", figure_pdf:"⬚",
  model:"⬡", log:"≣", archive:"▢", other:"·"
};
function glyphFor(c){ return GLYPHS[c] || GLYPHS.other; }
function catVar(c){ return "var(--c-"+(c||"other")+")"; }
// Image-like categories (png/jpg/svg → figure, pdf → figure_pdf). The row-level
// copy button defaults to the RELATIVE path for these: pasting an ABSOLUTE image
// path into a Claude CLI prompt makes the CLI ingest the image instead of the path
// text, so relative is what you actually want when copying straight from the index.
function isImageCat(c){ return c==="figure" || c==="figure_pdf"; }

// ---- Precompute a lowercased search haystack per entry ----
function flatten(obj, out){
  if(obj==null) return;
  if(typeof obj==="string"){ out.push(obj); return; }
  if(typeof obj==="number"||typeof obj==="boolean"){ out.push(String(obj)); return; }
  if(Array.isArray(obj)){ for(const v of obj) flatten(v,out); return; }
  if(typeof obj==="object"){ for(const k in obj){ out.push(k); flatten(obj[k],out);} }
}
// LAZY haystack: building e._hay for all ~15k entries at load flattens every
// entry's meta up-front (idle RAM with no search active). Defer it — hayOf(e)
// builds + CACHES e._hay on FIRST use, so browsing with no query never builds
// a single haystack; searchFilter (the only reader) calls hayOf(e). Same exact
// flatten logic, just deferred. MEASURED: a full search sweep builds all 37k
// haystacks (~+150 MB resident) that WebKit never frees — so refresh() drops them
// when the search box is cleared (see _hayBuilt + the clear in refresh's else).
let _hayBuilt=false;
function hayOf(e){
  if(e._hay!=null) return e._hay;
  const parts=[e.path||"", e.category||"", e.ext||"", e.extractor||""];
  if(Array.isArray(e.tags)) parts.push(e.tags.join(" "));
  flatten(e.meta||{}, parts);
  e._hay = parts.join("").toLowerCase();
  _hayBuilt=true;
  return e._hay;
}

// ---- Category set ----
const CATS = {};
ENTRIES.forEach(e=>{ const c=e.category||"other"; CATS[c]=(CATS[c]||0)+1; });
const activeCats = new Set(Object.keys(CATS));

// ---- DOM refs ----
const treeEl=document.getElementById("tree");
const panelEl=document.getElementById("panel");
const searchEl=document.getElementById("search");
const crumbEl=document.getElementById("crumb");
const statusEl=document.getElementById("statusFilter");
let selectedPath=null;   // entry loaded in the inspector (Enter/click only)
let activeRow=null;      // keyboard cursor — does NOT rebuild inspector

// ---- Tree build from a filtered list ----
function buildTree(entries){
  const root={dirs:new Map(), files:[]};
  for(const e of entries){
    const segs=(e.path||"").split("/");
    let node=root;
    for(let i=0;i<segs.length-1;i++){
      const d=segs[i];
      if(!node.dirs.has(d)) node.dirs.set(d,{dirs:new Map(),files:[]});
      node=node.dirs.get(d);
    }
    node.files.push(e);
  }
  return root;
}
// Build a tree node ROOTED AT `prefix` from entries that all live under it: the
// path is split relative to `prefix` so the node's immediate children are the
// folder's direct entries (not the whole prefix re-nested). Used to render a
// search folder-hit's contents as the SAME structured, lazy tree as Browse.
function buildSubTreeAt(entries, prefix){
  const pfx = prefix ? (prefix.replace(/\/+$/,"")+"/") : "";
  const root={dirs:new Map(), files:[]};
  for(const e of entries){
    let rest = e.path||"";
    if(pfx && rest.indexOf(pfx)===0) rest = rest.slice(pfx.length);
    const segs=rest.split("/");
    let node=root;
    for(let i=0;i<segs.length-1;i++){
      const d=segs[i];
      if(!node.dirs.has(d)) node.dirs.set(d,{dirs:new Map(),files:[]});
      node=node.dirs.get(d);
    }
    node.files.push(e);
  }
  return root;
}
function fmtBytes(n){
  if(n==null) return "";
  const u=["B","KB","MB","GB","TB"]; let i=0,v=n;
  while(v>=1024&&i<u.length-1){v/=1024;i++;}
  return (i===0? v : v.toFixed(1))+u[i];
}
// Annotate every node with subtree aggregates so DIRECTORIES can be sorted by
// the same key as files: file count, newest descendant mtime, total size. One
// cheap recursive pass over the (already-built) tree.
function annotate(node){
  let count=node.files.length, newest="", size=0;
  for(const e of node.files){
    const m=e.mtime_iso||""; if(m>newest) newest=m;
    size+=e.size_bytes||0;
  }
  for(const child of node.dirs.values()){
    annotate(child);
    count+=child._count; size+=child._size;
    if(child._newest>newest) newest=child._newest;
  }
  node._count=count; node._newest=newest; node._size=size;
}
// A uniform sort descriptor for a file entry (dirs build their own inline).
function descOf(e){
  return {name:e.path.split("/").pop(), mtime:e.mtime_iso||"",
          size:e.size_bytes||0, type:e.category||"", entry:e};
}
// Active sort key + a comparator over {name,mtime,size,type} descriptors.
// DEFAULT is "newest" (mtime-descending) so the app/fresh index opens with the
// most-recently-touched files at the top of every sibling group — the common
// "what did I just produce / change?" question. TRADE-OFF (accepted): on coarse-
// mtime exFAT a touched / re-stat'd file can jump to the top of its sibling group
// and reshuffle the tree mid-browse; switch to "name" (path/basename-stable, in
// the sort menu / `s`) when a fixed order is wanted.
let sortKey="newest";
function cmpFor(key){
  const byName=(a,b)=> a.name<b.name?-1:(a.name>b.name?1:0);
  switch(key){
    case "newest":   return (a,b)=> (b.mtime||"").localeCompare(a.mtime||"") || byName(a,b);
    case "oldest":   return (a,b)=> (a.mtime||"").localeCompare(b.mtime||"") || byName(a,b);
    case "largest":  return (a,b)=> (b.size||0)-(a.size||0) || byName(a,b);
    case "smallest": return (a,b)=> (a.size||0)-(b.size||0) || byName(a,b);
    case "type":     return (a,b)=> (a.type||"").localeCompare(b.type||"") || byName(a,b);
    default:         return byName;
  }
}

// ---- relative time (§3 fmtRel) ----
function fmtRel(iso){
  if(!iso) return "";
  const t=Date.parse(iso); if(isNaN(t)) return iso.slice(0,10);
  const now=Date.now(); let s=(now-t)/1000;
  if(s<0) s=0;
  if(s<60) return "now";
  const m=s/60; if(m<60) return Math.floor(m)+"m";
  const h=m/60; if(h<24) return Math.floor(h)+"h";
  const d=h/24; if(d<7) return Math.floor(d)+"d";
  const w=d/7; if(w<5) return Math.floor(w)+"w";
  const dt=new Date(t);
  const MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  if(dt.getFullYear()===new Date(now).getFullYear()) return MON[dt.getMonth()]+" "+dt.getDate();
  return MON[dt.getMonth()]+" "+dt.getFullYear();
}
function recencyDot(iso){
  if(!iso) return null;
  const t=Date.parse(iso); if(isNaN(t)) return null;
  const age=(Date.now()-t)/1000;
  if(age<86400) return "var(--ok)";
  if(age<604800) return "var(--accent)";
  return null;
}
function absOf(path){ return ROOT ? (ROOT.replace(/\/+$/,"")+"/"+path) : path; }

// ---- clipboard (file:// safe — execCommand fallback mandatory) ----
function copyText(text, label){
  function done(){ toast((label||"copied")+" ✓"); }
  function fallback(){
    const ta=document.createElement("textarea");
    ta.value=text; ta.style.position="fixed"; ta.style.left="-9999px";
    document.body.appendChild(ta); ta.focus(); ta.select();
    let ok=false;
    try{ ok=document.execCommand("copy"); }catch(e){ ok=false; }
    document.body.removeChild(ta);
    if(ok) done();
    else{
      const ta2=document.createElement("textarea");
      ta2.value=text; ta2.style.position="fixed"; ta2.style.top="40%";
      ta2.style.left="50%"; ta2.style.transform="translateX(-50%)";
      ta2.style.zIndex="999"; ta2.style.width="60vw"; ta2.style.height="80px";
      document.body.appendChild(ta2); ta2.focus(); ta2.select();
      toast("press ⌘C / Ctrl+C to copy");
      setTimeout(()=>{ try{document.body.removeChild(ta2);}catch(e){} }, 4000);
    }
  }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done, fallback);
  } else fallback();
}
let _toastT=null;
function toast(msg){
  const t=document.getElementById("toast");
  const live=document.getElementById("toastLive");
  t.textContent=msg; t.classList.add("show");
  if(live) live.textContent=msg;
  if(_toastT) clearTimeout(_toastT);
  _toastT=setTimeout(()=>t.classList.remove("show"), 1200);
}
// ---- compact dims string (15.9M×300, 2.5k×5) ----
function compactNum(n){
  if(n==null) return "";
  // Always keep one decimal for millions so the headline matrix reads 15.9M
  // (not 16M) — the spec's canonical example. Thousands drop the decimal at
  // >=10k (spec mockup shows "36k").
  if(n>=1e6) return (n/1e6).toFixed(1).replace(/\.0$/,"")+"M";
  if(n>=1e3) return (n/1e3).toFixed(n>=1e4?0:1).replace(/\.0$/,"")+"k";
  return String(n);
}
function hasUmap(m){
  if(!m.obsm||typeof m.obsm!=="object") return false;
  return Object.keys(m.obsm).some(k=>/umap|pca|tsne|spatial/i.test(k));
}

// Build one clickable file row. showFullPath=true (flat SEARCH results) shows
// the full relative path so each hit is locatable; the tree shows the basename.
// Hover action overlay for any row (file OR dir): copy path + reveal/open in
// Finder, plus (while a search is active) "locate in browse tree". Absolutely
// positioned, so it never disturbs the grid.
// - copy DEFAULT is the absolute path; alt-click = relative. EXCEPT image rows
//   (isImage), which invert to relative-by-default (see isImageCat) so a copied
//   png path pastes as TEXT in the Claude CLI instead of being ingested as an image.
// - isDir routes the locate action to the directory (…/__dir__) form.
function rowActions(path, isImage, isDir){
  const a=el("span","rowacts");
  if(_searchActive){
    const lc=el("span","cp loc","⌖"); lc.title="locate in browse tree (clears search — lets you go up the tree)";
    lc.addEventListener("click",ev=>{ ev.stopPropagation(); revealInTree(isDir? path+"/__dir__" : path); });
    a.appendChild(lc);
  }
  const cp=el("span","cp","⧉");
  cp.title = isImage ? "copy relative path (alt-click = absolute)" : "copy path (alt-click = relative)";
  cp.addEventListener("click",ev=>{ ev.stopPropagation();
    const wantRel = isImage ? !ev.altKey : ev.altKey;
    copyText(wantRel?path:absOf(path), wantRel?"relative path":"absolute path");
    cp.classList.add("ok"); cp.textContent="✓"; setTimeout(()=>{cp.classList.remove("ok");cp.textContent="⧉";},900); });
  const rv=el("span","cp rv","⤤"); rv.title="reveal in Finder (o) — needs the repo_index app or `repo_index serve`";
  rv.addEventListener("click",ev=>{ ev.stopPropagation(); revealInFinder(path); });
  a.appendChild(cp); a.appendChild(rv);
  return a;
}
function renderFileRow(e, depth, showFullPath, labelOverride){
  const cat=e.category||"other";
  const li=document.createElement("li"); li.className="file";
  const row=document.createElement("div"); row.className="row"; row.tabIndex=-1;
  row.style.setProperty("--depth", depth);
  row.dataset.path=e.path;
  row.dataset.abs=absOf(e.path);
  row.dataset.cat=cat;
  row.dataset.mtime=e.mtime_iso||"";
  row.dataset.kind="file";
  row.appendChild(el("span","tw",""));
  const g=el("span","glyph",glyphFor(cat)); g.style.color=catVar(cat);
  g.style.setProperty("--rail",catVar(cat)); g.title=cat;
  row.appendChild(g);
  const nm=el("span","nm");
  const label= (labelOverride!=null) ? labelOverride : (showFullPath ? e.path : e.path.split("/").pop());
  const hq=_hilite;
  if(hq){
    const i=label.toLowerCase().indexOf(hq);
    if(i>=0){
      nm.appendChild(document.createTextNode(label.slice(0,i)));
      nm.appendChild(el("mark",null,label.slice(i,i+hq.length)));
      nm.appendChild(document.createTextNode(label.slice(i+hq.length)));
    } else nm.textContent=label;
  } else nm.textContent=label;
  if(e.is_symlink){
    nm.classList.add("sym");
    nm.appendChild(document.createTextNode(" ↪"));
    nm.title=e.symlink_target||"";
  }
  if(_searchActive && e._mr==="meta"){
    const tg=el("span","mrtag","meta"); tg.title="matched in metadata, not the path"; nm.appendChild(tg);
  }
  row.appendChild(nm);
  const shape=el("span","shape");
  const m=e.meta||{};
  if(cat==="data_matrix" && m.n_obs!=null){
    shape.style.color=catVar("data_matrix");
    shape.appendChild(document.createTextNode(compactNum(m.n_obs)+"×"+compactNum(m.n_vars)));
    const flags=[]; const ftitle=[];
    if(hasUmap(m)){ flags.push("⊞"); ftitle.push("has umap/pca/spatial obsm"); }
    if(Array.isArray(m.layers)&&m.layers.length){ flags.push("≣"); ftitle.push("layers"); }
    if(m.has_raw){ flags.push("ʀ"); ftitle.push("has_raw"); }
    if(flags.length){ const fl=el("span","flags",flags.join("")); fl.title=ftitle.join(", "); shape.appendChild(fl); }
  } else if(cat==="data_table" && m.row_count!=null){
    shape.style.color=catVar("data_table");
    const pfx=(m.row_count_exact===false)?"~":"";
    shape.textContent=pfx+compactNum(m.row_count)+"×"+(m.n_columns!=null?m.n_columns:"?");
  } else if(cat==="code" && PREFS.density==="comfortable" && Array.isArray(m.defs) && m.defs.length){
    shape.textContent="❴"+m.defs.length+"def";
  }
  row.appendChild(shape);
  const sz=el("span","sz",fmtBytes(e.size_bytes));
  if((e.size_bytes||0)>=1073741824) sz.classList.add("heavy");
  row.appendChild(sz);
  const mt=el("span","mt"); mt.title=e.mtime_iso||"";
  const dc=recencyDot(e.mtime_iso);
  if(dc){ const dot=el("span","dot","●"); dot.style.color=dc; mt.appendChild(dot); }
  const mtt=PREFS.dateAbs ? (e.mtime_iso||"").slice(0,10) : fmtRel(e.mtime_iso);
  mt.appendChild(document.createTextNode(mtt));
  row.appendChild(mt);
  row.appendChild(rowActions(e.path, isImageCat(cat), false));
  if(e.path===selectedPath) row.classList.add("sel");
  row.addEventListener("click",ev=>{ ev.stopPropagation(); setActive(row); selectEntry(e.path, row); });
  li.appendChild(row);
  return li;
}
// LAZY tree render: render this node's immediate dirs (collapsed) + files. A
// directory's children are materialized into the DOM only the FIRST time it is
// expanded, so the whole 15k-entry tree is reachable without ever building 15k
// live nodes at once — no cap, nothing hidden.
function renderNodeLazy(node, container, depth, prefix){
  const cmp=cmpFor(sortKey);
  const dirDescs=[...node.dirs.entries()].map(([d,child])=>(
    {name:d, mtime:child._newest||"", size:child._size||0, type:"", child}
  ));
  dirDescs.sort(cmp);
  for(const dd of dirDescs){
    const child=dd.child;
    const dirpath = prefix ? (prefix+"/"+dd.name) : dd.name;   // full folder path
    const li=document.createElement("li"); li.className="dir collapsed";
    const row=document.createElement("div"); row.className="row"; row.tabIndex=-1;
    row.style.setProperty("--depth", depth);
    row.dataset.kind="dir"; row.dataset.dirname=dd.name;
    row.dataset.path=dirpath; row.dataset.abs=absOf(dirpath);
    const tw=el("span","tw","▸");
    const g=el("span","glyph","");
    const nm=el("span","nm",dd.name+"/");
    const shape=el("span","shape","");
    const sz=el("span","sz",fmtBytes(child._size||0));
    if((child._size||0)>=1073741824) sz.classList.add("heavy");
    const mt=el("span","mt"); const dc=recencyDot(child._newest);
    if(dc){ const dot=el("span","dot","●"); dot.style.color=dc; mt.appendChild(dot); }
    mt.appendChild(document.createTextNode(PREFS.dateAbs?(child._newest||"").slice(0,10):fmtRel(child._newest)));
    mt.title=child._newest||"";
    const cnt=el("span","cnt",String(child._count));
    row.appendChild(tw); row.appendChild(g); row.appendChild(nm);
    row.appendChild(shape); row.appendChild(sz); row.appendChild(mt); row.appendChild(cnt);
    row.appendChild(rowActions(dirpath, false, true));   // copy folder path / open folder in Finder / locate (hover)
    li.appendChild(row);
    const ul=document.createElement("ul"); li.appendChild(ul);
    let populated=false;
    function toggle(){
      const collapsed=li.classList.toggle("collapsed");
      tw.textContent=collapsed?"▸":"▾";
      if(!collapsed && !populated){ renderNodeLazy(child, ul, depth+1, dirpath); populated=true; }
    }
    li._toggle=toggle;
    li._ensureOpen=function(){
      if(li.classList.contains("collapsed")) toggle();
      else if(!populated){ renderNodeLazy(child, ul, depth+1, dirpath); populated=true; }
    };
    row.addEventListener("click",ev=>{ ev.stopPropagation(); setActive(row); toggle(); });
    container.appendChild(li);
  }
  const fileDescs=node.files.map(descOf);
  fileDescs.sort(cmp);
  for(const fd of fileDescs) container.appendChild(renderFileRow(fd.entry, depth, false));
}
// Flat list (SEARCH results): each matching file with its full path, capped.
function renderFlatList(entries, container){
  for(const e of entries) container.appendChild(renderFileRow(e, 0, true));
}

let _hilite="";
let _searchActive=false;
// Parse a query into operators (cat:/type:/ext:/path:/obs:/dir:) + free tokens.
function parseQuery(raw){
  const ops={cat:[],ext:[],path:[],obs:[],dir:[]};
  const free=[];
  for(const tok of raw.trim().split(/\s+/)){
    if(!tok) continue;
    const m=tok.match(/^(cat|type|ext|path|obs|dir):(.+)$/i);
    if(m){ const k=m[1].toLowerCase(); const v=m[2].toLowerCase(); (k==="type"?ops.cat:ops[k]).push(v); }
    else free.push(tok.toLowerCase());
  }
  return {ops, freeTokens:free};
}
// Browse-mode filter: just the active category set (no query).
function currentFilter(){
  return ENTRIES.filter(e=>activeCats.has(e.category||"other"));
}
// Search-mode filter: AND of operators (cat/ext/path/obs/dir) + free tokens, honoring
// the active categories. Tags each match with _mr ("path"|"meta") = whether the free
// terms hit the path (so metadata-only matches can be flagged + scoped out via path:).
function searchFilter(parsed){
  const {ops, freeTokens}=parsed;
  const out=[];
  for(const e of ENTRIES){
    const cat=e.category||"other";
    if(!activeCats.has(cat)) continue;
    if(ops.cat.length && !ops.cat.some(c=>cat.indexOf(c)!==-1)) continue;
    const ext=(e.ext||"").toLowerCase();
    if(ops.ext.length && !ops.ext.some(x=>ext===x)) continue;
    const pl=e.path.toLowerCase();
    if(ops.path.length && !ops.path.every(p=>pl.indexOf(p)!==-1)) continue;
    if(ops.dir.length){ const d=pl.split("/").slice(0,-1).join("/"); if(!ops.dir.every(p=>d.indexOf(p)!==-1)) continue; }
    if(ops.obs.length){ const cols=(((e.meta||{}).obs_columns)||[]).join(" ").toLowerCase(); if(!ops.obs.every(p=>cols.indexOf(p)!==-1)) continue; }
    let ok=true;
    const hay=hayOf(e);
    for(const t of freeTokens){ if(hay.indexOf(t)===-1){ ok=false; break; } }
    if(!ok) continue;
    e._mr = (freeTokens.length && !freeTokens.every(t=>pl.indexOf(t)!==-1)) ? "meta" : "path";
    out.push(e);
  }
  return out;
}
function setSearchCount(txt){
  const sc=document.getElementById("scount"); if(sc) sc.textContent=txt;
}
// SEARCH results, grouped by category into collapsible sections (the structural
// fix for "a flat dump of a thousand matches"): one section per category present,
// ordered data/code-first and figures last; bulky/figure groups collapse by
// default so you see a typed breakdown with counts and expand only what you want.
function renderSearchGroups(entries, container){
  const cmp=cmpFor(sortKey);
  const groups=new Map();
  for(const e of entries){ const c=e.category||"other"; if(!groups.has(c)) groups.set(c,[]); groups.get(c).push(e); }
  const PRIO=["data_matrix","data_table","notebook","code","config","doc","model","log","archive","figure","figure_pdf","other"];
  const cats=[...groups.keys()].sort((a,b)=>{ const ia=PRIO.indexOf(a),ib=PRIO.indexOf(b); return (ia<0?99:ia)-(ib<0?99:ib); });
  const expandAll = entries.length<=30;   // few matches → just show them all
  let rendered=0;
  for(const cat of cats){
    const arr=groups.get(cat).map(descOf); arr.sort(cmp);
    const collapsed = !expandAll && cats.length>1 && (arr.length>25 || cat==="figure" || cat==="figure_pdf");
    const sec=document.createElement("li"); sec.className="grp"+(collapsed?" collapsed":"");
    const hd=document.createElement("div"); hd.className="grphd";
    const tw=el("span","tw",collapsed?"▸":"▾");
    const g=el("span","glyph",glyphFor(cat)); g.style.color=catVar(cat); g.title=cat;
    const nm=el("span","gnm",cat);
    const cnt=el("span","gcnt",String(arr.length));
    hd.appendChild(tw); hd.appendChild(g); hd.appendChild(nm); hd.appendChild(cnt);
    sec.appendChild(hd);
    const gul=document.createElement("ul"); sec.appendChild(gul);
    let pop=false;
    function fill(){ if(pop) return; for(const d of arr){ if(rendered>=RENDER_CAP) break; gul.appendChild(renderFileRow(d.entry,0,true)); rendered++; } pop=true; }
    if(!collapsed) fill();
    hd.addEventListener("click",()=>{ const c=sec.classList.toggle("collapsed"); tw.textContent=c?"▸":"▾"; if(!c) fill(); });
    container.appendChild(sec);
  }
}
function dirOf(p){ const i=p.lastIndexOf("/"); return i<0?"":p.slice(0,i); }
// SEARCH results grouped by FOLDER (parent dir). Folder groups are ordered by
// RELEVANCE to the query: a folder whose own name matches the term comes first
// (so "HNOCA" surfaces HNOCATables/ at the very top), then folders that match
// elsewhere in their path, then folders that matched only via file metadata.
// The top groups expand (up to ~40 rows); the long tail collapses — click to open.
function renderSearchGroupsByFolder(entries, container){
  const cmp=cmpFor(sortKey);
  const tok=_hilite;
  // Group each match under the SHALLOWEST ancestor folder whose name matches the
  // query (so a HNOCA-named folder shows its whole subtree as ONE group, "the
  // contents of the folder"); fall back to the immediate parent when no ancestor
  // name matches (file matched by its own name or metadata).
  function gkey(path){
    if(tok){
      const segs=path.split("/");
      for(let i=0;i<segs.length-1;i++){ if(segs[i].toLowerCase().indexOf(tok)!==-1) return segs.slice(0,i+1).join("/"); }
    }
    return dirOf(path);
  }
  const groups=new Map();
  for(const e of entries){ const k=gkey(e.path); if(!groups.has(k)) groups.set(k,[]); groups.get(k).push(e); }
  function score(k){
    if(!tok) return 2;
    const segs=k.split("/"); const last=(segs[segs.length-1]||"").toLowerCase();
    if(last.indexOf(tok)!==-1) return 0;          // folder's own name matches
    if(k.toLowerCase().indexOf(tok)!==-1) return 1;  // some ancestor matches
    return 2;                                      // matched only via file meta
  }
  const info=new Map();
  for(const [k,arr] of groups){ let nw=""; for(const e of arr){ if((e.mtime_iso||"")>nw) nw=e.mtime_iso||""; }
    info.set(k,{s:score(k), depth:k===""?0:k.split("/").length, nw}); }
  const keys=[...groups.keys()].sort((a,b)=>{
    const A=info.get(a),B=info.get(b);
    if(A.s!==B.s) return A.s-B.s;            // relevance tier
    if(A.depth!==B.depth) return A.depth-B.depth;  // shallower first (top-level wins)
    if(A.nw!==B.nw) return A.nw<B.nw?1:-1;   // newer folder first
    return a<b?-1:1;
  });
  const expandAll=entries.length<=30;
  let shown=0, rendered=0;
  keys.forEach((k,i)=>{
    const entriesK=groups.get(k);
    const arr=entriesK.map(descOf); arr.sort(cmp);
    // A folder-NAME hit (the query matched this folder's own name) shows the
    // folder's CONTENTS as the same structured, lazy, collapsible tree as Browse —
    // "the folder, as it would look outside of search". Other groups (file-name or
    // metadata matches) stay a flat list of the matching files within the folder.
    const isFolderHit = info.get(k).s===0 && k!=="";
    const collapsed = !expandAll && i>0 && shown>=60;
    if(!collapsed) shown+=arr.length;
    const sec=document.createElement("li"); sec.className="grp"+(collapsed?" collapsed":"");
    const hd=document.createElement("div"); hd.className="grphd";
    const tw=el("span","tw",collapsed?"▸":"▾");
    hd.appendChild(tw);
    const fg=el("span","glyph",isFolderHit?"▸▾":""); if(isFolderHit){ fg.style.color="var(--muted)"; fg.title="folder match — contents shown as a tree"; hd.appendChild(fg); }
    const nm=el("span","gnm gpath");
    const label=k===""?"(root)":k;
    const i2= tok? label.toLowerCase().indexOf(tok) : -1;
    if(i2>=0){ nm.appendChild(document.createTextNode(label.slice(0,i2)));
      nm.appendChild(el("mark",null,label.slice(i2,i2+tok.length)));
      nm.appendChild(document.createTextNode(label.slice(i2+tok.length))); }
    else nm.textContent=label;
    nm.title=label;
    const cnt=el("span","gcnt",String(arr.length));
    const fp=k||".";
    if(k!==""){
      const floc=el("span","cp loc","⌖"); floc.title="locate in browse tree (clears search — lets you go up the tree)";
      floc.addEventListener("click",ev=>{ ev.stopPropagation(); revealInTree(k+"/__dir__"); });
      hd.appendChild(floc);
    }
    const fcp=el("span","cp","⧉"); fcp.title="copy folder path (alt-click = relative)";
    fcp.addEventListener("click",ev=>{ ev.stopPropagation(); const rel=ev.altKey;
      copyText(rel?(k||"."):absOf(fp), rel?"relative folder path":"folder path");
      fcp.classList.add("ok"); fcp.textContent="✓"; setTimeout(()=>{fcp.classList.remove("ok");fcp.textContent="⧉";},900); });
    const frv=el("span","cp rv","⤤"); frv.title="open folder in Finder — needs the repo_index app or `repo_index serve`";
    frv.addEventListener("click",ev=>{ ev.stopPropagation(); revealInFinder(fp); });
    hd.appendChild(nm); hd.appendChild(fcp); hd.appendChild(frv); hd.appendChild(cnt);
    sec.appendChild(hd);
    const gul=document.createElement("ul"); sec.appendChild(gul);
    let pop=false;
    function fill(){ if(pop)return; pop=true;
      if(isFolderHit){
        // Structured: build a Browse-style subtree rooted at the matched folder and
        // render it lazily (subdirs expand on click). Not RENDER_CAP-bounded — it is
        // lazy exactly like Browse, so even a large folder stays cheap.
        const sub=buildSubTreeAt(entriesK, k);
        annotate(sub);
        renderNodeLazy(sub, gul, 0, k);
      } else {
        for(const d of arr){ if(rendered>=RENDER_CAP)break;
          const rel = k ? d.entry.path.slice(k.length+1) : d.entry.path;  // path within the folder
          gul.appendChild(renderFileRow(d.entry,0,false,rel)); rendered++; }
      }
    }
    if(!collapsed) fill();
    hd.addEventListener("click",()=>{ const c=sec.classList.toggle("collapsed"); tw.textContent=c?"▸":"▾"; if(!c)fill(); });
    container.appendChild(sec);
  });
}
function refresh(){
  const raw=(searchEl.value||"").trim();
  treeEl.innerHTML="";
  activeRow=null;
  const ul=document.createElement("ul");
  const nOff=Object.keys(CATS).length - activeCats.size;
  if(raw){
    _searchActive=true;
    const parsed=parseQuery(raw);
    _hilite=parsed.freeTokens[0]||"";
    const matches=searchFilter(parsed);
    if(PREFS.groupBy==="folder") renderSearchGroupsByFolder(matches, ul);
    else renderSearchGroups(matches, ul);
    treeEl.appendChild(ul);
    setSearchCount(matches.length+" match"+(matches.length===1?"":"es"));
    statusEl.textContent = matches.length+" / "+ENTRIES.length
      + (nOff? (" · "+nOff+" filter"+(nOff===1?"":"s")+" off"):"")
      + (matches.length? (" · grouped by "+PREFS.groupBy+" — click a header to expand"):"");
  } else {
    _searchActive=false;
    _hilite="";
    // Search cleared → free the per-entry haystacks built during the query (~150 MB
    // for 37k entries; WebKit won't reclaim them otherwise). They rebuild on demand
    // (hayOf) the next time a search runs. Only sweep when some were actually built.
    if(_hayBuilt){ for(const e of ENTRIES){ if(e._hay!=null) e._hay=null; } _hayBuilt=false; }
    const filtered=currentFilter();
    const root=buildTree(filtered);
    annotate(root);
    renderNodeLazy(root, ul, 0, "");
    treeEl.appendChild(ul);
    setSearchCount(filtered.length+" / "+ENTRIES.length);
    statusEl.textContent = filtered.length+" / "+ENTRIES.length
      + (nOff? (" · "+nOff+" filter"+(nOff===1?"":"s")+" off"):"");
  }
}
// Debounce search input: rebuilding the tree on every keystroke is expensive on
// a large repo, so coalesce rapid typing into one rebuild ~150ms after the last
// keypress.
let _refreshTimer=null;
function refreshDebounced(){
  if(_refreshTimer) clearTimeout(_refreshTimer);
  _refreshTimer=setTimeout(refresh,150);
}

// ================= in-place delta update (NO reload, §W3) =================
// The app/served refresh returns an ENTRY-LEVEL delta {added,changed,removed}.
// We mutate the in-memory ENTRIES model in place, re-render the tree from it, and
// restore the live DOM expansion/scroll/selection — so a refresh that adds or
// touches a handful of files NEVER does a full location.reload() (which would
// drop every open folder + the scroll position, and ratchet WebView memory).

// Rebuild CATS (category counts behind the filter chips) from the current ENTRIES.
// SAME derivation as the one-time build at load (the `CATS={}; ENTRIES.forEach…`
// near the top); kept in sync after a delta so chip counts stay truthful.
function recomputeCats(){
  for(const k in CATS) delete CATS[k];
  ENTRIES.forEach(e=>{ const c=e.category||"other"; CATS[c]=(CATS[c]||0)+1; });
}
// Mutate ENTRIES IN PLACE per the delta and keep it path-sorted (the canonical
// order buildTree/renderNodeLazy and the dataset.path navigation rely on). Returns
// true on success, false if the delta is unusable (caller then falls back to a
// reload). `changed` REPLACES by path (preserving none of the stale _hay cache —
// a replaced entry's meta may differ, so we drop the cached haystack with it).
function applyDelta(delta){
  if(!delta || typeof delta!=="object") return false;
  const added=Array.isArray(delta.added)?delta.added:[];
  const changed=Array.isArray(delta.changed)?delta.changed:[];
  const removed=Array.isArray(delta.removed)?delta.removed:[];
  if(!added.length && !changed.length && !removed.length) return true;  // no-op delta
  const removedSet=new Set(removed);
  const changedByPath=new Map();
  for(const e of changed){ if(e && typeof e.path==="string") changedByPath.set(e.path, e); }
  // One pass: drop removed, swap changed, keep the rest.
  const next=[];
  for(const e of ENTRIES){
    const p=e.path;
    if(removedSet.has(p)) continue;
    const repl=changedByPath.get(p);
    next.push(repl!==undefined ? repl : e);
  }
  // Append genuinely-new entries (a changed item whose path wasn't in ENTRIES —
  // shouldn't happen, but treat as an add so nothing is silently lost).
  const present=new Set(next.map(e=>e.path));
  for(const e of added){ if(e && typeof e.path==="string" && !present.has(e.path)){ next.push(e); present.add(e.path); } }
  for(const e of changed){ if(e && typeof e.path==="string" && !present.has(e.path)){ next.push(e); present.add(e.path); } }
  // Keep ENTRIES path-sorted (canonical order), mutating the SAME array object so
  // every closure that captured `ENTRIES` sees the update.
  next.sort((a,b)=>{ const x=a.path||"", y=b.path||""; return x<y?-1:(x>y?1:0); });
  ENTRIES.length=0; for(const e of next) ENTRIES.push(e);
  recomputeCats();
  // Prune chips/active set for categories that vanished; the filter UI is rebuilt
  // lazily on the next syncFilterChips()/refresh — counts come from CATS.
  for(const c of [...activeCats]){ if(!(c in CATS)) activeCats.delete(c); }
  return true;
}

// Snapshot the live view: every OPEN dir's full path, the scroll offset of the
// pane that actually scrolls (#left — the .pane wrapping #tree/#ruler; #tree
// itself does NOT scroll), and the selected entry's path.
function _scrollPane(){ return document.getElementById("left"); }
function captureViewState(){
  const expanded=[];
  treeEl.querySelectorAll("li.dir:not(.collapsed)").forEach(li=>{
    const r=li.querySelector(":scope > .row");
    if(r && r.dataset.path) expanded.push(r.dataset.path);
  });
  const pane=_scrollPane();
  return { expanded, scrollTop: pane?pane.scrollTop:0, selected: selectedPath };
}
// Open one directory by its FULL path, walking from the tree root and expanding
// each ancestor (reusing the per-li _ensureOpen lazy-populate). Returns the dir's
// <li> (or null if the path isn't in the current tree, e.g. filtered out).
function openDirByPath(dirpath){
  let container=treeEl.querySelector("ul");
  const segs=(dirpath||"").split("/").filter(Boolean);
  let li=null;
  for(const seg of segs){
    if(!container) return null;
    li=[...container.children].find(li2=>{
      const r=li2.querySelector(":scope > .row");
      return li2.classList.contains("dir") && r && r.dataset.dirname===seg;
    });
    if(!li) return null;
    if(li._ensureOpen) li._ensureOpen();
    container=li.querySelector(":scope > ul");
  }
  return li;
}
// Re-apply a captured view to the freshly re-rendered tree: open the same folders
// (SHALLOWEST FIRST so an ancestor is populated before we descend into it), then
// restore scroll + selection. All synchronous — runs in the same task as the
// re-render so the browser paints the result ONCE (no flicker, no reload).
function restoreViewState(st){
  if(!st) return;
  const paths=(st.expanded||[]).slice().sort((a,b)=>{
    const da=a.split("/").length, db=b.split("/").length;
    return da!==db ? da-db : (a<b?-1:(a>b?1:0));
  });
  for(const p of paths) openDirByPath(p);
  const pane=_scrollPane();
  if(pane && typeof st.scrollTop==="number") pane.scrollTop=st.scrollTop;
  if(st.selected){
    const row=[...treeEl.querySelectorAll(".row")].find(r=>r.dataset.path===st.selected && r.dataset.kind==="file");
    selectedPath=st.selected;
    if(row){ document.querySelectorAll(".row.sel").forEach(r=>r.classList.remove("sel")); row.classList.add("sel"); }
  }
}
// One atomic in-place update: snapshot → mutate ENTRIES → re-render → restore.
// Synchronous end-to-end so it paints once. Returns true if applied in place,
// false if the delta couldn't be applied (caller falls back to location.reload()).
function reconcile(delta){
  const st=captureViewState();
  if(!applyDelta(delta)) return false;
  syncFilterChips();   // chip on/off reflects the pruned activeCats
  refresh();           // rebuild the tree from the mutated ENTRIES (honours search/filter/sort)
  restoreViewState(st);
  return true;
}

// ---- helpers ----
function el(tag,cls,txt){ const x=document.createElement(tag); if(cls)x.className=cls; if(txt!=null)x.textContent=txt; return x; }

// ---- breadcrumb (parent path of selection) ----
function setBreadcrumb(path){
  crumbEl.innerHTML="";
  const segs=(path||"").split("/").filter(Boolean);
  const base = segs.length>1 ? segs.slice(0,-1) : null;
  if(!base){
    crumbEl.appendChild(el("span","seg-c", ROOT || "/"));
  } else {
    let acc=[];
    base.forEach((s,i)=>{
      acc.push(s);
      const c=el("span","seg-c",s);
      const target=acc.join("/");
      c.addEventListener("click",()=>{ revealInTree(target+"/__dir__"); });
      crumbEl.appendChild(c);
      if(i<base.length-1) crumbEl.appendChild(el("span","sep"," / "));
    });
  }
  const cpy=el("span","cpy","⧉"); cpy.title="copy this directory (absolute)";
  cpy.addEventListener("click",()=>{
    const d = base? absOf(base.join("/")) : (ROOT||"/");
    copyText(d,"directory");
  });
  crumbEl.appendChild(cpy);
}

// ================= INSPECTOR (card stack §6) =================
// ---- on-demand FULL metadata (app mode only) ----
// The HTML embed truncates long meta arrays (wide-table `columns`, etc.) to a
// head + an `n_<key>_total` count to keep the WebView lean. In the always-open
// app, the pywebview bridge `api.meta(path)` returns the COMPLETE meta from
// INDEX.jsonl on demand; a static file:// page has no bridge and just shows the
// head + count (the full list is one grep of INDEX.jsonl away).
// LRU-capped: each "load all" fetch caches the FULL meta (a wide table's columns can
// be ~1 MB) keyed by path with NO eviction → unbounded growth over days of inspecting.
// A Map preserves insertion order, so the oldest key is Map.keys().next() — evict it
// past _FULLMETA_CAP. loadFullMeta's miss path re-fetches via the bridge, so eviction
// is correctness-free.
const _FULLMETA_CAP=64;
const _fullMetaCache=new Map();
function _fullMetaGet(path){
  if(!_fullMetaCache.has(path)) return undefined;
  const v=_fullMetaCache.get(path);
  _fullMetaCache.delete(path); _fullMetaCache.set(path, v);  // bump to most-recent
  return v;
}
function _fullMetaPut(path, meta){
  _fullMetaCache.set(path, meta);
  while(_fullMetaCache.size>_FULLMETA_CAP){ _fullMetaCache.delete(_fullMetaCache.keys().next().value); }
}
function loadFullMeta(path, cb){
  const hit=_fullMetaGet(path);
  if(hit!==undefined){ cb(hit); return; }
  if(window.pywebview && window.pywebview.api && window.pywebview.api.meta){
    window.pywebview.api.meta(path).then(r=>{
      if(r && r.ok && r.meta){ _fullMetaPut(path, r.meta); cb(r.meta); }
      else toast("could not load full metadata"+(r&&r.error?": "+r.error:""));
    }).catch(e=>toast("load failed: "+e));
  } else {
    toast("full list is in the app — or grep INDEX.jsonl");
  }
}
// True total of a (possibly truncated) embedded array: prefer n_<key>_total.
function arrTotal(m,key,arr){ const t=m["n_"+key+"_total"]; return (t!=null)?t:(Array.isArray(arr)?arr.length:0); }
// Append a "H of N · load all" affordance to a header when meta[key] was truncated
// for the embed. renderArr(arr) re-renders `wrap`; "load all" fetches the full
// array via the bridge and re-renders with it.
function maybeLoadAll(headerEl, e, m, key, wrap, renderArr){
  const total=m["n_"+key+"_total"];
  const shown=Array.isArray(m[key])?m[key].length:0;
  if(total==null || total<=shown) return;
  const note=el("span","loadall");
  note.appendChild(document.createTextNode(shown+" of "+total+" · "));
  const a=el("span","loadall-link","load all"); a.title="fetch the complete list (app mode)";
  a.addEventListener("click",ev=>{ ev.stopPropagation();
    loadFullMeta(e.path, full=>{
      const arr=full&&full[key];
      if(Array.isArray(arr)){ wrap.innerHTML=""; renderArr(arr); note.remove(); }
      else toast("full "+key+" unavailable");
    });
  });
  note.appendChild(a);
  headerEl.appendChild(note);
}
function selectEntry(path,row){
  selectedPath=path;
  document.querySelectorAll(".row.sel").forEach(r=>r.classList.remove("sel"));
  if(row) row.classList.add("sel");
  const e=ENTRIES.find(x=>x.path===path);
  panelEl.innerHTML="";
  if(!e){ panelEl.appendChild(el("div","insp-empty","Not found")); return; }
  setBreadcrumb(path);
  const cat=e.category||"other"; const m=e.meta||{};
  const cv=catVar(cat);

  const hero=el("div","hero");
  const top=el("div","top");
  const hg=el("span","hg",glyphFor(cat)); hg.style.color=cv; hg.title=cat;
  const hname=el("span","hname",path.split("/").pop());
  const hb=el("div","hbtns");
  function cbtn(label,title,fn){ const b=el("button","cbtn",label); b.title=title;
    b.addEventListener("click",()=>{ fn();
      const o=b.textContent; b.classList.add("ok"); b.textContent="✓";
      setTimeout(()=>{ b.classList.remove("ok"); b.textContent=o; },900);
    }); return b; }
  hb.appendChild(cbtn("⧉ abs","copy absolute path",()=>copyText(absOf(e.path),"absolute path")));
  hb.appendChild(cbtn("⧉ rel","copy relative path",()=>copyText(e.path,"relative path")));
  hb.appendChild(cbtn("⇱ Finder","reveal in Finder (o) — needs the repo_index app or `repo_index serve`",()=>revealInFinder(e.path)));
  top.appendChild(hg); top.appendChild(hname); top.appendChild(hb);
  hero.appendChild(top);
  const sub=el("div","sub",
    cat+" · "+(e.ext||"(none)")+" · "+fmtBytes(e.size_bytes)+" · "+fmtRel(e.mtime_iso));
  hero.appendChild(sub);
  const rule=el("div","rule"); rule.style.background=cv; hero.appendChild(rule);
  panelEl.appendChild(hero);

  const cards=el("div","cards");
  function card(){ const c=el("div","card"); cards.appendChild(c); return c; }
  function hdr(parent,txt){ const h=el("div","sechdr",txt); parent.appendChild(h); return h; }

  // 1) PATH
  {
    const c=card(); hdr(c,"PATH");
    const rel=el("div","pathline");
    const rc=el("code",null,e.path); rc.title="click to copy relative path";
    rc.addEventListener("click",()=>copyText(e.path,"relative path"));
    const rpc=el("span","pc","⧉"); rpc.addEventListener("click",()=>copyText(e.path,"relative path"));
    rel.appendChild(rc); rel.appendChild(rpc); c.appendChild(rel);
    const ab=el("div","pathline dim");
    const ac=el("code",null,absOf(e.path)); ac.title="click to copy absolute path";
    ac.addEventListener("click",()=>copyText(absOf(e.path),"absolute path"));
    const apc=el("span","pc","⧉"); apc.addEventListener("click",()=>copyText(absOf(e.path),"absolute path"));
    ab.appendChild(ac); ab.appendChild(apc); c.appendChild(ab);
  }

  // 2) DATASET
  if(cat==="data_matrix" && m.n_obs!=null){
    const c=card(); hdr(c,"DATASET");
    const bs=el("div","bigstat");
    bs.appendChild(document.createTextNode(m.n_obs.toLocaleString()+" × "+(m.n_vars!=null?m.n_vars.toLocaleString():"?")));
    bs.appendChild(el("span","sub","≈ "+compactNum(m.n_obs)+" cells"));
    c.appendChild(bs);
    const mg=el("div","metagrid");
    const kv=(k,v)=>{ mg.appendChild(el("div","k",k)); mg.appendChild(el("div","v",v==null?"—":String(v))); };
    if(m.X_encoding!=null) kv("X encoding",m.X_encoding);
    if(m.X_dtype!=null) kv("X dtype",m.X_dtype);
    if(m.obs_index!=null) kv("obs_index",m.obs_index);
    kv("has_raw", m.has_raw?"✓":"✗");
    if(m.n_var_columns!=null) kv("n_var_cols",m.n_var_columns);
    c.appendChild(mg);
  } else if(cat==="data_table" && m.row_count!=null){
    const c=card(); hdr(c,"DATASET");
    const bs=el("div","bigstat");
    const pfx=(m.row_count_exact===false)?"~":"";
    bs.appendChild(document.createTextNode(pfx+m.row_count.toLocaleString()+" × "+(m.n_columns!=null?m.n_columns:"?")));
    bs.appendChild(el("span","sub","rows × cols"));
    c.appendChild(bs);
    const mg=el("div","metagrid");
    const kv=(k,v)=>{ mg.appendChild(el("div","k",k)); mg.appendChild(el("div","v",v==null?"—":String(v))); };
    if(m.delimiter!=null) kv("delimiter", JSON.stringify(m.delimiter));
    kv("exact", m.row_count_exact===false?"no":"yes");
    c.appendChild(mg);
  }

  // 3) OBS COLUMNS (searchable)
  if(Array.isArray(m.obs_columns) && m.obs_columns.length){
    const c=card(); const h=hdr(c,"OBS COLUMNS ("+m.obs_columns.length+")");
    const hr=el("div","sechdr-r");
    const flt=el("input","obsfilter"); flt.placeholder="filter obs…"; flt.type="text";
    const allc=el("span","pc","⧉ all"); allc.style.cursor="pointer"; allc.title="copy all obs columns";
    allc.addEventListener("click",()=>copyText(m.obs_columns.join(","),"obs columns"));
    hr.appendChild(flt); hr.appendChild(allc); h.appendChild(hr);
    const wrap=el("div","chips");
    const q=(searchEl.value||"").trim().toLowerCase();
    m.obs_columns.forEach(col=>{
      const ch=el("span","chip",col);
      if(q && String(col).toLowerCase().indexOf(q)>=0) ch.classList.add("hit");
      ch.addEventListener("click",()=>copyText(col,"column name"));
      ch._t=String(col).toLowerCase();
      wrap.appendChild(ch);
    });
    c.appendChild(wrap);
    flt.addEventListener("input",()=>{
      const fv=flt.value.toLowerCase();
      wrap.querySelectorAll(".chip").forEach(ch=>{
        ch.classList.toggle("hidden", !!fv && ch._t.indexOf(fv)<0);
      });
    });
  }

  // 4) OBSM
  if(m.obsm && typeof m.obsm==="object"){
    const keys=Object.keys(m.obsm);
    if(keys.length){
      const c=card(); hdr(c,"OBSM ("+keys.length+")");
      const wrap=el("div","chips");
      keys.forEach(k=>{
        const shp=m.obsm[k];
        const isU=/umap|pca|tsne|spatial/i.test(k);
        const lbl=(isU?"⊞ ":"")+k+(Array.isArray(shp)?(" ["+shp.join("×")+"]"):"");
        const ch=el("span","chip",lbl);
        if(isU) ch.classList.add("umap");
        ch.addEventListener("click",()=>copyText(k,"obsm key"));
        wrap.appendChild(ch);
      });
      c.appendChild(wrap);
    }
  }

  // 5) STRUCTURE (collapsed)
  {
    const fields=[["layers",m.layers],["varm",m.varm],["obsp",m.obsp],
                  ["uns_keys",m.uns_keys],["var_columns",m.var_columns]];
    const present=fields.filter(f=>Array.isArray(f[1])&&f[1].length);
    if(present.length){
      const c=card();
      const det=el("details","raw");
      det.appendChild(el("summary",null,"STRUCTURE"));
      present.forEach(([name,arr])=>{
        det.appendChild(el("div","sechdr",name+" ("+arrTotal(m,name,arr)+")"));
        const wrap=el("div","chips");
        arr.forEach(x=>{ const ch=el("span","chip",String(x));
          ch.addEventListener("click",()=>copyText(String(x),name)); wrap.appendChild(ch); });
        det.appendChild(wrap);
      });
      c.appendChild(det);
    }
  }

  // 6) CONTENTS (non-h5ad)
  if(cat!=="data_matrix"){
    if(Array.isArray(m.columns) && m.columns.length){
      const c=card(); const h=hdr(c,"COLUMNS ("+arrTotal(m,"columns",m.columns)+")");
      const hr=el("div","sechdr-r");
      const flt=el("input","obsfilter"); flt.placeholder="filter…"; flt.type="text";
      hr.appendChild(flt); h.appendChild(hr);
      const wrap=el("div","chips");
      function renderCols(arr){ arr.forEach(col=>{
        const lbl=(col&&typeof col==="object")?(col.name+":"+col.type):String(col);
        const ch=el("span","chip",lbl); ch._t=lbl.toLowerCase();
        ch.addEventListener("click",()=>copyText(lbl,"column"));
        wrap.appendChild(ch);
      }); }
      renderCols(m.columns);
      c.appendChild(wrap);
      maybeLoadAll(h, e, m, "columns", wrap, renderCols);   // wide-table cols: truncated in embed, fetch full on demand
      flt.addEventListener("input",()=>{
        const fv=flt.value.toLowerCase();
        wrap.querySelectorAll(".chip").forEach(ch=>ch.classList.toggle("hidden",!!fv && ch._t.indexOf(fv)<0));
      });
    } else if(DATA.index_columns===false && m.n_columns!=null && m.n_columns>0){
      // Columns were NOT indexed (build ran with --no-columns). The count is kept,
      // so show a count-only placeholder instead of silently dropping the card. No
      // filter / load-all bridge (the list is absent from INDEX.json/.jsonl too).
      const c=card(); hdr(c,"COLUMNS ("+m.n_columns+")");
      c.appendChild(el("div","muted","names not indexed — click the ⊞ button in the header to re-index"));
    }
    if(cat==="code" && (m.docstring_first_line || (Array.isArray(m.defs)&&m.defs.length) ||
        (Array.isArray(m.classes)&&m.classes.length) || (Array.isArray(m.imports)&&m.imports.length))){
      const c=card(); hdr(c,"CONTENTS");
      if(m.docstring_first_line) c.appendChild(el("div","docpara","“"+m.docstring_first_line+"”"));
      [["defs",m.defs],["classes",m.classes],["imports",m.imports]].forEach(([nm,arr])=>{
        if(Array.isArray(arr)&&arr.length){
          const det=el("details","raw"); det.appendChild(el("summary",null,nm+" ("+arrTotal(m,nm,arr)+")"));
          const wrap=el("div","chips");
          arr.forEach(x=>{ const ch=el("span","chip",String(x));
            ch.addEventListener("click",()=>copyText(String(x),nm)); wrap.appendChild(ch); });
          det.appendChild(wrap); c.appendChild(det);
        }
      });
    }
    if(cat==="doc" && (m.h1_title || m.first_paragraph || (Array.isArray(m.outbound_refs)&&m.outbound_refs.length))){
      const c=card(); hdr(c,"CONTENTS");
      if(m.h1_title) c.appendChild(el("div","docpara title",m.h1_title));
      if(m.first_paragraph) c.appendChild(el("div","docpara",m.first_paragraph));
      if(Array.isArray(m.outbound_refs)&&m.outbound_refs.length){
        c.appendChild(el("div","sechdr","outbound refs ("+arrTotal(m,"outbound_refs",m.outbound_refs)+")"));
        const wrap=el("div","chips");
        m.outbound_refs.forEach(r=>{ const ch=el("span","chip",String(r));
          ch.title="click to search for this ref";
          ch.addEventListener("click",()=>{ searchEl.value=String(r);
            document.getElementById("clr").classList.add("show"); refresh(); searchEl.focus(); });
          wrap.appendChild(ch); });
        c.appendChild(wrap);
      }
    }
    if(cat==="figure"||cat==="figure_pdf"){
      const c=card(); hdr(c,"CONTENTS");
      c.appendChild(el("div","docpara","preview not embedded"));
    }
  }

  // 7) PROVENANCE
  {
    const c=card(); hdr(c,"PROVENANCE");
    const mg=el("div","metagrid");
    const kv=(k,v)=>{ mg.appendChild(el("div","k",k)); mg.appendChild(el("div","v",v==null?"—":String(v))); };
    kv("category",cat); kv("ext",e.ext||"(none)");
    kv("size",fmtBytes(e.size_bytes)+" ("+(e.size_bytes||0)+" B)");
    kv("mtime",e.mtime_iso||"—");
    kv("extractor",e.extractor||"—");
    if(Array.isArray(e.tags)&&e.tags.length) kv("tags",e.tags.join(", "));
    c.appendChild(mg);
    if(e.is_symlink){
      const sl=el("div","pathline dim");
      sl.appendChild(document.createTextNode("↪ "+(e.symlink_target||"")+"  "+
        (e.symlink_ok===false?"BROKEN":"OK")));
      if(e.symlink_target){ const pc=el("span","pc","⧉");
        pc.addEventListener("click",()=>copyText(e.symlink_target,"symlink target")); sl.appendChild(pc); }
      if(e.symlink_ok===false) sl.style.color="var(--err)";
      c.appendChild(sl);
    }
    if(e.error){ const er=el("div","docpara",e.error); er.style.color="var(--warn)"; c.appendChild(er); }
  }

  // 8) RAW META (collapsed)
  {
    const c=card();
    const det=el("details","raw");
    det.appendChild(el("summary",null,"raw meta"));
    const cj=el("span","pc","⧉ copy json"); cj.style.cursor="pointer";
    cj.addEventListener("click",()=>copyText(JSON.stringify(m,null,2),"json"));
    const pre=el("pre","raw",JSON.stringify(m,null,2));
    det.appendChild(cj); det.appendChild(pre);
    c.appendChild(det);
  }

  panelEl.appendChild(cards);
}
function inspectorEmpty(){
  panelEl.innerHTML="";
  const e=el("div","insp-empty");
  e.appendChild(el("span","big","▦"));
  e.appendChild(document.createTextNode("select a file — ↑↓ move · Enter inspect · y copy path · / search · ? help"));
  panelEl.appendChild(e);
  setBreadcrumb("");
}

// ================= KEY MATRICES STRIP (top-12 by n_obs) =================
function buildStrip(){
  const pills=document.getElementById("stripPills");
  pills.innerHTML="";
  const dm=ENTRIES.filter(e=>e.category==="data_matrix" && (e.meta||{}).n_obs!=null);
  dm.sort((a,b)=> (b.meta.n_obs||0)-(a.meta.n_obs||0));
  dm.slice(0,12).forEach(e=>{
    const m=e.meta;
    const p=el("span","kmpill");
    p.appendChild(el("span","g","▦"));
    p.appendChild(document.createTextNode(" "+e.path.split("/").pop()+" "));
    p.appendChild(el("span","dim",compactNum(m.n_obs)+"×"+compactNum(m.n_vars)));
    p.title=e.path;
    p.addEventListener("click",()=>revealInTree(e.path));
    pills.appendChild(p);
  });
}

// ================= revealInTree (filter-reset guarded, §5) =================
function revealInTree(path){
  // Disambiguate the "<dir>/__dir__" directory sentinel from a real FILE whose
  // basename happens to be "__dir__": an entry existing at the exact path means it
  // IS a file (select it); only treat a trailing /__dir__ as the dir sentinel when
  // no such file entry exists. Avoids mis-routing a "__dir__"-named file's locate.
  const exact = ENTRIES.find(x=>x.path===path);
  const isDir = !exact && path.endsWith("/__dir__");
  const realPath = isDir ? path.slice(0, -("/__dir__".length)) : path;
  const e = isDir? null : exact;
  const cat = e? (e.category||"other") : null;
  const allOn = activeCats.size===Object.keys(CATS).length;
  const needReset = (searchEl.value.trim()!=="") || (cat && !activeCats.has(cat)) || (isDir && !allOn);
  if(needReset){
    searchEl.value="";
    document.getElementById("clr").classList.remove("show");
    activeCats.clear(); Object.keys(CATS).forEach(c=>activeCats.add(c));
    syncFilterChips();
    refresh();
    toast("filters cleared to reveal");
  }
  const segs = realPath.split("/").filter(Boolean);
  const dirSegs = isDir ? segs : segs.slice(0,-1);
  // walk + expand ancestors
  let container = treeEl.querySelector("ul");
  let dirRow=null;
  for(const seg of dirSegs){
    if(!container) break;
    const li = [...container.children].find(li2=>{
      const r=li2.querySelector(":scope > .row");
      return li2.classList.contains("dir") && r && r.dataset.dirname===seg;
    });
    if(!li){ container=null; break; }
    dirRow=li.querySelector(":scope > .row");
    if(li._ensureOpen) li._ensureOpen();
    container = li.querySelector(":scope > ul");
  }
  let leafRow=null;
  if(isDir){ leafRow=dirRow; }
  else if(container){
    leafRow=[...container.children]
      .map(li2=>li2.querySelector(":scope > .row"))
      .find(r=>r && r.dataset.path===realPath);
  }
  if(leafRow){
    setActive(leafRow);
    leafRow.scrollIntoView({block:"center"});
    if(!isDir) selectEntry(realPath, leafRow);
    return true;
  } else {
    toast("could not locate in tree");
    return false;
  }
}

// ================= keyboard nav (active vs selected split, §5) =================
function visibleRows(){
  return [...treeEl.querySelectorAll(".row")].filter(r=>r.offsetParent!==null);
}
function setActive(row){
  if(activeRow) activeRow.classList.remove("active");
  activeRow=row;
  if(row) row.classList.add("active");
}
function moveActive(delta){
  const rows=visibleRows(); if(!rows.length) return;
  let idx = activeRow? rows.indexOf(activeRow) : -1;
  idx = idx<0 ? (delta>0?0:rows.length-1) : idx+delta;
  if(idx<0) idx=0; if(idx>=rows.length) idx=rows.length-1;
  const r=rows[idx]; setActive(r);
  r.scrollIntoView({block:"nearest"});
}
function activeEntry(){
  if(!activeRow || activeRow.dataset.kind!=="file") return null;
  return ENTRIES.find(x=>x.path===activeRow.dataset.path);
}
function liOfRow(row){ return row? row.closest("li.dir") : null; }

let _gPending=false;
document.addEventListener("keydown",ev=>{
  const help=document.getElementById("help");
  if(help.classList.contains("open")){
    if(ev.key==="Escape"||ev.key==="?"){ help.classList.remove("open"); ev.preventDefault(); }
    return;
  }
  const inSearch = document.activeElement===searchEl;
  const k=ev.key;
  if(inSearch){
    if(k==="Escape"){
      if(searchEl.value){ searchEl.value=""; document.getElementById("clr").classList.remove("show"); refresh(); }
      else searchEl.blur();
      ev.preventDefault();
    } else if(k==="ArrowDown"){ searchEl.blur(); moveActive(1); ev.preventDefault(); }
    else if(k==="ArrowUp"){ searchEl.blur(); moveActive(-1); ev.preventDefault(); }
    else if(k==="Enter"){
      searchEl.blur();
      const rows=visibleRows(); if(rows.length){ setActive(rows[0]);
        if(rows[0].dataset.kind==="file") selectEntry(rows[0].dataset.path, rows[0]); }
      ev.preventDefault();
    }
    return;
  }
  if(ev.metaKey || ev.ctrlKey){
    if(k==="f"||k==="F"){ ev.preventDefault(); searchEl.focus(); searchEl.select(); }
    return;
  }
  // Ignore plain-letter shortcuts while typing in any field (e.g. the obs filter).
  const _ae=document.activeElement;
  if(_ae && /^(INPUT|TEXTAREA|SELECT)$/.test(_ae.tagName)) return;
  switch(k){
    case "/": ev.preventDefault(); searchEl.focus(); searchEl.select(); break;
    case "Escape":
      document.querySelectorAll(".row.sel").forEach(r=>r.classList.remove("sel"));
      selectedPath=null; inspectorEmpty(); ev.preventDefault(); break;
    case "ArrowDown": case "j": moveActive(1); ev.preventDefault(); break;
    case "ArrowUp": case "k": moveActive(-1); ev.preventDefault(); break;
    case "ArrowRight": {
      if(!activeRow) break; ev.preventDefault();
      if(activeRow.dataset.kind==="dir"){
        const li=liOfRow(activeRow);
        if(li && li.classList.contains("collapsed")){ li._toggle(); }
        else { moveActive(1); }
      } else {
        // file → load + focus the inspector pane (matches help/statusbar text)
        selectEntry(activeRow.dataset.path, activeRow);
        const right=document.getElementById("right"); if(right) right.focus();
      }
      break;
    }
    case "ArrowLeft": {
      if(!activeRow) break; ev.preventDefault();
      if(activeRow.dataset.kind==="dir"){
        const li=liOfRow(activeRow);
        if(li && !li.classList.contains("collapsed")){ li._toggle(); break; }
      }
      const li2=activeRow.closest("li"); const pul=li2? li2.parentElement:null;
      const pli=pul? pul.closest("li.dir"):null;
      if(pli){ const pr=pli.querySelector(":scope > .row"); setActive(pr); pr.scrollIntoView({block:"nearest"}); }
      break;
    }
    case "Enter": {
      if(!activeRow) break; ev.preventDefault();
      if(activeRow.dataset.kind==="dir"){ const li=liOfRow(activeRow); if(li)li._toggle(); }
      else selectEntry(activeRow.dataset.path, activeRow);
      break;
    }
    case " ": {
      if(activeRow && activeRow.dataset.kind==="dir"){ ev.preventDefault(); const li=liOfRow(activeRow); if(li)li._toggle(); }
      break;
    }
    case "y": { const p=activeRow&&activeRow.dataset.path; if(p){ copyText(absOf(p),"absolute path"); ev.preventDefault(); } break; }
    case "Y": { const p=activeRow&&activeRow.dataset.path; if(p){ copyText(p,"relative path"); ev.preventDefault(); } break; }
    case "o": { const p=activeRow&&activeRow.dataset.path; if(p){ revealInFinder(p); ev.preventDefault(); } break; }
    case "s": cycleSort(); ev.preventDefault(); break;
    case "\\": { if(activeRow && activeRow.dataset.cat){ setCats(new Set([activeRow.dataset.cat])); } ev.preventDefault(); break; }
    case "a": setCats(new Set(Object.keys(CATS))); ev.preventDefault(); break;
    case "n": setCats(new Set()); ev.preventDefault(); break;
    case "d": PREFS.dateAbs=!PREFS.dateAbs; refresh(); toast(PREFS.dateAbs?"dates: absolute":"dates: relative"); ev.preventDefault(); break;
    case "z": toggleDensity(); ev.preventDefault(); break;
    case "t": toggleTheme(); ev.preventDefault(); break;
    case "r": doRefresh(); ev.preventDefault(); break;
    case "f": toggleGroup(); ev.preventDefault(); break;
    case "b": showBrowse(); ev.preventDefault(); break;
    case "h": showHealth(); ev.preventDefault(); break;
    case "g": {
      ev.preventDefault();
      if(_gPending){ _gPending=false; const rows=visibleRows(); if(rows.length){ setActive(rows[0]); rows[0].scrollIntoView({block:"center"});} }
      else { _gPending=true; setTimeout(()=>{_gPending=false;},600); }
      break;
    }
    case "G": { ev.preventDefault(); const rows=visibleRows(); if(rows.length){ const r=rows[rows.length-1]; setActive(r); r.scrollIntoView({block:"center"}); } break; }
    case "?": ev.preventDefault(); help.classList.add("open"); break;
  }
});

// ================= SORT (menu + cycle + ruler) =================
const SORTS=[["name","Name"],["newest","Newest"],["oldest","Oldest"],
             ["largest","Largest"],["smallest","Smallest"],["type","Type"]];
function sortLabel(){ const s=SORTS.find(x=>x[0]===sortKey); return s?s[1]:"Name"; }
function applySort(){
  const btn=document.getElementById("sortLabel"); if(btn) btn.textContent=sortLabel();
  const rsz=document.getElementById("rSize"), rmt=document.getElementById("rMod");
  rsz.classList.remove("act"); rmt.classList.remove("act");
  const a1=rsz.querySelector(".arr"); if(a1) a1.remove();
  const a2=rmt.querySelector(".arr"); if(a2) a2.remove();
  if(sortKey==="largest"||sortKey==="smallest"){ rsz.classList.add("act");
    rsz.appendChild(el("span","arr",sortKey==="largest"?"▾":"▴")); }
  if(sortKey==="newest"||sortKey==="oldest"){ rmt.classList.add("act");
    rmt.appendChild(el("span","arr",sortKey==="newest"?"▾":"▴")); }
  refresh();
}
function cycleSort(){
  const i=SORTS.findIndex(x=>x[0]===sortKey);
  sortKey=SORTS[(i+1)%SORTS.length][0];
  applySort();
}

// ================= FILTER CHIPS =================
const filtersEl=document.getElementById("filters");
const chipByCat={};
function setCats(keep){
  activeCats.clear();
  Object.keys(CATS).forEach(c=>{ if(keep.has(c)) activeCats.add(c); });
  syncFilterChips();
  refresh();
}
function syncFilterChips(){
  Object.keys(chipByCat).forEach(c=>{
    const on=activeCats.has(c);
    chipByCat[c].classList.toggle("on",on);
    chipByCat[c].classList.toggle("off",!on);
  });
}
function fbtn(txt,title,fn){
  const b=document.createElement("button"); b.type="button"; b.className="filterbtn";
  b.textContent=txt; if(title) b.title=title; b.addEventListener("click",fn); return b;
}
filtersEl.appendChild(fbtn("all","show all categories",()=>setCats(new Set(Object.keys(CATS)))));
filtersEl.appendChild(fbtn("none","hide all categories",()=>setCats(new Set())));
Object.keys(CATS).sort().forEach(cat=>{
  const chip=el("span","filterchip on"); chip.style.setProperty("--cc",catVar(cat));
  chip.appendChild(el("span","g",glyphFor(cat)));
  chip.appendChild(document.createTextNode(" "+cat+" "));
  chip.appendChild(el("span","cnt",String(CATS[cat])));
  const only=el("span","only","only"); only.title="show only "+cat;
  only.addEventListener("click",ev=>{ ev.stopPropagation(); setCats(new Set([cat])); });
  chip.appendChild(only);
  chip.addEventListener("click",()=>{
    if(activeCats.has(cat)) activeCats.delete(cat); else activeCats.add(cat);
    syncFilterChips(); refresh();
  });
  chipByCat[cat]=chip;
  filtersEl.appendChild(chip);
});

// search wiring
searchEl.addEventListener("input",()=>{
  document.getElementById("clr").classList.toggle("show", searchEl.value.trim()!=="");
  refreshDebounced();
});
document.getElementById("clr").addEventListener("click",()=>{
  searchEl.value=""; document.getElementById("clr").classList.remove("show"); refresh(); searchEl.focus();
});

// sort menu
const sortBtn=document.getElementById("sortBtn");
const sortMenu=document.getElementById("sortMenu");
SORTS.forEach(([k,lab])=>{
  const o=el("div","opt",lab); o.dataset.k=k;
  o.addEventListener("click",()=>{ sortKey=k; sortMenu.classList.remove("open"); applySort(); });
  sortMenu.appendChild(o);
});
sortBtn.addEventListener("click",ev=>{
  ev.stopPropagation();
  sortMenu.querySelectorAll(".opt").forEach(o=>o.classList.toggle("cur",o.dataset.k===sortKey));
  const r=sortBtn.getBoundingClientRect();
  sortMenu.style.left=r.left+"px"; sortMenu.style.top=(r.bottom+2)+"px";
  sortMenu.classList.toggle("open");
});
document.addEventListener("click",()=>sortMenu.classList.remove("open"));

// ruler clickable headers
document.getElementById("rSize").addEventListener("click",()=>{ sortKey=(sortKey==="largest")?"smallest":"largest"; applySort(); });
document.getElementById("rMod").addEventListener("click",()=>{ sortKey=(sortKey==="newest")?"oldest":"newest"; applySort(); });

// ================= theme / density toggles =================
function toggleTheme(){
  PREFS.theme = PREFS.theme==="dark"?"light":"dark";
  if(PREFS.theme==="light") document.documentElement.setAttribute("data-theme","light");
  else document.documentElement.removeAttribute("data-theme");
}
function toggleDensity(){
  PREFS.density = PREFS.density==="compact"?"comfortable":"compact";
  if(PREFS.density==="comfortable") document.documentElement.setAttribute("data-density","comfortable");
  else document.documentElement.removeAttribute("data-density");
  refresh();
}
function toggleGroup(){
  PREFS.groupBy = PREFS.groupBy==="folder" ? "type" : "folder";
  const gl=document.getElementById("groupLabel"); if(gl) gl.textContent="group: "+PREFS.groupBy;
  refresh();
}
document.getElementById("groupBtn").addEventListener("click",toggleGroup);
document.getElementById("themeBtn").addEventListener("click",toggleTheme);
document.getElementById("helpBtn").addEventListener("click",()=>document.getElementById("help").classList.add("open"));
// Refresh: only possible when served over http (a static file:// page cannot
// re-walk the FS). POST /refresh runs the ~3s incremental build, then reload.
function doRefresh(indexColumns){
  const btn=document.getElementById("refreshBtn");
  if(btn.classList.contains("spin")) return;
  // Prefer the native pywebview bridge (the always-open app): its .refresh()
  // returns a Promise resolving to the Python {ok,...} dict. Fall back to the
  // http POST (repo_index serve), then a toast for a bare static file://.
  // Apply a CHANGED refresh in place when an entry-level delta is present (no
  // location.reload → keeps scroll + every open folder, no WebView memory ratchet).
  // Fall back to a reload only when the delta is absent (older build) or can't be
  // applied. The unchanged path never reloads; a static file:// page just toasts.
  function applyResult(r, kind){
    if(!(r && r.ok)){ toast("refresh failed"+(r&&r.error?": "+r.error:"")); btn.classList.remove("spin"); return; }
    // APP path (kind==="app"): the backend (app.Api.refresh) now computes the delta
    // against the PAGE'S OWN state (api._served), not the on-disk prior — so it ALWAYS
    // composes with our ENTRIES, even when an external process advanced the index. We
    // therefore TRUST it and reconcile in place with NO baseline check and NO routine
    // location.reload() — eliminating the reload that WebKit's never-recycled WebContent
    // process never reclaims (the measured over-days RAM ratchet). Reload survives only
    // as a last resort if reconcile() itself throws.
    if(kind==="app"){
      // Keep the columns-toggle state (DATA.index_columns) + its button + the open
      // inspector in sync with the EFFECTIVE setting the backend just reported.
      const _oldCols = DATA.index_columns!==false;
      if(typeof r.index_columns==="boolean"){ DATA.index_columns=r.index_columns; updateColsBtn(); }
      const _colsChanged = (typeof r.index_columns==="boolean") && (_oldCols!==(r.index_columns!==false));
      function _afterApply(){
        if(_colsChanged && selectedPath){
          const row=[...treeEl.querySelectorAll(".row")].find(rw=>rw.dataset.path===selectedPath && rw.dataset.kind==="file");
          selectEntry(selectedPath, row||null);   // re-render the inspector's COLUMNS card
        }
      }
      if(r.unchanged){ toast(_colsChanged?"columns updated":"up to date — no changes"); btn.classList.remove("spin"); _afterApply(); return; }
      if(r.delta && typeof r.delta==="object"){
        let ok=false;
        try{ ok=reconcile(r.delta); }catch(e){ ok=false; }
        if(ok){ if(typeof r.after==="string" && r.after) BASELINE_DIGEST=r.after; toast(_colsChanged?"columns re-indexed in place":"refreshed in place"); btn.classList.remove("spin"); _afterApply(); return; }
      }
      toast("refreshed — reloading…");
      setTimeout(()=>location.reload(), 300);
      return;
    }
    // HTTP path (repo_index serve): stateless — the delta is computed against the
    // ON-DISK prior (digest r.before), so it is only safe to apply when our baseline
    // matches that prior. On divergence (another process advanced the on-disk index)
    // r.before !== BASELINE_DIGEST and we reload to re-embed the fresh INDEX.html
    // rather than silently fall behind. (The app path above avoids this entirely.)
    const baselineMatches = (typeof r.before==="string") && r.before.length>0
                            && (r.before===BASELINE_DIGEST);
    if(r.unchanged && baselineMatches){ toast("up to date — no changes"); btn.classList.remove("spin"); return; }
    if(baselineMatches && r.delta && typeof r.delta==="object"){
      let ok=false;
      try{ ok=reconcile(r.delta); }catch(e){ ok=false; }
      if(ok){ if(typeof r.after==="string" && r.after) BASELINE_DIGEST=r.after; toast("refreshed in place"); btn.classList.remove("spin"); return; }
    }
    toast("refreshed — reloading…");
    setTimeout(()=>location.reload(), kind==="http"?350:300);
  }
  if(window.pywebview && window.pywebview.api && window.pywebview.api.refresh){
    btn.classList.add("spin");
    // indexColumns (from the cols toggle) flips the index_columns setting for this
    // build; undefined ⇒ a plain refresh that inherits the persisted setting.
    const p = (indexColumns===undefined) ? window.pywebview.api.refresh()
                                         : window.pywebview.api.refresh(indexColumns);
    p.then(r=>applyResult(r,"app"))
     .catch(e=>{ toast("refresh failed: "+e); btn.classList.remove("spin"); });
    return;
  }
  if(location.protocol==="http:"){
    btn.classList.add("spin");
    fetch("refresh",{method:"POST"}).then(r=>r.json())
      .then(j=>applyResult(j,"http"))
      .catch(e=>{ toast("refresh failed: "+e); btn.classList.remove("spin"); });
    return;
  }
  toast("Refresh needs the repo_index app or `repo_index serve`");
}
document.getElementById("refreshBtn").addEventListener("click",()=>doRefresh());
// ---- wide column-name indexing toggle (app only) ----
// Drops/restores the per-CSV/TSV/parquet `columns` lists (shrinks INDEX.json/.jsonl
// ~37%) without the terminal. A flip is a FULL re-extract (minutes on a large repo)
// and is STICKY: the backend persists it in config_used, so later plain refreshes
// inherit it. n_columns (the count) is always kept; obs_columns/var_columns too.
function updateColsBtn(){
  const b=document.getElementById("colsBtn"); if(!b) return;
  const on = DATA.index_columns!==false;
  b.classList.toggle("off", !on);
  b.title = on
    ? "wide column names: INDEXED — click to DROP them (smaller index; full re-extract, may take minutes)"
    : "wide column names: NOT indexed — click to RE-INDEX them (full re-extract, may take minutes)";
}
function toggleColumns(){
  if(!(window.pywebview && window.pywebview.api && window.pywebview.api.refresh)){
    toast("the columns toggle needs the repo_index app"); return;
  }
  const want = (DATA.index_columns===false);   // currently off → turn on; else turn off
  toast(want ? "re-indexing WITH column names — full rebuild, may take a few minutes…"
             : "dropping column names — full rebuild, may take a few minutes…");
  doRefresh(want);
}
document.getElementById("colsBtn").addEventListener("click",toggleColumns);
// Reveal a file in macOS Finder (open -R) via the local server. Needs `repo_index serve`.
function revealInFinder(relPath){
  // Prefer the native pywebview bridge (always-open app); fall back to the http
  // server (repo_index serve), then a toast for a bare static file://.
  if(window.pywebview && window.pywebview.api && window.pywebview.api.reveal){
    window.pywebview.api.reveal(relPath)
      .then(r=>{ toast(r&&r.ok?"revealed in Finder":"reveal failed"+(r&&r.error?": "+r.error:"")); })
      .catch(e=>toast("reveal failed: "+e));
    return;
  }
  if(location.protocol==="http:"){
    fetch("reveal",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:relPath})})
      .then(r=>r.json()).then(j=>{ if(j&&j.ok) toast("revealed in Finder"); else toast("reveal failed"+(j&&j.error?": "+j.error:"")); })
      .catch(e=>toast("reveal failed: "+e));
    return;
  }
  toast("Reveal in Finder needs the repo_index app or `repo_index serve`");
}
document.getElementById("help").addEventListener("click",ev=>{ if(ev.target.id==="help") ev.currentTarget.classList.remove("open"); });
document.getElementById("helpClose").addEventListener("click",()=>document.getElementById("help").classList.remove("open"));

// strip collapse
document.getElementById("stripLbl").addEventListener("click",()=>{
  document.getElementById("strip").classList.toggle("hidden");
});

// ================= Health view =================
function buildHealth(){
  const broken=ENTRIES.filter(e=>e.is_symlink && e.symlink_ok===false);
  const errs=ENTRIES.filter(e=>e.error);
  const host=document.getElementById("health");
  host.innerHTML="";
  const total=SUMMARY.total_files!=null?SUMMARY.total_files:ENTRIES.length;
  const nsym=SUMMARY.n_symlinks!=null?SUMMARY.n_symlinks:ENTRIES.filter(e=>e.is_symlink).length;
  const tiles=el("div","tiles");
  tiles.appendChild(tile(total,"total files"));
  tiles.appendChild(tile(nsym,"symlinks"));
  tiles.appendChild(tile(broken.length,"broken",broken.length>0));
  tiles.appendChild(tile(errs.length,"errors",errs.length>0));
  host.appendChild(tiles);
  if(!broken.length && !errs.length){
    host.appendChild(el("div","allclear","✓ no broken links, no errors"));
    return;
  }
  host.appendChild(el("div","sechdr","Broken symlinks ("+broken.length+")"));
  host.appendChild(healthTable(broken,["path","target"],"broken",e=>[e.path,e.symlink_target||""]));
  host.appendChild(el("div","sechdr","Errors ("+errs.length+")"));
  host.appendChild(healthTable(errs,["path","extractor","error"],"errc",e=>[e.path,e.extractor||"",e.error||""]));
}
function tile(n,label,isErr){
  const t=el("div","tile"+(isErr?" err":""));
  t.appendChild(el("div","n",typeof n==="number"?n.toLocaleString():String(n)));
  t.appendChild(el("div","l",label)); return t;
}
function healthTable(rows,headers,lastCls,cols){
  const t=document.createElement("table"); t.className="htable";
  const thead=document.createElement("thead"); const htr=document.createElement("tr");
  headers.forEach(h=>{ const th=document.createElement("th"); th.textContent=h; htr.appendChild(th); });
  htr.appendChild(document.createElement("th")); thead.appendChild(htr); t.appendChild(thead);
  const tb=document.createElement("tbody");
  if(!rows.length){
    const tr=document.createElement("tr"); const td=document.createElement("td");
    td.colSpan=headers.length+1; td.textContent="none"; td.style.color="var(--muted)";
    tr.appendChild(td); tb.appendChild(tr);
  } else for(const e of rows){
    const tr=document.createElement("tr");
    cols(e).forEach((v,i)=>{ const td=document.createElement("td");
      if(i===headers.length-1) td.className=lastCls;
      td.appendChild(el("code",null,v)); tr.appendChild(td); });
    const cpd=document.createElement("td"); const cp=el("span","pc","⧉"); cp.style.cursor="pointer";
    cp.addEventListener("click",ev=>{ ev.stopPropagation(); copyText(absOf(e.path),"path"); });
    cpd.appendChild(cp); tr.appendChild(cpd);
    tr.addEventListener("click",()=>{ selectEntry(e.path,null); });
    tb.appendChild(tr);
  }
  t.appendChild(tb); return t;
}

// ================= view switch (two-pane shell) =================
const browseTab=document.getElementById("tabBrowse");
const healthTab=document.getElementById("tabHealth");
const rulerEl=document.getElementById("ruler");
const healthEl=document.getElementById("health");
const stripEl=document.getElementById("strip");
// Health stays in the two-pane shell: Browse content (#ruler + #tree) and the
// Health tables (#health) swap WITHIN the left pane; the inspector (#right) is
// always live so row clicks in either view still populate it.
function showBrowse(){
  browseTab.classList.add("active"); healthTab.classList.remove("active");
  rulerEl.style.display=""; treeEl.style.display=""; healthEl.style.display="none";
  stripEl.style.display="flex";
}
function showHealth(){
  healthTab.classList.add("active"); browseTab.classList.remove("active");
  rulerEl.style.display="none"; treeEl.style.display="none"; healthEl.style.display="block";
  stripEl.style.display="none";
  buildHealth();
}
browseTab.addEventListener("click",showBrowse);
healthTab.addEventListener("click",showHealth);
document.getElementById("brokenChip").addEventListener("click",()=>{
  if(!document.getElementById("brokenChip").classList.contains("zero")) showHealth();
});
document.getElementById("headBtn").addEventListener("click",()=>copyText(DATA.git_commit||"","HEAD"));

// External reveal hook: the always-open app's file-watcher (app._start_reveal_watcher)
// calls this via window.evaluate_js when the OS (a Finder Quick Action → `repo_index
// reveal-in-app`) asks to jump to a path. `target` is a repo-RELATIVE path, or
// "<dir>/__dir__" for a directory. Switches to Browse, clears search/filters, then
// expands ancestors + scrolls/selects (revealInTree). Best-effort, never throws.
window.__revealFromExternal=function(target){
  try{ showBrowse(); return revealInTree(String(target||"")) ? "ok" : "notfound"; }
  catch(e){ return "error:"+((e&&e.message)||e); }
};

// ================= init =================
if(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches){
  PREFS.theme="light"; document.documentElement.setAttribute("data-theme","light");
}
buildStrip();
updateColsBtn();   // reflect the persisted index_columns state on the header toggle
applySort();   // sets sort label + ruler, then calls refresh()
inspectorEmpty();
"""


def render_html(manifest: Dict[str, Any]) -> str:
    """Return the full self-contained INDEX.html document as a string.

    Embeds ``manifest`` (projected via :func:`_project_manifest`) as a JSON blob
    inside an inline ``<script type="application/json">`` element (every ``<`` is
    escaped to ``\\u003c`` so the blob can never break out of the script), and
    ships inline CSS + JS implementing: a collapsible directory tree, a category
    filter, a client-side substring search over path + meta (incl. h5ad
    ``obs_columns`` / ``obsm`` keys), a click-to-inspect metadata panel, and a
    Health tab listing broken symlinks + extraction errors. No external
    resources. STDLIB-ONLY.
    """
    proj = _project_manifest(manifest)
    blob = _embed_json(proj)
    summary = proj.get("summary") or {}

    # ----- ALL header/strip facts DERIVED at render time (never hard-coded). -----
    entries = proj.get("entries") or []
    total_files = summary.get("total_files", len(entries))
    total_bytes = summary.get("total_bytes", 0)
    n_broken = summary.get("n_broken_symlinks", 0)
    n_errors = summary.get("n_errors", 0)
    by_ext = summary.get("by_ext") or {}
    root = proj.get("root") or ""
    git = proj.get("git_commit") or "(none)"
    git_short = str(git)[:7] if git and git != "(none)" else "(none)"
    gen = proj.get("generated_at") or ""
    digest = proj.get("content_digest") or ""

    def esc(s: Any) -> str:
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def fmt_bytes(n: int) -> str:
        try:
            v = float(n)
        except Exception:
            return str(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if v < 1024 or unit == "TB":
                return (f"{int(v)}{unit}" if unit == "B" else f"{v:.1f}{unit}")
            v /= 1024
        return f"{v:.1f}TB"

    def fmt_bytes_si(n: int) -> str:
        # Base-1000 (SI) so the "GB" label is numerically correct: the same byte
        # count that base-1024 reports as "GiB" must read in true GB here (the
        # headline total is what the spec's verified figure refers to). Value is
        # still DERIVED from total_bytes, never hard-coded.
        try:
            v = float(n)
        except Exception:
            return str(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if v < 1000 or unit == "TB":
                return (f"{int(v)} {unit}" if unit == "B" else f"{v:.1f} {unit}")
            v /= 1000
        return f"{v:.1f} TB"

    def grp(n: int) -> str:
        try:
            return f"{int(n):,}"
        except Exception:
            return str(n)

    # ext-breakdown counts for the stat strip — DERIVED from summary.by_ext.
    n_h5ad = by_ext.get("h5ad", 0)
    n_npz = by_ext.get("npz", 0)
    n_csv = by_ext.get("csv", 0)
    stat_strip = (
        f"{grp(total_files)} files · {esc(fmt_bytes_si(total_bytes))} · "
        f"{grp(n_h5ad)} h5ad · {grp(n_npz)} npz · {grp(n_csv)} csv"
    )

    # generated-time short form (best-effort; falls back to raw string).
    gen_short = gen
    try:
        # generated_at is ISO8601 with offset; show MM-DD HH:MM
        from datetime import datetime
        dt = datetime.fromisoformat(gen)
        gen_short = dt.strftime("%m-%d %H:%M")
    except Exception:
        gen_short = (gen[:16].replace("T", " ") if gen else "")

    broken_cls = "brokenchip" + ("" if (n_broken or 0) > 0 else " zero")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>repo_index — Locator</title>
<style>{_CSS}</style>
</head>
<body>
<div id="app">

  <div id="hdr">
    <span class="word"><span class="g">▦</span> repo_index</span>
    <span class="stat" title="{esc(root)}">{stat_strip}</span>
    <div class="right">
      <span id="brokenChip" class="{broken_cls}" title="broken symlinks (click → Health)">⚠ {grp(n_broken)} broken</span>
      <span id="headBtn" class="hmeta click" title="HEAD {esc(git)} (click copies full SHA)">HEAD {esc(git_short)}</span>
      <span class="hmeta" title="generated {esc(gen)}">gen {esc(gen_short)}</span>
      <div class="seg">
        <button id="tabBrowse" class="active">Browse</button>
        <button id="tabHealth">Health</button>
      </div>
      <button id="refreshBtn" class="iconbtn" title="refresh index (r) — needs `repo_index serve`">⟳</button>
      <button id="colsBtn" class="iconbtn" title="wide column-name indexing">⊞</button>
      <button id="themeBtn" class="iconbtn" title="toggle theme (t)">◐</button>
      <button id="helpBtn" class="iconbtn" title="keyboard help (?)">?</button>
    </div>
  </div>

  <div id="strip">
    <span id="stripLbl" class="lbl" title="collapse / expand">KEY MATRICES ▾</span>
    <div id="stripPills"></div>
  </div>

  <div id="toolbar">
    <div class="searchwrap">
      <span class="lead">⌕</span>
      <input id="search" type="text" autocomplete="off" spellcheck="false"
             placeholder="search…   ext:h5ad · cat:code · path:… · obs:…   (results grouped by type)">
      <span class="trail"><span id="scount" class="scount"></span><span id="clr" class="clr" title="clear (Esc)">⎋</span></span>
    </div>
    <div id="tbrow2">
      <button id="sortBtn" class="sortbtn" title="sort (s cycles)">↕ <span id="sortLabel">Newest</span> <span class="arr">▾</span></button>
      <button id="groupBtn" class="sortbtn" title="group search results: folder ↔ type (f)">⊟ <span id="groupLabel">group: folder</span></button>
      <div id="filters" class="filters" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;"></div>
    </div>
    <div id="crumb"></div>
  </div>
  <div id="sortMenu" class="sortmenu"></div>

  <div id="body">
    <div id="left" class="pane">
      <div id="ruler">
        <span></span><span></span>
        <span class="r-name">name</span>
        <span></span>
        <span id="rSize" class="r-sz">size</span>
        <span id="rMod" class="r-mt">modified</span>
        <span class="r-cp">⧉</span>
      </div>
      <div id="tree"></div>
      <div id="health"></div>
    </div>
    <div id="gutter"></div>
    <div id="right" class="pane" tabindex="-1">
      <div id="panel"></div>
    </div>
  </div>

  <div id="statusbar">
    <span id="statusFilter"></span>
    <span class="keys">/ search · ↑↓ move · → expand · ⏎ inspect · y copy · o reveal · s sort · ? help</span>
  </div>

</div>

<div id="toast"></div>
<div id="toastLive" aria-live="polite" style="position:fixed;left:-9999px;"></div>

<div id="help">
  <div class="box">
    <span id="helpClose" class="close">✕</span>
    <h3>Keyboard — repo_index Locator</h3>
    <div class="kg">
      <kbd>/</kbd><span>focus search · <kbd>Ctrl/⌘F</kbd> too</span>
      <kbd>Esc</kbd><span>clear search / blur / deselect</span>
      <kbd>↑↓ j k</kbd><span>move the <b>active</b> cursor (does not load inspector)</span>
      <kbd>→</kbd><span>expand dir / step into / focus inspector</span>
      <kbd>←</kbd><span>collapse dir / jump to parent</span>
      <kbd>Enter</kbd><span>file → load inspector · dir → toggle</span>
      <kbd>Space</kbd><span>toggle active dir</span>
      <kbd>y</kbd> / <kbd>Y</kbd><span>copy absolute / relative path</span>
      <kbd>s</kbd><span>cycle sort</span>
      <kbd>\\</kbd><span>solo active row's category</span>
      <kbd>a</kbd> / <kbd>n</kbd><span>all / none categories</span>
      <kbd>d</kbd><span>relative ↔ absolute dates</span>
      <kbd>z</kbd><span>compact ↔ comfortable density</span>
      <kbd>t</kbd><span>dark ↔ light theme</span>
      <kbd>b</kbd> / <kbd>h</kbd><span>Browse / Health</span>
      <kbd>g g</kbd> / <kbd>G</kbd><span>first / last visible row</span>
      <kbd>?</kbd><span>toggle this overlay</span>
    </div>
    <div class="note"><b>active</b> (cursor, accent bar) is decoupled from
      <b>selected</b> (the entry in the inspector). Arrowing the cursor never
      rebuilds the inspector — only Enter/click loads it. Prefs (theme/density/date)
      are in-memory only (no localStorage).</div>
  </div>
</div>

<script id="repo-index-data" type="application/json">{blob}</script>
<script>{_js()}</script>
</body>
</html>
<!-- digest: {esc(digest)} -->
"""
    return html


def write_html(manifest: Dict[str, Any], out_dir: Path) -> Path:
    """Render and write INDEX.html into ``out_dir``; return its path.

    Creates ``out_dir`` if needed (space-safe via pathlib). Writes UTF-8.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "INDEX.html"
    # Atomic publication (same rationale as manifest._atomic_write_text): render to
    # a temp sibling on the same volume then Path.replace, so the always-open app /
    # `repo_index open` never loads a half-written INDEX.html mid-freshen, and a
    # drive-yank mid-write leaves the previous complete page intact. Stale temp from
    # a prior hard-kill is cleared first; the temp is cleaned up on failure.
    tmp = out_path.with_name(out_path.name + ".tmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        tmp.write_text(render_html(manifest), encoding="utf-8")
        tmp.replace(out_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return out_path
