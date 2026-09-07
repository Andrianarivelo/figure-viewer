Baseline: `d2e562d4665a7f1756a55385c99abf7233622f1b`. Optimized: frozen working-tree snapshot. Exact measured source SHA256 hashes are in the accompanying sources JSON.

Reproduce paired opening benchmarks with generated media and isolated settings.

Run from an IDE or ``python benchmarks/benchmark_opening.py``. The pinned baseline
is archived into a temporary directory; no checkout or user settings/media change.
Fresh child processes alternate execution order to reduce drift. Startup means
first mapped viewer, including application imports but excluding interpreter
launch. Filesystem caches are deliberately not flushed.


Summarize paired observations with exact sign tests and Holm correction.

A sign test needs no normality or symmetry of differences. Pairs share
fixtures and adjacent measurement blocks; execution order alternates.
Independence is approximate on one machine, so p-values describe this
controlled local run rather than a population of users or computers.


Seed 20260907; 100 PNGs, (1536, 1024); background job delay 0.025s. The foreground test is a controlled queue stress test, not measured thumbnail decoding. PNGs use deterministic random pixels. PDF is one generated vector page; the first-PDF metric includes deferred backend import when applicable, so startup savings may transfer to that first open. Single-instance IPC is disabled in isolated settings. Results exclude launcher discovery and OS interpreter startup.
