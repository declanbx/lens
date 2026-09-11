# How Lens works

The detailed sheet. Plain language, honest limits, and every number carrying what it counts. For
installing and running the app, see [`README.md`](README.md).

The numbers throughout come from the folder Lens was built against: **112,634 files, 1.40 TB**.

---

## 1. The idea: a catalogue of a big folder, not a copy of it

Lens makes a catalogue. Every file in the folder you choose gets one row: where it sits, how big it
is, when it last changed, what kind of thing it is. For the file types it understands it goes one
step further and opens the file's *front matter* — the header, the labels, the dimensions — and
records the structure it finds there without ever reading the data itself. A single-cell matrix
contributes its shape and the names of its annotation columns; a spreadsheet contributes its column
names and its row count; a figure drawn as SVG contributes the words printed on it.

Searching and browsing then run entirely against that catalogue, which is why they are instant and
why they do not touch your drive. On the 1.40 TB folder above, the catalogue comes to about 0.6 GB —
roughly 0.04% of the data it describes.

Your files are never written to, moved, renamed or deleted. Lens has no way to do any of those
things (see §9), and the window is not permitted to reach the network at all — its content policy
lets it talk to the app itself and to nothing else.

![The Lens pipeline in five stages: your folder, the crawl that walks it once, the per-file reader
that takes only each file's header, the catalogue that gets written beside your data, and the app
window that searches the catalogue — with a closing band listing what Lens does not
do.](docs/figures/pipeline.png)

*The one thing to take from this: the reading is done once, on the way into the catalogue — and
everything you do in the window afterwards is a question asked of that catalogue, not of your disk.
The colour split marks which half does what: the crawler that reads your files is a Python program,
and the window that searches the result is the app itself.*

---

## 2. The crawl

### What it visits

Everything under the folder you chose, at every depth, whatever the size. **There is no size limit on
which files are indexed** — a 200 GB file gets a row like any other. Size limits apply only to how
deep Lens reads into a file (§3), never to whether it appears.

The folder tree you browse is derived from the paths of the files themselves; folders are not
separately hunted for.

### What it skips, and why

- **Machine-generated directories, never descended into at all:** `.git`, `node_modules`, `build`,
  `dist`, `.venv`, `venv`, `site-packages`, `__pycache__`, `.cache`, `.ipynb_checkpoints`,
  `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.eggs`, `_vendor`, and Lens's own catalogue folder.
  These are large, machine-owned, and nobody searches them. You can add your own include and exclude
  patterns; where they conflict, exclude wins.
- **macOS's `._` companion files**, which the system writes beside real files on some disks. They are
  dropped before Lens even looks at them.
- **Its own output**, compared by resolved path, so moving the catalogue elsewhere inside the folder
  does not cause it to index itself.

**Hidden files are not skipped.** A `.gitignore` or any other dotfile is indexed normally; it simply
has no extension, so it lands in the catch-all category.

**Unreadable folders are not silently dropped.** A folder Lens cannot open becomes a visible error
row, so a permissions problem shows up rather than quietly shrinking your index.

### Symlinks: recorded, never followed

A symbolic link gets exactly one row, noting what it points at and whether that target currently
exists. Lens does not descend into a link that resolves to a folder, and it never opens a file
through a link to read its header. The reason is that following a link would record the *target's*
structure under the *link's* path and size, which makes both wrong. In the reference folder: **571
symlinks, 20 of them broken**.

### What "changed" means on a re-index

The walk is always complete. Every folder is listed and every file's size and modified time is
checked, every time. What is skipped on a re-index is the *reading*.

A file is opened again only if one of these is true:

- its size differs from last time, **or**
- its modified time differs, compared to the nearest second, **or**
- reading it failed last time (a failure is always retried, never cached), **or**
- it was written within 2 seconds of the previous index.

That last one is not paranoia. Some filesystems record modified times only to the nearest 2 seconds,
so a file edited in the same tick as the index — same second, same size — would otherwise look
untouched forever. Anything inside that window is re-read on principle.

**There is no content hashing.** Freshness is size and modified time, nothing else, so a file
rewritten to exactly the same size without its modified time changing would go unnoticed. In practice
the live watch (§5) catches changes as they happen while the app is open.

