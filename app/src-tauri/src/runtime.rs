//! `runtime` — the two things Lens has to LOCATE on whatever Mac it happens to be running on: the
//! Python crawler package (`repo_index`) and an interpreter able to run it.
//!
//! Both used to be `const` absolute paths in `db.rs` (`REPO_INDEX_PKG_PARENT` pointing into a
//! specific external volume, `PYTHON_BIN_DEFAULT` pointing into one user's anaconda install). On the
//! machine they were written for they worked; on any other Mac they are the reason "Add folder…"
//! fails the moment it tries to index — the window opens, the picker works, and then the indexer
//! cannot be spawned. This module is what replaces them.
//!
//! Two resolutions, same shape (env override → a real lookup → a fallback), both CACHED because
//! they are consulted from the live reconciler's hot path:
//!
//!   * the crawler — `$LENS_REPO_INDEX` → the bundled resource (`$RESOURCE/repo_index` inside the
//!     `.app`) → a dev fallback that walks up from this crate to the in-repo `indexer/`. What the
//!     spawn sites actually want is the PARENT directory, which is what goes on `PYTHONPATH` so
//!     `-m repo_index` resolves without the package being pip-installed in the chosen interpreter.
//!   * the interpreter — `$LENS_PYTHON` → a saved choice in `settings.json` → a list of the places
//!     a Mac actually keeps python3 → a login shell's `PATH`. **Every candidate is verified by
//!     RUNNING it**, never by existence: a path that exists and is not a ≥3.9 interpreter is worse
//!     than no path at all, because the failure then surfaces as an opaque spawn error much later.
//!
//! A login shell is the last resort rather than the first because it costs a full shell startup,
//! but it must be in the list: a `.app` launched from Finder inherits **no** shell `PATH` at all
//! (`launchd`'s environment, not the user's), so a bare `python3` can never be used here — which is
//! precisely why the original constant was absolute.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Mutex, OnceLock};

// ───────────────────────────────────────────────────────────────────────────────────────────
// Process-wide caches. `.setup()` fills the two `AppHandle`-derived ones; everything else
// resolves lazily on first use, so `cargo test` (which has no Tauri app at all) still works.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// `$RESOURCE/repo_index` as Tauri resolved it at startup — `None` when the app is not bundled
/// (or the resource is missing). Set ONCE from [`init`].
static RESOURCE_REPO_INDEX: OnceLock<Option<PathBuf>> = OnceLock::new();

/// The app config dir (`~/Library/Application Support/com.declan.lens`), home of `settings.json`.
/// Set ONCE from [`init`]; `None` in tests and if Tauri cannot compute it.
static CONFIG_DIR: OnceLock<Option<PathBuf>> = OnceLock::new();

/// The resolved `PYTHONPATH` value: the PARENT of the `repo_index` package directory.
static PKG_PARENT: OnceLock<Option<PathBuf>> = OnceLock::new();

/// The resolved interpreter. A `OnceLock<Mutex<…>>` rather than a plain `OnceLock` because
/// `set_python_path` must be able to REPLACE the cached choice after the user picks one in
/// Settings — the once-ness that matters is "probe the candidate list at most once", and that is
/// what the `Option` inside the mutex gives.
static PYTHON: OnceLock<Mutex<Option<Python>>> = OnceLock::new();

fn python_cell() -> &'static Mutex<Option<Python>> {
    PYTHON.get_or_init(|| Mutex::new(None))
}

/// What `python_status()` reports. `source` names WHICH rule won, so a user who set `LENS_PYTHON`
/// and still sees the wrong interpreter can tell at a glance that their variable was not the thing
/// that answered. `error` is `None` on the normal path and carries the reason a chosen interpreter
/// was rejected (an extra field; a consumer reading only `{path, version, source}` is unaffected).
#[derive(Clone, serde::Serialize)]
pub struct PythonStatus {
    pub path: Option<String>,
    pub version: Option<String>,
    pub source: String,
    pub error: Option<String>,
}

#[derive(Clone)]
struct Python {
    path: Option<String>,
    version: Option<String>,
    /// `"env" | "settings" | "probe" | "shell" | "none"` — see [`PythonStatus::source`].
    source: &'static str,
    error: Option<String>,
}

