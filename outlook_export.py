#!/usr/bin/env python3
"""
Outlook/Exchange-Export als .eml über Microsoft Graph (delegiert, kein Admin nötig).

- Jede Mail als .eml (volle MIME über /messages/{id}/$value, inkl. Anhänge und
  Inline-Bildern). Direkt in jedem Mailprogramm importierbar.
- Optional zusätzlich wählbar: Kalender als .ics (Termine, Zeiten in UTC) und
  Kontakte als .vcf – in eigenen Unterordnern kalender/ und kontakte/.
- Ordnerstruktur des Postfachs wird unter E-Mail/ als Verzeichnisbaum gespiegelt
  (rekursiv) – parallel zu kalender/ und kontakte/.
- PARALLEL: bis zu 4 Downloads gleichzeitig. Exchange Online erlaubt pro Postfach
  nur 4 gleichzeitige Anfragen (MailboxConcurrency, festes Limit) – mehr erzeugt
  nur 429er. Ein globaler Semaphor hält Listing + Downloads zusammen unter dieser
  Grenze; bei 429 wird mit Retry-After zurückgenommen.
- ROBUST: Netzwerkfehler (Timeout, Verbindungsabbruch, TLS) werden mit Backoff
  wiederholt; ein Ordner, der sich nicht vollständig listen lässt, wird
  übersprungen statt den Lauf abzubrechen (nächster Lauf holt ihn nach).

Setup:   pip install msal requests
Start:   python3 outlook_export.py [ausgabe-ordner] [-default]
         -default überspringt alle Abfragen und nutzt die Vorgaben (E-Mail ohne
         Archiv/Entwürfe/Gelöschte/Junk/Postausgang, Standardkalender, Kontakte).
         EXPORT_CATEGORIES="mail,calendar,contacts" wählt ohne Abfrage genau
         diese Kategorien (für app.py, Scheduler, Cron).
         --folders bzw. --calendars holen nur die Ordnerstruktur bzw. die
         Kalenderliste und legen sie ab, ohne etwas zu exportieren.

Welche Ordner und welche Kalender mitkommen, entscheiden geordnete Regeln
    (FOLDER_RULES, CALENDAR_RULES; siehe folders.py) über den abgelegten Listen
    folders.json und calendars.json – nicht mehr eine Abfrage beim Start.

Token-Modus (wenn der Tenant für neue Apps "Approval required" verlangt):
    Access Token im Graph Explorer holen (Mail.Read muss zugestimmt sein; für
    Kalender/Kontakte zusätzlich Calendars.Read und Contacts.Read),
    in gx_token.txt neben dieses Skript legen ODER  export GRAPH_TOKEN="eyJ0…"

Resume: exported.tsv im Ausgabeordner (eine Zeile pro fertige Mail). Bereits
    exportierte Mails werden übersprungen. Token tot -> frischen Token setzen,
    neu starten, es geht weiter. Kompletter Neu-Export: exported.tsv löschen.

Schalter (alle per Umgebungsvariable, siehe README): EXPORT_WORKERS
    (Parallelität, sinnvoll max 4), INCLUDE_HIDDEN (versteckte Systemordner),
    SKIP_FOLDERS (Ordner, die die Standardauswahl auslässt, kommagetrennt).
    Ohne gesetzte Variable gilt app_config.json neben diesem Skript, sonst die
    Vorgabe unten – siehe settings.py.
"""

import os
import sys
import re
import html
import hashlib
import threading
from datetime import datetime, UTC
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import json

import auth
import folders
import graph_client
import settings
import progress

try:
    # msal wird erst in auth.py gebraucht (und nur im Login-Modus) – hier nur
    # geprüft, damit die Meldung über fehlende Pakete früh und gemeinsam kommt.
    import msal  # noqa: F401
    import requests
except ImportError:
    print("Fehlende Pakete. Bitte installieren:  pip install msal requests")
    raise SystemExit(1) from None

# Auf Windows nutzt die Konsole standardmäßig eine Legacy-Codepage (z. B. cp1252),
# und bei Umleitung in eine Datei (python … > log.txt) die Locale-Kodierung. Beides
# lässt print() an Unicode-Zeichen wie →, ✓, · oder Emoji mit UnicodeEncodeError
# scheitern und bricht den Export ab. UTF-8 erzwingen (auf macOS/Linux ein No-op).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Mail.Read", RES + "Calendars.Read", RES + "Contacts.Read", RES + "User.Read"]
# Umgebungsvariable > app_config.json > Vorgabe hier (siehe settings.py)
INCLUDE_HIDDEN = settings.flag("INCLUDE_HIDDEN", "include_hidden", False)
WORKERS = 4                 # parallele Downloads; Exchange-Limit pro Postfach = 4
PAGE = 50                   # $top für Listenabfragen
OUT_ROOT = settings.value("outlook_dir", "outlook_export")  # fest -> Resume über Läufe
DONE_FILE = "exported.tsv"
MAIL_DIR = "E-Mail"          # Postfach-Ordnerbaum liegt darunter (parallel zu kalender/kontakte)
KALENDER_DIR = "kalender"    # ein Unterordner je Kalender, darin die .ics

# Diese Postfach-Ordner sind bei "alle" (Enter) standardmäßig NICHT dabei – nur per
# expliziter Auswahl. Vergleich case-insensitive über den Anzeigenamen (DE + EN).
BUILTIN_SKIP_FOLDERS = {
    "archive", "archiv",
    "entwürfe", "drafts",
    "erneut erinnern aktiviert",
    "gelöschte elemente", "deleted items",
    "junk-e-mail", "junk email", "junk-email",
    "postausgang", "outbox",
}


DEFAULT_SKIP_FOLDERS = settings.folders("SKIP_FOLDERS", "skip_folders", BUILTIN_SKIP_FOLDERS)

# Netz, Drosselung, Retry und Paging liegen in graph_client.py – bis 5.3 stand
# diese Schicht hier (und in den anderen Exporten) als eigene Kopie.
STOP = threading.Event()                     # Signal: Token tot -> nichts Neues mehr starten
ASSUME_DEFAULT = False                       # -default: keine Abfragen, überall die Vorgabe

# Anmeldung, Schlüsselmodus und Konfiguration liegen in auth.py.
TokenExpired = auth.TokenExpired
load_pasted_token = auth.load_pasted_token
TokenClient = graph_client.TokenClient


def graph_login(nur_still=False):
    """Angemeldeter Zugriff mit den Mail-/Kalender-/Kontakte-Scopes."""
    return graph_client.Graph(SCOPES, nur_still=nur_still)


