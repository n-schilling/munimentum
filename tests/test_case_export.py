"""
case_export.py – a case as a folder of its own.

A small archive with one item of every kind, a case book pointing at
them, and the export run against it: what is copied, what the three
files say, what happens to an item the archive no longer holds.
"""

import csv
import sys
import json
import zipfile
import subprocess
from pathlib import Path

import pytest

import faelle
import progress
import case_export

RES = Path(__file__).resolve().parent.parent


def _archiv(tmp_path):
    """One item per source, laid out as the exports write them."""
    a = tmp_path / "archiv"
    outlook = a / "outlook"
    (outlook / "inbox").mkdir(parents=True)
    (outlook / "inbox" / "mail1.eml").write_text(
        "Message-ID: <m1@example.com>\nSubject: Rechnung 4711\n\nDie Rechnung.\n", encoding="utf-8")
    (outlook / "kalender" / "Arbeit").mkdir(parents=True)
    (outlook / "kalender" / "Arbeit" / "termin.ics").write_text("BEGIN:VEVENT\nUID:ev1\nEND:VEVENT\n")
    (outlook / "kontakte" / "Team").mkdir(parents=True)
    (outlook / "kontakte" / "Team" / "alice.vcf").write_text("BEGIN:VCARD\nUID:c1\nFN:Alice Beispiel\nEND:VCARD\n")
    teams = a / "teams"
    (teams / "1on1" / "Anhaenge" / "alice__abc").mkdir(parents=True)
    (teams / "1on1" / "alice__abc.html").write_text(
        '<html><body><div class="msg" id="m-42" data-id="42"><div class="body">Hallo</div></div></body></html>')
    (teams / "1on1" / "Anhaenge" / "alice__abc" / "plan.pdf").write_bytes(b"%PDF-1.4 plan")
    onedrive = a / "onedrive"
    (onedrive / "Dateien" / "Projekte").mkdir(parents=True)
    (onedrive / "Dateien" / "Projekte" / "Angebot.pdf").write_bytes(b"%PDF-1.4 angebot")
    sharepoint = a / "sharepoint"
    (sharepoint / "TeamX" / "Dokumente").mkdir(parents=True)
    (sharepoint / "TeamX" / "Dokumente" / "Plan.xlsx").write_bytes(b"xlsx")
    pages = a / "pages"
    (pages / "TeamX" / "SitePages").mkdir(parents=True)
    (pages / "TeamX" / "SitePages" / "Start.html").write_text("<html><body>Start</body></html>")
    planner = a / "planner"
    (planner / "Board A" / "Anhaenge").mkdir(parents=True)
    (planner / "Board A" / "board.html").write_text(
        '<html><body><details class="karte" id="k-t1"><summary>Aufgabe 1</summary>'
        '<a href="Anhaenge/skizze.png">skizze.png</a></details>'
        '<details class="karte" id="k-t2"><summary>Aufgabe 2</summary>'
        '<a href="Anhaenge/andere.png">andere.png</a></details></body></html>')
    (planner / "Board A" / "Anhaenge" / "skizze.png").write_bytes(b"png1")
    (planner / "Board A" / "Anhaenge" / "andere.png").write_bytes(b"png2")
    todo = a / "todo"
    (todo / "Einkauf__abcd1234").mkdir(parents=True)
    (todo / "Einkauf__abcd1234" / "list.html").write_text(
        '<html><body><details class="aufgabe" id="t-x1"><summary>Milch</summary></details></body></html>')
    onenote = a / "onenote"
    (onenote / "Projekte" / "Allgemein" / "Besprechung__aaaa1111.files").mkdir(parents=True)
    (onenote / "Projekte" / "Allgemein" / "Besprechung__aaaa1111.html").write_text("<html><body>Notiz</body></html>")
    (onenote / "Projekte" / "Allgemein" / "Besprechung__aaaa1111.files" / "bild.png").write_bytes(b"png")
    pfade = {"outlook": str(outlook), "teams": str(teams), "onedrive": str(onedrive),
             "sharepoint": str(sharepoint), "pages": str(pages), "planner": str(planner),
             "todo": str(todo), "onenote": str(onenote)}
    return a, pfade


