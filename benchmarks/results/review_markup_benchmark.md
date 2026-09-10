# Default markup microbenchmark

Baseline: `edf2f9c4526d5c748ee4387065e9ffe6a3d4749e`. Python: 3.13.13; PyMuPDF: 1.28.2.

24 paired samples, 40 additions per fresh session, 2 warmup pairs discarded. Execution order alternates. Each timing is milliseconds per add_markup with empty optional metadata. Opening, text search, save and close are outside the timed interval.

Median baseline: 1.1416 ms; median current: 1.1244 ms.

Descriptive results only: samples share one process and machine, and independence and distributional assumptions are not established. No significance test or speed superiority claim is appropriate. This does not measure the review UI, large documents, or Acrobat.

Reproduce from the repository root: `.venv-build/Scripts/python.exe benchmarks/results/review_benchmark.py`.
