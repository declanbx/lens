//! Lens preview layer — the `repoindex://` custom-scheme byte producers (ported from figpolish's
//! `figpolish://` pattern). Heavy previews are served OFF the JSON/IPC path as RAW BYTES, NEVER
//! base64-in-JSON — the memory invariant of the app.
//!
//! Routes implemented (CONTRACT.md §`repoindex://`):
//!   * `repoindex://img/<id>` → the image file bytes (png/jpg/jpeg/svg/gif/webp) with the correct
//!     `Content-Type`, via `std::fs::read` — a pure passthrough, NO decode/re-encode.
//!   * `repoindex://md/<id>`  → comrak-rendered (`unsafe` OFF) + ammonia-sanitized HTML
//!     (`text/html`); the markdown source is capped at [`MD_SOURCE_CAP`] before rendering.
//!
//! Phase-2 routes (`thumb/`, `pdf/`, `code/`, `nb/`) are intentionally LEFT UNIMPLEMENTED — the
//! `match` in [`handle_repoindex`] is shaped so a new arm slots straight in next to `img`/`md`.
//!
//! The handler is SYNCHRONOUS and PANIC-SAFE: a bad header / build error degrades to a plain
//! body rather than unwinding the scheme thread (the figpolish discipline). It resolves an entry
//! id → (path, ext) by briefly locking the read-only [`crate::db::Db`] held in `tauri::State`.

use crate::db::{abs_of, Db, Projects};

/// Source cap for the markdown preview pipeline (CONTRACT: "source capped 512 KB"). The md route
/// truncates the on-disk source to this many BYTES before handing it to comrak, so a pathological
/// multi-MB markdown file can never blow up the render path.
pub const MD_SOURCE_CAP: usize = 512 * 1024;

// ───────────────────────────────────────────────────────────────────────────────────────────
// Response helpers
// ───────────────────────────────────────────────────────────────────────────────────────────

/// Build a scheme `Response` without ever panicking the (synchronous) scheme thread: a bad header
/// value degrades to a plain body instead of unwinding the whole app. Every response is
/// `Cache-Control: no-store` — phase-1 previews are disposable (no caching ratchet).
fn scheme_response(
    status: u16,
    content_type: &str,
    body: Vec<u8>,
) -> tauri::http::Response<Vec<u8>> {
    tauri::http::Response::builder()
        .status(status)
        .header(tauri::http::header::CONTENT_TYPE, content_type)
        .header(tauri::http::header::CACHE_CONTROL, "no-store")
        .body(body)
        .unwrap_or_else(|e| {
            let msg = format!("repoindex scheme: response build failed: {e}");
            eprintln!("[lens] {msg}");
            tauri::http::Response::new(msg.into_bytes())
        })
}

/// A 404 with a plain-text body — the common "no such id / file gone" reply.
fn not_found(msg: String) -> tauri::http::Response<Vec<u8>> {
    scheme_response(404, "text/plain; charset=utf-8", msg.into_bytes())
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// MIME + entry resolution
// ───────────────────────────────────────────────────────────────────────────────────────────

/// Map a file extension to an image `Content-Type` for the passthrough route. Unknown extensions
/// fall back to `application/octet-stream` (the browser still receives the bytes verbatim).
pub fn image_content_type(ext: &str) -> &'static str {
    match ext.to_ascii_lowercase().as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "svg" => "image/svg+xml",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "bmp" => "image/bmp",
        "tif" | "tiff" => "image/tiff",
        "avif" => "image/avif",
        _ => "application/octet-stream",
    }
}

/// The ACTIVE project's root — what [`abs_of`] resolves a stored repo-relative path against in the
/// scheme routes. Reads the managed [`Projects`] state (same `try_state` idiom as
/// [`entry_path_ext`]); degrades to [`crate::db::PROJECT_ROOT`] when the State is absent/poisoned so
/// a preview is best-effort rather than a hard failure.
fn active_root(app: &tauri::AppHandle) -> String {
    use tauri::Manager;
    app.try_state::<Projects>()
        .map(|p| p.active_root())
        .unwrap_or_else(|| crate::db::PROJECT_ROOT.to_string())
}

