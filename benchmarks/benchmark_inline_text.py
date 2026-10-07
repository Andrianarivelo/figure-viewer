# ruff: noqa: E402
"""Benchmark: in-place text editing (new ``textedit``) vs the previous
redact-and-reinsert HTML path (``reflow.commit_edit``, the editor used up to
commit 1736e6b).

Questions, answered on generated fixtures only (no user file is touched):

1. Position fidelity: after editing a block, how far does its first line
   move? (|dx| + |dy| of the first baseline origin, PDF points.)
2. Rewrite fidelity: rewriting a block with UNCHANGED text should be
   invisible. Metric: mean absolute pixel difference of the block region.
3. Background damage: fraction of pure-white pixels inside the block region
   after an edit, on a light-blue panel (white = an opaque box was painted).
4. Font fidelity: fraction of edited spans whose font family equals the
   original family.
5. Cost: commit time per edit (ms) and, for the new editor, the live
   preview latency per keystroke (layout + MuPDF render).

Statistics: paired per block. Normality of paired differences is checked
with Shapiro-Wilk; the Wilcoxon signed-rank test is used throughout (robust
to the non-normal, often zero-inflated differences), with the matched-pairs
rank-biserial correlation as effect size and Holm correction across the
metrics. All parameters and figure styling are configurable below.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from figureviewer import docs as D
from figureviewer import pdfedit as PE
from figureviewer import reflow as R
from figureviewer import textedit as TE

# --------------------------------------------------------------- parameters
FONT_FILES = {                     # family -> Windows font file (skipped if absent)
    "Calibri": r"C:\Windows\Fonts\calibri.ttf",
    "Arial": r"C:\Windows\Fonts\arial.ttf",
    "Times New Roman": r"C:\Windows\Fonts\times.ttf",
    "Georgia": r"C:\Windows\Fonts\georgia.ttf",
    "Helvetica": None,             # PDF base-14, always available
}
SIZES = (10.0, 14.0)
BLOCK_KINDS = ("paragraph", "label", "title")
PANEL_RGB = (0.86, 0.93, 1.0)
RENDER_DPI = 144
PREVIEW_ZOOM = 2.0
PREVIEW_REPEATS = 25
EDIT_WORD = "modified"
PARAGRAPH = ("Neurons in layer two showed a robust increase in firing rate after "
             "the stimulus onset and the effect persisted across sessions and animals.")
LABEL = "Figure 3: population response"
TITLE = "Results overview"
OUT_DIR = Path(__file__).resolve().parent / "results" / "inline_text"
SEED = 7

# figure styling
FIG_SIZE = (7.2, 5.0)
FONT_SIZE = 8
BAR_WIDTH = 0.28                   # thin bars, by request
COLORS = {"previous": "#9aa5b1", "new": "#2f6fdf"}
DPI_EXPORT = 300


# ------------------------------------------------------------------ fixtures
def build_fixture(path: Path) -> list[dict]:
    """One block per page on a light-blue panel; returns the block specs."""
    fitz = D.fitz
    doc = fitz.open()
    specs = []
    for family, fontfile in FONT_FILES.items():
        if fontfile is not None and not Path(fontfile).is_file():
            continue
        for size in SIZES:
            for kind in BLOCK_KINDS:
                page = doc.new_page(width=595, height=842)
                page.draw_rect(fitz.Rect(40, 40, 555, 500), color=None, fill=PANEL_RGB)
                if fontfile:
                    page.insert_font(fontname="F0", fontfile=fontfile)
                    name = "F0"
                else:
                    name = "helv"
                if kind == "paragraph":
                    page.insert_textbox(fitz.Rect(80, 100, 380, 300), PARAGRAPH,
                                        fontname=name, fontsize=size, lineheight=1.25,
                                        color=(0.15, 0.15, 0.2))
                elif kind == "label":
                    page.insert_text((80, 200), LABEL, fontname=name, fontsize=size)
                else:
                    page.insert_textbox(fitz.Rect(80, 180, 515, 260), TITLE,
                                        fontname=name, fontsize=size * 1.6, align=1,
                                        color=(0.1, 0.2, 0.45))
                specs.append({"page": len(specs), "family": family, "size": size,
                              "kind": kind})
    doc.subset_fonts()
    doc.save(str(path))
    doc.close()
    return specs


def entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


# ------------------------------------------------------------------ metrics
def spans_of(session, page):
    with session.document._lock:
        raw = session.document._doc[page].get_text("dict")
    out = []
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip():
                    out.append(span)
    return sorted(out, key=lambda s: (round(s["origin"][1], 1), s["origin"][0]))


FAMILY_REGISTRY = TE.FontRegistry()


def family_token(font_name: str) -> str:
    """Family a PDF font name resolves to (PostScript names, subset prefixes
    and MuPDF's Nimbus base-14 clones all normalised by the real matcher)."""
    return FAMILY_REGISTRY.match_pdf_font(font_name)[0]


def region_pixels(session, page, bbox) -> np.ndarray:
    fitz = D.fitz
    rect = fitz.Rect(*bbox) + (-2, -2, 2, 2)
    with session.document._lock:
        pix = session.document._doc[page].get_pixmap(clip=rect, dpi=RENDER_DPI,
                                                     alpha=False, colorspace=fitz.csRGB)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)