---

## 3. What is recorded

### For every file, whatever type it is

Its path inside the folder; its extension; a **category** — one of twelve: code, data table, data
matrix, config, document, notebook, figure, PDF figure, model, log, archive, and other; its size in
bytes; its modified date to the second; whether it is a symlink, what it points at and whether that
target exists; which reader handled it; and any error that reader hit.

One honest oddity: Lens also tags paths against a short list of word patterns (`d70`, `d100`, `qian`,
`braun`, and so on). That list is specific to the single-cell project Lens was built for and ships as
the default. On someone else's files those tags are simply meaningless — they do no harm, they just
find nothing.

### Then, per file type

Everything below is read from the file's **header**, not its contents, unless the text says
otherwise. The two exceptions are stated where they occur: a row count walks the record boundaries of
a spreadsheet, and SVG figure text is read from the whole file.

---

#### Spreadsheets — `.csv`, `.tsv`, `.csv.gz`, `.tsv.gz`

*26,538 CSVs out of 112,634 files indexed — the single largest file type in the reference folder.*

**Column names: yes, in full, and they are searchable.** Lens reads the first line of the file, works
out whether the separator is a comma or a tab, and parses the header. Every column name goes into the
search index, so a search for an obscure column name finds the files that have it.

Where it stops:

- **The header read is capped at 1 MB.** A file whose first line runs past a megabyte before its
  first line break — a very wide matrix written on one line — yields column names parsed from the
  truncated prefix only. The cap exists so that a binary file mislabelled as `.csv` cannot be read
  into memory whole.
- **The row count is exact, but only up to 100 MB** (25 MB for a gzipped file). It is counted by
  walking the file's record boundaries, so a quoted value containing a line break counts as one row
  and not two. Above the size limit the count is left empty and marked as size-limited; the column
  names are still there.
- **No cell value is ever read.** The only thing read past the header is the record boundaries needed
  for the count.
- **The column list can be switched off** when building the index to make it smaller. With it off,
  the *number* of columns survives and their *names* do not, and searching for a column name stops
  working until the folder is indexed again with it on.
- The information panel does not currently display the column list, and shows dashes where a
  spreadsheet's rows × columns would go (see §9). The names are indexed and findable; they are just
  not drawn.

#### `.parquet`

Column names with their types, the total row count and the number of row groups, all read from the
file's footer — no data pages are touched. **Needs `pyarrow`**; without it a `.parquet` file gets
name, size and date only. *486 parquet files in the reference folder.*

#### `.xlsx` — Excel

**Nothing.** No sheet names, no column headers, no row counts. Excel files are categorised as data
tables and then treated like any unknown type. *111 `.xlsx` files in the reference folder got name,
size and date only.*

---

#### `.h5ad` — single-cell matrices

*430 of them in the reference folder.*

Read **without loading the matrix**, by looking only at the file's internal labels, dimensions and
numeric types:

- the shape — how many cells by how many genes;
- how the matrix is stored (compressed sparse by row, by column, or dense) and the numeric type of
  its values;
- the name of the cell-identifier column;
- the **full, ordered list of per-cell annotation column names** — the thing you most often want to
  search for;
- the per-gene column names, and how many there are;
- the names of the embeddings, each with its shape;
- the names of the layers, the pairwise matrices, the per-gene matrices and the unstructured
  entries;
- whether a raw copy of the counts is present.

No data array is ever read into memory. **Needs `h5py`** — and without it the degradation is
**silent**: the file still appears, with no structure and no error message to tell you why.

#### `.h5` — generic HDF5

The top-level group names, and the shapes of the datasets inside them, **two levels deep only**.
Shapes, never values. Also needs `h5py`, with the same silent degradation. *180 `.h5` files.*

#### `.npy` / `.npz` — NumPy arrays

Shape, numeric type and storage order, read from the array's header bytes. For a `.npz` bundle, Lens
opens the zip and reads only the leading header bytes of each member — nothing is decompressed, and a
member it cannot parse is left out rather than turned into an error. This one works whether or not
NumPy is installed. *408 `.npy` and 1,503 `.npz` files.*

