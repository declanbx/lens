// @ts-nocheck
// ════════════════════════════════════════════════════════════════════════════════════════════════
// expand.test.ts — unit tests for the adaptive expand budget (LOCKED SPEC §2.2 + §6 test plan).
//
//   cd tools/repo_index/lens && node --test src/expand.test.ts
//
// Node's built-in runner strips the types and runs this file directly — zero new dependencies.
// `@ts-nocheck` above is load-bearing: `npm run build` runs `tsc` over everything under src/, and
// this project deliberately ships no @types/node, so the node:test / node:assert / node:fs imports
// would otherwise fail the build. Nothing else in the file needs the escape hatch.
//
// Every fixture is hermetic — declared here, never read from the live index, whose counts churn on
// every reindex. Where a fixture reproduces a measured shape from the real repo, the measurement is
// named in a comment beside it.
// ════════════════════════════════════════════════════════════════════════════════════════════════

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import type { ExpandNode, ExpandPlan } from "./expand.ts";
import {
  HARD_CEILING,
  SOFT_BUDGET,
  expansionSurvives,
  planExpand,
  subtreeFiles,
  subtreeRows,
} from "./expand.ts";

// ── fixture builders ────────────────────────────────────────────────────────────────────────────

function node(path: string, ownFiles: number, dirs: ExpandNode[] = []): ExpandNode {
  return { path, ownFiles, dirs };
}

/** root with `childCount` leaf children of `filesPerChild` files each.
 *  fullBelow(root) === rootFiles + childCount * (1 + filesPerChild). */
function fanout(rootPath: string, childCount: number, filesPerChild: number, rootFiles = 0): ExpandNode {
  const dirs: ExpandNode[] = [];
  for (let i = 0; i < childCount; i++) dirs.push(node(`${rootPath}/c${i}`, filesPerChild));
  return node(rootPath, rootFiles, dirs);
}

function allDirPaths(n: ExpandNode, out: string[] = []): string[] {
  out.push(n.path);
  for (const c of n.dirs) allDirPaths(c, out);
  return out;
}

function indexByPath(n: ExpandNode, m = new Map<string, ExpandNode>()): Map<string, ExpandNode> {
  m.set(n.path, n);
  for (const c of n.dirs) indexByPath(c, m);
  return m;
}

/** Rows a UNIFORM-DEPTH implementation would render if it opened `depth` levels below n.
 *  Only used to demonstrate the trap the greedy walk exists to avoid (perf finding P3). */
function rowsAtDepth(n: ExpandNode, depth: number): number {
  if (depth <= 0) return 0;
  let total = n.ownFiles + n.dirs.length;
  for (const c of n.dirs) total += rowsAtDepth(c, depth - 1);
  return total;
}

/** Invariants every plan must satisfy, whatever the fixture or budget. */
function checkPlan(root: ExpandNode, plan: ExpandPlan, budget: number, label: string): void {
  const byPath = indexByPath(root);

  assert.ok(plan.rows <= budget, `${label}: rows ${plan.rows} exceeded budget ${budget}`);
  assert.ok(plan.rows <= HARD_CEILING, `${label}: rows ${plan.rows} exceeded HARD_CEILING`);
  assert.equal(new Set(plan.open).size, plan.open.length, `${label}: duplicate path in open`);
  assert.equal(plan.openedFolders, plan.open.length, `${label}: openedFolders != open.length`);
  assert.equal(plan.truncated, plan.closedFolders > 0, `${label}: truncated != (closedFolders > 0)`);
  assert.equal(plan.totalFiles, subtreeFiles(root), `${label}: totalFiles wrong`);
  assert.ok(plan.files <= plan.totalFiles, `${label}: files > totalFiles`);
  assert.equal(plan.ceilingHit, subtreeRows(root) - 1 > HARD_CEILING, `${label}: ceilingHit wrong`);

  // rows === file rows + header rows, where a header row is drawn for every child of an open dir.
  let headers = 0;
  for (const p of plan.open) {
    const n = byPath.get(p);
    assert.ok(n !== undefined, `${label}: plan.open holds a path not in the tree: ${p}`);
    headers += n.dirs.length;
  }
  assert.equal(plan.rows, plan.files + headers, `${label}: rows != files + header rows`);

  if (plan.open.length > 0) assert.equal(plan.open[0], root.path, `${label}: root not opened first`);
}

