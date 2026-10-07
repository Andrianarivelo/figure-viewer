# In-place text editing benchmark

Previous editor: `reflow.commit_edit` (redact, then reinsert the paragraph as
HTML with `insert_htmlbox`), used up to commit 1736e6b.
New editor: `textedit` (real-font layout shared by the live preview and the
commit, text-only redaction, glyphs written with the matched font file).

Fixture: 30 generated blocks, one per page on a light-blue panel:
5 font families (Calibri, Arial, Times New Roman, Georgia, base-14
Helvetica) x 2 sizes (10, 14 pt) x 3 kinds (wrapped paragraph, label,
centred title). Edit applied: the word "modified" inserted after the first
word. No user file was read or written. Reproduce with
`python benchmarks/benchmark_inline_text.py`.

## Method

Paired design (same block, both editors). Shapiro-Wilk on the paired
differences rejected normality for most metrics (p < 0.001 for the position
shift and commit time; several metrics are zero-inflated), so every metric
uses the Wilcoxon signed-rank test, matched-pairs rank-biserial correlation
(rb) as effect size, and Holm correction across the six metrics.

## Results (medians, n = 30 pairs)

| Metric | Previous | New | p (Holm) | rb |
|---|---|---|---|---|
| First-line shift after edit (pt) | 1.64 (IQR 1.17) | 0 (IQR 0) | 6.9e-06 | -1.00 |
| Unchanged rewrite, mean pixel diff (0-255) | 33.8 (IQR 11.3) | 0 (IQR 0.08) | 1.1e-08 | -1.00 |
| Spans keeping the original font | 0 | 1 | 4.8e-06 | +1.00 |
| White-box pixels after edit | 0 | 0 | 1.0 (n.s.) | 0 |
| Font size change (pt) | 0 | 0 | 1.0 (n.s.) | 0 |
| Commit time (ms) | 53.6 (IQR 3.9) | 112.2 (IQR 63.6) | 2.1e-05 | +0.85 |

Live preview (new editor only, layout plus MuPDF render at zoom 2, median of
25 renders per block): median 1.6 ms, max 3.5 ms, far below a 16.7 ms frame.

## Interpretation

* The previous path moved text on every edit (HTML boxes place the first
  line by line box, not by baseline) and replaced the font (the HTML engine
  only knows its built-in fonts), so even retyping identical text visibly
  changed the block. The new path reproduces the original baselines exactly
  and keeps the font in every block.
* Neither path painted white boxes on the coloured panel in this PDF test:
  the "black bar" complaint came from the old Tk editing widget (a dark
  opaque `Text` widget), not from the saved PDF. The new editor has no
  widget over the text at all.
* Cost: commits are about 60 ms slower for installed fonts (bimodal: base-14
  blocks stay near 50 ms) because a whole font file is embedded; it is
  subset on save, so saved files stay small (an embedded Calibri adds tens of
  KB, not 800 KB). A commit happens once per edit, so this is not felt.
* Caveats: fixtures are synthetic single blocks; mixed-style real-world
  paragraphs, missing fonts and right-to-left scripts were not benchmarked.

Files: `inline_text_raw.csv`, `inline_text_statistics.csv`,
`inline_text_manifest.json`, `inline_text_benchmark.{png,pdf,svg}`.
