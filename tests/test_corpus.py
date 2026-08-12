"""Tests für corpus.py – Parsen der Exporte und Chunking (nur Standardbibliothek)."""

import textwrap

import pytest

import corpus


# --------------------------------------------------------------------------
# HTML / Text-Aufbereitung
# --------------------------------------------------------------------------
def test_strip_html_removes_tags_scripts_and_entities():
    s = "<p>Hallo <b>Welt</b></p><script>alert(1)</script><style>p{}</style>&amp; mehr"
    out = corpus.strip_html(s)
    assert "alert" not in out
    assert "p{}" not in out
    assert "<" not in out
    assert "Hallo" in out and "Welt" in out
    assert "& mehr" in out


def test_collapse_whitespace_and_cap():
    assert corpus.collapse("  a \n\t b   c ") == "a b c"
    assert corpus.collapse("x" * 100, cap=10) == "x" * 10
    assert corpus.collapse(None) == ""


def test_strip_quoted_cuts_outlook_history():
    text = textwrap.dedent("""\
        Danke, passt für mich!

        ________________________________
        Von: Alice Example <alice@example.com>
        Gesendet: Montag, 7. Juli 2025 10:00
        Betreff: AW: Termin
        Alter zitierter Text.
        """)
    out = corpus.strip_quoted(text)
    assert "Danke, passt für mich!" in out
    assert "Alter zitierter Text" not in out
    assert "Gesendet" not in out


def test_strip_quoted_cuts_on_wrote_marker_and_quote_lines():
    text = "Neue Antwort.\n\nAm 07.07.2025 um 10:00 schrieb Bob:\n> alte Zeile\n> noch eine\n"
    out = corpus.strip_quoted(text)
    assert "Neue Antwort." in out
    assert "alte Zeile" not in out


def test_strip_quoted_cuts_signature():
    text = "Kurze Antwort.\n-- \nAlice Example\nFirma GmbH\n"
    out = corpus.strip_quoted(text)
    assert "Kurze Antwort." in out
    assert "Firma GmbH" not in out


def test_parse_local():
    assert corpus.parse_local("2025-07-07 10:00") is not None
    assert corpus.parse_local("kein datum") is None
    assert corpus.parse_local(None) is None


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------
def test_split_short_text_is_single_chunk():
    assert corpus._split("Hallo Welt", 100, 20) == ["Hallo Welt"]
    assert corpus._split("   ", 100, 20) == []
    assert corpus._split("", 100, 20) == []


def test_split_long_text_overlaps_and_covers():
    words = " ".join(f"wort{i}" for i in range(200))
    chunks = corpus._split(words, size=120, overlap=30)
    assert len(chunks) > 1
    # Jedes Stück ist Substring des Originals; Anfang und Ende sind abgedeckt
    for c in chunks:
        assert c in words
    assert words.startswith(chunks[0])
    assert words.endswith(chunks[-1])
    # Überlappung: der Anfang jedes Stücks liegt noch im Vorgänger
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert b[:15] in a
    # Kein Stück (deutlich) über der Zielgröße
    assert all(len(c) <= 120 for c in chunks)


def test_chunk_records_assigns_chunk_ids():
    rec = {"uid": "outlook:a.eml:0", "title": "Betreff", "text": "kurzer text"}
    chunks = corpus.chunk_records([rec], size=1500, overlap=200)
    assert len(chunks) == 1
    assert chunks[0]["cid"] == "outlook:a.eml:0#0"

    long_rec = {"uid": "u", "title": "t", "text": "x" * 4000}
    chunks = corpus.chunk_records([long_rec], size=1500, overlap=200)
    assert len(chunks) > 1
    assert [c["cid"] for c in chunks] == [f"u#{j}" for j in range(len(chunks))]


def test_embed_text_and_hash_are_deterministic():
    c = {"title": "Betreff", "text": "Inhalt"}
    assert corpus.embed_text(c) == "Betreff\nInhalt"
    assert corpus.chunk_hash(c) == corpus.chunk_hash(dict(c))
    assert corpus.chunk_hash(c) != corpus.chunk_hash({"title": "Betreff", "text": "anders"})


