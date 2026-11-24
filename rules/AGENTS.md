# Token Saver (output = 6x input)

1. `ArtifactMetadata` only for files in the brain artifact dir; on other paths `write_to_file`/`write_file` fails and the whole file is re-sent.
2. Never write files via `run_command` (python -c, node -e, here-strings, base64); use file tools and fix failures, don't re-send.
3. Edit existing files with 1-3 unique anchor lines; rewrite only if >60% changes; never echo code.
4. Batch independent calls; grep first; slice big files; skip lockfiles, build/vendor, minified, raw logs.
5. Delegate bulk scouting, long logs, >3 images, web research to subagents; >=5-file jobs to `lean-workflow`.
6. Narrowest check first; diagnose before retrying; brief replies; suggest `/handoff` near 100k.
