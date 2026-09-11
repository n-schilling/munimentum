#!/usr/bin/env python3
"""
corpus.py – shared data foundation for the local RAG search.

Reads the Teams export (HTML) and the Outlook export (.eml) into uniform
records and splits long texts into overlapping chunks. Used by rag_index.py
(embeddings). Standard library only.
"""

import json
import os
import base64
import binascii
import re
import email
import html as html_lib
import hashlib
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from datetime import datetime, UTC
from pathlib import Path
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from functools import partial
from concurrent.futures import ProcessPoolExecutor, BrokenExecutor

import export_util

CATS = {"1on1", "group", "meeting", "channels"}
_BLOCK = {"br", "p", "div", "li", "tr"}
SAFETY_CAP = 500_000   # cap absurdly long individual texts (before chunking)

# From how many files onward the process pool pays off (amortizes the spawn
# overhead).
_PAR_THRESHOLD = 200

# Why the pool did not come about – None as long as everything went normally.
# This file prints nothing itself (it is a library, see tests/
# test_projekt.py); whoever uses it reads the note and reports it. rag_index
# does so after loading.
POOL_FEHLER = None


def _pmap(func, files, root_dir):
    """func(p_str, root_str) over all files – parallel across all CPU cores.

    Parsing the exports is pure CPU work and, single-threaded, the slowest
    part before the (GPU-bound) embedding. With many files, spread it over
    all cores; with few, run serially (spawning does not pay off).

    If the pool fails, work continues serially. That takes longer, but it is
    still the job to be done here – an index that never gets built because a
    worker process could not start helps nobody. The cause is left in
    POOL_FEHLER afterwards.
    """
    global POOL_FEHLER
    paths = [str(p) for p in files]

    def seriell():
        return [func(p, root_dir) for p in paths]

    if len(paths) < _PAR_THRESHOLD or POOL_FEHLER:
        return seriell()
    workers = os.cpu_count() or 4
    chunksize = max(1, len(paths) // (workers * 8))
    try:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(partial(func, root_str=root_dir), paths,
                               chunksize=chunksize))
    except (BrokenExecutor, OSError) as e:
        # BrokenExecutor: a worker process died instead of answering.
        # OSError: it could not be started at all (out of handles, blocked
        # by security software, no /dev/shm in the container).
        POOL_FEHLER = f"{type(e).__name__}: {e}"
        return seriell()


# --------------------------------------------------------------------------
# Teams: parse the exported conversation HTML
# --------------------------------------------------------------------------
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
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d %H:%M").timestamp()
    except Exception:
        return None


def _teams_file(p_str, root_str):
    p, root = Path(p_str), Path(root_str)
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
    # The storage folder, not a display name: ctx is the column the search
    # filters on, and a path there always means everything below it too.
    # "channels" thus matches every channel without the picker needing one
    # entry per channel – and "1on1" exactly the 1:1 chats.
    ctx = rel.rsplit("/", 1)[0] if "/" in rel else cat
    out = []
    for i, m in enumerate(msgs):
        out.append({
            "uid": f"teams:{rel}:{i}", "src": "teams", "root": "teams", "rel": rel,
            "thread": f"chat:{rel}",
            "who": m["n"] or "(unbekannt)", "ppl": (m["n"] + " " + title).lower(),
            "ts": parse_local(m["t"]), "date": m["t"], "title": title, "ctx": ctx,
            "text": (m["x"] or "")[:SAFETY_CAP],
        })
    return out


def load_teams(root_dir, nur=None):
    root = Path(root_dir)
    files = _nur(_dateien_teams(root), root, nur)
    recs = []
    for out in _pmap(_teams_file, files, root_dir):
        recs.extend(out)
    return recs


# --------------------------------------------------------------------------
# Outlook: .eml parsen
# --------------------------------------------------------------------------
def strip_html(s):
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</tr>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html_lib.unescape(s)


def collapse(s, cap=SAFETY_CAP):
    return " ".join((s or "").split())[:cap]


