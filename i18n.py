#!/usr/bin/env python3
"""
i18n.py – load the interface language files and pick the right one.

Every language is a JSON file in lang/ (de.json, en.json, fr.json). Another
language is added by dropping in a file – code and display name live under
"_meta" inside it, nothing else needs registering.

Selection on page load:

    setting "language" in app_config.json, otherwise the browser's
    Accept-Language header, otherwise German.

The language is determined server-side and delivered with the page – so the
wrong language never flashes up, and the interface needs no extra round trip
before it can show anything.

Only the app's interface and its own messages are translated. Whatever the
export scripts write to their console goes into the log unchanged – they are
standalone tools with their own documentation. Exported content is never
touched anyway.
"""

import json
import re
from pathlib import Path

FALLBACK = "de"          # source language: every key is guaranteed here
LANG_DIRNAME = "lang"

_cache = {}


def lang_dir(base=None):
    return Path(base or Path(__file__).resolve().parent) / LANG_DIRNAME


def available(base=None):
    """[{"code": "de", "name": "Deutsch"}, …] – sorted, fallback first."""
    out = []
    for p in sorted(lang_dir(base).glob("*.json")):
        daten = _read(p)
        meta = daten.get("_meta") or {}
        code = (meta.get("code") or p.stem).lower()
        out.append({"code": code, "name": meta.get("name") or code.upper()})
    out.sort(key=lambda x: (x["code"] != FALLBACK, x["name"].lower()))
    return out


def _read(path):
    key = str(path)
    if key in _cache:
        return _cache[key]
    try:
        daten = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        daten = {}
    if not isinstance(daten, dict):
        daten = {}
    _cache[key] = daten
    return daten


def reset():
    """Clear the cache (tests, changed language files)."""
    _cache.clear()


def strings(code, base=None):
    """All texts of a language, missing ones filled from the source language.

    Without this fill-in, a still incomplete translation would stay blank in
    places – better a German sentence than none at all.
    """
    d = lang_dir(base)
    basis = dict(_read(d / f"{FALLBACK}.json"))
    if code and code != FALLBACK:
        basis.update({k: v for k, v in _read(d / f"{code}.json").items() if v})
    basis.pop("_meta", None)
    return basis


def parse_accept_language(header):
    """Language codes from Accept-Language, sorted by weight.

    "de-DE,de;q=0.9,en;q=0.8" -> ["de-de", "de", "en"]
    """
    eintraege = []
    for i, teil in enumerate((header or "").split(",")):
        teil = teil.strip()
        if not teil:
            continue
        stueck = teil.split(";")
        code = stueck[0].strip().lower()
        if not code or code == "*":
            continue
        q = 1.0
        for teilstueck in stueck[1:]:
            m = re.match(r"\s*q\s*=\s*(\S+)", teilstueck, re.I)
            if m:
                # An unreadable q counts as 0, not 1: a broken value must
                # not push a language to the top.
                try:
                    q = float(m.group(1))
                except ValueError:
                    q = 0.0
        eintraege.append((-q, i, code))
    return [c for _, _, c in sorted(eintraege)]


def negotiate(configured=None, accept_language=None, base=None):
    """Which language applies? Setting before browser before source language.

    "auto" (or nothing) means: ask the browser. A regional code like de-CH
    counts for de – otherwise someone with a Swiss setting would land on
    German as a last resort instead of as a match.
    """
    codes = {e["code"] for e in available(base)}
    gewuenscht = (configured or "auto").strip().lower()
    if gewuenscht != "auto" and gewuenscht in codes:
        return gewuenscht
    for code in parse_accept_language(accept_language):
        if code in codes:
            return code
        if code.split("-", 1)[0] in codes:
            return code.split("-", 1)[0]
    return FALLBACK
