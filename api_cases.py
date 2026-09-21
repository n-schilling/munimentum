"""
api_cases.py – the Cases door of /api/v1: the cases, their items, folders,
notes and stored lists, and the search's two memories, the history and
the saved searches. Every function takes the handler `h` (see api.py) and
answers an `Antwort`; the case book (faelle.py) is the only store.
"""
import hashlib
import json
from pathlib import Path

import api
import api_archive
import api_explore
import archive_check
import case_export
import faelle
import i18n
import rest
import store_layout
from api import Ablehnung


def faelle_liste(h, _p, _q, _data):
    return api.json({"items": [rest.fall(f) for f in h.app.faelle.faelle()]})


def anlegen(h, _p, _q, data):
    try:
        neu = h.app.faelle.fall_anlegen(data.get("name"), data.get("description"))
    except ValueError:
        raise Ablehnung(400, "srv.case.noname") from None
    return fall_antwort(h, neu, code=201, ort=f"{api.API_V1}/cases/{neu}")


def kennzeichen(h, fall):
    """The case's ETag (RFC 9110): a digest of the case as the book keeps
    it – every write changes it, two in one second included, which a
    stamp would miss – and the index's build, because an item's
    `thread_open` and `who_mail` come from the index. Weak: two builds of
    the same archive differ in nothing but that stamp."""
    inhalt = json.dumps(fall, sort_keys=True, default=str, ensure_ascii=False)
    index = h.M._mtime_iso(store_layout.db_path(h.M.STORE_PFAD)) or ""
    return f'W/"{hashlib.sha1(inhalt.encode()).hexdigest()[:16]}.{index}"'


def _kopfliste(h, name):
    roh = h.headers.get(name, "") if getattr(h, "headers", None) else ""
    return [t.strip() for t in str(roh).split(",") if t.strip()]


def minimal_gewuenscht(h):
    """RFC 7240: `Prefer: return=minimal` asks for the status alone."""
    return "return=minimal" in [t.lower() for t in _kopfliste(h, "Prefer")]


def unveraendert(h, etag):
    """RFC 9110 `If-None-Match`, compared weakly: the tag with or without
    its `W/`, or `*`."""
    schwach = etag.removeprefix("W/")
    marken = [t.removeprefix("W/") for t in _kopfliste(h, "If-None-Match")]
    return "*" in marken or schwach in marken


def fall(h, p, _q, _data):
    """One case in full. With `If-None-Match` a case that has not changed
    – nor the index its items are read against – is a 304 without a
    body: the tag is what the last answer carried."""
    fall = h.app.faelle.fall(api.kennung(p))
    if fall is None:
        raise Ablehnung(404, "srv.case.unknown")
    etag = kennzeichen(h, fall)
    if unveraendert(h, etag):
        return api.Antwort(304, None, {"ETag": etag})
    index_stand(h, fall)
    return api.json({"case": rest.fall(fall)}, extra={"ETag": etag})


def aendern(h, p, _q, data):
    """PATCH: the fields the body names, nothing else. `status` opens
    and closes the case – the action routes of the app's own surface
    do the same thing under their own names.

    Everything that can be judged is judged before anything changes:
    a PATCH takes hold whole, or leaves the case as it was. The case
    book has no transaction across two calls, so the order does the
    work – opening first, because the fields need the case open,
    closing last, because nothing follows it.
    """
    kennung, buch = api.kennung(p), h.app.faelle
    fall = buch.fall(kennung)
    if fall is None:
        raise Ablehnung(404, "srv.case.unknown")
    ziel = None
    if "status" in data:
        ziel = rest.STATUS_ZURUECK.get(str(data.get("status") or ""))
        if ziel is None:
            raise Ablehnung(400, "srv.case.badstatus",
                            {"status": str(data.get("status") or "")})
        if ziel == fall["status"]:
            ziel = None                  # already there: nothing to do
    felder = "name" in data or "description" in data
    if "name" in data and not str(data.get("name") or "").strip():
        raise Ablehnung(400, "srv.case.noname")
    if felder and fall["status"] != faelle.OFFEN and ziel != faelle.OFFEN:
        raise Ablehnung(409, "srv.case.closed")
    try:
        if ziel == faelle.OFFEN:
            buch.oeffnen(kennung)
        if felder:
            buch.fall_aendern(kennung, data.get("name") if "name" in data else None,
                              data.get("description") if "description" in data else None)
        if ziel is not None and ziel != faelle.OFFEN:
            buch.schliessen(kennung)
    except ValueError:
        raise Ablehnung(400, "srv.case.noname") from None
    return fall_antwort(h, kennung)


