"""Benchmark FigView 3.3 explicit-file opening against the 3.2 folder scan.

The benchmark uses metadata-only fixtures. It never decodes or alters source
media. Configuration and figure styling are intentionally centralized here.
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
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from figureviewer import docs as D  # noqa: E402


# User-configurable analysis parameters and styling.
FIXTURE_SIZES = (100, 1_000, 5_000)
REPEATS = 15
WARMUPS = 3
ALPHA = 0.05
FIGURE_SIZE = (8.2, 3.35)
COLOURS = {"3.2 folder scan": "#8b95a5", "3.3 explicit file": "#35b6a8"}
OUTPUT_DIR = Path(__file__).resolve().parent / "results"


def timed(function) -> float:
    """Return one call's elapsed time in milliseconds."""
    started = time.perf_counter_ns()
    function()
    return (time.perf_counter_ns() - started) / 1_000_000


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    """Write a stable, human-readable CSV result table."""
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    """Generate fixtures, paired timings, statistics, and validation figures."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_rows: list[dict] = []
    summary_rows: list[dict] = []
    statistics_rows: list[dict] = []
    kinds = D.kinds_for_filter(D.FILTER_ALL)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for size in FIXTURE_SIZES:
            fixture = root / str(size)
            fixture.mkdir()
            targets = []
            for index in range(size):
                path = fixture / f"figure_{index:05d}.png"
                path.write_bytes(b"classification benchmark")
                targets.append(path)
            chosen = targets[size // 2]

            def legacy():
                return D.scan_folder(fixture, kinds=kinds, max_depth=0)

            def explicit():
                return D.entries_for_paths([chosen], kinds=kinds)
            for _ in range(WARMUPS):
                legacy()
                explicit()

            legacy_times, explicit_times = [], []
            for repeat in range(REPEATS):
                # Alternate order to limit systematic cache/order bias.
                if repeat % 2:
                    explicit_ms = timed(explicit)
                    legacy_ms = timed(legacy)
                else:
                    legacy_ms = timed(legacy)
                    explicit_ms = timed(explicit)
                legacy_times.append(legacy_ms)
                explicit_times.append(explicit_ms)
                raw_rows.extend((
                    {"fixture_files": size, "repeat": repeat + 1,
                     "method": "3.2 folder scan", "elapsed_ms": legacy_ms,
                     "listed_files": size},
                    {"fixture_files": size, "repeat": repeat + 1,
                     "method": "3.3 explicit file", "elapsed_ms": explicit_ms,
                     "listed_files": 1},
                ))

            differences = [old - new for old, new in zip(legacy_times, explicit_times)]
            shapiro = stats.shapiro(differences)
            wilcoxon = stats.wilcoxon(legacy_times, explicit_times, alternative="greater")
            for method, values, listed in (
                ("3.2 folder scan", legacy_times, size),
                ("3.3 explicit file", explicit_times, 1),
            ):
                summary_rows.append({
                    "fixture_files": size,
                    "method": method,
                    "median_ms": statistics.median(values),
                    "mean_ms": statistics.fmean(values),
                    "stdev_ms": statistics.stdev(values),
                    "listed_files": listed,
                })
            statistics_rows.append({
                "fixture_files": size,
                "paired_repeats": REPEATS,
                "shapiro_difference_p": shapiro.pvalue,
                "normal_difference_at_alpha": shapiro.pvalue >= ALPHA,
                "test": "one-sided paired Wilcoxon",
                "wilcoxon_statistic": wilcoxon.statistic,
                "p_value": wilcoxon.pvalue,
                "alpha": ALPHA,
            })

    write_csv(OUTPUT_DIR / "explicit_opening_v33_raw.csv", raw_rows,
              ["fixture_files", "repeat", "method", "elapsed_ms", "listed_files"])
    write_csv(OUTPUT_DIR / "explicit_opening_v33_summary.csv", summary_rows,
              ["fixture_files", "method", "median_ms", "mean_ms", "stdev_ms",
               "listed_files"])
    write_csv(OUTPUT_DIR / "explicit_opening_v33_statistics.csv", statistics_rows,
              ["fixture_files", "paired_repeats", "shapiro_difference_p",
               "normal_difference_at_alpha", "test", "wilcoxon_statistic",
               "p_value", "alpha"])

    medians = {(row["fixture_files"], row["method"]): row["median_ms"]
               for row in summary_rows}
    fig, axes = plt.subplots(1, 2, figsize=FIGURE_SIZE)
    x = list(range(len(FIXTURE_SIZES)))
    for method in COLOURS:
        axes[0].plot(x, [medians[(size, method)] for size in FIXTURE_SIZES],
                     marker="o", linewidth=1.8, markersize=4.5,
                     color=COLOURS[method], label=method)
    axes[0].set_xticks(x, [f"{size:,}" for size in FIXTURE_SIZES])
    axes[0].set_xlabel("Sibling files in directory")
    axes[0].set_ylabel("Median open-list work (ms)")
    axes[0].set_title("Opening latency")
    axes[0].legend(frameon=False, fontsize=8)

    width = 0.32
    axes[1].bar([i - width / 2 for i in x], FIXTURE_SIZES, width,
                color=COLOURS["3.2 folder scan"], label="3.2 folder scan")
    axes[1].bar([i + width / 2 for i in x], [1] * len(x), width,
                color=COLOURS["3.3 explicit file"], label="3.3 explicit file")
    axes[1].set_yscale("log")
    axes[1].set_xticks(x, [f"{size:,}" for size in FIXTURE_SIZES])
    axes[1].set_xlabel("Sibling files in directory")
    axes[1].set_ylabel("Files admitted to list (log scale)")
    axes[1].set_title("Session isolation")

    for axis in axes:
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y", alpha=0.18, linewidth=0.7)
    fig.suptitle("FigView 3.3 explicit file opening", fontsize=12, fontweight="semibold")
    fig.tight_layout()
    for suffix in ("png", "pdf", "svg"):
        fig.savefig(OUTPUT_DIR / f"explicit_opening_v33.{suffix}", dpi=220,
                    bbox_inches="tight")
    plt.close(fig)

    manifest = {
        "fixture_sizes": FIXTURE_SIZES,
        "repeats": REPEATS,
        "warmups": WARMUPS,
        "alpha": ALPHA,
        "comparison": "3.2 top-level folder scan versus 3.3 one-file metadata load",
        "integrity": "Generated temporary metadata fixtures only; no user media read or changed.",
    }
    (OUTPUT_DIR / "explicit_opening_v33_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
