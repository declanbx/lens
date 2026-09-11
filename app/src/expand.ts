// ════════════════════════════════════════════════════════════════════════════════════════════════
// expand.ts — the adaptive expand budget behind "expand all here"          (LOCKED SPEC §2.2)
//
// PURE MODULE. No DOM access, no ambient globals, no import from main.ts. src/expand.test.ts greps
// this file to prove that claim, so keep it free of browser identifiers even inside comments.
//
// WHY A BUDGET AT ALL. The largest folder in this repo renders 39,559 rows; drawing it in full is a
// measured 85,601 ms freeze plus 1,799 ms teardown in the WebKit view (perf finding P1). So there is
// no unbounded mode: planExpand is the only sanctioned way to open a subtree, and it can never plan
// more than HARD_CEILING rows. Option-click / the visible "show all" word RAISE the budget from
// SOFT_BUDGET to HARD_CEILING — they never remove it.
//
// WHY GREEDY-PER-SUBTREE AND NOT A UNIFORM DEPTH LIMIT (perf finding P3). Fan-out here is quantised
// 5-30x per level — outputs/ goes L1=44 -> L2=828 -> L3=5,530 — so a depth rule lands at 828 of a
// 1,500-row budget (55%) and physically cannot spend the rest. The walk below descends in DISPLAY
// order (the caller has already applied sortDirs), so the user's active sort is never silently
// reordered, and it uses two passes so a child too large to open whole cannot eat the budget its
// siblings would have used.
//
// The user-facing label counts FILES, not levels ("1,482 of 12,431 files shown · show all") — hence
// `files` and `totalFiles` on the plan alongside the internal `rows`.
// ════════════════════════════════════════════════════════════════════════════════════════════════

/** DOM-free structural view of TreeDir (main.ts:317-329). The caller flattens `TreeDir.dirs`
 *  (a Map) into an array in display order — i.e. through sortDirs — before calling in. */
export interface ExpandNode {
  path: string;
  /** d.files.length AFTER filtering — the budget counts rows that will actually be drawn. */
  ownFiles: number;
  /** ALREADY in display order. */
  dirs: ExpandNode[];
}

export interface ExpandPlan {
  /** Dir paths to add to openDirs, including the clicked root. Pre-order, display order. */
  open: string[];
  /** Rendered rows the plan produces (file rows + dir header rows), EXCLUDING the clicked root's
   *  own header row, which is already on screen. Never exceeds the budget, never HARD_CEILING. */
  rows: number;
  /** File rows only — this is the number shown to the user. */
  files: number;
  /** Files in the whole subtree, expanded or not — the "of 12,431" half of the notice. */
  totalFiles: number;
  openedFolders: number;
  /** Descendant folders left closed. (If the clicked root itself did not fit, every folder in the
   *  subtree is left closed and is counted here, root included — otherwise a plan that renders
   *  nothing would report truncated=false and the UI would show no "show all" escape hatch.) */
  closedFolders: number;
  truncated: boolean;
  /** True iff even HARD_CEILING could not fit the subtree — independent of the budget passed in. */
  ceilingHit: boolean;
}

/** Default budget. Measured: 51 ms to render 1,000 rows, 119 ms for 2,000; 1,500 sits inside the
 *  100 ms perceptual budget once buildTree's 3-32 ms is added, and expands 99.54% of the 9,151
 *  folders in this repo completely on the first click. */
export const SOFT_BUDGET = 1500;

/** The raised budget behind option-click and the "show all" word. Measured 980 ms — unpleasant but
 *  survivable. Nothing in the app may plan above this. */
export const HARD_CEILING = 8000;

/** Does a budgeted expansion survive the rebuild that is about to happen?
 *
 *  A plan is only valid for the row set it was costed against, so a FILTER change must retire it:
 *  `outputs` budgeted to 8,000 png rows becomes 21,899 uncosted rows the moment the png chip comes
 *  off. But the rebuild path is shared. `loadBrowse()` re-applies the SAME query to a fresh row set
 *  after every live-index flush, and retiring there collapses the user's expanded tree while the
 *  filter visibly stays on — which is a bug report, not a safety feature, and it contradicts the
 *  PLACE-PRESERVING invariant the live-refresh path documents at main.ts:1944-1946.
 *
 *  So the test is the QUERY, not the call. Same query in and out → the rows moved underneath us and
 *  the plan still describes what the user asked for → keep it. Different query → retire it.
 *  Compared as the composed string, which is what actually determines the row set. */
export function expansionSurvives(prevQuery: string, nextQuery: string): boolean {
  return prevQuery === nextQuery;
}

// ── memoisation ─────────────────────────────────────────────────────────────────────────────────
// Three WeakMaps keyed on the node, built once per public call. The walk asks for fullBelow(child)
// in pass 1 and then, for deferred children, re-derives the same subtree sums one level down; the
// memo turns that quadratic re-walk into a single pass over the subtree.

interface Memo {
  rows: WeakMap<ExpandNode, number>;
  files: WeakMap<ExpandNode, number>;
  dirs: WeakMap<ExpandNode, number>;
}

function newMemo(): Memo {
  return { rows: new WeakMap(), files: new WeakMap(), dirs: new WeakMap() };
}

