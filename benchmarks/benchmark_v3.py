"""Reproducible Figure Viewer 3.0 performance and regression benchmark.

The committed ``HEAD`` is extracted into a temporary directory and treated as
the 2.2 baseline. Runs are interleaved and paired to reduce drift from CPU
temperature, filesystem caching, and background load. Raw measurements,
summary statistics, distribution checks, paired Wilcoxon results, and a compact
three-format figure are written under ``benchmarks/results``.

No user file is read or changed. All workload files are generated inside a
temporary directory and removed automatically.
"""
from __future__ import annotations

# Parameters deliberately precede imports so the benchmark is configurable at
# a glance. Ruff's normal import-order rule is waived for this analysis script.
# ruff: noqa: E402

# ---------------------------------------------------------------- parameters
PAIRS = 16
SCAN_IMAGE_FILES = 1_800
SCAN_PDF_FILES = 600
VIDEO_SIZE = (1920, 1080)
VIDEO_FPS = 30.0
VIDEO_FRAMES = 100
VIDEO_SAMPLE_SECONDS = 1.8
VIDEO_VIEWPORT = (1000, 650)
RANDOM_SEED = 20260823
FIGURE_DPI = 180

# ------------------------------------------------------------------ styling
BASELINE_COLOR = "#7c879d"
CURRENT_COLOR = "#3b82f6"
VIDEO_COLOR = "#06b6d4"
TEXT_COLOR = "#172033"
GRID_COLOR = "#d9dfeb"