def loeschen(h, p, _q, _data):
    kennung = api.kennung(p)
    if h.app.faelle.fall(kennung) is None:
        raise Ablehnung(404, "srv.case.unknown")
    h.app.faelle.fall_loeschen(kennung)
    return api.leer()


def fall_da(h, p):
    """The case a path names, in full – or a 404. Only where its items
    are read with the index's word on them (`thread_open`,
    `who_mail`): everything else takes `fall_buch`, which does
    not walk the index for a case it is about to answer with anyway."""
    fall = fall_voll(h, api.kennung(p))
    if fall is None:
        raise Ablehnung(404, "srv.case.unknown")
    return fall


def fall_buch(h, p):
    """The case as the case book keeps it – items, folders, notes,
    lists, searches – or a 404. No index: enough to resolve a
    sub-resource before a write, and to read anything but an item."""
    fall = h.app.faelle.fall(api.kennung(p))
    if fall is None:
        raise Ablehnung(404, "srv.case.unknown")
    return fall


def fall_id(h, p):
    """The id of the case a path names – or a 404."""
    return fall_buch(h, p)["id"]


def fall_offen(fall, ordner=None):
    """What the case book would refuse on the first write, refused
    before anything expensive runs and with the same answer: the case
    is open, and the folder – where one is named – is its own."""
    if fall["status"] != faelle.OFFEN:
        raise Ablehnung(409, "srv.case.closed")
    if ordner is not None and all(o["id"] != ordner for o in fall["ordner_liste"]):
        raise Ablehnung(404, "srv.case.nofolder")


def fall_antwort(h, kennung, extra=None, code=200, ort=None):
    """Every write answers the case it changed: the page never has to
    guess what a change did, and a script gets the new state in the
    same call. A script that does not want it says so – `Prefer:
    return=minimal` (RFC 7240) – and gets the status, the `Location`
    and the `ETag` alone, and `Preference-Applied` to say so."""
    fall = h.app.faelle.fall(kennung)
    # The tag before the index's word is added – the same basis a GET
    # compares against, and the 304 must not need the index walk.
    kopf = {**({"Location": ort} if ort else {}), "ETag": kennzeichen(h, fall)}
    if minimal_gewuenscht(h):
        kopf["Preference-Applied"] = "return=minimal"
        return api.Antwort(204 if code == 200 else code, None, kopf)
    index_stand(h, fall)
    return api.json({**(extra or {}), "case": rest.fall(fall)}, code, extra=kopf)


def eintraege(h, p, _q, _data):
    return api.json({"items": rest.fall(fall_da(h, p))["item_list"]})


def teil(h, p, liste, feld):
    """One row of a case's collection on its own – the object the
    collection answers, and what `Location` named when it was created.
    Read from the translated case, so one row says exactly what the
    list says about it; a note, a folder or a list is the case book's
    alone."""
    fall = rest.fall(fall_buch(h, p))
    zeile = api.zeile(fall[liste], api.teilkennung(p, feld), api.TEIL_FEHLT[feld])
    return api.json({feld: zeile})


