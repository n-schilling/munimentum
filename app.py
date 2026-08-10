#!/usr/bin/env python3
"""
app.py – Bedienoberfläche für den Office-365-Export im Browser.

Ein Start, ein Fenster: das Skript startet einen kleinen HTTP-Server auf
127.0.0.1 und öffnet die Oberfläche im Standardbrowser. Von dort aus laufen
alle Teile des Projekts, ohne dass jemand ein Terminal braucht:

    Token       Assistent: Access Token im Graph Explorer holen und einfügen.
                Erscheint bei jedem Start und immer dann, wenn kein gültiger
                Token da ist. Eine Anmeldung findet NICHT statt – der Token
                wird ausschließlich manuell besorgt (gx_token.txt).
    Export      Outlook und/oder Teams, Auswahl per Klick statt per Abfrage
                (setzt EXPORT_CATEGORIES für die Export-Skripte).
    Index       rag_index.py im Anschluss, für Suche und MCP.
    Suche       eingebettet – dieselbe Rangfolge wie im MCP-Server
                (BM25 + Embeddings, per RRF fusioniert).
    Zeitplan    solange die App läuft: Export + Index in festem Abstand.
    MCP         mcp_server.py starten/stoppen, Konfigschnipsel für Claude.

Ohne Ollama zeigt die App einen Assistenten zur Installation. Alternativ läuft
alles weiter: der MCP-Server wird gestartet und die Indizierung ausgelassen
(bzw. auf Wunsch als reiner Volltextindex gebaut, rag_index.py --no-embeddings).

    python3 app.py [--port 8700] [--no-browser]

Der Server bindet nur auf die Loopback-Adresse und prüft den Host-Header. Er
hat keine Authentifizierung und liefert den gesamten Mail- und Chatbestand
aus – er gehört nicht auf 0.0.0.0.
"""

import os
import re
import sys
import copy
import json
import time
import gzip
import base64
import shutil
import sqlite3
import argparse
import platform
import importlib
import subprocess
import threading
import webbrowser
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import answer
import auth
import folders
import i18n
import progress
import settings
import updates
import version

# Auf Windows nutzt die Konsole standardmäßig eine Legacy-Codepage; UTF-8
# erzwingen, damit print() an Unicode nicht scheitert (macOS/Linux: No-op).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

APP_DIRNAME = "Microsoft365-Archiv"
FROZEN = bool(getattr(sys, "frozen", False))

# Teilprogramme, die die gebündelte Datei über "--run <name>" selbst starten
# kann. Als Skripte liegen sie nebeneinander, im Bündel als Module darin.
RUNNABLE = ("outlook_export", "teams_export", "rag_index", "combined_search",
            "mcp_server", "rag_server",
            # auth ist kein Exportschritt, sondern eine Selbstauskunft: welcher
            # Anmeldeweg gilt, liegt ein Schlüssel vor, gibt es einen Cache.
            # Im Bündel ist das der einzige Weg, das ohne Netz zu prüfen –
            # der Rauchtest tut genau das.
            "auth", "onedrive_export")


