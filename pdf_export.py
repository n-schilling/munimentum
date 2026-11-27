"""
pdf_export.py – one item of the archive as a PDF to hand on.

An .eml opens differently on every machine, or not at all; a PDF opens
everywhere. So a mail, a contact card or an appointment can be saved as
one: its head lines as a table of facts, its text set plainly, and at
the end where it comes from – the original's file name and SHA-256 and
the moment of the export.

The PDF is a rendering, not the original: the file in the archive stays
the record, and every PDF says so. A mail written in HTML is set as its
text – paragraphs and line breaks, no layout, no pictures; its
attachments are listed by name, not included.

reportlab sets the pages; the two DejaVu fonts in `fonts/` are embedded
(the standard PDF fonts know no character beyond Latin-1). A character
neither font has is shown as a box.
"""

import io
import re
from datetime import datetime
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import corpus
import export_util
import version

KINDS = ("outlook", "kalender", "kontakte")     # the index sources a PDF is made for
FONT, BOLD = "MunimentumSans", "MunimentumSans-Bold"
BODY_CAP = 400_000              # characters of a body set: a megabyte of text is a hundred pages
BLOCK_LINES = 40                # lines of one paragraph set as one piece
LINE_CHARS = 3000               # … and characters of one line, before it is cut at a sentence
BLOCK_CHARS = 4000              # … and characters of one piece
ROW_CHARS = 1000                # characters of one row of a table: a row cannot break across a page
_MISSING = "□"             # what stands for a character the font does not have
_INK, _MUTED, _LINE = colors.HexColor("#1b1f24"), colors.HexColor("#5b6570"), colors.HexColor("#dfe3e8")

_fonts = {}


def _font():
    """The embedded fonts, registered once per process; the characters
    the regular one knows."""
    if not _fonts:
        folder = export_util.resource_dir() / "fonts"
        regular = TTFont(FONT, str(folder / "DejaVuSans.ttf"))
        pdfmetrics.registerFont(regular)
        pdfmetrics.registerFont(TTFont(BOLD, str(folder / "DejaVuSans-Bold.ttf")))
        _fonts["known"] = regular.face.charToGlyph
    return _fonts["known"]


def _clean(text):
    """Text the font can set: no control characters, a box for what it
    has no glyph for."""
    known = _font()
    return "".join(ch if ch in "\n\t" or ord(ch) in known else
                   ("" if ord(ch) < 32 or 0xFE00 <= ord(ch) <= 0xFE0F or ch == "‍" else _MISSING)
                   for ch in str(text or ""))


def _indent(match):
    return "\u00a0" * len(match.group(0).replace("\t", "    "))


def _markup(text):
    """Cleaned text as Paragraph markup: escaped, its line breaks kept –
    and each line's indent, as no-break spaces: a Paragraph takes a run
    of plain spaces for one, and a quoted piece of code would stand flush
    left."""
    text = re.sub(r"(?m)^[ \t]+", _indent, _clean(text))
    return escape(text).replace("\t", "    ").replace("\n", "<br/>")


# -- a mail's text with its lines ---------------------------------------------
_BLOCKS = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
           "table", "ul", "ol", "pre", "section", "article", "header", "footer"}
_VOID = {"br", "hr"}            # one line end each, however the tag is written: <br>, <br/>, <br />
# Not <head>: its end tag is optional, and a mail that leaves it out would
# lose every word; what a head holds beside these has no text.
_SKIPPED = {"script", "style", "title"}
_BREAK = "\x00"                 # a block's edge: two of them and more are still one empty line


class _Lines(HTMLParser):
    """An HTML body as text: an empty line where a block begins or ends,
    a line end at a <br>, the lines of a <pre> as they stand, nothing of
    scripts and styles."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.pre = [], 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self.skip += 1
        elif tag in _VOID:
            self.parts.append("\n")
        elif tag in _BLOCKS:
            self.parts.append(_BREAK)
            self.pre += tag == "pre"
        elif tag in ("td", "th"):
            self.parts.append(" ")               # the cells of a row, apart

    def handle_startendtag(self, tag, attrs):
        # <br/> is one tag, not a start and an end: one line end.
        if tag in _VOID:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIPPED:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCKS:
            self.parts.append(_BREAK)
            self.pre = max(0, self.pre - (tag == "pre"))

    def handle_data(self, data):
        if not self.skip:
            # In <pre> the lines stand as written; their indent is kept as
            # no-break spaces, which nothing below takes for a line's edge.
            self.parts.append(re.sub(r"(?m)^[ \t]+", _indent, data) if self.pre
                              else re.sub(r"\s+", " ", data))


def html_text(markup):
    reader = _Lines()
    try:
        reader.feed(markup or "")
        reader.close()
    except Exception:                            # noqa: BLE001 – broken markup: what was read so far
        pass
    # A block's edge ends the line; edges in a row – a paragraph closing,
    # the next opening – leave one empty line, and a <br> inside stays one
    # line end.
    # (The spaces before an edge are left to the line's own strip: a
    # pattern beginning with them scans a long run of spaces from every
    # one of its positions.)
    text = re.sub(r"\x00[\x00 \t]*", "\n\n", "".join(reader.parts))
    return _paragraphs("\n".join(line.strip(" \t") for line in text.split("\n")))


def _paragraphs(text):
    """Line ends alike, no run of more than one empty line, none at the ends."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # (Empty lines go at the ends, not the first line's indent.)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.split("\n"))).strip("\n")