// ════════════════════════════════════════════════════════════════════════════════════════════════
// subtree arithmetic
// ════════════════════════════════════════════════════════════════════════════════════════════════

test("subtreeRows counts files + dir headers including the node's own header", () => {
  const leaf = node("a/b", 2);
  const root = node("a", 3, [leaf]);
  assert.equal(subtreeRows(leaf), 3); // 1 header + 2 files
  assert.equal(subtreeRows(root), 7); // 1 header + 3 files + 3
  assert.equal(subtreeFiles(root), 5);
  assert.equal(subtreeRows(node("empty", 0)), 1);
  assert.equal(subtreeFiles(node("empty", 0)), 0);
});

test("subtreeRows === dirCount + fileCount on a deep fixture", () => {
  const big = fanout("outputs", 40, 0);
  for (const c of big.dirs) for (let i = 0; i < 20; i++) c.dirs.push(node(`${c.path}/g${i}`, 48));
  const dirCount = allDirPaths(big).length; // 1 + 40 + 800
  assert.equal(dirCount, 841);
  assert.equal(subtreeRows(big), dirCount + subtreeFiles(big));
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// spec §6: planExpand on a subtree smaller than SOFT_BUDGET
// ════════════════════════════════════════════════════════════════════════════════════════════════

test("subtree under SOFT_BUDGET expands completely: truncated=false, closedFolders=0, open covers every dir", () => {
  const root = node("outputs", 4, [
    node("outputs/figures", 12, [node("outputs/figures/panels", 30)]),
    node("outputs/tables", 7),
  ]);
  const plan = planExpand(root, SOFT_BUDGET);
  checkPlan(root, plan, SOFT_BUDGET, "small");

  assert.equal(plan.truncated, false);
  assert.equal(plan.closedFolders, 0);
  assert.equal(plan.ceilingHit, false);
  assert.deepEqual(new Set(plan.open), new Set(allDirPaths(root)));
  assert.equal(plan.files, 53);
  assert.equal(plan.totalFiles, 53);
  assert.equal(plan.rows, subtreeRows(root) - 1); // 53 files + 3 descendant headers
  assert.equal(plan.rows, 56);
  assert.equal(plan.openedFolders, 4);
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// spec §6: the quantised fan-out fixture — a uniform depth budget cannot spend the budget
// ════════════════════════════════════════════════════════════════════════════════════════════════

/** Reproduces the measured outputs/ shape: L1=44, L2=828, L3=5,530 cumulative rows. */
function quantisedFanout(): ExpandNode {
  const root = node("outputs", 0, []);
  for (let i = 0; i < 44; i++) {
    const l1 = node(`outputs/l1_${i}`, 0, []);
    const subdirs = i < 36 ? 18 : 17; // 36*18 + 8*17 = 784 -> 44 + 784 = 828 at L2
    for (let j = 0; j < subdirs; j++) l1.dirs.push(node(`${l1.path}/l2_${j}`, 6));
    root.dirs.push(l1);
  }
  // 784 L2 dirs * 6 files = 4,704; shave 2 to land L3 on exactly 5,530.
  root.dirs[43].dirs[16].ownFiles = 5;
  root.dirs[43].dirs[15].ownFiles = 5;
  return root;
}

test("quantised fan-out fixture reproduces the measured L1/L2/L3 row counts", () => {
  const root = quantisedFanout();
  assert.equal(rowsAtDepth(root, 1), 44);
  assert.equal(rowsAtDepth(root, 2), 828);
  assert.equal(rowsAtDepth(root, 3), 5530);
  assert.equal(subtreeRows(root) - 1, 5530);
});

test("greedy walk spends the budget where a uniform depth budget stops at 55%", () => {
  const root = quantisedFanout();
  const plan = planExpand(root, SOFT_BUDGET);
  checkPlan(root, plan, SOFT_BUDGET, "quantised");

  // The trap: depth 2 renders 828 of 1,500 (55.2%); depth 3 blows straight past to 5,530.
  assert.ok(rowsAtDepth(root, 2) < 0.6 * SOFT_BUDGET);
  assert.ok(rowsAtDepth(root, 3) > SOFT_BUDGET);

  assert.ok(
    plan.rows > 0.6 * SOFT_BUDGET,
    `greedy walk rendered only ${plan.rows} of ${SOFT_BUDGET} — a depth rule in disguise`,
  );
  assert.equal(plan.rows, 1496); // pinned: 44 headers + 11 whole L1 subtrees + a partial 12th
  assert.equal(plan.truncated, true);
  assert.equal(plan.ceilingHit, false);
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// spec §6: rows never exceeds the budget, at SOFT_BUDGET and HARD_CEILING
// ════════════════════════════════════════════════════════════════════════════════════════════════

/** Every fixture in the file, keyed by name, so budget sweeps cover all of them. */
function allFixtures(): Array<[string, ExpandNode]> {
  return [
    ["median (5 rows)", medianFolder()],
    ["p99 (418 rows)", p99Folder()],
    ["max (39,559 rows)", maxFolder()],
    ["quantised fan-out", quantisedFanout()],
    ["flat 3,000 files", node("flat", 3000)],
    ["oversized first child", oversizedFirstChild()],
    ["ceiling boundary (8,000)", fanout("edge", 4, 1999)],
    ["ceiling boundary (8,001)", fanout("edge", 4, 1999, 1)],
    ["empty child", node("r", 0, [node("r/empty", 0), node("r/leaf", 3)])],
    ["single empty dir", node("solo", 0)],
  ];
}

test("rows never exceeds the budget for any fixture, at SOFT_BUDGET and HARD_CEILING", () => {
  for (const [name, root] of allFixtures()) {
    for (const budget of [0, 1, SOFT_BUDGET, HARD_CEILING]) {
      const plan = planExpand(root, budget);
      checkPlan(root, plan, budget, `${name} @ ${budget}`);
    }
  }
});

test("a budget above HARD_CEILING is clamped — there is no unbounded render path", () => {
  const root = maxFolder();
  for (const budget of [HARD_CEILING + 1, 39_559, 1e9, Number.POSITIVE_INFINITY]) {
    const plan = planExpand(root, budget);
    assert.ok(plan.rows <= HARD_CEILING, `budget ${budget} rendered ${plan.rows} rows`);
    checkPlan(root, plan, HARD_CEILING, `clamped @ ${budget}`);
  }
  // Degenerate budgets are floors, not crashes. NaN in particular must NOT slip through: every
  // `cost > NaN` comparison is false, so an unguarded NaN would expand the subtree unbounded.
  for (const budget of [0, -1, Number.NEGATIVE_INFINITY, Number.NaN]) {
    const plan = planExpand(root, budget);
    assert.equal(plan.rows, 0);
    assert.equal(plan.open.length, 0);
    assert.equal(plan.truncated, true);
  }
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// spec §6: an oversized child stays closed, later siblings still expand (pass-2 ordering)
// ════════════════════════════════════════════════════════════════════════════════════════════════

function oversizedFirstChild(): ExpandNode {
  return node("r", 2, [node("r/big", 5000), node("r/small", 10)]);
}

test("a child larger than the whole budget stays closed while its later siblings expand", () => {
  const root = oversizedFirstChild();
  const plan = planExpand(root, SOFT_BUDGET);
  checkPlan(root, plan, SOFT_BUDGET, "oversized");

  assert.deepEqual(plan.open, ["r", "r/small"]);
  assert.ok(!plan.open.includes("r/big"));
  assert.equal(plan.rows, 14); // 2 own files + 2 headers + 10 files under small
  assert.equal(plan.files, 12);
  assert.equal(plan.totalFiles, 5012);
  assert.equal(plan.closedFolders, 1);
  assert.equal(plan.truncated, true);
});

test("an oversized child does not starve siblings drawn after it, at any position", () => {
  // Same three folders, big in the middle: the two small ones must both open regardless.
  const root = node("r", 0, [node("r/a", 10), node("r/big", 5000), node("r/z", 10)]);
  const plan = planExpand(root, SOFT_BUDGET);
  assert.deepEqual(plan.open, ["r", "r/a", "r/z"]);
  assert.equal(plan.rows, 23); // 3 headers + 10 + 10
  checkPlan(root, plan, SOFT_BUDGET, "big in the middle");
});

test("a child too big to open whole is still opened PARTIALLY when its own direct cost fits", () => {
  // budget 100: root(2 dirs) -> heavy has 3 subdirs of 60 files each; heavy itself costs 3.
  const heavy = node("r/heavy", 0, [
    node("r/heavy/a", 60),
    node("r/heavy/b", 60),
    node("r/heavy/c", 60),
  ]);
  const root = node("r", 0, [heavy, node("r/tail", 5)]);
  const plan = planExpand(root, 100);
  checkPlan(root, plan, 100, "partial");

  assert.ok(plan.open.includes("r/heavy"), "heavy should open partially, not stay shut");
  assert.equal(plan.open.includes("r/heavy/c"), false, "the third sub-branch cannot fit");
  assert.equal(plan.truncated, true);
  assert.ok(plan.files < plan.totalFiles);
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// spec §6: display order is respected (the user's sort is never silently reordered)
// ════════════════════════════════════════════════════════════════════════════════════════════════

test("with children [big, small] both fitting, open lists big before small", () => {
  const root = node("r", 0, [node("r/big", 100), node("r/small", 5)]);
  const plan = planExpand(root, SOFT_BUDGET);
  assert.deepEqual(plan.open, ["r", "r/big", "r/small"]);
  checkPlan(root, plan, SOFT_BUDGET, "display order");
});

test("display order is pre-order, so a nested subtree's paths stay contiguous", () => {
  const root = node("r", 0, [
    node("r/b", 1, [node("r/b/b1", 1), node("r/b/b2", 1)]),
    node("r/a", 1),
  ]);
  const plan = planExpand(root, SOFT_BUDGET);
  assert.deepEqual(plan.open, ["r", "r/b", "r/b/b1", "r/b/b2", "r/a"]);
});

test("a cheaper later sibling is NOT promoted ahead of an expensive earlier one", () => {
  // A cheapest-first implementation would open r/z before r/a. Display order forbids that.
  const root = node("r", 0, [node("r/a", 900), node("r/z", 1)]);
  const plan = planExpand(root, SOFT_BUDGET);
  assert.deepEqual(plan.open, ["r", "r/a", "r/z"]);
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// spec §6: ceilingHit, and the files/totalFiles pair behind the notice text
// ════════════════════════════════════════════════════════════════════════════════════════════════

test("ceilingHit is true iff subtreeRows(root) - 1 > HARD_CEILING, independent of the budget", () => {
  const atCeiling = fanout("edge", 4, 1999); // fullBelow === 8,000
  const overCeiling = fanout("edge", 4, 1999, 1); // fullBelow === 8,001
  assert.equal(subtreeRows(atCeiling) - 1, HARD_CEILING);
  assert.equal(subtreeRows(overCeiling) - 1, HARD_CEILING + 1);

  for (const budget of [0, SOFT_BUDGET, HARD_CEILING]) {
    assert.equal(planExpand(atCeiling, budget).ceilingHit, false, `at ceiling @ ${budget}`);
    assert.equal(planExpand(overCeiling, budget).ceilingHit, true, `over ceiling @ ${budget}`);
  }
});

test("a subtree of exactly HARD_CEILING rows expands completely at HARD_CEILING", () => {
  const root = fanout("edge", 4, 1999);
  const plan = planExpand(root, HARD_CEILING);
  checkPlan(root, plan, HARD_CEILING, "exact ceiling");
  assert.equal(plan.rows, HARD_CEILING);
  assert.equal(plan.truncated, false);
  assert.equal(plan.files, plan.totalFiles);
  assert.deepEqual(new Set(plan.open), new Set(allDirPaths(root)));
});

test("files/totalFiles carry the notice text numbers", () => {
  const root = fanout("edge", 4, 1999, 1);
  const plan = planExpand(root, HARD_CEILING);
  checkPlan(root, plan, HARD_CEILING, "notice");
  // "5,998 of 7,997 files shown · too big to open at once"
  assert.equal(plan.files, 5998); // 1 root file + 3 whole children
  assert.equal(plan.totalFiles, 7997);
  assert.equal(plan.ceilingHit, true);
  assert.equal(plan.truncated, true);
  assert.equal(plan.closedFolders, 1);
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// the measured folder-size distribution: median 5, p99 418, max 39,559 rendered rows
// ════════════════════════════════════════════════════════════════════════════════════════════════

/** median folder: 5 rendered rows. */
function medianFolder(): ExpandNode {
  return node("assets/gene_modules", 3, [node("assets/gene_modules/cache", 1)]);
}

/** p99 folder: 418 rendered rows (4 subfolders, 414 files). */
function p99Folder(): ExpandNode {
  const root = node("outputs/ExampleProject/r1", 0, []);
  const per = [103, 103, 104, 104];
  per.forEach((n, i) => root.dirs.push(node(`${root.path}/s${i}`, n)));
  return root;
}

/** largest folder in the repo: 39,559 rendered rows across 841 dirs. */
function maxFolder(): ExpandNode {
  const root = fanout("outputs", 40, 0, 319);
  for (const c of root.dirs) for (let i = 0; i < 20; i++) c.dirs.push(node(`${c.path}/g${i}`, 48));
  return root;
}

test("median folder (5 rows) opens fully on the first click", () => {
  const root = medianFolder();
  assert.equal(subtreeRows(root) - 1, 5);
  const plan = planExpand(root, SOFT_BUDGET);
  checkPlan(root, plan, SOFT_BUDGET, "median");
  assert.equal(plan.rows, 5);
  assert.equal(plan.files, 4);
  assert.equal(plan.truncated, false);
  assert.equal(plan.ceilingHit, false);
  assert.deepEqual(new Set(plan.open), new Set(allDirPaths(root)));
});

test("p99 folder (418 rows) opens fully on the first click", () => {
  const root = p99Folder();
  assert.equal(subtreeRows(root) - 1, 418);
  const plan = planExpand(root, SOFT_BUDGET);
  checkPlan(root, plan, SOFT_BUDGET, "p99");
  assert.equal(plan.rows, 418);
  assert.equal(plan.files, 414);
  assert.equal(plan.truncated, false);
  assert.equal(plan.ceilingHit, false);
  assert.deepEqual(new Set(plan.open), new Set(allDirPaths(root)));
});

test("max folder (39,559 rows) is never rendered in full — the 85.6 s freeze is unreachable", () => {
  const root = maxFolder();
  assert.equal(subtreeRows(root) - 1, 39_559);
  assert.equal(allDirPaths(root).length, 841);

  const soft = planExpand(root, SOFT_BUDGET);
  checkPlan(root, soft, SOFT_BUDGET, "max @ soft");
  assert.ok(soft.rows <= SOFT_BUDGET);
  assert.equal(soft.truncated, true);
  assert.equal(soft.ceilingHit, true);
  assert.ok(soft.files < soft.totalFiles);
  assert.equal(soft.totalFiles, 38_719);
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// the opt-click / "show all" force-full path: RAISE the budget, never remove it
// ════════════════════════════════════════════════════════════════════════════════════════════════

test("opt-click raises SOFT_BUDGET to HARD_CEILING and shows strictly more, still bounded", () => {
  const root = maxFolder();
  const soft = planExpand(root, SOFT_BUDGET);
  const hard = planExpand(root, HARD_CEILING);
  checkPlan(root, hard, HARD_CEILING, "max @ hard");

  assert.ok(hard.rows > soft.rows, "opt-click must reveal more");
  assert.ok(hard.files > soft.files);
  assert.ok(hard.openedFolders > soft.openedFolders);
  assert.ok(hard.rows <= HARD_CEILING, "opt-click is still capped");
  assert.equal(hard.truncated, true, "39,559 rows cannot fit even at the ceiling");
  assert.equal(hard.ceilingHit, true);
  assert.ok(hard.files < hard.totalFiles);
});

test("opt-click completes a folder that merely overflowed the soft budget", () => {
  const root = fanout("mid", 10, 299); // fullBelow === 3,000
  assert.equal(subtreeRows(root) - 1, 3000);

  const soft = planExpand(root, SOFT_BUDGET);
  checkPlan(root, soft, SOFT_BUDGET, "mid @ soft");
  assert.equal(soft.truncated, true);
  assert.equal(soft.ceilingHit, false); // so the notice says "show all", not "too big"

  const hard = planExpand(root, HARD_CEILING);
  checkPlan(root, hard, HARD_CEILING, "mid @ hard");
  assert.equal(hard.rows, 3000);
  assert.equal(hard.truncated, false);
  assert.equal(hard.closedFolders, 0);
  assert.equal(hard.files, hard.totalFiles);
  assert.deepEqual(new Set(hard.open), new Set(allDirPaths(root)));
});

test("99% of folders are unaffected by opt-click: soft and hard plans are identical", () => {
  for (const root of [medianFolder(), p99Folder()]) {
    const soft = planExpand(root, SOFT_BUDGET);
    const hard = planExpand(root, HARD_CEILING);
    assert.deepEqual(soft, hard);
  }
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// edge cases and bookkeeping
// ════════════════════════════════════════════════════════════════════════════════════════════════

test("a folder whose own direct contents outrun the budget reports truncated, not silence", () => {
  const root = node("flat", 3000); // 3,000 own files, no subdirs
  const soft = planExpand(root, SOFT_BUDGET);
  assert.equal(soft.rows, 0);
  assert.equal(soft.files, 0);
  assert.deepEqual(soft.open, []);
  assert.equal(soft.openedFolders, 0);
  assert.equal(soft.truncated, true, "a plan that draws nothing must offer the show-all escape");
  assert.equal(soft.closedFolders, 1);
  assert.equal(soft.ceilingHit, false);
  assert.equal(soft.totalFiles, 3000);

  const hard = planExpand(root, HARD_CEILING);
  assert.equal(hard.rows, 3000);
  assert.equal(hard.truncated, false);
});

test("an empty child dir is neither opened nor reported as truncated", () => {
  const root = node("r", 0, [node("r/empty", 0), node("r/leaf", 3)]);
  const plan = planExpand(root, SOFT_BUDGET);
  checkPlan(root, plan, SOFT_BUDGET, "empty child");
  assert.deepEqual(plan.open, ["r", "r/leaf"]);
  assert.equal(plan.closedFolders, 0);
  assert.equal(plan.truncated, false);
  assert.equal(plan.rows, 5); // 2 header rows + 3 files
});

test("an entirely empty folder plans cleanly", () => {
  const root = node("solo", 0);
  const plan = planExpand(root, SOFT_BUDGET);
  checkPlan(root, plan, SOFT_BUDGET, "solo");
  assert.deepEqual(plan.open, ["solo"]);
  assert.equal(plan.rows, 0);
  assert.equal(plan.files, 0);
  assert.equal(plan.totalFiles, 0);
  assert.equal(plan.truncated, false);
});

test("closedFolders counts a closed child AND everything beneath it", () => {
  // budget 3: root opens (3 rows: 1 file + 2 headers); neither child can open.
  const buried = node("r/x", 400, [node("r/x/y", 5, [node("r/x/y/z", 5)])]);
  const root = node("r", 1, [buried, node("r/w", 400)]);
  const plan = planExpand(root, 3);
  checkPlan(root, plan, 3, "closedFolders");
  assert.deepEqual(plan.open, ["r"]);
  assert.equal(plan.closedFolders, 4); // x + y + z + w
  assert.equal(plan.truncated, true);
});

test("planExpand is deterministic and does not mutate the tree", () => {
  const root = quantisedFanout();
  const before = JSON.stringify(root);
  const a = planExpand(root, SOFT_BUDGET);
  const b = planExpand(root, SOFT_BUDGET);
  assert.deepEqual(a, b);
  assert.equal(JSON.stringify(root), before, "planExpand must not mutate ExpandNode");
});

test("planExpand on the largest fixture stays fast (memoised subtree sums)", () => {
  const root = maxFolder();
  const t0 = performance.now();
  for (let i = 0; i < 20; i++) planExpand(root, HARD_CEILING);
  const ms = (performance.now() - t0) / 20;
  assert.ok(ms < 25, `planExpand averaged ${ms.toFixed(2)} ms over 841 dirs / 39,559 rows`);
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// expansionSurvives — which rebuilds retire a plan, and which must preserve place
//
// REGRESSION. A plan is costed against one row set, so a FILTER change must retire it (clearing a
// png filter turned an 8,000-row plan into 21,899 uncosted rows). But the rebuild path is shared:
// loadBrowse() re-applies the SAME query to fresh rows after every live-index flush, and retiring
// there collapsed the user's expanded tree while the filter visibly stayed on — breaking the
// PLACE-PRESERVING invariant stated at main.ts:1944-1946.
// ════════════════════════════════════════════════════════════════════════════════════════════════

test("expansionSurvives: a live refresh re-applying the SAME query preserves the expansion", () => {
  // The reported bug, as a test: filter on, folder expanded, watcher flush → loadBrowse → applyQuery.
  assert.equal(expansionSurvives("ext:png", "ext:png"), true);
  assert.equal(expansionSurvives("", ""), true, "boot and unfiltered refresh preserve place too");
  assert.equal(
    expansionSurvives("cholesterol ext:png", "cholesterol ext:png"),
    true,
    "free text + filter is still the same query",
  );
});

test("expansionSurvives: any change to the query retires the plan", () => {
  assert.equal(expansionSurvives("ext:png", ""), false, "clearing the filter — the 21,899-row case");
  assert.equal(expansionSurvives("", "ext:png"), false, "adding a filter");
  assert.equal(expansionSurvives("ext:png", "ext:png ext:svg"), false, "widening to a second chip");
  assert.equal(expansionSurvives("ext:png", "ext:svg"), false, "swapping the chip");
  assert.equal(expansionSurvives("umap", "umap "), false, "trailing space still recomposes the rows");
});

// ════════════════════════════════════════════════════════════════════════════════════════════════
// purity: the module must be usable outside a browser and inside a test runner
// ════════════════════════════════════════════════════════════════════════════════════════════════

test("expand.ts is pure: no DOM, no ambient browser globals, no import from main.ts", () => {
  const src = readFileSync(new URL("./expand.ts", import.meta.url), "utf8");
  const banned = /\b(document|window|globalThis|navigator|localStorage|HTMLElement|requestAnimationFrame)\b/;
  const hit = src.match(banned);
  assert.equal(hit, null, `expand.ts references a browser global: ${hit?.[0]}`);
  assert.equal(/from\s+["'][^"']*main(\.ts)?["']/.test(src), false, "expand.ts must not import main.ts");
  assert.equal(/^\s*import\s/m.test(src), false, "expand.ts should have no imports at all");
});