# ---------------------------------------------------------------------------
# Fortschritt: append-only Log, thread-sicher (skaliert auf zehntausende Mails)
# ---------------------------------------------------------------------------
class DoneLog:
    def __init__(self, path):
        self.path = path
        self.done = {}
        self._lock = threading.Lock()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if "\t" in line:
                    mid, rel = line.split("\t", 1)
                    self.done[mid] = rel
        self._fh = open(path, "a", encoding="utf-8")

    def is_done(self, out, mid):
        rel = self.done.get(mid)
        return bool(rel) and (out / rel).exists()

    def mark(self, mid, rel):
        with self._lock:
            self.done[mid] = rel
            self._fh.write(f"{mid}\t{rel}\n")
            self._fh.flush()

    def remap(self, fn):
        """Wendet fn(rel)->rel auf alle Einträge an und schreibt die Datei atomar neu.
        Für einmalige Pfad-Migrationen (Resume bleibt erhalten)."""
        with self._lock:
            self.done = {mid: fn(rel) for mid, rel in self.done.items()}
            try:
                self._fh.close()
            except Exception:
                pass
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for mid, rel in self.done.items():
                    f.write(f"{mid}\t{rel}\n")
            os.replace(tmp, self.path)
            self._fh = open(self.path, "a", encoding="utf-8")

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Interaktive Ordnerauswahl
# ---------------------------------------------------------------------------
def _read(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def _interactive():
    """Nur fragen, wenn ein Terminal da ist und -default nicht gesetzt wurde.

    stdin allein reicht als Nachweis nicht: unter Windows meldet auch das
    Nullgerät isatty() == True, weil NUL ein Zeichengerät ist. Ein Aufruf aus
    der App (stdin auf DEVNULL, stdout in einer Pipe) sähe damit interaktiv aus
    und bliebe an einer Frage stehen, die niemand sieht. Ein echtes Terminal
    hat beide Enden.
    """
    if ASSUME_DEFAULT:
        return False
    for strom in (sys.stdin, sys.stdout):
        try:
            if not strom.isatty():
                return False
        except (AttributeError, ValueError):
            return False
    return True


def parse_indices(raw, n):
    out = []
    for tok in re.split(r"[\s,]+", raw.strip()):
        if tok.isdigit():
            v = int(tok)
            if 1 <= v <= n and v not in out:
                out.append(v)
    return out


def list_calendars(graph):
    """Liest die Kalenderliste für die gezielte Auswahl. Leere Liste bei fehlender
    Berechtigung – dann erscheinen keine Kalender-Einträge im Menü."""
    try:
        cals = list(graph.paged(f"{GRAPH}/me/calendars", {"$top": PAGE}))
    except TokenExpired:
        raise
    except Exception as e:
        print(f"  Kalender nicht lesbar – fehlt die Berechtigung Calendars.Read? ({e})")
        return []
    cals.sort(key=lambda c: (not c.get("isDefaultCalendar"), (c.get("name") or "").lower()))
    return cals


def env_categories(options):
    """Auswahl aus EXPORT_CATEGORIES, z. B. "mail,contacts".

    Für Aufrufer ohne Terminal (app.py, Scheduler, Cron), die nicht alles
    wollen. Unbekannte Namen werden ignoriert; bleibt nichts übrig, zählt die
    Variable als nicht gesetzt -> None (normale Abfrage bzw. Standardauswahl).
    """
    raw = os.environ.get("EXPORT_CATEGORIES")
    if not raw:
        return None
    picked = {t.strip().lower() for t in raw.replace(";", ",").split(",")}
    sel = {k for k, _ in options if k.lower() in picked}
    return sel or None


def prompt_categories():
    """Schritt 1: Was exportieren? Mehrfachauswahl (z. B. 1,2).
    Liefert ein Set aus {"mail", "calendar", "contacts"}."""
    options = [("mail", "E-Mail (Postfach-Ordner)"),
               ("calendar", "Kalender"),
               ("contacts", "Kontakte")]
    env = env_categories(options)
    if env is not None:
        print("Auswahl aus EXPORT_CATEGORIES – keine Abfrage.")
        return env
    if not _interactive():
        print("Standardauswahl – exportiere E-Mail, Standardkalender und Kontakte.")
        return {k for k, _ in options}
    print("\nWas möchtest du exportieren? (Mehrfachauswahl möglich, z. B. 1,2)")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    raw = _read("Auswahl (Enter = alles): ").strip()
    if not raw:
        return {k for k, _ in options}
    idxs = parse_indices(raw, len(options))
    if not idxs:
        print("Keine gültige Auswahl – nehme alles.")
        return {k for k, _ in options}
    return {options[i - 1][0] for i in idxs}


def _is_default_skip(top):
    name = (top["folder"].get("displayName") or "").strip().lower()
    return name in DEFAULT_SKIP_FOLDERS


def select_mail_folders(tops):
    """Schritt 2a: welche Postfach-Ordner (jeweils inkl. Unterordner).
    Enter = alle AUSSER den Standard-Ausschlüssen (Archiv, Entwürfe, Gelöschte
    Elemente, Junk-E-Mail, Postausgang, „Erneut erinnern aktiviert"). Diese werden
    angezeigt, aber nur auf explizite Auswahl exportiert. Mehrfachauswahl möglich."""
    tops.sort(key=lambda t: (t["folder"].get("displayName") or "").lower())
    default = [t for t in tops if not _is_default_skip(t)]
    if not tops or not _interactive():
        return default
    n = len(tops)
    print("\nWelche Postfach-Ordner? (Mehrfachauswahl; Enter = alle ohne die mit (aus); inkl. Unterordner)")
    for i, t in enumerate(tops, 1):
        name = t["folder"].get("displayName", "Ordner")
        subs = t["nfolders"] - 1
        extra = f", {subs} Unterordner" if subs > 0 else ""
        flag = "  (aus)" if _is_default_skip(t) else ""
        print(f"  {i}) {name}  ({t['items']} Elemente{extra}){flag}")
    raw = _read("Auswahl (Enter = alle ohne die mit (aus)): ").strip()
    if not raw:
        return default
    idxs = parse_indices(raw, n)
    if not idxs:
        print("Keine gültige Auswahl – nehme alle ohne die Standard-Ausschlüsse.")
        return default
    return [tops[i - 1] for i in idxs]


def kalender_eintraege(cals):
    """Die Kalenderliste in der Form, in der folders.py mit ihr rechnet.

    Der Pfad ist der, unter dem der Kalender auch auf der Platte landet
    (kalender/<Name>). Damit greifen dieselben Regeln, dieselbe Vorschau und
    dieselbe Zählung wie bei den Postfach-Ordnern – und ein umbenannter
    Kalender fällt in der Vorschau als „nur noch im Archiv“ auf, statt still
    doppelt zu liegen.
    """
    return [{
        "id": c.get("id") or f"{KALENDER_DIR}/{safe(c.get('name') or 'Kalender')}",
        "pfad": f"{KALENDER_DIR}/{safe(c.get('name') or 'Kalender')}",
        "name": c.get("name") or "Kalender",
        "standard": bool(c.get("isDefaultCalendar")),
        "elemente": 0,      # Graph zählt Termine nicht mit; die Vorschau zählt
    } for c in cals or ()]  # stattdessen, was schon im Archiv liegt


def kalender_regeln(daten=None):
    """Welche Kalender exportiert werden – Umgebung schlägt Datei schlägt Vorgabe.

    Ohne eigene Regeln bleibt es beim Standardkalender: ein Postfach hat neben
    dem eigenen oft noch Geburtstage, Feiertage und fremde Freigaben, und die
    hat niemand gemeint, der „Kalender“ ankreuzt.
    """
    roh = os.environ.get("CALENDAR_RULES")
    if roh is None:
        roh = settings.value("calendar_rules", None)
    if roh and roh.strip():
        return folders.lies_regeln(roh)
    return folders.nur_standard((daten or {}).get("ordner", []))


def waehle_kalender(graph, out):
    """Kalender aus calendars.json auswählen; die Liste einmalig holen, wenn sie fehlt."""
    daten = folders.lade(out, folders.KALENDER)
    if daten is None:
        print("Lade Kalenderliste…")
        eintraege = kalender_eintraege(list_calendars(graph))
        if not eintraege:
            # Meist die fehlende Berechtigung Calendars.Read. Eine leere Liste
            # abzulegen hieße, sie nie wieder zu holen – und der Export bliebe
            # für immer still leer, ohne dass jemand den Grund sähe.
            print("  Keine Kalender lesbar – die Liste wird nicht abgelegt.")
            return []
        daten = folders.speichere(out, eintraege, datei=folders.KALENDER)
    regeln = kalender_regeln(daten)
    gewaehlt = folders.gewaehlt(daten, regeln)
    alle = daten.get("ordner", [])
    print(f"Kalender: {len(gewaehlt)} von {len(alle)} gewählt"
          + (" – " + ", ".join(e["name"] for e in gewaehlt) if gewaehlt else ""))
    if daten.get("neu"):
        print(f"  Hinweis: {len(daten['neu'])} Kalender sind seit dem Abgleich neu "
              f"dazugekommen und folgen den Regeln automatisch.")
    return [{"id": e["id"], "name": e["name"]} for e in gewaehlt]


def gleiche_kalender_ab(argv):
    """--calendars: nur die Kalenderliste holen und ablegen, nichts exportieren."""
    out = Path(argv[0]) if argv else Path(OUT_ROOT)
    graph = auth.waehle_zugang(TokenClient, graph_login)
    print(f"Gleiche Kalenderliste ab: {out.resolve()}")
    vorher = folders.lade(out, folders.KALENDER)
    daten = folders.speichere(out, kalender_eintraege(list_calendars(graph)),
                              vorher, datei=folders.KALENDER)
    gewaehlt = folders.gewaehlt(daten, kalender_regeln(daten))
    print(f"\n{len(daten['ordner'])} Kalender im Postfach, {len(gewaehlt)} gewählt.")
    for art, liste in (("neu", daten["neu"]), ("nicht mehr da", daten["verschwunden"]),
                       ("umbenannt", daten["umbenannt"])):
        if liste:
            print(f"  {len(liste)} {art}: " + ", ".join(liste[:5])
                  + (" …" if len(liste) > 5 else ""))
    print(f"Abgelegt: {folders.pfad(out, folders.KALENDER)}")


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------
def safe(name, maxlen=80):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:maxlen] or "unbenannt"


