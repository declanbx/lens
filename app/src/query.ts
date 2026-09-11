// Lens — the query engine (LOCKED SPEC §2.1).
//
// PURE MODULE. No DOM, no globals, no imports from main.ts, no Tauri. Everything here is a total
// function over plain data so `node --test src/query.test.ts` can pin it without a browser shell.
//
// It owns four things:
//   1. THE ONE TOKENIZER. `foldToken` + `tokenize` are the only place in the app that decides what
//      a token is. The Rust side (`search_ids`) receives ALREADY folded/split tokens and parses
//      nothing, so the two can never disagree (spec §3, finding C2).
//   2. THE FILTER PREDICATE. `ext:` is EXACT equality, `cat:`/`type:` is SUBSTRING, OR within a
//      kind and AND across kinds — byte-for-byte the semantics of db.rs `parse_query`/`build_where`
//      (findings C3/C4, regressions B2 + B10).
//   3. THE TREE PREDICATE. `treeRows` is what the folder tree is rebuilt from: a filter narrows
//      the tree IN PLACE, it never flips the pane into a separate grouped mode.
//   4. THE BAND RANKING. `topHits` returns at most `BAND_SIZE` ranked hits in four tiers
//      (0 folder · 1 filename · 2 path · 3 meta), by bounded insertion — never a sort over the
//      candidate set.
//
// MEMORY discipline (CONTRACT.md): nothing here parses `meta`. Tier 3 arrives as a set of paths
// the backend already resolved; this module only joins on it.

// ── Structural DTO ──────────────────────────────────────────────────────────────────────────
// A structural subset of main.ts's `Row`. Declared here (NOT imported) so this module stays
// importable by a test with no app shell. `Row` is assignable to `RowLike` structurally.
export interface RowLike {
  id: number;
  path: string;
  name: string;
  category: string;
  ext: string;
  size_bytes: number;
  mtime: string;
}

/** `type:` is an ALIAS for `cat:` and parses to "cat" — never to "ext". */
export type FilterKind = "ext" | "cat";

/** Mirrors main.ts's sort keys exactly; `sortComparator` mirrors `sortFiles` (main.ts:402-424). */
export type SortKey = "name" | "newest" | "oldest" | "largest" | "smallest" | "type";

export interface ParsedQuery {
  /** Folded free tokens, input order preserved, de-duped, empties dropped. */
  free: string[];
  /** OR within a kind, AND across kinds. A `Set` per kind — NEVER one value per kind (B2). */
  filters: Map<FilterKind, Set<string>>;
  /** Operator names typed with no value, e.g. `ext:` → ["ext"]. Drives the §4 inline hint. */
  dangling: string[];
  raw: string;
}

/** Index-aligned parallel arrays over the whole repo, built once at boot / after a live refresh. */
export interface Corpus {
  rows: RowLike[];
  nameLc: string[];
  pathLc: string[];
  /** RAW (unfolded) path → index into `rows`. The join key for the backend's tier-3 reply. */
  byPath: Map<string, number>;
}

/** 0 folder · 1 filename · 2 path · 3 meta/other. Lower is better. */
export type Tier = 0 | 1 | 2 | 3;

export interface Hit {
  /** null only for tier-0 folder hits. */
  row: RowLike | null;
  /** set only for tier-0. */
  dirPath: string | null;
  tier: Tier;
  /** How many free tokens this row matched. Full matches carry `free.length`. */
  matched: number;
  /** A token started at a word boundary in the name. */
  boundary: boolean;
}

export const TIER3_DEBOUNCE_MS = 150;
export const BAND_SIZE = 20;
export const FOLDER_HITS_MAX = 3;

// ── Fold ────────────────────────────────────────────────────────────────────────────────────

