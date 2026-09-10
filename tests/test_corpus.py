"""Tests for corpus.py – parsing the exports and chunking (stdlib only)."""

import textwrap

import pytest

import corpus


# --------------------------------------------------------------------------
# HTML / text clean-up
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
    # Every chunk is a substring of the original; start and end are covered
    for c in chunks:
        assert c in words
    assert words.startswith(chunks[0])
    assert words.endswith(chunks[-1])
    # Overlap: the start of each chunk still lies within its predecessor
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert b[:15] in a
    # No chunk (noticeably) above the target size
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
# Teams HTML
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
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")  # is ignored

    recs = corpus.load_teams(str(tmp_path))
    assert len(recs) == 2
    r = recs[0]
    assert r["uid"] == "teams:1on1/alice__abc123.html:0"
    assert r["src"] == "teams"
    # The storage folder: search filters by it, "channels" matches all channels.
    assert r["ctx"] == "1on1"
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
# Calendar (.ics) and contacts (.vcf)
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
    # The folder path, so search can filter by it just like by E-Mail/…
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
    assert r["ctx"] == "kontakte/Team"
    assert "Firma GmbH · Entwicklung" in r["text"]
    assert "Erste Zeileweiter gefaltet" in r["text"]  # RFC line folding resolved
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


def test_ics_when_zeitzonen():
    from datetime import datetime
    utc = corpus.UTC
    # Windows time zone name from Exchange invitations: 15:00 London = 14:00 UTC
    ts, _ = corpus._ics_when("20250610T150000", False, "GMT Standard Time")
    assert datetime.fromtimestamp(ts, utc).hour == 14
    ts, _ = corpus._ics_when("20250610T150000", False, "Pacific Standard Time")
    assert datetime.fromtimestamp(ts, utc).hour == 22
    # IANA names directly, "Z" beats TZID, unknown zones stay local time
    ts, _ = corpus._ics_when("20250610T150000", False, "America/New_York")
    assert datetime.fromtimestamp(ts, utc).hour == 19
    ts, _ = corpus._ics_when("20250610T150000Z", False, "Pacific Standard Time")
    assert datetime.fromtimestamp(ts, utc).hour == 15
    naiv = corpus._ics_when("20250610T150000", False)[0]
    assert corpus._ics_when("20250610T150000", False, "Quatsch/Zone")[0] == naiv
    assert corpus._zone("") is None


def test_unfold_unescape_prop_pval_demail():
    assert corpus._unfold("A:1\r\n b\nB:2\n\tc") == ["A:1b", "B:2c"]
    assert corpus._unescape(r"a\,b\;c\nd\\e") == "a,b;c\nd\\e"
    name, params, value = corpus._prop(
        'ORGANIZER;CN="Alice; Ex":mailto:alice@example.com')
    assert name == "ORGANIZER"
    assert corpus._pval(params, "CN") == "Alice; Ex"  # quotes protect the ;
    assert value == "mailto:alice@example.com"
    assert corpus._prop("zeile ohne doppelpunkt") == (None, None, None)
    assert corpus._pval(";CN=Bob", "CN") == "Bob"
    assert corpus._pval("", "CN") == ""
    assert corpus._demail("MAILTO:Alice@Example.com") == "Alice@Example.com"
    assert corpus._demail(None) == ""


def test_calendar_file_folgt_der_tzid(tmp_path):
    """An event with a Windows TZID must carry the same time in the index as
    in the calendar view – not be read as local time."""
    from datetime import datetime
    d = tmp_path / "kalender" / "Arbeit"
    d.mkdir(parents=True)
    (d / "termin.ics").write_text("\r\n".join([
        "BEGIN:VCALENDAR", "BEGIN:VEVENT",
        "SUMMARY:Abstimmung",
        "DTSTART;TZID=GMT Standard Time:20250610T150000",
        "END:VEVENT", "END:VCALENDAR",
    ]), encoding="utf-8")
    recs = corpus.load_calendar(str(tmp_path))
    assert datetime.fromtimestamp(recs[0]["ts"], corpus.UTC).hour == 14


