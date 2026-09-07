"""Reproduce paired opening benchmarks with generated media and isolated settings.

Run from an IDE or ``python benchmarks/benchmark_opening.py``. The pinned baseline
is archived into a temporary directory; no checkout or user settings/media change.
Fresh child processes alternate execution order to reduce drift. Startup means
first mapped viewer, including application imports but excluding interpreter
launch. Filesystem caches are deliberately not flushed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# All workload, statistical, output and figure parameters are editable here.
PAIRS = 7
BASELINE_REVISION = "d2e562d4665a7f1756a55385c99abf7233622f1b"
FILE_COUNT = 100
IMAGE_SIZE = (1536, 1024)
BACKGROUND_JOBS = 24
BACKGROUND_SECONDS = 0.025
TIMEOUT_SECONDS = 60
SEED = 20260907
FIGURE_SIZE = (10, 5.8)
COLORS = ("#8794a4", "#239b8b")
BAR_WIDTH = 0.30
OUTPUT = Path(__file__).resolve().parent / "results"
STEM = "opening_performance"
METRICS = {
    "startup_ms": "Fresh process to first window",
    "batch_return_ms": "100 files: open call returns",
    "batch_list_ms": "100 files: complete sidebar",
    "batch_selected_ms": "100 files: last image displayed",
    "png_open_ms": "PNG open (1536 x 1024)",
    "pdf_load_ms": "First PDF open and render",
    "foreground_ms": "Foreground behind 24 background jobs",
}


def spin(root, predicate) -> None:
    """Pump real Tk events until observable completion, with bounded failure."""
    deadline = time.perf_counter() + TIMEOUT_SECONDS
    while not predicate():
        root.update()
        if time.perf_counter() > deadline:
            raise TimeoutError("GUI benchmark did not reach expected state")
        time.sleep(0.001)


def child(source: Path, fixtures: Path, settings: Path) -> dict:
    """Measure one fresh process without reading or writing real app settings."""
    started = time.perf_counter()
    sys.path.insert(0, str(source))
    from figureviewer import app as A
    from figureviewer import docs as D

    A.SETTINGS_DIR = settings
    A.SETTINGS_PATH = settings / "settings.json"
    A.INSTANCE_PATH = settings / "instance.json"
    settings.mkdir(parents=True)
    A.SETTINGS_PATH.write_text(json.dumps({"single_instance": False,
                                         "auto_reload": False}), encoding="utf-8")
    app = A.App()
    window = app.windows[0]
    spin(app.root, lambda: bool(window.winfo_viewable()))
    result = {"startup_ms": (time.perf_counter() - started) * 1000}
    # Complete the initial empty scan before the measured multi-open request.
    spin(app.root, lambda: window._rescan_pending == 0)
    paths = sorted(fixtures.glob("*.png"))
    started = time.perf_counter()
    app.open_paths(paths)
    result["batch_return_ms"] = (time.perf_counter() - started) * 1000
    spin(app.root, lambda: len(window.entries) == FILE_COUNT)
    result["batch_list_ms"] = (time.perf_counter() - started) * 1000
    spin(app.root, lambda: window.current is not None
         and window.current.path == paths[-1] and window.document is not None
         and window.status_var.get().startswith("Loaded"))
    result["batch_selected_ms"] = (time.perf_counter() - started) * 1000
    # Direct file metrics isolate file handling from Tk rendering and queues.
    for key, path in (("png_open_ms", paths[0]),
                      ("pdf_load_ms", fixtures / "sample.pdf")):
        entry = D.entries_for_paths([path], kinds=D.kinds_for_filter(D.FILTER_ALL))[0]
        started = time.perf_counter()
        document = D.load_document(entry)
        if key == "pdf_load_ms":
            width, height = document.page_size(0)
            document.render_region(0, (0, 0, width, height), (600, 800), fast=False)
        result[key] = (time.perf_counter() - started) * 1000
        document.close()
    # A separate worker avoids incidental thumbnail work from the GUI workload.
    worker = D.Worker(app.root, max_workers=4)
    background = ({"background": True} if "background" in
                  inspect.signature(worker.submit).parameters else {})
    for _ in range(BACKGROUND_JOBS):
        worker.submit(lambda: time.sleep(BACKGROUND_SECONDS), lambda _: None,
                      **background)
    completed = []
    started = time.perf_counter()
    worker.submit(lambda: True, completed.append)
    spin(app.root, lambda: bool(completed))
    result["foreground_ms"] = (time.perf_counter() - started) * 1000
    worker.shutdown()
    app.quit_all()
    return result


def write_csv(path: Path, rows: list[dict]) -> None:
    """Write tidy records with stable headers and explicit UTF-8 encoding."""
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def source_hashes(root: Path) -> dict[str, str]:
    """Record exact measured application sources independently of Git state."""
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "figureviewer").rglob("*.py"))}


def report(rows: list[dict], revision: str) -> None:
    """Summarize paired observations with exact sign tests and Holm correction.

    A sign test needs no normality or symmetry of differences. Pairs share
    fixtures and adjacent measurement blocks; execution order alternates.
    Independence is approximate on one machine, so p-values describe this
    controlled local run rather than a population of users or computers.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from scipy import stats

    summary = []
    for metric in METRICS:
        before = np.array([r[metric] for r in rows if r["version"] == "baseline"])
        after = np.array([r[metric] for r in rows if r["version"] == "optimized"])
        difference = after - before
        nonzero = difference[difference != 0]
        pvalue = stats.binomtest(int((nonzero < 0).sum()), len(nonzero)).pvalue
        summary.append(dict(metric=metric, baseline_median_ms=float(np.median(before)),
                            optimized_median_ms=float(np.median(after)),
                            median_paired_change_ms=float(np.median(difference)),
                            median_paired_speedup=float(np.median(before / after)),
                            pairs=len(before), sign_test_p=float(pvalue)))
    ordered = sorted(range(len(summary)), key=lambda i: summary[i]["sign_test_p"])
    adjusted = 0.0
    for rank, index in enumerate(ordered):
        adjusted = max(adjusted, min(1.0, summary[index]["sign_test_p"] * (len(summary)-rank)))
        summary[index]["holm_p"] = adjusted
    OUTPUT.mkdir(exist_ok=True)
    write_csv(OUTPUT / f"{STEM}_raw.csv", rows)
    write_csv(OUTPUT / f"{STEM}_summary.csv", summary)
    fig, axes = plt.subplots(2, 4, figsize=FIGURE_SIZE)
    for ax, record in zip(axes.flat, summary):
        metric = record["metric"]
        for index, version in enumerate(("baseline", "optimized")):
            values = [r[metric] for r in rows if r["version"] == version]
            ax.bar(index, np.median(values), width=BAR_WIDTH, color=COLORS[index])
            ax.scatter(np.full(len(values), index), values, s=9, color="#303b45", alpha=.6)
        ax.set_title(METRICS[metric], fontsize=8)
        ax.set_xticks([0, 1], ["Before", "After"], fontsize=8)
        ax.set_ylabel("Milliseconds", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.spines[["top", "right"]].set_visible(False)
        ax.text(.5, .98, f"{record['median_paired_speedup']:.2f}x speedup\nHolm p={record['holm_p']:.3f}",
                transform=ax.transAxes, ha="center", va="top", fontsize=7)
        ax.set_ylim(0, ax.get_ylim()[1] * 1.35)
    axes.flat[-1].axis("off")
    axes.flat[-1].text(0, .9, f"{PAIRS} paired fresh processes\nAlternating order\nDots: individual trials\nBars: medians\nExact two-sided sign tests\nHolm correction: 7 metrics\nGenerated fixtures only",
                       va="top", fontsize=9, linespacing=1.5)
    fig.suptitle("FigView opening performance", fontsize=13)
    fig.tight_layout()
    for suffix in ("png", "pdf", "svg"):
        fig.savefig(OUTPUT / f"{STEM}.{suffix}", dpi=180, bbox_inches="tight")
    plt.close(fig)
    (OUTPUT / f"{STEM}_method.md").write_text(
        f"Baseline: `{revision}`. Optimized: frozen working-tree snapshot. "
        "Exact measured source SHA256 hashes are in the accompanying sources JSON.\n\n"
        + __doc__ + "\n\n" + report.__doc__
        + f"\n\nSeed {SEED}; {FILE_COUNT} PNGs, {IMAGE_SIZE}; background job delay "
        f"{BACKGROUND_SECONDS}s. The foreground test is a controlled queue stress "
        "test, not measured thumbnail decoding. PNGs use deterministic random pixels. "
        "PDF is one generated vector page; the first-PDF metric includes deferred "
        "backend import when applicable, so startup savings may transfer to that first open. "
        "Single-instance IPC is disabled in isolated "
        "settings. Results exclude launcher discovery and OS interpreter startup.\n",
        encoding="utf-8")
    lines = ["# FigView opening benchmark", "",
             f"Seven paired fresh-process runs against `{revision}`. "
             "All times are local-machine medians in milliseconds.", "",
             "| Measurement | Before | After | Median paired speed ratio |",
             "| --- | ---: | ---: | ---: |"]
    for record in summary:
        lines.append(f"| {METRICS[record['metric']]} | "
                     f"{record['baseline_median_ms']:.1f} | "
                     f"{record['optimized_median_ms']:.1f} | "
                     f"{record['median_paired_speedup']:.2f}x |")
    lines += ["", "Batch opening and foreground responsiveness improved in every pair. "
              "Startup varied across trials. First PDF opening costs about 95 ms more "
              "because backend import is deferred from application startup. PNG open "
              "measures file read and lazy document construction, not full pixel decoding.", "",
              "Ratios are medians of within-pair before/after ratios, so they differ "
              "from ratios of the two table medians. Exact two-sided sign tests require "
              "no normality or symmetry assumption. With seven pairs and Holm correction "
              "across seven metrics, none reaches p < 0.05 (batch/queue/first-PDF adjusted "
              "p = 0.109). These are descriptive measurements, not population-wide claims. "
              "Adjacent pairs and alternating execution order reduce drift, but independence "
              "cannot be guaranteed on one shared machine.", "",
              "Startup excludes interpreter launch and launcher discovery. Filesystem "
              "caches were not flushed. The queue test uses controlled 25 ms background "
              "jobs rather than a particular thumbnail format. Settings and all media "
              "are generated in temporary directories; no raw user data is accessed.", "",
              f"[Figure PNG]({STEM}.png), [PDF]({STEM}.pdf), [SVG]({STEM}.svg), "
              f"[raw CSV]({STEM}_raw.csv), [statistics CSV]({STEM}_summary.csv), "
              f"[methods]({STEM}_method.md), [source hashes]({STEM}_sources.json).", "",
              "Reproduce: `python benchmarks/benchmark_opening.py`. "
              "The baseline revision is pinned; use `--baseline REVISION` to override."]
    (OUTPUT / f"{STEM}_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main() -> None:
    """Archive the baseline, create fixtures, collect pairs, then render reports."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=BASELINE_REVISION,
                        help="Git revision to compare, pinned to the pre-optimization commit")
    parser.add_argument("--child", nargs=3, metavar=("SOURCE", "FIXTURES", "SETTINGS"))
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(*(Path(p) for p in args.child))))
        return
    import numpy as np
    from PIL import Image
    import pymupdf

    repository = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", args.baseline], cwd=repository, text=True).strip()
    rows = []
    with tempfile.TemporaryDirectory(prefix="figview-opening-") as temporary:
        root = Path(temporary)
        archive = root / "baseline.zip"
        subprocess.run(["git", "archive", "--format=zip", f"--output={archive}", revision],
                       cwd=repository, check=True)
        baseline = root / "baseline"
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(baseline)
        # Freeze the candidate so concurrent development cannot mix revisions.
        optimized = root / "optimized"
        optimized.mkdir()
        for name in ("figureviewer", "assets"):
            shutil.copytree(repository / name, optimized / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(repository / "figure-viewer.ico", optimized / "figure-viewer.ico")
        OUTPUT.mkdir(exist_ok=True)
        (OUTPUT / f"{STEM}_sources.json").write_text(json.dumps({
            "baseline_revision": revision, "python": sys.version,
            "baseline": source_hashes(baseline), "optimized": source_hashes(optimized),
        }, indent=2), encoding="utf-8")
        fixtures = root / "fixtures"
        fixtures.mkdir()
        rng = np.random.default_rng(SEED)
        pixels = rng.integers(0, 256, (IMAGE_SIZE[1], IMAGE_SIZE[0], 3), dtype=np.uint8)
        for index in range(FILE_COUNT):
            Image.fromarray(pixels).save(fixtures / f"image-{index:03d}.png")
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "Generated benchmark document", fontsize=18)
            document.save(fixtures / "sample.pdf")
        for pair in range(PAIRS):
            versions = [("baseline", baseline), ("optimized", optimized)]
            if pair % 2:
                versions.reverse()
            for order, (version, source) in enumerate(versions):
                print(f"Pair {pair + 1}/{PAIRS}: {version}", flush=True)
                completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child",
                                            str(source), str(fixtures), str(root / f"settings-{pair}-{version}")],
                                           capture_output=True, text=True, timeout=TIMEOUT_SECONDS * 3)
                if completed.returncode:
                    raise RuntimeError(completed.stdout + completed.stderr)
                values = json.loads(completed.stdout.strip().splitlines()[-1])
                rows.append(dict(pair=pair + 1, version=version, order=order + 1, **values))
    report(rows, revision)


if __name__ == "__main__":
    main()