def eintrag(h, p, _q, _data):
    """One item with the index's word on it (`thread_open`, `who_mail`)
    – asked for this item alone, not for the whole case's worth, which
    is what the list does."""
    e = eintrag_da(h, fall_buch(h, p), p)
    index_stand(h, {"eintraege_liste": [e]})
    return api.json({"item": rest.eintrag(e)})


def fallordner_eins(h, p, _q, _data):
    return teil(h, p, "folder_list", "folder")


def notiz(h, p, _q, _data):
    return teil(h, p, "note_list", "note")


def liste_eine(h, p, _q, _data):
    return teil(h, p, "list_list", "list")


def eintraege_hinzu(h, p, _q, data):
    """Items into the case. `items` are the rows themselves – what a
    hit carries; `threads` names items already in the case whose
    conversations should be completed. Both may stand in one body."""
    fall = fall_buch(h, p)
    kennung = fall["id"]
    # A row without a key names nothing in the archive and is no item:
    # dropped here, as the case book would drop it – but a body that
    # carries nothing else is refused, not answered with `added: 0`.
    eintraege = [e for e in (rest.eintrag_hinein(e) for e in (data.get("items") or [])
                             if isinstance(e, dict))
                 if str(e.get("key") or "").strip()]
    schluessel = [str(k) for k in (data.get("threads") or []) if isinstance(k, str)]
    if not eintraege and not schluessel:
        raise Ablehnung(400, "srv.case.noitems")
    ordner = api.ordner_aus(data, "folder")
    # Everything that can refuse happens before the first write: a
    # request that adds its items and then fails on the conversations
    # would have changed the case and reported failure, and a caller
    # who retries adds nothing twice but is told so.
    fall_offen(fall, ordner)
    if schluessel:
        fehler = gespraeche_moeglich(h)
        if fehler:
            raise Ablehnung(409, fehler)
    neu = 0
    if eintraege:
        neu = h.app.faelle.hinzufuegen(kennung, eintraege, ordner_id=ordner)
    dazu = 0
    if schluessel:
        dazu, fehler = thread_holen(h, kennung, schluessel)
        if fehler:
            raise Ablehnung(409, fehler)
    gab_es = len(eintraege) - neu
    return fall_antwort(h, kennung, {"added": neu + dazu, "already": gab_es},
                                 201 if neu + dazu else 200)


def eintraege_verschieben(h, p, _q, data):
    """Several items into one folder at once – what a selection in the
    case does. `folder: null` means unsorted."""
    kennung = fall_id(h, p)
    keys = [str(k) for k in (data.get("keys") or []) if isinstance(k, str)]
    if not keys:
        raise Ablehnung(400, "srv.case.noitems")
    n = h.app.faelle.verschieben(kennung, keys, api.ordner_aus(data, "folder"))
    return fall_antwort(h, kennung, {"moved": n})


def eintrag_aendern(h, p, _q, data):
    """One item: its remark, its folder, or both."""
    fall = fall_buch(h, p)
    kennung, eintrag = fall["id"], eintrag_da(h, fall, p)
    buch = h.app.faelle
    # The whole body is judged before the first write: a folder the
    # case does not have, or a closed case, would otherwise leave the
    # remark behind and refuse.
    ordner = api.ordner_aus(data, "folder") if "folder" in data else None
    fall_offen(fall, ordner)
    if "remark" in data:
        buch.bemerkung_setzen(kennung, eintrag["key"], data.get("remark"))
    if "folder" in data:
        buch.verschieben(kennung, [eintrag["key"]], ordner)
    return fall_antwort(h, kennung)


def eintrag_loeschen(h, p, _q, _data):
    fall = fall_buch(h, p)
    h.app.faelle.entfernen(fall["id"], eintrag_da(h, fall, p)["key"])
    return fall_antwort(h, fall["id"])


def eintrag_da(h, fall, p):
    """The item a path names – by the id the case gave it."""
    return api.zeile(fall["eintraege_liste"], api.teilkennung(p, "item"),
                          api.TEIL_FEHLT["item"])


