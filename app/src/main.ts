// Lens frontend — the locked liquid-glass FINDER wired to the frozen IPC surface (CONTRACT.md).
//
// Two top-level modes (#modeseg):
//   • BROWSE — a LAZY folder tree built once from list_page(0, ALL, 'path'). The whole repo is
//     fetched as a flat Row[] on boot and folded into a nested folder model; only EXPANDED
//     directories materialise DOM rows (the perf core for ~20–40k entries). Single click on a
//     dir row toggles it; single click on a file row inspects it. Per-folder ordering follows
//     the active sort key, computed client-side. A query NEVER flips the pane into another mode:
//     it FILTERS THE TREE IN PLACE (the tree is rebuilt from the surviving rows so folder
//     aggregates — and therefore "Newest" — mean newest MATCHING file), and a short ranked
//     TOP-HITS BAND (#topband) appears above it so the best 20 results need no clicking.
//   • HEALTH — a tiles view (total · symlinks · broken · errors) from facets() + the table count.
//
// Search itself lives in two PURE, unit-tested modules that know nothing about the DOM:
// `src/query.ts` (the one tokenizer, the filter predicate, the tree predicate, the band ranking)
// and `src/expand.ts` (the rendered-row budget behind "expand all here"). Only tier 3 — a token
// found in `meta`, which the Row DTO deliberately omits — needs the backend, via search_ids.
//
// RIGHT: a preview pane (image via <img src="repoindex://img/<id>">, markdown via an <iframe
// sandbox> fed repoindex://md/<id>) + an inspector of metadata cards (hero, Path, Dataset, Obs
// columns, Obsm, Structure, Lineage [clickable xrefs], Provenance) built from get_entry().
//
// MEMORY discipline mirrors the backend: list/search rows never parse meta; previews are
// disposable per-id fetches over the custom scheme (never base64 in JSON); the tree only ever
// holds DOM rows for the folders the user has opened.

import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { resolveResource } from "@tauri-apps/api/path";
import { open } from "@tauri-apps/plugin-dialog";
import { startDrag } from "@crabnebula/tauri-plugin-drag";

// The search engine. `query.ts` is the SOLE tokenizer in the app (the Rust side receives already
// split + folded tokens and parses nothing, so the two can never disagree); `expand.ts` owns the
// rendered-row budget. Both are pure and covered by `node --test` — keep the logic there, keep the
// DOM here.
import {
  BAND_SIZE,
  TIER3_DEBOUNCE_MS,
  buildCorpus,
  parseQuery,
  topHits,
  treeRows,
  type Corpus,
  type FilterKind,
  type ParsedQuery,
  type SortKey,
} from "./query";
import {
  HARD_CEILING,
  SOFT_BUDGET,
  expansionSurvives,
  planExpand,
  type ExpandNode,
} from "./expand";

// ── Contract DTOs (mirror src-tauri/src/db.rs — keep in lockstep with CONTRACT.md) ───────────
export interface Row {
  id: number;
  path: string;
  name: string;
  category: string;
  ext: string;
  size_bytes: number;
  mtime: string;
  n_obs: number | null;
  n_vars: number | null;
  extractor: string;
  error: string | null;
  symlink_ok: boolean;
  is_dir: boolean;
}
export interface SearchResult {
  rows: Row[];
  total: number;
}
export interface EntryDetail {
  row: Row;
  meta: unknown;
  refs: string[];
  ref_by: string[];
  // Subset of refs/ref_by whose target is NOT an entry in the index (archived/deleted/out-of-tree).
  // These render as the disabled .is-dangling xref (non-navigable) rather than a live link.
  dangling_refs: string[];
  dangling_ref_by: string[];
}
// Reply shape of `search_ids` (db::IdPath) — the top-hits band's tier 3. IDS + PATHS ONLY, never
// Rows: the payload is ~52× smaller than the equivalent `search_all` (88 KB vs 4,551 KB for
// "figure") and carries no `meta`, so the MEMORY rule holds. The frontend joins on PATH, because
// ids are sqlite rowids and churn across a reconcile.
export interface IdPath {
  id: number;
  path: string;
  /// True when every free token also appears in this row's figure text — i.e. the row would have
  /// been found by searching the figure's rendered text alone. Always false unless the caller
  /// opted in, so the badge can never appear on a default search.
  via_figure_text: boolean;
}
export interface Facet {
  key: string;
  count: number;
}
export interface Facets {
  categories: Facet[];
  exts: Facet[];
}
export interface ReindexReport {
  entries: number;
}
// A registered project (mirrors db::Project). EXACTLY one is resident at a time on the backend.
export interface Project {
  name: string;
  root: string;
}
// One row of the project switcher (`list_projects_status`). `Project` only says a folder is
// REGISTERED; this says whether it is still THERE — which is the whole point of the rebuilt
// dropdown: until now a folder that had been moved, renamed or unplugged rendered identically to
// one that works, and the only way to clear it was to hand-edit projects.json.
export interface ProjectStatus {
  name: string;
  root: string; // the registry key — pass THIS to every other project command
  is_active: boolean;
  folder_exists: boolean; // the folder itself is on disk right now
  has_index: boolean; // Lens has a catalogue for it (<root>/_repo_index/INDEX.sqlite)
  index_bytes: number; // how big that catalogue is; 0 when there is none
}
// Reply of `remove_project`. `now_active` is what to DISPLAY afterwards: a root the backend moved
// us to, or null meaning nothing is registered any more → the first-run screen.
export interface RemoveReport {
  removed: string;
  index_deleted: boolean;
  bytes_freed: number;
  now_active: string | null;
}
// Reply of `python_status` / `set_python_path`. `path: null` means no interpreter was found, so
// nothing can be indexed at all — the one failure that makes the app permanently empty.
// `source` ∈ "env" | "settings" | "probe" | "shell" | "none" — surfaced so the user can see WHICH
// Python won when several are installed.
export interface PythonStatus {
  path: string | null;
  version: string | null;
  source: string;
}
// Payload of the backend "index-progress" event (mirrors lib.rs::IndexProgress) — one indexer
// phase line tagged with the project root it belongs to (the overlay filters by `root`).
interface IndexProgress {
  root: string;
  line: string;
}

/// Payload of the watcher's post-flush `index-changed` event (`lib.rs` `IndexChanged`). `dirs` is
/// the list of changed entry keys — despite the name it contains FILE keys too, and carries no
/// is_dir flag, so it is not yet usable for a targeted per-directory refetch (see LIVE_INDEX_PLAN.md
/// track B). Today we only use `root` to filter, and treat the event as "something changed".
interface IndexChanged {
  root: string;
  dirs: string[];
}

/// The four states the live watch can be in, as reported by the backend. A STRING over IPC, so it
/// is narrowed at the boundary (`asHealth`) rather than trusted — an unknown word must degrade to
/// something honest, never throw in an event handler.
///   live        watching, and the folder is readable right now
///   paused      live updates are parked (the #livebtn hold)
///   unreachable the folder cannot be read — drive unplugged, renamed or ejected
///   stopped     there is no watcher at all (read-only degrade mode, or no folder open)
export type WatchHealth = "live" | "paused" | "unreachable" | "stopped";

/// Reply of `watch_status` (mirrors `lib.rs` `WatchStatus`).
///
/// `watching` + `root` are the original pair, and on their own they CANNOT describe the failure
/// this exists for: the watcher arms its OS-level watch exactly once, so when the drive is
/// unplugged the watch dies while the watcher OBJECT lives on — `watching` stays true, the window
/// keeps showing the catalogue it already had, and nothing on disk reaches it again. `health` and
/// `root_reachable` are what tell those two situations apart; `rearms` counts how many times the
/// watch has been re-established, which is the only way to see that a folder has been flapping.
export interface WatchStatus {
  watching: boolean;
  root: string;
  health: string; // one of WatchHealth — narrowed by asHealth()
  root_reachable: boolean;
  rearms: number;
  /// Why there is no live engine: "" when healthy, else "no_project" | "folder_unreachable" |
  /// "locked_by_other" | "other". "Updates off" alone cannot say WHICH, and the window used to
  /// name a second Lens window as the cause even when the drive was simply unplugged.
  degrade_code?: string;
  degrade_message?: string;
  /// Whether asking the backend to try again could plausibly work now.
  retryable?: boolean;
}

/// Payload of the backend "watch-health" event. Emitted ON EVERY TRANSITION of `health` and never
/// on a timer, so an arriving event always means the state genuinely changed.
interface WatchHealthEvent {
  root: string;
  health: string;
  rearms: number;
}

// ── Typed IPC bindings (1:1 onto the #[tauri::command]s) ─────────────────────────────────────
const api = {
  listPage: (offset: number, limit: number, sort: string) =>
    invoke<Row[]>("list_page", { offset, limit, sort }),
  listAll: () => invoke<Row[]>("list_all"),
  search: (query: string, offset: number, limit: number) =>
    invoke<SearchResult>("search", { query, offset, limit }),
  // NOTE: `search_all` is deliberately NOT bound here. It existed to feed the grouped-search view,
  // which is gone — tiers 0–2 now run client-side over state.corpus and tier 3 uses `search_ids`.
  // The Rust command stays frozen per CONTRACT.md; an unused binding is exactly the drift the
  // contract exists to prevent, so the binding goes and the command stays.
  // Tier 3 of the top-hits band. Takes ALREADY tokenized/folded tokens from query.ts (the backend
  // parses nothing), and the same ext/cat filters, and returns {id, path} for every row whose
  // haystack — path + category + ext + extractor + tags + META — contains every token.
  searchIds: (tokens: string[], exts: string[], cats: string[], includeFigureText: boolean) =>
    invoke<IdPath[]>("search_ids", {
      tokens,
      exts,
      cats,
      // The Rust side takes Option<bool>, so omitting this stays valid — pass it explicitly
      // anyway: an implicit default is exactly how a UI toggle silently stops being wired.
      includeFigureText,
    }),
  getEntry: (id: number) => invoke<EntryDetail>("get_entry", { id }),
  facets: () => invoke<Facets>("facets"),
  revealInFinder: (path: string) => invoke<void>("reveal_in_finder", { path }),
  openFile: (path: string) => invoke<void>("open_file", { path }),
  copyPath: (path: string, kind: "abs" | "rel" | "posix" | "file_uri") =>
    invoke<string>("copy_path", { path, kind }),
  reindex: () => invoke<ReindexReport>("reindex"),
  // The live watch's own state. Already on the backend before this window ever asked — which was
  // the problem: a status nobody calls cannot warn anybody. Polled at boot and after every project
  // switch; between those, the "watch-health" event does the talking.
  watchStatus: () => invoke<WatchStatus>("watch_status"),
  retryLiveEngine: () => invoke<boolean>("retry_live_engine"),
  // Multi-project surface (Phase-2 UI). The backend keeps EXACTLY ONE project resident; switch
  // repoints the single connection. `add_project` only registers (caller indexes + switches).
  listProjects: () => invoke<Project[]>("list_projects"),
  // The switcher's own list: same projects, plus whether each one is still reachable. `list_projects`
  // stays bound but unused by the menu — it is the flat shape the rest of the surface was frozen on.
  listProjectsStatus: () => invoke<ProjectStatus[]>("list_projects_status"),
  // `null` is a REAL answer here, not a failure: no folder is registered (first launch, or the user
  // just forgot the last one). It drives the first-run screen.
  currentProject: () => invoke<Project | null>("current_project"),
  addProject: (root: string, name?: string) =>
    invoke<Project>("add_project", { root, name: name ?? null }),
  switchProject: (root: string) => invoke<void>("switch_project", { root }),
  // Forgetting the ACTIVE folder is allowed now: the backend moves off it first (stopping the
  // watcher and releasing the writer lock) and reports where it landed in `now_active`.
  removeProject: (root: string, deleteIndex: boolean) =>
    invoke<RemoveReport>("remove_project", { root, deleteIndex }),
  // Runs the canonical indexer subprocess for an arbitrary root; emits "index-progress" per phase.
  indexProject: (root: string) => invoke<ReindexReport>("index_project", { root }),
  // Which Python the indexer will spawn, and how it was found. `path: null` = none, which is the
  // one condition under which "Add folder…" cannot possibly work.
  pythonStatus: () => invoke<PythonStatus>("python_status"),
  setPythonPath: (path: string) => invoke<PythonStatus>("set_python_path", { path }),
};

// ── Small DOM + format helpers ───────────────────────────────────────────────────────────────
function el<T extends HTMLElement = HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`[lens] missing required #${id}`);
  return node as T;
}
function basename(path: string): string {
  const i = path.lastIndexOf("/");
  return i >= 0 ? path.slice(i + 1) : path;
}
function extOf(path: string): string {
  const b = basename(path);
  const i = b.lastIndexOf(".");
  return i > 0 ? b.slice(i + 1).toLowerCase() : "";
}
function fmtBytes(n: number): string {
  if (!n || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let u = 0;
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024;
    u++;
  }
  const s = v >= 100 || u === 0 ? Math.round(v).toString() : v.toFixed(1);
  return `${s} ${units[u]}`;
}
function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US");
}
function fmtDate(iso: string): string {
  if (!iso) return "—";
  // mtime_iso is e.g. "2026-04-20T07:21:29Z" — show the date part.
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(iso);
  return m ? m[1] : iso;
}
// Compact "modified" like the design ("5h", "2d", "3w", "4mo") from an ISO mtime.
function fmtRel(iso: string): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return fmtDate(iso);
  const secs = (Date.now() - t) / 1000;
  if (secs < 60) return "now";
  const mins = secs / 60;
  if (mins < 60) return `${Math.round(mins)}m`;
  const hrs = mins / 60;
  if (hrs < 24) return `${Math.round(hrs)}h`;
  const days = hrs / 24;
  if (days < 7) return `${Math.round(days)}d`;
  const wks = days / 7;
  if (wks < 5) return `${Math.round(wks)}w`;
  const mos = days / 30;
  if (mos < 12) return `${Math.round(mos)}mo`;
  return `${Math.round(days / 365)}y`;
}
// Recency dot colour: green <24h, accent <7d, none older. Returns a CSS var or "".
function recencyDot(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const hrs = (Date.now() - t) / 36e5;
  if (hrs < 24) return "var(--ok)";
  if (hrs < 24 * 7) return "var(--accent)";
  return "";
}
function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const IMAGE_EXTS = new Set([
  "png",
  "jpg",
  "jpeg",
  "svg",
  "gif",
  "webp",
  "bmp",
  "tif",
  "tiff",
  "avif",
]);
const MD_EXTS = new Set(["md", "markdown", "mdx", "rst", "txt"]);

// ── Native file DRAG-OUT support ─────────────────────────────────────────────────────────────
// A file row is draggable onto Finder / Inkscape / any other app via the crabnebula drag plugin's
// startDrag(). The plugin's macOS side PANICS if the drag-preview `icon` is a path that isn't a
// valid NSImage, so we only ever hand it a real raster image: the dragged file itself when it is a
// safe raster format, else a small bundled fallback PNG (resolved once at boot). SVG is NOT in this
// set — NSImage can't decode an .svg from a file ref, so SVGs drag with the fallback icon.
const RASTER_ICON_EXTS = new Set(["png", "jpg", "jpeg", "gif", "tif", "tiff", "bmp"]);
let dragFallbackIcon = ""; // abs path to the bundled fallback drag icon (resolveResource at boot)

// Resolve a stored (repo-relative POSIX) path to an absolute on-disk path under the ACTIVE project
// root — mirrors the backend abs_of(): an already-absolute path is returned as-is. Empty string if
// the active root isn't known yet (the drag is then skipped rather than handing startDrag a bad path).
function absPathOf(rel: string): string {
  if (rel.startsWith("/")) return rel;
  if (!activeProjectRoot) return "";
  return `${activeProjectRoot.replace(/\/+$/, "")}/${rel}`;
}

