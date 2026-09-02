# ruff: noqa: E402
"""Benchmark the FigView 3.5 PDF studio.

Four questions, each answered with generated fixtures only (no user media is
read or altered):

1. OCR accuracy: does the OpenCV preprocessing pipeline (denoise, deskew,
   threshold) measurably beat the baseline integrated-Tesseract route on
   degraded scans? Metric: character error rate, paired per fixture.
2. Annotation latency: how long from an add-markup command to a repainted
   view region (the user-perceived edit cost)?
3. Save cost: how does the atomic save (serialise + temp + replace) scale
   with document size?
4. Export throughput: pages per second for the text, Markdown, Word, Excel
   and PowerPoint exporters.

Configuration and figure styling are centralized below.
"""
from __future__ import annotations

import csv
import io
import json
import os
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
from figureviewer import ocr as O
from figureviewer import pdfedit as PE
from figureviewer import pdfexport as X

# --------------------------------------------------------------- parameters
# Degradations chosen to resemble a bad flatbed or phone scan: heavy sensor
# noise and real page skew, rendered from a mediocre 150 dpi source. Mild
# degradations were piloted first and Tesseract absorbs them unaided (CER
# near 0 for both routes); the pipeline exists for the hard cases.
OCR_NOISE_SIGMAS = (16, 40)
OCR_ANGLES_DEG = (3.0, 8.0, 12.0)
OCR_REPEATS = 4                 # per condition; conditions x repeats pairs
OCR_DPI = 250
OCR_FIXTURE_RENDER_DPI = 150
ANNOT_REPEATS = 40
SAVE_PAGE_COUNTS = (2, 10, 40)
SAVE_REPEATS = 8
EXPORT_PAGES = 10
EXPORT_REPEATS = 5
EXPORT_FORMATS = ("text", "markdown", "docx", "xlsx", "pptx")
ALPHA = 0.05
SEED = 20260902

FIGURE_SIZE = (9.4, 6.2)
COLOUR_BASE = "#8b95a5"
COLOUR_NEW = "#35b6a8"
COLOUR_ACCENT = "#5aa7f0"
BAR_WIDTH = 0.32
OUTPUT_DIR = Path(__file__).resolve().parent / "results"
STEM = "pdf_studio_v35"

WORDS = ("neural cortical synaptic dendritic axonal manifold attractor "
         "stimulus response latency spike rate population geometry dynamics "
         "recording session electrode plasticity gradient learning circuit "
         "inhibition excitation balance oscillation theta gamma coupling").split()


def timed(function) -> float:
    started = time.perf_counter_ns()
    function()
    return (time.perf_counter_ns() - started) / 1_000_000


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _sentence(rng: np.random.Generator, count: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(count))


def _entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


# ----------------------------------------------------------------- fixtures
def make_scan_pdf(root: Path, name: str, text: str, *, angle: float,
                  sigma: float, rng: np.random.Generator) -> Path:
    """Render known text, rotate and add noise, save as an image-only PDF."""
    import cv2
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(pymupdf.Rect(60, 80, 540, 700), text, fontsize=12,
                        fontname="helv")
    pix = page.get_pixmap(dpi=OCR_FIXTURE_RENDER_DPI)
    doc.close()
    array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    matrix = cv2.getRotationMatrix2D((pix.width / 2, pix.height / 2),
                                     angle, 1.0)
    rotated = cv2.warpAffine(gray, matrix, (pix.width, pix.height),
                             borderValue=255)
    noisy = np.clip(rotated.astype(np.float64)
                    + rng.normal(0.0, sigma, rotated.shape), 0, 255
                    ).astype(np.uint8)
    ok, png = cv2.imencode(".png", noisy)
    assert ok
    out = pymupdf.open()
    target = out.new_page(width=595, height=842)
    target.insert_image(target.rect, stream=png.tobytes())
    path = root / name
    out.save(str(path))
    out.close()
    return path


def make_content_pdf(path: Path, pages: int, rng: np.random.Generator) -> None:
    """Text plus a table grid plus a small image per page: exporter food."""
    import pymupdf

    doc = pymupdf.open()
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (120, 80), "#35b6a8").save(buffer, "PNG")
    image_bytes = buffer.getvalue()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 70), f"Section {index + 1}", fontsize=16)
        page.insert_textbox(pymupdf.Rect(60, 90, 540, 320),
                            _sentence(rng, 90), fontsize=11)
        for row in range(4):
            page.draw_line((60, 360 + 22 * row), (400, 360 + 22 * row))
        for x in (60, 175, 290, 400):
            page.draw_line((x, 360), (x, 426))
        for row in range(3):
            for column, x in enumerate((66, 181, 296)):
                page.insert_text((x, 376 + 22 * row),
                                 f"v{index}{row}{column}", fontsize = 9)
        page.insert_image(pymupdf.Rect(430, 340, 540, 420),
                          stream=image_bytes)
    doc.save(str(path))
    doc.close()


