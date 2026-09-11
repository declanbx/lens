// Lens — unit tests for src/query.ts (LOCKED SPEC §6, the two `src/query.test.ts` sections).
//
// Run: cd tools/repo_index/lens && node --test src/query.test.ts
//
// Every assertion runs against the HERMETIC fixture declared below — never against the live
// index, whose counts churn with every reindex. Where the spec quotes a live-index number (the
// three `popv umap` counts) the fixture reproduces the SHAPE of that number, and all three are
// asserted separately so an OR implementation of the tree fails loudly.
//
// An NFC and an NFD string literal are byte-different but VISUALLY IDENTICAL in a source file, and
// several assertions turn on that difference — so the two forms are pinned by a runtime self-check
// (search CAFE_NFD below) that fires before any test runs.
//
// This project has no @types/node (the brief forbids new deps), so the three built-in module
// imports carry a local ts-ignore; `npm run build` type-checks src/ and must stay green.

// @ts-ignore -- node:test has no ambient typing in this project
import { test } from "node:test";
// @ts-ignore -- node:assert has no ambient typing in this project
import assert from "node:assert/strict";
// @ts-ignore -- node:fs has no ambient typing in this project
import { readFileSync } from "node:fs";

import {
  BAND_SIZE,
  FOLDER_HITS_MAX,
  TIER3_DEBOUNCE_MS,
  buildCorpus,
  foldToken,
  folderHits,
  matchesFilters,
  parseQuery,
  tierOf,
  tokenize,
  topHits,
  treeRows,
} from "./query.ts";
import type { Corpus, FilterKind, Hit, RowLike, SortKey } from "./query.ts";

// ═══════════════════════════════════════════════════════════════════════════════════════════
// FIXTURE
// ═══════════════════════════════════════════════════════════════════════════════════════════

const CAFE_NFD: string = "café"; // c a f e + COMBINING ACUTE — how the index stores it
const CAFE_NFC: string = "café"; // the single-codepoint form String.toLowerCase() produces
const CAFE_PATH = `notes/${CAFE_NFD}_notes.md`;
const NEWLINE_PATH = "weird/file\nwith_newline.txt";

// Load-bearing self-check. The two CAFE_* literals above are VISUALLY IDENTICAL in a source file;
// only their byte length distinguishes decomposed (5) from precomposed (4). If any editor or
// formatter ever normalizes this file, the café assertions below would silently pass for the wrong
// reason — so fail loudly here instead.
if (CAFE_NFD.length !== 5 || CAFE_NFC.length !== 4 || CAFE_NFD === CAFE_NFC) {
  throw new Error("query.test.ts: the café literals lost their NFD/NFC distinction");
}

// [path, category, ext]
const SPEC: Array<[string, string, string]> = [
  /*  0 */ ["outputs/popv_v5/figures/popv_umap_L1.png", "figure", "png"],
  /*  1 */ ["outputs/popv_v5/umap_grid.png", "figure", "png"],
  /*  2 */ ["outputs/popv_v5/figures/qc_report.png", "figure", "png"],
  /*  3 */ ["assets/umap/legend.svg", "figure", "svg"],
  /*  4 */ ["outputs/popv_v5/umap/popv_summary.csv", "data_table", "csv"],
  /*  5 */ ["notes/popv_umap.md", "doc", "md"],
  /*  6 */ ["misc/umap_only.txt", "other", "txt"],
  /*  7 */ ["outputs/popv_v5/production/umap_prod.png", "figure", "png"],
  /*  8 */ ["outputs/production_showcase/summary.csv", "data_table", "csv"],
  /*  9 */ ["outputs/lipids/cholesterol_panel.png", "figure", "png"],
  /* 10 */ ["outputs/lipids/module_scores.csv", "data_matrix", "csv"], // tier-3 only: token is in meta
  /* 11 */ [CAFE_PATH, "doc", "md"], // stored NFD, like every other macOS path
  /* 12 */ [NEWLINE_PATH, "other", "txt"], // a literal LF inside a path
  /* 13 */ ["misc/Makefile", "other", ""], // ext = "" (2,763 such rows live)
  /* 14 */ ["data/matrix_one.h5ad", "data_matrix", "h5ad"],
  /* 15 */ ["data/table_one.csv", "data_table", "csv"],
  /* 16 */ ["figures/panel_A.svg", "figure", "svg"],
  /* 17 */ ["figures/panel_B.svg", "figure", "svg"],
  /* 18 */ ["figures/panel_C.png", "figure", "png"],
  /* 19 */ ["reports/Report_Final.md", "doc", "md"],
  /* 20 */ ["reports/appendix.md", "doc", "md"],
  /* 21 */ ["scripts/run_all.py", "code", "py"],
  /* 22 */ ["scripts/helper_utils.py", "code", "py"],
  /* 23 */ ["scripts/CamelCaseThing.py", "code", "py"],
  /* 24 */ ["scripts/abc_lowercase_util.py", "code", "py"],
  /* 25 */ ["logs/run_2026.log", "other", "log"],
  /* 26 */ ["logs/run_2025.log", "other", "log"],
  /* 27 */ ["archive/old_notes.txt", "other", "txt"],
  /* 28 */ ["misc/screenshot.png", "other", "png"], // a png that is NOT category=figure
  /* 29 */ ["archive/legacy_readme.md", "doc", "md"],
];

