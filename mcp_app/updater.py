"""Tagged GitHub Release checker, SHA-256 + optional gh attestation verifier, and updater."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from config import DATA_DIR, GITHUB_REPO, GITHUB_RELEASES_API, GITHUB_RELEASES_URL, PLUGIN_ROOT

_update_cache: tuple[float, dict[str, Any]] = (0.0, {})

def _parse_semver(v: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(v or ""))
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def get_installed_version() -> str:
    pj = PLUGIN_ROOT / "plugin.json"
    if pj.is_file():
        try:
            d = json.loads(pj.read_text(encoding="utf-8"))
            if d.get("version"):
                return str(d["version"]).lstrip("v")
        except Exception:
            pass
    return "1.0.0"


def get_local_changelog() -> str:
    cl = PLUGIN_ROOT / "CHANGELOG.md"
    if cl.is_file():
        try:
            return cl.read_text(encoding="utf-8")[:12000]
        except OSError:
            pass
    return ""


def check_requirements() -> list[dict[str, Any]]:
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = (sys.version_info.major, sys.version_info.minor) >= (3, 9)

    plugin_writable = os.access(PLUGIN_ROOT, os.W_OK)
    data_exists = DATA_DIR.is_dir() or os.access(DATA_DIR.parent, os.W_OK)
    return [
        {
            "name": "Python 3.9+ (single zero-dep runtime)",
            "status": "ok" if py_ok else "bad",
            "detail": f"Python {py_ver}",
            "required": True,
        },
        {
            "name": "Plugin folder writable",
            "status": "ok" if plugin_writable else "bad",
            "detail": str(PLUGIN_ROOT),
            "required": True,
        },
        {
            "name": "Statistics & archive preserved",
            "status": "ok" if data_exists else "warn",
            "detail": f"Stored separately in {DATA_DIR} (never overwritten)",
            "required": False,
        },
    ]


def get_update_snapshot() -> dict[str, Any]:
    """Fast local + cached update snapshot (never blocks build_state on network)."""
    installed = get_installed_version()
    base = {
        "repo": GITHUB_REPO,
        "releases_url": GITHUB_RELEASES_URL,
        "installed_version": installed,
        "latest_version": installed,
        "update_available": False,
        "release_notes": "",
        "published_at": "",
        "tarball_url": "",
        "local_changelog": get_local_changelog(),
        "requirements": check_requirements(),
        "checked_at": 0,
        "error": "",
    }
    if _update_cache[1]:
        merged = {**base, **_update_cache[1], "installed_version": installed, "requirements": base["requirements"]}
        merged["update_available"] = _parse_semver(merged.get("latest_version", installed)) > _parse_semver(installed)
        return merged
    return base


def parse_sha256sums(text: str, filename: str) -> str | None:
    """Extract expected SHA-256 hex digest for `filename` from a SHA256SUMS.txt file."""
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            digest = parts[0].lower()
            name = parts[-1].lstrip("*")
            if name == filename and re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
    return None


def verify_sha256_bytes(blob: bytes, expected_hex: str) -> bool:
    actual = hashlib.sha256(blob).hexdigest().lower()
    return actual == expected_hex.strip().lower()


def check_github_updates(force: bool = False) -> dict[str, Any]:
    global _update_cache
    now = time.time()
    if not force and _update_cache[1] and (now - _update_cache[0]) < 600:
        return get_update_snapshot()

    installed = get_installed_version()
    req = urllib.request.Request(
        GITHUB_RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"agy-tokensaver/{installed}",
        },
    )
    info: dict[str, Any] = {
        "latest_version": installed,
        "tag_name": "",
        "release_notes": "",
        "published_at": "",
        "asset_name": "",
        "asset_url": "",
        "checksum_url": "",
        "checked_at": int(now),
        "error": "",
    }
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        tag_raw = str(data.get("tag_name") or "").strip()
        if not tag_raw or not re.match(r"^v?\d+\.\d+\.\d+", tag_raw):
            info["error"] = "Latest release has no valid semver tag."
        else:
            info["tag_name"] = tag_raw
            info["latest_version"] = tag_raw.lstrip("v")
            info["release_notes"] = str(data.get("body") or "")
            info["published_at"] = str(data.get("published_at") or "")[:10]
            info["releases_url"] = str(data.get("html_url") or GITHUB_RELEASES_URL)
            for asset in data.get("assets") or []:
                aname = str(asset.get("name") or "")
                aurl = str(asset.get("browser_download_url") or "")
                if aname.endswith(".zip") and aname.startswith("agy-tokensaver-"):
                    info["asset_name"] = aname
                    info["asset_url"] = aurl
                elif aname == "SHA256SUMS.txt":
                    info["checksum_url"] = aurl
    except urllib.error.HTTPError as e:
        if e.code == 404:
            info["error"] = "No published GitHub release found yet."
        else:
            info["error"] = f"GitHub API HTTP {e.code}"
    except Exception as e:
        info["error"] = f"Could not reach GitHub ({e})"

    _update_cache = (now, info)
    return get_update_snapshot()


def verify_gh_attestation(zip_path: Path) -> tuple[bool, str]:
    """Optional provenance verification via `gh attestation verify` when gh CLI is available.
    Returns (ok, status_note). Never fails when gh is absent or unauthenticated; rejects explicit
    verification mismatches on attested artifacts."""
    gh_bin = shutil.which("gh") or shutil.which("gh.exe")
    if not gh_bin:
        return True, "skipped (gh not installed)"
    try:
        p = subprocess.run(
            [gh_bin, "attestation", "verify", str(zip_path), "--repo", GITHUB_REPO],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
        )
        if p.returncode == 0:
            return True, "verified"
        err = (p.stderr or p.stdout or "").lower()
        if any(k in err for k in ("no attestations found", "not logged into", "authentication", "network", "could not resolve")):
            return True, "skipped (unavailable)"
        return False, f"attestation verify failed: {(p.stderr or p.stdout).strip()[:160]}"
    except Exception:
        return True, "skipped (error)"


def apply_update() -> dict[str, Any]:
    """Download latest tagged release ZIP, verify SHA-256 checksum, and update files while preserving plugin_data."""
    info = check_github_updates(force=True)
    if info.get("error"):
        return {"ok": False, "error": info["error"], "updates": info}
    if not info.get("tag_name") or not info.get("asset_url") or not info.get("checksum_url"):
        return {
            "ok": False,
            "error": "Update refused: latest tagged release is missing a release ZIP or SHA256SUMS.txt (unverified main branch updates are disabled).",
            "updates": info,
        }

    installed_before = info["installed_version"]
    asset_name = info["asset_name"]
    ua = {"User-Agent": f"agy-tokensaver/{installed_before}"}

    try:
        with urllib.request.urlopen(urllib.request.Request(info["checksum_url"], headers=ua), timeout=12) as cr:
            sums_text = cr.read().decode("utf-8", errors="replace")
        expected_sha = parse_sha256sums(sums_text, asset_name)
        if not expected_sha:
            return {"ok": False, "error": f"SHA256SUMS.txt does not contain a checksum for {asset_name}.", "updates": info}

        with urllib.request.urlopen(urllib.request.Request(info["asset_url"], headers=ua), timeout=30) as ar:
            zip_bytes = ar.read()

        if not verify_sha256_bytes(zip_bytes, expected_sha):
            return {"ok": False, "error": f"SHA-256 checksum mismatch for {asset_name}; update aborted.", "updates": info}

        with tempfile.TemporaryDirectory(prefix="agy-ts-upd-") as td:
            zip_path = Path(td) / asset_name
            zip_path.write_bytes(zip_bytes)

            att_ok, att_note = verify_gh_attestation(zip_path)
            if not att_ok:
                return {"ok": False, "error": f"Provenance verification failed for {asset_name}: {att_note}", "updates": info}

            extract_dir = Path(td) / "src"
            extract_dir.mkdir()
            with zipfile.ZipFile(zip_path, "r") as zf:
                for zi in zf.infolist():
                    p = Path(zi.filename)
                    if p.is_absolute() or ".." in p.parts:
                        continue
                    zf.extract(zi, extract_dir)

            roots = [p for p in extract_dir.iterdir() if p.is_dir()]
            src_root = roots[0] if len(roots) == 1 else extract_dir
            if not (src_root / "plugin.json").is_file():
                return {"ok": False, "error": "Invalid release archive (missing plugin.json).", "updates": info}

            # Preserve user-owned rules/AGENTS.local.md across updates
            local_rules_path = PLUGIN_ROOT / "rules" / "AGENTS.local.md"
            saved_local_rules = local_rules_path.read_bytes() if local_rules_path.is_file() else None

            # Copy updated plugin files into PLUGIN_ROOT while NEVER touching .git or plugin_data
            preserve = {".git", ".data", "__pycache__"}
            for item in src_root.iterdir():
                if item.name in preserve:
                    continue
                dest = PLUGIN_ROOT / item.name
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            if saved_local_rules is not None:
                local_rules_path.parent.mkdir(parents=True, exist_ok=True)
                local_rules_path.write_bytes(saved_local_rules)

            # On native Windows, pin real Python executable to avoid Microsoft Store python3 stub
            if os.name == "nt":
                mcp_cfg = PLUGIN_ROOT / "mcp_config.json"
                if mcp_cfg.is_file():
                    try:
                        cfg_data = json.loads(mcp_cfg.read_text(encoding="utf-8"))
                        srv_cfg = cfg_data.get("mcpServers", {}).get("token_saver_ui")
                        if isinstance(srv_cfg, dict):
                            srv_cfg["command"] = sys.executable
                            mcp_cfg.write_text(json.dumps(cfg_data, indent=2) + "\n", encoding="utf-8")
                    except Exception:
                        pass
                hj = PLUGIN_ROOT / "hooks.json"
                if hj.is_file():
                    try:
                        hdata = json.loads(hj.read_text(encoding="utf-8"))
                        quoted_py = f'"{sys.executable}"'
                        for grp in hdata.values():
                            if not isinstance(grp, dict):
                                continue
                            for entries in grp.values():
                                if not isinstance(entries, list):
                                    continue
                                for ent in entries:
                                    hlist = ent.get("hooks") if isinstance(ent.get("hooks"), list) else [ent]
                                    for hk in hlist:
                                        cmd = str(hk.get("command") or "")
                                        if cmd.startswith("python3 "):
                                            hk["command"] = quoted_py + cmd[7:]
                        hj.write_text(json.dumps(hdata, indent=2) + "\n", encoding="utf-8")
                    except Exception:
                        pass
            else:
                for pat in ("scripts/*.py", "mcp_app/*.py", "tests/*.sh", "install.py", "install.sh", "Install-TokenSaver.command"):
                    for fp in PLUGIN_ROOT.glob(pat):
                        try:
                            fp.chmod(fp.stat().st_mode | 0o111)
                        except OSError:
                            pass

        # Best-effort live MCP server refresh inside Antigravity
        ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
        csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
        if ls_addr and csrf:
            try:
                rreq = urllib.request.Request(
                    f"http://{ls_addr}/exa.language_server_pb.LanguageServerService/RefreshMcpServers",
                    data=json.dumps({"serverName": "token-saver_token_saver_ui"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "x-codeium-csrf-token": csrf},
                    method="POST",
                )
                urllib.request.urlopen(rreq, timeout=3).read()
            except Exception:
                pass

        new_info = check_github_updates(force=True)
        return {
            "ok": True,
            "from_version": installed_before,
            "to_version": new_info["installed_version"],
            "sha256": expected_sha[:16],
            "stats_preserved": True,
            "updates": new_info,
        }
    except Exception as e:
        return {"ok": False, "error": f"Update failed: {e}", "updates": info}


