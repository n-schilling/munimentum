#!/usr/bin/env python3
"""
case_export.py – one case (faelle.py) as one ZIP that stands on its own.

    case_export.py <teams> <outlook> <onedrive> --sharepoint … --pages …
                   --planner … --todo … --onenote … --faelle <faelle.db>
                   --fall <id> --ziel <folder> [--lang de --res <dir>]

The ZIP holds the originals of everything the case points at, copied out
of the archive under the case's folder, their source's name and path –
the .eml, the .ics, the chat's HTML with its attachment folder, the file,
the page, the board – plus three files that tie them together:

    index.html    the case: description, casebook, every item with a link
                  into the copied original, the stored result lists, the
                  attached searches
    items.csv     the same list for a spreadsheet
    casebook.md   description and notes as plain text

With the data folder (--data) the ZIP carries the evidence as well
(evidence.py), so whoever receives it can check it without the app:

    SHA256SUMS.txt        every file's checksum – `shasum -a 256 -c`
    evidence/manifest.csv every original with the checksum the archive's
                          chain has for it and when the chain first saw it
    evidence/chain.jsonl  the chain's lines about those files
    evidence/closed/      the manifest written when the case was closed,
                          with its time stamp where a service signed it
    evidence/stamp/       the chain's last signed head
    evidence/README.txt   how to check all of it, in the page's language
    <versions>/           an item changed since it came into the case: the
                          version it came in with, beside today's

An item whose original the archive no longer holds is listed all the
same, marked, with what the case remembers about it; what Claude added
through MCP says so. Nothing in the archive is touched; the folder named
by --ziel is built, packed into <ziel>.zip and removed – only the ZIP
stays, under the user's folder (Settings → App), never below the export
folders.

Runs as a step of the app (progress.py protocol) – the run window shows
it, the run history keeps it.
"""

import re
import csv
import sys
import shutil
import argparse
import html as html_lib
from pathlib import Path
from datetime import datetime

import i18n
import evidence
import faelle
import versions
import version
import progress
import export_util

export_util.erzwinge_utf8()

ANHANG_DIR = "Anhaenge"          # Teams and Planner: a conversation's / board's files
ONENOTE_SUFFIX = ".files"        # OneNote: a page's images and attachments

# The folders the copied originals land in, by index root – readable
# names, one per source, so a folder full of exports needs no legend.
ORDNER = {"outlook": "Outlook", "teams": "Teams", "onedrive": "OneDrive",
          "sharepoint": "SharePoint", "pages": "SharePoint pages",
          "onenote": "OneNote", "planner": "Planner", "todo": "To Do"}


def quelle_key(e):
    """The text key naming an entry's source – the page's own labels,
    files by their mirror (search.source.*)."""
    if e.get("src") == "datei":
        return "search.source." + ("sharepoint" if e.get("root") == "sharepoint"
                                   else "teams" if e.get("root") == "teams" else "onedrive")
    return "search.source." + str(e.get("src") or "onedrive")


def _texte(lang, res):
    """The export's labels in the page's language – the same lang files
    the app serves, so the folder reads like the app did."""
    try:
        return i18n.strings(lang, res)
    except OSError:
        return {}


class Texte:
    """The export's texts, filled the one way everything outside the
    browser fills them (i18n.fuelle)."""

    def __init__(self, strings):
        self.s = strings or {}

    def __call__(self, key, **v):
        return i18n.fuelle(self.s.get(key) or key, v)


# --------------------------------------------------------------------------
# What to copy for one entry
# --------------------------------------------------------------------------
def _msg_anker(key):
    """teams:<conversation>#<message id> -> #m-<message id>, or ''."""
    rest = str(key or "").partition(":")[2]
    msg = rest.rpartition("#")[2] if "#" in rest else ""
    return f"#m-{msg}" if msg else ""


def _planner_anhaenge(board_html, tid):
    """The files the card of one task links to (Anhaenge/…) – read out of
    the board's HTML, so only that task's attachments travel."""
    m = re.search(r'<details class="karte[^"]*" id="k-' + re.escape(tid) + r'">(.*?)(?=<details class="karte|\Z)',
                  board_html, re.S)
    if not m:
        return []
    return sorted({html_lib.unescape(h) for h in re.findall(r'href="(' + ANHANG_DIR + r'/[^"]+)"', m.group(1))})


