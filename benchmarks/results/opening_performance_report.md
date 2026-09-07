# FigView opening benchmark

Seven paired fresh-process runs against `d2e562d4665a7f1756a55385c99abf7233622f1b`. All times are local-machine medians in milliseconds.

| Measurement | Before | After | Median paired speed ratio |
| --- | ---: | ---: | ---: |
| Fresh process to first window | 622.7 | 463.8 | 1.28x |
| 100 files: open call returns | 1116.9 | 47.7 | 15.94x |
| 100 files: complete sidebar | 2304.3 | 217.1 | 9.20x |
| 100 files: last image displayed | 3639.2 | 304.5 | 10.21x |
| PNG open (1536 x 1024) | 3.1 | 2.6 | 1.23x |
| First PDF open and render | 15.0 | 110.1 | 0.14x |
| Foreground behind 24 background jobs | 155.1 | 26.9 | 5.86x |

Batch opening and foreground responsiveness improved in every pair. Startup varied across trials. First PDF opening costs about 95 ms more because backend import is deferred from application startup. PNG open measures file read and lazy document construction, not full pixel decoding.

Ratios are medians of within-pair before/after ratios, so they differ from ratios of the two table medians. Exact two-sided sign tests require no normality or symmetry assumption. With seven pairs and Holm correction across seven metrics, none reaches p < 0.05 (batch/queue/first-PDF adjusted p = 0.109). These are descriptive measurements, not population-wide claims. Adjacent pairs and alternating execution order reduce drift, but independence cannot be guaranteed on one shared machine.

Startup excludes interpreter launch and launcher discovery. Filesystem caches were not flushed. The queue test uses controlled 25 ms background jobs rather than a particular thumbnail format. Settings and all media are generated in temporary directories; no raw user data is accessed.

[Figure PNG](opening_performance.png), [PDF](opening_performance.pdf), [SVG](opening_performance.svg), [raw CSV](opening_performance_raw.csv), [statistics CSV](opening_performance_summary.csv), [methods](opening_performance_method.md), [source hashes](opening_performance_sources.json).

Reproduce: `python benchmarks/benchmark_opening.py`. The baseline revision is pinned; use `--baseline REVISION` to override.

The final application also includes thumbnail-key cleanup added after the frozen benchmark snapshot. That small correctness fix was tested separately and is not represented by these timing measurements.
