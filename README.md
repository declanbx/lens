# Lens

Lens is a macOS app that turns a very large research folder into something you can search as fast as
you can type. It catalogues the folder once — visiting every file, opening only its header, never its
data — and every search after that answers from the catalogue instead of the disk. Nothing is copied,
moved or altered; the catalogue sits in a folder beside your data.

It was built against a real working folder of **113,444 files and 1.40 TB**, and the numbers
throughout this documentation come from that folder.

## What actually gets searched

For every file, Lens builds **one line of text** and searches that line. The line is made of:

> the file's **path** + its **category, extension and which reader handled it** + its **tags** + the
> **descriptors** pulled out of the file itself

**It does not search the contents of your files.** Not the rows of a spreadsheet, not the body of a
document, not the text inside a PDF. What goes into that line is names, paths, and a specific,
limited set of descriptors read from each file's header — nothing more.

Measured over the whole index (28.9 MB of searchable text): the file path makes up **42.3%** of it,
the extracted descriptors **50.5%**, and category/extension/reader/tags together **7.2%**. Text drawn
*inside* SVG figures — axis labels, legend entries, gene symbols — lives in a separate 3.95 MB store
and is searched only when you tick the **figure text** box; it is never part of the default search.
That box matters more than it looks: the gene symbol `HMGCR` is found in **20** files by the default
search, and in **134 further files** only as text drawn inside a figure.

**What "descriptors" means depends entirely on the file type**, and for most files it means nothing at
all:

| File type | What Lens reads | What it never reads |
|---|---|---|
| `.h5ad` single-cell matrices | shape (cells × genes), how the matrix is stored, its numeric type, every per-cell and per-gene annotation column name, embedding and layer names *(needs `h5py`)* | the matrix values |
| Markdown | the first heading and the first paragraph, nothing else | any later heading or paragraph |
| Python / R / shell scripts | top-level function and class names, imported modules, the first docstring line | anything inside a function body |
| JSON / YAML / TOML | top-level keys only, and only if the file is under 5 MB | nested keys, and every value |
| CSV / TSV / parquet | column names and the exact row count — **column names are on by default but can be switched off at index time; with it off, a search by column name finds nothing until the folder is re-indexed with it back on** | any cell value |
| SVG figures | the words rendered inside the figure | — (see the figure-text box above) |
| Everything else — **46.1% of files** | name, size and date only | everything inside |

Two concrete examples: typing `padj` finds the result tables whose column headers include `padj` —
provided column names were switched on when that folder was indexed. Typing `Braak` finds the
single-cell files that carry a `Braak` annotation, without opening any of them.

## How matching works

Matching is **plain substring, case-insensitive** — typing `umap` finds anything whose line contains
"umap" anywhere, including in the middle of a word. There is **no typo tolerance** (one wrong letter
finds nothing), **no wildcards**, **no regular expressions**, **no AND/OR**, and accents are not
folded (`cafe` will not match `café`). Results are ordered by how many of your words matched, then
*where* they matched (folder name beats file name beats path beats descriptors), then whether the
match landed on a word boundary, then your chosen sort column — there is no relevance score. Type and
category filters combine with whatever text you typed.

**53.9% of files had at least one descriptor extracted beyond name, size and date; 46.1% did not.**
[`HOW_IT_WORKS.md`](HOW_IT_WORKS.md) is the detailed sheet: exactly what is read from each file type,
and exactly where each capability stops.

---

## Install

If someone sent you `Lens.dmg`:

1. Double-click the `.dmg` and drag **Lens** into your **Applications** folder.
2. Open Applications, **right-click** (or Control-click) **Lens**, and choose **Open**. A warning
   appears; click the **Open** button in it.
3. From then on a normal double-click works.

Step 2 is a one-time step and it is not optional. The app is not signed with a paid Apple Developer
account, so macOS blocks a plain double-click the first time it sees it. Right-click ▸ Open is how
you tell macOS you trust it. If your version of macOS refuses even that, open **System Settings ▸
Privacy & Security**, scroll down, and use the button that offers to open the blocked app anyway.

---

## What you need

**macOS.** Lens is macOS-only, and that is a fact about the app rather than a temporary gap: it uses
macOS-only window effects, and Reveal in Finder and Open are the system's own commands. There is no
Windows or Linux build and none is configured. The build declares macOS 10.13 as its minimum. The
`.dmg` you were sent is either universal (Apple Silicon and Intel) or built for one architecture,
depending on how it was made — the default build is universal.

