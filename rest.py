#!/usr/bin/env python3
"""The versioned surface (/api/v1) speaks English and REST.

The app's own interface grew with the app: its routes name actions
(`/api/faelle/anlegen`), everything is a POST, and where the data is
German the keys are too – the page and the app share one vocabulary, and
that is worth more inside the app than conformity would be.

For everyone else there is `/api/v1`: resources instead of actions, the
method says what happens, the names are English. This module is the one
place that translates between the two, so the case book keeps its own
words and nothing in the app has to know about the outside spelling.

Only cases and the search live there so far. The page moves over one
resource at a time – its search already has – and the action routes go
in 13.0, when none of them is left in use.
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
    "suchen_liste": "search_list",
}
EINTRAG = {
    "titel": "title", "datum": "date", "wer": "who", "hinzugefuegt": "added",
    "liste": "list", "ordner": "folder", "quelle": "via", "bemerkung": "remark",
    # What the index knows on top of the case (app.py, _index_stand).
    "thread_offen": "thread_open", "wer_mail": "who_mail",
}
NOTIZ = {"wann": "when", "quelle": "via"}
LISTE = {"wann": "when", "kriterien": "criteria", "anzahl": "hits", "ordner": "folder"}
SUCHE = {
    "kriterien": "criteria", "angelegt": "created", "zuletzt": "last_run",
    "treffer": "hits", "fall": "case", "fall_name": "case_name",
    "ordner": "folder", "ordner_name": "folder_name",
}
ORDNER = {"angelegt": "created", "anzahl": "items"}
KRITERIEN = {"fall": "case", "ordner": "folder"}

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


def kriterien(d):
    """A search's criteria, English."""
    return _um(d, KRITERIEN)
