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

VERSION = "7.0.1"

# For the update check: this is where the releases live.
REPO = "n-schilling/munimentum"
RELEASES_URL = f"https://github.com/{REPO}/releases"