# --------------------------------------------------------------------------
# Teams-HTML
# --------------------------------------------------------------------------
TEAMS_HTML = """<html><body>
<h1>Projekt Alpha</h1>
<div class="msg">
  <span class="name">Alice Example</span>
  <span class="time">2025-06-01 09:30</span>
  <div class="body"><p>Hallo <b>Bob</b>,</p><div>wie besprochen.</div></div>
</div>
<div class="msg">
  <span class="name">Bob</span>
  <span class="time">2025-06-01 09:35</span>
  <div class="body">Danke!</div>
</div>
</body></html>"""


def test_conv_parser_extracts_title_and_messages():
    pr = corpus.ConvParser()
    pr.feed(TEAMS_HTML)
    pr.finish()
    assert pr.title == "Projekt Alpha"
    assert len(pr.msgs) == 2
    assert pr.msgs[0]["n"] == "Alice Example"
    assert pr.msgs[0]["t"] == "2025-06-01 09:30"
    assert "Hallo Bob" in " ".join(pr.msgs[0]["x"].split())
    assert pr.msgs[1] == {"n": "Bob", "t": "2025-06-01 09:35", "x": "Danke!"}


def test_load_teams_builds_records(tmp_path):
    d = tmp_path / "1on1"
    d.mkdir()
    (d / "alice__abc123.html").write_text(TEAMS_HTML, encoding="utf-8")
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")  # wird ignoriert

    recs = corpus.load_teams(str(tmp_path))
    assert len(recs) == 2
    r = recs[0]
    assert r["uid"] == "teams:1on1/alice__abc123.html:0"
    assert r["src"] == "teams"
    assert r["ctx"] == "1:1-Chat"
    assert r["who"] == "Alice Example"
    assert "alice example" in r["ppl"]
    assert r["ts"] is not None


# --------------------------------------------------------------------------
# Outlook (.eml)
# --------------------------------------------------------------------------
EML = b"""\
From: Alice Example <alice@example.com>
To: Bob Builder <bob@example.com>
Subject: Testmail
Date: Mon, 07 Jul 2025 10:00:00 +0000
Content-Type: text/plain; charset=utf-8

Hallo Bob,

hier die neue Nachricht.

________________________________
Von: Bob Builder <bob@example.com>
Gesendet: Sonntag, 6. Juli 2025 09:00
Alter zitierter Verlauf.
"""


def test_load_outlook_parses_eml(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    (d / "mail.eml").write_bytes(EML)

    recs = corpus.load_outlook(str(tmp_path))
    assert len(recs) == 1
    r = recs[0]
    assert r["uid"] == "outlook:inbox/mail.eml:0"
    assert r["who"] == "Alice Example"
    assert r["title"] == "Testmail"
    assert r["ctx"] == "inbox"
    assert "bob@example.com" in r["ppl"]
    assert "hier die neue Nachricht" in r["text"]
    assert "Alter zitierter Verlauf" not in r["text"]
    assert r["ts"] is not None


# --------------------------------------------------------------------------
# Kalender (.ics) und Kontakte (.vcf)
# --------------------------------------------------------------------------
ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "BEGIN:VEVENT",
    "SUMMARY:Planung\\, Quartal",
    "LOCATION:Raum 42",
    "DESCRIPTION:Agenda folgt",
    "DTSTART:20250601T120000Z",
    'ORGANIZER;CN="Alice Example":mailto:alice@example.com',
    'ATTENDEE;CN="Bob Builder":mailto:bob@example.com',
    "END:VEVENT",
    "END:VCALENDAR",
])


def test_load_calendar_parses_ics(tmp_path):
    d = tmp_path / "kalender" / "Arbeit"
    d.mkdir(parents=True)
    (d / "termin.ics").write_text(ICS, encoding="utf-8")

    recs = corpus.load_calendar(str(tmp_path))
    assert len(recs) == 1
    r = recs[0]
    assert r["title"] == "Planung, Quartal"
    # Der Ordnerpfad, damit die Suche danach filtern kann wie nach E-Mail/…
    assert r["ctx"] == "kalender/Arbeit"
    assert r["who"] == "Alice Example"
    assert "bob@example.com" in r["ppl"]
    assert r["text"].startswith("Ort: Raum 42.")
    assert r["ts"] is not None


