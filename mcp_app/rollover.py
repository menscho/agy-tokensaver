"""Causal adaptive + cold-cache rollover policy and session simulator."""
from __future__ import annotations

from typing import Any

from config import (
    AUTO_CAP_MAX, AUTO_CAP_MIN, COLD_MIN_CTX, COLD_START_TOK, DELEGATED_EXTRA_TOK,
    DELEGATED_POST_DROP_MIN_CTX, FRESH_CTX_TOK, PACKET_REPLY_OUT_TOK,
    POLICY_CONTINUE_PRIOR, POLICY_DROP_PRIOR, POLICY_DROP_PRIOR_SHORT_TTL, POLICY_HORIZON_TURNS,
    POLICY_MARGIN, REDISCOVERY_TAX_TOK, W_CR, W_IN, W_OUT, WARMUP_TURNS, _SIM_CFG,
)

class RolloverTracker:
    """Causal per-conversation features, updated one generation at a time.
    The SAME tracker feeds the live HUD/tick (over the real chat) and the counterfactual simulation
    (over the simulated chat), so the benchmark measures the policy that ships."""

    __slots__ = ("turns", "ctx", "peak", "growth", "drops", "compactions", "warm_in", "warm_cr",
                 "warm_miss", "_own_warm")

    def __init__(self, warm_miss: list[float] | None = None) -> None:
        self.turns = 0
        self.ctx = 0
        self.peak = 0
        self.growth = 0.0
        self.drops = 0
        self.compactions = 0
        self.warm_in = 0
        self.warm_cr = 0
        self.warm_miss: list[float] = list(warm_miss) if warm_miss is not None else []
        self._own_warm = warm_miss is None

    def classify(self, inp: int, cr: int) -> str:
        ctx, prev = inp + cr, self.ctx
        if self.turns == 0:
            return "first"
        if prev >= 20_000 and ctx < prev * 0.6:
            return "compaction"            # context shrank: Antigravity checkpoint / truncation
        if prev >= 4_000 and cr < prev * 0.7:
            return "warmup" if self.turns <= WARMUP_TURNS else "drop"   # prefix not served from cache
        return "turn"

    def push(self, inp: int, cr: int, out: int = 0) -> str:
        ev = self.classify(inp, cr)
        ctx, prev = inp + cr, self.ctx
        if ev == "compaction":
            self.compactions += 1
        elif ev == "drop":
            self.drops += 1
        if self._own_warm and 1 <= self.turns <= WARMUP_TURNS and prev > 0:
            self.warm_miss.append(min(1.0, max(0.0, (prev - cr) / prev)))
        if self.turns > 0 and ev != "compaction":
            d = ctx - prev
            if d > 0:
                self.growth = float(d) if self.growth == 0 else 0.7 * self.growth + 0.3 * d
        if self.turns > 0:
            self.warm_in += inp
            self.warm_cr += cr
        self.turns += 1
        self.ctx = ctx
        if ctx > self.peak:
            self.peak = ctx
        return ev

    def features(self) -> dict[str, Any]:
        wden = self.warm_in + self.warm_cr
        return {
            "ctx": self.ctx,
            "turns": self.turns,
            "growth": int(self.growth),
            "drops": self.drops,
            "compactions": self.compactions,
            "warm_turns": max(0, self.turns - 1 - WARMUP_TURNS),
            "warm_hit_pct": round(self.warm_cr / wden * 100.0, 1) if wden else 0.0,
            "warm_miss": [round(m, 3) for m in self.warm_miss[:WARMUP_TURNS]],
        }


def rollover_cost_tok(feat: dict[str, Any] | None = None, cold_start: int = COLD_START_TOK,
                      rediscovery_tax: int = REDISCOVERY_TAX_TOK) -> int:
    """Uncached tokens a rollover costs: cold start + re-read tax + expected warm-up cache misses of
    the new chat (this chat's own measured warm-up pattern applied to the new chat's size)."""
    base = cold_start + rediscovery_tax
    wm = (feat or {}).get("warm_miss") or []
    return int(base + sum(min(1.0, max(0.0, float(m))) for m in wm[:WARMUP_TURNS]) * base)