EINTRAEGE = [
    {"key": "mail:<m1@example.com>", "src": "outlook", "root": "outlook", "rel": "inbox/mail1.eml",
     "titel": "Rechnung 4711", "datum": "2025-06-10 08:00", "wer": "Carla Chef"},
    {"key": "event:ev1", "src": "kalender", "root": "outlook", "rel": "kalender/Arbeit/termin.ics",
     "titel": "Quartalsplanung", "datum": "2025-06-15 14:00", "wer": "Alice Beispiel"},
    {"key": "contact:c1", "src": "kontakte", "root": "outlook", "rel": "kontakte/Team/alice.vcf",
     "titel": "Alice Beispiel", "datum": "", "wer": ""},
    {"key": "teams:19:abc#42", "src": "teams", "root": "teams", "rel": "1on1/alice__abc.html",
     "titel": "Projekt Alpha", "datum": "2025-06-01 09:30", "wer": "Alice Beispiel"},
    {"key": "file:od1", "src": "datei", "root": "onedrive", "rel": "Dateien/Projekte/Angebot.pdf",
     "titel": "Angebot.pdf", "datum": "2025-05-01 10:00", "wer": ""},
    {"key": "file:sp1", "src": "datei", "root": "sharepoint", "rel": "TeamX/Dokumente/Plan.xlsx",
     "titel": "Plan.xlsx", "datum": "2025-05-02 10:00", "wer": ""},
    {"key": "page:p1", "src": "pages", "root": "pages", "rel": "TeamX/SitePages/Start.html",
     "titel": "Start", "datum": "2025-05-03 10:00", "wer": ""},
    {"key": "planner:t1", "src": "planner", "root": "planner", "rel": "Board A/board.html",
     "titel": "Aufgabe 1", "datum": "2025-05-04 10:00", "wer": "Bob Baumeister"},
    {"key": "todo:x1", "src": "todo", "root": "todo", "rel": "Einkauf__abcd1234/list.html",
     "titel": "Milch", "datum": "2025-05-05 10:00", "wer": ""},
    {"key": "note:n1", "src": "onenote", "root": "onenote", "rel": "Projekte/Allgemein/Besprechung__aaaa1111.html",
     "titel": "Besprechung", "datum": "2025-05-06 10:00", "wer": ""},
    # gone from the archive – the case still remembers it
    {"key": "mail:<weg@example.com>", "src": "outlook", "root": "outlook", "rel": "inbox/weg.eml",
     "titel": "Verschwunden", "datum": "2025-01-01 08:00", "wer": "Doris Docs"},
]


@pytest.fixture
def welt(tmp_path):
    a, pfade = _archiv(tmp_path)
    buch = faelle.Fallbuch(tmp_path / "heim" / faelle.DB_NAME)
    fid = buch.fall_anlegen("Nordwind", "Die Rechnung 4711 und alles drumherum")
    buch.hinzufuegen(fid, EINTRAEGE)
    buch.notiz(fid, "Erste Notiz")
    buch.notiz(fid, "Zweite Notiz <mit & Zeichen>")
    k = faelle.kriterien({"q": "Rechnung", "source": "outlook", "person": "Carla"})
    buch.liste_anlegen(fid, k, [EINTRAEGE[0]])
    buch.speichern("Rechnungen", k, fid)
    return {"archiv": a, "pfade": pfade, "buch": buch, "fall": fid, "ziel": tmp_path / "export"}


