#!/usr/bin/env python3
"""Local telemetry aggregator for /token-stats (streaming, null-safe, fresh-mtime skip)."""

import argparse
import collections
import json
import os
import time
from pathlib import Path


def get_data_dirs() -> list[Path]:
    dirs: list[Path] = []
    if os.environ.get("PLUGIN_DATA"):
        dirs.append(Path(os.environ["PLUGIN_DATA"]))
    agy_pd = Path.home() / ".gemini" / "antigravity" / "plugin_data" / "token-saver"
    if agy_pd.is_dir():
        dirs.append(agy_pd)
    plugin_root = Path(os.environ.get("PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    dirs.append(plugin_root / ".data")
    seen = set()
    uniq = []
    for d in dirs:
        rp = str(d.resolve()) if d.exists() else str(d)
        if rp not in seen:
            seen.add(rp)
            uniq.append(d)
    return uniq


def parse_transcript(tx_path: Path) -> dict | None:
    turns = total_in = total_cache = total_out = warm_in = warm_cache = cache_drops = tail_2049 = prev_cache = 0
    models: set[str] = set()
    try:
        with tx_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"PLANNER_RESPONSE"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "PLANNER_RESPONSE":
                    continue
                inp = int(obj.get("input_tokens") or 0)
                cache = int(obj.get("cache_read_tokens") or 0)
                out = int(obj.get("output_tokens") or 0)
                if inp == 0 and cache == 0 and out == 0:
                    continue
                turns += 1
                total_in += inp
                total_cache += cache
                total_out += out
                if obj.get("model"):
                    models.add(str(obj["model"]))
                if turns > 1:
                    warm_in += inp
                    warm_cache += cache
                    if prev_cache >= 4000 and cache < prev_cache * 0.8:
                        cache_drops += 1
                    if 2048 <= inp <= 2060 and cache >= 4000:
                        tail_2049 += 1
                prev_cache = cache
    except OSError:
        return None
    if turns == 0:
        return None
    denom = total_in + total_cache
    warm_denom = warm_in + warm_cache
    return {
        "updated_ts": int(tx_path.stat().st_mtime),
        "turns": turns,
        "models": sorted(models),
        "uncached_input_tokens": total_in,
        "cache_read_tokens": total_cache,
        "output_tokens": total_out,
        "output_per_turn": total_out // turns,
        "cache_hit_rate": round(total_cache / denom, 4) if denom else 0.0,
        "warm_cache_hit_rate": round(warm_cache / warm_denom, 4) if warm_denom else 0.0,
        "cache_drops": cache_drops,
        "tail_2049_hits": tail_2049,
    }


def scan_live_transcripts(cutoff: float, existing: dict, limit: int = 35) -> dict:
    brain = Path.home() / ".gemini" / "antigravity" / "brain"
    if not brain.is_dir():
        return {}
    candidates = []
    for p in brain.glob("*/.system_generated/logs/transcript.jsonl"):
        try:
            mt = p.stat().st_mtime
            if mt >= cutoff:
                candidates.append((mt, p))
        except OSError:
            continue
    candidates.sort(reverse=True)
    out = {}
    for mt, tx in candidates[:limit]:
        cid = tx.parents[2].name
        # Skip re-parsing if sessions.json is already fresh
        if cid in existing and existing[cid].get("updated_ts", 0) >= int(mt) and "tail_2049_hits" in existing[cid]:
            continue
        parsed = parse_transcript(tx)
        if parsed:
            out[cid] = parsed
    return out


def fmt_num(n: int) -> str:
    return f"{n:,}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="Filter to last N days (0 = all time)")
    ap.add_argument("--sessions", action="store_true", help="Show recent per-session cache breakdown")
    args = ap.parse_args()

    data_dirs = get_data_dirs()
    cutoff = time.time() - (args.days * 86400) if args.days > 0 else 0

    events = []
    sessions = {}
    for d in data_dirs:
        ev_file = d / "events.jsonl"
        if ev_file.is_file():
            with ev_file.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                        if (ev.get("ts") or 0) >= cutoff:
                            events.append(ev)
                    except Exception:
                        continue
        s_file = d / "sessions.json"
        if s_file.is_file():
            try:
                for k, v in json.loads(s_file.read_text(encoding="utf-8")).items():
                    if isinstance(v, dict) and (v.get("updated_ts") or 0) >= cutoff:
                        sessions[k] = v
            except Exception:
                pass

    for cid, rec in scan_live_transcripts(cutoff, sessions).items():
        if cid not in sessions or rec["updated_ts"] >= (sessions[cid].get("updated_ts") or 0):
            sessions[cid] = rec

    scope = f"last {args.days}d" if args.days > 0 else "all time"
    print(f"=== Token Saver Telemetry ({scope}) ===")
    print(f"Data dir: {data_dirs[0]}\n")

    if events:
        total_raw = sum(int(e.get("raw_bytes") or 0) for e in events)
        total_kept = sum(int(e.get("kept_bytes") or 0) for e in events)
        total_saved_tok = sum(int(e.get("est_tokens_saved") or 0) for e in events)
        replay_est = total_saved_tok * 6
        pct = (1.0 - total_kept / total_raw) * 100 if total_raw > 0 else 0.0

        print("1. Direct Savings (Hooks & Output Compression)")
        print(f"   Events recorded        : {len(events)}")
        print(f"   Raw -> Kept bytes      : {fmt_num(total_raw)} -> {fmt_num(total_kept)} (-{pct:.1f}%)")
        print(f"   Est. direct tok saved  : ~{fmt_num(total_saved_tok)} tokens")
        print(f"   Est. w/ 6x turn replay : ~{fmt_num(replay_est)} context tokens avoided\n")

        by_key: dict[str, dict[str, int]] = collections.defaultdict(lambda: {"count": 0, "raw": 0, "kept": 0, "tok": 0})
        for e in events:
            k = f"{e.get('source', '?')}:{e.get('category', '?')} ({e.get('action', '?')})"
            b = by_key[k]
            b["count"] += 1
            b["raw"] += int(e.get("raw_bytes") or 0)
            b["kept"] += int(e.get("kept_bytes") or 0)
            b["tok"] += int(e.get("est_tokens_saved") or 0)

        print("2. Breakdown by Layer & Category")
        for k, b in sorted(by_key.items(), key=lambda kv: kv[1]["tok"], reverse=True):
            red = (1.0 - b["kept"] / b["raw"]) * 100 if b["raw"] > 0 else 0.0
            print(f"   {k:40s} | n={b['count']:3d} | -{red:4.1f}% | ~{fmt_num(b['tok'])} tok saved")
        print()
    else:
        print("1. Direct Savings (Hooks & Output Compression)")
        print("   No hook events logged yet (activates on wrapped commands, large reads, and write guards).\n")

    if sessions:
        s_list = list(sessions.values())
        tot_in = sum(int(s.get("uncached_input_tokens") or 0) for s in s_list)
        tot_cache = sum(int(s.get("cache_read_tokens") or 0) for s in s_list)
        tot_out = sum(int(s.get("output_tokens") or 0) for s in s_list)
        tot_turns = sum(int(s.get("turns") or 0) for s in s_list)
        tot_drops = sum(int(s.get("cache_drops") or 0) for s in s_list)
        tot_2049 = sum(int(s.get("tail_2049_hits") or 0) for s in s_list)
        warm_turns = max(1, tot_turns - len(s_list))
        overall_hr = (tot_cache / (tot_in + tot_cache) * 100) if (tot_in + tot_cache) else 0.0
        warm_hrs = [float(s.get("warm_cache_hit_rate") or 0) * 100 for s in s_list if (s.get("turns") or 0) > 1]
        avg_warm_hr = sum(warm_hrs) / len(warm_hrs) if warm_hrs else 0.0
        out_per_turn = tot_out // max(1, tot_turns)
        # Weighted cost share at Gemini 4 ratios (0.05x cache : 1.0x uncached : 6.0x output)
        w_cr = tot_cache * 0.05
        w_in = tot_in * 1.0
        w_out = tot_out * 6.0
        w_tot = max(1.0, w_cr + w_in + w_out)

        print("3. Real Prompt Cache-Hit & Quota-Weight Telemetry (Antigravity transcripts)")
        print(f"   Sessions tracked       : {len(s_list)} ({tot_turns} planner turns)")
        print(f"   Cache read tokens      : {fmt_num(tot_cache)} ({w_cr/w_tot*100:.1f}% of weighted quota @0.05x)")
        print(f"   Uncached input tokens  : {fmt_num(tot_in)} ({w_in/w_tot*100:.1f}% of weighted quota @1.0x)")
        print(f"   Output tokens          : {fmt_num(tot_out)} ({out_per_turn:,}/turn; {w_out/w_tot*100:.1f}% of quota @6x)")
        print(f"   Overall cache-hit rate : {overall_hr:.1f}% (avg warm turns: {avg_warm_hr:.1f}%)")
        print(f"   Detected cache drops   : {tot_drops}")
        print(f"   Gemini-4 2049-tail hits: {tot_2049}/{warm_turns} warm turns ({tot_2049/warm_turns*100:.1f}%)\n")

        if args.sessions:
            print("   Recent Sessions:")
            for cid, s in sorted(sessions.items(), key=lambda kv: kv[1].get("updated_ts") or 0, reverse=True)[:10]:
                print(
                    f"   - {cid[:8]} | turns={int(s.get('turns') or 0):3d} | "
                    f"warm_hit={float(s.get('warm_cache_hit_rate') or 0)*100:5.1f}% | "
                    f"out/t={int(s.get('output_per_turn') or 0):4d} | "
                    f"drops={int(s.get('cache_drops') or 0):2d} | models={','.join(s.get('models') or [])}"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