**Python 3.9 or newer.** The part of Lens that reads your files is a Python program, bundled inside
the app; what Lens needs from you is an interpreter to run it with. Most Macs used for science
already have one — from Anaconda, Homebrew, or python.org. Lens looks for one in the usual places,
checks that it is really version 3.9 or newer by running it, and remembers the one that worked.

**If it cannot find one**, Lens says so in plain language and offers a **Choose Python…** button that
opens a file picker; pick the interpreter you use for your own work and Lens saves the choice.
Until then, nothing can be indexed — the catalogue is the whole product. (If you manage several
Python installations and want to force a specific one, the error message names an environment
variable that overrides everything else.)

You do **not** need to install any Python packages for the basic catalogue. The crawler uses only
the standard library.

### Optional extras, and exactly what each one adds

Install these into the same Python that Lens uses:

```sh
python3 -m pip install h5py pyarrow
```

| Package | Unlocks | Without it |
|---|---|---|
| `h5py` | `.h5ad` single-cell matrices and generic `.h5` files: the shape (cells × genes), how the matrix is stored, the numeric type, the full list of per-cell and per-gene annotation column names, the embedding names and shapes, the layer and unstructured-entry names | those files are catalogued by name, size and date only — **and silently**, with no error shown anywhere |
| `pyarrow` | `.parquet` files: column names with their types, the row count, the number of row groups, all read from the file's footer | nothing beyond name, size and date |
| `pyyaml` | slightly more robust YAML reading | Lens falls back to scanning for unindented `key:` lines, which still finds the top-level keys |

One more version note: `.toml` files are read with a parser that only exists in Python **3.11 and
newer**. On 3.9 or 3.10 a `.toml` file yields nothing, even though the rest of Lens works fine.

---

## First use

1. Open Lens and choose a folder to index.
2. Wait for the first pass. Lens walks the entire folder, records every file, and opens the header of
   every file type it understands. The first index of a large folder takes a while — it is reading
   each file's front matter, not just listing names — so start it and go and do something else. The
   window reports progress throughout.

   **Measured:** the 1.40 TB, 113,444-file folder above took **10 minutes 11 seconds** for a
   complete cold pass, over USB to an external SSD — about 186 files a second. A folder of a few
   thousand ordinary files is done in seconds. Your own time will track the number of files far more
   than the number of terabytes, because the cost is opening each file's header, not reading it.
3. After that, re-indexing is cheap. The walk still visits everything, but a file is only re-read if
   its size or its modified time changed.

![Lens on first run: a single card asking you to choose a folder to index.](docs/screenshots/01_first_run.png)
*On a fresh install Lens has no folders and no preset path — it asks you to pick one.*

**What you will see:** a folder tree on the left that opens fully collapsed; a band of best matches
above it once you type; a preview and an information panel on the right. The bottom-left of the
window counts what is in view against the whole index. `/` jumps to the search box, `↑ ↓` move,
`⏎` inspects.

One honest note on that information panel: the rows × columns line only appears for matrix files
like `.h5ad`. For a spreadsheet it shows nothing there, and it never lists column names — even for a
folder where column names were indexed and are fully searchable.

**Where the catalogue goes:** into a folder named `_repo_index` inside the folder you indexed. On the
1.40 TB folder above the database the app reads is **193 MB** — 0.014% of the data — and the whole
folder, counting the companion exports written beside it, about 388 MB. Deleting it loses the
catalogue and nothing else.

**While Lens is open it watches the folder.** New, changed and deleted files appear by themselves, a
moment after the change settles. The **Live** button parks that if a repaint is getting in your way; it
counts what is waiting and applies it all when you release it. The **⟳** button beside it re-scans
the whole folder from scratch, which is what to reach for if you suspect the catalogue has drifted.

---

## Managing your folders

The button at the top right lists every folder Lens knows about and switches between them. One
folder is open at a time.

![The folder list, showing an active folder, one that cannot be found, and one not yet indexed.](docs/screenshots/02_folder_list.png)
*Three states, told apart at a glance: a tick marks the folder you are in; an amber ⚠ marks a folder
Lens cannot find right now — moved, renamed, or on a drive that is unplugged; and a folder Lens has
never read is labelled so.*

Hover a row and a **✕** appears at its right-hand end. It asks before doing anything:

