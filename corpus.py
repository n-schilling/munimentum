#!/usr/bin/env python3
"""
corpus.py – gemeinsame Datengrundlage für die lokale RAG-Suche.

Liest Teams-Export (HTML) und Outlook-Export (.eml) in einheitliche Datensätze
und zerlegt lange Texte in überlappende Chunks. Wird von rag_index.py
(Embeddings) genutzt. Nur Standardbibliothek.
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
SAFETY_CAP = 500_000   # absurd lange Einzeltexte begrenzen (vor dem Chunking)

# Ab wie vielen Dateien sich der Prozess-Pool lohnt (Spawn-Overhead amortisiert).
_PAR_THRESHOLD = 200

# Warum der Pool nicht zustande kam – None, solange alles normal lief. Diese
# Datei gibt selbst nichts aus (sie ist eine Bibliothek, siehe tests/
# test_projekt.py); wer sie benutzt, liest die Notiz und meldet sie. rag_index
# tut das nach dem Einlesen.
POOL_FEHLER = None


def _pmap(func, files, root_dir):
    """func(p_str, root_str) über alle Dateien – parallel über alle CPU-Kerne.

    Das Parsen der Exporte ist reine CPU-Arbeit und war bisher single-threaded
    der langsamste Teil vor dem (GPU-gebundenen) Einbetten. Bei vielen Dateien
    auf alle Kerne verteilen; bei wenigen seriell (Spawn lohnt nicht).

    Scheitert der Pool, wird seriell weitergemacht. Das dauert länger, aber es
    ist immer noch die Aufgabe, die hier zu erledigen ist – ein Index, der gar
    nicht erst gebaut wird, weil ein Arbeitsprozess nicht starten konnte, hilft
    niemandem. Die Ursache steht danach in POOL_FEHLER.
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
        # BrokenExecutor: ein Arbeitsprozess ist gestorben, statt zu antworten.
        # OSError: er ließ sich gar nicht erst starten (keine Handles mehr,
        # gesperrt durch eine Sicherheitssoftware, kein /dev/shm im Container).
        POOL_FEHLER = f"{type(e).__name__}: {e}"
        return seriell()


# --------------------------------------------------------------------------
# Teams: exportierte Konversations-HTML parsen
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
    # Der Ablageordner, nicht ein Schmuckname: ctx ist die Spalte, über die in
    # der Suche gefiltert wird, und ein Pfad meint dort immer auch alles
    # darunter. "channels" trifft damit jeden Kanal, ohne dass die Auswahl je
    # Kanal einen Eintrag braucht – und "1on1" genau die 1:1-Chats.
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


def load_teams(root_dir):
    root = Path(root_dir)
    files = [p for p in sorted(root.rglob("*.html"))
             if p.name not in ("index.html", "search.html")]
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


# In Mail-Threads wird die komplette Historie in jeder Antwort erneut zitiert –
# in diesem Korpus oft >80 % des Textvolumens. Das bläht den Index auf (langsames
# Einbetten) und verschlechtert das Retrieval (Duplikat-Rauschen). Wir schneiden
# vor dem Chunking an der ersten Zitat-Grenze ab und behalten nur die neue
# Nachricht. Konservativ: nur bei eindeutigen Outlook-/Mail-Client-Markern.
_QUOTE_CUTS = [
    re.compile(r"_{25,}"),                                  # Outlook-Trennlinie
    re.compile(r"(?m)^\s*_{10,}\s*$"),                      # Trennlinie auf eigener Zeile
    re.compile(r"-{3,}\s*(Original Message|Ursprüngliche Nachricht)\s*-{3,}", re.I),
    re.compile(r"(?m)^\s*(From|Von):\s.*(?:\n.*){0,4}?^\s*(Sent|Gesendet|Date):\s", re.I),
    re.compile(r"(?im)^[ \t>]*On\b.{0,300}?\bwrote:\s*$", re.S),
    re.compile(r"(?im)^[ \t>]*Am\b.{0,300}?\bschrieb\b.{0,120}?:\s*$", re.S),
]
_SIG_CUTS = [
    re.compile(r"(?m)^-- ?$"),                              # RFC-3676-Signaturtrenner
    re.compile(r"(?im)^\s*Sent from (my |Outlook).*$"),
    re.compile(r"(?im)^\s*Von meinem (iPhone|iPad|Samsung|Android).*$"),
    re.compile(r"(?im)^\s*Get Outlook for (iOS|Android).*$"),
]


def strip_quoted(text):
    """Zitierte Thread-Historie und Signatur abschneiden, neue Nachricht behalten."""
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
    return re.sub(r"(?m)^[ \t]*>.*$", "", head)            # restliche Zitatzeilen


