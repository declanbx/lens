//! `watcher` — the FSEvents watcher + debounced, stat-is-truth reconciler (PHASE_0_1_SPEC.md §4.1–§4.3).
//! Turns the read-only viewer into a self-updating index: `⟳` becomes automatic and the tree never
//! lags disk by more than ~2 s.
//!
//! Design (§0.5G): use `notify`'s RAW watcher (the reconciler owns debounce, NOT notify) behind a
//! `Watcher` trait so a later swap to raw `fsevent-sys` (for MarkSelf/OwnEvent, Phase 4) is a cheap
//! backend change. The callback does ZERO work → it pushes onto a channel; a dedicated `std::thread`
//! owns the dirty-set + debounce and is the ONLY caller of the reconcile authority. Events are HINTS,
//! never facts — the reconciler `lstat`s and believes only the filesystem.
//!
//! Phase 1 processes FOREIGN changes (the deferral/drain self-event machinery in ops.rs is built +
//! tested but dormant in the app, since `apply_op` is internal/test-only — no FS-mutating command).
//!
//! §4.9 HEALTH + RE-ARM. The OS watch used to be armed exactly ONCE, in [`WatcherHandle::spawn`],
//! and the `notify` watcher was then parked in a `_watcher` keep-alive field with no re-arm path
//! anywhere. On this app's normal machine — a project living on an external drive — that is a
//! silent failure: unplug the drive and the OS watch dies with it, replug it and NOTHING
//! re-establishes it. The window keeps running, keeps serving the old catalogue, and stops
//! noticing disk forever, with no indicator to say so.
//!
//! The fix is a SECOND, cheap thread ([`monitor_loop`]) that owns the health state and can re-arm.
//! It is deliberately not the reconciler: when idle the reconciler blocks in `recv_timeout` for
//! [`IDLE_TIMEOUT`] (an hour), so a returning drive would wait up to an hour to be noticed, and
//! shortening that constant would disturb the event debouncing it exists to serve.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU8, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use notify::{RecursiveMode, Watcher};

use crate::reconcile::{reconcile_paths, reconcile_tree, Change, ReconcileCtx, WalkConfig};
use crate::writer::IndexWriter;

/// Flush this long after the LAST event (§4.3).
const QUIESCENCE: Duration = Duration::from_millis(300);
/// A sustained storm still flushes within this cap.
const MAX_LATENCY: Duration = Duration::from_secs(2);
/// Storm backstop: past this many point paths, collapse to a whole-tree rescan.
const DIRTY_HARD_CAP: usize = 10_000;
/// The recv timeout when there's nothing to flush (idle, or paused): effectively block on the channel.
const IDLE_TIMEOUT: Duration = Duration::from_secs(3600);
/// How often the health monitor asks the filesystem whether the root is still readable. A `read_dir`
/// of a mount point is cheap but NOT free (it is a real syscall against a possibly-sleeping external
/// disk), and nothing downstream needs sub-5s notice of a drive returning — so this is a floor, not
/// a target. `pause`/`resume`/teardown do not wait for it: they nudge the monitor's condvar.
const HEALTH_POLL: Duration = Duration::from_secs(5);

/// Callback invoked after each committed flush with the changed directory keys (the app emits the
/// `index-changed` Tauri event; tests use it to observe progress).
pub type OnChange = Box<dyn Fn(Vec<String>) + Send>;

enum Msg {
    Event(notify::Result<notify::Event>),
    Control(Control),
}

enum Control {
    Rescan,
    Pause,
    Resume,
    Stop,
}

/// Drop an event BEFORE it enters the dirty-set (§4.1): the index's own churn (`_repo_index/` +
/// `.repo_index/` — both spellings until §3.8 unifies), `._*` AppleDouble sidecars, `.DS_Store`, and
/// the prune-dir set (`.git`, `node_modules`, …). Keeping the index's WAL churn out of the reconciler
/// is the feedback-loop guard.
pub fn is_watch_excluded(rel: &str, config: &WalkConfig) -> bool {
    for comp in rel.split('/') {
        if comp == "_repo_index" || comp == ".repo_index" {
            return true;
        }
        if comp.starts_with("._") || comp == ".DS_Store" {
            return true;
        }
        if config.prune_dirs.contains(comp) {
            return true;
        }
    }
    false
}

/// The dirty-set: point paths (stat one) + recursive subtrees (rescan). A recursive entry subsumes
/// everything beneath it; a point under a recursive ancestor is dropped (§4.3). Keyed by RAW rel
/// (reconcile is idempotent, so any residual case-redundancy is harmless).
#[derive(Default)]
struct DirtySet {
    points: HashSet<String>,
    recursive: HashSet<String>,
    first_dirty: Option<Instant>,
    last_event: Option<Instant>,
}

fn is_descendant(child: &str, ancestor: &str) -> bool {
    if ancestor.is_empty() {
        return true;
    }
    child == ancestor
        || (child.len() > ancestor.len()
            && child.starts_with(ancestor)
            && child.as_bytes()[ancestor.len()] == b'/')
}

impl DirtySet {
    fn is_empty(&self) -> bool {
        self.points.is_empty() && self.recursive.is_empty()
    }