const BASE_MS = Date.UTC(2026, 0, 1, 0, 0, 0);

function baseName(p: string): string {
  const cut = p.lastIndexOf("/");
  return cut === -1 ? p : p.slice(cut + 1);
}

// mtimes and sizes are scattered (not path-ordered) and pairwise distinct, so a "Newest 20"
// assertion is a real ordering test and no comparator ever hits a tie.
const FIXTURE: RowLike[] = SPEC.map(([path, category, ext], i) => ({
  id: i + 1,
  path,
  name: baseName(path),
  category,
  ext,
  size_bytes: (((i * 53) % 97) + 1) * 1000 + i,
  mtime: new Date(BASE_MS + ((i * 41) % 101) * 86_400_000).toISOString(),
}));

const CORPUS: Corpus = buildCorpus(FIXTURE);
const NO_TIER3: Set<string> = new Set<string>();
const NO_DIRS: string[] = [];

function pathsOf(hits: Hit[]): string[] {
  return hits.map((h) => (h.row ? h.row.path : (h.dirPath as string)));
}
function byPath(p: string): RowLike {
  const r = FIXTURE.find((x) => x.path === p);
  assert.ok(r, `fixture has no row ${JSON.stringify(p)}`);
  return r as RowLike;
}
function tree(q: string): string[] {
  return treeRows(CORPUS, parseQuery(q)).map((r) => r.path);
}

// ── tree ∪ tier 3 ───────────────────────────────────────────────────────────────────────────
// The tree is built from the corpus (name + path); a token that lives only in `meta` or in a
// figure's rendered TEXT can only come from the backend. Before this, such a row appeared in the
// band and was absent from the tree AND from the "N matching" count — the band asserted hits the
// tree denied. `ZZ_BACKEND_ONLY` appears in no fixture path, so it can match only via tier 3.
const ZZ_BACKEND_ONLY = "zzbackendonly";
const T3_ROW = "outputs/lipids/module_scores.csv"; // category data_matrix

test("treeRows includes tier-3 backend-only hits so tree and band agree", () => {
  const t3 = new Set([T3_ROW]);
  assert.deepEqual(
    treeRows(CORPUS, parseQuery(ZZ_BACKEND_ONLY), t3).map((r) => r.path),
    [T3_ROW],
  );
});

test("without tier-3 paths a backend-only token still matches nothing", () => {
  assert.equal(treeRows(CORPUS, parseQuery(ZZ_BACKEND_ONLY), NO_TIER3).length, 0);
});

test("a tier-3 hit still obeys the active filters", () => {
  const t3 = new Set([T3_ROW]);
  assert.deepEqual(treeRows(CORPUS, parseQuery(`cat:figure ${ZZ_BACKEND_ONLY}`), t3), []);
});

test("a row matching BOTH by path and via tier 3 appears exactly once", () => {
  const both = "outputs/popv_v5/umap_grid.png";
  const rows = treeRows(CORPUS, parseQuery("umap"), new Set([both])).map((r) => r.path);
  assert.equal(rows.filter((x) => x === both).length, 1);
});

test("tier-3 rows keep the corpus row ORDER (tree grouping stays stable)", () => {
  // Index order is path order from buildCorpus; an appended tier-3 row would jump the grouping.
  const t3 = new Set([T3_ROW, "outputs/popv_v5/umap_grid.png"]);
  const got = treeRows(CORPUS, parseQuery(""), t3).map((r) => r.path);
  const idx = (p: string) => got.indexOf(p);
  assert.ok(idx("outputs/popv_v5/umap_grid.png") < idx(T3_ROW) === (
    FIXTURE.findIndex((r) => r.path === "outputs/popv_v5/umap_grid.png") <
    FIXTURE.findIndex((r) => r.path === T3_ROW)));
});
function band(
  q: string,
  sort: SortKey = "newest",
  k: number = BAND_SIZE,
  tier3: Set<string> = NO_TIER3,
  dirs: string[] = NO_DIRS,
): Hit[] {
  return topHits(CORPUS, parseQuery(q), tier3, dirs, sort, k);
}