# --------------------------------------------------------------------------
# Threads: which mails belong together
#
# Everything needed is in the .eml files – no re-export required. Measured
# on the real corpus (sample of 400 out of about 45,000): Thread-Index 89 %,
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
    """Exchange appends 5 bytes per reply – only the first 22 count."""
    import base64
    kopf = bytes(range(22))
    erste = corpus.thread_key(_msg(Thread_Index=base64.b64encode(kopf).decode()))
    antwort = corpus.thread_key(
        _msg(Thread_Index=base64.b64encode(kopf + b"\x01\x02\x03\x04\x05").decode()))
    assert erste == antwort


def test_references_nimmt_den_anfang_des_gespraechs():
    """Not the last message but the first – otherwise a thread would fall
    apart into as many conversations as it has replies."""
    key = corpus.thread_key(_msg(References="<start@x> <mitte@x> <ende@x>"))
    assert key == "mid:start@x"


def test_in_reply_to_als_naechstes():
    assert corpus.thread_key(_msg(In_Reply_To="<vorher@x>")) == "mid:vorher@x"


def test_ohne_alles_ein_gespraech_fuer_sich():
    """A thread of one message is correct, just boring – better than no
    assignment at all."""
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
# Disappeared mails: the file stays, the marker is added
# --------------------------------------------------------------------------
def test_verschwundene_werden_markiert(tmp_path):
    post = tmp_path / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    for name in ("weg.eml", "da.eml"):
        (post / name).write_bytes(
            b"From: a@b.c\nTo: d@e.f\nSubject: X\nDate: Sun, 1 Jun 2025 10:00:00 +0000\n\nText\n")
    import state_db
    state_db.StateDb(tmp_path).verschwunden_ergaenzen(
        ["E-Mail/Posteingang/weg.eml"], "2026-03-12T09:00:00")

    recs = {r["rel"]: r for r in corpus.load_outlook(str(tmp_path))}
    assert recs["E-Mail/Posteingang/weg.eml"]["gone"] == "2026-03-12T09:00:00"
    assert "gone" not in recs["E-Mail/Posteingang/da.eml"]
    # The file is still there – that is the difference between a copy
    # and an archive.
    assert (post / "weg.eml").exists()


def test_ohne_verschwundene_datei_ist_nichts_markiert(tmp_path):
    post = tmp_path / "E-Mail"
    post.mkdir(parents=True)
    (post / "a.eml").write_bytes(b"From: a@b.c\nSubject: X\n\nText\n")
    assert all("gone" not in r for r in corpus.load_outlook(str(tmp_path)))


# --------------------------------------------------------------------------
# Attachments: the contract sat in the archive but no word of it was findable
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
    """Signature logos are named image001.png and would flood the search."""
    assert corpus.anhaenge(_mit_anhang("echt.pdf", inline=("image001.png",))) == ["echt.pdf"]


def test_anhang_ohne_namen_wird_uebergangen():
    assert corpus.anhaenge(_mit_anhang()) == []


def test_doppelte_namen_nur_einmal():
    assert corpus.anhaenge(_mit_anhang("gleich.pdf", "gleich.pdf")) == ["gleich.pdf"]


@pytest.mark.parametrize("roh,erwartet", [
    ("../../.ssh/id_rsa", "id_rsa"),          # path stripped first, then the chars
    ("C:\\Temp\\x.pdf", "x.pdf"),
    (".profile", "profile"),                  # no hidden result
    ("normal.pdf", "normal.pdf"),
    ("", "anhang"),
    ("a" * 300, "a" * 150),
])
def test_dateiname_wird_entschaerft(roh, erwartet):
    assert corpus.sicherer_dateiname(roh) == erwartet