def short_id(s):
    return hashlib.sha1((s or "").encode()).hexdigest()[:8]


def mail_filename(msg):
    dt = msg.get("receivedDateTime") or msg.get("sentDateTime") or ""
    stamp = ""
    if dt:
        try:
            s = dt.replace("Z", "+00:00")
            s = re.sub(r"(\.\d{6})\d+", r"\1", s)
            stamp = datetime.fromisoformat(s).astimezone().strftime("%Y-%m-%d_%H%M")
        except Exception:
            stamp = dt[:10]
    subj = (msg.get("subject") or "").strip() or "(kein Betreff)"
    prefix = (stamp + "__") if stamp else ""
    return f"{prefix}{safe(subj, 90)}__{short_id(msg['id'])}.eml"


def folder_params():
    p = {"$top": 100}
    if INCLUDE_HIDDEN:
        p["includeHiddenFolders"] = "true"
    return p


def list_children(graph, folder):
    """Listet die direkten Unterordner – unabhängig von childFolderCount."""
    try:
        return list(graph.paged(f"{GRAPH}/me/mailFolders/{folder['id']}/childFolders",
                                folder_params()))
    except TokenExpired:
        raise
    except Exception as e:
        print(f"  Warnung: Unterordner von '{folder.get('displayName')}' nicht lesbar: {e}")
        return []


def _subtree(graph, folder, rel_path, acc):
    """Hängt (folder, rel_path) für den Ordner und ALLE Nachkommen an acc an.
    Verlässt sich nicht auf childFolderCount, sondern listet immer die Kinder –
    so werden auch tief verschachtelte Unterordner zuverlässig erfasst."""
    acc.append((folder, rel_path))
    for child in list_children(graph, folder):
        cname = safe(child.get("displayName") or "Ordner")
        _subtree(graph, child, f"{rel_path}/{cname}", acc)


def build_tree(graph):
    """Liest die komplette Ordnerstruktur EINMAL und liefert pro oberstem Ordner
    den Teilbaum samt rekursiver Elementzahl. Das Ergebnis wird für die Auswahl UND
    den Export genutzt (kein erneutes Ordner-Listing im parallelen Download)."""
    tops = []
    roots = list(graph.paged(f"{GRAPH}/me/mailFolders", folder_params()))
    count = 0
    for tf in roots:
        rel = f"{MAIL_DIR}/{safe(tf.get('displayName') or 'Ordner')}"
        sub = []
        _subtree(graph, tf, rel, sub)
        items = sum((f.get("totalItemCount") or 0) for f, _ in sub)
        tops.append({"folder": tf, "rel": rel, "subtree": sub,
                     "items": items, "nfolders": len(sub)})
        count += len(sub)
        print(f"  … {count} Ordner erfasst", end="\r", flush=True)
    print(f"  {count} Ordner erfasst.            ")
    return tops


# ---------------------------------------------------------------------------
# Verschwundene Mails erkennen
#
# Ein Archiv, das nur wächst, beantwortet die wichtigste Frage nicht: was war
# hier einmal und ist jetzt weg? Die Datei bleibt selbstverständlich liegen –
# vermerkt wird nur, dass sie im Postfach nicht mehr auftaucht.
#
# Die Falle dabei ist die Verwechslung von gelöscht und verschoben. Eine Mail,
# die in einen Ordner wandert, den dieser Lauf nicht exportiert (Archiv steht
# in der Standardauswahl nicht drin), sähe verschwunden aus. Deshalb wird jeder
# Verdacht bei Graph nachgefragt: 404 heißt wirklich weg, alles andere heißt
# verschoben.
# ---------------------------------------------------------------------------
GONE_FILE = "verschwunden.tsv"


class Bestand:
    """Was in diesem Lauf wirklich im Postfach lag."""

    def __init__(self):
        self.gesehen = set()        # IDs aus vollständig gelisteten Ordnern
        self.briefe = set()         # deren internetMessageId (überlebt ein Verschieben)
        self.vollstaendig = []      # deren Pfade, mit Schrägstrich am Ende

    def ordner_fertig(self, rel_path):
        self.vollstaendig.append(rel_path.rstrip("/") + "/")

    def aus_gelistetem_ordner(self, rel):
        return any(rel.startswith(p) for p in self.vollstaendig)


def brief_kennung(pfad):
    """Die Message-ID aus dem Kopf einer abgelegten .eml.

    Nur der Kopf wird gelesen: eine .eml mit einem 40-MB-Anhang ganz zu laden,
    um eine Zeile daraus zu holen, wäre bei hunderten Verdachtsfällen teuer.
    Gefaltete Fortsetzungszeilen kommen bei dieser Kopfzeile praktisch nicht
    vor, werden aber mitgenommen, damit ein Sonderfall keine falsche Antwort
    erzeugt.
    """
    try:
        with open(pfad, "rb") as f:
            wert = None
            for roh in f:
                if roh in (b"\r\n", b"\n"):        # Ende des Kopfes
                    break
                if wert is not None:
                    if roh[:1] in (b" ", b"\t"):     # Fortsetzung
                        wert += roh.strip()
                        continue
                    break
                if roh[:11].lower() == b"message-id:":
                    wert = roh[11:].strip()
            return wert.decode("utf-8", "replace").strip() if wert else None
    except OSError:
        return None


