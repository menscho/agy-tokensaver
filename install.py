#!/usr/bin/env python3
"""
Token Saver (agy-tokensaver) — Instant Installer & Stats-Preserving Updater.

Works on Linux, macOS, WSL, and Windows using pure Python 3.9+ stdlib.
- Double-click (or launch via Install-TokenSaver.bat / .command) for a clean native installer window.
- Or run in terminal: `python3 install.py` / `--yes` / `--remote` / `--uninstall`.
- Automatically detects existing installations and updates them in place while
  strictly preserving all statistics, settings, and rollover history in
  `~/.gemini/antigravity/plugin_data/token-saver/`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

REPO = "menscho/agy-tokensaver"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"

PLUGIN_DIR = Path.home() / ".gemini" / "config" / "plugins" / "token-saver"
DATA_DIR = Path.home() / ".gemini" / "antigravity" / "plugin_data" / "token-saver"

PRESERVE_NAMES = {".git", ".data", "__pycache__"}
EXEC_GLOBS = ("scripts/*.py", "mcp_app/*.py", "tests/*.sh", "install.py", "install.sh", "Install-TokenSaver.command")


def read_version(root: Path) -> str | None:
    pj = root / "plugin.json"
    if not pj.is_file():
        return None
    try:
        return str(json.loads(pj.read_text(encoding="utf-8")).get("version") or "1.0.0").lstrip("v")
    except Exception:
        return None


def local_bundle_root() -> Path | None:
    here = Path(__file__).resolve().parent
    if (here / "plugin.json").is_file() and (here / "mcp_app" / "server.py").is_file():
        return here
    return None


def _wire_hooks_python(plugin_dir: Path, python_exe: str | None = None) -> bool:
    """Point installed hooks.json at an explicit Python executable when python3 isn't directly usable."""
    hj = plugin_dir / "hooks.json"
    if not hj.is_file():
        return False
    exe = python_exe or sys.executable
    quoted = f'"{exe}"' if (" " in exe or os.name == "nt") else exe
    data = json.loads(hj.read_text(encoding="utf-8"))
    changed = False
    for group in data.values():
        if not isinstance(group, dict):
            continue
        for entries in group.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                hlist = entry.get("hooks") if isinstance(entry.get("hooks"), list) else [entry]
                for hook in hlist:
                    cmd = str(hook.get("command") or "")
                    if cmd.startswith("python3 "):
                        hook["command"] = quoted + cmd[7:]
                        changed = True
    if changed:
        hj.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return changed


