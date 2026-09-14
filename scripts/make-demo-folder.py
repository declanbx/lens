#!/usr/bin/env python3
"""Build the neutral demo folder the README screenshots are recorded against.

Everything here is synthetic. No file name, folder name, column header or figure label
comes from a real project — the point of this script is that the screenshots in a public
repository can show a realistic working folder without publishing one.

    python3 scripts/make-demo-folder.py ~/lens-demo-folder

The shape is chosen to exercise every reader Lens has: CSV and TSV headers, Excel sheet
names, single-cell annotation names, Python and R definitions, markdown headings, and —
the differentiator — SVG figures whose axis labels carry gene symbols that appear nowhere
in the path. HMGCR is the worked example the README quotes: a handful of files carry it in
a name or a column, and many more carry it only as text drawn inside a figure.
"""

from __future__ import annotations

import csv
import random
import shutil
import sys
from pathlib import Path

random.seed(11)

# The worked example. It is in a public gene-symbol vocabulary, not a project's private data.
HERO = "HMGCR"
OTHER_GENES = [
    "SQLE", "INSIG1", "LDLR", "FDFT1", "MSMO1", "DHCR7", "SREBF2", "ACAT2",
    "IDI1", "MVD", "LSS", "CYP51A1", "NSDHL", "EBP", "SC5D", "FDPS",
]
CELL_TYPES = ["neuron_a", "neuron_b", "neuron_c", "glia_a", "glia_b", "glia_c",
              "progenitor", "dividing", "mixed"]
CONDITIONS = ["control", "low_dose", "high_dose"]


