#!/usr/bin/env python3
"""Pull every file under /flash/ off the connected Cardputer into a local
timestamped backup directory.

Usage:
    python3 buddy/scripts/backup_flash.py --port /dev/ttyACM0 [--out DIR]

Reads the device's own recursive file listing over the REPL, then pulls
each file's bytes back as base64 (same paste-mode REPL channel
install_apps.py/push_*_mpy.py use in the other direction). Binary files
(*.mpy) round-trip fine since everything travels as base64 text.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / ".claude" / "skills" / "m5-onboard" / "scripts"))

READ_CHUNK = 2048  # bytes per on-device read, base64'd for transport


def _to_str(out) -> str:
    return out if isinstance(out, str) else out.decode("utf-8", "replace")


def list_files(s) -> list[tuple[str, int]]:
    import mpy_repl as r  # type: ignore

    script = (
        "import os, ujson\n"
        "def _walk(p):\n"
        "    out = []\n"
        "    for name in os.listdir(p):\n"
        "        full = p + '/' + name\n"
        "        try:\n"
        "            entries = os.listdir(full)\n"
        "        except OSError:\n"
        "            out.append((full, os.stat(full)[6]))\n"
        "        else:\n"
        "            out.extend(_walk(full))\n"
        "    return out\n"
        "print('FILES_JSON', ujson.dumps(_walk('/flash')))\n"
    )
    out = _to_str(r.paste_exec(s, script, settle=2))
    for line in out.splitlines():
        if line.startswith("FILES_JSON"):
            import json

            return [tuple(x) for x in json.loads(line[len("FILES_JSON "):])]
    sys.exit("could not list device files:\n" + out)


def read_file(s, path: str, size: int) -> bytes:
    import mpy_repl as r  # type: ignore

    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        script = (
            "import ubinascii\n"
            f'f = open("{path}", "rb")\n'
            f"f.seek({offset})\n"
            f"d = f.read({READ_CHUNK})\n"
            "f.close()\n"
            'print("B64_START")\n'
            "print(ubinascii.b2a_base64(d).decode().strip())\n"
            'print("B64_END")\n'
        )
        out = _to_str(r.paste_exec(s, script, settle=1))
        lines = out.splitlines()
        try:
            start = lines.index("B64_START")
            end = lines.index("B64_END")
        except ValueError:
            sys.exit(f"read failed for {path} at offset {offset}:\n{out}")
        b64 = "".join(lines[start + 1 : end])
        data = base64.b64decode(b64)
        chunks.append(data)
        offset += len(data)
        if not data:
            break
    return b"".join(chunks)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", required=True)
    ap.add_argument(
        "--out",
        default=None,
        help="backup output dir (default: ~/cardputer-backups/<timestamp>)",
    )
    args = ap.parse_args()

    import mpy_repl as r  # type: ignore

    out_dir = Path(args.out) if args.out else (
        Path.home()
        / "cardputer-backups"
        / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    s = r.open_port(args.port)
    try:
        r.interrupt_to_repl(s)
        if s.in_waiting:
            s.read(s.in_waiting)

        print("listing device files...")
        files = list_files(s)
        print(f"found {len(files)} files under /flash")

        manifest = []
        for i, (path, size) in enumerate(files, 1):
            rel = path[len("/flash/"):] if path.startswith("/flash/") else path.lstrip("/")
            local_path = out_dir / rel
            local_path.parent.mkdir(parents=True, exist_ok=True)
            data = read_file(s, path, size)
            local_path.write_bytes(data)
            ok = len(data) == size
            status = "ok" if ok else f"MISMATCH got={len(data)} expected={size}"
            print(f"  [{i}/{len(files)}] {rel} ({size}B) {status}")
            manifest.append(f"{rel}\t{size}\t{status}")

        (out_dir / "MANIFEST.txt").write_text(
            f"Cardputer flash backup — {datetime.now(timezone.utc).isoformat()}\n"
            f"Port: {args.port}\n\n" + "\n".join(manifest) + "\n"
        )
        print(f"\nbackup complete: {out_dir}")

        s.write(b"import machine; machine.reset()\r\n")
        s.flush()
        print("device rebooted into launcher")
    finally:
        s.close()


if __name__ == "__main__":
    main()