# ------------------------------------------------------------------- parts
def bench_ocr(root: Path, raw: list[dict], summary: list[dict],
              statistics_rows: list[dict]):
    """Preprocessed vs baseline OCR, paired per degraded fixture."""
    rng = np.random.default_rng(SEED)
    tessdata = O.default_tessdata_root()
    baseline_cer: list[float] = []
    preprocessed_cer: list[float] = []
    per_condition: dict[tuple, list[tuple[float, float]]] = {}
    for sigma in OCR_NOISE_SIGMAS:
        for angle in OCR_ANGLES_DEG:
            for repeat in range(OCR_REPEATS):
                text = _sentence(rng, 60)
                path = make_scan_pdf(root, f"scan_{sigma}_{angle}_{repeat}.pdf",
                                     text, angle=angle, sigma=sigma, rng=rng)
                document = D.VectorDocument(_entry(path), path.read_bytes())
                try:
                    results = {}
                    for label, preprocess_on in (("baseline", False),
                                                 ("preprocessed", True)):
                        words = O.page_ocr(document, 0, lang="eng",
                                           variant="fast",
                                           preprocess_on=preprocess_on,
                                           dpi=OCR_DPI, tessdata_dir=tessdata)
                        hypothesis = " ".join(w.text for w in words)
                        results[label] = O.cer(text, hypothesis)
                        raw.append({"part": "ocr", "noise_sigma": sigma,
                                    "angle_deg": angle, "repeat": repeat + 1,
                                    "route": label,
                                    "cer": results[label]})
                finally:
                    document.close()
                baseline_cer.append(results["baseline"])
                preprocessed_cer.append(results["preprocessed"])
                per_condition.setdefault((sigma, angle), []).append(
                    (results["baseline"], results["preprocessed"]))

    for (sigma, angle), pairs in sorted(per_condition.items()):
        for route, values in (("baseline", [p[0] for p in pairs]),
                              ("preprocessed", [p[1] for p in pairs])):
            summary.append({"part": "ocr", "condition": f"s{sigma}_a{angle}",
                            "method": route,
                            "median": statistics.median(values),
                            "mean": statistics.fmean(values),
                            "stdev": statistics.stdev(values)
                            if len(values) > 1 else "",
                            "n": len(values)})
    differences = [b - p for b, p in zip(baseline_cer, preprocessed_cer)]
    shapiro = stats.shapiro(differences)
    wilcoxon = stats.wilcoxon(baseline_cer, preprocessed_cer,
                              alternative="greater")
    statistics_rows.append({
        "part": "ocr_cer", "pairs": len(differences),
        "shapiro_difference_p": shapiro.pvalue,
        "normal_difference_at_alpha": shapiro.pvalue >= ALPHA,
        "test": "one-sided paired Wilcoxon (baseline CER > preprocessed CER)",
        "statistic": wilcoxon.statistic, "p_value": wilcoxon.pvalue,
        "alpha": ALPHA,
        "median_baseline": statistics.median(baseline_cer),
        "median_new": statistics.median(preprocessed_cer),
    })
    return per_condition, baseline_cer, preprocessed_cer, wilcoxon.pvalue


def bench_annotation(root: Path, raw: list[dict], summary: list[dict]):
    """Add-markup command plus the visible-region repaint that follows."""
    import pymupdf

    path = root / "annot.pdf"
    rng = np.random.default_rng(SEED + 1)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(pymupdf.Rect(60, 80, 540, 700), _sentence(rng, 200),
                        fontsize=11)
    doc.save(str(path))
    doc.close()
    session = PE.EditSession(_entry(path))
    latencies = []
    try:
        width, height = session.document.page_size(0)
        for repeat in range(ANNOT_REPEATS):
            y = 90.0 + (repeat % 30) * 18.0
            quads = [D.fitz.Rect(70, y, 250, y + 12)]

            def one() -> None:
                session.add_markup(0, quads, PE.TYPE_HIGHLIGHT,
                                   color=(1.0, 0.8, 0.2), opacity=0.35)
                session.document.render_region(
                    0, (0, 0, width, height), (900, 1273), fast=True)

            elapsed = timed(one)
            latencies.append(elapsed)
            raw.append({"part": "annotation", "repeat": repeat + 1,
                        "elapsed_ms": elapsed})
    finally:
        session.close()
    summary.append({"part": "annotation", "condition": "highlight+repaint",
                    "method": "3.5 edit session",
                    "median": statistics.median(latencies),
                    "mean": statistics.fmean(latencies),
                    "stdev": statistics.stdev(latencies),
                    "n": len(latencies)})
    return latencies