# --------------------------------------------------------------------------
# What travels with an entry
# --------------------------------------------------------------------------
def test_plan_je_quelle(welt):
    pf = welt["pfade"]
    q, kopien, anker = case_export.plan(EINTRAEGE[3], pf)          # Teams: chat + its files
    assert kopien == ["1on1/alice__abc.html", "1on1/Anhaenge/alice__abc"] and anker == "#m-42"
    q, kopien, anker = case_export.plan(EINTRAEGE[7], pf)          # Planner: board + the card's files
    assert kopien == ["Board A/board.html", "Board A/Anhaenge/skizze.png"] and anker == "#k-t1"
    q, kopien, anker = case_export.plan(EINTRAEGE[8], pf)          # To Do: the list, anchor on the task
    assert kopien == ["Einkauf__abcd1234/list.html"] and anker == "#t-x1"
    q, kopien, anker = case_export.plan(EINTRAEGE[9], pf)          # OneNote: page + .files
    assert kopien == ["Projekte/Allgemein/Besprechung__aaaa1111.html",
                      "Projekte/Allgemein/Besprechung__aaaa1111.files"] and anker == ""
    q, kopien, anker = case_export.plan(EINTRAEGE[0], pf)
    assert kopien == ["inbox/mail1.eml"] and anker == ""
    # unknown root, no rel: nothing to copy
    assert case_export.plan({"src": "outlook", "root": "nirgends", "rel": "x"}, pf) == (None, [], "")
    assert case_export.plan({"src": "outlook", "root": "outlook", "rel": ""}, pf) == (None, [], "")


def test_msg_anker():
    assert case_export._msg_anker("teams:19:abc#42") == "#m-42"
    assert case_export._msg_anker("teams:1on1/alice.html#7") == "#m-7"
    assert case_export._msg_anker("mail:<x@y>") == ""
    assert case_export._msg_anker("") == ""


def test_quelle_key():
    assert case_export.quelle_key({"src": "outlook"}) == "search.source.outlook"
    assert case_export.quelle_key({"src": "datei", "root": "sharepoint"}) == "search.source.sharepoint"
    assert case_export.quelle_key({"src": "datei", "root": "teams"}) == "search.source.teams"
    assert case_export.quelle_key({"src": "datei", "root": "onedrive"}) == "search.source.onedrive"


# --------------------------------------------------------------------------
# The export
# --------------------------------------------------------------------------
def _entpackt(zip_pfad):
    """The ZIP's contents next to it, for looking inside."""
    ziel = zip_pfad.parent / "entpackt"
    zipfile.ZipFile(zip_pfad).extractall(ziel)
    return ziel / zip_pfad.stem