def resource_dir():
    """Verzeichnis der mitgelieferten Skripte (im Bündel: das entpackte Archiv)."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


ZEIGER_DATEI = "datenordner.txt"


def standard_data_dir():
    """Wo die Daten liegen, wenn niemand etwas anderes sagt.

    Als Skript: der Projektordner – dort liegen Exporte und rag_store schon.
    Gebündelt: der Datenordner des Benutzers, denn das Bündel selbst entpackt
    sich in ein Temp-Verzeichnis, das bei jedem Ende verschwindet, und in
    /Applications bzw. C:\\Program Files darf eine App nicht schreiben.
    """
    if not FROZEN:
        return Path(__file__).resolve().parent
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIRNAME
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(root) / APP_DIRNAME
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / APP_DIRNAME


def zeiger_datei():
    """Die eine Datei, die am Standardort bleibt und woandershin zeigt.

    Der Datenordner lässt sich nicht in app_config.json einstellen – die Datei
    liegt ja selbst darin, man müsste sie lesen, um zu wissen, wo sie liegt.
    Deshalb ein Zeiger am Standardort: eine Zeile, ein Pfad.
    """
    return standard_data_dir() / ZEIGER_DATEI


def lies_zeiger():
    try:
        roh = zeiger_datei().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not roh:
        return None
    ziel = Path(roh).expanduser()
    # Ein Zeiger auf einen Ordner, den es nicht mehr gibt (externe Platte ab),
    # darf die App nicht am Starten hindern – dann eben wieder der Standardort.
    return ziel.resolve() if ziel.is_dir() else None


def data_dir():
    """Verzeichnis für Exporte, Index, Konfiguration und Token.

    Reihenfolge wie überall im Projekt: Umgebung schlägt Datei schlägt Vorgabe.
    Mit OFFICE365_DATA_DIR bzw. --data-dir für einen einzelnen Lauf, mit dem
    Zeiger dauerhaft (z. B. eine externe Platte – ein Postfach kann
    zweistellige Gigabyte haben).
    """
    env = os.environ.get("OFFICE365_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return lies_zeiger() or standard_data_dir()


RES = resource_dir()
BASE = data_dir()
CONFIG_FILE = BASE / settings.CONFIG_NAME   # dieselbe Datei, die die Einzelskripte lesen
TOKEN_FILE = BASE / "gx_token.txt"


def set_data_dir(path):
    """Datenverzeichnis umhängen (--data-dir). Liefert den neuen Pfad."""
    global BASE, CONFIG_FILE, TOKEN_FILE
    BASE = Path(path).expanduser().resolve()
    CONFIG_FILE = BASE / settings.CONFIG_NAME
    TOKEN_FILE = BASE / "gx_token.txt"
    # Die Teilprogramme suchen ihre Vorgaben über dieselbe Variable – sonst läse
    # ein Unterprozess die Datei neben dem Skript statt die hier gewählte.
    os.environ["OFFICE365_DATA_DIR"] = str(BASE)
    settings.reset()
    return BASE

def pruefe_datenordner(pfad):
    """Taugt der Ordner? Liefert (Pfad, Fehlerschlüssel).

    Lieber jetzt ablehnen als beim nächsten Start: ein Zeiger auf einen Ordner
    ohne Schreibrecht führte in eine App, die nichts mehr speichern kann – und
    die Einstellung, mit der man es zurücknähme, liegt genau dort.
    """
    roh = str(pfad or "").strip()
    if not roh:
        return None, "srv.datadir.empty"
    ziel = Path(roh).expanduser()
    try:
        ziel.mkdir(parents=True, exist_ok=True)
        probe = ziel / ".schreibprobe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return None, {"k": "srv.datadir.unwritable", "v": {"detail": str(e)}}
    return ziel.resolve(), None


def schreibe_zeiger(pfad):
    """Den Zeiger setzen – oder löschen, wenn er auf den Standardort zeigt."""
    datei = zeiger_datei()
    try:
        datei.parent.mkdir(parents=True, exist_ok=True)
        if Path(pfad).resolve() == standard_data_dir().resolve():
            datei.unlink(missing_ok=True)
        else:
            datei.write_text(str(pfad) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


GRAPH_EXPLORER = "https://developer.microsoft.com/en-us/graph/graph-explorer"
OLLAMA_SITE = "https://ollama.com/download"

# Postfach-Ordner, die die Standardauswahl von outlook_export.py auslässt.
# Bewusst hier gespiegelt statt outlook_export zu importieren: das Modul bricht
# beim Import ohne msal/requests ab, und die App soll auch dann eine Oberfläche
# zeigen. Ein Test hält beide Listen zusammen (test_app: gespiegelte_liste).
SKIP_FOLDERS_DEFAULT = {
    "archive", "archiv",
    "entwürfe", "drafts",
    "erneut erinnern aktiviert",
    "gelöschte elemente", "deleted items",
    "junk-e-mail", "junk email", "junk-email",
    "postausgang", "outbox",
}

DEFAULT_CONFIG = {
    "teams_dir": "teams_export",
    "outlook_dir": "outlook_export",
    "onedrive_dir": "onedrive_export",
    # Aus, bis jemand es einschaltet: ein Laufwerk kann zweistellige
    # Gigabyte haben, und niemand soll die beim ersten Klick ziehen.
    "onedrive_enabled": False,
    # Include/Exclude auf OneDrive-Pfaden, dieselbe Mechanik wie beim Postfach.
    "onedrive_rules": "",
    "onedrive_max_mb": 0,
    "store_dir": "rag_store",
    "outlook_categories": ["mail", "calendar", "contacts"],
    "teams_categories": ["1on1", "group", "meeting"],
    "workers": 4,
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
    "skip_folders": sorted(SKIP_FOLDERS_DEFAULT),
    # Ordnerauswahl als geordnete Regeln, letzte Übereinstimmung gewinnt.
    # Leer heißt: die alte Namensliste oben gilt weiter (siehe folders.py).
    "folder_rules": "",
    "index_batch": 64,
    "ollama": "http://localhost:11434",
    "embed_model": "bge-m3",
    "chat_model": "qwen3.6:27b",            # formuliert die Antwort, lokal
    "answer_sources": 8,                    # wie viele Treffer sie dafür liest
    # Treffer je Seite in der Suche. Mehr heißt weniger Blättern, aber auch
    # eine längere Liste, durch die man erst einmal hindurchsehen muss.
    "search_results": 20,
    "mcp_port": 8365,
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
        "index": True,
        "calendar": True,
    },
}

# Kategorie -> Graph-Berechtigung. Der Assistent prüft damit, ob der eingefügte
# Token für das reicht, was ausgewählt ist (scp-Claim im JWT).
SCOPE_FOR = {
    "mail": "Mail.Read",
    "calendar": "Calendars.Read",
    "contacts": "Contacts.Read",
    "1on1": "Chat.Read",
    "group": "Chat.Read",
    "meeting": "Chat.Read",
    "channels": "ChannelMessage.Read.All",
    "files": "Files.Read.All",
}
LABEL_FOR = {
    "mail": "E-Mail", "calendar": "Kalender", "contacts": "Kontakte",
    "1on1": "1:1-Chats", "group": "Gruppenchats",
    "meeting": "Meeting-Chats", "channels": "Team-Kanäle", "files": "OneDrive",
}

# Weitere Berechtigungen, die die jeweils nötige mit abdecken. Der Graph
# Explorer vergibt oft gleich die Schreibvariante: wer Mail.ReadWrite hat, darf
# erst recht lesen, im Token steht dann aber nie Mail.Read. Ohne diese Tabelle
# meldete der Assistent fehlende Rechte, die in Wahrheit da sind.
#
# Bewusst großzügig: eine ausbleibende Warnung kostet höchstens einen 403 im
# Lauf (so war es vor der Prüfung ohnehin), eine falsche Warnung schickt
# dagegen jemanden los, im Graph Explorer etwas zu suchen, das er längst hat.
# NICHT enthalten sind Varianten, die weniger können, als der Export braucht:
# Chat.ReadBasic und Mail.ReadBasic liefern keine Nachrichteninhalte.
SCOPE_COVERED_BY = {
    "Mail.Read": ("Mail.ReadWrite", "Mail.Read.Shared", "Mail.ReadWrite.Shared"),
    "Calendars.Read": ("Calendars.ReadWrite", "Calendars.Read.Shared",
                       "Calendars.ReadWrite.Shared"),
    "Contacts.Read": ("Contacts.ReadWrite", "Contacts.Read.Shared",
                      "Contacts.ReadWrite.Shared"),
    "Chat.Read": ("Chat.ReadWrite",),
    # Graph erlaubt das Lesen von Kanalnachrichten auch mit den Gruppenrechten.
    "ChannelMessage.Read.All": ("Group.Read.All", "Group.ReadWrite.All"),
}


# Der Reiter "Modify permissions" im Graph Explorer listet nur die Rechte zu der
# Abfrage, die gerade in der Adresszeile steht. Wer dort nie eine Mail-Abfrage
# ausgeführt hat, bekommt Mail.Read schlicht nie angeboten und sucht vergeblich.
# Deshalb zu jedem Recht die Abfrage, die es sichtbar macht.
SCOPE_QUERY = {
    "Mail.Read": "https://graph.microsoft.com/v1.0/me/messages?$top=1",
    "Calendars.Read": "https://graph.microsoft.com/v1.0/me/events?$top=1",
    "Contacts.Read": "https://graph.microsoft.com/v1.0/me/contacts?$top=1",
    "Chat.Read": "https://graph.microsoft.com/v1.0/me/chats?$top=1",
    "ChannelMessage.Read.All": "https://graph.microsoft.com/v1.0/me/joinedTeams",
    "Files.Read.All": "https://graph.microsoft.com/v1.0/me/drive/root/children?$top=1",
    "User.Read": "https://graph.microsoft.com/v1.0/me",
}


def scope_missing(needed_scopes, have):
    """Welche der nötigen Berechtigungen deckt der Token nicht ab?"""
    have = set(have)
    return sorted(s for s in needed_scopes
                  if s not in have
                  and not have.intersection(SCOPE_COVERED_BY.get(s, ())))


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------
def _merge_defaults(base, loaded):
    """Geladene Werte über die Vorgaben legen, eine Ebene tief rekursiv.

    Damit fehlt nach einem Update nie ein Schlüssel, und eine von Hand
    verkürzte app_config.json bleibt gültig. Tiefe Kopie, sonst würde ein
    späteres cfg["schedule"]["enabled"] = True DEFAULT_CONFIG selbst verändern –
    die Vorgaben wären dann für den Rest der Laufzeit verstellt.
    """
    out = copy.deepcopy(base)
    for k, v in (loaded or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = {**out[k], **v}
        elif k in out:
            out[k] = v
    return out


def load_config(path=None):
    path = Path(path or CONFIG_FILE)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = {}
    return _merge_defaults(DEFAULT_CONFIG, loaded)


def save_config(cfg, path=None):
    path = Path(path or CONFIG_FILE)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _clean_categories(values, allowed):
    """Nur bekannte Kategorien, in der Reihenfolge von `allowed`."""
    picked = {str(v).strip().lower() for v in (values or [])}
    return [k for k in allowed if k in picked]


def _clean_folders(values):
    """Ordnernamen aus Liste oder Text (kommagetrennt): kleingeschrieben, ohne Doppel.

    outlook_export.py vergleicht Anzeigenamen case-insensitive, also wird hier
    schon kleingeschrieben – sonst steht in der Oberfläche etwas anderes als
    das, wonach am Ende verglichen wird.
    """
    if isinstance(values, str):
        values = values.replace("\n", ",").split(",")
    return sorted({str(v).strip().lower() for v in (values or []) if str(v).strip()})


def auswahlregeln(cfg, roh=None, namen=None):
    """Die Regeln, nach denen der Export Ordner auswählt.

    Dieselbe Reihenfolge wie outlook_export.aktuelle_regeln – die Regeln
    gelten, und nur solange keine da sind, wirkt die alte Namensliste weiter.
    Beides ist hier an einer Stelle, damit die Vorschau nicht anders rechnet
    als der Lauf, den sie vorhersagt.

    `roh` und `namen` sind das, was gerade in den Feldern steht: wer eine Regel
    tippt, will sie prüfen können, bevor er sie speichert.
    """
    regeln = folders.lies_regeln(
        (cfg.get("folder_rules") if roh is None else roh) or "")
    if regeln:
        return regeln
    return folders.aus_namensliste(
        _clean_folders(cfg.get("skip_folders") if namen is None else namen))


# --------------------------------------------------------------------------
# Token: einfügen, prüfen, ablegen
# --------------------------------------------------------------------------
def normalize_token(raw):
    """Eingefügten Token säubern: Anführungszeichen, "Bearer ", Zeilenumbrüche.

    Der Graph Explorer liefert den Token oft mit Zeilenumbrüchen aus dem
    Kopier-Feld; ein JWT enthält selbst keinen Whitespace, also darf alles
    davon weg.
    """
    if not raw:
        return ""
    val = str(raw).strip().strip('"').strip("'").strip()
    if val.lower().startswith("bearer "):
        val = val[7:]
    return re.sub(r"\s+", "", val)


def decode_jwt(token):
    """Nutzlast eines JWT ohne Signaturprüfung lesen. {} wenn das nichts ist.

    Nur zur Anzeige (Konto, Ablauf, Berechtigungen) – geprüft wird der Token
    ohnehin von Graph beim ersten Aufruf.
    """
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def token_status(token, now=None, needed=()):
    """Zustand des Tokens für die Oberfläche.

    `needed` sind Kategorien (mail, 1on1, …); fehlende Berechtigungen dazu
    werden benannt, damit der Assistent sagen kann, was im Graph Explorer noch
    zuzustimmen ist. Lässt sich der Token nicht als JWT lesen, gilt er als
    vorhanden mit unbekanntem Ablauf – er wird dann einfach ausprobiert.
    """
    now = now if now is not None else time.time()
    out = {"present": bool(token), "valid": False, "expired": False,
           "readable": False, "account": None, "name": None,
           "expires_at": None, "expires_in_minutes": None,
           "scopes": [], "missing": []}
    if not token:
        return out
    claims = decode_jwt(token)
    if not claims:
        out["valid"] = True          # unlesbar, aber vorhanden -> ausprobieren
        return out
    out["readable"] = True
    out["account"] = (claims.get("upn") or claims.get("preferred_username")
                      or claims.get("unique_name"))
    out["name"] = claims.get("name")
    out["scopes"] = sorted((claims.get("scp") or "").split())
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        out["expires_at"] = datetime.fromtimestamp(exp, UTC).isoformat()
        out["expires_in_minutes"] = int((exp - now) // 60)
        out["expired"] = exp <= now
    out["valid"] = not out["expired"]
    # Fehlende Rechte nur melden, wenn der scp-Claim wirklich gelesen wurde –
    # sonst sähe ein Token ohne lesbare Claims so aus, als fehlte alles.
    if out["scopes"]:
        want = {SCOPE_FOR[c] for c in needed if c in SCOPE_FOR}
        out["missing"] = scope_missing(want, out["scopes"])
    return out


def read_token(path=None):
    try:
        return normalize_token(Path(path or TOKEN_FILE).read_text(encoding="utf-8"))
    except OSError:
        return ""


def write_token(token, path=None):
    """Token ablegen – nur für den eigenen Benutzer lesbar."""
    token = normalize_token(token)
    p = Path(path or TOKEN_FILE)
    p.write_text(token + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass                          # z. B. Windows/FAT: Rechte nicht setzbar
    return token


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------
def _hat_modell(namen, gesucht):
    """"bge-m3" in der Liste heißt "bge-m3:latest" – ohne Tag vergleichen."""
    if not gesucht:
        return False
    rumpf = gesucht.split(":", 1)[0]
    return any(n == gesucht or n.split(":", 1)[0] == rumpf for n in namen)


def check_ollama(url, model, chat_model=None, timeout=1.5):
    """Läuft Ollama – und welche der beiden Modelle liegen bereit?

    Das Embedding-Modell trägt die semantische Suche, das Chat-Modell die
    formulierte Antwort. Beide getrennt gemeldet: wer nur das erste hat, soll
    suchen können, ohne dass die Oberfläche eine Antwort verspricht, die kein
    Modell erzeugen kann.
    """
    out = {"running": False, "models": [], "has_model": False,
           "has_chat_model": False, "error": None,
           "model": model, "chat_model": chat_model, "url": url}
    try:
        import requests
        r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
        r.raise_for_status()
        names = [m.get("name", "") for m in (r.json().get("models") or [])]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["running"] = True
    out["models"] = sorted(names)
    out["has_model"] = _hat_modell(names, model)
    out["has_chat_model"] = _hat_modell(names, chat_model)
    return out


def ollama_hint():
    """Installationshinweis passend zum Betriebssystem – als Textschlüssel.

    Die Sätze stehen in den Sprachdateien; hier wird nur entschieden, welche
    Schritte gelten und mit welchen Werten sie zu füllen sind.
    """
    sysname = platform.system()
    if sysname == "Darwin":
        return {"os": "macOS", "url": OLLAMA_SITE,
                "steps": ["wizard.ollama.step.mac1", "wizard.ollama.step.mac2",
                          "wizard.ollama.step.recheck"],
                "pkg": "brew install --cask ollama"}
    if sysname == "Windows":
        return {"os": "Windows", "url": OLLAMA_SITE,
                "steps": ["wizard.ollama.step.win1", "wizard.ollama.step.win2",
                          "wizard.ollama.step.recheck"],
                "pkg": "winget install Ollama.Ollama"}
    return {"os": sysname or "Linux", "url": OLLAMA_SITE,
            "steps": ["wizard.ollama.step.linux1", "wizard.ollama.step.linux2",
                      "wizard.ollama.step.recheck"],
            "pkg": None}


# --------------------------------------------------------------------------
# Zustand von Exporten und Index
# --------------------------------------------------------------------------
def _mtime_iso(p):
    try:
        return datetime.fromtimestamp(Path(p).stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def export_status(cfg):
    """Gibt es die Export-Ordner, und wann liefen sie zuletzt?

    Als Zeitpunkt dient die Fortschrittsdatei des jeweiligen Exports – die
    Ordnergröße bleibt bewusst außen vor: ein Postfach kann zweistellige
    Gigabyte haben, das bei jedem Statusabruf durchzuzählen wäre teuer.
    """
    teams = BASE / cfg["teams_dir"]
    outlook = BASE / cfg["outlook_dir"]
    onedrive = BASE / cfg["onedrive_dir"]
    return {
        "teams": {"dir": str(teams), "exists": teams.is_dir(),
                  "last_run": _mtime_iso(teams / "export_state.json")},
        "outlook": {"dir": str(outlook), "exists": outlook.is_dir(),
                    "last_run": _mtime_iso(outlook / "exported.tsv")},
        # Der Bestand, nicht der Delta-Zeiger: der wird auch nach einem Lauf
        # ohne Änderung neu geschrieben und behauptete dann einen Abgleich,
        # bei dem nichts geholt wurde.
        "onedrive": {"dir": str(onedrive), "exists": onedrive.is_dir(),
                     "last_run": _mtime_iso(onedrive / "dateien.tsv")},
    }


_ZAEHLUNG = {}          # db-Pfad -> (Kennung der Datei, Zahlen)


def _zaehle(db):
    """Textstellen und Nachrichten im Index – gepuffert.

    Die Oberfläche fragt den Zustand alle paar Sekunden ab. COUNT(DISTINCT uid)
    läuft über den ganzen Index (auf 270.000 Zeilen rund 30 ms); das jedes Mal
    zu wiederholen wäre Verschwendung, denn die Zahlen ändern sich nur, wenn
    die Datei sich ändert. Größe und Änderungszeit sind die Kennung dafür.
    """
    try:
        s = db.stat()
        kennung = (s.st_mtime_ns, s.st_size)
    except OSError:
        return {"chunks": 0, "messages": 0}
    alt = _ZAEHLUNG.get(str(db))
    if alt and alt[0] == kennung:
        return alt[1]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        zahlen = {
            "chunks": con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            # Was der Anwender „Nachricht“ nennt: eine Mail, ein Chat, ein
            # Termin. Lange Nachrichten stehen als mehrere Textstellen im
            # Index – die Zahl der Zeilen wäre also deutlich höher als das,
            # was jemand in seinem Archiv wiederzufinden erwartet.
            "messages": con.execute("SELECT COUNT(DISTINCT uid) FROM chunks").fetchone()[0],
            # Was dieser Index kann. Ein älterer kennt Verlauf und Löschungen
            # nicht; die Oberfläche bietet sie dann gar nicht erst an, statt
            # den Anwender in einen Fehler laufen zu lassen.
            "features": sorted({r[1] for r in con.execute("PRAGMA table_info(chunks)")}
                               & {"thread", "gone"}),
        }
    finally:
        con.close()
    _ZAEHLUNG[str(db)] = (kennung, zahlen)
    return zahlen


def lies_bericht(cfg, schluessel="outlook_dir"):
    """Der letzte Vollständigkeitsbericht, falls es einen gibt.

    Er entsteht nur auf Knopfdruck: die Prüfung fragt Microsoft, und das soll
    niemand ungefragt tun, nur weil eine Ansicht aufgeht.
    """
    pfad = BASE / cfg[schluessel] / "vollstaendigkeit.json"
    try:
        return json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def kennzahlen(cfg):
    """Was steckt im Archiv? Eine Antwort aus dem Index, ohne Graph zu fragen.

    Bewusst alles aus corpus.db: die Zahlen sollen sofort dastehen, wenn der
    Reiter aufgeht. Was nur Microsoft beantworten kann – ob etwas FEHLT – ist
    ein eigener Schritt mit eigenem Knopf.
    """
    db = BASE / cfg["store_dir"] / "corpus.db"
    # None heißt „weiß ich nicht“, 0 hieße „keine“. Ein Index aus einer
    # älteren Fassung kennt die Spalten nicht; „0 mit Anhang“ zu melden wäre
    # eine Behauptung statt einer Auskunft.
    out = {"exists": db.exists(), "quellen": [], "nachrichten": 0,
           "gespraeche": None, "mit_anhang": None, "personen": 0,
           "verschwunden": None, "von": None, "bis": None,
           "built_at": _mtime_iso(db), "groesse": {}}
    for schluessel, ordner in (("teams", cfg["teams_dir"]),
                               ("outlook", cfg["outlook_dir"]),
                               ("onedrive", cfg["onedrive_dir"])):
        out["groesse"][schluessel] = ordner_groesse(BASE / ordner)
    out["groesse"]["index"] = ordner_groesse(BASE / cfg["store_dir"])
    if not out["exists"]:
        return out
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        spalten = {r[1] for r in con.execute("PRAGMA table_info(chunks)")}
        out["quellen"] = [
            {"src": src, "nachrichten": n}
            for src, n in con.execute(
                "SELECT src, COUNT(DISTINCT uid) FROM chunks GROUP BY src "
                "ORDER BY 2 DESC")]
        out["nachrichten"] = sum(q["nachrichten"] for q in out["quellen"])
        out["personen"] = con.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        von, bis = con.execute(
            "SELECT MIN(ts), MAX(ts) FROM chunks WHERE ts IS NOT NULL").fetchone()
        out["von"], out["bis"] = von, bis
        if "thread" in spalten:
            out["gespraeche"] = con.execute(
                "SELECT COUNT(DISTINCT thread) FROM chunks "
                "WHERE thread IS NOT NULL AND thread != ''").fetchone()[0]
        if "att" in spalten:
            out["mit_anhang"] = con.execute(
                "SELECT COUNT(*) FROM chunks WHERE seq = 0 AND att IS NOT NULL "
                "AND att != ''").fetchone()[0]
        if "gone" in spalten:
            out["verschwunden"] = con.execute(
                "SELECT COUNT(*) FROM chunks WHERE seq = 0 "
                "AND gone IS NOT NULL").fetchone()[0]
    except sqlite3.Error as e:
        out["error"] = str(e)
    finally:
        con.close()
    return out


_GROESSE = {}          # Pfad -> (Zeitpunkt, Bytes)
GROESSE_TTL = 120      # Sekunden


def ordner_groesse(pfad, ttl=GROESSE_TTL):
    """Belegter Platz in Bytes, kurz gepuffert.

    Der Gang über 45.000 Dateien kostet kalt ein paar Sekunden. Für eine
    Ansicht, die man mehrmals öffnet, ist das jedes Mal zu viel – und so schnell
    ändert sich die Größe eines Archivs nicht.

    Fehlerhafte Einträge werden übergangen: eine Größenangabe darf keine
    Ansicht zum Absturz bringen.
    """
    schluessel = str(pfad)
    jetzt = time.time()
    alt = _GROESSE.get(schluessel)
    if alt and jetzt - alt[0] < ttl:
        return alt[1]
    gesamt = 0
    try:
        for p in Path(pfad).rglob("*"):
            try:
                if p.is_file():
                    gesamt += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    _GROESSE[schluessel] = (jetzt, gesamt)
    return gesamt


def store_status(cfg):
    """Zustand des Index: wie viel steckt drin, mit oder ohne Embeddings."""
    store = BASE / cfg["store_dir"]
    db = store / "corpus.db"
    vec = store / "vectors.npy"
    out = {"dir": str(store), "exists": db.exists(), "chunks": 0, "messages": 0,
           "features": [],
           "semantic": vec.exists(), "built_at": _mtime_iso(db), "model": None}
    if not out["exists"]:
        return out
    try:
        out.update(_zaehle(db))
    except sqlite3.Error as e:
        out["error"] = str(e)
    try:
        info = json.loads((store / "info.json").read_text(encoding="utf-8"))
        out["model"] = info.get("model")
    except (OSError, ValueError):
        pass
    return out


# --------------------------------------------------------------------------
# Schritte eines Laufs (rein – ohne Seiteneffekte, daher gut testbar)
# --------------------------------------------------------------------------
def script_argv(name, *args):
    """Kommandozeile für eines der Teilprogramme.

    Als Skript: python3 <name>.py …
    Gebündelt: die eigene ausführbare Datei mit "--run <name>" – dort gibt es
    keinen Python-Interpreter und keine .py-Dateien mehr, die Module stecken
    im Bündel und werden von run_bundled() importiert.
    """
    if name not in RUNNABLE:
        raise ValueError(f"Unbekanntes Teilprogramm: {name}")
    if FROZEN:
        return [sys.executable, "--run", name, *(str(a) for a in args)]
    return [sys.executable, str(RES / f"{name}.py"), *(str(a) for a in args)]


def run_bundled(name, argv):
    """Ein Teilprogramm im Bündel starten (Gegenstück zu script_argv).

    Die Teilprogramme lesen ihre Argumente selbst aus sys.argv, also wird die
    Liste vorher so hergerichtet, wie sie beim direkten Aufruf aussähe.
    """
    if name not in RUNNABLE:
        raise SystemExit(f"Unbekanntes Teilprogramm: {name}. "
                         f"Möglich: {', '.join(RUNNABLE)}")
    sys.argv = [f"{name}.py", *argv]
    importlib.import_module(name).main()


def _flag(value):
    """Schalter so schreiben, wie env_flag() in den Export-Skripten ihn liest."""
    return "1" if value else "0"


def calendar_file(cfg):
    return BASE / cfg["store_dir"] / "calendar.json"


def calendar_plan(cfg):
    """Was der Kalenderschritt in diesem Lauf zu tun hat.

    Liefert (noetig, mit_mails). Termine und Kontakte stammen ausschließlich
    aus dem Outlook-Export – ist keine der beiden Kategorien gewählt, gäbe es
    nichts aufzubauen. Die Wiederherstellung gelöschter Termine liest darüber
    hinaus jede einzelne .eml; das lohnt nur, wenn in diesem Lauf auch Mails
    geholt wurden. Wer nur Kontakte exportiert, wartete sonst minutenlang auf
    eine Auswertung, an der sich nichts geändert haben kann.
    """
    cats = set(_clean_categories(cfg.get("outlook_categories"),
                                 ["mail", "calendar", "contacts"]))
    return bool(cats & {"calendar", "contacts"}), "mail" in cats


def _auth_env(cfg):
    """Anmeldung an die Unterprozesse weiterreichen.

    Wie bei den Kategorien: die App führt ihre Konfiguration im Speicher und
    gibt sie als Umgebungsvariable mit, statt sich darauf zu verlassen, dass
    settings.py dieselbe Datei zur selben Zeit gleich liest.
    """
    env = {"GRAPH_AUTH": ("login" if str(cfg.get("auth_mode", "token")).lower()
                          == "login" else "token")}
    if str(cfg.get("folder_rules") or "").strip():
        env["FOLDER_RULES"] = cfg["folder_rules"]
    for schluessel, name in (("client_id", "GRAPH_CLIENT_ID"),
                             ("tenant", "GRAPH_TENANT")):
        wert = str(cfg.get(schluessel) or "").strip()
        if wert:
            env[name] = wert
    return env


def build_steps(cfg, outlook=False, teams=False, index=False, calendar=False,
                embeddings=True, search_page=False, token="", reconstruct=None,
                check=False, sync_folders=False, onedrive=False,
                sync_onedrive=False, check_onedrive=False):
    """Kommandozeilen für einen Lauf zusammenstellen.

    Die Export-Skripte bekommen die Auswahl über EXPORT_CATEGORIES – so laufen
    sie ohne jede Rückfrage, mit genau dem, was in der Oberfläche angehakt ist.
    Der Token geht als GRAPH_TOKEN mit, damit der Lauf nicht davon abhängt, in
    welchem Verzeichnis er gestartet wurde.
    """
    steps = []
    # None heißt „wie eingestellt“. Der Aufrufer setzt es nur, wenn er es besser
    # weiß – etwa weil in diesem Lauf gar keine Mails geholt wurden.
    if reconstruct is None:
        reconstruct = bool(cfg.get("calendar_reconstruct", True))
    base_env = {"PYTHONUNBUFFERED": "1", "EXPORT_WORKERS": str(cfg.get("workers", 4)),
                "EXPORT_PROGRESS": "1",   # Zahlen für den Balken, siehe progress.py
                **_auth_env(cfg)}
    if token:
        base_env["GRAPH_TOKEN"] = token

    if outlook:
        cats = _clean_categories(cfg["outlook_categories"], ["mail", "calendar", "contacts"])
        steps.append({
            "key": "outlook", "label": "job.step.outlook",
            "argv": script_argv("outlook_export", cfg["outlook_dir"]),
            "env": {**base_env, "EXPORT_CATEGORIES": ",".join(cats),
                    "INCLUDE_HIDDEN": _flag(cfg.get("include_hidden")),
                    # Immer setzen, auch leer: leer heißt "nichts auslassen",
                    # nicht gesetzt hieße "Vorgabe des Skripts".
                    "SKIP_FOLDERS": ",".join(cfg.get("skip_folders") or [])},
        })
    if onedrive:
        steps.append({
            "key": "onedrive", "label": "job.step.onedrive",
            "argv": script_argv("onedrive_export", cfg["onedrive_dir"]),
            "env": {**base_env,
                    # Immer setzen, auch leer: leer heißt "alles mitnehmen",
                    # nicht gesetzt hieße "was in app_config.json steht".
                    "ONEDRIVE_RULES": str(cfg.get("onedrive_rules") or ""),
                    "ONEDRIVE_MAX_MB": str(int(cfg.get("onedrive_max_mb") or 0))},
        })
    if teams:
        cats = _clean_categories(cfg["teams_categories"],
                                 ["1on1", "group", "meeting", "channels"])
        steps.append({
            "key": "teams", "label": "job.step.teams",
            "argv": script_argv("teams_export", cfg["teams_dir"]),
            "env": {**base_env, "EXPORT_CATEGORIES": ",".join(cats),
                    "EMBED_IMAGES": _flag(cfg.get("embed_images")),
                    "CACHE_IMAGES": _flag(cfg.get("cache_images")),
                    "REFRESH_CHANNELS": _flag(cfg.get("refresh_channels")),
                    "SKIP_EMPTY_CHATS": _flag(cfg.get("skip_empty_chats"))},
        })
    if index:
        argv = script_argv("rag_index", cfg["teams_dir"], cfg["outlook_dir"],
                           cfg["onedrive_dir"],
                           "--store", cfg["store_dir"], "--model", cfg["embed_model"],
                           "--ollama", cfg["ollama"],
                           "--batch", cfg.get("index_batch", 64))
        if not embeddings:
            argv.append("--no-embeddings")
        steps.append({
            "key": "index",
            "label": "job.step.index" if embeddings else "job.step.index.lexical",
            "argv": argv, "env": dict(base_env),
            # Hat der Export nichts Neues gebracht, indiziert dieser Schritt
            # denselben Bestand ein zweites Mal. "ziel" ist die Bedingung, unter
            # der das Auslassen sicher ist: nur wenn es schon einen Index gibt.
            "nur_bei_neuem": True, "ziel": BASE / cfg["store_dir"] / "corpus.db",
        })
    if calendar:
        # Termine und Kontakte aus dem Export zu lesen geht schnell. Teuer ist
        # nur die Wiederherstellung gelöschter Termine: dafür wird jede .eml
        # gelesen, bei 45.000 Mails ein paar Minuten. Deshalb ein eigener
        # Schritt mit Ergebnisdatei – und abschaltbar.
        argv = script_argv("combined_search", cfg["teams_dir"], cfg["outlook_dir"],
                           "--json", str(Path(cfg["store_dir"]) / "calendar.json"))
        if not reconstruct:
            argv.append("--no-reconstruct")
        steps.append({
            "key": "calendar",
            "label": "job.step.calendar" if reconstruct else "job.step.calendar.plain",
            "argv": argv, "env": dict(base_env),
            "nur_bei_neuem": True, "ziel": calendar_file(cfg),
        })
    if sync_onedrive:
        steps.append({
            "key": "onedrive_folders", "label": "job.step.folders",
            "argv": script_argv("onedrive_export", "--folders", cfg["onedrive_dir"]),
            "env": {**base_env,
                    "ONEDRIVE_RULES": str(cfg.get("onedrive_rules") or "")},
        })
    if sync_folders:
        steps.append({
            "key": "folders", "label": "job.step.folders",
            "argv": script_argv("outlook_export", "--folders", cfg["outlook_dir"]),
            "env": dict(base_env),
        })
    if check:
        steps.append({
            "key": "check", "label": "job.step.check",
            "argv": script_argv("outlook_export", "--check", cfg["outlook_dir"]),
            "env": dict(base_env),
        })
    if check_onedrive:
        steps.append({
            "key": "check_onedrive", "label": "job.step.check",
            "argv": script_argv("onedrive_export", "--check", cfg["onedrive_dir"]),
            "env": {**base_env,
                    "ONEDRIVE_RULES": str(cfg.get("onedrive_rules") or ""),
                    "ONEDRIVE_MAX_MB": str(int(cfg.get("onedrive_max_mb") or 0))},
        })
    if search_page:
        steps.append({
            "key": "search_page", "label": "job.step.searchpage",
            "argv": script_argv("combined_search", cfg["teams_dir"], cfg["outlook_dir"]),
            "env": dict(base_env),
        })
    return steps


def due_now(last_run, interval_minutes, now):
    """Ist der nächste geplante Lauf fällig? (last_run None = sofort)"""
    if last_run is None:
        return True
    return now >= last_run + max(1, int(interval_minutes)) * 60


# --------------------------------------------------------------------------
# Läufe ausführen: ein Job nach dem anderen, Ausgabe live in den Puffer
# --------------------------------------------------------------------------
_TOKEN_DEAD = re.compile(r"Token abgelaufen|Anmeldung fehlgeschlagen|InvalidAuthenticationToken")


class JobRunner:
    """Führt eine Folge von Schritten als Unterprozesse aus, einer zur Zeit.

    Ein Job nach dem anderen ist Absicht und keine Einschränkung: Export und
    Index schreiben in dieselben Ordner, und Graph drosselt ohnehin pro
    Postfach. Die Ausgabe landet zeilenweise in einem Ringpuffer, den die
    Oberfläche pollt.
    """

    MAX_LINES = 4000

    def __init__(self):
        self.lock = threading.Lock()
        self.lines = deque(maxlen=self.MAX_LINES)
        self.seq = 0
        self.thread = None
        self.proc = None
        self.cancelled = False
        self.job = None            # {"label", "steps", "step", "started"}
        self.last = None           # {"label", "ok", "finished", "detail"}
        self.token_expired = False
        # Summe der neu geschriebenen Stücke über alle Export-Schritte dieses
        # Laufs. None heißt „kein Export-Schritt hat sich geäußert“ – dann wird
        # nichts übersprungen, denn Unwissen ist kein Grund.
        self.neu = None

    # -- Protokoll ---------------------------------------------------------
    def log(self, text, level="info"):
        """Rohe Protokollzeile – so, wie die Export-Skripte sie ausgeben."""
        with self.lock:
            self.seq += 1
            self.lines.append({"n": self.seq, "level": level,
                               "t": datetime.now().strftime("%H:%M:%S"),
                               "text": text})

    def logk(self, key, level="info", **vars):
        """Protokollzeile als Textschlüssel; übersetzt wird erst beim Anzeigen.

        Getrennt von log(), damit nichts geraten werden muss: eine Skriptzeile
        kann aussehen wie ein Schlüssel. Und erst beim Anzeigen zu übersetzen
        heißt, dass ein Sprachwechsel auch das vorhandene Protokoll umstellt,
        statt es in der Sprache von damals einzufrieren.
        """
        self.log({"k": key, "v": vars}, level)

    def log_since(self, since):
        with self.lock:
            return [ln for ln in self.lines if ln["n"] > since], self.seq

    # -- Zustand -----------------------------------------------------------
    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def snapshot(self):
        job = dict(self.job) if self.job else None
        return {"busy": self.busy, "job": job, "last": self.last,
                "token_expired": self.token_expired, "seq": self.seq}

    # -- Steuerung ---------------------------------------------------------
    def start(self, steps, label):
        if self.busy:
            return False
        if not steps:
            return False
        self.cancelled = False
        self.token_expired = False
        self.neu = None
        self.job = {"label": label, "steps": [s["label"] for s in steps],
                    "step": steps[0]["label"], "index": 0, "progress": None,
                    "started": datetime.now().isoformat(timespec="seconds")}
        self.thread = threading.Thread(target=self._run, args=(steps, label), daemon=True)
        self.thread.start()
        return True

    def cancel(self):
        self.cancelled = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            self.logk("srv.job.cancel", "warn")
            return True
        return False

    def _run(self, steps, label):
        self.logk("srv.job.start", "head", label=label)
        ok = True
        detail = ""
        for i, step in enumerate(steps):
            if self.cancelled:
                ok, detail = False, {"k": "srv.job.cancelled", "v": {}}
                break
            self.job = {**self.job, "step": step["label"], "index": i,
                        "progress": None}      # jeder Schritt zählt bei null an
            if self._erspart(step):
                self.logk("srv.job.skipped", "info", step=step["label"])
                continue
            self.logk("srv.job.step", "head", step=step["label"])
            code = self._exec(step)
            if code != 0:
                ok = False
                detail = ({"k": "srv.job.aborted", "v": {"step": step["label"]}}
                          if self.cancelled else
                          {"k": "srv.job.exitcode",
                           "v": {"step": step["label"], "code": code}})
                self.logk("srv.job.stepfail", "err", detail=detail)
                break
            self.logk("srv.job.stepdone", "ok", step=step["label"])
        if ok:
            self.logk("srv.job.done", "ok", label=label)
        self.last = {"label": label, "ok": ok, "detail": detail,
                     "finished": datetime.now().isoformat(timespec="seconds")}
        self.job = None
        self.proc = None

    def _erspart(self, step):
        """Darf dieser Schritt entfallen, weil der Export nichts Neues brachte?

        Aus der Praxis: ein Lauf mit nur „Kontakte“ meldete „Neu exportiert: 0“
        und indizierte danach zwei Minuten lang denselben Bestand.

        Drei Bedingungen, jede einzeln nötig:
          * Der Schritt ist überhaupt dafür vorgesehen (Index, Kalender).
          * Es lief ein Export-Schritt, der sich geäußert hat, und er brachte
            nichts. Ohne Meldung wird gearbeitet – Unwissen ist kein Grund.
          * Das Ergebnis existiert bereits. Sonst gäbe es nach dem ersten Lauf
            mit unverändertem Bestand nie einen Index.
        """
        if not step.get("nur_bei_neuem") or self.neu is None or self.neu > 0:
            return False
        ziel = step.get("ziel")
        return bool(ziel and Path(ziel).exists())

    def _exec(self, step):
        env = {**os.environ, **step.get("env", {})}
        try:
            self.proc = subprocess.Popen(
                step["argv"], cwd=str(BASE), env=env, bufsize=0,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
        except OSError as e:
            self.logk("srv.job.spawnfail", "err", error=str(e))
            return -1
        for line in _stream_lines(self.proc.stdout):
            stand = progress.lies(line)
            if stand is not None:
                # Zahlen für den Balken – im Protokoll wären sie nur Rauschen.
                if self.job:
                    self.job = {**self.job, "progress": stand}
                continue
            fazit = progress.lies_ergebnis(line)
            if fazit is not None:
                self.neu = (self.neu or 0) + fazit["neu"]
                continue
            if _TOKEN_DEAD.search(line):
                self.token_expired = True
            self.log(line)
        return self.proc.wait()


def _stream_lines(stream):
    """Zeilen aus einem Prozess-Stream, auch bei Fortschritt per \\r.

    Die Skripte überschreiben Fortschrittszeilen mit "\\r" statt sie mit "\\n"
    abzuschließen (rag_index.py: "… 500/12000 eingebettet"). readline() würde
    darauf bis zum Ende des Schritts warten, deshalb wird roh gelesen und an
    beiden Zeichen getrennt.
    """
    buf = ""
    while True:
        try:
            chunk = stream.read(4096)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        buf += chunk.decode("utf-8", errors="replace")
        parts = re.split(r"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            if p.strip():
                yield p.rstrip()
    if buf.strip():
        yield buf.rstrip()


# --------------------------------------------------------------------------
# Zeitplan: läuft nur, solange die App offen ist
# --------------------------------------------------------------------------
class Scheduler(threading.Thread):
    """Stößt in festem Abstand Export + Index an.

    Bewusst an die Laufzeit der App gebunden (kein launchd/Task Scheduler): der
    Token wird von Hand geholt und ist typischerweise etwa eine Stunde gültig –
    ein Zeitplan, der im Hintergrund ohne offene Oberfläche weiterläuft, würde
    vor allem abgelaufene Token produzieren, die niemand sieht.
    """

    TICK = 10                       # Sekunden zwischen zwei Fälligkeitsprüfungen

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.last_run = None
        self.last_result = None

    @property
    def plan(self):
        return self.app.cfg["schedule"]

    def next_due(self):
        if not self.plan.get("enabled"):
            return None
        if self.last_run is None:
            return time.time()
        return self.last_run + max(1, int(self.plan.get("interval_minutes", 60))) * 60

    def reset(self):
        """Nach einer Änderung am Plan: Abstand ab jetzt neu zählen."""
        self.last_run = time.time() if self.plan.get("enabled") else None

    def run(self):
        while not self.stop_event.wait(self.TICK):
            try:
                self._tick()
            except Exception as e:
                self.app.jobs.logk("srv.sched.error", "err",
                                   error=f"{type(e).__name__}: {e}")

    def _tick(self):
        plan = self.plan
        if not plan.get("enabled") or self.app.jobs.busy:
            return
        if not due_now(self.last_run, plan.get("interval_minutes", 60), time.time()):
            return
        self.last_run = time.time()
        token = read_token()
        st = token_status(token)
        if not st["valid"]:
            self.app.jobs.logk("srv.sched.notoken", "warn")
            self.app.jobs.token_expired = True
            return
        # Kalender nur, wenn Outlook mitläuft – die Daten dafür kommen
        # ausschließlich von dort – und nur, wenn die Auswahl etwas hergibt.
        noetig, mit_mails = calendar_plan(self.app.cfg)
        kalender = bool(plan.get("outlook", True) and plan.get("calendar", True) and noetig)
        ok, why = self.app.launch(outlook=plan.get("outlook", True),
                                  teams=plan.get("teams", True),
                                  index=plan.get("index", True),
                                  calendar=kalender,
                                  reconstruct=None if mit_mails else False,
                                  label="job.scheduled")
        if not ok:
            self.app.jobs.logk("srv.sched.skipped", "warn", why=why)


# --------------------------------------------------------------------------
# MCP-Server als Unterprozess
# --------------------------------------------------------------------------
def mcp_client_config(cfg, port):
    """Fertige Einträge für Claude Code (HTTP) und Claude Desktop (stdio).

    Serverseitig gebaut, weil nur hier bekannt ist, wie das Teilprogramm
    aufzurufen ist (Skript oder gebündelte Datei) und wo die Daten liegen. Die
    stdio-Variante bekommt absolute Pfade: Claude startet sie in einem
    unbekannten Arbeitsverzeichnis.
    """
    argv = script_argv("mcp_server", "--transport", "stdio",
                       "--store", str(BASE / cfg["store_dir"]),
                       "--teams", str(BASE / cfg["teams_dir"]),
                       "--outlook", str(BASE / cfg["outlook_dir"]))
    return {
        "http": {"mcpServers": {"office365-export": {
            "type": "http", "url": f"http://127.0.0.1:{port}/mcp"}}},
        "stdio": {"mcpServers": {"office365-export": {
            "command": argv[0], "args": argv[1:]}}},
    }


class McpProcess:
    """Startet/stoppt mcp_server.py und sammelt dessen Ausgabe im Protokoll."""

    def __init__(self, jobs):
        self.jobs = jobs
        self.proc = None
        self.port = None
        self.error = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self, cfg):
        return {"running": self.running, "port": self.port or cfg["mcp_port"],
                "url": f"http://127.0.0.1:{self.port or cfg['mcp_port']}/mcp",
                "error": self.error, "config": mcp_client_config(cfg,
                                                                 self.port or cfg["mcp_port"])}

    def start(self, cfg):
        if self.running:
            return True, {"k": "srv.mcp.running", "v": {}}
        db = BASE / cfg["store_dir"] / "corpus.db"
        if not db.exists():
            self.error = {"k": "srv.mcp.noindex", "v": {}}
            return False, self.error
        argv = script_argv("mcp_server",
                           "--store", cfg["store_dir"], "--teams", cfg["teams_dir"],
                           "--outlook", cfg["outlook_dir"],
                           "--embed-model", cfg["embed_model"],
                           "--ollama", cfg["ollama"], "--port", str(cfg["mcp_port"]))
        try:
            self.proc = subprocess.Popen(
                argv, cwd=str(BASE), env={**os.environ, "PYTHONUNBUFFERED": "1"},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0)
        except OSError as e:
            self.error = {"k": "srv.mcp.spawnfail", "v": {"error": str(e)}}
            return False, self.error
        self.port = cfg["mcp_port"]
        self.error = None
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self.jobs.logk("srv.mcp.started", "ok", port=self.port)
        return True, {"k": "srv.mcp.startok", "v": {}}

    def _pump(self, proc):
        for line in _stream_lines(proc.stdout):
            self.jobs.log(f"[MCP] {line}")
        code = proc.wait()
        if proc is self.proc and code not in (0, -15):
            self.error = {"k": "srv.mcp.exit", "v": {"code": code}}
            self.jobs.logk("srv.mcp.exit", "err", code=code)

    def stop(self):
        if not self.running:
            return False
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.jobs.logk("srv.mcp.stopped", "warn")
        return True


# --------------------------------------------------------------------------
# Eingebettete Suche – nutzt die Rangfolge des MCP-Servers
# --------------------------------------------------------------------------
class SearchBridge:
    """Bindet mcp_server.py als Bibliothek ein statt die Suche nachzubauen.

    Die Tool-Funktionen dort sind ganz normale Funktionen (der Dekorator meldet
    sie nur zusätzlich am MCP-Server an) und arbeiten auf mcp_server.STATE. Wir
    füllen STATE genauso wie dessen main() und rufen sie direkt auf – dieselbe
    hybride Rangfolge, ohne einen zweiten Suchpfad zu pflegen.
    """

    def __init__(self):
        self.module = None
        self.stamp = None
        self.error = None
        self.lock = threading.Lock()

    def _store_stamp(self, cfg):
        store = BASE / cfg["store_dir"]
        out = []
        for name in ("corpus.db", "vectors.npy"):
            p = store / name
            try:
                out.append((name, p.stat().st_mtime_ns, p.stat().st_size))
            except OSError:
                out.append((name, None, None))
        return tuple(out)

    def ensure(self, cfg):
        """STATE (neu) aufsetzen, wenn der Index sich geändert hat."""
        with self.lock:
            stamp = self._store_stamp(cfg)
            if self.module is not None and stamp == self.stamp:
                return self.module
            db = BASE / cfg["store_dir"] / "corpus.db"
            if not db.exists():
                self.error = {"k": "srv.noindex", "v": {}}
                self.module = None
                return None
            try:
                import mcp_server
            except ImportError as e:
                self.error = {"k": "srv.nomcpmodule", "v": {"error": str(e)}}
                self.module = None
                return None
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                con.close()
                np, V = mcp_server._open_vectors(str(BASE / cfg["store_dir"]), n)
            except sqlite3.Error as e:
                self.error = {"k": "srv.badindex", "v": {"error": str(e)}}
                self.module = None
                return None
            mcp_server.STATE.update(
                db=str(db), V=V, np=np, semantic=(np is not None),
                vector_dtype=str(V.dtype) if V is not None else None,
                teams_dir=str(BASE / cfg["teams_dir"]),
                outlook_dir=str(BASE / cfg["outlook_dir"]),
                embed_model=cfg["embed_model"], ollama=cfg["ollama"])
            self.module, self.stamp, self.error = mcp_server, stamp, None
            return mcp_server


# --------------------------------------------------------------------------
# Anwendung: hält Konfiguration, Läufe, Zeitplan, MCP und Suche zusammen
# --------------------------------------------------------------------------
class App:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.jobs = JobRunner()
        self.mcp = McpProcess(self.jobs)
        self.search = SearchBridge()
        self.scheduler = Scheduler(self)
        self._ollama_cache = (0.0, None)
        self._calendar_cache = None      # (Kennung, roh, gzip)
        self.device_login = None         # laufende Gerätecode-Anmeldung
        self._update = {"status": "off", "current": version.VERSION,
                        "latest": None, "url": None, "newer": False,
                        "ahead": False, "error": None}

    # -- abgeleiteter Zustand ---------------------------------------------
    def selected_categories(self):
        return (_clean_categories(self.cfg["outlook_categories"],
                                  ["mail", "calendar", "contacts"])
                + _clean_categories(self.cfg["teams_categories"],
                                    ["1on1", "group", "meeting", "channels"]))

    def ollama(self, force=False):
        """Ergebnis kurz zwischenspeichern – der Status wird im Sekundentakt abgefragt."""
        age, cached = self._ollama_cache
        if not force and cached is not None and time.time() - age < 10:
            return cached
        res = check_ollama(self.cfg["ollama"], self.cfg["embed_model"],
                           self.cfg.get("chat_model"))
        self._ollama_cache = (time.time(), res)
        return res

    def check_updates(self, blockierend=False):
        """Einmal nachsehen, ob es ein neueres Release gibt.

        Im Hintergrund, weil der Start nicht auf eine Netzantwort warten soll –
        wer offline ist, will die App trotzdem sofort sehen.
        """
        def lauf():
            self._update = updates.check(version.VERSION, version.REPO,
                                         enabled=bool(self.cfg.get("update_check", True)))
            if self._update["newer"]:
                self.jobs.logk("srv.update.available", "info",
                               version=self._update["latest"],
                               url=self._update["url"] or version.RELEASES_URL)
            # Alles andere bleibt still: kein Release, kein Netz oder abgeschaltet
            # sind keine Ereignisse, mit denen man jemanden behelligt.
        if blockierend:
            lauf()
        else:
            threading.Thread(target=lauf, daemon=True).start()
        return self._update

    def log_token_state(self):
        """Beim Start einmal sagen, woran man ist.

        Der Assistent geht nur noch auf, wenn etwas fehlt – ohne diese Zeile
        wäre der häufige Fall (Token liegt da und ist gültig) völlig stumm, und
        niemand wüsste, wie lange er noch trägt.
        """
        st = token_status(read_token(), needed=self.selected_categories())
        if not st["present"]:
            return self.jobs.logk("srv.token.none", "warn")
        if st["expired"]:
            return self.jobs.logk("srv.token.expired", "warn")
        # Vier vollständige Sätze statt zusammengesetzter Bruchstücke: was in
        # der einen Sprache aneinandergehängt funktioniert, ergibt in der
        # nächsten keinen Satz mehr.
        konto, minuten = st["account"], st["expires_in_minutes"]
        schluessel = ("srv.token.found" if konto and minuten is not None else
                      "srv.token.found.unknown" if konto else
                      "srv.token.found.nowho" if minuten is not None else
                      "srv.token.found.plain")
        self.jobs.logk(schluessel, "ok", account=konto or "", minutes=minuten)
        if st["missing"]:
            self.jobs.logk("srv.token.scopes", "warn", list=", ".join(st["missing"]))
        return None

    def status(self):
        token = read_token()
        tok = token_status(token, needed=self.selected_categories())
        oll = self.ollama()
        store = store_status(self.cfg)
        jobs = self.jobs.snapshot()
        plan = self.cfg["schedule"]
        nxt = self.scheduler.next_due()
        # Der Assistent geht nur auf, wenn er gebraucht wird: kein Token, ein
        # abgelaufener, oder einer, den ein Lauf gerade als tot erkannt hat.
        # Ein noch gültiger Token wird nicht angetastet – seine Laufzeit hängt
        # am Tenant und reicht durchaus über einen Arbeitstag. Wer ihn trotzdem
        # ersetzen will, klickt oben auf die Token-Kachel.
        wizard = None
        if not tok["valid"] or jobs["token_expired"]:
            wizard = "token"
        elif not oll["running"] or not oll["has_model"]:
            wizard = "ollama"
        return {
            "token": tok,
            "ollama": oll,
            "ollama_hint": ollama_hint(),
            "store": store,
            "calendar": {"exists": calendar_file(self.cfg).exists(),
                         "built_at": _mtime_iso(calendar_file(self.cfg))},
            "exports": export_status(self.cfg),
            "jobs": jobs,
            "mcp": self.mcp.status(self.cfg),
            "config": self.cfg,
            "schedule_next": (datetime.fromtimestamp(nxt).isoformat(timespec="seconds")
                              if nxt else None),
            "schedule_enabled": bool(plan.get("enabled")),
            "wizard": wizard,
            "data_dir": str(BASE),
            "data_dir_default": str(standard_data_dir()),
            "frozen": FROZEN,
            "update": dict(self._update, releases_url=version.RELEASES_URL),
            "skip_folders_default": sorted(SKIP_FOLDERS_DEFAULT),
            "graph_explorer": GRAPH_EXPLORER,
            "scopes_needed": sorted({SCOPE_FOR[c] for c in self.selected_categories()
                                     if c in SCOPE_FOR} | {"User.Read"}),
            "scope_queries": SCOPE_QUERY,
            "auth": self.auth_status(),
            "folders": folders.zusammenfassung(
                folders.lade(BASE / self.cfg["outlook_dir"]),
                auswahlregeln(self.cfg)),
            "folders_onedrive": folders.zusammenfassung(
                folders.lade(BASE / self.cfg["onedrive_dir"]),
                folders.lies_regeln(self.cfg.get("onedrive_rules") or "")),
        }

    def auth_modus(self):
        """Die App führt ihre Konfiguration selbst – nicht über settings.py.

        settings.py liest app_config.json und ist die Quelle für die Skripte im
        Terminal. Die App hat ihr cfg schon im Speicher; beides gleichzeitig zu
        befragen hieße, zwei Wahrheiten für dieselbe Einstellung zu pflegen.
        Weitergereicht wird sie an die Unterprozesse als Umgebungsvariable.
        """
        return "login" if str(self.cfg.get("auth_mode", "token")).lower() == "login" \
            else "token"

    def auth_ziel(self):
        """(Client-ID, Tenant) – leer heißt Microsofts öffentliche Anwendung."""
        return (str(self.cfg.get("client_id") or "").strip() or auth.STANDARD_CLIENT_ID,
                str(self.cfg.get("tenant") or "").strip() or auth.STANDARD_TENANT)

    def auth_status(self):
        """Wie sich die App anmeldet – und ob das gerade trägt.

        `signed_in` fragt nur den Cache und öffnet dabei nichts: die Kachel soll
        den Zustand anzeigen können, ohne ungefragt eine Anmeldung anzustoßen.
        """
        klient, mandant = self.auth_ziel()
        konto = auth.angemeldet(client=klient, mandant=mandant)
        laeuft = self.device_login
        return {
            "mode": self.auth_modus(),
            "signed_in": bool(konto),
            "account": konto if isinstance(konto, str) else None,
            "own_registration": (klient, mandant) != (auth.STANDARD_CLIENT_ID,
                                                      auth.STANDARD_TENANT),
            "client_id": klient,
            "tenant": mandant,
            "default_client_id": auth.STANDARD_CLIENT_ID,
            # Läuft gerade eine Gerätecode-Anmeldung? Dann Code und Adresse.
            "device": dict(laeuft) if laeuft else None,
        }

    # -- Aktionen ----------------------------------------------------------
    def calendar_payload(self):
        """Kalenderdaten roh und gzip-gepackt, gepuffert bis die Datei sich ändert.

        Rund 5 MB JSON – neu einlesen und packen bei jedem Tab-Wechsel wäre
        Verschwendung, gepackt gehen daraus 0,75 MB über die Leitung.
        """
        p = calendar_file(self.cfg)
        try:
            st = p.stat()
        except OSError:
            return None, None
        stamp = (st.st_mtime_ns, st.st_size)
        if self._calendar_cache and self._calendar_cache[0] == stamp:
            return self._calendar_cache[1], self._calendar_cache[2]
        roh = p.read_bytes()
        self._calendar_cache = (stamp, roh, gzip.compress(roh, 6))
        return self._calendar_cache[1], self._calendar_cache[2]

    def launch(self, outlook=False, teams=False, index=False, calendar=False,
               embeddings=None, search_page=False, label="Lauf", reconstruct=None,
               check=False, sync_folders=False, onedrive=False,
               sync_onedrive=False, check_onedrive=False):
        if self.jobs.busy:
            return False, {"k": "srv.busy", "v": {}}
        gewaehlt = embeddings is not None      # ausdrücklich gesetzt vs. selbst ermittelt
        if embeddings is None:
            embeddings = self.ollama()["running"] and self.ollama()["has_model"]
        # Die Prüfung fragt das Postfach ab, braucht also denselben Zugang.
        braucht_zugang = (outlook or teams or onedrive or check
                          or sync_folders or sync_onedrive or check_onedrive)
        token = read_token() if braucht_zugang else ""
        # Im Login-Modus trägt der Cache auf der Platte – dann ist ein
        # eingefügter Schlüssel nicht nötig, und sein Fehlen darf keinen Lauf
        # verhindern.
        if braucht_zugang and not token and self.auth_modus() != "login":
            return False, {"k": "srv.notoken", "v": {}}
        if index and not embeddings:
            self.jobs.logk("srv.lexical.choice" if gewaehlt
                           else "srv.lexical.noollama", "warn")
        steps = build_steps(self.cfg, outlook=outlook, teams=teams, index=index,
                            calendar=calendar, embeddings=embeddings,
                            search_page=search_page, token=token,
                            reconstruct=reconstruct, check=check,
                            sync_folders=sync_folders, onedrive=onedrive,
                            sync_onedrive=sync_onedrive,
                            check_onedrive=check_onedrive)
        if not steps:
            return False, {"k": "srv.nothing", "v": {}}
        if not self.jobs.start(steps, label):
            return False, {"k": "srv.nostart", "v": {}}
        return True, {"k": "srv.mcp.startok", "v": {}}

    def login_starten(self):
        """Gerätecode holen und im Hintergrund auf die Zustimmung warten.

        Ein natives Anmeldefenster gibt es hier nicht – die App hat keins. Die
        Seite zeigt stattdessen den Code; dieser Faden wartet, bis Microsoft
        bestätigt, und legt das Ergebnis in den Cache auf der Platte.
        """
        if self.device_login and not self.device_login.get("done"):
            return True, self.device_login          # schon einer offen
        scopes = sorted({auth.RES + s for s in
                         ({SCOPE_FOR[c] for c in self.selected_categories()
                           if c in SCOPE_FOR} | {"User.Read"})})
        klient, mandant = self.auth_ziel()
        try:
            vorgang = auth.DeviceLogin(scopes, client=klient, mandant=mandant)
            daten = vorgang.start()
        except Exception as e:                      # noqa: BLE001
            self.jobs.logk("srv.login.failed", "err", detail=f"{type(e).__name__}: {e}")
            return False, {"error": f"{type(e).__name__}: {e}"}
        self.device_login = {**daten, "done": False, "ok": False}
        self.jobs.logk("srv.login.code", code=daten["code"], url=daten["url"])

        def warten():
            ok, meldung = vorgang.warten()
            self.device_login = {**self.device_login, "done": True, "ok": ok,
                                 "error": None if ok else meldung}
            if ok:
                self.jobs.logk("srv.login.ok", "ok")
            else:
                self.jobs.logk("srv.login.failed", "err", detail=meldung)
        threading.Thread(target=warten, daemon=True).start()
        return True, self.device_login

    def abmelden(self):
        """Refresh Token verwerfen. Der Schlüssel bleibt, wo er ist."""
        auth.cache_leeren()
        self.device_login = None
        self.jobs.logk("srv.logout")
        return True

    def autostart_mcp(self):
        """Beim App-Start: MCP hochfahren, wenn ein Index da ist.

        Genau der Fall aus der Anforderung „ohne Ollama läuft der MCP-Server
        trotzdem“: der Server rankt dann rein lexikalisch weiter.
        """
        if not self.cfg.get("mcp_autostart"):
            return
        ok, why = self.mcp.start(self.cfg)
        if not ok:
            self.jobs.logk("srv.mcp.notstarted", "warn", why=why)

    def shutdown(self):
        self.scheduler.stop_event.set()
        self.jobs.cancel()
        self.mcp.stop()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "office365-export-app"
    protocol_version = "HTTP/1.1"
    app = None                 # von serve() gesetzt
    allowed_hosts = ()

    def log_message(self, fmt, *args):
        pass                    # kein Zugriffsprotokoll auf stdout

    # -- Hilfen ------------------------------------------------------------
    def _host_ok(self):
        """Nur die eigene Loopback-Adresse akzeptieren.

        Ohne diese Prüfung könnte eine beliebige Webseite über einen auf
        127.0.0.1 zeigenden DNS-Namen (Rebinding) mit dem Server sprechen – und
        der liefert den kompletten Mail- und Chatbestand aus.
        """
        host = (self.headers.get("Host") or "").lower()
        return host in self.allowed_hosts

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 4 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- Routen ------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "Nur über http://127.0.0.1 erreichbar.",
                              "text/plain; charset=utf-8")
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        one = {k: v[0] for k, v in q.items()}
        app = self.app
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, self._page(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self._json(app.status())
            if u.path == "/api/log":
                lines, seq = app.jobs.log_since(int(one.get("since", 0) or 0))
                return self._json({"lines": lines, "seq": seq})
            if u.path == "/api/search":
                return self._json(self._search(one))
            if u.path == "/api/thread":
                return self._json(self._thread(one))
            if u.path == "/api/folders":
                return self._json(self._folders(one))
            if u.path == "/api/people":
                return self._json(self._people(one))
            if u.path == "/api/document":
                return self._json(self._document(one))
            if u.path == "/api/analytics":
                return self._json({
                    **kennzahlen(app.cfg),
                    "vollstaendigkeit": lies_bericht(app.cfg),
                    "vollstaendigkeit_onedrive": lies_bericht(app.cfg, "onedrive_dir")})
            if u.path == "/api/calendar":
                return self._calendar()
            if u.path == "/source":
                return self._source(one)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send(404, json.dumps({"error": "Unbekannter Pfad"}))

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "Nur über http://127.0.0.1 erreichbar.",
                              "text/plain; charset=utf-8")
        u = urlsplit(self.path)
        app = self.app
        data = self._body()
        try:
            if u.path == "/api/token":
                return self._json(self._save_token(data))
            if u.path == "/api/wizard-seen":
                # "Später": die Merkung eines totgelaufenen Tokens zurücksetzen,
                # sonst ginge der Assistent bei jedem Statusabruf wieder auf.
                app.jobs.token_expired = False
                return self._json({"ok": True})
            if u.path == "/api/run":
                mit_outlook = bool(data.get("outlook"))
                kalender = bool(data.get("calendar"))
                rekonstruktion = None            # None: wie eingestellt
                if kalender and mit_outlook:
                    # Teil eines Exportlaufs: der Schritt richtet sich danach,
                    # was überhaupt geholt wird. Der Knopf „Kalender & Kontakte
                    # aufbauen“ kommt ohne outlook und bleibt unangetastet.
                    kalender, mit_mails = calendar_plan(app.cfg)
                    if not mit_mails:
                        rekonstruktion = False
                ok, why = app.launch(
                    outlook=mit_outlook, teams=bool(data.get("teams")),
                    index=bool(data.get("index")), calendar=kalender,
                    search_page=bool(data.get("search_page")),
                    check=bool(data.get("check")),
                    sync_folders=bool(data.get("sync_folders")),
                    onedrive=bool(data.get("onedrive")),
                    sync_onedrive=bool(data.get("sync_onedrive")),
                    check_onedrive=bool(data.get("check_onedrive")),
                    embeddings=data.get("embeddings"),
                    label=str(data.get("label") or "job.export"),
                    reconstruct=rekonstruktion)
                return self._json({"ok": ok, "message": why}, 200 if ok else 409)
            if u.path == "/api/login":
                ok, daten = app.login_starten()
                return self._json({"ok": ok, "device": daten}, 200 if ok else 500)
            if u.path == "/api/data-dir":
                ziel, fehler = pruefe_datenordner(data.get("path"))
                if fehler:
                    return self._json({"ok": False, "message": fehler}, 400)
                if not schreibe_zeiger(ziel):
                    return self._json({"ok": False,
                                       "message": {"k": "srv.datadir.unwritable",
                                                   "v": {"detail": str(zeiger_datei())}}}, 500)
                app.jobs.logk("srv.datadir.set", "warn", path=str(ziel))
                # BASE steht seit dem Start fest und geht als Arbeitsverzeichnis an
                # jeden Unterprozess. Ihn mitten im Betrieb umzuhängen – womöglich
                # während ein Export läuft – wäre grob fahrlässig.
                return self._json({"ok": True, "path": str(ziel),
                                   "restart": str(ziel) != str(BASE)})
            if u.path == "/api/folder-plan":
                return self._json(self._ordnerplan(data))
            if u.path == "/api/logout":
                return self._json({"ok": app.abmelden()})
            if u.path == "/api/cancel":
                return self._json({"ok": app.jobs.cancel()})
            if u.path == "/api/config":
                return self._json(self._save_config(data))
            if u.path == "/api/schedule":
                return self._json(self._save_schedule(data))
            if u.path == "/api/mcp":
                return self._json(self._mcp(data))
            if u.path == "/api/ollama-recheck":
                return self._json(app.ollama(force=True))
            if u.path == "/api/answer":
                return self._answer(data)
            if u.path == "/api/update-check":
                return self._json(app.check_updates(blockierend=True))
            if u.path == "/api/quit":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self._json({"ok": True})
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send(404, json.dumps({"error": "Unbekannter Pfad"}))

    # -- Route-Implementierungen ------------------------------------------
    def _page(self):
        """Die Oberfläche mit ihren Texten ausliefern.

        Die Sprache steht damit schon beim ersten Aufbau fest – nachzuladen
        hieße, dass kurz die falsche Sprache zu sehen ist.
        """
        code = i18n.negotiate(self.app.cfg.get("language"),
                              self.headers.get("Accept-Language"), RES)
        nutzlast = json.dumps({"lang": code, "strings": i18n.strings(code, RES),
                               "languages": i18n.available(RES)},
                              ensure_ascii=False).replace("<", "\\u003c")
        return PAGE.replace("/*__I18N__*/", nutzlast)

    def _save_token(self, data):
        token = normalize_token(data.get("token"))
        if not token:
            return {"ok": False, "message": {"k": "srv.token.empty", "v": {}}}
        if len(token) < 40:
            return {"ok": False, "message": {"k": "srv.token.short", "v": {}}}
        write_token(token)
        self.app.jobs.token_expired = False
        st = token_status(token, needed=self.app.selected_categories())
        if st["expired"]:
            return {"ok": False, "token": st,
                    "message": {"k": "srv.token.stale", "v": {}}}
        msg = ({"k": "srv.token.saved.scopes", "v": {"list": ", ".join(st["missing"])}}
               if st["missing"] else {"k": "srv.token.saved", "v": {}})
        self.app.jobs.log(msg, "warn" if st["missing"] else "ok")
        return {"ok": True, "message": msg, "token": st}

    def _save_config(self, data):
        cfg = self.app.cfg
        if "outlook_categories" in data:
            cfg["outlook_categories"] = _clean_categories(
                data["outlook_categories"], ["mail", "calendar", "contacts"])
        if "teams_categories" in data:
            cfg["teams_categories"] = _clean_categories(
                data["teams_categories"], ["1on1", "group", "meeting", "channels"])
        for key in ("teams_dir", "outlook_dir", "store_dir", "embed_model",
                    "chat_model", "ollama"):
            if key in data and str(data[key]).strip():
                cfg[key] = str(data[key]).strip()
        # Grenzen, damit eine vertippte Zahl den nächsten Lauf nicht lahmlegt:
        # Graph erlaubt 4 gleichzeitige Anfragen pro Postfach, alles darüber
        # erzeugt vor allem Drosselung; Ports jenseits von 65535 gibt es nicht.
        for key, low, high in (("workers", 1, 8), ("mcp_port", 1024, 65535),
                               ("index_batch", 1, 512), ("answer_sources", 1, 20),
                               ("search_results", 5, 100)):
            if key in data:
                try:
                    cfg[key] = max(low, min(high, int(data[key])))
                except (TypeError, ValueError):
                    pass
        for key in ("mcp_autostart", "update_check", "embed_images", "cache_images",
                    "refresh_channels", "skip_empty_chats", "include_hidden",
                    "calendar_reconstruct"):
            if key in data:
                cfg[key] = bool(data[key])
        if "folder_rules" in data:
            cfg["folder_rules"] = folders.schreibe_regeln(
                folders.lies_regeln(str(data["folder_rules"] or "")))
        if "onedrive_rules" in data:
            cfg["onedrive_rules"] = folders.schreibe_regeln(
                folders.lies_regeln(str(data["onedrive_rules"] or "")))
        if "skip_folders" in data:
            cfg["skip_folders"] = _clean_folders(data["skip_folders"])
        if "auth_mode" in data:
            # Alles Unbekannte wird zum Schlüssel-Modus – dem Weg, der ohne
            # Rückfrage bei der IT funktioniert.
            cfg["auth_mode"] = ("login" if str(data["auth_mode"]).strip().lower()
                                == "login" else "token")
        for key in ("client_id", "tenant"):
            if key in data:
                cfg[key] = str(data[key] or "").strip()
        if "language" in data:
            # Nur bekannte Codes – ein Tippfehler sonst und die Oberfläche
            # spräche für immer die Notsprache.
            gewuenscht = str(data["language"] or "auto").strip().lower()
            erlaubt = {e["code"] for e in i18n.available(RES)} | {"auto"}
            if gewuenscht in erlaubt:
                cfg["language"] = gewuenscht
        save_config(cfg)
        return {"ok": True, "config": cfg}

    def _save_schedule(self, data):
        plan = self.app.cfg["schedule"]
        for key in ("enabled", "outlook", "teams", "index", "calendar"):
            if key in data:
                plan[key] = bool(data[key])
        if "interval_minutes" in data:
            try:
                plan["interval_minutes"] = max(5, int(data["interval_minutes"]))
            except (TypeError, ValueError):
                pass
        save_config(self.app.cfg)
        self.app.scheduler.reset()
        self.app.jobs.logk("srv.sched.state", "info", min=plan["interval_minutes"],
                           state={"k": "srv.sched.on" if plan["enabled"]
                                  else "srv.sched.off", "v": {}})
        return {"ok": True, "schedule": plan,
                "next": self.app.scheduler.next_due()}

    def _mcp(self, data):
        action = str(data.get("action") or "").lower()
        if action == "start":
            ok, why = self.app.mcp.start(self.app.cfg)
            return {"ok": ok, "message": why, "mcp": self.app.mcp.status(self.app.cfg)}
        if action == "stop":
            self.app.mcp.stop()
            return {"ok": True, "mcp": self.app.mcp.status(self.app.cfg)}
        return {"ok": False, "message": {"k": "srv.mcp.badaction", "v": {}}}

    def _answer(self, data):
        """Aus den Treffern einer Suche eine Antwort formulieren lassen.

        Gesucht wird mit derselben Funktion wie im Reiter daneben – die Antwort
        sieht also genau die Treffer, die auch in der Liste stehen. Ein zweites
        Retrieval hier hieße, dass sie Dinge zitieren könnte, die niemand
        nachschlagen kann.

        Die Antwort läuft stückweise heraus (eine JSON-Zeile je Stück): ein
        lokales Modell braucht für einen Absatz gut und gern eine Minute, und
        so viel Wartezeit vor einem leeren Kasten hält niemand aus.
        """
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return self._json({"error": self.app.search.error}, 503)
        oll = self.app.ollama()
        if not (oll["running"] and oll["has_chat_model"]):
            return self._json({"error": {"k": "srv.answer.nomodel",
                                         "v": {"model": self.app.cfg["chat_model"]}}}, 503)

        query = str(data.get("q") or "").strip()
        if not query:
            return self._json({"error": {"k": "srv.answer.noquery", "v": {}}}, 400)
        k = max(1, min(int(self.app.cfg.get("answer_sources", 8)), 20))
        res = mod.search_messages(
            query=query, person=str(data.get("person") or ""),
            date_from=str(data.get("from") or ""), date_to=str(data.get("to") or ""),
            source=str(data.get("source") or "all"), k=k, preview_chars=0)
        treffer = res.get("results") or []
        if not treffer:
            return self._json({"error": {"k": "srv.answer.nohits", "v": {}}}, 200)

        # Volltext je Treffer: die Vorschau in der Liste ist zu kurz, um daraus
        # etwas zu beantworten.
        quellen = []
        for h in treffer:
            doc = mod.get_document(uid=h["uid"])
            quellen.append({**h, "text": doc.get("text") or ""})

        lang = i18n.negotiate(self.app.cfg.get("language"),
                              self.headers.get("Accept-Language"), RES)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")   # Ende des Stroms = Verbindungsende
        self.end_headers()
        self.close_connection = True

        def schicke(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        schicke({"sources": [{"n": i, "uid": h["uid"], "title": h.get("title"),
                              "who": h.get("who"), "date": h.get("date"),
                              "uri": h.get("uri")}
                             for i, h in enumerate(treffer, 1)],
                 "model": self.app.cfg["chat_model"]})
        try:
            for stueck in answer.stream(query, quellen, self.app.cfg["chat_model"],
                                        self.app.cfg["ollama"], lang):
                schicke(stueck)
            schicke({"done": True})
        except (BrokenPipeError, ConnectionResetError):
            pass          # Fenster zu oder abgebrochen – kein Grund für Lärm

    def _search(self, q):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "hits": [], "count": 0}
        kw = dict(person=q.get("person", ""), date_from=q.get("from", ""),
                  date_to=q.get("to", ""), source=q.get("source", "all"),
                  k=min(int(q.get("k", 20) or 20), 100),
                  offset=max(int(q.get("offset", 0) or 0), 0),
                  only_gone=str(q.get("gone", "")).lower() in ("1", "true", "ja"),
                  folder=str(q.get("folder", "") or ""))
        query = (q.get("q") or "").strip()
        if query:
            res = mod.search_messages(query=query, mode=q.get("mode", "auto"), **kw)
        else:
            res = mod.browse_messages(**kw)
        res["semantic"] = bool(mod.STATE.get("semantic"))
        return res

    def _thread(self, q):
        """Alle Nachrichten eines Gesprächs – dieselbe Auswertung wie im MCP."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "messages": [], "count": 0}
        return mod.get_thread(thread=q.get("key", ""),
                              limit=min(int(q.get("limit", 50) or 50), 200))

    def _folders(self, q):
        """Welche Postfachordner im Archiv liegen – für den Filter."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "folders": []}
        return mod.list_folders(contains=q.get("contains", ""),
                                limit=min(int(q.get("limit", 300) or 300), 1000))

    def _ordnerplan(self, data):
        """Was der nächste Lauf täte – ohne ihn zu starten.

        Nimmt die Regeln aus dem Formular, nicht die gespeicherten: sonst
        zeigte die Vorschau den Stand von vorhin, während daneben schon die
        neue Regel steht.

        Zwei Quellen, eine Auswertung. Der Unterschied ist klein genug, dass
        eine zweite Kopie sich nicht rechnet: beim Postfach zählen die `.eml`,
        beim Spiegel alle Dateien.
        """
        cfg = self.app.cfg
        onedrive = str(data.get("quelle") or "") == "onedrive"
        if onedrive:
            regeln = folders.lies_regeln(
                data.get("onedrive_rules")
                if data.get("onedrive_rules") is not None
                else cfg.get("onedrive_rules") or "")
            ordner, endung = BASE / cfg["onedrive_dir"], None
        else:
            regeln = auswahlregeln(cfg, data.get("folder_rules"),
                                   data.get("skip_folders"))
            ordner, endung = BASE / cfg["outlook_dir"], ".eml"
        daten = folders.lade(ordner)
        if not daten:
            return {"ok": False, "leer": True}
        return {"ok": True, "regeln": folders.schreibe_regeln(regeln),
                **folders.plan(ordner, regeln, daten, endung)}

    def _people(self, q):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "people": []}
        return mod.list_people(source=q.get("source", "all"),
                               contains=q.get("contains", ""),
                               limit=min(int(q.get("limit", 50) or 50), 200))

    def _document(self, q):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error}
        return mod.get_document(uid=q.get("uid", ""),
                                context_before=int(q.get("before", 0) or 0),
                                context_after=int(q.get("after", 0) or 0))

    def _calendar(self):
        """Kalender, rekonstruierte Termine und Kontakte am Stück ausliefern.

        Gepackt, wenn der Browser es anbietet: ~5 MB JSON werden dabei zu
        ~0,75 MB. Die Auswertung selbst läuft als eigener Schritt (sie liest
        jede Mail), hier wird nur deren Ergebnisdatei durchgereicht.
        """
        roh, gz = self.app.calendar_payload()
        if roh is None:
            return self._json({"error": {"k": "cal.missing", "v": {}},
                               "recs": []}, 404)
        akzeptiert = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        if akzeptiert:
            return self._send(200, gz, "application/json; charset=utf-8",
                              {"Content-Encoding": "gzip"})
        return self._send(200, roh, "application/json; charset=utf-8")

    def _source(self, q):
        """Exportierte Quelldatei ausliefern (für die Links in den Treffern).

        Content-Security-Policy: sandbox setzt die Seite in einen eigenen,
        undurchsichtigen Ursprung. Ein exportiertes Teams-HTML kann damit kein
        Skript gegen die API dieser App laufen lassen, zeigt aber weiterhin
        seine eingebetteten Bilder.
        """
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return self._send(503, "no index loaded", "text/plain; charset=utf-8")
        target, err = mod._resolve_source(q.get("root", ""), q.get("path", ""))
        if err:
            return self._send(404, err, "text/plain; charset=utf-8")
        # Teams-Exporte sind zum Lesen gemacht und bleiben im Browser. Alles
        # andere gehört in das Programm, das es kennt: eine .eml als roher Text
        # im Browserfenster ist für niemanden zu gebrauchen, im Mailprogramm
        # dagegen eine Mail mit Anhängen. Dasselbe gilt für .ics und .vcf.
        endung = target.suffix.lower()
        ctype = _CONTENT_TYPE.get(endung, "application/octet-stream")
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Security-Policy", "sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        if endung not in (".html", ".htm"):
            # Der Dateiname ohne Pfad, und nur mit unbedenklichen Zeichen: er
            # landet in einem Header und im Downloadordner.
            self.send_header("Content-Disposition",
                             f'attachment; filename="{_sicherer_name(target.name)}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(target, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 64 * 1024)


# Womit das Betriebssystem etwas anfangen kann. .eml öffnet das Mailprogramm,
# .ics den Kalender, .vcf die Kontakte – vorausgesetzt, der Typ stimmt.
_CONTENT_TYPE = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".eml": "message/rfc822",
    ".ics": "text/calendar; charset=utf-8",
    ".vcf": "text/vcard; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


def _sicherer_name(name):
    """Dateiname für Content-Disposition: keine Anführungszeichen, keine
    Zeilenumbrüche, kein Pfad – sonst ließe sich der Header aufbrechen."""
    sauber = re.sub(r'[\\"\r\n]', "_", Path(name).name).strip()
    return sauber or "datei"


def laeuft_bereits(port, host="127.0.0.1", timeout=1.5):
    """Antwortet auf dem Port schon eine Instanz dieser App?

    Ohne diese Prüfung startete jeder weitere Doppelklick eine zweite Instanz
    auf dem nächsten freien Port. Die fällt niemandem auf – die App hat kein
    Fenster und bleibt auch nicht im Dock stehen – und war nur über die
    Aktivitätsanzeige wieder loszuwerden.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status",
                                    timeout=timeout) as r:
            daten = json.loads(r.read().decode("utf-8"))
    except Exception:
        return False
    # Auf dem Port könnte etwas völlig anderes horchen; nur unsere eigene
    # Antwort zählt als "läuft schon".
    return isinstance(daten, dict) and "data_dir" in daten and "token" in daten