/**
 * THE ONE FOLD RULE: NFD-normalize, then lowercase ASCII A–Z **only**.
 *
 * NFD because the index stores decomposed text (macOS filenames): `café` is stored
 * `63 61 66 65 CC 81`, so an NFC needle can never match it.
 * ASCII-only because the SQL side folds with `to_ascii_lowercase` against SQLite's ASCII-only
 * `LOWER()` (db.rs:782-788). `String.prototype.toLowerCase()` is FORBIDDEN here: it maps `É` to
 * the single NFC codepoint `é`, which the NFD-stored column never contains.
 *
 * TWO FAST PATHS, both provably identical to the rule above — they matter because this runs
 * 2 x 61,524 times at boot and per row inside `matchesFilters`:
 *   • no A–Z and nothing above U+007F  → the string IS its own fold; return it untouched.
 *   • something above U+007F is absent → `toLowerCase()` is EXACTLY the ASCII fold here, and 21x
 *     cheaper than normalize+regex (measured 1.0 ms vs 21.4 ms over 61,524 paths). No ASCII
 *     character has a canonical decomposition, so NFD is the identity; and over U+0000–U+007F
 *     `toLowerCase()` maps A–Z to a–z and fixes everything else. The behaviour that makes
 *     `toLowerCase()` unusable in general (`É`→`é`, `İ`→two codepoints) needs a code point above
 *     U+007F, which by construction never reaches this branch.
 * Anything with a non-ASCII code unit takes the full rule.
 */
export function foldToken(s: string): string {
  let hasUpper = false;
  let hasNonAscii = false;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c > 127) {
      hasNonAscii = true;
      break;
    }
    if (c >= 65 && c <= 90) hasUpper = true;
  }
  if (!hasNonAscii) return hasUpper ? s.toLowerCase() : s;
  return s.normalize("NFD").replace(/[A-Z]/g, (c) => c.toLowerCase());
}

// ASCII whitespace ONLY — space, tab, LF, CR, FF, VT. Deliberately NOT /\s/, which also matches
// U+FEFF and U+00A0 and would split a token the SQL side keeps whole.
function isAsciiWs(ch: string): boolean {
  if (ch.length !== 1) return false;
  const c = ch.charCodeAt(0);
  return c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d || c === 0x0c || c === 0x0b;
}

/**
 * Split a raw query into terms. Mirrors db.rs `split_terms`: a `"` toggles "inside a phrase" and is
 * dropped, ASCII whitespace outside a phrase ends a term, an unclosed phrase closes at end of input.
 * Inner spaces of a phrase are PRESERVED, so `"cell type"` is ONE term.
 */
export function tokenize(input: string): string[] {
  const terms: string[] = [];
  let cur = "";
  let inQuotes = false;
  for (const ch of input) {
    if (ch === '"') {
      inQuotes = !inQuotes;
      continue;
    }
    if (!inQuotes && isAsciiWs(ch)) {
      if (cur) {
        terms.push(cur);
        cur = "";
      }
      continue;
    }
    cur += ch;
  }
  if (cur) terms.push(cur);
  return terms;
}

// The operator vocabulary. `ext`/`cat`/`type` become filters; `path`/`dir`/`obs`/`obsm` are
// recognized (so their value is not lost) but DEGRADE TO FREE TEXT — the client-side tree and band
// have no separate column to scope them to.
const OPERATORS = new Set(["ext", "cat", "type", "path", "dir", "obs", "obsm"]);

export function parseQuery(input: string): ParsedQuery {
  const free: string[] = [];
  const seen = new Set<string>();
  const filters = new Map<FilterKind, Set<string>>();
  const dangling: string[] = [];

  const addFree = (v: string): void => {
    if (v.length === 0 || seen.has(v)) return;
    seen.add(v);
    free.push(v);
  };
  const addFilter = (kind: FilterKind, v: string): void => {
    let set = filters.get(kind);
    if (!set) {
      set = new Set<string>();
      filters.set(kind, set);
    }
    set.add(v);
  };

  for (const term of tokenize(input)) {
    const i = term.indexOf(":");
    // `i > 0`: a term that STARTS with ':' has no operator name, so it is free text.
    const kind = i > 0 ? term.slice(0, i).toLowerCase() : "";
    if (i > 0 && OPERATORS.has(kind)) {
      const value = term.slice(i + 1); // split on the FIRST ':' so a value may contain ':'
      if (value === "") {
        dangling.push(kind); // a bare `ext:` filters nothing — §4 shows a hint instead
        continue;
      }
      if (kind === "ext") addFilter("ext", foldToken(value));
      else if (kind === "cat" || kind === "type") addFilter("cat", foldToken(value));
      else addFree(foldToken(value));
    } else {
      addFree(foldToken(term));
    }
  }

  return { free, filters, dangling, raw: input };
}

// ── Corpus ──────────────────────────────────────────────────────────────────────────────────

export function buildCorpus(rows: RowLike[]): Corpus {
  const n = rows.length;
  const nameLc: string[] = new Array<string>(n);
  const pathLc: string[] = new Array<string>(n);
  const byPath = new Map<string, number>();
  for (let i = 0; i < n; i++) {
    const r = rows[i];
    nameLc[i] = foldToken(r.name);
    pathLc[i] = foldToken(r.path);
    byPath.set(r.path, i);
  }
  return { rows, nameLc, pathLc, byPath };
}

