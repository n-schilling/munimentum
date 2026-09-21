"""
api.py – what every versioned route shares: the refusal, the answer, the
parsers a body or a query is read with, and the few tables the routes
agree on. The route modules (api_cases, api_explore, api_archive, api_app)
take the handler `h` – for `h.app`, the running App, and `h.M`, the running
app module with its paths – and return an `Antwort`, which the handler
sends. Nothing here touches a socket.
"""
import faelle

API_VERSION = "v1"          # the contract's version, not the program's
API_V1 = "/api/" + API_VERSION


class Ablehnung(Exception):
    """A refusal raised inside a route; the handler turns it into the one
    error body (Handler._fehler). `grund` is a text key, an i18n message
    or a plain sentence from the search module; `extra` are the fields
    the answer keeps beside it (an empty list, the token's state)."""

    def __init__(self, code, grund, v=None, **extra):
        super().__init__(grund)
        self.code, self.grund, self.v, self.extra = code, grund, v, extra


class Antwort:
    """What a route answers: a body – a dict or list, sent as JSON; bytes,
    sent as they are under `ctype`; None, a 204 – with its status and the
    headers the status calls for. `nachher` runs once the answer is out
    – for the one route that stops the server after answering."""

    def __init__(self, code=200, body=None, headers=None, ctype=None, nachher=None):
        self.code, self.body, self.headers, self.ctype = code, body, headers or {}, ctype
        self.nachher = nachher


def json(obj, code=200, extra=None, nachher=None):
    """A JSON body – the handler serialises it."""
    return Antwort(code, obj, extra, nachher=nachher)


def leer(extra=None):
    return Antwort(204, None, extra)


def roh(code, body, ctype, extra=None):
    """Bytes or text under their own content type."""
    return Antwort(code, body.encode("utf-8") if isinstance(body, str) else body, extra, ctype)


# What a saved search stores and what the engine is asked for are two
# vocabularies for one thing: the interface named its three modes after
# what they do for the user, the engine after how it ranks. A stored
# criteria set is handed back to /api/v1/search unchanged, so the route
# understands both spellings.
MODUS = faelle.ENGINE_MODUS

# The six of the eight sources with folder rules to preview. The mailbox
# has two rule sets – its folders and its calendars – told apart by
# `unit`, not by a source of their own. A name outside the eight is
# unknown; one of the eight without rules says so.
PLAN_QUELLEN = ("outlook", "onedrive", "sharepoint", "teams", "todo", "onenote")
PLAN_EINHEITEN = ("mail", "calendar")

# What a sub-resource of a case says when its id names nothing there.
TEIL_FEHLT = {"folder": "srv.case.nofolder", "note": "srv.case.nonote",
              "list": "srv.case.nolist", "item": "srv.case.noitem"}


def modus(wert):
    wert = str(wert or "auto").strip().lower()
    return MODUS.get(wert, wert)


def zahl(q, name, vorgabe=0, kleinste=None, groesste=None):
    """A number out of a query or a body, clamped where the route has
    bounds. Something that is not a number is the caller's mistake and
    answers 400 – not the 500 an uncaught ValueError would be."""
    roh = q.get(name, vorgabe)
    try:
        wert = int(roh if roh not in (None, "") else vorgabe)
    except (TypeError, ValueError):
        raise Ablehnung(400, "srv.badparam", {"name": name}) from None
    if kleinste is not None:
        wert = max(kleinste, wert)
    if groesste is not None:
        wert = min(wert, groesste)
    return wert


def id_aus(p, name, fehlt):
    """The {name} of a path as a number. A value that is not one names
    nothing that exists – `fehlt` says what: the case, the search,
    the run, the row of a case's collection."""
    try:
        return int(p[name])
    except (KeyError, TypeError, ValueError):
        raise Ablehnung(404, fehlt) from None


def zeile(zeilen, kennung, fehlt):
    """The row with this id, or the 404 that names its collection."""
    for zeile in zeilen:
        if zeile.get("id") == kennung:
            return zeile
    raise Ablehnung(404, fehlt)


def kennung(p):
    """The {id} of a case path."""
    return id_aus(p, "id", "srv.case.unknown")


def teilkennung(p, name):
    """The id of a sub-resource in the path – the folder, the note, the
    list, the item. A value that is not a number names nothing in that
    collection, and says so: the case it sits in is there."""
    return id_aus(p, name, TEIL_FEHLT[name])


def ordner_aus(data, feld="ordner"):
    """A folder id from a request body: an integer, or None for
    "unsorted". Anything else is the caller's mistake – silently
    unfiling an item on a value nobody could read looks exactly like
    the documented way to say "out of its folder"."""
    wert = data.get(feld)
    if wert in (None, "", 0, "0"):
        return None
    try:
        return int(wert)
    except (TypeError, ValueError):
        raise Ablehnung(400, "srv.case.badfolder",
                        {"value": str(wert)[:40]}) from None


def liste(res, schluessel, grenze=None):
    """A collection answers `items` – one name for every list on this
    surface, whatever the engine calls its own.

    These lists are capped, not paged: the engine orders them by what
    makes them useful (count, then name) and stops at `limit`. Whether
    the cap cut something off is `has_more` – a caller that needs the
    rest narrows with `contains` or `source` rather than paging."""
    daten = dict(res)
    roh = daten.pop(schluessel, [])
    if grenze:
        # The engine was asked for one more than the cap: whether it
        # came says exactly whether the cap cut something off.
        daten["items"] = roh[:grenze]
        daten["limit"] = grenze
        daten["has_more"] = len(roh) > grenze
        if "count" in daten:
            daten["count"] = len(daten["items"])
    else:
        daten["items"] = roh
    return json(daten)