def edited_text(text: str) -> str:
    words = text.split(" ")
    return " ".join([words[0], EDIT_WORD] + words[1:])


def run_previous(session, page, block, new_text):
    t0 = time.perf_counter()
    R.commit_edit(session, page, block, new_text)
    return (time.perf_counter() - t0) * 1000.0


def run_new(session, page, block, new_text, registry):
    t0 = time.perf_counter()
    seed = TE.seed_from_block(block, registry)
    if new_text == seed.text:
        text, styles = seed.text, seed.styles
    else:
        # The same edit a user makes: insert the word after the first word.
        cut = seed.text.find(" ")
        insert = " " + EDIT_WORD
        text = seed.text[:cut] + insert + seed.text[cut:]
        styles = seed.styles[:cut] + [seed.styles[cut - 1]] * len(insert) + seed.styles[cut:]
    anchor_x = seed.origin[0]
    if seed.wrap_width is None and seed.align == "center":
        anchor_x = (block.bbox[0] + block.bbox[2]) / 2.0
    elif seed.wrap_width is None and seed.align == "right":
        anchor_x = block.bbox[2]
    layout = TE.layout_rich(text, styles, registry, align=seed.align,
                            line_height=seed.line_height, wrap_width=seed.wrap_width,
                            fixed_baselines=seed.fixed_baselines,
                            indents=seed.indents if seed.align == "left" else None)
    origin = TE.anchored_origin(anchor_x, seed.origin[1], layout, seed.align,
                                seed.wrap_width)
    TE.commit_text(session, page, layout, origin, erase_rects=seed.erase_rects,
                   label="Edit text")
    return (time.perf_counter() - t0) * 1000.0


def preview_latency(block, registry) -> float:
    seed = TE.seed_from_block(block, registry)
    renderer = TE.PreviewRenderer()
    timings = []
    try:
        for _ in range(PREVIEW_REPEATS):
            t0 = time.perf_counter()
            layout = TE.layout_rich(seed.text, seed.styles, registry, align=seed.align,
                                    line_height=seed.line_height,
                                    wrap_width=seed.wrap_width,
                                    fixed_baselines=seed.fixed_baselines)
            renderer.render(layout, PREVIEW_ZOOM)
            timings.append((time.perf_counter() - t0) * 1000.0)
    finally:
        renderer.close()
    return statistics.median(timings)


def measure(method, pdf, spec, registry, *, noop: bool) -> dict:
    session = PE.EditSession(entry(pdf))
    try:
        page = spec["page"]
        block = [b for b in R.blocks(session.document, page) if b.text.strip()][0]
        before_spans = spans_of(session, page)
        before_pix = region_pixels(session, page, block.bbox)
        new_text = block.text.replace("\xa0", " ") if noop else edited_text(
            block.text.replace("\xa0", " ").replace("\n", " "))
        if method == "previous":
            ms = run_previous(session, page, block, new_text)
        else:
            ms = run_new(session, page, block, new_text if not noop else
                         TE.seed_from_block(block, registry).text, registry)
        after_spans = spans_of(session, page)
        after_pix = region_pixels(session, page, block.bbox)
        o0 = before_spans[0]["origin"]
        o1 = after_spans[0]["origin"]
        family0 = family_token(before_spans[0]["font"])
        same_font = [family_token(s["font"]) == family0 for s in after_spans]
        size0 = before_spans[0]["size"]
        white = np.all(after_pix == 255, axis=2).mean() - np.all(before_pix == 255, axis=2).mean()
        h = min(before_pix.shape[0], after_pix.shape[0])
        w = min(before_pix.shape[1], after_pix.shape[1])
        diff = np.abs(after_pix[:h, :w].astype(int) - before_pix[:h, :w].astype(int)).mean()
        return {"origin_shift_pt": abs(o1[0] - o0[0]) + abs(o1[1] - o0[1]),
                "pixel_diff": float(diff), "white_fraction": float(max(0.0, white)),
                "font_kept": float(np.mean(same_font)),
                "size_shift_pt": abs(after_spans[0]["size"] - size0),
                "commit_ms": ms}
    finally:
        session.close()