def test_export_kopiert_originale_und_schreibt_die_drei_dateien(welt):
    ordner = welt["ziel"] / "Nordwind_2026-01-01_1200"
    zip_pfad, zeilen, fehlt = case_export.exportieren(
        welt["buch"], welt["fall"], welt["pfade"], ordner, lang="en", res=RES)
    # one ZIP, nothing beside it
    assert zip_pfad == ordner.with_suffix(".zip") and zip_pfad.exists()
    assert not ordner.exists(), "the folder is packed and gone"
    assert [p.name for p in welt["ziel"].iterdir()] == [zip_pfad.name]
    ziel = _entpackt(zip_pfad)
    assert fehlt == 1 and len(zeilen) == len(EINTRAEGE)
    # the originals, under their source's folder and path
    assert (ziel / "Outlook" / "inbox" / "mail1.eml").read_text().startswith("Message-ID")
    assert (ziel / "Outlook" / "kalender" / "Arbeit" / "termin.ics").exists()
    assert (ziel / "Outlook" / "kontakte" / "Team" / "alice.vcf").exists()
    assert (ziel / "Teams" / "1on1" / "alice__abc.html").exists()
    assert (ziel / "Teams" / "1on1" / "Anhaenge" / "alice__abc" / "plan.pdf").read_bytes() == b"%PDF-1.4 plan"
    assert (ziel / "OneDrive" / "Dateien" / "Projekte" / "Angebot.pdf").exists()
    assert (ziel / "SharePoint" / "TeamX" / "Dokumente" / "Plan.xlsx").exists()
    assert (ziel / "SharePoint pages" / "TeamX" / "SitePages" / "Start.html").exists()
    assert (ziel / "Planner" / "Board A" / "board.html").exists()
    assert (ziel / "Planner" / "Board A" / "Anhaenge" / "skizze.png").exists()
    assert not (ziel / "Planner" / "Board A" / "Anhaenge" / "andere.png").exists(), \
        "only the card's own files travel"
    assert (ziel / "To Do" / "Einkauf__abcd1234" / "list.html").exists()
    assert (ziel / "OneNote" / "Projekte" / "Allgemein" / "Besprechung__aaaa1111.files" / "bild.png").exists()
    assert not (ziel / "Outlook" / "inbox" / "weg.eml").exists()
    # nothing was written into the archive
    assert not list(welt["archiv"].rglob("index.html"))

    html = (ziel / "index.html").read_text(encoding="utf-8")
    assert "<title>Nordwind</title>" in html and "Die Rechnung 4711 und alles drumherum" in html
    assert "Zweite Notiz &lt;mit &amp; Zeichen&gt;" in html                 # escaped
    assert 'href="Outlook/inbox/mail1.eml"' in html
    assert 'href="Teams/1on1/alice__abc.html#m-42"' in html
    assert 'href="Planner/Board A/board.html#k-t1"' in html
    assert 'href="To Do/Einkauf__abcd1234/list.html#t-x1"' in html
    assert "no longer in the archive" in html and "Verschwunden" in html
    assert "Rechnungen" in html and "Carla" in html                          # the attached search
    assert "Items (11)" in html and "Casebook" in html and "Result lists" in html
    assert "Exported with Munimentum" in html

    zeilen_csv = list(csv.DictReader((ziel / "items.csv").read_text(encoding="utf-8").splitlines()))
    assert len(zeilen_csv) == 11
    mail = next(z for z in zeilen_csv if z["key"] == "mail:<m1@example.com>")
    assert mail == {"key": "mail:<m1@example.com>", "folder": "", "source": "Mail", "date": "2025-06-10 08:00",
                    "who": "Carla Chef", "title": "Rechnung 4711", "remark": "", "origin": "page",
                    "file": "Outlook/inbox/mail1.eml", "original": "inbox/mail1.eml", "in_archive": "yes"}
    weg = next(z for z in zeilen_csv if z["title"] == "Verschwunden")
    assert weg["in_archive"] == "no" and weg["file"] == ""
    planner = next(z for z in zeilen_csv if z["key"] == "planner:t1")
    assert planner["file"] == "Planner/Board A/board.html#k-t1"

    md = (ziel / "casebook.md").read_text(encoding="utf-8")
    assert md.startswith("# Nordwind\n") and "- " in md and "Erste Notiz" in md
    assert "Zweite Notiz <mit & Zeichen>" in md                              # plain text, not escaped
    assert "no longer in the archive" in md

    # a case without folders: no folder level, no folder column filled
    assert all(z["origin"] == "page" and z["folder"] == "" for z in zeilen_csv)
    assert "via MCP" not in html
    # the case remembers the ZIP – the page opens its folder
    fall = welt["buch"].fall(welt["fall"])
    assert fall["exportiert"] == str(zip_pfad) and fall["exportiert_wann"]


def test_export_in_deutsch(welt):
    ordner = welt["ziel"] / "Nordwind_de"
    zip_pfad, zeilen, fehlt = case_export.exportieren(
        welt["buch"], welt["fall"], welt["pfade"], ordner, lang="de", res=RES)
    namen = zipfile.ZipFile(zip_pfad).namelist()
    assert "Nordwind_de/index.html" in namen and "Nordwind_de/Outlook/inbox/mail1.eml" in namen
    html = (_entpackt(zip_pfad) / "index.html").read_text(encoding="utf-8")
    assert "Fallbuch" in html and "Einträge (11)" in html and "nicht mehr im Archiv" in html