def plan(e, pfade):
    """Where an entry's original lies and what goes with it:
    (source folder, [relative paths to copy], link target inside the export).
    The link is the copied original – with the anchor that lands on the
    message, card or task where the HTML carries one."""
    src, root, rel, key = e.get("src"), e.get("root"), str(e.get("rel") or ""), str(e.get("key") or "")
    wurzel = pfade.get(root)
    if not rel or not wurzel:
        return None, [], ""
    quelle = Path(wurzel)
    kopien = [rel]
    anker = ""
    if src == "teams":
        ordner, _, datei = rel.rpartition("/")
        stamm = datei[:-5] if datei.endswith(".html") else datei
        anhang = f"{ordner}/{ANHANG_DIR}/{stamm}" if ordner else f"{ANHANG_DIR}/{stamm}"
        if (quelle / anhang).is_dir():
            kopien.append(anhang)
        anker = _msg_anker(key)
    elif src == "onenote" and rel.endswith(".html"):
        if (quelle / (rel[:-5] + ONENOTE_SUFFIX)).is_dir():
            kopien.append(rel[:-5] + ONENOTE_SUFFIX)
    elif src == "planner":
        tid = key.partition(":")[2]
        try:
            roh = (quelle / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            roh = ""
        board = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for a in _planner_anhaenge(roh, tid):
            pfad = f"{board}/{a}" if board else a
            if (quelle / pfad).is_file():
                kopien.append(pfad)
        anker = f"#k-{tid}" if tid else ""
    elif src == "todo":
        tid = key.partition(":")[2]
        anker = f"#t-{tid}" if tid else ""
    return quelle, kopien, anker


def _kopieren(quelle, rel, ziel):
    """One file or folder out of the archive into the export – False when
    the archive no longer has it."""
    von, nach = quelle / rel, ziel / rel
    try:
        if von.is_dir():
            shutil.copytree(von, nach, dirs_exist_ok=True)
        elif von.is_file():
            nach.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(von, nach)
        else:
            return False
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# The three files
# --------------------------------------------------------------------------
_STIL = """
:root{color-scheme:light dark;--fg:#1e1e1e;--bg:#fbfaf7;--mute:#6b6b6b;--line:#dcd8cf;--akz:#2c5f8a;--karte:#f2efe8}
@media(prefers-color-scheme:dark){:root{--fg:#e6e3dc;--bg:#1c1b19;--mute:#9a978f;--line:#3a3833;--akz:#8fb8dc;--karte:#26251f}}
body{margin:0 auto;padding:24px 16px 48px;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;max-width:960px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--mute);font-size:13px}.beschreibung{white-space:pre-wrap;margin:12px 0 0}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mute);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
td.datum{white-space:nowrap;font-variant-numeric:tabular-nums}a{color:var(--akz)}.weg{color:var(--mute)}
.notiz{background:var(--karte);border-radius:6px;padding:10px 12px;margin:8px 0}.notiz .wann{color:var(--mute);font-size:12px}
.notiz p{margin:4px 0 0;white-space:pre-wrap}.leer{color:var(--mute)}
.kriterien{font-size:13px;color:var(--mute)}footer{margin-top:40px;color:var(--mute);font-size:12px}
.mcp{font-size:11px;color:var(--akz);border:1px solid var(--akz);border-radius:999px;padding:0 6px;margin-left:6px;white-space:nowrap}
h2 .n{font-weight:400;color:var(--mute);font-size:14px;margin-left:6px}
.bem{margin-top:3px;padding-left:7px;border-left:2px solid var(--akz);font-size:13px}
.zeit td.notiz-zeile{color:var(--mute)}.ordner{font-size:12px;color:var(--mute);white-space:nowrap}
"""


def _mcp(t, herkunft):
    return f' <span class="mcp">{html_lib.escape(t("cases.origin.mcp"))}</span>' if herkunft == "mcp" else ""


def _kriterien_text(k, t):
    """A stored search's criteria as one readable line."""
    teile = []
    if k.get("q"):
        teile.append(f"“{k['q']}”")
    modus = {"text": "search.mode.text", "aehnlich": "search.mode.aehnlich",
             "ki": "search.mode.ki"}.get(k.get("mode"))
    if modus and k.get("q"):
        teile.append(t(modus))
    if k.get("person"):
        teile.append(t("cases.export.person", name=k["person"]))
    if k.get("source") and k["source"] != "all":
        teile.append(t("cases.export.source", name=k["source"]))
    if k.get("from") or k.get("to"):
        teile.append(f"{k.get('from') or '…'} – {k.get('to') or '…'}")
    if k.get("folder"):
        teile.append(t("cases.export.folder", name=k["folder"]))
    if k.get("filetype"):
        teile.append(f".{k['filetype']}")
    if k.get("gone"):
        teile.append(t("cases.export.gone"))
    if k.get("party") in faelle.PARTEIEN:
        teile.append(t('cases.export.party.' + k["party"]))
    if k.get("fall"):
        teile.append(t("cases.export.incase"))
    return " · ".join(teile) or t("cases.export.everything")


def _wann(iso):
    """ISO UTC -> local, minute precision; anything else as it is."""
    try:
        d = datetime.fromisoformat(str(iso))
        return d.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso or "")