// Independent oracle: a verbatim copy of main.ts `sortFiles` (main.ts:402-424). The band with no
// free tokens must reproduce this order exactly.
function sortFilesOracle(files: RowLike[], k: SortKey): RowLike[] {
  const arr = files.slice();
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
          a.category.localeCompare(b.category, "en-US") || a.name.localeCompare(b.name, "en-US")
        );
    }
  });
  return arr;
}

// ═══════════════════════════════════════════════════════════════════════════════════════════
// 1. TOKENIZER & FOLD — the golden table (spec §6, first table)
// ═══════════════════════════════════════════════════════════════════════════════════════════

test("constants match the locked spec", () => {
  assert.equal(TIER3_DEBOUNCE_MS, 150);
  assert.equal(BAND_SIZE, 20);
  assert.equal(FOLDER_HITS_MAX, 3);
});

test("`popv umap` → two free tokens", () => {
  const q = parseQuery("popv umap");
  assert.deepEqual(q.free, ["popv", "umap"]);
  assert.equal(q.filters.size, 0);
  assert.deepEqual(q.dangling, []);
  assert.equal(q.raw, "popv umap");
});

test('`"cell type"` is ONE token, not two', () => {
  // Today's main.ts splits the phrase while the Rust side keeps it whole. query.ts is now the
  // only tokenizer, so the phrase stays intact on both sides of the IPC.
  assert.deepEqual(tokenize('"cell type"'), ["cell type"]);
  assert.deepEqual(parseQuery('"cell type"').free, ["cell type"]);
});

test('`"unclosed phrase` closes at end of input', () => {
  assert.deepEqual(tokenize('"unclosed phrase'), ["unclosed phrase"]);
  assert.deepEqual(parseQuery('"unclosed phrase').free, ["unclosed phrase"]);
});

test("CAFÉ folds to NFD and matches an NFD-stored path; a toLowerCase fold cannot", () => {
  const folded = foldToken("CAFÉ");
  assert.equal(folded, CAFE_NFD);
  assert.equal(folded.length, 5);

  assert.ok(CAFE_PATH.includes(folded));

  // The rule this test exists to pin: String.prototype.toLowerCase() yields the PRECOMPOSED form,
  // which an NFD-stored path never contains — the query would silently return zero rows.
  assert.equal("CAFÉ".toLowerCase(), CAFE_NFC);
  assert.notEqual(CAFE_NFC, CAFE_NFD);
  assert.ok(!CAFE_PATH.includes(CAFE_NFC));

  assert.deepEqual(tree("CAFÉ"), [CAFE_PATH]);
  assert.deepEqual(tree(CAFE_NFC), [CAFE_PATH]); // an NFC needle typed by the user still matches
});

test("İstanbul: the fold is NFD-then-ASCII and is deterministic", () => {
  // SPEC DISCREPANCY, resolved in favour of the normative algorithm in §2.1.
  // §6's table row says "folded length 8, not 9". That is unreachable from §2.1's own fold rule:
  // NFD decomposes U+0130 to <0049 0307>, so ANY NFD-first fold yields 9 — and dropping the NFD
  // step would break the CAFÉ row above and desync the needle from the NFD-stored index. The row
  // also cannot discriminate the two implementations it was written to separate: for this one
  // input, NFD+ASCII and toLowerCase() produce the IDENTICAL 9-codepoint string (asserted below).
  // CAFÉ is the real discriminator and it is tested above.
  const folded = foldToken("İstanbul");
  assert.equal(folded, "i̇stanbul");
  assert.equal(folded.length, 9);
  assert.equal("İstanbul".length, 8);
  assert.equal("İstanbul".toLowerCase(), folded); // the two folds are indistinguishable here
});