// The 12 categories → their locked unicode glyph (foundation.html / tree.html). Unknown → "·".
const CATEGORY_GLYPH: Record<string, string> = {
  code: "❴",
  data_matrix: "▦",
  data_table: "▤",
  config: "⚙",
  doc: "¶",
  notebook: "◆",
  figure: "◑",
  figure_pdf: "⬚",
  model: "⬡",
  log: "≣",
  archive: "▢",
  other: "·",
};
function catGlyph(category: string): string {
  return CATEGORY_GLYPH[category] ?? "·";
}
// A category always maps to its own colour var; unknown → --c-other.
function catColorVar(category: string): string {
  return category in CATEGORY_GLYPH ? `var(--c-${category})` : "var(--c-other)";
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// VIEW STATE — the whole app is driven by this. Browse vs Health is the mode; the composed query
// NARROWS Browse's tree (it never switches it for a different list) and feeds the top-hits band.
// ═════════════════════════════════════════════════════════════════════════════════════════════

interface AppState {
  mode: "browse" | "health";
  query: string; // composed query (filter chips + free text) — display + parse source only
  parsed: ParsedQuery; // the ONE parse of `query`; tree, band and status all read it
  sort: SortKey;
  selectedId: number | null;
  allRows: Row[]; // every entry, fetched once on boot (list_all, path order)
  byId: Map<number, Row>;
  corpus: Corpus; // folded name/path per row — built once per load, matched per keystroke
  tier3Paths: Set<string>; // paths search_ids found in meta/tags; joined by PATH, never by id
  figureText: boolean; // the "figure text" opt-in (persisted); widens tier 3 to figure_text
  tier3FigurePaths: Set<string>; // subset of tier3Paths whose FIGURE TEXT carries the query
  tier3Gen: number; // monotonic; a reply from an older generation is discarded
  total: number; // table count (== allRows.length once loaded)
  symlinkOkCount: number;
  errorCount: number;
}

/// Persisted key for the "figure text" opt-in. A search toggle that silently resets on every
/// launch trains the user to distrust it, so it survives restarts. A corrupt/absent value reads
/// as OFF, which is also the no-cost default.
const FIGTEXT_KEY = "lens.searchFigureText";

function loadFigureTextPref(): boolean {
  try {
    return window.localStorage.getItem(FIGTEXT_KEY) === "1";
  } catch {
    return false; // private mode / storage disabled — degrade to the default, never throw
  }
}

const state: AppState = {
  mode: "browse",
  query: "",
  parsed: parseQuery(""),
  sort: "newest",
  selectedId: null,
  allRows: [],
  byId: new Map(),
  corpus: buildCorpus([]),
  tier3Paths: new Set(),
  figureText: loadFigureTextPref(),
  tier3FigurePaths: new Set(),
  tier3Gen: 0,
  total: 0,
  symlinkOkCount: 0,
  errorCount: 0,
};

const SORT_LABEL: Record<SortKey, string> = {
  name: "Name",
  newest: "Newest",
  oldest: "Oldest",
  largest: "Largest",
  smallest: "Smallest",
  type: "Type",
};
// Display order of the #sortbtn dropdown menu (Newest/Oldest/Name/Type/Largest/Smallest).
const SORT_CYCLE: SortKey[] = ["newest", "oldest", "name", "type", "largest", "smallest"];

// ── Shell elements (frozen ids from index.html) ─────────────────────────────────────────────
const listEl = el("list");
const bandEl = el("topband"); // the top-hits strip, immediately above #list in the same scroller
const filterChipsEl = el("filterchips"); // the controls-row slot holding the removable filter chips
const filtersEl = el("filters");
const searchEl = el<HTMLInputElement>("search");
const figToggleEl = el<HTMLInputElement>("figtoggle");
const inspectorEl = el("inspector");
const previewEl = el("preview");
const menuEl = el("rowmenu");
const statusCountEl = el("statuscount");
const statusHealthEl = el("statushealth");
const sortBtn = el("sortbtn");
const sortLabelEl = el("sortlabel");
const typesBtn = el("typesbtn");
const refreshBtn = el<HTMLButtonElement>("refresh");
const modeSeg = el("modeseg");
const rulerEl = el("ruler");
const appEl = el("app");
const welcomeEl = el("welcome"); // the first-run scrim (hidden unless no folder is registered)
const welcomePickBtn = el<HTMLButtonElement>("welcomepick");
const welcomePyEl = el("welcomepython"); // the Python notice slot inside the first-run card

// ═════════════════════════════════════════════════════════════════════════════════════════════
// FOLDER TREE MODEL — fold the flat Row[] into a nested directory model. Files live on leaves;
// directories carry an open/closed flag. We never build DOM for closed dirs.
// ═════════════════════════════════════════════════════════════════════════════════════════════

interface TreeDir {
  name: string; // segment name ("" for root)
  path: string; // full dir path ("" for root)
  dirs: Map<string, TreeDir>;
  files: Row[];
  fileCount: number; // recursive file total (for the dir-row count badge)
  open: boolean;
  // Recursive aggregates over the whole subtree — drive folder ordering so a folder sorts by its
  // CONTENT, not its own name (e.g. under "newest" the folder holding the most-recent file rises).
  newestMs: number; // max Date.parse(mtime) over all descendant files (0 if none)
  oldestMs: number; // min Date.parse(mtime) over all descendant files (0 if none)
  totalBytes: number; // sum of descendant file sizes
}

function newDir(name: string, path: string): TreeDir {
  return {
    name,
    path,
    dirs: new Map(),
    files: [],
    fileCount: 0,
    open: false,
    newestMs: 0,
    oldestMs: 0,
    totalBytes: 0,
  };
}

let treeRoot: TreeDir = newDir("", "");
// Open-state survives rebuilds: remember which dir paths the user expanded.
const openDirs = new Set<string>();
// The subset of `openDirs` that a BUDGETED expand opened, rather than the user's own clicks. A plan
// is only valid for the row set it was costed against: `outputs` budgeted to 8,000 png rows becomes
// 21,899 rows the moment the png filter comes off, and buildTree would faithfully re-open all 1,804
// of those dirs with no plan in sight. So applyQuery drains this set — a hand-opened folder survives
// a filter change (it costs one row), a planned expansion does not.
const expandOpened = new Set<string>();

function buildTree(rows: Row[]): void {
  treeRoot = newDir("", "");
  for (const r of rows) {
    const segs = r.path.split("/").filter((s) => s.length > 0);
    if (segs.length === 0) continue;
    let cur = treeRoot;
    // All but the last segment are directories; the last is the file leaf.
    for (let i = 0; i < segs.length - 1; i++) {
      const seg = segs[i];
      let child = cur.dirs.get(seg);
      if (!child) {
        const dpath = cur.path ? `${cur.path}/${seg}` : seg;
        child = newDir(seg, dpath);
        cur.dirs.set(seg, child);
      }
      cur = child;
    }
    cur.files.push(r);
  }
  // Recursive file counts + recency/size aggregates + restore open flags (post-order: children
  // are tallied first, then folded into the parent's aggregates).
  const tally = (d: TreeDir): number => {
    let n = d.files.length;
    let newest = 0;
    let oldest = Number.POSITIVE_INFINITY;
    let bytes = 0;
    for (const f of d.files) {
      const t = Date.parse(f.mtime) || 0;
      if (t > newest) newest = t;
      if (t < oldest) oldest = t;
      bytes += f.size_bytes;
    }
    for (const sub of d.dirs.values()) {
      n += tally(sub);
      if (sub.newestMs > newest) newest = sub.newestMs;
      if (sub.oldestMs < oldest && sub.oldestMs > 0) oldest = sub.oldestMs;
      bytes += sub.totalBytes;
    }
    d.fileCount = n;
    d.newestMs = newest;
    d.oldestMs = Number.isFinite(oldest) ? oldest : 0;
    d.totalBytes = bytes;
    d.open = openDirs.has(d.path);
    return n;
  };
  tally(treeRoot);
  // Open on a FULLY COLLAPSED tree: only the top-level folder rows show, nothing expanded (a fresh
  // Finder window). We deliberately do NOT force-open the root's children here — `.open` is restored
  // purely from `openDirs` in the tally above, so the only expanded folders are ones the user (or a
  // locate warp) explicitly opened this session.
}

// Per-folder ordering follows the active sort key (computed client-side).
function sortFiles(files: Row[]): Row[] {
  const arr = files.slice();
  const k = state.sort;
  arr.sort((a, b) => {
    switch (k) {
      case "name":
        return a.name.localeCompare(b.name, "en-US");
      case "newest":
        return (Date.parse(b.mtime) || 0) - (Date.parse(a.mtime) || 0);
      case "oldest":
        return (Date.parse(a.mtime) || 0) - (Date.parse(b.mtime) || 0);
      case "largest":
        return b.size_bytes - a.size_bytes;
      case "smallest":
        return a.size_bytes - b.size_bytes;
      case "type":
        return (
          a.category.localeCompare(b.category, "en-US") ||
          a.name.localeCompare(b.name, "en-US")
        );
    }
  });
  return arr;
}
// Folder ordering FOLLOWS the active sort key, by the folder's recursive content (not its name):
//   newest/oldest → by the subtree's newest/oldest file mtime (so the folder holding the most
//   recently modified item rises to the top — matches the old viewer); largest/smallest → by total
//   subtree bytes; name/type → alphabetical (folders carry no category). Ties break alphabetically.
function sortDirs(dirs: TreeDir[]): TreeDir[] {
  const byName = (a: TreeDir, b: TreeDir): number => a.name.localeCompare(b.name, "en-US");
  return dirs.slice().sort((a, b) => {
    switch (state.sort) {
      case "newest":
        return b.newestMs - a.newestMs || byName(a, b);
      case "oldest":
        return a.oldestMs - b.oldestMs || byName(a, b);
      case "largest":
        return b.totalBytes - a.totalBytes || byName(a, b);
      case "smallest":
        return a.totalBytes - b.totalBytes || byName(a, b);
      default:
        return byName(a, b);
    }
  });
}

// ── Row markup builders (DOM contract: index.html + tree.html) ───────────────────────────────

// Build a file <div class="row"> per the contract. depth controls --ind for indentation.
function buildFileRow(r: Row, depth: number): HTMLElement {
  const node = document.createElement("div");
  node.className = depth > 0 ? "row indented" : "row";
  node.style.setProperty("--cc", catColorVar(r.category));
  if (depth > 1) node.style.setProperty("--ind", `${depth * 16}px`);
  if (r.error) node.classList.add("broken");
  node.dataset.id = String(r.id);
  node.dataset.path = r.path;
  node.dataset.cat = r.category;
  node.dataset.ext = r.ext || extOf(r.path); // dragstart picks the preview icon by extension
  node.draggable = true; // native file drag-out (delegated dragstart handler on #list)
  if (r.id === state.selectedId) node.classList.add("sel");

  // Shape cell. Matrix files carry cells × genes, tables rows × columns — both
  // now land in the same two denormalized columns, so one branch serves both and
  // spreadsheets stop showing a blank cell next to a csv that shows one.
  const dims =
    (r.category === "data_matrix" || r.category === "data_table") && r.n_obs !== null
      ? `${fmtNum(r.n_obs)}<span class="x">×</span>${r.n_vars ?? "—"}`
      : "";
  const szClass = r.size_bytes >= 1e9 ? "sz heavy" : "sz";
  const dotVar = recencyDot(r.mtime);
  const dotHtml = dotVar ? `<i class="dot" style="background:${dotVar}"></i>` : "";

  node.innerHTML =
    `<span class="rail"></span><span class="tw"></span>` +
    `<span class="glyph">${catGlyph(r.category)}</span>` +
    `<span class="nm" title="${esc(r.path)}">${esc(r.name)}</span>` +
    `<span class="dims">${dims}</span>` +
    `<span class="${szClass}">${esc(r.size_bytes > 0 ? fmtBytes(r.size_bytes) : "—")}</span>` +
    `<span class="mt">${dotHtml}${esc(fmtRel(r.mtime))}</span>` +
    `<span class="rowacts">` +
    `<span class="a" data-act="copy" title="copy path">⧉</span>` +
    `<span class="a" data-act="reveal" title="reveal in Finder">⤤</span></span>`;
  return node;
}

// Build a directory <div class="row dir"> header per the contract.
//
// Two slots change meaning when the app is doing something the user has to be told about:
//   .cnt      — with a filter on, d.fileCount counts only what SURVIVED the filter, so the badge
//               prints both numbers ("412 of 3,180") rather than silently changing meaning (U8).
//   .nm .path — an already-styled, otherwise-unused slot; it carries the "expand all here"
//               truncation notice for THIS folder ("1,482 of 12,431 files shown · show all").
function buildDirRow(d: TreeDir, depth: number): HTMLElement {
  const node = document.createElement("div");
  node.className = depth > 0 ? "row dir indented" : "row dir";
  if (depth > 1) node.style.setProperty("--ind", `${depth * 16}px`);
  node.dataset.path = d.path;
  if (d.open) node.dataset.open = "";
  const twisty = d.open ? "▾" : "▸";

  const shown = d.fileCount.toLocaleString("en-US");
  const total = dirTotals.get(d.path) ?? d.fileCount;
  // Only when they actually differ: "3,180 of 3,180" is noise, and a folder the filter did not
  // touch is honestly reported by a single number.
  const cnt = queryIsActive() && total !== d.fileCount
    ? `${shown} of ${total.toLocaleString("en-US")}`
    : shown;

  const notice = expandNotices.get(d.path);
  const noticeHtml = notice
    ? `<span class="path"> ${notice.files.toLocaleString("en-US")} of ` +
      `${notice.totalFiles.toLocaleString("en-US")} files shown · ` +
      (notice.forced
        ? `too big to open at once`
        : `<span class="more">show all</span>`) +
      `</span>`
    : "";

  node.innerHTML =
    `<span class="tw">${twisty}</span><span></span>` +
    `<span class="nm">${esc(d.name)}${noticeHtml}</span>` +
    `<span class="cnt">${cnt}</span>` +
    `<span class="dir-acts"><span class="a" data-act="expand" title="expand all here">⇊</span>` +
    `<span class="a" data-act="locate" title="locate">⌖</span>` +
    `<span class="a" data-act="copy" title="copy path">⧉</span>` +
    `<span class="a" data-act="reveal" title="reveal in Finder">⤤</span></span>`;
  return node;
}

// LAZY render: walk the tree, emitting DOM only for open dirs (and the files within them).
function renderTree(): void {
  listEl.innerHTML = "";
  const frag = document.createDocumentFragment();
  const walk = (d: TreeDir, depth: number): void => {
    // Subdirectories first (alpha), then files (by active sort) — folders read as a band.
    for (const sub of sortDirs([...d.dirs.values()])) {
      frag.appendChild(buildDirRow(sub, depth));
      if (sub.open) walk(sub, depth + 1);
    }
    for (const f of sortFiles(d.files)) {
      frag.appendChild(buildFileRow(f, depth));
    }
  };
  walk(treeRoot, 0);
  listEl.appendChild(frag);
  if (listEl.childElementCount === 0) {
    listEl.innerHTML = emptyTreeHtml();
  }
}

// The three ways the tree can come up empty read completely differently to someone who is not an
// engineer, so each says what happened AND offers the one click that undoes it (U7). A blank pane
// is indistinguishable from a broken app.
function emptyTreeHtml(): string {
  const q = state.parsed;
  if (state.total > 0 && q.free.length > 0) {
    // The tree can be empty while the band is full: tier 3 matched inside the files' recorded
    // contents, which no path contains, so treeRows legitimately returns nothing. "Nothing matches"
    // printed directly beneath a list of matches reads as a bug to anyone who is not an engineer —
    // so when the band has rows, say where they came from instead of denying them.
    const banded = bandEl.hidden ? 0 : bandEl.querySelectorAll(".row").length;
    if (banded > 0) {
      return (
        `<p class="lens-empty">No file or folder is <i>named</i> “${esc(searchEl.value.trim())}”. ` +
        `The ${banded} match${banded === 1 ? "" : "es"} above were found inside file contents. ` +
        `<span class="reidx" data-clear="all">Clear the search</span></p>`
      );
    }
    return (
      `<p class="lens-empty">Nothing matches “${esc(searchEl.value.trim())}”. ` +
      `<span class="reidx" data-clear="all">Clear the search</span></p>`
    );
  }
  if (state.total > 0 && q.filters.size > 0) {
    return (
      `<p class="lens-empty">No ${esc(filterWord())} files in this project. ` +
      `<span class="reidx" data-clear="filters">Show all types</span></p>`
    );
  }
  // No project resident at all — the backend is parked on the empty placeholder index. "No entries."
  // there is a lie by omission: there is no folder to have entries IN. (Normally the first-run
  // scrim covers this pane; it is still what shows behind a dismissed one.)
  if (!activeProjectRoot) {
    return (
      `<p class="lens-empty">No folder is open yet. ` +
      `<span class="reidx" data-pick="1">Choose a folder to index</span></p>`
    );
  }
  return `<p class="lens-empty">No entries.</p>`;
}

// ── Unfiltered per-folder file totals ────────────────────────────────────────────────────────
// The live tree is rebuilt from the FILTERED rows, so it no longer knows what it dropped — but a
// filtered dir row has to say "412 of 3,180". One pass over the full row set at load time gives
// every ancestor folder its unfiltered total (~370k increments over this repo; a few ms).
const dirTotals = new Map<string, number>();
function buildDirTotals(rows: Row[]): void {
  dirTotals.clear();
  for (const r of rows) {
    const segs = r.path.split("/");
    let acc = "";
    for (let i = 0; i < segs.length - 1; i++) {
      acc = acc ? `${acc}/${segs[i]}` : segs[i];
      dirTotals.set(acc, (dirTotals.get(acc) ?? 0) + 1);
    }
  }
}

// ── "Expand all here" — the adaptive rendered-row budget (SPEC §2.2 / D2) ────────────────────
// Per-folder truncation notices, keyed by the CLICKED folder's path. Never stamped on the
// descendants the walk stopped at: the user never asked about those and would read it as an error.
interface ExpandNotice {
  files: number; // file rows actually opened
  totalFiles: number; // files in the whole subtree
  forced: boolean; // this plan already ran at HARD_CEILING — there is no bigger budget to offer
}
const expandNotices = new Map<string, ExpandNotice>();

// TreeDir → the DOM-free view expand.ts plans over. Children are flattened through sortDirs so the
// walk descends in DISPLAY order and the user's active sort is never silently reordered.
function toExpandNode(d: TreeDir): ExpandNode {
  return {
    path: d.path,
    ownFiles: d.files.length, // already post-filter: the tree was rebuilt from the filtered rows
    dirs: sortDirs([...d.dirs.values()]).map(toExpandNode),
  };
}

// Open as much of `path` as fits a rendered-row budget, then SAY what was left. There is no
// unbounded mode: drawing this repo's largest folder in full is a measured 85.6 s freeze, so
// planExpand caps every path at HARD_CEILING. `force` (the visible "show all" word, or an
// option-click on ⇊) RAISES the budget from SOFT_BUDGET to HARD_CEILING — it never removes it.
function expandAllHere(path: string, force: boolean): void {
  const d = dirByPath(path);
  if (!d) return;
  const plan = planExpand(toExpandNode(d), force ? HARD_CEILING : SOFT_BUDGET);
  for (const p of plan.open) {
    // Claim plan-ownership only for dirs the plan actually OPENED. One the user had already opened
    // by hand is theirs, and must survive a filter change — otherwise expanding here would quietly
    // repossess folders they opened themselves.
    if (!openDirs.has(p)) expandOpened.add(p);
    openDirs.add(p);
    // openDirs alone is not enough: renderTree only descends into dirs whose live TreeDir.open is
    // true, and that flag is otherwise only restored by buildTree.
    const sub = dirByPath(p);
    if (sub) sub.open = true;
  }
  if (plan.truncated) {
    expandNotices.set(path, { files: plan.files, totalFiles: plan.totalFiles, forced: force });
  } else {
    expandNotices.delete(path);
  }
  renderTree();
}

// Find a TreeDir by its full path (root = "").
function dirByPath(path: string): TreeDir | null {
  if (path === "") return treeRoot;
  const segs = path.split("/").filter((s) => s.length > 0);
  let cur = treeRoot;
  for (const seg of segs) {
    const next = cur.dirs.get(seg);
    if (!next) return null;
    cur = next;
  }
  return cur;
}

function toggleDir(path: string): void {
  const d = dirByPath(path);
  if (!d) return;
  d.open = !d.open;
  // Either way the notice retires. Closing it: a notice on a closed folder describes nothing on
  // screen. OPENING it: a hand-toggle draws every direct child unbudgeted, so a folder whose plan
  // opened nothing (its own children already blow the budget — e.g. target/debug/deps, 2,058 direct
  // files) would otherwise keep reading "0 of 2,058 files shown" while all 2,058 are on screen.
  expandNotices.delete(path);
  // A hand-toggle transfers ownership to the user either way: opening it makes it theirs, closing it
  // means the plan's claim is spent. Either way it stops being plan-owned.
  expandOpened.delete(path);
  if (d.open) openDirs.add(path);
  else openDirs.delete(path);
  // renderTree() rebuilds every row, detaching the node activeRow points at. If the user was
  // navigating by keyboard (Enter / ←/→ on this folder), re-pin the cursor to the just-toggled
  // folder afterwards — otherwise the next ↑/↓ finds no active row in the fresh list (indexOf →
  // -1) and snaps to the top of the tree. Only when a cursor already existed, so a pure mouse
  // toggle introduces no keyboard cursor.
  const hadCursor = activeRow !== null;
  renderTree();
  if (hadCursor) {
    const node = listEl.querySelector<HTMLElement>(`.row.dir[data-path="${cssEscape(path)}"]`);
    if (node) setActiveRow(node);
  }
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// TOP-HITS BAND (#topband) — the short ranked strip above the tree.
//
// It replaces the retired grouped-search mode outright, because that mode WAS the reported bug:
// it ordered folder groups by relevance tier first, so a filter-only query ("ext:png") — which has
// no free text and therefore no relevance at all — collapsed onto DEPTH, and the newest PNG in the
// repo landed in group 392 of 1,244. The tree now simply narrows in place, and this band puts the
// best 20 rows on screen in the FIRST paint with nothing to click.
//
// It renders whenever the query is non-empty:
//   • filters only → the first BAND_SIZE rows in the ACTIVE SORT order, headed "Newest 20 png".
//   • free tokens  → "Best matches": folder-name hits (tier 0, max 3), then filename hits (1),
//                    then path hits (2), then meta hits (3 — supplied asynchronously by search_ids).
// All ranking lives in query.ts `topHits`; everything below only paints what it returns.
// ═════════════════════════════════════════════════════════════════════════════════════════════

// Folder paths of the CURRENT (filtered) tree — the tier-0 candidate set. Recomputed by applyQuery
// after every buildTree, so a folder whose files were all filtered away can never be offered.
let treeDirPaths: string[] = [];
function collectDirPaths(d: TreeDir, out: string[]): string[] {
  for (const sub of d.dirs.values()) {
    out.push(sub.path);
    collectDirPaths(sub, out);
  }
  return out;
}

function queryIsActive(): boolean {
  return state.parsed.free.length > 0 || state.parsed.filters.size > 0;
}

// The word that names what is being counted. "png" only when a single ext chip is the ONLY thing
// narrowing the set — otherwise no one word is honest, so we say "matching"/"files".
function filterWord(): string {
  const q = state.parsed;
  if (q.free.length > 0) return "matching";
  if ((q.filters.get("cat")?.size ?? 0) > 0) return "matching";
  const exts = q.filters.get("ext");
  if (exts && exts.size === 1) return [...exts][0];
  return "matching";
}

// Band heading for a pure filter: it names the SORT, so "Newest" visibly means newest png.
function bandHeading(n: number): string {
  const w = filterWord();
  const word = w === "matching" ? "files" : w;
  switch (state.sort) {
    case "name":
      return `First ${n} ${word} (A–Z)`;
    case "type":
      return `First ${n} ${word} (by type)`;
    default:
      return `${SORT_LABEL[state.sort]} ${n} ${word}`;
  }
}

function renderBand(): void {
  const q = state.parsed;
  bandEl.innerHTML = "";

  // A bare operator ("ext:" with nothing after it) narrows nothing. The backend's 0-row guard for
  // that case is a BACKEND rule and must never leak into the tree — so the tree stays full and the
  // band says what is missing instead.
  if (q.dangling.length > 0 && q.free.length === 0 && q.filters.size === 0) {
    bandEl.hidden = false;
    bandEl.innerHTML =
      `<p class="hint">finish the filter — type a value after <b>${esc(q.dangling[0])}:</b></p>`;
    return;
  }
  if (!queryIsActive()) {
    bandEl.hidden = true;
    return;
  }

  const hits = topHits(state.corpus, q, state.tier3Paths, treeDirPaths, state.sort, BAND_SIZE);
  if (hits.length === 0) {
    bandEl.hidden = true; // the tree's own empty state carries the explanation
    return;
  }

  const head = document.createElement("div");
  head.className = "band-h";
  const title = q.free.length > 0 ? "Best matches" : bandHeading(hits.length);
  head.innerHTML = `<span class="band-title">${esc(title)}</span>`;
  bandEl.appendChild(head);

  for (const h of hits) {
    if (h.row !== null) {
      // Full path, always: the band is a flat jump list, so an indented basename would be ambiguous.
      bandEl.appendChild(buildSearchRow(h.row as Row, true));
      continue;
    }
    // Tier 0 — the folder's OWN name matched. Rendered as a dir row so it reads exactly like the
    // tree, but it does not toggle: clicking warps the tree open at that folder (see wireBandEvents).
    const d = h.dirPath === null ? null : dirByPath(h.dirPath);
    if (!d) continue;
    const node = buildDirRow(d, 0);
    node.title = "show this folder in the tree";
    // Nothing here expands, so nothing may LOOK like it expands: blank the twisty and drop the
    // open flag the tree row carries. The full path replaces the bare folder name (and with it any
    // truncation notice buildDirRow may have stamped for the tree copy of this row).
    delete node.dataset.open;
    const tw = node.querySelector<HTMLElement>(".tw");
    if (tw) tw.textContent = "";
    // ⇊ "expand all here" would open a folder the user cannot see — drop it; ⌖ ⧉ ⤤ still apply.
    node.querySelector<HTMLElement>('.dir-acts .a[data-act="expand"]')?.remove();
    const nm = node.querySelector<HTMLElement>(".nm");
    if (nm) {
      nm.textContent = d.path;
      nm.setAttribute("title", d.path);
    }
    bandEl.appendChild(node);
  }
  bandEl.hidden = false;
}

// Band rows are a JUMP LIST, never a second tree: a folder hit warps the tree open at that folder,
// a file hit selects it, and nothing here toggles or mutates the band itself. Row actions
// (⌖ locate · ⧉ copy · ⤤ reveal) route through the same runRowAction as the tree.
function wireBandEvents(): void {
  bandEl.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement;
    const rowNode = target.closest<HTMLElement>(".row");
    const act = target.closest<HTMLElement>(".rowacts .a, .dir-acts .a");
    if (act) {
      void runRowAction(
        act.dataset.act ?? "",
        rowNode?.dataset.path ?? "",
        ev,
        rowNode?.classList.contains("dir") === true,
      );
      ev.stopPropagation();
      return;
    }
    if (!rowNode) return;
    if (rowNode.classList.contains("dir")) warpToTree(rowNode.dataset.path ?? "", true);
    // cursor: keep the keyboard cursor on the BAND row that was clicked, so ↑/↓ carry on through
    // the rest of the best matches rather than resuming from the tree.
    else if (rowNode.dataset.id) selectRow(Number(rowNode.dataset.id), { cursor: rowNode });
  });

  bandEl.addEventListener("dragstart", onRowDragStart);

  bandEl.addEventListener("contextmenu", (ev) => {
    const rowNode = (ev.target as HTMLElement).closest<HTMLElement>(".row");
    if (!rowNode || !rowNode.dataset.path) return;
    ev.preventDefault();
    if (rowNode.dataset.id) selectRow(Number(rowNode.dataset.id));
    openContextMenu(ev.clientX, ev.clientY, rowNode.dataset.path);
  });
}

