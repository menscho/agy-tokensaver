# Token & Cache Efficiency

Zero-loss defaults:

1. **Minimal-anchor edits (6× output weight):** Use `edit_file` with 1–3 unique anchor lines only (never paste 15+ unchanged surrounding lines into `Target`/`Replacement`); `write_file` only for new files, <100L, or >60% rewrites; never echo edited code in chat.
2. **Batch & combine:** Parallelize independent reads, searches, distinct-file edits, and checks in one turn; merge sequential `ssh` checks into one remote command. Call tools directly without filler narration; read target definitions before editing.
3. **Slice & skip bloat:** Grep first; pass `StartLine`/`EndLine` on >250L files; reuse slices already in context. Skip lockfiles, vendor/build dirs, `.ipynb`/minified files, and raw `output.txt` logs (`ts_inspect.py` or grep/tail).
4. **Delegate bulk (skip 1–2 files):** `lite-sifter` for mechanical sifting, `fast-explorer` for >=3-file scouting, `triage-scout` for >150L logs/diffs/tests, `web-scout` for docs/web, `vision-scout` for >3 images, `lean-workflow` for >=5-file audits/refactors/test loops. Run `repo_digest.py` before swarms.
5. **Stop-on-green & cache hygiene:** Run narrowest check while iterating, full suite once at end; diagnose before retrying. Keep replies concise; suggest `/compact` or `/handoff` near 100k+ or before >2m breaks.
