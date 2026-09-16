#!/usr/bin/env python3
"""
version.py – the one place where the version number lives.

When releasing: bump here, commit, then set the matching tag
(`git tag v4.0.0 && git push --tags`). The build workflow checks that the
tag and this number match – otherwise the app would report a different
version than the download carries, and the update check would forever
recommend updating.

Format: MAJOR.MINOR.PATCH, without a leading "v" (only the tag carries that).
"""

VERSION = "11.2.0"

# The build: the short commit hash the bundle was built from. The release
# workflow stamps it in (packaging/stamp_build.py) before PyInstaller runs;
# from source it is read from git, and where neither applies it stays
# empty. Settings › App shows it next to the version, the bug report
# carries it – "11.0.0" alone does not say which commit someone runs.
BUILD = ""

# For the update check: this is where the releases live.
REPO = "n-schilling/munimentum"
RELEASES_URL = f"https://github.com/{REPO}/releases"


def build():
    """The build id: the stamped one, else git's short hash when this file
    lies in a checkout, else ''. Asked once; the answer never changes."""
    global _BUILD
    if _BUILD is None:
        _BUILD = BUILD or _aus_git()
    return _BUILD


_BUILD = None


def _aus_git():
    import subprocess
    from pathlib import Path
    wurzel = Path(__file__).resolve().parent
    if not (wurzel / ".git").exists():
        return ""
    try:
        r = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], cwd=wurzel,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""
