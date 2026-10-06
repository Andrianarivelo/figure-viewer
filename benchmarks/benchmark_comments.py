"""Compare comment indexing with the prior revision using synthetic PDFs only.

Run in an IDE or with ``python benchmarks/benchmark_comments.py``. Paired
measurements alternate order; they describe one machine and are not independent
experimental observations, so no inferential statistical claim is made.
"""
from __future__ import annotations

import csv
import statistics
import sys
import tempfile
import time
from pathlib import Path

# Workload, baseline, output and figure options are configurable here.
BASELINE_REVISION = "721ecac12e6466fad322c781f13db37369d4293f"
PAIRS = 24
ANNOTATIONS = 100
REPEATS = 20
OUTPUT = Path(__file__).resolve().parent / "results" / "comments"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmarks" / "results"))
import review_benchmark as chart  # noqa: E402
from figureviewer import docs as D, pdfedit as current  # noqa: E402


def measure(module, entry):
    """Measure read-only indexing, excluding document opening and closing."""
    session = module.EditSession(entry)
    try:
        session.annots_on(0)  # Warm the PDF page and metadata caches.
        start = time.perf_counter_ns()
        for _ in range(REPEATS):
            session.annots_on(0)
        return (time.perf_counter_ns() - start) / REPEATS / 1_000_000
    finally:
        session.close()


def main():
    """Retain paired samples and export the shared chart in PNG, SVG and PDF."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    chart.BASELINE_REVISION = BASELINE_REVISION
    baseline = chart.baseline_module()
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic_comments.pdf"
        with D.fitz.open() as doc:
            page = doc.new_page()
            for index in range(ANNOTATIONS):
                annotation = page.add_text_annot((40 + index % 10 * 40,
                                                   40 + index // 10 * 40),
                                                  f"Reviewer comment {index}")
                annotation.set_info(title="External reviewer")
            doc.save(path)
        stat = path.stat()
        entry = D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)
        for pair in range(PAIRS):
            order = [("baseline", baseline), ("current", current)]
            if pair % 2:
                order.reverse()
            timings = {name: measure(module, entry) for name, module in order}
            rows.append({"pair": pair + 1, "first": order[0][0],
                         "baseline_ms": timings["baseline"],
                         "current_ms": timings["current"]})
    with (OUTPUT / "index_timings.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    chart.OUTPUT, chart.PAIRS, chart.ADDITIONS = OUTPUT, PAIRS, ANNOTATIONS
    chart.render_timings(rows, title="PDF comment indexing: paired timings",
                         subtitle="Milliseconds per page of 100 comments; lower is faster",
                         current_label="Comment display", workload="comments")
    medians = {name: statistics.median(row[f"{name}_ms"] for row in rows)
               for name in ("baseline", "current")}
    report = (f"Baseline {BASELINE_REVISION}; Python {sys.version.split()[0]}; "
              f"PyMuPDF {D.fitz.VersionBind}.\n"
              f"{PAIRS} paired samples, {REPEATS} indexes/sample, {ANNOTATIONS} comments/page.\n"
              f"Median baseline {medians['baseline']:.3f} ms; current {medians['current']:.3f} ms.\n"
              "Descriptive only. Shared-machine measurements do not establish independence; "
              "no significance test or superiority claim. Measures indexing, not click latency.\n")
    (OUTPUT / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