test("foldToken's ASCII fast paths are identical to the normalize+regex rule", () => {
  // The reference implementation: §2.1's rule, written out with no shortcuts.
  const reference = (s: string): string => s.normalize("NFD").replace(/[A-Z]/g, (c) => c.toLowerCase());

  // exhaustive over every ASCII code point
  for (let c = 0; c < 128; c++) {
    const ch = String.fromCharCode(c);
    assert.equal(foldToken(ch), reference(ch), `U+${c.toString(16).padStart(4, "0")}`);
  }
  // and over ASCII strings, including the shapes that hit each branch
  const cases = [
    "",
    "png",
    "PNG",
    "Report_Final.md",
    "CamelCaseThing.py",
    "outputs/ExampleProject/Figures/Asset_9_UMAP.png",
    "no_upper_here/at_all.csv",
    "file\nwith_newline.txt",
    "a\tb",
    "0123456789-_./",
  ];
  for (const s of cases) assert.equal(foldToken(s), reference(s), JSON.stringify(s));
  // non-ASCII always takes the full rule
  for (const s of ["CAFÉ", "café", "café", "İstanbul", "Δelta", "a﻿B", "🧬X"]) {
    assert.equal(foldToken(s), reference(s), JSON.stringify(s));
  }
});

test("U+FEFF is not whitespace — `a\\uFEFFb` is ONE token", () => {
  assert.deepEqual(tokenize("a﻿b"), ["a﻿b"]);
  assert.deepEqual(parseQuery("a﻿b").free, ["a﻿b"]);
});

test("ASCII whitespace splits — `a\\tb\\nc` is three tokens", () => {
  assert.deepEqual(tokenize("a\tb\nc"), ["a", "b", "c"]);
  assert.deepEqual(parseQuery("a\tb\nc").free, ["a", "b", "c"]);
  assert.deepEqual(tokenize("a\rb\fc\vd"), ["a", "b", "c", "d"]);
});

test("free tokens are de-duped and empties dropped, input order preserved", () => {
  assert.deepEqual(parseQuery("umap  popv umap").free, ["umap", "popv"]);
  assert.deepEqual(parseQuery('""   ').free, []);
});

test("B10 — `ext:PNG` is case-insensitive", () => {
  const q = parseQuery("ext:PNG");
  assert.deepEqual([...(q.filters.get("ext") as Set<string>)], ["png"]);
  assert.deepEqual(tree("ext:PNG"), tree("ext:png"));
  assert.ok(tree("ext:PNG").length > 0);
});

test("B2/C3 — two ext chips OR into one Set, never AND", () => {
  const q = parseQuery("ext:png ext:svg");
  const set = q.filters.get("ext") as Set<string>;
  assert.ok(set instanceof Set, "filters must hold a Set per kind, never a single value");
  assert.equal(set.size, 2);
  assert.deepEqual([...set].sort(), ["png", "svg"]);

  const nPng = FIXTURE.filter((r) => r.ext === "png").length;
  const nSvg = FIXTURE.filter((r) => r.ext === "svg").length;
  const union = tree("ext:png ext:svg").length;
  assert.ok(nPng > 0 && nSvg > 0);
  assert.equal(union, nPng + nSvg);
  assert.notEqual(union, 0);
  assert.notEqual(union, nPng);
  assert.notEqual(union, nSvg);
});

test("AND across kinds — `ext:png cat:figure`", () => {
  const both = tree("ext:png cat:figure");
  const expected = FIXTURE.filter((r) => r.ext === "png" && r.category.includes("figure")).map(
    (r) => r.path,
  );
  assert.deepEqual(both, expected);
  // strictly narrower than either kind alone (the fixture has a non-figure png and figure svgs)
  assert.ok(both.length < tree("ext:png").length);
  assert.ok(both.length < tree("cat:figure").length);
});

test("`type:` parses to cat, NEVER to ext", () => {
  const q = parseQuery("type:figure");
  assert.equal(q.filters.get("ext"), undefined);
  assert.deepEqual([...(q.filters.get("cat") as Set<string>)], ["figure"]);
  assert.deepEqual(tree("type:figure"), tree("cat:figure"));
});

test("`type:png` is a CATEGORY substring → 0 rows (a rebind to ext would return every png)", () => {
  assert.equal(tree("type:png").length, 0);
  assert.ok(tree("ext:png").length > 0);
});

test("`cat:` is a SUBSTRING match — `cat:data` catches data_matrix and data_table", () => {
  const cats = new Set(treeRows(CORPUS, parseQuery("cat:data")).map((r) => r.category));
  assert.deepEqual([...cats].sort(), ["data_matrix", "data_table"]);
});

test("`ext:` is EQUALITY — `ext:data` matches neither data_matrix nor data_table", () => {
  assert.equal(tree("ext:data").length, 0);
});

