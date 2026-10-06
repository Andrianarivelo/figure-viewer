"""Compare PDF comment and reply indexing against the previous review pane.

Run through an IDE or ``python benchmarks/benchmark_threads.py``. Only generated
PDFs are used. Paired timings are descriptive shared-machine observations;
independence assumptions do not justify inferential tests or speed claims.
"""
from __future__ import annotations

import csv
import statistics
import sys
import tempfile
from pathlib import Path

# Workload, baseline and report destinations are configurable here.
BASELINE_REVISION = "dc20acd"
PAIRS = 24
ROOT_COMMENTS = 20
REPLIES_PER_ROOT = 4
OUTPUT = Path(__file__).resolve().parent / "results" / "threads"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_comments as benchmark  # noqa: E402


def make_fixture(path, replies):
    """Create native imported threads without relying on either measured version."""
    fitz = benchmark.D.fitz
    with fitz.open() as doc:
        page = doc.new_page()
        for index in range(ROOT_COMMENTS):
            parent = page.add_text_annot((40 + index % 5 * 80, 40 + index // 5 * 80),
                                         f"Review comment {index}")
            parent.set_info(title="Original reviewer")
            for reply_index in range(replies):
                reply = page.add_text_annot((400, 40), f"Reply {reply_index} to comment {index}")
                reply.set_info(title="Responding reviewer", creationDate="D:20261006080000Z")
                reply.set_flags(fitz.PDF_ANNOT_IS_NO_VIEW)
                reply.set_irt_xref(parent.xref)
                doc.xref_set_key(reply.xref, "RT", "/R")
        doc.save(path)


def main():
    """Alternate versions for each workload and retain all paired raw timings."""
    chart = benchmark.chart
    chart.BASELINE_REVISION = BASELINE_REVISION
    baseline = chart.baseline_module()
    reports = []
    with tempfile.TemporaryDirectory() as directory:
        for workload, replies in (("comments", 0), ("threads", REPLIES_PER_ROOT)):
            destination = OUTPUT / workload
            destination.mkdir(parents=True, exist_ok=True)
            path = Path(directory) / f"{workload}.pdf"
            make_fixture(path, replies)
            stat = path.stat()
            entry = benchmark.D.Entry(path, stat.st_size, stat.st_mtime, benchmark.D.KIND_DOC)
            rows = []
            for pair in range(PAIRS):
                order = [("baseline", baseline), ("current", benchmark.current)]
                if pair % 2:
                    order.reverse()
                timings = {name: benchmark.measure(module, entry) for name, module in order}
                rows.append({"pair": pair + 1, "first": order[0][0],
                             "baseline_ms": timings["baseline"],
                             "current_ms": timings["current"]})
            with (destination / "index_timings.csv").open("w", newline="", encoding="utf-8") as out:
                writer = csv.DictWriter(out, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            total = ROOT_COMMENTS * (1 + replies)
            chart.OUTPUT, chart.PAIRS, chart.ADDITIONS = destination, PAIRS, total
            chart.render_timings(rows, title=f"PDF {workload}: paired indexing timings",
                                 subtitle=f"Milliseconds per page of {total} messages; lower is faster",
                                 current_label="Threaded review", workload="messages")
            medians = {name: statistics.median(row[f"{name}_ms"] for row in rows)
                       for name in ("baseline", "current")}
            reports.append(f"{workload}: {total} messages, median baseline "
                           f"{medians['baseline']:.3f} ms, current {medians['current']:.3f} ms.")
    report = (f"Baseline {BASELINE_REVISION}; Python {sys.version.split()[0]}; "
              f"PyMuPDF {benchmark.D.fitz.VersionBind}.\n"
              f"{PAIRS} paired samples/workload, {benchmark.REPEATS} indexes/sample.\n"
              + "\n".join(reports) + "\n"
              "Descriptive only: same-process observations do not establish independence. "
              "No significance test or superiority claim. Tests backend indexing, not GUI latency.\n")
    (OUTPUT / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