import csv
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks" / "results"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _python_time(package_root: Path, code: str, *arguments: str) -> float:
    """Run one fresh interpreter and parse its final line as milliseconds."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(
        [sys.executable, "-c", code, *arguments], cwd=package_root,
        env=environment, capture_output=True, text=True, check=True, timeout=45,
    )
    return float(completed.stdout.strip().splitlines()[-1])


def _extract_baseline(destination: Path) -> Path:
    """Extract the committed repository state without changing this worktree."""
    archive = destination / "baseline.zip"
    subprocess.run(
        ["git", "archive", "--format=zip", "--output", str(archive), "HEAD"],
        cwd=ROOT, check=True, capture_output=True,
    )
    target = destination / "baseline"
    target.mkdir()
    with zipfile.ZipFile(archive) as package:
        package.extractall(target)
    return target


def _make_scan_fixture(root: Path) -> None:
    """Create metadata-only files because scanning must never decode content."""
    root.mkdir()
    for index in range(SCAN_IMAGE_FILES):
        (root / f"figure_{index:05d}.png").touch()
    for index in range(SCAN_PDF_FILES):
        (root / f"report_{index:05d}.pdf").touch()


def _paired_measurements(baseline: Path, fixture: Path) -> list[dict[str, float | int | str]]:
    """Measure import and folder-scan latency with alternating run order."""
    import_code = (
        "import time; start=time.perf_counter(); import figureviewer.docs; "
        "print((time.perf_counter()-start)*1000)"
    )
    scan_code = (
        "import time,sys; from pathlib import Path; from figureviewer import docs as D; "
        "root=Path(sys.argv[1]); start=time.perf_counter(); "
        "entries,truncated=D.scan_folder(root,kinds=D.kinds_for_filter(D.FILTER_ALL)); "
        "assert not truncated and len(entries)==" + str(SCAN_IMAGE_FILES + SCAN_PDF_FILES) + "; "
        "print((time.perf_counter()-start)*1000)"
    )
    rows: list[dict[str, float | int | str]] = []
    versions = (("2.2 baseline", baseline), ("3.0 current", ROOT))
    random.seed(RANDOM_SEED)

    for metric, code, arguments in (
        ("Cold docs import", import_code, ()),
        ("2,400-file scan", scan_code, (str(fixture),)),
    ):
        for pair in range(PAIRS):
            order = list(versions)
            if pair % 2:
                order.reverse()
            # A deterministic occasional shuffle prevents the benchmark from
            # depending solely on a strict AB/BA cadence.
            if random.random() < 0.25:
                order.reverse()
            pair_values: dict[str, float] = {}
            for version, root in order:
                pair_values[version] = _python_time(root, code, *arguments)
            rows.append({
                "metric": metric,
                "pair": pair + 1,
                "baseline_ms": pair_values["2.2 baseline"],
                "current_ms": pair_values["3.0 current"],
                "delta_ms": pair_values["3.0 current"] - pair_values["2.2 baseline"],
                "delta_percent": (
                    (pair_values["3.0 current"] / pair_values["2.2 baseline"] - 1.0) * 100.0
                ),
            })
    return rows


def _video_benchmark(path: Path) -> dict[str, float | int | str]:
    """Measure first-frame latency and steady 1080p playback separately."""
    import cv2

    from figureviewer.video import VideoSession

    width, height = VIDEO_SIZE
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), VIDEO_FPS, (width, height)
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not create the benchmark video")
    for index in range(VIDEO_FRAMES):
        frame = np.full((height, width, 3), (19, 25, 39), dtype=np.uint8)
        x = 130 + (index * 16) % (width - 260)
        y = int(height / 2 + 260 * np.sin(index / 13.0))
        cv2.circle(frame, (x, y), 80, (235, 170, 24), -1, cv2.LINE_AA)
        writer.write(frame)
    writer.release()

    opened_at = time.perf_counter()
    session = VideoSession(path, VIDEO_VIEWPORT, autoplay=True)
    first_frame = None
    try:
        open_deadline = opened_at + 5.0
        while time.perf_counter() < open_deadline and first_frame is None:
            first_frame = session.pop_frame()
            snapshot = session.snapshot()
            if snapshot.state == "error":
                raise RuntimeError(snapshot.error)
            time.sleep(1 / 240)
        if first_frame is None:
            raise RuntimeError("Timed out waiting for the benchmark's first video frame")
        first_frame_ms = (time.perf_counter() - opened_at) * 1000.0
        media_start = session.snapshot().position

        presented = 0
        start = time.perf_counter()
        deadline = start + VIDEO_SAMPLE_SECONDS
        while time.perf_counter() < deadline:
            if session.pop_frame() is not None:
                presented += 1
            snapshot = session.snapshot()
            if snapshot.state == "error":
                raise RuntimeError(snapshot.error)
            time.sleep(1 / 120)
        elapsed = time.perf_counter() - start
        snapshot = session.snapshot()
    finally:
        session.close()
    return {
        "metric": "1080p video playback",
        "first_frame_ms": first_frame_ms,
        "wall_seconds": elapsed,
        "media_seconds": snapshot.position - media_start,
        "realtime_ratio": (snapshot.position - media_start) / elapsed,
        "presented_fps": presented / elapsed,
        "mailbox_drops": snapshot.dropped_frames,
        "viewport_width": VIDEO_VIEWPORT[0],
        "viewport_height": VIDEO_VIEWPORT[1],
    }


def _summary(rows: list[dict[str, float | int | str]]) -> tuple[list[dict], list[dict]]:
    """Compute robust summaries and validate the paired-test assumptions."""
    summaries: list[dict] = []
    tests: list[dict] = []
    for metric in sorted({str(row["metric"]) for row in rows}):
        selected = [row for row in rows if row["metric"] == metric]
        baseline = np.array([float(row["baseline_ms"]) for row in selected])
        current = np.array([float(row["current_ms"]) for row in selected])
        differences = current - baseline
        shapiro = stats.shapiro(differences)
        wilcoxon = stats.wilcoxon(current, baseline, alternative="two-sided")
        for version, values in (("2.2 baseline", baseline), ("3.0 current", current)):
            summaries.append({
                "metric": metric,
                "version": version,
                "n": len(values),
                "mean_ms": float(np.mean(values)),
                "median_ms": float(np.median(values)),
                "stdev_ms": float(np.std(values, ddof=1)),
                "p95_ms": float(np.percentile(values, 95)),
            })
        tests.append({
            "metric": metric,
            "paired_n": len(differences),
            "difference_shapiro_p": float(shapiro.pvalue),
            "normality_at_0.05": "not rejected" if shapiro.pvalue >= 0.05 else "rejected",
            "selected_test": "paired Wilcoxon signed-rank",
            "wilcoxon_statistic": float(wilcoxon.statistic),
            "wilcoxon_p": float(wilcoxon.pvalue),
            "median_delta_ms": float(np.median(differences)),
            "median_delta_percent": float(np.median([
                float(row["delta_percent"]) for row in selected
            ])),
        })
    return summaries, tests


def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write a non-empty list of dictionaries with stable column ordering."""
    if not rows:
        raise ValueError(f"No rows for {path}")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _plot(rows: list[dict], video: dict, tests: list[dict]) -> None:
    """Create compact paired comparison panels and one video health panel."""
    metrics = ["Cold docs import", "2,400-file scan"]
    fig, axes = plt.subplots(1, 3, figsize=(10.4, 3.25), constrained_layout=True)
    fig.patch.set_facecolor("white")

    for axis, metric in zip(axes[:2], metrics):
        selected = [row for row in rows if row["metric"] == metric]
        baseline = np.array([float(row["baseline_ms"]) for row in selected])
        current = np.array([float(row["current_ms"]) for row in selected])
        for left, right in zip(baseline, current):
            axis.plot((0, 1), (left, right), color=GRID_COLOR, linewidth=0.75, zorder=1)
        axis.scatter(np.zeros_like(baseline), baseline, s=18, color=BASELINE_COLOR,
                     alpha=0.8, zorder=2)
        axis.scatter(np.ones_like(current), current, s=18, color=CURRENT_COLOR,
                     alpha=0.8, zorder=2)
        axis.plot((-.12, .12), (np.median(baseline),) * 2, color=TEXT_COLOR, linewidth=2)
        axis.plot((.88, 1.12), (np.median(current),) * 2, color=TEXT_COLOR, linewidth=2)
        result = next(item for item in tests if item["metric"] == metric)
        axis.set_title(metric, fontsize=10, weight="semibold")
        axis.set_xticks((0, 1), ("2.2", "3.0"))
        axis.set_ylabel("Milliseconds")
        axis.text(0.5, 0.98,
                  f"median Δ {result['median_delta_percent']:+.1f}%\n"
                  f"paired p = {result['wilcoxon_p']:.3g}",
                  transform=axis.transAxes, ha="center", va="top", fontsize=8,
                  color=TEXT_COLOR)
        axis.grid(axis="y", color=GRID_COLOR, linewidth=0.6)
        axis.spines[["top", "right"]].set_visible(False)

    axis = axes[2]
    ratio = float(video["realtime_ratio"])
    presented = float(video["presented_fps"])
    axis.axhline(1.0, color=GRID_COLOR, linewidth=1.2, linestyle="--")
    axis.scatter((0,), (ratio,), s=90, color=VIDEO_COLOR, zorder=3)
    axis.set_xlim(-0.75, 0.75)
    axis.set_ylim(0, max(1.15, ratio * 1.15))
    axis.set_xticks((0,), ("3.0",))
    axis.set_ylabel("Media time / wall time")
    axis.set_title("1080p playback", fontsize=10, weight="semibold")
    first_frame = float(video["first_frame_ms"])
    axis.text(0.5, 0.72,
              f"{ratio:.3f}x real time\n{presented:.1f} frames/s presented\n"
              f"first frame {first_frame:.0f} ms",
              transform=axis.transAxes, ha="center", va="top", fontsize=8,
              color=TEXT_COLOR)
    axis.grid(axis="y", color=GRID_COLOR, linewidth=0.6)
    axis.spines[["top", "right"]].set_visible(False)

    fig.suptitle("Figure Viewer 3.0 performance validation", fontsize=12,
                 weight="semibold", color=TEXT_COLOR)
    for extension in ("png", "pdf", "svg"):
        output = RESULTS / f"performance_v3.{extension}"
        fig.savefig(output, dpi=FIGURE_DPI, bbox_inches="tight")
        if extension == "svg":
            # Matplotlib writes path-data lines with trailing spaces. They are
            # harmless XML but fail strict repository whitespace checks.
            clean = "\n".join(line.rstrip() for line in output.read_text(
                encoding="utf-8").splitlines()) + "\n"
            output.write_text(clean, encoding="utf-8")
    plt.close(fig)


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    # Keep baseline, current checkout, and fixtures on the same volume. On
    # Windows, comparing a C-drive temp extraction with a D-drive checkout
    # measures storage and security-scanner differences as well as code.
    with tempfile.TemporaryDirectory(dir=ROOT / "benchmarks") as directory:
        temporary = Path(directory)
        baseline = _extract_baseline(temporary)
        fixture = temporary / "scan-fixture"
        _make_scan_fixture(fixture)
        rows = _paired_measurements(baseline, fixture)
        video = _video_benchmark(temporary / "video-benchmark.avi")

    summaries, tests = _summary(rows)
    _write_csv(RESULTS / "performance_raw.csv", rows)
    _write_csv(RESULTS / "performance_summary.csv", summaries)
    _write_csv(RESULTS / "performance_statistics.csv", tests)
    _write_csv(RESULTS / "video_summary.csv", [video])
    _plot(rows, video, tests)

    manifest = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "parameters": {
            "pairs": PAIRS,
            "scan_files": SCAN_IMAGE_FILES + SCAN_PDF_FILES,
            "video_size": VIDEO_SIZE,
            "video_fps": VIDEO_FPS,
            "video_sample_seconds": VIDEO_SAMPLE_SECONDS,
            "video_viewport": VIDEO_VIEWPORT,
        },
    }
    (RESULTS / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summaries, "tests": tests, "video": video}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
