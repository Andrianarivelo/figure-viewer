"""Compare real Tk review filtering before and after local status colors.

Run in an IDE or ``python benchmarks/benchmark_review_states.py``. Data is
generated, not user material. Timings cover widget/filter work, not disk writes
when marking a thread read. Results are descriptive, without inferential tests.
"""
from __future__ import annotations

import csv
import statistics
import subprocess
import sys
import tempfile
import time
import types
from contextlib import ExitStack
from pathlib import Path

# Configure the historical version, workload and plot destination here.
BASELINE_REVISION = "d5b32c7"
PAIRS = 16
FILTERS_PER_SAMPLE = 6
OUTPUT = Path(__file__).resolve().parent / "results" / "review_states"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmarks"))
import benchmark_threads as fixture  # noqa: E402
from tests.test_reviewui import ReviewUITests  # noqa: E402


def load_baseline():
    """Load the previous pane without replacing current application modules."""
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE_REVISION}:figureviewer/reviewui.py"],
        cwd=ROOT, text=True, encoding="utf-8")
    module = types.ModuleType("figureviewer._baseline_review_states")
    module.__package__ = "figureviewer"
    sys.modules[module.__name__] = module
    exec(compile(source, "baseline_reviewui.py", "exec"), module.__dict__)
    return module


def measure(controller):
    """Time identical list rebuilds, without selecting or marking messages read."""
    start = time.perf_counter_ns()
    for _ in range(FILTERS_PER_SAMPLE):
        controller._filter()
    return (time.perf_counter_ns() - start) / FILTERS_PER_SAMPLE / 1_000_000


def main():
    """Alternate implementations within one Tk application and export samples."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    tester = ReviewUITests()
    rows = []
    baseline_module = load_baseline()
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        path = root / "generated_threads.pdf"
        fixture.make_fixture(path, fixture.REPLIES_PER_ROOT)
        application = tester._start(stack, root, path)
        try:
            window = application.windows[0]
            tester._wait(application, lambda: window.document is not None)
            current = window.edit.review
            current.open_panel()
            tester.indexed(application, current, 100)
            baseline = baseline_module.ReviewController(window.edit)
            baseline._build_panel()
            baseline.rows = current.rows
            baseline._index_threads()
            for controller in (baseline, current):
                controller._filter()
            for pair in range(PAIRS):
                order = [("baseline", baseline), ("current", current)]
                if pair % 2:
                    order.reverse()
                timings = {name: measure(controller) for name, controller in order}
                rows.append({"pair": pair + 1, "first": order[0][0],
                             "baseline_ms": timings["baseline"],
                             "current_ms": timings["current"]})
                application.root.update()
        finally:
            application.quit_all()
    with (OUTPUT / "filter_timings.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    chart = fixture.benchmark.chart
    chart.OUTPUT, chart.PAIRS, chart.ADDITIONS = OUTPUT, PAIRS, 100
    chart.render_timings(rows, title="Review pane filtering with comment states",
                         subtitle="Milliseconds per list refresh of 100 messages; lower is faster",
                         current_label="State colors", workload="messages")
    medians = {name: statistics.median(row[f"{name}_ms"] for row in rows)
               for name in ("baseline", "current")}
    report = (f"Baseline {BASELINE_REVISION}; Python {sys.version.split()[0]}.\n"
              f"{PAIRS} alternating pairs; {FILTERS_PER_SAMPLE} filters/sample; 100 messages.\n"
              f"Median baseline {medians['baseline']:.3f} ms; current {medians['current']:.3f} ms.\n"
              "Descriptive only: shared-machine/process observations do not establish independence. "
              "No significance test or superiority claim. Excludes read-preference disk writes.\n")
    (OUTPUT / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