/// Look up an entry's `(path, ext)` by id from the read-only DB — the resolve step every scheme
/// route shares. Briefly locks the `Db` State (held in `State<Db>` by `lib.rs`). Returns `None`
/// when the State is absent, the lock is poisoned, or the id is not in the table.
fn entry_path_ext(app: &tauri::AppHandle, id: i64) -> Option<(String, String)> {
    use tauri::Manager;
    let db = app.try_state::<Db>()?;
    // `Db` is now a reader POOL; check out a connection via `with`. A dir id resolves to
    // `(path, "")` harmlessly — dirs have no img/md preview and the frontend never requests one.
    db.with(|conn| {
        conn.query_row(
            "SELECT path, ext FROM entries WHERE id = ?1",
            [id],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default())),
        )
        .map_err(|e| e.to_string())
    })
    .ok()
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Markdown render — comrak (unsafe OFF) → ammonia sanitize.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// Render markdown source → sanitized HTML:
///   1. comrak with `unsafe` OFF — raw inline HTML in the source is ESCAPED, never passed through
///      (no `<script>`/`<iframe>`/event-handler injection from a malicious `.md`);
///   2. ammonia's default allowlist sanitizer strips anything dangerous that survived.
/// The source is capped by the caller at [`MD_SOURCE_CAP`] before reaching here.
pub fn render_markdown(src: &str) -> String {
    let mut opts = comrak::Options::default();
    // CONTRACT: comrak `unsafe` OFF — the single most important sanitizer toggle for untrusted MD.
    opts.render.unsafe_ = false;
    // Common, safe niceties that match how the in-repo markdown is authored. None re-enable raw
    // HTML; ammonia still post-sanitizes regardless.
    opts.extension.table = true;
    opts.extension.strikethrough = true;
    opts.extension.autolink = true;
    opts.extension.tasklist = true;
    let html = comrak::markdown_to_html(src, &opts);
    let body = ammonia::clean(&html);
    // Wrap the SANITIZED body in a dark-themed document so the preview iframe is readable on the
    // dark app (the iframe is a separate document — the app's CSS can't reach inside it). The
    // <style> + doc shell are TRUSTED (added AFTER ammonia); only `body` is sanitized-from-source.
    format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><style>{MD_PREVIEW_CSS}</style></head><body>{body}</body></html>"
    )
}

/// Dark markdown theme for the `repoindex://md` preview iframe — mirrors the locked design's
/// markdown card (substrate bg, light ink, Claude-orange links, violet inline code). Fonts fall
/// back to system since the iframe is a separate document.
const MD_PREVIEW_CSS: &str = r#"
:root{color-scheme:dark}
html,body{margin:0;background:#0e1219;color:#c5cfda;
  font:13px/1.6 "IBM Plex Sans",-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
  padding:16px 18px;-webkit-font-smoothing:antialiased;word-wrap:break-word;overflow-wrap:anywhere}
