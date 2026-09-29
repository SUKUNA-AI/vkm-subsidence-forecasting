"""Shared-GPU lock of the workstation (one model server at a time).

    python gpu_lock.py acquire --lock <path> --owner QV --purpose "..."   (exit 3 if the lock exists)
    python gpu_lock.py release --lock <path> --owner QV                   (refuses to delete another owner's lock)
    python gpu_lock.py show --lock <path>

The lock is created atomically (O_CREAT | O_EXCL); its content is JSON {owner, purpose, started}.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["acquire", "release", "show"])
    ap.add_argument("--lock", required=True)
    ap.add_argument("--owner", default="")
    ap.add_argument("--purpose", default="")
    a = ap.parse_args()
    if a.action == "acquire":
        try:
            fd = os.open(a.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            with open(a.lock, encoding="utf-8") as f:
                print("LOCK HELD:", f.read().strip())
            sys.exit(3)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"owner": a.owner, "purpose": a.purpose,
                       "started": datetime.now().astimezone().isoformat(timespec="seconds")}, f, ensure_ascii=False)
        print("acquired")
    elif a.action == "release":
        if not os.path.exists(a.lock):
            print("no lock")
            return
        with open(a.lock, encoding="utf-8") as f:
            held = json.load(f)
        if held.get("owner") != a.owner:
            print("LOCK OWNED BY", held.get("owner"), "- not released")
            sys.exit(4)
        os.remove(a.lock)
        print("released")
    else:
        print(open(a.lock, encoding="utf-8").read() if os.path.exists(a.lock) else "no lock")


if __name__ == "__main__":
    main()