// A band row: a file row plus the ⌖ "locate" action that warps the tree to it. `fullPath` swaps the
// basename for the whole path (the band is flat, so an indented basename would be ambiguous), and
// `depth` exists for indentation the band never uses but a future nested caller would.
function buildSearchRow(r: Row, fullPath: boolean, depth = 0): HTMLElement {
  const node = buildFileRow(r, depth);
  const acts = node.querySelector(".rowacts");
  if (acts) {
    const loc = document.createElement("span");
    loc.className = "a";
    loc.dataset.act = "locate";
    loc.title = "locate in tree";
    loc.textContent = "⌖";
    acts.insertBefore(loc, acts.firstChild);
  }
  if (fullPath) {
    const nm = node.querySelector(".nm");
    if (nm) {
      nm.textContent = r.path;
      nm.setAttribute("title", r.path);
    }
  }
  // Match provenance. A figure that matched on text the user cannot see in the row reads as an
  // arbitrary result, so say WHY it is here — otherwise the opt-in looks broken rather than wide.
  if (state.tier3FigurePaths.has(r.path)) {
    const nm = node.querySelector(".nm");
    const badge = document.createElement("span");
    badge.className = "figbadge";
    badge.textContent = "figure text";
    badge.title = "matched text rendered inside this figure";
    nm?.parentElement?.insertBefore(badge, nm.nextSibling);
  }
  return node;
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// LEFT-PANE ROUTER — band + tree, or the Health tiles. There is no longer a branch on the query:
// the tree is the only left-pane list, and a query narrows it rather than replacing it.
// ═════════════════════════════════════════════════════════════════════════════════════════════

function renderLeft(): void {
  if (state.mode === "health") {
    // Health replaces the left pane wholesale; a top-hits band floating above the tiles would be
    // describing a list that is not on screen.
    bandEl.hidden = true;
    renderHealth();
    return;
  }
  renderBand();
  renderTree();
  setStatusCount(statusText());
}

// "6,907 png · of 61,524 files · Newest" while a filter is on; "61,524 files · Newest" otherwise.
// It names the sort because that is the thing the reported bug made a liar of.
function statusText(): string {
  const total = state.total.toLocaleString("en-US");
  const sortWord = SORT_LABEL[state.sort];
  if (!queryIsActive()) return `${total} files · ${sortWord}`;
  return `${treeRoot.fileCount.toLocaleString("en-US")} ${filterWord()} · of ${total} files · ${sortWord}`;
}

function setStatusCount(text: string): void {
  statusCountEl.textContent = text;
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// LIST EVENT WIRING — single click: dir toggles, file inspects. Right-click → context menu.
// Hover row-actions (⌖ locate / ⧉ copy / ⤤ reveal) are wired through the same handler.
// ═════════════════════════════════════════════════════════════════════════════════════════════

function wireListEvents(): void {
  listEl.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement;

    // The no-folder empty state's one offer. Same escape-hatch idea as [data-clear] below: the
    // pane says what happened AND carries the click that fixes it.
    if (target.closest<HTMLElement>("[data-pick]")) {
      void addFolderFlow();
      return;
    }

    // The empty-state escape hatch ("Show all types" / "Clear the search") — the click that undoes
    // whatever emptied the pane, so a filtered-to-nothing tree is never a dead end.
    const clear = target.closest<HTMLElement>("[data-clear]");
    if (clear) {
      if (clear.dataset.clear === "all") searchEl.value = "";
      activeFilters.clear();
      void buildTypesPopover().then(applyQuery); // repaint the popover chips as off, then re-render
      return;
    }

    // "show all" inside a dir row's truncation notice — re-plan at the hard ceiling. It MUST
    // stopPropagation: the click is inside a .row.dir, which would otherwise collapse the folder
    // we just finished opening.
    if (target.classList.contains("more")) {
      const dirRow = target.closest<HTMLElement>(".row.dir");
      if (dirRow?.dataset.path !== undefined) expandAllHere(dirRow.dataset.path, true);
      ev.stopPropagation();
      return;
    }

    // Hover action buttons (⇊ ⌖ ⧉ ⤤) on a row. The event goes through so ⇊ can read altKey.
    const act = target.closest<HTMLElement>(".rowacts .a, .dir-acts .a");
    if (act) {
      const row = act.closest<HTMLElement>(".row");
      const path = row?.dataset.path ?? "";
      void runRowAction(act.dataset.act ?? "", path, ev, row?.classList.contains("dir") === true);
      ev.stopPropagation();
      return;
    }
    // Inline ⧉ copy affordance.
    if (target.classList.contains("cp")) {
      const row = target.closest<HTMLElement>(".row");
      const path = row?.dataset.path ?? "";
      if (path) void runRowAction("copy", path);
      ev.stopPropagation();
      return;
    }

    const rowNode = target.closest<HTMLElement>(".row");
    if (!rowNode) return;
    if (rowNode.classList.contains("dir")) {
      // Directory: single click toggles expand/collapse.
      toggleDir(rowNode.dataset.path ?? "");
    } else if (rowNode.dataset.id) {
      selectRow(Number(rowNode.dataset.id));
    }
  });

  // Native file DRAG-OUT — same handler on the tree and on the band, so a band row drags out to
  // Finder exactly like the tree row for the same file (see onRowDragStart).
  listEl.addEventListener("dragstart", onRowDragStart);

  listEl.addEventListener("contextmenu", (ev) => {
    const rowNode = (ev.target as HTMLElement).closest<HTMLElement>(".row");
    if (!rowNode || !rowNode.dataset.path) return;
    ev.preventDefault();
    if (rowNode.dataset.id) selectRow(Number(rowNode.dataset.id));
    openContextMenu(ev.clientX, ev.clientY, rowNode.dataset.path);
  });

  // Keyboard accelerator (secondary): ↑/↓ move active row, Enter inspect, →/← expand/collapse.
  listEl.addEventListener("keydown", (ev) => {
    const keys = ["ArrowDown", "ArrowUp", "Enter", "ArrowRight", "ArrowLeft"];
    if (!keys.includes(ev.key)) return;
    ev.preventDefault();
    handleListKey(ev.key);
  });
}

// Native file DRAG-OUT (delegated): pressing+dragging a FILE row starts an OS drag session so it
// can be dropped onto Finder / Inkscape / any app — like dragging from Finder. We preventDefault
// the webview's own HTML5 drag (per the plugin's example) so the native NSDraggingSession owns the
// gesture. Default mode is COPY (non-destructive); holding ⌘ (metaKey) switches to MOVE so a drop
// onto another folder relocates the file. Dir rows aren't draggable; a press that began on a row
// action button (⧉/⤤/⌖) is ignored so those keep working.
function onRowDragStart(ev: DragEvent): void {
  const target = ev.target as HTMLElement;
  const rowNode = target.closest<HTMLElement>(".row");
  if (!rowNode || rowNode.classList.contains("dir") || !rowNode.dataset.path) return;
  if (target.closest(".rowacts .a, .dir-acts .a, .cp")) {
    ev.preventDefault();
    return;
  }
  const abs = absPathOf(rowNode.dataset.path);
  if (!abs) return; // active root not known yet → let the row behave normally
  ev.preventDefault(); // suppress the webview HTML5 drag ghost; the OS drag takes over
  const ext = rowNode.dataset.ext ?? "";
  // A valid NSImage path is REQUIRED (the plugin panics otherwise): the file itself for safe
  // rasters, else the bundled fallback. If neither resolves, skip rather than start a bad drag.
  const icon = RASTER_ICON_EXTS.has(ext) ? abs : dragFallbackIcon;
  if (!icon) return;
  void startDrag(
    { item: [abs], icon, mode: ev.metaKey ? "move" : "copy" },
    // onEvent is optional; we don't need the Dropped/Cancelled result.
  ).catch((e) => console.error("[lens] startDrag failed:", e));
}

// `ev` is optional and read for ONE thing: option-click on ⇊, the undocumented accelerator that
// raises the expand budget. The documented path is the visible "show all" word (CONTRACT.md's
// mouse-first rule), so nothing here may become reachable only with a modifier held.
async function runRowAction(
  act: string,
  path: string,
  ev?: MouseEvent,
  isDir = false,
): Promise<void> {
  if (!path) return;
  if (act === "reveal") {
    try {
      await api.revealInFinder(path);
    } catch (e) {
      console.error("[lens] reveal_in_finder failed:", e);
    }
    return;
  }
  if (act === "copy") {
    try {
      // Default copy = repo-RELATIVE path: pastes into the Claude Code prompt as text rather than
      // resolving to (and attaching) the on-disk image. Absolute stays available via the Path
      // card's "copy absolute path" line and the right-click menu.
      const text = await api.copyPath(path, "rel");
      await navigator.clipboard.writeText(text);
      flashStatus(`Copied path · ${basename(path)}`);
    } catch (e) {
      console.error("[lens] copy_path failed:", e);
    }
    return;
  }
  if (act === "expand") {
    expandAllHere(path, ev?.altKey === true);
    return;
  }
  if (act === "locate") {
    // isDir matters: for a FILE we open its parent, for a FOLDER we open the folder itself. Passing
    // a folder as a file dropped its last segment, so ⌖ on a dir row landed one level too high.
    warpToTree(path, isDir);
  }
}

// "Locate in tree" (⌖) — from a band row, a tree row, or a lineage xref: LEAVE THE SEARCH BEHIND
// and reveal the entry where it actually lives, in the full folder tree.
//
// ⚠ Deliberate reversal of U6 (SEARCH_REDESIGN_2026-08-07.md), by lead ruling 2026-08-28. U6 held
// that the band is a jump list INTO the filtered tree, so ⌖ must not touch the query. In practice
// that made the button do nothing worth doing: it landed you in the folder you were already looking
// at, still filtered by the search, with every sibling the query excluded still hidden. ⌖ now means
// "show me this in its real folder" — search box cleared, filter chips dropped, full tree. The row
// is still selected when we arrive, so nothing about WHAT you found is lost. (If you want the
// filtered view back, the query is one ⌘Z-equivalent away: retype, or re-tick the chip.)
function warpToTree(path: string, isDir = false): void {
  const segs = path.split("/");
  if (!isDir) segs.pop(); // a file: drop the filename → ancestor dirs remain. A folder opens itself.
  const chain: string[] = [];
  let acc = "";
  for (const s of segs) {
    acc = acc ? `${acc}/${s}` : s;
    openDirs.add(acc);
    // Claim the whole chain as USER-opened. applyQuery's retirement sweep deletes every dir in
    // expandOpened when the query changes — and under an active search the target's ancestors were
    // almost certainly opened by autoExpandForQuery, so without this the tree we are clearing the
    // query to reach would collapse from under us on the very next line.
    expandOpened.delete(acc);
    chain.push(acc);
  }

  // Health replaces the left pane wholesale — there is no tree to warp into until we're back.
  if (state.mode !== "browse") {
    state.mode = "browse";
    modeSeg.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("active", b.getAttribute("data-mode") === "browse"));
  }

  if (queryIsActive()) {
    searchEl.value = "";
    activeFilters.clear();
    // applyQuery rebuilds the tree from the now-empty query, and buildTree restores every dir's
    // open flag from openDirs — which we stamped above — so the chain comes back already expanded.
    // It also repaints the band (now hidden), the chips and the status line.
    applyQuery();
    void buildTypesPopover(); // the popover's own chips must not stay lit for filters just dropped
  } else {
    // renderTree only descends into dirs whose live TreeDir.open is true (openDirs alone is NOT
    // consulted there — it's read only by buildTree at boot/reindex). Without this, the target's
    // ancestors stay collapsed, its row is never materialised, and the rAF querySelector below
    // finds nothing → locate silently no-ops for any entry below a top-level folder (~99%).
    for (const p of chain) {
      const d = dirByPath(p);
      if (d) d.open = true;
    }
    renderLeft();
  }
  requestAnimationFrame(() => {
    const row = listEl.querySelector<HTMLElement>(`.row[data-path="${cssEscape(path)}"]`);
    if (!row) {
      // The search is already gone by here, so a miss now means the tree itself is withholding the
      // row: its folder is past the expand budget ("too big to open at once"), or the path is not
      // in this project's index at all. Saying nothing reads as a broken button (B5's failure mode
      // in a new shape), so name the reason.
      flashStatus(`could not open ${basename(path)} — its folder is too big to expand at once`);
      return;
    }
    row.scrollIntoView({ block: "center" });
    // A file gets inspected; a folder has nothing to inspect, so just show where we arrived.
    if (row.dataset.id) selectRow(Number(row.dataset.id));
    else setActiveRow(row);
  });
}