def test_export_mit_ordnern_und_herkunft(welt):
    """The case's folders become the ZIP's folders, unsorted items land in
    "Unsorted"; what Claude added says "via MCP" – in the page, the CSV
    and the casebook."""
    buch, fid = welt["buch"], welt["fall"]
    belege = buch.ordner_anlegen(fid, "Belege / Rechnungen")      # a name the disk cannot take as is
    vertraege = buch.ordner_anlegen(fid, "Verträge")
    buch.verschieben(fid, ["mail:<m1@example.com>", "teams:19:abc#42"], belege)
    buch.verschieben(fid, ["file:sp1"], vertraege)
    buch.hinzufuegen(fid, [{"key": "mail:<m9@example.com>", "src": "outlook", "root": "outlook",
                            "rel": "inbox/m9.eml", "titel": "Von Claude"}], ordner_id=belege, quelle=faelle.MCP)
    buch.notiz(fid, "Claudes Notiz", quelle=faelle.MCP)
    buch.bemerkung_setzen(fid, "mail:<m1@example.com>", "Der Beleg <x>")
    zip_pfad, zeilen, fehlt = case_export.exportieren(
        buch, fid, welt["pfade"], welt["ziel"] / "Nordwind_ordner", lang="en", res=RES)
    ziel = _entpackt(zip_pfad)
    assert (ziel / "Belege _ Rechnungen" / "Outlook" / "inbox" / "mail1.eml").exists()
    assert (ziel / "Belege _ Rechnungen" / "Teams" / "1on1" / "alice__abc.html").exists()
    assert (ziel / "Verträge" / "SharePoint" / "TeamX" / "Dokumente" / "Plan.xlsx").exists()
    assert (ziel / "Unsorted" / "OneDrive" / "Dateien" / "Projekte" / "Angebot.pdf").exists()
    assert not (ziel / "Outlook").exists(), "with folders, nothing lies at the top"
    html = (ziel / "index.html").read_text(encoding="utf-8")
    # one heading per folder, the case's order, unsorted last
    assert html.index("<h2>Belege / Rechnungen") < html.index("<h2>Verträge") < html.index("<h2>Unsorted")
    assert 'href="Belege _ Rechnungen/Teams/1on1/alice__abc.html#m-42"' in html
    assert html.count('class="mcp">via MCP<') == 2                  # the item and the note
    assert "Von Claude" in html.split('<h2>Belege')[1].split("<h2>")[0]
    zeilen_csv = list(csv.DictReader((ziel / "items.csv").read_text(encoding="utf-8").splitlines()))
    claude = next(z for z in zeilen_csv if z["key"] == "mail:<m9@example.com>")
    assert claude["folder"] == "Belege / Rechnungen" and claude["origin"] == "mcp" and claude["in_archive"] == "no"
    ohne = next(z for z in zeilen_csv if z["key"] == "file:od1")
    assert ohne["folder"] == "" and ohne["origin"] == "page"
    assert ohne["file"] == "Unsorted/OneDrive/Dateien/Projekte/Angebot.pdf"
    md = (ziel / "casebook.md").read_text(encoding="utf-8")
    assert "### Belege / Rechnungen" in md and "### Unsorted" in md
    assert "· via MCP – Claudes Notiz" in md and "Von Claude · via MCP" in md
    # the remark: under the item in the page, a column in the CSV
    assert 'class="bem">Der Beleg &lt;x&gt;</div>' in html
    m1 = next(z for z in zeilen_csv if z["key"] == "mail:<m1@example.com>")
    assert m1["remark"] == "Der Beleg <x>" and claude["remark"] == ""
    # the timeline: items and notes by month, oldest first, undated last
    zeit = (ziel / "timeline.html").read_text(encoding="utf-8")
    assert zeit.index("<h2>2025-05</h2>") < zeit.index("<h2>2025-06</h2>") < zeit.index("Claudes Notiz")
    assert zeit.index("Angebot.pdf") < zeit.index("Projekt Alpha") < zeit.index("Rechnung 4711")
    assert 'class="bem">Der Beleg &lt;x&gt;</div>' in zeit and zeit.count('class="mcp">via MCP<') == 2
    assert zeit.index("Claudes Notiz") < zeit.index("Without date") < zeit.index("Alice Beispiel</a>")


