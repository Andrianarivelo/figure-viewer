"""Reproduce paired default-markup timings against the pre-review revision.

Run from the repository root with the project Python interpreter. Measurements
exclude document opening, search, closing and serialization. Generated data is
synthetic and contains no user PDFs. This is a descriptive microbenchmark only.
"""
from __future__ import annotations

import csv
import statistics
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

# All workload choices are configurable here; keep revision fixed for reruns.
BASELINE_REVISION = "edf2f9c4526d5c748ee4387065e9ffe6a3d4749e"
PAIRS = 24
ADDITIONS = 40
WARMUP_PAIRS = 2
OUTPUT = Path(__file__).resolve().parent
ROOT = OUTPUT.parents[1]
FIGURE_WIDTH = 620
FIGURE_HEIGHT = 340
BASELINE_COLOR = "#526783"
CURRENT_COLOR = "#0d9488"
sys.path.insert(0, str(ROOT))
from figureviewer import docs as D  # noqa: E402
from figureviewer import pdfedit as current  # noqa: E402


def baseline_module():
    """Load the exact historical module without changing the working tree."""
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE_REVISION}:figureviewer/pdfedit.py"], cwd=ROOT,
        text=True, encoding="utf-8")
    module = types.ModuleType("figureviewer._review_baseline")
    module.__package__ = "figureviewer"
    sys.modules[module.__name__] = module
    exec(compile(source, "historical_pdfedit.py", "exec"), module.__dict__)
    return module


def measure(module, entry):
    """Time identical default calls on a fresh session for every sample."""
    session = module.EditSession(entry)
    try:
        quads = session._page(0).search_for("selectable", quads=True)
        start = time.perf_counter_ns()
        for _ in range(ADDITIONS):
            session.add_markup(0, quads, module.TYPE_HIGHLIGHT, (1, 0.8, 0.2), 0.35)
        return (time.perf_counter_ns() - start) / ADDITIONS / 1_000_000
    finally:
        session.close()


def main():
    """Alternate measurement order, retain paired raw timings and a report."""
    baseline = baseline_module()
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic.pdf"
        with D.fitz.open() as doc:
            doc.new_page().insert_text((72, 100), "A selectable sentence")
            doc.save(path)
        stat = path.stat()
        entry = D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)
        for pair in range(-WARMUP_PAIRS, PAIRS):
            order = [("baseline", baseline), ("current", current)]
            if pair % 2:
                order.reverse()
            timings = {name: measure(module, entry) for name, module in order}
            if pair >= 0:
                rows.append({"pair": pair + 1, "first": order[0][0],
                             "baseline_ms": timings["baseline"],
                             "current_ms": timings["current"],
                             "difference_ms": timings["current"] - timings["baseline"]})
    with (OUTPUT / "review_markup_timings.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    medians = {name: statistics.median(row[f"{name}_ms"] for row in rows)
               for name in ("baseline", "current")}
    render_timings(rows)
    report = (
        "# Default markup microbenchmark\n\n"
        f"Baseline: `{BASELINE_REVISION}`. Python: {sys.version.split()[0]}; "
        f"PyMuPDF: {D.fitz.VersionBind}.\n\n"
        f"{PAIRS} paired samples, {ADDITIONS} additions per fresh session, "
        f"{WARMUP_PAIRS} warmup pairs discarded. Execution order alternates. "
        "Each timing is milliseconds per add_markup with empty optional metadata. "
        "Opening, text search, save and close are outside the timed interval.\n\n"
        f"Median baseline: {medians['baseline']:.4f} ms; "
        f"median current: {medians['current']:.4f} ms.\n\n"
        "Descriptive results only: samples share one process and machine, and "
        "independence and distributional assumptions are not established. "
        "No significance test or speed superiority claim is appropriate. "
        "This does not measure the review UI, large documents, or Acrobat.\n\n"
        "Reproduce from the repository root: "
        "`.venv-build/Scripts/python.exe benchmarks/results/review_benchmark.py`.\n")
    (OUTPUT / "review_markup_benchmark.md").write_text(report, encoding="utf-8")
    print(report)


def render_timings(rows, *, title="Default PDF markup: paired timings",
                   subtitle="Milliseconds per addition; synthetic PDF; lower is faster",
                   current_label="Review version", workload="additions"):
    """Export a compact paired-time figure in SVG, PDF and PNG using MuPDF."""
    maximum = max(row[f"{name}_ms"] for row in rows
                  for name in ("baseline", "current")) * 1.08
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{FIGURE_WIDTH}" '
             f'height="{FIGURE_HEIGHT}" viewBox="0 0 {FIGURE_WIDTH} {FIGURE_HEIGHT}">',
             '<rect width="100%" height="100%" fill="white"/>',
             '<g font-family="sans-serif" fill="#25334a">',
             f'<text x="48" y="28" font-size="17">{title}</text>',
             f'<text x="48" y="48" font-size="11">{subtitle}</text>']
    for tick in range(5):
        value = maximum * tick / 4
        y = 266 - value / maximum * 190
        parts.extend([
            f'<path d="M 80 {y} H 548" stroke="#e5e9ef"/>',
            f'<text x="68" y="{y + 4}" text-anchor="end" font-size="11">{value:.2f}</text>'])
    for row in rows:
        left = 266 - row["baseline_ms"] / maximum * 190
        right = 266 - row["current_ms"] / maximum * 190
        parts.extend([
            f'<path d="M 220 {left} L 440 {right}" stroke="#ccd4df" stroke-width="1"/>',
            f'<circle cx="220" cy="{left}" r="3" fill="{BASELINE_COLOR}"/>',
            f'<circle cx="440" cy="{right}" r="3" fill="{CURRENT_COLOR}"/>'])
    parts.extend([
        '<text x="220" y="287" text-anchor="middle" font-size="12">Previous version</text>',
        f'<text x="440" y="287" text-anchor="middle" font-size="12">{current_label}</text>',
        f'<text x="48" y="319" font-size="11">{PAIRS} paired samples; {ADDITIONS} {workload}/sample. Descriptive comparison, no significance claim.</text>',
        '</g></svg>'])
    svg = "".join(parts)
    (OUTPUT / "review_markup_timings.svg").write_text(svg, encoding="utf-8")
    with D.fitz.open(stream=svg.encode(), filetype="svg") as graphic:
        data = graphic.convert_to_pdf()
    (OUTPUT / "review_markup_timings.pdf").write_bytes(data)
    with D.fitz.open(stream=data, filetype="pdf") as document:
        document[0].get_pixmap(dpi=150).save(OUTPUT / "review_markup_timings.png")


if __name__ == "__main__":
    main()