/** Rows the whole subtree occupies when fully open, INCLUDING n's own header row.
 *  Identity: rowsIn(n) === dirsIn(n) + filesIn(n). */
function rowsIn(n: ExpandNode, m: Memo): number {
  const hit = m.rows.get(n);
  if (hit !== undefined) return hit;
  let total = 1 + n.ownFiles;
  for (const c of n.dirs) total += rowsIn(c, m);
  m.rows.set(n, total);
  return total;
}

function filesIn(n: ExpandNode, m: Memo): number {
  const hit = m.files.get(n);
  if (hit !== undefined) return hit;
  let total = n.ownFiles;
  for (const c of n.dirs) total += filesIn(c, m);
  m.files.set(n, total);
  return total;
}

/** Dirs in the subtree, INCLUDING n itself — i.e. `1 + descendantDirCount(n)`. */
function dirsIn(n: ExpandNode, m: Memo): number {
  const hit = m.dirs.get(n);
  if (hit !== undefined) return hit;
  let total = 1;
  for (const c of n.dirs) total += dirsIn(c, m);
  m.dirs.set(n, total);
  return total;
}

/** Files + dir header rows over the whole subtree, n's own header row included. */
export function subtreeRows(n: ExpandNode): number {
  return rowsIn(n, newMemo());
}

/** Files over the whole subtree. */
export function subtreeFiles(n: ExpandNode): number {
  return filesIn(n, newMemo());
}

// ── the budget walk ─────────────────────────────────────────────────────────────────────────────

/**
 * Plan how far "expand all here" may open `root` within `budget` rendered rows.
 *
 *   selfCost(D)  = D.ownFiles + D.dirs.length     rows drawn by opening D, minus D's own header row
 *   fullBelow(D) = subtreeRows(D) - 1             rows drawn by opening D and everything under it
 *
 * The walk is greedy in display order, two passes per opened folder:
 *   pass 1 opens outright every child whose ENTIRE subtree fits, deferring the rest;
 *   pass 2 revisits the deferred children in the same order and recurses into them with whatever
 *          budget is left, so a child that cannot be opened whole may still be opened partially,
 *          and one oversized child never starves the siblings drawn after it.
 *
 * `budget` is clamped to [0, HARD_CEILING]: a caller that asks for more than the ceiling (or for
 * Infinity) still gets a bounded plan, because there is no supported unbounded render path.
 */
export function planExpand(root: ExpandNode, budget: number): ExpandPlan {
  const memo = newMemo();
  const plan: ExpandPlan = {
    open: [],
    rows: 0,
    files: 0,
    totalFiles: filesIn(root, memo),
    openedFolders: 0,
    closedFolders: 0,
    truncated: false,
    ceilingHit: rowsIn(root, memo) - 1 > HARD_CEILING,
  };

  // Clamp into [0, HARD_CEILING]. +Infinity saturates at the ceiling, -Infinity at 0; NaN is treated
  // as 0 rather than allowed through, because every `cost > NaN` comparison below is false and a NaN
  // budget would otherwise expand the whole subtree unbounded.
  const cap = Number.isNaN(budget) ? 0 : Math.min(Math.max(Math.floor(budget), 0), HARD_CEILING);

  const selfCost = (d: ExpandNode): number => d.ownFiles + d.dirs.length;
  const fullBelow = (d: ExpandNode): number => rowsIn(d, memo) - 1;

  // Open d and every dir beneath it, unconditionally. Callers must have checked fullBelow(d) first.
  const openAll = (d: ExpandNode): void => {
    plan.open.push(d.path);
    plan.openedFolders++;
    plan.files += d.ownFiles;
    for (const c of d.dirs) openAll(c);
  };

  // Returns rows consumed. 0 means d stayed closed (d is NOT in plan.open).
  const spend = (d: ExpandNode, remaining: number): number => {
    const cost = selfCost(d);
    if (cost > remaining) return 0;

    plan.open.push(d.path);
    plan.openedFolders++;
    plan.files += d.ownFiles;
    let left = remaining - cost;
    let spent = cost;

    const deferred: ExpandNode[] = [];
    for (const c of d.dirs) {
      const below = fullBelow(c);
      // c holds nothing at all; its header row is already paid for by d's selfCost, and opening it
      // would draw zero rows. Leave it alone — neither opened nor reported as truncated.
      if (below === 0) continue;
      if (below <= left) {
        openAll(c);
        left -= below;
        spent += below;
      } else {
        deferred.push(c);
      }
    }

    for (const c of deferred) {
      const s = spend(c, left);
      if (s === 0) plan.closedFolders += dirsIn(c, memo); // c itself + everything under it
      else {
        left -= s;
        spent += s;
      }
    }
    return spent;
  };

  plan.rows = spend(root, cap);
  if (plan.openedFolders === 0) {
    // The clicked folder's own direct contents outrun the budget, so nothing opened at all. Report
    // the whole subtree as left-closed; `truncated` then drives the "show all" affordance, which
    // re-plans at HARD_CEILING.
    plan.closedFolders = dirsIn(root, memo);
  }
  plan.truncated = plan.closedFolders > 0;
  return plan;
}