// ── Filter predicate ────────────────────────────────────────────────────────────────────────

/**
 * The three operator rules are NOT the same rule:
 *   ext: EXACT equality on the folded `ext` column (db.rs:864) — and a `""` set member matches
 *        NOTHING, so an extension-less row can never be pulled in by an `ext:` chip.
 *   cat: SUBSTRING of the folded `category` column (db.rs:859) — `cat:data` matches `data_matrix`
 *        and `data_table`.
 *   Composition: OR within a kind, AND across kinds.
 */
export function matchesFilters(row: RowLike, filters: Map<FilterKind, Set<string>>): boolean {
  if (filters.size === 0) return true;

  const exts = filters.get("ext");
  if (exts !== undefined) {
    const e = foldToken(row.ext);
    let ok = false;
    for (const v of exts) {
      if (v.length > 0 && v === e) {
        ok = true;
        break;
      }
    }
    if (!ok) return false;
  }

  const cats = filters.get("cat");
  if (cats !== undefined) {
    const c = foldToken(row.category);
    let ok = false;
    for (const v of cats) {
      if (c.includes(v)) {
        ok = true;
        break;
      }
    }
    if (!ok) return false;
  }

  return true;
}

// ── Tiering ─────────────────────────────────────────────────────────────────────────────────

/**
 * AND over tokens; the WORST token sets the tier. A row is tier 1 only when EVERY token is in the
 * basename. One token in the basename and one only in the path is tier 2, not tier 1.
 * `null` = not a tier-1/2 candidate at all (some token is nowhere in the path).
 * An empty token list is neutral (tier 2): band ordering then falls entirely to the sort key.
 */
export function tierOf(nameLc: string, pathLc: string, free: string[]): Tier | null {
  if (free.length === 0) return 2;
  let worst: Tier = 1;
  for (let i = 0; i < free.length; i++) {
    const t = free[i];
    if (nameLc.includes(t)) continue;
    if (pathLc.includes(t)) {
      if (worst < 2) worst = 2;
      continue;
    }
    return null;
  }
  return worst;
}

/**
 * What the folder tree is rebuilt from: filters, AND every free token as a substring of the PATH.
 * Adding a word must NARROW the tree, so free tokens compose with AND.
 *
 * Tier-3 (meta) rows deliberately never enter the tree — the tree is synchronous and must never
 * wait on IPC (finding F3).
 */
const NO_TIER3_PATHS: ReadonlySet<string> = new Set<string>();

export function treeRows(
  c: Corpus,
  q: ParsedQuery,
  tier3: ReadonlySet<string> = NO_TIER3_PATHS,
): RowLike[] {
  const out: RowLike[] = [];
  const free = q.free;
  const rows = c.rows;
  const hasTier3 = tier3.size > 0;
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (!matchesFilters(r, q.filters)) continue;
    const p = c.pathLc[i];
    let ok = true;
    for (let j = 0; j < free.length; j++) {
      if (!p.includes(free[j])) {
        ok = false;
        break;
      }
    }
    // A row the BACKEND matched — the token lives in `meta` or in a figure's rendered TEXT, neither
    // of which the corpus carries — is a hit even though its PATH does not contain the token.
    // Without this the band asserted hits the tree denied, and the "N matching" count contradicted
    // the rows on screen. Tested in the corpus loop (not appended) so tier-3 rows keep path order
    // and a row matching BOTH ways still appears exactly once.
    if (!ok && hasTier3) ok = tier3.has(r.path);
    if (ok) out.push(r);
  }
  return out;
}

// ── Tier 0: folders ─────────────────────────────────────────────────────────────────────────

interface DirHit {
  path: string;
  segLen: number;
}

function dirHitCompare(a: DirHit, b: DirHit): number {
  if (a.segLen !== b.segLen) return a.segLen - b.segLen;
  return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
}

/**
 * Tier 0. A folder matches iff its LAST SEGMENT, folded, contains EVERY free token — so a hit is
 * about the folder's own name, not about where it happens to sit. Ordered shortest-segment first
 * (the tightest name wins), then path ascending; capped at `k`.
 *
 * Paths are split on "/" and on nothing else: a path may legitimately contain a newline.
 */