function cssEscape(s: string): string {
  // Minimal attribute-selector escaping for data-path lookups.
  return s.replace(/(["\\])/g, "\\$1");
}

// Active-row cursor (keyboard). Tracks a DOM .row element rather than an index.
let activeRow: HTMLElement | null = null;
function setActiveRow(node: HTMLElement | null): void {
  if (activeRow) activeRow.classList.remove("active");
  activeRow = node;
  if (activeRow) {
    activeRow.classList.add("active");
    activeRow.scrollIntoView({ block: "nearest" });
  }
}
// EVERY row the keyboard cursor may visit, in the order they appear on screen: the Best-matches
// band first (only while a query has actually put it there), then the tree.
//
// The band used to be unreachable from the keyboard: you typed a query, the best 20 rows appeared
// directly under the search box, and ↓ walked straight past them into the tree — so the one list
// ranked by relevance was the one list you had to reach for the mouse to use.
function navRows(): HTMLElement[] {
  const band = bandEl.hidden ? [] : Array.from(bandEl.querySelectorAll<HTMLElement>(".row"));
  return band.concat(Array.from(listEl.querySelectorAll<HTMLElement>(".row")));
}

function inBand(node: HTMLElement | null): boolean {
  return node !== null && bandEl.contains(node);
}

function handleListKey(key: string): void {
  const rows = navRows();
  if (rows.length === 0) return;
  let idx = activeRow ? rows.indexOf(activeRow) : -1;

  if (key === "ArrowDown") idx = Math.min(rows.length - 1, idx + 1);
  else if (key === "ArrowUp") idx = Math.max(0, idx <= 0 ? 0 : idx - 1);
  else if (key === "Enter") {
    const cur = activeRow;
    // A band row is a JUMP LIST entry, never a second tree, so ⏎ on a band folder warps to it
    // (exactly what clicking it does) rather than toggling a twisty the band does not draw.
    if (inBand(cur)) {
      if (cur!.classList.contains("dir")) warpToTree(cur!.dataset.path ?? "", true);
      else if (cur!.dataset.id) selectRow(Number(cur!.dataset.id), { cursor: cur! });
      return;
    }
    if (cur?.classList.contains("dir")) toggleDir(cur.dataset.path ?? "");
    else if (cur?.dataset.id) selectRow(Number(cur.dataset.id));
    return;
  } else if (key === "ArrowRight") {
    const cur = activeRow;
    if (inBand(cur)) return; // nothing in the band expands
    if (cur?.classList.contains("dir") && cur.dataset.open === undefined) {
      toggleDir(cur.dataset.path ?? "");
    }
    return;
  } else if (key === "ArrowLeft") {
    const cur = activeRow;
    if (inBand(cur)) return;
    if (cur?.classList.contains("dir") && cur.dataset.open !== undefined) {
      toggleDir(cur.dataset.path ?? "");
    }
    return;
  }
  // ↑/↓ tail: move the cursor AND auto-preview. Landing on a file row selects it (loads the
  // inspector + preview, debounced so holding the key doesn't flood get_entry); a dir row has
  // nothing to preview, so we only move the cursor and leave the prior preview up.
  // `cursor` pins the cursor to the row we actually landed on — without it selectRow would jump it
  // to the TREE copy of the same file, and the next ↓ would resume from there mid-band.
  const target = rows[idx] ?? null;
  if (target?.dataset.id) selectRow(Number(target.dataset.id), { defer: true, cursor: target });
  else setActiveRow(target);
}

// Debounce handle for the keyboard-driven preview load (see selectRow's `defer`).
let inspectorLoadTimer: number | undefined;
function selectRow(id: number, opts?: { defer?: boolean; cursor?: HTMLElement }): void {
  state.selectedId = id;
  // Repaint selection on the currently-rendered rows without a full rebuild. The band is a second
  // container showing the SAME rows, so it is repainted too — otherwise a file selected in the tree
  // stays unhighlighted in the band and reads as a different entry.
  listEl.querySelectorAll<HTMLElement>(".row.sel").forEach((n) => n.classList.remove("sel"));
  bandEl.querySelectorAll<HTMLElement>(".row.sel").forEach((n) => n.classList.remove("sel"));
  bandEl.querySelector<HTMLElement>(`.row[data-id="${id}"]`)?.classList.add("sel");
  const node = listEl.querySelector<HTMLElement>(`.row[data-id="${id}"]`);
  if (node) node.classList.add("sel");
  // The cursor follows the row that was ACTUALLY actioned. A band row keeps the cursor in the band,
  // so the next ↓ walks the rest of the best matches instead of teleporting into the tree copy.
  const cursor = opts?.cursor ?? node;
  if (cursor) setActiveRow(cursor);
  // Cursor + selection paint are instant; the get_entry fetch is deferred for keyboard nav so a
  // held ↑/↓ only fires one load when the run settles. Clicks (defer absent) load immediately.
  window.clearTimeout(inspectorLoadTimer);
  if (opts?.defer) {
    inspectorLoadTimer = window.setTimeout(() => void loadInspector(id), 90);
  } else {
    void loadInspector(id);
  }
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// SEARCH BOX + FILTER CHIPS — the box composes free tokens with the active filters into one
// grammar query. Filters carry a kind: "ext" chips (the Types popover, → ext:<ext>) and "cat"
// quick-filters (the Matrices button, → cat:<category>). Free text from the box composes last.
// ═════════════════════════════════════════════════════════════════════════════════════════════

// Active filters: ONE SET PER KIND. The Set is load-bearing — two ext chips must OR into
// `ext:png ext:svg` (11,202 rows). A Map<kind, value> would make the second chip replace the first,
// which is regression B2 of SEARCH_BUGHUNT_2026-06-25.md.
const activeFilters = new Map<FilterKind, Set<string>>();

function hasFilter(kind: FilterKind, value: string): boolean {
  return activeFilters.get(kind)?.has(value) ?? false;
}
function toggleFilter(kind: FilterKind, value: string): void {
  const set = activeFilters.get(kind);
  if (set === undefined) {
    activeFilters.set(kind, new Set([value]));
    return;
  }
  if (set.delete(value)) {
    // Drop the empty kind rather than keeping an empty Set: `filters.size` is what tells the tree,
    // the band and the status line whether anything is filtered at all.
    if (set.size === 0) activeFilters.delete(kind);
    return;
  }
  set.add(value);
}

// The composed grammar string. It still drives #statuscount wording and the single parse below, but
// NOTHING sends it to `search`/`search_all` any more: the tree and the band are computed
// client-side from state.corpus, and only tier 3 reaches the backend — as pre-split tokens.
function composeQuery(): string {
  const parts: string[] = [];
  for (const [kind, values] of activeFilters) {
    for (const v of values) parts.push(`${kind}:${v}`);
  }
  const free = searchEl.value.trim();
  if (free) parts.push(free);
  return parts.join(" ");
}

// The one entry point that turns "what the user typed/ticked" into what is on screen.
//
// The tree is REBUILT from the filtered rows, never pruned in place. buildTree's `tally` computes
// fileCount/newestMs/oldestMs/totalBytes and sortDirs orders folders on exactly those fields, so
// hiding rows instead of rebuilding would leave the aggregates unfiltered and "Newest" would keep
// meaning "newest file of ANY type" — the reported bug. Measured cost for ext:png: 0.6 ms to filter
// + 3.5 ms to rebuild, against the 40 ms SQL + 2.2 MB IPC round trip it replaces.
// TWO CALLERS, TWO MEANINGS. Typing / ticking a chip changes the QUERY; loadBrowse() re-applies the
// SAME query to a fresh row set after boot, a reindex, a project switch, or a live-index flush. Only
// the first invalidates a budgeted expansion, and conflating them collapsed the user's expanded tree
// on every watcher flush while the filter stayed visibly on. Hence expansionSurvives, keyed on the
// composed query rather than on which function called us.
function applyQuery(): void {
  const prevQuery = state.query;
  state.query = composeQuery();
  state.parsed = parseQuery(state.query);
  if (!expansionSurvives(prevQuery, state.query)) {
    // The filter moved, so every plan-opened dir was costed against a row set that no longer
    // exists — `outputs` budgeted to 8,000 png rows is 21,899 rows once the chip comes off. Retire
    // the plan (and the counts it stamped) so no subtree renders uncosted; a folder the user opened
    // by hand is theirs and is never in expandOpened, so it survives either way.
    expandNotices.clear();
    for (const p of expandOpened) openDirs.delete(p);
    expandOpened.clear();
  }
  rebuildTree();
  paintFilterChrome();
  renderLeft();
  scheduleTier3();
}

/// Rebuild the tree from the corpus UNION the backend's tier-3 hits. Split out of `applyQuery` so
/// the async tier-3 reply can refresh the tree without re-entering `scheduleTier3` (which would
/// loop). `treeBuiltWithTier3` records how many tier-3 paths the current tree was built with, so
/// the reply can skip a pointless rebuild — see the call site.
let treeBuiltWithTier3 = 0;

function rebuildTree(): void {
  buildTree(treeRows(state.corpus, state.parsed, state.tier3Paths) as Row[]);
  treeDirPaths = collectDirPaths(treeRoot, []);
  treeBuiltWithTier3 = state.tier3Paths.size;
  autoExpandForQuery();
}

/// While a query is ACTIVE, open the filtered tree down to the matches.
///
/// A search tree is not a browse tree: every row in it is already a hit, so a closed folder hides
/// the very thing the user asked for and makes them click five levels down to reach three files.
/// Browsing is the opposite — there the closed state is what keeps 62k rows navigable — so this
/// runs only under `queryIsActive()`, and the whole expansion retires the moment the query changes
/// (`expansionSurvives` → the `expandOpened` sweep in `applyQuery`), restoring the browse tree.
///
/// Budgeted through the SAME planner as the ⇊ button rather than opening everything: a query
/// matching thousands of files would otherwise render tens of thousands of rows (measured 85.6 s
/// for this repo's largest folder). `SOFT_BUDGET` fits inside the 100 ms perceptual budget; folders
/// past it stay closed and keep their own ⇊ affordance.
///
/// Ownership follows `expandAllHere`: a dir the user opened BY HAND is never claimed, so it
/// survives the retirement sweep unchanged.
function autoExpandForQuery(): void {
  if (!queryIsActive()) return;
  const plan = planExpand(toExpandNode(treeRoot), SOFT_BUDGET);
  for (const dirPath of plan.open) {
    if (dirPath === "") continue; // the root has no header row to open
    if (!openDirs.has(dirPath)) expandOpened.add(dirPath);
    openDirs.add(dirPath);
    // openDirs alone is not enough: renderTree only descends into dirs whose live TreeDir.open is
    // true, and that flag is otherwise only restored by buildTree.
    const sub = dirByPath(dirPath);
    if (sub) sub.open = true;
  }
}

// TIER 3 — the only asynchronous part of search, and it feeds the BAND ONLY.
//
// Tiers 0–2 (folder name · filename · path) run synchronously against state.corpus on every input
// event, no debounce: measured 1–4 ms over 61k rows. Tier 3 is a token that appears only inside
// `meta`, which the Row DTO deliberately omits (CONTRACT.md MEMORY), so it has to come from the
// backend — debounced, and guarded by a monotonic generation so a slow reply for an older query can
// never overwrite a newer one. The tree never waits on any of this.
function scheduleTier3(): void {
  window.clearTimeout(tier3Timer);
  const q = state.parsed;
  if (q.free.length === 0) {
    // Bump the generation on THIS path too. Without it, a searchIds reply already in flight for the
    // query we just cleared still passes its `gen !== state.tier3Gen` guard, and goes on to
    // rebuildTree() + renderLeft() with the dead query's tier-3 paths — repainting the tree a
    // moment after we finished with it. That is what made ⌖ land on an UNSELECTED row: the warp
    // selected it, then the stale reply re-rendered the rows out from under the selection.
    state.tier3Gen++;
    state.tier3Paths = new Set(); // no free text → no meta tier
    state.tier3FigurePaths = new Set();
    return;
  }
  const gen = ++state.tier3Gen;
  tier3Timer = window.setTimeout(() => {
    void (async () => {
      try {
        const out = await api.searchIds(
          q.free,
          // Drop a "" ext: query.ts's rule is that an empty ext member matches NOTHING, and the
          // backend has no such guard — it would emit `e.ext IN ('')` and pull in every
          // extension-less row.
          [...(q.filters.get("ext") ?? [])].filter((v) => v.length > 0),
          [...(q.filters.get("cat") ?? [])],
          state.figureText,
        );
        // Superseded by a newer query, or the user left Browse while this was in flight — renderLeft
        // hid the band on the way to Health, and renderBand's last act is to unhide it, which would
        // float a 20-row file strip over the health tiles.
        if (gen !== state.tier3Gen || state.mode !== "browse") return;
        // Join on PATH, never on id: ids are sqlite rowids and churn across a reconcile (C6).
        state.tier3Paths = new Set(out.map((o) => o.path));
        // Provenance for the badge: which of these matched on text rendered INSIDE the figure.
        state.tier3FigurePaths = new Set(
          out.filter((o) => o.via_figure_text).map((o) => o.path),
        );
        // The tree is corpus-only until this reply lands, so a backend-only hit (a token in
        // `meta`, or in a figure's rendered text) showed in the band while the tree and the
        // "N matching" count denied it. Rebuild so the three agree.
        //
        // Skipped when tier 3 neither adds nor removes anything — the common case for a plain
        // path search — which keeps the author's "don't fight the user's scroll" guarantee for
        // every query that never had a backend-only hit in the first place.
        if (state.tier3Paths.size > 0 || treeBuiltWithTier3 > 0) {
          rebuildTree();
          renderLeft(); // repaints the band AND the tree from the new row set
        } else {
          renderBand();
          // …the tree's empty state has no scroll to fight, and its wording depends on whether
          // the band ended up with rows (see emptyTreeHtml).
          if (listEl.querySelector(".lens-empty")) renderTree();
        }
      } catch (e) {
        console.error("[lens] search_ids failed:", e);
      }
    })();
  }, TIER3_DEBOUNCE_MS);
}
let tier3Timer: number | undefined;

// Everything on screen that says "a filter is on": the Types button lights and names the filter,
// and one removable chip per active filter sits in the controls row at eye level. A filtered tree
// that looks exactly like an unfiltered tree is indistinguishable from a broken one (U2).
function paintFilterChrome(): void {
  const chips: string[] = [];
  for (const [kind, values] of activeFilters) {
    for (const v of values) {
      // ext chips take their category's hue (as in the Types popover); a cat chip IS its category.
      const cc = catColorVar(kind === "ext" ? extCategory(v) : v);
      chips.push(
        `<span class="filterchip on" data-kind="${kind}" data-value="${esc(v)}" style="--cc:${cc}">` +
          `<span class="lbl">${esc(kind === "cat" ? `cat:${v}` : v)}</span>` +
          `<b class="x" title="remove this filter">⨯</b></span>`,
      );
    }
  }
  filterChipsEl.innerHTML = chips.join("");
  typesBtn.classList.toggle("on", activeFilters.size > 0);

  // #typescount names the active ext filter ("png ▾", "png +2 ▾") instead of the constant ext
  // count. `.on` marks "a filter is active"; `.lit` stays reserved for "the popover is open".
  const typesCountEl = document.getElementById("typescount");
  if (typesCountEl) {
    const exts = [...(activeFilters.get("ext") ?? [])];
    if (exts.length === 1) typesCountEl.textContent = `${exts[0]} ▾`;
    else if (exts.length > 1) typesCountEl.textContent = `${exts[0]} +${exts.length - 1} ▾`;
    else if (facetCache) {
      typesCountEl.textContent = `${facetCache.exts.filter((f) => f.key.length > 0).length} ▾`;
    }
  }
}

// One removable chip per filter — clicking its ⨯ drops that filter and re-renders.
function wireFilterChips(): void {
  filterChipsEl.addEventListener("click", (ev) => {
    const x = (ev.target as HTMLElement).closest<HTMLElement>(".filterchip .x");
    if (!x) return;
    const chip = x.closest<HTMLElement>(".filterchip");
    const kind = chip?.dataset.kind as FilterKind | undefined;
    const value = chip?.dataset.value;
    if (!kind || value === undefined) return;
    toggleFilter(kind, value);
    void buildTypesPopover().then(applyQuery); // keep the popover's own chip states in step
  });
}

// Typing is now SYNCHRONOUS. The old 140 ms debounce existed to protect a SQL round trip that no
// longer happens: tiers 0–2 (folder name · filename · path) match precomputed strings in
// state.corpus in 1–4 ms over 61k rows, so the tree and the band can keep up with the keys. The
// one thing still worth debouncing — the search_ids IPC behind tier 3 — is debounced inside
// scheduleTier3, where it belongs.
function wireSearch(): void {
  searchEl.addEventListener("input", applyQuery);
  // Reflect the persisted pref, then re-run the CURRENT query whenever it flips — a toggle that
  // only affects the NEXT keystroke reads as not working.
  figToggleEl.checked = state.figureText;
  figToggleEl.addEventListener("change", () => {
    state.figureText = figToggleEl.checked;
    try {
      window.localStorage.setItem(FIGTEXT_KEY, state.figureText ? "1" : "0");
    } catch {
      /* storage disabled — the toggle still works for this session */
    }
    state.tier3FigurePaths = new Set();
    applyQuery();
  });
  searchEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      searchEl.value = "";
      applyQuery();
      return;
    }
    // ↑/↓/⏎ steer the Best-matches band WITHOUT leaving the search box — type, then arrow straight
    // down into the results and keep refining. ←/→ are left alone: they move the text caret.
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp" || ev.key === "Enter") {
      ev.preventDefault();
      handleListKey(ev.key);
    }
  });
}

// ── Types popover (#filters) built from facets().exts — one .filterchip per file EXTENSION ────
// Extensions are far easier to parse than abstract categories. FAVOURITES (png, svg, csv, h5ad,
// py) pin to the top in that order; a .pop-sep divides them from the alphabetical long tail. Each
// chip is coloured by the ext's CATEGORY (derived from the loaded rows), and toggles an ext:<ext>
// grammar filter.
let facetCache: Facets | null = null;

// Favourite extensions, pinned to the top of the popover in this order (when present).
const FAVOURITE_EXTS = ["png", "svg", "pdf", "csv", "h5ad", "h5", "py", "docx"];

// Canonical ext → category, mirroring the indexer's ext_to_category (repo_index/config.py:57) so
// chip colours are STABLE regardless of the row-derivation race below. One lens-side deviation:
// config.py leaves `docx` unmapped (→ indexer stores "other"), but a Word doc is canonically a
// document, so we colour it as "doc". Compound keys (csv.gz, …) never match the single-token .ext
// and are kept only for fidelity with config.py.
const CANONICAL_EXT_CATEGORY: Record<string, string> = {
  py: "code", r: "code", sh: "code", cpp: "code", c: "code",
  h5ad: "data_matrix", h5: "data_matrix", npy: "data_matrix", npz: "data_matrix", loom: "data_matrix",
  csv: "data_table", tsv: "data_table", "csv.gz": "data_table", "tsv.gz": "data_table",
  parquet: "data_table", xlsx: "data_table",
  yaml: "config", yml: "config", toml: "config", json: "config", ini: "config",
  md: "doc", txt: "doc", rst: "doc", docx: "doc",
  ipynb: "notebook",
  png: "figure", svg: "figure", jpg: "figure", jpeg: "figure",
  pdf: "figure_pdf",
  pkl: "model", pt: "model", pth: "model", joblib: "model", model: "model", rds: "model", onnx: "model",
  log: "log", out: "log", err: "log",
  gz: "archive", tgz: "archive", zip: "archive", tar: "archive",
};

// Derive ext → category from the loaded rows (each Row carries both .ext and .category) in one
// pass, cached. First non-empty category wins; unseen exts fall back to "other".
let extCatCache: Map<string, string> | null = null;
function extCategoryMap(): Map<string, string> {
  if (extCatCache) return extCatCache;
  const m = new Map<string, string>();
  for (const r of state.allRows) {
    if (r.ext && r.category && !m.has(r.ext)) m.set(r.ext, r.category);
  }
  extCatCache = m;
  return m;
}
// Prefer the canonical map (stable, race-free) → row-derived map → "other".
function extCategory(ext: string): string {
  return CANONICAL_EXT_CATEGORY[ext] ?? extCategoryMap().get(ext) ?? "other";
}

// One full-width ext .filterchip — a canonical-category GLYPH (the chip's colour cue) + ext text +
// count. The glyph fills the chip's `.g` slot, which base.css colours via --cc; we ALSO set the
// colour inline so the canonical category colour stays visible in the unselected (off) state — the
// chip is `off` by default, and `.filterchip.off .g` otherwise mutes every glyph to grey (the
// reported "everything is colourless": --cc was set correctly but nothing visible consumed it,
// because the chip rendered no `.g` element at all). Glyph + colour mirror the file-row convention.
function extChip(ext: string, count: number): string {
  const on = hasFilter("ext", ext);
  const cat = extCategory(ext);
  const cc = catColorVar(cat);
  return (
    `<span class="filterchip ${on ? "on" : "off"}" data-ext="${esc(ext)}" style="--cc:${cc}">` +
    `<span class="g" style="color:${cc}">${catGlyph(cat)}</span>` +
    `<span class="lbl">${esc(ext)}</span>` +
    `<span class="cnt">${count.toLocaleString("en-US")}</span></span>`
  );
}

async function buildTypesPopover(): Promise<void> {
  let facets: Facets;
  try {
    facets = facetCache ?? (await api.facets());
    facetCache = facets;
  } catch (e) {
    console.error("[lens] facets() failed:", e);
    return;
  }
  const exts = facets.exts.filter((f) => f.key.length > 0);
  const total = exts.reduce((s, f) => s + f.count, 0);
  const byKey = new Map(exts.map((f) => [f.key, f.count]));

  // FAVOURITES first (in the fixed order, only those present) …
  const favPresent = FAVOURITE_EXTS.filter((e) => byKey.has(e));
  const favSet = new Set(favPresent);
  // … then ALL remaining exts, alphabetically.
  const rest = exts
    .map((f) => f.key)
    .filter((k) => !favSet.has(k))
    .sort((a, b) => a.localeCompare(b, "en-US"));

  const favLines = favPresent.map((e) => extChip(e, byKey.get(e) ?? 0)).join("");
  const restLines = rest.map((e) => extChip(e, byKey.get(e) ?? 0)).join("");
  const body =
    (favLines ? favLines + (restLines ? `<div class="pop-sep"></div>` : "") : "") + restLines;

  filtersEl.innerHTML =
    `<div class="pophead"><span class="sechdr">Types · ${total.toLocaleString("en-US")}</span>` +
    `<span class="allnone"><span data-all="all">all</span><span class="div">/</span>` +
    `<span data-all="none">none</span></span></div>` +
    `<div class="extlist">${body}</div>`;

  // The toolbar chip + the removable filter chips are painted from ONE place, so the popover and
  // the controls row can never disagree about what is filtered.
  paintFilterChrome();
}

// Weld the popover to #typesbtn in VIEWPORT coordinates (it is position:fixed — see lens.css).
//
// The whole shell lives inside the ONE .scroll container, so an absolutely-positioned popover was
// laid out against SCROLLED content: scrolling the tree slid it away from its own sticky button.
// Fixed positioning fixes the containing block; this re-reads the button's rect so the two stay
// together through the first 48px of scroll (before .lefthead's sticky engages) and through any
// window resize. It also clamps the height to whatever is left below the button, so a long ext
// list scrolls inside the popover instead of running off the bottom of the window.
function anchorTypesPopover(): void {
  if (filtersEl.classList.contains("hidden")) return;
  const r = typesBtn.getBoundingClientRect();
  const GAP = 6;
  const MARGIN = 10;
  const w = filtersEl.offsetWidth || 236;
  const left = Math.max(MARGIN, Math.min(r.left, window.innerWidth - w - MARGIN));
  const top = r.bottom + GAP;
  filtersEl.style.left = `${Math.round(left)}px`;
  filtersEl.style.top = `${Math.round(top)}px`;
  filtersEl.style.maxHeight = `${Math.round(Math.max(160, window.innerHeight - top - MARGIN))}px`;
}

function wireTypesPopover(): void {
  typesBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const opening = filtersEl.classList.contains("hidden");
    filtersEl.classList.toggle("hidden");
    typesBtn.classList.toggle("lit", !filtersEl.classList.contains("hidden"));
    anchorTypesPopover();
    // Rebuild on open so ext→category colours reflect the loaded rows (the boot build can race
    // ahead of loadBrowse, painting every chip --c-other until the rows arrive). Re-anchor after,
    // because the rebuilt content is what settles the popover's final width.
    if (opening) void buildTypesPopover().then(anchorTypesPopover);
  });
  // Capture:true so a scroll of .scroll (which does not bubble) still reaches us.
  window.addEventListener("scroll", anchorTypesPopover, true);
  window.addEventListener("resize", anchorTypesPopover);
  filtersEl.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement;
    const all = target.closest<HTMLElement>("[data-all]");
    if (all) {
      // Both "all" and "none" clear the ext filters (empty = no ext: tokens = every ext shown).
      // A literal "none" (match nothing) isn't a useful finder state. Leave cat: filters intact.
      activeFilters.delete("ext");
      void buildTypesPopover().then(applyQuery);
      return;
    }
    const chip = target.closest<HTMLElement>(".filterchip");
    if (chip?.dataset.ext) {
      toggleFilter("ext", chip.dataset.ext);
      void buildTypesPopover().then(applyQuery);
    }
  });
  // Dismiss on outside click.
  document.addEventListener("mousedown", (ev) => {
    if (
      !filtersEl.classList.contains("hidden") &&
      !filtersEl.contains(ev.target as Node) &&
      ev.target !== typesBtn &&
      !typesBtn.contains(ev.target as Node)
    ) {
      filtersEl.classList.add("hidden");
      typesBtn.classList.remove("lit");
    }
  });
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// INSPECTOR + PREVIEW — on file select → get_entry → render the hero + cards (inspector.html) +
// the preview. Lineage refs/ref_by render as CLICKABLE xref links that navigate by PATH.
// ═════════════════════════════════════════════════════════════════════════════════════════════

async function loadInspector(id: number): Promise<void> {
  let detail: EntryDetail;
  try {
    detail = await api.getEntry(id);
  } catch (e) {
    console.error("[lens] get_entry failed:", e);
    inspectorEl.innerHTML = `<div class="empty"><p class="em">Could not load entry ${id}.</p></div>`;
    return;
  }
  renderInspector(detail);
  renderPreview(detail.row);
}

function metaObj(meta: unknown): Record<string, unknown> {
  return meta && typeof meta === "object" ? (meta as Record<string, unknown>) : {};
}
function metaStr(meta: Record<string, unknown>, key: string): string | null {
  const v = meta[key];
  if (v === null || v === undefined) return null;
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
  return null;
}
function metaList(meta: Record<string, unknown>, key: string): string[] {
  const v = meta[key];
  if (Array.isArray(v)) return v.map((x) => String(x));
  if (v && typeof v === "object") return Object.keys(v as Record<string, unknown>);
  return [];
}