impl Python {
    fn found(path: &str, version: String, source: &'static str) -> Python {
        Python {
            path: Some(path.to_string()),
            version: Some(version),
            source,
            error: None,
        }
    }
    fn none() -> Python {
        Python { path: None, version: None, source: "none", error: None }
    }
    fn status(&self) -> PythonStatus {
        PythonStatus {
            path: self.path.clone(),
            version: self.version.clone(),
            source: self.source.to_string(),
            error: self.error.clone(),
        }
    }
}

/// The message a NON-PROGRAMMER has to be able to act on. It is the only thing between them and an
/// app that silently does nothing, so it says what is missing, what it is for, and both ways out.
pub const NO_PYTHON_MSG: &str = "Lens needs Python 3.9 or newer to read your folder. None was \
                                 found. Install it from python.org, or choose one in Settings.";

// ───────────────────────────────────────────────────────────────────────────────────────────
// Startup hook
// ───────────────────────────────────────────────────────────────────────────────────────────

/// Capture everything that needs an `AppHandle`, ONCE, from `.setup()`.
///
/// This exists because the three production spawn sites (`db::run_reindex_streamed`,
/// `db::run_manifest_streamed`, `helper::run_extract`) take no `AppHandle` — the live reconciler's
/// helper is called from a watcher thread that has never seen one. Rather than thread a handle
/// through three call chains, resolve once here and cache.
///
/// Infallible by construction: a missing resource is a logged warning, not a startup failure.
/// `.setup()` returning `Err` is not a graceful failure in Tauri — it panics inside
/// `did_finish_launching`, a non-unwinding ObjC boundary, so it becomes `abort()` with no window.
pub fn init(app: &tauri::AppHandle) {
    use tauri::Manager;

    let resolved = app
        .path()
        .resolve("repo_index", tauri::path::BaseDirectory::Resource)
        .ok()
        .filter(|p| p.is_dir());
    if resolved.is_none() {
        eprintln!(
            "[lens] runtime: no bundled `repo_index` resource — falling back to \
             $LENS_REPO_INDEX or the in-repo indexer/ (dev build)"
        );
    }
    let _ = RESOURCE_REPO_INDEX.set(resolved);
    let _ = CONFIG_DIR.set(app.path().app_config_dir().ok());
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// The crawler package
// ───────────────────────────────────────────────────────────────────────────────────────────

/// The `PYTHONPATH` every indexer spawn exports — the PARENT of the `repo_index` package dir, so
/// `python -m repo_index` resolves whether or not the package is installed in that interpreter.
/// `None` means the crawler could not be located at all; the spawn sites surface that as an error
/// naming the override rather than spawning with a silently-wrong `PYTHONPATH` (which Python
/// tolerates, producing the much worse `No module named repo_index` three frames away).
pub fn pythonpath() -> Option<String> {
    pkg_parent().map(|p| p.to_string_lossy().into_owned())
}

/// [`pythonpath`], or the message to show when the crawler is nowhere to be found. The three spawn
/// sites use this rather than exporting a `PYTHONPATH` that resolves nothing: Python tolerates a
/// bogus entry silently, so the failure would otherwise surface as `No module named repo_index`
/// three frames away with nothing pointing at the real cause.
pub fn pythonpath_required() -> Result<String, String> {
    pythonpath().ok_or_else(|| {
        "Lens cannot find its own indexing code (the bundled `repo_index` folder). \
         Reinstalling the app should fix it; a developer can set LENS_REPO_INDEX to that folder."
            .to_string()
    })
}

fn pkg_parent() -> Option<PathBuf> {
    PKG_PARENT.get_or_init(resolve_pkg_parent).clone()
}

fn resolve_pkg_parent() -> Option<PathBuf> {
    // 1. The explicit override, mirroring LENS_PYTHON. Accepts EITHER spelling (see `pkg_parent_of`).
    if let Some(v) = std::env::var("LENS_REPO_INDEX").ok().filter(|s| !s.trim().is_empty()) {
        match pkg_parent_of(Path::new(v.trim())) {
            Some(p) => return Some(p),
            None => eprintln!(
                "[lens] runtime: LENS_REPO_INDEX=\"{v}\" holds no `repo_index` package \
                 (expected that directory, or its parent) — ignoring it"
            ),
        }
    }
    // 2. The bundled copy inside the .app.
    if let Some(Some(dir)) = RESOURCE_REPO_INDEX.get() {
        if let Some(p) = pkg_parent_of(dir) {
            return Some(p);
        }
    }
    // 3. Dev fallback. `resource_dir()` resolves to the cargo target dir under `cargo run`, which
    //    holds no resources, so without this a dev build has no crawler at all.
    dev_indexer_dir()
}

/// Accept either spelling of a `repo_index` location: the package directory itself
/// (`…/indexer/repo_index`) or the directory CONTAINING it (`…/indexer`). Both name the same thing
/// to a human and the distinction — only the parent goes on `PYTHONPATH` — is exactly the kind of
/// detail a user setting an env var gets wrong once and then debugs for an hour. Returns the parent.
fn pkg_parent_of(candidate: &Path) -> Option<PathBuf> {
    if candidate.file_name().is_some_and(|n| n == "repo_index")
        && candidate.join("__init__.py").is_file()
    {
        return candidate.parent().map(|p| p.to_path_buf());
    }
    if candidate.join("repo_index").join("__init__.py").is_file() {
        return Some(candidate.to_path_buf());
    }
    None
}

/// Walk up from this crate's source dir looking for the in-repo `indexer/repo_index`. Baked in at
/// COMPILE time, so in a shipped `.app` it simply names a directory that does not exist on the
/// user's machine and every probe below fails closed.
fn dev_indexer_dir() -> Option<PathBuf> {
    let mut dir = Some(Path::new(env!("CARGO_MANIFEST_DIR")));
    while let Some(d) = dir {
        let cand = d.join("indexer");
        if cand.join("repo_index").join("__init__.py").is_file() {
            return Some(cand);
        }
        dir = d.parent();
    }
    None
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// The interpreter
// ───────────────────────────────────────────────────────────────────────────────────────────

/// The Python program run by `-c` to accept a candidate. Two jobs in one subprocess: prove the
/// binary IS a Python ≥ 3.9 (the crawler's `requires-python`), and report the version for display.
const PROBE_SRC: &str =
    "import sys; assert sys.version_info[:2] >= (3, 9); print('%d.%d.%d' % sys.version_info[:3])";

/// The absolute interpreter to spawn, or a message written for someone who has never heard the
/// word "interpreter". Resolved (and cached) on first call.
pub fn python_bin() -> Result<String, String> {
    let mut slot = python_cell().lock().map_err(|e| format!("python cache poisoned: {e}"))?;
    if slot.is_none() {
        *slot = Some(resolve_python());
    }
    slot.as_ref()
        .and_then(|p| p.path.clone())
        .ok_or_else(|| NO_PYTHON_MSG.to_string())
}

/// What `python_status()` answers — resolves on first call exactly like [`python_bin`].
pub fn status() -> PythonStatus {
    match python_cell().lock() {
        Ok(mut slot) => {
            if slot.is_none() {
                *slot = Some(resolve_python());
            }
            slot.as_ref().map(|p| p.status()).unwrap_or_else(|| Python::none().status())
        }
        Err(e) => PythonStatus {
            path: None,
            version: None,
            source: "none".into(),
            error: Some(format!("python cache poisoned: {e}")),
        },
    }
}

/// Validate `path`, persist it to `settings.json`, and re-cache it as the winner.
///
/// A rejected path changes NOTHING — not the file, not the cache — and comes back as an `Err`
/// whose text is a sentence the picker can show verbatim. Two reasons it is an error rather than a
/// status with a flag: picking the wrong file in a file dialog is an ordinary mistake that needs an
/// answer, and a "saved" reply that silently kept the old interpreter is the worst of both.
///
/// The one case that still SUCCEEDS is an interpreter that works but could not be written down:
/// it is used for this session and says so in `error`.
pub fn set_python_path(path: &str) -> Result<PythonStatus, String> {
    let path = path.trim();
    let version = probe(path).map_err(|e| {
        format!("\"{path}\" is not a Python 3.9 or newer program ({e}). Look for one called \"python3\".")
    })?;
    let mut chosen = Python::found(path, version, "settings");
    if let Err(e) = save_python_bin(path) {
        // The interpreter still works for THIS session; only the memory of it failed.
        eprintln!("[lens] runtime: could not save the Python choice: {e}");
        chosen.error = Some(format!("chosen for this session only — could not save it: {e}"));
    }
    if let Ok(mut slot) = python_cell().lock() {
        *slot = Some(chosen.clone());
    }
    Ok(chosen.status())
}

fn resolve_python() -> Python {
    // 1. The documented escape hatch, first — it is the one a user was TOLD to set by an error
    //    message, so anything else winning silently would be a lie. A broken value is reported
    //    loudly and then stepped over: an unusable override must not leave the app with no Python.
    if let Some(v) = std::env::var("LENS_PYTHON").ok().filter(|s| !s.trim().is_empty()) {
        let v = v.trim().to_string();
        match probe(&v) {
            Ok(version) => return Python::found(&v, version, "env"),
            Err(e) => eprintln!(
                "[lens] runtime: LENS_PYTHON=\"{v}\" is not a usable Python 3.9+ ({e}) — \
                 looking for another one"
            ),
        }
    }
    // 2. The user's saved choice from the Settings picker.
    if let Some(saved) = saved_python_bin() {
        match probe(&saved) {
            Ok(version) => return Python::found(&saved, version, "settings"),
            Err(e) => eprintln!(
                "[lens] runtime: the saved Python \"{saved}\" no longer works ({e}) — \
                 looking for another one"
            ),
        }
    }
    // 3. Where a Mac actually keeps python3, in the order a scientific Mac tends to have them.
    for cand in candidates() {
        if let Ok(version) = probe(&cand) {
            return Python::found(&cand, version, "probe");
        }
    }
    // 4. A LOGIN shell does have the user's PATH (the app process does not) — so this finds a
    //    pyenv/asdf/uv shim that no absolute-path list could enumerate. Last because it pays a
    //    full shell startup, including the user's rc files.
    if let Some(p) = shell_python3() {
        if let Ok(version) = probe(&p) {
            return Python::found(&p, version, "shell");
        }
    }
    eprintln!("[lens] runtime: no usable Python 3.9+ found — indexing is unavailable until one is chosen");
    Python::none()
}

/// The fixed candidate list, `$HOME` expanded. Homebrew first (Apple Silicon, then Intel), then the
/// conda family, then a python.org framework install, and `/usr/bin/python3` LAST: on a Mac without
/// the Xcode command line tools that path is a stub that fails and offers to install them, so it
/// must never be reached while a real interpreter exists.
fn candidates() -> Vec<String> {
    let home = std::env::var("HOME").unwrap_or_default();
    let mut v = vec![
        "/opt/homebrew/bin/python3".to_string(),
        "/usr/local/bin/python3".to_string(),
    ];
    if !home.is_empty() {
        v.push(format!("{home}/anaconda3/bin/python3"));
        v.push(format!("{home}/miniconda3/bin/python3"));
        v.push(format!("{home}/miniforge3/bin/python3"));
    }
    v.push("/Library/Frameworks/Python.framework/Versions/Current/bin/python3".to_string());
    v.push("/usr/bin/python3".to_string());
    v
}

/// Run a candidate and require it to answer as a Python ≥ 3.9. Existence is NOT the test — the
/// whole point of resolving at runtime is that the thing at the path may be anything at all.
fn probe(path: &str) -> Result<String, String> {
    if path.is_empty() {
        return Err("no path given".into());
    }
    if !Path::new(path).is_file() {
        return Err("no such file".into());
    }
    let out = Command::new(path)
        .args(["-c", PROBE_SRC])
        .output()
        .map_err(|e| format!("could not run it: {e}"))?;
    if !out.status.success() {
        let tail = String::from_utf8_lossy(&out.stderr);
        let tail = tail.lines().last().unwrap_or("").trim();
        return Err(if tail.is_empty() {
            format!("exited with {}", out.status)
        } else {
            tail.to_string()
        });
    }
    let ver = String::from_utf8_lossy(&out.stdout).trim().to_string();
    Ok(if ver.is_empty() { "unknown".into() } else { ver })
}

/// Ask a LOGIN shell where `python3` is. `-l` is what makes this different from anything the app
/// process can see for itself: it sources the user's profile, so pyenv/asdf/uv shims appear.
fn shell_python3() -> Option<String> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".to_string());
    let out = Command::new(&shell).args(["-lc", "command -v python3"]).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let p = String::from_utf8_lossy(&out.stdout).lines().next()?.trim().to_string();
    // A shell function or alias resolves to something that is not a path — only take an absolute one.
    p.starts_with('/').then_some(p)
}

// ── settings.json ───────────────────────────────────────────────────────────────────────────

fn settings_path() -> Option<PathBuf> {
    CONFIG_DIR.get()?.clone().map(|d| d.join("settings.json"))
}

fn saved_python_bin() -> Option<String> {
    let text = std::fs::read_to_string(settings_path()?).ok()?;
    let doc: serde_json::Value = serde_json::from_str(&text).ok()?;
    doc.get("python_bin")?.as_str().map(str::to_string).filter(|s| !s.is_empty())
}

/// Write `python_bin` into `settings.json`, PRESERVING any other key already there — the file is
/// the app's settings file, not this one setting's private store.
fn save_python_bin(path: &str) -> Result<(), String> {
    let file = settings_path().ok_or("the app settings folder is unavailable")?;
    if let Some(parent) = file.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("could not create {}: {e}", parent.display()))?;
    }
    let mut doc = std::fs::read_to_string(&file)
        .ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .filter(|v| v.is_object())
        .unwrap_or_else(|| serde_json::json!({}));
    doc["python_bin"] = serde_json::Value::String(path.to_string());
    let text =
        serde_json::to_string_pretty(&doc).map_err(|e| format!("could not serialize: {e}"))?;
    std::fs::write(&file, text).map_err(|e| format!("could not write {}: {e}", file.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(tag: &str) -> PathBuf {
        let d = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("_runtime_scratch_{tag}"));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// The replacement for the deleted `REPO_INDEX_PKG_PARENT` constant test: what is asserted now
    /// is the RESOLUTION (a `repo_index` package dir yields its PARENT as the PYTHONPATH), not a
    /// hardcoded location that only existed on one machine.
    #[test]
    fn pkg_parent_takes_the_parent_of_the_package_dir() {
        let dir = scratch("pkg_parent");
        let pkg = dir.join("indexer").join("repo_index");
        std::fs::create_dir_all(&pkg).unwrap();
        std::fs::write(pkg.join("__init__.py"), b"").unwrap();

        // Pointed AT the package → its parent.
        assert_eq!(pkg_parent_of(&pkg).unwrap(), dir.join("indexer"));
        // Pointed at the parent → itself. Both spellings are accepted on purpose.
        assert_eq!(pkg_parent_of(&dir.join("indexer")).unwrap(), dir.join("indexer"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A directory that merely has the right NAME is not a package — `__init__.py` is the test, so
    /// a typo'd override fails closed instead of exporting a PYTHONPATH that resolves nothing.
    #[test]
    fn pkg_parent_rejects_a_directory_without_the_package() {
        let dir = scratch("pkg_parent_bad");
        let fake = dir.join("repo_index");
        std::fs::create_dir_all(&fake).unwrap();
        assert!(pkg_parent_of(&fake).is_none());
        assert!(pkg_parent_of(&dir).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The dev fallback must find the in-repo crawler from this crate's own location — that is what
    /// keeps `cargo run` (whose resource dir is the cargo target dir) and the test suite working.
    #[test]
    fn dev_fallback_finds_the_in_repo_indexer() {
        let found = dev_indexer_dir().expect("indexer/ should be findable from this crate");
        assert!(found.join("repo_index").join("__init__.py").is_file());
        assert_eq!(found.file_name().unwrap(), "indexer");
    }

    /// The interpreter is accepted by RUNNING it, never by existence — a real file that is not a
    /// Python must be rejected, or the failure resurfaces as an opaque spawn error much later.
    #[test]
    fn probe_rejects_a_non_python_and_a_missing_file() {
        assert!(probe("/bin/ls").is_err(), "/bin/ls is a real file and not an interpreter");
        assert!(probe("/definitely/not/here/python3").is_err());
        assert!(probe("").is_err());
    }

    /// …and accepts a real one, reporting its version. Skips when this machine has none on the
    /// candidate list (the same skip convention the subprocess tests in `helper.rs` use).
    #[test]
    fn probe_accepts_a_real_python_and_reports_its_version() {
        let Some(found) = candidates().into_iter().find(|c| probe(c).is_ok()) else {
            eprintln!("SKIP: no python3 on the candidate list");
            return;
        };
        let ver = probe(&found).unwrap();
        let major: u32 = ver.split('.').next().unwrap().parse().unwrap();
        assert!(major >= 3, "probe returned {ver}");
    }

    /// `$HOME` is expanded into the candidate list (the conda entries are worthless otherwise), and
    /// the Xcode-stub `/usr/bin/python3` stays LAST so it is only reached as a true last resort.
    #[test]
    fn candidate_list_is_expanded_and_ordered() {
        let c = candidates();
        assert_eq!(c.first().unwrap(), "/opt/homebrew/bin/python3");
        assert_eq!(c.last().unwrap(), "/usr/bin/python3");
        assert!(!c.iter().any(|p| p.contains("$HOME") || p.starts_with('~')));
    }
}
