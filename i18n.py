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
        eigen = {k: v for k, v in _read(d / f"{code}.json").items() if v}
        basis.update(eigen)
        # A singular is never filled in beside the language's own plural:
        # "1 mails" beats a German "1 Mail" in an English line.
        for k in [k for k in basis if k.endswith(".one")]:
            if k not in eigen and k[:-4] in eigen:
                del basis[k]
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


class _Platzhalter(dict):
    """format_map's map: a placeholder nobody filled stays visible instead
    of raising."""

    def __missing__(self, key):
        return "{" + key + "}"


def fuelle(vorlage, werte=None):
    """A text with its {placeholders} filled – the server-side counterpart
    of the page's t(): plain, no plural forms, no nesting. Everything that
    renders a text outside the browser goes through here, so one spelling
    of the rule is enough."""
    try:
        return vorlage.format_map(_Platzhalter(werte or {}))
    except (ValueError, IndexError, AttributeError):
        return vorlage


# Placeholders whose number is never a count – a code, a port, an id.
# Every other integer in a log line is a count and grouped like one. A
# position is not: an emitter that logs one passes it as a string, and the
# keys that carried positions as numbers before (stored runs) are named
# with them in RAW_BY_KEY. The page's LOG_RAW_NUMBERS and LOG_RAW_BY_KEY
# hold the same (a test compares them).
RAW_NUMBERS = frozenset({"code", "status", "port", "id", "version", "pointer"})
RAW_BY_KEY = {
    **{k: frozenset({"i", "total"}) for k in (
        "run.conv.new", "run.conv.updated", "run.conv.changed", "run.conv.changed_parts",
        "run.conv.same", "run.conv.empty", "run.conv.failed")},
    "run.evidence.stamped": frozenset({"n"}),      # a line of the chain
    "run.evidence.broken": frozenset({"n"}),
}
# How a count is grouped per language – what the page's toLocaleString does.
_GROUPING = {"de": ".", "fr": "\u202f"}


def _own(code, key, base):
    return _read(lang_dir(base) / f"{code}.json").get(key) if code else None


def _text(code, key, base):
    """A text of the language, or of the source language when it lacks it."""
    return _own(code, key, base) or _read(lang_dir(base) / f"{FALLBACK}.json").get(key)


def _count(code, n):
    return f"{n:,}".replace(",", _GROUPING.get(code, ","))


def _elements(code, base, value):
    """A run's selection, as the page's runElements() names it."""
    import steps
    alle = _text(code, "ana.runs.all", base) or "all"
    parts = []
    for key, name, every in (("outlook", "Outlook", 3), ("teams", "Teams", 4)):
        cats = value.get(key) or []
        if cats:
            names = [alle] if len(cats) >= every else [
                _text(code, f"export.cat.{c}", base) or c for c in cats]
            parts.append(f"{name} ({', '.join(names)})")
    for key, meta in steps.ui_metadaten().items():
        if key not in ("outlook", "teams") and value.get(key):
            q = meta.get("quelle") or key
            parts.append(f"{(_text(code, q, base) or q) if '.' in q else q} ({alle})")
    return ", ".join(parts) or "–"


def _render_value(code, base, name, value, one, raw):
    """One placeholder's value as the page's mtext() renders it: a nested
    message translated (the `unit` in its singular under an `n` of 1), a
    list of messages joined by " · ", a step's result and a run's selection
    as their lines, a count with the language's grouping."""
    if isinstance(value, list):
        parts = (_render_value(code, base, name, v, False, raw) for v in value)
        return " · ".join(str(p) for p in parts if p not in (None, ""))
    if name == "ergebnis" and isinstance(value, dict) and "new" in value:
        bits = [f"{_text(code, 'ana.runs.' + k, base) or k} {_count(code, value[k])}"
                for k in ("new", "unchanged", "excluded", "errors")
                if isinstance(value.get(k), int)]
        bits += [f"{k} {_count(code, v)}" for k, v in (value.get("extra") or {}).items()
                 if isinstance(v, int) and v]
        return " · ".join(bits) or "–"
    if name == "elements" and isinstance(value, dict) and not value.get("k"):
        return _elements(code, base, value)
    if isinstance(value, dict) and value.get("k"):
        return log_line(code, value["k"], base, value.get("v"),
                        one=one and name == "unit") or value["k"]
    if isinstance(value, int) and not isinstance(value, bool) and name not in raw:
        return _count(code, value)
    return value


def log_line(code, key, base=None, values=None, one=False):
    """A log line as the page's mtext() renders it – for the MCP server,
    which hands a run's log to Claude: a `.one` variant when `n` is 1 (only
    where the language has one of its own), nested messages, lists,
    counts. Archive text and refusals keep the plain satz(): an exported
    page must not change with a log rule."""
    values = values if isinstance(values, dict) else {}
    n1 = values.get("n") == 1 and not isinstance(values.get("n"), bool)
    text = None
    if n1 or one:
        text = _own(code, key + ".one", base)
        if not text and not _own(code, key, base):
            text = _text(code, key + ".one", base)
    text = text or _text(code, key, base)
    if not text:
        return None
    raw = RAW_NUMBERS | RAW_BY_KEY.get(key, frozenset())
    return fuelle(text, {k: _render_value(code, base, k, v, n1, raw) for k, v in values.items()})


def satz(code, key, base=None, werte=None):
    """One text of a language, filled – without copying the whole table.

    `strings()` builds a fresh dict of every key; a single lookup does not
    need that, and error answers do exactly one per refusal. Returns None
    when neither the language nor the source language knows the key.
    """
    text = _text(code, key, base)
    return None if not text else fuelle(text, werte)