// A numeric meta field, or null. Kept separate from metaStr because the dataset
// card needs to distinguish "absent" from the string "0".
function metaNum(meta: Record<string, unknown>, key: string): number | null {
  const v = meta[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// Column NAMES out of a meta field, for every shape the crawler writes one in:
// csv/tsv/xlsx store a plain list of names, parquet stores {name, type} records
// (so it can show the column's type). Anything else yields nothing rather than
// Object.keys of a stray object, which is what metaList would do.
function metaColumnNames(meta: Record<string, unknown>, key: string): string[] {
  const v = meta[key];
  if (!Array.isArray(v)) return [];
  return v.map((x) =>
    x && typeof x === "object" && "name" in (x as Record<string, unknown>)
      ? String((x as Record<string, unknown>).name)
      : String(x),
  );
}

// One worksheet as the xlsx extractor records it. A workbook that the crawler
// could not open has no sheets array at all, and the card is simply not drawn.
interface SheetInfo {
  name: string;
  columns: string[];
  n_columns: number;
  row_count: number | null;
  row_count_exact: boolean;
  row_count_reason: string | null;
}
function sheetsOf(meta: Record<string, unknown>): SheetInfo[] {
  const v = meta["sheets"];
  if (!Array.isArray(v)) return [];
  return v.map((raw) => {
    const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
    const rc = o["row_count"];
    return {
      name: typeof o["name"] === "string" ? (o["name"] as string) : "—",
      columns: metaColumnNames(o, "columns"),
      n_columns: typeof o["n_columns"] === "number" ? (o["n_columns"] as number) : 0,
      row_count: typeof rc === "number" ? rc : null,
      row_count_exact: o["row_count_exact"] === true,
      row_count_reason:
        typeof o["row_count_reason"] === "string" ? (o["row_count_reason"] as string) : null,
    };
  });
}

// Why a row count is missing or approximate, in words rather than the crawler's
// token. Shown beside the count so an estimate is never mistaken for a census.
function rowCountNote(reason: string | null, exact: boolean): string {
  if (exact) return "";
  switch (reason) {
    case "size_gated":
      return "declared range — file too large to count";
    case "no_dimension":
      return "not counted — file too large";
    case "read_error":
      return "could not be read";
    case "sheet_cap":
      return "sheet not inspected";
    case "empty":
      return "empty";
    default:
      return reason ? reason : "not counted";
  }
}

function chipList(names: string[], extraClass = ""): string {
  if (!names.length) return `<span class="chip" style="color:var(--faint)">none</span>`;
  return names
    .map((n) => {
      const umap = /umap/i.test(n) ? " umap" : "";
      return `<span class="chip${extraClass}${umap}">${esc(n)}</span>`;
    })
    .join("");
}

function renderInspector(d: EntryDetail): void {
  const r = d.row;
  const meta = metaObj(d.meta);
  const cc = catColorVar(r.category);
  const glyph = catGlyph(r.category);
  const isMatrix = r.category === "data_matrix";
  const isTable = r.category === "data_table";

  const sub = `${esc(r.category || "—")} · ${esc(r.ext || "—")} · ${esc(
    r.size_bytes > 0 ? fmtBytes(r.size_bytes) : "—",
  )} · ${esc(fmtRel(r.mtime))} ago`;

  // ── PATH card: relative (stored) + best-effort absolute via copy_path on click. ──
  const pathCard =
    `<div class="card"><div class="ch"><span class="sechdr">Path</span></div>` +
    `<div class="pathline"><code data-copy="rel" data-path="${esc(r.path)}">${esc(r.path)}</code>` +
    `<span class="cpy" data-copy="rel" data-path="${esc(r.path)}">⧉</span></div>` +
    `<div class="pathline dim"><code data-copy="abs" data-path="${esc(r.path)}">copy absolute path ⧉</code>` +
    `<span class="cpy" data-copy="abs" data-path="${esc(r.path)}">⧉</span></div></div>`;

  // ── DATASET card (matrix/table only): bigstat shape + metagrid from meta. ──
  let datasetCard = "";
  const sheets = sheetsOf(meta);
  // Column names, whichever kind of table this is. h5ad keeps its annotation
  // names under a different key and is handled by its own card below.
  const tableColumns = metaColumnNames(meta, "columns");
  if (isMatrix || isTable) {
    // The denormalized n_obs/n_vars columns are filled for matrix files only, so
    // a spreadsheet fell through to "— × —" even though the crawler had recorded
    // both numbers in meta. Read those as the fallback: row_count/n_columns for
    // csv/tsv/xlsx, num_rows for parquet (pyarrow's own field name).
    const rowsN = r.n_obs ?? metaNum(meta, "row_count") ?? metaNum(meta, "num_rows");
    const colsN = r.n_vars ?? metaNum(meta, "n_columns");
    const nObs = fmtNum(rowsN);
    const nVars = colsN === null || colsN === undefined ? "—" : fmtNum(colsN);
    // A multi-sheet workbook has no single shape, so it leads with its sheet
    // count and the per-sheet block below carries each sheet's rows x columns.
    const multiSheet = sheets.length > 1;
    const xEnc = metaStr(meta, "X_encoding");
    const xDtype = metaStr(meta, "X_dtype");
    const obsIndex = metaStr(meta, "obs_index");
    const hasRaw = meta["has_raw"];
    const delim = metaStr(meta, "delimiter");
    const rowsExact = meta["row_count_exact"] === true;
    const rowsReason = metaStr(meta, "row_count_reason");
    const nSheets = metaNum(meta, "n_sheets");
    const primarySheet = metaStr(meta, "primary_sheet");
    const rowGroups = metaNum(meta, "num_row_groups");
    const grid: string[] = [];
    if (xEnc) grid.push(`<span class="k">X encoding</span><span class="v">${esc(xEnc)}</span>`);
    if (xDtype) grid.push(`<span class="k">X dtype</span><span class="v">${esc(xDtype)}</span>`);
    if (obsIndex)
      grid.push(`<span class="k">obs_index</span><span class="v">${esc(obsIndex)}</span>`);
    if (hasRaw !== undefined && hasRaw !== null) {
      const yes = hasRaw === true || hasRaw === "true";
      grid.push(
        `<span class="k">has_raw</span><span class="v ${yes ? "yes" : "no"}">${yes ? "✓" : "✗"}</span>`,
      );
    }
    if (delim)
      grid.push(
        `<span class="k">delimiter</span><span class="v">${esc(delim === "\t" ? "tab" : delim)}</span>`,
      );
    // Only worth a row when the headline number is NOT already the sheet count.
    if (nSheets !== null && !multiSheet)
      grid.push(`<span class="k">sheets</span><span class="v">${fmtNum(nSheets)}</span>`);
    if (primarySheet && !multiSheet)
      grid.push(`<span class="k">sheet</span><span class="v">${esc(primarySheet)}</span>`);
    if (rowGroups !== null)
      grid.push(`<span class="k">row groups</span><span class="v">${fmtNum(rowGroups)}</span>`);
    // Never let an estimated row count pass as a counted one.
    if (rowsN !== null && !rowsExact && (rowsReason || meta["row_count_exact"] === false))
      grid.push(
        `<span class="k">row count</span><span class="v no">${esc(rowCountNote(rowsReason, false))}</span>`,
      );
    const gridHtml = grid.length ? `<div class="metagrid">${grid.join("")}</div>` : "";
    datasetCard =
      `<div class="card"><div class="ch"><span class="sechdr">Dataset</span></div>` +
      (multiSheet
        ? `<div class="bigstat">${fmtNum(sheets.length)}` +
          `<span class="sub">worksheets — each sheet's shape is listed below</span></div>`
        : `<div class="bigstat">${nObs} <span class="x">×</span> ${nVars}` +
          `<span class="sub">${isMatrix ? "cells × genes" : "rows × columns"}</span></div>`) +
      gridHtml +
      `</div>`;
  }

  // ── COLUMN NAMES card (collapsed details.raw with a filter + ⧉all). ──
  // One card, two sources: a matrix file's per-cell annotation names, or a
  // table's header row. They are the same thing to a reader looking for a field
  // name, so they get the same control — filter box, copy-all, chips — and only
  // the wording changes. `filterNames` is what the filter + copy-all act on.
  const obs = metaList(meta, "obs_columns");
  const filterNames = obs.length ? obs : tableColumns;
  const columnsLabel = obs.length ? "Obs columns" : "Column names";
  let obsCard = "";
  if (filterNames.length) {
    obsCard =
      `<div class="card"><details class="raw"${obs.length ? "" : " open"}>` +
      `<summary>${columnsLabel} <span class="cbadge">${filterNames.length}</span></summary>` +
      `<div class="ch" style="margin:9px 0"><span></span><span class="r">` +
      `<input class="obsfilter" placeholder="filter columns…" />` +
      `<span class="miniact" data-copy-list="obs">⧉ all</span></span></div>` +
      `<div class="chips" data-obs-chips>${chipList(filterNames)}</div>` +
      `</details></div>`;
  }

  // ── SHEETS card (workbooks): one block per worksheet — its name, its shape,
  // and its own header row. A workbook is several tables in one file, so
  // collapsing it to the first sheet would hide most of what it contains.
  let sheetsCard = "";
  if (sheets.length) {
    const blocks = sheets
      .map((sh) => {
        const note = rowCountNote(sh.row_count_reason, sh.row_count_exact);
        const shape =
          `${sh.row_count === null ? "—" : fmtNum(sh.row_count)} × ${fmtNum(sh.n_columns)}` +
          (note ? ` <span style="color:var(--err)">(${esc(note)})</span>` : "");
        return (
          `<div class="sub-lbl asis">${esc(sh.name)} <span class="cbadge">${shape}</span></div>` +
          `<div class="chips">${chipList(sh.columns.slice(0, 64))}</div>` +
          (sh.columns.length > 64
            ? `<div class="trunc">64 of ${sh.columns.length} shown</div>`
            : "")
        );
      })
      .join("");
    sheetsCard =
      `<div class="card"><details class="raw" open>` +
      `<summary>Sheets <span class="cbadge">${sheets.length}</span></summary>` +
      `<div class="substruct">${blocks}</div></details></div>`;
  }

  // ── OBSM card (chips; umap keys get .chip.umap). ──
  const obsm = metaList(meta, "obsm");
  let obsmCard = "";
  if (obsm.length) {
    obsmCard =
      `<div class="card"><div class="ch"><span class="sechdr">Obsm (${obsm.length})</span></div>` +
      `<div class="chips">${chipList(obsm)}</div></div>`;
  }

  // ── STRUCTURE card (layers / uns_keys / varm / obsp as details.raw substruct). ──
  const structParts: string[] = [];
  for (const key of ["layers", "varm", "obsp", "uns_keys", "uns"]) {
    const items = metaList(meta, key);
    if (items.length) {
      const label = key === "uns_keys" ? "uns keys" : key;
      structParts.push(
        `<div class="sub-lbl">${esc(label)} (${items.length})</div>` +
          `<div class="chips">${chipList(items.slice(0, 48))}</div>` +
          (items.length > 48
            ? `<div class="trunc">48 of ${items.length} shown</div>`
            : ""),
      );
    }
  }
  const structCard = structParts.length
    ? `<div class="card"><details class="raw" open><summary>Structure</summary>` +
      `<div class="substruct">${structParts.join("")}</div></details></div>`
    : "";

  // ── LINEAGE card (refs/ref_by → clickable xrefs; dangling → .is-dangling). ──
  const danglingRefs = new Set(d.dangling_refs ?? []);
  const danglingRefBy = new Set(d.dangling_ref_by ?? []);
  const xref = (path: string, dir: "→" | "←", dangling: boolean): string => {
    const name = basename(path);
    if (dangling) {
      return (
        `<a class="xref is-dangling" aria-disabled="true" title="Target missing (dangling): ${esc(path)}">` +
        `<span class="xref__kind">${dir}</span><span class="xref__name">${esc(name)}</span></a>`
      );
    }
    return (
      `<a class="xref" href="#" data-path="${esc(path)}" title="${esc(path)}">` +
      `<span class="xref__kind">${dir}</span><span class="xref__name">${esc(name)}</span></a>`
    );
  };
  const refsHtml = d.refs.length
    ? d.refs.map((p) => xref(p, "→", danglingRefs.has(p))).join("")
    : `<div class="lineage-empty">No outgoing references.</div>`;
  const refByHtml = d.ref_by.length
    ? d.ref_by.map((p) => xref(p, "←", danglingRefBy.has(p))).join("")
    : `<div class="lineage-empty">Nothing references this entry.</div>`;
  const lineageCard =
    `<div class="card"><div class="ch"><span class="sechdr">Lineage</span></div>` +
    `<div id="inspector-lineage" data-view="list">` +
    `<div class="lineage-group"><span class="lineage-group__label">References ` +
    `<span class="lineage-group__count">${d.refs.length}</span></span>` +
    `<div class="lineage-list">${refsHtml}</div></div>` +
    `<div class="lineage-group"><span class="lineage-group__label">Referenced by ` +
    `<span class="lineage-group__count">${d.ref_by.length}</span></span>` +
    `<div class="lineage-list">${refByHtml}</div></div></div></div>`;

  // ── PROVENANCE card (category/ext/extractor + error). ──
  const provGrid: string[] = [
    `<span class="k">category</span><span class="v">${esc(r.category || "—")}</span>`,
    `<span class="k">ext</span><span class="v">${esc(r.ext || "—")}</span>`,
    `<span class="k">extractor</span><span class="v">${esc(r.extractor || "—")}</span>`,
  ];
  if (r.error) provGrid.push(`<span class="k">error</span><span class="v no">${esc(r.error)}</span>`);
  const provCard =
    `<div class="card"><details class="raw"><summary>Provenance</summary>` +
    `<div class="metagrid tight" style="margin-top:6px">${provGrid.join("")}</div></details></div>`;

  inspectorEl.innerHTML =
    `<div class="insp" style="--cc:${cc}">` +
    `<div class="hero"><div class="top"><span class="hg">${glyph}</span>` +
    `<span class="hname" title="${esc(r.name)}">${esc(r.name)}</span>` +
    `<span class="hbtns"><span class="cbtn primary" data-openfile data-path="${esc(r.path)}">↗ Open</span>` +
    `<span class="cbtn" data-copy="rel" data-path="${esc(r.path)}">⧉ Copy</span>` +
    `<span class="cbtn" data-reveal data-path="${esc(r.path)}">⇱ Reveal</span></span></div>` +
    `<div class="sub">${sub}</div><div class="colorrule"></div></div>` +
    `<div class="cards">` +
    pathCard +
    datasetCard +
    obsCard +
    sheetsCard +
    obsmCard +
    structCard +
    lineageCard +
    provCard +
    `</div></div>`;

  wireInspectorEvents(filterNames, columnsLabel.toLowerCase());
}

function wireInspectorEvents(obsColumns: string[], columnsLabel = "obs columns"): void {
  // Copy / reveal buttons (hero + path lines).
  inspectorEl.querySelectorAll<HTMLElement>("[data-copy]").forEach((node) => {
    node.addEventListener("click", () => {
      const path = node.dataset.path;
      const kind = node.dataset.copy as "abs" | "rel" | "posix" | "file_uri";
      if (path) void copyPathFlash(path, kind);
    });
  });
  inspectorEl.querySelectorAll<HTMLElement>("[data-reveal]").forEach((node) => {
    node.addEventListener("click", () => {
      const path = node.dataset.path;
      if (path) void api.revealInFinder(path).catch((e) => console.error("[lens] reveal:", e));
    });
  });
  // ↗ Open — launch the file in its default app (open, no -R). Distinct attribute from the tree's
  // data-open (expanded-folder flag) so the two never collide.
  inspectorEl.querySelectorAll<HTMLElement>("[data-openfile]").forEach((node) => {
    node.addEventListener("click", () => {
      const path = node.dataset.path;
      if (!path) return;
      void api
        .openFile(path)
        .then(() => flashStatus(`Opened · ${basename(path)}`))
        .catch((e) => {
          console.error("[lens] open_file:", e);
          flashStatus(`Could not open · ${basename(path)}`);
        });
    });
  });
  // Lineage xref clicks → navigate by path.
  inspectorEl.querySelectorAll<HTMLAnchorElement>(".xref:not(.is-dangling)").forEach((a) => {
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      const path = a.dataset.path;
      if (path) void navigateToPath(path);
    });
  });
  // Obs filter input → live-filter the chip list.
  const filter = inspectorEl.querySelector<HTMLInputElement>(".obsfilter");
  const chipBox = inspectorEl.querySelector<HTMLElement>("[data-obs-chips]");
  if (filter && chipBox) {
    filter.addEventListener("input", () => {
      const q = filter.value.trim().toLowerCase();
      const matched = q
        ? obsColumns.filter((c) => c.toLowerCase().includes(q))
        : obsColumns;
      chipBox.innerHTML = chipList(matched);
    });
  }
  // ⧉ all → copy the obs column list.
  const copyAll = inspectorEl.querySelector<HTMLElement>('[data-copy-list="obs"]');
  if (copyAll) {
    copyAll.addEventListener("click", () => {
      void navigator.clipboard
        .writeText(obsColumns.join("\n"))
        .then(() => flashStatus(`Copied ${obsColumns.length} ${columnsLabel}`));
    });
  }
}

async function copyPathFlash(path: string, kind: "abs" | "rel" | "posix" | "file_uri"): Promise<void> {
  try {
    const text = await api.copyPath(path, kind);
    await navigator.clipboard.writeText(text);
    flashStatus(`Copied ${kind} path · ${basename(path)}`);
  } catch (e) {
    console.error("[lens] copy_path failed:", e);
  }
}

// Resolve a path (from a lineage xref) to its entry and select it. IDs are unstable across
// rebuilds, so resolve by PATH: prefer the in-memory byId map (keyed by current ids), else a
// path: search. Selecting loads the inspector + preview.
async function navigateToPath(path: string): Promise<void> {
  // First try the in-memory rows (the tree we already loaded).
  const local = state.allRows.find((r) => r.path === path);
  if (local) {
    selectRow(local.id);
    return;
  }
  try {
    const res = await api.search(`path:"${path}"`, 0, 25);
    const exact = res.rows.find((r) => r.path === path) ?? res.rows[0];
    if (!exact) {
      console.warn("[lens] navigateToPath: no entry for", path);
      flashStatus(`Not in index · ${basename(path)}`);
      return;
    }
    selectRow(exact.id);
  } catch (e) {
    console.error("[lens] navigateToPath failed:", e);
  }
}

function renderPreview(r: Row): void {
  const ext = r.ext || extOf(r.path);
  previewEl.innerHTML = "";
  if (IMAGE_EXTS.has(ext)) {
    const stage = document.createElement("div");
    stage.className = ext === "svg" ? "stage-paper stage-paper--svg" : "stage-paper";
    stage.dataset.preview = "image";
    const img = document.createElement("img");
    img.alt = r.name;
    img.src = `repoindex://img/${r.id}`;
    img.addEventListener("error", () => showPreviewEmpty(`Could not load image: ${r.name}`));
    stage.appendChild(img);
    previewEl.appendChild(stage);
    return;
  }
  if (MD_EXTS.has(ext)) {
    const frame = document.createElement("iframe");
    frame.className = "md-doc";
    frame.setAttribute("sandbox", ""); // most restrictive: no scripts, no same-origin
    frame.setAttribute("data-preview", "markdown");
    frame.style.width = "100%";
    frame.style.border = "0";
    frame.style.minHeight = "60vh";
    frame.src = `repoindex://md/${r.id}`;
    previewEl.appendChild(frame);
    return;
  }
  // No preview renderer for this kind (code/notebook/pdf are Phase-2). A calm placeholder.
  showPreviewEmpty(
    `No preview for .${ext || r.category} — the inspector shows its metadata; reveal it in Finder to open.`,
  );
}

function showPreviewEmpty(msg: string): void {
  previewEl.innerHTML = `<p class="lens-empty">${esc(msg)}</p>`;
}