def verschoben_statt_weg(out, kandidaten, bestand):
    """Verdachtsfälle aussortieren, deren Brief im Postfach wieder auftaucht.

    Exchange vergibt beim Verschieben eine NEUE Nachrichten-ID. Die Rückfrage
    nach der alten beantwortet Graph deshalb mit 404 – und eine nur in einen
    anderen Ordner geschobene Mail galt als gelöscht. An einem echten Archiv
    waren so 16 von 19 Vermerken falsch.

    Die internetMessageId übersteht das Verschieben. Sie steht in jeder
    abgelegten .eml und kommt beim Listen ohne Zusatzkosten mit, also lässt
    sich der Fall hier ohne eine einzige weitere Anfrage entscheiden.
    """
    if not bestand.briefe:
        return kandidaten, 0
    bleibt, verschoben = [], 0
    for mid, rel in kandidaten:
        kennung = brief_kennung(out / rel)
        if kennung and kennung in bestand.briefe:
            verschoben += 1
        else:
            bleibt.append((mid, rel))
    return bleibt, verschoben


def zuruecknehmen(out, bekannt, bestand):
    """Frühere Vermerke prüfen: Was wieder im Postfach liegt, war nie gelöscht.

    Ohne das bliebe der Fehler für immer stehen – die Vermerke von damals
    entstanden unter der alten, falschen Annahme.
    """
    if not bestand.briefe:
        return bekannt, 0
    behalten = {}
    for rel, wann in bekannt.items():
        kennung = brief_kennung(out / rel)
        if kennung and kennung in bestand.briefe:
            continue
        behalten[rel] = wann
    return behalten, len(bekannt) - len(behalten)


def verdaechtige(done, bestand):
    """Früher exportiert, in diesem Lauf nicht mehr gesehen.

    Nur aus Ordnern, die vollständig gelistet wurden – ein abgebrochenes
    Listing darf nicht den halben Ordner für gelöscht erklären.
    """
    return sorted((mid, rel) for mid, rel in done.done.items()
                  if mid not in bestand.gesehen and bestand.aus_gelistetem_ordner(rel))


def wirklich_weg(graph, kandidaten, grenze=2000):
    """Jeden Verdacht bei Graph nachfragen. Liefert (weg, verschoben).

    Ein Fehler, der kein 404 ist (Drosselung, Netz), zählt als „nicht weg“:
    lieber eine Löschung später melden als eine falsche jetzt.
    """
    weg, verschoben = [], 0
    for mid, rel in kandidaten[:grenze]:
        try:
            graph.get(f"{GRAPH}/me/messages/{mid}", {"$select": "id"})
            verschoben += 1
        except TokenExpired:
            raise
        except Exception as e:
            if "404" in str(e) or getattr(e, "status", None) == 404:
                weg.append(rel)
            # sonst: unklar – nichts behaupten
    if len(kandidaten) > grenze:
        print(f"  Hinweis: {len(kandidaten) - grenze} weitere Verdachtsfälle erst "
              f"beim nächsten Lauf geprüft (Grenze {grenze}).")
    return weg, verschoben


def lies_verschwunden(pfad):
    """rel -> Zeitpunkt des ersten Fehlens."""
    out = {}
    try:
        for zeile in pfad.read_text(encoding="utf-8").splitlines():
            if "\t" in zeile:
                rel, wann = zeile.split("\t", 1)
                out[rel] = wann
    except OSError:
        pass
    return out


def schreibe_verschwunden(pfad, bekannt, neue, jetzt):
    """Bestehende Vermerke behalten, neue ergänzen. Atomar."""
    zusammen = dict(bekannt)
    for rel in neue:
        zusammen.setdefault(rel, jetzt)
    tmp = pfad.with_name(pfad.name + ".tmp")
    tmp.write_text("".join(f"{rel}\t{wann}\n" for rel, wann in sorted(zusammen.items())),
                   encoding="utf-8")
    tmp.replace(pfad)
    return zusammen


def iter_messages_to_export(graph, out, done, stats, selected, bestand=None):
    """Spiegelt die Ordner aufs Dateisystem und liefert (mid, rel) für jede
    noch nicht exportierte Mail. Listing läuft im Hauptthread (lazy)."""
    # internetMessageId kostet nichts extra und ist der einzige Schlüssel, der
    # ein Verschieben übersteht – siehe brief_kennung und pruefe_verschwundene.
    select = ("id,internetMessageId,subject,receivedDateTime,sentDateTime,"
              "from,hasAttachments")
    for top in selected:
        for folder, rel_path in top["subtree"]:
            (out / rel_path).mkdir(parents=True, exist_ok=True)
            total = folder.get("totalItemCount")
            print(f"\nOrdner: {rel_path}" + (f"  ({total} Elemente)" if total is not None else ""))
            seen = 0
            try:
                for msg in graph.paged(f"{GRAPH}/me/mailFolders/{folder['id']}/messages",
                                       {"$top": PAGE, "$select": select}):
                    seen += 1
                    mid = msg["id"]
                    if bestand is not None:
                        bestand.gesehen.add(mid)
                        if msg.get("internetMessageId"):
                            bestand.briefe.add(msg["internetMessageId"].strip())
                    if done.is_done(out, mid):
                        stats["skipped"] += 1
                        continue
                    yield mid, f"{rel_path}/{mail_filename(msg)}"
            except TokenExpired:
                raise
            except Exception as e:
                # Ein dauerhaft hängender Ordner darf nicht den ganzen Lauf killen:
                # Rest überspringen, weiter mit dem nächsten. Was schon exportiert
                # ist, steht in exported.tsv – der nächste Lauf holt den Rest.
                stats["folder_errors"] = stats.get("folder_errors", 0) + 1
                print(f"  ! Ordner unvollständig gelistet ({type(e).__name__}: {e})"
                      f" – nach {seen} Mails abgebrochen, nächster Lauf setzt fort.")
                continue
            # Nur ein vollständig durchlaufener Ordner taugt zum Vergleich –
            # nach einem Abbruch oben sind wir hier gar nicht.
            if bestand is not None:
                bestand.ordner_fertig(rel_path)
            if seen:
                print(f"  {seen} Mails gesichtet.")


# ---------------------------------------------------------------------------
# Worker + paralleler Treiber
# ---------------------------------------------------------------------------
def download_one(graph, out, done, mid, rel):
    if STOP.is_set():
        return ("stopped", mid)
    try:
        content, _ = graph.get_bytes(f"{GRAPH}/me/messages/{mid}/$value", label=" (MIME)")
    except TokenExpired:
        return ("expired", mid)
    except Exception as e:
        return ("error", f"{mid[:16]}…: {e}")
    try:
        (out / rel).write_bytes(content)
    except Exception as e:
        return ("error", f"{rel}: {e}")
    done.mark(mid, rel)
    return ("ok", rel)


def run_export(graph, out, done, stats, selected, workers, bestand=None):
    gen = iter_messages_to_export(graph, out, done, stats, selected, bestand)
    cap = max(workers * 8, workers)      # so viele Tasks gleichzeitig in der Pipeline
    pending = set()
    expired = False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        def fill():
            nonlocal expired
            while len(pending) < cap:
                try:
                    mid, rel = next(gen)
                except StopIteration:
                    return
                except TokenExpired:        # Token kann schon beim Listing sterben
                    expired = True
                    STOP.set()
                    return
                pending.add(ex.submit(download_one, graph, out, done, mid, rel))

        fill()
        while pending:
            finished, rest = wait(pending, return_when=FIRST_COMPLETED)
            pending = set(rest)
            for fut in finished:
                try:
                    status, info = fut.result()
                except Exception as e:
                    status, info = "error", str(e)
                if status == "ok":
                    stats["new"] += 1
                    # Ohne Gesamtzahl: der Generator entdeckt die Mails erst im
                    # Laufen. Gemeldet wird deshalb nur der Stand.
                    progress.melde(stats["new"], what="mails")
                    if stats["new"] % 50 == 0:
                        print(f"  … {stats['new']} Mails neu exportiert")
                elif status == "expired":
                    expired = True
                    STOP.set()
                elif status == "error":
                    print(f"    Mail übersprungen ({info})")
                # "stopped" -> ignorieren
            if not expired:
                fill()

    return "expired" if expired else "done"