---

#### JSON, YAML and TOML

*8,486 JSON files out of 112,634.*

**Top-level keys only. Values are never recorded, and nested keys are never walked.**

- An object contributes the names of its top-level keys and how many there are. Those key names are
  searchable.
- A JSON file that is an array at the top level contributes only its length. One that is a bare
  number or string contributes only which of those it is.
- **Anything over 5 MB is not opened at all** and contributes nothing to search beyond its name.
- YAML is parsed properly when `pyyaml` is installed and otherwise falls back to scanning for
  unindented `key:` lines, which finds the top-level keys anyway.
- TOML needs Python 3.11 or newer. On 3.9 or 3.10 a `.toml` file yields nothing.

So: if you are looking for *which configuration file has a `random_seed` setting*, Lens will find it.
If you are looking for *which configuration file sets the seed to 42*, it will not — the value is
not recorded anywhere.

#### Notebooks — `.ipynb`

How many cells there are, how many are code, how many are markdown, the kernel's name, and the first
heading in the first markdown cell. **Cell source and outputs are never read** — not the code, not
the imports, not the results. And a notebook over 5 MB, which is common as soon as outputs with
embedded images have been saved, is not opened at all. *17 notebooks in the reference folder.*

#### Markdown — `.md`

*3,235 files.* The first `#` heading, the first paragraph of prose (fenced code blocks are skipped
over), and the list of paths the document links to. **There is no heading outline and no body text
beyond that first paragraph.** Over 5 MB, nothing is read.

The link list is also what feeds the lineage graph (§7). A bare word in backticks counts as a path if
it contains a slash and either ends in one of 38 recognised extensions or begins with one of five
folder names hard-coded to one particular project — so on someone else's repository the backtick half
of that detection will find much less.

#### Code — `.py`, `.R`, `.sh`

*9,523 Python, 621 R, 318 shell.* Parsed, **never executed**.

- **Python:** the first line of the module docstring, plus the names of the top-level functions,
  classes and imported packages. **Top level only** — a function defined inside a class or inside
  another function is invisible to Lens. A file with a syntax error yields nothing.
- **R:** function names, the packages it loads, and the roxygen title.
- **Shell:** the interpreter line and the first comment block, read only as far as the first real
  command.
- Over 5 MB, nothing.
- `.c` and `.cpp` are categorised as code but nothing is parsed from them. *8 files in the reference
  folder.*

---

#### SVG figures — the one place text inside a file is read

*5,803 SVGs.* Lens pulls the words out of the figure: axis labels, tick labels, legend entries,
titles, gene symbols.

It handles **both** ways plotting libraries write text. The easy one is real text elements. The hard
one is the case where every character has been converted into an outline shape — matplotlib's default
for many figures — where nothing in the file is text at all and the character has to be recovered
from each glyph's code point. So a gene symbol printed on a volcano plot is findable even though the
file contains no letters.

Where it stops:

- words are lowercased, de-duplicated, kept between 2 and 40 characters, and pure numbers are
  dropped;
- a file up to 20 MB is read whole; above that Lens reads the first quarter and the **last** three
  quarters, because matplotlib writes the axis text *after* the plot data — reading only the start of
  a big figure would find nothing;
- **figure text is deliberately kept out of the default search.** It is a large, noisy surface, so it
  sits behind the **figure text** checkbox next to the search box. With the box off, searching behaves
  exactly as it did before the feature existed.

#### PNG, JPEG, PDF and everything else

*8,877 PNGs, 7,094 JPEGs, 1,344 PDFs.* Name, size, date and category. **No width or height, no colour
depth, no EXIF, no PDF text.**

That is not a small hole, and it is worth seeing at scale: figures are the second-largest category in
the reference folder at **21,774 files**, and only the **5,803** of them that are SVG give up anything
at all.

The same applies to plain text (`.txt` — **14,444 files**, the second largest single type), `.rst`,
`.ini`, pickles and saved models, logs, archives, Word and PowerPoint documents, and the **2,697**
files that have no extension at all. They are catalogued, findable by name, and nothing is read from
inside them.