function showInspectorEmpty(): void {
  inspectorEl.innerHTML =
    `<div class="insp"><div class="empty"><span class="eg">▦</span>` +
    `<p class="em">select a file to inspect</p>` +
    `<div class="ek"><span><b>↑↓</b> move</span><span><b>⏎</b> inspect</span>` +
    `<span><b>/</b> search</span></div></div></div>`;
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// CONTEXT MENU — right-click a row → Reveal in Finder · Copy path (abs/rel/posix/file://).
// ═════════════════════════════════════════════════════════════════════════════════════════════

let menuPath = "";
function openContextMenu(x: number, y: number, path: string): void {
  menuPath = path;
  menuEl.classList.remove("hidden");
  const rect = menuEl.getBoundingClientRect();
  const mw = rect.width || 200;
  const mh = rect.height || 220;
  const px = x + mw > window.innerWidth ? window.innerWidth - mw - 6 : x;
  const py = y + mh > window.innerHeight ? window.innerHeight - mh - 6 : y;
  menuEl.style.left = `${Math.max(4, px)}px`;
  menuEl.style.top = `${Math.max(4, py)}px`;
}
function closeContextMenu(): void {
  menuEl.classList.add("hidden");
  menuPath = "";
}
function wireContextMenu(): void {
  menuEl.addEventListener("click", async (ev) => {
    const item = (ev.target as HTMLElement).closest<HTMLElement>(".menu-item");
    if (!item || !menuPath) return;
    const action = item.dataset.action;
    const path = menuPath;
    closeContextMenu();
    if (action === "reveal") {
      try {
        await api.revealInFinder(path);
      } catch (e) {
        console.error("[lens] reveal_in_finder failed:", e);
      }
      return;
    }
    if (action === "copy") {
      const kind = (item.dataset.kind as "abs" | "rel" | "posix" | "file_uri") ?? "abs";
      await copyPathFlash(path, kind);
    }
  });
  document.addEventListener("mousedown", (ev) => {
    if (!menuEl.classList.contains("hidden") && !menuEl.contains(ev.target as Node)) {
      closeContextMenu();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeContextMenu();
  });
  window.addEventListener("blur", closeContextMenu);
}

let statusFlashTimer: number | undefined;
function flashStatus(msg: string): void {
  const prev = statusHealthEl.textContent;
  statusHealthEl.textContent = msg;
  window.clearTimeout(statusFlashTimer);
  statusFlashTimer = window.setTimeout(() => {
    statusHealthEl.textContent = prev && !prev.startsWith("Copied") ? prev : "Index ready";
  }, 1600);
}

// ── Reindex (the ⟳ refresh button) ───────────────────────────────────────────────────────────
// Runs the canonical `repo_index` indexer (Rust `reindex` command, ~2.6s) to bring INDEX.sqlite
// current with disk, then reloads the in-memory rows + facets so NEW/CHANGED files appear. The
// button spins + is disabled while it runs (guarded against re-entrancy); feedback goes to the
// right-hand #statushealth span via flashStatus, leaving #statuscount to loadBrowse/renderLeft.
let reindexing = false;
async function doReindex(): Promise<void> {
  if (reindexing) return; // debounce — one indexer subprocess at a time
  reindexing = true;
  refreshBtn.disabled = true;
  refreshBtn.classList.add("spinning");
  window.clearTimeout(statusFlashTimer);
  statusHealthEl.textContent = "reindexing…";
  try {
    const res = await api.reindex();
    facetCache = null; // facets/types popover must repopulate from the fresh index
    // Reload the tree/list (and re-run an active search) from the reopened connection.
    await Promise.all([buildTypesPopover(), loadBrowse()]);
    statusHealthEl.textContent = "Index ready";
    flashStatus(`index refreshed · ${res.entries.toLocaleString("en-US")} files`);
  } catch (e) {
    console.error("[lens] reindex failed:", e);
    statusHealthEl.textContent = "Index ready";
    flashStatus(`reindex failed — see console`);
  } finally {
    reindexing = false;
    refreshBtn.disabled = false;
    refreshBtn.classList.remove("spinning");
  }
}

// ── Live index refresh (the `index-changed` event) ───────────────────────────────────────────
// The watcher reconciles INDEX.sqlite within ~0.5s of a disk change and emits `index-changed`
// after every committed flush. Nothing subscribed to it until now — which is why the INDEX was
// live but the WINDOW was not (`lib.rs` documented the frontend half as "may ignore it in Phase 1").
//
// Two properties matter for an always-on window:
//   * COALESCED — a burst (rsync, git checkout, npm install) fires many flushes. They collapse
//     into one reload via the debounce, and two reloads never overlap; a flush landing mid-reload
//     queues exactly one more pass rather than stacking.
//   * PLACE-PRESERVING — `openDirs` already survives a rebuild (buildTree restores `.open` in its
//     tally), so the expanded tree stays expanded. Scroll offset and the keyboard cursor do NOT
//     survive, so they are saved and restored around the reload.
//
// This is the whole-index reload (`list_all` + buildTree + repaint). The targeted per-directory
// refetch that `index-changed`'s payload was designed for is track B in LIVE_INDEX_PLAN.md.
const LIVE_REFRESH_DEBOUNCE_MS = 400;
let liveRefreshTimer = 0;
let liveRefreshing = false;
let liveRefreshQueued = false;

// ── HOLD (the #livebtn switch) ───────────────────────────────────────────────────────────────
// A whole-window reload lands as: the tree is torn down and rebuilt, the scroll offset moves, and
// the row that was under the pointer at mousedown is a different DOM node by mouseup — so the click
// is dropped. That is tolerable while browsing and intolerable while searching, and only the user
// knows which they are doing. Held is therefore an explicit, VISIBLE state, remembered across
// launches: nothing repaints until it is released, and the button counts what is waiting.
const LIVEHOLD_KEY = "lens.liveUpdatesHeld";
let liveHeld = (() => {
  try {
    return window.localStorage.getItem(LIVEHOLD_KEY) === "1";
  } catch {
    return false; // storage disabled — default to following the disk, as before this switch existed
  }
})();
let liveHeldPending = 0; // flushes that arrived while held (coalesced — one reload clears them all)

// ── WATCH HEALTH (what the pill is actually reporting) ───────────────────────────────────────
// The pill used to report ONE thing: whether the user had parked live updates. That is the only
// state it could report, because nothing ever asked the backend how the watch itself was doing —
// and the watch arms its OS-level watch exactly once, at startup. Unplug the drive this app's data
// normally lives on and that watch dies; replug it and nothing re-establishes it. The window keeps
// running, keeps showing the catalogue it already had, and silently stops noticing the disk. With
// no indicator there was no way to tell that apart from a quiet afternoon — the window was lying.
//
// So the pill now reports the BACKEND's health, with one local overlay: the hold switch is a
// frontend decision (localStorage), so "the user parked updates" is something only this side can
// know for certain. Precedence, and the reason for it:
//   unreachable / stopped  ALWAYS win — a held window and a blind window look identical from the
//                          outside, and only one of them is a problem the user must act on.
//   paused                 shown when the backend is otherwise live and the hold is on.
// State arrives two ways and both land here: `watch_status` (polled at boot and after every project
// switch) and the "watch-health" event (every transition, never a timer).
// Until the first answer lands this claims "live", which is exactly what the pill said before it
// could ask: at boot the catalogue has just been read, so the window IS current at that instant.
// Starting at "stopped" would be its own small lie, told before anyone had been asked.
let watchHealth: WatchHealth = "live";
let watchRoot = ""; // the folder the health above is ABOUT — the tooltip names it
let watchRootReachable = true;
/// Why the engine is down, straight from the backend — never inferred here.
let watchDegradeCode = "";
let watchDegradeMessage = "";
let watchRearms = 0;

/// Narrow the wire string. An unrecognised value is not a crash and not a guess: fall back to the
/// old boolean's meaning, which is the most this window can honestly claim to know.
function asHealth(word: string | undefined, watching: boolean | undefined): WatchHealth {
  switch (word) {
    case "live":
    case "paused":
    case "unreachable":
    case "stopped":
      return word;
    default:
      return watching === true ? "live" : "stopped";
  }
}

/// What the pill should SAY, given the backend's health and the local hold. Kept separate from the
/// painting so the precedence rule above is one readable expression rather than a nest of ifs.
function effectiveHealth(): WatchHealth {
  if (watchHealth === "unreachable" || watchHealth === "stopped") return watchHealth;
  return liveHeld ? "paused" : watchHealth;
}

/// The folder's short name, for prose ("FieldDrive"). The FULL path goes on its own line at the
/// end of the tooltip and never in a sentence — the person reading this is a scientist looking for
/// their files, not a programmer reading a log.
function watchFolderName(): string {
  const base = basename(watchRoot.replace(/\/+$/, ""));
  return base === "" ? "this folder" : base;
}

function paintLiveBtn(): void {
  const btn = document.getElementById("livebtn");
  const label = document.getElementById("livelabel");
  if (!btn || !label) return;
  const health = effectiveHealth();

  // The state is carried by the LABEL TEXT and the aria-label, never by the dot colour alone.
  btn.classList.toggle("held", health === "paused");
  btn.classList.toggle("watch-unreachable", health === "unreachable");
  btn.classList.toggle("watch-stopped", health === "stopped");
  // aria-pressed describes the HOLD switch (what the click does), which is still what this button
  // is; the health goes in aria-label, where it does not pretend to be a toggle position.
  btn.setAttribute("aria-pressed", liveHeld ? "true" : "false");

  // A folder Lens cannot reach, but which is not the headline state, is still worth one sentence —
  // "updates are off" and "your drive is gone" are not the same news.
  const alsoGone =
    health !== "unreachable" && watchRoot !== "" && !watchRootReachable
      ? `\nLens also cannot reach ${watchFolderName()} at the moment.`
      : "";
  const where = watchRoot === "" ? "" : `\nFolder: ${watchRoot}`;

  switch (health) {
    case "unreachable": {
      // The one state where the window would otherwise look completely normal while being wrong.
      // \uFE0E forces the TEXT presentation of ⚠ — without it WebKit renders it as a colour
      // emoji, which is both off-palette and the one glyph on the bar that ignores the theme.
      // The mark matters: it is the redundancy that makes this state readable without colour.
      label.textContent = "\u26a0\ufe0e Folder missing";
      btn.title =
        `Lens cannot reach ${watchFolderName()} right now, so this list has stopped updating and may ` +
        `already be out of date.\nReconnect the drive, or put the folder back, and Lens will catch up ` +
        `on its own.${where}`;
      btn.setAttribute(
        "aria-label",
        "Problem: Lens cannot reach the folder, so this list has stopped updating and may be out of date.",
      );
      return;
    }
    case "stopped": {
      // Calm on purpose — the commonest cause is a second Lens window, which is not a fault. But
      // the CAUSE comes from the backend now: naming a second window while the real problem is an
      // unplugged drive sends the user looking for the wrong thing.
      const folder = watchFolderName();
      const cause =
        watchDegradeCode === "locked_by_other"
          ? `Another Lens window already has ${folder} open, so this one is not updating the ` +
            `catalogue. Close the other window and Lens will pick it up within a few seconds.`
          : watchDegradeCode === "folder_unreachable"
            ? `Lens cannot reach ${folder}, so it is not updating the catalogue. Reconnect the ` +
              `drive and Lens will pick it up on its own.`
            : watchDegradeCode === "other"
              ? `Lens could not start watching ${folder}, so new files will not appear on their ` +
                `own.\nReason: ${watchDegradeMessage}`
              : `Lens is not watching ${folder} for changes, so new files will not appear on ` +
                `their own.\nUse the refresh button to check for new work.`;
      label.textContent = "Updates off";
      btn.title =
        watchRoot === ""
          ? "No folder is open, so there is nothing to keep up with yet."
          : `${cause}${alsoGone}${where}`;
      btn.setAttribute(
        "aria-label",
        watchDegradeCode === "locked_by_other"
          ? "Live updates are off because another Lens window has this folder open."
          : watchDegradeCode === "folder_unreachable"
            ? "Live updates are off because Lens cannot reach the folder."
            : "Live updates are off. New files will not appear on their own; use the refresh button.",
      );
      return;
    }
    case "paused": {
      // Name the number, not just the state: "Held" alone cannot tell you whether you are looking at
      // a stale window or simply a quiet one.
      label.textContent = liveHeldPending > 0 ? `Held · ${liveHeldPending}` : "Held";
      btn.title =
        (liveHeldPending > 0
          ? `${liveHeldPending} index update${liveHeldPending === 1 ? "" : "s"} waiting — click to apply`
          : "Live updates held — the window will not reload on disk changes. Click to resume.") +
        alsoGone;
      btn.setAttribute(
        "aria-label",
        liveHeldPending > 0
          ? `Live updates held. ${liveHeldPending} update${liveHeldPending === 1 ? "" : "s"} waiting. Click to apply.`
          : "Live updates held. Click to resume following the folder.",
      );
      return;
    }
    default: {
      label.textContent = "Live";
      // A watch that has had to be re-established is worth saying once: a folder that keeps
      // dropping out is a hardware story the user can act on, and it is invisible otherwise.
      const recovered =
        watchRearms > 0
          ? `\nLens has reconnected to this folder ${watchRearms === 1 ? "once" : `${watchRearms} times`} since it was opened.`
          : "";
      btn.title =
        `Live updates on — the window follows the index. Click to hold them.${recovered}` + alsoGone;
      btn.setAttribute("aria-label", "Live updates on. This list is following changes in the folder.");
    }
  }
}

function setLiveHeld(held: boolean): void {
  liveHeld = held;
  try {
    window.localStorage.setItem(LIVEHOLD_KEY, held ? "1" : "0");
  } catch {
    /* storage disabled — the switch still works for this session */
  }
  if (!held && liveHeldPending > 0) {
    liveHeldPending = 0;
    paintLiveBtn();
    void runLiveRefresh(); // apply everything that accumulated, in one pass
    return;
  }
  paintLiveBtn();
}

function wireLiveButton(): void {
  document.getElementById("livebtn")?.addEventListener("click", () => {
    setLiveHeld(!liveHeld);
    // In the two states where the pill is REPORTING a fault rather than showing a switch position,
    // the hold still flips (it is a remembered preference that matters again the moment the folder
    // comes back) but nothing on the pill moves. Say where the setting landed, so the click is not
    // simply swallowed.
    if (watchHealth === "unreachable" || watchHealth === "stopped") {
      flashStatus(
        liveHeld
          ? "live updates held · nothing to follow right now"
          : "live updates on · nothing to follow right now",
      );
    }
  });
  paintLiveBtn();
}

/// Take one new reading of the watch, repaint, and — only on a genuine change — say so in the
/// status line. Both sources (the poll and the event) funnel through here so the transition rules
/// live in exactly one place.
function applyWatchHealth(
  next: WatchHealth,
  root: string,
  rearms: number,
  rootReachable: boolean,
  degradeCode = "",
  degradeMessage = "",
): void {
  const prev = watchHealth;
  watchHealth = next;
  watchRoot = root;
  watchRearms = rearms;
  watchRootReachable = rootReachable;
  watchDegradeCode = degradeCode;
  watchDegradeMessage = degradeMessage;
  paintLiveBtn();
  if (next === prev) return;

  if (prev === "unreachable" && next === "live") {
    // The backend forces a whole-tree rescan when the folder comes back, and the existing
    // `index-changed` handler repaints off it — so the list fixes itself. Silently, though, which
    // from the user's side is indistinguishable from the failure they were just looking at. One
    // line in the status bar is the difference between "it recovered" and "did it recover?".
    flashStatus(
      liveHeld
        ? "folder reconnected · updates still held"
        : "folder reconnected · bringing the list up to date",
    );
  } else if (next === "unreachable") {
    // Said once, here, for the person who is looking at the file list rather than at the pill. The
    // pill keeps saying it afterwards, which is the part that has to persist.
    flashStatus("can't reach the folder · this list has stopped updating");
  }
}

// ── SELF-HEAL (the live engine retry) ────────────────────────────────────────────────────────
//
// The engine — the part that holds the write lock and runs the folder watch — is built once at
// launch and once per project switch, and nowhere else. So the two ways it can fail to start (a
// second Lens window holding the lock, or the drive being absent) both left the window read-only
// until it was restarted, however long ago the cause went away. This asks the backend to try again
// while the window is in that state, and stops the moment it succeeds.
//
// Backoff rather than a fixed interval: a lock genuinely held by a window the user is still using
// would otherwise be probed forever at full rate, and a probe takes a lock attempt on their disk.
const ENGINE_RETRY_MIN_MS = 5_000;
const ENGINE_RETRY_MAX_MS = 60_000;
let engineRetryTimer: number | null = null;
let engineRetryDelay = ENGINE_RETRY_MIN_MS;

/// Arm or disarm the retry. `wanted` is the backend's own verdict on whether trying again could
/// work — never a guess made here.
function scheduleEngineRetry(wanted: boolean): void {
  if (!wanted) {
    if (engineRetryTimer !== null) {
      window.clearTimeout(engineRetryTimer);
      engineRetryTimer = null;
    }
    engineRetryDelay = ENGINE_RETRY_MIN_MS; // a healthy engine resets the backoff
    return;
  }
  if (engineRetryTimer !== null) return; // already armed — never stack a second timer
  engineRetryTimer = window.setTimeout(() => {
    engineRetryTimer = null;
    void attemptEngineRetry();
  }, engineRetryDelay);
}

async function attemptEngineRetry(): Promise<void> {
  let live = false;
  try {
    live = await api.retryLiveEngine();
  } catch (e) {
    console.error("[lens] retry_live_engine failed:", e);
  }
  if (live) {
    engineRetryDelay = ENGINE_RETRY_MIN_MS;
    flashStatus("live updates resumed");
    // Repaint off the truth rather than assuming: the engine being up is not by itself proof the
    // folder is reachable, and `refreshWatchHealth` re-arms or disarms the timer for us.
    await refreshWatchHealth();
    return;
  }
  engineRetryDelay = Math.min(engineRetryDelay * 2, ENGINE_RETRY_MAX_MS);
  scheduleEngineRetry(true);
}

/// Ask the backend outright. Called at boot and after every project switch — the two moments where
/// there is no transition to listen for because the thing being watched has just changed.
async function refreshWatchHealth(): Promise<void> {
  try {
    const st = await api.watchStatus();
    applyWatchHealth(
      asHealth(st.health, st.watching),
      st.root ?? "",
      st.rearms ?? 0,
      st.root_reachable ?? true,
      st.degrade_code ?? "",
      st.degrade_message ?? "",
    );
    scheduleEngineRetry(st.retryable === true);
  } catch (e) {
    // Leave the pill exactly as it was. A failed status call says the backend is unwell; it does
    // not say the watch is dead, and painting "Folder missing" off a failed question would be a
    // second, louder lie than the one this whole feature exists to stop.
    console.error("[lens] watch_status failed:", e);
  }
}

/// The "watch-health" subscription. ONE per window, for the window's lifetime — a project switch
/// reuses the same webview, so re-subscribing on each switch is how a listener leak starts (the
/// `index-changed` subscription is single for the same reason).
let watchHealthUnlisten: UnlistenFn | null = null;

async function wireWatchHealth(): Promise<void> {
  if (watchHealthUnlisten) return; // already subscribed — never stack a second one
  try {
    watchHealthUnlisten = await listen<WatchHealthEvent>("watch-health", (ev) => {
      // A switch stops one watcher and starts another, so it emits transitions for BOTH the folder
      // being left and the one arriving, in that order — painting them would flicker the pill
      // through a state the user is not in. `teardownAndReload` polls the truth at the end of every
      // switch, so the honest thing here is to say nothing until it has.
      if (projectBusy) return;
      // Otherwise filter on the root the last STATUS reply named, NOT on activeProjectRoot: the
      // label is repainted a step after the reload, so a filter built on it is briefly one folder
      // behind — and `watchRoot` comes from the very reply that names the active folder.
      if (watchRoot !== "" && ev.payload.root !== watchRoot) return;
      const health = asHealth(ev.payload.health, undefined);
      // The event carries no reachability flag of its own, so infer the only thing it can honestly
      // support: "unreachable" means the folder is gone, anything else means assume it is there.
      // A poll is what can report "watching stopped AND the folder is missing" together.
      applyWatchHealth(health, ev.payload.root, ev.payload.rearms ?? 0, health !== "unreachable");
    });
    // Tauri keeps the webview alive across project switches, so this is the only teardown there is:
    // a reload or a window close must not leave the handler registered on the Rust side.
    window.addEventListener(
      "pagehide",
      () => {
        watchHealthUnlisten?.();
        watchHealthUnlisten = null;
        scheduleEngineRetry(false); // never leave a retry timer running on a dead window
      },
      { once: true },
    );
  } catch (e) {
    console.error("[lens] could not subscribe to watch-health:", e);
  }
}

async function runLiveRefresh(): Promise<void> {
  // Never race a bulk reindex or a project switch — both reload themselves when they finish.
  if (reindexing || projectBusy) return;
  if (liveRefreshing) {
    liveRefreshQueued = true;
    return;
  }
  liveRefreshing = true;
  const t0 = performance.now();
  // ⚠ THE SCROLLER IS `.scroll`, NOT `#list`. #list is a plain block inside the single scroll
  // container and its own scrollTop is always 0, so saving/restoring it preserved nothing — which
  // is exactly why a live reload appeared to "jump to a different position".
  const scroller = document.querySelector<HTMLElement>(".scroll");
  const scroll = scroller?.scrollTop ?? 0;
  const cursorPath = activeRow?.dataset.path ?? null;
  const cursorWasDir = activeRow?.classList.contains("dir") ?? false;
  try {
    await loadBrowse();
    // Re-pin the keyboard cursor by PATH (row ids churn across a reconcile), then restore the
    // scroll offset LAST — setActiveRow's scrollIntoView would otherwise move us off the spot.
    if (cursorPath !== null) {
      const node = listEl.querySelector<HTMLElement>(
        `.row${cursorWasDir ? ".dir" : ""}[data-path="${cssEscape(cursorPath)}"]`,
      );
      if (node) setActiveRow(node);
    }
    if (scroller) scroller.scrollTop = scroll;
    // The only visible confirmation that live-watch is alive, and the only place the reload cost
    // is observable (release builds ship no devtools).
    flashStatus(`live · ${state.total.toLocaleString("en-US")} files · ${Math.round(performance.now() - t0)} ms`);
  } catch (e) {
    console.error("[lens] live refresh failed:", e);
  } finally {
    liveRefreshing = false;
    if (liveRefreshQueued) {
      liveRefreshQueued = false;
      scheduleLiveRefresh();
    }
  }
}

function scheduleLiveRefresh(): void {
  if (liveHeld) {
    // Count it and repaint the button — but touch NOTHING else. The index itself keeps reconciling
    // in the background; only this window's repaint is withheld.
    liveHeldPending++;
    paintLiveBtn();
    return;
  }
  window.clearTimeout(liveRefreshTimer);
  liveRefreshTimer = window.setTimeout(() => void runLiveRefresh(), LIVE_REFRESH_DEBOUNCE_MS);
}

/// Subscribe to the watcher's post-flush event for the lifetime of the window. A project switch
/// reuses the same window, so events are filtered by root rather than re-subscribed.
async function wireLiveIndex(): Promise<void> {
  try {
    await listen<IndexChanged>("index-changed", (ev) => {
      if (activeProjectRoot && ev.payload.root !== activeProjectRoot) return;
      scheduleLiveRefresh();
    });
  } catch (e) {
    console.error("[lens] could not subscribe to index-changed:", e);
  }
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// HEALTH MODE — tiles (total · symlinks · broken · errors) from the loaded rows + facets().
// Broken/errors tables are a calm stub note (the index has no dir rows / broken data readily).
// ═════════════════════════════════════════════════════════════════════════════════════════════

function renderHealth(): void {
  const total = state.total;
  // symlink_ok is a per-row bool; count rows that are symlinks resolving OK as "symlinks".
  const symlinks = state.symlinkOkCount;
  const broken = state.allRows.filter((r) => r.symlink_ok === false && r.error).length;
  const errors = state.errorCount;
  const totalBytes = state.allRows.reduce((acc, r) => acc + (r.size_bytes || 0), 0);
  const clean = broken === 0 && errors === 0;

  setStatusCount(
    `${total.toLocaleString("en-US")} files · ${broken} broken · ${errors} errors`,
  );

  const tile = (num: string, lbl: string, kind: "" | "ok" | "err"): string =>
    `<div class="tile${kind ? " " + kind : ""}">` +
    (kind ? `<span class="tag">${kind === "ok" ? "✓" : "⚠"}</span>` : "") +
    `<span class="num">${num}</span><span class="lbl">${lbl}</span></div>`;

  let problemTables = "";
  if (errors > 0) {
    const errRows = state.allRows
      .filter((r) => r.error)
      .slice(0, 200)
      .map(
        (r) =>
          `<div class="prow" style="--rc:var(--warn)" data-id="${r.id}" data-path="${esc(r.path)}" title="open in inspector">` +
          `<span class="c path">${esc(r.path)}</span>` +
          `<span class="c extr">${esc(r.extractor || "—")}</span>` +
          `<span class="c errmsg">${esc(r.error ?? "")}</span>` +
          `<span class="cp" title="copy path">⧉</span></div>`,
      )
      .join("");
    problemTables =
      `<div class="probhdr"><span class="sechdr">Extraction errors <span class="cbadge">${errors}</span></span></div>` +
      `<div class="ptable errs"><div class="thead"><span>path</span><span>extractor</span>` +
      `<span>error</span><span></span></div>${errRows}</div>`;
  } else {
    problemTables =
      `<div class="allclear"><span class="ck">✓</span>` +
      `<span class="msg">all ${symlinks} links resolve <span class="d">·</span> no extraction errors</span></div>`;
  }

  listEl.innerHTML =
    `<div class="scan"><div class="scanhead"><span class="h">Index health</span>` +
    `<span class="sub">${total.toLocaleString("en-US")} files</span></div>` +
    `<div class="tiles">` +
    tile(total.toLocaleString("en-US"), "Total files", "") +
    tile(fmtBytes(totalBytes), "Total size", "") +
    tile(symlinks.toLocaleString("en-US"), "Symlinks", "") +
    tile(String(broken), "Broken", clean ? "ok" : broken === 0 ? "ok" : "err") +
    tile(String(errors), "Errors", errors === 0 ? "ok" : "err") +
    `</div>${problemTables}</div>`;

  // Health-table rows clickable into the inspector.
  listEl.querySelectorAll<HTMLElement>(".ptable .prow").forEach((prow) => {
    prow.addEventListener("click", (ev) => {
      const cp = (ev.target as HTMLElement).closest(".cp");
      if (cp) {
        const p = prow.dataset.path ?? "";
        if (p) void runRowAction("copy", p);
        return;
      }
      if (prow.dataset.id) selectRow(Number(prow.dataset.id));
    });
  });
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// CONTROLS — sort menu (#sortbtn), ruler-tap sort, mode segment, refresh.
// ═════════════════════════════════════════════════════════════════════════════════════════════

function setSort(key: SortKey): void {
  state.sort = key;
  sortLabelEl.textContent = SORT_LABEL[key];
  updateRuler();
  renderLeft();
}

// Paint the active-sort affordance on the column ruler — mirrors the foundation's applySort
// (render_html.py ~1753-1763): clear any prior highlight/arrow, then light the column matching
// state.sort with .is-active and a ▾ (primary: newest/largest) or ▴ (secondary: oldest/smallest)
// direction arrow. name is monodirectional (no arrow). The foundation runs this at boot too, so
// the default "newest" reads as the active modified sort from first paint (the missing cue that
// made the first modified click feel like it reversed an unmarked default).
function updateRuler(): void {
  rulerEl.querySelectorAll<HTMLElement>(".rh").forEach((h) => {
    h.classList.remove("is-active");
    h.querySelector(".arr")?.remove();
  });
  const mark = (sel: string, arrow: "" | "▾" | "▴"): void => {
    const h = rulerEl.querySelector<HTMLElement>(sel);
    if (!h) return;
    h.classList.add("is-active");
    if (arrow) {
      const a = document.createElement("span");
      a.className = "arr";
      a.textContent = arrow;
      h.appendChild(a);
    }
  };
  switch (state.sort) {
    case "newest": mark(".rmod", "▾"); break;
    case "oldest": mark(".rmod", "▴"); break;
    case "largest": mark(".rsize", "▾"); break;
    case "smallest": mark(".rsize", "▴"); break;
    case "name": mark(".rname", ""); break;
  }
}

// ── #sortbtn dropdown — a real glass menu over the 6 sort keys (was a fake cycle button) ───────
// Reuses the context-menu surface (.rowmenu.glass-3 = position:fixed z-index:60 + frosted glass)
// and .menu-item rows. Built in JS, appended to <body>, positioned under #sortbtn. Each row carries
// the same direction arrow as updateRuler (▾ primary, ▴ secondary, none for name/type); the active
// key gets a ✓ + accent. Dismiss mirrors wireContextMenu (outside mousedown · Escape · window blur).
let sortMenuEl: HTMLElement | null = null;

function buildSortMenu(): void {
  if (!sortMenuEl) return;
  const arrowFor = (k: SortKey): string =>
    k === "newest" || k === "largest" ? "▾" : k === "oldest" || k === "smallest" ? "▴" : "";
  sortMenuEl.innerHTML = SORT_CYCLE.map((k) => {
    const active = k === state.sort;
    const arr = arrowFor(k);
    return (
      `<button class="menu-item" data-sort-key="${k}"${active ? ' style="color:var(--accent)"' : ""}>` +
      `<span style="width:1em">${active ? "✓" : ""}</span>` +
      `<span>${esc(SORT_LABEL[k])}</span>` +
      `<span style="flex:1"></span>` +
      `<span class="arr" style="color:var(--muted)">${arr}</span></button>`
    );
  }).join("");
}

function openSortMenu(): void {
  if (!sortMenuEl) return;
  buildSortMenu(); // refresh the ✓/accent — ruler taps can change state.sort while closed
  filtersEl.classList.add("hidden"); // don't let two glass panels overlap
  typesBtn.classList.remove("lit");
  sortMenuEl.classList.remove("hidden");
  const r = sortBtn.getBoundingClientRect();
  const mw = sortMenuEl.getBoundingClientRect().width || 200;
  const left = r.left + mw > window.innerWidth ? window.innerWidth - mw - 6 : r.left;
  sortMenuEl.style.left = `${Math.max(4, left)}px`;
  sortMenuEl.style.top = `${r.bottom + 4}px`;
}

function closeSortMenu(): void {
  sortMenuEl?.classList.add("hidden");
}

function wireSortMenu(): void {
  sortMenuEl = document.createElement("div");
  sortMenuEl.className = "rowmenu hidden glass-3";
  sortMenuEl.setAttribute("role", "menu");
  document.body.appendChild(sortMenuEl);

  // #sortbtn toggles the menu (stopPropagation so the outside-click dismiss below skips it).
  sortBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (sortMenuEl?.classList.contains("hidden")) openSortMenu();
    else closeSortMenu();
  });

  sortMenuEl.addEventListener("click", (ev) => {
    const item = (ev.target as HTMLElement).closest<HTMLElement>(".menu-item[data-sort-key]");
    const key = item?.dataset.sortKey as SortKey | undefined;
    if (!key) return;
    setSort(key);
    closeSortMenu();
  });

  // Dismiss — outside mousedown (excluding #sortbtn + its child spans) · Escape · window blur.
  document.addEventListener("mousedown", (ev) => {
    if (
      sortMenuEl &&
      !sortMenuEl.classList.contains("hidden") &&
      !sortMenuEl.contains(ev.target as Node) &&
      ev.target !== sortBtn &&
      !sortBtn.contains(ev.target as Node)
    ) {
      closeSortMenu();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && sortMenuEl && !sortMenuEl.classList.contains("hidden")) closeSortMenu();
  });
  window.addEventListener("blur", closeSortMenu);
}

function wireControls(): void {
  // #sortbtn opens the sort dropdown menu (wired in wireSortMenu).

  // The column ruler headers are tappable to sort.
  rulerEl.addEventListener("click", (ev) => {
    const rh = (ev.target as HTMLElement).closest<HTMLElement>(".rh[data-sort]");
    if (!rh) return;
    const col = rh.dataset.sort;
    if (col === "name") setSort("name");
    else if (col === "size") setSort(state.sort === "largest" ? "smallest" : "largest");
    else if (col === "mtime") setSort(state.sort === "newest" ? "oldest" : "newest");
  });

  // #modeseg Browse | Health.
  modeSeg.addEventListener("click", (ev) => {
    const btn = (ev.target as HTMLElement).closest<HTMLButtonElement>("button[data-mode]");
    if (!btn) return;
    const mode = btn.dataset.mode as "browse" | "health";
    if (mode === state.mode) return;
    state.mode = mode;
    modeSeg.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    renderLeft();
  });

  // #refresh → reindex: re-scan the repo for new/changed files (runs the canonical indexer), then
  // reopen the DB + reload. NOT just a re-query — it brings INDEX.sqlite up to date with disk.
  refreshBtn.addEventListener("click", () => {
    void doReindex();
  });

}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// SPLITTER — drag the seam to resize the left pane. Writes --lens-left-w on the shell.
// ═════════════════════════════════════════════════════════════════════════════════════════════

function wireSplitter(): void {
  const splitter = el("splitter");
  let dragging = false;
  const onMove = (ev: MouseEvent): void => {
    if (!dragging) return;
    const min = 280;
    const max = Math.min(900, window.innerWidth - 360);
    const w = Math.max(min, Math.min(max, ev.clientX));
    appEl.style.setProperty("--lens-left-w", `${w}px`);
  };
  const onUp = (): void => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove("is-dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
  };
  splitter.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    dragging = true;
    splitter.classList.add("is-dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// TITLEBAR DRAG — explicit window-move fallback for the bar1 glass. wry's native
// data-tauri-drag-region handler is unreliable on macOS under window-vibrancy + transparent +
// Overlay titlebar (the focused webview / NSVisualEffectView can swallow the mousedown so the OS
// move never starts — drag then only works on an unfocused window's focus-click). On a left-button
// mousedown inside the drag region that is NOT an interactive control (the #search box or a
// #modeseg button), we kick off the OS drag ourselves. Additive: data-tauri-drag-region stays, so
// where wry's native path works this startDragging() is a harmless no-op.
function wireTitlebarDrag(): void {
  document.querySelectorAll<HTMLElement>("[data-tauri-drag-region]").forEach((region) => {
    region.addEventListener("mousedown", (ev) => {
      if (ev.button !== 0) return; // left button only
      const t = ev.target as HTMLElement;
      // Never hijack clicks on interactive controls living inside the bar (search box, seg buttons).
      if (t.closest("input, button, a, .filterchip, [data-act], [contenteditable]")) return;
      void getCurrentWindow()
        .startDragging()
        .catch((e) => console.error("[lens] startDragging failed:", e));
    });
  });
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// GLOBAL KEYBOARD (secondary): '/' focuses the search box; Escape blurs it.
// ═════════════════════════════════════════════════════════════════════════════════════════════

/// True while a modal card (first run · forget-a-folder · Python-not-found) is up. A modal owns the
/// keyboard completely: '/' must not yank focus into a search box the user cannot even see, and the
/// arrows must not walk a tree behind the scrim.
function modalIsOpen(): boolean {
  return document.querySelector(".scrim:not([hidden])") !== null;
}

function wireGlobalKeys(): void {
  document.addEventListener("keydown", (ev) => {
    if (modalIsOpen()) return;
    if (ev.key === "/" && document.activeElement !== searchEl) {
      ev.preventDefault();
      searchEl.focus();
    }
  });

  // ↑/↓/⏎/→/← steer the left pane from anywhere that is NOT a text field. Needed because #list is
  // the only focusable surface there: click a Best-matches row and focus lands on nothing, so
  // without this the arrows go dead the moment you use the band with the mouse.
  document.addEventListener("keydown", (ev) => {
    const NAV = ["ArrowDown", "ArrowUp", "Enter", "ArrowRight", "ArrowLeft"];
    if (!NAV.includes(ev.key)) return;
    if (modalIsOpen()) return; // a modal card owns the keyboard while it is up
    if (state.mode !== "browse") return; // Health replaces the left pane; there are no rows
    const t = ev.target as HTMLElement | null;
    if (t === searchEl) return; // the search box wires its own subset (↑/↓/⏎ only)
    if (t && listEl.contains(t)) return; // #list has its own listener; don't double-handle
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    // An open popover or dropdown menu owns the arrows while it is up.
    if (document.querySelector(".popover:not(.hidden), .rowmenu:not(.hidden)")) return;
    ev.preventDefault();
    handleListKey(ev.key);
  });
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// PROJECT SWITCHER — a .tbtn trigger injected into the title bar (left of #modeseg) opening a glass
// .rowmenu of every registered project (active marked) + "＋ Add folder…". Selecting a project
// switches the SINGLE resident backend connection; "Add folder…" picks a dir, registers + indexes
// it (live progress overlay), then switches to it. Every switch runs teardownAndReload(), which
// DROPS all per-project in-memory state before reloading — the hard memory constraint (one project's
// 350–900 MB must never accumulate across switches).
// ═════════════════════════════════════════════════════════════════════════════════════════════

let projBtn: HTMLButtonElement | null = null;
let projLabelEl: HTMLElement | null = null;
let projMenuEl: HTMLElement | null = null;
let activeProjectRoot = ""; // the resident project's root (marks the ✓ in the menu)
let projectBusy = false; // one switch/index flow at a time (the backend is one-at-a-time too)

/// Reset ALL per-project in-memory caches, drop the old tree/rows so they GC, then reload from the
/// (now repointed) backend connection — the same loadBrowse()+buildTypesPopover() path reindex uses.
async function teardownAndReload(): Promise<void> {
  // 1) Drop per-project state. Reassigning allRows/byId/treeRoot releases the previous project's
  //    (potentially 100k+ row) arrays + folder tree so they are eligible for GC before the reload
  //    allocates the new ones — never hold both projects' data at once.
  state.selectedId = null;
  state.query = "";
  state.parsed = parseQuery("");
  searchEl.value = "";
  activeFilters.clear(); // the old project's ext:/cat: filter chips must not carry over (buildTypesPopover re-renders them off)
  window.clearTimeout(inspectorLoadTimer); // cancel any pending keyboard-nav inspector load so loadInspector(oldId) can't fire against the new DB
  window.clearTimeout(tier3Timer); // ditto for a queued search_ids against the old connection
  state.allRows = [];
  state.byId = new Map();
  state.corpus = buildCorpus([]); // the old project's folded strings are ~2 × 61k strings — drop them
  state.tier3Paths = new Set();
  state.tier3Gen++; // any in-flight reply belongs to the old project
  state.total = 0;
  state.symlinkOkCount = 0;
  state.errorCount = 0;
  openDirs.clear();
  expandNotices.clear();
  dirTotals.clear();
  treeDirPaths = [];
  treeRoot = newDir("", "");
  facetCache = null; // facet counts belong to the old index — repopulate from the new one
  extCatCache = null; // ext→category map belongs to the old rows
  // 2) Clear the right pane (its selection/preview pointed at the old project's ids).
  showInspectorEmpty();
  showPreviewEmpty("Select a file to preview. Images render on the stage; markdown as a document.");
  // 3) Reload the tree/list + types popover from the reopened connection.
  await Promise.all([buildTypesPopover(), loadBrowse()]);
  // 4) Re-ask how the watch is doing. Every project switch, add and forget runs through here, and
  //    each one stops one watcher and (usually) starts another — the pill must not keep reporting
  //    the health of the folder we just left.
  await refreshWatchHealth();
}

/// Re-read the active project from the backend and paint its name on the trigger label.
///
/// Three outcomes, and they are NOT interchangeable — which is why this returns a word rather than
/// a nullable project. `current_project` now answers `null` for "no folder is registered", a real
/// and expected state (first launch, or the user just forgot their last folder) that must raise the
/// first-run screen. A thrown error is something else entirely — the backend is unwell — and must
/// NOT be mistaken for a fresh install, or a user with folders gets told they have none.
async function refreshProjectLabel(): Promise<"active" | "none" | "error"> {
  try {
    const cur = await api.currentProject();
    if (cur === null) {
      activeProjectRoot = "";
      if (projLabelEl) projLabelEl.textContent = "No folder";
      return "none";
    }
    activeProjectRoot = cur.root;
    if (projLabelEl) projLabelEl.textContent = cur.name;
    return "active";
  } catch (e) {
    console.error("[lens] currentProject failed:", e);
    return "error";
  }
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// MODAL CARDS — first run · forget-a-folder · Python-not-found.
// One shape (.scrim > .mcard.glass-3, lens.css §11) and one set of keyboard rules: Escape cancels,
// Tab cycles inside the card, and the destructive button is never what focus lands on.
// ═════════════════════════════════════════════════════════════════════════════════════════════

/// Mount a card on a fresh scrim and hand back a `close()`. Everything shared lives here so the two
/// dynamic cards cannot drift apart: focus is captured and restored, Escape and a backdrop click
/// both cancel, and Tab is trapped inside the card (a modal you can Tab out of leaves the keyboard
/// somewhere the user cannot see).
function mountModal(card: HTMLElement, onCancel: () => void): () => void {
  const prevFocus = document.activeElement as HTMLElement | null;
  const scrim = document.createElement("div");
  scrim.className = "scrim";
  scrim.appendChild(card);
  document.body.appendChild(scrim);

  const close = (): void => {
    scrim.remove();
    // Restore focus only if it is still meaningful — the trigger may have been re-rendered away
    // (the project menu rebuilds its rows), in which case document.body is the honest answer.
    if (prevFocus && prevFocus.isConnected) prevFocus.focus();
  };
  scrim.addEventListener("mousedown", (ev) => {
    if (ev.target === scrim) onCancel(); // click the wash, not the card ⇒ cancel
  });
  card.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      ev.stopPropagation(); // the document-level Escape handlers close menus; this one owns it
      onCancel();
      return;
    }
    if (ev.key !== "Tab") return;
    const focusable = [...card.querySelectorAll<HTMLElement>("button, input, [href], [tabindex]")].filter(
      (n) => !(n as HTMLButtonElement | HTMLInputElement).disabled && n.tabIndex !== -1,
    );
    if (focusable.length === 0) return;
    ev.preventDefault();
    const at = focusable.indexOf(document.activeElement as HTMLElement);
    const next = ev.shiftKey ? (at <= 0 ? focusable.length : at) - 1 : (at + 1) % focusable.length;
    focusable[next].focus();
  });
  return close;
}

// ── The Python notice ────────────────────────────────────────────────────────────────────────
// Written for someone who has never heard the word "interpreter". It appears in two places from
// this one builder: inline on the first-run card, and as the body of its own modal when the
// add-folder flow hits the same wall.

/// Plain words for `PythonStatus.source`, so "which Python won" is answerable without knowing how
/// the search works. Small print only — never the sentence that carries the message.
const PY_SOURCE_WORD: Record<string, string> = {
  env: "chosen by the LENS_PYTHON setting",
  settings: "the one you picked",
  probe: "already installed on this Mac",
  shell: "already installed on this Mac",
};

function pythonNoticeHtml(st: PythonStatus, withButton: boolean): string {
  const choose = withButton
    ? `<div class="pyacts"><button class="btn" data-act="pypick">Choose Python…</button></div>`
    : "";
  if (st.path === null) {
    return (
      `<div class="pyh"><span class="ic" aria-hidden="true">⚠</span>Lens cannot read folders yet</div>` +
      `<p>Lens uses a free program called Python to look inside your files. Most Macs used for ` +
      `research already have one, but Lens could not find it here.</p>` +
      `<p class="pyfine">Install it from python.org, or point Lens at a copy you already have — ` +
      `in the file window, ⌘⇧G lets you type a location such as /usr/bin/python3.</p>` +
      choose
    );
  }
  const how = PY_SOURCE_WORD[st.source] ?? "in use";
  const ver = st.version ? ` ${esc(st.version)}` : "";
  return (
    `<div class="pyh"><span class="ic" aria-hidden="true">✓</span>Python${ver} is ready</div>` +
    `<p>Lens can read your folders. It is using the copy ${esc(how)}.</p>` +
    `<p class="pypath">${esc(st.path)}</p>` +
    choose
  );
}

/// Open the file picker, hand the choice to the backend, and report back. Returns the new status,
/// or null when the user cancelled / the choice was refused (the caller shows the reason).
async function pickPython(host: HTMLElement): Promise<PythonStatus | null> {
  let picked: string | string[] | null;
  try {
    picked = await open({ directory: false, multiple: false, title: "Choose a Python program" });
  } catch (e) {
    console.error("[lens] python picker failed:", e);
    return null;
  }
  if (typeof picked !== "string") return null; // cancelled
  try {
    return await api.setPythonPath(picked);
  } catch (e) {
    // The backend refuses anything it cannot actually run, and says why in plain words — show that
    // sentence rather than a generic failure, because it is the one that tells them what to pick.
    console.error("[lens] set_python_path failed:", e);
    const err = document.createElement("p");
    err.className = "pyerr";
    err.textContent = String(e);
    host.querySelector(".pyerr")?.remove();
    host.appendChild(err);
    return null;
  }
}

/// Fill a `.pynotice` slot. Hidden entirely when Python is fine and `alwaysShow` is false — the
/// first-run screen should be calm, and "your computer is configured correctly" is not news.
async function paintPythonNotice(host: HTMLElement, alwaysShow = false): Promise<void> {
  let st: PythonStatus;
  try {
    st = await api.pythonStatus();
  } catch (e) {
    console.error("[lens] python_status failed:", e);
    host.hidden = true;
    return;
  }
  if (st.path !== null && !alwaysShow) {
    host.hidden = true;
    return;
  }
  host.classList.toggle("ok", st.path !== null);
  host.innerHTML = pythonNoticeHtml(st, st.path === null);
  host.hidden = false;
  host.querySelector<HTMLButtonElement>('[data-act="pypick"]')?.addEventListener("click", () => {
    void pickPython(host).then((next) => {
      if (next) void paintPythonNotice(host, true); // repaint green so the fix is visibly done
    });
  });
}

/// The add-folder flow's guard. Resolves true when there is a usable Python — either there already
/// was one, or the user just chose one. False means they backed out, and the caller must stop:
/// walking them through a folder picker only to fail at indexing is the worst of both.
async function ensurePythonOrExplain(): Promise<boolean> {
  let st: PythonStatus;
  try {
    st = await api.pythonStatus();
  } catch (e) {
    // Can't tell — don't invent a blocker. Let the flow run; if it really is broken, indexing
    // surfaces its own error.
    console.error("[lens] python_status failed:", e);
    return true;
  }
  if (st.path !== null) return true;

  return await new Promise<boolean>((resolve) => {
    const card = document.createElement("div");
    card.className = "mcard glass-3";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "pyh");
    card.innerHTML =
      `<h2 class="mtitle" id="pyh">One thing is missing</h2>` +
      `<div class="pynotice"></div>` +
      `<div class="macts"><button class="btn" data-act="later">Not now</button></div>`;
    const notice = card.querySelector<HTMLElement>(".pynotice")!;
    notice.innerHTML = pythonNoticeHtml(st, true);

    let close = (): void => {};
    const finish = (ok: boolean): void => {
      close();
      resolve(ok);
    };
    close = mountModal(card, () => finish(false));
    // Delegated, not bound per button: a failed pick repaints the notice's innerHTML, which would
    // throw away a listener attached to the old "Choose Python…" node.
    card.addEventListener("click", (ev) => {
      const act = (ev.target as HTMLElement).closest<HTMLElement>("[data-act]")?.dataset.act;
      if (act === "later") {
        finish(false);
      } else if (act === "pypick") {
        void pickPython(notice).then((next) => {
          if (!next) return; // cancelled, or refused — pickPython has printed the reason in place
          if (next.path) finish(true); // fixed — carry straight on into the folder picker
          else notice.innerHTML = pythonNoticeHtml(next, true);
        });
      }
    });
    card.querySelector<HTMLButtonElement>('[data-act="later"]')?.focus();
  });
}

// ── The first-run screen ─────────────────────────────────────────────────────────────────────

function showWelcome(): void {
  welcomeEl.hidden = false;
  void paintPythonNotice(welcomePyEl); // silent when Python is fine
  welcomePickBtn.focus();
}
function hideWelcome(): void {
  welcomeEl.hidden = true;
}

// ── "Forget this folder" — the confirm card ──────────────────────────────────────────────────

/// Ask before forgetting `ps`; resolves the user's answer, or null if they backed out.
/// Deliberately NOT a browser confirm(): the decision needs the folder's real path, its ⚠ state and
/// the size of what would be deleted on screen at the moment of deciding.
function confirmForget(ps: ProjectStatus): Promise<{ deleteIndex: boolean } | null> {
  return new Promise((resolve) => {
    const card = document.createElement("div");
    card.className = "mcard glass-3";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "forget-h");

    const missing = !ps.folder_exists
      ? `<div class="mwarn"><span class="ic" aria-hidden="true">⚠</span><span>Lens cannot see this ` +
        `folder at the moment — it may be on a drive that is not plugged in, or it may have been ` +
        `moved or renamed.</span></div>`
      : "";
    // The checkbox is off by default and disabled with its reason showing when there is nothing to
    // delete. A disabled control with no stated reason reads as a bug.
    const why = ps.has_index
      ? `<span class="why">These are the notes Lens made about the folder, not your files. ` +
        `Deleting them frees the space; if you add the folder back later Lens reads it again from scratch.</span>`
      : `<span class="why">There is nothing to delete — Lens has not built a catalogue for this folder.</span>`;
    const size = ps.index_bytes > 0 ? ` (${fmtBytes(ps.index_bytes)})` : "";

    card.innerHTML =
      `<h2 class="mtitle" id="forget-h">Forget “${esc(ps.name)}”?</h2>` +
      `<p class="mbody">Lens will stop listing this folder. Nothing inside it is changed, moved or deleted.</p>` +
      `<p class="mpath">${esc(ps.root)}</p>` +
      missing +
      `<label class="mcheck${ps.has_index ? "" : " is-off"}">` +
      `<input type="checkbox" data-act="delidx"${ps.has_index ? "" : " disabled"} />` +
      `<span>Also delete its index files${size}${why}</span></label>` +
      `<div class="macts">` +
      `<button class="btn" data-act="cancel">Cancel</button>` +
      `<button class="btn danger" data-act="forget">Forget</button>` +
      `</div>`;

    const box = card.querySelector<HTMLInputElement>('[data-act="delidx"]')!;
    let close = (): void => {};
    const finish = (answer: { deleteIndex: boolean } | null): void => {
      close();
      resolve(answer);
    };
    close = mountModal(card, () => finish(null));
    card.querySelector<HTMLButtonElement>('[data-act="cancel"]')?.addEventListener("click", () => finish(null));
    card.querySelector<HTMLButtonElement>('[data-act="forget"]')?.addEventListener("click", () =>
      finish({ deleteIndex: box.checked && ps.has_index }),
    );
    // Cancel takes the initial focus, never Forget: a stray Return must not delete anything.
    card.querySelector<HTMLButtonElement>('[data-act="cancel"]')?.focus();
  });
}

/// Confirm, call `remove_project`, then follow `now_active` — the backend has already moved off the
/// folder (stopping its watcher and releasing its writer lock) if it was the open one, so all the
/// frontend owes is a repaint of whatever it landed on.
async function forgetFolderFlow(ps: ProjectStatus): Promise<void> {
  if (projectBusy) return;
  const answer = await confirmForget(ps);
  if (answer === null) return;
  projectBusy = true;
  try {
    const rep = await api.removeProject(ps.root, answer.deleteIndex);
    if (rep.now_active === null) {
      // Nothing registered any more. Drop this project's rows before painting the first-run screen —
      // a forgotten folder's 350–900 MB of in-memory state must not survive its own removal.
      activeProjectRoot = "";
      if (projLabelEl) projLabelEl.textContent = "No folder";
      await teardownAndReload();
      showWelcome();
    } else if (rep.now_active !== activeProjectRoot) {
      // It was the open folder and the backend moved us to another one.
      await teardownAndReload();
      await refreshProjectLabel();
    }
    const freed =
      rep.index_deleted && rep.bytes_freed > 0 ? ` · freed ${fmtBytes(rep.bytes_freed)}` : "";
    // Asking to delete and silently not deleting is worse than not offering: say so.
    const kept = answer.deleteIndex && !rep.index_deleted ? " · index files could not be deleted" : "";
    flashStatus(`forgot ${ps.name}${freed}${kept}`);
  } catch (e) {
    console.error("[lens] remove_project failed:", e);
    flashStatus(`could not forget ${ps.name} — see console`);
  } finally {
    projectBusy = false;
  }
}

// ── Index-progress overlay — a centered glass card showing the latest phase line + spinner + elapsed.
interface IndexOverlay {
  setPhase(line: string): void;
  setElapsed(seconds: number): void;
  close(): void;
}
function showIndexOverlay(root: string): IndexOverlay {
  const backdrop = document.createElement("div");
  backdrop.style.cssText =
    "position:fixed;inset:0;z-index:90;display:flex;align-items:center;justify-content:center;" +
    "background:rgba(0,0,0,0.34);";
  const card = document.createElement("div");
  card.className = "glass-3"; // reuse the frosted surface (bg/blur/border/radius/lift)
  card.style.cssText =
    "min-width:360px;max-width:560px;padding:22px 24px;border-radius:14px;" +
    "display:flex;flex-direction:column;gap:12px;";
  const head = document.createElement("div");
  head.style.cssText = "display:flex;align-items:center;gap:10px;";
  const spin = document.createElement("span");
  spin.textContent = "⟳"; // reuse the @keyframes lens-spin (same as #refresh.spinning)
  spin.style.cssText =
    "display:inline-block;font-size:18px;color:var(--accent);animation:lens-spin 0.8s linear infinite;";
  const title = document.createElement("div");
  title.textContent = `Indexing ${basename(root) || root}…`;
  title.style.cssText = "font-weight:600;color:var(--fg);";
  head.append(spin, title);
  const phase = document.createElement("div");
  phase.style.cssText =
    "font-family:var(--font-mono);font-size:var(--fs-sm);color:var(--muted);" +
    "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
  phase.textContent = "starting…";
  const elapsed = document.createElement("div");
  elapsed.style.cssText = "font-size:var(--fs-xs);color:var(--faint);";
  elapsed.textContent = "0.0s";
  card.append(head, phase, elapsed);
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);
  return {
    setPhase: (line) => {
      phase.textContent = line;
    },
    setElapsed: (seconds) => {
      elapsed.textContent = `${seconds.toFixed(1)}s`;
    },
    close: () => {
      backdrop.remove(); // drop the overlay node (and its captured closures with the unlisten below)
    },
  };
}

/// Index `root` with a live overlay: subscribe to "index-progress" BEFORE invoking index_project so
/// no early phase line is missed, then tear EVERYTHING down (unlisten + clear timer + remove overlay)
/// in finally so no listener/closure/timer leaks across operations (memory constraint).
async function indexWithProgress(root: string): Promise<void> {
  const overlay = showIndexOverlay(root);
  const start = Date.now();
  const timer = window.setInterval(() => overlay.setElapsed((Date.now() - start) / 1000), 200);
  let unlisten: UnlistenFn | null = null;
  try {
    unlisten = await listen<IndexProgress>("index-progress", (ev) => {
      if (ev.payload.root === root) overlay.setPhase(ev.payload.line);
    });
    await api.indexProject(root);
  } finally {
    if (unlisten) unlisten();
    window.clearInterval(timer);
    overlay.close();
  }
}

/// Make `root` active: switch the resident connection (indexing first if it has no index yet), then
/// teardown + reload + repaint the label. `projectBusy` serialises overlapping requests.
///
/// There is deliberately NO "already active, nothing to do" early return. When a project's drive was
/// unplugged at boot, the registry still NAMES it as the active one while the resident connection is
/// parked on the empty placeholder index — so re-picking it from the menu after replugging the drive
/// is exactly the recovery gesture, and that early return made it a dead click, leaving the user
/// staring at an empty tree the backend already knew how to fix (`switch_project` compares the
/// resident index PATH as well as the root, so it does the right thing here and a genuinely
/// redundant re-pick costs one cheap no-op).
async function activateProject(root: string): Promise<void> {
  if (projectBusy) return;
  projectBusy = true;
  try {
    try {
      await api.switchProject(root);
    } catch (e) {
      // switch_project errors when the target has no index yet → build it (with progress), then switch.
      console.warn("[lens] switch_project failed; indexing first:", e);
      await indexWithProgress(root);
      await api.switchProject(root);
    }
    await teardownAndReload();
    await refreshProjectLabel();
    hideWelcome(); // a folder is open again; the first-run screen has nothing left to say
    flashStatus(`switched to ${projLabelEl?.textContent ?? "project"}`);
  } catch (e) {
    console.error("[lens] activateProject failed:", e);
    flashStatus("project switch failed — see console");
  } finally {
    projectBusy = false;
  }
}

/// "Add folder…": check Python can run at all, pick a directory, register it, index it (live
/// overlay), then switch to it.
async function addFolderFlow(): Promise<void> {
  if (projectBusy) return;
  // Python first, BEFORE the folder picker. Without an interpreter `index_project` cannot do
  // anything, and walking someone through choosing a folder only to fail at the indexing step is
  // the worst possible order to discover that in.
  if (!(await ensurePythonOrExplain())) return;
  let picked: string | string[] | null;
  try {
    picked = await open({ directory: true, multiple: false, title: "Add a folder to index" });
  } catch (e) {
    console.error("[lens] folder picker failed:", e);
    return;
  }
  if (typeof picked !== "string") return; // cancelled (null) — nothing to do
  const root = picked;
  projectBusy = true;
  try {
    await api.addProject(root); // register (idempotent; name derived backend-side)
    await indexWithProgress(root); // build its index with the progress overlay
    await api.switchProject(root); // now switchable (index exists)
    await teardownAndReload();
    await refreshProjectLabel();
    hideWelcome(); // first run is over the moment the first folder is in
    flashStatus(`added ${projLabelEl?.textContent ?? "project"}`);
  } catch (e) {
    console.error("[lens] addFolderFlow failed:", e);
    flashStatus("add folder failed — see console");
  } finally {
    projectBusy = false;
  }
}

// ── The dropdown menu (reuses the #sortbtn / context-menu glass pattern: .rowmenu.glass-3 of
//    .menu-item rows, appended to <body>, positioned under the trigger, outside-mousedown/Esc/blur).
/// The rows as the menu last painted them. The ✕ handler needs the FULL status of the row it was
/// clicked on (path, ⚠ state, index size) to build an honest confirm card, and re-fetching at click
/// time would race the very removal it is about to do.
let projStatuses: ProjectStatus[] = [];

async function buildProjectMenu(): Promise<void> {
  if (!projMenuEl) return;
  try {
    projStatuses = await api.listProjectsStatus();
  } catch (e) {
    console.error("[lens] list_projects_status failed:", e);
    projStatuses = [];
  }
  const rows = projStatuses
    .map((p) => {
      const active = p.is_active || p.root === activeProjectRoot;
      // The two state markers are the POINT of this menu. Until now a folder that had been moved,
      // renamed or left on an unplugged drive rendered exactly like a working one, and the only
      // clue was that clicking it did nothing useful.
      let marker = "";
      if (!p.folder_exists) {
        marker =
          `<span class="mk warn" title="Lens cannot see this folder right now — ${esc(p.root)}"` +
          ` aria-label="not on this Mac right now">⚠</span>`;
      } else if (!p.has_index) {
        marker = `<span class="mk" title="Lens has not read this folder yet. Choosing it will read it now.">not indexed yet</span>`;
      }
      return (
        `<div class="menu-row${p.folder_exists ? "" : " is-missing"}" role="none">` +
        `<button class="menu-item" role="menuitem" data-proj-root="${esc(p.root)}" title="${esc(p.root)}"` +
        `${active ? ' style="color:var(--accent)"' : ""}>` +
        `<span class="tick" aria-hidden="true">${active ? "✓" : ""}</span>` +
        `<span class="nm">${esc(p.name)}</span>${marker}</button>` +
        // Sibling of the row button, never nested inside it: a button inside a button is invalid
        // HTML and the outer one swallows the inner one's clicks.
        `<button class="menu-x" role="menuitem" data-proj-forget="${esc(p.root)}"` +
        ` aria-label="Forget ${esc(p.name)}" title="Forget “${esc(p.name)}” — stop listing this folder">✕</button>` +
        `</div>`
      );
    })
    .join("");
  projMenuEl.innerHTML =
    rows +
    `<div class="menu-sep"></div>` +
    `<button class="menu-item" role="menuitem" data-proj-add="1"><span class="tick" aria-hidden="true">＋</span>` +
    `<span class="nm">Add folder…</span></button>`;
}

async function openProjectMenu(): Promise<void> {
  if (!projMenuEl || !projBtn) return;
  await buildProjectMenu();
  projMenuEl.classList.remove("hidden");
  const r = projBtn.getBoundingClientRect();
  const mw = projMenuEl.getBoundingClientRect().width || 220;
  const left = r.left + mw > window.innerWidth ? window.innerWidth - mw - 6 : r.left;
  projMenuEl.style.left = `${Math.max(4, left)}px`;
  projMenuEl.style.top = `${r.bottom + 4}px`;
}

function closeProjectMenu(): void {
  projMenuEl?.classList.add("hidden");
}

function wireProjectSwitcher(): void {
  // Trigger button — reuse .tbtn (same look as #sortbtn/#typesbtn). Injected into the title bar
  // just LEFT of the Browse/Health segment (#modeseg, after the .sb-flex spacer) — no index.html edit.
  projBtn = document.createElement("button");
  projBtn.className = "tbtn";
  projBtn.id = "projbtn";
  projBtn.title = "Switch project / add a folder";
  projBtn.innerHTML = `◇ <span id="projlabel">…</span> <span class="ar">▾</span>`;
  projLabelEl = projBtn.querySelector<HTMLElement>("#projlabel");
  modeSeg.before(projBtn); // parentElement is header.bar1; lands after .sb-flex, before #modeseg

  // The menu surface. #projmenu (lens.css §12) widens it and styles the removable rows; the
  // .rowmenu.glass-3 material is the same one the right-click menu uses.
  projMenuEl = document.createElement("div");
  projMenuEl.id = "projmenu";
  projMenuEl.className = "rowmenu hidden glass-3";
  projMenuEl.setAttribute("role", "menu");
  document.body.appendChild(projMenuEl);

  // The first-run screen's primary button runs the same add-folder flow as the menu item — one
  // path, so the Python guard and the progress overlay can never be wired to only one of them.
  welcomePickBtn.addEventListener("click", () => void addFolderFlow());

  // Trigger toggles the menu (stopPropagation so the outside-click dismiss below skips this click).
  projBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (projMenuEl?.classList.contains("hidden")) void openProjectMenu();
    else closeProjectMenu();
  });

  // Selection — ✕ forgets, a project row switches, the "Add folder…" row opens the picker. The ✕
  // is checked FIRST and is a sibling of the row button, so it can never double-fire a switch.
  projMenuEl.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement;
    const forget = target.closest<HTMLElement>("[data-proj-forget]");
    if (forget?.dataset.projForget) {
      closeProjectMenu();
      const ps = projStatuses.find((p) => p.root === forget.dataset.projForget);
      if (ps) void forgetFolderFlow(ps);
      return;
    }
    const item = target.closest<HTMLElement>(".menu-item");
    if (!item) return;
    closeProjectMenu();
    if (item.dataset.projAdd) {
      void addFolderFlow();
    } else if (item.dataset.projRoot) {
      void activateProject(item.dataset.projRoot);
    }
  });

  // Dismiss — outside mousedown (excluding the trigger + its children) · Escape · window blur.
  document.addEventListener("mousedown", (ev) => {
    if (
      projMenuEl &&
      !projMenuEl.classList.contains("hidden") &&
      !projMenuEl.contains(ev.target as Node) &&
      ev.target !== projBtn &&
      !projBtn?.contains(ev.target as Node)
    ) {
      closeProjectMenu();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && projMenuEl && !projMenuEl.classList.contains("hidden")) {
      closeProjectMenu();
    }
  });
  window.addEventListener("blur", closeProjectMenu);
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// BOOT — assert ids, wire everything, load facets + the whole tree, set empty inspector/preview.
// ═════════════════════════════════════════════════════════════════════════════════════════════