# --------------------------------------------------------------------------
# Verlauf: welche Mails zusammengehören
#
# Alles dafür steht längst in den .eml-Dateien – ein Neu-Export ist nicht nötig.
# An einem echten Bestand (Stichprobe 400 von rund 45.000) gemessen: Thread-Index 89 %,
# References/In-Reply-To 58 %, Message-ID 100 %. Deshalb eine Kaskade, die mit
# der genauesten Angabe beginnt und am Ende jede Mail wenigstens sich selbst
# zuordnet – ein Verlauf aus einer Nachricht ist richtig, nur langweilig.
# --------------------------------------------------------------------------
def thread_key(msg):
    """Stabile Kennung des Gesprächs, zu dem diese Mail gehört."""
    roh = hdr(msg, "thread-index")
    if roh:
        try:
            # Exchange: die ersten 22 Byte sind die Kennung des Gesprächs,
            # jede Antwort hängt weitere 5 Byte an. Nur der Kopf zählt.
            kopf = base64.b64decode(roh + "===", validate=False)[:22]
            if len(kopf) == 22:
                return "tix:" + kopf.hex()
        except (ValueError, binascii.Error):
            pass
    for name in ("references", "in-reply-to"):
        wert = hdr(msg, name)
        if wert:
            # Der erste Eintrag in References ist der Anfang des Gesprächs.
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


# Zeichen, die in keinen Dateinamen gehören – und der Punkt am Anfang, damit
# aus einem Anhang kein verstecktes „.profile“ wird.
_UNGUT = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sicherer_dateiname(name, ersatz="anhang"):
    """Aus dem Namen im Anhang einen Dateinamen machen, dem man trauen kann."""
    # Erst den Pfad abschneiden, dann die Zeichen ersetzen. Andersherum wird
    # aus "../../.ssh/id_rsa" ein "_.._.ssh_id_rsa" – der Pfad ist dann zwar
    # harmlos, steckt aber noch vollständig im Namen.
    roh = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    roh = _UNGUT.sub("_", roh).strip(". ")
    return roh[:150] or ersatz


def anhaenge(msg):
    """Namen der Anhänge einer Mail, in der Reihenfolge des Auftretens.

    Der Index kannte bisher nur den Mailtext – der Vertrag im Anhang lag im
    Archiv, war aber mit keinem Wort zu finden. Die Namen allein bringen schon
    den größten Teil: „Vertrag_Musterkunde.pdf“ sucht man ohnehin so.

    Nur echte Anhänge: Inline-Bilder (Signaturlogos!) tragen Namen wie
    image001.png und würden die Suche mit Rauschen fluten.
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
    """Die Dateitypen hinter einer Anhangliste – entdoppelt, klein, sortiert.

    Aus "Vertrag_Musterkunde.pdf Anlage.XLSX" wird "pdf xlsx". Gefiltert wird
    danach in SQL und nicht über den Volltext: die Bedeutungssuche und die
    KI-Antwort schränken dort ein, und ein Filter, der nur in der Textsuche
    wirkt, wäre in zwei von drei Sucharten stillschweigend wirkungslos.

    Nur was wie eine Endung aussieht: Buchstaben und Ziffern, höchstens acht
    Zeichen. Ein Name wie "Bericht.2024-final" hat keine.
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
    """rel -> Zeitpunkt, seit dem die Mail nicht mehr im Postfach steht.

    Geschrieben von den Exporten in die state.db des Ordners. Die Datei
    selbst bleibt liegen; hier wird nur vermerkt, dass sie an der Quelle
    fehlt – das ist der Unterschied zwischen einer Kopie und einem Archiv.
    """
    import state_db
    return state_db.StateDb(root_dir).verschwunden_lesen()


def load_outlook(root_dir):
    root = Path(root_dir)
    files = sorted(root.rglob("*.eml"))
    recs = [r for r in _pmap(_outlook_file, files, root_dir) if r is not None]
    weg = lies_verschwunden(root_dir)
    if weg:
        for r in recs:
            wann = weg.get(r["rel"])
            if wann:
                r["gone"] = wann
    return recs


# --------------------------------------------------------------------------
# Kalender (.ics) und Kontakte (.vcf) – liegen im Outlook-Export
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
    # Der Ordnerpfad, nicht ein Schmuckname: ctx ist die Spalte, über die in
    # der Suche nach Ordnern gefiltert wird. Als "kalender/Arbeit" steht der
    # Kalender dort neben "E-Mail/Kunden" – dieselbe Auswahl wie im Export,
    # mit demselben Pfad, unter dem er auch auf der Platte liegt.
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
# Zusammenführen + Chunking
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# OneDrive: die gespiegelten Dateien
#
# Stufe eins ist bewusst der NAME, nicht der Inhalt: er kostet nichts, ist
# sofort da und beantwortet die häufigste Frage („wo lag noch mal das Angebot").
# Ein PDF zu öffnen ist eine andere Größenordnung – an einem echten Bestand
# gemessen rund eine Stunde für Extraktion und Einbetten (siehe ROADMAP.md).
#
# Damit das nachrüstbar bleibt, ist `text` hier schon das INHALTSFELD und trägt
# vorerst nur den Pfad. Wer später extrahiert, ersetzt genau diesen Wert; Aufbau,
# Kennung und Suchfilter bleiben, wie sie sind, und ein alter Index wird nicht
# ungültig, sondern nur ärmer.
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
    # Der Pfad als Text: so findet die Volltextsuche auch über den Ordnernamen,
    # nicht nur über den Dateinamen. Zwei Wörter, kein Rauschen.
    return {
        "uid": f"datei:{rel}:0", "src": "datei", "root": "onedrive",
        "rel": rel,
        "who": "", "ppl": "",
        "ts": ts,
        "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
        "title": p.name,
        "ctx": ordner,
        "att": p.name,          # dieselbe Spalte wie Mailanhänge: att:pdf findet beides
        "text": rel.replace("/", " / "),
        "groesse": groesse,
    }


