# Performance & Hook Latency Budget

All measurements below were collected on Linux (Perl v5.40.1, `JSON::PP` 4.16) via `python3 scripts/benchmark.py --demo` and direct Perl microbenchmarks.

## Latency Budget

| Path | Target budget | Measured (median) | Notes |
| :--- | ---: | ---: | :--- |
| `cmd_hook.pl` fast-path miss (~83% of commands) | `< 10 ms` | `~2.5 ms` | Conservative keyword pre-check avoids loading `JSON::PP` and `cmd_hook_core.pl`. |
| `read_hook.pl` (active `JSON::PP` parse) | `< 30 ms` | `~14.1 ms` | Includes ~`6.1 ms` Perl `require JSON::PP` compile time + `0.03 ms` decode. |
| `cmd_hook.pl` hit (active `JSON::PP` parse) | `< 30 ms` | `~16.5 ms` | Loads `TsJson` (`JSON::PP`) + `cmd_hook_core.pl` quote-aware classifier. |
| `benchmark.py --demo` (8 hook hits) | `< 30 ms` | `17.1 ms` | Up from `8.9 ms` pre-`JSON::PP` (see below). |
| 10 MB malformed / oversized payload (all hooks) | `< 200 ms` | `16–28 ms` | `TsJson::read_stdin_capped()` stops reading past 2 MB and fails open to `allow`. |

## Why `JSON::PP` adds ~6–8 ms on active hook hits

Replacing regex JSON extraction (`json_get_str` / `json_get_int`) with core Perl `JSON::PP` (`scripts/lib/TsJson.pm`) eliminated key-spoofing, nesting, duplicate-key, and Unicode escape bugs with zero third-party dependencies.

Measured Perl module load floor (`perl -MTime::HiRes`):

| Operation | Median |
| :--- | ---: |
| Bare `perl -e 1` | `1.38 ms` |
| `require JSON::PP` (2,900 lines pure Perl) | `6.11 ms` |
| `JSON::PP->new->utf8(1)->decode(...)` (1 payload) | `0.03 ms` |

Because `benchmark.py --demo` executes 8 active hook hits (0 fast-path misses), its median hook latency moved from **`8.9 ms` → `17.1 ms`** (`+8.2 ms`). This exceeds a naive 2 ms delta solely due to `JSON::PP`'s fixed Perl compile time (`6.1 ms`), while:

- Fast-path misses (`cmd_hook.pl`, `write_hook.pl`, `swarm_hook.pl`) do not load `JSON::PP` and remain at **`~2.5 ms`**.
- Against a `1,000–15,000 ms` LLM turn and `5,000 ms` hook timeout, `17 ms` is ~`0.1%` of turn wall time.
- Dropping `use B ()` from `TsJson.pm` (using a Unicode-safe bitwise string check instead) saved `1.14 ms` per active hook call.

## Reproducing

```bash
python3 scripts/benchmark.py --demo
```