def bench_save(root: Path, raw: list[dict], summary: list[dict]):
    """Atomic save cost (serialise + temp + fsync + replace) by page count."""
    rng = np.random.default_rng(SEED + 2)
    results = {}
    for pages in SAVE_PAGE_COUNTS:
        path = root / f"save_{pages}.pdf"
        make_content_pdf(path, pages, rng)
        session = PE.EditSession(_entry(path))
        session.add_note(0, (300, 100), "benchmark")
        times = []
        try:
            target = root / f"save_{pages}_out.pdf"
            for repeat in range(SAVE_REPEATS):
                def one() -> None:
                    data = session.save_bytes()
                    tmp = target.with_suffix(".tmp")
                    with open(tmp, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, target)

                elapsed = timed(one)
                times.append(elapsed)
                raw.append({"part": "save", "pages": pages,
                            "repeat": repeat + 1, "elapsed_ms": elapsed})
        finally:
            session.close()
        summary.append({"part": "save", "condition": f"{pages} pages",
                        "method": "atomic save",
                        "median": statistics.median(times),
                        "mean": statistics.fmean(times),
                        "stdev": statistics.stdev(times), "n": len(times)})
        results[pages] = times
    return results


def bench_export(root: Path, raw: list[dict], summary: list[dict]):
    """Pages per second for each exporter on one content-rich document."""
    rng = np.random.default_rng(SEED + 3)
    path = root / "export.pdf"
    make_content_pdf(path, EXPORT_PAGES, rng)
    data = path.read_bytes()
    results = {}
    for name in EXPORT_FORMATS:
        available, hint = X.FORMATS[name].probe()
        if not available:
            summary.append({"part": "export", "condition": name,
                            "method": "unavailable", "median": float("nan"),
                            "mean": float("nan"), "n": 0})
            continue
        exporter = X.EXPORTERS[name]
        suffix = X.FORMATS[name].suffix or ""
        times = []
        for repeat in range(EXPORT_REPEATS):
            target = root / f"export_{name}_{repeat}{suffix}"
            elapsed = timed(lambda t=target: exporter(data, t))
            times.append(elapsed)
            raw.append({"part": "export", "format": name,
                        "repeat": repeat + 1, "elapsed_ms": elapsed,
                        "pages_per_s": EXPORT_PAGES / (elapsed / 1000.0)})
        results[name] = times
        summary.append({"part": "export", "condition": name,
                        "method": "3.5 exporter",
                        "median": statistics.median(times),
                        "mean": statistics.fmean(times),
                        "stdev": statistics.stdev(times), "n": len(times)})
        _ = hint
    return results