test("bare `ext:` is dangling — no filter, no free token", () => {
  const q = parseQuery("ext:");
  assert.deepEqual(q.dangling, ["ext"]);
  assert.equal(q.filters.size, 0);
  assert.deepEqual(q.free, []);
  // §4: a dangling operator must NOT filter the tree — the full tree stays on screen
  assert.equal(tree("ext:").length, FIXTURE.length);
});

test("`ext: png` is a dangling operator plus a free token", () => {
  const q = parseQuery("ext: png");
  assert.deepEqual(q.dangling, ["ext"]);
  assert.equal(q.filters.size, 0);
  assert.deepEqual(q.free, ["png"]);
});

test("`path:`/`dir:`/`obs:`/`obsm:` degrade to free text", () => {
  const q = parseQuery("path:foo");
  assert.deepEqual(q.free, ["foo"]);
  assert.equal(q.filters.size, 0);
  for (const op of ["dir", "obs", "obsm"]) {
    assert.deepEqual(parseQuery(`${op}:foo`).free, ["foo"], `${op}: must degrade to free text`);
  }
});

test("an unrecognized prefix keeps the whole term as free text", () => {
  assert.deepEqual(parseQuery("http://example").free, ["http://example"]);
  assert.deepEqual(parseQuery(":leading").free, [":leading"]);
});

test("a row with ext='' is never matched by ext: with any value", () => {
  const makefile = byPath("misc/Makefile");
  assert.equal(makefile.ext, "");
  for (const v of ["makefile", "png", "", " "]) {
    const filters = new Map<FilterKind, Set<string>>([["ext", new Set([v])]]);
    assert.equal(matchesFilters(makefile, filters), false, `ext:${JSON.stringify(v)}`);
  }
  assert.equal(tree("ext:makefile").length, 0);
  assert.ok(!tree("ext:png").includes("misc/Makefile"));
});

// ═══════════════════════════════════════════════════════════════════════════════════════════
// 2. TIERING & BAND
// ═══════════════════════════════════════════════════════════════════════════════════════════

test("`popv umap` — AND-over-path, AND-over-basename and OR-over-basename are three DIFFERENT sets", () => {
  const free = parseQuery("popv umap").free;
  const andPath = FIXTURE.filter((r) => free.every((t) => foldToken(r.path).includes(t)));
  const andName = FIXTURE.filter((r) => free.every((t) => foldToken(r.name).includes(t)));
  const orName = FIXTURE.filter((r) => free.some((t) => foldToken(r.name).includes(t)));

  assert.equal(andPath.length, 5);
  assert.equal(andName.length, 2);
  assert.equal(orName.length, 6);

  // The TREE is AND-over-path. An OR implementation returns 6+, an AND-over-basename one 2 —
  // both fail loudly here.
  assert.deepEqual(
    tree("popv umap"),
    andPath.map((r) => r.path),
  );
  assert.equal(tree("popv umap").length, 5);
});

test("tierOf — worst token wins; not-a-candidate is null", () => {
  // both tokens in the basename → 1
  assert.equal(
    tierOf("popv_umap_l1.png", "outputs/popv_v5/figures/popv_umap_l1.png", ["popv", "umap"]),
    1,
  );
  // one token in the basename, one only in the path → 2 (NOT 1)
  assert.equal(tierOf("umap_grid.png", "outputs/popv_v5/umap_grid.png", ["popv", "umap"]), 2);
  // a token that is nowhere → null
  assert.equal(
    tierOf("qc_report.png", "outputs/popv_v5/figures/qc_report.png", ["popv", "umap"]),
    null,
  );
  // empty token list is the neutral tier 2
  assert.equal(tierOf("anything.png", "a/anything.png", []), 2);
});

test("treeRows excludes tier-3-only rows; topHits includes them when tier3Paths supplies them", () => {
  const META_ONLY = "outputs/lipids/module_scores.csv";
  // `cholesterol` lives only in that row's meta, which the Row DTO deliberately does not carry.
  assert.ok(!foldToken(byPath(META_ONLY).path).includes("cholesterol"));

  assert.deepEqual(tree("cholesterol"), ["outputs/lipids/cholesterol_panel.png"]);

  const without = band("cholesterol");
  assert.deepEqual(pathsOf(without), ["outputs/lipids/cholesterol_panel.png"]);

  const withT3 = band("cholesterol", "newest", BAND_SIZE, new Set([META_ONLY]));
  assert.deepEqual(pathsOf(withT3), ["outputs/lipids/cholesterol_panel.png", META_ONLY]);
  assert.deepEqual(
    withT3.map((h) => h.tier),
    [1, 3],
  );
  // the tree is NEVER changed by the async tier-3 reply
  assert.deepEqual(tree("cholesterol"), ["outputs/lipids/cholesterol_panel.png"]);
});

