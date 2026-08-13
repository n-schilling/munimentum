#!/usr/bin/env python3
"""
settings.py – app_config.json als Vorgabeschicht für die Einzelskripte.

Die Einstellungen aus der Oberfläche (app.py) landen in app_config.json. Ohne
dieses Modul gälten sie nur für Läufe, die die App selbst startet: ruft jemand
`python3 outlook_export.py` direkt auf, kennt das Skript nur seine eingebauten
Vorgaben. Genau das überrascht – eine Konfigurationsdatei im Projektordner
sollte gelten, egal wer startet.

Rangfolge, von stark nach schwach:

    Umgebungsvariable  >  app_config.json  >  eingebaute Vorgabe

Die Umgebung bleibt oben, damit ein einzelner Lauf die Datei ausstechen kann
(`INCLUDE_HIDDEN=0 python3 outlook_export.py`) – und damit die App, die alles
als Umgebungsvariable mitgibt, unverändert eindeutig bleibt.

Bewusst NICHT aus der Datei bedient wird die Auswahl, was exportiert werden
soll (outlook_categories / teams_categories). Wer ein Skript direkt startet,
will gefragt werden; sonst wäre der interaktive Modus faktisch abgeschafft.

Gesucht wird die Datei in MUNIMENTUM_DATA_DIR (früher OFFICE365_DATA_DIR),
sonst neben diesem Modul.
Nur Standardbibliothek.
"""

import json
import os
from pathlib import Path

CONFIG_NAME = "app_config.json"

_FALSCH = ("0", "false", "no", "nein", "off", "")

_cache = {"pfad": None, "daten": None}
_uebernommen = []          # (Schlüssel, Anzeigewert) – alles, was aus der Datei kam


def data_dir_env():
    """Der per Umgebung gesetzte Datenordner – oder None.

    MUNIMENTUM_DATA_DIR ist der Name seit 5.0.0; OFFICE365_DATA_DIR gilt
    weiter, damit vorhandene Skripte und Verknüpfungen nicht brechen. app.py
    fragt für seinen Datenordner dieselbe Stelle – die beiden Namen stehen
    nur hier.
    """
    return os.environ.get("MUNIMENTUM_DATA_DIR") or os.environ.get("OFFICE365_DATA_DIR")


def config_path():
    """Wo die Konfiguration liegt: Datenordner der App, sonst neben dem Modul."""
    env = data_dir_env()
    base = Path(env).expanduser() if env else Path(__file__).resolve().parent
    return base / CONFIG_NAME


def load(path=None):
    """Konfiguration lesen (einmal je Pfad gepuffert).

    Fehlende oder kaputte Datei ergibt {} – ein unlesbares app_config.json darf
    einen Export niemals verhindern, es liefert ja nur Vorgaben.
    """
    p = Path(path) if path is not None else config_path()
    if _cache["pfad"] == str(p) and _cache["daten"] is not None:
        return _cache["daten"]
    try:
        daten = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        daten = {}
    if not isinstance(daten, dict):
        daten = {}
    _cache.update(pfad=str(p), daten=daten)
    return daten


def reset():
    """Puffer und Merkliste leeren (Tests, erneutes Einlesen)."""
    _cache.update(pfad=None, daten=None)
    _uebernommen.clear()


def _truthy(raw):
    return str(raw).strip().lower() not in _FALSCH


def _note(key, anzeige):
    _uebernommen.append((key, anzeige))


def flag(env_name, key, default):
    """Schalter: Umgebung, sonst Datei, sonst Vorgabe."""
    raw = os.environ.get(env_name)
    if raw is not None:
        return _truthy(raw)
    val = load().get(key)
    if isinstance(val, bool) and val is not default:
        _note(key, "an" if val else "aus")
        return val
    return default


def number(env_name, key, default, low=1):
    """Zahl: Umgebung, sonst Datei, sonst Vorgabe. Unbrauchbares wird ignoriert."""
    for quelle, roh in (("env", os.environ.get(env_name)), ("datei", load().get(key))):
        if roh is None or isinstance(roh, bool):
            continue
        try:
            zahl = max(low, int(roh))
        except (TypeError, ValueError):
            continue
        if quelle == "datei" and zahl != default:
            _note(key, zahl)
        return zahl
    return default


def value(key, default=None):
    """Wert aus der Datei, sonst Vorgabe (ohne Umgebungsvariable).

    Absichtlich ohne Vermerk für report(): dieser Zweig liefert nur Vorgaben für
    Argumente, die die Kommandozeile aussticht (Ausgabeordner, --store, --model).
    Eine Meldung "aus app_config.json übernommen" wäre dort schlicht falsch,
    sobald jemand das Argument mitgibt.
    """
    val = load().get(key)
    return default if val is None else val


def folders(env_name, key, default):
    """Ordnerliste: Umgebung (kommagetrennt), sonst Datei (Liste), sonst Vorgabe.

    Leer gesetzt heißt leere Liste, nicht "Vorgabe" – app.py braucht diesen
    Unterschied, um "wirklich alle Ordner" ausdrücken zu können.
    """
    raw = os.environ.get(env_name)
    if raw is not None:
        return {t.strip().lower() for t in raw.split(",") if t.strip()}
    val = load().get(key)
    if isinstance(val, list):
        aus_datei = {str(t).strip().lower() for t in val if str(t).strip()}
        if aus_datei != set(default):
            _note(key, f"{len(aus_datei)} Ordner")
        return aus_datei
    return set(default)


def report():
    """Einzeiler, was aus der Datei kam – oder "" wenn nichts.

    Ohne diese Zeile wundert man sich später über eine Datei, die man vergessen
    hat: das Skript verhielte sich anders als dokumentiert, ohne es zu sagen.
    """
    if not _uebernommen:
        return ""
    teile = ", ".join(f"{k}={v}" for k, v in _uebernommen)
    return f"Aus {config_path().name} übernommen: {teile}"
