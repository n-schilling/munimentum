#!/usr/bin/env python3
"""
Kombinierte Volltextsuche über Teams- UND Outlook-Export in EINER search.html.

Liest beide Export-Ordner einmal ein und erzeugt eine eigenständige Suchseite mit
eingebettetem Index. Filter-Reihenfolge wie gewünscht: zuerst Person und Datum,
danach Inhalt und Komponente (Teams/Outlook). Verlinkt direkt auf die jeweilige
Quelldatei (Teams-HTML bzw. Outlook-.eml) – relativ zum Speicherort der Suchseite.

Neben der Suche enthält die Seite zwei einfache Ansichten auf dieselben Rohdaten:
einen Kalender (Wochen-/Monatsansicht der .ics-Termine samt Status) und ein
Adressbuch (Kontakte aus den .vcf-Dateien).

Gelöschte Termine werden aus den Mails rekonstruiert: Einladungen, Antworten und
Absagen tragen den kompletten Termin samt UID im text/calendar-Teil. Fehlt diese
UID im Kalenderexport, taucht der Termin trotzdem im Kalender auf – als "gelöscht"
(wenn eine Absage vorliegt) bzw. "nicht im Kalender" (nur eingeladen/zugesagt).
Damit dabei keine Geisterkopien entstehen, werden in Exchange-IDs eingebettete
Fremd-UIDs ausgepackt (siehe norm_uid) und Treffer verworfen, deren Titel und
Startminute schon im Kalender stehen. Die Kalenderansicht "Rekonstruiert" listet
genau diese wiederhergestellten Termine auf.

Nur Standardbibliothek – keine Installation nötig.

    python3 combined_search.py [teams-ordner] [outlook-ordner] [-o ausgabe.html]

Mit --json datei.json wird statt der Seite nur die Kalender-/Kontaktauswertung
als JSON geschrieben (Termine, rekonstruierte Termine, Kontakte). Das nutzt
app.py, um Kalender und Adressbuch selbst darzustellen – die Rekonstruktion
gibt es damit einmal im Projekt statt zweimal leicht anders.

--no-reconstruct lässt die Wiederherstellung gelöschter Termine aus Mails weg.
Sie ist der mit Abstand teuerste Teil – jede .eml wird gelesen, bei 45.000
Mails Minuten – und für Termine und Kontakte allein nicht nötig.

Standard: teams_export, outlook_export. Die Ausgabe wird per Default in den
gemeinsamen übergeordneten Ordner beider Exporte geschrieben (combined_search.html),
damit die relativen Links funktionieren. Die Datei danach nicht relativ zu den
Export-Ordnern verschieben, sonst brechen die Links.

Die App bietet diese Seite seit 5.2 nicht mehr an: sie benutzt von hier nur
--json (Kalender und Kontakte). Auf einem gewachsenen Archiv wurde die Seite
dreistellig viele Megabyte groß, und dann muss ein Browser sie erst vollständig
lesen, bevor irgendetwas erscheint – „ohne die App lesbar“ leisten die .eml,
.ics und .vcf im Archiv besser. Wer sie trotzdem will, ruft dieses Skript auf;
dafür ist es geblieben.
"""

import os
import sys
import re
import json
import email
import html as html_lib
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from datetime import datetime, UTC
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote, unquote
from html.parser import HTMLParser

import progress
import settings

# Auf Windows nutzt die Konsole standardmäßig eine Legacy-Codepage (z. B. cp1252),
# und bei Umleitung in eine Datei die Locale-Kodierung. Beides lässt print() an
# Unicode-Zeichen wie →, · oder … mit UnicodeEncodeError scheitern. UTF-8 erzwingen
# (auf macOS/Linux ein No-op).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BODY_CAP = 4000
CATS = {"1on1", "group", "meeting", "channels"}
CAT_LABEL = {"1on1": "1:1-Chat", "group": "Gruppenchat",
             "meeting": "Besprechung", "channels": "Kanal"}
MAIL_DIR = "E-Mail"   # Outlook-Export legt den Postfachbaum hierunter ab
_BLOCK = {"br", "p", "div", "li", "tr"}


# ===========================================================================
# Teams: exportierte Konversations-HTML parsen
# ===========================================================================
class ConvParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.msgs = []
        self._depth = 0
        self._cur = None
        self._msg_depth = None
        self._in_body = False
        self._body_depth = None
        self._capture = None
        self._in_h1 = False
        self._nb, self._tb, self._bb, self._h1 = [], [], [], []

    @staticmethod
    def _classes(attrs):
        for k, v in attrs:
            if k == "class":
                return (v or "").split()
        return []

    def handle_starttag(self, tag, attrs):
        cls = self._classes(attrs)
        if tag == "div":
            self._depth += 1
            if self._cur is None and "msg" in cls:
                self._cur = True
                self._msg_depth = self._depth
                self._nb, self._tb, self._bb = [], [], []
            if self._cur is not None and "body" in cls and not self._in_body:
                self._in_body = True
                self._body_depth = self._depth
            elif self._in_body:
                self._bb.append(" ")
        elif tag == "span":
            if self._cur is not None and not self._in_body:
                if "name" in cls:
                    self._capture = "name"
                elif "time" in cls:
                    self._capture = "time"
        elif tag in _BLOCK and self._in_body:
            self._bb.append(" ")
        elif tag == "h1":
            self._in_h1 = True

    def handle_endtag(self, tag):
        if tag == "span":
            self._capture = None
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "div":
            if self._in_body and self._depth == self._body_depth:
                self._in_body = False
            if self._cur is not None and self._depth == self._msg_depth:
                text = " ".join("".join(self._bb).split())
                name = "".join(self._nb).strip()
                time = "".join(self._tb).strip()
                if text:
                    self.msgs.append({"n": name, "t": time, "x": text})
                self._cur = None
            self._depth -= 1

    def handle_data(self, data):
        if self._in_h1:
            self._h1.append(data)
        elif self._capture == "name":
            self._nb.append(data)
        elif self._capture == "time":
            self._tb.append(data)
        elif self._in_body:
            self._bb.append(data)

    def finish(self):
        self.title = "".join(self._h1).strip()


def parse_local(s):
    """'YYYY-MM-DD HH:MM' (lokale Zeit aus dem Teams-Export) -> Epoch oder None."""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").timestamp()
    except Exception:
        return None


def read_teams(root, out_dir, people):
    recs = []
    # nur echte Konversations-HTML – Index/Suche und versteckte Ordner (.imgcache,
    # .deltastate) überspringen; deren Inhalte sind keine Konversationen
    files = [p for p in sorted(root.rglob("*.html"))
             if p.name not in ("index.html", "search.html")
             and not any(seg.startswith(".") for seg in p.relative_to(root).parts)]
    for p in files:
        raw = p.read_text(encoding="utf-8", errors="replace")
        pr = ConvParser()
        try:
            pr.feed(raw)
            pr.finish()
            title, msgs = pr.title, pr.msgs
        except Exception:
            title, msgs = p.stem.rsplit("__", 1)[0], []
        rel = p.relative_to(root).as_posix()
        top = rel.split("/")[0]
        cat = top if top in CATS else "other"
        ctx = (f"Kanal: {title}" if cat == "channels"
               else CAT_LABEL.get(cat, "Teams"))
        href = link(p, out_dir)
        for m in msgs:
            who = m["n"] or "(unbekannt)"
            if m["n"]:
                people.add(m["n"])
            recs.append({
                "src": "teams",
                "who": who,
                "ppl": (m["n"] + " " + title).lower(),
                "ts": parse_local(m["t"]),
                "d": m["t"],
                "title": title,
                "ctx": ctx,
                "x": m["x"][:BODY_CAP],
                "p": href,
            })
    return recs


# ===========================================================================
# Outlook: .eml parsen
# ===========================================================================
def strip_html(s):
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</tr>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html_lib.unescape(s)


def collapse(s, cap=BODY_CAP):
    return " ".join((s or "").split())[:cap]