def make_server(app, port, host="127.0.0.1", tries=12):
    """Server binden und die erlaubten Host-Header festlegen.

    Erst binden, dann die Liste bauen: mit port=0 sucht das Betriebssystem
    einen freien Port aus, und der muss in den erlaubten Headern stehen.
    Ist der Wunschport belegt (zweiter Start, fremdes Programm), werden die
    nächsten durchprobiert – ein Doppelklick soll nicht mit einem Traceback
    enden, den niemand sieht.
    """
    httpd = None
    for versuch in range(tries if port else 1):
        try:
            httpd = ThreadingHTTPServer((host, port + versuch), Handler)
            break
        except OSError as e:
            if versuch == tries - 1:
                raise SystemExit(f"Kein freier Port ab {port}: {e}") from None
    real = httpd.server_address[1]
    Handler.app = app
    Handler.allowed_hosts = (f"{host}:{real}", f"localhost:{real}",
                             f"127.0.0.1:{real}", f"[::1]:{real}")
    return httpd


def serve(app, port, open_browser=True, host="127.0.0.1"):
    if port and laeuft_bereits(port, host):
        url = f"http://{host}:{port}/"
        print(f"Läuft bereits – öffne {url}")
        print("Beenden geht dort oben rechts über „Beenden“.")
        if open_browser:
            webbrowser.open(url)
        return None
    httpd = make_server(app, port, host)
    port = httpd.server_address[1]
    url = f"http://{host}:{port}/"
    app.log_token_state()
    app.check_updates()
    app.scheduler.start()
    app.autostart_mcp()
    print(f"Office-365-Export läuft: {url}")
    print("Beenden mit Strg+C (schließt auch den MCP-Server).")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBeende…")
    finally:
        app.shutdown()
        httpd.server_close()
    return httpd