![The confirmation card for a folder that cannot be found.](docs/screenshots/03_forget_missing_folder.png)
*Forgetting a folder Lens cannot find. It explains why the folder is flagged, and the option to
delete the catalogue is greyed out with the reason, because there is no catalogue to delete.*

![The confirmation card for a folder that has been indexed, with the delete option available.](docs/screenshots/04_forget_with_index.png)
*For a folder that has been indexed, the same card offers to delete its catalogue too and tells you
how much that frees. It is off unless you tick it.*

Forgetting a folder removes it from the list and nothing else — your files are never touched, and
the catalogue stays on disk unless you ask for it to go. You can forget the folder you are currently
viewing: Lens moves to another folder first, or returns to the opening screen if that was the last
one.

---

## Building from source

**Requirements:** macOS, **Node 22 or newer**, and **Rust** (install from rustup.rs). The exact Rust
version Lens needs — 1.96.1 — is pinned inside the checkout and installs itself; you do not have to
choose a version. It is a hard floor, not a preference: the database engine compiled into the app
will not build on anything older.

One script does everything:

```sh
./scripts/build-dmg.sh              # universal (Apple Silicon + Intel) — the default
./scripts/build-dmg.sh --native     # this Mac's architecture only; much faster, for testing
./scripts/build-dmg.sh --help
```

It checks your toolchain, installs either missing architecture for the pinned Rust version, installs
the frontend dependencies, builds, and copies the finished installer and app into `release/`, printing
the exact path at the end. The first build takes several minutes.

Build output stays inside the repository and is ignored by git.

---

## Troubleshooting

**The app will not open.** macOS is blocking it because it is unsigned, not because it is broken. Do
the right-click ▸ Open step under Install, from the Applications folder rather than from the mounted
disk image.

**Indexing failed, or Lens says it needs Python.** Choose an interpreter with the **Choose Python…**
button and make sure it really is version 3.9 or newer — Lens checks by running it, so an interpreter
that is too old is refused rather than half-working. If indexing then fails on particular files
rather than at the start, that is usually a missing optional package: `.h5ad` and `.h5` files need
`h5py` and `.parquet` needs `pyarrow`, and without them those files are catalogued with no detail and
no complaint.

**A search finds nothing, and you are sure the word is in the file.** It probably is — inside the
file, which is not searched. Searching covers names, paths and the descriptors Lens extracts from
headers. There is also no spelling tolerance: one wrong letter matches nothing, and there are no
wildcards. If the word is drawn inside an SVG figure, tick the **figure text** box next to the search
box, which folds that text into the search. The full account of what is and is not searched is in
[`HOW_IT_WORKS.md`](HOW_IT_WORKS.md).

**The folder moved, or the drive was unplugged.** A registered folder whose catalogue cannot be found
is shown dimmed with a warning marker and its missing path, so it is distinguishable from a working
one. Plug the drive back in and pick the folder again from the switcher. Use the **✕** on its row to
forget a folder for good; the confirmation card offers to delete its catalogue files too and tells
you how much that frees.

One honest caveat: if a drive is unplugged and replugged while Lens is running, the live watch does
not re-arm itself — the window will stop noticing changes with no indication that it has. Restart
Lens after replugging.

**Two copies of Lens are open.** The second one runs read-only: it can browse and search but it will
not update the catalogue, and it does not announce this.

---

## Keeping it in sync with the research-repo copy

Lens is developed inside a much larger research repository and released from this standalone one. Two
scripts move changes between them. **Both are dry runs by default** — they print exactly which files
would change and write nothing until you add `--apply`.

```sh
./scripts/sync-from-home.sh          # show what would come IN from the research repo
./scripts/sync-from-home.sh --apply

./scripts/sync-to-home.sh            # show what would go OUT to the research repo
./scripts/sync-to-home.sh --apply
```

Always run without `--apply` first and read the plan.

Two guards worth knowing about. Pulling in refuses to run if this repository has uncommitted changes,
so whatever it does can be undone. Pushing out asks you to type `yes` before it overwrites anything
inside the research repository, and it does not commit for you — check that repository's status
afterwards and commit deliberately.

Only the app and the crawler are synced. This repository's own documentation, scripts and build
configuration are release-only and are never copied in either direction. If the research repository
has moved, set the environment variable named in the scripts' own error message to its new location.

---

## Where to read more

[`HOW_IT_WORKS.md`](HOW_IT_WORKS.md) — what the crawl visits and skips, what is recorded from each
file type and where each capability stops, how search ranks results, what updates by itself, what
previews, and an honest list of what is not built yet.
