"""Paired Figure Viewer 3.1 navigation and discovery benchmark.

The committed 3.0 implementation is extracted to a temporary directory and
compared with the working 3.1 tree. Every workload uses generated files only.
Raw observations, summaries, assumption checks, paired statistics, and the
same compact figure in PNG, PDF, and SVG are written to ``benchmarks/results``.
"""
from __future__ import annotations

# Parameters and styling live first so reviewers can change the workload
# without searching through analysis code. Ruff's import-order rule is waived
# for this intentionally analysis-oriented layout.
# ruff: noqa: E402

# ---------------------------------------------------------------- parameters
PAIRS = 12
VIDEO_SIZE = (640, 360)
VIDEO_FPS = 30.0
VIDEO_FRAMES = 90
VIDEO_VIEWPORT = (640, 360)
START_FRAME = 30
WAIT_SECONDS = 3.0
FILES_BY_DEPTH = (40, 30, 20, 10)
FIGURE_DPI = 180

# ------------------------------------------------------------------ styling
BASELINE_COLOR = "#7c879d"
CURRENT_COLOR = "#06b6d4"
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
RANDOM_SEED = 20260824


def _extract_baseline(destination: Path) -> Path:
    """Extract committed 3.0 without changing or stashing the worktree."""
    archive = destination / "baseline.zip"
    subprocess.run(
        ["git", "archive", "--format=zip", "--output", str(archive), "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    target = destination / "baseline"
    target.mkdir()
    with zipfile.ZipFile(archive) as package:
        package.extractall(target)
    return target


def _make_video(path: Path) -> None:
    """Create a deterministic MJPG clip whose frames have distinct colours."""
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), VIDEO_FPS, VIDEO_SIZE
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV cannot create the MJPG benchmark clip")
    width, height = VIDEO_SIZE
    for index in range(VIDEO_FRAMES):
        frame = np.full(
            (height, width, 3),
            (20 + index % 80, 30 + (index * 3) % 100, 55),
            dtype=np.uint8,
        )
        cv2.putText(
            frame,
            str(index),
            (width // 3, height // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.0,
            (245, 245, 245),
            3,
            cv2.LINE_AA,
        )
        writer.write(frame)
    writer.release()


def _make_nested_fixture(root: Path) -> None:
    """Create supported files from depth zero through depth three."""
    current = root
    for depth, count in enumerate(FILES_BY_DEPTH):
        current.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (current / f"depth_{depth}_figure_{index:03d}.png").touch()
        current /= f"level_{depth + 1}"


FRAME_PROBE = r"""
import json
import sys
import time
from pathlib import Path
from figureviewer import video as V

path = Path(sys.argv[1])
mode = sys.argv[2]
start_frame = int(sys.argv[3])
timeout = float(sys.argv[4])
session = V.VideoSession(path, (640, 360), autoplay=False)

def wait_for(predicate):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        snapshot = session.snapshot()
        if snapshot.state == "error":
            raise RuntimeError(snapshot.error)
        if predicate(snapshot):
            return snapshot
        time.sleep(0.001)
    raise TimeoutError(str(session.snapshot()))

try:
    ready = wait_for(lambda state: state.ready and state.fps > 0)
    if mode == "current":
        session.step_frames(start_frame - ready.frame_index)
        before = wait_for(lambda state: state.frame_index == start_frame)
        forward_started = time.perf_counter()
        session.step_frames(1)
        after = wait_for(lambda state: state.frame_index == start_frame + 1)
        forward_latency = (time.perf_counter() - forward_started) * 1000.0
        delta = after.frame_index - before.frame_index
        backward_started = time.perf_counter()
        session.step_frames(-1)
        returned = wait_for(lambda state: state.frame_index == start_frame)
        backward_latency = (time.perf_counter() - backward_started) * 1000.0
        back_delta = returned.frame_index - after.frame_index
    else:
        session.seek(start_frame / ready.fps)
        before = wait_for(
            lambda state: abs(state.position - start_frame / ready.fps) < 0.45 / ready.fps
        )
        before_index = round(before.position * before.fps)
        forward_started = time.perf_counter()
        session.seek_relative(1.0 / before.fps)
        after = wait_for(lambda state: abs(state.position - before.position) > 0.45 / before.fps)
        forward_latency = (time.perf_counter() - forward_started) * 1000.0
        after_index = round(after.position * after.fps)
        delta = after_index - before_index
        backward_started = time.perf_counter()
        session.seek_relative(-1.0 / after.fps)
        returned = wait_for(lambda state: abs(state.position - after.position) > 0.45 / after.fps)
        backward_latency = (time.perf_counter() - backward_started) * 1000.0
        back_delta = round(returned.position * returned.fps) - after_index
    print(json.dumps({
        "latency_ms": (forward_latency + backward_latency) / 2.0,
        "forward_latency_ms": forward_latency,
        "backward_latency_ms": backward_latency,
        "frame_delta": delta,
        "back_frame_delta": back_delta,
        "absolute_error_frames": max(abs(delta - 1), abs(back_delta + 1)),
    }))
finally:
    session.close()
"""


SCAN_PROBE = r"""
import json
import sys
import time
from pathlib import Path
from figureviewer import app as A
from figureviewer import docs as D

root = Path(sys.argv[1])
started = time.perf_counter()
entries, truncated = D.scan_folder(
    root,
    kinds=D.kinds_for_filter(D.FILTER_ALL),
    max_depth=A.DEFAULT_SETTINGS["scan_depth"],
)
print(json.dumps({
    "latency_ms": (time.perf_counter() - started) * 1000.0,
    "files_found": len(entries),
    "truncated": truncated,
    "default_depth": A.DEFAULT_SETTINGS["scan_depth"],
}))
"""


def _probe(package_root: Path, code: str, *arguments: str) -> dict:
    """Run one isolated version and parse its final JSON output line."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", code, *arguments],
        cwd=package_root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _measure(baseline: Path, video: Path, nested: Path) -> list[dict]:
    """Collect paired results with alternating execution order."""
    versions = (("3.0 baseline", baseline, "baseline"),
                ("3.1 current", ROOT, "current"))
    random.seed(RANDOM_SEED)
    rows: list[dict] = []

    for pair in range(PAIRS):
        order = list(versions)
        if pair % 2:
            order.reverse()
        for label, package, mode in order:
            result = _probe(
                package,
                FRAME_PROBE,
                str(video),
                mode,
                str(START_FRAME),
                str(WAIT_SECONDS),
            )
            rows.append({"pair": pair, "metric": "One-frame command",
                         "version": label, **result})

    # Scan timing is paired too. Completeness itself is deterministic, while
    # latency is retained so the expanded default's cost remains transparent.
    for pair in range(PAIRS):
        order = list(versions)
        if pair % 2:
            order.reverse()
        for label, package, _mode in order:
            result = _probe(package, SCAN_PROBE, str(nested))
            rows.append({"pair": pair, "metric": "Default nested scan",
                         "version": label, **result})
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write a rectangular CSV using the union of observed columns."""
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _analyse(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Summarise distributions and run paired non-parametric tests."""
    summaries: list[dict] = []
    statistics: list[dict] = []
    for metric in ("One-frame command", "Default nested scan"):
        metric_rows = [row for row in rows if row["metric"] == metric]
        value_key = "latency_ms"
        for version in ("3.0 baseline", "3.1 current"):
            version_rows = [row for row in metric_rows if row["version"] == version]
            values = np.array([float(row[value_key]) for row in version_rows])
            summary = {
                "metric": metric,
                "version": version,
                "n": len(values),
                "median_ms": float(np.median(values)),
                "q1_ms": float(np.quantile(values, 0.25)),
                "q3_ms": float(np.quantile(values, 0.75)),
                "mean_ms": float(np.mean(values)),
                "std_ms": float(np.std(values, ddof=1)),
            }
            if metric == "One-frame command":
                errors = [int(row["absolute_error_frames"]) for row in version_rows]
                summary["exact_trials"] = sum(error == 0 for error in errors)
                summary["max_error_frames"] = max(errors)
            else:
                summary["files_found"] = int(version_rows[0]["files_found"])
                summary["default_depth"] = int(version_rows[0]["default_depth"])
            summaries.append(summary)
        baseline_values = np.array([
            float(row[value_key]) for row in sorted(
                (item for item in metric_rows if item["version"] == "3.0 baseline"),
                key=lambda item: item["pair"],
            )
        ])
        current_values = np.array([
            float(row[value_key]) for row in sorted(
                (item for item in metric_rows if item["version"] == "3.1 current"),
                key=lambda item: item["pair"],
            )
        ])
        differences = current_values - baseline_values
        shapiro = stats.shapiro(differences)
        wilcoxon = stats.wilcoxon(current_values, baseline_values)
        statistics.append({
            "metric": metric,
            "paired_n": len(differences),
            "difference_shapiro_w": float(shapiro.statistic),
            "difference_shapiro_p": float(shapiro.pvalue),
            "paired_wilcoxon_w": float(wilcoxon.statistic),
            "paired_wilcoxon_p": float(wilcoxon.pvalue),
            "median_change_percent": float(
                (np.median(current_values) / np.median(baseline_values) - 1.0) * 100.0
            ),
        })
    return summaries, statistics


def _plot(rows: list[dict], statistics: list[dict]) -> None:
    """Render latency and discovery completeness without overlapping labels."""
    frame_rows = [row for row in rows if row["metric"] == "One-frame command"]
    versions = ("3.0 baseline", "3.1 current")
    colours = (BASELINE_COLOR, CURRENT_COLOR)
    latency = [
        np.array([float(row["latency_ms"]) for row in frame_rows
                  if row["version"] == version])
        for version in versions
    ]
    found = [
        int(next(row["files_found"] for row in rows
                 if row["metric"] == "Default nested scan" and row["version"] == version))
        for version in versions
    ]
    frame_stat = next(item for item in statistics if item["metric"] == "One-frame command")

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.labelcolor": TEXT_COLOR,
        "axes.titlecolor": TEXT_COLOR,
        "xtick.color": TEXT_COLOR,
        "ytick.color": TEXT_COLOR,
    })
    figure, axes = plt.subplots(1, 2, figsize=(8.2, 3.35), constrained_layout=True)

    box = axes[0].boxplot(latency, patch_artist=True, widths=0.42,
                          medianprops={"color": "white", "linewidth": 1.5})
    for patch, colour in zip(box["boxes"], colours):
        patch.set_facecolor(colour)
        patch.set_edgecolor(colour)
    for index, values in enumerate(latency, start=1):
        jitter = np.linspace(-0.07, 0.07, len(values))
        axes[0].scatter(index + jitter, values, s=11, color=colours[index - 1],
                        alpha=0.65, zorder=3)
    axes[0].set_title("Forward/back one-frame latency", loc="left", weight="bold")
    axes[0].set_ylabel("Latency (ms)")
    axes[0].set_xticks((1, 2), ("3.0 time seek", "3.1 frame index"))
    axes[0].grid(axis="y", color=GRID_COLOR, linewidth=0.7)
    axes[0].text(
        0.02, 0.97,
        f"Paired Wilcoxon p = {frame_stat['paired_wilcoxon_p']:.3g}",
        transform=axes[0].transAxes, va="top", color=TEXT_COLOR,
    )

    x = np.arange(2)
    bars = axes[1].bar(x, found, width=0.38, color=colours)
    expected = sum(FILES_BY_DEPTH)
    axes[1].axhline(expected, color=TEXT_COLOR, linewidth=0.9, linestyle=":")
    axes[1].set_title("Files found with default settings", loc="left", weight="bold")
    axes[1].set_ylabel("Supported files discovered")
    axes[1].set_xticks(x, ("3.0 top only", "3.1 all levels"))
    axes[1].set_ylim(0, expected * 1.18)
    axes[1].grid(axis="y", color=GRID_COLOR, linewidth=0.7)
    for bar, value in zip(bars, found):
        axes[1].text(bar.get_x() + bar.get_width() / 2, value + expected * 0.025,
                     f"{value}/{expected}", ha="center", weight="bold", color=TEXT_COLOR)

    figure.suptitle("Figure Viewer 3.1 validation", fontsize=13,
                    fontweight="bold", color=TEXT_COLOR)
    for suffix in ("png", "pdf", "svg"):
        figure.savefig(RESULTS / f"frame_navigation_v31.{suffix}", dpi=FIGURE_DPI,
                       bbox_inches="tight", facecolor="white")
    plt.close(figure)


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        baseline = _extract_baseline(temporary)
        video = temporary / "frame-navigation.avi"
        nested = temporary / "nested"
        _make_video(video)
        _make_nested_fixture(nested)
        rows = _measure(baseline, video, nested)

    summaries, statistics = _analyse(rows)
    _write_csv(RESULTS / "frame_navigation_raw.csv", rows)
    _write_csv(RESULTS / "frame_navigation_summary.csv", summaries)
    _write_csv(RESULTS / "frame_navigation_statistics.csv", statistics)
    _plot(rows, statistics)
    manifest = {
        "generated_at_local": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "pairs": PAIRS,
        "video": {"size": VIDEO_SIZE, "fps": VIDEO_FPS, "frames": VIDEO_FRAMES},
        "nested_files_by_depth": FILES_BY_DEPTH,
        "raw_data": "frame_navigation_raw.csv",
    }
    (RESULTS / "frame_navigation_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps({"summary": summaries, "statistics": statistics}, indent=2))


if __name__ == "__main__":
    main()