def fetch_remote_bundle(tmp_dir: Path, log: Callable[[str], None]) -> Path:
    ua = {"Accept": "application/vnd.github+json", "User-Agent": "agy-tokensaver-installer/1.0"}
    req = urllib.request.Request(RELEASES_API, headers=ua)
    with urllib.request.urlopen(req, timeout=10) as r:
        rel = json.loads(r.read().decode("utf-8", errors="replace"))

    tag = str(rel.get("tag_name") or "").strip()
    if not tag or not re.match(r"^v?\d+\.\d+\.\d+", tag):
        raise RuntimeError("No tagged semver release found on GitHub (unverified branch downloads are disabled).")

    zip_name = zip_url = sum_url = ""
    for a in rel.get("assets") or []:
        an = str(a.get("name") or "")
        au = str(a.get("browser_download_url") or "")
        if an.endswith(".zip") and an.startswith("agy-tokensaver-"):
            zip_name, zip_url = an, au
        elif an == "SHA256SUMS.txt":
            sum_url = au
    if not zip_url or not sum_url:
        raise RuntimeError(f"Release {tag} is missing a release ZIP or SHA256SUMS.txt.")

    log(f"Downloading tagged release {tag} and verifying SHA-256…")
    with urllib.request.urlopen(urllib.request.Request(sum_url, headers=ua), timeout=15) as sr:
        sums_txt = sr.read().decode("utf-8", errors="replace")
    expected = None
    for line in sums_txt.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == zip_name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            expected = parts[0].lower()
            break
    if not expected:
        raise RuntimeError(f"SHA256SUMS.txt does not list a valid SHA-256 for {zip_name}.")

    with urllib.request.urlopen(urllib.request.Request(zip_url, headers=ua), timeout=30) as zr:
        blob = zr.read()
    actual = hashlib.sha256(blob).hexdigest().lower()
    if actual != expected:
        raise RuntimeError(f"SHA-256 mismatch for {zip_name} (expected {expected[:12]}, got {actual[:12]}).")
    log(f"SHA-256 verified ({actual[:16]}…).")

    zpath = tmp_dir / zip_name
    zpath.write_bytes(blob)
    gh_bin = shutil.which("gh") or shutil.which("gh.exe")
    if gh_bin:
        try:
            att = subprocess.run(
                [gh_bin, "attestation", "verify", str(zpath), "--repo", REPO],
                capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
            )
            if att.returncode == 0:
                log("GitHub build provenance attestation verified.")
            else:
                err = (att.stderr or att.stdout or "").lower()
                if not any(k in err for k in ("no attestations found", "not logged into", "authentication", "network", "could not resolve")):
                    raise RuntimeError(f"GitHub attestation verification failed: {(att.stderr or att.stdout).strip()[:160]}")
        except RuntimeError:
            raise
        except Exception:
            pass
    out = tmp_dir / "extracted"
    out.mkdir()
    with zipfile.ZipFile(zpath, "r") as zf:
        for zi in zf.infolist():
            p = Path(zi.filename)
            if p.is_absolute() or ".." in p.parts:
                continue
            zf.extract(zi, out)

    roots = [p for p in out.iterdir() if p.is_dir()]
    src = roots[0] if len(roots) == 1 else out
    if not (src / "plugin.json").is_file():
        raise RuntimeError("Downloaded archive is missing plugin.json.")
    return src


def install_or_update(remote: bool = False, log: Callable[[str], None] = print) -> tuple[bool, str]:
    old_ver = read_version(PLUGIN_DIR)
    is_update = old_ver is not None

    with tempfile.TemporaryDirectory(prefix="agy-ts-inst-") as td:
        src = None if remote else local_bundle_root()
        if src is None:
            src = fetch_remote_bundle(Path(td), log)
        elif src.resolve() == PLUGIN_DIR.resolve():
            log("Running from installed plugin directory — checking GitHub for latest version…")
            src = fetch_remote_bundle(Path(td), log)

        new_ver = read_version(src) or "1.0.0"
        log(f"{'Updating' if is_update else 'Installing'} Token Saver: "
            f"{f'v{old_ver} -> v{new_ver}' if is_update else f'v{new_ver}'}")

        PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Copy plugin files without touching user statistics in DATA_DIR
        for item in src.iterdir():
            if item.name in PRESERVE_NAMES:
                continue
            dest = PLUGIN_DIR / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

        if os.name == "nt":
            # Avoid the Microsoft Store `python3` placeholder stub on native Windows
            mcp_cfg = PLUGIN_DIR / "mcp_config.json"
            if mcp_cfg.is_file():
                try:
                    cfg_data = json.loads(mcp_cfg.read_text(encoding="utf-8"))
                    srv_cfg = cfg_data.get("mcpServers", {}).get("token_saver_ui")
                    if isinstance(srv_cfg, dict):
                        srv_cfg["command"] = sys.executable
                        mcp_cfg.write_text(json.dumps(cfg_data, indent=2) + "\n", encoding="utf-8")
                        log(f"Configured Windows Python executable: {sys.executable}")
                except Exception:
                    pass
        else:
            for pat in EXEC_GLOBS:
                for fp in PLUGIN_DIR.glob(pat):
                    try:
                        fp.chmod(fp.stat().st_mode | 0o111)
                    except OSError:
                        pass

    # Verify MCP server responds cleanly
    log("Verifying Token Saver MCP App server…")
    srv_py = PLUGIN_DIR / "mcp_app" / "server.py"
    p = subprocess.Popen(
        [sys.executable, str(srv_py)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert p.stdin and p.stdout
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}) + "\n")
        p.stdin.flush()
        resp = json.loads(p.stdout.readline())
        if "result" not in resp:
            raise RuntimeError(f"Unexpected MCP initialize response: {resp}")
    finally:
        p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()

    # Best-effort live MCP refresh inside Antigravity
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
            log("Refreshed running Antigravity MCP server live.")
        except Exception:
            pass

    if os.name == "nt" or not shutil.which("python3"):
        if _wire_hooks_python(PLUGIN_DIR, sys.executable):
            log(f"Wired hooks.json to Python executable ({sys.executable}).")

    msg = (
        f"Token Saver v{new_ver} {'updated' if is_update else 'installed'} successfully!\n"
        f"Plugin path : {PLUGIN_DIR}\n"
        f"Stats path  : {DATA_DIR} (preserved)\n"
        f"Open 'Token Saver' in the Antigravity sidebar (reload window if not visible yet)."
    )
    log(msg)
    return True, msg