**The honest total: 52,151 of the 112,634 files — 46.3% — were handled by the fallback reader, which
takes nothing.** Counting files that ended with no extracted detail for any reason (fallback,
size limit, or a missing optional package) brings it to 52,214, or 46.4%. Just over half of a real
research folder gets described in depth; the rest is findable by name, path and date alone. That
ratio is worth knowing before you go looking for something.

---

## 4. Search

### What you get

Results appear as you type, with no delay to speak of: the first three stages are measured at 1–4 ms
across 61,524 files. The folder tree on the left narrows to the matches at the same time as the band
of best matches fills at the top — the tree is *rebuilt* rather than pruned, so when you sort by
"newest" you get the newest **matching** file in each folder rather than the newest file of any kind.

### What is actually searched

File names, the full path (so a folder name matches everything beneath it), and the descriptors from
§3: column names, matrix shapes and annotation names, top-level JSON and YAML keys, Python function
and import names, markdown titles and opening paragraphs, the category, the extension, the tags.

**Not the text inside files.** No CSV cell, no PDF page, no notebook cell, no document body, no
source-code line is searched. The one exception is SVG figure text, behind its checkbox.

### How results are ordered

Four stages, each looking at a wider surface than the last:

1. **folder names** whose last segment contains every word you typed — at most 3, always shown first;
2. **file names** that contain every word;
3. **paths** that contain every word;
4. **descriptors** that contain every word — this one is a question to the catalogue rather than a
   scan of what the window already holds, so it lands about a sixth of a second behind your typing
   and then folds into the same list.

Within that order, a match that begins at a word boundary — the start of the name, or just after a
`/`, `-`, `.`, `_`, a digit, or a capital in the middle of a `camelCase` name — is ranked above one
buried mid-word. Then your chosen sort (name, newest, oldest, largest, smallest, or type), then the
path itself, so the ordering never wobbles between keystrokes.

The band holds **20** results. If fewer than 20 things match every word, it is topped up with partial
matches, always pinned below the full ones.

### The filters

- `ext:png` — an exact extension.
- `cat:data` or `type:data` — a category, matched as a substring, so `cat:data` catches both data
  tables and data matrices.
- Several filters of the same kind are OR'd (two extension chips show both); different kinds are
  AND'd; and every filter is AND'd with every word you typed.
- The **Types** button lists the extensions actually present, most common first, with eight common
  ones pinned at the top. Ticking a chip and typing the filter by hand are the same operation.
- Two words are AND, not OR: each word you add narrows the result.
- Double quotes make a phrase, so `"cell type"` looks for that as one string.

### Plainly stated: no typo tolerance

There is **no fuzzy matching** of any kind — no edit distance, no stemming, no synonyms, no
"did you mean". One wrong letter matches nothing. There are also no wildcards (`*` and `?` are
ordinary characters), no regular expressions, no `NOT`, no `OR`, no brackets, and no size or date
comparisons.

Matching is plain substring containment, which cuts both ways: `ser` finds `serum` and also `laser`,
and a word that appears in neither the name nor the path nor the descriptors is simply not findable.

Two more things worth knowing before you blame yourself:

- **Accents are not folded.** `cafe` will not find `café`. Upper and lower case are treated as the
  same thing for the English alphabet only, so a capital `Δ` or `É` is not case-folded on either side.
- The search box's placeholder still advertises `path:`, `dir:` and `obs:`. Those are accepted
  without complaint but **do not restrict anything** — the value is treated as an ordinary word. Only
  `ext:`, `cat:` and `type:` really filter.

### Opening big folders

The tree starts fully collapsed and opening a folder is budgeted: about 1,500 rows go on screen per
click, with a hard ceiling of 8,000 and a "show all" that re-plans up to it. A folder too large even
for that says so rather than hanging. This is not timidity — on the index these timings were taken
from (61,524 files in 9,151 folders), drawing the largest folder, 39,559 rows, froze the window for
86 seconds. The budget opens 99.5% of those 9,151 folders completely on the first click.

---

## 5. The live index

### What updates by itself

