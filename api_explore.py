"""
api_explore.py – the Explore door of /api/v1: the search and what it
narrows by, the mirrored files, one item with its facts and attachments.
Everything here reads the index through the search module the app loaded
(`h.app.search`); nothing writes.
"""
from pathlib import Path

import api
import detail
import faelle
import rest
from api import Ablehnung


def suche(h, _p, q, _data):
    """The same engine as the page's search, paged the usual way:
    `limit` and `offset`, and `has_more` instead of a total – the
    ranking would have to run to the end for a total, and a search
    across the whole archive is not worth that."""
    grenze = api.zahl(q, "limit", 20, 1, 100)
    versatz = api.zahl(q, "offset", 0, 0)
    # The search history is the page's. A caller says whether this
    # search belongs in it – `remember`, off unless asked for, because
    # a script paging through the archive would otherwise fill the
    # rows the page offers a human. `saved` only counts along with it.
    merken = str(q.get("remember") or "").strip().lower() in ("1", "true", "yes", "ja")
    kriterien = dict(q) if merken else {k: v for k, v in q.items() if k != "saved"}
    res = search(h, {**kriterien, "k": str(grenze + 1),
                        "offset": str(versatz)}, merken=merken)
    treffer = rest.treffer(res.get("results") or [])
    return api.json({"items": treffer[:grenze], "limit": grenze,
                       "offset": versatz, "has_more": len(treffer) > grenze,
                       "backend": res.get("backend"),
                       "semantic": res.get("semantic")})


def dateien(h, _p, q, _data):
    return api.json(files(h, q))


def ordner(h, _p, q, _data):
    grenze = api.zahl(q, "limit", 300, 1, 1000)
    return api.liste(folders(h, q, grenze + 1), "folders", grenze)


def dateitypen(h, _p, q, _data):
    return api.liste(filetypes(h, q), "filetypes", api.zahl(q, "limit", 40, 1, 200))


def aehnlich(h, _p, q, _data):
    """More like this one hit – the index answers it without Ollama."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error, items=[], count=0)
    # The numbers first: an unusable one is the caller's mistake, and
    # saying so must not depend on the engine being there.
    cid = api.zahl(q, "cid", 0)
    grenze = api.zahl(q, "limit", 20, 1, 100)
    res = mod.similar_messages(cid=cid, k=grenze + 1)
    if res.get("error"):
        raise Ablehnung(409, res["error"], items=[], count=0)
    res["results"] = rest.treffer(res.get("results") or [])
    return api.liste(res, "results", grenze)


def personen(h, _p, q, _data):
    grenze = api.zahl(q, "limit", 50, 1, 200)
    return api.liste(people(h, q, grenze + 1), "people", grenze)


def adressen(h, _p, q, _data):
    grenze = api.zahl(q, "limit", 12, 1, 100)
    return api.liste(addresses(h, q, grenze + 1), "addresses", grenze)


def gespraech(h, _p, q, _data):
    grenze = api.zahl(q, "limit", 50, 1, 200)
    res = thread(h, q, grenze + 1)
    res["messages"] = rest.treffer(res.get("messages") or [])
    return api.liste(res, "messages", grenze)


def dokument(h, _p, q, _data):
    return api.json(document(h, q))


def fakten(h, _p, q, _data):
    return api.json(fakten_lesen(h, q))


def anhang(h, _p, q, _data):
    """One attachment of a mail as a download: the n-th real one
    (1-based), in the order the facts list them – so what the detail
    shows as a chip is what this hands out."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error)
    n = api.zahl(q, "n", 1, 1, 999)
    con = mod._db()
    try:
        row = con.execute("SELECT src, root, rel FROM chunks WHERE uid = ? AND seq = 0",
                          (q.get("uid", ""),)).fetchone()
    finally:
        con.close()
    if row is None:
        raise Ablehnung(404, "srv.detail.none")
    teil = None
    if row["src"] == "outlook":
        ziel, _fehler = mod._resolve_source(row["root"], row["rel"])
        teil = detail.anhang(ziel, n) if ziel is not None else None
    if teil is None:
        raise Ablehnung(404, "srv.attach.none", {"n": n})
    name, inhalt, ctype = teil
    # What lies in a mail was written by someone else: offered as a
    # download under a harmless name, never shown in this origin.
    return api.roh(200, inhalt, ctype, extra={
        "Content-Disposition": f'attachment; filename="{h.M._sicherer_name(name)}"',
        "Content-Security-Policy": "sandbox"})