def mail_text(path):
    """The whole text of a mail as it was sent – the quoted history
    included, which the index leaves out: the plain part, else the HTML
    part read as text. "" when the file cannot say."""
    try:
        with open(path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
    except Exception:                            # noqa: BLE001 – unreadable: the caller has the index text
        return ""
    for kind in ("plain", "html"):
        try:
            part = msg.get_body(preferencelist=(kind,))
        except Exception:                        # noqa: BLE001
            part = None
        if part is None:
            continue
        text = corpus.decode_part(part)
        text = html_text(text) if kind == "html" else _paragraphs(text)
        if text:
            return text
    return ""


def event_text(path):
    """An appointment's description with its lines (the master's, for a series)."""
    try:
        lines = corpus._unfold(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    for line in lines:
        name, _params, value = corpus._prop(line)
        if name == "DESCRIPTION":
            return _paragraphs(corpus._unescape(value))
        if name == "END" and value.strip().upper() == "VEVENT":
            break
    return ""


def _pieces(line, most=None):
    """A line no longer than `most` characters at a time, cut after a
    sentence where one ends in reach, else at a space: a mail written as
    one paragraph of a hundred thousand characters is laid out as slowly
    as one of thousands of lines."""
    most = most or LINE_CHARS
    out = []
    while len(line) > most:
        cut = max(line.rfind(". ", most // 2, most) + 1, 0) or line.rfind(" ", most // 2, most)
        cut = cut if cut > 0 else most
        out.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    return out + [line]


def event_repeats(path):
    """Is the appointment a series – does its master carry a rule?"""
    try:
        lines = corpus._unfold(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
    for line in lines:
        name, _params, value = corpus._prop(line)
        if name == "RRULE":
            return True
        if name == "END" and value.strip().upper() == "VEVENT":
            break
    return False


# -- the rows of facts, kind by kind ------------------------------------------
def _person(p):
    name, mail = (p or {}).get("name") or "", (p or {}).get("mail") or ""
    return f"{name} <{mail}>" if name and mail and name != mail else name or mail


def _people(found):
    return "\n".join(filter(None, (_person(p) for p in found or ())))


def mail_rows(facts, say):
    names = [a.get("name") or "" for a in facts.get("attachments") or ()]
    return [(say("pdf.from"), _person(facts.get("from"))),
            (say("pdf.to"), _people(facts.get("to"))),
            (say("pdf.cc"), _people(facts.get("cc"))),
            (say("pdf.bcc"), _people(facts.get("bcc"))),
            (say("pdf.date"), facts.get("date") or ""),
            (say("pdf.folder"), facts.get("folder") or ""),
            (say("pdf.attachments"), "\n".join(n for n in names if n))]


def event_rows(facts, say):
    start = facts.get("start") or ""
    if facts.get("allday"):
        start = f"{start[:10]} ({say('pdf.allday')})"
    return [(say("pdf.start"), start),
            (say("pdf.end"), facts.get("end") or ""),
            (say("pdf.location"), facts.get("location") or ""),
            (say("pdf.organiser"), _person(facts.get("organiser"))),
            (say("pdf.attendees"), _people(facts.get("attendees"))),
            (say("pdf.calendar"), facts.get("calendar") or ""),
            # A series is one item: the PDF shows it as it was set up. Whoever
            # reads it without the app must be told it is no single date.
            (say("pdf.series"), say("pdf.series.note") if facts.get("repeats") else "")]


def contact_rows(card, say):
    kinds = {"mobile": say("pdf.phone.mobile"), "fax": say("pdf.phone.fax"),
             "home": say("pdf.phone.home"), "work": say("pdf.phone.work")}
    phones = [p["number"] + (f" ({kinds[p['type']]})" if p.get("type") in kinds else "")
              for p in card.get("phones") or ()]
    return [(say("pdf.jobtitle"), card.get("title") or ""),
            (say("pdf.company"), card.get("organisation") or ""),
            (say("pdf.department"), card.get("department") or ""),
            (say("pdf.mail"), "\n".join(card.get("emails") or ())),
            (say("pdf.phones"), "\n".join(phones))]


# -- the pages ----------------------------------------------------------------
def render(title, kind, rows, body, origin, say, now=None):
    """The PDF as bytes. `kind` is the word for what this is (a mail, an
    appointment, a contact), `rows` its facts as (label, value) – an empty
    value is no row –, `body` its text, `origin` what the last block says
    of the original: `file` (its name), `sha256` ("" when the file is not
    at hand: the note then says so instead of pointing at a checksum),
    `folder`, and `mail` when it is one (its note then speaks of
    formatting and attachments). `say(key)`
    gives a text in the reader's language."""
    _font()
    now = now or datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    base = dict(fontName=FONT, textColor=_INK, fontSize=10, leading=14)
    styles = {"kind": ParagraphStyle("kind", **{**base, "fontSize": 8.5, "textColor": _MUTED, "leading": 11}),
              "title": ParagraphStyle("title", **{**base, "fontName": BOLD, "fontSize": 15, "leading": 19,
                                                  "spaceAfter": 8}),
              "label": ParagraphStyle("label", **{**base, "textColor": _MUTED, "fontSize": 9}),
              "value": ParagraphStyle("value", **{**base, "fontSize": 9.5, "leading": 13}),
              "body": ParagraphStyle("body", **{**base, "spaceAfter": 7}),
              "run": ParagraphStyle("run", **base),          # a piece of a long paragraph: no gap after it
              "head": ParagraphStyle("head", **{**base, "fontName": BOLD, "fontSize": 9, "textColor": _MUTED}),
              "small": ParagraphStyle("small", **{**base, "fontSize": 8, "leading": 11, "textColor": _MUTED})}

    def table(pairs, style):
        # A line of a value is a row of its own, the label on the first:
        # a table breaks across a page between rows, never inside one – a
        # mail to two hundred people would otherwise fit no page at all.
        # Nor would one line of thousands of characters (a place holding a
        # pasted dial-in block): it is cut into rows as well.
        cells = [[Paragraph(_markup(label if n == 0 else ""), styles["label" if style == "value" else "small"]),
                  Paragraph(_markup(line), styles[style])]
                 for label, value in pairs if value
                 for n, line in enumerate(piece for whole in str(value).split("\n")
                                          for piece in _pieces(whole, ROW_CHARS))]
        if not cells:
            return []
        grid = Table(cells, colWidths=[40 * mm, None], hAlign="LEFT")
        grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
        return [grid]

    story = [Paragraph(_markup(kind.upper()), styles["kind"]),
             Paragraph(_markup(title or say("pdf.untitled")), styles["title"])]
    story += table(rows, "value")
    story.append(Spacer(1, 6 * mm))
    body = _paragraphs(body)
    if len(body) > BODY_CAP:
        body = body[:BODY_CAP] + "\n\n" + say("pdf.cut")
    # One paragraph per paragraph: each can break across a page by itself.
    # A paragraph of very many lines – a log, a table pasted as text – is
    # set in pieces: reportlab lays out one paragraph of thousands of line
    # ends in minutes, the same lines in pieces in a second or two.
    for block in body.split("\n\n") if body else ():
        lines = [piece for line in block.split("\n") for piece in _pieces(line)]
        groups, size = [[]], 0
        for line in lines:
            if groups[-1] and (len(groups[-1]) >= BLOCK_LINES or size + len(line) > BLOCK_CHARS):
                groups.append([])
                size = 0
            groups[-1].append(line)
            size += len(line)
        for n, group in enumerate(groups):
            story.append(Paragraph(_markup("\n".join(group)),
                                   styles["body" if n == len(groups) - 1 else "run"]))
    story += [Spacer(1, 8 * mm), Paragraph(_markup(say("pdf.origin")), styles["head"]), Spacer(1, 1.5 * mm)]
    story += table([(say("pdf.origin.folder"), origin.get("folder") or ""),
                    (say("pdf.origin.file"), origin.get("file") or ""),
                    (say("pdf.origin.sha256"), origin.get("sha256") or ""),
                    (say("pdf.origin.exported"), stamp)], "small")
    # What the note may promise: an original at hand in the archive, and a
    # checksum above only when there is one.
    note = " ".join(filter(None, (
        say("pdf.origin.note"),
        say("pdf.origin.note.kept" if origin.get("sha256") else "pdf.origin.note.gone"),
        say("pdf.origin.note.mail") if origin.get("mail") else "")))
    story += [Spacer(1, 2 * mm), Paragraph(_markup(note), styles["small"])]

    def foot(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 7.5)
        canvas.setFillColor(_MUTED)
        canvas.setStrokeColor(_LINE)
        canvas.line(doc.leftMargin, 14 * mm, A4[0] - doc.rightMargin, 14 * mm)
        canvas.drawString(doc.leftMargin, 10 * mm, _clean(f"Munimentum {version.VERSION} · {stamp}"))
        canvas.drawRightString(A4[0] - doc.rightMargin, 10 * mm, str(doc.page))
        canvas.restoreState()

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm,
                            topMargin=20 * mm, bottomMargin=22 * mm,
                            title=_clean(title or ""), author="", creator=f"Munimentum {version.VERSION}")
    doc.build(story, onFirstPage=foot, onLaterPages=foot)
    return out.getvalue()