def hdr(msg, name):
    v = msg[name]
    return str(v).strip() if v is not None else ""


def decode_part(part):
    try:
        c = part.get_content()
        if isinstance(c, str):
            return c
    except Exception:
        pass
    try:
        b = part.get_payload(decode=True) or b""
        return b.decode(part.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return ""


def extract_body(msg):
    part = None
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        for p in msg.walk():
            if (p.get_content_maintype() == "text"
                    and p.get_content_disposition() != "attachment"):
                part = p
                break
    if part is None:
        return ""
    text = decode_part(part)
    if part.get_content_type() == "text/html":
        text = strip_html(text)
    return collapse(text)


def addr_people(msg, *headers):
    raw = []
    for h in headers:
        vals = msg.get_all(h)
        if vals:
            raw += [str(v) for v in vals]
    names, emails = [], []
    for name, addr in getaddresses(raw):
        if name.strip():
            names.append(name.strip())
        if addr.strip():
            emails.append(addr.strip())
    return names, emails


def mail_ical(msg):
    """(METHOD, iCalendar-Text) aus dem text/calendar-Teil einer Mail."""
    for part in msg.walk():
        if part.get_content_type() != "text/calendar":
            continue
        try:
            txt = part.get_content()
        except Exception:
            txt = None
        if not isinstance(txt, str):
            raw = part.get_payload(decode=True) or b""
            txt = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        return (part.get_param("method") or "").upper(), txt
    return "", ""


def read_outlook(root, out_dir, people, invites=None):
    recs = []
    dateien = sorted(root.rglob("*.eml"))
    progress.melde(0, len(dateien), "mails")
    for n, p in enumerate(dateien, 1):
        if n % 200 == 0:
            progress.melde(n, len(dateien), "mails")
        try:
            with open(p, "rb") as f:
                msg = BytesParser(policy=policy.default).parse(f)
        except Exception:
            continue
        fn, fe = addr_people(msg, "from")
        tn, te = addr_people(msg, "to", "cc")
        who = (fn[0] if fn else (fe[0] if fe else "")) or "(unbekannt)"
        for x in fn + fe + tn + te:
            people.add(x)
        raw_date = hdr(msg, "date")
        ts, disp = None, raw_date
        try:
            dt = email.utils.parsedate_to_datetime(raw_date)
            if dt is not None:
                ts = dt.timestamp()
                disp = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        rel = p.relative_to(root).as_posix()
        parts = rel.split("/")
        if parts and parts[0] == MAIL_DIR:
            parts = parts[1:]            # "E-Mail/" nur für die Anzeige entfernen
        folder = "/".join(parts[:-1]) if len(parts) > 1 else "(Stamm)"
        if invites is not None:
            method, ical = mail_ical(msg)
            if ical:
                m2, evs = parse_vevents(ical)
                meth = method or m2
                # Antwortmails führen keinen ORGANIZER, nur den antwortenden
                # ATTENDEE – der Organisator ist ihr Empfänger. Einladungen und
                # Absagen kommen umgekehrt vom Organisator selbst.
                hn, hm = addr_people(msg, "to") if meth in ("REPLY", "COUNTER") else (fn, fe)
                hint = (hn[0] if hn else "", hm[0] if hm else "")
                for ev in evs:
                    if ev["uid"]:
                        invites.append({"method": meth, "ev": ev, "org_hint": hint,
                                        "href": link(p, out_dir), "mts": ts, "md": disp})
        recs.append({
            "src": "outlook",
            "who": who,
            "ppl": " ".join(fn + fe + tn + te).lower(),
            "ts": ts,
            "d": disp,
            "title": hdr(msg, "subject") or "(kein Betreff)",
            "ctx": folder,
            "x": extract_body(msg),
            "p": link(p, out_dir),
        })
    return recs


# ===========================================================================
# Kalender (.ics) und Kontakte (.vcf) – liegen im Outlook-Export
# ===========================================================================
def _unfold(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(v):
    res, i = [], 0
    while i < len(v):
        ch = v[i]
        if ch == "\\" and i + 1 < len(v):
            res.append({"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}.get(v[i + 1], v[i + 1]))
            i += 2
        else:
            res.append(ch)
            i += 1
    return "".join(res)


def _prop(line):
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            name = line[:i].split(";", 1)[0].upper()
            return name, line[:i][len(name):], line[i + 1:]
    return None, None, None


def _pval(params, key):
    m = re.search(rf';{key}=("([^"]*)"|([^;:]*))', params or "", re.I)
    if not m:
        return ""
    return m.group(2) if m.group(2) is not None else (m.group(3) or "")


def _demail(v):
    return re.sub(r"(?i)^mailto:", "", (v or "").strip())


# Exchange schreibt in Einladungsmails Windows-Zeitzonennamen statt IANA-Namen.
# Ohne Zuordnung landen Termine aus anderen Zeitzonen um deren Differenz versetzt
# im Kalender. Die häufigsten Namen genügen – alles andere fällt auf Lokalzeit
# zurück (wie bisher).
WIN_TZ = {
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Etc/UTC",
    "UTC": "Etc/UTC",
    "GTB Standard Time": "Europe/Athens",
    "FLE Standard Time": "Europe/Helsinki",
    "Turkey Standard Time": "Europe/Istanbul",
    "Russian Standard Time": "Europe/Moscow",
    "Israel Standard Time": "Asia/Jerusalem",
    "Arabian Standard Time": "Asia/Dubai",
    "India Standard Time": "Asia/Kolkata",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Pacific SA Standard Time": "America/Santiago",
    "South Africa Standard Time": "Africa/Johannesburg",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "E. Africa Standard Time": "Africa/Nairobi",
}
_ZONES = {}


def _zone(tzid):
    """TZID (Windows- oder IANA-Name) -> tzinfo, sonst None (= Lokalzeit)."""
    tzid = (tzid or "").strip().strip('"')
    if not tzid:
        return None
    if tzid not in _ZONES:
        try:
            _ZONES[tzid] = ZoneInfo(WIN_TZ.get(tzid, tzid))
        except Exception:
            # unbekannter Name oder fehlende Zeitzonendaten (Windows ohne tzdata)
            _ZONES[tzid] = None
    return _ZONES[tzid]


def _ics_when(val, dateonly, tzid=""):
    if not val:
        return None, ""
    try:
        if dateonly or (len(val) == 8 and val.isdigit()):
            dt = datetime.strptime(val[:8], "%Y%m%d")
            return dt.timestamp(), dt.strftime("%Y-%m-%d")
        utc = val.endswith("Z")
        dt = datetime.strptime(val.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
        zone = UTC if utc else _zone(tzid)
        if zone is not None:
            dt = dt.replace(tzinfo=zone)
            return dt.timestamp(), dt.astimezone().strftime("%Y-%m-%d %H:%M")
        return dt.timestamp(), dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None, val


def parse_vevents(text):
    """iCalendar-Text -> (METHOD, [VEVENT-Felder, …]).

    Blockweise, damit Eigenschaften aus VTIMEZONE/VALARM nicht im Termin landen:
    Einladungsmails von Exchange enthalten ein VTIMEZONE mit eigenem DTSTART
    (z. B. 16010101T030000), das ein flacher Parser als Termindatum lesen würde.
    """
    method, events, stack, ev = "", [], [], None
    for line in _unfold(text):
        name, params, value = _prop(line)
        if not name:
            continue
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                ev = {"uid": "", "recid": "", "summary": "", "location": "",
                      "description": "", "dtstart": "", "dateonly": False,
                      "tzstart": "", "dtend": "", "enddateonly": False,
                      "tzend": "", "status": "", "seq": 0,
                      "org_cn": "", "org_mail": "",
                      "att_names": [], "att_mails": []}
            continue
        if name == "END":
            if value.strip().upper() == "VEVENT" and ev is not None:
                events.append(ev)
                ev = None
            if stack:
                stack.pop()
            continue
        if name == "METHOD" and ev is None:
            method = value.strip().upper()
        if ev is None or (stack and stack[-1] != "VEVENT"):
            continue
        if name == "UID":
            ev["uid"] = value.strip()
        elif name == "RECURRENCE-ID":
            ev["recid"] = value.strip()
        elif name == "SUMMARY":
            ev["summary"] = _unescape(value)
        elif name == "LOCATION":
            ev["location"] = _unescape(value)
        elif name == "DESCRIPTION":
            ev["description"] = _unescape(value)
        elif name == "DTSTART":
            ev["dtstart"] = value.strip()
            ev["dateonly"] = "VALUE=DATE" in (params or "").upper()
            ev["tzstart"] = _pval(params, "TZID")
        elif name == "DTEND":
            ev["dtend"] = value.strip()
            ev["enddateonly"] = "VALUE=DATE" in (params or "").upper()
            ev["tzend"] = _pval(params, "TZID")
        elif name == "STATUS":
            ev["status"] = value.strip().lower()
        elif name == "SEQUENCE":
            try:
                ev["seq"] = int(value.strip())
            except ValueError:
                pass
        elif name == "ORGANIZER":
            ev["org_cn"], ev["org_mail"] = _pval(params, "CN"), _demail(value)
        elif name == "ATTENDEE":
            cn, mail = _pval(params, "CN"), _demail(value)
            if cn:
                ev["att_names"].append(cn)
            if mail:
                ev["att_mails"].append(mail)
    return method, events


def event_rec(ev, *, ctx, href, status, cal):
    """Gemeinsamer Datensatz für Kalender- und rekonstruierte Termine."""
    ts, disp = _ics_when(ev["dtstart"], ev["dateonly"], ev.get("tzstart", ""))
    te, _ = _ics_when(ev["dtend"], ev["enddateonly"], ev.get("tzend", ""))
    names = [ev["org_cn"], ev["org_mail"]] + ev["att_names"] + ev["att_mails"]
    text = ((f"Ort: {ev['location']}. " if ev["location"] else "") + ev["description"]).strip()
    return {
        "src": "kalender",
        "who": ev["org_cn"] or ev["org_mail"] or "(unbekannt)",
        "ppl": " ".join(x for x in names if x).lower(),
        "ts": ts, "d": disp,
        "title": ev["summary"] or "(kein Betreff)",
        "ctx": ctx,
        "x": text[:BODY_CAP],
        "p": href,
        # zusätzlich für die Kalenderansicht
        "te": te,
        "ad": 1 if ev["dateonly"] else 0,
        "st": status,
        "cal": cal,
        "loc": ev["location"],
        "att": (ev["att_names"] or ev["att_mails"])[:25],
        "uid": ev["uid"],
    }


def read_calendar(root, out_dir, people):
    recs = []
    for p in sorted(root.rglob("*.ics")):
        _, events = parse_vevents(p.read_text(encoding="utf-8", errors="replace"))
        if not events:
            continue
        ev = events[0]                       # der Export legt einen Termin je Datei ab
        segs = p.relative_to(root).as_posix().split("/")
        cal = segs[1] if len(segs) >= 3 and segs[0] == "kalender" else "Kalender"
        for x in ([ev["org_cn"], ev["org_mail"]] + ev["att_names"] + ev["att_mails"]):
            if x:
                people.add(x)
        recs.append(event_rec(ev, ctx=f"Kalender: {cal}", href=link(p, out_dir),
                              status=ev["status"] or "confirmed", cal=cal))
    return recs


# Outlook stellt Antwort-/Absagemails einen Status vor den Betreff – für den
# rekonstruierten Termin ist das Rauschen, den Status zeigt die Ansicht selbst.
REPLY_PREFIX = re.compile(
    r"^(Abgesagt|Canceled|Cancelled|Angenommen|Accepted|Abgelehnt|Declined|"
    r"Mit Vorbehalt|Tentative|Vorläufig zugesagt|Aktualisiert|Updated|"
    r"Weitergeleitet|Forwarded|Zeitvorschlag|New Time Proposed)\s*:\s*", re.I)

# Welche Mail beschreibt einen Termin am besten? Einladung vor Absage vor Antwort.
METHOD_RANK = {"REQUEST": 4, "PUBLISH": 3, "CANCEL": 2, "COUNTER": 1, "REPLY": 0}

_VCAL_UID = b"vCal-Uid\x01\x00\x00\x00"


def norm_uid(uid):
    """Termin-UID auf eine vergleichbare Form bringen.

    Exchange verpackt fremde UIDs (Google, Zoom, …) in seine eigene Global Object
    ID: ein Hex-Blob, der die ursprüngliche UID als ASCII hinter der Kennung
    "vCal-Uid" enthält. Der Kalenderexport liefert diesen Blob, die Einladungsmail
    dagegen die nackte UID – ohne Auspacken gälte derselbe Termin als gelöscht.
    """
    u = (uid or "").strip()
    if len(u) < len(_VCAL_UID) * 2 or len(u) % 2:
        return u.lower()
    try:
        raw = bytes.fromhex(u)
    except ValueError:
        return u.lower()
    i = raw.find(_VCAL_UID)
    if i < 0:
        return u.lower()
    inner = raw[i + len(_VCAL_UID):].split(b"\x00", 1)[0]
    return inner.decode("ascii", "replace").lower() or u.lower()


def reconstruct_events(invites, cal_recs):
    """Termine wiederherstellen, die nur noch in Mails existieren.

    Einladungs-, Antwort- und Absagemails tragen den kompletten VEVENT samt UID.
    Fehlt diese UID im Kalenderexport, ist der Termin dort gelöscht – aus der Mail
    lässt er sich rekonstruieren. Liegt eine Absage vor (METHOD:CANCEL), gilt er
    als abgesagt/gelöscht, sonst nur als "nicht im Kalender" (z. B. nie zugesagt).

    Rückgabe: (rekonstruierte Datensätze, Anzahl nachträglich als abgesagt
    markierter Kalendertermine, Anzahl als Doppel verworfener Rekonstruktionen).
    """
    known, same = {}, set()
    for r in cal_recs:
        if r.get("uid"):
            known.setdefault(norm_uid(r["uid"]), []).append(r)
        if r.get("ts"):
            same.add((r["title"].strip().lower(), int(r["ts"] // 60)))

    groups = {}
    for it in invites:
        ev = it["ev"]
        cancelled = it["method"] == "CANCEL" or ev["status"] == "cancelled"
        rank = (1 if ev["dtstart"] else 0, METHOD_RANK.get(it["method"], 0),
                ev["seq"], it["mts"] or 0)
        g = groups.setdefault((norm_uid(ev["uid"]), ev["recid"]), {"cancel": None, "best": None})
        if cancelled and (g["cancel"] is None or rank > g["cancel"][0]):
            g["cancel"] = (rank, it)
        if g["best"] is None or rank > g["best"][0]:
            g["best"] = (rank, it)

    ghosts, marked, dupes = [], 0, 0
    for (uid, recid), g in groups.items():
        cancel = g["cancel"]
        if uid in known:
            if not recid:
                # Termin ist im Kalender – Absagemail heilt einen veralteten Status
                for r in (known[uid] if cancel else []):
                    if r["st"] != "cancelled":
                        r["st"] = "cancelled"
                        marked += 1
                continue
            if not cancel:
                continue      # Serieninstanz ohne Absage steckt bereits im Serientermin
        it = (cancel or g["best"])[1]
        ev = it["ev"]
        if not ev["dtstart"]:
            continue          # ohne Startzeit im Kalender nicht platzierbar
        ev = dict(ev, summary=REPLY_PREFIX.sub("", ev["summary"]).strip())
        if not (ev["org_cn"] or ev["org_mail"]):
            ev["org_cn"], ev["org_mail"] = it.get("org_hint") or ("", "")
        state = "deleted" if cancel else "gone"
        note = "abgesagt" if cancel else "nicht im Kalender"
        rec = event_rec(ev, ctx=f"Kalender: {note} · rekonstruiert aus Mail vom {it['md']}",
                        href=it["href"], status=state, cal="(rekonstruiert)")
        # Fängt UID-Formate ab, die hier noch nicht bekannt sind: steht derselbe
        # Termin (Titel + Startminute) schon im Kalender, ist er nicht gelöscht.
        if rec["ts"] and (rec["title"].strip().lower(), int(rec["ts"] // 60)) in same:
            dupes += 1
            continue
        ghosts.append(rec)
    ghosts.sort(key=lambda r: (r["ts"] is None, -(r["ts"] or 0)))
    return ghosts, marked, dupes


def read_contacts(root, out_dir, people):
    recs = []
    for p in sorted(root.rglob("*.vcf")):
        fn = org = title = note = given = family = ""
        emails, tels = [], []
        for line in _unfold(p.read_text(encoding="utf-8", errors="replace")):
            name, params, value = _prop(line)
            if not name:
                continue
            if name == "FN":
                fn = _unescape(value)
            elif name == "N":
                parts = [_unescape(x) for x in value.split(";")]
                family = parts[0] if len(parts) > 0 else ""
                given = parts[1] if len(parts) > 1 else ""
            elif name == "ORG":
                org = " · ".join(x for x in _unescape(value).split(";") if x)
            elif name == "TITLE":
                title = _unescape(value)
            elif name == "EMAIL":
                emails.append(value.strip())
            elif name == "TEL":
                tels.append(value.strip())
            elif name == "NOTE":
                note = _unescape(value)
        if not fn:
            fn = (given + " " + family).strip() or "(ohne Namen)"
        segs = p.relative_to(root).as_posix().split("/")
        folder = segs[1] if len(segs) >= 3 and segs[0] == "kontakte" else ""
        people.add(fn)
        for e in emails:
            people.add(e)
        text = " · ".join(x for x in ([org, title] + emails + tels + ([note] if note else [])) if x)
        recs.append({
            "src": "kontakte",
            "who": org or title or "Kontakt",
            "ppl": " ".join([fn] + emails).lower(),
            "ts": None, "d": "",
            "title": fn,
            "ctx": f"Kontakte: {folder}" if folder else "Kontakte",
            "x": text[:BODY_CAP],
            "p": link(p, out_dir),
            # zusätzlich für das Adressbuch
            "em": emails[:10],
            "tel": tels[:10],
            "org": org,
            "role": title,
        })
    return recs


# ===========================================================================
# Gemeinsam
# ===========================================================================
def link(path, out_dir):
    try:
        rel = os.path.relpath(path, start=out_dir).replace(os.sep, "/")
        return "/".join(quote(seg) for seg in rel.split("/"))
    except ValueError:
        return Path(path).as_uri()


def collect_calendar_data(outlook_dir, text_cap=600, reconstruct=True):
    """Kalender, Kontakte und rekonstruierte Termine als reine Daten liefern.

    Dieselbe Auswertung wie build(), nur ohne Seite drumherum – für app.py, das
    Kalender und Adressbuch selbst darstellt. Damit gibt es die Rekonstruktion
    gelöschter Termine genau einmal im Projekt und nicht zweimal leicht anders.

    Die Pfade kommen als `root` + `rel` (unkodiert) statt als fertiger Link:
    die statische Seite verlinkt relativ zu ihrem Ablageort, die App über ihre
    eigene /source-Route. Beschreibungstexte werden gekürzt (`text_cap`) – im
    Kalender stehen sie nur im Tooltip und in der Suche über die
    rekonstruierten Termine, ungekürzt blähen 5000 Termine die Antwort auf.
    """
    root = Path(outlook_dir).resolve()
    if not root.is_dir():
        raise SystemExit(f"Outlook-Export nicht gefunden: {outlook_dir}")
    people = set()
    invites = []
    ghosts, marked, dupes = [], 0, 0
    # out_dir = root: link() liefert dann Pfade relativ zum Export-Stamm, genau
    # das, was die /source-Route der App erwartet.
    #
    # Das Lesen aller .eml ist der teure Teil – bei 45.000 Mails Minuten – und
    # geschieht ausschließlich für die Einladungen. Wer den Kalender nur wegen
    # der Termine oder der Kontakte aufbaut, zahlt das sonst mit, ohne etwas
    # davon zu haben.
    if reconstruct:
        read_outlook(root, root, people, invites)     # nur wegen der Einladungen
    cal = read_calendar(root, root, people)
    if reconstruct:
        ghosts, marked, dupes = reconstruct_events(invites, cal)
    contacts = read_contacts(root, root, people)

    recs = cal + ghosts + contacts
    for r in recs:
        r["root"] = "outlook"
        r["rel"] = unquote(r.pop("p", ""))
        r.pop("uid", None)          # nur fürs Zuordnen oben gebraucht, knapp 1 MB
        # Personenliste und Beschreibung braucht nur die Suche über die
        # rekonstruierten Termine. Über alle 5860 Termine hinweg sind das zwei
        # Drittel der Antwort, ohne dass sie je jemand liest.
        if r.get("st") in ("deleted", "gone"):
            if text_cap is not None and r.get("x"):
                r["x"] = r["x"][:text_cap]
        else:
            r.pop("ppl", None)
            r.pop("x", None)
    recs.sort(key=lambda r: (r["ts"] is None, -(r["ts"] or 0)))
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "outlook_dir": str(root),
        # Ob rekonstruiert wurde, muss mit: sonst könnte die Oberfläche eine
        # leere Liste nur als „es gab nichts“ deuten und nicht als „danach
        # wurde gar nicht gesucht“.
        "reconstruct": bool(reconstruct),
        "counts": {"kalender": len(cal), "rekonstruiert": len(ghosts),
                   "kontakte": len(contacts), "abgesagt_markiert": marked,
                   "doppel_verworfen": dupes},
        "recs": recs,
    }


def build(teams_dir, outlook_dir, output):
    out_dir = output.parent.resolve()
    people = set()
    recs = []
    counts = {"teams": 0, "outlook": 0, "kalender": 0, "kontakte": 0, "rekonstruiert": 0}
    tp, op = Path(teams_dir), Path(outlook_dir)
    if tp.is_dir():
        r = read_teams(tp.resolve(), out_dir, people)
        recs += r
        counts["teams"] = len(r)
        print(f"  Teams:    {len(r)} Nachrichten aus {teams_dir}")
    else:
        print(f"  Teams-Ordner übersprungen (nicht gefunden): {teams_dir}")
    if op.is_dir():
        opr = op.resolve()
        invites = []
        m = read_outlook(opr, out_dir, people, invites)
        c = read_calendar(opr, out_dir, people)
        g, marked, dupes = reconstruct_events(invites, c)
        k = read_contacts(opr, out_dir, people)
        recs += m + c + g + k
        counts.update(outlook=len(m), kalender=len(c), kontakte=len(k), rekonstruiert=len(g))
        print(f"  Mail:     {len(m)} Mails aus {outlook_dir}")
        print(f"  Kalender: {len(c)} Termine"
              + (f" ({marked} per Absagemail als abgesagt markiert)" if marked else ""))
        if g:
            weg = sum(1 for r in g if r["st"] == "deleted")
            print(f"            {len(g)} nicht mehr im Kalender, aus Mails rekonstruiert "
                  f"({weg} davon abgesagt)"
                  + (f", {dupes} als Doppel verworfen" if dupes else ""))
        print(f"  Kontakte: {len(k)} Personen")
    else:
        print(f"  Outlook-Ordner übersprungen (nicht gefunden): {outlook_dir}")

    recs.sort(key=lambda r: (r["ts"] is None, -(r["ts"] or 0)))   # neueste zuerst, undatierte zuletzt

    index = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "people": sorted(people, key=str.lower),
        "recs": recs,
    }
    # "<" als \u003c einbetten: neutralisiert </script>, <script und <!--,
    # die der Browser sonst im Script-Block interpretiert (JSON.parse bleibt gleich).
    payload = json.dumps(index, ensure_ascii=False).replace("<", "\\u003c")
    html = TEMPLATE.replace("/*__INDEX__*/", payload)
    output.write_text(html, encoding="utf-8")
    return output, counts


TEMPLATE = r"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Teams + Outlook · Suche</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#1b1b1f;background:#f6f7f9}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #e3e5e8;padding:14px 20px;z-index:2}
h1{margin:0 0 10px;font-size:17px}
.tabs{display:flex;gap:6px;margin:0 0 10px}
.tab{padding:6px 14px;border:1px solid #d4d8dd;border-radius:8px;background:#fff;font-size:13.5px;cursor:pointer;color:#3b3f46}
.tab.on{background:#1b1b1f;border-color:#1b1b1f;color:#fff}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.nav{padding:7px 12px;border:1px solid #cfd3d8;border-radius:8px;background:#fff;font-size:14px;cursor:pointer}
.nav:hover{border-color:#2b6cb0;color:#2b6cb0}
#calTitle{font-weight:600;margin-left:4px}
.legend{display:flex;gap:12px;margin-left:auto;font-size:12px;color:#8a8f98;align-items:center}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:4px;vertical-align:-1px}
.legend i.confirmed{background:#2b6cb0}
.legend i.tentative{background:#c98a17}
.legend i.cancelled{background:#c0392b}
.legend i.deleted{background:#fbeceb;border:1px dashed #c0392b}
.legend i.gone{background:#f2f3f5;border:1px dashed #9aa0a6}
.primary{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:3px}
.field label{font-size:11px;color:#8a8f98;text-transform:uppercase;letter-spacing:.03em}
input,select{padding:9px 11px;font-size:14px;border:1px solid #cfd3d8;border-radius:8px;outline:none;background:#fff}
input:focus,select:focus{border-color:#2b6cb0;box-shadow:0 0 0 3px rgba(43,108,176,.12)}
#person{min-width:200px}
#q{width:100%;margin-top:10px;padding:11px 13px;font-size:15px}
.chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
.chip{padding:5px 12px;border:1px solid #d4d8dd;border-radius:999px;background:#fff;font-size:13px;cursor:pointer;color:#3b3f46}
.chip.on{background:#2b6cb0;border-color:#2b6cb0;color:#fff}
#stats{color:#9aa0a6;font-size:12px;margin-left:auto}
#summary{color:#5b5f66;font-size:13px;margin:0 0 4px}
main{max-width:900px;margin:0 auto;padding:16px}
main.wide{max-width:1280px}
.hint{color:#9aa0a6;padding:24px 4px}

/* Kalender */
.grid{display:grid;gap:8px}
/* minmax(0,…): sonst sprengen lange Termintitel die Spaltenbreite */
.wk{grid-template-columns:repeat(7,minmax(0,1fr))}
.mo{grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}
.dow{font-size:11px;color:#8a8f98;text-transform:uppercase;letter-spacing:.03em;padding:0 2px}
.day{background:#fff;border:1px solid #ececef;border-radius:10px;padding:8px;min-height:110px;min-width:0}
.day.today{border-color:#2b6cb0;box-shadow:0 0 0 2px rgba(43,108,176,.12)}
.day.out{background:#f2f3f5;color:#a8adb4}
.dnum{font-size:12px;color:#8a8f98;margin-bottom:5px;display:flex;gap:5px;align-items:baseline}
.dnum b{font-size:14px;color:#1b1b1f}
.dnum .wd{display:none}   /* Wochentag steht schon in der Spaltenüberschrift */
.day.out .dnum b{color:#a8adb4}
.ev{display:block;font-size:12px;line-height:1.35;margin:3px 0;padding:4px 6px;border-radius:6px;text-decoration:none;
    border-left:3px solid #2b6cb0;background:#eef4fb;color:#1b1b1f;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ev:hover{white-space:normal}
.ev .evt{color:#5b5f66;font-variant-numeric:tabular-nums}
.ev.tentative{border-left-color:#c98a17;background:#fdf6e7;border-left-style:dashed}
.ev.cancelled{border-left-color:#c0392b;background:#fbeceb;text-decoration:line-through;opacity:.75}
/* nur aus Mails rekonstruiert: gestrichelter Rahmen statt Balken */
.ev.deleted{border:1px dashed #c0392b;border-left-width:3px;background:#fbeceb;text-decoration:line-through;opacity:.8}
.ev.gone{border:1px dashed #b6bbc2;border-left-width:3px;background:#f2f3f5;color:#5b5f66}
.mo .day{min-height:96px}
@media(max-width:820px){.wk,.mo{grid-template-columns:minmax(0,1fr)}.dowrow{display:none}.day{min-height:0}.dnum .wd{display:inline}}

/* Ansicht "Rekonstruiert": nur aus Mails wiederhergestellte Termine */
.rbnote{color:#8a8f98;font-size:12.5px;margin:0 0 10px}
.rbbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
.rbbar input{min-width:260px}
.rbcount{color:#9aa0a6;font-size:12px;margin-left:auto}
.rbmonth{margin:16px 0 6px;font-size:13px;font-weight:700;color:#8a8f98;border-bottom:1px solid #e3e5e8;padding-bottom:3px}
.rbrow{display:flex;gap:10px;align-items:baseline;background:#fff;border:1px solid #ececef;border-radius:9px;
       padding:8px 11px;margin:5px 0;text-decoration:none;color:#1b1b1f}
.rbrow:hover{border-color:#2b6cb0}
.rbrow.deleted{border-left:3px solid #c0392b}
.rbrow.gone{border-left:3px solid #b6bbc2}
.rbdate{color:#5b5f66;font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap;min-width:158px}
.rbstate{font-size:11px;padding:2px 8px;border-radius:6px;font-weight:600;white-space:nowrap}
.rbrow.deleted .rbstate{background:#fbeceb;color:#c0392b}
.rbrow.gone .rbstate{background:#f2f3f5;color:#5b5f66}
.rbtitle{font-weight:600;overflow-wrap:anywhere;min-width:0;flex:1}
.rbrow.deleted .rbtitle{text-decoration:line-through}
.rbwho{color:#8a8f98;font-size:12.5px;overflow-wrap:anywhere;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
@media(max-width:820px){.rbrow{flex-wrap:wrap;gap:4px 9px}.rbwho{max-width:none}}

/* Adressbuch */
.letter{margin:18px 0 6px;font-size:13px;font-weight:700;color:#8a8f98;border-bottom:1px solid #e3e5e8;padding-bottom:3px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.card{background:#fff;border:1px solid #ececef;border-radius:11px;padding:11px 13px}
.cname{font-weight:600;word-wrap:break-word;overflow-wrap:anywhere}
.cname a{color:#1b1b1f;text-decoration:none}
.cname a:hover{color:#2b6cb0;text-decoration:underline}
.crole{font-size:12.5px;color:#8a8f98;margin-bottom:5px;word-wrap:break-word;overflow-wrap:anywhere}
.cline{font-size:13px;word-wrap:break-word;overflow-wrap:anywhere}
.cline a{color:#2b6cb0;text-decoration:none}
.cline a:hover{text-decoration:underline}
.cline span{color:#9aa0a6;margin-right:5px}
.rec{background:#fff;border:1px solid #ececef;border-radius:11px;margin:10px 0;padding:12px 14px}
.rtop{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:4px}
.badge{font-size:11px;padding:2px 9px;border-radius:6px;font-weight:600}
.badge.teams{background:#efe7fb;color:#6b3fa0}
.badge.outlook{background:#e6f0fb;color:#1f5fa6}
.badge.kalender{background:#e3f3ec;color:#1f7a4d}
.badge.kontakte{background:#fdeee3;color:#b5651d}
.who{font-weight:600}
.when{color:#9aa0a6;font-size:12.5px;margin-left:auto}
.title{font-weight:600;margin:2px 0;word-wrap:break-word;overflow-wrap:anywhere}
.title a{color:#1b1b1f;text-decoration:none}
.title a:hover{color:#2b6cb0;text-decoration:underline}
.ctx{font-size:12px;color:#8a8f98;margin-bottom:4px;word-break:break-word}
.snip{color:#26282c;word-wrap:break-word;overflow-wrap:anywhere}
.snip mark,.title mark{background:#ffe9a8;padding:0 1px;border-radius:2px}
.more{padding:10px 4px;color:#9aa0a6;font-size:12.5px}
</style></head>
<body>
<header>
  <h1>Teams + Outlook</h1>
  <div class="tabs">
    <button class="tab on" data-view="search">Suche</button>
    <button class="tab" data-view="cal">Kalender</button>
    <button class="tab" data-view="book">Adressbuch</button>
  </div>
  <div id="bar-search">
    <div class="primary">
      <div class="field"><label>Person</label>
        <input id="person" type="text" list="ppl" placeholder="Name oder E-Mail…" autocomplete="off">
        <datalist id="ppl"></datalist></div>
      <div class="field"><label>Von</label><input id="from" type="date"></div>
      <div class="field"><label>Bis</label><input id="to" type="date"></div>
    </div>
    <input id="q" type="search" placeholder="Inhalt durchsuchen… (mehrere Wörter = alle müssen vorkommen)" autocomplete="off">
    <div class="chips" id="srcChips">
      <span class="chip on" data-src="all">Alle Quellen</span>
      <span class="chip" data-src="teams">Teams</span>
      <span class="chip" data-src="outlook">Mail</span>
      <span class="chip" data-src="kalender">Kalender</span>
      <span class="chip" data-src="kontakte">Kontakte</span>
      <span id="stats"></span>
    </div>
  </div>
  <div id="bar-cal" class="bar" hidden>
    <span id="calNav">
      <button class="nav" id="calPrev">‹</button>
      <button class="nav" id="calToday">Heute</button>
      <button class="nav" id="calNext">›</button>
      <span id="calTitle"></span>
    </span>
    <span class="chips" id="calChips" style="margin-top:0">
      <span class="chip on" data-mode="week">Woche</span>
      <span class="chip" data-mode="month">Monat</span>
      <span class="chip" data-mode="rebuilt"
            title="Aus E-Mails wiederhergestellte Kalendereinträge">Rekonstruiert</span>
    </span>
    <span class="legend" id="calLegend">
      <span><i class="confirmed"></i>Bestätigt</span>
      <span><i class="tentative"></i>Vorläufig</span>
      <span><i class="cancelled"></i>Abgesagt</span>
      <span><i class="deleted"></i>Gelöscht (aus Mail)</span>
      <span><i class="gone"></i>Nicht im Kalender</span>
    </span>
  </div>
  <div id="bar-book" class="bar" hidden>
    <input id="bookQ" type="search" placeholder="Name, Firma, E-Mail oder Telefon…" autocomplete="off" style="min-width:280px">
    <span id="bookStats" style="color:#9aa0a6;font-size:12px"></span>
  </div>
</header>
<main>
  <section id="view-search">
    <p id="summary"></p>
    <div id="results"><p class="hint">Person, Datum, Inhalt oder Quelle wählen…</p></div>
  </section>
  <section id="view-cal" hidden></section>
  <section id="view-book" hidden></section>
</main>
<script type="application/json" id="idx">/*__INDEX__*/</script>
<script>
const DATA = JSON.parse(document.getElementById('idx').textContent);
const recs = DATA.recs || [];
let src = 'all';
const qEl = document.getElementById('q');
const personEl = document.getElementById('person');
const fromEl = document.getElementById('from');
const toEl = document.getElementById('to');
const out = document.getElementById('results');

(DATA.people || []).forEach(p=>{ const o=document.createElement('option'); o.value=p; document.getElementById('ppl').appendChild(o); });
const LABEL = {teams:'Teams', outlook:'Mail', kalender:'Kalender', kontakte:'Kontakte'};
const cnt = {teams:0, outlook:0, kalender:0, kontakte:0};
for(const r of recs){ if(cnt[r.src]!=null) cnt[r.src]++; }
document.getElementById('stats').textContent =
  recs.length+' Einträge · Teams '+cnt.teams+' · Mail '+cnt.outlook+' · Kalender '+cnt.kalender+' · Kontakte '+cnt.kontakte;

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function toks(q){return q.toLowerCase().split(/\s+/).filter(Boolean);}
function allIn(hay,t){hay=(hay||'').toLowerCase();return t.every(x=>hay.includes(x));}
function reEsc(t){return t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}
function hi(s,t){let h=esc(s||'');for(const x of t){h=h.replace(new RegExp('('+reEsc(x)+')','ig'),'<mark>$1</mark>');}return h;}
function snippet(text,t){
  text=text||'';
  if(!t.length) return esc(text.slice(0,240))+(text.length>240?' …':'');
  const low=text.toLowerCase(); let idx=-1;
  for(const x of t){const i=low.indexOf(x); if(i>=0&&(idx<0||i<idx))idx=i;}
  let start=0,pre='',suf='';
  if(idx>90){start=idx-70;pre='… ';}
  let end=Math.min(text.length,start+260); if(end<text.length)suf=' …';
  return pre+hi(text.slice(start,end),t)+suf;
}
function dayStart(s){return s?new Date(s+'T00:00:00').getTime()/1000:null;}
function dayEnd(s){return s?new Date(s+'T23:59:59').getTime()/1000:null;}

function render(){
  const q=qEl.value.trim();
  const pq=personEl.value.trim().toLowerCase();
  const fromTs=dayStart(fromEl.value), toTs=dayEnd(toEl.value);
  const active = q||pq||fromEl.value||toEl.value||src!=='all';
  if(!active){ out.innerHTML='<p class="hint">Person, Datum, Inhalt oder Quelle wählen…</p>'; document.getElementById('summary').textContent=''; return; }
  const ct=toks(q);
  const LIMIT=500;
  let shown=0,total=0,frag='';
  for(const r of recs){
    if(src!=='all' && r.src!==src) continue;
    if(pq && !(r.ppl||'').includes(pq)) continue;
    if(fromTs!==null){ if(r.ts===null || r.ts<fromTs) continue; }
    if(toTs!==null){ if(r.ts===null || r.ts>toTs) continue; }
    if(ct.length && !allIn((r.title||'')+' '+(r.x||''),ct)) continue;
    total++;
    if(shown<LIMIT){
      frag += '<div class="rec"><div class="rtop">'
            + '<span class="badge '+r.src+'">'+(LABEL[r.src]||r.src)+'</span>'
            + '<span class="who">'+esc(r.who)+'</span>'
            + '<span class="when">'+esc(r.d)+'</span></div>'
            + '<div class="title"><a href="'+r.p+'" target="_blank" rel="noopener">'+hi(r.title,ct)+'</a></div>'
            + '<div class="ctx">'+esc(r.ctx)+'</div>'
            + '<div class="snip">'+snippet(r.x,ct)+'</div></div>';
      shown++;
    }
  }
  document.getElementById('summary').textContent = total+' Treffer'+(total>shown?(' (zeige '+shown+')'):'');
  let extra = total>shown ? '<div class="more">… '+(total-shown)+' weitere – Filter verfeinern</div>' : '';
  out.innerHTML = frag ? frag+extra : '<p class="hint">Keine Treffer.</p>';
}

let timer;
function debounced(){clearTimeout(timer);timer=setTimeout(render,120);}
qEl.addEventListener('input',debounced);
personEl.addEventListener('input',debounced);
fromEl.addEventListener('change',render);
toEl.addEventListener('change',render);
document.querySelectorAll('#srcChips .chip').forEach(ch=>ch.addEventListener('click',()=>{
  document.querySelectorAll('#srcChips .chip').forEach(x=>x.classList.remove('on'));
  ch.classList.add('on'); src=ch.dataset.src; render();
}));

/* ===================== Ansichten ===================== */
const VIEWS=['search','cal','book'];
function setView(v){
  VIEWS.forEach(x=>{
    document.getElementById('view-'+x).hidden = x!==v;
    document.getElementById('bar-'+x).hidden = x!==v;
  });
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on', t.dataset.view===v));
  document.querySelector('main').classList.toggle('wide', v!=='search');
  if(v==='search') personEl.focus();
  if(v==='cal') drawCal();
  if(v==='book'){ drawBook(); }
}
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>setView(t.dataset.view)));

/* ===================== Kalender ===================== */
const DAYMS=86400000;
const WD=['Mo','Di','Mi','Do','Fr','Sa','So'];
const MON=['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember'];
const STL={confirmed:'Bestätigt', tentative:'Vorläufig', cancelled:'Abgesagt',
           deleted:'Gelöscht – aus Absagemail rekonstruiert',
           gone:'Nicht mehr im Kalender – aus Mail rekonstruiert'};
const events = recs.filter(r=>r.src==='kalender' && r.ts!=null);

function dkey(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
function midnight(ms){const d=new Date(ms); d.setHours(0,0,0,0); return d;}
function addDays(d,n){const x=new Date(d); x.setDate(x.getDate()+n); return x;}
function startOfWeek(d){return addDays(midnight(d.getTime()), -((d.getDay()+6)%7));}
function hhmm(d){return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}
function isoWeek(d){
  const t=midnight(d.getTime()); t.setDate(t.getDate()+3-((t.getDay()+6)%7));
  const w1=new Date(t.getFullYear(),0,4);
  return 1+Math.round(((t-w1)/DAYMS-3+((w1.getDay()+6)%7))/7);
}

// Termine auf Tage verteilen (mehrtägige Termine erscheinen an jedem Tag)
const byDay=new Map();
for(const r of events){
  const s=midnight(r.ts*1000);
  let endMs=(r.te!=null?r.te:r.ts)*1000;
  if(r.ad) endMs-=DAYMS;                      // DTEND ist bei Ganztags-Terminen exklusiv
  const e=midnight(Math.max(endMs,r.ts*1000));
  for(let d=new Date(s), n=0; d<=e && n<366; d=addDays(d,1), n++){
    const k=dkey(d);
    if(!byDay.has(k)) byDay.set(k,[]);
    byDay.get(k).push(r);
  }
}
for(const list of byDay.values()) list.sort((a,b)=>(a.ad?0:1)-(b.ad?0:1)||a.ts-b.ts);

let calMode='week';
let cursor=new Date();
// Archiv liegt meist in der Vergangenheit: auf den jüngsten Termin springen
if(events.length){
  const last=events.reduce((m,r)=>r.ts>m?r.ts:m,-Infinity);
  if(last*1000 < midnight(Date.now()).getTime()) cursor=new Date(last*1000);
}

function evTime(r){
  if(r.ad) return 'ganztägig';
  let s=hhmm(new Date(r.ts*1000));
  if(r.te!=null && r.te>r.ts) s+='–'+hhmm(new Date(r.te*1000));
  return s;
}
function evHtml(r){
  const st=STL[r.st]?r.st:'confirmed';
  const tip=[r.title, STL[st], r.d, r.loc?('Ort: '+r.loc):'', r.who?('Organisator: '+r.who):'',
             (r.att&&r.att.length)?('Teilnehmer: '+r.att.join(', ')):'', r.ctx].filter(Boolean).join('\n');
  return '<a class="ev '+st+'" href="'+r.p+'" target="_blank" rel="noopener" title="'+esc(tip)+'">'
       + '<span class="evt">'+esc(evTime(r))+'</span> '+esc(r.title)+'</a>';
}
function dayCell(d, extraCls){
  const k=dkey(d), list=byDay.get(k)||[];
  const today = k===dkey(new Date()) ? ' today' : '';
  return '<div class="day'+(extraCls||'')+today+'">'
       + '<div class="dnum"><b>'+d.getDate()+'</b><span class="wd">'+WD[(d.getDay()+6)%7]+'</span></div>'
       + (list.length?list.map(evHtml).join(''):'')
       + '</div>';
}

/* Nur aus Mails rekonstruierte Termine – eigene Liste statt Kalenderraster */
const REBUILT=events.filter(r=>r.st==='deleted'||r.st==='gone');
let rbSt='all';
function rbRow(r){
  const st=r.st, d=new Date(r.ts*1000);
  return '<a class="rbrow '+st+'" href="'+r.p+'" target="_blank" rel="noopener" title="'+esc(r.ctx)+'">'
       + '<span class="rbdate">'+WD[(d.getDay()+6)%7]+' '+esc(r.d)+'</span>'
       + '<span class="rbstate">'+(st==='deleted'?'Gelöscht':'Nicht im Kalender')+'</span>'
       + '<span class="rbtitle">'+esc(r.title)+'</span>'
       + '<span class="rbwho">'+esc(r.who)+'</span></a>';
}
function rbFrame(){
  const nDel=REBUILT.filter(r=>r.st==='deleted').length;
  const box=document.getElementById('view-cal');
  box.innerHTML=
      '<p class="rbnote">Termine, die nicht (mehr) im Kalenderexport stehen und allein aus '
    + 'Einladungs-, Antwort- oder Absagemails wiederhergestellt wurden. '
    + '„Gelöscht“ heißt: es liegt eine Absagemail vor. Klick öffnet die Quellmail.</p>'
    + '<div class="rbbar">'
    + '<span class="chip" data-rb="all">Alle ('+REBUILT.length+')</span>'
    + '<span class="chip" data-rb="deleted">Gelöscht ('+nDel+')</span>'
    + '<span class="chip" data-rb="gone">Nicht im Kalender ('+(REBUILT.length-nDel)+')</span>'
    + '<input id="rbQ" type="search" placeholder="Titel, Person oder Inhalt…" autocomplete="off">'
    + '<span class="rbcount"></span></div><div id="tslist"></div>';
  box.querySelector('#rbQ').addEventListener('input',()=>{
    clearTimeout(timer); timer=setTimeout(rbList,160);
  });
  box.querySelectorAll('[data-rb]').forEach(ch=>ch.addEventListener('click',()=>{
    rbSt=ch.dataset.rb; rbList();
  }));
}
function rbList(){
  const t=toks(document.getElementById('rbQ').value.trim());
  const hits=REBUILT.filter(r=>(rbSt==='all'||r.st===rbSt)
                            && (!t.length||allIn(r.title+' '+(r.ppl||'')+' '+(r.x||''),t)));
  document.querySelectorAll('.rbbar .chip').forEach(c=>c.classList.toggle('on',c.dataset.rb===rbSt));
  document.querySelector('.rbcount').textContent=hits.length+' Treffer';
  let h='', monat=null;
  for(const r of hits){
    const d=new Date(r.ts*1000), m=MON[d.getMonth()]+' '+d.getFullYear();
    if(m!==monat){ h+='<div class="rbmonth">'+m+'</div>'; monat=m; }
    h+=rbRow(r);
  }
  document.getElementById('tslist').innerHTML = h ||
    (REBUILT.length ? '<p class="hint">Keine Treffer.</p>'
     : '<p class="hint">Keine rekonstruierten Termine – im Postfach lagen keine passenden '
       + 'Einladungs- oder Absagemails.</p>');
}
function drawRebuilt(){
  if(!document.querySelector('#view-cal .rbbar')) rbFrame();
  rbList();
}

function drawCal(){
  const box=document.getElementById('view-cal');
  document.getElementById('calNav').hidden = calMode==='rebuilt';
  document.getElementById('calLegend').hidden = calMode==='rebuilt';   // Zeilen sind beschriftet
  if(calMode==='rebuilt'){ drawRebuilt(); return; }
  if(!events.length){ document.getElementById('calTitle').textContent='';
    box.innerHTML='<p class="hint">Keine Termine im Export.</p>'; return; }
  let head='<div class="grid '+(calMode==='week'?'wk':'mo')+' dowrow" style="margin-bottom:2px">'
         + WD.map(w=>'<div class="dow">'+w+'</div>').join('')+'</div>';
  let cells='';
  if(calMode==='week'){
    const mon=startOfWeek(cursor), sun=addDays(mon,6);
    for(let i=0;i<7;i++) cells+=dayCell(addDays(mon,i));
    document.getElementById('calTitle').textContent =
      'KW '+isoWeek(mon)+' · '+mon.getDate()+'. '+MON[mon.getMonth()]+' – '+sun.getDate()+'. '+MON[sun.getMonth()]+' '+sun.getFullYear();
  }else{
    const first=new Date(cursor.getFullYear(),cursor.getMonth(),1);
    const start=startOfWeek(first);
    const lastDay=new Date(cursor.getFullYear(),cursor.getMonth()+1,0);
    const weeks=Math.round((startOfWeek(lastDay)-start)/DAYMS/7)+1;
    for(let i=0;i<weeks*7;i++){
      const d=addDays(start,i);
      cells+=dayCell(d, d.getMonth()!==cursor.getMonth()?' out':'');
    }
    document.getElementById('calTitle').textContent = MON[cursor.getMonth()]+' '+cursor.getFullYear();
  }
  box.innerHTML = head+'<div class="grid '+(calMode==='week'?'wk':'mo')+'">'+cells+'</div>';
}
document.getElementById('calPrev').addEventListener('click',()=>{
  cursor = calMode==='week' ? addDays(cursor,-7) : new Date(cursor.getFullYear(),cursor.getMonth()-1,1);
  drawCal();
});
document.getElementById('calNext').addEventListener('click',()=>{
  cursor = calMode==='week' ? addDays(cursor,7) : new Date(cursor.getFullYear(),cursor.getMonth()+1,1);
  drawCal();
});
document.getElementById('calToday').addEventListener('click',()=>{ cursor=new Date(); drawCal(); });
document.querySelectorAll('#calChips .chip').forEach(ch=>ch.addEventListener('click',()=>{
  document.querySelectorAll('#calChips .chip').forEach(x=>x.classList.remove('on'));
  ch.classList.add('on'); calMode=ch.dataset.mode; drawCal();
}));

/* ===================== Adressbuch ===================== */
const contacts = recs.filter(r=>r.src==='kontakte')
  .sort((a,b)=>(a.title||'').localeCompare(b.title||'','de',{sensitivity:'base'}));
const bookQEl=document.getElementById('bookQ');

function telHref(t){return 'tel:'+(t||'').replace(/[^\d+]/g,'');}
function cardHtml(r){
  const sub=[r.role,r.org].filter(Boolean).join(' · ');
  let h='<div class="card"><div class="cname"><a href="'+r.p+'" target="_blank" rel="noopener">'+esc(r.title)+'</a></div>';
  if(sub) h+='<div class="crole">'+esc(sub)+'</div>';
  for(const e of (r.em||[])) h+='<div class="cline"><span>✉</span><a href="mailto:'+esc(e)+'">'+esc(e)+'</a></div>';
  for(const t of (r.tel||[])) h+='<div class="cline"><span>☎</span><a href="'+esc(telHref(t))+'">'+esc(t)+'</a></div>';
  return h+'</div>';
}
function drawBook(){
  const box=document.getElementById('view-book');
  if(!contacts.length){ document.getElementById('bookStats').textContent='';
    box.innerHTML='<p class="hint">Keine Kontakte im Export.</p>'; return; }
  const t=toks(bookQEl.value.trim());
  const hits=contacts.filter(r=>!t.length ||
    allIn([r.title,r.org,r.role,(r.em||[]).join(' '),(r.tel||[]).join(' ')].join(' '),t));
  document.getElementById('bookStats').textContent =
    hits.length+' von '+contacts.length+' Kontakten';
  if(!hits.length){ box.innerHTML='<p class="hint">Keine Kontakte gefunden.</p>'; return; }
  let h='', letter=null;
  for(const r of hits){
    const first=(r.title||'#').trim().charAt(0).toUpperCase();
    const L=/[A-ZÄÖÜ]/.test(first)?first:'#';
    if(L!==letter){ h+=(letter!==null?'</div>':'')+'<div class="letter">'+L+'</div><div class="cards">'; letter=L; }
    h+=cardHtml(r);
  }
  box.innerHTML=h+'</div>';
}
bookQEl.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(drawBook,120);});

personEl.focus();
</script>
</body></html>
"""


def write_calendar_json(outlook_dir, ziel, reconstruct=True):
    """Kalenderdaten nach `ziel` schreiben (atomar). Liefert die Zählungen."""
    daten = collect_calendar_data(outlook_dir, reconstruct=reconstruct)
    ziel = Path(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(ziel.name + ".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ziel)
    return daten["counts"]


def _hilfe_gewuenscht(argv):
    """-h/--help beantworten, statt einen Ordner dieses Namens anzulegen.

    Diese Skripte deuten das erste freie Argument als Ausgabeordner. Ohne diese
    Abfrage legte `python3 combined_search.py --help` brav einen Ordner namens „--help“ an
    und begann zu exportieren – einmal passiert und dann sogar eingecheckt.
    """
    return any(a in ("-h", "--help", "-help") for a in argv)


def main():
    if _hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__.strip())
        return

    args = sys.argv[1:]
    output = None
    kalender_json = None
    # Vorgabe aus app_config.json, damit der Einzelaufruf dieselbe Einstellung
    # trägt wie die App. --no-reconstruct schlägt sie auf der Zeile.
    reconstruct = settings.flag("CALENDAR_RECONSTRUCT", "calendar_reconstruct", True)
    pos = []
    i = 0
    while i < len(args):
        if args[i] in ("-o", "--out") and i + 1 < len(args):
            output = args[i + 1]
            i += 2
        elif args[i] == "--json" and i + 1 < len(args):
            kalender_json = args[i + 1]
            i += 2
        elif args[i] == "--no-reconstruct":
            reconstruct = False
            i += 1
        else:
            pos.append(args[i])
            i += 1
    teams_dir = pos[0] if len(pos) > 0 else settings.value("teams_dir", "teams_export")
    outlook_dir = pos[1] if len(pos) > 1 else settings.value("outlook_dir", "outlook_export")
    hinweis = settings.report()
    if hinweis:
        print(hinweis)

    if kalender_json:
        print(f"Kalenderdaten → {kalender_json}"
              + ("" if reconstruct else " (ohne Wiederherstellung aus Mails)"))
        c = write_calendar_json(outlook_dir, kalender_json, reconstruct=reconstruct)
        print(f"Fertig. {c['kalender']} Termine, {c['rekonstruiert']} aus Mails "
              f"rekonstruiert, {c['kontakte']} Kontakte."
              + (f" {c['abgesagt_markiert']} nachträglich als abgesagt markiert."
                 if c["abgesagt_markiert"] else "")
              + (f" {c['doppel_verworfen']} als Doppel verworfen."
                 if c["doppel_verworfen"] else ""))
        return

    tp, op = Path(teams_dir), Path(outlook_dir)
    if not tp.is_dir() and not op.is_dir():
        raise SystemExit(f"Weder '{teams_dir}' noch '{outlook_dir}' gefunden – nichts zu tun.")

    if output:
        outp = Path(output)
    else:
        existing = [p.resolve() for p in (tp, op) if p.is_dir()]
        if len(existing) == 2:
            try:
                base = Path(os.path.commonpath(existing))
            except ValueError:
                base = Path.cwd()
            if not base.is_dir():
                base = base.parent
        else:
            base = existing[0].parent
        outp = base / "combined_search.html"

    print(f"Erzeuge kombinierte Suche → {outp}")
    out, counts = build(teams_dir, outlook_dir, outp)
    total = sum(counts.values())
    print(f"\nFertig. {total} Einträge gesamt (Teams {counts['teams']}, Mail {counts['outlook']}, "
          f"Kalender {counts['kalender']} + {counts['rekonstruiert']} rekonstruiert, "
          f"Kontakte {counts['kontakte']}).")
    print(f"Suche öffnen: {out.resolve()}")


if __name__ == "__main__":
    main()