def search(h, q, merken=True):
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error, items=[], count=0)
    k = faelle.kriterien(q)
    mod.STATE["internal_domains"] = interne_domains(h)
    kw = dict(person=k["person"], date_from=k["from"], date_to=k["to"],
              source=k["source"],
              # `limit` is the name everywhere else in the API; `k` is
              # the search's own and stays. One above the largest page
              # is allowed: that is how /api/v1/search sees has_more.
              k=api.zahl(q, "k", q.get("limit") or 20, 1, 101),
              offset=api.zahl(q, "offset", 0, 0),
              only_gone=k["gone"], folder=k["folder"], filetype=k["filetype"],
              party=k["party"], with_attachments=k["attachments"],
              # The four mail lines (13.0): they narrow mail, nothing else.
              **{m: k[m] for m in faelle.MAIL},
              # The "Cases" filter: only what one case – or one of its
              # folders – holds.
              case=str(k["fall"]) if k["fall"] else "",
              case_folder=str(k["ordner"]) if k["fall"] and k["ordner"] else "")
    if k["q"]:
        res = mod.search_messages(query=k["q"], mode=api.modus(q.get("mode")), **kw)
    else:
        res = mod.browse_messages(**kw)
    if res.get("error"):
        # The engine's own refusals: a filter naming an unknown case or
        # folder, a mode this index cannot rank, the embedder failing.
        raise Ablehnung(409, res["error"], hits=[], count=0)
    res["semantic"] = bool(mod.STATE.get("semantic"))
    if kw["offset"] == 0 and merken:
        # The first page of a search is the search: the history keeps
        # its criteria (never its hits) when the setting allows, and a
        # saved search that was run remembers when and how many.
        if h.M.historie_tage(h.app.cfg) != 0:
            h.app.faelle.suche_merken(k, res.get("count", 0))
        try:
            gespeichert = api.zahl(q, "saved", 0, 0)
        except ValueError:
            gespeichert = 0
        if gespeichert:
            h.app.faelle.gelaufen(gespeichert, res.get("count", 0))
    return res


def interne_domains(h):
    """Which mail domains are "us" – the app's one rule (app.interne_domains)."""
    return h.M.interne_domains(h.app.cfg)


# How many hits "the whole result" holds at most – for a case's list and
# for the two views over a result. Measured at 2 000 items a full case
# answer costs ~10 ms and 625 KB; five thousand hits without previews stay
# in that order.
WHOLE_LIMIT = 5000


def engine_filters(k):
    """The criteria of a search as the engine's keyword arguments – the
    filters alone, without the query, the mode or the paging."""
    return dict(person=k["person"], date_from=k["from"], date_to=k["to"],
                source=k["source"], only_gone=k["gone"], folder=k["folder"],
                filetype=k["filetype"], with_attachments=k["attachments"],
                case=str(k["fall"]) if k["fall"] else "",
                case_folder=str(k["ordner"]) if k["fall"] and k["ordner"] else "",
                party=k["party"],
                **{m: k[m] for m in faelle.MAIL})


