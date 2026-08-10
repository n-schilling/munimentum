#!/usr/bin/env python3
"""
i18n.py – Sprachdateien der Oberfläche laden und die passende auswählen.

Jede Sprache ist eine JSON-Datei in lang/ (de.json, en.json, fr.json). Eine
weitere Sprache ergänzt man, indem man eine Datei dazulegt – Code und Anzeige-
name stehen unter "_meta" darin, es ist nichts weiter zu registrieren.

Auswahl beim Seitenaufruf:

    Einstellung "language" in app_config.json, sonst der Accept-Language-Header
    des Browsers, sonst Deutsch.

Die Sprache wird serverseitig bestimmt und mit der Seite ausgeliefert – so
erscheint nie kurz die falsche Sprache, und die Oberfläche braucht keinen
zusätzlichen Abruf, bevor sie etwas anzeigen kann.

Übersetzt wird ausschließlich die Oberfläche der App samt ihrer eigenen
Meldungen. Was die Export-Skripte auf ihre Konsole schreiben, geht unverändert
ins Protokoll – es sind eigenständige Werkzeuge mit eigener Dokumentation.
Exportierte Inhalte werden ohnehin nie angefasst.
"""

import json
import re
from pathlib import Path

FALLBACK = "de"          # Quellsprache: hier ist garantiert jeder Schlüssel da
LANG_DIRNAME = "lang"

_cache = {}


def lang_dir(base=None):
    return Path(base or Path(__file__).resolve().parent) / LANG_DIRNAME


def available(base=None):
    """[{"code": "de", "name": "Deutsch"}, …] – sortiert, Fallback zuerst."""
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
    """Puffer leeren (Tests, geänderte Sprachdateien)."""
    _cache.clear()


def strings(code, base=None):
    """Alle Texte einer Sprache, fehlende aus der Quellsprache ergänzt.

    Ohne diese Ergänzung bliebe eine noch unvollständige Übersetzung an einzelnen
    Stellen leer – lieber ein deutscher Satz als gar keiner.
    """
    d = lang_dir(base)
    basis = dict(_read(d / f"{FALLBACK}.json"))
    if code and code != FALLBACK:
        basis.update({k: v for k, v in _read(d / f"{code}.json").items() if v})
    basis.pop("_meta", None)
    return basis


def parse_accept_language(header):
    """Sprachcodes aus Accept-Language, nach Gewicht sortiert.

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
                # Unlesbares q zählt als 0, nicht als 1: ein kaputter Wert soll
                # eine Sprache nicht an die Spitze setzen.
                try:
                    q = float(m.group(1))
                except ValueError:
                    q = 0.0
        eintraege.append((-q, i, code))
    return [c for _, _, c in sorted(eintraege)]


def negotiate(configured=None, accept_language=None, base=None):
    """Welche Sprache gilt? Einstellung vor Browser vor Quellsprache.

    "auto" (oder nichts) heißt: den Browser fragen. Ein Regionalcode wie de-CH
    zählt für de – sonst fiele jemand mit Schweizer Einstellung auf Deutsch als
    Notnagel statt als Treffer.
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
