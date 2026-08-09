#!/usr/bin/env python3
"""
smoke_test.py – prüft ein gebautes Bündel, bevor es jemand herunterlädt.

    python3 packaging/smoke_test.py dist/Microsoft365-Archiv/Microsoft365-Archiv

Läuft in der CI nach jedem Build. Getestet wird genau das, was beim Bündeln
schiefgehen kann und beim reinen "Datei existiert"-Test unbemerkt bliebe:

  1. Der Selbstaufruf der Teilprogramme (--run …) findet die Module im Bündel.
  2. Die App startet, bindet einen Port und liefert die Oberfläche aus.
  3. Sie meldet sich als gebündelt und nutzt den übergebenen Datenordner.
  4. Ein Volltextindex lässt sich bauen – das startet die gebündelte Datei als
     eigenen Unterprozess, also die komplette Kette aus Punkt 1.
  5. Die eingebettete Suche findet den indizierten Inhalt (SQLite/FTS5 an Bord).
  6. Der MCP-Server startet (uvicorn/starlette/pydantic vollständig gebündelt).
  7. Die Sprachdateien sind im Bündel und die Browsersprache greift (de/en/fr).
  8. Kalender und Adressbuch entstehen – ein zweiter Selbstaufruf, diesmal von
     combined_search, mit eigenen Parsern (E-Mail, iCalendar, vCard).

Ohne Netz, ohne Graph, ohne Ollama – nur das Bündel selbst.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows-Konsolen nutzen eine Legacy-Codepage (cp1252); "→" in der Fortschritts-
# ausgabe lässt print() dort sonst mit UnicodeEncodeError sterben – und zwar
# bevor auch nur ein einziger Prüfschritt gelaufen ist. Dieselbe Umstellung wie
# in allen anderen Skripten des Projekts (auf macOS/Linux ein No-op).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Ein Termin und ein Kontakt: daraus baut combined_search Kalender und
# Adressbuch. Klein gehalten – geprüft wird das Bündeln, nicht das Auswerten.
ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:rauchtest-1
SUMMARY:Rauchtest-Termin
DTSTART:20250601T090000Z
DTEND:20250601T100000Z
END:VEVENT
END:VCALENDAR"""

VCF = """BEGIN:VCARD
VERSION:3.0
FN:Alice Example
EMAIL:alice@example.com
END:VCARD"""

TEAMS_HTML = """<html><body>
<h1>Projekt Alpha</h1>
<div class="msg">
  <span class="name">Alice Example</span>
  <span class="time">2025-06-01 09:30</span>
  <div class="body">Die Rechnung 4711 ist bezahlt.</div>
</div>
</body></html>"""


class Fehler(RuntimeError):
    pass


def schritt(text):
    print(f"→ {text}", flush=True)


