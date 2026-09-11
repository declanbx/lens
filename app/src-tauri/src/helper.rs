//! `helper` — the Rust side of the stateless Python `extract-batch` bridge (PHASE_0_1_SPEC.md §6.8).
//! Rust hands N absolute paths on the child's stdin (NDJSON) and reads N JSON responses on stdout,
//! streaming BOTH concurrently on separate threads. Writing all stdin then reading all stdout
//! CERTAINLY deadlocks (a 512-path chunk of h5ad responses with `obs_columns` overflows the ~64 KB
//! stdout pipe buffer → child blocks on write, parent blocks on write → wedge).
//!
//! The helper supplies the digest-critical heavy fields (extractor, meta, error, n_obs/n_vars, §6.1);
//! Rust owns the cheap stat + ext/category/tags fields, so Rust does NOT pass `--path-fields`.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

use serde::Deserialize;
use serde_json::value::RawValue;

use crate::db::{PYTHON_BIN_DEFAULT, REPO_INDEX_PKG_PARENT};
use crate::reconcile::{Extracted, MetaSource};

/// Max paths per subprocess (§6.7): one failure kills at most one chunk, memory stays bounded.
const CHUNK: usize = 512;
/// Per-chunk wall-clock cap (§6.8): a wedged HDF5 read can't hang the reconciler forever.
const CHUNK_TIMEOUT: Duration = Duration::from_secs(120);

/// One `extract-batch` response line (§6.8). `meta` is captured as `RawValue` and stored verbatim
/// (never re-serialized, §4.7). Rust consumes only `v`/`input_path`/`extractor`/`meta`/`error`; it
/// computes ext/category/tags + n_obs/n_vars itself, so those fields are deserialized to DOCUMENT
/// the full wire contract but deliberately unread (hence the allow). `ext`/`category`/`tags` are
/// only present with `--path-fields`, which Rust does not pass — hence `#[serde(default)]`.
#[derive(Deserialize, Debug)]
#[allow(dead_code)]
pub struct ExtractResult {
    pub v: u32,
    pub input_path: Option<String>,
    pub extractor: String,
    pub meta: Box<RawValue>,
    pub error: Option<String>,
    pub n_obs: Option<i64>,
    pub n_vars: Option<i64>,
    #[serde(default)]
    pub ext: Option<String>,
    #[serde(default)]
    pub category: Option<String>,
    #[serde(default)]
    pub tags: Option<Vec<String>>,
}

/// Run `python -m repo_index extract-batch` over `paths` (absolute), returning one [`ExtractResult`]
/// per path IN INPUT ORDER via concurrent stdin-write / stdout-read / stderr-drain threads.
/// `cfg_flags` carries the resolved config gates (`--no-columns`, `--max-csv-bytes …`, §6.5). On ANY
/// non-zero/killed exit the WHOLE chunk is discarded (`Err`) — never partially consumed. Validates
/// `v == 1`. Correlation by the echoed `input_path` is the caller's job.
pub fn extract_batch(
    root: &str,
    cfg_flags: &[String],
    paths: &[String],
) -> Result<Vec<ExtractResult>, String> {
    let python = std::env::var("LENS_PYTHON").unwrap_or_else(|_| PYTHON_BIN_DEFAULT.to_string());
    run_extract(&python, root, cfg_flags, paths)
}