While Lens is open it watches the folder you are browsing. Files that appear, change or disappear on
disk are folded into the catalogue and the window repaints, a moment after the change settles. The
delays Lens itself adds are small and deliberate: the watcher waits 300 ms for changes to stop before
acting on them, forces them through anyway after 2 seconds if they keep arriving, and the window
waits a further 400 ms before repainting. How long the update itself takes depends on the folder.

Filesystem notifications are treated as hints, never as facts: Lens looks at the file itself rather
than believing an event that says "created" or "deleted". Its own catalogue folder is excluded from
the watch, so writing the index does not trigger a re-index of the index.

Your place is kept across a refresh: which folders are open, where you had scrolled, and where the
keyboard cursor was.

The **Live** button parks all of this. While it is held nothing repaints, the button counts what is
waiting (`Held · 3`), and releasing it applies everything in one pass. It exists because a repaint
that swaps the row out from under your pointer eats the click — tolerable while browsing, maddening
mid-search.

### What does not update by itself

- **The lineage links (§7).** They are rebuilt only by a full re-index, and a re-index from inside the
  app does not reload them into the window — they keep showing what was loaded when the app started
  until you switch folders or restart.
- **The watch does not survive unplugging a drive.** If the volume goes away and comes back, the
  watch is not re-armed, and there is no indicator anywhere telling you that it has gone quiet.
  Restart Lens after replugging.
- **A second copy of Lens runs read-only** with no live index, and does not say so.
- **A storm of more than 10,000 changed paths** stops being tracked individually and collapses into a
  full re-scan of the tree.
- Every accepted change reloads the whole catalogue into the window rather than patching the affected
  rows. This is fast at the tested scale and is the known cost of the current design.

---

## 6. Previews

Two kinds of file render. Everything else shows its metadata.

**Images** — `png`, `jpg`, `jpeg`, `svg`, `gif`, `webp`, `bmp`, `tif`, `tiff`, `avif` — are handed to
the window as raw bytes, never re-encoded, on a white ground so figures with transparent backgrounds
read correctly against the dark app. SVGs go through the same path as any other image. Limits: there
is **no size limit**, so a very large image is decoded at full resolution and will cost you the memory
to do it; there is no zoom or fit control; and the image is stretched to the pane's width whether it
is larger or smaller than that. Whether the system actually draws `tif`, `avif` and `bmp` has not been
tested here — Lens hands them over and lets the system decide.

**Markdown** renders as a document: headings, tables, strikethrough, automatic links, fenced code
blocks. It is sanitised on the way through, and the sanitising has visible consequences worth knowing:
any raw HTML written into the file **disappears with no marker**; a code block's language label is
stripped, so there is no syntax colouring; checkboxes are removed from task lists, leaving bare list
items; footnotes and mathematics are off; and **no image inside a previewed markdown file will load**,
in any form the link can take. Source over 512 KB is silently truncated, and links do not navigate.

`.txt` and `.rst` are put through that same markdown renderer. So `#`, `*` and `_` in a plain text
file are interpreted as formatting, and reStructuredText is mangled — nothing in Lens parses
reStructuredText.

**Everything else** — PDFs, notebooks, spreadsheets, code — shows a line naming the type and pointing
at the information panel beside it. That panel carries: the name, category, extension, size and
relative date; the path, clickable to copy, with a second button for the absolute path; for a matrix,
its cells × genes and the storage and type details; the per-cell annotation column names in a
collapsible list with a live filter and a copy-all button; the embedding names as chips; the layer and
unstructured-entry names, capped at 48 each with a note saying how many there were; the lineage card;
and a provenance card naming which reader handled the file and any error it hit.

**What you can do with a file from here:** reveal it in Finder, open it in its normal application
(from the information panel), copy its path four ways, and drag it out into another application —
holding ⌘ to move rather than copy, which is the system performing the move, not Lens.

---

## 7. Lineage — "references" and "referenced by"

Select a file and the information panel shows two lists: what this file points at, and what points at
it. Clicking either navigates there. A target that is not in the index renders struck through and
does not click — about **14%** of link targets are files the index does not hold.

**The links come from exactly two places, and this is the honest limit of the feature:**

1. **Markdown documents** — the targets of their links, plus backticked words that look like paths
   (see §3).