async function loadBrowse(): Promise<void> {
  setStatusCount("loading…");
  try {
    // Fetch ALL rows once (the perf core: build the tree from this, render lazily). Uses the
    // dedicated unclamped list_all — list_page caps at MAX_PAGE_LIMIT (500), which silently
    // truncated the whole browse tree to 500 entries on the real (38k-row) index.
    const rows = await api.listAll();
    state.allRows = rows;
    state.byId = new Map(rows.map((r) => [r.id, r]));
    state.total = rows.length;
    state.symlinkOkCount = rows.filter((r) => r.symlink_ok).length;
    state.errorCount = rows.filter((r) => r.error).length;
    extCatCache = null; // rows changed → rebuild the ext→category map lazily
    // The corpus (one folded name + path per row) is built ONCE per load — measured ~24 ms over
    // 61.5k rows — so every later keystroke matches precomputed strings instead of re-folding.
    // applyQuery must never rebuild it; only this path and a live refresh do.
    state.corpus = buildCorpus(rows);
    buildDirTotals(rows);
    // Any tier-3 reply still in flight was computed against the PREVIOUS row set; retire it by
    // bumping the generation. applyQuery below schedules a fresh one.
    state.tier3Gen++;
  } catch (e) {
    console.error("[lens] loadBrowse failed:", e);
    listEl.innerHTML = `<p class="lens-empty">Could not load the index.</p>`;
    return;
  }
  // Rebuild the tree THROUGH the active filter (a live refresh must not silently drop it), then
  // paint band + tree + status.
  applyQuery();
}