def alle_treffer(h, k, grenze=WHOLE_LIMIT):
    """Every hit of a search, for a result list: the criteria as the
    page had them, paged through the same engine up to `grenze`."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error)
    mod.STATE["internal_domains"] = interne_domains(h)
    kw = dict(engine_filters(k), preview_chars=0)
    modus = api.MODUS[k["mode"]]
    treffer, offset, schritt = [], 0, 100
    while offset < grenze:
        if k["q"]:
            res = mod.search_messages(query=k["q"], mode=modus, k=schritt, offset=offset, **kw)
        else:
            res = mod.browse_messages(k=schritt, offset=offset, **kw)
        if res.get("error"):
            return None, res["error"]
        seite = res.get("results") or []
        treffer += seite
        if len(seite) < schritt:
            break
        offset += schritt
    return treffer[:grenze], None


def suche_zeitleiste(h, _p, q, _data):
    """The whole result in date order – the timeline over a search. Paged
    through the same engine as a case's list add, up to the same cap and
    without previews; `capped` says when the cap cut. A hit keeps the
    shape of `/search`, so the page's detail and case marks work on it
    unchanged."""
    k = faelle.kriterien(q)
    treffer, fehler = alle_treffer(h, k, WHOLE_LIMIT)
    if fehler:
        raise Ablehnung(409, fehler, items=[], count=0)
    # Oldest first, the undated at the end – the page turns it around
    # itself when asked for newest first.
    treffer.sort(key=lambda t: (not t.get("date"), t.get("date") or ""))
    return api.json({"items": rest.treffer(treffer), "count": len(treffer),
                     "capped": len(treffer) >= WHOLE_LIMIT, "limit": WHOLE_LIMIT})


def suche_personen(h, _p, q, _data):
    """Who a result names – the people view over a search. With a search
    term the people are counted over the collected hits (up to the cap);
    without one the engine hands over every item the filters admit, so
    the whole archive is counted honestly, with no cap."""
    k = faelle.kriterien(q)
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error, items=[], count=0)
    if k["q"]:
        treffer, fehler = alle_treffer(h, k, WHOLE_LIMIT)
        rows = [(t.get("who"), t.get("who_mail"), t.get("date"), t.get("source"))
                for t in treffer or ()]
        capped = len(rows) >= WHOLE_LIMIT
    else:
        mod.STATE["internal_domains"] = interne_domains(h)
        res = mod.facet_rows(**engine_filters(k))
        fehler, rows, capped = res.get("error"), res.get("rows") or [], False
    if fehler:
        raise Ablehnung(409, fehler, items=[], count=0)
    leute = people_of(rows)
    return api.json({"items": leute, "count": len(leute), "hits": len(rows),
                     "capped": capped})


UNKNOWN = "(unbekannt)"


def _date_key(text):
    """'YYYY-MM-DD HH:MM' of an index date, '' when it is none."""
    text = str(text or "").strip()
    return text[:16] if len(text) >= 10 and text[4] == "-" else ""


def people_of(rows):
    """Who the rows name, counted the way the page counts a case's people
    (personenAus in page.html): `who` split at ", " – an assignee list
    names several –, the unknown sender skipped, one address only when the
    row names exactly one person (a list would pin it on the wrong one),
    first and last date, a count per source. An address book entry
    names its company, not a person, so the `kontakte` rows stay out –
    the address book lists them itself. Sorted by last contact, newest
    first, then by count, then by name; the undated at the end."""
    je = {}
    for who, who_mail, date, src in rows:
        if src == "kontakte":
            continue
        names = [n.strip() for n in str(who or "").split(", ")
                 if n.strip() and n.strip() != UNKNOWN]
        adresse = str(who_mail or "").strip().lower()
        k = _date_key(date)
        for name in names:
            p = je.setdefault(name, {"name": name, "address": "", "items": 0,
                                     "by_source": {}, "first": "", "last": ""})
            p["items"] += 1
            p["by_source"][src] = p["by_source"].get(src, 0) + 1
            if adresse and len(names) == 1 and not p["address"]:
                p["address"] = adresse
            if k:
                if not p["first"] or k < p["first"]:
                    p["first"] = k
                if not p["last"] or k > p["last"]:
                    p["last"] = k
    leute = sorted(je.values(), key=lambda p: p["name"].lower())
    leute.sort(key=lambda p: -p["items"])
    leute.sort(key=lambda p: p["last"], reverse=True)
    return leute


def thread(h, q, grenze):
    """All messages of one conversation – the same evaluation as in MCP."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error, items=[], count=0)
    res = mod.get_thread(thread=q.get("key", ""),
                         limit=grenze)
    if res.get("error"):
        raise Ablehnung(409, res["error"], items=[], count=0)
    # Every message says which cases it sits in – the add-to-case
    # window counts from that how much of the conversation is there.
    marken = getattr(mod, "_mit_faellen", None)
    if res.get("messages") and marken:
        marken(res["messages"])
    return res


