#!/usr/bin/env python3
"""
version.py – die eine Stelle, an der die Versionsnummer steht.

Beim Veröffentlichen: hier anheben, committen, dann den passenden Tag setzen
(`git tag v4.0.0 && git push --tags`). Der Build-Workflow prüft, dass Tag und
diese Zahl zusammenpassen – sonst meldete die App eine andere Version, als der
Download trägt, und die Aktualisierungsprüfung riete dauerhaft zum Update.

Format: MAJOR.MINOR.PATCH, ohne führendes "v" (das trägt nur der Tag).
"""

VERSION = "6.3.0"

# Für die Aktualisierungsprüfung: hier liegen die Releases.
REPO = "n-schilling/munimentum"
RELEASES_URL = f"https://github.com/{REPO}/releases"