    fn touch(&mut self, now: Instant) {
        self.first_dirty.get_or_insert(now);
        self.last_event = Some(now);
    }

    fn mark_point(&mut self, rel: String, now: Instant) {
        self.touch(now);
        // covered by a recursive ancestor? then it's already going to be rescanned.
        if self.recursive.iter().any(|r| is_descendant(&rel, r)) {
            return;
        }
        self.points.insert(rel);
        if self.points.len() > DIRTY_HARD_CAP {
            self.mark_recursive(String::new(), now); // storm → whole-tree rescan
        }
    }

    fn mark_recursive(&mut self, rel: String, now: Instant) {
        self.touch(now);
        if rel.is_empty() {
            self.points.clear();
            self.recursive.clear();
            self.recursive.insert(String::new());
            return;
        }
        self.points.retain(|p| !is_descendant(p, &rel));
        self.recursive.retain(|r| !is_descendant(r, &rel));
        // if an ancestor recursive already covers rel, nothing to add
        if self.recursive.iter().any(|r| is_descendant(&rel, r)) {
            return;
        }
        self.recursive.insert(rel);
    }

    /// Is the debounce satisfied (quiescence since the last event, or the max-latency cap reached)?
    fn ready(&self, now: Instant) -> bool {
        if self.is_empty() {
            return false;
        }
        let quiet = self.last_event.map_or(true, |t| now.saturating_duration_since(t) >= QUIESCENCE);
        let capped =
            self.first_dirty.map_or(false, |t| now.saturating_duration_since(t) >= MAX_LATENCY);
        quiet || capped
    }

    /// The recv_timeout to use: when idle, block long; when dirty, wake at the nearer of the
    /// quiescence / max-latency deadlines.
    fn timeout(&self, now: Instant) -> Duration {
        if self.is_empty() {
            return Duration::from_secs(3600);
        }
        let quiet_at = self.last_event.map(|t| t + QUIESCENCE);
        let cap_at = self.first_dirty.map(|t| t + MAX_LATENCY);
        let next = [quiet_at, cap_at].into_iter().flatten().min().unwrap_or(now);
        next.saturating_duration_since(now)
    }

    fn drain(&mut self) -> (Vec<String>, Vec<String>) {
        let recs: Vec<String> = self.recursive.drain().collect();
        let pts: Vec<String> = self.points.drain().collect();
        self.first_dirty = None;
        self.last_event = None;
        (recs, pts)
    }
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Health (§4.9) — what the UI's live indicator shows, and the ONE place it is decided.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// The four states the watcher can be in. The health monitor thread WRITES this; no consumer
/// recomputes it from "does a watcher object exist", because that is precisely the old bug: the
/// `notify` object is a keep-alive and outlives its OS watch, so a presence check answers `true` in
/// exactly the broken state (drive unplugged, watch dead, catalogue quietly frozen).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Health {
    /// Watching, and the root is readable.
    Live,
    /// The user parked live updates (`pause`, e.g. the in-app Rebuild).
    Paused,
    /// The root cannot be read — drive unplugged, renamed, or ejected.
    Unreachable,
    /// No watcher at all: read-only degrade mode, or no project open.
    Stopped,
}

impl Health {
    /// The wire spelling. Frozen — the frontend switches on exactly these four strings.
    pub fn as_str(self) -> &'static str {
        match self {
            Health::Live => "live",
            Health::Paused => "paused",
            Health::Unreachable => "unreachable",
            Health::Stopped => "stopped",
        }
    }

    fn as_u8(self) -> u8 {
        match self {
            Health::Live => 0,
            Health::Paused => 1,
            Health::Unreachable => 2,
            Health::Stopped => 3,
        }
    }

    fn from_u8(v: u8) -> Health {
        match v {
            0 => Health::Live,
            1 => Health::Paused,
            2 => Health::Unreachable,
            _ => Health::Stopped,
        }
    }
}

/// One consistent reading of the monitor's state — what `watch_status` answers with and what the
/// `watch-health` event carries.
#[derive(Clone, Copy, Debug)]
pub struct HealthSnapshot {
    pub health: Health,
    pub root_reachable: bool,
    pub rearms: u32,
}

/// Callback invoked ON EVERY TRANSITION of the health value, never on a timer (the app emits the
/// `watch-health` Tauri event; tests use it to count transitions).
pub type OnHealth = Box<dyn Fn(HealthSnapshot) + Send>;

/// The monitor's state, shared by the handle (for `watch_status`), the monitor thread (which owns
/// every write to `health`) and the reconciler (which reads `paused`). Atomics rather than a
/// `Mutex`: it is read from the IPC thread on every status poll and must never be blockable by —
/// or poisonable along with — a health tick that is mid-`read_dir` on a sleeping external disk.
struct HealthState {
    health: AtomicU8,
    /// Was the root readable at the last tick?
    reachable: AtomicBool,
    /// How many times the OS watch has been RE-established since spawn. The number is the proof:
    /// "live" after an unplug means nothing unless the watch was actually re-armed.
    rearms: AtomicU32,
    /// The existing pause control, hoisted out of the reconciler's local variable so the monitor can
    /// respect it too. Set by `pause`/`resume_with_full_rescan` BEFORE the control message is sent.
    paused: AtomicBool,
    /// One log line per unreachable episode / per re-arm failure — the monitor ticks forever and
    /// must not fill the log with the same sentence every 5 s.
    logged_unreachable: AtomicBool,
    logged_rearm_failure: AtomicBool,
}

