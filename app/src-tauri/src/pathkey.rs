//! `pathkey` — the SINGLE normalized-key function and path primitives (PHASE_0_1_SPEC.md §1.1,
//! §2.3, §0.5B/§0.5D). Rust is the **sole** key computer, so there is exactly one definition of
//! identity in the whole system and no cross-language parity surface.
//!
//! `norm_key(s) = simple_casefold(NFC(s))` — NFC normalization followed by Unicode **simple**
//! (per-code-point) case fold. This is the exact behavior of the exFAT up-case table on the mount
//! this app targets, verified by the on-mount oracle test below:
//!   * `Ä ≡ ä`  collapse   (so plain ASCII-lowercasing is INSUFFICIENT), yet
//!   * `ß ≢ ss` and `İ ≢ i` stay DISTINCT (so *full* casefold OVER-merges).
//! It is therefore NOT `to_ascii_lowercase`, NOT `to_lowercase`, and NOT full `str::casefold`.
//!
//! `path` (the raw, human-readable NFC-of-readdir spelling) is NEVER overwritten by a key: the
//! normalized output lands only in the separate `path_key`/`parent_key`/`name_key` columns, used
//! solely for identity / ordering / joins. Path is identity (never `st_ino` — exFAT synthesizes
//! inode numbers with no cross-remount stability, §0.5G).

use unicode_casefold::{Locale, UnicodeCaseFold, Variant};
use unicode_normalization::UnicodeNormalization;

/// Stamped into `schema_meta.norm_version`; a reader whose code `NORM_VERSION` disagrees with the
/// on-disk value forces a cold rebuild rather than trusting stale keys (§1.1). Phase 0–1 ships 1 =
/// NFC + Unicode simple case fold. Bumping the fold later means bumping this and rebuilding — never
/// reconciling two implementations (there is only one).
pub const NORM_VERSION: i64 = 1;

/// The one normalizer. `norm_key(s) = simple_casefold(NFC(s))` (§1.1). Applied verbatim to compute
/// `path_key`/`parent_key`/`name_key`, the reconciler's UPSERT conflict target, and deferral-token
/// matching — all the SAME function, so no call site can diverge on a code point.
pub fn norm_key(s: &str) -> String {
    // NFC first, then simple (1:1) case fold. Simple fold keeps composed characters composed, so
    // the NFC form is preserved; we follow the spec formula exactly (no re-normalization after).
    s.nfc()
        .case_fold_with(Variant::Simple, Locale::NonTurkic)
        .collect()
}

/// A normalized identity key (the output of [`norm_key`]). Wrapping it in a newtype keeps the
/// "raw path vs identity key" distinction in the type system: you cannot accidentally bind a raw
/// path where SQL expects a `path_key`, or vice versa.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct NormPath(String);

impl NormPath {
    /// Compute the identity key of a RAW (root-relative, POSIX) path.
    pub fn of(raw: &str) -> Self {
        NormPath(norm_key(raw))
    }

    /// Wrap an already-normalized key string (e.g. read from the `path_key` column). The caller
    /// asserts it is already `norm_key` output; no re-normalization is performed.
    pub fn from_key(key: String) -> Self {
        NormPath(key)
    }