def rollover_policy(feat: dict[str, Any] | None, cfg: dict[str, Any] | None = None,
                    cold_start: int = COLD_START_TOK, rediscovery_tax: int = REDISCOVERY_TAX_TOK) -> dict[str, Any]:
    """THE rollover policy (live tick, HUD threshold and counterfactual all call this).
    Causal: uses only the current chat's context size, turn count, cache-drop history and measured
    warm-up behaviour. Auto mode picks the smallest context C at which a rollover pays back
    POLICY_MARGIN x its cost within POLICY_HORIZON_TURNS turns:
        gain/turn = w * (C - cost),  w = 0.05 + p_drop * 0.95  (cache read + expected TTL-drop rebuild)
        net cost  = cost + warmup - 0.05 * C  (the replaced old-chat turn is not paid)
    """
    cfg = cfg or _SIM_CFG
    f = feat or {}
    cost = rollover_cost_tok(f, cold_start, rediscovery_tax)
    min_turns = int(str(cfg.get("min_turns", 14)))
    tmode = str(cfg.get("threshold_mode", "auto"))
    drops = int(f.get("drops", 0))
    warm_turns = int(f.get("warm_turns", 0))
    ttl_cliff = int(f.get("ttl_cliff_sec") or 150)
    a, b = POLICY_DROP_PRIOR_SHORT_TTL if ttl_cliff <= 90 else POLICY_DROP_PRIOR
    p_drop = (drops + a) / (warm_turns + b)
    w = W_CR + min(0.5, p_drop) * (W_IN - W_CR)
    p_cont = min(1.0, max(0.0, float(f.get("p_continue", cfg.get("p_continue", 1.0)))))
    paused = False
    if tmode != "auto" and tmode.isdigit():
        cap = int(tmode) * 1000
        is_auto = False
        reason = f"Manual threshold fixed at {cap // 1000}k tokens."
    else:
        h, m = POLICY_HORIZON_TURNS * p_cont, POLICY_MARGIN
        cap_raw = (m * cost + h * w * cost) / (h * w + m * W_CR) if h > 0 else float("inf")
        paused = cap_raw > AUTO_CAP_MAX
        cap = int(min(AUTO_CAP_MAX, max(AUTO_CAP_MIN, cap_raw)) // 1000 * 1000)
        is_auto = True
        if paused:
            reason = (f"Auto rollover paused: only ~{p_cont * 100:.0f}% of your past settled long chats "
                      f"continued afterwards, so a ~{cost // 1000}k rollover would not pay back.")
        else:
            reason = (f"Auto {cap // 1000}k: rollover costs ~{cost // 1000}k uncached; with "
                      f"{drops} cache drop(s) in {warm_turns} warm turns (~{p_drop * 100:.1f}%/turn) it pays back "
                      f"{m:g}x within {h:.0f} expected turns above {cap // 1000}k.")
    ctx = int(f.get("ctx", 0))
    gain_turn = w * max(0, ctx - cost)
    be = (cost - W_CR * ctx) / gain_turn if gain_turn > 0 else None
    roll = bool(f) and not paused and ctx >= cap and int(f.get("turns", 0)) >= min_turns
    return {
        "paused": paused,
        "p_continue": round(p_cont, 3),
        "cap": cap,
        "cap_k": cap // 1000,
        "is_auto": is_auto,
        "reason": reason,
        "roll": roll,
        "cost_tok": cost,
        "p_drop": round(p_drop, 4),
        "break_even_turns": round(be, 1) if be is not None and be > 0 else 0.0,
    }


def _simulate_rollover_session(
    turns: list[tuple[int, int, int]],
    cap: int | None = None,
    rediscovery_tax: int = REDISCOVERY_TAX_TOK,
    settled: list[bool] | None = None,
    cfg: dict[str, Any] | None = None,
    cold_start: int = COLD_START_TOK,
    detail: bool = False,
    gaps: list[float | None] | None = None,
    ttl_cliff: int = 150,
    on_user_turn: bool = False,
    cold_min_ctx: int = COLD_MIN_CTX,
    delegated: bool = False,
) -> Any:
    """
    Causal counterfactual: replays the session generation by generation and lets `rollover_policy`
    (the live policy) decide after each generation whether to roll over - it never sees the future.
      * cap=None -> auto policy (the shipped default); cap=int -> manual threshold at that cap.
      * settled  -> optional per-generation flags (see _settled_flags): rollover may only fire after
        generations where the live tick could fire. None = assume every generation is eligible
        (UPPER BOUND: the live tick cannot roll over in the middle of an agent run).
      * per-conversation turn counter: after a rollover the new chat must reach min_turns again.
      * cost of a rollover: the NEXT generation runs in the new chat with cold_start +
        rediscovery_tax + its real new content all uncached, followed by this session's own
        measured warm-up cache misses; a rollover after the final generation is still paid
        (cold start + PACKET_REPLY_OUT_TOK output folded into input-equivalents).
      * real TTL cache drops are replayed on the (smaller) simulated context; a real compaction
        (context shrinks > 40%) is NOT a cache drop: the smaller simulated chat keeps its warm cache
        (unless it is at least as large as the real chat was, then it compacts the same way).
    Returns (sim_in, sim_cr, rollovers) [, detail list when detail=True].
    """
    log: list[dict[str, Any]] = []
    if not turns:
        return (0.0, 0.0, 0, log) if detail else (0.0, 0.0, 0)
    pcfg = dict(cfg or _SIM_CFG)
    if cap is not None:
        pcfg["threshold_mode"] = str(max(1, int(cap) // 1000))
    n = len(turns)
    base = cold_start + rediscovery_tax
    real = RolloverTracker()
    conv = RolloverTracker()
    sim_in = sim_cr = 0.0
    rollovers = 0
    rolled = False
    pending = False
    j = 0                      # generation index inside the current simulated (new) chat
    sim_ctx = 0
    for idx, (inp, cr, out) in enumerate(turns):
        prev_real = real.ctx
        g_before = real.growth
        ev = real.push(inp, cr, out)
        if not rolled:
            ti, tc = inp, cr
            sim_ctx = inp + cr
        else:
            old = sim_ctx
            delta = (inp + cr) - prev_real
            if pending:
                new = max(0, delta) if ev != "compaction" else int(g_before)
                sim_ctx = base + new
                ti, tc = sim_ctx, 0
                pending = False
                j = 0
            else:
                j += 1
                if ev == "compaction":
                    new = int(g_before)
                    if old + new >= prev_real * 0.95:      # as large as the real chat -> compacts too
                        sim_ctx = inp + cr
                        ti, tc = inp, cr
                    else:
                        sim_ctx = old + new
                        ti, tc = min(inp, sim_ctx), max(0, sim_ctx - min(inp, sim_ctx))
                else:
                    sim_ctx = max(FRESH_CTX_TOK, old + delta)
                    if ev == "drop":
                        ti, tc = sim_ctx, 0
                    else:
                        wm = real.warm_miss
                        miss = wm[j - 1] if 1 <= j <= len(wm) and j <= WARMUP_TURNS else 0.0
                        ti = int(min(sim_ctx, min(inp, sim_ctx) + miss * old))
                        tc = sim_ctx - ti
        sim_in += ti
        sim_cr += tc
        conv.push(int(ti), int(tc), out)
        c_feat = conv.features()
        if ttl_cliff <= 90:
            c_feat["ttl_cliff_sec"] = ttl_cliff
        pol_cold = cold_start + (DELEGATED_EXTRA_TOK if delegated else 0)
        pol = rollover_policy(c_feat, pcfg, pol_cold, rediscovery_tax)
        is_settled_now = (settled is None) or (idx < len(settled) and settled[idx])
        # PreInvocation hooks only fire when the user actually sends a next turn (never after the final turn).
        if on_user_turn and idx == n - 1:
            is_settled_now = False
        # Causal Cold-Cache Auto-Handoff: only yields Turn-0 payback in Direct mode (not delegated Path 2).
        g_after = gaps[idx] if (gaps is not None and idx < len(gaps)) else None
        cold_ttl = ttl_cliff if ttl_cliff <= 90 else 240
        cold_roll = bool(
            not delegated
            and is_settled_now
            and g_after is not None
            and g_after >= cold_ttl
            and conv.ctx >= cold_min_ctx
            and conv.turns >= (4 if on_user_turn else 6)
        )
        post_drop_floor = DELEGATED_POST_DROP_MIN_CTX if delegated else AUTO_CAP_MIN
        post_drop_roll = bool(
            on_user_turn
            and is_settled_now
            and conv.drops >= 1
            and conv.ctx >= post_drop_floor
            and conv.turns >= 6
        )
        if (pol["roll"] and is_settled_now) or cold_roll or post_drop_roll:
            rollovers += 1
            rolled = True
            if detail:
                log.append({"after_turn": idx, "ctx": conv.ctx, "conv_turns": conv.turns, "cap": pol["cap"],
                            "remaining": n - 1 - idx})
            if idx == n - 1:
                sim_in += base + PACKET_REPLY_OUT_TOK * W_OUT / W_IN   # spawned chat nobody used
            else:
                if delegated:
                    # Path 2 executes 1 delegated LLM turn in the old chat (Turn 2 is terminated by hook).
                    next_cold = g_after is not None and g_after >= ttl_cliff
                    if next_cold:
                        sim_in += sim_ctx + DELEGATED_EXTRA_TOK
                    else:
                        sim_cr += sim_ctx
                        sim_in += DELEGATED_EXTRA_TOK
                pending = True
            conv = RolloverTracker(warm_miss=real.warm_miss)
    if detail:
        return sim_in, sim_cr, rollovers, log
    return sim_in, sim_cr, rollovers


# ----------------------------------------------------------------------------- session parsing

def _p_continue(continued: int, points: int) -> float:
    a, b = POLICY_CONTINUE_PRIOR
    return (continued + a) / (points + b)


def continuation_probability(sessions: list[dict[str, Any]], exclude: str = "") -> float:
    """Share of past settled long-chat points (live tick could have rolled over) that the user
    followed with more generations - learned from this machine's own history, causal for the
    current chat (the current chat itself is excluded)."""
    pts = cont = 0
    for s in sessions:
        if s.get("conversation_id") == exclude:
            continue
        pts += int(s.get("settled_hi", 0))
        cont += int(s.get("settled_hi_continued", 0))
    return _p_continue(cont, pts)


def compute_adaptive_threshold(active: dict[str, Any] | None, cfg: dict[str, Any]) -> dict[str, Any]:
    """Live rollover threshold for the current chat = rollover_policy (same function the
    counterfactual simulation uses)."""
    if not active:
        pol = rollover_policy({"ctx": 0, "turns": 0, "drops": 0, "warm_turns": 0, "warm_miss": []}, cfg)
        if pol["is_auto"]:
            pol["reason"] = (f"Auto baseline {pol['cap_k']}k (rollover cost ~{pol['cost_tok'] // 1000}k) - "
                             "adapts live to cache drops and warm-up misses.")
        pol["roll"] = False
        return pol
    feat = active.get("rollover_features")
    if not feat:
        feat = {
            "ctx": int(active.get("cur_tot", 0)),
            "turns": int(active.get("turns", 0)),
            "drops": int(active.get("drops", 0)),
            "warm_turns": max(0, int(active.get("turns", 0)) - 1 - WARMUP_TURNS),
            "warm_miss": [],
        }
    if "p_continue" in active:
        feat = {**feat, "p_continue": active["p_continue"]}
    if active.get("ttl_cliff_sec"):
        feat = {**feat, "ttl_cliff_sec": active["ttl_cliff_sec"]}
    return rollover_policy(feat, cfg)