def fallordner(h, p, _q, _data):
    return api.json({"items": rest.fall(fall_buch(h, p))["folder_list"]})


def fallordner_anlegen(h, p, _q, data):
    """A new folder. The case book hands back an existing one of that
    name; on this surface 201 means created, so a name that is there
    already is a 409 – as a rename onto it is."""
    fall = fall_buch(h, p)
    kennung, name = fall["id"], str(data.get("name") or "").strip()
    if name and any(o["name"] == name for o in fall["ordner_liste"]):
        raise Ablehnung(409, "srv.case.folder.exists")
    try:
        ordner = h.app.faelle.ordner_anlegen(kennung, name)
    except ValueError as e:
        raise Ablehnung(*wert_fehler(e)) from None
    return fall_antwort(h, kennung, {"folder": ordner}, 201,
                                 f"{api.API_V1}/cases/{kennung}/folders/{ordner}")


def fallordner_aendern(h, p, _q, data):
    kennung = fall_id(h, p)
    try:
        h.app.faelle.ordner_umbenennen(kennung, api.teilkennung(p, "folder"),
                                          data.get("name"))
    except ValueError as e:
        raise Ablehnung(*wert_fehler(e)) from None
    return fall_antwort(h, kennung)


def fallordner_loeschen(h, p, _q, _data):
    """The folder goes, its items stay – unsorted, in the case."""
    kennung = fall_id(h, p)
    h.app.faelle.ordner_loeschen(kennung, api.teilkennung(p, "folder"))
    return fall_antwort(h, kennung)


def notizen(h, p, _q, _data):
    return api.json({"items": rest.fall(fall_buch(h, p))["note_list"]})


def notiz_anlegen(h, p, _q, data):
    kennung = fall_id(h, p)
    try:
        notiz = h.app.faelle.notiz(kennung, data.get("text"))
    except ValueError:
        raise Ablehnung(400, "srv.case.noname") from None
    return fall_antwort(h, kennung, {"note": notiz}, 201,
                                 f"{api.API_V1}/cases/{kennung}/notes/{notiz}")


def notiz_aendern(h, p, _q, data):
    """The case book answers whether the note was there; one that is
    not is a 404, not a quiet 200 with the case unchanged."""
    kennung = fall_id(h, p)
    try:
        da = h.app.faelle.notiz_aendern(kennung, api.teilkennung(p, "note"),
                                           data.get("text"))
    except ValueError:
        raise Ablehnung(400, "srv.case.noname") from None
    if not da:
        raise Ablehnung(404, "srv.case.nonote")
    return fall_antwort(h, kennung)


def notiz_loeschen(h, p, _q, _data):
    kennung = fall_id(h, p)
    if not h.app.faelle.notiz_loeschen(kennung, api.teilkennung(p, "note")):
        raise Ablehnung(404, "srv.case.nonote")
    return fall_antwort(h, kennung)


def listen(h, p, _q, _data):
    return api.json({"items": rest.fall(fall_buch(h, p))["list_list"]})


def liste_anlegen(h, p, _q, data):
    """A whole result as it stood at this moment: the criteria are run
    once more here, every hit goes into the case, and the list keeps
    what it was."""
    fall = fall_buch(h, p)
    kennung = fall["id"]
    ordner = api.ordner_aus(data, "folder")
    k = faelle.kriterien(data.get("criteria"))
    # The search is the expensive part: what the case book would
    # refuse after it – a closed case, a folder it does not have – is
    # refused before it.
    fall_offen(fall, ordner)
    treffer, fehler = api_explore.alle_treffer(h, k)
    if fehler:
        raise Ablehnung(409, fehler)
    liste, neu = h.app.faelle.liste_anlegen(kennung, k, treffer, ordner_id=ordner)
    return fall_antwort(h, kennung, {"list": liste, "hits": len(treffer), "added": neu},
                                 201, f"{api.API_V1}/cases/{kennung}/lists/{liste}")