VCF = "\r\n".join([
    "BEGIN:VCARD",
    "FN:Alice Example",
    "N:Example;Alice;;;",
    "ORG:Firma GmbH;Entwicklung",
    "TITLE:Engineer",
    "EMAIL:alice@example.com",
    "TEL:+49 123 456",
    "NOTE:Erste Zeile",
    " weiter gefaltet",
    "END:VCARD",
])


def test_load_contacts_parses_vcf(tmp_path):
    d = tmp_path / "kontakte" / "Team"
    d.mkdir(parents=True)
    (d / "alice.vcf").write_text(VCF, encoding="utf-8")

    recs = corpus.load_contacts(str(tmp_path))
    assert len(recs) == 1
    r = recs[0]
    assert r["title"] == "Alice Example"
    assert r["ctx"] == "Kontakte: Team"
    assert "Firma GmbH · Entwicklung" in r["text"]
    assert "Erste Zeileweiter gefaltet" in r["text"]  # RFC-Zeilenfaltung aufgelöst
    assert "alice@example.com" in r["ppl"]


def test_ics_when_variants():
    ts, disp = corpus._ics_when("20250601", dateonly=True)
    assert disp == "2025-06-01" and ts is not None
    ts, disp = corpus._ics_when("20250601T120000", dateonly=False)
    assert disp == "2025-06-01 12:00" and ts is not None
    ts, disp = corpus._ics_when("", dateonly=False)
    assert ts is None and disp == ""
    ts, disp = corpus._ics_when("unsinn", dateonly=False)
    assert ts is None and disp == "unsinn"


# --------------------------------------------------------------------------
# Verlauf: welche Mails zusammengehören
#
# Alles dafür steht in den .eml-Dateien – ein Neu-Export ist nicht nötig. Am
# echten Bestand gemessen (Stichprobe 400 von 45.615): Thread-Index 89 %,
# References/In-Reply-To 58 %, Message-ID 100 %.
# --------------------------------------------------------------------------
def _msg(**kopf):
    from email import policy
    from email.parser import BytesParser
    roh = "".join(f"{k.replace('_', '-')}: {v}\n" for k, v in kopf.items())
    return BytesParser(policy=policy.default).parsebytes(
        (roh + "Subject: X\n\nText\n").encode("utf-8"))


def test_thread_index_gewinnt():
    import base64
    kopf = bytes(range(22))
    key = corpus.thread_key(_msg(Thread_Index=base64.b64encode(kopf).decode(),
                                 References="<anders@x>"))
    assert key == "tix:" + kopf.hex()


def test_antwort_landet_im_selben_gespraech():
    """Exchange hängt je Antwort 5 Byte an – nur die ersten 22 zählen."""
    import base64
    kopf = bytes(range(22))
    erste = corpus.thread_key(_msg(Thread_Index=base64.b64encode(kopf).decode()))
    antwort = corpus.thread_key(
        _msg(Thread_Index=base64.b64encode(kopf + b"\x01\x02\x03\x04\x05").decode()))
    assert erste == antwort


def test_references_nimmt_den_anfang_des_gespraechs():
    """Nicht die letzte Nachricht, sondern die erste – sonst zerfiele ein
    Verlauf in so viele Gespräche, wie er Antworten hat."""
    key = corpus.thread_key(_msg(References="<start@x> <mitte@x> <ende@x>"))
    assert key == "mid:start@x"


def test_in_reply_to_als_naechstes():
    assert corpus.thread_key(_msg(In_Reply_To="<vorher@x>")) == "mid:vorher@x"


def test_ohne_alles_ein_gespraech_fuer_sich():
    """Ein Verlauf aus einer Nachricht ist richtig, nur langweilig – besser als
    gar keine Zuordnung."""
    assert corpus.thread_key(_msg(Message_ID="<allein@x>")) == "mid:allein@x"


def test_kaputter_thread_index_faellt_zurueck():
    key = corpus.thread_key(_msg(Thread_Index="das ist kein base64!!",
                                 Message_ID="<rettung@x>"))
    assert key == "mid:rettung@x"