# ---------------------------------------------------------------------------
# Kalender (.ics) und Kontakte (.vcf)
# ---------------------------------------------------------------------------
_WD = {"monday": "MO", "tuesday": "TU", "wednesday": "WE", "thursday": "TH",
       "friday": "FR", "saturday": "SA", "sunday": "SU"}
_IDX = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}


def _plain_text(body):
    body = body or {}
    c = body.get("content", "") or ""
    if (body.get("contentType") or "").lower() == "html":
        c = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", c)
        c = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</tr>", " ", c)
        c = re.sub(r"<[^>]+>", " ", c)
        c = html.unescape(c)
    return " ".join(c.split())


def _esc(s):
    s = (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def _cn(name):
    return '"' + " ".join((name or "").split()).replace('"', "'") + '"'


def _fold(line):
    """iCal/vCard-Zeilen auf <=75 Oktette falten (CRLF + Leerzeichen)."""
    out, cur = "", 0
    for ch in line:
        w = len(ch.encode("utf-8"))
        if cur + w > 73:
            out += "\r\n "
            cur = 1
        out += ch
        cur += w
    return out


def _graph_dt(s):
    if not s:
        return None
    s = s.strip().replace("Z", "")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


def _ics_dt(node, all_day):
    dt = _graph_dt((node or {}).get("dateTime") or "")
    if dt is None:
        return None
    return dt.strftime("%Y%m%d") if all_day else dt.strftime("%Y%m%dT%H%M%SZ")


def _stamp(node, all_day):
    dt = _graph_dt((node or {}).get("dateTime") or "")
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d") if all_day else dt.strftime("%Y-%m-%d_%H%M")


def build_rrule(recurrence, all_day):
    if not recurrence:
        return None
    try:
        pat = recurrence.get("pattern") or {}
        rng = recurrence.get("range") or {}
        ptype = pat.get("type", "")
        interval = int(pat.get("interval", 1) or 1)
        days = [_WD[d.lower()] for d in (pat.get("daysOfWeek") or []) if d.lower() in _WD]
        idx = _IDX.get(pat.get("index", "first"), 1)
        parts = []
        if ptype == "daily":
            parts.append("FREQ=DAILY")
        elif ptype == "weekly":
            parts.append("FREQ=WEEKLY")
            if days:
                parts.append("BYDAY=" + ",".join(days))
        elif ptype == "absoluteMonthly":
            parts.append("FREQ=MONTHLY")
            if pat.get("dayOfMonth"):
                parts.append(f"BYMONTHDAY={pat['dayOfMonth']}")
        elif ptype == "relativeMonthly":
            parts.append("FREQ=MONTHLY")
            if days:
                parts.append("BYDAY=" + ",".join(f"{idx}{d}" for d in days))
        elif ptype == "absoluteYearly":
            parts.append("FREQ=YEARLY")
            if pat.get("month"):
                parts.append(f"BYMONTH={pat['month']}")
            if pat.get("dayOfMonth"):
                parts.append(f"BYMONTHDAY={pat['dayOfMonth']}")
        elif ptype == "relativeYearly":
            parts.append("FREQ=YEARLY")
            if pat.get("month"):
                parts.append(f"BYMONTH={pat['month']}")
            if days:
                parts.append("BYDAY=" + ",".join(f"{idx}{d}" for d in days))
        else:
            return None
        if interval != 1:
            parts.append(f"INTERVAL={interval}")
        rtype = rng.get("type", "")
        if rtype == "endDate" and rng.get("endDate"):
            d = rng["endDate"].replace("-", "")
            parts.append("UNTIL=" + (d if all_day else d + "T235959Z"))
        elif rtype == "numbered" and rng.get("numberOfOccurrences"):
            parts.append(f"COUNT={int(rng['numberOfOccurrences'])}")
        return ";".join(parts)
    except Exception:
        return None


def event_filename(ev):
    all_day = bool(ev.get("isAllDay"))
    stamp = _stamp(ev.get("start"), all_day)
    subj = (ev.get("subject") or "").strip() or "(kein Betreff)"
    prefix = (stamp + "__") if stamp else ""
    return f"{prefix}{safe(subj, 90)}__{short_id(ev.get('id') or ev.get('iCalUId') or subj)}.ics"


def build_ics(ev):
    all_day = bool(ev.get("isAllDay"))
    uid = ev.get("iCalUId") or ev.get("id") or short_id(ev.get("subject") or "")
    stamp = _graph_dt(ev.get("lastModifiedDateTime") or ev.get("createdDateTime") or "")
    dtstamp = (stamp or datetime.now(UTC).replace(tzinfo=None)).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//outlook_export//Graph//DE",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
         f"UID:{_esc(uid)}", f"DTSTAMP:{dtstamp}"]
    start, end = _ics_dt(ev.get("start"), all_day), _ics_dt(ev.get("end"), all_day)
    if start:
        L.append(("DTSTART;VALUE=DATE:" if all_day else "DTSTART:") + start)
    if end:
        L.append(("DTEND;VALUE=DATE:" if all_day else "DTEND:") + end)
    L.append("SUMMARY:" + _esc(ev.get("subject") or "(kein Betreff)"))
    loc = (ev.get("location") or {}).get("displayName")
    if loc:
        L.append("LOCATION:" + _esc(loc))
    desc = _plain_text(ev.get("body"))
    if desc:
        L.append("DESCRIPTION:" + _esc(desc))
    org = (ev.get("organizer") or {}).get("emailAddress") or {}
    if org.get("address"):
        L.append(f'ORGANIZER;CN={_cn(org.get("name") or org["address"])}:mailto:{org["address"]}')
    for a in ev.get("attendees") or []:
        em = a.get("emailAddress") or {}
        if em.get("address"):
            L.append(f'ATTENDEE;CN={_cn(em.get("name") or em["address"])}:mailto:{em["address"]}')
    show = ev.get("showAs", "")
    if ev.get("isCancelled"):
        L.append("STATUS:CANCELLED")
    elif show == "tentative":
        L.append("STATUS:TENTATIVE")
    else:
        L.append("STATUS:CONFIRMED")
    L.append("TRANSP:" + ("TRANSPARENT" if show == "free" else "OPAQUE"))
    rr = build_rrule(ev.get("recurrence"), all_day)
    if rr:
        L.append("RRULE:" + rr)
    L += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in L) + "\r\n"


def export_calendar(graph, out, done, stats, cals):
    if not cals:
        return
    print("\nKalender…")
    pref = {"Prefer": 'outlook.timezone="UTC"'}      # Zeiten in UTC -> korrekte .ics
    select = ("id,iCalUId,subject,start,end,isAllDay,location,organizer,attendees,"
              "body,showAs,isCancelled,recurrence,seriesMasterId,type,"
              "createdDateTime,lastModifiedDateTime")
    for cal in cals:
        cname = safe(cal.get("name") or "Kalender")
        url = (f"{GRAPH}/me/calendars/{cal['id']}/events" if cal.get("id")
               else f"{GRAPH}/me/events")
        print(f"\nKalender: {cname}")
        seen = 0
        try:
            for ev in graph.paged(url, {"$top": PAGE, "$select": select}, extra_headers=pref):
                seen += 1
                rel = f"kalender/{cname}/{event_filename(ev)}"
                # Termine ohne Graph-ID: Dateipfad als stabiler Ersatzschlüssel,
                # sonst landet der Schlüssel None im Log und Resume greift nie.
                key = ev.get("id") or ev.get("iCalUId") or rel
                if done.is_done(out, key):
                    stats["skipped"] += 1
                    continue
                (out / "kalender" / cname).mkdir(parents=True, exist_ok=True)
                try:
                    (out / rel).write_text(build_ics(ev), encoding="utf-8")
                except Exception as e:
                    print(f"    Termin übersprungen ({e})")
                    continue
                done.mark(key, rel)
                stats["new"] += 1
                if stats["new"] % 100 == 0:
                    print(f"  … {stats['new']} neu exportiert")
        except TokenExpired:
            raise
        except Exception as e:
            print(f"  Kalender '{cname}' abgebrochen: {e}")
            continue
        if seen:
            print(f"  {seen} Termine gesichtet.")