impl HealthState {
    /// The state right after a SUCCESSFUL `watch()` — the only way a handle is ever constructed.
    fn live() -> HealthState {
        HealthState {
            health: AtomicU8::new(Health::Live.as_u8()),
            reachable: AtomicBool::new(true),
            rearms: AtomicU32::new(0),
            paused: AtomicBool::new(false),
            logged_unreachable: AtomicBool::new(false),
            logged_rearm_failure: AtomicBool::new(false),
        }
    }

    fn health(&self) -> Health {
        Health::from_u8(self.health.load(Ordering::SeqCst))
    }

    fn set_health(&self, h: Health) {
        self.health.store(h.as_u8(), Ordering::SeqCst);
    }

    fn snapshot(&self) -> HealthSnapshot {
        HealthSnapshot {
            health: self.health(),
            root_reachable: self.reachable.load(Ordering::SeqCst),
            rearms: self.rearms.load(Ordering::SeqCst),
        }
    }
}

/// Is `root` READABLE right now? A real `read_dir`, never `Path::exists`: on macOS an ejected or
/// force-unplugged volume can leave a stale-but-present directory entry under `/Volumes` that
/// `exists()` happily answers `true` for, while the first real read returns EIO/ENXIO. This is the
/// same signal `reconcile::walk_subtree` treats as "abort, do NOT sweep the index", so the monitor
/// and the reconciler agree on what "the drive is gone" means. `read_dir` only OPENS the directory
/// — the entries are never iterated — so the cost does not scale with the tree.
pub fn root_is_readable(root: &Path) -> bool {
    !root.as_os_str().is_empty() && std::fs::read_dir(root).is_ok()
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// The handle
// ───────────────────────────────────────────────────────────────────────────────────────────

/// A running watcher + its reconciler thread + its health monitor. Dropping (or `stop`) tears all
/// three down cleanly.
pub struct WatcherHandle {
    /// The `notify` watcher. This was `_watcher`: a keep-alive field, held only because dropping it
    /// is what stops FSEvents delivery. It is now SHARED with the health monitor, because the OS
    /// watch was armed exactly once and died with the first unplug of an external drive, with no
    /// re-arm path anywhere (§4.9) — re-arming means calling `unwatch`+`watch` on this very object.
    /// The keep-alive guarantee is unchanged: `shutdown` joins the monitor BEFORE this field drops,
    /// so the handle still holds the last `Arc` and dropping it still stops event delivery.
    ///
    /// Never READ through this field — the monitor works through its own clone — which is what the
    /// old `_` prefix was saying. Keep the ownership here anyway: if the handle did not hold an
    /// `Arc`, the watcher would die with the monitor THREAD, so a panicked monitor would silently
    /// end event delivery while the handle still looked alive.
    #[allow(dead_code)]
    watcher: Arc<Mutex<notify::RecommendedWatcher>>,
    control: Sender<Msg>,
    thread: Option<std::thread::JoinHandle<()>>,
    health: Arc<HealthState>,
    /// `(stop, condvar)` — the monitor waits on this instead of sleeping, so teardown and the
    /// pause/resume controls are acted on at once rather than up to `HEALTH_POLL` later.
    wake: Arc<(Mutex<bool>, Condvar)>,
    monitor: Option<std::thread::JoinHandle<()>>,
}

impl WatcherHandle {
    /// Watch-then-scan (§4.8): construct the `notify` watcher FIRST (events buffer into the channel),
    /// THEN the reconciler thread runs the initial `reconcile_tree("")` and begins draining — so an
    /// event during startup is never lost (buffered events replay harmlessly, idempotent). The
    /// health monitor starts last, in the `Live` state this successful `watch()` has just proven.
    pub fn spawn(
        root: &str,
        writer: Arc<IndexWriter>,
        ctx: Arc<ReconcileCtx>,
        on_change: OnChange,
        on_health: OnHealth,
    ) -> Result<WatcherHandle, String> {
        WatcherHandle::spawn_with_poll(root, writer, ctx, on_change, on_health, HEALTH_POLL)
    }

    /// [`spawn`](WatcherHandle::spawn) with the health poll interval exposed, so the tests can drive
    /// a real monitor thread in milliseconds instead of `HEALTH_POLL`. Production always takes the
    /// constant — a shorter interval in the app would spin against the disk for nothing.
    fn spawn_with_poll(
        root: &str,
        writer: Arc<IndexWriter>,
        ctx: Arc<ReconcileCtx>,
        on_change: OnChange,
        on_health: OnHealth,
        poll: Duration,
    ) -> Result<WatcherHandle, String> {
        let (tx, rx) = mpsc::channel::<Msg>();

        let ev_tx = tx.clone();
        let mut watcher = notify::recommended_watcher(move |res| {
            let _ = ev_tx.send(Msg::Event(res)); // callback does ZERO work
        })
        .map_err(|e| format!("notify watcher: {e}"))?;
        watcher
            .watch(Path::new(root), RecursiveMode::Recursive)
            .map_err(|e| format!("notify watch {root}: {e}"))?;
        let watcher = Arc::new(Mutex::new(watcher));

        let health = Arc::new(HealthState::live());
        let root_owned = root.to_string();
        let health_for_loop = health.clone();
        let thread = std::thread::spawn(move || {
            reconciler_loop(rx, root_owned, writer, ctx, on_change, health_for_loop);
        });

        let wake = Arc::new((Mutex::new(false), Condvar::new()));
        let monitor = {
            let root = PathBuf::from(root);
            let watcher = watcher.clone();
            let health = health.clone();
            let wake = wake.clone();
            let control = tx.clone();
            std::thread::spawn(move || {
                monitor_loop(root, watcher, health, control, on_health, wake, poll);
            })
        };

        Ok(WatcherHandle {
            watcher,
            control: tx,
            thread: Some(thread),
            health,
            wake,
            monitor: Some(monitor),
        })
    }

    /// The monitor's REAL state (never recomputed from object presence — see [`Health`]).
    pub fn health(&self) -> HealthSnapshot {
        self.health.snapshot()
    }

    /// Push a whole-tree rescan into the reconciler (the `force_rescan` command routes here — writing
    /// on the IPC thread would interleave with the reconciler's flush, §4.4).
    pub fn force_rescan(&self) {
        let _ = self.control.send(Msg::Control(Control::Rescan));
    }

    /// Stop flushing (buffer events) — used by the in-app Rebuild before the JSONL re-ingest (§3.6).
    /// The flag is set HERE rather than in the reconciler, so the health monitor sees the same pause
    /// the reconciler does; the control message still goes, to wake the reconciler out of its
    /// `recv_timeout`. The nudge makes the `paused` health visible immediately instead of at the
    /// next poll — a mode the user chose is not something to learn about 5 s later.
    pub fn pause(&self) {
        self.health.paused.store(true, Ordering::SeqCst);
        let _ = self.control.send(Msg::Control(Control::Pause));
        self.nudge_monitor();
    }

    /// Resume flushing and force a full rescan to reconverge Rust↔disk (§3.6).
    pub fn resume_with_full_rescan(&self) {
        self.health.paused.store(false, Ordering::SeqCst);
        let _ = self.control.send(Msg::Control(Control::Resume));
        self.nudge_monitor();
    }

    /// Wake the monitor out of its `wait_timeout` so it re-evaluates NOW. Never sets the stop flag.
    /// The `paused` flag is stored BEFORE the notify, so a notify that races the monitor into its
    /// wait costs one poll interval — never a missed transition, since the next tick reads the flag.
    fn nudge_monitor(&self) {
        self.wake.1.notify_all();
    }

    pub fn stop(mut self) {
        self.shutdown();
    }

    fn shutdown(&mut self) {
        // The MONITOR goes first. It holds an `Arc` on the `notify` watcher and a `Sender` on the
        // control channel, and it must not re-arm a watch that is about to be dropped — joining it
        // here is also what makes the handle the last `Arc` holder, preserving the original
        // "dropping the handle stops event delivery" guarantee.
        {
            let (stop, cv) = &*self.wake;
            // RECOVER from a poisoned lock rather than skipping: leaving the flag unset would make
            // the join below wait on a thread that never exits (the same class of stranding
            // `switch_project`'s `set_watcher` guards against).
            *stop.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = true;
            cv.notify_all();
        }
        if let Some(m) = self.monitor.take() {
            let _ = m.join();
        }
        // Safe to publish only now: with the monitor joined, nothing can write `health` again.
        self.health.set_health(Health::Stopped);

        let _ = self.control.send(Msg::Control(Control::Stop));
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

impl Drop for WatcherHandle {
    fn drop(&mut self) {
        self.shutdown();
    }
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// The health monitor (§4.9) — a second, cheap thread whose whole job is to notice the drive
// leaving, notice it coming back, and RE-ARM the OS watch when it does.
// ───────────────────────────────────────────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
fn monitor_loop(
    root: PathBuf,
    watcher: Arc<Mutex<notify::RecommendedWatcher>>,
    health: Arc<HealthState>,
    control: Sender<Msg>,
    on_health: OnHealth,
    wake: Arc<(Mutex<bool>, Condvar)>,
    poll: Duration,
) {
    let (stop, cv) = &*wake;
    loop {
        // Wait `poll`, or until woken by teardown / pause / resume. A condvar rather than a sleep so
        // `shutdown` never has to wait out a poll interval to join this thread.
        let guard = stop.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if *guard {
            return;
        }
        let (guard, _timed_out) =
            cv.wait_timeout(guard, poll).unwrap_or_else(|poisoned| poisoned.into_inner());
        if *guard {
            return;
        }
        drop(guard); // never hold the wake lock across a filesystem call
        health_tick(&root, &watcher, &health, &control, &on_health);
    }
}

/// One evaluation of the world: is the root readable, what does that make the health, and does the
/// OS watch need re-arming? Factored out of the loop so the tests can step it deterministically
/// instead of sleeping through poll intervals.
fn health_tick(
    root: &Path,
    watcher: &Mutex<notify::RecommendedWatcher>,
    health: &HealthState,
    control: &Sender<Msg>,
    on_health: &OnHealth,
) {
    let was = health.health();
    let reachable = root_is_readable(root);
    health.reachable.store(reachable, Ordering::SeqCst);
    let paused = health.paused.load(Ordering::SeqCst);

    // UNREACHABLE outranks PAUSED on purpose: pause is a mode the user chose and already knows
    // about, a missing drive is a fact about the world they do not. Reporting "paused" over a
    // vanished volume would hide the one thing the indicator exists to show.
    let mut now = if !reachable {
        Health::Unreachable
    } else if paused {
        Health::Paused
    } else {
        Health::Live
    };

    if was == Health::Unreachable && reachable {
        // The drive is back. The OS watch died with it and NOTHING else re-establishes one, so this
        // is the re-arm — and it must succeed before we are allowed to call ourselves live.
        match rearm(watcher, root) {
            Ok(()) => {
                health.rearms.fetch_add(1, Ordering::SeqCst);
                health.logged_unreachable.store(false, Ordering::SeqCst);
                health.logged_rearm_failure.store(false, Ordering::SeqCst);
                eprintln!(
                    "[lens] watcher: {} is back — OS watch re-armed (re-arm #{})",
                    root.display(),
                    health.rearms.load(Ordering::SeqCst)
                );
                // Reconverge with whatever changed while the drive was away. Events for those
                // changes were never delivered, so only a full rescan can find them. Through the
                // EXISTING rescan control — writing from this thread would interleave with the
                // reconciler's flush (§4.4). Not while paused: `resume_with_full_rescan` already
                // owes a full rescan, and the user asked for the index to hold still.
                if !paused {
                    let _ = control.send(Msg::Control(Control::Rescan));
                }
            }
            Err(e) => {
                // Try again next tick, forever — a drive that is mounting, or a root whose
                // permissions are still settling, comes good on its own. One line per episode.
                if !health.logged_rearm_failure.swap(true, Ordering::SeqCst) {
                    eprintln!(
                        "[lens] watcher: {} is readable again but the OS watch would not re-arm \
                         ({e}) — retrying every {}s",
                        root.display(),
                        HEALTH_POLL.as_secs()
                    );
                }
                now = Health::Unreachable;
            }
        }
    }

    if now == Health::Unreachable && was != Health::Unreachable {
        if !health.logged_unreachable.swap(true, Ordering::SeqCst) {
            eprintln!(
                "[lens] watcher: {} cannot be read (unplugged, ejected or renamed) — live updates \
                 are OFF until it comes back; the catalogue on screen is a snapshot",
                root.display()
            );
        }
    } else if now != Health::Unreachable {
        health.logged_unreachable.store(false, Ordering::SeqCst);
    }

    // Transitions ONLY. A consumer that hears nothing is entitled to believe nothing changed.
    if now != was {
        health.set_health(now);
        on_health(health.snapshot());
    }
}

/// Re-establish the OS watch on `root`.
///
/// `unwatch` FIRST, and its error deliberately ignored. Both halves matter, and both are read off
/// notify 6.1.1's macOS backend (`~/.cargo/registry/src/index.crates.io-*/notify-6.1.1/src/fsevent.rs`):
/// `watch()` → `append_path()` APPENDS to the FSEvents path array, so arming twice without removing
/// leaves a duplicate entry; and `unwatch()` → `remove_path()` answers `WatchNotFound` when the path
/// is no longer in that array, which is exactly the state a previous failed re-arm leaves behind.
/// `append_path` also requires the path to exist and canonicalize, so a `watch()` attempted while the
/// drive is still absent fails cleanly rather than pretending to have armed.
fn rearm(watcher: &Mutex<notify::RecommendedWatcher>, root: &Path) -> Result<(), String> {
    let mut w = watcher.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let _ = w.unwatch(root);
    w.watch(root, RecursiveMode::Recursive).map_err(|e| e.to_string())
}

fn reconciler_loop(
    rx: mpsc::Receiver<Msg>,
    root: String,
    writer: Arc<IndexWriter>,
    ctx: Arc<ReconcileCtx>,
    on_change: OnChange,
    health: Arc<HealthState>,
) {
    // Initial full reconcile (startup) — buffered startup events replay harmlessly after.
    let changes = reconcile_tree(&writer, &ctx, "").unwrap_or_default();
    emit(&on_change, &changes);

    let mut dirty = DirtySet::default();
    // The pause flag lives in the shared `HealthState`, not in a local, so the health monitor
    // respects the same pause this loop does (§4.9). `pause()`/`resume_with_full_rescan()` set it
    // BEFORE sending their control message, so the message's only remaining job here is to wake
    // this thread out of `recv_timeout` (and, on Resume, to queue the reconverging rescan).
    loop {
        let paused = health.paused.load(Ordering::SeqCst);
        let now = Instant::now();
        // When PAUSED, no flush can happen until Resume — so ignore the debounce deadlines (a
        // non-empty dirty set would make `timeout()` return ~0, and `recv_timeout(0)` would return
        // immediately, spinning the loop at 100% CPU). Block on the channel instead (§4.3 guard).
        let wait = if paused { IDLE_TIMEOUT } else { dirty.timeout(now) };
        match rx.recv_timeout(wait) {
            Ok(Msg::Event(Ok(ev))) => ingest(&mut dirty, &ev, &root, &ctx.config),
            Ok(Msg::Event(Err(_))) => dirty.mark_recursive(String::new(), Instant::now()), // notify error → rescan
            Ok(Msg::Control(Control::Rescan)) => dirty.mark_recursive(String::new(), Instant::now()),
            Ok(Msg::Control(Control::Pause)) => {} // the flag is already set; this only woke us
            Ok(Msg::Control(Control::Resume)) => {
                dirty.mark_recursive(String::new(), Instant::now());
            }
            Ok(Msg::Control(Control::Stop)) => break,
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        if !health.paused.load(Ordering::SeqCst) && dirty.ready(Instant::now()) {
            let (recs, pts) = dirty.drain();
            let changes = flush(&writer, &ctx, &root, recs, pts);
            emit(&on_change, &changes);
        }
    }
}

/// Interpret an event ONLY as "needs a rescan?" + "which paths" — never branch on create/modify/
/// remove flags (that decision is deferred to `stat`, §4.3). Excluded paths are dropped here.
fn ingest(dirty: &mut DirtySet, ev: &notify::Event, root: &str, config: &WalkConfig) {
    let now = Instant::now();
    if ev.need_rescan() {
        // MustScanSubDirs / dropped → rescan the shallowest reported dir (else the whole tree).
        let shallow = ev
            .paths
            .iter()
            .filter_map(|p| rel_of(p, root))
            .filter(|r| !is_watch_excluded(r, config))
            .min_by_key(|r| r.matches('/').count());
        dirty.mark_recursive(shallow.unwrap_or_default(), now);
        return;
    }
    for p in &ev.paths {
        if let Some(rel) = rel_of(p, root) {
            if rel.is_empty() || is_watch_excluded(&rel, config) {
                continue;
            }
            dirty.mark_point(rel, now);
        }
    }
}

fn flush(
    writer: &IndexWriter,
    ctx: &ReconcileCtx,
    root: &str,
    recs: Vec<String>,
    pts: Vec<String>,
) -> Vec<Change> {
    let mut changes = Vec::new();
    // A whole-tree rescan subsumes everything.
    if recs.iter().any(|r| r.is_empty()) {
        return reconcile_tree(writer, ctx, "").unwrap_or_default();
    }
    for r in &recs {
        changes.extend(reconcile_tree(writer, ctx, r).unwrap_or_default());
    }
    // Point paths not covered by a recursive entry → one batched reconcile_paths.
    let abs: Vec<PathBuf> = pts
        .iter()
        .filter(|p| !recs.iter().any(|r| is_descendant(p, r)))
        .map(|p| Path::new(root).join(p))
        .collect();
    if !abs.is_empty() {
        changes.extend(reconcile_paths(writer, ctx, &abs).unwrap_or_default());
    }
    changes
}

fn emit(on_change: &OnChange, changes: &[Change]) {
    if changes.is_empty() {
        return;
    }
    let dirs: Vec<String> = changes
        .iter()
        .map(|c| match c {
            Change::Upserted { key, .. } => key.clone(),
            Change::Deleted { key } => key.clone(),
        })
        .collect();
    on_change(dirs);
}

fn rel_of(p: &Path, root: &str) -> Option<String> {
    p.strip_prefix(root).ok().map(|r| r.to_string_lossy().replace('\\', "/"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reconcile::GenericMetaSource;

    #[test]
    fn is_watch_excluded_drops_index_churn_and_noise() {
        let cfg = WalkConfig::default();
        assert!(is_watch_excluded("_repo_index/INDEX.sqlite-wal", &cfg));
        assert!(is_watch_excluded(".repo_index/INDEX.json", &cfg));
        assert!(is_watch_excluded("a/._sidecar.csv", &cfg));
        assert!(is_watch_excluded("a/.DS_Store", &cfg));
        assert!(is_watch_excluded("node_modules/x/y.js", &cfg));
        assert!(!is_watch_excluded("a/b/real.csv", &cfg));
    }

    #[test]
    fn dirty_set_collapses_points_under_a_recursive_ancestor() {
        let now = Instant::now();
        let mut d = DirtySet::default();
        d.mark_point("a/b/c.csv".into(), now);
        d.mark_point("a/b/d.csv".into(), now);
        d.mark_recursive("a".into(), now); // subsumes the two points
        let (recs, pts) = d.drain();
        assert_eq!(recs, vec!["a".to_string()]);
        assert!(pts.is_empty(), "points under 'a' collapsed into the recursive rescan");
    }

    #[test]
    fn live_watcher_indexes_a_touched_file_within_two_seconds() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("_watcher_scratch");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("_repo_index")).unwrap();
        let writer = Arc::new(
            IndexWriter::open(dir.to_str().unwrap(), dir.join("_repo_index/INDEX.sqlite").to_str().unwrap())
                .unwrap(),
        );
        let ctx = Arc::new(ReconcileCtx::new(dir.to_str().unwrap(), Arc::new(GenericMetaSource)));
        let handle = WatcherHandle::spawn(
            dir.to_str().unwrap(),
            writer.clone(),
            ctx,
            Box::new(|_| {}),
            Box::new(|_| {}),
        )
        .unwrap();

        // give the watcher a beat to arm + finish the initial scan, then create a file
        std::thread::sleep(Duration::from_millis(200));
        std::fs::write(dir.join("live.csv"), b"hello").unwrap();

        // poll up to ~3s for the row to appear (debounce is ~300ms)
        let mut seen = false;
        for _ in 0..30 {
            std::thread::sleep(Duration::from_millis(100));
            let n: i64 = writer
                .with_conn(|c| {
                    c.query_row("SELECT COUNT(*) FROM entries WHERE path='live.csv'", [], |r| r.get(0))
                        .map_err(|e| e.to_string())
                })
                .unwrap();
            if n == 1 {
                seen = true;
                break;
            }
        }
        handle.stop();
        drop(writer);
        let _ = std::fs::remove_dir_all(&dir);
        assert!(seen, "the watcher must index a touched file within ~2s (⟳ automatic)");
    }

    // ── §4.9 health + re-arm ────────────────────────────────────────────────────────────────
    //
    // The drive is simulated with a directory we create and remove, NOT a real volume: the
    // property under test is "the root cannot be read → the OS watch is dead → re-arm it", and an
    // unreadable root is an unreadable root. These drive `health_tick` directly rather than
    // sleeping through poll intervals, so they are deterministic and cost milliseconds; the
    // monitor THREAD (poll timing, teardown) is covered by the last test.
    //
    // What a directory CANNOT reproduce: an unmount tears down the FSEvents stream under `notify`
    // while its own path bookkeeping still lists the root, whereas a deleted-and-recreated
    // directory leaves both intact. So these tests assert the DECISIONS (health, re-arm count,
    // rescan, teardown) and `rearm_refuses_an_absent_root…` asserts the primitive — proving the
    // stream is genuinely re-established needs the physical drive.

    /// A watcher armed on `dir`, plus the pieces `health_tick` needs, plus the receiving end of the
    /// control channel so a test can count the rescans the monitor pushes.
    fn health_rig(
        tag: &str,
    ) -> (
        PathBuf,
        Arc<Mutex<notify::RecommendedWatcher>>,
        Arc<HealthState>,
        Sender<Msg>,
        mpsc::Receiver<Msg>,
    ) {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("_watch_health_scratch_{tag}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let (tx, rx) = mpsc::channel::<Msg>();
        let ev_tx = tx.clone();
        let mut w = notify::recommended_watcher(move |res| {
            let _ = ev_tx.send(Msg::Event(res));
        })
        .unwrap();
        w.watch(&dir, RecursiveMode::Recursive).unwrap();
        (dir, Arc::new(Mutex::new(w)), Arc::new(HealthState::live()), tx, rx)
    }

    /// How many whole-tree rescans the monitor pushed (FSEvents traffic from the scratch dir shares
    /// the channel and is not a rescan).
    fn rescans(rx: &mpsc::Receiver<Msg>) -> usize {
        rx.try_iter().filter(|m| matches!(m, Msg::Control(Control::Rescan))).count()
    }

    /// A recorder for the transition callback — the thing the `watch-health` event is emitted from.
    fn recorder() -> (Arc<Mutex<Vec<HealthSnapshot>>>, OnHealth) {
        let seen: Arc<Mutex<Vec<HealthSnapshot>>> = Arc::new(Mutex::new(Vec::new()));
        let sink = seen.clone();
        (seen, Box::new(move |snap| sink.lock().unwrap().push(snap)))
    }

    #[test]
    fn a_root_that_disappears_flips_health_to_unreachable() {
        let (dir, w, health, tx, _rx) = health_rig("gone");
        let (seen, on_health) = recorder();

        health_tick(&dir, &w, &health, &tx, &on_health);
        assert_eq!(health.health(), Health::Live, "a readable root is live");
        assert!(seen.lock().unwrap().is_empty(), "no transition, no event");

        std::fs::remove_dir_all(&dir).unwrap(); // the drive goes away
        health_tick(&dir, &w, &health, &tx, &on_health);
        let snap = health.snapshot();
        assert_eq!(snap.health, Health::Unreachable);
        assert!(!snap.root_reachable);
        assert_eq!(seen.lock().unwrap().len(), 1, "exactly one transition event");

        // Still gone: the monitor ticks forever, but a non-transition emits nothing (and logs
        // nothing — the same flag guards both).
        health_tick(&dir, &w, &health, &tx, &on_health);
        assert_eq!(seen.lock().unwrap().len(), 1, "events are transitions, never a heartbeat");
    }

    #[test]
    fn a_root_that_comes_back_rearms_once_and_forces_one_rescan() {
        let (dir, w, health, tx, rx) = health_rig("back");
        let (seen, on_health) = recorder();

        std::fs::remove_dir_all(&dir).unwrap();
        health_tick(&dir, &w, &health, &tx, &on_health);
        assert_eq!(health.health(), Health::Unreachable);
        let _ = rescans(&rx); // drain the unplug's own FSEvents noise

        std::fs::create_dir_all(&dir).unwrap(); // the drive is plugged back in
        health_tick(&dir, &w, &health, &tx, &on_health);
        let snap = health.snapshot();
        assert_eq!(snap.health, Health::Live, "back to live only after the watch is re-armed");
        assert!(snap.root_reachable);
        assert_eq!(snap.rearms, 1, "the OS watch was re-established exactly once");
        assert_eq!(rescans(&rx), 1, "exactly one full rescan reconverges the catalogue");
        assert_eq!(seen.lock().unwrap().len(), 2, "unreachable, then live");

        // Steady state: no second re-arm, no repeat rescan, no repeat event.
        health_tick(&dir, &w, &health, &tx, &on_health);
        assert_eq!(health.snapshot().rearms, 1);
        assert_eq!(rescans(&rx), 0);
        assert_eq!(seen.lock().unwrap().len(), 2);

        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_paused_watcher_reports_paused_and_never_rescans() {
        let (dir, w, health, tx, rx) = health_rig("paused");
        let (seen, on_health) = recorder();
        health.paused.store(true, Ordering::SeqCst);

        health_tick(&dir, &w, &health, &tx, &on_health);
        assert_eq!(health.health(), Health::Paused, "the user parked live updates");

        std::fs::remove_dir_all(&dir).unwrap();
        health_tick(&dir, &w, &health, &tx, &on_health);
        assert_eq!(
            health.health(),
            Health::Unreachable,
            "a missing drive outranks a pause — pause is a mode the user chose, this is not"
        );

        std::fs::create_dir_all(&dir).unwrap();
        let _ = rescans(&rx);
        health_tick(&dir, &w, &health, &tx, &on_health);
        // The watch is re-armed even while paused (pause gates FLUSHING, not watching), but the
        // rescan is not pushed: `resume_with_full_rescan` already owes one.
        assert_eq!(health.snapshot().rearms, 1, "the watch is re-armed while paused");
        assert_eq!(health.health(), Health::Paused);
        assert_eq!(rescans(&rx), 0, "a paused watcher must not rescan");
        assert_eq!(seen.lock().unwrap().len(), 3, "paused → unreachable → paused");

        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The re-arm primitive itself, which is what the "stay unreachable and try again next tick"
    /// branch rests on: `watch()` must REFUSE an absent root (notify 6.1.1's `append_path` requires
    /// the path to exist and canonicalize) and succeed on a present one — so a re-arm attempted one
    /// tick too early can never report a live watch it does not have.
    #[test]
    fn rearm_refuses_an_absent_root_and_succeeds_on_a_present_one() {
        let (dir, w, _health, _tx, _rx) = health_rig("rearm");
        assert!(rearm(&w, &dir).is_ok(), "a present root re-arms");

        std::fs::remove_dir_all(&dir).unwrap();
        let err = rearm(&w, &dir).expect_err("an absent root must NOT report a live watch");
        assert!(!err.is_empty(), "the failure carries notify's reason: {err}");

        std::fs::create_dir_all(&dir).unwrap();
        assert!(rearm(&w, &dir).is_ok(), "and it re-arms again once the root is back");

        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn dropping_the_handle_stops_the_monitor_thread() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("_watcher_monitor_scratch");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("_repo_index")).unwrap();
        let writer = Arc::new(
            IndexWriter::open(
                dir.to_str().unwrap(),
                dir.join("_repo_index/INDEX.sqlite").to_str().unwrap(),
            )
            .unwrap(),
        );
        let ctx = Arc::new(ReconcileCtx::new(dir.to_str().unwrap(), Arc::new(GenericMetaSource)));

        let transitions = Arc::new(AtomicU32::new(0));
        let counter = transitions.clone();
        let handle = WatcherHandle::spawn_with_poll(
            dir.to_str().unwrap(),
            writer.clone(),
            ctx,
            Box::new(|_| {}),
            Box::new(move |_| {
                counter.fetch_add(1, Ordering::SeqCst);
            }),
            Duration::from_millis(20),
        )
        .unwrap();

        // The monitor is really running (many ticks at 20 ms) and finds nothing to report.
        std::thread::sleep(Duration::from_millis(200));
        assert_eq!(handle.health().health, Health::Live);
        assert_eq!(transitions.load(Ordering::SeqCst), 0, "a healthy root is silent");

        drop(handle); // joins BOTH threads — must not hang, must not panic
        drop(writer);

        // If the monitor thread had leaked, THIS would make it fire: the root vanishing is the one
        // thing it reports. Nothing may arrive after teardown.
        let _ = std::fs::remove_dir_all(&dir);
        std::thread::sleep(Duration::from_millis(200)); // 10 poll intervals
        assert_eq!(
            transitions.load(Ordering::SeqCst),
            0,
            "the monitor thread is gone: a root that vanishes after teardown emits nothing"
        );
    }
}