def test_zu_kurzer_thread_index_faellt_zurueck():
    import base64
    key = corpus.thread_key(_msg(Thread_Index=base64.b64encode(b"kurz").decode(),
                                 Message_ID="<rettung@x>"))
    assert key == "mid:rettung@x"


def test_ohne_jede_kopfzeile_leer():
    assert corpus.thread_key(_msg()) == ""


def test_gross_und_kleinschreibung_egal():
    a = corpus.thread_key(_msg(Message_ID="<Gross@Example.COM>"))
    b = corpus.thread_key(_msg(In_Reply_To="<gross@example.com>"))
    assert a == b


# --------------------------------------------------------------------------
# Verschwundene Mails: die Datei bleibt, der Vermerk kommt dazu
# --------------------------------------------------------------------------
def test_verschwundene_werden_markiert(tmp_path):
    post = tmp_path / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    for name in ("weg.eml", "da.eml"):
        (post / name).write_bytes(
            b"From: a@b.c\nTo: d@e.f\nSubject: X\nDate: Sun, 1 Jun 2025 10:00:00 +0000\n\nText\n")
    (tmp_path / "verschwunden.tsv").write_text(
        "E-Mail/Posteingang/weg.eml\t2026-03-12T09:00:00\n", encoding="utf-8")

    recs = {r["rel"]: r for r in corpus.load_outlook(str(tmp_path))}
    assert recs["E-Mail/Posteingang/weg.eml"]["gone"] == "2026-03-12T09:00:00"
    assert "gone" not in recs["E-Mail/Posteingang/da.eml"]
    # Die Datei liegt weiterhin da – das ist der Unterschied zwischen einer
    # Kopie und einem Archiv.
    assert (post / "weg.eml").exists()


def test_ohne_verschwundene_datei_ist_nichts_markiert(tmp_path):
    post = tmp_path / "E-Mail"
    post.mkdir(parents=True)
    (post / "a.eml").write_bytes(b"From: a@b.c\nSubject: X\n\nText\n")
    assert all("gone" not in r for r in corpus.load_outlook(str(tmp_path)))


# --------------------------------------------------------------------------
# Anhänge: der Vertrag lag im Archiv, war aber mit keinem Wort zu finden
# --------------------------------------------------------------------------
def _mit_anhang(*namen, inline=()):
    from email import policy
    from email.parser import BytesParser
    teile = [b"Content-Type: text/plain\n\nText\n"]
    for n in namen:
        teile.append(f'Content-Type: application/pdf\n'
                     f'Content-Disposition: attachment; filename="{n}"\n\nx\n'.encode())
    for n in inline:
        teile.append(f'Content-Type: image/png\n'
                     f'Content-Disposition: inline; filename="{n}"\n\nx\n'.encode())
    roh = (b"From: a@b.c\nSubject: X\nMIME-Version: 1.0\n"
           b"Content-Type: multipart/mixed; boundary=B\n\n--B\n"
           + b"--B\n".join(teile) + b"--B--\n")
    return BytesParser(policy=policy.default).parsebytes(roh)


def test_anhaenge_werden_gefunden():
    assert corpus.anhaenge(_mit_anhang("Vertrag Musterkunde.pdf", "Anhang 2.docx")) == \
        ["Vertrag Musterkunde.pdf", "Anhang 2.docx"]


def test_inline_bilder_zaehlen_nicht():
    """Signaturlogos heißen image001.png und würden die Suche fluten."""
    assert corpus.anhaenge(_mit_anhang("echt.pdf", inline=("image001.png",))) == ["echt.pdf"]


def test_anhang_ohne_namen_wird_uebergangen():
    assert corpus.anhaenge(_mit_anhang()) == []


def test_doppelte_namen_nur_einmal():
    assert corpus.anhaenge(_mit_anhang("gleich.pdf", "gleich.pdf")) == ["gleich.pdf"]


