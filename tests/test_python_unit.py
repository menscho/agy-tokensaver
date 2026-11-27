#!/usr/bin/env python3
"""In-process Python unit tests for mcp_app/ and scripts/ (enables coverage.py reporting)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_app"))
sys.path.insert(0, str(ROOT / "scripts"))


class TestMcpAppModules(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="agy-pyunit-")
        self.tmp = Path(self._td.name)
        self._old_env = {
            k: os.environ.get(k)
            for k in (
                "PLUGIN_DATA",
                "AGY_TS_GEMINI_HOME",
                "AGY_TS_SYNC_RULES",
                "AGY_TS_SPILL_DIR",
                "ANTIGRAVITY_LS_ADDRESS",
                "ANTIGRAVITY_CSRF_TOKEN",
                "ANTIGRAVITY_PROJECT_ID",
                "ANTIGRAVITY_CONVERSATION_ID",
            )
        }
        os.environ["PLUGIN_DATA"] = str(self.tmp / "pdata")
        os.environ["AGY_TS_GEMINI_HOME"] = str(self.tmp / "gemini")
        os.environ["AGY_TS_SPILL_DIR"] = str(self.tmp / "spills")
        for k in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_PROJECT_ID", "ANTIGRAVITY_CONVERSATION_ID"):
            os.environ.pop(k, None)
        # Reload config-bound modules against sandbox dirs
        import importlib
        for modname in ("config", "rules_sync", "rollover", "transcript_io", "telemetry", "updater", "snapshots", "core", "server"):
            if modname in sys.modules:
                importlib.reload(sys.modules[modname])
            else:
                importlib.import_module(modname)
        import config
        import core
        import rollover
        import rules_sync
        import server
        import snapshots
        import telemetry
        import transcript_io
        import updater
        self.config = config
        self.core = core
        self.rollover = rollover
        self.rules_sync = rules_sync
        self.server = server
        self.snapshots = snapshots
        self.telemetry = telemetry
        self.transcript_io = transcript_io
        self.updater = updater

    def tearDown(self) -> None:
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()

    def test_config_profiles_and_state(self) -> None:
        c0 = self.config.load_config()
        self.assertEqual(c0["profile"], "auto_handoff")
        self.assertEqual(c0["mode"], "smart_auto")
        self.assertFalse(c0["compact_reads"])
        self.assertFalse(c0["concise_output"])

        for prof, exp_mode, exp_cr, exp_co in (
            ("auto_handoff", "smart_auto", False, False),
            ("compact_reads", "prepare_only", True, False),
            ("concise_no_handoff", "prepare_only", True, True),
            ("handoff_only", "smart_auto", True, False),
            ("small_loss", "smart_auto", True, True),
            ("zero_loss", "prepare_only", False, False),
        ):
            r = self.config.save_config({"profile": prof, "threshold_mode": "150", "threshold_k": 150})
            self.assertEqual((r["mode"], r["compact_reads"], r["concise_output"]), (exp_mode, exp_cr, exp_co))
            self.assertEqual(r["threshold_k"], 150)

        self.config.save_config({"threshold_mode": "auto", "mode": "off"})
        self.assertEqual(self.config.load_config()["mode"], "off")
        self.config.save_config({"auto_rollover": True})
        self.assertEqual(self.config.load_config()["mode"], "smart_auto")
        self.config.save_config({"profile": "off"})
        self.assertEqual(self.config.load_config()["mode"], "off")
        self.config.save_config({"auto_rollover": False})
        self.assertFalse(self.config.load_config()["auto_rollover"])
        self.config.CFG_FILE.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(self.config.load_config()["profile"], "auto_handoff")
        self.assertEqual(self.config._infer_profile("smart_auto", False, False), "auto_handoff")
        self.assertEqual(self.config._infer_profile("smart_auto", True, True), "small_loss")
        self.assertEqual(self.config._infer_profile("smart_auto", True, False), "handoff_only")
        self.assertEqual(self.config._infer_profile("prepare_only", True, True), "concise_no_handoff")
        self.assertEqual(self.config._infer_profile("prepare_only", True, False), "compact_reads")
        self.assertEqual(self.config._infer_profile("prepare_only", False, False), "zero_loss")

        # Rollover state + history + snapshot migration
        self.config.append_rollover_history({"from_conv": "abc12345", "kind": "snapshot"})
        self.config._save_rollover_state({"rolled_convs": {"abc12345-full": 1700000000}, "last_rollover_ts": 1700000000})
        rs = self.config._load_rollover_state()
        self.assertIn("abc12345-full", rs["snapshotted_convs"])
        self.assertNotIn("abc12345-full", rs["rolled_convs"])
        self.assertIsNotNone(self.config.parse_iso_ts("2026-11-24T12:00:00Z"))
        self.assertIsNone(self.config.parse_iso_ts("not-a-timestamp"))

    def test_rules_sync_defer_and_flush(self) -> None:
        self.assertFalse(self.rules_sync._should_defer_concise_sync())
        self.assertFalse(self.rules_sync.flush_pending_concise_sync(force=True))
        sb_root = self.tmp / "rs_root"
        sb_root.mkdir(parents=True)
        with mock.patch.object(self.rules_sync, "PLUGIN_ROOT", sb_root), \
             mock.patch.dict(os.environ, {"AGY_TS_SYNC_RULES": "1"}):
            self.rules_sync._sync_concise_rule(True)
            agents_p = sb_root / "rules" / "AGENTS.md"
            self.assertTrue(agents_p.is_file())
            self.assertIn("1. Concise replies", agents_p.read_text(encoding="utf-8"))
            self.assertIn("2. Targeted & batched edits: change existing files with targeted edits", agents_p.read_text(encoding="utf-8"))
            self.assertIn("3. Hybrid batching", agents_p.read_text(encoding="utf-8"))
            self.assertFalse(self.rules_sync._rules_text_stale(agents_p))
            import pre_invocation_handoff as pih
            self.assertEqual(pih.RULES_TEXT, self.rules_sync._RULES_TEXT, "hook and MCP app must write identical rules")
            # A rules file written by an older version is detected as drift and rewritten on flush
            agents_p.write_text("# Token Saver\n" + self.rules_sync._CONCISE_RULE_LINE + "\n", encoding="utf-8")
            self.assertTrue(self.rules_sync._rules_text_stale(agents_p))
            with mock.patch.object(self.rules_sync, "load_config", return_value={"concise_output": True}):
                self.assertTrue(self.rules_sync.flush_pending_concise_sync(force=True))
            self.assertEqual(agents_p.read_bytes().decode("utf-8"), self.rules_sync._RULES_TEXT)
            self.rules_sync._sync_concise_rule(False)
            self.assertFalse(agents_p.exists())
        cfg = self.config.load_config()
        cfg["pending_concise_sync"] = True
        self.config._atomic_write_json(self.config.CFG_FILE, cfg)
        self.assertTrue(self.rules_sync.flush_pending_concise_sync(force=True))
        self.assertFalse(self.config.load_config()["pending_concise_sync"])

        # Warm-cache (>30k, idle <150s) triggers deferral guard when not sandboxed
        warm_rec = {"cur_tot": 45_000, "last_ts": time.time() - 10, "ttl_cliff_sec": 150}
        with mock.patch.object(self.rules_sync, "_is_sandbox_data_dir", return_value=False), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("transcript_io.discover_session_sources", return_value=[(time.time(), "cid1", self.tmp / "tx", None)]), \
             mock.patch("transcript_io.parse_session_cached", return_value=warm_rec):
            os.environ.pop("AGY_TS_SYNC_RULES", None)
            self.assertTrue(self.rules_sync._should_defer_concise_sync())
        with mock.patch.object(self.rules_sync, "_should_defer_concise_sync", return_value=True):
            cfg2 = self.config.load_config()
            cfg2["pending_concise_sync"] = True
            self.config._atomic_write_json(self.config.CFG_FILE, cfg2)
            self.assertFalse(self.rules_sync.flush_pending_concise_sync(force=False))

    def test_auto_rollover_tick_and_server_stream(self) -> None:
        # Exercise run_smart_rollover_tick cold-cache & warm-cache paths safely with mocked state/rollover
        self.config.save_config({"profile": "small_loss"})
        fake_act = {
            "conversation_id": "aaaa1111-bbbb-2222-cccc-333333333333",
            "app": "antigravity",
            "last_settled": True,
            "is_cold": True,
            "cur_tot": 95_000,
            "turns": 18,
            "idle_sec": 300,
            "recent_turns_10m": 5,
            "rollover_features": {"turns": 18, "ctx": 95_000, "drops": 2, "warm_turns": 15},
        }
        fake_st = {"active": fake_act, "adaptive": {"cap_k": 90, "roll": True}, "workspace": str(ROOT)}
        with mock.patch.object(self.snapshots, "build_state", return_value=fake_st), \
             mock.patch.object(self.snapshots, "perform_rollover", return_value={"ok": True, "to_conv": "new-1234", "entry": {"kind": "auto"}}) as m_pr:
            self.snapshots.run_smart_rollover_tick()
            self.assertTrue(m_pr.called)
        # Mocked spawn_chat=True rollover via agentapi (success + failure branches)
        with mock.patch.object(self.snapshots, "_find_agentapi_cmd", return_value=["agentapi"]), \
             mock.patch.object(self.snapshots, "build_handoff_packet", return_value={"packet": "# Handoff", "est_tokens": 50}), \
             mock.patch.object(self.snapshots.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps({"conversationId": "conv-new-99"}), stderr="")):
            sp_res = self.snapshots.perform_rollover(str(ROOT), fake_act, kind="manual", spawn_chat=True, cap_k=90)
            self.assertTrue(sp_res["ok"])
            self.assertEqual(sp_res["to_conv"], "conv-new-99")
        with mock.patch.object(self.snapshots, "_find_agentapi_cmd", return_value=None), \
             mock.patch.object(self.snapshots, "build_handoff_packet", return_value={"packet": "# Handoff", "est_tokens": 50}):
            sp_err = self.snapshots.perform_rollover(str(ROOT), fake_act, kind="manual", spawn_chat=True, cap_k=90)
            self.assertFalse(sp_err["ok"])
        # Exercise server._read_messages on NDJSON + Content-Length framed input
        nd = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        body = b'{"jsonrpc":"2.0","id":2,"method":"ping"}'
        framed = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        fake_stdin = mock.Mock(buffer=io.BytesIO(nd + framed))
        with mock.patch("sys.stdin", fake_stdin):
            msgs = list(self.server._read_messages())
        self.assertEqual(len(msgs), 2)
        # Exercise ts_check_updates / ts_apply_update / ts_trigger_rollover dispatch
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf), \
             mock.patch.object(self.server.core, "check_github_updates", return_value={"update_available": False}), \
             mock.patch.object(self.server.core, "apply_update", return_value={"ok": False, "error": "none"}), \
             mock.patch.object(self.server.core, "perform_rollover", return_value={"ok": True, "entry": {}}):
            self.server._dispatch({"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {"name": "ts_check_updates", "arguments": {}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {"name": "ts_apply_update", "arguments": {}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 22, "method": "tools/call", "params": {"name": "ts_trigger_rollover", "arguments": {"workspace": str(ROOT)}}})
        self.assertEqual(len([ln for ln in out_buf.getvalue().splitlines() if ln.strip()]), 3)

    def test_rollover_tracker_policy_and_simulator(self) -> None:
        tr = self.rollover.RolloverTracker()
        turns = [(20000, 0, 500)] + [(3000, 20000 + i * 3500, 400) for i in range(1, 32)]
        # Inject a mid-session cache drop
        turns[18] = (90000, 0, 400)
        for inp, cr, out in turns:
            tr.push(inp, cr, out)
        feat = tr.features()
        self.assertEqual(feat["turns"], len(turns))
        self.assertGreaterEqual(feat["drops"], 1)

        pol_auto = self.rollover.rollover_policy(feat, {"threshold_mode": "auto", "min_turns": 10})
        self.assertIn("cap_k", pol_auto)
        pol_fix = self.rollover.rollover_policy(feat, {"threshold_mode": "90", "min_turns": 10})
        self.assertEqual(pol_fix["cap_k"], 90)
        self.assertTrue(pol_fix["roll"])
        pol_hi = self.rollover.rollover_policy(feat, {"threshold_mode": "200", "min_turns": 10})
        self.assertFalse(pol_hi["roll"])

        si, sc, nr = self.rollover._simulate_rollover_session(turns, cap=90_000)
        self.assertGreaterEqual(nr, 1)
        self.assertGreater(si + sc, 0)

        # Cold-cache trigger via gaps
        gaps = [10.0] * (len(turns) - 1) + [None]
        gaps[20] = 300.0
        settled = [True] * len(turns)
        si2, sc2, nr2 = self.rollover._simulate_rollover_session(turns, cap=None, settled=settled, gaps=gaps, ttl_cliff=150)
        self.assertGreaterEqual(nr2, 1)

        p_cont = self.rollover.continuation_probability([{"conversation_id": "s1", "hi_pts": 10, "hi_cont": 8}], exclude="other")
        self.assertGreaterEqual(p_cont, 0.5)
        adp = self.rollover.compute_adaptive_threshold({"rollover_features": feat, "p_continue": p_cont}, {"threshold_mode": "auto"})
        self.assertTrue(adp["is_auto"])

    def _make_pb_usage(self, inp: int, cr: int, out: int) -> bytes:
        def varint(n: int) -> bytes:
            b = bytearray()
            while True:
                x = n & 0x7F
                n >>= 7
                b.append(x | (0x80 if n else 0))
                if not n:
                    return bytes(b)
        def field(num: int, wt: int, val: bytes) -> bytes:
            return varint((num << 3) | wt) + (varint(len(val)) + val if wt == 2 else val)
        inner = field(2, 0, varint(inp)) + field(5, 0, varint(cr)) + field(3, 0, varint(out))
        return field(1, 2, field(4, 2, inner))

    def test_transcript_io_telemetry_and_snapshots(self) -> None:
        app = self.tmp / "gemini" / "antigravity"
        sid = "11111111-2222-3333-4444-555555555555"
        logs = app / "brain" / sid / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (app / "conversations").mkdir(parents=True)
        db_path = app / "conversations" / f"{sid}.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
        for i in range(18):
            con.execute("INSERT INTO gen_metadata VALUES (?, ?)", (i, self._make_pb_usage(18000 if i == 0 else 2500, i * 4500, 350)))
        con.commit()
        con.close()

        recs: list[dict[str, object]] = [{"type": "USER_INPUT", "content": "<USER_REQUEST>ship v1.2</USER_REQUEST>", "created_at": "2026-11-24T10:00:00Z"}]
        for i in range(18):
            recs.append({
                "type": "PLANNER_RESPONSE",
                "created_at": f"2026-11-24T10:{i:02d}:00Z",
                "model": "claude-sonnet",
                "tool_calls": None if i == 17 else '[{"name":"read_file","args":{}}]',
                "content": "Done." if i == 17 else "",
            })
        tx = logs / "transcript.jsonl"
        tx.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

        cands = self.transcript_io.discover_session_sources()
        self.assertEqual(len(cands), 1)
        rec = self.transcript_io.parse_session_cached(sid, tx, db_path, "antigravity")
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec["turns"], 18)
        self.assertEqual(rec["source"], "db")
        self.assertTrue(rec["last_settled"])

        # Hook events + analytics
        self.config.EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.EVENTS_FILE.write_text(
            json.dumps({"ts": int(time.time()), "source": "read_guard", "category": "lockfile", "est_tokens_saved": 1200}) + "\n",
            encoding="utf-8",
        )
        ev = self.telemetry.load_events_summary()
        self.assertEqual(ev["saved_tok"], 1200)
        sav = self.telemetry.compute_savings_analytics([rec], ev, [], self.config.load_config())
        self.assertEqual(sav["sessions_count"], 1)

        # Snapshot + state builder
        st = self.snapshots.build_state(sid)
        self.assertEqual(st["active"]["conversation_id"], sid)
        snap = self.snapshots.perform_rollover(str(ROOT), st["active"], kind="snapshot", spawn_chat=False)
        self.assertTrue(snap["ok"])
        self.snapshots.run_smart_rollover_tick()
        dig = self.snapshots.build_repo_digest(str(ROOT))
        self.assertIn("digest", dig)

    def test_updater_and_server_rpc(self) -> None:
        self.assertEqual(self.updater._parse_semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(self.updater._parse_semver("bad"), (0, 0, 0))
        self.assertTrue(len(self.updater.get_installed_version()) >= 5)
        self.assertIn("Changelog", self.updater.get_local_changelog())
        reqs = self.updater.check_requirements()
        self.assertTrue(len(reqs) >= 3)

        blob = b"hello release zip"
        h = hashlib.sha256(blob).hexdigest()
        self.assertEqual(self.updater.parse_sha256sums(f"{h}  agy-tokensaver-v1.2.0.zip\n", "agy-tokensaver-v1.2.0.zip"), h)
        self.assertTrue(self.updater.verify_sha256_bytes(blob, h))
        self.assertFalse(self.updater.verify_sha256_bytes(b"other", h))
        snap = self.updater.get_update_snapshot()
        self.assertEqual(snap["repo"], "menscho/agy-tokensaver")

        # Verify gh attestation helper fail-open when gh absent and on success/failure
        with mock.patch("shutil.which", return_value=None):
            ok_a, note_a = self.updater.verify_gh_attestation(self.tmp / "fake.zip")
            self.assertTrue(ok_a)
            self.assertIn("skipped", note_a)
        fake_ok = mock.Mock(returncode=0, stdout="Verified", stderr="")
        with mock.patch("shutil.which", return_value="gh"), mock.patch("subprocess.run", return_value=fake_ok):
            self.assertEqual(self.updater.verify_gh_attestation(self.tmp / "fake.zip"), (True, "verified"))
        fake_bad = mock.Mock(returncode=1, stdout="", stderr="digest mismatch")
        with mock.patch("shutil.which", return_value="gh"), mock.patch("subprocess.run", return_value=fake_bad):
            self.assertFalse(self.updater.verify_gh_attestation(self.tmp / "fake.zip")[0])

        # Mocked check_github_updates + apply_update into a sandbox PLUGIN_ROOT
        import io as _io
        import zipfile
        zbuf = _io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as zf:
            zf.writestr("agy-tokensaver-v9.9.9/plugin.json", json.dumps({"name": "token-saver", "version": "9.9.9"}))
            zf.writestr("agy-tokensaver-v9.9.9/README.md", "# Updated\n")
        zbytes = zbuf.getvalue()
        zsha = hashlib.sha256(zbytes).hexdigest()
        rel_json = json.dumps({
            "tag_name": "v9.9.9",
            "html_url": "https://example.test/v9.9.9",
            "published_at": "2026-11-24T00:00:00Z",
            "body": "Release 9.9.9",
            "assets": [
                {"name": "agy-tokensaver-v9.9.9.zip", "browser_download_url": "https://example.test/z.zip"},
                {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.test/SHA256SUMS.txt"},
            ],
        }).encode("utf-8")
        sums_bytes = f"{zsha}  agy-tokensaver-v9.9.9.zip\n".encode("utf-8")

        class _Resp:
            def __init__(self, data: bytes) -> None:
                self._d = data
            def read(self) -> bytes:
                return self._d
            def __enter__(self) -> _Resp:
                return self
            def __exit__(self, *a: object) -> None:
                return None

        def _fake_urlopen(req: object, timeout: int = 10) -> _Resp:
            u = getattr(req, "full_url", str(req))
            if u.endswith(".zip"):
                return _Resp(zbytes)
            if u.endswith("SHA256SUMS.txt"):
                return _Resp(sums_bytes)
            return _Resp(rel_json)

        sandbox_root = self.tmp / "plugin_root"
        sandbox_root.mkdir()
        (sandbox_root / "plugin.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
        with mock.patch.object(self.updater, "PLUGIN_ROOT", sandbox_root), \
             mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
             mock.patch("shutil.which", return_value=None):
            upd = self.updater.check_github_updates(force=True)
            self.assertTrue(upd["update_available"])
            res_u = self.updater.apply_update()
            self.assertTrue(res_u["ok"])
            self.assertEqual(res_u["to_version"], "9.9.9")

        # Verify MCP server JSON-RPC dispatch + error handling + message framing
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            self.server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": self.server.RESOURCES[0]["uri"]}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "open_token_saver", "arguments": {}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "ts_set_config", "arguments": {"profile": "zero_loss"}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "ts_get_state", "arguments": {}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "ts_build_handoff", "arguments": {"workspace": str(ROOT)}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "ts_build_digest", "arguments": {"workspace": str(ROOT)}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "ts_snapshot_only", "arguments": {"workspace": str(ROOT)}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 11, "method": "ping", "params": {}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "unknown_tool_xyz", "arguments": {}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 13, "method": "resources/read", "params": {"uri": "ui://nope"}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 14, "method": "unknown/method", "params": {}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 15, "method": "resources/templates/list", "params": {}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 16, "method": "tools/call", "params": {"name": "ts_save_settings", "arguments": {"profile": "auto_handoff"}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 17, "method": "tools/call", "params": {"name": "ts_build_handoff", "arguments": {"workspace": str(ROOT), "archive": True}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 18, "method": "tools/call", "params": {"name": "ts_rollover_now", "arguments": {"workspace": str(ROOT), "spawn_chat": False}}})
            self.server._dispatch({"jsonrpc": "2.0", "id": 19, "method": "tools/call", "params": {"name": "ts_clear_history", "arguments": {"id": "__all__"}}})
            self.server._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        resps = [json.loads(ln) for ln in out_buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(resps), 19)
        self.assertIn("tools", resps[1]["result"])
        self.assertIn("contents", resps[3]["result"])
        self.assertIn("error", resps[12])
        self.assertIn("error", resps[13])

    def test_scripts_ts_inspect_audit_stats(self) -> None:
        import audit
        import stats
        import ts_inspect

        # ts_inspect purge + outline + query + json + csv
        sp = self.tmp / "spills"
        sp.mkdir()
        (sp / "a.log").write_text("line1\nERR_BOOM_99\nline3\n", encoding="utf-8")
        self.assertEqual(ts_inspect.cmd_query(str(sp / "a.log"), ["ERR_BOOM_99"]), 0)
        self.assertEqual(ts_inspect.cmd_outline(str(ROOT / "scripts" / "ts_inspect.py"), []), 0)
        jfile = self.tmp / "d.json"
        jfile.write_text('{"a": [1, 2, 3, 4], "b": {"c": "hi"}}', encoding="utf-8")
        self.assertEqual(ts_inspect.cmd_json(str(jfile), []), 0)
        cfile = self.tmp / "d.csv"
        cfile.write_text("id,name\n1,alice\n2,bob\n", encoding="utf-8")
        self.assertEqual(ts_inspect.cmd_csv(str(cfile), ["--rows", "1"]), 0)
        self.assertEqual(ts_inspect.cmd_purge(["--dir", str(sp)]), 0)
        self.assertEqual(list(sp.iterdir()), [])

        # audit workspace & stats helpers
        traps, warns = audit.inspect_workspace(ROOT)
        self.assertIsInstance(traps, list)
        self.assertIsInstance(warns, list)
        self.assertEqual(stats.fmt_num(1500), "1,500")

    def test_eval_live_driver_and_summarize(self) -> None:
        sys.path.insert(0, str(ROOT / "eval"))
        import runner as eval_runner

        task = eval_runner.load_tasks()[0]
        # Manual live task WITHOUT applying reference fix must report passed=False
        with mock.patch("builtins.input", return_value=""), \
             mock.patch.object(eval_runner, "_collect_live_session_metrics", return_value=(1200, 1500.0, 0, "sid-1")):
            rec = eval_runner.run_live_task(task, "off", 1, "manual")
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertFalse(rec["passed"])
        self.assertEqual(rec["total_tokens"], 1200)

        # Headless agy live task where mock agent solves task in cwd reports passed=True
        from typing import Any
        orig_run = eval_runner.subprocess.run

        def _fake_agy(cmd_parts: Any, **kwargs: Any) -> Any:
            if isinstance(cmd_parts, list) and cmd_parts and cmd_parts[0] == "agy":
                eval_runner.apply_reference(task, Path(str(kwargs.get("cwd") or "")))
                return mock.Mock(returncode=0)
            return orig_run(cmd_parts, **kwargs)

        with mock.patch.object(eval_runner.subprocess, "run", side_effect=_fake_agy), \
             mock.patch.object(eval_runner, "_collect_live_session_metrics", return_value=(800, 1000.0, 1, "sid-2")):
            rec_ok = eval_runner.run_live_task(task, "mode1", 1, "agy", agy_cmd="agy {workspace} {prompt}")
        self.assertIsNotNone(rec_ok)
        assert rec_ok is not None
        self.assertTrue(rec_ok["passed"])

        # Mocked antigravity ConversationService 2-phase handoff continuity task
        ho_task = next(t for t in eval_runner.load_tasks() if t["id"] == "H01-handoff-partial-py")

        def _fake_conv(title: str, prompt: str, model: str = "flash", poll_timeout: int = 150) -> tuple[str, dict[str, Any]]:
            # Phase 2 prompt embeds packet; parse workspace path and apply reference fix cleanly
            import re as _re
            m = _re.search(r"directory `([^`]+)`", prompt)
            assert m, prompt
            if "Step 2" in prompt:
                eval_runner.apply_reference(ho_task, Path(m.group(1)))
            return "cid-mock-" + ("1" if "Step 1" in prompt else "2"), {
                "cid": "cid-mock", "input_tokens": 500, "cache_read_tokens": 1000, "output_tokens": 120,
                "turns": 3, "total_tokens": 1620, "cost_proxy": 1270.0, "retries": 0,
            }

        with mock.patch.object(eval_runner, "SCRATCH_EVAL_BASE", self.tmp / "scratch"), \
             mock.patch.object(eval_runner, "_run_antigravity_conversation", side_effect=_fake_conv), \
             mock.patch.object(eval_runner, "_build_handoff_packet", return_value="# Session Continuation Handoff\nLast state: fix clamp"), \
             mock.patch.object(eval_runner, "_delete_conversation_quiet"):
            rec_ho = eval_runner.run_live_task(ho_task, "default", 1, "antigravity", model="flash")
        self.assertIsNotNone(rec_ho)
        assert rec_ho is not None
        self.assertTrue(rec_ho["passed"])
        self.assertTrue(rec_ho["handoff_reset"])
        self.assertEqual(rec_ho["total_tokens"], 3240)

        # Summarize flags >2pp drop cleanly with Wilson CI
        out_buf = io.StringIO()
        sample = {
            "driver": "agy", "n_tasks": 10, "runs": 1,
            "records": (
                [{"mode": "off", "run": 1, "passed": True, "total_tokens": 100, "cost_proxy": 120.0, "retries": 0} for _ in range(10)]
                + [{"mode": "mode1", "run": 1, "passed": (i < 9), "total_tokens": 80, "cost_proxy": 90.0, "retries": 0} for i in range(10)]
            ),
        }
        with mock.patch("sys.stdout", out_buf):
            eval_runner.summarize(sample)
        summary_txt = out_buf.getvalue()
        self.assertIn("Pass Rate", summary_txt)
        self.assertIn("LOSS >2pp", summary_txt)

    def test_installer_doctor_and_python_wiring(self) -> None:
        sys.path.insert(0, str(ROOT))
        import install as inst

        pdir = self.tmp / "pdir"
        (pdir / "mcp_app").mkdir(parents=True)
        (pdir / "scripts").mkdir(parents=True)
        (pdir / "plugin.json").write_text(json.dumps({"version": "1.2.1"}), encoding="utf-8")
        (pdir / "mcp_app" / "server.py").write_text("# srv\n", encoding="utf-8")
        (pdir / "scripts" / "read_hook.py").write_text("# hook\n", encoding="utf-8")
        (pdir / "hooks.json").write_text(
            json.dumps({"r": {"PreToolUse": [{"matcher": "read_file", "hooks": [{"type": "command", "command": "python3 -S ./scripts/read_hook.py"}]}]}}),
            encoding="utf-8",
        )
        # Verify _wire_hooks_python replaces leading "python3 " cleanly with explicit path
        changed = inst._wire_hooks_python(pdir, r"C:\Program Files\Python312\python.exe")
        self.assertTrue(changed)
        wired = json.loads((pdir / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(wired["r"]["PreToolUse"][0]["hooks"][0]["command"], r'"C:\Program Files\Python312\python.exe" -S ./scripts/read_hook.py')

        # Restore plain "python3 -S ./scripts/..." (or sys.executable) and verify run_doctor succeeds on valid env
        (pdir / "hooks.json").write_text(
            json.dumps({"r": {"PreToolUse": [{"matcher": "read_file", "hooks": [{"type": "command", "command": f'"{sys.executable}" -S ./scripts/read_hook.py'}]}]}}),
            encoding="utf-8",
        )
        lines: list[str] = []
        with mock.patch.object(inst, "PLUGIN_DIR", pdir), mock.patch.object(inst, "DATA_DIR", self.tmp / "pdata"):
            rc = inst.run_doctor(log=lines.append)
        self.assertEqual(rc, 0, "\n".join(lines))
        self.assertTrue(any("Hook registration" in ln and "[OK  ]" in ln for ln in lines))

        # Missing plugin dir causes run_doctor to return 1 with Fix guidance
        lines_bad: list[str] = []
        with mock.patch.object(inst, "PLUGIN_DIR", self.tmp / "missing_pdir"), mock.patch.object(inst, "DATA_DIR", self.tmp / "pdata"):
            rc_bad = inst.run_doctor(log=lines_bad.append)
        self.assertEqual(rc_bad, 1)
        self.assertTrue(any("Fix:" in ln for ln in lines_bad))

    def test_installer_seeds_max_profile_only_on_fresh_install(self) -> None:
        sys.path.insert(0, str(ROOT))
        import hook_common
        import install as inst

        pdir = self.tmp / "fresh_pdir"
        (pdir / "mcp_app").mkdir(parents=True)
        for name in ("rules_sync.py", "config.py"):
            (pdir / "mcp_app" / name).write_bytes((ROOT / "mcp_app" / name).read_bytes())
        ddir = self.tmp / "pdata"

        logs: list[str] = []
        self.assertTrue(inst.seed_fresh_install_config(pdir, ddir, log=logs.append))
        cfg = self.config.load_config()
        self.assertEqual(cfg["profile"], "small_loss")
        self.assertTrue(cfg["compact_reads"] and cfg["concise_output"] and cfg["startup_snapshot"])
        self.assertEqual(cfg["mode"], "smart_auto")
        self.assertTrue(hook_common.read_compact_reads(str(pdir)), "hooks must see the same compact_reads setting")
        agents_md = pdir / "rules" / "AGENTS.md"
        self.assertEqual(agents_md.read_bytes().decode("utf-8"), self.rules_sync._RULES_TEXT)
        self.assertTrue(any("Max mode" in ln for ln in logs))

        # A saved choice is never overwritten
        self.config.save_config({"profile": "auto_handoff"})
        self.assertFalse(inst.seed_fresh_install_config(pdir, ddir, log=logs.append))
        self.assertEqual(self.config.load_config()["profile"], "auto_handoff")

    def test_model_cache_profiles_match_and_cap_gemini4(self) -> None:
        import hook_common
        import pre_invocation_handoff as pih

        names = [
            "gemini-4-neon-high", "Gemini 4 Neon (XHigh)", "gemini-4-hydrogen-low", "gemini-4-carbon-high",
            "gemini-4-argon-medium", "gemini-pro-orchestrator-neon", "gemini-pro-orchestrator", "gemini-3.8-flash",
            "claude-opus-5-5-medium", "",
        ]
        for n in names:
            self.assertEqual(hook_common.model_cache_profile(n), self.config.model_cache_profile(n), n)
        neon = hook_common.model_cache_profile("gemini-4-neon-high")
        self.assertEqual((neon["ttl_sec"], neon["ctx_cap"], neon["volatile"]), (240, 180_000, False))
        self.assertEqual(hook_common.model_cache_profile("gemini-4-hydrogen-high")["ttl_sec"], 90)
        orch = hook_common.model_cache_profile("gemini-pro-orchestrator-neon")
        self.assertEqual((orch["family"], orch["volatile"], orch["ttl_sec"]), ("orchestrator", True, 120))
        self.assertIsNone(hook_common.model_cache_profile("claude-opus-5-5-medium")["ctx_cap"])

        # Live hook policy: a chat whose warm-up turns miss the cache (typical on Carbon/Argon) makes a
        # rollover look expensive, so the auto threshold drifts to AUTO_CAP_MAX; Gemini 4 caps at 180k.
        state = {"ctx": 190_000, "turns": 30, "drops": 0, "warm_turns": 400, "warm_miss": [1.0, 1.0, 1.0]}
        cfg = {"threshold_mode": "auto", "min_turns": 10}
        free = pih._compute_policy(state, cfg, ttl_cliff=240, delegated=False)
        capped = pih._compute_policy(state, cfg, ttl_cliff=240, delegated=False, ctx_cap=180_000)
        self.assertGreater(free["cap"], 180_000)
        self.assertFalse(free["warm_roll"])
        self.assertEqual(capped["cap"], 180_000)
        self.assertTrue(capped["warm_roll"])
        manual = pih._compute_policy(state, {"threshold_mode": "200", "min_turns": 10}, ctx_cap=180_000)
        self.assertEqual(manual["cap"], 200_000, "a manual threshold is the user's choice and is never capped")

        # Dashboard / replay policy uses the same cap
        feat = {"ctx": 190_000, "turns": 30, "drops": 0, "warm_turns": 400, "warm_miss": [1.0, 1.0, 1.0], "p_continue": 1.0}
        pol_free = self.rollover.rollover_policy(feat, cfg)
        pol_cap = self.rollover.rollover_policy({**feat, "ctx_cap": 180_000}, cfg)
        self.assertGreater(pol_free["cap"], 180_000)
        self.assertEqual(pol_cap["cap"], 180_000)
        self.assertIn("Gemini 4", pol_cap["reason"])

    def test_volatile_model_advisory_is_user_only_and_once(self) -> None:
        import hook_common
        import pre_invocation_handoff as pih

        dd = str(self.tmp / "pdata")
        emitted: list[dict[str, object]] = []
        carbon = hook_common.model_cache_profile("gemini-4-carbon-high")
        neon = hook_common.model_cache_profile("gemini-4-neon-high")
        with mock.patch.object(pih, "emit_json", side_effect=emitted.append):
            self.assertEqual(pih._maybe_model_advisory(dd, "c1", neon, "gemini-4-neon-high", 150_000), 0)
            self.assertEqual(emitted, [], "Neon keeps its cache: no advisory")
            pih._maybe_model_advisory(dd, "c1", carbon, "gemini-4-carbon-high", 20_000)
            self.assertEqual(emitted, [], "small chats: a miss is cheap, no advisory")
            pih._maybe_model_advisory(dd, "c1", carbon, "gemini-4-carbon-high", 150_000)
            pih._maybe_model_advisory(dd, "c1", carbon, "gemini-4-carbon-high", 160_000)
        self.assertEqual(len(emitted), 1, "once per conversation")
        sm = emitted[0]["injectSteps"][0]["systemMessage"]  # type: ignore[index]
        self.assertEqual(sm["systemMessage"], "", "0 prompt tokens: the note goes to the user only")
        self.assertIn("Neon", sm["metadata"]["user_markdown"])

    def test_run_checks_cache_safe_budget(self) -> None:
        import fast_checks as fc
        import fast_utils as fu

        seen: list[int] = []

        def fake_run(cmd: str, cwd: str, timeout_ms: int) -> object:
            seen.append(timeout_ms)
            return fu.ExecResult(exit_code=0, sig=None, timed_out=False, stdout="ok", stderr="", truncated=False, duration_ms=5, error=None)

        with mock.patch.object(fc, "run_shell_command", side_effect=fake_run):
            fc.run_checks({"cwd": str(self.tmp), "commands": ["a", "b"]})
            fc.run_checks({"cwd": str(self.tmp), "commands": [{"command": "slow", "timeoutSec": 120}]})
            fc.run_checks({"cwd": str(self.tmp), "commands": ["c"], "timeoutSec": 150, "budgetSec": 30})
        self.assertEqual(fc.CACHE_SAFE_BUDGET_SEC, 90)
        self.assertEqual(seen[:2], [60_000, 60_000])
        self.assertGreater(seen[2], 110_000, "a command is never cut before its own timeoutSec")
        self.assertGreater(seen[3], 140_000, "budget never undercuts the longest requested timeout")

    def test_rollover_parses_nested_agentapi_conversation_id(self) -> None:
        import snapshots as sn

        ws = self.tmp / "ws"
        ws.mkdir()
        (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
        proc = mock.MagicMock(
            returncode=0,
            stdout=json.dumps({"response": {"newConversation": {"conversationId": "new-conv-uuid-123"}}}),
            stderr="",
        )
        with (
            mock.patch.object(sn, "build_handoff_packet", return_value={"packet": "## Handoff\npacket", "est_tokens": 12}),
            mock.patch.object(sn, "_find_agentapi_cmd", return_value=["/usr/bin/agentapi"]),
            mock.patch.object(sn, "append_rollover_history"),
            mock.patch.object(sn.subprocess, "run", return_value=proc),
        ):
            res = sn.perform_rollover(str(ws), {"conversation_id": "old-conv-1", "cur_tot": 50000, "turns": 8})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["to_conv"], "new-conv-uuid-123")
        self.assertEqual(res["entry"]["to_conv"], "new-conv")

    def test_pre_invocation_handoff_and_exec(self) -> None:
        import handoff_exec
        import pre_invocation_handoff as pih

        ws = self.tmp / "ws_ho"
        ws.mkdir()
        (ws / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

        # Build a realistic settled transcript with >105k context and >10m idle gap
        tx = self.tmp / "transcript.jsonl"
        old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        recs: list[dict[str, object]] = [
            {"type": "USER_INPUT", "content": "<USER_REQUEST>implement auth</USER_REQUEST>", "created_at": old_ts},
        ]
        for i in range(16):
            recs.append({
                "type": "PLANNER_RESPONSE",
                "created_at": old_ts,
                "model": "gemini-4-hydrogen-high",
                "input_tokens": 18000 if i == 0 else 5000,
                "cache_read_tokens": 0 if i == 0 else 30000 + i * 5000,
                "output_tokens": 400,
                "tool_calls": "None" if i == 15 else "[{\"name\": \"read_file\", \"args\": {}}]",
                "content": "Finished auth refactor.",
            })
        tx.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

        def run_pih(payload: dict, extra_env: dict | None = None) -> dict:
            raw = json.dumps(payload).encode("utf-8")
            out_buf = io.BytesIO()
            fake_in = mock.Mock(buffer=io.BytesIO(raw))
            fake_out = mock.Mock(buffer=out_buf)
            env_patch = {
                "PLUGIN_DATA": str(self.tmp / "pdata"),
                "ANTIGRAVITY_LS_ADDRESS": "",
                "ANTIGRAVITY_CSRF_TOKEN": "",
                **(extra_env or {}),
            }
            with mock.patch("sys.stdin", fake_in), mock.patch("sys.stdout", fake_out), \
                 mock.patch.dict(os.environ, env_patch, clear=False):
                rc = pih.main()
            self.assertEqual(rc, 0)
            return json.loads(out_buf.getvalue().decode("utf-8"))

        base_payload = {
            "conversationId": "conv-cold-1234",
            "transcriptPath": str(tx),
            "lastUserInput": "Now add dark mode",
            "invocationNum": 0,
            "workspacePaths": [str(ws)],
            "cwd": str(ws),
            "modelName": "gemini-4-hydrogen-high",
        }

        # Guardrails: mid-turn (invocationNum > 0), slash command, [stay] opt-out, and trivial replies <180k return {}
        self.assertEqual(run_pih({**base_payload, "invocationNum": 1}), {})
        self.assertEqual(run_pih({**base_payload, "lastUserInput": "/handoff"}), {})
        self.assertEqual(run_pih({**base_payload, "lastUserInput": "fix bug [stay]"}), {})
        self.assertEqual(run_pih({**base_payload, "lastUserInput": "yes"}), {})
        self.assertEqual(run_pih({**base_payload, "lastUserInput": "are you sure?"}), {})

        # Delegated mode (no LS env in hook): writes pending JSON + injects 1-step handoff_exec directive
        delegated = run_pih(base_payload)
        self.assertIn("injectSteps", delegated)
        sys_msg = delegated["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("AUTO-HANDOFF TRIGGERED", sys_msg)
        self.assertIn("handoff_exec.py", sys_msg)
        pending_file = self.tmp / "pdata" / "pending" / "conv-cold-1234.json"
        self.assertTrue(pending_file.is_file())
        self.assertTrue(json.loads(pending_file.read_text(encoding="utf-8")).get("delegated"))
        # Second invocation while pending does NOT double-trigger
        self.assertEqual(run_pih(base_payload), {})

        # Execute pending handoff via handoff_exec with mocked ConversationService
        rpc_calls: list[tuple[str, dict]] = []

        def fake_rpc(ls_addr: str, csrf: str, method: str, body: dict, timeout: float = 12.0) -> dict:
            rpc_calls.append((method, body))
            if method == "GetConversation":
                return {
                    "id": body["conversationId"],
                    "metadata": {"projectId": "proj-1", "environmentId": "env-1"},
                    "annotations": {"title": "Auth Refactor", "conversationGroupId": "grp-7"},
                }
            if method == "CreateConversation":
                return {"conversation": {"id": "new-conv-8888"}}
            return {}

        with mock.patch.object(handoff_exec, "_conv_rpc", side_effect=fake_rpc), \
             mock.patch.dict(os.environ, {"ANTIGRAVITY_LS_ADDRESS": "localhost:9999", "ANTIGRAVITY_CSRF_TOKEN": "tok"}, clear=False):
            out_s = io.StringIO()
            with mock.patch("sys.stdout", out_s), mock.patch("sys.argv", ["handoff_exec.py", "--pending", str(pending_file)]):
                rc = handoff_exec.main()
        self.assertEqual(rc, 0)
        self.assertIn("AUTO_HANDOFF_OK:", out_s.getvalue())
        self.assertIn("conversation://new-conv-8888", out_s.getvalue())
        self.assertFalse(pending_file.exists())
        methods = [m for m, _ in rpc_calls]
        self.assertEqual(methods, ["GetConversation", "CreateConversation", "UpdateConversation", "UpdateConversation"])
        create_body = rpc_calls[1][1]
        self.assertEqual(create_body["projectId"], "proj-1")
        self.assertEqual(create_body["environmentId"], "env-1")
        self.assertEqual(create_body["title"], "Auth Refactor (cont. 2)")
        self.assertIn("Now add dark mode", create_body["prompt"][0]["text"])
        self.assertTrue(rpc_calls[3][1]["annotations"]["archived"])

        # Turn-2 termination: right after delegated handoff_exec succeeds, invocationNum == 1 terminates
        # immediately with clickable user_markdown (0 LLM tokens spent on Turn 2), and only once.
        term2 = run_pih({**base_payload, "invocationNum": 1})
        self.assertEqual(term2.get("terminationBehavior"), "terminate")
        md2 = term2["injectSteps"][0]["systemMessage"]["metadata"]["user_markdown"]
        self.assertIn("conversation://new-conv-8888", md2)
        self.assertEqual(run_pih({**base_payload, "invocationNum": 2}), {})

        # Direct 0-token mode (LS env present in hook) terminates immediately with clickable user_markdown
        # Backdate last_rollover_ts past the 60s anti-burst guard
        rs = self.config._load_rollover_state()
        rs["last_rollover_ts"] = int(time.time()) - 120
        self.config._save_rollover_state(rs)
        direct_payload = {**base_payload, "conversationId": "conv-direct-5678"}
        with mock.patch.object(handoff_exec, "_conv_rpc", side_effect=fake_rpc):
            direct = run_pih(
                direct_payload,
                extra_env={"ANTIGRAVITY_LS_ADDRESS": "localhost:9999", "ANTIGRAVITY_CSRF_TOKEN": "tok"},
            )
        self.assertEqual(direct.get("terminationBehavior"), "terminate")
        md = direct["injectSteps"][0]["systemMessage"]["metadata"]["user_markdown"]
        self.assertIn("conversation://new-conv-8888", md)

        # Failed tick does NOT poison rolled_convs
        self.config.save_config({"profile": "small_loss"})
        fail_act = {
            "conversation_id": "conv-fail-retry",
            "app": "antigravity",
            "last_settled": True,
            "is_cold": True,
            "cur_tot": 95_000,
            "turns": 18,
            "idle_sec": 300,
            "recent_turns_10m": 5,
            "rollover_features": {"turns": 18, "ctx": 95_000, "drops": 2, "warm_turns": 15},
        }
        fail_st = {"active": fail_act, "adaptive": {"cap_k": 90, "roll": True}, "workspace": str(ws), "can_new_chat": True}
        with mock.patch.object(self.snapshots, "build_state", return_value=fail_st), \
             mock.patch.object(self.snapshots, "perform_rollover", return_value={"ok": False, "error": "boom"}):
            self.snapshots.run_smart_rollover_tick()
        self.assertNotIn("conv-fail-retry", self.config._load_rollover_state()["rolled_convs"])

        # ConversationService path inside snapshots.perform_rollover
        with mock.patch.object(self.snapshots, "_conv_rpc", side_effect=fake_rpc), \
             mock.patch.object(self.snapshots, "build_handoff_packet", return_value={"packet": "# Handoff", "est_tokens": 20}), \
             mock.patch.dict(os.environ, {"ANTIGRAVITY_LS_ADDRESS": "localhost:9999", "ANTIGRAVITY_CSRF_TOKEN": "tok"}, clear=False):
            cs_res = self.snapshots.perform_rollover(str(ws), fail_act, kind="manual", spawn_chat=True, cap_k=90)
        self.assertTrue(cs_res["ok"], cs_res)
        self.assertEqual(cs_res["to_conv"], "new-conv-8888")

    def test_smarter_handoff_pack_and_cache_drop_shields(self) -> None:
        import subprocess
        import handoff_pack as hp
        import pre_invocation_handoff as pih
        import rollover as ro

        # 1. Smarter handoff pack: auto-detects git root, recovers full assistant text, extracts artifacts & read ranges,
        #    deduplicates exclude_prompt, and ignores source-code FAIL lines from non-command outputs.
        repo = self.tmp / "myrepo"
        repo.mkdir()
        def g(*a: str) -> None:
            subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (repo / "mod.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        g("add", ".")
        g("commit", "-qm", "init")
        (repo / "mod.py").write_text("def alpha():\n    return 2\n\ndef beta():\n    return 3\n", encoding="utf-8")

        sid = "abcd1234-5678-9012-3456-abcdef123456"
        conv_dir = self.tmp / "gemini" / "antigravity" / "brain" / sid
        logs = conv_dir / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (conv_dir / "task.md").write_text("# Plan\n- [x] Fix bug\n- [ ] Add tests\n", encoding="utf-8")

        long_reply = (
            "### Summary\n1. Fixed `alpha()`.\n2. Next step: wire cache-drop shields.\n"
            + ("Detail line.\n" * 240)
            + "### Final Conclusion\nAll 42 checks passed with 0 regressions.\n"
        )
        tc_edit = [{"name": "edit_file", "args": {"Path": str(repo / "mod.py")}}]
        tc_read = [{"name": "read_file", "args": {"Path": str(repo / "README.md"), "StartLine": 10, "EndLine": 45}}]
        tc_steps = [{"name": "update_steps", "args": {"steps": [{"description": "Run unit tests", "status": "in_progress"}]}}]
        compact_recs = [
            {"type": "USER_INPUT", "content": "<USER_REQUEST>Build cache shields</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "tool_calls": tc_edit + tc_read + tc_steps, "content": ""},
            {"type": "GENERIC", "content": '124: print("FAIL: fake source snippet")'},
            {"type": "GENERIC", "content": "Command exited with code 1\nAssertionError: boom expected 2"},
            {"type": "PLANNER_RESPONSE", "tool_calls": None, "content": long_reply[:120], "truncated_fields": ["content"]},
            {"type": "USER_INPUT", "content": "<USER_REQUEST>Now do step 2</USER_REQUEST>"},
        ]
        full_recs = list(compact_recs)
        full_recs[4] = {"type": "PLANNER_RESPONSE", "tool_calls": None, "content": long_reply}
        (logs / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in compact_recs) + "\n", encoding="utf-8")
        (logs / "transcript_full.jsonl").write_text("\n".join(json.dumps(r) for r in full_recs) + "\n", encoding="utf-8")

        non_git_ws = self.tmp / "plain_home"
        non_git_ws.mkdir()
        with mock.patch.object(hp, "APP_DIRS", (self.tmp / "gemini" / "antigravity",)):
            pkt = hp.build_packet(non_git_ws, sid, exclude_prompt="Now do step 2")
        self.assertIn("Goal: Build cache shields", pkt)
        self.assertNotIn("Goal: Now do step 2", pkt)
        self.assertIn("Next step: wire cache-drop shields.", pkt)
        self.assertIn("All 42 checks passed with 0 regressions.", pkt)
        self.assertIn("## Active Plan Steps", pkt)
        self.assertIn("Run unit tests", pkt)
        self.assertIn("## Active Artifacts", pkt)
        self.assertIn("task.md", pkt)
        self.assertIn("AssertionError: boom expected 2", pkt)
        self.assertNotIn("fake source snippet", pkt)
        self.assertIn("M mod.py", pkt)
        self.assertIn("(L10-L45)", pkt)

        # 2. Hydrogen 90s TTL yields tighter adaptive cap than 150s TTL
        feat = {"ctx": 85_000, "turns": 15, "drops": 0, "warm_turns": 11, "warm_miss": [0.0, 0.0, 0.0]}
        pol_150 = ro.rollover_policy({**feat, "ttl_cliff_sec": 150}, {"threshold_mode": "auto", "min_turns": 10})
        pol_90 = ro.rollover_policy({**feat, "ttl_cliff_sec": 90}, {"threshold_mode": "auto", "min_turns": 10})
        self.assertLessEqual(pol_90["cap"], pol_150["cap"])
        self.assertGreater(pol_90["p_drop"], pol_150["p_drop"])

        # 3. PreInvocation triggers: Path 1 vs Path 2 honest economics, heavy-phase swarm prep, and mid-run advisory
        tx = self.tmp / "tx_shields.jsonl"
        fresh_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
        warm_recs: list[dict[str, object]] = [
            {"type": "USER_INPUT", "content": "<USER_REQUEST>refactor module</USER_REQUEST>", "created_at": fresh_ts},
        ]
        for i in range(6):
            warm_recs.append({
                "type": "PLANNER_RESPONSE",
                "created_at": fresh_ts,
                "model": "gemini-4-neon-high",
                "input_tokens": 18000 if i == 0 else 4000,
                "cache_read_tokens": 0 if i == 0 else 74000,
                "output_tokens": 300,
                "tool_calls": "None" if i == 5 else "[{\"name\": \"read_file\"}]",
                "content": "Ready for next step.",
            })
        tx.write_text("\n".join(json.dumps(r) for r in warm_recs) + "\n", encoding="utf-8")

        def run_pih(payload: dict, extra_env: dict | None = None) -> dict:
            raw = json.dumps(payload).encode("utf-8")
            out_buf = io.BytesIO()
            fake_in = mock.Mock(buffer=io.BytesIO(raw))
            fake_out = mock.Mock(buffer=out_buf)
            env_patch = {
                "PLUGIN_DATA": str(self.tmp / "pdata"),
                "ANTIGRAVITY_LS_ADDRESS": "",
                "ANTIGRAVITY_CSRF_TOKEN": "",
                **(extra_env or {}),
            }
            with mock.patch("sys.stdin", fake_in), mock.patch("sys.stdout", fake_out), \
                 mock.patch.dict(os.environ, env_patch, clear=False):
                rc = pih.main()
            self.assertEqual(rc, 0)
            return json.loads(out_buf.getvalue().decode("utf-8"))

        # Normal warm 78k chat with same model does NOT trigger
        base = {
            "conversationId": "conv-shield-1",
            "transcriptPath": str(tx),
            "lastUserInput": "tweak one line",
            "invocationNum": 0,
            "workspacePaths": [str(repo)],
            "cwd": str(repo),
            "modelName": "gemini-4-neon-high",
        }
        self.assertEqual(run_pih(base), {})

        # Model switch on 78k chat in Delegated mode (Path 2) does NOT fire (cannot avoid Turn-0 rebuild!)
        self.assertEqual(run_pih({**base, "conversationId": "conv-shield-ms-p2", "modelName": "gemini-4-hydrogen-high"}), {})

        # Model switch on 78k chat in Direct mode (Path 1, LS env present) DOES terminate with 0 tokens!
        import handoff_exec
        with mock.patch.object(handoff_exec, "execute_handoff", return_value={"ok": True, "new_id": "new-ms-1", "archived_old": True}):
            ms = run_pih(
                {**base, "conversationId": "conv-shield-ms-p1", "modelName": "gemini-4-hydrogen-high"},
                extra_env={"ANTIGRAVITY_LS_ADDRESS": "localhost:9999", "ANTIGRAVITY_CSRF_TOKEN": "tok"},
            )
        self.assertEqual(ms.get("terminationBehavior"), "terminate")
        self.assertIn("model switch", ms["injectSteps"][0]["systemMessage"]["metadata"]["user_markdown"])

        # Heavy-phase swarm prompt on warm >=75k chat DOES trigger pre-swarm handoff even in Path 2
        rs_s = self.config._load_rollover_state()
        rs_s["last_rollover_ts"] = 0
        self.config._save_rollover_state(rs_s)
        sw = run_pih({**base, "conversationId": "conv-shield-sw", "lastUserInput": "launch a swarm to audit all modules"})
        self.assertIn("heavy phase/swarm prep", sw["injectSteps"][0]["systemMessage"]["systemMessage"])

        # Path 2 delegated simulator accounts for delegated overhead (higher sim_in than direct Path 1)
        long_turns = [(18000, 0, 300)] + [(5000, 20000 + i * 5000, 300) for i in range(25)]
        elig = [False] * 14 + [True] + [False] * 11
        p1_in, p1_cr, p1_r = ro._simulate_rollover_session(long_turns, None, settled=elig, on_user_turn=True, delegated=False)
        p2_in, p2_cr, p2_r = ro._simulate_rollover_session(long_turns, None, settled=elig, on_user_turn=True, delegated=True)
        self.assertGreaterEqual(p1_r, 1)
        self.assertGreaterEqual(p2_r, 1)
        self.assertGreater(p2_in + p2_cr * 0.05, p1_in + p1_cr * 0.05)

        # Mid-run (invocationNum == 3) emits 1-line advisory ONCE right after a >=75k cache drop
        drop_recs = [
            {"type": "PLANNER_RESPONSE", "created_at": fresh_ts, "model": "gemini-4-hydrogen-high",
             "input_tokens": 3000, "cache_read_tokens": 92000, "output_tokens": 200, "tool_calls": "[{\"name\": \"swarm_wait\"}]"},
            {"type": "PLANNER_RESPONSE", "created_at": fresh_ts, "model": "gemini-4-hydrogen-high",
             "input_tokens": 96000, "cache_read_tokens": 0, "output_tokens": 200, "tool_calls": "[{\"name\": \"read_file\"}]"},
        ]
        tx_drop = self.tmp / "tx_drop.jsonl"
        tx_drop.write_text("\n".join(json.dumps(r) for r in drop_recs) + "\n", encoding="utf-8")
        adv1 = run_pih({**base, "conversationId": "conv-midrun-1", "transcriptPath": str(tx_drop), "invocationNum": 3})
        self.assertIn("Mid-run KV cache drop", adv1["injectSteps"][0]["systemMessage"]["systemMessage"])
        # Second mid-run invocation within 10m is silent (no spam)
        self.assertEqual(
            run_pih({**base, "conversationId": "conv-midrun-1", "transcriptPath": str(tx_drop), "invocationNum": 4}),
            {},
        )

        # Proactive high-context cache shield (Hydrogen >=110k at inv_num >= 6) warns ONCE before a drop
        hi_recs = [
            {"type": "PLANNER_RESPONSE", "created_at": fresh_ts, "model": "gemini-4-hydrogen-high",
             "input_tokens": 3000, "cache_read_tokens": 112000, "output_tokens": 200, "tool_calls": "[{\"name\": \"read_file\"}]"},
            {"type": "PLANNER_RESPONSE", "created_at": fresh_ts, "model": "gemini-4-hydrogen-high",
             "input_tokens": 3500, "cache_read_tokens": 115000, "output_tokens": 200, "tool_calls": "[{\"name\": \"read_file\"}]"},
        ]
        tx_hi = self.tmp / "tx_hi.jsonl"
        tx_hi.write_text("\n".join(json.dumps(r) for r in hi_recs) + "\n", encoding="utf-8")
        pro1 = run_pih({**base, "conversationId": "conv-proactive-1", "transcriptPath": str(tx_hi), "invocationNum": 6})
        self.assertIn("High-context cache shield", pro1["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertEqual(
            run_pih({**base, "conversationId": "conv-proactive-1", "transcriptPath": str(tx_hi), "invocationNum": 7}),
            {},
        )

        # PostInvocation Turn-2 termination + huge_overwrite model nudge
        import post_invocation_alerts as pia

        def run_pia(payload: dict) -> dict:
            raw = json.dumps(payload).encode("utf-8")
            out_buf = io.BytesIO()
            fake_in = mock.Mock(buffer=io.BytesIO(raw))
            fake_out = mock.Mock(buffer=out_buf)
            with mock.patch("sys.stdin", fake_in), mock.patch("sys.stdout", fake_out), \
                 mock.patch.dict(os.environ, {"PLUGIN_DATA": str(self.tmp / "pdata")}, clear=False):
                rc = pia.main()
            self.assertEqual(rc, 0)
            return json.loads(out_buf.getvalue().decode("utf-8"))

        # PostInvocation terminates immediately after delegated handoff_exec completes (even at inv_num=0!)
        (self.tmp / "pdata" / "last_auto_event.json").write_text(
            json.dumps({
                "ok": True,
                "ts": int(time.time()),
                "from_conv": "conv-post",
                "from_conv_full": "conv-post-term",
                "to_conv_full": "conv-fresh-9999",
                "archived_old": True,
            }),
            encoding="utf-8",
        )
        pt1 = run_pia({"conversationId": "conv-post-term", "invocationNum": 0, "transcriptPath": str(tx_hi)})
        self.assertEqual(pt1.get("terminationBehavior"), "terminate")
        self.assertIn("conversation://conv-fresh-9999", pt1["injectSteps"][0]["systemMessage"]["metadata"]["user_markdown"])
        # Idempotent: does not double-terminate
        self.assertEqual(run_pia({"conversationId": "conv-post-term", "invocationNum": 1, "transcriptPath": str(tx_hi)}), {})

        # Huge overwrite injects both user_markdown AND a 1-line model nudge
        adir = self.tmp / "pdata" / "alerts"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "conv-hw-1.huge_write").write_text("big.py\t180\t200\n", encoding="utf-8")
        hw_out = run_pia({"conversationId": "conv-hw-1", "invocationNum": 5, "transcriptPath": str(tx_hi)})
        sm_hw = hw_out["injectSteps"][0]["systemMessage"]
        self.assertIn("use edit_file", sm_hw["systemMessage"])
        self.assertIn("6x output cost", sm_hw["metadata"]["user_markdown"])

        # Neon 240s warm-cache deferral in rules_sync (idle=200s still protected on Neon!)
        neon_warm = {"cur_tot": 25_000, "last_ts": time.time() - 200, "ttl_cliff_sec": 240}
        with mock.patch.object(self.rules_sync, "_is_sandbox_data_dir", return_value=False), \
             mock.patch("transcript_io.discover_session_sources", return_value=[(time.time() - 200, "cid-n", self.tmp / "tx", None)]), \
             mock.patch("transcript_io.parse_session_cached", return_value=neon_warm):
            os.environ.pop("AGY_TS_SYNC_RULES", None)
            self.assertTrue(self.rules_sync._should_defer_concise_sync())


class TestFastAcceleratorModules(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="agy-fastunit-")
        self.tmp = Path(self._td.name)
        self.ws = self.tmp / "repo"
        self.ws.mkdir()
        (self.ws / "src").mkdir()
        (self.ws / "docs").mkdir()
        (self.ws / "package.json").write_text(
            json.dumps({"name": "demo-app", "scripts": {"test": "pytest", "build": "tsc"}}),
            encoding="utf-8",
        )
        (self.ws / "pyproject.toml").write_text("[tool.pytest.ini_options]\n[tool.ruff]\n", encoding="utf-8")
        (self.ws / "Makefile").write_text("build:\n\techo hi\ntest:\n\tpytest\n", encoding="utf-8")
        (self.ws / "src" / "app.ts").write_text(
            "export interface User { id: string; }\n"
            "export type Mode =\n  | 'fast'\n  | 'safe';\n"
            "export class Service {\n"
            "  async fetchUser(id: string): Promise<User> {\n"
            "    return { id };\n"
            "  }\n"
            "}\n"
            "export function registerRoutes(app: any) {\n"
            + "\n".join(f"  // pad line {i}" for i in range(45))
            + "\n  app.get('/health', () => 'ok');\n}\n"
            "describe('Service', () => {\n  it('works', () => {});\n});\n",
            encoding="utf-8",
        )
        (self.ws / "src" / "worker.py").write_text(
            "import os\n\n"
            "@dataclass\n"
            "class Worker:\n"
            "    def run(self, x: int) -> int:\n"
            "        return x + 1\n\n"
            "async def main_loop():\n"
            "    return 42\n",
            encoding="utf-8",
        )
        (self.ws / "src" / "game.gd").write_text(
            "class_name Player\nsignal hit(dmg)\nenum State { IDLE, RUN }\n@export var hp := 10\n"
            "func take_damage(n: int) -> void:\n    hp -= n\n",
            encoding="utf-8",
        )
        (self.ws / "src" / "main.go").write_text(
            "package main\ntype Server struct { Port int }\nfunc (s *Server) Start() {\n}\nfunc main() {\n}\n",
            encoding="utf-8",
        )
        (self.ws / "src" / "lib.rs").write_text(
            "pub struct Engine<'a> { pub name: &'a str }\npub fn boot() {\n    let c = 'x';\n}\n",
            encoding="utf-8",
        )
        (self.ws / "src" / "Widget.java").write_text(
            "public class Widget {\n  public int compute(int a) {\n    return a * 2;\n  }\n}\n",
            encoding="utf-8",
        )
        (self.ws / "src" / "run.sh").write_text("deploy() {\n  echo ok\n}\n", encoding="utf-8")
        (self.ws / "src" / "view.vue").write_text(
            "<template>\n  <main id=\"app\"><CustomCard /></main>\n</template>\n"
            "<script setup>\nfunction onSave() {\n  return 1;\n}\n</script>\n"
            "<style>\n/* Theme */\n:root { --bg: #000; }\n@media (max-width: 600px) { body { margin: 0; } }\n</style>\n",
            encoding="utf-8",
        )
        (self.ws / "src" / "style.css").write_text(
            "/* Layout */\n:root { --x: 1; }\n.card {\n  display: flex;\n}\n",
            encoding="utf-8",
        )
        (self.ws / "docs" / "guide.md").write_text("# Intro\ntext\n## Setup\n```md\n# fake\n```\n", encoding="utf-8")
        (self.ws / "src" / "cfg.yaml").write_text("server:\n  port: 8080\nlogging:\n  level: info\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fast_utils_truncation_waterfill_and_output_filter(self) -> None:
        import fast_utils as fu

        # UTF-8 slicing with multi-byte characters
        cjk = "你好世界" * 40
        sl = fu.slice_utf8_bytes(cjk, 20)
        self.assertLessEqual(fu.utf8_len(sl), 20)
        slt = fu.slice_utf8_bytes_tail(cjk, 20)
        self.assertLessEqual(fu.utf8_len(slt), 20)
        self.assertLessEqual(fu.utf8_len(fu.truncate_with_byte_notice(cjk, 120)), 120)
        self.assertLessEqual(fu.utf8_len(fu.truncate_head_tail_with_byte_notice(cjk, 220)), 220)
        self.assertLessEqual(len(fu.truncate_with_notice("a\n" * 200, 120)), 120)
        self.assertLessEqual(len(fu.truncate_head_tail_with_notice("a\n" * 200, 220)), 220)

        # Water-filling
        buds = fu.compute_water_fill_budgets([10, 500, 500], 210)
        self.assertEqual(buds, [10, 100, 100])
        fitted = fu.fit_sections_to_byte_budget(["short", "x" * 1000, "你好" * 400], 300, 300)
        self.assertLessEqual(fu.utf8_len(fitted), 300)
        self.assertIn("short", fitted)

        # ANSI / spinner normalization + noise stripping + green phase folding + head-tail failure
        raw_term = "\x1b[32mPASS\x1b[0m\r\x1b[2K\x1b[31mFAIL src/a.test.ts\x1b[0m"
        self.assertEqual(fu.normalize_terminal_output(raw_term), "FAIL src/a.test.ts")
        self.assertEqual(fu.strip_benign_zero_counts("0 errors, warnings: 0").strip(), ",")
        self.assertIn("Worker 0 failed", fu.strip_benign_zero_counts("Worker 0 failed"))
        self.assertTrue(fu.is_failure_noise_line('{"level":30,"msg":"server started"}'))
        self.assertFalse(fu.is_failure_noise_line('{"level":30,"statusCode":500,"msg":"boom"}'))
        self.assertTrue(fu.is_failure_noise_line("[ERROR] Re-run Maven using the -X switch"))

        green_log = (
            ["✓ test_a_pass_ok_long_line_padding_1234567890" for _ in range(10)]
            + ["ERROR: assertion failed at line 42"]
            + [f"detail line {i} " + ("z" * 40) for i in range(40)]
        )
        folded, cnt = fu.fold_green_build_phase_lines(green_log)
        self.assertGreaterEqual(cnt, 10)
        self.assertTrue(any("collapsed" in ln for ln in folded))
        comp = fu.format_compact_command_output("\n".join(green_log), "", 1, 400)
        self.assertLessEqual(len(comp), 400)
        self.assertIn("ERROR: assertion failed", comp)
        self.assertEqual(fu.format_compact_command_output("", "", 0, 200), "OK")
        self.assertIn("TIMED OUT", fu.format_compact_command_output("partial", "", None, 200, timed_out=True))
        self.assertIn("SIGKILL", fu.format_exit_failure_label(137))

        # Arg validation + fuzzy + glob + Windows PS unwrap + schema compat + host compat
        self.assertEqual(fu.parse_maybe_json_array('["a","b"]'), (True, "ok", ["a", "b"]))
        self.assertEqual(fu.parse_maybe_json_array("true"), (False, "corrupted_literal", True))
        self.assertEqual(fu.parse_maybe_string_or_json_array("src/a.py"), (True, "ok", ["src/a.py"]))
        self.assertIn("corruption", fu.format_corrupted_array_error("H", "t", [("x", True, "obj")], "fallback"))
        matches, total = fu.rank_fuzzy_paths("app ts", ["src/app.ts", "docs/guide.md"], 5)
        self.assertEqual(total, 1)
        self.assertEqual(matches[0][0], "src/app.ts")
        self.assertTrue(fu.glob_to_regex("**/*.{ts,py}").match("src/app.ts"))
        self.assertEqual(fu.unwrap_trivial_windows_powershell_command('powershell -NoProfile -Command "git status"'), "git status")
        self.assertIn("Get-ChildItem", fu.unwrap_trivial_windows_powershell_command('powershell -Command "Get-ChildItem"'))
        san = fu.sanitize_schema_to_minimal_subset({"type": "object", "properties": {"pattern": {"type": "string", "minLength": 1}, "n": {"type": "integer", "minimum": 1}}})
        self.assertIn("pattern", san["properties"])
        self.assertNotIn("minLength", san["properties"]["pattern"])
        self.assertNotIn("minimum", san["properties"]["n"])
        fmt, _lbl = fu.resolve_edits_format(client_info={"name": "antigravity-cli", "version": "1.5.5"}, env={})
        self.assertEqual(fmt, "string")
        fmt2, _lbl2 = fu.resolve_edits_format(client_info={"name": "antigravity-cli", "version": "1.5.9"}, env={})
        self.assertEqual(fmt2, "array")

    def test_fast_search_all_modes_and_git_hardening(self) -> None:
        import fast_search as fs

        # Validate read-only git security
        self.assertIn("status", fs.validate_readonly_git_args("status --short"))
        for bad_git in ("push origin main", "status -v", "log && rm -rf /", "branch new-br", "tag v1.0", "remote add x y", "-c core.pager=sh log"):
            with self.assertRaises(ValueError):
                fs.validate_readonly_git_args(bad_git)

        # Run mixed batch (text, regex, glob with | auto-recovery, fuzzy) with BOTH rg and Python fallback
        for force_py in (False, True):
            res = fs.run_batch_search(
                {
                    "cwd": str(self.ws),
                    "queries": [
                        {"type": "text", "pattern": "fetchUser", "fileType": "ts", "contextLines": 1},
                        {"type": "regex", "pattern": "def\\s+run", "outputMode": "files_with_matches"},
                        {"type": "text", "pattern": "Worker", "outputMode": "count", "fileType": "python"},
                        {"type": "glob", "pattern": "app|worker", "sortBy": "mtime"},
                        {"type": "fuzzy", "pattern": "worker py"},
                    ],
                },
                force_python_fallback=force_py,
            )
            self.assertFalse(res["isError"], res["text"])
            self.assertIn("fetchUser", res["text"])
            self.assertIn("src/worker.py", res["text"])
            self.assertIn("auto-expanded regex '|'", res["text"])
            self.assertLessEqual(len(res["text"].encode("utf-8")), 8800)

        # Validation & corruption handling
        self.assertTrue(fs.run_batch_search({"cwd": str(self.ws), "queries": True})["isError"])
        self.assertTrue(fs.run_batch_search({"cwd": str(self.ws), "queries": []})["isError"])

    def test_fast_outline_and_read_slices(self) -> None:
        import fast_outline as fo

        res = fo.run_quick_outline(
            {
                "cwd": str(self.ws),
                "paths": [
                    "src/app.ts",
                    "src/worker.py",
                    "src/game.gd",
                    "src/main.go",
                    "src/lib.rs",
                    "src/Widget.java",
                    "src/run.sh",
                    "src/view.vue",
                    "src/style.css",
                    "docs/guide.md",
                    "package.json",
                    "src/cfg.yaml",
                ],
            }
        )
        self.assertFalse(res["isError"], res["text"])
        for expected in (
            "[class] Service", "[method] fetchUser", "[route] GET /health", "[suite] Service",
            "[class] Worker", "[function] main_loop", "[class_name] Player", "[signal] hit",
            "[struct] Server", "[fn] boot", "[class] Widget", "[function] deploy",
            "[function] onSave", "[tokens] :root", "[h1] Intro", "[key] scripts", "[key] server",
        ):
            self.assertIn(expected, res["text"])

        sl = fo.run_read_slices(
            {
                "cwd": str(self.ws),
                "outlines": ["src/worker.py"],
                "lineRanges": ["src/worker.py:3-6"],
            }
        )
        self.assertFalse(sl["isError"], sl["text"])
        self.assertIn("## Outline: src/worker.py", sl["text"])
        self.assertIn("class Worker:", sl["text"])
        # Corrupt args & invalid ranges
        self.assertTrue(fo.run_quick_outline({"cwd": str(self.ws), "paths": True})["isError"])
        self.assertTrue(fo.run_read_slices({"cwd": str(self.ws), "lineRanges": ["badspec"]})["isError"])

    def test_fast_edits_atomic_best_effort_hints_and_verify(self) -> None:
        import fast_edits as fe

        # Atomic multi-file edit + inline verify
        res = fe.run_apply_edits(
            {
                "cwd": str(self.ws),
                "edits": [
                    {"path": "src/worker.py", "hunks": [{"target": "return x + 1", "replacement": "return x + 2"}]},
                    {"path": "src/new_file.txt", "create": "hello\nworld\n"},
                ],
                "verify": f'"{sys.executable}" -c "print(\'2 passed\')"',
            }
        )
        self.assertFalse(res["isError"], res["text"])
        self.assertIn("Applied edits to 2 file(s):", res["text"])
        self.assertIn("Verify [PASS", res["text"])
        self.assertIn("return x + 2", (self.ws / "src" / "worker.py").read_text(encoding="utf-8"))
        self.assertEqual((self.ws / "src" / "new_file.txt").read_text(encoding="utf-8"), "hello\nworld\n")

        # Line-drift hint + closest-region snippet + atomic rollback (0 files modified)
        bad = fe.run_apply_edits(
            {
                "cwd": str(self.ws),
                "edits": [
                    {"path": "src/worker.py", "hunks": [{"target": "return x + 2", "replacement": "return x + 99", "startLine": 1, "endLine": 2}]},
                    {"path": "src/new_file.txt", "hunks": [{"target": "hello", "replacement": "changed"}]},
                ],
            }
        )
        self.assertTrue(bad["isError"])
        self.assertIn("Exact match found outside range", bad["text"])
        self.assertEqual((self.ws / "src" / "new_file.txt").read_text(encoding="utf-8"), "hello\nworld\n")

        # bestEffort commits valid file and reports closest match for failed file
        be = fe.run_apply_edits(
            {
                "cwd": str(self.ws),
                "bestEffort": True,
                "edits": [
                    {"path": "src/new_file.txt", "hunks": [{"oldText": "hello", "newText": "hi"}]},
                    {"path": "src/worker.py", "hunks": [{"target": "def run(self, x: int) -> str:\n    return x + 999", "replacement": "pass"}]},
                ],
            }
        )
        self.assertTrue(be["isError"])
        self.assertIn("Partial edit applied", be["text"])
        self.assertIn("Closest match near line", be["text"])
        self.assertIn("hi\n", (self.ws / "src" / "new_file.txt").read_text(encoding="utf-8"))

        # .git write protection + JSON array string format + CRLF preservation
        crlf_f = self.ws / "src" / "win.txt"
        crlf_f.write_bytes(b"\xef\xbb\xbfa\r\nb\r\n")
        ok_str = fe.run_apply_edits(
            {
                "cwd": str(self.ws),
                "edits": json.dumps([{"path": "src/win.txt", "hunks": [{"target": "a\nb", "replacement": "a\nc"}]}]),
            }
        )
        self.assertFalse(ok_str["isError"], ok_str["text"])
        self.assertEqual(crlf_f.read_bytes(), b"\xef\xbb\xbfa\r\nc\r\n")

        git_block = fe.run_apply_edits({"cwd": str(self.ws), "edits": [{"path": ".git/config", "create": "x"}]})
        self.assertTrue(git_block["isError"])
        self.assertIn(".git", git_block["text"])

    def test_fast_checks_compact_full_and_budget(self) -> None:
        import fast_checks as fc

        res = fc.run_checks(
            {
                "cwd": str(self.ws),
                "commands": [
                    {"name": "ok1", "command": f'"{sys.executable}" -c "print(\'5 tests passed\')"'},
                    {"name": "fail1", "command": f'"{sys.executable}" -c "import sys; print(\'ERROR: boom\'); sys.exit(1)"'},
                ],
                "concurrency": 2,
                "outputMode": "compact",
            }
        )
        self.assertFalse(res["isError"])
        self.assertIn("1/2 passed, 1 failed", res["text"])
        self.assertIn("[PASS", res["text"])
        self.assertIn("ERROR: boom", res["text"])
        self.assertLessEqual(len(res["text"].encode("utf-8")), 8800)

        full = fc.run_checks(
            {
                "cwd": str(self.ws),
                "commands": [{"name": "p", "command": f'"{sys.executable}" -c "print(\'hello_full\')"'}],
                "outputMode": "full",
            }
        )
        self.assertFalse(full["isError"])
        self.assertIn("hello_full", full["text"])
        self.assertTrue(fc.run_checks({"cwd": str(self.ws), "commands": []})["isError"])

    def test_fast_snapshot_session_start_hook_and_cli_and_server(self) -> None:
        import fast_cli
        import fast_snapshot
        import server
        import session_start_hook as ssh

        snap = fast_snapshot.build_workspace_snapshot(str(self.ws))
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertIn("package.json (demo-app)", snap)
        self.assertIn("pyproject.toml (pytest, ruff)", snap)
        self.assertIn("Makefile targets: build, test", snap)
        self.assertIn("Layout (2 levels):", snap)
        self.assertLessEqual(len(snap.encode("utf-8")), 1500)

        # SessionStart hook subprocess check
        out_buf = io.BytesIO()
        fake_in = mock.Mock(buffer=io.BytesIO(json.dumps({"cwd": str(self.ws)}).encode("utf-8")))
        fake_out = mock.Mock(buffer=out_buf)
        with mock.patch("sys.stdin", fake_in), mock.patch("sys.stdout", fake_out), \
             mock.patch.dict(os.environ, {"PLUGIN_DATA": str(self.tmp / "pdata")}, clear=False):
            rc = ssh.main()
        self.assertEqual(rc, 0)
        ss_doc = json.loads(out_buf.getvalue().decode("utf-8"))
        self.assertIn("injectSteps", ss_doc)

        # Kill switch disables SessionStart cleanly
        out_off = io.BytesIO()
        with mock.patch("sys.stdin", mock.Mock(buffer=io.BytesIO(json.dumps({"cwd": str(self.ws)}).encode("utf-8")))), \
             mock.patch("sys.stdout", mock.Mock(buffer=out_off)), \
             mock.patch.dict(os.environ, {"AGY_TS_STARTUP_SNAPSHOT": "off"}, clear=False):
            ssh.main()
        self.assertEqual(json.loads(out_off.getvalue().decode("utf-8")), {})

        # Direct --cli runner for outline, search, slice, edit, check
        for cli_args, expected in (
            (["--help"], "agy-tokensaver direct CLI mode"),
            (["outline", "--cwd", str(self.ws), "src/worker.py"], "[class] Worker"),
            (["search", "--cwd", str(self.ws), "--text", "fetchUser"], "fetchUser"),
            (["slice", "--cwd", str(self.ws), "src/worker.py:3-6"], "class Worker:"),
            (["check", "--cwd", str(self.ws), f'"{sys.executable}" -c "print(\'ok\')"'], "1/1 passed"),
        ):
            so, se = io.StringIO(), io.StringIO()
            rc_cli = fast_cli.run_cli(cli_args, stdin=io.StringIO(""), stdout=so, stderr=se)
            self.assertEqual(rc_cli, 0, se.getvalue() + so.getvalue())
            self.assertIn(expected, so.getvalue())

        # MCP server exposes all 5 eager accelerator tools + respects --tools / --schema-compat
        sent: list[dict] = []
        with mock.patch.object(server, "_send", side_effect=sent.append):
            server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "antigravity-cli", "version": "1.5.10"}}})
            server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            server._dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "quick_outline", "arguments": {"cwd": str(self.ws), "paths": ["src/worker.py"]}}})
        tool_names = {t["name"] for t in sent[1]["result"]["tools"]}
        self.assertTrue({"open_token_saver", "batch_search", "apply_edits", "quick_outline", "read_slices", "run_checks"}.issubset(tool_names))
        self.assertIn("[class] Worker", sent[2]["result"]["content"][0]["text"])

        show_h, err = server.parse_server_args(["--tools", "batch_search,quick_outline", "--schema-compat=minimal"])
        self.assertFalse(show_h)
        self.assertIsNone(err)
        filtered_names = {t["name"] for t in server.get_exposed_tools()}
        self.assertIn("batch_search", filtered_names)
        self.assertNotIn("run_checks", filtered_names)
        server.parse_server_args([])

    def test_fast_snapshot_git_nested_manifests_and_capping(self) -> None:
        import fast_snapshot as fsnap

        # parse_porcelain_branch_header variants
        self.assertEqual(fsnap.parse_porcelain_branch_header("## main...origin/main [ahead 2]"), ("main", False))
        self.assertEqual(fsnap.parse_porcelain_branch_header("## No commits yet on feat/x"), ("feat/x", True))
        self.assertEqual(fsnap.parse_porcelain_branch_header("## HEAD (no branch)"), ("HEAD", False))
        self.assertEqual(fsnap.parse_porcelain_branch_header("## Initial commit on dev"), ("dev", True))

        # is_home_or_system_root detection
        self.assertTrue(fsnap.is_home_or_system_root(os.path.abspath(os.sep)))
        self.assertTrue(fsnap.is_home_or_system_root(r"C:\Users" if os.name == "nt" else "/home"))
        self.assertTrue(fsnap.is_home_or_system_root(str(Path.home())))
        self.assertFalse(fsnap.is_home_or_system_root(str(self.ws)))
        self.assertIsNone(fsnap.build_workspace_snapshot(str(self.tmp / "nonexistent_dir")))

        # Real git repo with unborn branch -> initial commit -> dirty working tree -> nested subdir + multi-module manifests
        repo = self.tmp / "git_snap_repo"
        repo.mkdir()
        (repo / "pom.xml").write_text("<project><parent><artifactId>p</artifactId></parent><artifactId>demo-svc</artifactId></project>\n", encoding="utf-8")
        (repo / "build.gradle.kts").write_text("plugins {}\n", encoding="utf-8")
        (repo / "Cargo.toml").write_text("[workspace]\nmembers = [\"crates/a\"]\n", encoding="utf-8")
        (repo / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
        (repo / "docs").mkdir()
        (repo / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
        (repo / "packages" / "api").mkdir(parents=True)
        (repo / "packages" / "api" / "package.json").write_text(json.dumps({"name": "api", "scripts": {"dev": "vite"}}), encoding="utf-8")
        (repo / "packages" / "api" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
        for idx in range(22):
            (repo / f"extra_file_{idx:02d}_very_long_filename_here.txt").write_text("x\n", encoding="utf-8")

        def _git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=str(repo), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        _git("init", "-b", "main")
        _git("config", "user.email", "test@example.com")
        _git("config", "user.name", "Test User")

        unborn_snap = fsnap.build_workspace_snapshot(str(repo))
        self.assertIsNotNone(unborn_snap)
        assert unborn_snap is not None
        self.assertIn("unborn, no commits yet", unborn_snap)
        self.assertIn("demo-svc, maven", unborn_snap)
        self.assertIn("Cargo.toml (workspace)", unborn_snap)
        self.assertIn("go.mod (example.com/demo)", unborn_snap)
        self.assertIn("docs/ (a.md)", unborn_snap)

        _git("add", "pom.xml", "packages/api/index.ts")
        _git("commit", "-m", "initial commit")

        # Clean nested subdir inside repo
        nested = repo / "packages" / "api"
        nested_snap = fsnap.build_workspace_snapshot(str(nested))
        self.assertIsNotNone(nested_snap)
        assert nested_snap is not None
        self.assertIn("repo root: ../..", nested_snap)
        self.assertIn("initial commit", nested_snap)

        # Modify tracked file + add untracked file -> Modified summary
        (repo / "pom.xml").write_text("<project><artifactId>demo-svc-2</artifactId></project>\n", encoding="utf-8")
        dirty_snap = fsnap.build_workspace_snapshot(str(repo))
        self.assertIsNotNone(dirty_snap)
        assert dirty_snap is not None
        self.assertIn("Modified (", dirty_snap)

        # Tight byte cap exercises _fit_sec_bytes and proportional truncation
        capped = fsnap.build_workspace_snapshot(str(repo), max_bytes=320)
        self.assertIsNotNone(capped)
        assert capped is not None
        self.assertLessEqual(len(capped.encode("utf-8")), 320)

        # Tiny cap exercises header-only truncate_with_byte_notice fallback
        tiny = fsnap.build_workspace_snapshot(str(repo), max_bytes=40)
        self.assertIsNotNone(tiny)
        assert tiny is not None
        self.assertLessEqual(len(tiny.encode("utf-8")), 40)

        # Home-mode 1-level layout
        with mock.patch.object(fsnap, "is_home_or_system_root", return_value=True):
            home_snap = fsnap.build_workspace_snapshot(str(repo))
            self.assertIsNotNone(home_snap)
            assert home_snap is not None
            self.assertIn("Layout (1 level):", home_snap)

        # Scan deadline already expired triggers skipped dirs note
        expired = fsnap.build_workspace_snapshot(str(repo), scan_timeout_ms=1)
        self.assertIsNotNone(expired)

    def test_fast_search_git_multiline_edges_and_fallbacks(self) -> None:
        import fast_search as fs

        repo = self.tmp / "search_git_repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "alpha.py").write_text(
            "def first():\n    return 'hello'\n\ndef second():\n    # AlphaMarker\n    return 'world'\n",
            encoding="utf-8",
        )
        (repo / "src" / "beta.ts").write_text(
            "export function greet(name: string) {\n  return `hi ${name}`;\n}\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(repo), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "init search repo"], cwd=str(repo), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (repo / "src" / "alpha.py").write_text(
            "def first():\n    return 'hello2'\n\ndef second():\n    # AlphaMarker\n    return 'world'\n",
            encoding="utf-8",
        )

        for force_py in (False, True):
            res = fs.run_batch_search(
                {
                    "cwd": str(repo),
                    "queries": [
                        {"type": "regex", "pattern": "def first\\(\\):\\n\\s+return", "multiline": True},
                        {"type": "text", "pattern": "alphamarker", "caseSensitive": False, "glob": "*.py"},
                        {"type": "text", "pattern": "greet", "path": "src/beta.ts"},
                        {"type": "glob", "pattern": "alpha"},
                        {"type": "glob", "pattern": "*.ts", "sortBy": "path"},
                        {"type": "fuzzy", "pattern": "nonexistent_xyz_file"},
                        {"type": "git", "pattern": "status --short"},
                        {"type": "git", "pattern": "log -n 2 --oneline"},
                        {"type": "git", "pattern": "diff --stat"},
                        {"type": "git", "pattern": "ls-files"},
                        {"type": "git", "pattern": "rev-parse --abbrev-ref HEAD"},
                        {"type": "git", "pattern": "branch -a"},
                        {"type": "git", "pattern": "show --stat HEAD"},
                        {"type": "git", "pattern": "grep greet"},
                    ],
                },
                force_python_fallback=force_py,
            )
            self.assertFalse(res["isError"], res["text"])
            self.assertIn("AlphaMarker", res["text"])
            self.assertIn("init search repo", res["text"])
            self.assertIn("src/beta.ts", res["text"])
            self.assertLessEqual(len(res["text"].encode("utf-8")), 8800)

        # Validation & error branches
        self.assertTrue(fs.run_batch_search({"cwd": str(self.tmp / "nope"), "queries": [{"type": "text", "pattern": "a"}]})["isError"])
        self.assertTrue(fs.run_batch_search({"cwd": str(repo), "queries": [{"type": "text", "pattern": "a"}] * 21})["isError"])
        self.assertTrue(fs.run_batch_search({"cwd": str(repo), "queries": [123]})["isError"])
        bogus_q = fs.run_batch_search({"cwd": str(repo), "queries": [{"type": "bogus", "pattern": "a"}]})
        self.assertIn("error", bogus_q["text"].lower())
        empty_q = fs.run_batch_search({"cwd": str(repo), "queries": [{"type": "text", "pattern": ""}]})
        self.assertIn("error", empty_q["text"].lower())
        bad_re = fs.run_batch_search({"cwd": str(repo), "queries": [{"type": "regex", "pattern": "[unclosed"}]})
        self.assertIn("error", bad_re["text"].lower())
        # Tight maxChars budget packing
        tight = fs.run_batch_search(
            {
                "cwd": str(repo),
                "maxChars": 500,
                "queries": [
                    {"type": "text", "pattern": "return", "maxResults": 50},
                    {"type": "git", "pattern": "diff"},
                ],
            }
        )
        self.assertFalse(tight["isError"])
        self.assertLessEqual(len(tight["text"].encode("utf-8")), 8800)

    def test_fast_outline_and_slices_overflow_and_extra_languages(self) -> None:
        import fast_outline as fo

        extra = self.tmp / "outline_extra"
        extra.mkdir()
        # Large TS file with many classes/methods to trigger nested + top-level budget packing
        big_ts = "\n".join(
            [f"export class Service{i} {{\n  handle{i}() {{\n    return {i};\n  }}\n}}" for i in range(60)]
        ) + "\n"
        (extra / "big.ts").write_text(big_ts, encoding="utf-8")
        # Broken Python syntax triggers regex fallback
        (extra / "broken.py").write_text("class Broken(\n    pass\n\ndef recover_fn():\n    return 1\n", encoding="utf-8")
        # Pure HTML, C++, pretty JSON, binary file
        (extra / "index.html").write_text(
            "<html><head><style>:root { --c: 1; }</style></head><body><main id=\"root\"><script>function boot() { return 1; }</script></main></body></html>\n",
            encoding="utf-8",
        )
        (extra / "engine.cpp").write_text("namespace core {\nclass Engine {\n  int run() {\n    return 0;\n  }\n};\n}\n", encoding="utf-8")
        (extra / "pretty.json").write_text(json.dumps({"alpha": 1, "beta": {"nested": True}}, indent=2) + "\n", encoding="utf-8")
        (extra / "bin.dat").write_bytes(b"\x00\x01\x02\x03")

        res = fo.run_quick_outline(
            {
                "cwd": str(extra),
                "paths": ["big.ts", "broken.py", "index.html", "engine.cpp", "pretty.json", "bin.dat", "missing.py"],
                "maxChars": 1200,
            }
        )
        self.assertFalse(res["isError"])
        self.assertIn("recover_fn", res["text"])
        self.assertIn("pretty.json", res["text"])
        self.assertLessEqual(len(res["text"].encode("utf-8")), 8800)

        # Very small per-file budget forces top-level L<min>-L<max> truncation footer
        tiny_out = fo.run_quick_outline({"cwd": str(extra), "paths": ["big.ts"], "maxChars": 1000})
        self.assertFalse(tiny_out["isError"])
        self.assertIn("omitted", tiny_out["text"])

        # read_slices with structured slices array + overlapping/clamped ranges + error paths
        sl = fo.run_read_slices(
            {
                "cwd": str(extra),
                "slices": [
                    {"path": "big.ts", "ranges": [[1, 5], [3, 8], [999, 1005]]},
                    {"path": "missing.ts", "ranges": [[1, 5]]},
                ],
                "outlines": ["pretty.json"],
                "maxChars": 1000,
            }
        )
        self.assertFalse(sl["isError"])
        self.assertIn("Service0", sl["text"])
        self.assertTrue(fo.run_read_slices({"cwd": str(extra)})["isError"])
        self.assertTrue(fo.run_quick_outline({"cwd": str(extra), "paths": []})["isError"])
        self.assertTrue(fo.run_quick_outline({"cwd": str(extra), "paths": ["a.py"] * 41})["isError"])

    def test_fast_edits_edge_cases_and_checks_budget_and_timeout(self) -> None:
        import fast_checks as fc
        import fast_edits as fe

        ed = self.tmp / "edits_ws"
        ed.mkdir()
        (ed / "a.py").write_text("x = 1\nx = 1\ny = 2\n", encoding="utf-8")

        # dryRun does not write file
        dr = fe.run_apply_edits(
            {
                "cwd": str(ed),
                "dryRun": True,
                "edits": [{"path": "a.py", "hunks": [{"old_string": "y = 2", "new_string": "y = 9"}]}],
                "verify": f'"{sys.executable}" -c "print(\'should skip\')"',
            }
        )
        self.assertFalse(dr["isError"], dr["text"])
        self.assertIn("Dry run", dr["text"])
        self.assertIn("y = 2", (ed / "a.py").read_text(encoding="utf-8"))

        # Ambiguous multiple matches fail without replaceAll; succeed with replaceAll
        amb = fe.run_apply_edits({"cwd": str(ed), "edits": [{"path": "a.py", "hunks": [{"target": "x = 1", "replacement": "x = 2"}]}]})
        self.assertTrue(amb["isError"])
        self.assertIn("2 times", amb["text"])

        rep_all = fe.run_apply_edits(
            {"cwd": str(ed), "edits": [{"path": "a.py", "hunks": [{"target": "x = 1", "replacement": "x = 2", "replaceAll": True}]}]}
        )
        self.assertFalse(rep_all["isError"], rep_all["text"])
        self.assertEqual((ed / "a.py").read_text(encoding="utf-8"), "x = 2\nx = 2\ny = 2\n")

        # No-op edit + overlapping hunks + failing verify + validation errors
        noop = fe.run_apply_edits(
            {"cwd": str(ed), "edits": [{"path": "a.py", "hunks": [{"target": "y = 2", "replacement": "y = 2"}]}]}
        )
        self.assertFalse(noop["isError"])
        self.assertIn("no-op", noop["text"])

        ov = fe.run_apply_edits(
            {
                "cwd": str(ed),
                "edits": [
                    {
                        "path": "a.py",
                        "hunks": [
                            {"target": "x = 2\nx = 2", "replacement": "z = 1"},
                            {"target": "x = 2\ny = 2", "replacement": "z = 2"},
                        ],
                    }
                ],
            }
        )
        self.assertTrue(ov["isError"])
        self.assertIn("overlap", ov["text"].lower())

        vfail = fe.run_apply_edits(
            {
                "cwd": str(ed),
                "edits": [{"path": "a.py", "hunks": [{"target": "y = 2", "replacement": "y = 3"}]}],
                "verify": f'"{sys.executable}" -c "import sys; print(\'FAIL detail\'); sys.exit(2)"',
                "maxVerifyChars": 400,
            }
        )
        self.assertFalse(vfail["isError"])
        self.assertIn("Verify [FAIL", vfail["text"])
        self.assertIn("FAIL detail", vfail["text"])

        # Invalid args & duplicate file path
        self.assertTrue(fe.run_apply_edits({"cwd": str(ed), "edits": True})["isError"])
        self.assertTrue(fe.run_apply_edits({"cwd": str(ed), "edits": []})["isError"])
        self.assertTrue(fe.run_apply_edits({"cwd": str(ed), "edits": [{"path": "a.py", "create": "x", "hunks": [{"target": "a", "replacement": "b"}]}]})["isError"])
        self.assertTrue(
            fe.run_apply_edits(
                {"cwd": str(ed), "edits": [{"path": "a.py", "create": "1"}, {"path": "a.py", "create": "2"}]}
            )["isError"]
        )

        # fast_checks: budget exhaustion -> [NOT RUN], timeout -> [TIMEOUT], pass shrinking, full Head-Tail truncation
        not_run = fc.run_checks(
            {
                "cwd": str(ed),
                "concurrency": 1,
                "commands": [
                    f'"{sys.executable}" -c "import time; time.sleep(0.25)"',
                    f'"{sys.executable}" -c "print(\'second\')"',
                ],
            },
            budget_ms=150,
        )
        self.assertFalse(not_run["isError"])
        self.assertIn("[NOT RUN]", not_run["text"])

        noisy_fail = fc.run_checks(
            {
                "cwd": str(ed),
                "maxChars": 700,
                "commands": [
                    {"name": "p1", "command": f'"{sys.executable}" -c "print(\'10 tests passed\')"'},
                    {"name": "p2", "command": f'"{sys.executable}" -c "print(\'20 tests passed\')"'},
                    {
                        "name": "bigfail",
                        "command": f'"{sys.executable}" -c "import sys; print(\'\\n\'.join(f\'ERR line {{i}}: \' + \'x\'*40 for i in range(60))); sys.exit(1)"',
                    },
                ],
                "outputMode": "compact",
            }
        )
        self.assertFalse(noisy_fail["isError"])
        self.assertIn("[FAIL", noisy_fail["text"])
        self.assertLessEqual(len(noisy_fail["text"].encode("utf-8")), 8800)

        full_trunc = fc.run_checks(
            {
                "cwd": str(ed),
                "maxChars": 500,
                "outputMode": "full",
                "commands": [
                    {
                        "name": "longcmd",
                        "command": f'"{sys.executable}" -c "print(\'\\n\'.join(f\'line {{i}}\' for i in range(120)))"',
                    }
                ],
            }
        )
        self.assertFalse(full_trunc["isError"])
        self.assertIn("folded", full_trunc["text"])
        self.assertTrue(fc.run_checks({"cwd": str(ed), "commands": True})["isError"])
        self.assertTrue(fc.run_checks({"cwd": str(ed), "commands": [123]})["isError"])
        self.assertTrue(fc.run_checks({"cwd": str(ed), "commands": [{"command": "echo hi", "cwd": "does_not_exist"}]})["isError"])

    def test_fast_cli_and_utils_and_server_roots_guards(self) -> None:
        import fast_cli
        import fast_utils as fu
        import server

        # fast_utils: output noise filters, head-tail compaction, env host compat, schema compat, fuzzy ranking
        noisy = "\n".join(
            [
                "\x1b[32m[INFO] Building demo\x1b[0m",
                '{"level":30,"time":1700000000,"msg":"pino info"}',
                "0 errors, 0 warnings",
                "PASS tests/a.test.ts",
                "PASS tests/b.test.ts",
                "FAIL tests/c.test.ts",
                "AssertionError: expected 1 to equal 2",
            ]
        )
        compact = fu.format_compact_command_output(noisy, "", 1, 600)
        self.assertIn("AssertionError", compact)
        self.assertNotIn("pino info", compact)
        self.assertNotIn("\x1b[32m", compact)
        long_txt = "\n".join(f"L{i}: " + "z" * 30 for i in range(80))
        self.assertIn("folded", fu.truncate_head_tail_with_notice(long_txt, 400))
        self.assertIn("folded", fu.truncate_head_tail_with_byte_notice(long_txt, 400))
        self.assertEqual(fu.resolve_edits_format(client_info=None, env={"AGY_TS_EDITS_FORMAT": "string"})[0], "string")
        self.assertEqual(fu.resolve_edits_format(client_info=None, env={"AGY_FAST_EDITS_FORMAT": "array"})[0], "array")
        self.assertIn(";", fu.unwrap_trivial_windows_powershell_command('powershell -Command "echo a; echo b"'))
        san = fu.sanitize_schema_to_minimal_subset(
            {"type": "object", "properties": {"arr": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "string", "pattern": "^[a-z]+$"}}}}
        )
        self.assertNotIn("minItems", san["properties"]["arr"])
        self.assertNotIn("maxItems", san["properties"]["arr"])
        self.assertNotIn("pattern", san["properties"]["arr"]["items"])
        ranked, total_ranked = fu.rank_fuzzy_paths("auth token", ["src/utils/auth_token.py", "README.md", "src/auth.py"], 5)
        self.assertGreaterEqual(total_ranked, 1)
        self.assertEqual(ranked[0][0], "src/utils/auth_token.py")

        # fast_cli: all subcommands, flags, JSON shorthand, stdin, errors, non-zero exit codes
        cases = [
            (["search", "--cwd", str(self.ws), "--regex", "def\\s+run", "--file-type=python", "--output-mode=count", "-i", "--max-chars=2000"], "", 0),
            (["search", "--cwd", str(self.ws), "--glob", "*.py", "--sort-by=mtime", "--context", "1", "--max-results", "5", "--multiline"], "", 0),
            (["search", "--cwd", str(self.ws), "--fuzzy", "worker", "--", "fetchUser"], "", 0),
            (["search", "--cwd", str(self.ws), "--json", json.dumps([{"type": "text", "pattern": "Worker"}])], "", 0),
            (["outline", "--cwd", str(self.ws), "--json", json.dumps(["src/worker.py"])], "", 0),
            (["slice", "--cwd", str(self.ws), "--outline", "src/worker.py", "src/worker.py:1-4"], "", 0),
            (["slice", "--cwd", str(self.ws), "--json", json.dumps([{"path": "src/worker.py", "ranges": [[1, 3]]}])], "", 0),
            (
                ["edit", "--cwd", str(self.ws), "--dry-run", "--best-effort", "--verify", f'"{sys.executable}" -c "print(1)"', "--verify-timeout", "10", "--max-verify-chars", "500", "--stdin"],
                json.dumps([{"path": "src/worker.py", "hunks": [{"target": "class Worker:", "replacement": "class Worker2:"}]}]),
                0,
            ),
            (["check", "--cwd", str(self.ws), "--full", "--concurrency", "1", "--timeout", "10", "--json", json.dumps([f'"{sys.executable}" -c "print(1)"'])], "", 0),
            (["check", "--cwd", str(self.ws), "--compact", f'"{sys.executable}" -c "import sys; sys.exit(1)"'], "", 1),
            (["unknown_cmd"], "", 1),
            (["search", "--cwd", str(self.ws)], "", 1),
            (["outline", "--cwd", str(self.ws)], "", 1),
            (["slice", "--cwd", str(self.ws)], "", 1),
            (["edit", "--cwd", str(self.ws)], "", 1),
            (["check", "--cwd", str(self.ws)], "", 1),
            (["search", "--bad-flag"], "", 1),
            (["search", "--type", "invalid", "x"], "", 1),
            (["search", "--max-results", "notint", "x"], "", 1),
            (["search", "--json", "{bad json"], "", 1),
        ]
        for argv, stdin_txt, exp_rc in cases:
            so, se = io.StringIO(), io.StringIO()
            rc = fast_cli.run_cli(argv, stdin=io.StringIO(stdin_txt), stdout=so, stderr=se)
            self.assertEqual(rc, exp_rc, f"argv={argv} rc={rc} out={so.getvalue()} err={se.getvalue()}")

        # server.py: roots/list handshake, notifications/roots/list_changed, CWD guard, host-compat edits schema, CLI/help/framing
        sent: list[dict] = []
        with mock.patch.object(server, "_send", side_effect=sent.append):
            server._dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "clientInfo": {"name": "antigravity-cli", "version": "1.5.5"},
                        "capabilities": {"roots": {"listChanged": True}},
                    },
                }
            )
            # Server requested roots/list
            self.assertTrue(any(m.get("method") == "roots/list" for m in sent))
            # Client replies with file:// URI for self.ws
            server._dispatch({"jsonrpc": "2.0", "id": "ts-roots-init", "result": {"roots": [{"uri": self.ws.as_uri()}]}})
            self.assertEqual(server._default_workspace_cwd, str(self.ws))
            # tools/list switches apply_edits.edits schema to string for CLI 1.5.5
            server._dispatch({"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}})
            tools_map = {t["name"]: t for t in sent[-1]["result"]["tools"]}
            self.assertEqual(tools_map["apply_edits"]["inputSchema"]["properties"]["edits"]["type"], "string")
            # roots/list_changed notification triggers refresh
            server._dispatch({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"})
            self.assertEqual(sent[-1].get("id"), "ts-roots-refresh")
            # Call all 5 accelerator tools through server._dispatch using resolved root
            server._dispatch({"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "batch_search", "arguments": {"queries": [{"type": "text", "pattern": "Worker"}]}}})
            self.assertIn("Worker", sent[-1]["result"]["content"][0]["text"])
            server._dispatch({"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "read_slices", "arguments": {"lineRanges": ["src/worker.py:1-5"]}}})
            self.assertIn("class Worker", sent[-1]["result"]["content"][0]["text"])
            server._dispatch({"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {"name": "run_checks", "arguments": {"commands": [{"command": f'"{sys.executable}" -c "print(42)"'}]}}})
            self.assertIn("1/1 passed", sent[-1]["result"]["content"][0]["text"])
            server._dispatch({"jsonrpc": "2.0", "id": 15, "method": "tools/call", "params": {"name": "apply_edits", "arguments": {"dryRun": True, "edits": [{"path": "src/worker.py", "hunks": [{"target": "class Worker:", "replacement": "class W:"}]}]}}})
            self.assertIn("Dry run", sent[-1]["result"]["content"][0]["text"])

            # CWD safety guard blocks plugin-root / home CWD when roots/list is absent and explicit cwd is omitted
            server._default_workspace_cwd = None
            with mock.patch("os.getcwd", return_value=str(Path.home())):
                server._dispatch({"jsonrpc": "2.0", "id": 16, "method": "tools/call", "params": {"name": "batch_search", "arguments": {"queries": [{"type": "text", "pattern": "x"}]}}})
            self.assertTrue(sent[-1]["result"].get("isError"))
            self.assertIn("explicitly in 'cwd'", sent[-1]["result"]["content"][0]["text"])

        # parse_server_args error & help branches + _read_messages Content-Length framing
        self.assertEqual(server.parse_server_args(["--help"]), (True, None))
        self.assertIsNotNone(server.parse_server_args(["--tools", "bogus"])[1])
        self.assertIsNotNone(server.parse_server_args(["--schema-compat", "bogus"])[1])
        self.assertIsNotNone(server.parse_server_args(["--unknown"])[1])
        server.parse_server_args([])
        server._active_client_info = None
        server._default_workspace_cwd = None

        frame = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        framed_stream = b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\n" + frame + b"\n"
        with mock.patch("sys.stdin", mock.Mock(buffer=io.BytesIO(framed_stream))):
            msgs = list(server._read_messages())
        self.assertEqual(len(msgs), 2)
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(server.main(["--help"]), 0)
            self.assertEqual(server.main(["--cli", "--help"]), 0)

    def test_fast_security_mixed_eol_rollback_and_noise_filters(self) -> None:
        import fast_edits as fe
        import fast_search as fs
        import fast_utils as fu

        # Comprehensive read-only git validation + hardening coverage
        for ok_git in (
            "branch --list main",
            "branch --contains HEAD",
            "branch --sort=refname -l",
            "branch --merged=HEAD -- main",
            "tag --list v*",
            "tag -n5 -l",
            "tag --contains HEAD",
            "tag --sort=creatordate --points-at=HEAD",
            "remote -v",
            "remote show origin",
            "remote get-url --push origin",
            "blame src/a.py",
            "shortlog -sn",
            "shortlog -s -- src/a.py",
        ):
            args = fs.validate_readonly_git_args(ok_git)
            hardened = fs._harden_git_args(args)
            self.assertIn("--no-optional-locks", hardened)
            if ok_git.startswith("shortlog"):
                self.assertIn("HEAD", hardened)
            if ok_git == "remote show origin":
                self.assertIn("-n", hardened)

        for bad_git in (
            "branch --delete x",
            "branch --foo=1",
            "branch --contains -x",
            "branch -D x",
            "branch -- main",
            "tag --delete v1",
            "tag --foo=1",
            "tag --contains -x",
            "tag -d v1",
            "tag -- v1",
            "remote --bad",
            "remote show --bad origin",
            "remote show https://example.com/a.git",
        ):
            with self.assertRaises(ValueError, msg=f"expected ValueError for {bad_git}"):
                fs.validate_readonly_git_args(bad_git)

        # Glob fallback with wildcard '|' branches + pure-Python truncation caps
        gdir = self.tmp / "glob_py_ws"
        gdir.mkdir()
        for i in range(6):
            (gdir / f"f{i}.py").write_text("\n".join(f"hit_token line {j}" for j in range(15)) + "\n", encoding="utf-8")
        (gdir / "f0.ts").write_text("export const hit_token = 1;\n", encoding="utf-8")
        py_res = fs.run_batch_search(
            {
                "cwd": str(gdir),
                "queries": [
                    {"type": "glob", "pattern": "*.py|*.ts"},
                    {"type": "text", "pattern": "hit_token", "maxResults": 4, "contextLines": 1},
                    {"type": "text", "pattern": "hit_token", "outputMode": "files_with_matches", "maxResults": 2},
                    {"type": "text", "pattern": "hit_token", "outputMode": "count", "maxResults": 2},
                ],
            },
            force_python_fallback=True,
        )
        self.assertFalse(py_res["isError"], py_res["text"])
        self.assertIn("auto-expanded regex '|'", py_res["text"])

        # fast_edits: mixed EOL preservation, nested dir creation, atomic rollback on I/O failure, alias conflict, bad line bounds
        ew = self.tmp / "edits_edge_ws"
        ew.mkdir()
        mixed = ew / "mixed.txt"
        mixed.write_bytes(b"line1\r\nline2\nline3\r\nline4\n")
        m_res = fe.run_apply_edits(
            {
                "cwd": str(ew),
                "edits": [
                    {"path": "mixed.txt", "hunks": [{"target": "line1\nline2", "replacement": "LINE1\nLINE2"}]},
                    {"path": "deep/sub/new.txt", "create": "fresh\n"},
                ],
            }
        )
        self.assertFalse(m_res["isError"], m_res["text"])
        self.assertEqual(mixed.read_bytes(), b"LINE1\r\nLINE2\nline3\r\nline4\n")
        self.assertTrue((ew / "deep" / "sub" / "new.txt").is_file())

        # Atomic rollback restores existing file and removes newly created file + dirs when a later replace fails
        orig_bytes = mixed.read_bytes()
        real_replace = os.replace
        call_n = {"n": 0}

        def _flaky_replace(src: str, dst: str) -> None:
            call_n["n"] += 1
            if call_n["n"] == 2:
                raise OSError("simulated disk full")
            real_replace(src, dst)

        with mock.patch("os.replace", side_effect=_flaky_replace):
            rb = fe.run_apply_edits(
                {
                    "cwd": str(ew),
                    "edits": [
                        {"path": "mixed.txt", "hunks": [{"target": "LINE1", "replacement": "ROLLBACK_ME"}]},
                        {"path": "rb_dir/sub/f2.txt", "create": "temp\n"},
                    ],
                }
            )
        self.assertTrue(rb["isError"])
        self.assertEqual(mixed.read_bytes(), orig_bytes)
        self.assertFalse((ew / "rb_dir" / "sub" / "f2.txt").exists())

        # Hunk alias conflicts & invalid line ranges & verify timeout
        self.assertTrue(
            fe.run_apply_edits(
                {"cwd": str(ew), "edits": [{"path": "mixed.txt", "hunks": [{"target": "a", "oldText": "b", "replacement": "c"}]}]}
            )["isError"]
        )
        self.assertTrue(
            fe.run_apply_edits(
                {"cwd": str(ew), "edits": [{"path": "mixed.txt", "hunks": [{"target": "LINE1", "replacement": "X", "startLine": 5, "endLine": 2}]}]}
            )["isError"]
        )
        self.assertTrue(fe.run_apply_edits({"cwd": str(ew), "edits": "[object Object]"})["isError"])

        # fast_utils: corrupted literals, glob_to_regex, green-phase pass folding, missing binary, shell timeout
        self.assertEqual(fu.parse_maybe_json_array("true")[1], "corrupted_literal")
        self.assertEqual(fu.parse_maybe_json_array("null")[1], "corrupted_literal")
        self.assertEqual(fu.parse_maybe_json_array("[object Object]")[1], "invalid_json")
        self.assertEqual(fu.parse_maybe_json_array("{bad")[1], "invalid_json")
        self.assertEqual(fu.parse_maybe_string_or_json_array("true")[1], "corrupted_literal")
        gre = fu.glob_to_regex("src/**/{a,b}_?.py")
        self.assertIsNotNone(gre.match("src/sub/a_1.py"))
        self.assertIsNone(gre.match("src/sub/c_1.py"))
        green_then_fail = "\n".join([f"PASS tests/t{i}.spec.ts" for i in range(18)] + ["FAIL tests/broken.spec.ts", "TypeError: boom"])
        folded = fu.format_compact_command_output(green_then_fail, "", 1, 500)
        self.assertIn("TypeError: boom", folded)
        miss_bin = fu.exec_binary("definitely_nonexistent_binary_agy_ts_xyz", ["--version"], cwd=str(ew), timeout_ms=1000)
        self.assertIsNotNone(miss_bin.error)
        tout = fu.run_shell_command(f'"{sys.executable}" -c "import time; time.sleep(2)"', cwd=str(ew), timeout_ms=120)
        self.assertTrue(tout.timed_out)

        # Multi-byte UTF-8 safe truncation, water-filling, byte budget fitting, exit labels, host compat versions
        emoji = "🔥" * 80
        self.assertLessEqual(len(fu.truncate_with_byte_notice(emoji, 60).encode("utf-8")), 60)
        self.assertLessEqual(len(fu.truncate_with_notice(emoji, 30)), 30)
        self.assertEqual(fu.compute_water_fill_budgets([100, 100], 0), [0, 0])
        self.assertEqual(sum(fu.compute_water_fill_budgets([50, 500, 1000], 300)), 300)
        fitted = fu.fit_sections_to_byte_budget(["a" * 300, "b" * 300], 200, 200, "\n\n")
        self.assertLessEqual(len(fitted.encode("utf-8")), 200)
        self.assertIn("SIGTERM", fu.format_exit_failure_label(None, "SIGTERM"))
        self.assertIn("exit 3", fu.format_exit_failure_label(3, None))
        for ver, exp_fmt in (("1.4.9", "array"), ("1.5.0", "string"), ("1.5.7", "string"), ("1.5.8", "array")):
            self.assertEqual(fu.resolve_edits_format(client_info={"name": "antigravity-cli", "version": ver}, env={})[0], exp_fmt)
        self.assertEqual(fu.resolve_edits_format(client_info={"name": "other-client", "version": "1.5.5"}, env={})[0], "array")

        # Symlink edit preserves symlink (skip only if OS denies symlink privilege), plus missing file / empty hunks / mocked verify timeout
        real_f = ew / "real_target.txt"
        real_f.write_text("alpha\nbeta\n", encoding="utf-8")
        sym_f = ew / "link_target.txt"
        try:
            os.symlink(str(real_f), str(sym_f))
            has_sym = True
        except OSError:
            has_sym = False
        if has_sym:
            sym_res = fe.run_apply_edits({"cwd": str(ew), "edits": [{"path": "link_target.txt", "hunks": [{"target": "alpha", "replacement": "ALPHA"}]}]})
            self.assertFalse(sym_res["isError"], sym_res["text"])
            self.assertTrue(os.path.islink(str(sym_f)))
            self.assertIn("ALPHA", real_f.read_text(encoding="utf-8"))
        self.assertTrue(fe.run_apply_edits({"cwd": str(ew), "edits": [{"path": "does_not_exist.txt", "hunks": [{"target": "a", "replacement": "b"}]}]})["isError"])
        self.assertTrue(fe.run_apply_edits({"cwd": str(ew), "edits": [{"path": "real_target.txt", "hunks": []}]})["isError"])
        self.assertTrue(fe.run_apply_edits({"cwd": str(ew), "edits": [123]})["isError"])
        fake_to = fu.ExecResult(exit_code=None, sig="SIGTERM", timed_out=True, stdout="partial", stderr="", truncated=False, duration_ms=1000, error=None)
        with mock.patch.object(fe, "run_shell_command", return_value=fake_to):
            vt = fe.run_apply_edits(
                {
                    "cwd": str(ew),
                    "edits": [{"path": "real_target.txt", "hunks": [{"target": "beta", "replacement": "BETA"}]}],
                    "verify": "echo slow",
                    "verifyTimeoutSec": 1,
                }
            )
            self.assertIn("Verify [TIMEOUT", vt["text"])

        # Search zero-match, non-git dir git query, fileType unknown ext, camelCase fuzzy match
        (gdir / "getUserData.toml").write_text("key = 1\n", encoding="utf-8")
        z_res = fs.run_batch_search(
            {
                "cwd": str(gdir),
                "queries": [
                    {"type": "text", "pattern": "never_matches_anything_xyz", "fileType": "toml"},
                    {"type": "glob", "pattern": "*.doesnotexist"},
                    {"type": "fuzzy", "pattern": "getUserData"},
                    {"type": "git", "pattern": "status --short"},
                ],
            }
        )
        self.assertFalse(z_res["isError"])
        self.assertIn("getUserData.toml", z_res["text"])

    def test_fast_search_rg_path_mocked(self) -> None:
        import fast_search as fs
        import fast_utils as fu

        def _mk_res(code: int | None = 0, out: str = "", err: str = "", spawn_err: str | None = None) -> fu.ExecResult:
            return fu.ExecResult(exit_code=code, sig=None, timed_out=False, duration_ms=5, stdout=out, stderr=err, truncated=False, error=spawn_err)

        def _fake_rg(bin_name: str, args: list[str], cwd: str = "", timeout_ms: int = 1000, max_output_bytes: int = 1024 * 1024) -> fu.ExecResult:
            self.assertEqual(bin_name, "rg")
            if "--files-with-matches" in args:
                return _mk_res(0, "src/worker.py\0src/app.ts\0")
            if "--count-matches" in args or ("--count" in args and "--json" not in args):
                return _mk_res(0, "src/worker.py\0" + "2\n")
            if "--json" in args:
                evs = [
                    {"type": "context", "data": {"path": {"text": "src/worker.py"}, "line_number": 3, "lines": {"text": "@dataclass\n"}}},
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "src/worker.py"},
                            "line_number": 4,
                            "lines": {"text": "class Worker:\n    def run(self):\n"} if "--multiline" in args else {"text": "class Worker:\n"},
                            "submatches": [{"start": 6, "end": 12}],
                        },
                    },
                ]
                return _mk_res(0, "\n".join(json.dumps(e) for e in evs) + "\n")
            return _mk_res(2, "", "regex parse error")

        with mock.patch.object(fs, "is_ripgrep_available", return_value=True), \
             mock.patch.object(fs, "exec_binary", side_effect=_fake_rg):
            res = fs.run_batch_search(
                {
                    "cwd": str(self.ws),
                    "queries": [
                        {"type": "text", "pattern": "Worker", "fileType": "python", "contextLines": 1, "caseSensitive": False, "glob": "*.py"},
                        {"type": "regex", "pattern": "class Worker:\\n", "multiline": True},
                        {"type": "text", "pattern": "Worker", "outputMode": "files_with_matches"},
                        {"type": "text", "pattern": "Worker", "outputMode": "count", "multiline": True},
                        {"type": "text", "pattern": "Worker", "path": "src/worker.py", "glob": "*.py"},
                        {"type": "text", "pattern": "Worker", "path": "src/worker.py", "fileType": "ts"},
                        {"type": "text", "pattern": "Worker", "path": "src/worker.py", "glob": "!*.py"},
                    ],
                }
            )
            self.assertFalse(res["isError"], res["text"])
            self.assertIn("class Worker:", res["text"])
            self.assertIn("src/worker.py", res["text"])

        # Also test rg exit_code=2 error propagation and spawn error fallback
        with mock.patch.object(fs, "is_ripgrep_available", return_value=True), \
             mock.patch.object(fs, "exec_binary", return_value=_mk_res(2, "", "bad regex")):
            err_res = fs.run_batch_search({"cwd": str(self.ws), "queries": [{"type": "regex", "pattern": "[bad"}]})
            self.assertIn("bad regex", err_res["text"])

        with mock.patch.object(fs, "is_ripgrep_available", return_value=True), \
             mock.patch.object(fs, "exec_binary", return_value=_mk_res(None, "", "", "ENOENT")):
            fb_res = fs.run_batch_search({"cwd": str(self.ws), "queries": [{"type": "text", "pattern": "Worker"}]})
            self.assertFalse(fb_res["isError"])
            self.assertIn("Worker", fb_res["text"])


if __name__ == "__main__":
    unittest.main()