2. **Scripts** — paths found in what was *already extracted* from the script: its imports, its
   top-level function and class names, its docstring's first line. **Not the body of the file.**

That second one has a consequence worth stating outright: a path written inside a function, such as
reading a CSV in the middle of an analysis script, produces **no link**. Lineage catches paths
mentioned in prose and on a script's surface. It is not a data-flow graph and it does not read your
code.

Targets are resolved by trying the exact relative path, then the path relative to the folder the
reference came from, then a unique filename match. Spreadsheets, matrices, notebooks and
configuration files contribute no links in either direction.

And, as in §5, the graph is only rebuilt by a full re-index and does not refresh live.

---

## 8. The numbers

Lens was built and measured against a real working folder: **112,634 files, 1.40 TB**, with
**571 symlinks** (20 broken) and **0 extraction errors** in the run these figures come from.
**60,420 of those files (53.6%) had something read out of them beyond name, size and date; the
other 52,214 (46.4%) did not** — see the end of §3 for why.

![Composition of the reference folder: 112,634 files across twelve categories, and the extensions
that dominate it, each shown twice — once by how many files it holds and once by how many
bytes.](docs/figures/corpus_composition.png)

*The point of this figure is that counting files and counting bytes give opposite answers. The 26,538
CSVs are the largest population of files — 23.6% of them — and 6.3% of the 1.40 TB. The 2,521 data
matrices are 2.2% of the files and **71.7% of the bytes**; the 430 `.h5ad` files alone are 58.4% of
the total. Whichever way you look at this folder, you are looking at a different folder.*

The largest slices, and what Lens gets from each:

| Slice | Files | What is recorded beyond name, size and date |
|---|---:|---|
| `.csv` | 26,538 | column names, exact row count up to 100 MB |
| `.txt` | 14,444 | nothing |
| `.py` | 9,523 | docstring first line, top-level functions, classes, imports |
| `.gz` (archives) | 8,910 | nothing |
| `.png` | 8,877 | nothing |
| `.json` | 8,486 | top-level key names, up to 5 MB |
| `.jpg` | 7,094 | nothing |
| `.svg` | 5,803 | the text drawn inside the figure |
| `.md` | 3,235 | first heading, first paragraph, outgoing links |
| `.npz` | 1,503 | member names, shapes and types |
| `.pdf` | 1,344 | nothing |
| `.h5ad` | 430 | shape, encoding, annotation column names, embeddings, layers |

By category, counted both ways — files, and then share of the 1.40 TB:

| Category | Files | Share of bytes |
|---|---:|---:|
| data tables | 30,241 | 10.3% |
| figures | 21,774 | 1.1% |
| documents | 17,680 | 0.04% |
| code | 10,470 | 0.008% |
| archives | 8,965 | 7.2% |
| config | 8,611 | 0.2% |
| other | 8,288 | 4.7% |
| **data matrices** | **2,521** | **71.7%** |
| logs | 2,267 | 0.003% |
| PDF figures | 1,344 | 0.2% |
| models | 456 | 4.6% |
| notebooks | 17 | 0.001% |

The speed figures quoted in §4 come from a **different, smaller index of 61,524 files** — the one the
search work was benchmarked against — and not from the 112,634-file folder above. On that index, the
whole catalogue is loaded into the window in one go and turned into its searchable form in about
24 ms; each keystroke rescans it in 1–4 ms; and the fourth search stage waits 150 ms after you stop
typing before asking the catalogue. Nothing here has been re-timed at 112,634 files.

---

## 9. Not built yet

Honest inventory. These are the absences a user would notice, as distinct from internal tidying.

**The five that are most often assumed to exist:**

1. **No searching inside file contents.** Names, paths and extracted descriptors only (§4).
2. **No PDF text and no PDF preview.** A PDF is a name, a size and a date. *1,344 of them in the
   reference folder.*
3. **No Excel support.** No sheet names, no headers, no row counts. *111 files.*
4. **No image dimensions** for any raster format — no width, height, colour depth or EXIF. The only
   image reader handles SVG *text*, not pixels.
