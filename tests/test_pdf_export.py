"""pdf_export.py – a mail, an appointment, a contact as a PDF to hand on:
what goes into it, that it is a PDF, and the route that makes one from an
item of the synthetic archive."""

import http.client
import time
from datetime import UTC, datetime
from urllib.parse import quote, unquote

import pdf_export
from hilfen import call
from testdata import people, sources
from tests.test_evidence_api import _uid, archive, built  # noqa: F401 – fixtures


def say(key):
    return key


def test_an_html_mail_is_read_as_text_with_its_lines():
    text = pdf_export.html_text(
        "<html><head><style>p{color:red}</style><title>t</title></head><body><p>Hello Alice,</p>"
        "<div>line one<br>line two</div><blockquote>From: Dana<br>earlier &amp; quoted</blockquote>"
        "<script>bad()</script></body></html>")
    # Blocks are paragraphs, a break a line; the quoted history stays, scripts and styles do not.
    assert text == "Hello Alice,\n\nline one\nline two\n\nFrom: Dana\nearlier & quoted"
    assert pdf_export.html_text("<p>unclosed <b>markup") == "unclosed markup"
    # A break is one line end however it is written – an address block stays a block.
    assert pdf_export.html_text("Dana<br/>Beispielweg 1<br />Hamburg<br>Germany") == "Dana\nBeispielweg 1\nHamburg\nGermany"
    # What stands in <pre> keeps its lines and its indent; the cells of a table row stand apart.
    assert pdf_export.html_text("<p>before</p><pre>x = 1\n  y = 2</pre><p>after</p>") == \
        "before\n\nx = 1\n" + chr(0xA0) * 2 + "y = 2\n\nafter"
    assert pdf_export.html_text("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>") == "a b\n\nc"
    # … the indent too, the first line's and a tab's, as no-break spaces a line's strip leaves alone.
    nb = chr(0xA0)
    assert pdf_export.html_text("<pre>    first\n  second\n\tthird</pre>") == f"{nb * 4}first\n{nb * 2}second\n{nb * 4}third"
    # A head without its end tag – it is optional – does not swallow the mail.
    assert pdf_export.html_text('<html><head><meta charset="utf-8"><title>t</title><body><p>Hi there</p></body></html>') == "Hi there"
    # A long run of empty inline elements is read in a moment, not in the square of its length.
    began = time.monotonic()
    assert pdf_export.html_text("<p>a</p>" + "<b> </b>" * 200000 + "<p>b</p>") == "a\n\nb"
    assert time.monotonic() - began < 5


