//! `defer` — the self-event deferral registry (PHASE_0_1_SPEC.md §5.5). The load-bearing correctness
//! choice is **defer, not suppress**: a naive `(path, op, TTL)` suppression window is LOSSY because
//! FSEvents COALESCES changes to one path into a single event with a union of flags — one delivered
//! event for `dst` can represent BOTH our rename AND a near-simultaneous FOREIGN write, so dropping
//! it drops the foreign change and the index rots silently (§5.1). Instead we PARK the matching event
//! and, at drain, hand its path back to `reconcile_paths`, which `stat`s it as it is on disk NOW —
//! our change PLUS any coalesced foreign write. The token FAILS OPEN (reconcile on lease expiry even
//! without a clean completion).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crate::pathkey::{basename_of, parent_of, NormPath};

/// How long a self-event token stays active before the fail-open lease expires WITHOUT a renewal
/// (§5.5). A long-running Phase-4 copy renews the lease while bytes flow; a Phase-1 rename/mkdir
/// completes well within one TTL, so no renewal is exercised.
pub const LEASE_TTL: Duration = Duration::from_secs(10);
/// The short trailing window after an op commits, during which a coalesced foreign change is still
/// drained once (§5.6).
pub const DRAIN_MARGIN: Duration = Duration::from_millis(500);

/// The `._<basename>` AppleDouble sidecar sibling of a root-relative path (`rename(2)`/`unlink(2)`
/// orphans/moves it, surfacing a `._foo` event we must recognize as ours — the walker already skips
/// `._`, so it never becomes an `entries` row).
pub fn sidecar_of(rel: &str) -> String {
    let base = basename_of(rel);
    let parent = parent_of(rel);
    if parent.is_empty() {
        format!("._{base}")
    } else {
        format!("{parent}/._{base}")
    }
}

/// A self-mutation's full physical footprint for deferral matching (§5.5).
pub struct DeferralToken {
    pub op_id: i64,
    pub batch_id: String,
    /// EXACT normalized keys: src, dst, temp_path AND each `._<basename>` sidecar. FSEvents is
    /// file-level on this mount, so the footprint is a set of file paths (not dir-granular).
    pub footprint: HashSet<NormPath>,
    /// norm_key PREFIXES for a dir rename/copy whose descendant path list is UNENUMERABLE (§0.5G): an
    /// event matches if its `path_key` falls under any prefix.
    pub subtree_prefixes: HashSet<NormPath>,
    /// The RAW abs paths of the dir-op roots (src/dst for a directory rename) — handed to
    /// `reconcile_tree` at drain so the subtree is re-stated at its true casing, NOT the folded key.
    pub subtree_roots_raw: Vec<PathBuf>,
    /// A LEASE, not a fixed deadline (§0.5C): renewed while bytes flow; times out only on a stalled
    /// lease (no renewal within the window) → fail-open reconcile.
    pub lease_until: Instant,
    /// Set when the op commits/fails — the short trailing drain window (§5.6).
    pub drain_at: Option<Instant>,
}

impl DeferralToken {
    fn matches(&self, ev: &NormPath) -> bool {
        self.footprint.contains(ev) || self.subtree_prefixes.iter().any(|p| ev.is_under(p))
    }
    fn expired(&self, now: Instant) -> bool {
        // Retire when the trailing drain window elapsed, OR the lease expired without renewal
        // (fail-open — a stalled op still reconciles).
        self.drain_at.map_or(false, |d| now >= d) || now >= self.lease_until
    }
}