5. **Lens cannot move, rename, copy, delete or trash anything, and cannot make a folder.** There is no
   file-modifying command in the app at all. Dragging a file out to another application works, and
   that is the system doing the move. There is also no undo, because there is nothing to undo.

**And two that are often assumed to be missing, but are not** — see §3 for exactly where each stops:

- **CSV and TSV column names *are* indexed and searchable**, from a header read capped at 1 MB, with
  an exact row count up to 100 MB (25 MB gzipped). Cell values are never read, and the column list can
  be switched off at index time to shrink the catalogue.
- **JSON, YAML and TOML keys *are* indexed and searchable** — the **top level only**, never nested
  keys, and never any value. Files over 5 MB are not opened. TOML needs Python 3.11 or newer.

**Previews and display**

- Only images and markdown render. No thumbnails (a large figure is decoded at full size), no
  syntax-highlighted code, no rendered notebooks, no Quick Look with the spacebar.
- Markdown preview truncates silently at 512 KB, strips raw HTML without a trace, loses code-block
  language labels and task-list checkboxes, and cannot load images.
- `.rst` is rendered with a markdown parser, which is the wrong parser for it.
- The information panel shows dashes where a spreadsheet's rows × columns would go, and never shows
  the column list even though the names are indexed and searchable.
- The Health view's "broken" and "errors" tables are a placeholder note rather than real tables.

**Browsing and search**

- You can only browse folders that have been indexed; the tree is read from the catalogue, never live
  from the disk.
- The whole index is loaded into the window at once — there is no per-folder lazy loading, and opening
  a very large folder is budget-capped (§4).
- No column view, no path breadcrumbs, no back and forward, no multi-select, no batch actions, no
  double-click to open (a single click inspects; open is in the information panel).
- No Finder tags, no AirDrop or share sheet, no saved searches, and no searching outside the folders
  you have indexed.
- Network volumes and iCloud Drive are explicitly out of scope.

**File types with no reader at all**

`.pdf`, `.png`, `.jpg`, `.jpeg`, `.xlsx`, `.txt`, `.rst`, `.ini`, `.cpp`, `.c`, `.loom`, `.pkl`,
`.pt`, `.pth`, `.joblib`, `.rds`, `.onnx`, `.log`, `.out`, `.err`, `.gz`, `.tgz`, `.zip`, `.tar`,
`.docx`, `.pptx`, and files with no extension. They are catalogued and findable by name; nothing is
read from inside them. No archive is listed — you cannot see what is inside a `.zip` or a `.tar`
(the one exception is `.npz`, whose members are enumerated).

**Live index**

- The watch does not re-arm after a drive is unplugged and replugged, and nothing indicates that it
  has stopped.
- A second running copy of Lens is silently read-only.
- Behaviour across sleep and wake is untested, and the cost of the full reconcile Lens runs at startup
  has not been measured.

**Platform**

- **macOS only.** Not a deferral: Reveal in Finder and Open are the system's own commands and are not
  guarded for anything else, the window treatment is macOS-specific, and no Windows or Linux build
  target is configured. The Python crawler on its own is portable; the app is not.

---

## 10. Where Lens keeps its own files

Nothing is hidden, and nothing is anywhere surprising.

**Inside the folder you indexed** — a folder named `_repo_index`. It holds the catalogue database and
its companions: the same catalogue as JSON and as one-record-per-line JSON, a standalone browsable
HTML version, a short summary for machine readers, the lineage graph, and a small lock file that stops
two writers from working on the catalogue at once. On the 1.40 TB reference folder this comes to about
0.6 GB. Deleting it costs you the catalogue and nothing else; the next index rebuilds it from scratch.

**In your home Library** — `~/Library/Application Support/com.declan.lens/`. It holds the list of
folders you have registered and which one was last open, an operation log per folder, your saved
Python choice, and an empty placeholder catalogue that Lens opens when the real one cannot be reached
(so the window always appears, even with an unplugged drive, rather than failing to launch).

**In the window's own storage** — two preferences: whether figure text is folded into search, and
whether live updates are held.

**Nowhere else.** No scattered temporary directories, and no telemetry — the window is not allowed to
reach the network at all.