def index_html(fall, zeilen, t, lang):
    """The case page: what the folder holds and where each piece lies."""
    esc = html_lib.escape
    status = t('cases.export.status.' + ("open" if fall["status"] == faelle.OFFEN else "closed"))
    out = [f'<!doctype html><html lang="{esc(lang)}"><head><meta charset="utf-8">',
           f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(fall["name"])}</title>',
           f"<style>{_STIL}</style></head><body>",
           f'<h1>{esc(fall["name"])}</h1>',
           f'<div class="meta">{esc(status)}'
           f' · {esc(t("cases.export.created", when=_wann(fall["angelegt"])))}'
           + (f' · {esc(t("cases.export.closed", when=_wann(fall["geschlossen"])))}' if fall.get("geschlossen") else "")
           + "</div>"]
    if fall.get("beschreibung"):
        out.append(f'<p class="beschreibung">{esc(fall["beschreibung"])}</p>')

    out.append(f'<h2>{esc(t("cases.export.casebook"))}</h2>')
    if fall["notizen_liste"]:
        for n in fall["notizen_liste"]:
            out.append(f'<div class="notiz"><div class="wann">{esc(_wann(n["wann"]))}{_mcp(t, _herkunft(n))}</div>'
                       f'<p>{esc(n["text"])}</p></div>')
    else:
        out.append(f'<p class="leer">{esc(t("cases.export.nonotes"))}</p>')

    def tabelle(teil):
        rows = [f'<table><thead><tr><th>{esc(t("cases.export.col.source"))}</th><th>{esc(t("cases.export.col.date"))}</th>'
                f'<th>{esc(t("cases.export.col.who"))}</th><th>{esc(t("cases.export.col.title"))}</th></tr></thead><tbody>']
        for z in teil:
            titel = esc(z["titel"] or z["rel"] or z["key"])
            if z["datei"]:
                link = f'<a href="{esc(z["datei"] + z["anker"])}">{titel}</a>'
            else:
                link = f'<span class="weg">{titel} – {esc(t("cases.export.missing"))}</span>'
            rows.append(f'<tr><td>{esc(t(z["quelle"]))}</td><td class="datum">{esc(z["datum"] or "")}</td>'
                        f'<td>{esc(z["wer"] or "")}</td><td>{link}{_mcp(t, z["herkunft"])}{_bem(z)}</td></tr>')
        rows.append("</tbody></table>")
        return "\n".join(rows)

    out.append(f'<h2>{esc(t("cases.export.items", n=len(zeilen)))}</h2>')
    if not zeilen:
        out.append(f'<p class="leer">{esc(t("cases.export.noitems"))}</p>')
    elif any(z["ordner"] for z in zeilen):
        # The case's folders, in the case's order, unsorted last – each
        # its own heading and table, like the ZIP's folders.
        for name in _ordnerfolge(zeilen, fall):
            teil = [z for z in zeilen if z["ordner"] == name]
            out.append(f'<h2>{esc(name)}<span class="n">{len(teil)}</span></h2>')
            out.append(tabelle(teil))
    else:
        out.append(tabelle(zeilen))

    if fall["listen_liste"]:
        out.append(f'<h2>{esc(t("cases.export.lists"))}</h2><table><thead><tr>'
                   f'<th>{esc(t("cases.export.col.taken"))}</th><th>{esc(t("cases.export.col.criteria"))}</th>'
                   f'<th>{esc(t("cases.export.col.hits"))}</th></tr></thead><tbody>')
        for li in fall["listen_liste"]:
            out.append(f'<tr><td class="datum">{esc(_wann(li["wann"]))}</td>'
                       f'<td class="kriterien">{esc(_kriterien_text(li["kriterien"], t))}</td><td>{li["anzahl"]}</td></tr>')
        out.append("</tbody></table>")
    if fall["suchen_liste"]:
        out.append(f'<h2>{esc(t("cases.export.searches"))}</h2><table><thead><tr>'
                   f'<th>{esc(t("cases.export.col.name"))}</th><th>{esc(t("cases.export.col.criteria"))}</th>'
                   f'<th>{esc(t("cases.export.col.lastrun"))}</th></tr></thead><tbody>')
        for g in fall["suchen_liste"]:
            zuletzt = (f'{_wann(g["zuletzt"])} · {g["treffer"]}' if g.get("zuletzt") else "–")
            out.append(f'<tr><td>{esc(g["name"])}</td><td class="kriterien">{esc(_kriterien_text(g["kriterien"], t))}</td>'
                       f'<td class="datum">{esc(zuletzt)}</td></tr>')
        out.append("</tbody></table>")
    out.append(f'<footer>{esc(t("cases.export.footer", version=version.VERSION, when=datetime.now().strftime("%Y-%m-%d %H:%M")))}</footer>')
    out.append("</body></html>")
    return "\n".join(out)


