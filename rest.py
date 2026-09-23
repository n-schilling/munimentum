#!/usr/bin/env python3
"""What /api/v1 speaks, and what the app speaks.

Inside the app the data is German where it grew that way: the case book
keeps `eintraege_liste`, `titel`, `wer`, `offen`. The page and the app
share that one vocabulary, and inside the app it is worth more than
conformity would be.

Outward there is `/api/v1`: resources instead of actions, the method says
what happens, the names are English, a collection is always `items`. This
module is the one place that translates between the two, so nothing in
the app has to know about the outside spelling.

With 13.0 both doors that hold data live there – *Explore archive* with
the search, its memories and the file behind a hit, and *Cases* with the
case and everything that hangs on it. What is left on the app's own
surface is the running of the archive.
"""

# -- Cases -----------------------------------------------------------------
# One mapping per object the case book returns; anything not named here
# keeps its name (id, name, key, src, root, rel, text, status …).
FALL = {
    "beschreibung": "description", "angelegt": "created", "geaendert": "changed",
    "geschlossen": "closed_at", "exportiert": "exported", "exportiert_wann": "exported_at",
    "eintraege": "items", "je_quelle": "items_by_source", "listen": "lists",
    "notizen": "notes", "suchen": "searches", "ordner": "folders",
    "ordner_liste": "folder_list", "eintraege_liste": "item_list",
    "listen_liste": "list_list", "notizen_liste": "note_list",
    # The manifest a closed case wrote (evidence.py), or null.
    "nachweis": "evidence",
    "suchen_liste": "search_list",
}
EINTRAG = {
    "titel": "title", "datum": "date", "wer": "who", "hinzugefuegt": "added",
    "liste": "list", "ordner": "folder", "quelle": "via", "bemerkung": "remark",
    # The saved search that collected it (via: auto), else null.
    "suche": "search",
    # What the index knows on top of the case (app.py, _index_stand).
    "thread_offen": "thread_open", "wer_mail": "who_mail",
    # The version it came in with, and whether the item has changed since
    # (evidence.py).
    "fassung": "pinned", "fassung_geaendert": "changed",
}
NOTIZ = {"wann": "when", "quelle": "via"}
LISTE = {"wann": "when", "kriterien": "criteria", "anzahl": "hits", "ordner": "folder"}
SUCHE = {
    "kriterien": "criteria", "angelegt": "created", "zuletzt": "last_run",
    "treffer": "hits", "fall": "case", "fall_name": "case_name",
    # Its own folder is the case's, and `folder` is the mailbox folder in
    # its criteria – so it is `case_folder` here, the name the body takes.
    "ordner": "case_folder", "ordner_name": "case_folder_name",
    # The automatic search (13.3) and what its last collecting run did.
    "automatisch": "auto", "auto_zuletzt": "auto_last_run",
    "auto_neu": "auto_added", "auto_uebersprungen": "auto_skipped",
}
ORDNER = {"angelegt": "created", "anzahl": "items"}
# A search's criteria. `folder` is the mailbox folder and was there
# first, so the case's folder is `case_folder` – the names the search's own
# query parameters use.
KRITERIEN = {"fall": "case", "ordner": "case_folder"}
# One row of the search history: when it ran, what was asked, how many hits.
VERLAUF = {"wann": "when", "kriterien": "criteria", "treffer": "hits"}

# The case book's two states, spelled out.
STATUS = {"offen": "open", "zu": "closed"}
STATUS_ZURUECK = {v: k for k, v in STATUS.items()}


def _um(d, karte):
    """One object with its keys renamed; the order stays as it was."""
    return {karte.get(k, k): v for k, v in d.items()}


def fall(d):
    """A case as /api/v1 returns it – the summary always, the lists only
    when the case book sent them (it does for one case, not for the list)."""
    out = _um(d, FALL)
    out["status"] = STATUS.get(d.get("status"), d.get("status"))
    if "folder_list" in out:
        out["folder_list"] = [_um(o, ORDNER) for o in out["folder_list"]]
    if "item_list" in out:
        out["item_list"] = [_um(e, EINTRAG) for e in out["item_list"]]
    if "note_list" in out:
        out["note_list"] = [_um(n, NOTIZ) for n in out["note_list"]]
    if "list_list" in out:
        out["list_list"] = [{**_um(x, LISTE),
                             "criteria": _um(x["kriterien"], KRITERIEN)}
                            for x in out["list_list"]]
    if "search_list" in out:
        out["search_list"] = [{**_um(g, SUCHE),
                               "criteria": _um(g["kriterien"], KRITERIEN)}
                              for g in out["search_list"]]
    return out


def eintrag(d):
    """One item of a case as /api/v1 returns it."""
    return _um(d, EINTRAG)


def kriterien(d):
    """A search's criteria, English."""
    return _um(d, KRITERIEN)


def treffer(hits):
    """Hits as /api/v1 hands them over. The engine answers English by
    itself – only the mark that says which cases a hit sits in comes from
    the case book, in its own words."""
    for h in hits or ():
        if h.get("cases"):
            h["cases"] = [{**_um(f, {"ordner": "folder"}),
                           "status": STATUS.get(f.get("status"), f.get("status"))}
                          for f in h["cases"]]
    return hits


# The way back: what a caller sends becomes what the case book stores.
EINTRAG_ZURUECK = {v: k for k, v in EINTRAG.items()}


def eintrag_hinein(d):
    """One item as a caller hands it over – English in, German where the
    case book keeps it. Keys it does not know are dropped: a case stores
    the pointer into the archive, not whatever a client carries along."""
    erlaubt = {"key", "src", "root", "rel", "titel", "datum", "wer", "bemerkung"}
    out = _um(d or {}, EINTRAG_ZURUECK)
    return {k: v for k, v in out.items() if k in erlaubt}


def verlauf(d):
    """One search of the history, English – its criteria too."""
    out = _um(d, VERLAUF)
    out["criteria"] = kriterien(out.get("criteria") or {})
    return out


def gespeicherte_suche(d):
    """A saved search, English – its criteria too."""
    out = _um(d, SUCHE)
    out["criteria"] = kriterien(out.get("criteria") or {})
    return out
