#!/usr/bin/env python3
"""
Kalender- und Kontaktauswertung des Outlook-Exports als JSON.

Liest die .ics- und .vcf-Dateien des Exports und schreibt Termine, Kontakte und
rekonstruierte Termine in eine JSON-Datei. app.py stellt daraus Kalender und
Adressbuch dar – die Rekonstruktion gibt es damit einmal im Projekt und nicht
zweimal leicht anders.

Gelöschte Termine werden aus den Mails rekonstruiert: Einladungen, Antworten und
Absagen tragen den kompletten Termin samt UID im text/calendar-Teil. Fehlt diese
UID im Kalenderexport, taucht der Termin trotzdem im Kalender auf – als "gelöscht"
(wenn eine Absage vorliegt) bzw. "nicht im Kalender" (nur eingeladen/zugesagt).
Damit dabei keine Geisterkopien entstehen, werden in Exchange-IDs eingebettete
Fremd-UIDs ausgepackt (siehe norm_uid) und Treffer verworfen, deren Titel und
Startminute schon im Kalender stehen.

Nur Standardbibliothek – keine Installation nötig.

    python3 combined_search.py [outlook-ordner] --json ziel.json

--no-reconstruct lässt die Wiederherstellung gelöschter Termine aus Mails weg.
Sie ist der mit Abstand teuerste Teil – jede .eml wird gelesen, bei einem
großen Postfach Minuten – und für Termine und Kontakte allein nicht nötig.

Bis 5.3 erzeugte dieses Skript zusätzlich eine eigenständige HTML-Suchseite
über beide Exporte; die App bot sie seit 5.2 nicht mehr an, und auf einem
gewachsenen Archiv wurde sie dreistellig viele Megabyte groß. Wer sie sucht,
findet sie in der Git-Historie.
"""

import os
import sys
import re
import json
import email
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from datetime import datetime, UTC
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote, unquote

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


# ===========================================================================
# Outlook: Einladungs-/Absagemails (.eml mit text/calendar) einsammeln
# ===========================================================================
def hdr(msg, name):
    v = msg[name]
    return str(v).strip() if v is not None else ""


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


def read_outlook(root, out_dir, invites):
    """Termin-Anhänge (text/calendar) aus allen .eml einsammeln.

    Nur dafür werden die Mails hier gelesen – ihre Inhalte indexiert corpus.py.
    """
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
        method, ical = mail_ical(msg)
        if not ical:
            continue
        m2, evs = parse_vevents(ical)
        meth = method or m2
        # Antwortmails führen keinen ORGANIZER, nur den antwortenden
        # ATTENDEE – der Organisator ist ihr Empfänger. Einladungen und
        # Absagen kommen umgekehrt vom Organisator selbst.
        fn, fe = addr_people(msg, "from")
        hn, hm = addr_people(msg, "to") if meth in ("REPLY", "COUNTER") else (fn, fe)
        hint = (hn[0] if hn else "", hm[0] if hm else "")
        raw_date = hdr(msg, "date")
        ts, disp = None, raw_date
        try:
            dt = email.utils.parsedate_to_datetime(raw_date)
            if dt is not None:
                ts = dt.timestamp()
                disp = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        for ev in evs:
            if ev["uid"]:
                invites.append({"method": meth, "ev": ev, "org_hint": hint,
                                "href": link(p, out_dir), "mts": ts, "md": disp})


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


def read_calendar(root, out_dir):
    recs = []
    for p in sorted(root.rglob("*.ics")):
        _, events = parse_vevents(p.read_text(encoding="utf-8", errors="replace"))
        if not events:
            continue
        ev = events[0]                       # der Export legt einen Termin je Datei ab
        segs = p.relative_to(root).as_posix().split("/")
        cal = segs[1] if len(segs) >= 3 and segs[0] == "kalender" else "Kalender"
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


def read_contacts(root, out_dir):
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

    Die Pfade kommen als `root` + `rel` (unkodiert) statt als fertiger Link:
    die App liefert die Dateien über ihre eigene /source-Route aus.
    Beschreibungstexte werden gekürzt (`text_cap`) – im Kalender stehen sie nur
    im Tooltip und in der Suche über die rekonstruierten Termine, ungekürzt
    blähen tausende Termine die Antwort auf.
    """
    root = Path(outlook_dir).resolve()
    if not root.is_dir():
        raise SystemExit(f"Outlook-Export nicht gefunden: {outlook_dir}")
    invites = []
    ghosts, marked, dupes = [], 0, 0
    # out_dir = root: link() liefert dann Pfade relativ zum Export-Stamm, genau
    # das, was die /source-Route der App erwartet.
    #
    # Das Lesen aller .eml ist der teure Teil – bei einem großen Postfach
    # Minuten – und geschieht ausschließlich für die Einladungen. Wer den
    # Kalender nur wegen der Termine oder der Kontakte aufbaut, zahlt das
    # sonst mit, ohne etwas davon zu haben.
    if reconstruct:
        read_outlook(root, root, invites)
    cal = read_calendar(root, root)
    if reconstruct:
        ghosts, marked, dupes = reconstruct_events(invites, cal)
    contacts = read_contacts(root, root)

    recs = cal + ghosts + contacts
    for r in recs:
        r["root"] = "outlook"
        r["rel"] = unquote(r.pop("p", ""))
        r.pop("uid", None)          # nur fürs Zuordnen oben gebraucht, knapp 1 MB
        # Personenliste und Beschreibung braucht nur die Suche über die
        # rekonstruierten Termine. Über alle Termine hinweg sind das zwei
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
    kalender_json = None
    # Vorgabe aus app_config.json, damit der Einzelaufruf dieselbe Einstellung
    # trägt wie die App. --no-reconstruct schlägt sie auf der Zeile.
    reconstruct = settings.flag("CALENDAR_RECONSTRUCT", "calendar_reconstruct", True)
    pos = []
    i = 0
    while i < len(args):
        if args[i] == "--json" and i + 1 < len(args):
            kalender_json = args[i + 1]
            i += 2
        elif args[i] == "--no-reconstruct":
            reconstruct = False
            i += 1
        else:
            pos.append(args[i])
            i += 1
    # Das letzte freie Argument ist der Outlook-Export: bis 5.3 kam davor noch
    # der Teams-Ordner (für die HTML-Seite) – alte Aufrufe laufen so weiter.
    outlook_dir = pos[-1] if pos else settings.value("outlook_dir", "outlook_export")
    hinweis = settings.report()
    if hinweis:
        print(hinweis)

    if not kalender_json:
        raise SystemExit("Nutzung: python3 combined_search.py [outlook-ordner] "
                         "--json ziel.json [--no-reconstruct]")

    print(f"Kalenderdaten → {kalender_json}"
          + ("" if reconstruct else " (ohne Wiederherstellung aus Mails)"))
    c = write_calendar_json(outlook_dir, kalender_json, reconstruct=reconstruct)
    print(f"Fertig. {c['kalender']} Termine, {c['rekonstruiert']} aus Mails "
          f"rekonstruiert, {c['kontakte']} Kontakte."
          + (f" {c['abgesagt_markiert']} nachträglich als abgesagt markiert."
             if c["abgesagt_markiert"] else "")
          + (f" {c['doppel_verworfen']} als Doppel verworfen."
             if c["doppel_verworfen"] else ""))


if __name__ == "__main__":
    main()