def _herkunft(e):
    return "mcp" if (e.get("quelle") == faelle.MCP) else "page"


def _bem(z):
    """The remark under an item, in the index and the timeline."""
    return f'<div class="bem">{html_lib.escape(z["bemerkung"])}</div>' if z.get("bemerkung") else ""


def _zeitschluessel(text):
    """A sortable "YYYY-MM-DD HH:MM" from what an item or a note carries –
    empty when it has no date, so it sorts last."""
    text = str(text or "").strip()
    return text[:16] if len(text) >= 10 and text[4] == "-" else ""


def timeline_html(fall, zeilen, t, lang):
    """The case in the order it happened: every item and every note, month
    by month, oldest first – the page's timeline view on paper."""
    esc = html_lib.escape
    stellen = []
    for z in zeilen:
        stellen.append((_zeitschluessel(z["datum"]), "eintrag", z))
    for n in fall["notizen_liste"]:
        stellen.append((_zeitschluessel(_wann(n["wann"])), "notiz", n))
    stellen.sort(key=lambda s: (s[0] == "", s[0]))
    out = [f'<!doctype html><html lang="{esc(lang)}"><head><meta charset="utf-8">',
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{esc(t("cases.export.timeline"))} – {esc(fall["name"])}</title>',
           f"<style>{_STIL}</style></head><body>",
           f'<h1>{esc(fall["name"])}</h1>',
           f'<div class="meta">{esc(t("cases.export.timeline"))} · {esc(t("cases.export.items", n=len(zeilen)))}</div>']
    if not stellen:
        out.append(f'<p class="leer">{esc(t("cases.export.noitems"))}</p>')
    monat = None
    for schluessel, art, x in stellen:
        kopf = schluessel[:7] if schluessel else t("cases.export.undated")
        if kopf != monat:
            if monat is not None:
                out.append("</tbody></table>")
            out.append(f"<h2>{esc(kopf)}</h2><table class=\"zeit\"><tbody>")
            monat = kopf
        if art == "notiz":
            out.append(f'<tr><td class="datum">{esc(_wann(x["wann"]))}</td><td class="notiz-zeile" colspan="3">'
                       f'{esc(t("cases.export.note"))}{_mcp(t, _herkunft(x))} · {esc(x["text"])}</td></tr>')
            continue
        titel = esc(x["titel"] or x["rel"] or x["key"])
        link = (f'<a href="{esc(x["datei"] + x["anker"])}">{titel}</a>' if x["datei"]
                else f'<span class="weg">{titel} – {esc(t("cases.export.missing"))}</span>')
        ordner = f'<span class="ordner">{esc(x["ordner"])}</span>' if x["ordner"] else ""
        out.append(f'<tr><td class="datum">{esc(x["datum"] or "")}</td><td>{esc(t(x["quelle"]))}</td>'
                   f'<td>{esc(x["wer"] or "")}</td><td>{link}{_mcp(t, x["herkunft"])} {ordner}{_bem(x)}</td></tr>')
    if monat is not None:
        out.append("</tbody></table>")
    out.append(f'<footer>{esc(t("cases.export.footer", version=version.VERSION, when=datetime.now().strftime("%Y-%m-%d %H:%M")))}</footer>')
    out.append("</body></html>")
    return "\n".join(out)


