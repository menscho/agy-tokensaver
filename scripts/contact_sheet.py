#!/usr/bin/env python3
"""
Packs multiple .jpg/.png/.webp frames into a single labeled contact-sheet PNG
using ffmpeg (cuts 16 frames x 1,500 tok = 24K -> 1 image x 1,800 tok = -92.5%!).
Usage:
  python3 contact_sheet.py <dir_or_file1> [file2 ...] [-o /tmp/sheet.png]
"""

import getpass
import glob
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def collect_images(args: list[str]) -> tuple[list[Path], Path]:
    uid = str(os.getuid()) if hasattr(os, "getuid") else re.sub(r"[^A-Za-z0-9_-]", "_", getpass.getuser() or "user")
    out_path = Path(tempfile.gettempdir()) / f"agy-tokensaver-{uid}" / "contact-sheet.png"
    imgs: list[Path] = []
    i = 0
    while i < len(args):
        if args[i] == "-o" and i + 1 < len(args):
            out_path = Path(args[i + 1])
            i += 2
            continue
        p = Path(args[i])
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.is_file() and f.suffix.lower() in IMG_EXTS:
                    imgs.append(f)
        elif p.is_file():
            imgs.append(p)
        else:
            for m in sorted(glob.glob(args[i])):
                mp = Path(m)
                if mp.is_file() and mp.suffix.lower() in IMG_EXTS:
                    imgs.append(mp)
        i += 1
    return imgs[:16], out_path


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: contact_sheet.py <dir|files...> [-o out.png]")
        return 1
    imgs, out_path = collect_images(sys.argv[1:])
    if not imgs:
        print("No images found.")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(imgs)
    cols = min(4, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)

    # Build ffmpeg filter_complex: scale each input to 400x260 padded, then xstack/tile
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for img in imgs:
        cmd.extend(["-i", str(img)])

    filters = []
    for idx in range(n):
        filters.append(f"[{idx}:v]scale=400:260:force_original_aspect_ratio=decrease,pad=400:260:(ow-iw)/2:(oh-ih)/2:color=black[v{idx}]")
    concat_in = "".join(f"[v{idx}]" for idx in range(n))
    filters.append(f"{concat_in}concat=n={n}:v=1:a=0,tile={cols}x{rows}:padding=6:margin=6:color=0x222222[out]")

    cmd.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-frames:v", "1", str(out_path)])
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        print(f"ffmpeg failed: {res.stderr.strip()}", file=sys.stderr)
        return res.returncode

    print(f"Contact sheet ({n} frames, {cols}x{rows} grid): {out_path}")
    for idx, img in enumerate(imgs, 1):
        r = (idx - 1) // cols + 1
        c = (idx - 1) % cols + 1
        print(f"  [r{r}c{c}] {img.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