# ------------------------------------------------------------------ figure
def build_figure(per_condition, baseline_cer, preprocessed_cer, ocr_p,
                 annot_latencies, save_results, export_results) -> None:
    plt.rcParams.update({
        "figure.dpi": 120, "font.size": 8.5, "axes.titlesize": 9.5,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.alpha": 0.25, "grid.linewidth": 0.5,
    })
    figure, axes = plt.subplots(2, 2, figsize=FIGURE_SIZE)
    figure.suptitle("FigView 3.5 PDF studio validation", fontsize=11,
                    fontweight="bold")

    # Panel A: OCR CER by degradation condition.
    axis = axes[0][0]
    conditions = sorted(per_condition)
    positions = np.arange(len(conditions))
    base_medians = [statistics.median([p[0] for p in per_condition[c]])
                    for c in conditions]
    new_medians = [statistics.median([p[1] for p in per_condition[c]])
                   for c in conditions]
    axis.bar(positions - BAR_WIDTH / 2, base_medians, BAR_WIDTH,
             color=COLOUR_BASE, label="baseline OCR")
    axis.bar(positions + BAR_WIDTH / 2, new_medians, BAR_WIDTH,
             color=COLOUR_NEW, label="preprocessed (3.5)")
    axis.set_xticks(positions)
    axis.set_xticklabels([f"σ{s}\n{a:g}°" for s, a in conditions])
    axis.set_ylabel("character error rate")
    axis.set_title("A  OCR accuracy on degraded scans")
    axis.legend(frameon=False, fontsize=7.5)
    axis.text(0.02, 0.95,
              f"paired Wilcoxon p = {ocr_p:.2g}\n"
              f"median CER {statistics.median(baseline_cer):.3f} → "
              f"{statistics.median(preprocessed_cer):.3f}",
              transform=axis.transAxes, va="top", fontsize=7.5)

    # Panel B: annotation latency distribution.
    axis = axes[0][1]
    axis.hist(annot_latencies, bins=14, color=COLOUR_NEW, alpha=0.85)
    median_latency = statistics.median(annot_latencies)
    axis.axvline(median_latency, color="#1a1a1a", linewidth=1.0,
                 linestyle="--")
    axis.set_xlabel("highlight + visible repaint (ms)")
    axis.set_ylabel("runs")
    axis.set_title("B  Annotation to repaint latency")
    axis.text(0.97, 0.95, f"median {median_latency:.1f} ms\nn = "
              f"{len(annot_latencies)}", transform=axis.transAxes,
              va="top", ha="right", fontsize=7.5)

    # Panel C: atomic save scaling.
    axis = axes[1][0]
    pages = sorted(save_results)
    medians = [statistics.median(save_results[p]) for p in pages]
    spreads = [np.percentile(save_results[p], [25, 75]) for p in pages]
    lower = [m - s[0] for m, s in zip(medians, spreads)]
    upper = [s[1] - m for m, s in zip(medians, spreads)]
    axis.errorbar(pages, medians, yerr=[lower, upper], marker="o",
                  markersize=4, linewidth=1.2, capsize=3, color=COLOUR_ACCENT)
    axis.set_xlabel("document pages")
    axis.set_ylabel("atomic save (ms)")
    axis.set_title("C  Save cost vs document size")

    # Panel D: export throughput.
    axis = axes[1][1]
    names = [n for n in EXPORT_FORMATS if n in export_results]
    throughput = [EXPORT_PAGES / (statistics.median(export_results[n]) / 1000)
                  for n in names]
    bars = axis.bar(np.arange(len(names)), throughput, 0.5, color=COLOUR_NEW)
    axis.set_xticks(np.arange(len(names)))
    axis.set_xticklabels(names)
    axis.set_ylabel("pages / second")
    axis.set_title(f"D  Export throughput ({EXPORT_PAGES}-page document)")
    for bar, value in zip(bars, throughput):
        axis.text(bar.get_x() + bar.get_width() / 2, value, f"{value:.0f}",
                  ha="center", va="bottom", fontsize=7.5)

    figure.tight_layout(rect=(0, 0, 1, 0.96))
    for suffix in ("png", "pdf", "svg"):
        figure.savefig(OUTPUT_DIR / f"{STEM}.{suffix}")
    plt.close(figure)


def main() -> None:
    if not O.ocr_available():
        raise SystemExit("The integrated OCR engine (eng pack) is required "
                         "for the accuracy part of this benchmark.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw: list[dict] = []
    summary: list[dict] = []
    statistics_rows: list[dict] = []
    started = time.strftime("%Y-%m-%d %H:%M:%S")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        print("OCR accuracy (preprocessed vs baseline)…")
        ocr_parts = bench_ocr(root, raw, summary, statistics_rows)
        print("Annotation latency…")
        annot_latencies = bench_annotation(root, raw, summary)
        print("Atomic save scaling…")
        save_results = bench_save(root, raw, summary)
        print("Export throughput…")
        export_results = bench_export(root, raw, summary)

    per_condition, baseline_cer, preprocessed_cer, ocr_p = ocr_parts
    write_csv(OUTPUT_DIR / f"{STEM}_raw.csv", raw,
              ["part", "condition", "noise_sigma", "angle_deg", "route",
               "format", "pages", "repeat", "elapsed_ms", "pages_per_s",
               "cer"])
    write_csv(OUTPUT_DIR / f"{STEM}_summary.csv", summary,
              ["part", "condition", "method", "median", "mean", "stdev", "n"])
    write_csv(OUTPUT_DIR / f"{STEM}_statistics.csv", statistics_rows,
              ["part", "pairs", "shapiro_difference_p",
               "normal_difference_at_alpha", "test", "statistic", "p_value",
               "alpha", "median_baseline", "median_new"])
    build_figure(per_condition, baseline_cer, preprocessed_cer, ocr_p,
                 annot_latencies, save_results, export_results)
    manifest = {
        "benchmark": STEM, "started": started,
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "parameters": {
            "ocr_noise_sigmas": OCR_NOISE_SIGMAS,
            "ocr_angles_deg": OCR_ANGLES_DEG, "ocr_repeats": OCR_REPEATS,
            "ocr_dpi": OCR_DPI, "annot_repeats": ANNOT_REPEATS,
            "save_page_counts": SAVE_PAGE_COUNTS,
            "save_repeats": SAVE_REPEATS, "export_pages": EXPORT_PAGES,
            "export_repeats": EXPORT_REPEATS, "alpha": ALPHA, "seed": SEED,
        },
        "integrity": "all fixtures generated in a TemporaryDirectory; "
                     "no user media read or modified",
        "ocr_paired_wilcoxon_p": ocr_p,
    }
    (OUTPUT_DIR / f"{STEM}_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Done. OCR paired p = {ocr_p:.3g}; outputs under {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