# --------------------------------------------------------------- statistics
def paired_stats(prev: np.ndarray, new: np.ndarray) -> dict:
    d = new - prev
    nonzero = d[d != 0]
    shapiro_p = float(stats.shapiro(d).pvalue) if len(np.unique(d)) > 2 else float("nan")
    if len(nonzero) == 0:
        return {"shapiro_p": shapiro_p, "wilcoxon_W": float("nan"), "p": 1.0,
                "rank_biserial": 0.0, "n_nonzero": 0}
    res = stats.wilcoxon(prev, new, zero_method="wilcox")
    ranks = stats.rankdata(np.abs(nonzero))
    r_plus = ranks[nonzero > 0].sum()
    r_minus = ranks[nonzero < 0].sum()
    rb = (r_plus - r_minus) / ranks.sum()
    return {"shapiro_p": shapiro_p, "wilcoxon_W": float(res.statistic),
            "p": float(res.pvalue), "rank_biserial": float(rb),
            "n_nonzero": int(len(nonzero))}


def holm(pvalues: list[float]) -> list[float]:
    order = np.argsort(pvalues)
    m = len(pvalues)
    adjusted = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[idx]))
        adjusted[idx] = running
    return adjusted


def stars(p: float) -> str:
    return "***" if p < 1e-3 else "**" if p < 1e-2 else "*" if p < 0.05 else "n.s."