def test_anhangnamen_nur_am_ersten_stueck():
    """Otherwise full-text search would count them once per chunk of the
    mail – a long mail would rank higher for that reason alone."""
    lang = {"uid": "outlook:a.eml:0", "text": "wort " * 2000, "att": "Vertrag.pdf",
            "src": "outlook", "root": "outlook", "rel": "a.eml"}
    stuecke = corpus.chunk_records([lang], size=500, overlap=50)
    assert len(stuecke) > 1, "Testtext war zu kurz"
    assert stuecke[0]["att"] == "Vertrag.pdf"
    assert all("att" not in c for c in stuecke[1:])


# --------------------------------------------------------------------------
# OneDrive: one record per mirrored file – name and path, no content
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
    assert e["ctx"] == "Dateien/Kunden"          # folder as a search criterion
    assert e["att"] == "Vertrag.docx"            # same column as mail attachments


def test_load_onedrive_faellt_nicht_aus_dem_index(tmp_path):
    """The record must carry text, otherwise chunking produces no entry at
    all and the file would be unfindable despite the index."""
    _spiegel(tmp_path, "Kunden/Vertrag.docx")
    recs = corpus.load_onedrive(tmp_path)
    chunks = corpus.chunk_records(recs)
    assert len(chunks) == len(recs) == 1
    assert "Kunden" in chunks[0]["text"], "der Ordner muss mitsuchbar sein"


def test_load_onedrive_uebernimmt_den_grabstein(tmp_path):
    _spiegel(tmp_path, "weg.pdf", "da.pdf")
    import state_db
    state_db.StateDb(tmp_path).verschwunden_ergaenzen(
        ["Dateien/weg.pdf"], "2026-01-01")
    r = {x["rel"]: x for x in corpus.load_onedrive(tmp_path)}
    assert r["Dateien/weg.pdf"]["gone"] == "2026-01-01"
    assert "gone" not in r["Dateien/da.pdf"]


def test_load_onedrive_ignoriert_teildateien(tmp_path):
    """An aborted transfer does not belong in the index."""
    _spiegel(tmp_path, "fertig.pdf")
    (tmp_path / "Dateien" / "halb.pdf.teil").write_bytes(b"x")
    assert [x["title"] for x in corpus.load_onedrive(tmp_path)] == ["fertig.pdf"]


def test_load_onedrive_ohne_ordner(tmp_path):
    assert corpus.load_onedrive(tmp_path / "gibtsnicht") == []
    assert corpus.load_onedrive(tmp_path) == []       # exists, but no Dateien/


def test_load_records_nimmt_onedrive_mit(tmp_path):
    _spiegel(tmp_path / "od", "a.pdf")
    assert corpus.load_records(None, None, tmp_path / "od")[0]["src"] == "datei"
    assert corpus.load_records(None, None, None) == []


# --------------------------------------------------------------------------
# The process pool during ingestion
#
# From the field: in the bundled app a worker process relaunched the app
# binary, ran into its argument parser and died – the whole index run ended
# in BrokenProcessPool although nothing was wrong with the files. The root
# fix lives in app.py (multiprocessing.freeze_support); here is the safety
# net: if the pool cannot be brought up, work continues serially instead
# of giving up.
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def pool_fehler_zuruecksetzen():
    """POOL_FEHLER is module state – without this one test would stain the next."""
    corpus.POOL_FEHLER = None
    yield
    corpus.POOL_FEHLER = None


def _viele(tmp_path, anzahl=None):
    anzahl = anzahl if anzahl is not None else corpus._PAR_THRESHOLD + 5
    return _spiegel(tmp_path, *[f"n{i:04d}.txt" for i in range(anzahl)])


def test_pmap_unter_der_schwelle_ohne_pool(tmp_path, monkeypatch):
    """With only a few files, starting processes is not worth it."""
    def kein_pool(*a, **k):
        raise AssertionError("Pool trotz weniger Dateien geöffnet")
    monkeypatch.setattr(corpus, "ProcessPoolExecutor", kein_pool)
    _spiegel(tmp_path, "a.pdf", "b.pdf")
    assert len(corpus.load_onedrive(tmp_path)) == 2