export function folderHits(dirPaths: string[], free: string[], k: number): string[] {
  if (free.length === 0 || k <= 0) return [];
  const picked: DirHit[] = [];
  for (let i = 0; i < dirPaths.length; i++) {
    const dp = dirPaths[i];
    const cut = dp.lastIndexOf("/");
    const seg = cut === -1 ? dp : dp.slice(cut + 1);
    const segLc = foldToken(seg);
    let all = true;
    for (let j = 0; j < free.length; j++) {
      if (!segLc.includes(free[j])) {
        all = false;
        break;
      }
    }
    if (!all) continue;
    insertBounded(picked, { path: dp, segLen: seg.length }, dirHitCompare, k);
  }
  const out: string[] = new Array<string>(picked.length);
  for (let i = 0; i < picked.length; i++) out[i] = picked[i].path;
  return out;
}

// ── Ranking ─────────────────────────────────────────────────────────────────────────────────

// Internal candidate: a Hit plus the precomputed mtime epoch, so `Date.parse` runs ONCE per
// candidate instead of once per comparison.
interface Cand {
  row: RowLike;
  tier: Tier;
  matched: number;
  boundary: boolean;
  ms: number;
}

// `localeCompare(x, "en-US")` is defined as a Collator("en-US") comparison; hoisting the collator
// keeps the semantics identical to sortFiles (main.ts:402-424) at a fraction of the cost.
const COLLATOR = new Intl.Collator("en-US");

/** The active sort key — identical semantics to main.ts `sortFiles`. */
function sortComparator(a: Cand, b: Cand, sort: SortKey): number {
  switch (sort) {
    case "name":
      return COLLATOR.compare(a.row.name, b.row.name);
    case "newest":
      return b.ms - a.ms;
    case "oldest":
      return a.ms - b.ms;
    case "largest":
      return b.row.size_bytes - a.row.size_bytes;
    case "smallest":
      return a.row.size_bytes - b.row.size_bytes;
    case "type":
      return (
        COLLATOR.compare(a.row.category, b.row.category) ||
        COLLATOR.compare(a.row.name, b.row.name)
      );
  }
}

// Band comparator. First difference wins:
//   1. matched DESC  — full matches always above backfilled partial matches
//   2. tier    ASC   — 0 folder < 1 name < 2 path < 3 meta
//   3. boundary DESC — a token that begins at a word boundary in the name
//   4. the active sort key
//   5. path ASC      — a TOTAL order, so the band is deterministic
function candCompare(a: Cand, b: Cand, sort: SortKey): number {
  if (a.matched !== b.matched) return b.matched - a.matched;
  if (a.tier !== b.tier) return a.tier - b.tier;
  if (a.boundary !== b.boundary) return a.boundary ? -1 : 1;
  const s = sortComparator(a, b, sort);
  if (s !== 0) return s;
  return a.row.path < b.row.path ? -1 : a.row.path > b.row.path ? 1 : 0;
}

// Bounded top-k insertion with worst-element early reject. NEVER a sort over the candidate set:
// with 60k+ rows a full sort is ~40x the work and blows the interaction budget.
function insertBounded<T>(arr: T[], item: T, cmp: (a: T, b: T) => number, k: number): void {
  if (k <= 0) return;
  if (arr.length >= k && cmp(item, arr[arr.length - 1]) >= 0) return;
  let lo = 0;
  let hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (cmp(item, arr[mid]) < 0) hi = mid;
    else lo = mid + 1;
  }
  arr.splice(lo, 0, item);
  if (arr.length > k) arr.length = k;
}

// A token "begins at a word boundary" when it starts the name, or follows one of `/ _ - .` or a
// digit, or starts an upperCase run inside a camelCase name.
//
// `nameLc` is `foldToken(nameRaw)`; the ASCII fold is 1:1 so the two are index-aligned whenever
// NFD did not expand the string (`nameRaw.length === nameLc.length`). If NFD did expand it, the
// camelCase probe is skipped rather than reading a shifted index.
function hasBoundary(nameRaw: string, nameLc: string, tokens: string[]): boolean {
  const aligned = nameRaw.length === nameLc.length;
  for (let ti = 0; ti < tokens.length; ti++) {
    const t = tokens[ti];
    if (t.length === 0) continue;
    let i = nameLc.indexOf(t);
    while (i !== -1) {
      if (i === 0) return true;
      const p = nameLc.charCodeAt(i - 1);
      // '/'=47 '-'=45 '.'=46 '_'=95, digits 48-57
      if (p === 47 || p === 45 || p === 46 || p === 95 || (p >= 48 && p <= 57)) return true;
      if (aligned) {
        const cur = nameRaw.charCodeAt(i);
        const prev = nameRaw.charCodeAt(i - 1);
        if (cur >= 65 && cur <= 90 && !(prev >= 65 && prev <= 90)) return true;
      }
      i = nameLc.indexOf(t, i + 1);
    }
  }
  return false;
}

