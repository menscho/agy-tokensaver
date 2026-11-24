# Token Saver (output = 6x input)

1. `ArtifactMetadata` only in brain artifact dir; elsewhere `write_to_file`/`write_file` fails and re-sends.
2. Never write files via `run_command` (python -c, node -e, heredoc, base64); use file tools, fix errors in place.
3. Edit with `edit_file` (1-3 anchors); rewrite via `write_file` only if >60% changes or new file; never echo code.
4. Batch independent calls; grep first; slice big files; skip lockfiles/vendor/minified/raw logs.
5. Delegate bulk search/logs/>3 images/web to subagents; >=5-file jobs to `lean-workflow`.
6. Narrowest check first; diagnose before retrying; stop once criteria pass; `/handoff` near 100k.
