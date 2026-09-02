#!/usr/bin/env python3
"""
settings.py – the configuration schema and the app_config.json access layer.

The settings edited in the interface (app.py) live in app_config.json; the
subprograms read the same file through this module. Precedence, strongest
first:

    environment variable  >  app_config.json  >  built-in default (VORGABEN)

The environment stays on top because the app hands its in-memory settings to
every subprocess as variables – that keeps a run unambiguous even while the
file is being edited.

The file is looked up in MUNIMENTUM_DATA_DIR (OFFICE365_DATA_DIR until
4.2.0), otherwise next to this module. Standard library only.
"""

import json
import os
from pathlib import Path

import ollama_client

CONFIG_NAME = "app_config.json"

# Die vier Unterordner im Datenordner. Früher waren sie einstellbar – ein Erbe
# aus der Zeit, als das hier lose Skripte waren, die jemand von Hand in einem
# beliebigen Verzeichnis aufrief. Die App ruft sie längst selbst auf, und drei
# Textfelder in den Einstellungen, die niemand anfasst, sind keine Freiheit,
# sondern Ballast. Die Schlüssel teams_dir/outlook_dir/… aus alten Dateien
# werden weiter gelesen (value unten), nur angeboten werden sie nicht mehr.
TEAMS_DIR = "teams_export"
OUTLOOK_DIR = "outlook_export"
ONEDRIVE_DIR = "onedrive_export"
SHAREPOINT_DIR = "sharepoint_export"
SHAREPOINT_PAGES_DIR = "sharepoint_pages"
PLANNER_DIR = "planner_export"
STORE_DIR = "rag_store"

# Postfach-Ordner, die die Standardauswahl von outlook_export.py auslässt.
SKIP_FOLDERS_STANDARD = {
    "archive", "archiv",
    "entwürfe", "drafts",
    "erneut erinnern aktiviert",
    "gelöschte elemente", "deleted items",
    "junk-e-mail", "junk email", "junk-email",
    "postausgang", "outbox",
}

# Was eine Anhangliste aufbläht, ohne dass jemand danach sucht: die Signatur-
# und Verschlüsselungsanhänge, die Mailprogramme selbst anhängen. Sie stehen als
# Vorgabe im Feld und sind dort zu sehen und zu ändern – eine stille Regel im
# Code wäre genau das, was man später nicht mehr findet.
FILETYPE_HIDDEN_STANDARD = {"p7s", "p7m", "asc", "pgp", "sig"}