# In mail threads the complete history is quoted again in every reply – in
# this corpus often >80 % of the text volume. That bloats the index (slow
# embedding) and hurts retrieval (duplicate noise). We cut at the first quote
# boundary before chunking and keep only the new message. Conservative: only
# on unambiguous Outlook/mail-client markers.
_QUOTE_CUTS = [
    re.compile(r"_{25,}"),                                  # Outlook divider
    re.compile(r"(?m)^\s*_{10,}\s*$"),                      # divider on a line of its own
    re.compile(r"-{3,}\s*(Original Message|Ursprüngliche Nachricht)\s*-{3,}", re.I),
    re.compile(r"(?m)^\s*(From|Von):\s.*(?:\n.*){0,4}?^\s*(Sent|Gesendet|Date):\s", re.I),
    re.compile(r"(?im)^[ \t>]*On\b.{0,300}?\bwrote:\s*$", re.S),
    re.compile(r"(?im)^[ \t>]*Am\b.{0,300}?\bschrieb\b.{0,120}?:\s*$", re.S),
]
_SIG_CUTS = [
    re.compile(r"(?m)^-- ?$"),                              # RFC 3676 signature separator
    re.compile(r"(?im)^\s*Sent from (my |Outlook).*$"),
    re.compile(r"(?im)^\s*Von meinem (iPhone|iPad|Samsung|Android).*$"),
    re.compile(r"(?im)^\s*Get Outlook for (iOS|Android).*$"),
]


def strip_quoted(text):
    """Cut off quoted thread history and signature, keep the new message."""
    if not text:
        return text
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    cut = len(t)
    for rx in _QUOTE_CUTS:
        m = rx.search(t)
        if m and m.start() < cut:
            cut = m.start()
    head = t[:cut]
    for rx in _SIG_CUTS:
        m = rx.search(head)
        if m and m.start() > 0:
            head = head[:m.start()]
    return re.sub(r"(?m)^[ \t]*>.*$", "", head)            # remaining quote lines


# --------------------------------------------------------------------------
# Threads: which mails belong together
#
# Everything for this is already in the .eml files – no re-export needed.
# Measured on a real mailbox (sample of 400 out of about 45,000): Thread-Index 89 %,
# References/In-Reply-To 58 %, Message-ID 100 %. Hence a cascade that starts
# with the most precise field and in the end assigns every mail at least to
# itself – a thread of one message is correct, just boring.
# --------------------------------------------------------------------------
def thread_key(msg):
    """Stable id of the conversation this mail belongs to."""
    roh = hdr(msg, "thread-index")
    if roh:
        try:
            # Exchange: the first 22 bytes are the conversation id; each
            # reply appends another 5 bytes. Only the head counts.
            kopf = base64.b64decode(roh + "===", validate=False)[:22]
            if len(kopf) == 22:
                return "tix:" + kopf.hex()
        except (ValueError, binascii.Error):
            pass
    for name in ("references", "in-reply-to"):
        wert = hdr(msg, name)
        if wert:
            # The first entry in References is the start of the conversation.
            treffer = re.findall(r"<[^>]+>", wert)
            if treffer:
                return "mid:" + treffer[0].strip("<>").lower()
    eigene = hdr(msg, "message-id")
    return "mid:" + eigene.strip("<>").lower() if eigene else ""


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
    text = strip_quoted(text)
    return collapse(text)


# Characters that belong in no filename – and the leading dot, so an
# attachment cannot turn into a hidden ".profile".
_UNGUT = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sicherer_dateiname(name, ersatz="anhang"):
    """Turn the name in the attachment into a filename that can be trusted."""
    # Cut off the path first, then replace the characters. The other way
    # around, "../../.ssh/id_rsa" becomes "_.._.ssh_id_rsa" – the path is
    # harmless then, but still sits complete in the name.
    roh = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    roh = _UNGUT.sub("_", roh).strip(". ")
    return roh[:150] or ersatz


def anhaenge(msg):
    """Names of a mail's attachments, in order of appearance.

    Without them, the contract in the attachment sits in the archive but
    cannot be found by a single word. The names alone already carry most of
    the value: "Vertrag_Musterkunde.pdf" is how one searches for it anyway.

    Real attachments only: inline images (signature logos!) carry names like
    image001.png and would flood the search with noise.
    """
    namen = []
    try:
        teile = list(msg.walk())
    except Exception:
        return namen
    for p in teile:
        if p.get_content_disposition() != "attachment":
            continue
        roh = p.get_filename()
        if not roh:
            continue
        try:
            name = str(roh)
        except Exception:
            continue
        name = sicherer_dateiname(name)
        if name and name not in namen:
            namen.append(name)
    return namen


