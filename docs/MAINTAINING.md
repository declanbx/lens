# Maintaining this repository

Release engineering for Lens. Nothing here is needed to *use* the app — see the
[README](../README.md) for that.

## Keeping it in sync with the research-repo copy

Lens is developed inside a larger research repository and released from this standalone one. Two
scripts move changes between them, **both dry runs by default** — they print what would change and
write nothing until you add `--apply`.

```sh
./scripts/sync-from-home.sh          # what would come IN from the research repo
./scripts/sync-to-home.sh            # what would go OUT to the research repo
```

Two guards: pulling in refuses to run if this repository has uncommitted changes, so it can be
undone; pushing out asks you to type `yes` and does not commit for you. Only the app and the crawler
are synced — this repository's documentation, scripts and build configuration are release-only. If
the research repository has moved, set the environment variable named in the scripts' error message.

**Read the diff before you commit a pull.** The research copy and this one deliberately differ in a
few places: code comments and test fixtures here use neutral placeholders where the research copy
names a real drive or a real analysis directory. A sync will offer to overwrite those. Accepting it
publishes them.

## Rebuilding the screenshots

Every image in the README is recorded against a synthetic folder, never a real one, so that a public
repository never shows somebody's directory names.

```sh
python3 scripts/make-demo-folder.py ~/lens-demo-folder
```

That writes a few hundred files shaped like a real analysis folder: SVG figures whose axis labels
carry gene symbols, differential-expression tables with real column headers, single-cell matrices
with annotation names, scripts and notes. The worked example is `HMGCR` — three files carry it in a
name, and many more carry it only as text drawn inside a figure, which is what the **figure text**
checkbox is for.

Point Lens at that folder, and at two throwaway siblings (one renamed away so it reads as missing,
one never indexed) to reproduce the folder-list and confirmation shots. The two confirmation cards
print an absolute path; redact the account name before publishing.