async function boot(): Promise<void> {
  const ids = [
    "search",
    "filters",
    "list",
    "topband",
    "filterchips",
    "splitter",
    "preview",
    "inspector",
    "rowmenu",
    "ruler",
    "statuscount",
    "statushealth",
    "refresh",
    "sortbtn",
    "typesbtn",
    "livebtn",
    "livelabel",
    "modeseg",
    "welcome",
    "welcomepick",
    "welcomepython",
  ] as const;
  const missing = ids.filter((id) => document.getElementById(id) === null);
  if (missing.length > 0) {
    console.error("[lens] shell skeleton missing required ids:", missing.join(", "));
    return;
  }

  wireListEvents();
  wireBandEvents();
  wireSearch();
  wireFilterChips();
  wireTypesPopover();
  wireContextMenu();
  wireSplitter();
  wireControls();
  wireSortMenu();
  wireGlobalKeys();
  wireLiveButton();
  wireTitlebarDrag();
  wireProjectSwitcher();

  // Resolve the bundled fallback drag-preview icon once (used as the OS drag image for non-raster
  // files in drag-out; raster files use themselves). Fire-and-forget — a drag before this resolves
  // simply skips non-raster previews.
  void resolveResource("icons/64x64.png")
    .then((p) => {
      dragFallbackIcon = p;
    })
    .catch((e) => console.error("[lens] resolveResource(drag icon) failed:", e));

  // Initial control labels.
  sortLabelEl.textContent = SORT_LABEL[state.sort];
  updateRuler();

  // Awaited, unlike before: its answer decides whether this is a first run. `"none"` is the new
  // representable state — no folder registered — and only that one raises the welcome screen. A
  // thrown error must NOT, or a user whose backend hiccuped is told they have no folders.
  if ((await refreshProjectLabel()) === "none") showWelcome();

  showInspectorEmpty();
  showPreviewEmpty("Select a file to preview. Images render on the stage; markdown as a document.");

  // Build the Types popover + load the whole tree in parallel.
  await Promise.all([buildTypesPopover(), loadBrowse()]);

  // Subscribe AFTER the first load so an event during boot can't race a half-built tree; the
  // watcher's own startup reconcile has already run by then, and any change we miss in that gap
  // lands on the next flush.
  void wireLiveIndex();

  // The watch's own health: subscribe to the transitions first, then take one reading for the
  // state we were already in when the window opened (a transition-only feed cannot tell you that —
  // a folder that was unplugged before launch never transitions).
  await wireWatchHealth();
  await refreshWatchHealth();

  console.info("[lens] mounted; total entries =", state.total);
}

window.addEventListener("DOMContentLoaded", () => {
  void boot();
});