h1{font-size:18px;font-weight:650;color:#e9eef5;margin:0 0 8px;padding-bottom:7px;border-bottom:1px solid rgba(255,255,255,.08)}
h2{font-size:14px;font-weight:650;color:#e9eef5;margin:16px 0 6px}
h3,h4{font-size:13px;font-weight:600;color:#e9eef5;margin:14px 0 4px}
p{margin:0 0 10px}
a{color:#d97757;text-decoration:none}a:hover{text-decoration:underline}
ul,ol{margin:0 0 10px;padding-left:20px}li{margin:2px 0}
code{font-family:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;font-size:12px;
  background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);border-radius:5px;padding:0 5px;color:#d2a8ff}
pre{background:rgba(0,0,0,.34);border:1px solid rgba(255,255,255,.08);border-radius:6px;padding:10px 12px;overflow:auto;margin:0 0 10px}
pre code{background:none;border:0;padding:0;color:#c5cfda;font-size:11.5px}
blockquote{margin:0 0 10px;padding:2px 0 2px 12px;border-left:2px solid #a5d6ff;color:#8b98a5}
table{border-collapse:collapse;margin:0 0 10px;font-size:12px}
th,td{border:1px solid rgba(255,255,255,.08);padding:4px 8px;text-align:left}
th{background:rgba(255,255,255,.04)}
img{max-width:100%}hr{border:0;border-top:1px solid rgba(255,255,255,.08);margin:14px 0}
"#;

// ───────────────────────────────────────────────────────────────────────────────────────────
// The route parser + dispatcher.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// The parsed shape of a `repoindex://<kind>/<id>` request: the kind (`img`/`md`/…) and the entry
/// id. `None` when the id segment is missing or not an integer.
struct Route {
    kind: String,
    id: i64,
}

/// Parse a `repoindex://<kind>/<id>` request into `(kind, id)`. The URI shape is
/// `repoindex://<kind>/<id>` where `<kind>` is the URI HOST (img|md|…) and `<id>` is the path
/// segment. On the macOS WKWebView build the host is sometimes folded into `uri.path()`, so this
/// recovers both robustly: prefer the explicit host, else split the first path segment.
fn parse_route(uri: &tauri::http::Uri) -> Option<Route> {
    let host = uri.host().map(|h| h.to_string());
    let trimmed = uri.path().trim_start_matches('/').to_string(); // "img/42" or just "42"
    let (kind, id_str) = match host {
        Some(h) if !h.is_empty() => (h, trimmed),
        _ => {
            let mut it = trimmed.splitn(2, '/');
            let k = it.next().unwrap_or("").to_string();
            let i = it.next().unwrap_or("").to_string();
            (k, i)
        }
    };
    let id: i64 = id_str.trim_end_matches('/').parse().ok()?;
    Some(Route { kind, id })
}

/// The single `repoindex://` handler. Resolves the entry id → (path, ext) from the read-only DB,
/// then dispatches on the route kind. Always returns a `Response` (never panics): malformed
/// requests → 400, unknown id / missing file → 404, unhandled kind → 404.
pub fn handle_repoindex(
    app: &tauri::AppHandle,
    request: tauri::http::Request<Vec<u8>>,
) -> tauri::http::Response<Vec<u8>> {
    let route = match parse_route(request.uri()) {
        Some(r) => r,
        None => return scheme_response(400, "text/plain; charset=utf-8", b"bad repoindex uri".to_vec()),
    };

    match route.kind.as_str() {
        // img/<id> → the image file bytes, passthrough, correct Content-Type. NO decode.
        "img" => match entry_path_ext(app, route.id) {
            Some((path, ext)) => {
                let abs = abs_of(&active_root(app), &path);
                match std::fs::read(&abs) {
                    Ok(bytes) => scheme_response(200, image_content_type(&ext), bytes),
                    Err(e) => not_found(format!("img {}: {e}", route.id)),
                }
            }
            None => not_found(format!("no entry {}", route.id)),
        },

        // md/<id> → comrak+ammonia HTML. Source capped at MD_SOURCE_CAP before rendering.
        "md" => match entry_path_ext(app, route.id) {
            Some((path, _ext)) => {
                let abs = abs_of(&active_root(app), &path);
                match std::fs::read_to_string(&abs) {
                    Ok(mut src) => {
                        if src.len() > MD_SOURCE_CAP {
                            // Truncate on a char boundary at/below the cap so the slice is valid
                            // UTF-8 (a hard `truncate` could split a multibyte char and panic).
                            let mut cut = MD_SOURCE_CAP;
                            while cut > 0 && !src.is_char_boundary(cut) {
                                cut -= 1;
                            }
                            src.truncate(cut);
                        }
                        let html = render_markdown(&src);
                        scheme_response(200, "text/html; charset=utf-8", html.into_bytes())
                    }
                    Err(e) => not_found(format!("md {}: {e}", route.id)),
                }
            }
            None => not_found(format!("no entry {}", route.id)),
        },

        // ── Phase-2 hooks (NOT implemented; the match is shaped so a new arm slots in here) ──
        //   "thumb" => …  // downscaled image bytes
        //   "pdf"   => …  // first-page render / raw bytes
        //   "code"  => …  // syntax-highlighted HTML
        //   "nb"    => …  // rendered notebook HTML
        other => not_found(format!("repoindex: unhandled kind {other:?}")),
    }
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Tests — the pure helpers (MIME map, markdown sanitization). The DB-backed routes are exercised
// end-to-end by the app; here we lock down the security-critical render behavior.
// ───────────────────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn image_content_type_maps_known_and_unknown() {
        assert_eq!(image_content_type("PNG"), "image/png");
        assert_eq!(image_content_type("jpeg"), "image/jpeg");
        assert_eq!(image_content_type("svg"), "image/svg+xml");
        assert_eq!(image_content_type("xyz"), "application/octet-stream");
    }

    #[test]
    fn markdown_renders_basic_structure() {
        let html = render_markdown("# Title\n\nsome **bold** text\n");
        assert!(html.contains("<h1>"));
        assert!(html.contains("<strong>bold</strong>"));
    }

    #[test]
    fn markdown_strips_raw_script_and_event_handlers() {
        // comrak unsafe OFF escapes raw HTML; ammonia removes anything that survives. A <script>
        // tag and an onerror handler must NOT appear executable in the output.
        let evil = "ok\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n";
        let html = render_markdown(evil);
        assert!(!html.contains("<script>"));
        assert!(!html.to_ascii_lowercase().contains("onerror"));
    }

    #[test]
    fn markdown_renders_tables() {
        let html = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n");
        assert!(html.contains("<table>"));
    }
}