test("band with no free tokens is exactly sortFiles order, capped at BAND_SIZE", () => {
  for (const sort of ["newest", "oldest", "name", "type", "largest", "smallest"] as SortKey[]) {
    const hits = band("", sort);
    assert.equal(hits.length, BAND_SIZE);
    assert.deepEqual(
      pathsOf(hits),
      sortFilesOracle(FIXTURE, sort)
        .slice(0, BAND_SIZE)
        .map((r) => r.path),
      `sort=${sort}`,
    );
    // no relevance influence: every row is the neutral tier with 0 tokens matched
    for (const h of hits) {
      assert.equal(h.tier, 2);
      assert.equal(h.matched, 0);
      assert.equal(h.boundary, false);
      assert.equal(h.dirPath, null);
    }
  }
});

test("a pure filter still gets a band, in the active sort order (the reported complaint)", () => {
  const expected = sortFilesOracle(
    FIXTURE.filter((r) => r.ext === "png"),
    "newest",
  ).map((r) => r.path);
  const hits = band("ext:png", "newest");
  assert.deepEqual(pathsOf(hits), expected.slice(0, BAND_SIZE));
  // the newest png is row 1 of the band, not buried hundreds of groups down
  assert.equal(hits[0].row?.path, expected[0]);
  // and it changes with the sort key, so "Newest" really means newest
  const largest = band("ext:png", "largest");
  assert.equal(
    largest[0].row?.path,
    sortFilesOracle(
      FIXTURE.filter((r) => r.ext === "png"),
      "largest",
    )[0].path,
  );
});

test("band with free tokens: tier-0 folders first (capped at 3), then tier 1, then 2", () => {
  const dirs = [
    "a/popv_umap",
    "b/xx_popv_umap",
    "c/popv_umap_long_name",
    "d/popv_umap_extra",
    "e/other",
  ];
  const hits = band("popv umap", "newest", BAND_SIZE, NO_TIER3, dirs);

  // tier-0 first, capped at FOLDER_HITS_MAX, ordered by segment length then path
  assert.equal(FOLDER_HITS_MAX, 3);
  assert.deepEqual(pathsOf(hits).slice(0, 3), [
    "a/popv_umap",
    "b/xx_popv_umap",
    "d/popv_umap_extra",
  ]);
  for (let i = 0; i < 3; i++) {
    assert.equal(hits[i].tier, 0);
    assert.equal(hits[i].row, null);
    assert.equal(hits[i].matched, 2);
  }
  // then the 5 full (2-of-2) matches, tier 1 before tier 2
  assert.deepEqual(
    hits.slice(0, 8).map((h) => h.tier),
    [0, 0, 0, 1, 1, 2, 2, 2],
  );
  // every full match precedes every backfilled partial match
  assert.equal(
    hits.findIndex((h) => h.matched === 1),
    8,
  );
  const tier1 = pathsOf(hits).slice(3, 5).sort();
  assert.deepEqual(tier1, ["notes/popv_umap.md", "outputs/popv_v5/figures/popv_umap_L1.png"]);
});

test("folderHits — every free token must be in the LAST segment; order is segment-length then path", () => {
  const dirs = ["x/popv_umap", "popv/deep/other", "y/umap_popv_zz", "z/popv_umap"];
  assert.deepEqual(folderHits(dirs, ["popv", "umap"], 10), [
    "x/popv_umap",
    "z/popv_umap",
    "y/umap_popv_zz",
  ]);
  assert.deepEqual(folderHits(dirs, ["popv", "umap"], 2), ["x/popv_umap", "z/popv_umap"]);
  assert.deepEqual(folderHits(dirs, [], 10), []);
  assert.deepEqual(folderHits(dirs, ["popv", "umap"], 0), []);
  // "popv" appears only in an ANCESTOR segment of "popv/deep/other" → not a folder hit
  assert.deepEqual(folderHits(["popv/deep/other"], ["popv"], 10), []);
});