def contact_filename(c):
    nm = (c.get("displayName")
          or " ".join(x for x in [c.get("givenName"), c.get("surname")] if x)).strip() or "Kontakt"
    return f"{safe(nm, 90)}__{short_id(c.get('id') or nm)}.vcf"


def build_vcf(c):
    given, sur, mid = c.get("givenName") or "", c.get("surname") or "", c.get("middleName") or ""
    fn = c.get("displayName") or " ".join(x for x in [given, sur] if x).strip() or "(ohne Namen)"
    L = ["BEGIN:VCARD", "VERSION:3.0",
         f"N:{_esc(sur)};{_esc(given)};{_esc(mid)};;", "FN:" + _esc(fn)]
    org, dept = c.get("companyName") or "", c.get("department") or ""
    if org or dept:
        L.append("ORG:" + _esc(org) + (";" + _esc(dept) if dept else ""))
    if c.get("jobTitle"):
        L.append("TITLE:" + _esc(c["jobTitle"]))
    for e in c.get("emailAddresses") or []:
        if e.get("address"):
            L.append("EMAIL;TYPE=INTERNET:" + _esc(e["address"]))
    for p in c.get("businessPhones") or []:
        if p:
            L.append("TEL;TYPE=WORK,VOICE:" + _esc(p))
    for p in c.get("homePhones") or []:
        if p:
            L.append("TEL;TYPE=HOME,VOICE:" + _esc(p))
    if c.get("mobilePhone"):
        L.append("TEL;TYPE=CELL,VOICE:" + _esc(c["mobilePhone"]))
    if c.get("personalNotes"):
        L.append("NOTE:" + _esc(c["personalNotes"]))
    if c.get("id"):
        L.append("UID:" + _esc(c["id"]))
    L.append("END:VCARD")
    return "\r\n".join(_fold(x) for x in L) + "\r\n"


def export_contacts(graph, out, done, stats):
    print("\nKontakte…")
    sources = [("", f"{GRAPH}/me/contacts")]          # Standardkontakte (kein Ordner)
    try:
        folders = list(graph.paged(f"{GRAPH}/me/contactFolders", {"$top": PAGE}))
    except TokenExpired:
        raise
    except Exception as e:
        print(f"  Kontaktordner nicht lesbar – fehlt Contacts.Read? ({e})")
        folders = []
    for f in folders:
        sources.append((safe(f.get("displayName") or "Ordner"),
                        f"{GRAPH}/me/contactFolders/{f['id']}/contacts"))
    select = ("id,displayName,givenName,surname,middleName,companyName,department,"
              "jobTitle,emailAddresses,businessPhones,homePhones,mobilePhone,personalNotes")
    for sub, url in sources:
        rel_dir = "kontakte" + (f"/{sub}" if sub else "")
        seen = 0
        try:
            for c in graph.paged(url, {"$top": PAGE, "$select": select}):
                seen += 1
                rel = f"{rel_dir}/{contact_filename(c)}"
                # Kontakte ohne Graph-ID: Dateipfad als stabiler Ersatzschlüssel
                # (sonst Schlüssel None im Log und Re-Export bei jedem Lauf).
                key = c.get("id") or rel
                if done.is_done(out, key):
                    stats["skipped"] += 1
                    continue
                (out / rel_dir).mkdir(parents=True, exist_ok=True)
                try:
                    (out / rel).write_text(build_vcf(c), encoding="utf-8")
                except Exception as e:
                    print(f"    Kontakt übersprungen ({e})")
                    continue
                done.mark(key, rel)
                stats["new"] += 1
        except TokenExpired:
            raise
        except Exception as e:
            print(f"  '{rel_dir}' abgebrochen: {e}")
            continue
        if seen:
            print(f"  {rel_dir}: {seen} Kontakte gesichtet.")


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------
def migrate_to_email_subdir(out, done):
    """Einmalige, idempotente Migration: bereits exportierte Mail-Ordner (oberste
    Ebene) nach E-Mail/ verschieben und die Resume-Pfade entsprechend umschreiben,
    damit nichts neu heruntergeladen wird. kalender/ und kontakte/ bleiben unberührt.
    No-op bei neuer/leerer Struktur."""
    reserved = {MAIL_DIR, "kalender", "kontakte"}
    try:
        children = [c for c in out.iterdir() if c.is_dir() and c.name not in reserved]
    except FileNotFoundError:
        return
    if not children:
        return
    target = out / MAIL_DIR
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for d in children:
        dest = target / d.name
        if dest.exists():
            continue   # Teilmigration/Namenskollision -> sicherheitshalber überspringen
        try:
            d.rename(dest)
            moved += 1
        except OSError as e:
            print(f"  Migration: '{d.name}' nicht verschoben ({e})")
    if not moved:
        return

    def fix(rel):
        return rel if rel.split("/", 1)[0] in reserved else f"{MAIL_DIR}/{rel}"
    done.remap(fix)
    print(f"Struktur aktualisiert: {moved} Mail-Ordner nach '{MAIL_DIR}/' verschoben "
          f"(Resume-Liste angepasst, kein erneuter Download).")


def pruefe_verschwundene(graph, out, done, bestand):
    """Was seit dem letzten Lauf aus dem Postfach verschwunden ist.

    Läuft nur nach einem sauberen Durchlauf: nach einem Abbruch oder einem
    unvollständig gelisteten Ordner wüssten wir nicht, ob etwas fehlt oder ob
    wir nur nicht hingesehen haben. Lieber gar keine Aussage als eine falsche.
    """
    pfad = out / GONE_FILE
    bekannt = lies_verschwunden(pfad)
    # Erst aufräumen, was unter der alten Annahme falsch vermerkt wurde.
    bekannt, geheilt = zuruecknehmen(out, bekannt, bestand)
    if geheilt:
        schreibe_verschwunden(pfad, bekannt, [], "")
        print(f"\n{geheilt} frühere Vermerke zurückgenommen: die Mails liegen "
              f"wieder im Postfach, sie waren nur verschoben.")

    kandidaten = verdaechtige(done, bestand)
    if not kandidaten:
        return {"gone_healed": geheilt} if geheilt else {}
    print(f"\nPrüfe {len(kandidaten)} Mails, die nicht mehr im Postfach standen…")
    # Ohne eine einzige Anfrage: Wer unter derselben Message-ID anderswo im
    # Postfach steht, ist verschoben und nicht gelöscht.
    kandidaten, verschoben_lokal = verschoben_statt_weg(out, kandidaten, bestand)
    weg, verschoben = wirklich_weg(graph, kandidaten)
    verschoben += verschoben_lokal
    neue = [rel for rel in weg if rel not in bekannt]
    if neue:
        schreibe_verschwunden(pfad, bekannt, neue,
                              datetime.now(UTC).isoformat(timespec="seconds"))
    print(f"  {len(weg)} gelöscht ({len(neue)} neu), {verschoben} nur verschoben.")
    return {"gone_new": len(neue), "gone_total": len(bekannt) + len(neue),
            "moved": verschoben, "gone_healed": geheilt}