def freier_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def hole(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:      # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def sende(url, body, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def warte_auf(bedingung, sekunden, was):
    ende = time.time() + sekunden
    letzter = None
    while time.time() < ende:
        try:
            wert = bedingung()
            if wert:
                return wert
        except (urllib.error.URLError, OSError, ValueError) as e:
            letzter = e
        time.sleep(0.5)
    raise Fehler(f"Zeitüberschreitung: {was}" + (f" ({letzter})" if letzter else ""))


def protokoll(basis):
    return "\n".join(f"  {zeile['level']:5} {zeile['text']}"
                     for zeile in hole(f"{basis}/api/log?since=0")["lines"])


def teilprogramm_aufrufbar(exe):
    schritt("Teilprogramm im Bündel aufrufen (--run rag_index --help)")
    r = subprocess.run([str(exe), "--run", "rag_index", "--help"],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or "--no-embeddings" not in r.stdout:
        raise Fehler("--run rag_index --help schlug fehl:\n"
                     f"  Code {r.returncode}\n  {r.stdout[-800:]}\n  {r.stderr[-800:]}")


def anmeldung_im_buendel(exe):
    """Die Selbstauskunft von auth.py aufrufen.

    Sie importiert msal und liest die Konfiguration – im Bündel der einzige Weg,
    den Anmeldeweg ohne Netz zu prüfen. Fehlte auth.py oder msal, liefe jeder
    Export sofort in einen ModuleNotFoundError, und das fiele erst beim Anwender
    auf: der Rauchtest startet selbst keinen Export (er hat keinen Token).
    """
    schritt("Anmeldung im Bündel (--run auth)")
    r = subprocess.run([str(exe), "--run", "auth"],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or "Client-ID" not in r.stdout:
        raise Fehler("--run auth schlug fehl:\n"
                     f"  Code {r.returncode}\n  {r.stdout[-800:]}\n  {r.stderr[-800:]}")


def testdaten(ordner):
    chat = Path(ordner) / "teams_export" / "1on1"
    chat.mkdir(parents=True)
    (chat / "alice__abc.html").write_text(TEAMS_HTML, encoding="utf-8")

    outlook = Path(ordner) / "outlook_export"
    (outlook / "kalender" / "Arbeit").mkdir(parents=True)
    (outlook / "kalender" / "Arbeit" / "termin.ics").write_text(ICS, encoding="utf-8")
    (outlook / "kontakte" / "Team").mkdir(parents=True)
    (outlook / "kontakte" / "Team" / "alice.vcf").write_text(VCF, encoding="utf-8")


def app_starten(exe, daten, port):
    schritt(f"App starten auf Port {port}")
    proc = subprocess.Popen(
        [str(exe), "--data-dir", str(daten), "--port", str(port), "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    return proc


def pruefe(exe, daten, port, proc):
    basis = f"http://127.0.0.1:{port}"

    def erreichbar():
        if proc.poll() is not None:
            aus = (proc.stdout.read() or b"").decode("utf-8", "replace")
            raise Fehler(f"App endete sofort (Code {proc.returncode}):\n{aus[-2000:]}")
        return hole(f"{basis}/api/status", timeout=3)

    st = warte_auf(erreichbar, 90, "App antwortet auf /api/status")

    schritt("Oberfläche ausliefern")
    with urllib.request.urlopen(f"{basis}/", timeout=10) as r:   # noqa: S310
        seite = r.read().decode("utf-8")
    if "Microsoft-365-Archiv" not in seite:
        raise Fehler("Die Oberfläche kam nicht vollständig zurück.")

    schritt("Als Bündel erkannt, Datenordner übernommen")
    if not st.get("frozen"):
        raise Fehler("Die App hält sich nicht für ein Bündel (frozen == False).")
    if Path(st["data_dir"]).resolve() != Path(daten).resolve():
        raise Fehler(f"Falscher Datenordner: {st['data_dir']} statt {daten}")

    schritt("Sprachdateien (Browsersprache und Umschaltung)")
    for kopf, erwartet in (("de-DE,de;q=0.9", "de"), ("fr-CH,fr;q=0.9", "fr"),
                           ("en-US,en;q=0.9", "en")):
        req = urllib.request.Request(f"{basis}/", headers={"Accept-Language": kopf})
        with urllib.request.urlopen(req, timeout=10) as r:                # noqa: S310
            seite = r.read().decode("utf-8")
        m = re.search(r'id="i18n">(.*?)</script>', seite, re.S)
        if not m:
            raise Fehler("Die Seite enthält keine Sprachdaten.")
        sprache = json.loads(m.group(1).replace("\\u003c", "<"))
        if sprache.get("lang") != erwartet:
            raise Fehler(f"Accept-Language {kopf} ergab {sprache.get('lang')}, "
                         f"erwartet {erwartet}")
        if not sprache.get("strings", {}).get("nav.export"):
            raise Fehler(f"Sprache {erwartet} hat keine Texte im Bündel.")

    schritt("Volltextindex bauen (Unterprozess = die gebündelte Datei selbst)")
    antwort = sende(f"{basis}/api/run",
                    {"index": True, "embeddings": False, "label": "Rauchtest"})
    if not antwort.get("ok"):
        raise Fehler(f"Index-Lauf nicht gestartet: {antwort}")

    def fertig():
        s = hole(f"{basis}/api/status")
        return None if s["jobs"]["busy"] else s["jobs"]["last"]

    letzter = warte_auf(fertig, 240, "Index-Lauf wird fertig")
    if not letzter.get("ok"):
        raise Fehler(f"Index-Lauf fehlgeschlagen: {letzter}\n{protokoll(basis)}")

    schritt("Eingebettete Suche")
    treffer = hole(f"{basis}/api/search?q=Rechnung&k=5")
    if treffer.get("count", 0) < 1:
        raise Fehler(f"Die Suche fand nichts: {treffer}\n{protokoll(basis)}")
    if "4711" not in json.dumps(treffer, ensure_ascii=False):
        raise Fehler(f"Unerwartetes Suchergebnis: {treffer}")

    schritt("Kalender & Kontakte aufbauen (Selbstaufruf von combined_search)")
    antwort = sende(f"{basis}/api/run", {"calendar": True, "label": "Rauchtest"})
    if not antwort.get("ok"):
        raise Fehler(f"Kalenderlauf nicht gestartet: {antwort}")
    letzter = warte_auf(fertig, 240, "Kalenderlauf wird fertig")
    if not letzter.get("ok"):
        raise Fehler(f"Kalenderlauf fehlgeschlagen: {letzter}\n{protokoll(basis)}")

    kal = hole(f"{basis}/api/calendar")
    titel = {r.get("title") for r in kal.get("recs", [])}
    if "Rauchtest-Termin" not in titel or "Alice Example" not in titel:
        raise Fehler(f"Kalender/Adressbuch unvollständig: {sorted(titel)}\n"
                     f"{protokoll(basis)}")

    schritt("MCP-Server starten")
    r = sende(f"{basis}/api/mcp", {"action": "start"})
    if not r.get("ok"):
        raise Fehler(f"MCP-Server startete nicht: {r}\n{protokoll(basis)}")
    lebt = warte_auf(lambda: hole(f"{basis}/api/status")["mcp"]["running"],
                     60, "MCP-Server läuft")
    if not lebt:
        raise Fehler(f"MCP-Server läuft nicht:\n{protokoll(basis)}")

    ausgabe = protokoll(basis)   # vor dem Beenden holen, danach hört niemand mehr zu
    schritt("Beenden")
    try:
        sende(f"{basis}/api/quit", {}, timeout=10)
    except (urllib.error.URLError, OSError):
        pass                     # Server ist beim Antworten schon weg – in Ordnung
    return ausgabe


def main():
    if len(sys.argv) < 2:
        raise SystemExit(f"Aufruf: {sys.argv[0]} <pfad/zur/ausfuehrbaren/datei>")
    exe = Path(sys.argv[1]).resolve()
    if not exe.exists():
        raise SystemExit(f"Nicht gefunden: {exe}")

    teilprogramm_aufrufbar(exe)
    anmeldung_im_buendel(exe)
    daten = Path(tempfile.mkdtemp(prefix="o365-rauchtest-"))
    port = freier_port()
    proc = None
    try:
        testdaten(daten)
        proc = app_starten(exe, daten, port)
        pruefe(exe, daten, port, proc)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(daten, ignore_errors=True)
    print("\nRauchtest bestanden.")


if __name__ == "__main__":
    try:
        main()
    except Fehler as e:
        print(f"\nRauchtest FEHLGESCHLAGEN:\n{e}", file=sys.stderr)
        raise SystemExit(1) from None