def endungen(att):
    """The file types behind an attachment list – deduped, lowercase, sorted.

    "Vertrag_Musterkunde.pdf Anlage.XLSX" becomes "pdf xlsx". Filtering on
    it happens in SQL and not via the full text: semantic search and the AI
    answer narrow down there, and a filter that only worked in the text
    search would be silently ineffective in two of the three search kinds.

    Only what looks like an extension: letters and digits, at most eight
    characters. A name like "Bericht.2024-final" has none.
    """
    gefunden = set()
    for name in (att or "").split(" "):
        stueck = name.rsplit(".", 1)
        if len(stueck) == 2 and stueck[1] and stueck[1].isalnum() and len(stueck[1]) <= 8:
            gefunden.add(stueck[1].lower())
    return " ".join(sorted(gefunden))


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


def _outlook_file(p_str, root_str):
    p, root = Path(p_str), Path(root_str)
    try:
        with open(p, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
    except Exception:
        return None
    fn, fe = addr_people(msg, "from")
    tn, te = addr_people(msg, "to", "cc")
    who = (fn[0] if fn else (fe[0] if fe else "")) or "(unbekannt)"
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
    folder = rel.rsplit("/", 1)[0] if "/" in rel else "(Stamm)"
    return {
        "uid": f"outlook:{rel}:0", "src": "outlook", "root": "outlook", "rel": rel,
        "thread": thread_key(msg),
        "att": " ".join(anhaenge(msg)),
        "who": who, "ppl": " ".join(fn + fe + tn + te).lower(),
        "ts": ts, "date": disp, "title": hdr(msg, "subject") or "(kein Betreff)",
        "ctx": folder, "text": extract_body(msg),
    }


def lies_verschwunden(root_dir):
    """rel -> time since which the mail is no longer in the mailbox.

    Written by the exports into the folder's state.db. The file itself
    stays put; this only records that it is missing at the source – that
    is the difference between a copy and an archive.
    """
    import state_db
    return state_db.StateDb(root_dir).verschwunden_lesen()


def load_outlook(root_dir, nur=None):
    root = Path(root_dir)
    files = _nur(_dateien_outlook(root), root, nur)
    recs = [r for r in _pmap(_outlook_file, files, root_dir) if r is not None]
    weg = grabsteine("outlook", root_dir)
    if weg:
        for r in recs:
            wann = weg.get(r["rel"])
            if wann:
                r["gone"] = wann
    return recs


# --------------------------------------------------------------------------
# Calendar (.ics) and contacts (.vcf) – they live in the Outlook export
# --------------------------------------------------------------------------
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


# Exchange writes Windows time zone names into invitation mails instead of
# IANA names. Without a mapping, appointments from other time zones land in
# the calendar shifted by that difference. The most common names suffice –
# everything else falls back to local time.
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
    """TZID (Windows or IANA name) -> tzinfo, else None (= local time)."""
    tzid = (tzid or "").strip().strip('"')
    if not tzid:
        return None
    if tzid not in _ZONES:
        try:
            _ZONES[tzid] = ZoneInfo(WIN_TZ.get(tzid, tzid))
        except Exception:
            # unknown name or missing time zone data (Windows without tzdata)
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


def _calendar_file(p_str, root_str):
    p, root = Path(p_str), Path(root_str)
    summary = location = description = org_cn = org_mail = dtstart = tzstart = ""
    dateonly = False
    att_names, att_mails = [], []
    for line in _unfold(p.read_text(encoding="utf-8", errors="replace")):
        name, params, value = _prop(line)
        if not name:
            continue
        if name == "SUMMARY":
            summary = _unescape(value)
        elif name == "LOCATION":
            location = _unescape(value)
        elif name == "DESCRIPTION":
            description = _unescape(value)
        elif name == "DTSTART":
            dtstart = value.strip()
            dateonly = "VALUE=DATE" in (params or "").upper()
            tzstart = _pval(params, "TZID")
        elif name == "ORGANIZER":
            org_cn, org_mail = _pval(params, "CN"), _demail(value)
        elif name == "ATTENDEE":
            cn, mail = _pval(params, "CN"), _demail(value)
            if cn:
                att_names.append(cn)
            if mail:
                att_mails.append(mail)
    ts, disp = _ics_when(dtstart, dateonly, tzstart)
    rel = p.relative_to(root).as_posix()
    segs = rel.split("/")
    # The folder path, not a display name: ctx is the column the folder
    # search filters on. As "kalender/Arbeit" the calendar sits there next
    # to "E-Mail/Kunden" – the same choice as in the export, with the same
    # path it also occupies on disk.
    cal = "/".join(segs[:2]) if len(segs) >= 3 and segs[0] == "kalender" else "kalender"
    ppl = " ".join(x for x in ([org_cn, org_mail] + att_names + att_mails) if x).lower()
    text = ((f"Ort: {location}. " if location else "") + description).strip()
    return {
        "uid": f"kalender:{rel}:0", "src": "kalender", "root": "outlook", "rel": rel,
        "who": org_cn or org_mail or "(unbekannt)", "ppl": ppl,
        "ts": ts, "date": disp, "title": summary or "(kein Betreff)",
        "ctx": cal, "text": text[:SAFETY_CAP],
    }


def load_calendar(root_dir):
    root = Path(root_dir)
    files = sorted(root.rglob("*.ics"))
    return [r for r in _pmap(_calendar_file, files, root_dir) if r is not None]


def load_contacts(root_dir):
    recs = []
    root = Path(root_dir)
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
        rel = p.relative_to(root).as_posix()
        segs = rel.split("/")
        folder = segs[1] if len(segs) >= 3 and segs[0] == "kontakte" else ""
        text = " · ".join(x for x in ([org, title] + emails + tels + ([note] if note else [])) if x)
        recs.append({
            "uid": f"kontakte:{rel}:0", "src": "kontakte", "root": "outlook", "rel": rel,
            "who": org or title or "Kontakt", "ppl": " ".join([fn] + emails).lower(),
            "ts": None, "date": "", "title": fn,
            "ctx": f"kontakte/{folder}" if folder else "kontakte",
            "text": text[:SAFETY_CAP],
        })
    return recs


# --------------------------------------------------------------------------
# Merging + chunking
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# OneDrive: the mirrored files
#
# Stage one is deliberately the NAME, not the content: it costs nothing, is
# there at once and answers the most common question ("where was that offer
# again"). Opening a PDF is a different order of magnitude – measured on a
# real mirror, about an hour for extraction and embedding (see ROADMAP.md).
#
# To keep that retrofittable, `text` here is already the CONTENT FIELD and
# carries only the path for now. Whoever extracts later replaces exactly this
# value; layout, id and search filters stay as they are, and an old index
# does not become invalid, only poorer.
# --------------------------------------------------------------------------
ONEDRIVE_DIR = "Dateien"


def _datei_satz(p_str, root_str):
    p, root = Path(p_str), Path(root_str)
    rel = p.relative_to(root).as_posix()
    try:
        st = p.stat()
        ts, groesse = st.st_mtime, st.st_size
    except OSError:
        ts, groesse = None, 0
    ordner = rel.rsplit("/", 1)[0] if "/" in rel else ONEDRIVE_DIR
    # The path as text: that way the full-text search also matches on the
    # folder name, not only the filename. Two words, no noise.
    return {
        "uid": f"datei:{rel}:0", "src": "datei", "root": "onedrive",
        "rel": rel,
        "who": "", "ppl": "",
        "ts": ts,
        "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
        "title": p.name,
        "ctx": ordner,
        "att": p.name,          # same column as mail attachments: att:pdf finds both
        "text": rel.replace("/", " / "),
        "groesse": groesse,
    }


def load_onedrive(root_dir, nur=None):
    """One record per mirrored file – name and path, no content."""
    root = Path(root_dir)
    dateien = _nur(_dateien_onedrive(root), root, nur)
    recs = [r for r in _pmap(_datei_satz, dateien, str(root)) if r]
    weg = grabsteine("onedrive", root)     # the same reader as for the mailbox
    for r in recs:
        if r["rel"] in weg:
            r["gone"] = weg[r["rel"]]
    return recs


def load_sharepoint(root_dir, nur=None):
    """One record per mirrored SharePoint file – name and path, no content.

    The mirror keeps one folder per library (<site>/<library>/Dateien/…),
    each with its own tombstone file. All libraries feed ONE parallel pass –
    a pool per library would pay the spawn cost once per library, every
    index run – and the tombstone paths get their library prefix back.
    """
    root = Path(root_dir)
    if not root.is_dir():
        return []
    dateien = _nur(_dateien_sharepoint(root), root, nur)
    weg = grabsteine("sharepoint", root)
    recs = [r for r in _pmap(_datei_satz, dateien, str(root)) if r]
    for r in recs:
        r["root"] = "sharepoint"
        r["uid"] = "sharepoint:" + r["uid"].split(":", 1)[1]
        if r["rel"] in weg:
            r["gone"] = weg[r["rel"]]
    return recs


def _seiten_satz(p_str, root_str):
    p, root = Path(p_str), Path(root_str)
    rel = p.relative_to(root).as_posix()
    try:
        roh = p.read_text(encoding="utf-8", errors="replace")
        ts = p.stat().st_mtime
    except OSError:
        return None
    # Embedded data URIs are megabytes of base64 that never contain
    # searchable text – dropping them first makes the parse cheap.
    roh = re.sub(r'"data:[^"]*"', '""', roh)
    m = re.search(r"<title>(.*?)</title>", roh, re.S | re.I)
    titel = collapse(html_lib.unescape(m.group(1))) if m else p.stem
    return {
        "uid": f"pages:{rel}:0", "src": "pages", "root": "pages",
        "rel": rel, "who": "", "ppl": "", "ts": ts,
        "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
        "title": titel,
        "ctx": rel.rsplit("/", 1)[0] if "/" in rel else "",
        "text": collapse(strip_html(roh)),
    }


def load_pages(root_dir, nur=None):
    """One record per rendered site page – full text, straight from the HTML.

    Unlike the file mirrors, the content is right there: the pages export
    writes the text web parts into the file, so the page body goes into the
    index and the full-text search reads SharePoint pages like mail. The
    parse fans out like every sibling loader.
    """
    root = Path(root_dir)
    if not root.is_dir():
        return []
    weg = grabsteine("pages", root)
    dateien = _nur(_dateien_pages(root), root, nur)
    recs = [r for r in _pmap(_seiten_satz, dateien, str(root)) if r]
    for satz in recs:
        if satz["rel"] in weg:
            satz["gone"] = weg[satz["rel"]]
    return recs


def load_planner(root_dir):
    """One record per Planner task, straight from the per-plan state.db –
    title, description, checklist and the COMMENTS are the searchable text;
    the source file is the plan's board.html."""
    import state_db
    root = Path(root_dir)
    if not root.is_dir():
        return []
    recs = []
    for ordner in sorted(pf for pf in root.iterdir() if pf.is_dir()):
        db = state_db.StateDb(ordner)
        try:
            plan = json.loads(db.kv_lesen("plan") or "{}")
            eintraege = json.loads(db.kv_lesen("tasks") or "{}")
            namen = json.loads(db.kv_lesen("namen") or "{}")
        except ValueError:
            continue
        if not eintraege:
            continue
        rel = f"{ordner.name}/board.html"
        buckets = plan.get("buckets") or {}
        titel_plan = str(plan.get("titel") or ordner.name)
        for tid, e in eintraege.items():
            task = e.get("task") or {}
            det = e.get("details") or {}
            kommentare = e.get("kommentare") or []
            # Reference aliases like mail attachments: names searchable, and
            # the file type filter (att:pdf) also hits Planner cards.
            anhaenge = " ".join(
                str((ref or {}).get("alias") or "").replace(" ", "_")
                for ref in (det.get("references") or {}).values()).strip()
            # Assignees AND comment authors, GUIDs resolved via the export's
            # name cache – raw ids in the result list mean nothing to
            # anybody.
            zustaendig = sorted(namen.get(k, k)
                                for k in (task.get("assignments") or {}))
            leute = sorted({namen.get(k.get("wer") or "", k.get("wer") or "")
                            for k in kommentare} - {""})
            text = "\n".join(
                [str(det.get("description") or "")]
                + [str(c.get("title") or "") for c in
                   (det.get("checklist") or {}).values()]
                + [strip_html(k.get("html") or "") for k in kommentare])
            ts = export_util.graph_zeit(task.get("createdDateTime"))
            for k in kommentare:
                kt = export_util.graph_zeit(k.get("wann"))
                if kt and (not ts or kt > ts):
                    ts = kt
            satz = {
                "uid": f"planner:{ordner.name}/{tid}:0", "src": "planner",
                "root": "planner", "rel": rel,
                "who": ", ".join((zustaendig or leute)[:3]),
                "ppl": " ".join(zustaendig + leute).lower(),
                "ts": ts.timestamp() if ts else None,
                "date": ts.strftime("%Y-%m-%d %H:%M") if ts else "",
                "title": str(task.get("title") or "(ohne Titel)"),
                "ctx": f'{titel_plan}/'
                       f'{buckets.get(task.get("bucketId"), "?")}',
                "text": text.strip(),
                "att": anhaenge or None,
            }
            if e.get("deleted"):
                satz["gone"] = e["deleted"]
            recs.append(satz)
    return recs


def load_todo(root_dir):
    """One record per To Do task, straight from the per-list state.db –
    title, notes, steps and linked resources are the searchable text; the
    source file is the list's list.html."""
    import state_db
    root = Path(root_dir)
    if not root.is_dir():
        return []
    recs = []
    for ordner in sorted(pf for pf in root.iterdir() if pf.is_dir()):
        db = state_db.StateDb(ordner)
        try:
            liste = json.loads(db.kv_lesen("list") or "{}")
            eintraege = json.loads(db.kv_lesen("tasks") or "{}")
        except ValueError:
            continue
        if not eintraege:
            continue
        rel = f"{ordner.name}/list.html"
        titel_liste = str(liste.get("titel") or ordner.name)
        for tid, e in eintraege.items():
            task = e.get("task") or {}
            body = task.get("body") or {}
            inhalt = str(body.get("content") or "")
            if (body.get("contentType") or "text") == "html":
                inhalt = collapse(strip_html(inhalt))
            text = "\n".join(
                [inhalt]
                + [str(s.get("displayName") or "") for s in
                   (task.get("checklistItems") or [])]
                + [str(r.get("displayName") or r.get("applicationName") or "")
                   for r in (task.get("linkedResources") or [])])
            anhaenge = " ".join(
                str(a.get("name") or "").replace(" ", "_")
                for a in (e.get("anhaenge") or [])).strip()
            ts = (export_util.graph_zeit(task.get("lastModifiedDateTime"))
                  or export_util.graph_zeit(task.get("createdDateTime")))
            satz = {
                "uid": f"todo:{ordner.name}/{tid}:0", "src": "todo",
                "root": "todo", "rel": rel,
                "who": "", "ppl": "",
                "ts": ts.timestamp() if ts else None,
                "date": ts.strftime("%Y-%m-%d %H:%M") if ts else "",
                "title": str(task.get("title") or "(ohne Titel)"),
                "ctx": titel_liste,
                "text": text.strip(),
                "att": anhaenge or None,
            }
            if e.get("deleted"):
                satz["gone"] = e["deleted"]
            recs.append(satz)
    return recs


ONENOTE_SUFFIX = ".files"       # per page: the folder for large images and attachments
_ONENOTE_KOPF_RE = re.compile(r'<div class="mn-(?:kopf|weg)">.*?</div>', re.S)


def _onenote_satz(p_str, root_str):
    p, root = Path(p_str), Path(root_str)
    rel = p.relative_to(root).as_posix()
    try:
        roh = p.read_text(encoding="utf-8", errors="replace")
        ts = p.stat().st_mtime
    except OSError:
        return None
    roh = re.sub(r'"data:[^"]*"', '""', roh)
    m = re.search(r"<title>(.*?)</title>", roh, re.S | re.I)
    titel = collapse(html_lib.unescape(m.group(1))) if m else p.stem
    # The header the export adds – notebook path and dates – is navigation,
    # not the note; the folder path carries it for the filters anyway.
    roh = _ONENOTE_KOPF_RE.sub("", roh)
    return {
        "uid": f"onenote:{rel}:0", "src": "onenote", "root": "onenote",
        "rel": rel, "who": "", "ppl": "", "ts": ts,
        "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
        "title": titel,
        "ctx": rel.rsplit("/", 1)[0] if "/" in rel else "",
        "text": collapse(strip_html(roh)),
    }


def load_onenote(root_dir, nur=None):
    """One record per OneNote page – full text, straight from the HTML the
    export wrote; the folder path (notebook/group/section) is the context
    the folder filter offers. Pages that left the notebook carry their
    marker from the notebook's state.db."""
    root = Path(root_dir)
    if not root.is_dir():
        return []
    weg = grabsteine("onenote", root)
    dateien = _nur(_dateien_onenote(root), root, nur)
    recs = [r for r in _pmap(_onenote_satz, dateien, str(root)) if r]
    for satz in recs:
        if satz["rel"] in weg:
            satz["gone"] = weg[satz["rel"]]
    return recs


TEAMS_ANHANG_DIR = "Anhaenge"   # files a Teams message referenced, per conversation


def load_teams_files(root_dir, nur=None):
    """One record per file next to a Teams conversation – the referenced
    attachments and the mirrored channel folders – name and path, no
    content, like every mirror. The kind folder up front (1on1, channels/…)
    is the context, so the Teams folder filter finds files and messages
    alike; tombstones come from the channel mirrors' state.db."""
    root = Path(root_dir)
    if not root.is_dir():
        return []
    dateien = _nur(_dateien_teams_files(root), root, nur)
    weg = grabsteine("teams_files", root)
    recs = [r for r in _pmap(_datei_satz, dateien, str(root)) if r]
    for r in recs:
        r["root"] = "teams"
        r["uid"] = "teamsdatei:" + r["uid"].split(":", 1)[1]
        if r["rel"] in weg:
            r["gone"] = weg[r["rel"]]
    return recs


# --------------------------------------------------------------------------
# What the index reads, file by file – shared by the loaders and by the
# incremental read in rag_index: ONE enumeration per source, so a file the
# manifest lists is exactly a file the loader would parse.
# --------------------------------------------------------------------------
def _teams_datei_ordner(teile):
    """Below an attachment or mirror folder? Those hold files, not
    conversations – and an attached .html must not read as a chat."""
    return TEAMS_ANHANG_DIR in teile[:-1] or ONEDRIVE_DIR in teile[:-1]


def _dateien_teams(root):
    return [p for p in sorted(root.rglob("*.html"))
            if p.name not in ("index.html", "search.html")
            and not _teams_datei_ordner(p.relative_to(root).parts)]


def _dateien_teams_files(root):
    return [p for p in sorted(root.rglob("*"))
            if p.is_file() and not p.name.endswith(".teil")
            and _teams_datei_ordner(p.relative_to(root).parts)]


def _dateien_onenote(root):
    # A page's .files folder may hold an attached .html – that is a file,
    # not a page.
    return [p for p in sorted(root.rglob("*.html"))
            if not any(t.endswith(ONENOTE_SUFFIX)
                       for t in p.relative_to(root).parts[:-1])]


def _dateien_outlook(root):
    return sorted(root.rglob("*.eml"))


def _dateien_onedrive(root):
    basis = root / ONEDRIVE_DIR
    if not basis.is_dir():
        return []
    return [p for p in sorted(basis.rglob("*"))
            if p.is_file() and not p.name.endswith(".teil")]


def _dateien_sharepoint(root):
    dateien = []
    for lib in sorted(p for p in root.glob("*/*") if p.is_dir()):
        dateien += [p for p in sorted((lib / ONEDRIVE_DIR).rglob("*"))
                    if p.is_file() and not p.name.endswith(".teil")]
    return dateien


def _dateien_pages(root):
    return sorted(root.rglob("*.html"))


DATEIEN = {"teams": _dateien_teams, "outlook": _dateien_outlook,
           "onedrive": _dateien_onedrive, "sharepoint": _dateien_sharepoint,
           "pages": _dateien_pages, "onenote": _dateien_onenote,
           "teams_files": _dateien_teams_files}


def _nur(files, root, nur):
    """Only the files whose rel path is wanted – None means all of them."""
    if nur is None:
        return files
    return [p for p in files if p.relative_to(root).as_posix() in nur]


def grabsteine(art, root_dir):
    """rel -> since when the item is gone at the source, for one export root.

    Kept apart from the parsing on purpose: a tombstone can appear while the
    file stays byte-identical, so an unchanged file re-used from the last
    index still has to pick up today's answer.
    """
    root = Path(root_dir)
    if art in ("outlook", "onedrive"):
        return lies_verschwunden(root)
    if art == "pages":
        import state_db
        return state_db.StateDb(root).verschwunden_lesen()
    if art == "sharepoint":
        import state_db
        weg = {}
        for lib in sorted(p for p in root.glob("*/*") if p.is_dir()):
            praefix = lib.relative_to(root).as_posix()
            weg.update({f"{praefix}/{rel}": ts for rel, ts in
                        state_db.StateDb(lib).verschwunden_lesen().items()})
        return weg
    if art == "teams_files":
        # The channel mirrors: one state.db per team library below
        # channels/, each with the tombstones of its own subtree.
        import state_db
        weg = {}
        for db in sorted((root / "channels").rglob(state_db.DB_NAME)):
            praefix = db.parent.relative_to(root).as_posix()
            weg.update({f"{praefix}/{rel}": ts for rel, ts in
                        state_db.StateDb(db.parent).verschwunden_lesen().items()})
        return weg
    if art == "onenote":
        # The export keeps a page's marker in the notebook's state.db – the
        # file stays, so the record knows since when it is gone.
        import state_db
        weg = {}
        for nb in sorted(p for p in root.iterdir() if p.is_dir()):
            db = state_db.StateDb(nb)
            try:
                # One row per page since 9.0; an archive written by 8.x
                # still carries the one blob until its next run.
                zeilen = db.saetze_lesen("pages")
                seiten = ({k: json.loads(v) for k, v in zeilen.items()} if zeilen
                          else json.loads(db.kv_lesen("pages") or "{}"))
            except ValueError:
                continue
            for e in seiten.values():
                if e.get("deleted") and e.get("rel"):
                    weg[f'{nb.name}/{e["rel"]}'] = e["deleted"]
        return weg
    return {}


def manifest(art, root_dir):
    """rel -> (mtime_ns, size) of every file the loader for `art` would
    read. Cheap – one stat per file, no parsing – and the whole reason the
    index can skip what did not change."""
    root = Path(root_dir)
    if not root.is_dir():
        return {}
    out = {}
    for p in DATEIEN[art](root):
        try:
            st = p.stat()
        except OSError:
            continue
        out[p.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
    return out


def load_records(teams_dir, outlook_dir, onedrive_dir=None,
                 sharepoint_dir=None, pages_dir=None, planner_dir=None,
                 todo_dir=None, onenote_dir=None):
    recs = []
    if teams_dir and Path(teams_dir).is_dir():
        recs += load_teams(teams_dir)
        recs += load_teams_files(teams_dir)   # referenced files, channel folders
    if outlook_dir and Path(outlook_dir).is_dir():
        recs += load_outlook(outlook_dir)     # .eml
        recs += load_calendar(outlook_dir)    # .ics
        recs += load_contacts(outlook_dir)    # .vcf
    if onedrive_dir and Path(onedrive_dir).is_dir():
        recs += load_onedrive(onedrive_dir)   # mirrored files
    if sharepoint_dir and Path(sharepoint_dir).is_dir():
        recs += load_sharepoint(sharepoint_dir)
    if pages_dir and Path(pages_dir).is_dir():
        recs += load_pages(pages_dir)              # rendered site pages
    if planner_dir and Path(planner_dir).is_dir():
        recs += load_planner(planner_dir)          # boards with their comments
    if todo_dir and Path(todo_dir).is_dir():
        recs += load_todo(todo_dir)                # lists with their tasks
    if onenote_dir and Path(onenote_dir).is_dir():
        recs += load_onenote(onenote_dir)          # pages, full text
    return recs


def _split(text, size, overlap):
    text = text or ""
    if len(text) <= size:
        return [text.strip()] if text.strip() else []
    out, i, n = [], 0, len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:
            sp = text.rfind(" ", i + int(size * 0.6), end)
            if sp != -1:
                end = sp
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return out


def chunk_records(records, size=1500, overlap=200):
    """One message/mail = base unit; long texts into overlapping pieces."""
    chunks = []
    for r in records:
        parts = _split(r["text"], size, overlap)
        for j, part in enumerate(parts):
            c = dict(r)
            c.pop("text", None)
            c["text"] = part
            c["cid"] = f'{r["uid"]}#{j}'
            # Attachment names belong to the message, not to each of its
            # pieces. Repeated on all of them, the full-text search would
            # count them as often as the mail has pieces – a long mail would
            # rank higher for that reason alone.
            if j > 0:
                c.pop("att", None)
            chunks.append(c)
    return chunks


def embed_text(chunk):
    """What actually gets embedded: title as context + chunk text."""
    return f'{chunk.get("title", "")}\n{chunk["text"]}'.strip()


def chunk_hash(chunk):
    return hashlib.sha1(embed_text(chunk).encode("utf-8")).hexdigest()