def test_pmap_faellt_auf_seriell_zurueck(tmp_path, monkeypatch):
    """A broken pool must not cost an index – only speed."""
    from concurrent.futures import BrokenExecutor

    def kaputt(*a, **k):
        raise BrokenExecutor("Arbeitsprozess abrupt beendet")
    monkeypatch.setattr(corpus, "ProcessPoolExecutor", kaputt)

    recs = corpus.load_onedrive(_viele(tmp_path))
    assert len(recs) == corpus._PAR_THRESHOLD + 5, "Datensätze fehlen"
    assert "BrokenExecutor" in corpus.POOL_FEHLER


def test_pmap_faellt_auch_zurueck_wenn_kein_prozess_startet(tmp_path, monkeypatch):
    """Out of handles, blocked by security software, no /dev/shm – then
    the pool does not even open."""
    def geht_nicht(*a, **k):
        raise OSError(24, "Too many open files")
    monkeypatch.setattr(corpus, "ProcessPoolExecutor", geht_nicht)
    assert len(corpus.load_onedrive(_viele(tmp_path))) == corpus._PAR_THRESHOLD + 5
    assert "OSError" in corpus.POOL_FEHLER


def test_pmap_versucht_es_nach_einem_fehlschlag_nicht_wieder(tmp_path, monkeypatch):
    """load_records calls _pmap up to four times. Once the pool is known to
    be broken, every further attempt is just waiting for the same answer."""
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
    """The path with a pool and the one without must yield the same result –
    otherwise the index content would depend on the number of files."""
    _viele(tmp_path)
    mit_pool = corpus.load_onedrive(tmp_path)
    assert corpus.POOL_FEHLER is None, "der normale Weg darf nicht zurückfallen"
    corpus.POOL_FEHLER = "erzwungen"          # serial from here on
    assert corpus.load_onedrive(tmp_path) == mit_pool


def test_endungen_aus_anhangnamen():
    """The column the file-type filter asks for: deduplicated and lowercase."""
    assert corpus.endungen("Vertrag.pdf Anlage.XLSX Nachtrag.pdf") == "pdf xlsx"
    assert corpus.endungen("Bild.jpeg") == "jpeg"
    assert corpus.endungen("") == ""
    assert corpus.endungen(None) == ""
    # What is not an extension does not become one: otherwise "2024-final"
    # would be offered as a file type.
    assert corpus.endungen("Bericht.2024-final") == ""
    assert corpus.endungen("Archiv.ohneinehrsehrlangeendung") == ""
    assert corpus.endungen("ohnepunkt") == ""


# --------------------------------------------------------------------------
# SharePoint mirror in the corpus: one tree per library, prefixed tombstones
# --------------------------------------------------------------------------
def test_load_sharepoint_liest_bibliotheken_mit_grabsteinen(tmp_path):
    lib = tmp_path / "Team X" / "Projects"
    (lib / "Dateien" / "N").mkdir(parents=True)
    (lib / "Dateien" / "N" / "plan.pdf").write_bytes(b"x")
    (lib / "Dateien" / "weg.pdf").write_bytes(b"y")
    import state_db
    state_db.StateDb(lib).verschwunden_ergaenzen(
        ["Dateien/weg.pdf"], "2026-03-01T00:00:00+00:00")

    recs = corpus.load_sharepoint(tmp_path)
    assert {r["rel"] for r in recs} == {"Team X/Projects/Dateien/N/plan.pdf",
                                        "Team X/Projects/Dateien/weg.pdf"}
    assert all(r["root"] == "sharepoint" for r in recs)
    assert all(r["uid"].startswith("sharepoint:") for r in recs)
    weg = next(r for r in recs if r["rel"].endswith("weg.pdf"))
    assert weg.get("gone", "").startswith("2026-03-01")
    da = next(r for r in recs if r["rel"].endswith("plan.pdf"))
    assert "gone" not in da


