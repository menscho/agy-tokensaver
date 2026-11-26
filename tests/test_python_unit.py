#!/usr/bin/env python3
"""In-process Python unit tests for mcp_app/ and scripts/ (enables coverage.py reporting)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
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


if __name__ == "__main__":
    unittest.main()