def _hilfe_gewuenscht(argv):
    """-h/--help beantworten, statt einen Ordner dieses Namens anzulegen.

    Diese Skripte deuten das erste freie Argument als Ausgabeordner. Ohne diese
    Abfrage legte `python3 outlook_export.py --help` brav einen Ordner namens „--help“ an
    und begann zu exportieren – einmal passiert und dann sogar eingecheckt.
    """
    return any(a in ("-h", "--help", "-help") for a in argv)


# ---------------------------------------------------------------------------
# Ordnerstruktur: eigener Schritt, eigenes Ergebnis
#
# Bis 2.x lief das bei jedem Export mit – zwei Minuten für über 400 Ordner,
# bevor eine
# einzige Mail geladen wurde. Der Baum ändert sich aber selten. Getrennt heißt:
# einmal abgleichen, danach liest der Export ihn von der Platte.
# ---------------------------------------------------------------------------
def baum_eintraege(graph):
    """Den Baum als flache Liste: Pfad, ID, Name, Elementzahl."""
    eintraege = []
    for top in build_tree(graph):
        for folder, rel_path in top["subtree"]:
            eintraege.append({
                "id": folder.get("id") or rel_path,
                "pfad": rel_path,
                "name": folder.get("displayName") or rel_path.rsplit("/", 1)[-1],
                "elemente": int(folder.get("totalItemCount") or 0),
            })
    eintraege.sort(key=lambda e: e["pfad"].lower())
    return eintraege


def gleiche_ordner_ab(argv):
    """--folders: nur die Struktur holen und ablegen, nichts exportieren."""
    out = Path(argv[0]) if argv else Path(OUT_ROOT)
    graph = auth.waehle_zugang(TokenClient, graph_login)
    print(f"Gleiche Ordnerstruktur ab: {out.resolve()}")
    vorher = folders.lade(out)
    daten = folders.speichere(out, baum_eintraege(graph), vorher)
    regeln = aktuelle_regeln()
    z = folders.zusammenfassung(daten, regeln)
    print(f"\n{z['ordner_gesamt']} Ordner, {z['mails_gesamt']} Mails im Postfach.")
    print(f"Nach den Regeln gewählt: {z['ordner_gewaehlt']} Ordner, "
          f"{z['mails_gewaehlt']} Mails.")
    for art, liste in (("neu", daten["neu"]), ("nicht mehr da", daten["verschwunden"]),
                       ("umbenannt", daten["umbenannt"])):
        if liste:
            print(f"  {len(liste)} {art}: " + ", ".join(liste[:5])
                  + (" …" if len(liste) > 5 else ""))
    print(f"Abgelegt: {folders.pfad(out)}")


def auswahl_aus_puffer(daten, regeln):
    """Aus folders.json die Auswahl bauen – in der Form, die der Export erwartet.

    Ein einziger Eintrag mit allen gewählten Ordnern: der Export braucht keine
    Gruppierung nach oberster Ebene, die stammt noch aus der interaktiven
    Abfrage.
    """
    gewaehlt = folders.gewaehlt(daten, regeln)
    if not gewaehlt:
        return []
    return [{"subtree": [({"id": e["id"], "totalItemCount": e.get("elemente")},
                          e["pfad"]) for e in gewaehlt]}]


def waehle_ordner(graph, out):
    """Welche Ordner exportiert werden – aus dem Puffer, sonst frisch.

    Der Puffer ist der Normalfall: zwei Minuten für über 400 Ordner will
    niemand bei jedem
    Lauf zahlen. Fehlt er, wird er einmal angelegt; danach entscheidet
    „Ordnerstruktur abgleichen“, wann er sich erneuert.
    """
    regeln = aktuelle_regeln()
    daten = folders.lade(out)
    if daten:
        z = folders.zusammenfassung(daten, regeln)
        print(f"\nOrdnerauswahl aus {folders.DATEI} (Stand {z['abgeglichen']}): "
              f"{z['ordner_gewaehlt']} von {z['ordner_gesamt']} Ordnern, "
              f"{z['mails_gewaehlt']} Mails.")
        if z["neu"]:
            print(f"  Hinweis: {len(z['neu'])} Ordner sind seit dem Abgleich neu "
                  f"dazugekommen und folgen den Regeln automatisch.")
        auswahl = auswahl_aus_puffer(daten, regeln)
        if auswahl:
            return auswahl
        print("  Die Regeln wählen keinen Ordner aus – nichts zu tun.")
        return []
    print("\nNoch kein Ordnerbaum – lade ihn einmalig (dauert bei großen "
          "Postfächern ein paar Minuten)…")
    daten = folders.speichere(out, baum_eintraege(graph))
    return auswahl_aus_puffer(daten, regeln)


def aktuelle_regeln():
    """Die Auswahlregeln – Umgebung schlägt Datei schlägt alte Namensliste.

    Wer aus einer früheren Fassung kommt, hat SKIP_FOLDERS gepflegt und soll
    seine Auswahl nicht neu eintippen müssen.
    """
    roh = os.environ.get("FOLDER_RULES")
    if roh is None:
        roh = settings.value("folder_rules", None)
    if roh:
        return folders.lies_regeln(roh)
    return folders.aus_namensliste(DEFAULT_SKIP_FOLDERS)


def nur_pruefen(argv):
    """--check: nur die Vollständigkeit melden, nichts exportieren."""
    out = Path(argv[0]) if argv else Path(OUT_ROOT)
    graph = auth.waehle_zugang(TokenClient, graph_login)
    print(f"Prüfe Vollständigkeit gegen das Postfach: {out.resolve()}")
    bericht = pruefe_vollstaendigkeit(
        graph, out, lies_verschwunden(out / GONE_FILE))
    ziel = schreibe_bericht(out, bericht)
    print(f"\n{bericht['erwartet']} erwartet, {bericht['vorhanden']} vorhanden, "
          f"{bericht['geloescht']} als gelöscht vermerkt, "
          f"{bericht['fehlt']} fehlen.")
    if bericht["ausgelassen"]:
        print(f"Nicht gezählt: {bericht['ausgelassen']} Mails in Ordnern, welche "
              f"die Auswahl auslässt ({', '.join(bericht['ausgelassene_ordner'])}).")
    for z in bericht["ordner"][:10]:
        if z["fehlt"]:
            print(f"  {z['fehlt']:>7} fehlen in {z['ordner']}")
    print(f"Bericht: {ziel}")


# ---------------------------------------------------------------------------
# Vollständigkeit: was Graph zählt gegen das, was auf der Platte liegt
#
# Graph liefert totalItemCount ohnehin mit der Ordnerliste – der Abgleich
# kostet also nichts extra. Allein wäre er nur ein Indikator: gelöschte Mails
# erzeugen eine Differenz, die keine Lücke ist. Erst zusammen mit
# verschwunden.tsv wird daraus eine Bilanz, in der jede Zahl erklärt ist.
# ---------------------------------------------------------------------------
BERICHT_DATEI = "vollstaendigkeit.json"


def zaehle_dateien(ordner):
    try:
        return sum(1 for p in Path(ordner).glob("*.eml") if p.is_file())
    except OSError:
        return 0