    /// The parent identity edge: `norm_key(dirname(raw))` — the authoritative `parent_key`.
    pub fn parent_of(raw: &str) -> Self {
        NormPath(norm_key(parent_of(raw)))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn into_string(self) -> String {
        self.0
    }

    /// `true` iff `self` is `prefix` itself or a descendant of it, using the `/`-byte boundary so
    /// `foo/bar` is under `foo` but `foobar` is NOT (subtree-prefix matching, §0.5G / §5.5). The
    /// empty prefix (the repo root) matches everything.
    pub fn is_under(&self, prefix: &NormPath) -> bool {
        let (s, p) = (self.0.as_str(), prefix.0.as_str());
        if p.is_empty() {
            return true;
        }
        s == p || (s.len() > p.len() && s.starts_with(p) && s.as_bytes()[p.len()] == b'/')
    }
}

impl std::fmt::Display for NormPath {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

// ── raw-path primitives (root-relative POSIX: no leading `/`, no trailing `/`) ──────────────────
// These operate on the RAW path string; callers apply `norm_key` to the result when an identity
// key is needed. They mirror the §2.5 reference algorithm exactly.

/// `parent_of("a/b/c") == "a/b"`, `parent_of("readme.md") == ""` (top level).
pub fn parent_of(path: &str) -> &str {
    match path.rfind('/') {
        Some(i) => &path[..i],
        None => "",
    }
}

/// `basename_of("a/b/c") == "c"`, `basename_of("readme.md") == "readme.md"`.
pub fn basename_of(path: &str) -> &str {
    match path.rfind('/') {
        Some(i) => &path[i + 1..],
        None => path,
    }
}

/// `depth_of("readme.md") == 0`, `depth_of("a/b.h5ad") == 1` — the count of `/` separators.
pub fn depth_of(path: &str) -> i64 {
    path.matches('/').count() as i64
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn simple_fold_collapses_case_and_nfc_but_not_ss_or_dotted_i() {
        // exFAT-correct SIMPLE fold contract (§0.5B):
        // collapse case
        assert_eq!(norm_key("Foo.CSV"), norm_key("foo.csv"));
        assert_eq!(norm_key("A/B/C.h5ad"), norm_key("a/b/c.h5ad"));
        // collapse the specific accented pair the FS collapses
        assert_eq!(norm_key("Ä"), norm_key("ä"));
        assert_eq!(norm_key("\u{00C4}"), norm_key("\u{00E4}"));
        // collapse NFC vs NFD (composed é vs e + combining acute)
        assert_eq!(norm_key("caf\u{00e9}"), norm_key("cafe\u{0301}"));
        // but do NOT over-merge — these stay DISTINCT under simple fold:
        assert_ne!(norm_key("\u{00df}"), norm_key("ss")); // ß ≠ ss
        assert_ne!(norm_key("\u{0130}"), norm_key("i")); //  İ ≠ i
        assert_ne!(norm_key("\u{fb03}"), norm_key("ffi")); // ﬃ ≠ ffi (ligature not decomposed)
    }

    #[test]
    fn simple_fold_is_idempotent_and_nfc_stable() {
        for s in ["Ä", "café", "STRASSE", "readme.MD", "a/PopV_v4/Atlas.H5AD"] {
            let k = norm_key(s);
            assert_eq!(norm_key(&k), k, "norm_key must be idempotent for {s:?}");
        }
    }

    #[test]
    fn path_primitives() {
        assert_eq!(parent_of("a/b/c.txt"), "a/b");
        assert_eq!(parent_of("readme.md"), "");
        assert_eq!(basename_of("a/b/c.txt"), "c.txt");
        assert_eq!(basename_of("readme.md"), "readme.md");
        assert_eq!(depth_of("readme.md"), 0);
        assert_eq!(depth_of("a/b.h5ad"), 1);
        assert_eq!(depth_of("a/b/c/d.txt"), 3);
    }

    #[test]
    fn is_under_uses_slash_boundary() {
        let foo = NormPath::of("foo");
        assert!(NormPath::of("foo").is_under(&foo));
        assert!(NormPath::of("foo/bar").is_under(&foo));
        assert!(NormPath::of("FOO/bar/baz").is_under(&foo)); // case-folded prefix still matches
        assert!(!NormPath::of("foobar").is_under(&foo)); // NOT a slash-boundary descendant
        assert!(!NormPath::of("fo").is_under(&foo));
        // the empty (root) prefix matches everything
        let root = NormPath::from_key(String::new());
        assert!(NormPath::of("anything/at/all").is_under(&root));
    }

    // ── the on-mount ORACLE test (§8 gate 3) — the STRONGEST key regression ─────────────────────
    // Create both members of a case/NFC/fold pair on the REAL filesystem and assert
    // norm_key-equality IFF the filesystem collapses them. Self-validating: it checks that our fold
    // exactly matches whatever mount it runs on, provided that mount folds like exFAT. It is gated
    // to exFAT (APFS folds differently) so it never false-fails on a non-exFAT checkout.

    #[cfg(target_os = "macos")]
    fn fstypename(path: &Path) -> Option<String> {
        use std::os::unix::ffi::OsStrExt;
        let cpath = std::ffi::CString::new(path.as_os_str().as_bytes()).ok()?;
        unsafe {
            let mut sfs: libc::statfs = std::mem::zeroed();
            if libc::statfs(cpath.as_ptr(), &mut sfs) != 0 {
                return None;
            }
            let cstr = std::ffi::CStr::from_ptr(sfs.f_fstypename.as_ptr());
            Some(cstr.to_string_lossy().into_owned())
        }
    }

    /// Create `a`, then `b`, each with DISTINCT content, in a fresh dir; return whether the FS
    /// collapsed them into a single file. Content-based (immune to `._` AppleDouble sidecars and
    /// `.DS_Store`, which would inflate a `read_dir` count): if the FS aliases `a≡b`, writing `b`
    /// truncates the shared file, so reading `a` back yields `b`'s bytes.
    fn fs_collapses(dir: &Path, a: &str, b: &str) -> std::io::Result<bool> {
        let _ = std::fs::remove_dir_all(dir);
        std::fs::create_dir_all(dir)?;
        std::fs::write(dir.join(a), b"AAAA")?;
        std::fs::write(dir.join(b), b"BBBB")?;
        let a_after = std::fs::read(dir.join(a))?;
        Ok(a_after == b"BBBB")
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn on_mount_oracle_norm_key_matches_filesystem_collapse() {
        let scratch = Path::new(env!("CARGO_MANIFEST_DIR")).join("_pathkey_oracle_scratch");
        let _ = std::fs::create_dir_all(&scratch);
        let fstype = fstypename(&scratch);
        if fstype.as_deref() != Some("exfat") {
            eprintln!("SKIP on_mount_oracle: not on exfat (fstype={fstype:?}); fold rules differ");
            let _ = std::fs::remove_dir_all(&scratch);
            return;
        }
        // (a, b, human note)
        let pairs: &[(&str, &str)] = &[
            ("Data.csv", "data.csv"),                     // case → collapse
            ("caf\u{00e9}.txt", "cafe\u{0301}.txt"),      // NFC é vs NFD é → collapse
            ("\u{00C4}nnex.txt", "\u{00E4}nnex.txt"),     // Ä vs ä → collapse
            ("stra\u{00df}e.txt", "strasse.txt"),         // ß vs ss → DISTINCT
            ("\u{0130}stanbul.txt", "istanbul.txt"),      // İ vs i → DISTINCT
        ];
        let mut failures = Vec::new();
        for (i, (a, b)) in pairs.iter().enumerate() {
            let d = scratch.join(format!("pair_{i}"));
            let fs = fs_collapses(&d, a, b).expect("fs probe");
            let key_eq = norm_key(a) == norm_key(b);
            if fs != key_eq {
                failures.push(format!(
                    "pair {a:?}/{b:?}: fs_collapses={fs} but norm_key_equal={key_eq}"
                ));
            }
        }
        let _ = std::fs::remove_dir_all(&scratch);
        assert!(
            failures.is_empty(),
            "on-mount oracle mismatch(es):\n  {}",
            failures.join("\n  ")
        );
    }
}