# -------------------------------------------------------------------- main
def main() -> None:
    np.random.seed(SEED)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    registry = TE.FontRegistry()
    registry.scan()
    FAMILY_REGISTRY.scan()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "bench.pdf"
        specs = build_fixture(pdf)
        session = PE.EditSession(entry(pdf))
        latency = {}
        try:
            for spec in specs:
                block = [b for b in R.blocks(session.document, spec["page"])
                         if b.text.strip()][0]
                latency[spec["page"]] = preview_latency(block, registry)
        finally:
            session.close()
        for spec in specs:
            for method in ("previous", "new"):
                edit = measure(method, pdf, spec, registry, noop=False)
                noop = measure(method, pdf, spec, registry, noop=True)
                rows.append({**spec, "method": method,
                             "origin_shift_pt": edit["origin_shift_pt"],
                             "white_fraction": edit["white_fraction"],
                             "font_kept": edit["font_kept"],
                             "size_shift_pt": edit["size_shift_pt"],
                             "commit_ms": edit["commit_ms"],
                             "noop_pixel_diff": noop["pixel_diff"],
                             "noop_origin_shift_pt": noop["origin_shift_pt"],
                             "preview_ms": latency[spec["page"]] if method == "new" else ""})

    raw_path = OUT_DIR / "inline_text_raw.csv"
    with raw_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    metrics = [("origin_shift_pt", "First-line shift after edit (pt)"),
               ("noop_pixel_diff", "Unchanged rewrite: pixel difference"),
               ("white_fraction", "White-box pixels after edit (fraction)"),
               ("font_kept", "Spans keeping the original font (fraction)"),
               ("size_shift_pt", "Font size change (pt)"),
               ("commit_ms", "Commit time (ms)")]
    by = {m: {"previous": [], "new": []} for m, _ in metrics}
    for row in rows:
        for m, _ in metrics:
            by[m][row["method"]].append(float(row[m]))
    stat_rows = []
    for m, label in metrics:
        prev = np.array(by[m]["previous"])
        new = np.array(by[m]["new"])
        st = paired_stats(prev, new)
        stat_rows.append({"metric": m, "label": label, "n_pairs": len(prev),
                          "previous_median": float(np.median(prev)),
                          "previous_iqr": float(np.subtract(*np.percentile(prev, [75, 25]))),
                          "new_median": float(np.median(new)),
                          "new_iqr": float(np.subtract(*np.percentile(new, [75, 25]))),
                          **st})
    adjusted = holm([r["p"] for r in stat_rows])
    for row, p_adj in zip(stat_rows, adjusted):
        row["p_holm"] = p_adj
        row["significance"] = stars(p_adj)
    stats_path = OUT_DIR / "inline_text_statistics.csv"
    with stats_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(stat_rows[0]))
        writer.writeheader()
        writer.writerows(stat_rows)
    preview = [float(r["preview_ms"]) for r in rows if r["method"] == "new"]

    # ------------------------------------------------------------ figure
    plt.rcParams.update({"font.size": FONT_SIZE, "axes.spines.top": False,
                         "axes.spines.right": False, "font.family": "DejaVu Sans"})
    fig, axes = plt.subplots(2, 3, figsize=FIG_SIZE, constrained_layout=True)
    panels = [("origin_shift_pt", "First-line shift after edit (pt)"),
              ("noop_pixel_diff", "Unchanged rewrite: pixel diff (0-255)"),
              ("font_kept", "Spans keeping original font"),
              ("commit_ms", "Commit time per edit (ms)"),
              ("preview_ms", "Live preview per keystroke (ms)")]
    rng = np.random.default_rng(SEED)
    for ax, (m, label), letter in zip(axes.flat, panels, "ABCDE"):
        ax.set_title(f"{letter}  {label}", loc="left", fontsize=FONT_SIZE, pad=4)
        if m == "preview_ms":
            ax.bar([0], [np.median(preview)], width=BAR_WIDTH, color=COLORS["new"])
            ax.scatter(rng.normal(0, 0.04, len(preview)), preview, s=6,
                       color="black", alpha=0.5, linewidths=0, zorder=3)
            ax.set_xticks([0], ["new"])
            ax.set_xlim(-0.6, 0.6)
            ax.axhline(16.7, color="#c0392b", lw=0.6, ls="--")
            ax.text(0.55, 17.2, "60 fps frame budget", ha="right", va="bottom",
                    fontsize=FONT_SIZE - 1, color="#c0392b")
            ax.set_ylim(0, max(20.0, max(preview) * 1.2))
            continue
        prev = np.array(by[m]["previous"])
        new = np.array(by[m]["new"])
        for a, b in zip(prev, new):
            ax.plot([0, 1], [a, b], color="#555555", lw=0.25, alpha=0.35, zorder=2)
        for i, (name, values) in enumerate((("previous", prev), ("new", new))):
            ax.bar([i], [np.median(values)], width=BAR_WIDTH, color=COLORS[name])
            ax.scatter(i + rng.normal(0, 0.04, len(values)), values, s=6,
                       color="black", alpha=0.5, linewidths=0, zorder=3)
        ax.set_xticks([0, 1], ["previous", "new"])
        ax.set_xlim(-0.6, 1.6)
        top = max(float(np.max(prev)), float(np.max(new)), 1e-6)
        ax.set_ylim(0, top * 1.3)
        row = next(r for r in stat_rows if r["metric"] == m)
        ax.plot([0, 1], [top * 1.12] * 2, color="black", lw=0.6)
        ax.text(0.5, top * 1.14, f"{row['significance']}  p(Holm)={row['p_holm']:.1e}",
                ha="center", va="bottom", fontsize=FONT_SIZE - 1)
    # F: null results stated as text rather than empty axes.
    ax = axes.flat[5]
    ax.axis("off")
    lines = ["F  Unchanged by the redesign", ""]
    for m, label in (("white_fraction", "White-box pixels"),
                     ("size_shift_pt", "Font-size change")):
        row = next(r for r in stat_rows if r["metric"] == m)
        lines.append(f"{label}: previous {row['previous_median']:.3g},"
                     f" new {row['new_median']:.3g}")
    lines += ["", "Bars: medians; dots: blocks;", "lines join paired blocks.",
              "Wilcoxon signed-rank, Holm-adjusted."]
    ax.text(0.0, 1.0, "\n".join(lines), va="top", ha="left", fontsize=FONT_SIZE - 0.5,
            transform=ax.transAxes)
    fig.suptitle(f"In-place text editing: previous HTML reinsert vs new engine "
                 f"(n = {len(preview)} paired blocks)", fontsize=FONT_SIZE + 1)
    for ext in ("png", "pdf", "svg"):
        fig.savefig(OUT_DIR / f"inline_text_benchmark.{ext}", dpi=DPI_EXPORT)
    plt.close(fig)

    manifest = {"fixture_blocks": len(preview), "fonts": list(FONT_FILES),
                "sizes": SIZES, "kinds": BLOCK_KINDS, "render_dpi": RENDER_DPI,
                "preview_zoom": PREVIEW_ZOOM, "preview_repeats": PREVIEW_REPEATS,
                "pymupdf": D.fitz.VersionBind, "python": sys.version.split()[0],
                "preview_median_ms": float(np.median(preview)),
                "preview_max_ms": float(np.max(preview))}
    (OUT_DIR / "inline_text_manifest.json").write_text(json.dumps(manifest, indent=2))
    for row in stat_rows:
        print(f"{row['metric']:>18}: previous {row['previous_median']:.4g} "
              f"new {row['new_median']:.4g}  Wilcoxon p_holm={row['p_holm']:.2e} "
              f"rb={row['rank_biserial']:+.2f} shapiro_p={row['shapiro_p']:.2g}")
    print("preview median ms", manifest["preview_median_ms"], "max", manifest["preview_max_ms"])


if __name__ == "__main__":
    main()