def liste_loeschen(h, p, _q, _data):
    """The list goes, and with it the items it brought; what was added
    another way stays, folder and remark included."""
    kennung = fall_id(h, p)
    if not h.app.faelle.liste_loeschen(kennung, api.teilkennung(p, "list")):
        raise Ablehnung(404, "srv.case.nolist")
    return fall_antwort(h, kennung)


def neue_treffer(h, p, _q, _data):
    """What the searches attached to this case find today that the case
    does not hold yet – per search, never stored."""
    fall = fall_buch(h, p)
    buch = h.app.faelle
    keys = buch.keys(fall["id"])
    bloecke = []
    for g in fall["suchen_liste"]:
        treffer, fehler = api_explore.alle_treffer(h, g["kriterien"], grenze=500)
        if fehler:
            bloecke.append({"id": g["id"], "name": g["name"], "error": fehler, "new": []})
            continue
        neu = rest.treffer([h for h in treffer
                            if h.get("key") and h["key"] not in keys])
        bloecke.append({"id": g["id"], "name": g["name"], "new": neu[:200],
                        "new_count": len(neu), "folder": g.get("ordner"),
                        "folder_name": g.get("ordner_name")})
    return api.json({"case": {"id": fall["id"], "name": fall["name"]}, "items": bloecke})


def fall_export(h, p, _q, _data):
    return fall_exportieren(h, fall_id(h, p))


def fall_export_oeffnen(h, p, _q, _data):
    """Show the export in the file manager – a side effect on this
    machine, not a change to anything here."""
    fall = fall_buch(h, p)
    pfad = Path(fall["exportiert"]) if fall.get("exportiert") else None
    if pfad is not None and pfad.suffix == ".zip":
        pfad = pfad.parent
    if pfad is None or not pfad.is_dir():
        pfad = h.M.fall_export_basis(h.app.cfg)
        if not pfad.is_dir():
            raise Ablehnung(404, "srv.case.noexport")
    if not archive_check.ordner_oeffnen(pfad):
        raise Ablehnung(500, "srv.archiv.open.fail", path=str(pfad))
    return api.json({"path": str(pfad)})


def wert_fehler(e):
    """What the case book means by a ValueError: a folder that is there
    already, or a name that is none."""
    return (409, "srv.case.folder.exists") if str(e) == "exists" else (400, "srv.case.noname")


def verlauf(h, _p, q, _data):
    """Every search that ran, newest first – criteria only, never hits.
    `retention` is what the setting keeps them for, in days."""
    buch = h.app.faelle
    grenze = api.zahl(q, "limit", 200, 1, 500)
    return api.liste(
        {"verlauf": [rest.verlauf(x) for x in buch.suchen(grenze + 1)],
         "retention": str(h.app.cfg.get("search_history") or "90")},
        "verlauf", grenze)


def verlauf_leeren(h, _p, _q, _data):
    h.app.faelle.suchen_leeren()
    return api.leer()


def gespeicherte(h, _p, _q, _data):
    return api.json({"items": [rest.gespeicherte_suche(g)
                                 for g in h.app.faelle.gespeicherte()]})


def suche_eine(h, p, _q, _data):
    """One saved search – what `Location` named when it was saved."""
    g = h.app.faelle.gespeichert(suche_kennung(h, p))
    if g is None:
        raise Ablehnung(404, "srv.search.unknown")
    return api.json({"search": rest.gespeicherte_suche(g)})