# Der Bauplan von app_config.json: jeder Schlüssel einmal, mit seiner Vorgabe.
# app.py zeigt genau diese Werte in den Einstellungen; die Einzelskripte holen
# sie über flag()/number()/value() unten – bis 5.3 trug jede Aufrufstelle ihre
# eigene Kopie der Vorgabe, und nichts hielt die Kopien zusammen.
VORGABEN = {
    # Ollama ist optional. Aus heißt: es wird gar nicht mehr danach gesucht
    # (bisher lief alle zehn Sekunden ein Verbindungsversuch ins Leere), die
    # Bedeutungssuche und die KI-Zusammenfassung verschwinden, und der Index
    # wird als reiner Volltextindex gebaut. Alles andere läuft unverändert.
    "ollama_enabled": True,
    # Auch mit Ollama kann man den Volltextindex wollen: Einbetten kostet auf
    # einem echten Bestand eine gute Stunde, und wer nur exakt sucht, zahlt sie
    # umsonst.
    "index_semantic": True,
    # Aus, bis jemand es einschaltet: ein Laufwerk kann zweistellige
    # Gigabyte haben, und niemand soll die beim ersten Klick ziehen.
    "onedrive_enabled": False,
    # Include/Exclude auf OneDrive-Pfaden, dieselbe Mechanik wie beim Postfach.
    "onedrive_rules": "",
    "onedrive_max_mb": 0,
    # Off until someone turns it on – the same caution as with the drive.
    "sharepoint_enabled": False,
    # Site or library URLs, one per line; each resolves to its libraries.
    "sharepoint_urls": "",
    # Extension filters, comma-separated ("pdf, docx"). Include empty means
    # every type; exclude wins when both name the same extension.
    "sharepoint_types_include": "",
    "sharepoint_types_exclude": "",
    "sharepoint_max_mb": 0,
    # Site pages as HTML – its own list: pages and libraries rarely overlap.
    "sharepoint_pages_enabled": False,
    "sharepoint_pages_urls": "",
    # Embed images up to this size; larger ones stay links. 0 = no limit.
    "sharepoint_pages_image_max_mb": 4,
    # Sync cadence per source or per SharePoint unit ("onedrive", "teams",
    # "sharepoint:<drive-id>", "pages:<site-id>") -> always|daily|weekly|
    # monthly. Missing key means always; the toggle narrows every run,
    # scheduled and manual alike.
    "sync_cadence": {},
    # Nichts vorausgewählt: jede dieser Kategorien kann zehntausende Elemente
    # und viele Gigabyte bedeuten. Was geholt wird, soll eine Entscheidung
    # sein und nicht das, was beim ersten Start zufällig angehakt war.
    "outlook_categories": [],
    "teams_categories": [],
    "workers": 4,
    # Drive mirrors (OneDrive/SharePoint) take their own concurrency: Graph
    # documents no fixed limit for drives – throttling is budget-based and
    # answered with visible waits – so more than the mailbox's four is fine.
    "mirror_workers": 8,
    # Schalter der Export-Skripte (dort per Umgebungsvariable, siehe env_flag)
    "embed_images": True,
    "cache_images": True,
    "refresh_channels": True,
    "skip_empty_chats": True,
    "include_hidden": False,
    # Holt gelöschte Termine aus Einladungs- und Absagemails zurück. Dafür wird
    # jede .eml gelesen – der mit Abstand teuerste Schritt. Standardmäßig an,
    # weil es Termine sichtbar macht, die es sonst nirgends mehr gibt.
    "calendar_reconstruct": True,
    "skip_folders": sorted(SKIP_FOLDERS_STANDARD),
    # Dateitypen, die im Suchfilter nicht angeboten werden. Rein kosmetisch:
    # exportiert und durchsuchbar bleibt alles, es steht nur nicht in der
    # Auswahlliste. Als sichtbare Vorgabe statt als Regel im Code.
    "filetype_hidden": sorted(FILETYPE_HIDDEN_STANDARD),
    # Personen, die in der Auswertung nicht gezählt werden – in aller Regel man
    # selbst: die eigenen Nachrichten stehen sonst mit Abstand oben und sagen
    # nichts über den Austausch mit anderen. Eine je Zeile, weil Namen Kommas
    # enthalten („Schilling, Nico“).
    "analytics_skip": [],
    # Ordnerauswahl als geordnete Regeln, letzte Übereinstimmung gewinnt.
    # Leer heißt: die alte Namensliste oben gilt weiter (siehe folders.py).
    "folder_rules": "",
    # Kalenderauswahl, dieselbe Mechanik wie oben. Leer heißt: nur der
    # Standardkalender (siehe folders.nur_standard).
    "calendar_rules": "",
    # 128 an einem echten Archiv gemessen: rund ein Fuenftel schneller
    # als 64, und auch die laengsten Chunks gehen noch durch. 256 lehnt
    # Ollama ab.
    "index_batch": 128,
    "ollama": ollama_client.DEFAULT_URL,
    "embed_model": "bge-m3",
    "chat_model": "qwen3.6:27b",            # formuliert die Antwort, lokal
    "answer_sources": 8,                    # wie viele Treffer sie dafür liest
    # Untergrenze der Bedeutungssuche; siehe mcp_server.SEM_MIN. Als Ganzzahl
    # in Prozent, damit die Oberfläche ein normales Zahlenfeld benutzen kann
    # und niemand über ein Komma stolpert.
    "semantic_min": 45,
    # Treffer je Seite in der Suche. Mehr heißt weniger Blättern, aber auch
    # eine längere Liste, durch die man erst einmal hindurchsehen muss.
    "search_results": 20,
    # UI-Userflow-Aufzeichnung: die letzten Bedienschritte für den Fehler-
    # bericht – nur die Art (Reiter, Suche, Lauf), nie Inhalte, rein im
    # Speicher der offenen Seite. 0 schaltet sie ab.
    "notifications": "errors",  # system notifications: off | errors | all
    "userflow_actions": 20,
    # Wie lange die Lauf-Historie (runs.db) zurückreicht. Aufgeräumt wird beim
    # Start und nach jedem Lauf; die Datei bleibt im Kilobyte-Bereich.
    "planner_enabled": False,
    "planner_urls": "",
    "planner_attachments": False,
    "runs_retention_months": 24,
    "log_retention_days": 14,
    "mcp_port": 8365,
    # Der harte Schalter: aus heißt, dass mcp_server den Dienst verweigert –
    # über HTTP wie über stdio. Start/Stop daneben betrifft nur den
    # HTTP-Endpunkt, den diese App selbst betreibt.
    "mcp_enabled": True,
    "mcp_autostart": True,
    "update_check": True,   # einmal beim Start bei GitHub nachsehen
    # Wie sich die App anmeldet. "token" = eingefügter Zugangsschlüssel (keine
    # Rückfrage bei der IT nötig, gilt aber nur Stunden); "login" = richtige
    # Anmeldung mit Refresh Token, damit der Zeitplan unbeaufsichtigt läuft.
    "auth_mode": "token",
    "client_id": "",        # leer = Microsofts öffentliche Anwendung
    "tenant": "",           # leer = organizations
    "device_code": False,   # Skripte im Terminal: Code statt Browserfenster
    "language": "auto",   # "auto" = Browsersprache, sonst ein Code aus lang/
    "schedule": {
        "enabled": False,
        "interval_minutes": 60,
        "outlook": True,
        "teams": True,
        # The mirrors ride along only where their master switch is on –
        # these toggles narrow a scheduled run, they cannot widen it.
        "onedrive": True,
        "sharepoint": True,
        "sharepoint_pages": True,
        "planner": True,
        "index": True,
        "calendar": True,
    },
}