/// The `extract_batch` core with an injectable interpreter (tests point it at `/bin/false` to
/// exercise the non-zero-exit → discard-chunk path deterministically).
fn run_extract(
    python: &str,
    root: &str,
    cfg_flags: &[String],
    paths: &[String],
) -> Result<Vec<ExtractResult>, String> {
    if paths.is_empty() {
        return Ok(Vec::new());
    }
    let mut args = vec!["-m".into(), "repo_index".into(), "extract-batch".into(), "--root".into(), root.to_string()];
    args.extend(cfg_flags.iter().cloned());

    let mut child = Command::new(python)
        .args(&args)
        .current_dir(root)
        .env("PYTHONPATH", REPO_INDEX_PKG_PARENT)
        // exFAT-on-macOS lacks proper POSIX locking → HDF5 open would fail without this (§6.9, V-H1).
        .env("HDF5_USE_FILE_LOCKING", "FALSE")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("extract-batch: spawn `{python}` failed: {e} (set LENS_PYTHON)"))?;

    let mut stdin = child.stdin.take().ok_or("extract-batch: child stdin unavailable")?;
    let stdout = child.stdout.take().ok_or("extract-batch: child stdout unavailable")?;
    let stderr = child.stderr.take().ok_or("extract-batch: child stderr unavailable")?;

    // Writer thread: stream one `{"path": …}` line per input, NEVER a blank line (§6.8), then drop
    // stdin to send EOF. Runs concurrently with the stdout read to avoid the bounded-pipe wedge.
    let paths_owned = paths.to_vec();
    let writer = std::thread::spawn(move || {
        for p in &paths_owned {
            let line = serde_json::json!({ "path": p }).to_string();
            if writeln!(stdin, "{line}").is_err() {
                break; // child closed stdin early (crash) — the exit-status check reports it
            }
        }
        // stdin dropped here → EOF
    });

    // Stderr drain thread (diagnostics only).
    let errh = std::thread::spawn(move || {
        let mut s = String::new();
        let _ = BufReader::new(stderr).read_to_string(&mut s);
        s
    });

    // Reader thread → channel, so the main thread can bound the wait and kill a wedged child.
    let (tx, rx) = mpsc::channel::<Vec<String>>();
    let reader = std::thread::spawn(move || {
        let mut lines = Vec::new();
        for line in BufReader::new(stdout).lines() {
            match line {
                Ok(l) if !l.trim().is_empty() => lines.push(l),
                Ok(_) => {}
                Err(_) => break,
            }
        }
        let _ = tx.send(lines);
    });

    let lines = match rx.recv_timeout(CHUNK_TIMEOUT) {
        Ok(l) => l,
        Err(_) => {
            let _ = child.kill();
            let _ = child.wait();
            let _ = writer.join();
            let _ = reader.join();
            return Err(format!(
                "extract-batch: timed out after {}s (wedged HDF5?) — chunk discarded",
                CHUNK_TIMEOUT.as_secs()
            ));
        }
    };
    let _ = writer.join();
    let _ = reader.join();
    let status = child.wait().map_err(|e| format!("extract-batch: wait: {e}"))?;
    let stderr_txt = errh.join().unwrap_or_default();
    if !status.success() {
        // Whole chunk discarded — never partially consumed (§6.8).
        return Err(format!(
            "extract-batch: exited {status} — chunk discarded. stderr: {}",
            stderr_txt.trim()
        ));
    }

    let mut out = Vec::with_capacity(lines.len());
    for l in &lines {
        let r: ExtractResult =
            serde_json::from_str(l).map_err(|e| format!("extract-batch: bad response line: {e}"))?;
        if r.v != 1 {
            return Err(format!("extract-batch: wire version {} != 1 — chunk discarded", r.v));
        }
        out.push(r);
    }
    Ok(out)
}

/// A [`MetaSource`] backed by the Python `extract-batch` helper. Chunks the request (≤ [`CHUNK`]),
/// correlates responses by the echoed `input_path`, and DEGRADES a failed chunk to per-path generic
/// error records (so a wedged HDF5 errors just those files, never the whole reconcile).
pub struct PyMetaSource {
    pub cfg_flags: Vec<String>,
}

impl PyMetaSource {
    pub fn new(root: &str) -> Self {
        PyMetaSource { cfg_flags: read_config_flags(root) }
    }
}

impl MetaSource for PyMetaSource {
    fn extract(&self, root: &str, abs_paths: &[String]) -> Result<HashMap<String, Extracted>, String> {
        let mut out = HashMap::new();
        for chunk in abs_paths.chunks(CHUNK) {
            match extract_batch(root, &self.cfg_flags, chunk) {
                Ok(results) => {
                    let mut by_path: HashMap<&str, &ExtractResult> = HashMap::new();
                    for r in &results {
                        if let Some(ip) = r.input_path.as_deref() {
                            by_path.insert(ip, r);
                        }
                    }
                    for p in chunk {
                        let ex = match by_path.get(p.as_str()) {
                            Some(r) => Extracted {
                                extractor: r.extractor.clone(),
                                meta: r.meta.get().to_string(),
                                error: r.error.clone(),
                            },
                            None => Extracted {
                                extractor: "generic".into(),
                                meta: "{}".into(),
                                error: Some("extract-batch: no response for path".into()),
                            },
                        };
                        out.insert(p.clone(), ex);
                    }
                }
                Err(e) => {
                    // Degrade the chunk: each path gets a generic+error record so reconcile proceeds.
                    for p in chunk {
                        out.insert(
                            p.clone(),
                            Extracted { extractor: "generic".into(), meta: "{}".into(), error: Some(e.clone()) },
                        );
                    }
                }
            }
        }
        Ok(out)
    }
}

