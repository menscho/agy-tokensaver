## Summary
- What changed and why?

## Checklist
- [ ] `bash tests/run_all.sh` passes locally
- [ ] Zero third-party runtime dependencies added
- [ ] Hooks fail open (`{"decision":"allow"}` / `{}`) on error or malformed input
- [ ] Any compressor change includes updated golden fixtures (`tests/golden/`) and preserves byte-exact spill files
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] No personal paths, secrets, or unverified benchmark numbers committed