test("word-boundary bonus outranks the sort key (F8)", () => {
  const hits = band("case", "name");
  // three rows carry "case": two in the basename (tier 1) and "…/production_showcase/summary.csv"
  // in the path only (tier 2).
  assert.deepEqual(pathsOf(hits).slice(0, 2), [
    "scripts/CamelCaseThing.py", // camelCase boundary → boundary=true
    "scripts/abc_lowercase_util.py", // mid-word → boundary=false, though it sorts FIRST by name
  ]);
  assert.equal(hits[0].boundary, true);
  assert.equal(hits[1].boundary, false);
  assert.deepEqual(
    hits.map((h) => h.tier),
    [1, 1, 2],
  );
  // the name-sort oracle would have put abc_… first; only the boundary key moves it
  assert.ok("abc_lowercase_util.py".localeCompare("CamelCaseThing.py", "en-US") < 0);
});

test("backfill: partial matches rank below every full match, and only when the band is short", () => {
  const FULL = "outputs/popv_v5/production/umap_prod.png"; // has BOTH tokens
  const PARTIAL = "outputs/production_showcase/summary.csv"; // has "production" only

  const hits = band("production umap");
  assert.equal(hits[0].row?.path, FULL);
  assert.equal(hits[0].matched, 2);
  for (let i = 1; i < hits.length; i++) assert.equal(hits[i].matched, 1);

  const idxPartial = pathsOf(hits).indexOf(PARTIAL);
  assert.ok(idxPartial > 0, "a 1-of-2 match must sit below the 2-of-2 match");

  // the tree stays AND — adding a word must NARROW it, so the partial match is NOT in the tree
  assert.deepEqual(tree("production umap"), [FULL]);

  // once the band is full of full matches, no backfill happens at all
  assert.deepEqual(pathsOf(band("production umap", "newest", 1)), [FULL]);
});

test("backfill tier: 1 when every matched token is in the basename, else 2", () => {
  const t = new Map(band("production umap").map((h) => [h.row?.path, h.tier]));
  // "umap_only.txt" — the one matched token is in the basename → tier 1
  assert.equal(t.get("misc/umap_only.txt"), 1);
  // "summary.csv" under …/production_showcase — the matched token is path-only → tier 2
  assert.equal(t.get("outputs/production_showcase/summary.csv"), 2);
});

test("a path containing a literal newline is matched, and split on '/' only", () => {
  const row = byPath(NEWLINE_PATH);
  assert.ok(row.name.includes("\n"));
  assert.equal(row.path.split("/").length, 2);
  assert.equal(row.name, "file\nwith_newline.txt");

  assert.deepEqual(tree("with_newline"), [NEWLINE_PATH]);
  assert.deepEqual(pathsOf(band("with_newline")), [NEWLINE_PATH]);
  // a dir path with an embedded newline is ONE segment after the last "/", never two lines
  assert.deepEqual(folderHits(["weird/dir\nname"], ["dir"], 3), ["weird/dir\nname"]);
  assert.deepEqual(folderHits(["weird/dir\nname"], ["name"], 3), ["weird/dir\nname"]);
  assert.deepEqual(folderHits(["weird/dir\nname"], ["weird"], 3), []);
});

test("filters and free tokens compose in the band as well as the tree", () => {
  assert.deepEqual(pathsOf(band("ext:csv popv")), ["outputs/popv_v5/umap/popv_summary.csv"]);
  assert.deepEqual(tree("ext:csv popv"), ["outputs/popv_v5/umap/popv_summary.csv"]);
});

test("nothing matches → empty band and empty tree, never a throw", () => {
  assert.deepEqual(tree("zzzznotarealtoken"), []);
  assert.deepEqual(band("zzzznotarealtoken"), []);
  assert.deepEqual(band("ext:png zzzznotarealtoken"), []);
  assert.deepEqual(band("popv umap", "newest", 0), []);
});

test("determinism — repeated calls return identical results", () => {
  const dirs = ["a/popv_umap", "b/xx_popv_umap", "d/popv_umap_extra"];
  const t3 = new Set(["outputs/lipids/module_scores.csv"]);
  for (const q of ["", "ext:png", "popv umap", "production umap", "cholesterol"]) {
    const a = topHits(CORPUS, parseQuery(q), t3, dirs, "newest", BAND_SIZE);
    const b = topHits(CORPUS, parseQuery(q), t3, dirs, "newest", BAND_SIZE);
    assert.deepEqual(a, b, `q=${JSON.stringify(q)}`);
    assert.deepEqual(treeRows(CORPUS, parseQuery(q)), treeRows(CORPUS, parseQuery(q)));
  }
});