def speichern(h, _p, _q, data):
    buch = h.app.faelle
    fall_id = fall_aus(h, data)
    ordner = api.ordner_aus(data, "case_folder")
    if ordner is not None and fall_id is None:
        # A folder is a case's: named without one, the request is
        # malformed – not accepted and dropped, as the case book would.
        raise Ablehnung(400, "srv.case.folder.nocase")
    try:
        neu = buch.speichern(data.get("name"), faelle.kriterien(data.get("criteria")),
                             fall_id, ordner)
    except ValueError:
        raise Ablehnung(400, "srv.case.noname") from None
    return api.json({"search": rest.gespeicherte_suche(buch.gespeichert(neu))}, 201,
                      extra={"Location": f"{api.API_V1}/searches/saved/{neu}"})


def suche_aendern(h, p, _q, data):
    """PATCH: the fields the body names. `name` renames, `case` attaches
    the search to a case (`null` detaches it) and `case_folder` says
    into which of its folders new hits go – on its own, for the case
    the search is attached to; with `case`, for the new one."""
    buch, kennung = h.app.faelle, suche_kennung(h, p)
    aktuell = buch.gespeichert(kennung)
    if aktuell is None:
        raise Ablehnung(404, "srv.search.unknown")
    if "name" in data and not str(data.get("name") or "").strip():
        raise Ablehnung(400, "srv.case.noname")
    fall_id = fall_aus(h, data) if "case" in data else aktuell["fall"]
    if "case_folder" in data:
        ordner = api.ordner_aus(data, "case_folder")
    else:
        # Only the named fields change: the folder stays with its case
        # and goes only when the case does – a folder is one case's.
        ordner = aktuell["ordner"] if fall_id == aktuell["fall"] else None
    if ordner is not None and fall_id is None:
        # A folder is a case's; a search attached to none has nothing
        # it could file into – the same 400 the POST gives.
        raise Ablehnung(400, "srv.case.folder.nocase")
    if fall_id is not None and ("case" in data or "case_folder" in data):
        # Judged before the rename: a closed case or a folder it lacks
        # must not leave the new name behind. The case book raises,
        # the dispatcher answers – the one mapping for every route.
        fall = h.app.faelle.fall(fall_id)
        if fall is None:
            raise Ablehnung(404, "srv.case.unknown")
        fall_offen(fall, ordner)
    if "name" in data:
        buch.umbenennen(kennung, data.get("name"))
    if "case" in data or "case_folder" in data:
        buch.anhaengen(kennung, fall_id, ordner)
    return api.json({"search": rest.gespeicherte_suche(buch.gespeichert(kennung))})


def suche_loeschen(h, p, _q, _data):
    if not h.app.faelle.loeschen(suche_kennung(h, p)):
        raise Ablehnung(404, "srv.search.unknown")
    return api.leer()


def suche_kennung(h, p):
    return api.id_aus(p, "id", "srv.search.unknown")


def fall_aus(h, data, feld="case"):
    """The case a body names: a number, or None for "no case". A case
    that does not exist is a 404, not a silent detach."""
    wert = data.get(feld)
    if wert in (None, "", 0, "0"):
        return None
    try:
        kennung = int(wert)
    except (TypeError, ValueError):
        # Not a missing case but a malformed request – like a folder
        # that is no id (`_ordner_aus`).
        raise Ablehnung(400, "srv.case.badcase", {"value": str(wert)[:40]}) from None
    if h.app.faelle.fall(kennung) is None:
        raise Ablehnung(404, "srv.case.unknown")
    return kennung


def fall_voll(h, kennung):
    """The case as every write answers it – with the conversations'
    state on its items."""
    fall = h.app.faelle.fall(kennung)
    if fall is not None:
        index_stand(h, fall)
    return fall