def load_onedrive(root_dir):
    """Ein Satz je gespiegelter Datei – Name und Pfad, kein Inhalt."""
    root = Path(root_dir)
    basis = root / ONEDRIVE_DIR
    if not basis.is_dir():
        return []
    dateien = [p for p in sorted(basis.rglob("*"))
               if p.is_file() and not p.name.endswith(".teil")]
    recs = [r for r in _pmap(_datei_satz, dateien, str(root)) if r]
    weg = lies_verschwunden(root)          # derselbe Leser wie beim Postfach
    for r in recs:
        if r["rel"] in weg:
            r["gone"] = weg[r["rel"]]
    return recs


def load_sharepoint(root_dir):
    """One record per mirrored SharePoint file – name and path, no content.

    The mirror keeps one folder per library (<site>/<library>/Dateien/…),
    each with its own tombstone file. All libraries feed ONE parallel pass –
    a pool per library would pay the spawn cost once per library, every
    index run – and the tombstone paths get their library prefix back.
    """
    root = Path(root_dir)
    if not root.is_dir():
        return []
    import state_db
    dateien, weg = [], {}
    for lib in sorted(p for p in root.glob("*/*") if p.is_dir()):
        dateien += [p for p in sorted((lib / ONEDRIVE_DIR).rglob("*"))
                    if p.is_file() and not p.name.endswith(".teil")]
        praefix = lib.relative_to(root).as_posix()
        weg.update({f"{praefix}/{rel}": ts
                    for rel, ts in
                    state_db.StateDb(lib).verschwunden_lesen().items()})
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


def load_pages(root_dir):
    """One record per rendered site page – full text, straight from the HTML.

    Unlike the file mirrors, the content is right there: the pages export
    writes the text web parts into the file, so the page body goes into the
    index and the full-text search reads SharePoint pages like mail. The
    parse fans out like every sibling loader.
    """
    import state_db
    root = Path(root_dir)
    if not root.is_dir():
        return []
    weg = state_db.StateDb(root).verschwunden_lesen()
    dateien = sorted(root.rglob("*.html"))
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
            # Referenz-Aliase wie Mail-Anhänge: Namen durchsuchbar, und der
            # Dateityp-Filter (att:pdf) trifft auch Planner-Karten.
            anhaenge = " ".join(
                str((ref or {}).get("alias") or "").replace(" ", "_")
                for ref in (det.get("references") or {}).values()).strip()
            # Zuständige UND Kommentar-Autoren, GUIDs über den Namenscache
            # des Exports aufgelöst – rohe IDs sagen in der Trefferliste
            # niemandem etwas.
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


def load_records(teams_dir, outlook_dir, onedrive_dir=None,
                 sharepoint_dir=None, pages_dir=None, planner_dir=None):
    recs = []
    if teams_dir and Path(teams_dir).is_dir():
        recs += load_teams(teams_dir)
    if outlook_dir and Path(outlook_dir).is_dir():
        recs += load_outlook(outlook_dir)     # .eml
        recs += load_calendar(outlook_dir)    # .ics
        recs += load_contacts(outlook_dir)    # .vcf
    if onedrive_dir and Path(onedrive_dir).is_dir():
        recs += load_onedrive(onedrive_dir)   # gespiegelte Dateien
    if sharepoint_dir and Path(sharepoint_dir).is_dir():
        recs += load_sharepoint(sharepoint_dir)
    if pages_dir and Path(pages_dir).is_dir():
        recs += load_pages(pages_dir)              # gerenderte Site-Seiten
    if planner_dir and Path(planner_dir).is_dir():
        recs += load_planner(planner_dir)          # Boards samt Kommentaren
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
    """Eine Nachricht/Mail = Basis-Einheit; lange Texte in überlappende Stücke."""
    chunks = []
    for r in records:
        parts = _split(r["text"], size, overlap)
        for j, part in enumerate(parts):
            c = dict(r)
            c.pop("text", None)
            c["text"] = part
            c["cid"] = f'{r["uid"]}#{j}'
            # Anhangnamen gehören der Nachricht, nicht jedem ihrer Stücke. Auf
            # allen wiederholt zählte die Volltextsuche sie so oft, wie die
            # Mail Stücke hat – eine lange Mail stünde allein deshalb weiter
            # oben.
            if j > 0:
                c.pop("att", None)
            chunks.append(c)
    return chunks


def embed_text(chunk):
    """Was tatsächlich eingebettet wird: Titel als Kontext + Chunk-Text."""
    return f'{chunk.get("title", "")}\n{chunk["text"]}'.strip()


def chunk_hash(chunk):
    return hashlib.sha1(embed_text(chunk).encode("utf-8")).hexdigest()
