#!/usr/bin/env python3
"""
stamp_build.py – write the build id into version.py before the bundle is
built: `python packaging/stamp_build.py <commit sha>`. The workflow calls
it with the commit the tag points at; the app then shows "11.0.0 · build
e727567" under Settings › App instead of the version alone. A checkout
started from source needs no stamp – version.build() asks git.
"""

import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

VERSION_PY = Path(__file__).resolve().parent.parent / "version.py"


def stempeln(pfad, sha):
    """Replace the empty BUILD with the short hash; returns the id written."""
    kurz = re.sub(r"[^0-9a-f]", "", str(sha).lower())[:7]
    if not kurz:
        raise SystemExit(f"no commit hash in {sha!r}")
    text = pfad.read_text(encoding="utf-8")
    neu, n = re.subn(r'^BUILD = ""$', f'BUILD = "{kurz}"', text, count=1, flags=re.M)
    if n != 1:
        raise SystemExit(f"{pfad}: no empty BUILD line to stamp")
    pfad.write_text(neu, encoding="utf-8")
    return kurz


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: stamp_build.py <commit sha>")
    print(f"build {stempeln(VERSION_PY, sys.argv[1])}")


if __name__ == "__main__":
    main()