/// The result of a drain tick (§5.5): parked point-paths whose LAST owning token retired (reconcile
/// these), the `subtree_prefixes` of the retired tokens (hand each to `reconcile_tree` — a dir
/// rename/copy touches an UNENUMERABLE descendant set, so a coalesced foreign descendant is caught by
/// re-stating the whole subtree, not just the parked points), and the retired op ids (the driver
/// transitions `committed → settled`).
pub struct RetireResult {
    /// RAW abs paths of the drained parked events (the drain re-stats these via `reconcile_paths`).
    pub drained: Vec<PathBuf>,
    /// RAW abs paths of the retired tokens' dir-op roots (re-stated via `reconcile_tree`).
    pub subtree_roots: Vec<PathBuf>,
    pub retired_ops: Vec<i64>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum DeferDecision {
    /// Parked (a matched non-own event) or Layer-2-dropped (an own event).
    Deferred,
    /// External change, or the token already retired → reconcile this path now.
    Reconcile,
}

/// The registry of active self-event tokens + the parked-path ownership map. `maybe_defer` does NOT
/// retire internally — the watcher's dispatcher owns the drain (§5.5), so a drained path is never
/// discarded.
pub struct DeferralRegistry {
    tokens: Vec<DeferralToken>,
    /// norm-key → (owning op_ids, the event's RAW abs path). A key drains only when its LAST owner
    /// retires (so a path shared by two active tokens isn't prematurely un-deferred → no self-thrash,
    /// no stuck path); the raw path is carried so the drain reconciles the TRUE on-disk spelling, not
    /// the folded key (which would overwrite the row's raw `path` column, §4 review finding).
    parked: HashMap<NormPath, (HashSet<i64>, PathBuf)>,
}

impl Default for DeferralRegistry {
    fn default() -> Self {
        DeferralRegistry { tokens: Vec::new(), parked: HashMap::new() }
    }
}

impl DeferralRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, token: DeferralToken) {
        self.tokens.push(token);
    }

    /// Extend the lease for a long-running op (Phase-4 copy) so it never expires mid-copy (§0.5C).
    pub fn renew_lease(&mut self, op_id: i64) {
        if let Some(t) = self.tokens.iter_mut().find(|t| t.op_id == op_id) {
            t.lease_until = Instant::now() + LEASE_TTL;
        }
    }

    /// Open the short trailing drain window for an op (called when it commits/fails, §5.6).
    pub fn begin_drain(&mut self, op_id: i64, drain_at: Instant) {
        if let Some(t) = self.tokens.iter_mut().find(|t| t.op_id == op_id) {
            t.drain_at = Some(drain_at);
        }
    }

    /// Classify one FSEvent WITHOUT retiring (§5.5): a matched non-own event is PARKED (recorded
    /// against every owning token); an own-event match is dropped without parking (Layer-2, Phase-4
    /// only — Phase 1 always passes `own_event=false`); no match → `Reconcile`.
    pub fn maybe_defer(&mut self, ev: &NormPath, ev_raw: &Path, own_event: bool) -> DeferDecision {
        let owners: Vec<i64> = self.tokens.iter().filter(|t| t.matches(ev)).map(|t| t.op_id).collect();
        if owners.is_empty() {
            return DeferDecision::Reconcile;
        }
        if own_event {
            return DeferDecision::Deferred; // Layer-2 drop (optimization; never the correctness path)
        }
        let entry = self
            .parked
            .entry(ev.clone())
            .or_insert_with(|| (HashSet::new(), ev_raw.to_path_buf()));
        entry.1 = ev_raw.to_path_buf(); // keep the latest raw spelling for this key
        for op in owners {
            entry.0.insert(op);
        }
        DeferDecision::Deferred
    }

    /// Retire every token past its drain window OR whose lease expired, and return the parked paths
    /// whose LAST owning token is now gone (to reconcile) plus the retired op ids (§5.5).
    pub fn retire_expired(&mut self, now: Instant) -> RetireResult {
        let mut retired_ops = Vec::new();
        let mut subtree_roots: Vec<PathBuf> = Vec::new();
        self.tokens.retain(|t| {
            if t.expired(now) {
                retired_ops.push(t.op_id);
                subtree_roots.extend(t.subtree_roots_raw.iter().cloned());
                false
            } else {
                true
            }
        });
        if retired_ops.is_empty() {
            return RetireResult { drained: Vec::new(), subtree_roots, retired_ops };
        }
        let retired: HashSet<i64> = retired_ops.iter().copied().collect();
        let mut drained = Vec::new();
        let mut empty_keys = Vec::new();
        for (key, (owners, raw)) in self.parked.iter_mut() {
            owners.retain(|op| !retired.contains(op));
            if owners.is_empty() {
                empty_keys.push((key.clone(), raw.clone()));
            }
        }
        for (key, raw) in empty_keys {
            self.parked.remove(&key);
            drained.push(raw); // the RAW abs path (true casing), not the folded key
        }
        RetireResult { drained, subtree_roots, retired_ops }
    }

    /// Clear all tokens + parked paths — a project switch abandons the old tree's in-flight tokens
    /// (§4.8).
    pub fn clear(&mut self) {
        self.tokens.clear();
        self.parked.clear();
    }

    #[cfg(test)]
    pub fn active_token_count(&self) -> usize {
        self.tokens.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn token(op_id: i64, footprint: &[&str], prefixes: &[&str], lease: Duration) -> DeferralToken {
        DeferralToken {
            op_id,
            batch_id: "b".into(),
            footprint: footprint.iter().map(|s| NormPath::of(s)).collect(),
            subtree_prefixes: prefixes.iter().map(|s| NormPath::of(s)).collect(),
            subtree_roots_raw: prefixes.iter().map(PathBuf::from).collect(),
            lease_until: Instant::now() + lease,
            drain_at: None,
        }
    }

    #[test]
    fn sidecar_of_forms_appledouble_sibling() {
        assert_eq!(sidecar_of("a/b/c.h5ad"), "a/b/._c.h5ad");
        assert_eq!(sidecar_of("top.csv"), "._top.csv");
    }

    #[test]
    fn unmatched_event_reconciles_matched_event_parks() {
        let mut reg = DeferralRegistry::new();
        reg.register(token(1, &["dst.csv"], &[], LEASE_TTL));
        assert_eq!(
            reg.maybe_defer(&NormPath::of("other.csv"), Path::new("other.csv"), false),
            DeferDecision::Reconcile
        );
        assert_eq!(
            reg.maybe_defer(&NormPath::of("dst.csv"), Path::new("dst.csv"), false),
            DeferDecision::Deferred
        );
    }

    #[test]
    fn subtree_prefix_matches_descendants() {
        let mut reg = DeferralRegistry::new();
        reg.register(token(1, &["moved"], &["moved"], LEASE_TTL));
        assert_eq!(
            reg.maybe_defer(&NormPath::of("moved/deep/child.csv"), Path::new("moved/deep/child.csv"), false),
            DeferDecision::Deferred
        );
    }

    #[test]
    fn drain_returns_path_only_when_last_owner_retires() {
        let mut reg = DeferralRegistry::new();
        // two ops both park the same path
        reg.register(token(1, &["shared.csv"], &[], LEASE_TTL));
        reg.register(token(2, &["shared.csv"], &[], LEASE_TTL));
        reg.maybe_defer(&NormPath::of("shared.csv"), Path::new("shared.csv"), false);

        // retire op 1 only (give it an already-past drain window)
        reg.begin_drain(1, Instant::now() - Duration::from_secs(1));
        let r1 = reg.retire_expired(Instant::now());
        assert_eq!(r1.retired_ops, vec![1]);
        assert!(r1.drained.is_empty(), "path still owned by op 2 → not drained");

        // retire op 2 → now the path drains
        reg.begin_drain(2, Instant::now() - Duration::from_secs(1));
        let r2 = reg.retire_expired(Instant::now());
        assert_eq!(r2.retired_ops, vec![2]);
        assert_eq!(r2.drained, vec![PathBuf::from("shared.csv")]); // the RAW path, not the folded key
    }

    #[test]
    fn lease_expiry_is_fail_open() {
        let mut reg = DeferralRegistry::new();
        // a token whose lease already expired (no renewal) retires without a drain_at (fail-open)
        reg.register(token(9, &["x.csv"], &[], Duration::from_millis(0)));
        std::thread::sleep(Duration::from_millis(2));
        let r = reg.retire_expired(Instant::now());
        assert_eq!(r.retired_ops, vec![9], "stalled lease retires fail-open");
    }
}
