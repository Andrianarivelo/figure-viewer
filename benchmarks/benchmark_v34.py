"""Compare FigView 3.4 PDF reading zoom with the 3.3 fit-page default.

This benchmark is geometry-only. It uses canonical PDF MediaBox dimensions and
does not read or modify user documents. Raw paired timings, statistical checks,
summaries, and PNG/PDF/SVG figures are written to ``benchmarks/results``.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from figureviewer.viewer import (  # noqa: E402
    PDF_SCREEN_DPI,
    pdf_reading_scale,
)


# Analysis parameters and styling.
BASE_DPI = 150.0
CANVAS_SIZE = (1_000, 650)
MARGIN = 16
PAIRS = 20
CALLS_PER_PAIR = 20_000
ALPHA = 0.05
FORMATS_POINTS = {
    "A5 portrait": (419.53, 595.28),
    "A4 portrait": (595.28, 841.89),
    "Letter": (612.0, 792.0),
    "A3 portrait": (841.89, 1190.55),
    "A4 landscape": (841.89, 595.28),
}
COLORS = {"3.3 fit page": "#8b95a5", "3.4 reading width": "#35b6a8"}
FIGURE_SIZE = (8.4, 3.5)
OUTPUT_DIR = Path(__file__).resolve().parent / "results"


def old_fit_page(width: int, height: int) -> float:
    """FigView 3.3 initial scale for the configured viewport."""
    return min((CANVAS_SIZE[0] - MARGIN) / width,
               (CANVAS_SIZE[1] - MARGIN) / height)


def timed_calls(function) -> float:
    """Average microseconds per call over one stable batch."""
    started = time.perf_counter_ns()
    for _ in range(CALLS_PER_PAIR):
        function()
    return (time.perf_counter_ns() - started) / CALLS_PER_PAIR / 1_000.0


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    format_rows: list[dict] = []
    for name, (width_points, height_points) in FORMATS_POINTS.items():
        width = round(width_points * BASE_DPI / 72.0)
        height = round(height_points * BASE_DPI / 72.0)
        old_scale = old_fit_page(width, height)
        new_scale = pdf_reading_scale(width, BASE_DPI, CANVAS_SIZE[0])
        for method, scale in (("3.3 fit page", old_scale),
                              ("3.4 reading width", new_scale)):
            format_rows.append({
                "format": name,
                "method": method,
                "display_zoom_percent": scale * BASE_DPI / PDF_SCREEN_DPI * 100,
                "display_width_px": width * scale,
                "display_height_px": height * scale,
            })

    a4_width = round(595.28 * BASE_DPI / 72.0)
    a4_height = round(841.89 * BASE_DPI / 72.0)
    def old_call() -> float:
        return old_fit_page(a4_width, a4_height)

    def new_call() -> float:
        return pdf_reading_scale(a4_width, BASE_DPI, CANVAS_SIZE[0])
    raw_rows: list[dict] = []
    old_times, new_times = [], []
    for pair in range(PAIRS):
        if pair % 2:
            new_us = timed_calls(new_call)
            old_us = timed_calls(old_call)
        else:
            old_us = timed_calls(old_call)
            new_us = timed_calls(new_call)
        old_times.append(old_us)
        new_times.append(new_us)
        raw_rows.extend((
            {"pair": pair + 1, "method": "3.3 fit page", "microseconds": old_us},
            {"pair": pair + 1, "method": "3.4 reading width", "microseconds": new_us},
        ))

    differences = [new - old for old, new in zip(old_times, new_times)]
    shapiro = stats.shapiro(differences)
    wilcoxon = stats.wilcoxon(new_times, old_times, alternative="greater")
    summary_rows = [
        {"method": method, "median_microseconds": statistics.median(values),
         "mean_microseconds": statistics.fmean(values),
         "stdev_microseconds": statistics.stdev(values)}
        for method, values in (("3.3 fit page", old_times),
                               ("3.4 reading width", new_times))
    ]
    statistics_rows = [{
        "paired_batches": PAIRS,
        "calls_per_batch": CALLS_PER_PAIR,
        "shapiro_difference_p": shapiro.pvalue,
        "normal_difference_at_alpha": shapiro.pvalue >= ALPHA,
        "test": "one-sided paired Wilcoxon",
        "wilcoxon_statistic": wilcoxon.statistic,
        "p_value": wilcoxon.pvalue,
        "alpha": ALPHA,
    }]

    write_csv(OUTPUT_DIR / "pdf_reading_v34_formats.csv", format_rows,
              ["format", "method", "display_zoom_percent", "display_width_px",
               "display_height_px"])
    write_csv(OUTPUT_DIR / "pdf_reading_v34_raw.csv", raw_rows,
              ["pair", "method", "microseconds"])
    write_csv(OUTPUT_DIR / "pdf_reading_v34_summary.csv", summary_rows,
              ["method", "median_microseconds", "mean_microseconds",
               "stdev_microseconds"])
    write_csv(OUTPUT_DIR / "pdf_reading_v34_statistics.csv", statistics_rows,
              ["paired_batches", "calls_per_batch", "shapiro_difference_p",
               "normal_difference_at_alpha", "test", "wilcoxon_statistic",
               "p_value", "alpha"])

    fig, axes = plt.subplots(1, 2, figsize=FIGURE_SIZE)
    names = list(FORMATS_POINTS)
    x = range(len(names))
    width = 0.34
    zooms = {(row["format"], row["method"]): row["display_zoom_percent"]
             for row in format_rows}
    for offset, method in ((-width / 2, "3.3 fit page"),
                           (width / 2, "3.4 reading width")):
        axes[0].bar([value + offset for value in x],
                    [zooms[(name, method)] for name in names], width,
                    color=COLORS[method], label=method)
    axes[0].axhline(125, color=COLORS["3.4 reading width"], linewidth=0.8,
                    linestyle="--", alpha=0.7)
    axes[0].set_xticks(list(x), names, rotation=25, ha="right")
    axes[0].set_ylabel("Initial displayed zoom (%)")
    axes[0].set_title("Format-aware reading size")
    axes[0].legend(frameon=False, fontsize=8)

    axes[1].bar([0, 1], [statistics.median(old_times), statistics.median(new_times)],
                width=0.42, color=list(COLORS.values()))
    axes[1].set_xticks([0, 1], ["3.3\nfit page", "3.4\nreading width"])
    axes[1].set_ylabel("Median calculation time (µs)")
    axes[1].set_title("Per-page geometry overhead")
    for axis in axes:
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y", alpha=0.18, linewidth=0.7)
    fig.suptitle("FigView 3.4 PDF reading layout", fontsize=12, fontweight="semibold")
    fig.tight_layout()
    for suffix in ("png", "pdf", "svg"):
        path = OUTPUT_DIR / f"pdf_reading_v34.{suffix}"
        fig.savefig(path, dpi=220, bbox_inches="tight")
        if suffix == "svg":
            text = path.read_text(encoding="utf-8")
            path.write_text("\n".join(line.rstrip() for line in text.splitlines()) + "\n",
                            encoding="utf-8")
    plt.close(fig)

    manifest = {
        "base_dpi": BASE_DPI,
        "canvas_size": CANVAS_SIZE,
        "paired_batches": PAIRS,
        "calls_per_batch": CALLS_PER_PAIR,
        "alpha": ALPHA,
        "integrity": "Canonical MediaBox geometry only; no user documents read or modified.",
    }
    (OUTPUT_DIR / "pdf_reading_v34_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