def index_stand(h, fall):
    """What the index knows about the case's items beyond what the case
    remembers: `thread_offen` – how many messages of the item's
    conversation the case lacks, 0 where there is none – and
    `wer_mail`, the address behind `wer` where the index has one. Two
    queries, whatever the case's size; an index without the columns
    leaves the zeros and the empty strings."""
    for e in fall["eintraege_liste"]:
        e["thread_offen"] = 0
        e["wer_mail"] = ""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None or not fall["eintraege_liste"]:
        return
    try:
        con = mod._db()
    except Exception:
        return
    try:
        if not mod._hat_spalte(con, "key"):
            return
        mit_thread = mod._hat_spalte(con, "thread")
        mit_adresse = mod._hat_spalte(con, "who_mail")
        if not (mit_thread or mit_adresse):
            return
        mod._keys_tabelle(con, {e["key"] for e in fall["eintraege_liste"]})
        felder = "key, " + ("thread" if mit_thread else "NULL") + ", " + ("who_mail" if mit_adresse else "NULL")
        je_key, im_fall, adressen = {}, {}, {}
        for key, thread, who_mail in con.execute(
                f"SELECT {felder} FROM chunks WHERE seq = 0 AND key IN (SELECT key FROM fallkeys)"):
            if thread:
                je_key[key] = thread
                im_fall[thread] = im_fall.get(thread, 0) + 1
            if who_mail:
                adressen[key] = who_mail
        for e in fall["eintraege_liste"]:
            e["wer_mail"] = adressen.get(e["key"], "")
        if not je_key:
            return
        con.execute("CREATE TEMP TABLE IF NOT EXISTS fallthreads(thread TEXT PRIMARY KEY)")
        con.execute("DELETE FROM fallthreads")
        con.executemany("INSERT OR IGNORE INTO fallthreads(thread) VALUES(?)",
                        ((t,) for t in im_fall))
        gesamt = dict(con.execute(
            "SELECT thread, COUNT(*) FROM chunks WHERE seq = 0 AND key IS NOT NULL AND key != '' "
            "AND thread IN (SELECT thread FROM fallthreads) GROUP BY thread"))
        for e in fall["eintraege_liste"]:
            t = je_key.get(e["key"])
            if t:
                e["thread_offen"] = max(0, gesamt.get(t, 0) - im_fall.get(t, 0))
    finally:
        con.close()