def _ordnerfolge(zeilen, fall):
    """The folder names in the case's order – only those with rows – and
    the unsorted rows' name last."""
    belegt = {z["ordner"] for z in zeilen if not z["unsortiert"]}
    namen = [o["name"] for o in fall.get("ordner_liste") or () if o["name"] in belegt]
    offen = [z["ordner"] for z in zeilen if z["unsortiert"]][:1]
    return namen + offen


def items_csv(zeilen, t):
    from io import StringIO
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["key", "folder", "source", "date", "who", "title", "remark", "origin", "file", "original", "in_archive"])
    for z in zeilen:
        w.writerow([z["key"], z["ordner"] if not z["unsortiert"] else "", t(z["quelle"]),
                    z["datum"] or "", z["wer"] or "", z["titel"] or "", z.get("bemerkung") or "", z["herkunft"],
                    (z["datei"] + z["anker"]) if z["datei"] else "", z["rel"] or "",
                    "yes" if z["datei"] else "no"])
    return buf.getvalue()


def casebook_md(fall, zeilen, t):
    out = [f"# {fall['name']}", ""]
    if fall.get("beschreibung"):
        out += [fall["beschreibung"], ""]
    out += [f"## {t('cases.export.casebook')}", ""]
    if fall["notizen_liste"]:
        for n in fall["notizen_liste"]:
            marke = f" · {t('cases.origin.mcp')}" if _herkunft(n) == "mcp" else ""
            out.append(f"- {_wann(n['wann'])}{marke} – {n['text']}")
    else:
        out.append(t("cases.export.nonotes"))
    out += ["", f"## {t('cases.export.items', n=len(zeilen))}", ""]

    def zeile(z):
        titel = z["titel"] or z["rel"] or z["key"]
        stelle = (z["datei"] + z["anker"]) if z["datei"] else t("cases.export.missing")
        marke = f" · {t('cases.origin.mcp')}" if z["herkunft"] == "mcp" else ""
        return f"- {z['datum'] or ''} {z['wer'] or ''} – {titel}{marke} ({t(z['quelle'])}: {stelle})".replace("  ", " ")
    if any(z["ordner"] for z in zeilen):
        for name in _ordnerfolge(zeilen, fall):
            out += [f"### {name}", ""] + [zeile(z) for z in zeilen if z["ordner"] == name] + [""]
    else:
        out += [zeile(z) for z in zeilen]
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# The export
# --------------------------------------------------------------------------
def zielordner(basis, name):
    """<basis>/<case name>_<date>[-n] – a fresh folder every time."""
    stamm = f"{export_util.safe(name, 60)}_{datetime.now().strftime('%Y-%m-%d_%H%M')}"
    ziel = Path(basis) / stamm
    n = 1
    while ziel.exists():
        n += 1
        ziel = Path(basis) / f"{stamm}-{n}"
    return ziel


def _pinned_copy(ev, prints, e, target):
    """An item changed since it came into the case: the version it came in
    with, next to today's – a file's kept bytes, a message's words. Returns
    the path written below `target`, or None."""
    pinned = e.get("fassung")
    if not pinned or prints.of(e) in (None, pinned):
        return None
    stamp = str(e.get("hinzugefuegt") or "")[:10]
    rel = str(e.get("rel") or "")
    if e.get("root") == "teams" and "#" in str(e.get("key") or ""):
        found = evidence.message_versions(prints.dirs.get("teams"), e["key"]) or []
        v = next((v for v in found if v["sha256"] == pinned), None)
        if v is None:
            return None
        out = target / f"{Path(rel).stem}.{e['key'].rpartition('#')[2]}.{stamp}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(evidence.message_text(v), encoding="utf-8")
        return out
    if str(e.get("key") or "").startswith(("planner:", "todo:")):
        return None
    base = prints.dirs.get(e.get("root"))
    rel_data = versions.rel_of(Path(base) / rel, ev.data) if base else None
    body = ev.bytes_of(rel_data, pinned) if rel_data and ev.exists() else None
    if body is None:
        return None
    path = Path(rel)
    out = target / path.parent / f"{path.stem}.{stamp}{path.suffix}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    return out