# ── figures ────────────────────────────────────────────────────────────────────────────
def volcano_svg(title: str, labelled: list[str]) -> str:
    """A volcano plot whose point labels are real <text> nodes — the searchable part."""
    pts = []
    for i in range(220):
        x, y = random.gauss(0, 1.1), abs(random.gauss(0, 1.0))
        cx, cy = 300 + x * 95, 330 - y * 78
        hue = "#d6d6d6" if abs(x) < 0.9 or y < 0.9 else ("#d97757" if x > 0 else "#5a7fb8")
        pts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2.6" fill="{hue}" opacity="0.8"/>')
    labels = []
    for i, g in enumerate(labelled):
        lx, ly = 120 + (i % 4) * 130, 90 + (i // 4) * 26
        labels.append(f'<text x="{lx}" y="{ly}" font-size="13" fill="#222">{g}</text>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="620" height="400" viewBox="0 0 620 400">
<rect width="620" height="400" fill="#ffffff"/>
<text x="310" y="26" font-size="15" text-anchor="middle" fill="#111">{title}</text>
{''.join(pts)}
{''.join(labels)}
<line x1="60" y1="340" x2="560" y2="340" stroke="#444"/>
<line x1="60" y1="60" x2="60" y2="340" stroke="#444"/>
<text x="310" y="378" font-size="12" text-anchor="middle" fill="#333">log2 fold change</text>
<text x="24" y="200" font-size="12" text-anchor="middle" fill="#333" transform="rotate(-90 24 200)">-log10 adjusted p</text>
</svg>"""


def bar_svg(title: str, genes: list[str]) -> str:
    """A bar chart whose category axis IS the gene list — labels only, never in the path."""
    bars, ticks = [], []
    for i, g in enumerate(genes):
        h = random.uniform(30, 210)
        x = 90 + i * 46
        bars.append(f'<rect x="{x}" y="{300 - h:.1f}" width="30" height="{h:.1f}" fill="#542788" opacity="0.85"/>')
        ticks.append(f'<text x="{x + 15}" y="322" font-size="11" text-anchor="end" fill="#222" '
                     f'transform="rotate(-40 {x + 15} 322)">{g}</text>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="620" height="380" viewBox="0 0 620 380">
<rect width="620" height="380" fill="#ffffff"/>
<text x="310" y="26" font-size="15" text-anchor="middle" fill="#111">{title}</text>
{''.join(bars)}
{''.join(ticks)}
<line x1="80" y1="300" x2="580" y2="300" stroke="#444"/>
<text x="34" y="170" font-size="12" text-anchor="middle" fill="#333" transform="rotate(-90 34 170)">mean expression</text>
</svg>"""


# ── tabular ────────────────────────────────────────────────────────────────────────────
def write_csv(path: Path, header: list[str], rows: int) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t" if path.suffix == ".tsv" else ",")
        w.writerow(header)
        for _ in range(rows):
            w.writerow([f"{random.uniform(-3, 3):.4f}" if i else random.choice(OTHER_GENES + [HERO])
                        for i in range(len(header))])


def write_xlsx(path: Path, sheets: dict[str, list[str]]) -> None:
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for name, header in sheets.items():
        ws = wb.create_sheet(name[:31])
        ws.append(header)
        for _ in range(40):
            ws.append([random.choice(OTHER_GENES)] + [round(random.uniform(-2, 2), 3)
                                                      for _ in header[1:]])
    wb.save(path)


def write_h5ad(path: Path, n_cells: int, n_genes: int) -> None:
    """A minimal AnnData-on-disk layout: Lens reads the annotation NAMES, never the values."""
    import h5py
    import numpy as np
    with h5py.File(path, "w") as f:
        f.attrs["encoding-type"] = "anndata"
        f.attrs["encoding-version"] = "0.1.0"
        f.create_dataset("X", data=np.random.rand(n_cells, n_genes).astype("float32"))
        obs = f.create_group("obs")
        obs.attrs["encoding-type"] = "dataframe"
        obs.attrs["_index"] = "cell_id"
        obs.attrs["column-order"] = ["cell_type", "condition", "batch", "n_counts", "pct_mito"]
        obs.create_dataset("cell_id", data=np.array([f"cell_{i}" for i in range(n_cells)], dtype="S12"))
        obs.create_dataset("cell_type", data=np.array([random.choice(CELL_TYPES).encode() for _ in range(n_cells)]))
        obs.create_dataset("condition", data=np.array([random.choice(CONDITIONS).encode() for _ in range(n_cells)]))
        obs.create_dataset("batch", data=np.random.randint(1, 3, n_cells))
        obs.create_dataset("n_counts", data=np.random.randint(900, 9000, n_cells))
        obs.create_dataset("pct_mito", data=np.random.rand(n_cells).astype("float32"))
        var = f.create_group("var")
        var.attrs["encoding-type"] = "dataframe"
        var.attrs["_index"] = "gene_symbol"
        var.attrs["column-order"] = ["highly_variable", "mean_expression"]
        var.create_dataset("gene_symbol", data=np.array([f"GENE{i:05d}".encode() for i in range(n_genes)]))
        var.create_dataset("highly_variable", data=np.random.randint(0, 2, n_genes))
        var.create_dataset("mean_expression", data=np.random.rand(n_genes).astype("float32"))
        obsm = f.create_group("obsm")
        obsm.create_dataset("X_umap", data=np.random.rand(n_cells, 2).astype("float32"))
        obsm.create_dataset("X_pca", data=np.random.rand(n_cells, 20).astype("float32"))


# ── the tree ───────────────────────────────────────────────────────────────────────────
def build(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    for d in ["figures/volcano", "figures/panels", "figures/qc", "results/differential",
              "results/summaries", "data/matrices", "data/tables", "scripts/python",
              "scripts/r", "notes", "logs"]:
        (root / d).mkdir(parents=True, exist_ok=True)

    # Figures. The hero symbol is DRAWN in most of them and NAMED in none of them —
    # that gap is what the "figure text" checkbox closes.
    for i in range(186):
        genes = random.sample(OTHER_GENES, 6)
        if i % 3 != 2:
            genes.insert(random.randrange(len(genes)), HERO)
        (root / "figures/volcano" / f"volcano_{CELL_TYPES[i % len(CELL_TYPES)]}_{CONDITIONS[i % 3]}_{i:02d}.svg").write_text(
            volcano_svg(f"{CELL_TYPES[i % len(CELL_TYPES)]} · {CONDITIONS[i % 3]}", genes))
    for i in range(124):
        genes = random.sample(OTHER_GENES, 8)
        if i % 2 == 0:
            genes[random.randrange(len(genes))] = HERO
        (root / "figures/panels" / f"panel_{chr(97 + i % 26)}_{i:02d}.svg").write_text(
            bar_svg(f"panel {chr(97 + i % 26)} — sterol enzymes", genes))
    for i in range(58):
        (root / "figures/qc" / f"qc_depth_vs_genes_{i:02d}.svg").write_text(
            volcano_svg("QC: depth against genes detected", random.sample(OTHER_GENES, 5)))

    # Differential-expression tables: headers are what search reads.
    de_header = ["gene", "log2FC", "lfcSE", "pvalue", "padj", "baseMean"]
    for ct in CELL_TYPES:
        for cond in CONDITIONS[1:]:
            write_csv(root / "results/differential" / f"de_{ct}_{cond}_vs_control.csv", de_header, 400)
    for ct in CELL_TYPES:
        write_csv(root / "results/summaries" / f"summary_{ct}.tsv",
                  ["gene", "mean_control", "mean_low", "mean_high", "spearman_rho", "q_value"], 180)
    # A few files that DO carry the hero symbol in the name, so the two routes differ visibly.
    for cond in CONDITIONS[1:]:
        write_csv(root / "results/differential" / f"pergene_{HERO}_{cond}.csv",
                  ["cell_type", "log2FC", "padj", "n_cells"], 60)
    write_csv(root / "results/summaries" / f"{HERO}_dose_response.csv",
              ["dose", "mean_expression", "sem", "n"], 12)

    write_xlsx(root / "data/tables/qc_metrics.xlsx",
               {"per_sample": ["sample", "n_cells", "median_genes", "pct_mito"],
                "per_cell_type": ["cell_type", "n_cells", "mean_depth"],
                "thresholds": ["metric", "lower", "upper"]})
    write_xlsx(root / "data/tables/sterol_panel.xlsx",
               {"enzymes": ["gene", "pathway_step", "in_panel"],
                "dose_response": ["gene", "control", "low_dose", "high_dose"]})

    for i, ct in enumerate(CELL_TYPES[:4]):
        write_h5ad(root / "data/matrices" / f"counts_{ct}.h5ad", 400 + i * 90, 300)

    (root / "scripts/python/differential_expression.py").write_text(
        '"""Fit the per-cell-type contrasts and write one table per comparison."""\n\n'
        "import pandas as pd\n\n\n"
        "def load_counts(path):\n    ...\n\n\n"
        "def fit_contrast(counts, design):\n    ...\n\n\n"
        "class ContrastResult:\n    pass\n")
    (root / "scripts/python/make_figures.py").write_text(
        '"""Draw the volcano and panel figures from the tables in results/."""\n\n'
        "import matplotlib.pyplot as plt\n\n\n"
        "def volcano(table, labels):\n    ...\n\n\n"
        "def panel_grid(tables):\n    ...\n")
    (root / "scripts/r/quality_control.R").write_text(
        "# Per-sample thresholds, written to data/tables/qc_metrics.xlsx\n"
        "compute_thresholds <- function(counts) NULL\n"
        "flag_outliers <- function(metrics) NULL\n")
    (root / "scripts/r/dose_model.R").write_text(
        "# Dose-response fit used by the summaries\n"
        "fit_dose <- function(expr, dose) NULL\n")

    (root / "notes/README.md").write_text(
        "# Demo folder\n\nA synthetic folder used only for the screenshots in the Lens README.\n"
        "Nothing here is real data.\n")
    (root / "notes/analysis_plan.md").write_text(
        "# Analysis plan\n\nThree doses, six cell types, one contrast per dose against control.\n")
    (root / "notes/figure_log.md").write_text(
        "# Figure log\n\nWhich panel came from which table, and what changed between drafts.\n")
    for i in range(24):
        (root / "logs" / f"run_{i:02d}.log").write_text("\n".join(f"step {j} ok" for j in range(50)))

    n = sum(1 for p in root.rglob("*") if p.is_file())
    print(f"{n} files in {root}")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "~/lens-demo-folder").expanduser())