test("buildCorpus is index-aligned and byPath joins on the RAW path", () => {
  assert.equal(CORPUS.rows.length, FIXTURE.length);
  assert.equal(CORPUS.nameLc.length, FIXTURE.length);
  assert.equal(CORPUS.pathLc.length, FIXTURE.length);
  for (let i = 0; i < FIXTURE.length; i++) {
    assert.equal(CORPUS.nameLc[i], foldToken(FIXTURE[i].name));
    assert.equal(CORPUS.pathLc[i], foldToken(FIXTURE[i].path));
    assert.equal(CORPUS.byPath.get(FIXTURE[i].path), i);
  }
  // the backend returns RAW paths — the join key must not be the folded one (finding C6)
  assert.equal(CORPUS.byPath.get("reports/Report_Final.md"), 19);
  assert.equal(CORPUS.byPath.get("reports/report_final.md"), undefined);
});

// ═══════════════════════════════════════════════════════════════════════════════════════════
// 3. PURITY & PERFORMANCE
// ═══════════════════════════════════════════════════════════════════════════════════════════

const SRC: string = readFileSync(new URL("./query.ts", import.meta.url), "utf8");

test("query.ts is pure: no DOM, no globals, no imports at all", () => {
  assert.ok(!/\bdocument\b/.test(SRC), "query.ts must not reference document");
  assert.ok(!/\bwindow\b/.test(SRC), "query.ts must not reference window");
  assert.ok(!/\bnavigator\b/.test(SRC), "query.ts must not reference navigator");
  assert.ok(!/\blocalStorage\b/.test(SRC), "query.ts must not reference localStorage");
  assert.ok(!/^\s*import\s/m.test(SRC), "query.ts must import nothing (no main.ts, no Tauri)");
  assert.ok(!/\binvoke\b/.test(SRC), "query.ts must not reach for IPC");
});

test("topHits never sorts the candidate set, and stays inside the interaction budget", () => {
  const N = 60_000;
  const big: RowLike[] = new Array<RowLike>(N);
  for (let i = 0; i < N; i++) {
    big[i] = {
      id: i + 1,
      path: `outputs/b${i % 97}/d${i % 31}/asset_${i}.png`,
      name: `asset_${i}.png`,
      category: "figure",
      ext: "png",
      size_bytes: i * 7 + 1,
      mtime: new Date(BASE_MS + i * 60_000).toISOString(),
    };
  }
  const bigCorpus = buildCorpus(big);
  const q = parseQuery("ext:png");

  // 1) the structural invariant: bounded insertion, NEVER Array.prototype.sort over 60k rows
  const proto = Array.prototype as unknown as { sort: unknown };
  const realSort = proto.sort;
  let sortCalls = 0;
  proto.sort = function patched(this: unknown[], ...args: unknown[]): unknown {
    sortCalls++;
    return (realSort as (...a: unknown[]) => unknown).apply(this, args);
  };
  let hits: Hit[];
  try {
    hits = topHits(bigCorpus, q, NO_TIER3, NO_DIRS, "newest", BAND_SIZE);
  } finally {
    proto.sort = realSort;
  }
  assert.equal(sortCalls, 0, "topHits must not sort the candidate set");
  assert.equal(hits.length, BAND_SIZE);
  assert.equal(hits[0].row?.name, `asset_${N - 1}.png`); // newest really is newest

  // 2) the budget. A full comparator sort of 60k rows is ~200 ms+; the bounded insertion is a
  //    single pass. The threshold is deliberately loose (machines vary) but far below anything
  //    an O(n log n) comparator sort can reach.
  const t0 = performance.now();
  for (let rep = 0; rep < 3; rep++) {
    topHits(bigCorpus, q, NO_TIER3, NO_DIRS, "newest", BAND_SIZE);
  }
  const per = (performance.now() - t0) / 3;
  assert.ok(per < 60, `filter-only topHits over ${N} rows took ${per.toFixed(1)} ms (budget 60)`);

  // 3) the free-token path over the same 60k rows (two passes: full match + backfill)
  const t1 = performance.now();
  const tokenHits = topHits(
    bigCorpus,
    parseQuery("asset_59999"),
    NO_TIER3,
    NO_DIRS,
    "newest",
    BAND_SIZE,
  );
  const tokenMs = performance.now() - t1;
  assert.equal(tokenHits.length, 1);
  assert.equal(tokenHits[0].row?.name, "asset_59999.png");
  assert.ok(tokenMs < 60, `free-token topHits took ${tokenMs.toFixed(1)} ms (budget 60)`);
});