def write_evidence(ziel, data, pfade, fall, zeilen, t):
    """The evidence part of the ZIP (see the module docstring): the pinned
    versions, the manifest, the chain's lines about the case's files, the
    closing manifest and the last stamp, the README – and last the
    checksum of every file."""
    ev = evidence.Evidence(data)
    prints = evidence.Fingerprints(data, evidence.export_dirs(data))
    folder = ziel / "evidence"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        entries, rels = [], set()
        versions_dir = ziel / export_util.safe(t("cases.export.versions.folder"))
        by_key = {e["key"]: e for e in fall["eintraege_liste"]}
        for z in zeilen:
            e = by_key.get(z["key"]) or {}
            base = pfade.get(e.get("root"))
            if not z["datei"] or not base:
                continue
            rel_data = versions.rel_of(Path(base) / e["rel"], data)
            if rel_data is None:
                continue
            rels.add(rel_data)
            entries.append({"archive_rel": rel_data, "file": z["datei"], "src": e.get("src"),
                            "key": e.get("key"), "item": prints.of(e)})
            _pinned_copy(ev, prints, e, versions_dir / z["datei"].rsplit("/", 1)[0])
        rows = evidence.manifest_rows(ev, entries) if ev.exists() else []
        (folder / "manifest.csv").write_text(evidence.manifest_csv(rows), encoding="utf-8")
        lines = []
        if ev.exists():
            names = set()
            for rel in rels:
                names.update(ev.names_of(rel))
            for text, d in ev.read_lines():
                if isinstance(d, dict) and (d.get("rel") in names or d.get("kind") == "genesis"):
                    lines.append(text)
        (folder / "chain.jsonl").write_text("".join(z + "\n" for z in lines), encoding="utf-8")
        closed = evidence.case_records(data, fall["id"])
        if closed:
            newest = ev.dir / closed[0]["file"]
            (folder / "closed").mkdir(exist_ok=True)
            shutil.copy2(newest, folder / "closed" / newest.name)
            if newest.with_suffix(".tsr").is_file():
                shutil.copy2(newest.with_suffix(".tsr"), folder / "closed" / newest.with_suffix(".tsr").name)
        stamps = sorted((ev.dir / evidence.STAMPS).glob("*.tsr"),
                        key=lambda p: int(p.stem) if p.stem.isdigit() else 0) \
            if (ev.dir / evidence.STAMPS).is_dir() else []
        if stamps:
            (folder / "stamp").mkdir(exist_ok=True)
            shutil.copy2(stamps[-1], folder / "stamp" / stamps[-1].name)
            head = stamps[-1].with_suffix(".head")
            if head.is_file():
                shutil.copy2(head, folder / "stamp" / head.name)
        (folder / "README.txt").write_text(t("cases.export.verify.text"), encoding="utf-8")
    finally:
        prints.close()
        ev.close()
    sums = []
    for p in sorted(ziel.rglob("*")):
        if p.is_file():
            sums.append(f"{versions.sha256_file(p)}  {p.relative_to(ziel).as_posix()}")
    (ziel / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")


def exportieren(buch, fall_id, pfade, ziel, lang="de", res=None, data=None):
    """Build the folder `ziel` (the app names it, see zielordner, so it can
    say before the run where the ZIP will lie), pack it into <ziel>.zip
    and remove the folder. Returns (zip path, rows, missing)."""
    fall = buch.fall(fall_id)
    if fall is None:
        raise faelle.KeinFall(fall_id)
    t = Texte(_texte(lang, res))
    ziel = Path(ziel)
    ziel.mkdir(parents=True, exist_ok=True)
    eintraege = fall["eintraege_liste"]
    # The case's folders become folders in the ZIP – only when it has
    # any; a case without folders keeps the sources at the top.
    ordner = {o["id"]: o["name"] for o in fall.get("ordner_liste") or ()}
    unsortiert = t("cases.folder.unsorted")
    progress.event("run.case.start", name=fall["name"], n=len(eintraege))
    zeilen, fehlt = [], 0
    for i, e in enumerate(eintraege, 1):
        quelle, kopien, anker = plan(e, pfade)
        unter = ORDNER.get(e.get("root"), e.get("root") or "?")
        name = ordner.get(e.get("ordner")) if ordner else None
        oben = (export_util.safe(name) if name else export_util.safe(unsortiert)) if ordner else ""
        wohin = f"{oben}/{unter}" if oben else unter
        da = False
        if quelle is not None:
            for k in kopien:
                ok = _kopieren(quelle, k, ziel / wohin)
                da = da or (ok and k == kopien[0])
        datei = f"{wohin}/{e['rel']}" if da else ""
        if not da:
            fehlt += 1
        zeilen.append({"key": e["key"], "quelle": quelle_key(e), "datum": e.get("datum"),
                       "wer": e.get("wer"), "titel": e.get("titel"), "rel": e.get("rel"),
                       "datei": datei, "anker": anker if da else "",
                       "ordner": (name or unsortiert) if ordner else "", "unsortiert": bool(ordner) and not name,
                       "herkunft": _herkunft(e), "bemerkung": e.get("bemerkung") or ""})
        progress.melde(i, len(eintraege))
    (ziel / "index.html").write_text(index_html(fall, zeilen, t, lang), encoding="utf-8")
    (ziel / "timeline.html").write_text(timeline_html(fall, zeilen, t, lang), encoding="utf-8")
    (ziel / "items.csv").write_text(items_csv(zeilen, t), encoding="utf-8", newline="")
    (ziel / "casebook.md").write_text(casebook_md(fall, zeilen, t), encoding="utf-8")
    if data:
        write_evidence(ziel, data, pfade, fall, zeilen, t)
    zip_pfad = Path(shutil.make_archive(str(ziel), "zip", root_dir=ziel.parent, base_dir=ziel.name))
    shutil.rmtree(ziel, ignore_errors=True)
    # The case remembers where its last export went – the page's "Show
    # folder"; a closed case may be exported, so this is no write to it.
    buch.export_vermerken(fall_id, str(zip_pfad))
    return zip_pfad, zeilen, fehlt


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("teams")
    ap.add_argument("outlook")
    ap.add_argument("onedrive")
    ap.add_argument("--sharepoint", required=True)
    ap.add_argument("--pages", required=True)
    ap.add_argument("--planner", required=True)
    ap.add_argument("--todo", required=True)
    ap.add_argument("--onenote", required=True)
    ap.add_argument("--faelle", required=True, help="the profile's faelle.db")
    ap.add_argument("--fall", required=True, type=int, help="the case's id")
    ap.add_argument("--ziel", required=True, help="the folder to build and pack (zielordner)")
    ap.add_argument("--lang", default="de")
    ap.add_argument("--res", default=None, help="where the lang/ folder lies")
    ap.add_argument("--data", default="", help="the data folder – adds the evidence")
    a = ap.parse_args()
    pfade = {"outlook": a.outlook, "teams": a.teams, "onedrive": a.onedrive,
             "sharepoint": a.sharepoint, "pages": a.pages,
             "planner": a.planner, "todo": a.todo, "onenote": a.onenote}
    buch = faelle.Fallbuch(a.faelle)
    try:
        zip_pfad, zeilen, fehlt = exportieren(buch, a.fall, pfade, a.ziel, a.lang, a.res,
                                              data=a.data or None)
    except faelle.KeinFall:
        progress.event("run.case.unknown", "err", id=a.fall)
        progress.ergebnis(0, errors=1)
        sys.exit(1)
    except OSError as e:
        progress.event("run.case.failed", "err", error=str(e))
        progress.ergebnis(0, errors=1)
        sys.exit(1)
    if fehlt:
        progress.event("run.case.missing", "warn", n=fehlt)
    progress.event("run.case.done", path=str(zip_pfad), n=len(zeilen) - fehlt)
    progress.ergebnis(0, extra={"items": len(zeilen) - fehlt, "missing": fehlt})


if __name__ == "__main__":
    main()