def run_doctor(log: Callable[[str], None] = print) -> int:
    """Check Python 3.9+, plugin paths, data dir, and hook registration with fix-it instructions."""
    import shlex

    ok_all = True

    def row(status: str, check: str, detail: str, fix: str = "") -> None:
        nonlocal ok_all
        if status == "FAIL":
            ok_all = False
        log(f"[{status:4s}] {check:18s} : {detail}")
        if fix:
            log(f"       Fix: {fix}")

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if (sys.version_info.major, sys.version_info.minor) >= (3, 9):
        row("OK", "Python 3.9+", f"{py_ver} ({sys.executable})")
    else:
        row("FAIL", "Python 3.9+", f"{py_ver} too old", "Install Python 3.9+ from https://www.python.org/downloads/")

    pj = PLUGIN_DIR / "plugin.json"
    srv = PLUGIN_DIR / "mcp_app" / "server.py"
    if pj.is_file() and srv.is_file():
        ver = read_version(PLUGIN_DIR) or "unknown"
        row("OK", "Plugin path", f"v{ver} at {PLUGIN_DIR}")
    else:
        row("FAIL", "Plugin path", f"missing in {PLUGIN_DIR}", "Run: python3 install.py --yes")

    data_ok = DATA_DIR.is_dir() or os.access(DATA_DIR.parent, os.W_OK)
    if data_ok:
        row("OK", "Data directory", str(DATA_DIR))
    else:
        row("WARN", "Data directory", f"parent not writable: {DATA_DIR}", f"Check write permissions on {DATA_DIR.parent}")

    hj = PLUGIN_DIR / "hooks.json"
    if not hj.is_file():
        row("FAIL", "Hook registration", f"missing {hj}", "Run: python3 install.py --yes")
    else:
        try:
            hdata = json.loads(hj.read_text(encoding="utf-8"))
            count = 0
            missing_scripts: list[str] = []
            missing_bins: list[str] = []
            for grp in hdata.values():
                if not isinstance(grp, dict):
                    continue
                for entries in grp.values():
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        hlist = entry.get("hooks") if isinstance(entry.get("hooks"), list) else [entry]
                        for h in hlist:
                            count += 1
                            parts = shlex.split(str(h.get("command") or ""), posix=(os.name != "nt"))
                            if parts:
                                exe = parts[0].strip('"')
                                if not shutil.which(exe) and not Path(exe).is_file():
                                    missing_bins.append(exe)
                            for arg in parts[1:]:
                                clean_arg = arg.strip('"')
                                if clean_arg.startswith("./") and not (PLUGIN_DIR / clean_arg[2:]).is_file():
                                    missing_scripts.append(clean_arg)
            if missing_scripts or missing_bins:
                detail = "; ".join(([f"missing scripts: {','.join(missing_scripts)}"] if missing_scripts else []) + ([f"unresolvable python: {','.join(sorted(set(missing_bins)))}"] if missing_bins else []))
                row("FAIL", "Hook registration", detail, "Run: python3 install.py --yes")
            else:
                row("OK", "Hook registration", f"{count} hook command(s) registered cleanly in {hj}")
        except Exception as exc:
            row("FAIL", "Hook registration", f"invalid JSON: {exc}", "Run: python3 install.py --yes")

    return 0 if ok_all else 1