/// Best-effort resolution of the extract gates from the committed `INDEX.json`'s `config_used`
/// (§6.5): translate `index_columns` → `--columns`/`--no-columns` and the byte gates → the
/// `--max-csv-bytes`/`--max-json-bytes` flags, so delta rows match the baseline's column policy.
/// Returns empty (defaults) when `INDEX.json` is absent or `config_used` is missing/unreadable.
pub fn read_config_flags(root: &str) -> Vec<String> {
    let path = std::path::Path::new(root).join("_repo_index").join("INDEX.json");
    let Ok(text) = std::fs::read_to_string(&path) else {
        return Vec::new();
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) else {
        return Vec::new();
    };
    let Some(cfg) = v.get("config_used") else {
        return Vec::new();
    };
    let mut flags = Vec::new();
    if cfg.get("index_columns").and_then(|x| x.as_bool()) == Some(false) {
        flags.push("--no-columns".into());
    }
    // The csv byte gate: max of the csv/csvgz gates is a safe single `--max-csv-bytes`.
    let csv_gate = ["csv_rowcount_max_bytes", "csvgz_rowcount_max_bytes"]
        .iter()
        .filter_map(|k| cfg.get(*k).and_then(|x| x.as_i64()))
        .max();
    if let Some(n) = csv_gate {
        flags.push("--max-csv-bytes".into());
        flags.push(n.to_string());
    }
    if let Some(n) = cfg.get("json_parse_max_bytes").and_then(|x| x.as_i64()) {
        flags.push("--max-json-bytes".into());
        flags.push(n.to_string());
    }
    flags
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};

    fn scratch(tag: &str) -> PathBuf {
        let d = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("_helper_scratch_{tag}"));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// Skip these subprocess tests if the interpreter can't import repo_index (CI without the env).
    fn python_ok() -> bool {
        let python = std::env::var("LENS_PYTHON").unwrap_or_else(|_| PYTHON_BIN_DEFAULT.to_string());
        Command::new(&python)
            .args(["-c", "import repo_index"])
            .env("PYTHONPATH", REPO_INDEX_PKG_PARENT)
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    #[test]
    fn extract_batch_large_is_deadlock_free_and_ordered() {
        if !python_ok() {
            eprintln!("SKIP: python cannot import repo_index");
            return;
        }
        let dir = scratch("big");
        // 200 CSVs with a wide header → combined stdout comfortably exceeds the ~64 KB pipe buffer.
        let wide_header: String =
            (0..40).map(|i| format!("column_number_{i:03}")).collect::<Vec<_>>().join(",");
        let mut paths = Vec::new();
        for i in 0..200 {
            let p = dir.join(format!("f{i:03}.csv"));
            std::fs::write(&p, format!("{wide_header}\n1,2,3\n")).unwrap();
            paths.push(p.to_string_lossy().into_owned());
        }
        let results = extract_batch(dir.to_str().unwrap(), &[], &paths).unwrap();
        assert_eq!(results.len(), 200, "one response per input — no deadlock, no drops");
        // correlation: every input has a matching echoed input_path
        let echoed: std::collections::HashSet<_> =
            results.iter().filter_map(|r| r.input_path.clone()).collect();
        for p in &paths {
            assert!(echoed.contains(p), "missing response for {p}");
        }
        assert!(results.iter().all(|r| r.v == 1));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn extract_batch_missing_path_is_in_band_error_not_a_crash() {
        if !python_ok() {
            eprintln!("SKIP: python cannot import repo_index");
            return;
        }
        let dir = scratch("missing");
        let ghost = dir.join("does_not_exist.h5ad").to_string_lossy().into_owned();
        // per-file failure is in-band (exit 0), not a chunk discard
        let results = extract_batch(dir.to_str().unwrap(), &[], &[ghost.clone()]).unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].input_path.as_deref(), Some(ghost.as_str()));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn extract_batch_nonzero_exit_discards_whole_chunk() {
        // `/bin/false` exits 1 without producing stdout — a deterministic stand-in for a helper that
        // failed to start / was killed. The chunk must be discarded as Err, never partially consumed.
        let dir = scratch("nonzero");
        let p = dir.join("a.csv").to_string_lossy().into_owned();
        std::fs::write(dir.join("a.csv"), "x\n1\n").unwrap();
        let r = run_extract("/bin/false", dir.to_str().unwrap(), &[], &[p]);
        assert!(r.is_err(), "non-zero exit must discard the chunk (Err), not partially consume it");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn py_meta_source_returns_generic_for_plain_file() {
        if !python_ok() {
            eprintln!("SKIP: python cannot import repo_index");
            return;
        }
        let dir = scratch("pms");
        std::fs::write(dir.join("note.txt"), "hello").unwrap();
        let abs = dir.join("note.txt").to_string_lossy().into_owned();
        let src = PyMetaSource { cfg_flags: vec![] };
        let map = src.extract(dir.to_str().unwrap(), &[abs.clone()]).unwrap();
        assert!(map.contains_key(&abs));
        assert!(map[&abs].error.is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