def ensure_streams():
    """Ohne Konsole (Windows-Bündel) ist sys.stdout None – jedes print() flöge.

    Beides landet dann in app.log neben den Daten; sonst wäre ein Fehlstart
    einer fensterlosen Anwendung vollkommen stumm.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        f = open(BASE / "app.log", "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    return f


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Selbstaufruf als Teilprogramm (siehe script_argv) – vor dem Argument-
    # Parser, denn die Teilprogramme haben ihre eigenen Optionen.
    if argv and argv[0] == "--run":
        if len(argv) < 2:
            raise SystemExit(f"--run braucht einen Namen: {', '.join(RUNNABLE)}")
        return run_bundled(argv[1], argv[2:])

    ensure_streams()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--no-browser", action="store_true",
                    help="Oberfläche nicht automatisch öffnen.")
    ap.add_argument("--data-dir", metavar="ORDNER",
                    help="Ordner für Exporte, Index, Konfiguration und Token "
                         "(Vorgabe gebündelt: Benutzerdatenordner, als Skript: "
                         "der Projektordner). Wie OFFICE365_DATA_DIR.")
    ap.epilog = ("Als erstes Argument startet --run NAME [Optionen] ein "
                 f"Teilprogramm direkt: {', '.join(RUNNABLE)}. So ruft sich die "
                 "gebündelte Datei selbst auf; von Hand nur zum Nachsehen nötig.")
    a = ap.parse_args(argv)
    if a.data_dir:
        set_data_dir(a.data_dir)
    BASE.mkdir(parents=True, exist_ok=True)
    serve(App(), a.port, open_browser=not a.no_browser)


PAGE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Microsoft-365-Archiv</title>
<!-- Der Archivkasten aus packaging/icon/icon.svg, klein nachgezeichnet. Als
     Datenadresse, damit auch das Bündel ohne zusätzliche Datei auskommt –
     sonst holt sich jeder Browser ein 404 auf /favicon.ico ab. -->
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1024 1024'%3E%3Crect width='1024' height='1024' rx='229' fill='%232f6fed'/%3E%3Crect x='196' y='330' width='632' height='158' rx='34' fill='%23fff'/%3E%3Crect x='246' y='500' width='532' height='300' rx='34' fill='%23fff' opacity='.93'/%3E%3Crect x='430' y='596' width='164' height='44' rx='22' fill='%232f6fed'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#f6f7f9; --card:#fff; --ink:#1b1f24; --muted:#5b6570; --line:#dfe3e8;
  --accent:#2f6fed; --ok:#1a7f4b; --warn:#a2650a; --err:#b3261e; --code:#f1f3f6;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#14171a; --card:#1c2024; --ink:#e8eaed; --muted:#9aa4ae; --line:#2c3238;
         --accent:#7aa2ff; --ok:#4cc38a; --warn:#e0a33a; --err:#f2837c; --code:#22272c; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  padding:14px 20px;background:var(--card);border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0;font-weight:650}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto;align-items:center}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;
  border:1px solid var(--line);font-size:13px;cursor:pointer;background:transparent;color:inherit}
.pill:hover{border-color:var(--muted)}
/* „Beenden“ ist kein Zustand, sondern eine Handlung – die Lücke trennt es von
   den vier Anzeigen, damit niemand es für eine weitere Meldung hält. */
.pill-luecke{width:10px}
/* Erklärung auf Abruf statt Fließtext neben jedem Knopf. Der Text steckt im
   title-Attribut – das zeigt jeder Browser, liest jeder Screenreader vor, und
   es braucht kein eigenes Fenster, das aufgehen und wieder zugehen muss. */
h2.mit-info{display:flex;align-items:center;gap:8px}
.info{display:inline-flex;align-items:center;justify-content:center;
  width:17px;height:17px;border-radius:50%;border:1px solid var(--line);
  color:var(--muted);font-size:11.5px;font-style:italic;font-weight:600;
  cursor:help;user-select:none;flex:0 0 auto}
.info:hover,.info:focus{color:var(--ink);border-color:var(--muted);outline:none}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.err{background:var(--err)}
nav{display:flex;gap:4px;padding:10px 20px 0;background:var(--card)}
nav button{border:0;background:transparent;color:var(--muted);padding:8px 14px;
  border-radius:8px 8px 0 0;font:inherit;cursor:pointer}
nav button.on{background:var(--bg);color:var(--ink);font-weight:600}
main{padding:20px;max-width:1080px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:18px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 4px}
.card p.sub{color:var(--muted);margin:0 0 14px;font-size:13px}
label.chk{display:flex;gap:8px;align-items:center;padding:4px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:6px 20px}
button.act{background:var(--accent);color:#fff;border:0;border-radius:8px;
  padding:9px 16px;font:inherit;font-weight:600;cursor:pointer}
button.act:disabled{opacity:.45;cursor:not-allowed}
button.ghost{background:transparent;border:1px solid var(--line);color:inherit;
  border-radius:8px;padding:9px 16px;font:inherit;cursor:pointer}
input[type=text],input[type=number],input[type=date],select,textarea{
  background:var(--bg);color:inherit;border:1px solid var(--line);border-radius:8px;
  padding:8px 10px;font:inherit}
textarea{width:100%;min-height:120px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
code,pre{background:var(--code);border-radius:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
code{padding:2px 5px} pre{padding:12px;overflow-x:auto;margin:8px 0}
.muted{color:var(--muted)} .small{font-size:13px}
.ok{color:var(--ok)} .warn{color:var(--warn)} .err{color:var(--err)}
#log{background:#0d1013;color:#cbd3da;border-radius:10px;padding:12px;height:230px;
  overflow:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;white-space:pre-wrap}
#log .l-head{color:#9ad0ff;font-weight:600} #log .l-ok{color:#7fdca4}
#log .l-warn{color:#f0c674} #log .l-err{color:#ff9c94}
.hit{border-top:1px solid var(--line);padding:12px 0}
.hit:first-child{border-top:0}
.hit h3{margin:0 0 3px;font-size:14px;font-weight:600}
.hit .meta{color:var(--muted);font-size:12.5px;margin-bottom:5px}
.hit .prev{font-size:13.5px}
.tag{display:inline-block;background:var(--code);border-radius:5px;padding:1px 6px;
  font-size:11.5px;color:var(--muted);margin-right:6px}
#overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;
  align-items:center;justify-content:center;padding:20px;z-index:20}
#overlay.on{display:flex}
.modal{background:var(--card);border-radius:14px;max-width:660px;width:100%;
  max-height:88vh;overflow:auto;padding:24px}
.modal h2{margin:0 0 6px;font-size:18px}
/* Alle Assistenten tragen denselben Rahmen: Titel mit Schließkreuz oben,
   unten genau eine primäre und eine sekundäre Aktion. */
.modal-kopf{display:flex;align-items:flex-start;gap:12px}
.modal-kopf h2{flex:1}
.modal-zu{flex:0 0 auto;border:0;background:transparent;color:var(--muted);
  font-size:22px;line-height:1;padding:0 4px;cursor:pointer;border-radius:6px}
.modal-zu:hover{color:var(--ink);background:var(--code)}
.modal-fuss{margin-top:16px;align-items:center}
/* Die Exportliste zeigt bis zu vierhundert Pfade – sie braucht mehr Breite als
   ein Assistent mit drei Sätzen, und jede Gruppe scrollt für sich, damit die
   dritte nicht unter der ersten begraben liegt. */
.modal.breit{max-width:860px}
.plangruppe{margin:10px 0;border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.plangruppe>summary{cursor:pointer;font-size:13.5px;font-weight:600}
.plangruppe>summary .dot{display:inline-block;margin-right:7px;vertical-align:middle}
.planliste{list-style:none;margin:8px 0 2px;padding:0;max-height:34vh;overflow:auto}
.planliste li{display:flex;gap:10px;align-items:baseline;padding:3px 0;font-size:13px;
  border-top:1px solid var(--line)}
.planliste li:first-child{border-top:0}
.planliste .pfad{flex:1;word-break:break-word}
.planliste .zahl{flex:0 0 auto;min-width:5em;text-align:right;color:var(--muted);
  font-variant-numeric:tabular-nums}
.planliste .regel{flex:0 0 auto;color:var(--muted);font-size:11.5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
ol{padding-left:20px;margin:12px 0} ol li{margin-bottom:9px}
.banner{border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:13.5px;
  border:1px solid var(--line)}
.banner.warn{border-color:var(--warn)} .banner.err{border-color:var(--err)}
.hide{display:none!important}

/* ---- Kalender und Adressbuch (übernommen aus combined_search.py, an die
       Farbvariablen der App angepasst, damit sie auch dunkel funktionieren) ---- */
:root{
  --ev-ok:#2b6cb0; --ev-ok-bg:#eef4fb; --ev-warn:#c98a17; --ev-warn-bg:#fdf6e7;
  --ev-bad:#c0392b; --ev-bad-bg:#fbeceb; --ev-gone:#b6bbc2; --ev-gone-bg:#f2f3f5;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ev-ok:#7aa2ff; --ev-ok-bg:#1e2733; --ev-warn:#e0a33a; --ev-warn-bg:#2e2716;
    --ev-bad:#f2837c; --ev-bad-bg:#33201f; --ev-gone:#4a525a; --ev-gone-bg:#23272c;
  }
}
:root[data-theme="dark"]{
  --ev-ok:#7aa2ff; --ev-ok-bg:#1e2733; --ev-warn:#e0a33a; --ev-warn-bg:#2e2716;
  --ev-bad:#f2837c; --ev-bad-bg:#33201f; --ev-gone:#4a525a; --ev-gone-bg:#23272c;
}
.calbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.chip{padding:6px 13px;border:1px solid var(--line);border-radius:8px;background:transparent;
  color:var(--muted);font-size:13.5px;cursor:pointer}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
#kalTitle{font-weight:650;margin-left:4px}
.legend{display:flex;gap:12px;margin-left:auto;font-size:12px;color:var(--muted);align-items:center;flex-wrap:wrap}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:4px;vertical-align:-1px}
.grid{display:grid;gap:8px}
/* minmax(0,…): sonst sprengen lange Termintitel die Spaltenbreite */
.wk,.mo{grid-template-columns:repeat(7,minmax(0,1fr))}
.mo{gap:6px}
.dow{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em;padding:0 2px}
.day{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;min-height:110px;min-width:0}
.day.today{border-color:var(--accent);box-shadow:0 0 0 2px rgba(122,162,255,.18)}
.day.out{opacity:.5}
.dnum{font-size:12px;color:var(--muted);margin-bottom:5px;display:flex;gap:5px;align-items:baseline}
.dnum b{font-size:14px;color:var(--ink)}
.dnum .wd{display:none}          /* Wochentag steht schon in der Spaltenüberschrift */
.ev{display:block;font-size:12px;line-height:1.35;margin:3px 0;padding:4px 6px;border-radius:6px;
  text-decoration:none;border-left:3px solid var(--ev-ok);background:var(--ev-ok-bg);color:var(--ink);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ev:hover{white-space:normal}
.ev .evt{color:var(--muted);font-variant-numeric:tabular-nums}
.ev.tentative{border-left-color:var(--ev-warn);background:var(--ev-warn-bg);border-left-style:dashed}
.ev.cancelled{border-left-color:var(--ev-bad);background:var(--ev-bad-bg);text-decoration:line-through;opacity:.75}
/* nur aus Mails rekonstruiert: gestrichelter Rahmen statt Balken */
.ev.deleted{border:1px dashed var(--ev-bad);border-left-width:3px;background:var(--ev-bad-bg);text-decoration:line-through;opacity:.85}
.ev.gone{border:1px dashed var(--ev-gone);border-left-width:3px;background:var(--ev-gone-bg);color:var(--muted)}
.mo .day{min-height:96px}
@media(max-width:820px){.wk,.mo{grid-template-columns:minmax(0,1fr)}.dowrow{display:none}
  .day{min-height:0}.dnum .wd{display:inline}}
.rbnote{color:var(--muted);font-size:12.5px;margin:0 0 10px}
.rbcount{color:var(--muted);font-size:12px;margin-left:auto}
.rbmonth{margin:16px 0 6px;font-size:13px;font-weight:700;color:var(--muted);
  border-bottom:1px solid var(--line);padding-bottom:3px}
.rbrow{display:flex;gap:10px;align-items:baseline;background:var(--card);border:1px solid var(--line);
  border-radius:9px;padding:8px 11px;margin:5px 0;text-decoration:none;color:var(--ink)}
.rbrow:hover{border-color:var(--accent)}
.rbrow.deleted{border-left:3px solid var(--ev-bad)}
.rbrow.gone{border-left:3px solid var(--ev-gone)}
.rbdate{color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap;min-width:158px}
.rbstate{font-size:11px;padding:2px 8px;border-radius:6px;font-weight:600;white-space:nowrap}
.rbrow.deleted .rbstate{background:var(--ev-bad-bg);color:var(--ev-bad)}
.rbrow.gone .rbstate{background:var(--ev-gone-bg);color:var(--muted)}
.rbtitle{font-weight:600;overflow-wrap:anywhere;min-width:0;flex:1}
.rbrow.deleted .rbtitle{text-decoration:line-through}
.rbwho{color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
@media(max-width:820px){.rbrow{flex-wrap:wrap;gap:4px 9px}.rbwho{max-width:none}}
.letter{margin:18px 0 6px;font-size:13px;font-weight:700;color:var(--muted);
  border-bottom:1px solid var(--line);padding-bottom:3px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.card2{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:11px 13px}
.cname{font-weight:600;overflow-wrap:anywhere}
.cname a{color:var(--ink);text-decoration:none}
.cname a:hover{color:var(--accent);text-decoration:underline}
.crole{font-size:12.5px;color:var(--muted);margin-bottom:5px;overflow-wrap:anywhere}
.cline{font-size:13px;overflow-wrap:anywhere}
.cline a{color:var(--accent);text-decoration:none}
.cline span{color:var(--muted);margin-right:5px}
.hint{color:var(--muted)}

/* Fortschritt: Schritt für Schritt, und innerhalb eines Schritts so genau,
   wie das Skript es weiß. Wo es keine Gesamtzahl gibt, läuft der Balken
   gestreift weiter, statt eine Prozentzahl zu erfinden. */
.fortschritt{margin-top:14px}
.balken{height:8px;background:var(--code);border-radius:99px;overflow:hidden}
.balken>div{height:100%;background:var(--accent);border-radius:99px;
  transition:width .3s ease;width:0}
.balken.unbekannt>div{width:35%;background:linear-gradient(90deg,
  var(--code) 0%,var(--accent) 50%,var(--code) 100%);animation:wandern 1.6s linear infinite}
@keyframes wandern{from{transform:translateX(-100%)}to{transform:translateX(340%)}}

/* Einzelschritte: eingeklappt, jeder Knopf mit dem Satz, wann man ihn braucht */
#einzelschritte summary{cursor:pointer;font-weight:650;font-size:15px}
.schritt{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0}
.schritt button{flex:0 0 auto;min-width:230px}
/* Die frühere Regel .schritt span{flex:1;min-width:240px} stammte vom
   Erklärungstext, der hier stand. Sie traf danach das (i) und zog den Kreis
   auf 240 Pixel Breite auseinander – als flache Ellipse quer durch die Zeile.
   Was hier steht, ist ein Zeichen und keine Textspalte. */
.schritt .info{flex:0 0 auto}

/* Berechtigungen im Token-Assistenten: eine unauffällige Zeile, solange sie
   nicht das Problem sind. */
details.rechte{margin:12px 0;border:1px solid var(--line);border-radius:8px;padding:8px 12px}
details.rechte summary{cursor:pointer;font-size:13px;color:var(--muted)}
details.rechte[open] summary{margin-bottom:4px;color:var(--ink)}
details.rechte p{margin:6px 0}

/* Die Auswahl der beiden Anmeldewege: zwei gleichwertige Karten, damit keiner
   wie eine Fußnote des anderen aussieht. */
.wahlreihe{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}
.wahl{flex:1 1 240px;display:flex;gap:9px;align-items:flex-start;cursor:pointer;
  border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.wahl.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.wahl input{margin-top:3px}
.wahl span{display:flex;flex-direction:column;gap:2px}
button.mini{border:1px solid var(--line);background:transparent;color:inherit;
  border-radius:8px;padding:5px 12px;font:inherit;font-size:13px;cursor:pointer}
button.mini:hover{border-color:var(--accent);color:var(--accent)}
/* Kopierknopf im Eck des Kastens – sichtbar, ohne den Inhalt zu verdecken. */
.mitkopie{position:relative}
.mitkopie pre{padding-right:96px}
button.kopie{position:absolute;top:8px;right:8px;background:var(--card)}
/* Verlauf unter einem Treffer: schmal und ruhig, damit er die Trefferliste
   nicht erschlägt. */
/* Gelöschtes ist die Ausnahme und darf auffallen – aber nur so weit, dass die
   Trefferliste ruhig bleibt. */
.tag.weg{border-color:var(--warn);color:var(--warn)}
.tag.herkunft{margin-left:8px;font-weight:400}
/* Analytics: Kennzahlen als ruhiges Raster, nicht als Armaturenbrett. */
.kpis{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
/* Suchfeld und Knopf gehören zusammen und füllen die Zeile – das Bild, das
   jeder aus anderen Programmen kennt. Alles Weitere liegt darunter. */
.suchzeile{display:flex;gap:8px}
.suchzeile input{flex:1;min-width:200px;font-size:15px;padding:9px 12px}
.suchzeile button{flex:0 0 auto;padding:9px 20px}
.feld{display:flex;align-items:center;gap:6px;color:var(--muted)}
.kpi{border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi.klickbar{cursor:pointer}
.kpi.klickbar:hover,.kpi.klickbar:focus{border-color:var(--accent);outline:none}
.kpi-titel{display:flex;align-items:center;gap:6px}
.kpi-wert{font-size:26px;font-weight:650;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.kpi-titel{font-size:13.5px;margin-top:2px}
.kpi-hint{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.4}
.kpi-fuss{grid-column:1/-1}
.anatab{width:100%;border-collapse:collapse;margin-top:12px;font-size:13.5px}
.anatab th{text-align:left;font-weight:600;border-bottom:1px solid var(--line);padding:6px 8px}
.anatab td{padding:5px 8px;border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums}
.anatab td:not(:first-child), .anatab th:not(:first-child){text-align:right}
.anatab td.fehlt{color:var(--warn);font-weight:600}
.warnzeile{color:var(--warn);font-weight:600;margin:0}
.okzeile{color:var(--ok);font-weight:600;margin:0}
.card2 button.mini{margin-top:8px}
.verlauf{margin-top:8px}
.verlaufliste{border-left:2px solid var(--line);padding-left:12px;margin-top:6px}
.verlaufliste p{margin:0 0 6px}
.vzeile{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;
  font-size:13.5px;padding:3px 0}
.vdatum{color:var(--muted);font-variant-numeric:tabular-nums;flex:0 0 auto}
.vwer{color:var(--muted);flex:0 0 auto;min-width:120px}
.geraetecode{border:1px solid var(--line);border-radius:10px;padding:12px;margin:12px 0;
  text-align:center}
.geraetecode p{margin:0 0 8px}
.code-gross{display:inline-block;font-size:26px;letter-spacing:.14em;font-weight:700;
  background:var(--code);border-radius:8px;padding:8px 16px}

/* Protokollleiste unten */
#protokoll{position:fixed;left:0;right:0;bottom:0;background:var(--card);
  border-top:1px solid var(--line);z-index:15;box-shadow:0 -2px 12px rgba(0,0,0,.10)}
#protokoll .pkopf{display:flex;gap:10px;align-items:center;padding:8px 20px;
  cursor:pointer;user-select:none}
#protokoll .pkopf .pfeil{color:var(--muted);transition:transform .2s}
#protokoll.zu .pkopf .pfeil{transform:rotate(180deg)}
#protokoll .pkopf #log-letzte{overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1}
#protokoll.zu #log{display:none}
#protokoll #log{margin:0 12px 12px;height:220px}
main{padding-bottom:60px}

/* Antwortkasten. Bewusst anders als eine Trefferkarte: was hier steht, hat
   kein Mensch geschrieben, sondern ein Modell aus den Treffern darunter
   zusammengefasst. Farbiger Balken links, eigene Kopfzeile, Fußnoten. */
.answer{background:var(--card);border:1px solid var(--line);
  border-left:4px solid var(--accent);border-radius:12px;padding:14px 18px;margin-bottom:16px}
.answer .ahead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  font-size:12px;color:var(--muted);margin-bottom:8px}
.answer .ahead .tag{background:var(--accent);color:#fff;border-radius:5px;
  padding:1px 8px;font-weight:700;letter-spacing:.04em}
.answer .atext{white-space:pre-wrap;overflow-wrap:anywhere}
.answer .atext a{color:var(--accent);text-decoration:none;font-weight:600}
.answer .afoot{font-size:12px;color:var(--muted);margin-top:10px;
  border-top:1px solid var(--line);padding-top:8px}
.answer.err{border-left-color:var(--err)}
.blink::after{content:"▍";animation:blink 1s steps(2,start) infinite}
@keyframes blink{to{visibility:hidden}}
.hit.zitiert{background:var(--code);border-radius:8px;padding-left:10px;
  margin-left:-10px;box-shadow:inset 3px 0 0 var(--accent)}
.hit .fussnote{color:var(--accent);font-weight:700;margin-right:6px}
</style>
</head>
<body>
<header>
  <h1 data-i18n="app.title">Microsoft-365-Archiv</h1>
  <!-- Die Kacheln sagen, was der Zustand für den Anwender bedeutet; der
       Fachbegriff (Token, Ollama, Chunks, MCP) steht im Tooltip, damit ihn
       findet, wer ihn braucht, ohne dass ihn lesen muss, wer ihn nicht kennt. -->
  <div class="pills">
    <button class="pill" id="pill-token" onclick="openWizard('token')"><span class="dot" id="p-token"></span><span id="p-token-t">Zugang</span></button>
    <button class="pill" id="pill-ollama" onclick="openWizard('ollama')"><span class="dot" id="p-ollama"></span><span id="p-ollama-t">KI-Suche</span></button>
    <button class="pill" id="pill-mcp" onclick="zeigeEinstellung('mcp-karte')"><span class="dot" id="p-mcp"></span><span id="p-mcp-t">Claude</span></button>
    <span class="pill-luecke"></span>
    <button class="pill" onclick="beenden()" id="btn-quit" data-i18n="app.quit"
            data-i18n-title="app.quit.tip"
            style="border-color:var(--err);color:var(--err)">Beenden</button>
  </div>
</header>

<nav>
  <button data-tab="export" class="on" onclick="tab('export')" data-i18n="nav.export">Daten exportieren</button>
  <button data-tab="suche" onclick="tab('suche')" data-i18n="nav.search">Daten durchsuchen</button>
  <button data-tab="analytics" onclick="tab('analytics')" data-i18n="nav.analytics">Analytics</button>
  <button data-tab="einstellungen" onclick="tab('einstellungen')" data-i18n="nav.settings">Einstellungen</button>
</nav>

<main>
<section id="tab-export">
  <div class="banner hide" id="update-banner" style="margin-bottom:16px"></div>
  <div class="card">
    <h2 class="mit-info" data-i18n="export.what">Was soll exportiert werden?
      <span class="info" tabindex="0" data-i18n-title="export.what.sub" role="img" aria-label="Info">i</span></h2>
    <div class="row" style="gap:36px;align-items:flex-start">
      <div>
        <strong class="small" data-i18n="export.outlook">Outlook</strong>
        <div id="cat-outlook"></div>
      </div>
      <div>
        <strong class="small" data-i18n="export.teams">Teams</strong>
        <div id="cat-teams"></div>
      </div>
      <div>
        <strong class="small" data-i18n="export.onedrive">OneDrive</strong>
        <label class="chk"><input type="checkbox" id="c-onedrive_enabled" onchange="saveCats()">
          <span data-i18n="export.cat.files">OneDrive-Dateien</span></label>
      </div>
    </div>
    <p class="small muted" id="teams-note" style="margin-top:10px"></p>
    <div class="row" style="margin-top:14px">
      <button class="act" id="btn-run" onclick="runExport()" data-i18n="export.start">Export starten</button>
      <button class="ghost hide" id="btn-cancel" onclick="post('/api/cancel')" data-i18n="export.cancel">Abbrechen</button>
      <span class="info" tabindex="0" data-i18n-title="export.start.hint"
            role="img" aria-label="Info">i</span>
    </div>

    <div class="fortschritt hide" id="fortschritt">
      <div class="balken"><div id="balken-fuell"></div></div>
      <p class="small muted" id="fortschritt-text"></p>
    </div>

  </div>

  <details class="card" id="einzelschritte">
    <summary data-i18n="export.steps.title">Expertenmodus</summary>
    <p class="sub" style="margin-top:10px" data-i18n="export.steps.sub">Normalerweise nicht nötig – „Export starten“ erledigt das alles. Einzeln braucht man sie nur in den unten genannten Fällen.</p>

    <div class="schritt">
      <button class="ghost" onclick="run({index:true}, t('job.index'))" data-i18n="export.index.only">Nur indizieren</button>
      <span class="info" tabindex="0" data-i18n-title="export.index.only.when" role="img" aria-label="Info">i</span>
    </div>
    <div class="schritt">
      <button class="ghost" onclick="run({calendar:true}, t('job.calendar'))" data-i18n="export.calendar.build">Kalender &amp; Kontakte aufbauen</button>
      <span class="info" tabindex="0" data-i18n-title="export.calendar.build.when" role="img" aria-label="Info">i</span>
    </div>
    <div class="schritt">
      <button class="ghost" onclick="run({search_page:true}, t('job.searchpage'))" data-i18n="export.page.build">Portable Suchseite erzeugen</button>
      <span class="info" tabindex="0" data-i18n-title="export.page.build.when" role="img" aria-label="Info">i</span>
    </div>
  </details>
</section>

<section id="tab-suche" class="hide">
  <div class="calbar" id="sichten" style="margin-bottom:14px">
    <span class="chip on" data-sicht="treffer" onclick="sicht('treffer')" data-i18n="view.hits">Treffer</span>
    <span class="chip" data-sicht="kalender" onclick="sicht('kalender')" data-i18n="nav.calendar">Kalender</span>
    <span class="chip" data-sicht="adressbuch" onclick="sicht('adressbuch')" data-i18n="nav.book">Adressbuch</span>
    <span class="chip" data-sicht="geloescht" id="chip-geloescht" onclick="sicht('geloescht')"
          data-i18n="view.gone" data-i18n-title="search.gone.note">Gelöschtes</span>
  </div>

  <div id="sicht-treffer">
  <div class="card">
    <div class="suchzeile">
      <input type="search" id="q" data-i18n-ph="search.query.ph"
             placeholder="Suchbegriff oder Frage" onkeydown="if(event.key==='Enter')doSearch(0)">
      <button class="act" onclick="doSearch(0)" data-i18n="search.go">Suchen</button>
    </div>
    <div class="row" style="margin-top:10px;gap:10px">
      <button class="mini" id="filter-auf" aria-expanded="false"
              onclick="filterUmschalten()" data-i18n="search.filter">Filter</button>
      <button class="mini hide" id="filter-weg" onclick="filterLeeren()"
              data-i18n="search.filter.clear">Zurücksetzen</button>
    </div>
    <div class="row hide" id="filter" style="margin-top:10px">
      <input type="text" id="f-person" data-i18n-ph="search.person.ph" placeholder="Person"
             style="width:180px" onchange="doSearch(0)">
      <select id="f-source" onchange="doSearch(0)">
        <option value="all" data-i18n="search.source.all">Alle Quellen</option><option value="teams" data-i18n="search.source.teams">Teams</option>
        <option value="outlook" data-i18n="search.source.outlook">Mail</option>
        <option value="kalender" data-i18n="search.source.kalender">Kalender</option>
        <option value="kontakte" data-i18n="search.source.kontakte">Kontakte</option>
        <option value="datei" data-i18n="search.source.datei">Dateien</option>
      </select>
      <label class="small feld"><span data-i18n="search.from">von</span>
        <input type="date" id="f-from" onchange="doSearch(0)"></label>
      <label class="small feld"><span data-i18n="search.to">bis</span>
        <input type="date" id="f-to" onchange="doSearch(0)"></label>
      <select id="f-folder" onchange="doSearch(0)" style="max-width:260px">
        <option value="" data-i18n="search.folder.all">Alle Ordner</option>
      </select>
    </div>
    <!-- Nicht mehr sichtbar: „Nur Gelöschtes“ ist jetzt eine Sicht in der
         Leiste oben. Das Feld bleibt als Zustand, den doSearch abfragt. -->
    <input type="checkbox" id="f-gone" class="hide">
  </div>
  <div class="answer hide" id="ai-box"></div>
  <div class="card">
    <label class="chk hide" id="ai-wrap" style="margin-bottom:10px">
      <input type="checkbox" id="ai-on" onchange="merkeKI()">
      <span data-i18n="search.ai">Antwort formulieren</span></label>
    <div id="results" class="muted small" data-i18n="search.none.yet">Noch keine Suche.</div>
    <div class="row" id="pager" style="margin-top:12px"></div></div>
  </div>

  <div id="sicht-kalender" class="hide">
  <div class="card">
    <div class="calbar">
      <span class="chip on" data-mode="week" data-i18n="cal.week">Woche</span>
      <span class="chip" data-mode="month" data-i18n="cal.month">Monat</span>
      <span class="chip" data-mode="rebuilt" data-i18n="cal.rebuilt">Rekonstruiert</span>
      <span id="kalNav">
        <button class="ghost" id="kalPrev" style="padding:6px 12px">‹</button>
        <button class="ghost" id="kalToday" style="padding:6px 12px" data-i18n="cal.today">Heute</button>
        <button class="ghost" id="kalNext" style="padding:6px 12px">›</button>
      </span>
      <span id="kalTitle"></span>
      <span class="legend" id="kalLegend">
        <span><i style="background:var(--ev-ok)"></i><span data-i18n="cal.legend.confirmed">Bestätigt</span></span>
        <span><i style="background:var(--ev-warn)"></i><span data-i18n="cal.legend.tentative">Vorläufig</span></span>
        <span><i style="background:var(--ev-bad)"></i><span data-i18n="cal.legend.cancelled">Abgesagt</span></span>
        <span><i style="background:var(--ev-gone)"></i><span data-i18n="cal.legend.rebuilt">Rekonstruiert</span></span>
      </span>
    </div>
    <p class="small muted" id="kalStats"></p>
    <div id="kalBox"><p class="hint" data-i18n="cal.loading">Wird geladen…</p></div>
  </div>
  </div>

  <div id="sicht-adressbuch" class="hide">
  <div class="card">
    <div class="calbar">
      <span class="chip on" data-book="all" data-i18n="book.f.all">Alle</span>
      <span class="chip" data-book="contacts" data-i18n="book.f.contacts">Aus Kontakten</span>
      <span class="chip" data-book="comm" data-i18n="book.f.comm">Aus Kommunikation</span>
      <input type="text" id="kbQ" data-i18n-ph="book.search.ph" placeholder="Name, Firma, Mail oder Telefon…" style="flex:1;min-width:220px">
      <span class="small muted" id="kbStats"></span>
    </div>
    <p class="small muted" style="margin:8px 0 0" data-i18n="book.f.note">Kontakte stammen aus dem Outlook-Adressbuch, Kommunikation aus Absendern und Empfängern.</p>
  </div>
  <div class="card"><div id="kbBox"><p class="hint" data-i18n="cal.loading">Wird geladen…</p></div></div>
  </div>
</section>




<section id="tab-analytics" class="hide">
  <div class="card">
    <h2 data-i18n="ana.title">Was im Archiv steckt</h2>
    <p class="sub" data-i18n="ana.sub">Alles aus dem Index gerechnet – ohne Microsoft zu fragen.</p>
    <div id="ana-kpi" class="kpis"><p class="hint" data-i18n="cal.loading">Wird geladen…</p></div>
    <p class="small muted" style="margin-top:10px" id="export-state"></p>
  </div>

  <div class="card">
    <h2 data-i18n="ana.check.title">Vollständigkeit</h2>
    <p class="sub" data-i18n="ana.check.sub">Vergleicht, was Microsoft je Ordner zählt, mit dem, was hier liegt.</p>
    <div class="row">
      <button class="act" id="ana-check" onclick="pruefeVollstaendigkeit()" data-i18n="ana.check.run">Jetzt prüfen</button>
      <span class="small muted" id="ana-check-state"></span>
    </div>
    <div id="ana-check-box"></div>
    <div id="ana-check-box-od"></div>
  </div>
</section>

<section id="tab-einstellungen" class="hide">
  <div class="card">
    <h2 data-i18n="settings.teams.title">Teams-Export</h2>
    <p class="sub" data-i18n="settings.teams.sub">Wirkt auf den nächsten Lauf.</p>
    <label class="chk"><input type="checkbox" id="c-embed_images"> <span data-i18n="settings.embed_images">Inline-Bilder einbetten</span>
      <span class="small muted" data-i18n="settings.embed_images.hint">– aus: kleinere Dateien</span></label>
    <label class="chk"><input type="checkbox" id="c-cache_images"> <span data-i18n="settings.cache_images">Bilder zwischenspeichern</span>
      <span class="small muted" data-i18n="settings.cache_images.hint">– schnellerer Neu-Export</span></label>
    <label class="chk"><input type="checkbox" id="c-refresh_channels"> <span data-i18n="settings.refresh_channels">Kanäle auf neue Antworten prüfen</span>
      <span class="small muted" data-i18n="settings.refresh_channels.hint">– aus: Kanäle nur einmalig</span></label>
    <label class="chk"><input type="checkbox" id="c-skip_empty_chats"> <span data-i18n="settings.skip_empty_chats">Chats ohne echte Nachricht überspringen</span>
      <span class="small muted" data-i18n="settings.skip_empty_chats.hint">– nur Beitritte, Anrufe</span></label>
  </div>

  <div class="card">
    <h2 data-i18n="settings.outlook.title">Outlook-Export</h2>
    <label class="chk"><input type="checkbox" id="c-include_hidden"> <span data-i18n="settings.include_hidden">Versteckte Systemordner mitnehmen</span>
      <span class="small muted" data-i18n="settings.include_hidden.hint">– Conversation History …</span></label>
    <label class="chk"><input type="checkbox" id="c-calendar_reconstruct"> <span data-i18n="settings.calendar_reconstruct">Gelöschte Termine aus Mails wiederherstellen</span></label>
    <p class="small muted" style="margin:2px 0 0 24px" data-i18n="settings.calendar_reconstruct.hint">Liest jede Mail – der langsamste Schritt.</p>
    <h3 style="margin:16px 0 4px;font-size:15px" data-i18n="folders.title">Welche Ordner exportiert werden</h3>
    <p class="small muted" id="folders-state"></p>
    <div class="row" style="margin:6px 0">
      <button class="mini" onclick="gleicheOrdnerAb()" data-i18n="folders.sync">Ordnerstruktur abgleichen</button>
      <span class="small muted" id="folders-msg"></span>
    </div>
    <textarea id="c-folder_rules" style="min-height:110px"
      placeholder="- E-Mail/Archiv/**&#10;+ E-Mail/Archiv/Wichtig/**"></textarea>
    <p class="small muted" data-i18n-html="folders.rules.hint">Eine Regel je Zeile.</p>

    <p class="sub" style="margin:12px 0 6px" data-i18n="settings.skip_folders.sub">Ordner, die die Standardauswahl auslässt.</p>
    <textarea id="c-skip_folders" style="min-height:120px"></textarea>
    <div class="row" style="margin-top:8px">
      <button class="ghost" onclick="ordnerZuruecksetzen()" data-i18n="settings.skip_folders.reset">Auf Vorgabe zurücksetzen</button>
      <button class="ghost" onclick="zeigeExportliste()" data-i18n="plan.open">Exportliste anzeigen</button>
    </div>
  </div>

  <div class="card">
    <h2 data-i18n="settings.onedrive.title">OneDrive</h2>
    <p class="sub" data-i18n="settings.onedrive.sub">Spiegelt dein Laufwerk hierher. Einschalten im Reiter „Exportieren“.</p>
    <div class="banner" data-i18n-html="settings.onedrive.note">
      Gesichert wird alles. <strong>Durchsuchbar sind Name und Ordner</strong> – der Inhalt der Dokumente steht noch nicht im Index.
    </div>
    <p class="small muted" data-i18n="settings.onedrive.mirror">Gelöschtes bleibt liegen; frühere Fassungen einer geänderten Datei nicht.</p>
    <h3 style="margin:16px 0 4px;font-size:15px" data-i18n="settings.onedrive.rules.title">Welche Ordner gespiegelt werden</h3>
    <p class="small muted" id="od-folders-state"></p>
    <div class="row" style="margin:6px 0">
      <button class="mini" onclick="gleicheOrdnerAb('onedrive')" data-i18n="folders.sync">Ordnerstruktur abgleichen</button>
      <span class="small muted" id="od-folders-msg"></span>
    </div>
    <textarea id="c-onedrive_rules" style="min-height:90px"
      placeholder="- Dateien/Fotos/**&#10;+ Dateien/Fotos/Wichtig/**"></textarea>
    <p class="small muted" data-i18n-html="settings.onedrive.rules.hint">Eine Regel je Zeile – wie beim Postfach, die <strong>letzte</strong> zutreffende gewinnt.</p>
    <div class="row" style="margin-top:10px">
      <label class="small"><span data-i18n="settings.onedrive.maxmb">Dateien überspringen ab</span>
        <input type="number" id="c-onedrive_max_mb" min="0" step="10" style="width:90px"> MB</label>
      <span class="small muted" data-i18n="settings.onedrive.maxmb.hint">0 = ohne Grenze.</span>
    </div>
    <div class="row" style="margin-top:10px">
      <button class="ghost" onclick="zeigeExportliste('onedrive')" data-i18n="plan.open">Exportliste anzeigen</button>
    </div>
  </div>

  <div class="card">
    <h2 data-i18n="sched.title">Regelmäßig exportieren</h2>
    <p class="sub" data-i18n="sched.sub">Läuft nur, solange die App geöffnet ist.</p>
    <label class="chk"><input type="checkbox" id="s-enabled"> <span data-i18n="sched.enabled">Zeitplan aktiv</span></label>
    <div class="row" style="margin:10px 0">
      <label class="small"><span data-i18n="sched.every">Alle</span> <input type="number" id="s-interval" min="5" step="5" value="60" style="width:80px"> <span data-i18n="sched.minutes">Minuten</span></label>
    </div>
    <label class="chk"><input type="checkbox" id="s-outlook"> <span data-i18n="sched.outlook">Outlook exportieren</span></label>
    <label class="chk"><input type="checkbox" id="s-teams"> <span data-i18n="sched.teams">Teams exportieren</span></label>
    <label class="chk"><input type="checkbox" id="s-index"> <span data-i18n="sched.index">Danach indizieren</span></label>
    <label class="chk"><input type="checkbox" id="s-calendar"> <span data-i18n="sched.calendar">Kalender &amp; Kontakte neu aufbauen</span></label>
    <div class="row" style="margin-top:14px">
      <button class="act" onclick="saveSchedule()" data-i18n="sched.save">Zeitplan speichern</button>
      <span class="small muted" id="s-next"></span>
    </div>
    <div class="banner warn" style="margin-top:14px" data-i18n="sched.token.note">
      Der Zeitplan trägt nur so weit, wie der Access Token gültig ist.
    </div>
  </div>

  <div class="card">
    <h2 data-i18n="settings.speed.title">Geschwindigkeit</h2>
    <div class="row">
      <label class="small"><span data-i18n="settings.workers">Parallele Downloads</span>
        <input type="number" id="c-workers" min="1" max="8" style="width:80px"></label>
      <span class="small muted" data-i18n="settings.workers.hint">Graph erlaubt 4 gleichzeitige Anfragen je Postfach.</span>
    </div>
    <div class="row" style="margin-top:10px">
      <label class="small"><span data-i18n="settings.batch">Embeddings je Anfrage</span>
        <input type="number" id="c-index_batch" min="1" max="512" style="width:90px"></label>
      <span class="small muted" data-i18n="settings.batch.hint">Größer = weniger Anfragen an Ollama.</span>
    </div>
  </div>

  <div class="card">
    <h2 data-i18n="settings.hits.title">Suche</h2>
    <div class="row">
      <label class="small"><span data-i18n="settings.search_results">Treffer je Seite</span>
        <input type="number" id="c-search_results" min="5" max="100" step="5" style="width:80px"></label>
      <span class="small muted" data-i18n="settings.search_results.hint">Mehr heißt weniger Blättern.</span>
    </div>
  </div>

  <div class="card">
    <h2 data-i18n="settings.search.title">KI-Suche (Ollama)</h2>
    <p class="sub" data-i18n="settings.search.sub">Beides läuft in deinem Ollama auf diesem Rechner.</p>
    <div class="row">
      <label class="small"><span data-i18n="settings.ollama">Ollama</span> <input type="text" id="c-ollama" style="width:230px"></label>
      <label class="small"><span data-i18n="settings.embed_model">Embedding-Modell</span> <input type="text" id="c-embed_model" style="width:160px"></label>
    </div>
    <div class="row" style="margin-top:10px">
      <label class="small"><span data-i18n="settings.chat_model">Antwort-Modell</span>
        <input type="text" id="c-chat_model" style="width:210px"></label>
      <label class="small"><span data-i18n="settings.answer_sources">Quellen je Antwort</span>
        <input type="number" id="c-answer_sources" min="1" max="20" style="width:80px"></label>
    </div>
    <p class="small muted" data-i18n="settings.chat_model.hint" style="margin-top:2px"></p>
  </div>

  <div class="card">
    <h2 data-i18n="mcp.title" id="mcp-karte">MCP-Server</h2>
    <p class="sub" data-i18n="mcp.sub">Damit durchsucht Claude das Archiv selbst und antwortet mit Quellenangaben.</p>
    <div class="row">
      <button class="act" id="mcp-toggle" onclick="toggleMcp()">Starten</button>
      <span class="small" id="mcp-state"></span>
    </div>
    <div class="row" style="margin-top:12px">
      <label class="small"><span data-i18n="settings.mcp_port">MCP-Port</span>
        <input type="number" id="c-mcp_port" min="1024" max="65535" style="width:110px"></label>
      <label class="chk"><input type="checkbox" id="c-mcp_autostart">
        <span data-i18n="settings.mcp_autostart">MCP-Server beim Start mitstarten</span></label>
    </div>
    <p class="small muted" style="margin-top:6px" data-i18n="settings.mcp.note">Ein geänderter Port wirkt erst nach einem Neustart des MCP-Servers.</p>
    <p class="small muted" style="margin-top:14px" data-i18n-html="mcp.code.note">In Claude Code eintragen:</p>
    <div class="mitkopie"><pre id="mcp-json"></pre>
      <button class="mini kopie" onclick="kopiere('mcp-json', this)" data-i18n="copy">Kopieren</button></div>
    <p class="small muted" data-i18n-html="mcp.desktop.note">Claude Desktop akzeptiert nur <code>command</code>-Einträge:</p>
    <div class="mitkopie"><pre id="mcp-stdio"></pre>
      <button class="mini kopie" onclick="kopiere('mcp-stdio', this)" data-i18n="copy">Kopieren</button></div>
    <p class="small muted" data-i18n="mcp.paths.note">Die Pfade folgen dem Datenordner.</p>
  </div>

  <div class="card">
    <h2 data-i18n="settings.dirs.title">Ordner</h2>
    <p class="sub" data-i18n="settings.dirs.sub">Relativ zum Datenordner, oder absolut.</p>
    <div class="row">
      <label class="small"><span data-i18n="settings.dir.teams">Teams</span> <input type="text" id="c-teams_dir" style="width:200px"></label>
      <label class="small"><span data-i18n="settings.dir.outlook">Outlook</span> <input type="text" id="c-outlook_dir" style="width:200px"></label>
      <label class="small"><span data-i18n="settings.dir.store">Index</span> <input type="text" id="c-store_dir" style="width:200px"></label>
    </div>
    <p class="small muted" style="margin-top:10px"><span data-i18n="settings.datadir">Datenordner:</span> <code id="data-dir2">…</code></p>
    <div class="row" style="margin-top:8px">
      <input type="text" id="c-data-dir" style="flex:1;min-width:280px">
      <button class="mini" onclick="setzeDatenordner()" data-i18n="settings.datadir.save">Übernehmen</button>
      <button class="mini" onclick="datenordnerZurueck()" data-i18n="settings.datadir.reset">Standard</button>
      <span class="small" id="datadir-msg"></span>
    </div>
    <p class="small muted" data-i18n="settings.datadir.note">Ein Wechsel verschiebt nichts.</p>
  </div>

  <div class="card">
    <h2 data-i18n="settings.lang.title">Sprache</h2>
    <p class="sub" data-i18n="settings.lang.sub">Gilt für diese Oberfläche.</p>
    <select id="c-language" style="min-width:240px"></select>
  </div>

  <div class="card">
    <h2 data-i18n="update.title">Version</h2>
    <p class="small" id="update-current"></p>
    <p class="small muted" id="update-state"></p>
    <label class="chk"><input type="checkbox" id="c-update_check">
      <span data-i18n="update.enabled">Beim Start nach Aktualisierungen sehen</span></label>
    <p class="small muted" data-i18n="update.enabled.hint" style="margin:2px 0 12px 26px"></p>
    <div class="row">
      <button class="ghost" onclick="pruefeUpdate()" data-i18n="update.check">Jetzt prüfen</button>
      <a class="ghost" id="update-link" target="_blank" rel="noopener"
         style="text-decoration:none" data-i18n="update.open">Zur Releases-Seite</a>
    </div>
  </div>
  <div class="row" style="margin-bottom:24px">
    <button class="act" onclick="speichereEinstellungen()" data-i18n="settings.save">Einstellungen speichern</button>
    <span class="small" id="cfg-msg"></span>
  </div>
</section>
</main>


<!-- Das Protokoll gehört zur Anwendung, nicht zum Export-Reiter: darin stehen
     auch Token-Zustand, MCP-Ausgabe und Meldungen des Zeitplans. Deshalb eine
     Leiste am unteren Rand, von überall erreichbar und normalerweise zu. -->
<div id="protokoll" class="zu">
  <div class="pkopf" onclick="protokollUmschalten()">
    <span class="pfeil" id="p-pfeil">▴</span>
    <strong data-i18n="log.title">Protokoll</strong>
    <span class="small muted" id="log-letzte"></span>
  </div>
  <div id="log"></div>
</div>

<div id="overlay"><div class="modal" id="modal" role="dialog" aria-modal="true"></div></div>

<script type="application/json" id="i18n">/*__I18N__*/</script>
<script>
var S = null, seen = 0, dismissed = {}, offset = 0, wizardOffen = null, wizardStand = null;

/* ---------- Sprache ----------
   Die Texte kommen fertig mit der Seite (window.I18N) – kein zusätzlicher
   Abruf, und damit auch kein kurzes Aufblitzen der falschen Sprache. */
var I18N = JSON.parse(document.getElementById('i18n').textContent);
var STR = I18N.strings || {};
var LOC = I18N.lang || 'de';
function t(key, vars){
  var text = STR[key];
  if(text == null) return key;          // fehlender Schlüssel: sichtbar statt leer
  if(vars) Object.keys(vars).forEach(function(k){
    text = text.split('{' + k + '}').join(vars[k]);
  });
  return text;
}
/* Meldungen vom Server sind entweder roher Text (Ausgabe der Export-Skripte)
   oder {k: Schlüssel, v: Werte}. Ein Wert darf selbst wieder so eine Meldung
   sein – so bleibt "Zeitplan aktiv (alle 60 Minuten)" ein Satz statt drei
   Bruchstücken. Enthält v ein `minutes`, wird daraus zusätzlich `rest` als
   lesbare Dauer gebildet: die Sprache kennt nur die Oberfläche. */
function mtext(m){
  if(m == null) return '';
  if(typeof m === 'string') return m;
  if(!m.k) return String(m);
  var v = {};
  Object.keys(m.v || {}).forEach(function(k){ v[k] = mtext(m.v[k]); });
  if(m.v && m.v.minutes !== undefined) v.rest = restzeit(m.v.minutes);
  return t(m.k, v);
}
function restzeit(min){
  if(min === null || min === undefined) return t('unit.unknown');
  if(min < 0) return t('unit.expired');
  if(min < 60) return t('unit.min', {n: min});
  var h = Math.floor(min / 60), m = min % 60;
  if(h < 24) return m ? t('unit.hoursmin', {h: h, m: m}) : t('unit.hours', {h: h});
  var d = Math.floor(h / 24); h = h % 24;
  return h ? t('unit.dayshours', {d: d, h: h}) : t('unit.days', {d: d});
}
function fuelleSprachen(){
  var sel = el('c-language');
  sel.innerHTML = '<option value="auto">' + esc(t('settings.lang.auto')) + '</option>' +
    (I18N.languages || []).map(function(l){
      return '<option value="' + esc(l.code) + '">' + esc(l.name) + '</option>';
    }).join('');
  sel.value = (S && S.config && S.config.language) || 'auto';
}
function uebersetzeSeite(){
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    el.textContent = t(el.dataset.i18n);
  });
  // Texte mit Auszeichnung (<code>, <strong>) – die Sprachdatei liefert HTML.
  document.querySelectorAll('[data-i18n-html]').forEach(function(el){
    el.innerHTML = t(el.dataset.i18nHtml);
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(function(el){
    el.placeholder = t(el.dataset.i18nPh);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(function(el){
    el.title = t(el.dataset.i18nTitle);
  });
  document.title = t('app.title');
  document.documentElement.lang = LOC;
}
uebersetzeSeite();

function api(p){ return fetch(p).then(function(r){ return r.json(); }); }

/* In die Zwischenablage. Auf 127.0.0.1 gilt die Seite als vertrauenswürdig,
   die Zwischenablage-Schnittstelle steht also zur Verfügung – aber nicht in
   jedem Browser und nicht, wenn das Fenster gerade nicht im Vordergrund ist.
   Deshalb der alte Weg als Rückfall, statt still nichts zu tun. */
function kopiere(id, knopf){
  var text = el(id).textContent || '';
  function fertig(ok){
    var vorher = knopf.textContent;
    knopf.textContent = t(ok ? 'copy.done' : 'copy.failed');
    setTimeout(function(){ knopf.textContent = vorher; }, 1600);
  }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(function(){ fertig(true); },
                                            function(){ altKopieren(text, fertig); });
  } else {
    altKopieren(text, fertig);
  }
}
function altKopieren(text, fertig){
  try {
    var feld = document.createElement('textarea');
    feld.value = text;
    feld.style.position = 'fixed';
    feld.style.opacity = '0';
    document.body.appendChild(feld);
    feld.select();
    var ok = document.execCommand('copy');
    document.body.removeChild(feld);
    fertig(ok);
  } catch(e){ fertig(false); }
}
function post(p, body){
  return fetch(p, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body || {})}).then(function(r){ return r.json(); });
}
function esc(s){ return String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function el(id){ return document.getElementById(id); }

/* Drei Reiter, mehr braucht es nicht: Daten holen, Daten ansehen, einstellen.
   Kalender und Adressbuch sind Sichten auf denselben Bestand wie die Suche und
   liegen deshalb eine Ebene darunter; Zeitplan und MCP sind Einstellungen. */
var KANN_VERLAUF = false;   // hängt am Index, siehe store.features
var REITER = ['export', 'suche', 'analytics', 'einstellungen'];
var SICHTEN = ['treffer', 'kalender', 'adressbuch'];
var offeneSicht = 'treffer';

function tab(name){
  REITER.forEach(function(t){
    el('tab-' + t).classList.toggle('hide', t !== name);
    document.querySelector('[data-tab=' + t + ']').classList.toggle('on', t === name);
  });
  if(name === 'suche'){ sicht(offeneSicht); ladeOrdner(); }
  if(name === 'analytics') ladeAnalytics();
}

/* „Gelöschtes“ war ein Häkchen zwischen fünf Filtern und ist in Wahrheit eine
   eigene Sicht auf denselben Bestand – wie Kalender und Adressbuch. Es teilt
   sich deren Trefferliste, setzt aber den Filter. */
function sicht(name){
  offeneSicht = name;
  var geloescht = name === 'geloescht';
  var zeigt = geloescht ? 'treffer' : name;
  SICHTEN.forEach(function(v){
    el('sicht-' + v).classList.toggle('hide', v !== zeigt);
  });
  document.querySelectorAll('#sichten .chip').forEach(function(c){
    c.classList.toggle('on', c.dataset.sicht === name);
  });
  if(zeigt === 'treffer' && el('f-gone').checked !== geloescht){
    el('f-gone').checked = geloescht;
    doSearch(0);
  }
  // Die Kalenderdaten sind ein paar Megabyte – erst holen, wenn jemand hinsieht.
  if(name === 'kalender' || name === 'adressbuch') ladeKalender(name);
}

/* Wer nichts filtert – der Normalfall – soll ein Suchfeld und einen Knopf
   sehen. Die Zahl am Schalter sagt, dass darunter etwas eingestellt ist;
   ohne sie wäre ein zugeklappter Filter eine Falle. */
function filterFelder(){
  return [el('f-person').value.trim(), el('f-source').value === 'all' ? '' : el('f-source').value,
          el('f-from').value, el('f-to').value, el('f-folder').value].filter(Boolean);
}
function filterUmschalten(){
  var zu = el('filter').classList.toggle('hide');      // true = jetzt versteckt
  el('filter-auf').setAttribute('aria-expanded', zu ? 'false' : 'true');
}
function filterLeeren(){
  el('f-person').value = ''; el('f-source').value = 'all';
  el('f-from').value = ''; el('f-to').value = ''; el('f-folder').value = '';
  zeigeFilterstand();
  doSearch(0);
}
function zeigeFilterstand(){
  var n = filterFelder().length;
  el('filter-auf').textContent = n ? t('search.filter.n', {n: n}) : t('search.filter');
  el('filter-weg').classList.toggle('hide', !n);
}

function zeigeEinstellung(anker){
  // Die Kachel oben führt weiterhin direkt zu ihrem Thema – nur liegt das
  // jetzt in den Einstellungen statt in einem eigenen Reiter.
  tab('einstellungen');
  var ziel = document.getElementById(anker);
  if(ziel) ziel.scrollIntoView({behavior: 'smooth', block: 'start'});
}

/* ---------- Status ----------
   Beschriftung in Alltagssprache, Fachbegriff im Tooltip. Wer „Chunks“ oder
   „MCP“ sucht, findet es beim Darüberfahren; wer die Wörter nicht kennt, muss
   sie nicht lesen, um den Zustand zu verstehen. */
function setPill(id, cls, text, tip){
  el('p-' + id).className = 'dot ' + cls;
  el('p-' + id + '-t').textContent = text;
  var knopf = el('pill-' + id);
  if(knopf) knopf.title = tip || '';
}

function renderStatus(s){
  var first = S === null;
  S = s;

  var tok = s.token, tokTip = t('pill.token.tip');
  if(tok.account) tokTip += '\n' + tok.account;
  if(!tok.present) setPill('token','err', t('pill.token.missing'), tokTip);
  else if(tok.expired) setPill('token','err', t('pill.token.expired'), tokTip);
  else if(tok.missing && tok.missing.length) setPill('token','warn', t('pill.token.scopes'), tokTip);
  else if(tok.expires_in_minutes != null) setPill('token','ok', t('pill.token.left', {rest: restzeit(tok.expires_in_minutes)}), tokTip);
  else setPill('token','ok', t('pill.token.set'), tokTip);

  var o = s.ollama;
  setPill('ollama', o.running ? (o.has_model ? 'ok' : 'warn') : 'err',
    o.running ? (o.has_model ? t('pill.ollama.ready') : t('pill.ollama.model')) : t('pill.ollama.off'),
    t('pill.ollama.tip'));

  // Der Zustand des Index stand einmal als Kachel im Kopf. Er steht jetzt im
  // Analytics-Reiter, wo auch alles andere über den Bestand steht – zweimal
  // dieselbe Zahl an zwei Orten hilft niemandem, sie widersprechen sich nur
  // irgendwann. Was der Kopf zeigt, sind Dinge, die eine Handlung verlangen.
  var st = s.store;

  setPill('mcp', s.mcp.running ? 'ok' : '',
    t(s.mcp.running ? 'pill.mcp.on' : 'pill.mcp.off'), t('pill.mcp.tip'));

  /* Export-Tab */
  if(first){
    fill('cat-outlook', ['mail','calendar','contacts'], s.config.outlook_categories, 'o');
    fill('cat-teams', ['1on1','group','meeting','channels'], s.config.teams_categories, 't');
    el('c-onedrive_enabled').checked = !!s.config.onedrive_enabled;
    fuelleSprachen();
    el('s-enabled').checked = s.config.schedule.enabled;
    el('s-interval').value = s.config.schedule.interval_minutes;
    el('s-outlook').checked = s.config.schedule.outlook;
    el('s-teams').checked = s.config.schedule.teams;
    el('s-index').checked = s.config.schedule.index;
    el('s-calendar').checked = s.config.schedule.calendar;
  }
  el('teams-note').textContent = checked('t').indexOf('channels') >= 0
    ? t('export.channels.note') : '';

  var ex = s.exports, parts = [];
  function wann(iso){ return iso ? t('export.state.last', {when: fmt(iso)}) : t('export.state.never'); }
  parts.push(t('export.state.outlook', {when: wann(ex.outlook.last_run)}));
  parts.push(t('export.state.teams', {when: wann(ex.teams.last_run)}));
  parts.push(t('export.state.onedrive', {when: wann(ex.onedrive && ex.onedrive.last_run)}));
  parts.push(t('export.state.index', {when: st.exists ? fmt(st.built_at) : t('export.state.never')}));
  el('export-state').textContent = parts.join('  ·  ');
  // Die Sicht „Gelöschtes“ und der Verlauf brauchen einen Index, der beides
  // kennt. Ein alter kennt die Spalten nicht – dann gibt es den Chip nicht.
  var kann = (st.features || []);
  el('chip-geloescht').classList.toggle('hide', kann.indexOf('gone') < 0);
  KANN_VERLAUF = kann.indexOf('thread') >= 0;
  zeigeOrdnerstand(s.folders || {});
  zeigeOrdnerstand(s.folders_onedrive || {}, 'od-folders-state');
  el('data-dir2').textContent = s.data_dir;
  // Nur beim ersten Zeichnen füllen – sonst überschriebe der Statusabruf alle
  // 2,5 Sekunden, was gerade getippt wird.
  if(first) el('c-data-dir').value = s.data_dir;
  zeigeUpdate(s.update || {});
  fuelleEinstellungen(s.config);

  var busy = s.jobs.busy;
  el('btn-run').disabled = busy;
  el('btn-cancel').classList.toggle('hide', !busy);
  zeigeFortschritt(s.jobs);

  /* Zeitplan / MCP */
  el('s-next').textContent = s.schedule_enabled && s.schedule_next
    ? t('sched.next', {when: fmt(s.schedule_next)}) : t('sched.none');
  el('mcp-toggle').textContent = t(s.mcp.running ? 'mcp.stop' : 'mcp.start');
  el('mcp-state').textContent = s.mcp.running
    ? t('mcp.running', {url: s.mcp.url, mode: t(st.semantic ? 'mcp.mode.hybrid' : 'mcp.mode.lexical')})
    : (mtext(s.mcp.error) || t('mcp.stopped'));
  el('mcp-json').textContent = JSON.stringify(s.mcp.config.http, null, 2);
  el('mcp-stdio').textContent = JSON.stringify(s.mcp.config.stdio, null, 2);
  // Die Antwort gibt es nur, wenn auch ein Modell sie formulieren kann.
  var kiMoeglich = !!(o.running && o.has_chat_model && st.exists);
  el('ai-wrap').classList.toggle('hide', !kiMoeglich);
  if(!kiMoeglich) el('ai-on').checked = false;

  // Nach einem Neuaufbau die Kalenderdaten verwerfen, sonst zeigten Kalender
  // und Adressbuch weiter den Stand von vor dem Lauf.
  if(kalGeladen && kalStand && s.calendar && s.calendar.built_at &&
     s.calendar.built_at !== kalStand){
    kalGeladen = false; kalStand = null;
    var reiter = document.querySelector('nav [data-tab].on');
    if(reiter && reiter.dataset.tab === 'suche' &&
       (offeneSicht === 'kalender' || offeneSicht === 'adressbuch'))
      ladeKalender(offeneSicht);
  }

  if(wizardOffen && !WIZARDS[wizardOffen]){ /* eigenes Fenster – nicht anfassen */ }
  else if(s.wizard && !dismissed[s.wizard]) openWizard(s.wizard);
  else if(wizardOffen) openWizard(wizardOffen);   // offenen Assistenten aktuell halten
  // Das zweite ist nicht optional: sobald das Modell geladen ist, verlangt der
  // Server keinen Assistenten mehr (s.wizard === null). Ohne diesen Zweig
  // stünde im offenen Fenster für immer „Modell fehlt“, während die Ampel im
  // Kopf längst grün ist.
}

function fmt(iso){
  if(!iso) return '–';
  var d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(LOC, {dateStyle:'short', timeStyle:'short'});
}

function fill(id, keys, active, pre){
  el(id).innerHTML = keys.map(function(k){
    return '<label class="chk"><input type="checkbox" id="' + pre + '-' + k + '" value="' +
      k + '"' + (active.indexOf(k) >= 0 ? ' checked' : '') +
      ' onchange="saveCats()"> ' + esc(t('export.cat.' + k)) + '</label>';
  }).join('');
}
function checked(pre){
  return Array.prototype.slice.call(document.querySelectorAll('#cat-' +
    (pre === 'o' ? 'outlook' : 'teams') + ' input:checked')).map(function(i){ return i.value; });
}
function saveCats(){
  post('/api/config', {outlook_categories: checked('o'), teams_categories: checked('t'),
                       onedrive_enabled: el('c-onedrive_enabled').checked}).then(refresh);
}

/* ---------- Läufe ---------- */
function run(what, label){
  what.label = label;
  post('/api/run', what).then(function(r){
    if(!r.ok) alert(mtext(r.message));
    refresh();
  });
}
function runExport(){
  var o = checked('o').length > 0, tm = checked('t').length > 0;
  var od = el('c-onedrive_enabled').checked;
  if(!o && !tm && !od){ alert(t('export.nothing')); return; }
  // Kalender nur mit Outlook: Termine, Kontakte und die Rekonstruktion
  // gelöschter Termine stammen ausschließlich aus dem Postfach.
  run({outlook:o, teams:tm, onedrive:od, index:true, calendar:o}, t('job.export'));
}

/* ---------- Fortschritt ----------
   Zwei Ebenen: der wievielte Schritt von wie vielen, und innerhalb des
   Schrittes so genau, wie das Skript es weiß. Der Outlook-Export kennt seine
   Gesamtzahl nicht – er entdeckt die Mails erst im Laufen. Dort läuft der
   Balken gestreift weiter und die Zeile nennt die Zahl, statt eine Prozent-
   angabe zu erfinden, die niemand halten kann. */
function zeigeFortschritt(jobs){
  var kasten = el('fortschritt'), balken = document.querySelector('.balken');
  kasten.classList.toggle('hide', !jobs.busy);
  if(!jobs.busy){
    var L = jobs.last;
    el('fortschritt-text').textContent = '';
    if(L) el('log-letzte').textContent = L.ok
      ? t('log.job.done', {label: mtext(L.label), when: fmt(L.finished)})
      : t('log.job.failed', {label: mtext(L.label), when: fmt(L.finished),
                             detail: mtext(L.detail)});
    return;
  }
  var j = jobs.job || {}, n = (j.steps || []).length || 1, i = j.index || 0;
  var p = j.progress, anteil = 0, kennt = false;
  if(p && p.total){ anteil = Math.min(p.done / p.total, 1); kennt = true; }

  balken.classList.toggle('unbekannt', !kennt);
  if(kennt) el('balken-fuell').style.width = Math.round((i + anteil) / n * 100) + '%';

  // Der Name des Schrittes kommt als Textschlüssel vom Server (job.step.…);
  // das Etikett des Laufs hat der Browser schon übersetzt, bevor er ihn
  // startete. mtext reicht Zeichenketten unverändert durch – für den Schritt
  // stand deshalb der Schlüssel selbst in der Zeile.
  var zeile = t('log.job.running', {label: mtext(j.label), step: t(j.step),
                                    i: i + 1, n: n});
  if(p) zeile += ' · ' + (p.total
    ? t('progress.of', {done: p.done.toLocaleString(LOC),
                        total: p.total.toLocaleString(LOC), what: einheit(p.what)})
    : t('progress.count', {done: p.done.toLocaleString(LOC), what: einheit(p.what)}));
  el('fortschritt-text').textContent = zeile;
  el('log-letzte').textContent = zeile;
}
function einheit(was){
  return was ? t('progress.unit.' + was) : '';
}

/* ---------- Protokoll ---------- */
function protokollUmschalten(){
  var p = el('protokoll');
  p.classList.toggle('zu');
  try { localStorage.setItem('protokoll', p.classList.contains('zu') ? 'zu' : 'auf'); } catch(e){}
  if(!p.classList.contains('zu')){
    var box = el('log'); box.scrollTop = box.scrollHeight;
  }
}
function stelleProtokollHer(){
  try {
    if(localStorage.getItem('protokoll') === 'auf') el('protokoll').classList.remove('zu');
  } catch(e){}
}

function pullLog(){
  if(beendet) return;
  api('/api/log?since=' + seen).then(function(r){
    if(!r.lines || !r.lines.length){ seen = r.seq; return; }
    var box = el('log'), atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    r.lines.forEach(function(l){
      var d = document.createElement('div');
      d.className = 'l-' + l.level;
      d.textContent = l.t + '  ' + mtext(l.text);
      box.appendChild(d);
    });
    while(box.childElementCount > 1200) box.removeChild(box.firstChild);
    if(!(S && S.jobs && S.jobs.busy)){
      var letzte = r.lines[r.lines.length - 1];
      el('log-letzte').textContent = mtext(letzte.text);
    }
    seen = r.seq;
    if(atEnd) box.scrollTop = box.scrollHeight;
  });
}

/* ---------- Suche ---------- */
/* Die Ordnerliste einmal holen: sie ändert sich nur beim Indizieren, und ein
   Auswahlfeld, das bei jedem Tastendruck nachlädt, wäre reine Last. */
var ordnerGeladen = false;
function ladeOrdner(){
  if(ordnerGeladen) return;
  ordnerGeladen = true;
  api('/api/folders?limit=300').then(function(r){
    var sel = el('f-folder'), gewaehlt = sel.value;
    var h = '<option value="">' + esc(t('search.folder.all')) + '</option>';
    (r.folders || []).forEach(function(f){
      h += '<option value="' + esc(f.path) + '">' + esc(f.path) +
           ' (' + f.messages.toLocaleString(LOC) + ')</option>';
    });
    sel.innerHTML = h;
    sel.value = gewaehlt;
  }).catch(function(){ ordnerGeladen = false; });
}

function trefferProSeite(){
  var n = S && S.config ? parseInt(S.config.search_results, 10) : NaN;
  return isNaN(n) ? 20 : Math.max(5, Math.min(n, 100));
}
function doSearch(off){
  offset = off || 0;
  var proSeite = trefferProSeite();
  var p = new URLSearchParams({q: el('q').value, person: el('f-person').value,
    source: el('f-source').value, from: el('f-from').value, to: el('f-to').value,
    gone: el('f-gone').checked ? '1' : '', folder: el('f-folder').value,
    k: proSeite, offset: offset});
  zeigeFilterstand();
  el('results').textContent = t('search.running');
  api('/api/search?' + p.toString()).then(function(r){
    renderHits(r);
    // Erst die Treffer, dann die Antwort: die Suche ist sofort da, das Modell
    // braucht eine Minute. Wer eine Rechnungsnummer sucht, soll nicht warten.
    if(el('ai-on').checked && (r.results || []).length) frageKI();
    else abbrechenKI();
  });
}
function renderHits(r){
  if(r.error){ el('results').innerHTML = '<span class="err">' + esc(mtext(r.error)) + '</span>'; return; }
  var hits = r.results || [];
  if(!hits.length){ el('results').textContent = t('search.nohits');
                    el('pager').innerHTML = ''; abbrechenKI(); return; }
  el('results').innerHTML = hits.map(function(h, i){
    var m = /^o365:\/\/([^/]+)\/(.*)$/.exec(h.uri || '');
    var link = m ? '/source?root=' + m[1] + '&path=' + m[2] : null;
    return '<div class="hit" id="treffer-' + (i + 1) + '">' +
      '<h3><span class="fussnote">[' + (i + 1) + ']</span>' +
      (link ? '<a href="' + link + '" target="_blank">' : '') +
      esc(h.title || t('search.nosubject')) + (link ? '</a>' : '') + '</h3>' +
      '<div class="meta"><span class="tag">' + esc(h.source_label) + '</span>' +
      (h.gone ? '<span class="tag weg" title="' +
        esc(t('search.gone.since', {when: fmt(h.gone)})) + '">' +
        esc(t('search.gone.tag')) + '</span>' : '') +
      esc(h.who || '') + ' · ' + esc(h.date || '') + '</div>' +
      '<div class="prev">' + esc(h.preview || '') + '…</div>' +
      (h.thread && KANN_VERLAUF ? '<div class="verlauf" id="verlauf-' + (i + 1) + '">' +
        '<button class="mini" onclick="zeigeVerlauf(' + (i + 1) + ',\'' +
        esc(h.thread).replace(/'/g, "\\'") + '\')">' +
        esc(t('search.thread.show')) + '</button></div>' : '') +
      '</div>';
  }).join('');
  // Auch das Blättern richtet sich nach der Einstellung – sonst übersprünge
  // „Weiter“ Treffer oder zeigte dieselben noch einmal.
  var proSeite = trefferProSeite();
  el('pager').innerHTML =
    (offset > 0 ? '<button class="ghost" onclick="doSearch(' + Math.max(0, offset - proSeite) + ')">' + esc(t('search.back')) + '</button>' : '') +
    (hits.length >= proSeite ? '<button class="ghost" onclick="doSearch(' + (offset + proSeite) + ')">' + esc(t('search.next')) + '</button>' : '');
  // Hier stand "Ranking: hybrid". Bei einer Suche ohne Begriff gibt es gar
  // kein Ranking, also stand meistens ein Strich da; und "hybrid" ist ein
  // Wort für Entwickler. Ob die Bedeutungssuche läuft, sagt die Kachel oben.
}

/* Ein Treffer allein sagt oft zu wenig: „Ja, machen wir so“ ist erst mit der
   Frage davor eine Aussage. Der Verlauf klappt deshalb unter dem Treffer auf,
   statt in eine andere Ansicht zu springen. */
function zeigeVerlauf(nr, schluessel){
  var kasten = el('verlauf-' + nr);
  if(!kasten) return;
  kasten.innerHTML = '<p class="hint">' + esc(t('cal.loading')) + '</p>';
  api('/api/thread?key=' + encodeURIComponent(schluessel)).then(function(r){
    if(r.error || !(r.messages || []).length){
      kasten.innerHTML = '<p class="hint">' + esc(t('search.thread.alone')) + '</p>';
      return;
    }
    kasten.innerHTML = '<div class="verlaufliste"><p class="small muted">' +
      esc(t('search.thread.count', {n: r.count})) + '</p>' +
      r.messages.map(function(m){
        var g = /^o365:\/\/([^/]+)\/(.*)$/.exec(m.uri || '');
        var link = g ? '/source?root=' + g[1] + '&path=' + g[2] : null;
        return '<div class="vzeile"><span class="vdatum">' + esc(m.date || '') + '</span>' +
          '<span class="vwer">' + esc(m.who || '') + '</span>' +
          (link ? '<a href="' + link + '" target="_blank">' : '<span>') +
          esc(m.title || t('search.nosubject')) + (link ? '</a>' : '</span>') +
          '</div>';
      }).join('') + '</div>';
  }).catch(function(e){
    kasten.innerHTML = '<p class="hint">' + esc(String(e)) + '</p>';
  });
}

/* =======================================================================
   Kalender und Adressbuch – Ansichten aus combined_search.py, hier gegen
   /api/calendar statt gegen eingebettete Daten. Die Auswertung selbst
   (inklusive der aus Mails rekonstruierten Termine) macht combined_search.py.
   ======================================================================= */
var KAL = null, kalGeladen = false, kTimer = null, kalStand = null;
var DAYMS = 86400000;
/* Wochentage und Monatsnamen liefert der Browser für die gewählte Sprache –
   sie gehören nicht in die Sprachdateien. */
var WD = wochentage(), MON = monatsnamen();
var STATI = ['confirmed','tentative','cancelled','deleted','gone'];
function wochentage(){
  var f = new Intl.DateTimeFormat(LOC, {weekday: 'short'});
  return [0,1,2,3,4,5,6].map(function(i){ return f.format(new Date(Date.UTC(2024, 0, 1 + i))); });
}
function monatsnamen(){
  var f = new Intl.DateTimeFormat(LOC, {month: 'long'});
  return [0,1,2,3,4,5,6,7,8,9,10,11].map(function(i){ return f.format(new Date(Date.UTC(2024, i, 15))); });
}
function stl(st){ return t('cal.st.' + (STATI.indexOf(st) >= 0 ? st : 'confirmed')); }
var events = [], byDay = new Map(), REBUILT = [], contacts = [];
var calMode = 'week', cursor = new Date(), rbSt = 'all';

function toks(q){ return q.toLowerCase().split(/\s+/).filter(Boolean); }
function allIn(hay, worte){ hay = (hay||'').toLowerCase();
  return worte.every(function(x){ return hay.indexOf(x) >= 0; }); }
function quelle(r){ return '/source?root=' + encodeURIComponent(r.root||'outlook') +
                           '&path=' + encodeURIComponent(r.rel||''); }

function ladeKalender(ziel){
  if(kalGeladen) return zeichneKalenderTeil(ziel);
  kalGeladen = true;
  api('/api/calendar').then(function(d){
    if(d.error){
      var h = '<p class="hint">' + esc(mtext(d.error)) + '</p>' +
        '<button class="act" onclick="run({calendar:true}, t(&quot;job.calendar&quot;))">' +
        esc(t('cal.build.now')) + '</button>';
      el('kalBox').innerHTML = h; el('kbBox').innerHTML = h;
      kalGeladen = false;                     // nach dem Aufbau erneut versuchen
      return;
    }
    KAL = d;
    // Denselben Wert merken, den der Status liefert (Dateizeit) – d.generated
    // steht im JSON und wäre nie gleich, der Vergleich schlüge immer an.
    kalStand = (S && S.calendar) ? S.calendar.built_at : null;
    var recs = d.recs || [];
    events = recs.filter(function(r){ return r.src === 'kalender' && r.ts != null; });
    ladePersonen();
    contacts = recs.filter(function(r){ return r.src === 'kontakte'; })
      .sort(function(a,b){ return (a.title||'').localeCompare(b.title||'',LOC,{sensitivity:'base'}); });
    REBUILT = events.filter(function(r){ return r.st === 'deleted' || r.st === 'gone'; });
    verteileAufTage();
    setzeStartwoche();
    var c = d.counts || {};
    el('kalStats').textContent = t('cal.stats', {n: c.kalender, r: c.rekonstruiert,
                                                 when: fmt(d.generated)});
    zeichneKalenderTeil(ziel);
  }).catch(function(e){
    // Ohne diesen Zweig verschluckt das Promise jeden Fehler und die Ansicht
    // bleibt für immer bei "Wird geladen…" – genau so ist es einmal passiert.
    kalGeladen = false;
    var h = '<p class="hint err">' + esc(String(e && e.message || e)) + '</p>';
    el('kalBox').innerHTML = h; el('kbBox').innerHTML = h;
  });
}
function zeichneKalenderTeil(ziel){
  if(!KAL) return;
  if(ziel === 'adressbuch') drawBook(); else drawCal();
}

// Termine auf Tage verteilen (mehrtägige erscheinen an jedem Tag)
function verteileAufTage(){
  byDay = new Map();
  events.forEach(function(r){
    var s = midnight(r.ts * 1000);
    var endMs = (r.te != null ? r.te : r.ts) * 1000;
    if(r.ad) endMs -= DAYMS;            // DTEND ist bei Ganztags-Terminen exklusiv
    var e = midnight(Math.max(endMs, r.ts * 1000));
    for(var d = new Date(s), n = 0; d <= e && n < 366; d = addDays(d,1), n++){
      var k = dkey(d);
      if(!byDay.has(k)) byDay.set(k, []);
      byDay.get(k).push(r);
    }
  });
  byDay.forEach(function(list){
    list.sort(function(a,b){ return (a.ad?0:1) - (b.ad?0:1) || a.ts - b.ts; });
  });
}
function setzeStartwoche(){
  // Ein Archiv liegt meist in der Vergangenheit: auf den jüngsten Termin springen
  if(!events.length) return;
  var last = events.reduce(function(m,r){ return r.ts > m ? r.ts : m; }, -Infinity);
  if(last * 1000 < midnight(Date.now()).getTime()) cursor = new Date(last * 1000);
}

function dkey(d){ return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') +
                         '-' + String(d.getDate()).padStart(2,'0'); }
function midnight(ms){ var d = new Date(ms); d.setHours(0,0,0,0); return d; }
function addDays(d,n){ var x = new Date(d); x.setDate(x.getDate()+n); return x; }
function startOfWeek(d){ return addDays(midnight(d.getTime()), -((d.getDay()+6)%7)); }
function hhmm(d){ return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0'); }
function isoWeek(d){
  var tag = midnight(d.getTime()); tag.setDate(tag.getDate() + 3 - ((tag.getDay()+6)%7));
  var w1 = new Date(tag.getFullYear(), 0, 4);
  return 1 + Math.round(((tag - w1)/DAYMS - 3 + ((w1.getDay()+6)%7))/7);
}
function evTime(r){
  if(r.ad) return t('cal.allday');
  var s = hhmm(new Date(r.ts*1000));
  if(r.te != null && r.te > r.ts) s += '–' + hhmm(new Date(r.te*1000));
  return s;
}
function evHtml(r){
  var st = STATI.indexOf(r.st) >= 0 ? r.st : 'confirmed';
  var tip = [r.title, stl(st), r.d,
             r.loc ? t('cal.tip.location', {v: r.loc}) : '',
             r.who ? t('cal.tip.organizer', {v: r.who}) : '',
             (r.att && r.att.length) ? t('cal.tip.attendees', {v: r.att.join(', ')}) : '',
             r.ctx].filter(Boolean).join('\n');
  return '<a class="ev ' + st + '" href="' + quelle(r) + '" target="_blank" rel="noopener" title="' +
         esc(tip) + '"><span class="evt">' + esc(evTime(r)) + '</span> ' + esc(r.title) + '</a>';
}
function dayCell(d, extraCls){
  var k = dkey(d), list = byDay.get(k) || [];
  var today = k === dkey(new Date()) ? ' today' : '';
  return '<div class="day' + (extraCls||'') + today + '">' +
         '<div class="dnum"><b>' + d.getDate() + '</b><span class="wd">' + WD[(d.getDay()+6)%7] + '</span></div>' +
         (list.length ? list.map(evHtml).join('') : '') + '</div>';
}

/* Nur aus Mails rekonstruierte Termine – eigene Liste statt Kalenderraster */
function rbRow(r){
  var d = new Date(r.ts*1000);
  return '<a class="rbrow ' + r.st + '" href="' + quelle(r) + '" target="_blank" rel="noopener" title="' +
         esc(r.ctx) + '"><span class="rbdate">' + WD[(d.getDay()+6)%7] + ' ' + esc(r.d) + '</span>' +
         '<span class="rbstate">' + esc(t(r.st === 'deleted' ? 'cal.rb.state.deleted' : 'cal.rb.state.gone')) + '</span>' +
         '<span class="rbtitle">' + esc(r.title) + '</span>' +
         '<span class="rbwho">' + esc(r.who) + '</span></a>';
}
function rbFrame(){
  var nDel = REBUILT.filter(function(r){ return r.st === 'deleted'; }).length;
  el('kalBox').innerHTML =
      '<p class="rbnote">' + esc(t('cal.rb.note')) + '</p><div class="calbar">' +
      '<span class="chip" data-rb="all">' + esc(t('cal.rb.all', {n: REBUILT.length})) + '</span>' +
      '<span class="chip" data-rb="deleted">' + esc(t('cal.rb.deleted', {n: nDel})) + '</span>' +
      '<span class="chip" data-rb="gone">' + esc(t('cal.rb.gone', {n: REBUILT.length - nDel})) + '</span>' +
      '<input type="text" id="rbQ" placeholder="' + esc(t('cal.rb.search.ph')) + '" style="min-width:240px">' +
      '<span class="rbcount"></span></div><div id="rblist"></div>';
  el('rbQ').addEventListener('input', function(){ clearTimeout(kTimer); kTimer = setTimeout(rbList, 160); });
  document.querySelectorAll('#kalBox [data-rb]').forEach(function(ch){
    ch.addEventListener('click', function(){ rbSt = ch.dataset.rb; rbList(); });
  });
}
function rbList(){
  var worte = toks(el('rbQ').value.trim());
  var hits = REBUILT.filter(function(r){
    return (rbSt === 'all' || r.st === rbSt) &&
           (!worte.length || allIn(r.title + ' ' + (r.ppl||'') + ' ' + (r.x||''), worte));
  });
  document.querySelectorAll('#kalBox [data-rb]').forEach(function(c){
    c.classList.toggle('on', c.dataset.rb === rbSt);
  });
  document.querySelector('#kalBox .rbcount').textContent = t('cal.rb.hits', {n: hits.length});
  var h = '', monat = null;
  hits.forEach(function(r){
    var d = new Date(r.ts*1000), m = MON[d.getMonth()] + ' ' + d.getFullYear();
    if(m !== monat){ h += '<div class="rbmonth">' + m + '</div>'; monat = m; }
    h += rbRow(r);
  });
  // Leer heißt nicht immer dasselbe: „nichts gefunden“ wäre gelogen, wenn gar
  // nicht gesucht wurde, weil die Wiederherstellung ausgeschaltet ist.
  var leer = (KAL && KAL.reconstruct === false) ? 'cal.rb.off' : 'cal.rb.empty';
  el('rblist').innerHTML = h || '<p class="hint">' +
    esc(t(REBUILT.length ? 'cal.rb.nohits' : leer)) + '</p>';
}

function drawCal(){
  el('kalNav').classList.toggle('hide', calMode === 'rebuilt');
  el('kalLegend').classList.toggle('hide', calMode === 'rebuilt');   // Zeilen sind beschriftet
  if(calMode === 'rebuilt'){
    el('kalTitle').textContent = '';
    if(!document.querySelector('#kalBox [data-rb]')) rbFrame();
    return rbList();
  }
  if(!events.length){
    el('kalTitle').textContent = '';
    el('kalBox').innerHTML = '<p class="hint">' + esc(t('cal.empty')) + '</p>';
    return;
  }
  var head = '<div class="grid ' + (calMode === 'week' ? 'wk' : 'mo') + ' dowrow" style="margin-bottom:2px">' +
             WD.map(function(w){ return '<div class="dow">' + w + '</div>'; }).join('') + '</div>';
  var cells = '';
  if(calMode === 'week'){
    var mon = startOfWeek(cursor), sun = addDays(mon, 6);
    for(var i = 0; i < 7; i++) cells += dayCell(addDays(mon, i));
    el('kalTitle').textContent = t('cal.kw', {week: isoWeek(mon),
      from: mon.getDate() + '. ' + MON[mon.getMonth()],
      to: sun.getDate() + '. ' + MON[sun.getMonth()] + ' ' + sun.getFullYear()});
  } else {
    var first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
    var start = startOfWeek(first);
    var lastDay = new Date(cursor.getFullYear(), cursor.getMonth()+1, 0);
    var weeks = Math.round((startOfWeek(lastDay) - start)/DAYMS/7) + 1;
    for(var j = 0; j < weeks*7; j++){
      var d = addDays(start, j);
      cells += dayCell(d, d.getMonth() !== cursor.getMonth() ? ' out' : '');
    }
    el('kalTitle').textContent = MON[cursor.getMonth()] + ' ' + cursor.getFullYear();
  }
  el('kalBox').innerHTML = head + '<div class="grid ' + (calMode === 'week' ? 'wk' : 'mo') + '">' + cells + '</div>';
}
el('kalPrev').addEventListener('click', function(){
  cursor = calMode === 'week' ? addDays(cursor, -7)
                              : new Date(cursor.getFullYear(), cursor.getMonth()-1, 1);
  drawCal();
});
el('kalNext').addEventListener('click', function(){
  cursor = calMode === 'week' ? addDays(cursor, 7)
                              : new Date(cursor.getFullYear(), cursor.getMonth()+1, 1);
  drawCal();
});
el('kalToday').addEventListener('click', function(){ cursor = new Date(); drawCal(); });
document.querySelectorAll('#sicht-kalender .calbar .chip[data-mode]').forEach(function(ch){
  ch.addEventListener('click', function(){
    document.querySelectorAll('#sicht-kalender .calbar .chip[data-mode]')
      .forEach(function(x){ x.classList.remove('on'); });
    ch.classList.add('on'); calMode = ch.dataset.mode;
    if(calMode !== 'rebuilt') el('kalBox').innerHTML = '';   // Rahmen der Liste verwerfen
    drawCal();
  });
});

/* ---------- Analytics ----------
   Zwei Karten mit zwei Herkünften: was oben steht, kommt aus dem Index und ist
   sofort da. Was unten steht, fragt Microsoft – und passiert deshalb nur auf
   Knopfdruck. */
var anaGeladen = false;

function ladeAnalytics(neu){
  if(anaGeladen && !neu) return;
  anaGeladen = true;
  api('/api/analytics').then(zeigeAnalytics).catch(function(e){
    el('ana-kpi').innerHTML = '<p class="hint">' + esc(String(e)) + '</p>';
  });
}

function bytes(n){
  if(!n) return '–';
  var e = ['B','KB','MB','GB','TB'], i = 0;
  while(n >= 1024 && i < e.length - 1){ n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + e[i];
}
function zahl(n){
  // null heisst „weiss ich nicht“ – 0 hiesse „keine“.
  return (n === null || n === undefined) ? '–' : Number(n).toLocaleString(LOC);
}
/* Zwei verschiedene Dinge standen bisher gleich aussehend unter jeder Kachel:
   ZAHLEN (die Aufteilung nach Quellen, wie groß der Index ist) und
   ERKLÄRUNGEN (was ein Gespräch ist, warum Gelöschtes noch da liegt). Nur die
   Zahlen gehören dauerhaft hin; die Erklärung liest man einmal. Deshalb bleibt
   `hinweis` sichtbar und `tip` wandert ans Infozeichen. */
function kachelHtml(wert, titel, hinweis, tip, klick){
  var info = tip ? ' <span class="info" tabindex="0" title="' + esc(tip) +
                   '" role="img" aria-label="Info">i</span>' : '';
  return '<div class="kpi' + (klick ? ' klickbar" role="button" tabindex="0"' +
             ' onclick="' + klick + '" onkeydown="if(event.key===\'Enter\')' + klick + '"'
           : '"') + '>' +
    '<div class="kpi-wert">' + esc(wert) + '</div>' +
    '<div class="kpi-titel">' + esc(titel) + info + '</div>' +
    (hinweis ? '<div class="kpi-hint">' + esc(hinweis) + '</div>' : '') + '</div>';
}

function zeigeAnalytics(a){
  if(!a.exists){
    el('ana-kpi').innerHTML = '<p class="hint">' + esc(t('search.sub.none')) + '</p>';
    return;
  }
  var quellen = (a.quellen || []).map(function(q){
    return t('search.source.' + q.src, {}) + ' ' + zahl(q.nachrichten);
  }).join(' · ');
  var zeitraum = (a.von && a.bis)
    ? fmtTag(a.von) + ' – ' + fmtTag(a.bis) : '–';
  var gesamt = (a.groesse || {});
  var dateien = (a.quellen || []).filter(function(q){ return q.src === 'datei'; })[0];
  // Anklickbar nur, wenn es auch etwas zu zeigen gibt – eine Kachel, die bei
  // null Treffern in eine leere Suche führt, ist eine Sackgasse.
  var klick = a.verschwunden ? 'zeigeVerschwundene()' : '';
  el('ana-kpi').innerHTML =
    kachelHtml(zahl(a.nachrichten), t('ana.messages'), quellen) +
    kachelHtml(zahl(a.gespraeche), t('ana.threads'), '', t('ana.threads.hint')) +
    kachelHtml(zahl(a.mit_anhang), t('ana.attachments'), '', t('ana.attachments.hint')) +
    // Ohne Spiegel keine Kachel. „OneDrive-Dateien 0" wäre für alle, die
    // OneDrive nicht nutzen, eine Zeile, die nichts sagt.
    (dateien ? kachelHtml(zahl(dateien.nachrichten), t('ana.files'), '',
                          t('ana.files.hint')) : '') +
    kachelHtml(zahl(a.personen), t('ana.people')) +
    kachelHtml(zahl(a.verschwunden), t('ana.gone'), '',
               t(klick ? 'ana.gone.hint.klick' : 'ana.gone.hint'), klick) +
    kachelHtml(zeitraum, t('ana.period')) +
    kachelHtml(bytes((gesamt.teams || 0) + (gesamt.outlook || 0) +
                     (gesamt.onedrive || 0)), t('ana.size'),
               t('ana.size.hint', {index: bytes(gesamt.index)}));
  zeigeBericht(a.vollstaendigkeit);
  // Zwei Berichte, zwei Kästen: sie entstehen unabhängig voneinander,
  // und einer soll den anderen nicht verdecken.
  zeigeBericht(a.vollstaendigkeit_onedrive, 'ana-check-box-od');
}

function fmtTag(ts){
  return new Date(ts * 1000).toLocaleDateString(LOC, {year: 'numeric', month: 'short'});
}

function zeigeVerschwundene(){
  el('f-gone').checked = true;
  el('q').value = ''; el('f-person').value = '';
  tab('suche'); sicht('treffer'); doSearch(0);
}

function zeigeBericht(b, id){
  var kasten = el(id || 'ana-check-box');
  var od = id === 'ana-check-box-od';
  // Beim Postfach steht der Hinweis "noch nie geprüft"; beim Spiegel bliebe der
  // Kasten sonst dauerhaft stehen, obwohl OneDrive vielleicht gar nicht genutzt wird.
  if(!b){ kasten.innerHTML = od ? '' :
            '<p class="hint">' + esc(t('ana.check.none')) + '</p>'; return; }
  var titel = '<h3 style="margin:14px 0 6px;font-size:14px">' +
              esc(t(od ? 'ana.check.title.onedrive' : 'ana.check.title.mail')) + '</h3>';
  var luecken = (b.ordner || []).filter(function(z){ return z.fehlt > 0; });
  var kopf = titel + '<p class="' + (b.fehlt ? 'warnzeile' : 'okzeile') + '">' +
    esc(t(b.fehlt ? (od ? 'ana.check.gaps.files' : 'ana.check.gaps')
                  : 'ana.check.complete',
          {n: zahl(b.fehlt), erwartet: zahl(b.erwartet), da: zahl(b.vorhanden),
           weg: zahl(b.geloescht)})) + '</p>' +
    '<p class="small muted">' + esc(t('ana.check.when', {when: fmt(b.geprueft)})) + '</p>' +
    // Ohne diese Zeile sähe es aus, als fehlten 20.000 Mails. Sie fehlen
    // nicht – sie wurden nie geholt, weil die Auswahl sie auslässt.
    (b.ausgelassen ? '<p class="small muted">' +
      esc(t(od ? 'ana.check.skipped.files' : 'ana.check.skipped',
            {n: zahl(b.ausgelassen),
                                  ordner: (b.ausgelassene_ordner || []).join(', ')})) +
      '</p>' : '');
  if(!luecken.length){ kasten.innerHTML = kopf; return; }
  kasten.innerHTML = kopf + '<table class="anatab"><thead><tr>' +
    '<th>' + esc(t('ana.check.folder')) + '</th><th>' + esc(t('ana.check.expected')) +
    '</th><th>' + esc(t('ana.check.present')) + '</th><th>' + esc(t('ana.check.missing')) +
    '</th></tr></thead><tbody>' +
    luecken.slice(0, 30).map(function(z){
      return '<tr><td>' + esc(z.ordner) + '</td><td>' + zahl(z.erwartet) +
        '</td><td>' + zahl(z.vorhanden) + '</td><td class="fehlt">' + zahl(z.fehlt) +
        '</td></tr>';
    }).join('') + '</tbody></table>';
}

/* Ein Knopf, nicht zwei. „Prüfen" ist eine Frage an das Archiv, keine an eine
   Quelle – wer zwei Knöpfe sieht, muss erst entscheiden, was er eigentlich
   wissen will. OneDrive kommt aber nur mit, wenn es benutzt wird: sonst wäre
   es eine Netzanfrage für eine Antwort, die niemanden interessiert. */
function nutztOneDrive(){
  return !!((S.config && S.config.onedrive_enabled) ||
            (S.folders_onedrive && S.folders_onedrive.abgeglichen));
}
function pruefeVollstaendigkeit(){
  el('ana-check-state').textContent = t('ana.check.running');
  post('/api/run', {check: true, check_onedrive: nutztOneDrive(),
                    label: 'job.check'}).then(function(r){
    if(!r.ok){ el('ana-check-state').textContent = mtext(r.message); return; }
    warteAufLauf();
  });
}
function warteAufLauf(){
  // Der Lauf meldet sich über den Statusabruf; danach den Bericht neu holen.
  var timer = setInterval(function(){
    if(S && S.jobs && !S.jobs.busy){
      clearInterval(timer);
      el('ana-check-state').textContent = '';
      ladeAnalytics(true);
    }
  }, 1500);
}

/* ---------- Adressbuch ---------- */
function telHref(t){ return 'tel:' + (t||'').replace(/[^\d+]/g, ''); }
function cardHtml(e){
  var r = e.c || {};
  var sub = [r.role, r.org].filter(Boolean).join(' · ');
  // Nur wer aus dem Adressbuch stammt, hat eine Quelldatei zum Verlinken.
  var h = '<div class="card2"><div class="cname">' +
    (e.c ? '<a href="' + quelle(r) + '" target="_blank" rel="noopener">' +
           esc(e.name) + '</a>' : esc(e.name)) +
    (e.quelle === 'comm' ? '<span class="tag herkunft">' + esc(t('book.tag.comm')) +
                           '</span>' : '') + '</div>';
  if(sub) h += '<div class="crole">' + esc(sub) + '</div>';
  (r.em||[]).forEach(function(m){
    h += '<div class="cline"><span>✉</span><a href="mailto:' + esc(m) + '">' + esc(m) + '</a></div>'; });
  (r.tel||[]).forEach(function(x){
    h += '<div class="cline"><span>☎</span><a href="' + esc(telHref(x)) + '">' + esc(x) + '</a></div>'; });
  if(e.n) h += '<div class="cline muted small">' + esc(t('book.messages', {n: e.n.toLocaleString(LOC)})) + '</div>';
  h += '<button class="mini" onclick="zeigeKommunikation(' +
       JSON.stringify(e.name).replace(/"/g, '&quot;') + ')">' +
       esc(t('book.show.comm')) + '</button>';
  return h + '</div>';
}
/* Zwei Quellen für dieselbe Frage „wer ist das?“: das Outlook-Adressbuch
   (.vcf, gepflegt, oft unvollständig) und die Kommunikation selbst (Absender
   und Empfänger, vollständig, aber ohne Telefonnummer). Sie zu mischen, ohne
   es zu sagen, wäre die schlechteste Lösung – deshalb ein Filter darüber. */
var personen = [], personenGeladen = false, bookF = 'all';

function ladePersonen(){
  if(personenGeladen) return;
  personenGeladen = true;
  api('/api/people?limit=2000').then(function(r){
    personen = r.people || [];
    if(offeneSicht === 'adressbuch') drawBook();
  }).catch(function(){ personen = []; });
}

function normName(n){ return (n || '').trim().toLowerCase(); }

function buchEintraege(){
  /* Kontakte gewinnen: sie tragen Firma, Rolle und Telefonnummer. Aus der
     Kommunikation kommt die Zahl der Nachrichten dazu – auch für die, die im
     Adressbuch stehen. */
  var nachName = {};
  contacts.forEach(function(c){
    nachName[normName(c.title)] = {c: c, name: c.title, quelle: 'contacts', n: 0};
  });
  personen.forEach(function(p){
    var k = normName(p.name);
    if(!k) return;
    if(nachName[k]){ nachName[k].n = p.messages; nachName[k].quelle = 'both'; }
    else nachName[k] = {c: null, name: p.name, quelle: 'comm', n: p.messages};
  });
  return Object.keys(nachName).map(function(k){ return nachName[k]; });
}

function drawBook(){
  var alle = buchEintraege();
  if(!alle.length){
    el('kbStats').textContent = '';
    el('kbBox').innerHTML = '<p class="hint">' + esc(t('book.empty')) + '</p>';
    return;
  }
  var imFilter = alle.filter(function(e){
    if(bookF === 'contacts') return e.quelle !== 'comm';
    if(bookF === 'comm') return e.quelle !== 'contacts';
    return true;
  });
  var worte = toks(el('kbQ').value.trim());
  var hits = imFilter.filter(function(e){
    var r = e.c || {};
    return !worte.length || allIn([e.name, r.org, r.role, (r.em||[]).join(' '),
                                   (r.tel||[]).join(' ')].join(' '), worte);
  });
  hits.sort(function(a, b){
    return (a.name || '').localeCompare(b.name || '', LOC, {sensitivity: 'base'});
  });
  document.querySelectorAll('#sicht-adressbuch .calbar .chip[data-book]')
    .forEach(function(c){ c.classList.toggle('on', c.dataset.book === bookF); });
  el('kbStats').textContent = t('book.stats', {n: hits.length, total: alle.length});
  if(!hits.length){ el('kbBox').innerHTML = '<p class="hint">' + esc(t('book.nohits')) + '</p>'; return; }
  var h = '', letter = null;
  hits.forEach(function(e){
    var first = (e.name || '#').trim().charAt(0).toUpperCase();
    var L = /[A-ZÄÖÜ]/.test(first) ? first : '#';
    if(L !== letter){ h += (letter !== null ? '</div>' : '') + '<div class="letter">' + L +
                           '</div><div class="cards">'; letter = L; }
    h += cardHtml(e);
  });
  el('kbBox').innerHTML = h + '</div>';
}
el('kbQ').addEventListener('input', function(){ clearTimeout(kTimer); kTimer = setTimeout(drawBook, 120); });
document.querySelectorAll('#sicht-adressbuch .calbar .chip[data-book]').forEach(function(ch){
  ch.addEventListener('click', function(){ bookF = ch.dataset.book; drawBook(); });
});

/* Von einer Person zu allem, was mit ihr gelaufen ist. Der Personenfilter der
   Suche kann das längst – er war nur nie mit dem Adressbuch verbunden. */
function zeigeKommunikation(name){
  el('f-person').value = name;
  el('q').value = '';
  el('f-source').value = 'all';
  el('f-gone').checked = false;
  sicht('treffer');
  doSearch(0);
}

/* ---------- Formulierte Antwort ----------
   Ergänzt die Treffer, ersetzt sie nicht: die Liste darunter bleibt unberührt,
   und jede Fußnote [1] springt genau dorthin. Was hier steht, hat ein lokales
   Modell aus eben diesen Treffern geschrieben – das sagt der Kasten auch. */
var kiLauf = null, kiQuellen = [];

function merkeKI(){
  // Die Wahl gilt für die nächste Suche, nicht rückwirkend – ein Haken soll
  // nicht ungefragt ein Modell anwerfen.
  try { localStorage.setItem('ki', el('ai-on').checked ? '1' : '0'); } catch(e){}
}
function stelleKIher(){
  try { el('ai-on').checked = localStorage.getItem('ki') === '1'; } catch(e){}
}
function abbrechenKI(){
  if(kiLauf){ kiLauf.abort(); kiLauf = null; }
  el('ai-box').classList.add('hide');
  markiereZitate([]);
}
function kiKopf(modell, laufend){
  // Der Kasten steht über den Treffern – die Kopfzeile muss deshalb in einem
  // Satz sagen, dass hier eine KI schreibt, dass sie über Ollama auf diesem
  // Rechner läuft und dass sie sich auf die Treffer darunter stützt.
  return '<div class="ahead">' +
    '<span class="tag">' + esc(t('search.ai.tag')) + '</span>' +
    '<span>' + esc(t('search.ai.label')) + '</span>' +
    (modell ? '<code class="small">' + esc(t('search.ai.model', {model: modell})) + '</code>' : '') +
    (laufend ? '<button class="ghost" style="margin-left:auto;padding:3px 10px" ' +
               'onclick="abbrechenKI()">' + esc(t('search.ai.stop')) + '</button>' : '') +
    '</div>';
}
function kiFuss(){
  return '<div class="afoot">' + esc(t('search.ai.note')) + '</div>';
}

function frageKI(){
  abbrechenKI();
  var box = el('ai-box');
  box.classList.remove('hide', 'err');
  box.innerHTML = kiKopf('', true) +
    '<div class="atext blink" id="ai-text"></div>';

  kiLauf = new AbortController();
  var text = '', modell = '';
  fetch('/api/answer', {
    method: 'POST', signal: kiLauf.signal,
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({q: el('q').value, person: el('f-person').value,
                          source: el('f-source').value,
                          from: el('f-from').value, to: el('f-to').value})
  }).then(function(r){
    if(!r.ok || !r.body){
      return r.json().then(function(d){ throw new Error(mtext(d.error)); });
    }
    var leser = r.body.getReader(), dekoder = new TextDecoder(), rest = '';
    function weiter(){
      return leser.read().then(function(st){
        if(st.done) return fertig();
        rest += dekoder.decode(st.value, {stream: true});
        var zeilen = rest.split('\n');
        rest = zeilen.pop();
        zeilen.forEach(function(z){
          if(!z.trim()) return;
          var d;
          try { d = JSON.parse(z); } catch(e){ return; }
          if(d.sources){ kiQuellen = d.sources; modell = d.model;
                         box.innerHTML = kiKopf(modell, true) +
                           '<div class="atext blink" id="ai-text"></div>'; }
          if(d.text){ text += d.text; el('ai-text').textContent = text; }
          if(d.error){ throw new Error(t(d.error === 'model'
                         ? 'search.ai.err.model' : 'search.ai.err.ollama',
                         {detail: d.detail || ''})); }
        });
        return weiter();
      });
    }
    function fertig(){
      kiLauf = null;
      box.innerHTML = kiKopf(modell, false) +
        '<div class="atext">' + mitFussnoten(text) + '</div>' + kiFuss();
      markiereZitate(zitierte(text));
    }
    return weiter();
  }).catch(function(e){
    kiLauf = null;
    if(e && e.name === 'AbortError') return;      // vom Benutzer gestoppt
    box.classList.add('err');
    box.innerHTML = kiKopf(modell, false) +
      '<div class="atext">' + esc(String(e && e.message || e)) + '</div>';
  });
}

/* [1] wird zu einem Sprung in die Trefferliste – keine zweite Quellenliste,
   die dieselben Einträge noch einmal zeigt. */
function mitFussnoten(text){
  return esc(text).replace(/\[(\d+)\]/g, function(m, n){
    return kiQuellen[+n - 1]
      ? '<a href="#treffer-' + n + '" onclick="zeigeTreffer(' + n + ');return false;">' + m + '</a>'
      : m;
  });
}
function zitierte(text){
  var raus = [], m, re = /\[(\d+)\]/g;
  while((m = re.exec(text))) if(raus.indexOf(+m[1]) < 0) raus.push(+m[1]);
  return raus;
}
function markiereZitate(nummern){
  document.querySelectorAll('#results .hit').forEach(function(el2, i){
    el2.classList.toggle('zitiert', nummern.indexOf(i + 1) >= 0);
  });
}
function zeigeTreffer(n){
  var el2 = document.getElementById('treffer-' + n);
  if(el2) el2.scrollIntoView({behavior: 'smooth', block: 'center'});
}

/* ---------- Zeitplan / MCP ---------- */
function saveSchedule(){
  post('/api/schedule', {enabled: el('s-enabled').checked,
    interval_minutes: parseInt(el('s-interval').value, 10) || 60,
    outlook: el('s-outlook').checked, teams: el('s-teams').checked,
    index: el('s-index').checked, calendar: el('s-calendar').checked}).then(refresh);
}
function toggleMcp(){
  post('/api/mcp', {action: S.mcp.running ? 'stop' : 'start'}).then(function(r){
    if(!r.ok && r.message) alert(mtext(r.message));
    refresh();
  });
}

/* ---------- Aktualisierungen ----------
   Nur eine Notiz: nichts wird geladen, nichts ersetzt. Gemeldet wird allein
   der Fall "es gibt etwas Neueres" – kein Release, kein Netz oder abgeschaltet
   sind normale Zustände und stehen nur in den Einstellungen. */
/* Drei Lagen, nicht zwei. „Du bist auf dem neuesten Stand" ist falsch, wenn
   die eigene Version HÖHER ist als alles Veröffentlichte – dann läuft hier ein
   selbstgebauter Stand, und das gehört gesagt, nicht verschwiegen. */
function zeigeUpdate(u){
  var banner = el('update-banner');
  var vorab = u.status === 'ok' && u.ahead;
  banner.classList.toggle('hide', !u.newer && !vorab);
  banner.classList.toggle('warn', vorab);
  if(u.newer){
    banner.innerHTML = esc(t('update.banner', {v: u.latest, current: u.current})) +
      ' <a href="' + esc(u.url || u.releases_url || '#') + '" target="_blank" rel="noopener">' +
      esc(t('update.open')) + '</a>';
  } else if(vorab){
    banner.textContent = t('update.ahead.banner', {v: u.current, latest: u.latest});
  }
  el('update-current').textContent = t('update.current', {v: u.current || '?'});
  el('update-state').textContent =
      u.status === 'ok' ? (u.newer ? t('update.available', {v: u.latest})
                         : u.ahead ? t('update.ahead', {v: u.latest})
                                   : t('update.uptodate'))
    : u.status === 'none' ? t('update.none')
    : u.status === 'error' ? t('update.error', {error: u.error || ''})
    : t('update.off');
  el('update-link').href = u.url || u.releases_url || '#';
}
function pruefeUpdate(){
  el('update-state').textContent = t('update.checking');
  post('/api/update-check').then(function(u){ zeigeUpdate(u); refresh(); });
}

/* ---------- Einstellungen ---------- */
var SCHALTER = ['embed_images','cache_images','refresh_channels','skip_empty_chats',
                'include_hidden','calendar_reconstruct','mcp_autostart','update_check'];
var ZAHLEN   = ['workers','index_batch','mcp_port','answer_sources','search_results',
                'onedrive_max_mb'];
var TEXTE    = ['ollama','embed_model','chat_model','teams_dir','outlook_dir','store_dir',
                'folder_rules','onedrive_rules'];
var cfgGefuellt = false;

function fuelleEinstellungen(cfg){
  // Nur einmal befüllen: der Status kommt alle 2,5 Sekunden, und ein Neusetzen
  // würde eine gerade getippte Zahl oder Ordnerliste unter den Fingern ersetzen.
  if(cfgGefuellt) return;
  cfgGefuellt = true;
  SCHALTER.forEach(function(k){ el('c-'+k).checked = !!cfg[k]; });
  ZAHLEN.forEach(function(k){ el('c-'+k).value = cfg[k]; });
  TEXTE.forEach(function(k){ el('c-'+k).value = cfg[k] || ''; });
  el('c-skip_folders').value = (cfg.skip_folders || []).join('\n');
  fuelleSprachen();
}
function speichereEinstellungen(){
  var body = {skip_folders: el('c-skip_folders').value,
              language: el('c-language').value};
  var spracheVorher = (S.config && S.config.language) || 'auto';
  SCHALTER.forEach(function(k){ body[k] = el('c-'+k).checked; });
  ZAHLEN.forEach(function(k){ body[k] = parseInt(el('c-'+k).value, 10); });
  TEXTE.forEach(function(k){ body[k] = el('c-'+k).value.trim(); });
  post('/api/config', body).then(function(r){
    // Die Sprache steckt in der ausgelieferten Seite – ein Wechsel braucht
    // einen Neuaufbau, alles andere wirkt sofort.
    if(r.config.language !== spracheVorher){ location.reload(); return; }
    cfgGefuellt = false;                 // gespeicherte (und begrenzte) Werte zurückspielen
    fuelleEinstellungen(r.config);
    var m = el('cfg-msg');
    m.className = 'small ok';
    m.textContent = t('settings.saved');
    setTimeout(function(){ m.textContent = ''; }, 4000);
    refresh();
  });
}
/* Der Ordnerbaum ist seit 3.0 ein eigenes Ergebnis, kein Nebenprodukt jedes
   Exports. Diese Zeile sagt, wie alt er ist und was die Regeln daraus machen. */
function zeigeOrdnerstand(f, id){
  var kasten = el(id || 'folders-state');
  if(!kasten) return;
  if(!f.abgeglichen){ kasten.textContent = t('folders.none'); return; }
  var text = t(id ? 'folders.state.files' : 'folders.state',
                              {an: (f.ordner_gewaehlt || 0).toLocaleString(LOC),
                                 gesamt: (f.ordner_gesamt || 0).toLocaleString(LOC),
                                 mails: (f.mails_gewaehlt || 0).toLocaleString(LOC),
                                 when: fmt(f.abgeglichen)});
  if((f.neu || []).length) text += ' ' + t('folders.new', {n: f.neu.length});
  kasten.textContent = text;
}

function gleicheOrdnerAb(quelle){
  var od = quelle === 'onedrive';
  var kasten = od ? 'od-folders-msg' : 'folders-msg';
  el(kasten).textContent = t('folders.syncing');
  post('/api/run', od ? {sync_onedrive: true, label: 'job.folders'}
                      : {sync_folders: true, label: 'job.folders'}).then(function(r){
    if(!r.ok){ el(kasten).textContent = mtext(r.message); return; }
    var timer = setInterval(function(){
      if(S && S.jobs && !S.jobs.busy){
        clearInterval(timer);
        el(kasten).textContent = '';
      }
    }, 1500);
  });
}

function setzeDatenordner(pfad){
  var ziel = pfad !== undefined ? pfad : el('c-data-dir').value.trim();
  post('/api/data-dir', {path: ziel}).then(function(r){
    var kasten = el('datadir-msg');
    if(!r.ok){ kasten.className = 'small err'; kasten.textContent = mtext(r.message); return; }
    el('c-data-dir').value = r.path;
    kasten.className = 'small muted';
    kasten.textContent = t(r.restart ? 'settings.datadir.restart' : 'settings.datadir.same');
  });
}
function datenordnerZurueck(){
  setzeDatenordner((S && S.data_dir_default) || '');
}

function ordnerZuruecksetzen(){
  el('c-skip_folders').value = (S.skip_folders_default || []).join('\n');
}

/* ---------- Exportliste ----------
   Die Regeln sind mächtig, ihr Ergebnis im Kopf auszurechnen ist es nicht:
   „- E-Mail/Kunden/**“ und zwei Zeilen später ein „+“ auf einen Unterordner
   entscheiden über vierhundert Ordner. Wer das nicht sehen kann, stellt blind
   ein. Hier läuft deshalb dieselbe Auswertung wie im Export – nur als Liste
   statt als Lauf, und mit dem, was gerade in den Feldern steht, nicht mit dem
   zuletzt Gespeicherten. */
var planDaten = null, planQuelle = 'outlook';

function zeigeExportliste(quelle){
  planDaten = null;
  planQuelle = quelle || 'outlook';
  planFenster();
  post('/api/folder-plan', planQuelle === 'onedrive'
      ? {quelle: 'onedrive', onedrive_rules: el('c-onedrive_rules').value}
      : {folder_rules: el('c-folder_rules').value,
         skip_folders: el('c-skip_folders').value})
    .then(function(p){
      planDaten = p;
      if(wizardOffen === 'plan') planFenster();
    });
}

function planFenster(){
  var p = planDaten, koerper;
  if(!p){
    koerper = '<p class="small muted">' + esc(t('plan.loading')) + '</p>';
  } else if(!p.ok){
    koerper = '<div class="banner warn">' + esc(t('folders.none')) + '</div>';
  } else {
    koerper = '<p class="small muted">' + esc(t('plan.stand', {when: fmt(p.abgeglichen)})) + '</p>' +
      '<input type="text" id="plan-filter" oninput="planListen()" ' +
        'placeholder="' + esc(t('plan.filter')) + '" style="width:100%;margin:12px 0 2px">' +
      '<div id="plan-listen"></div>';
  }
  oeffneEigenes('plan', modalKopf(t('plan.title'), 'plan') + koerper +
    modalFuss({text: t('folders.sync'),
               tun: 'planAbgleichen(&quot;' + planQuelle + '&quot;)'}));
  if(p && p.ok) planListen();
}

/* Erst der Pfad, dann die Zahl, dann der Grund – in der Reihenfolge, in der
   man fragt. Bei ausgelassenen Ordnern steht dazwischen, was trotzdem schon
   im Archiv liegt: „ausgelassen“ heißt nicht „leer“, und wer das verwechselt,
   sucht später Mails, die längst da sind. */
function planZeile(e, zahl, mitArchiv){
  return '<li><span class="pfad">' + esc(e.pfad) + '</span>' +
    '<span class="zahl">' + (e[zahl] || 0).toLocaleString(LOC) + '</span>' +
    (mitArchiv && e.archiv
      ? '<span class="regel">' + esc(t(planQuelle === 'onedrive' ? 'plan.here' : 'plan.inarchive',
                                       {n: e.archiv.toLocaleString(LOC)})) + '</span>'
      : '') +
    (e.regel ? '<span class="regel">' + esc(e.regel) + '</span>' : '') + '</li>';
}

function planListen(){
  var p = planDaten;
  if(!p || !p.ok || !document.getElementById('plan-listen')) return;
  var f = (el('plan-filter').value || '').trim().toLowerCase();
  function gruppe(schluessel, liste, zahl, mails, punkt, mitArchiv){
    var zeilen = liste.filter(function(e){
      return !f || e.pfad.toLowerCase().indexOf(f) >= 0; });
    return '<details class="plangruppe" open><summary><span class="dot ' + punkt + '"></span>' +
      esc(t(schluessel, {n: liste.length.toLocaleString(LOC),
                         mails: mails.toLocaleString(LOC)})) +
      (f ? ' <span class="small muted">' + esc(t('plan.shown', {n: zeilen.length})) + '</span>' : '') +
      '</summary>' +
      (zeilen.length
        ? '<ul class="planliste">' + zeilen.map(function(e){
            return planZeile(e, zahl, mitArchiv); }).join('') + '</ul>'
        : '<p class="small muted">' + esc(t('plan.nothing')) + '</p>') + '</details>';
  }
  // Beim Spiegel sind es Dateien, nicht Mails – und was hier liegt, ist keine
  // Archivierung, sondern der Rest eines gelöschten Ordners. Ausgeschrieben
  // statt zusammengesetzt, damit der Abgleich mit den Sprachdateien die
  // Schlüssel findet.
  var dat = planQuelle === 'onedrive';
  el('plan-listen').innerHTML =
    gruppe(dat ? 'plan.an.files'  : 'plan.an',  p.an,  'elemente', p.mails_an,  'ok',   false) +
    gruppe(dat ? 'plan.aus.files' : 'plan.aus', p.aus, 'elemente', p.mails_aus, 'warn', true) +
    gruppe(dat ? 'plan.weg.files' : 'plan.weg', p.weg, 'archiv',   p.mails_weg, 'err',  false);
}

function planAbgleichen(quelle){
  closeWizard('plan');
  gleicheOrdnerAb(quelle);
}

/* ---------- Assistenten ---------- */
/* Kennung dessen, was der Assistent gerade anzeigt. Ändert sie sich nicht,
   wird nicht neu gezeichnet – der Status kommt alle 2,5 Sekunden, und ein neu
   gesetztes innerHTML wirft sonst die halb fertige Eingabe weg. Ändert sie
   sich doch (Modell nachgeladen, Token gespeichert), muss neu gezeichnet
   werden, sonst behauptet der Assistent Dinge, die längst erledigt sind.
   Die Restlaufzeit des Tokens steht bewusst nicht drin: sie ändert sich jede
   Minute, ohne dass am Text etwas Wesentliches anders wird. */
function wizardKennung(kind){
  if(kind === 'ollama'){
    var o = S.ollama || {};
    return ['ollama', o.running, o.has_model, o.model].join('|');
  }
  var tk = S.token || {}, au = S.auth || {}, dev = au.device || {};
  return ['token', tk.present, tk.valid, tk.expired, tk.account,
          (tk.missing || []).join(','), (S.scopes_needed || []).join(','),
          au.mode, au.signed_in, au.account, au.own_registration,
          dev.code, dev.done, dev.ok].join('|');
}
/* ---------- Tastatur im Assistenten ----------
   Ein modales Fenster nimmt die Seite in Beschlag; wer keine Maus benutzt, muss
   trotzdem hinein, herum und wieder heraus. ESC schließt, Tab bleibt innerhalb
   (sonst wandert der Fokus unsichtbar hinter die Abdeckung), und beim Schließen
   geht er dorthin zurück, wo er herkam. */
var fokusVorher = null;

function fokussierbare(){
  return [].slice.call(el('modal').querySelectorAll(
    'button, [href], textarea, input, select, summary, [tabindex]:not([tabindex="-1"])'));
}
function modalTaste(e){
  if(!wizardOffen) return;
  if(e.key === 'Escape'){ e.preventDefault(); closeWizard(wizardOffen); return; }
  // Strg/Cmd+Enter im Textfeld: speichern, ohne zum Knopf tabben zu müssen.
  if(e.key === 'Enter' && (e.metaKey || e.ctrlKey)){
    var act = el('modal').querySelector('button.act');
    if(act){ e.preventDefault(); act.click(); }
    return;
  }
  if(e.key !== 'Tab') return;
  var liste = fokussierbare();
  if(!liste.length) return;
  var erster = liste[0], letzter = liste[liste.length - 1];
  if(e.shiftKey && document.activeElement === erster){ e.preventDefault(); letzter.focus(); }
  else if(!e.shiftKey && document.activeElement === letzter){ e.preventDefault(); erster.focus(); }
}
document.addEventListener('keydown', modalTaste);

/* Ein Fenster, das der Server nicht von sich aus aufzieht: es hat keine
   Kennung, die der Status alle 2,5 Sekunden vergleichen könnte, und darf beim
   Neuzeichnen des Assistenten nicht mit weggewischt werden (siehe refresh). */
var WIZARDS = {token: 1, ollama: 1};

function oeffneEigenes(kind, html){
  var warOffen = !!wizardOffen;
  if(!warOffen) fokusVorher = document.activeElement;
  el('modal').className = 'modal breit';
  el('modal').innerHTML = html;
  el('overlay').classList.add('on');
  wizardOffen = kind;
  wizardStand = null;
  if(!warOffen){
    var ziel = el('modal').querySelector('input, button.act');
    if(ziel && ziel.focus) ziel.focus();
  }
}

function openWizard(kind, neuZeichnen){
  var kennung = wizardKennung(kind);
  if(wizardOffen === kind && wizardStand === kennung && !neuZeichnen) return;
  var feld = document.getElementById('tok');       // bereits Eingefügtes retten
  var eingabe = feld ? feld.value : '';
  var warOffen = !!wizardOffen;
  if(!warOffen) fokusVorher = document.activeElement;
  el('modal').className = 'modal';
  el('modal').innerHTML = kind === 'ollama' ? ollamaWizard() : tokenWizard();
  var neu = document.getElementById('tok');
  if(neu && eingabe) neu.value = eingabe;
  el('overlay').classList.add('on');
  wizardOffen = kind;
  wizardStand = kennung;
  // Beim Öffnen in den Dialog springen – aber nicht bei jedem Neuzeichnen,
  // sonst risse es einem den Fokus mitten aus dem Textfeld.
  if(!warOffen){
    var ziel = document.getElementById('tok') || el('modal').querySelector('button.act');
    if(ziel && ziel.focus) ziel.focus();
  }
}
function closeWizard(kind){
  dismissed[kind] = true;
  el('overlay').classList.remove('on');
  wizardOffen = null;
  wizardStand = null;
  if(fokusVorher && fokusVorher.focus) fokusVorher.focus();
  fokusVorher = null;
  // Nur Assistenten sind „gesehen“ zu melden – ein selbst geöffnetes Fenster
  // hat der Server nie verlangt und darf ihm auch nichts zurücksetzen.
  if(WIZARDS[kind]) post('/api/wizard-seen');
}
/* Rahmen für alle Assistenten. Vorher hatte jeder eine andere Knopfzahl – zwei,
   drei, und im Ollama-Fenster war ausgerechnet „Schließen“ der primäre Knopf,
   während die eigentliche Aktion daneben blass stand. Jetzt gilt überall:
   Kreuz oben rechts zum Schließen, unten links die Aktion, daneben die
   Ausweichmöglichkeit. */
function modalKopf(titel, kind){
  var zu = esc(t('wizard.close'));
  return '<div class="modal-kopf"><h2>' + esc(titel) + '</h2>' +
    '<button class="modal-zu" title="' + zu + '" aria-label="' + zu + '" ' +
    'onclick="closeWizard(&quot;' + kind + '&quot;)">&times;</button></div>';
}
/* Der sekundäre Knopf darf fehlen. Ein „Später“, das nichts anderes tut als
   das Kreuz darüber, ist keine zweite Möglichkeit – nur derselbe Ausgang
   zweimal, und der Blick muss ihn zweimal prüfen. */
function modalFuss(primaer, sekundaer, anhang){
  return '<div class="row modal-fuss">' +
    '<button class="act" onclick="' + primaer.tun + '">' + esc(primaer.text) + '</button>' +
    (sekundaer ? '<button class="ghost" onclick="' + sekundaer.tun + '">' +
                 esc(sekundaer.text) + '</button>' : '') +
    (anhang || '') + '</div>';
}

function scopeListe(){
  var q = S.scope_queries || {};
  return '<ul style="margin:6px 0 0;padding-left:18px">' + (S.scopes_needed || []).map(function(x){
    return '<li style="margin-bottom:3px"><code>' + esc(x) + '</code>' +
      (q[x] ? '<br><span class="small muted">' + esc(t('wizard.token.scopes.query')) +
              ' </span><code class="small">' + esc(q[x]) + '</code>' : '') + '</li>';
  }).join('') + '</ul>';
}

/* Die Berechtigungen sind der technischste Teil des Dialogs – Namen wie
   Contacts.Read und dazu Graph-Adressen. Meist sind sie längst erteilt und
   stehen dann nur im Weg. Eingeklappt bleiben sie erreichbar; aufgeklappt
   genau dann, wenn sie wirklich fehlen und damit das Thema sind. */
function rechteBlock(offen){
  return '<details class="rechte"' + (offen ? ' open' : '') + '>' +
    '<summary>' + esc(t('wizard.token.scopes.title')) + '</summary>' +
    '<p class="small muted">' + t('wizard.token.scopes.intro') + '</p>' +
    scopeListe() +
    '<p class="small muted">' + esc(t('wizard.token.scopes.note')) + '</p></details>';
}

function modusWahl(){
  /* Zwei Wege, einer davon die Vorgabe. Der Unterschied, der zaehlt, steht
     direkt daneben – nicht in einer Hilfe, die niemand oeffnet. */
  var jetzt = (S.auth && S.auth.mode) || 'token';
  function karte(wert, titel, hinweis){
    return '<label class="wahl' + (jetzt === wert ? ' on' : '') + '">' +
      '<input type="radio" name="authmode" value="' + wert + '"' +
      (jetzt === wert ? ' checked' : '') + ' onchange="setzeModus(\'' + wert + '\')">' +
      '<span><strong>' + esc(t(titel)) + '</strong>' +
      '<span class="small muted">' + esc(t(hinweis)) + '</span></span></label>';
  }
  return '<div class="wahlreihe">' +
    karte('token', 'wizard.auth.token', 'wizard.auth.token.hint') +
    karte('login', 'wizard.auth.login', 'wizard.auth.login.hint') + '</div>';
}

function eigeneRegistrierung(){
  var au = S.auth || {};
  return '<details class="rechte"' + (au.own_registration ? ' open' : '') + '>' +
    '<summary>' + esc(t('wizard.login.own.title')) + '</summary>' +
    '<p class="small muted">' + t('wizard.login.own.intro') + '</p>' +
    '<div class="row"><label class="small">' + esc(t('wizard.login.own.client')) +
    ' <input type="text" id="au-client" style="width:320px" value="' +
    esc(au.own_registration ? (au.client_id || '') : '') + '" placeholder="' +
    esc(au.default_client_id || '') + '"></label>' +
    '<label class="small">' + esc(t('wizard.login.own.tenant')) +
    ' <input type="text" id="au-tenant" style="width:220px" value="' +
    esc(au.own_registration ? (au.tenant || '') : '') + '" placeholder="organizations"></label>' +
    '<button class="mini" onclick="speichereRegistrierung()">' +
    esc(t('wizard.login.own.save')) + '</button></div></details>';
}

function loginTeil(){
  var au = S.auth || {}, dev = au.device;
  var kopf;
  if(au.signed_in)
    kopf = banner('', '<span class="ok">✓</span> ' +
      t(au.account ? 'wizard.login.state.in' : 'wizard.login.state.in.plain',
        {who: esc(au.account || '')}));
  else
    kopf = banner('warn', esc(t('wizard.login.state.out')));

  var mitte = '';
  if(dev && !dev.done){
    // Der Code ist das Einzige, was jetzt zaehlt – gross und zum Kopieren.
    mitte = '<div class="geraetecode">' +
      '<p>' + t('wizard.login.code.intro', {url: esc(dev.url)}) + '</p>' +
      '<code class="code-gross">' + esc(dev.code) + '</code>' +
      '<p class="small muted">' + esc(t('wizard.login.waiting')) + '</p></div>';
  } else if(dev && dev.done && !dev.ok){
    mitte = banner('err', esc(t('wizard.login.failed', {detail: dev.error || ''})));
  }

  var primaer = au.signed_in
    ? {text: t('wizard.login.again'), tun: 'starteLogin()'}
    : {text: t('wizard.login.start'), tun: 'starteLogin()'};
  var sekundaer = au.signed_in
    ? {text: t('wizard.login.logout'), tun: 'abmelden()'} : null;

  return '<p class="muted small">' + esc(t('wizard.login.intro')) + '</p>' +
    kopf + mitte + eigeneRegistrierung() + modalFuss(primaer, sekundaer);
}

function schluesselTeil(){
  var tk = S.token, head, fehlen = !!(tk.missing && tk.missing.length);
  if(!tk.present) head = banner('err', t('wizard.token.none'));
  else if(tk.expired) head = banner('err', t('wizard.token.expired'));
  else if(fehlen)
    head = banner('warn', t('wizard.token.missing', {list: esc(tk.missing.join(', '))}));
  else {
    // Vier ganze Sätze statt zusammengesetzter Bruchstücke – siehe Sprachdateien.
    var hatWer = !!tk.account, hatZeit = tk.expires_in_minutes != null;
    var k = hatWer && hatZeit ? 'wizard.token.ok'
          : hatWer ? 'wizard.token.ok.unknown'
          : hatZeit ? 'wizard.token.ok.nowho' : 'wizard.token.ok.plain';
    head = banner('', '<span class="ok">✓</span> ' + t(k, {who: esc(tk.account || ''),
                                                          rest: restzeit(tk.expires_in_minutes)}));
  }
  return '<p class="muted small">' + esc(t('wizard.token.intro')) + '</p>' + head +
    rechteBlock(fehlen) +
    '<ol><li>' + t('wizard.token.step1', {url: esc(S.graph_explorer)}) + '</li>' +
    '<li>' + t('wizard.token.step2') + '</li>' +
    '<li>' + esc(t('wizard.token.step3')) + '</li></ol>' +
    '<textarea id="tok" placeholder="eyJ0eXAiOiJKV1QiLCJub25jZSI6…"></textarea>' +
    modalFuss({text: t('wizard.token.save'), tun: 'saveToken()'}, null,
              '<span class="small muted" id="tok-msg"></span>');
}

function tokenWizard(){
  var login = (S.auth && S.auth.mode) === 'login';
  return modalKopf(t('wizard.token.title'), 'token') +
    modusWahl() +
    (login ? loginTeil() : schluesselTeil()) +
    '<p class="small muted" style="margin-top:14px">' +
    t(login ? 'wizard.login.privacy' : 'wizard.token.privacy') + '</p>';
}

function setzeModus(wert){
  post('/api/config', {auth_mode: wert}).then(function(){
    refresh().then(function(){ openWizard('token', true); });
  });
}
function starteLogin(){
  post('/api/login').then(function(){
    refresh().then(function(){ openWizard('token', true); });
  });
}
function abmelden(){
  post('/api/logout').then(function(){
    refresh().then(function(){ openWizard('token', true); });
  });
}
function speichereRegistrierung(){
  post('/api/config', {client_id: el('au-client').value.trim(),
                       tenant: el('au-tenant').value.trim()}).then(function(){
    refresh().then(function(){ openWizard('token', true); });
  });
}

function banner(art, html){
  return '<div class="banner' + (art ? ' ' + art : '') + '">' + html + '</div>';
}
function saveToken(){
  post('/api/token', {token: el('tok').value}).then(function(r){
    el('tok-msg').textContent = mtext(r.message);
    el('tok-msg').className = 'small ' + (r.ok ? 'ok' : 'err');
    if(r.ok){ dismissed = {};
              setTimeout(function(){ el('overlay').classList.remove('on');
                                     wizardOffen = null; wizardStand = null; }, 1200); }
    refresh();
  });
}
function ollamaWizard(){
  var o = S.ollama, h = S.ollama_hint;

  // Alles da – das kann passieren, während der Assistent offen steht und
  // nebenher "ollama pull" durchläuft. Dann bestätigen statt weiter mahnen.
  if(o.running && o.has_model){
    // Das Neuindizieren ist hier die Handlung, für die das Fenster überhaupt
    // aufgeht – es steht vorn, nicht mehr blass neben „Schließen“.
    return modalKopf(t('wizard.ollama.ready.title'), 'ollama') +
      banner('', '<span class="ok">✓</span> ' + t('wizard.ollama.ready', {model: esc(o.model)})) +
      modalFuss({text: t('wizard.ollama.reindex'),
                 tun: 'closeWizard(&quot;ollama&quot;); run({index:true}, t(&quot;job.index&quot;))'},
                null);
  }

  var head = banner('warn', o.running ? t('wizard.ollama.nomodel', {model: esc(o.model)})
                                      : esc(t('wizard.ollama.off')));
  var steps = o.running
    ? [t('wizard.ollama.pull', {model: esc(o.model)}), esc(t('wizard.ollama.wait'))]
    : (h.steps || []).map(function(k){ return t(k, {url: esc(h.url || '')}); });
  return modalKopf(t('wizard.ollama.title'), 'ollama') +
    '<p class="muted small">' + esc(t('wizard.ollama.intro')) + '</p>' + head +
    '<ol>' + steps.map(function(x){ return '<li>' + x + '</li>'; }).join('') + '</ol>' +
    (h.pkg && !o.running ? '<p class="small muted">' + esc(t('wizard.ollama.pkg')) +
                           '</p><pre>' + esc(h.pkg) + '</pre>' : '') +
    // „Später“ ist hier weggefallen: das Kreuz oben tut dasselbe, und der
    // Ausweg ohne Ollama ist die Entscheidung, die wirklich ansteht.
    modalFuss({text: t('wizard.ollama.recheck'), tun: 'recheckOllama()'},
              {text: t('wizard.ollama.without'),
               tun: 'closeWizard(&quot;ollama&quot;); run({index:true, embeddings:false}, ' +
                    't(&quot;job.index.lexical&quot;))'}) +
    '<p class="small muted" style="margin-top:14px">' + esc(t('wizard.ollama.without.note')) + '</p>';
}
function recheckOllama(){
  post('/api/ollama-recheck').then(function(){ refresh().then(function(){ openWizard('ollama', true); }); });
}

/* ---------- Beenden ----------
   Die App hat kein Fenster und steht nicht im Dock – ohne diesen Knopf bliebe
   nur die Aktivitätsanzeige. Der MCP-Server geht mit; ein laufender Auftrag
   wird abgebrochen, bereits Exportiertes bleibt aber erhalten. */
var beendet = false;
function beenden(){
  var laeuft = S && S.jobs && S.jobs.busy;
  if(!confirm(t(laeuft ? 'quit.confirm.busy' : 'quit.confirm'))) return;
  beendet = true;
  post('/api/quit').catch(function(){});   // die Antwort kommt evtl. nicht mehr
  document.querySelector('main').innerHTML =
    '<div class="card"><p>' + esc(t('quit.done')) + '</p></div>';
  document.querySelector('nav').classList.add('hide');
  el('btn-quit').classList.add('hide');
}

/* ---------- Schleife ---------- */
function refresh(){
  if(beendet) return Promise.resolve();
  return api('/api/status').then(renderStatus);
}
stelleKIher();
stelleProtokollHer();
refresh();
setInterval(refresh, 2500);
setInterval(pullLog, 1000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
