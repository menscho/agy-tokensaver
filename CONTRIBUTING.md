# Contributing to agy-tokensaver

Thanks for contributing! Please follow the ground rules below so the plugin stays fast, fail-open, and dependency-free.

## Ground Rules

- **Zero third-party runtime dependencies:** only Perl 5 core modules (`JSON::PP`, `Digest::MD5`, `Fcntl`, `File::Path`) and the Python 3.9+ standard library.
- **Always fail-open:** any hook error, timeout, or malformed JSON input must exit `0` and emit `{"decision":"allow"}` (or `{}` for Post/Stop hooks).
- **Lossless spills:** any command compressor that drops content must leave the byte-exact original output in the `0600` spill file.
- **Small, focused commits:** plain imperative messages (e.g. `read_hook: skip leading SPDX banners`).

## Local Development Setup

```bash
git clone https://github.com/menscho/agy-tokensaver.git
cd agy-tokensaver
bash tests/run_all.sh
```

Optional dev-only linters (the same checks run in CI):

```bash
ruff check mcp_app scripts/*.py eval/*.py install.py tests/test_python_unit.py
mypy mcp_app scripts/*.py eval/*.py install.py tests/test_python_unit.py
perlcritic --profile .perlcriticrc --severity 4 scripts/*.pl scripts/lib/*.pm scripts/lib/*.pl scripts/compressors/*.pl
shellcheck -S error tests/*.sh install.sh Install-TokenSaver.command
```

## Adding a New Compressor

1. Add the compressor subroutine to the appropriate `scripts/compressors/<family>.pl` module and register it in `scripts/cmd_wrap.pl`'s `%CAT_FAMILY` map.
2. Ensure all hash-key iterations use deterministic sort tie-breakers (`|| $a cmp $b`).
3. Add at least **two** golden fixtures under `tests/golden/<name>_typical/` and `tests/golden/<name>_edge/` (with `meta.json` and `input.txt`).
4. Run `bash tests/update_golden.sh`, review `git diff tests/golden/` carefully, and confirm `bash tests/run_all.sh` passes.
5. Add a short entry under `## [Unreleased]` in `CHANGELOG.md`.