def gespraeche_moeglich(h):
    """Can this index answer for conversations at all? The columns came
    with 11.0/11.1; an older index cannot, and that has to be known
    before anything is written."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error)
    con = mod._db()
    try:
        if not (mod._hat_spalte(con, "thread") and mod._hat_spalte(con, "key")):
            return {"k": "srv.case.nothread", "v": {}}
    finally:
        con.close()
    return None


def thread_holen(h, kennung, keys):
    """The rest of these items' conversations into the case – each
    message into the folder its item sits in. Returns (added, error)."""
    buch = h.app.faelle
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error)
    fall = buch.fall(kennung)
    con = mod._db()
    try:
        if not (mod._hat_spalte(con, "thread") and mod._hat_spalte(con, "key")):
            return 0, {"k": "srv.case.nothread", "v": {}}
        im_fall = {e["key"]: e for e in fall["eintraege_liste"]}
        neu = 0
        for key in keys:
            e = im_fall.get(key)
            if e is None:
                continue
            r = con.execute("SELECT thread FROM chunks WHERE key = ? AND seq = 0 LIMIT 1",
                            (key,)).fetchone()
            if r is None or not r[0]:
                continue
            rows = con.execute(
                "SELECT * FROM chunks WHERE thread = ? AND seq = 0 AND key IS NOT NULL AND key != '' "
                "ORDER BY ts IS NULL, ts LIMIT 500", (r[0],)).fetchall()
            eintraege = [{"key": m["key"], "src": m["src"], "root": m["root"], "rel": m["rel"],
                          "titel": m["title"], "datum": m["date"], "wer": m["who"]}
                         for m in rows if m["key"] not in im_fall]
            if eintraege:
                neu += buch.hinzufuegen(kennung, eintraege, ordner_id=e.get("ordner"))
                for x in eintraege:
                    im_fall[x["key"]] = x
        return neu, None
    finally:
        con.close()


def fall_exportieren(h, kennung):
    """The export as a run of its own (case_export.py): one ZIP, named
    here before the run, so the answer can already say where it will
    lie."""
    app = h.app
    fall = app.faelle.fall(kennung)
    if app.jobs.busy:
        raise Ablehnung(409, "srv.busy")
    basis = h.M.fall_export_basis(app.cfg)
    try:
        basis.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Ablehnung(400, "srv.case.exportdir", {"error": str(e)}) from None
    ziel = case_export.zielordner(basis, fall["name"])
    ok, why = app.launch({"fall_export": True}, label="job.case_export",
                         fall_export={"faelle": str(h.M.HEIM / faelle.DB_NAME), "fall": kennung,
                                      "ziel": str(ziel),
                                      "lang": app.ui_lang or i18n.negotiate(app.cfg.get("language"), None, h.M.RES),
                                      "res": str(h.M.RES)})
    if not ok:
        raise Ablehnung(409, why)
    return api_archive.lauf_antwort(h, extra={"path": str(ziel.with_suffix(".zip"))})


# The routes of this door, in the order the table in app.py lists them.
ROUTEN = (
    ("GET", "/api/v1/cases", faelle_liste),
    ("POST", "/api/v1/cases", anlegen),
    ("GET", "/api/v1/cases/{id}", fall),
    ("PATCH", "/api/v1/cases/{id}", aendern),
    ("DELETE", "/api/v1/cases/{id}", loeschen),
    ("GET", "/api/v1/cases/{id}/items", eintraege),
    ("POST", "/api/v1/cases/{id}/items", eintraege_hinzu),
    ("PATCH", "/api/v1/cases/{id}/items", eintraege_verschieben),
    ("GET", "/api/v1/cases/{id}/items/{item}", eintrag),
    ("PATCH", "/api/v1/cases/{id}/items/{item}", eintrag_aendern),
    ("DELETE", "/api/v1/cases/{id}/items/{item}", eintrag_loeschen),
    ("GET", "/api/v1/cases/{id}/folders", fallordner),
    ("POST", "/api/v1/cases/{id}/folders", fallordner_anlegen),
    ("GET", "/api/v1/cases/{id}/folders/{folder}", fallordner_eins),
    ("PATCH", "/api/v1/cases/{id}/folders/{folder}", fallordner_aendern),
    ("DELETE", "/api/v1/cases/{id}/folders/{folder}", fallordner_loeschen),
    ("GET", "/api/v1/cases/{id}/notes", notizen),
    ("POST", "/api/v1/cases/{id}/notes", notiz_anlegen),
    ("GET", "/api/v1/cases/{id}/notes/{note}", notiz),
    ("PATCH", "/api/v1/cases/{id}/notes/{note}", notiz_aendern),
    ("DELETE", "/api/v1/cases/{id}/notes/{note}", notiz_loeschen),
    ("GET", "/api/v1/cases/{id}/lists", listen),
    ("POST", "/api/v1/cases/{id}/lists", liste_anlegen),
    ("GET", "/api/v1/cases/{id}/lists/{list}", liste_eine),
    ("DELETE", "/api/v1/cases/{id}/lists/{list}", liste_loeschen),
    ("GET", "/api/v1/cases/{id}/new-hits", neue_treffer),
    ("POST", "/api/v1/cases/{id}/export", fall_export),
    ("POST", "/api/v1/cases/{id}/export/open", fall_export_oeffnen),
    ("GET", "/api/v1/searches/history", verlauf),
    ("DELETE", "/api/v1/searches/history", verlauf_leeren),
    ("GET", "/api/v1/searches/saved", gespeicherte),
    ("POST", "/api/v1/searches/saved", speichern),
    ("GET", "/api/v1/searches/saved/{id}", suche_eine),
    ("PATCH", "/api/v1/searches/saved/{id}", suche_aendern),
    ("DELETE", "/api/v1/searches/saved/{id}", suche_loeschen),
)