def folders(h, q, grenze):
    """Which mailbox folders are in the archive – for the filter."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error, items=[])
    return mod.list_folders(contains=q.get("contains", ""),
                            limit=grenze,
                            source=q.get("source", ""))


def files(h, q):
    """One level of the mirrored file tree, sizes taken from disk."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error, roots=[], dirs=[], files=[])
    r = mod.list_files(root=q.get("root", ""), path=q.get("path", ""))
    # The root->directory knowledge lives in one place: the resolver's
    # STATE, which /source uses too – no second hand-copied map here.
    ordner = mod.STATE.get(f'{r.get("root")}_dir')
    basis = Path(ordner) if ordner else None
    for e in r.get("files") or ():
        try:
            e["size"] = (basis / e["rel"]).stat().st_size if basis else None
        except OSError:
            e["size"] = None
    return r


def filetypes(h, q):
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error, items=[])
    # Hiding happens here and not in the tool: list_filetypes is meant
    # to say what is in the archive – to Claude as well. The trimming is
    # a question of the interface, not of the corpus. Hence fetch
    # everything first, then hide; the route cuts to the requested
    # number and knows from what is left whether it cut anything.
    aus = set(h.app.cfg.get("filetype_hidden") or [])
    r = mod.list_filetypes(limit=500, source=q.get("source", ""))
    liste = [e for e in r.get("filetypes", []) if e["type"] not in aus]
    return {"count": len(liste),
            "total_distinct": r.get("total_distinct", len(liste)),
            "hidden": sorted(aus),
            "filetypes": liste}


def people(h, q, grenze):
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error, items=[])
    return mod.list_people(source=q.get("source", "all"),
                           contains=q.get("contains", ""),
                           limit=grenze)


def addresses(h, q, grenze):
    """The addresses of one mail line, for the Mail filter's suggestions
    (13.0). An unknown line is the caller's mistake, not ours."""
    mod = h.app.search.ensure(h.app.cfg)
    # A refusal keeps the shape of the answer it replaces: on this
    # surface a collection is `items`, empty or not.
    if mod is None:
        raise Ablehnung(503, h.app.search.error, items=[])
    res = mod.adressen(role=q.get("role", "from"), q=q.get("contains", ""),
                       limit=grenze)
    if res.get("error"):
        raise Ablehnung(400, res["error"], items=[])
    return res


def document(h, q):
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error)
    res = mod.get_document(uid=q.get("uid", ""),
                           context_before=api.zahl(q, "before", 0, 0, 20),
                           context_after=api.zahl(q, "after", 0, 0, 20))
    if res.get("error"):
        raise Ablehnung(404, res["error"])
    return res


def fakten_lesen(h, q):
    """The facts the hit's detail shows beyond the hit – per kind of
    item, from the index row and the source file (detail.py). The
    page draws only the keys that come back."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error)
    con = mod._db()
    try:
        row, text = mod._message_text(con, q.get("uid", ""))
    finally:
        con.close()
    if row is None:
        raise Ablehnung(404, "srv.detail.none")
    ziel, _fehler = mod._resolve_source(row["root"], row["rel"])
    return detail.fakten(row, text, ziel, mod.STATE)


# The routes of this door, in the order the table in app.py lists them.
ROUTEN = (
    ("GET", "/api/v1/search", suche),
    ("GET", "/api/v1/search/timeline", suche_zeitleiste),
    ("GET", "/api/v1/search/people", suche_personen),
    ("GET", "/api/v1/similar", aehnlich),
    ("GET", "/api/v1/files", dateien),
    ("GET", "/api/v1/folders", ordner),
    ("GET", "/api/v1/filetypes", dateitypen),
    ("GET", "/api/v1/people", personen),
    ("GET", "/api/v1/addresses", adressen),
    ("GET", "/api/v1/threads", gespraech),
    ("GET", "/api/v1/documents", dokument),
    ("GET", "/api/v1/documents/facts", fakten),
    ("GET", "/api/v1/documents/attachments", anhang),
)