def uninstall(remove_stats: bool = False, log: Callable[[str], None] = print) -> None:
    if PLUGIN_DIR.exists():
        shutil.rmtree(PLUGIN_DIR)
        log(f"Removed plugin: {PLUGIN_DIR}")
    else:
        log("Token Saver plugin is not installed.")
    if remove_stats and DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
        log(f"Removed stats directory: {DATA_DIR}")
    else:
        log(f"Statistics preserved at: {DATA_DIR}")


def launch_gui(remote: bool = False) -> bool:
    try:
        import tkinter as tk
        from tkinter import scrolledtext
    except Exception:
        return False

    try:
        root = tk.Tk()
    except Exception:
        return False

    old_ver = read_version(PLUGIN_DIR)
    src = local_bundle_root()
    src_ver = read_version(src) if src else "latest"
    action_label = (
        f"Update Token Saver (v{old_ver} -> v{src_ver})" if old_ver
        else f"Install Token Saver (v{src_ver})"
    )

    root.title("Token Saver Installer")
    root.geometry("540x360")
    root.configure(bg="#111214")
    root.resizable(False, False)

    tk.Label(root, text="Token Saver for Antigravity", font=("Segoe UI", 14, "bold"),
             bg="#111214", fg="#eceef2").pack(anchor="w", padx=22, pady=(18, 2))
    tk.Label(root, text="Zero-quality-loss quota optimizer · Statistics & archive preserved automatically",
             font=("Segoe UI", 9), bg="#111214", fg="#9ca3af").pack(anchor="w", padx=22, pady=(0, 12))

    out = scrolledtext.ScrolledText(root, height=11, bg="#181a1d", fg="#eceef2",
                                    insertbackground="#eceef2", relief="flat", font=("Consolas", 9))
    out.pack(fill="both", expand=True, padx=22, pady=(0, 12))

    def gui_log(msg: str) -> None:
        out.insert("end", msg + "\n")
        out.see("end")
        root.update_idletasks()

    gui_log(f"Target plugin directory : {PLUGIN_DIR}")
    gui_log(f"Preserved stats folder  : {DATA_DIR}")
    gui_log(f"Current installation    : {'v' + old_ver if old_ver else 'Not installed'}")
    gui_log("")

    btn_frame = tk.Frame(root, bg="#111214")
    btn_frame.pack(fill="x", padx=22, pady=(0, 16))

    def on_run() -> None:
        btn_run.config(state="disabled", text="Working…")
        try:
            install_or_update(remote=remote, log=gui_log)
            btn_run.config(text="Done", state="normal", command=root.destroy)
        except Exception as exc:
            gui_log(f"ERROR: {exc}")
            btn_run.config(text="Retry", state="normal")

    btn_run = tk.Button(btn_frame, text=action_label, command=on_run, bg="#eceef2", fg="#111214",
                        activebackground="#d1d5db", relief="flat", padx=14, pady=6, font=("Segoe UI", 9, "bold"))
    btn_run.pack(side="left")

    tk.Button(btn_frame, text="Close", command=root.destroy, bg="#202327", fg="#eceef2",
              relief="flat", padx=12, pady=6, font=("Segoe UI", 9)).pack(side="right")

    root.mainloop()
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Install or update Token Saver for Antigravity.")
    ap.add_argument("--yes", "-y", action="store_true", help="Non-interactive CLI install/update")
    ap.add_argument("--cli", action="store_true", help="Force terminal mode (no GUI window)")
    ap.add_argument("--remote", action="store_true", help="Always download latest release from GitHub")
    ap.add_argument("--doctor", action="store_true", help="Diagnose Python, plugin paths, data dir, and hook registration")
    ap.add_argument("--uninstall", action="store_true", help="Uninstall Token Saver plugin")
    ap.add_argument("--purge-stats", action="store_true", help="Also delete plugin_data stats when uninstalling")
    args = ap.parse_args()

    if args.doctor:
        return run_doctor()
    if args.uninstall:
        uninstall(remove_stats=args.purge_stats)
        return 0

    use_gui = (not args.yes and not args.cli and not sys.stdin.isatty() and (
        platform.system() in ("Windows", "Darwin") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    ))
    if use_gui and launch_gui(remote=args.remote):
        return 0

    try:
        install_or_update(remote=args.remote)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