_FALSCH = ("0", "false", "no", "nein", "off", "")

# Sentinel für „kein eigener Vorgabewert": dann gilt VORGABEN[key]. Ein
# ausdrückliches default=None bleibt dagegen None – die Regel-Schlüssel
# (folder_rules u. a.) unterscheiden „nicht gesetzt" von „leer".
_AUS_SCHEMA = object()


def _vorgabe(key, default):
    return VORGABEN[key] if default is _AUS_SCHEMA else default


_cache = {"pfad": None, "daten": None}


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
    """Puffer leeren (Tests, erneutes Einlesen)."""
    _cache.update(pfad=None, daten=None)


def _truthy(raw):
    return str(raw).strip().lower() not in _FALSCH


def flag(env_name, key, default=_AUS_SCHEMA):
    """Schalter: Umgebung, sonst Datei, sonst Vorgabe (aus VORGABEN)."""
    default = _vorgabe(key, default)
    raw = os.environ.get(env_name)
    if raw is not None:
        return _truthy(raw)
    val = load().get(key)
    if isinstance(val, bool):
        return val
    return default


def number(env_name, key, default=_AUS_SCHEMA, low=1):
    """Zahl: Umgebung, sonst Datei, sonst Vorgabe. Unbrauchbares wird ignoriert."""
    default = _vorgabe(key, default)
    for roh in (os.environ.get(env_name), load().get(key)):
        if roh is None or isinstance(roh, bool):
            continue
        try:
            return max(low, int(roh))
        except (TypeError, ValueError):
            continue
    return default


def value(key, default=_AUS_SCHEMA):
    """Wert aus der Datei, sonst Vorgabe (ohne Umgebungsvariable).

    Absichtlich ohne Vermerk für report(): dieser Zweig liefert nur Vorgaben für
    Argumente, die die Kommandozeile aussticht (Ausgabeordner, --store, --model).
    Eine Meldung "aus app_config.json übernommen" wäre dort schlicht falsch,
    sobald jemand das Argument mitgibt.
    """
    default = _vorgabe(key, default)
    val = load().get(key)
    return default if val is None else val


def folders(env_name, key, default=_AUS_SCHEMA):
    """Ordnerliste: Umgebung (kommagetrennt), sonst Datei (Liste), sonst Vorgabe.

    Leer gesetzt heißt leere Liste, nicht "Vorgabe" – app.py braucht diesen
    Unterschied, um "wirklich alle Ordner" ausdrücken zu können.
    """
    default = _vorgabe(key, default)
    raw = os.environ.get(env_name)
    if raw is not None:
        return {t.strip().lower() for t in raw.split(",") if t.strip()}
    val = load().get(key)
    if isinstance(val, list):
        return {str(t).strip().lower() for t in val if str(t).strip()}
    return set(default)
