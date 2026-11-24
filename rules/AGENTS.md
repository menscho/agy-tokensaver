# Token Saver (output=6x input)

1. `ArtifactMetadata` only in brain artifact dir.
2. Never write files via `run_command`; use file tools.
3. Prefer `edit_file` (1-3 anchors); rewrite via `write_file` only for >60% edits or new files; never echo code.
4. Batch parallel calls; grep first; delegate bulk/logs/>3 images to scouts and >=5-file jobs to `lean-workflow`.
5. Narrowest check first; stop once criteria pass.