def test_export_eines_geschlossenen_und_leeren_falls(welt):
    buch = welt["buch"]
    leer = buch.fall_anlegen("Leer", "")
    buch.schliessen(leer)
    zip_pfad, zeilen, fehlt = case_export.exportieren(buch, leer, welt["pfade"], welt["ziel"] / "Leer",
                                                      lang="en", res=RES)
    ziel = _entpackt(zip_pfad)
    assert zeilen == [] and fehlt == 0
    html = (ziel / "index.html").read_text(encoding="utf-8")
    assert "No items." in html and "No notes." in html and "Closed" in html
    assert (ziel / "items.csv").read_text().strip() == "key,folder,source,date,who,title,remark,origin,file,original,in_archive"


def test_unbekannter_fall(welt):
    with pytest.raises(faelle.KeinFall):
        case_export.exportieren(welt["buch"], 999, welt["pfade"], welt["ziel"] / "x")


def test_zielordner_ist_immer_frisch(tmp_path):
    a = case_export.zielordner(tmp_path, 'Nord/wind: "2026"')
    assert a.parent == tmp_path and "/" not in a.name and ":" not in a.name and '"' not in a.name
    a.mkdir()
    b = case_export.zielordner(tmp_path, 'Nord/wind: "2026"')
    assert b != a and b.name.startswith(a.name)


def test_ohne_texte_bleiben_die_schluessel_lesbar(welt):
    """No lang folder (a stripped install): the labels fall back to the
    keys rather than crashing."""
    t = case_export.Texte({})
    assert t("cases.export.items", n=3) == "cases.export.items"
    zip_pfad, _, _ = case_export.exportieren(welt["buch"], welt["fall"], welt["pfade"], welt["ziel"] / "roh",
                                             lang="xx", res=welt["ziel"])
    assert "roh/index.html" in zipfile.ZipFile(zip_pfad).namelist()


# --------------------------------------------------------------------------
# As the app runs it: a subprocess speaking the progress protocol
# --------------------------------------------------------------------------
def _lauf(welt, *extra, fall=None):
    pf = welt["pfade"]
    argv = [sys.executable, str(RES / "case_export.py"), pf["teams"], pf["outlook"], pf["onedrive"],
            "--sharepoint", pf["sharepoint"], "--pages", pf["pages"], "--planner", pf["planner"],
            "--todo", pf["todo"], "--onenote", pf["onenote"],
            "--faelle", str(welt["buch"].pfad), "--fall", str(fall or welt["fall"]),
            "--ziel", str(welt["ziel"] / "lauf"), "--lang", "en", "--res", str(RES), *extra]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=str(welt["archiv"]))


def test_main_meldet_ueber_das_protokoll(welt):
    r = _lauf(welt)
    assert r.returncode == 0, r.stderr
    events = [progress.lies_event(z) for z in r.stdout.splitlines() if z.startswith(progress.MARKE_LOG)]
    keys = [e["k"] for e in events]
    assert keys == ["run.case.start", "run.case.missing", "run.case.done"]
    assert events[0]["v"] == {"name": "Nordwind", "n": 11}
    assert events[1]["level"] == "warn" and events[1]["v"] == {"n": 1}
    assert events[2]["v"]["n"] == 10 and events[2]["v"]["path"] == str(welt["ziel"] / "lauf.zip")
    ergebnis = [progress.lies_ergebnis(z) for z in r.stdout.splitlines() if z.startswith(progress.MARKE_ERGEBNIS)]
    assert ergebnis == [{"new": 0, "extra": {"items": 10, "missing": 1}}]
    fortschritt = [z for z in r.stdout.splitlines() if z.startswith(progress.MARKE)]
    assert len(fortschritt) == 11                       # one line per item
    assert (welt["ziel"] / "lauf.zip").exists() and not (welt["ziel"] / "lauf").exists()


def test_main_ohne_fall(welt):
    r = _lauf(welt, fall=999)
    assert r.returncode == 1
    events = [progress.lies_event(z) for z in r.stdout.splitlines() if z.startswith(progress.MARKE_LOG)]
    assert events[0]["k"] == "run.case.unknown" and events[0]["level"] == "err"
    ergebnis = [json.loads(z.split(" ", 1)[1]) for z in r.stdout.splitlines() if z.startswith(progress.MARKE_ERGEBNIS)]
    assert ergebnis == [{"new": 0, "errors": 1}]