def test_load_records_nimmt_den_sharepoint_ordner_mit(tmp_path):
    sp = tmp_path / "sharepoint_export" / "S" / "L" / "Dateien"
    sp.mkdir(parents=True)
    (sp / "a.txt").write_bytes(b"x")
    recs = corpus.load_records(None, None,
                               sharepoint_dir=tmp_path / "sharepoint_export")
    assert [r["root"] for r in recs] == ["sharepoint"]


def test_load_pages_liest_titel_text_und_grabstein(tmp_path):
    (tmp_path / "Team X").mkdir()
    (tmp_path / "Team X" / "Home.html").write_text(
        "<!doctype html><html><head><title>Start &amp; Ziel</title></head>"
        "<body><h1>Start</h1><p>Inhalt der Seite</p></body></html>",
        encoding="utf-8")
    import state_db
    state_db.StateDb(tmp_path).verschwunden_ergaenzen(
        ["Team X/Home.html"], "2026-04-01T00:00:00+00:00")
    recs = corpus.load_pages(tmp_path)
    assert len(recs) == 1
    r = recs[0]
    assert r["src"] == "pages" and r["root"] == "pages"
    assert r["title"] == "Start & Ziel"
    assert "Inhalt der Seite" in r["text"]
    assert r["gone"].startswith("2026-04-01")


def test_teams_dateien_gehoeren_zu_ihrer_art(tmp_path):
    """Files next to a conversation – referenced attachments and mirrored
    channel folders – are file records with the conversation kind as
    context, and never parsed as conversations."""
    import state_db
    (tmp_path / "1on1").mkdir()
    (tmp_path / "1on1" / "alice__abc123.html").write_text(
        '<div class="msg"><div class="head"><span class="name">Alice</span>'
        '<span class="time">2025-06-01 09:30</span></div><div class="body">hi</div></div>',
        encoding="utf-8")
    anh = tmp_path / "1on1" / "Anhaenge" / "alice__abc123"
    anh.mkdir(parents=True)
    (anh / "Angebot__1a2b3c4d.pdf").write_bytes(b"PDF")
    (anh / "seite__ffffffff.html").write_text("<p>attached page</p>", encoding="utf-8")
    kanal = tmp_path / "channels" / "Team Rakete" / "Dateien" / "Allgemein"
    kanal.mkdir(parents=True)
    (kanal / "Plan.xlsx").write_bytes(b"X")
    (kanal / "halb.pdf.teil").write_bytes(b"X")
    state_db.StateDb(tmp_path / "channels" / "Team Rakete").verschwunden_ergaenzen(
        ["Dateien/Allgemein/weg.docx"], "2026-01-02T00:00:00+00:00")
    (kanal / "weg.docx").write_bytes(b"X")

    gespraeche = corpus.load_teams(tmp_path)
    assert [r["rel"] for r in gespraeche] == ["1on1/alice__abc123.html"]
    dateien = sorted(corpus.load_teams_files(tmp_path), key=lambda r: r["rel"])
    assert [r["rel"] for r in dateien] == [
        "1on1/Anhaenge/alice__abc123/Angebot__1a2b3c4d.pdf",
        "1on1/Anhaenge/alice__abc123/seite__ffffffff.html",
        "channels/Team Rakete/Dateien/Allgemein/Plan.xlsx",
        "channels/Team Rakete/Dateien/Allgemein/weg.docx"]
    pdf = dateien[0]
    assert pdf["src"] == "datei" and pdf["root"] == "teams"
    assert pdf["uid"].startswith("teamsdatei:") and pdf["ctx"] == "1on1/Anhaenge/alice__abc123"
    assert pdf["att"] == "Angebot__1a2b3c4d.pdf"
    assert dateien[3]["gone"] == "2026-01-02T00:00:00+00:00" and "gone" not in dateien[2]
    assert set(corpus.manifest("teams_files", tmp_path)) == {r["rel"] for r in dateien}
    assert set(corpus.manifest("teams", tmp_path)) == {"1on1/alice__abc123.html"}