@pytest.mark.parametrize("roh,erwartet", [
    ("../../.ssh/id_rsa", "id_rsa"),          # Pfad zuerst weg, dann die Zeichen
    ("C:\\Temp\\x.pdf", "x.pdf"),
    (".profile", "profile"),                  # kein verstecktes Ergebnis
    ("normal.pdf", "normal.pdf"),
    ("", "anhang"),
    ("a" * 300, "a" * 150),
])
def test_dateiname_wird_entschaerft(roh, erwartet):
    assert corpus.sicherer_dateiname(roh) == erwartet


def test_anhangnamen_nur_am_ersten_stueck():
    """Sonst zählte die Volltextsuche sie so oft, wie die Mail Stücke hat –
    eine lange Mail stünde allein deshalb weiter oben."""
    lang = {"uid": "outlook:a.eml:0", "text": "wort " * 2000, "att": "Vertrag.pdf",
            "src": "outlook", "root": "outlook", "rel": "a.eml"}
    stuecke = corpus.chunk_records([lang], size=500, overlap=50)
    assert len(stuecke) > 1, "Testtext war zu kurz"
    assert stuecke[0]["att"] == "Vertrag.pdf"
    assert all("att" not in c for c in stuecke[1:])


# --------------------------------------------------------------------------
# OneDrive: ein Satz je gespiegelter Datei – Name und Pfad, kein Inhalt
# --------------------------------------------------------------------------
def _spiegel(tmp_path, *rel):
    for r in rel:
        p = tmp_path / "Dateien" / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 10)
    return tmp_path


def test_load_onedrive_findet_jede_datei(tmp_path):
    _spiegel(tmp_path, "Angebot.pdf", "Kunden/Vertrag.docx")
    r = {x["rel"]: x for x in corpus.load_onedrive(tmp_path)}
    assert set(r) == {"Dateien/Angebot.pdf", "Dateien/Kunden/Vertrag.docx"}
    e = r["Dateien/Kunden/Vertrag.docx"]
    assert e["src"] == "datei" and e["root"] == "onedrive"
    assert e["title"] == "Vertrag.docx"
    assert e["ctx"] == "Dateien/Kunden"          # Ordner als Suchkriterium
    assert e["att"] == "Vertrag.docx"            # dieselbe Spalte wie Mailanhänge


def test_load_onedrive_faellt_nicht_aus_dem_index(tmp_path):
    """Der Satz muss Text tragen, sonst entsteht beim Chunking gar kein Eintrag
    und die Datei wäre trotz Index unauffindbar."""
    _spiegel(tmp_path, "Kunden/Vertrag.docx")
    recs = corpus.load_onedrive(tmp_path)
    chunks = corpus.chunk_records(recs)
    assert len(chunks) == len(recs) == 1
    assert "Kunden" in chunks[0]["text"], "der Ordner muss mitsuchbar sein"


def test_load_onedrive_uebernimmt_den_grabstein(tmp_path):
    _spiegel(tmp_path, "weg.pdf", "da.pdf")
    (tmp_path / "verschwunden.tsv").write_text(
        "Dateien/weg.pdf\t2026-01-01\n", encoding="utf-8")
    r = {x["rel"]: x for x in corpus.load_onedrive(tmp_path)}
    assert r["Dateien/weg.pdf"]["gone"] == "2026-01-01"
    assert "gone" not in r["Dateien/da.pdf"]


def test_load_onedrive_ignoriert_teildateien(tmp_path):
    """Eine abgebrochene Übertragung gehört nicht in den Index."""
    _spiegel(tmp_path, "fertig.pdf")
    (tmp_path / "Dateien" / "halb.pdf.teil").write_bytes(b"x")
    assert [x["title"] for x in corpus.load_onedrive(tmp_path)] == ["fertig.pdf"]


def test_load_onedrive_ohne_ordner(tmp_path):
    assert corpus.load_onedrive(tmp_path / "gibtsnicht") == []
    assert corpus.load_onedrive(tmp_path) == []       # da, aber ohne Dateien/


def test_load_records_nimmt_onedrive_mit(tmp_path):
    _spiegel(tmp_path / "od", "a.pdf")
    assert corpus.load_records(None, None, tmp_path / "od")[0]["src"] == "datei"
    assert corpus.load_records(None, None, None) == []