def test_a_mail_s_whole_text_comes_from_its_file(tmp_path):
    plain = tmp_path / "plain.eml"
    plain.write_bytes(b"From: dana@example.com\r\nSubject: x\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                      b"Hello\r\n\r\nline one\r\nline two\r\n\r\n\r\n\r\n> quoted\r\n")
    assert pdf_export.mail_text(plain) == "Hello\n\nline one\nline two\n\n> quoted"
    html = tmp_path / "html.eml"
    html.write_bytes(b"From: dana@example.com\r\nSubject: x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
                     b"<p>Hello</p><p>second</p>")
    assert pdf_export.mail_text(html) == "Hello\n\nsecond"
    assert pdf_export.mail_text(tmp_path / "missing.eml") == ""


def test_an_appointment_s_text_and_whether_it_repeats_come_from_its_file(tmp_path):
    single = tmp_path / "single.ics"
    single.write_text("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Review\r\nDTSTART:20260602T090000Z\r\n"
                      "DESCRIPTION:Agenda:\\n1. Budget\\, as agreed\r\n  2. Dates\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
                      encoding="utf-8")
    # Unfolded and unescaped, with its lines.
    assert pdf_export.event_text(single) == "Agenda:\n1. Budget, as agreed 2. Dates"
    assert pdf_export.event_repeats(single) is False
    # A series: the master's words and rule – an exception's are not the series'.
    series = tmp_path / "series.ics"
    series.write_text("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Weekly\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n"
                      "BEGIN:VEVENT\r\nRECURRENCE-ID:20260609T090000Z\r\nDESCRIPTION:Moved this once\r\nEND:VEVENT\r\n"
                      "END:VCALENDAR\r\n", encoding="utf-8")
    assert pdf_export.event_text(series) == "" and pdf_export.event_repeats(series) is True
    assert pdf_export.event_text(tmp_path / "gone.ics") == "" and pdf_export.event_repeats(tmp_path / "gone.ics") is False
    # A series says that it is one; a single appointment has no such row.
    rows = dict(pdf_export.event_rows({"start": "2026-06-02 09:00", "repeats": True}, say))
    assert rows["pdf.series"] == "pdf.series.note"
    assert dict(pdf_export.event_rows({"start": "2026-06-02 09:00"}, say))["pdf.series"] == ""


def test_the_closing_note_promises_only_what_is_there(monkeypatch):
    """A checksum above, an original kept – said only where they are."""
    said = []
    real = pdf_export.Paragraph

    def paragraph(text, style):
        said.append(text)
        return real(text, style)
    monkeypatch.setattr(pdf_export, "Paragraph", paragraph)
    pdf_export.render("t", "Mail", [], "", {"file": "a.eml", "sha256": "ab" * 32, "folder": "Inbox", "mail": True}, say)
    assert said[-1] == "pdf.origin.note pdf.origin.note.kept pdf.origin.note.mail"
    pdf_export.render("t", "Contact", [], "", {"file": "", "sha256": "", "folder": "kontakte"}, say)
    assert said[-1] == "pdf.origin.note pdf.origin.note.gone"


def test_the_facts_become_rows_and_an_empty_one_is_left_out_on_the_page():
    facts = {"from": {"name": "Dana Dienstleister", "mail": "dana@example.com"},
             "to": [{"name": "Alice Beispiel", "mail": "alice@nordwind.example"}, {"name": "", "mail": "bob@nordwind.example"}],
             "cc": [], "bcc": [], "date": "2026-05-30 06:05", "folder": "Inbox",
             "attachments": [{"name": "offer.pdf", "size": 1}, {"name": "terms.docx", "size": 2}]}
    rows = dict(pdf_export.mail_rows(facts, say))
    assert rows["pdf.from"] == "Dana Dienstleister <dana@example.com>"
    assert rows["pdf.to"] == "Alice Beispiel <alice@nordwind.example>\nbob@nordwind.example"
    assert rows["pdf.cc"] == "" and rows["pdf.attachments"] == "offer.pdf\nterms.docx"
    event = dict(pdf_export.event_rows({"start": "2026-06-02 00:00", "allday": True, "end": "",
                                        "organiser": {"name": "Carla Chef", "mail": ""}}, say))
    assert event["pdf.start"] == "2026-06-02 (pdf.allday)" and event["pdf.organiser"] == "Carla Chef"
    card = dict(pdf_export.contact_rows({"title": "Buyer", "emails": ["erik@example.com"],
                                         "phones": [{"type": "mobile", "number": "+49 170 0000012"},
                                                    {"type": "", "number": "+49 40 000000 12"}]}, say))
    assert card["pdf.phones"] == "+49 170 0000012 (pdf.phone.mobile)\n+49 40 000000 12"


def test_a_pdf_is_made_with_any_character_and_any_length():
    origin = {"file": "2026-05-30_0605__Offer.eml", "sha256": "ab" * 32, "folder": "Inbox"}
    one = pdf_export.render("Offer 4711 – Ünïcödé \U0001f600 日本", "Mail", [("From", "Dana"), ("Cc", "")],
                            "Hello Alice,\n\nline & <b>markup</b>\n\x07", origin, say,
                            now=datetime(2026, 6, 2, 9, 0, tzinfo=UTC))
    assert one.startswith(b"%PDF-") and one.rstrip().endswith(b"%%EOF") and one.count(b"/Type /Page\n") == 1
    # Both fonts are embedded: the PDF reads the same on a machine without them.
    assert one.count(b"/FontFile2") >= 2
    long = pdf_export.render("Long", "Mail", [], "\n\n".join(f"Paragraph {n} " + "word " * 80 for n in range(60)),
                             origin, say)
    assert long.count(b"/Type /Page\n") > 3
    # A mail to two hundred people: a line of a value is a row of its own, so the table breaks across pages.
    many = "\n".join(f"Person {n} <person{n}@example.com>" for n in range(200))
    wide = pdf_export.render("To everyone", "Mail", [("To", many), ("Attendees", many)], "x", origin, say)
    assert wide.count(b"/Type /Page\n") > 5
    # A log pasted as one paragraph of thousands of lines, a mail written as one line of a hundred
    # thousand words: set in pieces, in seconds – one paragraph of that size took minutes.
    began = time.monotonic()
    pdf_export.render("Log", "Mail", [], "\n".join(f"line {n} " + "x" * 25 for n in range(12000)), origin, say)
    pdf_export.render("One line", "Mail", [], "word " * 80000, origin, say)
    pdf_export.render("No space", "Mail", [], "x" * 300000, origin, say)
    assert time.monotonic() - began < 30
    assert pdf_export._pieces("One. Two three. " * 3, 20) == ["One. Two three.", "One. Two three.", "One. Two three. "]
    # One line of a fact longer than a page – a place holding a pasted dial-in block, a file's
    # name – is rows too: a row cannot break across a page.
    for value in ("word " * 4000, "x" * 20000):
        assert pdf_export.render("t", "Appointment", [("Location", value)], "", origin, say).startswith(b"%PDF-")
    assert pdf_export.render("t", "Mail", [], "", {**origin, "file": "f" * 30000}, say).startswith(b"%PDF-")
    # A line's indent reaches the page: a Paragraph takes plain spaces for one.
    assert pdf_export._markup("def f():\n    return 1\n\tx  y") == \
        "def f():<br/>" + chr(0xA0) * 4 + "return 1<br/>" + chr(0xA0) * 4 + "x  y"
    # No title, no rows, no text: still a document that says where it comes from.
    assert pdf_export.render("", "Contact", [], "", origin, say).startswith(b"%PDF-")
    # A character the font lacks is a box, a control character nothing.
    assert pdf_export._clean("a" + chr(0x65e5) + "b\x07c\td") == "a" + chr(0x25a1) + "bc\td"


def _pdf(port, uid):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    con.request("GET", f"/api/v1/documents/pdf?uid={quote(uid)}")
    resp = con.getresponse()
    body = resp.read()
    con.close()
    return resp, body


def test_a_mail_of_the_archive_is_handed_out_as_a_pdf(archive, monkeypatch):  # noqa: F811
    _a, port, _home, _built = archive
    mail = next(m for m in sources.MAILS if m["key"] == "site-plan")        # written in HTML
    code, r = call(port, "GET", f"/api/v1/search?q={quote(mail['subject'])}&source=outlook&limit=20")
    uid = next(h["uid"] for h in r["items"] if h["title"] == mail["subject"])
    seen = {}
    real = pdf_export.render

    def render(title, kind, rows, body, origin, say, now=None):
        seen.update(title=title, kind=kind, rows=dict(rows), body=body, origin=origin)
        return real(title, kind, rows, body, origin, say, now)
    monkeypatch.setattr(pdf_export, "render", render)
    resp, body = _pdf(port, uid)
    assert resp.status == 200 and resp.getheader("Content-Type") == "application/pdf"
    assert resp.getheader("Content-Disposition").startswith('attachment; filename="') \
        and resp.getheader("Content-Disposition").endswith('.pdf"')
    assert body.startswith(b"%PDF-")
    # What went onto the page: the subject, the sender, the text read from the HTML – and the original's checksum.
    assert seen["title"] == mail["subject"] and seen["kind"] == "Mail"
    assert mail["frm"][1] in next(v for v in seen["rows"].values() if "@" in v)
    assert seen["body"] and "<" not in seen["body"]
    assert seen["origin"]["file"].endswith(".eml") and len(seen["origin"]["sha256"]) == 64


def test_a_title_beyond_latin_1_names_the_download(archive, monkeypatch):  # noqa: F811
    """A header is sent as Latin-1: an en dash or a Greek name in `filename`
    broke the answer after its status line. RFC 6266: the name in
    `filename*`, an ASCII one beside it."""
    import api
    assert api.disposition("Offer.pdf") == 'attachment; filename="Offer.pdf"'
    named = api.disposition('Angebot \u2013 \u00dcbersicht "final"\r\n/x.pdf')
    assert named.startswith('attachment; filename="Angebot _ _bersicht _final____x.pdf"; filename*=UTF-8\'\'Angebot%20%E2%80%93%20%C3%9Cbersicht')
    named.encode("latin-1")
    assert api.disposition("") == 'attachment; filename="download"'
    # The route names the file after the item's title: give the index row one beyond Latin-1,
    # longer than a name should be, with what a path and a header must not carry.
    _a, port, _home, _built = archive
    mod = _a.search.ensure(_a.cfg)
    original = mod._message_text
    title = "Angebot \u2013 \u0391\u03b8\u03ae\u03bd\u03b1 \u65e5\u672c: a/b \"c\" " + "x" * 200

    def retitled(con, uid):
        row, text = original(con, uid)
        row = dict(row)
        row["title"] = title
        return row, text
    monkeypatch.setattr(mod, "_message_text", retitled)
    code, r = call(port, "GET", "/api/v1/search?q=Offer%204711&source=outlook&limit=1")
    resp, body = _pdf(port, r["items"][0]["uid"])
    assert resp.status == 200 and body.startswith(b"%PDF-")
    header = resp.getheader("Content-Disposition")
    assert header.startswith('attachment; filename="Angebot _ ') and "filename*=UTF-8''Angebot%20%E2%80%93%20%CE%91" in header
    name = unquote(header.split("filename*=UTF-8''")[1])
    assert name.endswith(".pdf") and len(name) <= 84 and not set(name) & set('/\\"')


def test_what_the_index_writes_for_nothing_is_not_the_item_s_word(archive, monkeypatch):  # noqa: F811
    """An appointment with a place and no description has no text – not the
    index's "Ort: …"; a mail without a subject is untitled in the reader's
    language, not "(kein Betreff)"."""
    import corpus
    _a, port, _home, _built = archive
    code, r = call(port, "GET", "/api/v1/search?q=&source=kalender&limit=50")
    seen = []
    real = pdf_export.render
    monkeypatch.setattr(pdf_export, "render",
                        lambda title, kind, rows, body, origin, say, now=None:
                        seen.append((title, dict(rows), body)) or real(title, kind, rows, body, origin, say, now))
    monkeypatch.setattr(pdf_export, "event_text", lambda path: "")
    assert _pdf(port, r["items"][0]["uid"])[0].status == 200
    assert seen[-1][2] == ""
    # … and a subject the index made up is none.
    mod = _a.search.ensure(_a.cfg)
    original = mod._message_text

    def untitled(con, uid):
        row, text = original(con, uid)
        row = dict(row)
        row["title"], row["who"] = corpus.NO_SUBJECT, corpus.NOBODY
        return row, text
    monkeypatch.setattr(mod, "_message_text", untitled)
    code, r = call(port, "GET", "/api/v1/search?q=Offer%204711&source=outlook&limit=1")
    resp, _body = _pdf(port, r["items"][0]["uid"])
    assert resp.status == 200 and seen[-1][0] == ""
    assert corpus.NO_SUBJECT not in resp.getheader("Content-Disposition")


def test_an_appointment_and_a_contact_are_handed_out_too_and_a_file_is_not(archive):  # noqa: F811
    _a, port, _home, _built = archive
    dana = people.EXTERNALS[0]
    code, r = call(port, "GET", f"/api/v1/search?q={quote(dana[0])}&source=kontakte&limit=5")
    assert _pdf(port, r["items"][0]["uid"])[1].startswith(b"%PDF-")
    code, r = call(port, "GET", "/api/v1/search?q=&source=kalender&limit=1")
    resp, body = _pdf(port, r["items"][0]["uid"])
    assert resp.status == 200 and body.startswith(b"%PDF-")
    # A file is itself what one hands on; a chat has no PDF yet.
    uid = _uid(port, "rollout plan", "datei:Dateien/Documents/Ostwind/rollout-plan.md")
    code, r = call(port, "GET", f"/api/v1/documents/pdf?uid={quote(uid)}")
    assert code == 404 and r["error"]["k"] == "srv.pdf.kind"
    code, r = call(port, "GET", "/api/v1/documents/pdf?uid=outlook:nothing:0")
    assert code == 404 and r["error"]["k"] == "srv.detail.none"