function toHit(c: Cand): Hit {
  return { row: c.row, dirPath: null, tier: c.tier, matched: c.matched, boundary: c.boundary };
}

/**
 * The top-hits band: at most `k` ranked hits, ALWAYS rendered while the query is non-empty.
 *
 *   free empty  → the first `k` filter-passing rows in the ACTIVE SORT order ("Newest 20 png").
 *                 This is the single behaviour that fixes the reported complaint: ticking a type
 *                 no longer buries the newest match hundreds of groups down.
 *   free tokens → tier-0 folder hits first (capped at FOLDER_HITS_MAX), then full matches by
 *                 tier 1/2/3, then — only if the band is still short — partial "backfill" matches
 *                 ranked strictly below every full match.
 *
 * `tier3Paths` holds RAW paths from the backend's `search_ids` reply (the join key is path, not
 * rowid: rowids churn across a reconcile — finding C6). It only ever ADDS to the band; the tree is
 * never gated on it.
 */
export function topHits(
  c: Corpus,
  q: ParsedQuery,
  tier3Paths: Set<string>,
  dirPaths: string[],
  sort: SortKey,
  k: number,
): Hit[] {
  if (k <= 0) return [];
  const free = q.free;
  const rows = c.rows;

  // Tier 0 is always first and keeps its own (segment-length) order.
  const dirs = folderHits(dirPaths, free, Math.min(FOLDER_HITS_MAX, k));
  const out: Hit[] = [];
  for (let i = 0; i < dirs.length; i++) {
    out.push({ row: null, dirPath: dirs[i], tier: 0, matched: free.length, boundary: true });
  }

  const room = k - out.length;
  if (room <= 0) return out;

  const needsMs = sort === "newest" || sort === "oldest";
  const useTier3 = free.length > 0 && tier3Paths.size > 0;
  const cmp = (a: Cand, b: Cand): number => candCompare(a, b, sort);
  const picked: Cand[] = [];

  // Full-match pass. With `free` empty every filter-passing row is a neutral tier-2 candidate, so
  // this single loop is exactly the spec's "candidates = treeRows(c,q), ranked by the sort key".
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (!matchesFilters(r, q.filters)) continue;
    const nlc = c.nameLc[i];
    let tier = tierOf(nlc, c.pathLc[i], free);
    if (tier === null) {
      if (!useTier3 || !tier3Paths.has(r.path)) continue;
      tier = 3;
    }
    insertBounded(
      picked,
      {
        row: r,
        tier,
        matched: free.length,
        boundary: hasBoundary(r.name, nlc, free),
        ms: needsMs ? Date.parse(r.mtime) || 0 : 0,
      },
      cmp,
      room,
    );
  }

  // Backfill pass — ONLY if the band is still short. Partial matches keep the band useful when an
  // added word over-narrows, and `matched DESC` pins them strictly below every full match.
  if (free.length > 0 && picked.length < room) {
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (!matchesFilters(r, q.filters)) continue;
      const nlc = c.nameLc[i];
      const plc = c.pathLc[i];
      if (tierOf(nlc, plc, free) !== null) continue; // already in as a full match
      if (useTier3 && tier3Paths.has(r.path)) continue; // already in as a tier-3 full match
      let matched = 0;
      let allInName = true;
      const hitTokens: string[] = [];
      for (let j = 0; j < free.length; j++) {
        const t = free[j];
        if (!plc.includes(t)) continue;
        matched++;
        hitTokens.push(t);
        if (!nlc.includes(t)) allInName = false;
      }
      if (matched === 0) continue;
      insertBounded(
        picked,
        {
          row: r,
          tier: allInName ? 1 : 2,
          matched,
          boundary: hasBoundary(r.name, nlc, hitTokens),
          ms: needsMs ? Date.parse(r.mtime) || 0 : 0,
        },
        cmp,
        room,
      );
    }
  }

  for (let i = 0; i < picked.length; i++) out.push(toHit(picked[i]));
  return out;
}