# --------------------------------------------------------------------------
# Der Prozess-Pool beim Einlesen
#
# Aus der Praxis: gebündelt startete ein Arbeitsprozess die App-Datei erneut,
# lief in deren Argumentparser und starb – der ganze Index-Lauf endete in
# BrokenProcessPool, obwohl an den Dateien nichts falsch war. Behoben ist das
# an der Wurzel (multiprocessing.freeze_support in app.py); hier steht das
# Sicherheitsnetz: kommt der Pool nicht zustande, wird seriell weitergemacht
# statt aufgegeben.
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def pool_fehler_zuruecksetzen():
    """POOL_FEHLER ist Modulzustand – sonst färbte ein Test auf den nächsten ab."""
    corpus.POOL_FEHLER = None
    yield
    corpus.POOL_FEHLER = None


def _viele(tmp_path, anzahl=None):
    anzahl = anzahl if anzahl is not None else corpus._PAR_THRESHOLD + 5
    return _spiegel(tmp_path, *[f"n{i:04d}.txt" for i in range(anzahl)])


def test_pmap_unter_der_schwelle_ohne_pool(tmp_path, monkeypatch):
    """Bei wenigen Dateien lohnt das Starten von Prozessen nicht."""
    def kein_pool(*a, **k):
        raise AssertionError("Pool trotz weniger Dateien geöffnet")
    monkeypatch.setattr(corpus, "ProcessPoolExecutor", kein_pool)
    _spiegel(tmp_path, "a.pdf", "b.pdf")
    assert len(corpus.load_onedrive(tmp_path)) == 2


def test_pmap_faellt_auf_seriell_zurueck(tmp_path, monkeypatch):
    """Ein kaputter Pool darf keinen Index kosten – nur Geschwindigkeit."""
    from concurrent.futures import BrokenExecutor

    def kaputt(*a, **k):
        raise BrokenExecutor("Arbeitsprozess abrupt beendet")
    monkeypatch.setattr(corpus, "ProcessPoolExecutor", kaputt)

    recs = corpus.load_onedrive(_viele(tmp_path))
    assert len(recs) == corpus._PAR_THRESHOLD + 5, "Datensätze fehlen"
    assert "BrokenExecutor" in corpus.POOL_FEHLER


def test_pmap_faellt_auch_zurueck_wenn_kein_prozess_startet(tmp_path, monkeypatch):
    """Keine Handles mehr, gesperrt durch eine Sicherheitssoftware, kein
    /dev/shm – der Pool geht dann gar nicht erst auf."""
    def geht_nicht(*a, **k):
        raise OSError(24, "Too many open files")
    monkeypatch.setattr(corpus, "ProcessPoolExecutor", geht_nicht)
    assert len(corpus.load_onedrive(_viele(tmp_path))) == corpus._PAR_THRESHOLD + 5
    assert "OSError" in corpus.POOL_FEHLER


def test_pmap_versucht_es_nach_einem_fehlschlag_nicht_wieder(tmp_path, monkeypatch):
    """load_records ruft _pmap bis zu viermal. Ist der Pool einmal als kaputt
    erkannt, wäre jeder weitere Versuch nur Wartezeit für dieselbe Antwort."""
    versuche = []

    def kaputt(*a, **k):
        versuche.append(1)
        raise OSError("nein")
    monkeypatch.setattr(corpus, "ProcessPoolExecutor", kaputt)

    _viele(tmp_path)
    corpus.load_onedrive(tmp_path)
    corpus.load_onedrive(tmp_path)
    assert len(versuche) == 1


def test_pmap_ohne_stoerung_liefert_dasselbe(tmp_path):
    """Der Pfad mit Pool und der ohne müssen dasselbe ergeben – sonst hinge
    der Inhalt des Index an der Zahl der Dateien."""
    _viele(tmp_path)
    mit_pool = corpus.load_onedrive(tmp_path)
    assert corpus.POOL_FEHLER is None, "der normale Weg darf nicht zurückfallen"
    corpus.POOL_FEHLER = "erzwungen"          # ab hier seriell
    assert corpus.load_onedrive(tmp_path) == mit_pool