def pruefe_vollstaendigkeit(graph, out, weg=None):
    """Je Postfachordner: erwartet, vorhanden, gelöscht, Differenz.

    `weg` sind die als verschwunden vermerkten Pfade – sie erklären, warum
    weniger auf der Platte liegt, als Graph zählt.
    """
    weg = weg or {}
    weg_je_ordner = {}
    for rel in weg:
        ordner = rel.rsplit("/", 1)[0] if "/" in rel else ""
        weg_je_ordner[ordner] = weg_je_ordner.get(ordner, 0) + 1

    zeilen = []
    for top in build_tree(graph):
        # Ordner, welche die Auswahl auslässt (Archiv, Gelöschte Elemente,
        # Junk …), sind nicht unvollständig – sie sind absichtlich leer. Sie
        # als Lücke zu melden war beim ersten echten Lauf ein Fehlalarm über
        # knapp 20.000 Mails, und ein Bericht, der beim ersten Mal Unsinn zeigt,
        # wird nie wieder aufgemacht.
        ausgelassen = _is_default_skip(top)
        for folder, rel_path in top["subtree"]:
            erwartet = folder.get("totalItemCount")
            if erwartet is None:
                continue
            da = zaehle_dateien(out / rel_path)
            geloescht = weg_je_ordner.get(rel_path, 0)
            zeilen.append({
                "ordner": rel_path,
                "erwartet": int(erwartet),
                "vorhanden": da,
                "geloescht": geloescht,
                "ausgelassen": ausgelassen,
                # Positiv heißt: es fehlt etwas. Gelöschtes zählt nicht als
                # Lücke – es liegt ja noch im Archiv, nur nicht mehr im Postfach.
                "fehlt": 0 if ausgelassen else max(0, int(erwartet) - (da - geloescht)),
            })
    zeilen.sort(key=lambda z: (-z["fehlt"], z["ordner"]))
    gezaehlt = [z for z in zeilen if not z["ausgelassen"]]
    return {
        "geprueft": datetime.now(UTC).isoformat(timespec="seconds"),
        "ordner": zeilen,
        "erwartet": sum(z["erwartet"] for z in gezaehlt),
        "vorhanden": sum(z["vorhanden"] for z in gezaehlt),
        "geloescht": sum(z["geloescht"] for z in gezaehlt),
        "fehlt": sum(z["fehlt"] for z in gezaehlt),
        # Was die Auswahl bewusst auslässt – als Zahl, nicht als Lücke.
        "ausgelassen": sum(z["erwartet"] for z in zeilen if z["ausgelassen"]),
        "ausgelassene_ordner": sorted({z["ordner"].split("/")[1]
                                       for z in zeilen if z["ausgelassen"]
                                       and "/" in z["ordner"]}),
    }


def schreibe_bericht(out, bericht):
    ziel = Path(out) / BERICHT_DATEI
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(ziel.name + ".tmp")
    tmp.write_text(json.dumps(bericht, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ziel)
    return ziel


def main():
    if _hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__.strip())
        return
    # Für die Sonderläufe zählt nur der Ausgabeordner; Schalter wie -default
    # sind hier ohne Bedeutung und dürften keinesfalls als Ordner durchgehen.
    nur_ordner = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--check" in sys.argv[1:]:
        return nur_pruefen(nur_ordner)
    if "--folders" in sys.argv[1:]:
        return gleiche_ordner_ab(nur_ordner)
    if "--calendars" in sys.argv[1:]:
        return gleiche_kalender_ab(nur_ordner)

    global OUT_ROOT, ASSUME_DEFAULT
    argv = sys.argv[1:]
    if any(a in ("-default", "--default") for a in argv):
        ASSUME_DEFAULT = True
        print("Standardauswahl (-default) aktiv – keine Abfragen.")
    argv = [a for a in argv if a not in ("-default", "--default")]
    if argv:
        OUT_ROOT = argv[0]

    workers = settings.number("EXPORT_WORKERS", "workers", WORKERS)
    print(f"Ausgabeordner: {OUT_ROOT}")
    hinweis = settings.report()
    if hinweis:
        print(hinweis)
    if workers > 4:
        print("Hinweis: Exchange Online erlaubt nur 4 gleichzeitige Anfragen pro "
              f"Postfach – {workers} Worker erzeugen v. a. Drosselung. 4 ist das "
              "sinnvolle Maximum.")
    graph_client.konfiguriere(workers)

    graph = auth.waehle_zugang(TokenClient, graph_login)

    out = Path(OUT_ROOT)
    out.mkdir(parents=True, exist_ok=True)
    done = DoneLog(out / DONE_FILE)
    migrate_to_email_subdir(out, done)   # einmalig: Alt-Struktur -> E-Mail/
    stats = {"new": 0, "skipped": 0, "folder_errors": 0}
    result = "done"

    try:
        me = graph.get(f"{GRAPH}/me")
        print(f"Angemeldet als {me.get('displayName')} ({me.get('userPrincipalName')})")
        print(f"Parallele Downloads: {workers} (Exchange-Limit pro Postfach)")

        categories = prompt_categories()
        selected_mail, sel_cals, want_con = [], [], False

        if "mail" in categories:
            selected_mail = waehle_ordner(graph, out)
        if "calendar" in categories:
            sel_cals = waehle_kalender(graph, out)
        want_con = "contacts" in categories

        if selected_mail:
            bestand = Bestand()
            result = run_export(graph, out, done, stats, selected_mail, workers, bestand)
            if result == "done" and not stats.get("folder_errors"):
                stats.update(pruefe_verschwundene(graph, out, done, bestand))
        if result != "expired" and sel_cals:
            try:
                export_calendar(graph, out, done, stats, sel_cals)
            except TokenExpired:
                result = "expired"
        if result != "expired" and want_con:
            try:
                export_contacts(graph, out, done, stats)
            except TokenExpired:
                result = "expired"
    except TokenExpired:
        result = "expired"
    except (requests.exceptions.RequestException, RuntimeError) as e:
        # Netz endgültig weg (alle Wiederholungen aufgebraucht) – kein Traceback,
        # der Fortschritt in exported.tsv bleibt erhalten.
        result = "network"
        print(f"\nAbgebrochen: Verbindung zu Graph nicht möglich ({type(e).__name__}: {e})")
    except KeyboardInterrupt:
        result = "aborted"
        print("\nAbgebrochen durch Benutzer.")
    finally:
        done.close()

    if result == "expired":
        print("\nAbgebrochen: Token abgelaufen. Frischen Access Token in gx_token.txt "
              "setzen und erneut starten – bereits Exportiertes bleibt erhalten.")
        sys.exit(1)
    if result in ("network", "aborted"):
        print(f"Bis hier neu exportiert: {stats['new']}. Einfach neu starten – "
              "der Export setzt bei der letzten Mail fort.")
        sys.exit(1)

    def _count(suffix):
        return sum(1 for rel in done.done.values()
                   if rel.endswith(suffix) and (out / rel).exists())
    print(f"\nFertig. Neu exportiert: {stats['new']}, übersprungen: {stats['skipped']}.")
    progress.ergebnis(stats["new"], uebersprungen=stats["skipped"])
    if stats.get("folder_errors"):
        print(f"{stats['folder_errors']} Ordner konnten nicht vollständig gelistet "
              "werden – Skript erneut starten, um den Rest zu holen.")
    print(f"Im Archiv: {_count('.eml')} Mails, {_count('.ics')} Termine, "
          f"{_count('.vcf')} Kontakte.")
    if stats.get("gone_total"):
        print(f"Nicht mehr im Postfach: {stats['gone_total']} Mails "
              f"({stats.get('gone_new', 0)} neu erkannt) – die Dateien bleiben.")
    print(f"Ordner: {out.resolve()}")


if __name__ == "__main__":
    main()
