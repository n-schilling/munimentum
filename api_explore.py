"""
api_explore.py – the Explore door of /api/v1: the search and what it
narrows by, the mirrored files, one item with its facts and attachments.
Everything here reads the index through the search module the app loaded
(`h.app.search`); nothing writes.
"""
from contextlib import contextmanager
from pathlib import Path

import re
import api
import detail
import evidence
import faelle
import footers
import org_csv
import organization
import own_contacts
import rest
import seniority
import state_db
import versions
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


PEOPLE_MOST = 5000              # the most the setting contacts_people can ask for


def personen(h, _p, q, _data):
    grenze = api.zahl(q, "limit", 50, 1, PEOPLE_MOST)
    return api.liste(people(h, q, grenze + 1), "people", grenze)


def _whole(value, default):
    """A setting that is a count: the number, the default for anything else."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _own_account(h):
    """The address the user is signed in with, by the way of access in
    use – the pasted key's or the sign-in's; "" when it names none. A key
    left on disk from before a sign-in is somebody else's word."""
    try:
        if h.app.auth_modus() == "login":
            account = h.app.auth_status().get("account")
        else:
            account = h.M.token_status(h.M.read_token()).get("account")
    except Exception:                            # noqa: BLE001 – unknown is an answer too
        account = None
    return account if isinstance(account, str) else ""


def _owner_known(h, mod):
    """Whose mailbox it is, handed to the search before anything counts
    or reads by it: the name and the addresses the user gave the app, the
    account in use."""
    mod.STATE["own_name"] = str(h.app.cfg.get("own_name") or "")
    mod.STATE["own_addresses"] = str(h.app.cfg.get("own_addresses") or "")
    mod.STATE["own_account"] = _own_account(h)


def correspondent_list(h, _p, _q, _data):
    """Whom the archive's owner exchanges mail with, person by person:
    mails received and sent, and whether the contact is a
    frequent one – what the address book draws on its cards and filters
    by. An index that cannot say answers an empty list; `frequent` names
    the rule the mark follows, the settings frequent_months and
    frequent_mails."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error, items=[])
    _owner_known(h, mod)
    rule = {key: _whole(h.app.cfg.get(f"frequent_{key}"), 12) for key in ("months", "mails")}
    items = []
    # The cards draw on this: everyone the address book knows, and of the
    # rest those with the most mail – as many as the address book lists
    # (the setting contacts_people) – a mailbox's thousands of one-off
    # senders are on no card.
    most = min(_whole(h.app.cfg.get("contacts_people"), 500), PEOPLE_MOST)
    people = mod.correspondent_people(**rule) or []
    # Whose footer is kept (footers.py): the card's mark, without a mail read.
    signed = footers.Footers(h.app.own_contacts.path).found()
    # A frequent contact is listed whatever the cap: the switch for them
    # would otherwise miss who writes often and little.
    for person in footers.listed(people, most):
        item = {"name": person["name"], "addresses": person["addresses"],
                "received": person["received"], "sent": person["sent"],
                "frequent": person["frequent"]}
        if person["also"]:
            item["also"] = person["also"]
        if person.get("writes_from"):
            item["writes_from"] = person["writes_from"]
        if footers.asked(person) in signed:
            item["footer"] = True
        items.append(item)
    # `total` says how many the mail knows: the page names the rest.
    return api.json({"items": items, "count": len(items), "total": len(people), "frequent": rule})


def _signature(mod, address):
    """One address's footer as the MCP tool reads it, or the refusal that
    says why none can be read: no mail files, an index from before the
    mail lines."""
    res = mod.get_signatures([address])
    if res.get("error"):
        old = res["error"] == getattr(mod, "_NO_MAIL_LINES", None)
        raise Ablehnung(409, "srv.signature.oldindex" if old else "srv.signature.nofiles")
    return res["signatures"][0]


def correspondent_signature(h, _p, q, _data):
    """The footer under one address's mails (signature.py), as the
    address book shows it in a person's window."""
    address = str(q.get("address") or "").strip().lower()
    if "@" not in address:
        raise Ablehnung(400, "srv.badparam", {"name": "address"})
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error)
    _owner_known(h, mod)
    # Kept from the last index run (footers.py), with the moment it was
    # read; an address not read yet is read now and kept.
    book = footers.Footers(h.app.own_contacts.path)
    kept = book.one(address)
    if kept is None:
        book.keep(address, _signature(mod, address))
        kept = book.one(address)
    # What the window draws, no more: the lines, what they rest on, the
    # mail they stand in, when they were read. Numbers and links are a
    # client's (get_signatures).
    return api.json(kept)


# -- the contacts added in the app (own_contacts.py) --------------------------
def _footer_of(h, address):
    """The footer under an address's mails as the MCP tool reads it – None
    while no index or no mail file can say: a card is added and kept
    without one."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        return None
    _owner_known(h, mod)
    try:
        res = mod.get_signatures([address])
    except Exception:                            # noqa: BLE001 – a footer is an extra
        return None
    return None if res.get("error") or not res.get("signatures") else res["signatures"][0]


def _own_contact(h, ident):
    card = h.app.own_contacts.one(ident)
    if card is None:
        raise Ablehnung(404, "srv.contact.unknown")
    return card


def own_contact_list(h, _p, _q, _data):
    """The contacts the user added to the address book in the app."""
    items = h.app.own_contacts.all()
    return api.json({"items": items, "count": len(items)})


def own_contact_add(h, _p, _q, data):
    """Add the person behind a mail address to the address book: a card
    of the app's own, begun from the name the address writes under and
    the numbers and links of its footer."""
    try:
        address = own_contacts.clean_address(data.get("address"))
    except own_contacts.BadValue:
        raise Ablehnung(400, "srv.contact.badvalue", {"field": "address"}) from None
    name = data.get("name", "")
    if not isinstance(name, str) or len(name) > own_contacts.TEXT_MOST:
        raise Ablehnung(400, "srv.contact.badvalue", {"field": "name"})
    for field in data:
        if field not in ("address", "name"):     # the rest is filled in with PATCH
            raise Ablehnung(400, "srv.contact.badvalue", {"field": str(field)[:40]})
    found = _footer_of(h, address)
    # The name the mail says, where the caller says none – at a name's length.
    name = name or str((found or {}).get("name") or "")[:own_contacts.TEXT_MOST]
    try:
        ident = h.app.own_contacts.add(address, name, found)
    except own_contacts.Exists:
        raise Ablehnung(409, "srv.contact.exists", {"address": address}) from None
    except own_contacts.BadValue as e:           # a name the mail says, longer than a name
        raise Ablehnung(400, "srv.contact.badvalue", {"field": e.field}) from None
    return api.json(_own_contact(h, ident), 201,
                    {"Location": f"{api.API_V1}/contacts/own/{ident}"})


def _own_id(p):
    return api.id_aus(p, "id", "srv.contact.unknown")


def own_contact_one(h, p, _q, _data):
    return api.json(_own_contact(h, _own_id(p)))


def own_contact_change(h, p, _q, data):
    """Set fields of a card – all or none; the address is who the card
    is and does not change."""
    ident = _own_id(p)
    if not isinstance(data, dict) or not data:
        raise Ablehnung(400, "srv.contact.badvalue", {"field": "body"})
    try:
        there = h.app.own_contacts.change(ident, data)
    except own_contacts.BadValue as e:
        raise Ablehnung(400, "srv.contact.badvalue", {"field": str(e.field)[:40]}) from None
    if not there:
        raise Ablehnung(404, "srv.contact.unknown")
    return api.json(_own_contact(h, ident))


def own_contact_remove(h, p, _q, _data):
    if not h.app.own_contacts.remove(_own_id(p)):
        raise Ablehnung(404, "srv.contact.unknown")
    return api.leer()


def own_contact_reread(h, p, _q, _data):
    """Read the footer again: its lines replace the kept ones, a number
    or a link the card lacks joins it, nothing typed is overwritten."""
    ident = _own_id(p)
    card = _own_contact(h, ident)
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error)
    _owner_known(h, mod)
    h.app.own_contacts.reread(ident, _signature(mod, card["address"]))
    return api.json(_own_contact(h, ident))


def own_contact_versions(h, p, _q, _data):
    """What a card said before: every version a change, a footer read
    again or a removal replaced, newest first."""
    items = h.app.own_contacts.versions(_own_id(p))
    if items is None:
        raise Ablehnung(404, "srv.contact.unknown")
    return api.json({"items": items, "count": len(items)})


def _ignored(h):
    items = h.app.own_contacts.ignored()
    return api.json({"items": items, "count": len(items)})


def ignored_contact_list(h, _p, _q, _data):
    """The addresses the user set to be ignored: not offered for the
    address book, whatever the mail says of them."""
    return _ignored(h)


def _ignore(h, address, on):
    try:
        h.app.own_contacts.ignore(address, on)
    except own_contacts.BadValue:
        raise Ablehnung(400, "srv.contact.badvalue", {"field": "address"}) from None
    return _ignored(h)


def ignored_contact_add(h, _p, _q, data):
    """Ignore an address; answers the whole list."""
    for field in data if isinstance(data, dict) else ():
        if field != "address":
            raise Ablehnung(400, "srv.contact.badvalue", {"field": str(field)[:40]})
    return _ignore(h, (data or {}).get("address") if isinstance(data, dict) else None, True)


def ignored_contact_remove(h, _p, q, _data):
    """No longer ignore an address; answers the whole list."""
    return _ignore(h, q.get("address"), False)


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
    (personenAus in page.html): `who` split at " · " – an assignee list
    names several, a sender written "Surname, Given" is one –, the
    unknown sender skipped, one address only when the
    row names exactly one person (a list would pin it on the wrong one),
    first and last date, a count per source. An address book entry
    names its company, not a person, so the `kontakte` rows stay out –
    the address book lists them itself. Sorted by last contact, newest
    first, then by count, then by name; the undated at the end."""
    je = {}
    for who, who_mail, date, src in rows:
        if src == "kontakte":
            continue
        names = [n.strip() for n in str(who or "").split(" · ")
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
    uid = q.get("uid", "")
    if not uid and (q.get("key") or (q.get("root") and q.get("rel"))):
        # A file named by its place – the evidence findings name files so –
        # or an item by its stable key: the link a citation over MCP
        # carries (#item=<key>).
        con = mod._db()
        try:
            if q.get("key"):
                row = (con.execute("SELECT uid FROM chunks WHERE key = ? AND seq = 0 "
                                   "ORDER BY uid LIMIT 1", (q["key"],)).fetchone()
                       if mod._hat_spalte(con, "key") else None)
            else:
                row = con.execute("SELECT uid FROM chunks WHERE root = ? AND rel = ? AND seq = 0 "
                                  "ORDER BY uid LIMIT 1", (q["root"], q["rel"])).fetchone()
        finally:
            con.close()
        if row is None:
            raise Ablehnung(404, "srv.detail.none")
        uid = row["uid"]
    res = mod.get_document(uid=uid,
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
    facts = detail.fakten(row, text, ziel, mod.STATE)
    checksums = item_checksums(h, row, ziel)
    if checksums:
        facts["checksums"] = checksums
    return facts


# Items that are part of a file holding many (a chat, a board, a list):
# the file's checksum says nothing about the one item.
_PART_OF_A_FILE = ("teams", "planner", "todo")


def item_checksums(h, row, path):
    """The checksums of the file an item is: our SHA-256 always, and –
    where a mirror fetched it – the quickXorHash we computed beside the one
    Microsoft gave, and whether they agree. From the evidence chain while
    the file is as chained, else the SHA-256 read now."""
    if path is None or row["src"] in _PART_OF_A_FILE:
        return None
    data = Path(h.M.BASE)
    rel = versions.rel_of(path, data)
    ev = evidence.Evidence(data)
    try:
        if rel is not None and ev.exists():
            current = next((v for v in ev.history(rel) if v.get("current")), None)
            if current:
                out = {"sha256": current["sha256"], "captured": current.get("captured")}
                if current.get("quickxor") or current.get("ms_quickxor"):
                    out.update(quickxor=current.get("quickxor"),
                               microsoft_quickxor=current.get("ms_quickxor"),
                               microsoft_match=current.get("ms_match"))
                return out
        return {"sha256": versions.sha256_file(path), "captured": None}
    except OSError:
        return None
    finally:
        ev.close()


# ---------------------------------------------------------------------------
# The versions of one item (evidence.py, versions.py)
# ---------------------------------------------------------------------------
_SHA = re.compile(r"^[0-9a-f]{16,64}$")


def _version_target(h, q):
    """What `uid` – or, for a case's item, `root` and `rel` with its `key` –
    names: its source, key, file and the data folder the chain lies in."""
    mod = h.app.search.ensure(h.app.cfg)
    if mod is None:
        raise Ablehnung(503, h.app.search.error)
    uid = str(q.get("uid") or "")
    if uid:
        con = mod._db()
        try:
            row = con.execute("SELECT * FROM chunks WHERE uid = ? AND seq = 0", (uid,)).fetchone()
        finally:
            con.close()
        if row is None:
            raise Ablehnung(404, "srv.detail.none")
        src, root, rel = row["src"], row["root"], row["rel"]
        key = row["key"] if "key" in row.keys() else ""
    else:
        root, rel, key = str(q.get("root") or ""), str(q.get("rel") or ""), str(q.get("key") or "")
        if not root or not rel:
            raise Ablehnung(400, "srv.badparam", {"name": "uid"})
        src = key.partition(":")[0]
    folders = mod._exportordner()
    base = folders.get(root)
    if not base:
        raise Ablehnung(404, "srv.versions.none")
    base = Path(base).resolve()
    path = (base / rel).resolve()
    if base != path and base not in path.parents:
        raise Ablehnung(404, "srv.versions.none")
    return {"src": src, "root": root, "rel": rel, "key": key or "", "path": path,
            "data": Path(h.M.BASE), "teams": folders.get("teams")}


def _message(target):
    """The versions of a Teams message, or None for anything else."""
    if target["root"] != "teams" or "#" not in target["key"]:
        return None
    return evidence.message_versions(target["teams"], target["key"])


def _format(name):
    ext = Path(str(name)).suffix.lower()
    return "html" if ext in versions.HTML_TYPES else "text" if versions.is_text(name) else "binary"


def _file_versions(target):
    rel = versions.rel_of(target["path"], target["data"])
    ev = evidence.Evidence(target["data"])
    if rel is None or not ev.exists():
        return rel, []
    try:
        return rel, ev.history(rel)
    finally:
        ev.close()


_VERSION_FIELDS = ("sha256", "size", "captured", "modified", "kind", "current", "available")


def _outward(v):
    """One version as the API names it: Microsoft's checksum under its
    own name beside ours."""
    out = {k: v.get(k) for k in _VERSION_FIELDS}
    if v.get("quickxor") or v.get("ms_quickxor"):
        out.update(quickxor=v.get("quickxor"), microsoft_quickxor=v.get("ms_quickxor"),
                   microsoft_match=v.get("ms_match"))
    return out


def item_versions(h, _p, q, _data):
    """Every version of the item the archive knows, newest first: when it
    was made, when the chain first saw it, its checksum, whether its bytes
    are here. A Teams message's versions are its earlier texts; any other
    item's are those of its file."""
    target = _version_target(h, q)
    message = _message(target)
    if message is not None:
        return api.json({"unit": "message", "format": "text",
                         "items": [_outward(v) for v in message]})
    _rel, found = _file_versions(target)
    return api.json({"unit": "file", "format": _format(target["rel"]),
                     "items": [_outward(v) for v in found]})


def _sha_param(q, name, required=True):
    value = str(q.get(name) or "").strip().lower()
    if not value and not required:
        return None
    if not _SHA.match(value):
        raise Ablehnung(400, "srv.badparam", {"name": name})
    return value


def _version_bytes(target, sha):
    rel = versions.rel_of(target["path"], target["data"])
    ev = evidence.Evidence(target["data"])
    if rel is None or not ev.exists():
        return None
    try:
        full = [v["sha256"] for v in ev.history(rel) if v["sha256"].startswith(sha)]
        return ev.bytes_of(rel, full[0]) if full else None
    finally:
        ev.close()


def version_content(h, _p, q, _data):
    """One version of a file, as its bytes: an archive page shown in a
    sandbox, anything else handed out as a download."""
    target = _version_target(h, q)
    sha = _sha_param(q, "sha")
    body = None if _message(target) is not None else _version_bytes(target, sha)
    if body is None:
        raise Ablehnung(404, "srv.versions.none")
    ext = target["path"].suffix.lower()
    ctype = h.M._CONTENT_TYPE.get(ext, "application/octet-stream")
    headers = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}
    if ext in versions.HTML_TYPES:
        body = h.M._links_umleiten(body, target["root"], target["rel"])
    else:
        headers["Content-Disposition"] = (
            f'attachment; filename="{h.M._sicherer_name(target["path"].name)}"')
    return api.roh(200, body, ctype, extra=headers)


def _version_text(target, sha):
    """The readable text of one version – None when it has none."""
    message = _message(target)
    if message is not None:
        v = next((v for v in message if v["sha256"].startswith(sha)), None)
        return None if v is None else evidence.message_text(v)
    if not versions.is_text(target["rel"]):
        return None
    raw = _version_bytes(target, sha)
    return None if raw is None else versions.plain_text(raw, target["rel"])


def version_diff(h, _p, q, _data):
    """What changed between two versions, as a word diff: `sha` the older
    one, `to` the newer (the current one when not named). The same
    version on both sides is its text in one piece."""
    target = _version_target(h, q)
    older = _sha_param(q, "sha")
    newer = _sha_param(q, "to", required=False)
    if newer is None:
        message = _message(target)
        if message is not None:
            newer = message[0]["sha256"]
        else:
            newer = next((v["sha256"] for v in _file_versions(target)[1] if v.get("current")), None)
    old_text = _version_text(target, older)
    new_text = _version_text(target, newer) if newer else None
    if old_text is None or new_text is None:
        raise Ablehnung(404, "srv.versions.notext")
    return api.json({"from": older, "to": newer, "ops": versions.diff(old_text, new_text)})


# ---------------------------------------------------------------------------
# The organization (org_export.py, organization.py): one file, its versions
# ---------------------------------------------------------------------------
# The versions, parsed on demand – the view asks one person at a time,
# and a whole tenant is a few megabytes of JSON.
_ORG_ARCHIVE = {}


def _org_archive(h):
    data, teams = Path(h.M.BASE), Path(h.M.BASE) / h.M.TEAMS_DIR
    key = str(teams)
    if key not in _ORG_ARCHIVE:
        _ORG_ARCHIVE.clear()
        _ORG_ARCHIVE[key] = organization.Archive(data, teams, keep=2)
    return _ORG_ARCHIVE[key]


def _org_version(h, q, found=None):
    """(the version asked for – `version`, a checksum or its first 16
    characters; the current one when absent –, its Org, every version)."""
    archive = _org_archive(h)
    found = archive.versions() if found is None else found
    if not found:
        raise Ablehnung(404, "srv.org.none")
    asked = _sha_param(q, "version", required=False)
    v = next((v for v in found if (v["sha256"].startswith(asked) if asked else v["current"])),
             None)
    org = archive.org(v) if v is not None else None
    if org is None:
        raise Ablehnung(404, "srv.org.noversion")
    return v, org, found


def _org_of(h, v):
    return _org_archive(h).org(v)


def _org_stamp(v):
    return {"sha256": v["sha256"], "captured": v.get("captured"), "current": v["current"]}


def org_versions(h, _p, _q, _data):
    """The versions of the organization, newest first – each change a
    run brought is one. Empty before the first run that fetched it."""
    return api.json({"items": [_org_stamp(v) for v in _org_archive(h).versions()]})


def org_top(h, _p, q, _data):
    """One version of the organization from above: the people at the top,
    the largest part first, how many it holds, and who the account is."""
    v, org, _found = _org_version(h, q)
    return api.json({"version": _org_stamp(v), "people": org.active,
                     "me": org.me if org.me in org.people else None,
                     "roots": [org.card(pid) for pid in org.roots()]})


def org_person(h, p, q, _data):
    """One person as the profile card shows them: the managers above, top
    first, the person, and whoever reports to them."""
    v, org, _found = _org_version(h, q)
    pid = str(p.get("id") or "")
    if pid not in org.people:
        raise Ablehnung(404, "srv.org.noperson")
    return api.json({"version": _org_stamp(v),
                     "chain": [org.card(b) for b in org.chain(pid)],
                     "person": org.person(pid),
                     "reports": [org.card(r) for r in org.reports.get(pid, ())]})


def org_roles(h, _p, q, _data):
    """Every job title of that version with how many hold it, the most
    held first; `contains` narrows, and `people` counts everyone holding
    one of the titles it leaves – whether or not `limit` cut the list."""
    _v, org, _found = _org_version(h, q)
    limit = api.zahl(q, "limit", 500, 1, 5000)
    roles = org.roles(q.get("contains") or "", q.get("department") or "")
    return api.json({"items": roles[:limit], "total": len(roles), "has_more": len(roles) > limit,
                     "people": sum(r["count"] for r in roles)})


def _role_model(h):
    cfg = h.app.cfg
    return cfg.get("role_model") or {}, cfg.get("role_levels") or {}


def org_people(h, _p, q, _data):
    """The people holding a role: whose title contains `role`, or is it
    with `exact=true` – by name, each with their manager's name. Or the
    people of one seniority level: `level` 1–10 by title (the role model),
    `depth` 0–9 by position in the tree (managers above; 9 = 9 or more)."""
    csv_wanted = _wants_csv(h)
    if csv_wanted:
        # Checked before anything is read or built.
        dialect, groups = _csv_dialect(q), _csv_groups(h, q)
    v, org, _found = _org_version(h, q)
    ids = _listed(h, org, q, disabled=csv_wanted and "disabled" in groups[0])
    if csv_wanted:
        # The same list as a file (content negotiation, RFC 9110): every
        # one of it, the column groups and the dialect as the export takes.
        return _csv_answer(dialect, "people", v, *org_csv.people(org, ids, *groups), vary=True)
    limit = api.zahl(q, "limit", 200, 1, 1000)
    offset = api.zahl(q, "offset", 0, 0)
    return api.json({"items": [{**org.card(i),
                                "manager": (org.people.get(org.manager(i)) or {}).get("name")}
                               for i in ids[offset:offset + limit]],
                     "total": len(ids), "limit": limit, "offset": offset,
                     "has_more": offset + limit < len(ids)}, extra=_VARY)


def _listed(h, org, q, disabled=False):
    """The ids of a list the organization view shows: a role (its text,
    `exact`, `department`), or a level of the pyramid (`level`, `depth`);
    switched-off accounts only with `disabled` (a file that asked for them)."""
    if q.get("level") is not None or q.get("depth") is not None:
        return _seniority_people(h, org, q, disabled)
    role = str(q.get("role") or "").strip()
    if not role:
        raise Ablehnung(400, "srv.badparam", {"name": "role"})
    exact = str(q.get("exact") or "").strip().lower() in ("1", "true", "yes")
    return org.with_role(role, exact=exact, department=q.get("department") or "", disabled=disabled)


def _wants_csv(h):
    """Whether `Accept` prefers CSV to JSON (RFC 9110 §12.5.1): `text/csv`
    weighted above JSON, or as high and named while JSON is not; a bare
    `*/*` keeps JSON."""
    accept = h.headers.get("Accept", "") if getattr(h, "headers", None) else ""
    weight = {}
    for part in accept.lower().split(","):
        kind, *params = [x.strip() for x in part.split(";")]
        q = 1.0
        for param in params:
            name, _sep, value = param.partition("=")
            if name.strip() == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        weight[kind] = max(weight.get(kind, 0.0), q)
    # The most specific range names a type's weight (RFC 9110 §12.5.1); at
    # equal weight a type named outright beats one a wildcard lets in.
    csv_q = weight.get("text/csv", weight.get("text/*", weight.get("*/*", 0.0)))
    json_q = weight.get("application/json", weight.get("application/*", weight.get("*/*", 0.0)))
    if csv_q <= 0 or csv_q < json_q:
        return False
    return csv_q > json_q or ("text/csv" in weight and "application/json" not in weight)


# One URL, two representations: a cache keys them by the request's Accept.
_VARY = {"Vary": "Accept"}


def _csv_groups(h, q):
    """(groups, role model, levels set by hand) for a people file."""
    raw = q.get("columns")
    groups = org_csv.DEFAULT_GROUPS if raw is None else org_csv.groups_of(raw)
    if groups is None:
        raise Ablehnung(400, "srv.badparam", {"name": "columns"})
    model, overrides = _role_model(h)
    return groups, model, overrides


def _csv_dialect(q):
    dialect = str(q.get("dialect") or "rfc4180").strip().lower()
    if dialect not in org_csv.DIALECTS:
        raise Ablehnung(400, "srv.badparam", {"name": "dialect"})
    return dialect


def _csv_answer(dialect, kind, stamp, header, rows, since=None, vary=False):
    name = org_csv.filename(kind, stamp, since)
    extra = {"Content-Disposition": f'attachment; filename="{name}"', **(_VARY if vary else {})}
    return api.roh(200, org_csv.render(header, rows, dialect),
                   "text/csv; charset=utf-8; header=present", extra=extra)


def org_export(h, p, q, _data):
    """GET /organization/export/{kind}: a file for a spreadsheet.
    `people` – one version (`version`), the whole organization, the part
    from `below` down, or a list the view shows (`role`, `level`,
    `depth`), in the column groups `columns` names. `changes` – every
    version after `from` up to `version`, each against the readable one
    before it, as the overview counts; `below` keeps the people of that
    part. `dialect`: `excel` or `rfc4180`."""
    kind = str(p.get("kind") or "")
    if kind not in org_csv.KINDS:
        raise Ablehnung(404, "srv.notfound", {"path": "/api/v1/organization/export/" + kind})
    # Everything the question says is checked before a version is read.
    dialect = _csv_dialect(q)
    groups = _csv_groups(h, q) if kind == "people" else None
    first = _sha_param(q, "from") if kind == "changes" and q.get("from") is not None else None
    v, org, found = _org_version(h, q)
    below = str(q.get("below") or "").strip() or None
    if kind == "people":
        if below is not None and below not in org.people:
            raise Ablehnung(404, "srv.org.noperson")
        if below:
            ids = org_csv.part(org, below)
        elif any(q.get(k) is not None for k in ("role", "level", "depth")):
            ids = _listed(h, org, q, disabled="disabled" in groups[0])
        else:
            ids = org_csv.everyone(org)
        return _csv_answer(dialect, kind, v, *org_csv.people(org, ids, *groups))
    end = next(i for i, w in enumerate(found) if w["sha256"] == v["sha256"])
    # Its own walk, two versions in hand at a time: a long span would push
    # the views' versions out of the shared cache and parse each one twice.
    archive = organization.Archive(Path(h.M.BASE), Path(h.M.BASE) / h.M.TEAMS_DIR, keep=2)
    if first is None:
        # The changes fold's comparison: `version` against the nearest
        # readable version before it – the same `previous` the fold asks.
        before, base = archive.previous(found, end)
        base_at = found.index(before) if before is not None else None
    else:
        start = next((i for i, w in enumerate(found) if w["sha256"].startswith(first)), None)
        if start is None:
            raise Ablehnung(404, "srv.org.noversion")
        if start <= end:
            raise Ablehnung(400, "srv.badparam", {"name": "from"})
        # The span starts at `from` itself; an unreadable one gives way to
        # the first readable version after it, never to one before – the
        # file holds nothing older than asked for.
        base_at, base = next(((i, o) for i in range(start, end, -1)
                              if (o := archive.readable(found[i])) is not None), (None, None))
    # A part's head may have left before `version`: known at either end of
    # the span is enough – and that is settled before the walk, so a typo
    # costs no version read.
    if below is not None and below not in org.people and (base is None or below not in base.people):
        raise Ablehnung(404, "srv.org.noperson")
    with _org_db(h) as db:
        store = organization.read_series_store(db)
    walk = {"first": None}

    def steps():
        old = base
        for i in range((base_at if base_at is not None else end + 1) - 1, end - 1, -1):
            # `version` is in hand already; the others are read once each.
            new = org if i == end else archive.readable(found[i])
            if new is None:
                continue
            if old is not None:
                walk["first"] = walk["first"] or found[i]
                flag = bool((store.get(found[i]["sha256"]) or {}).get("rules_changed"))
                yield _org_stamp(found[i]), flag, old, new
            old = new
    header, rows = org_csv.changes(steps(), below)
    # Named after the first and last version whose changes it holds.
    since = _org_stamp(walk["first"]) if walk["first"] else v
    return _csv_answer(dialect, kind, v, header, rows, since=since)


@contextmanager
def _org_db(h):
    """The org folder's state.db, read-only."""
    # Read-only: a missing file reads as empty and is never created.
    db = state_db.StateDb(Path(h.M.BASE) / h.M.TEAMS_DIR / organization.ORG_DIR, readonly=True)
    try:
        yield db
    finally:
        db.close()


def _seniority_people(h, org, q, disabled=False):
    """The ids of one level, by name – `level` by title, `depth` by tree;
    switched-off accounts only with `disabled`."""
    people = [pid for pid in org.people if disabled or pid not in org.disabled]
    if q.get("level") is not None:
        level = api.zahl(q, "level", 1, 1, seniority.LEVELS)
        model = seniority.Model(*_role_model(h))
        reports = seniority.direct_reports(org)
        ids = [pid for pid in people
               if model.place(org.people[pid].get("title"), bool(seniority.leads(org, reports, pid)))[0] == level]
    else:
        wanted = api.zahl(q, "depth", 0, 0, seniority.LEVELS - 1)
        ids = [pid for pid in people if min(seniority.depth(org, pid), seniority.LEVELS - 1) == wanted]
    return sorted(ids, key=lambda i: organization._name_key(org.people[i]))


def org_seniority(h, _p, q, _data):
    """The seniority pyramid of one version: per level how many people the
    rules place, how many a level set by hand, how many the tree moved
    (a leadership title without reports); the people without a title,
    outside the organization or not assigned; the tree's own layers; and
    the health figures – for this version and for the oldest one, so the
    page can say what changed. Numbers only: no title, no name."""
    v, org, found = _org_version(h, q)
    model, overrides = _role_model(h)
    out = {"version": _org_stamp(v), **seniority.analyse(org, model, overrides)}
    oldest = found[-1]
    old = _org_archive(h).readable(oldest) if oldest["sha256"] != v["sha256"] else None
    out["since"] = _org_stamp(oldest) if old is not None else None
    out["since_figures"] = seniority.figures(old) if old is not None else None
    return api.json(out)


def org_seniority_titles(h, _p, q, _data):
    """The review dialog's list: every cleaned title of the version once,
    with its holders, its level and why, how many of them lead somebody,
    their median depth; the words most common in the titles no rule covers;
    the role model in force and the presets to start from. The one answer
    with titles – asked when the dialog opens, never polled."""
    _v, org, _found = _org_version(h, q)
    model, overrides = _role_model(h)
    return api.json({**seniority.titles(org, model, overrides),
                     "model": seniority.clean_model(model),
                     "overrides": seniority.clean_overrides(overrides),
                     "presets": {k: seniority.preset_model(k) for k in seniority.PRESETS}})


def org_changes(h, _p, q, _data):
    """What a version changed against the one before it: who joined, who
    left, who moved to another manager, whose title or department
    changed. The first version has nothing before it."""
    v, org, found = _org_version(h, q)
    i = next(i for i, w in enumerate(found) if w["sha256"] == v["sha256"])
    # The nearest earlier version that can be read – the one the overview
    # counted against, so a number clicked there finds its changes here.
    before, old = _org_archive(h).previous(found, i)
    return api.json({"from": _org_stamp(before) if before else None, "to": _org_stamp(v),
                     "items": organization.changes(old, org)})


def org_series(h, _p, _q, _data):
    """The overview: per version, oldest first, its date, the people in it
    and how many joined, moved, changed and left against the version
    before – numbers only, no names; asked once when the overview opens.
    `missing` counts the versions the next organization run still
    computes (the one-time job for versions from before the overview)."""
    with _org_db(h) as db:
        return api.json(organization.series(Path(h.M.BASE), Path(h.M.BASE) / h.M.TEAMS_DIR, db))


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
    ("GET", "/api/v1/correspondents", correspondent_list),
    ("GET", "/api/v1/correspondents/signature", correspondent_signature),
    ("GET", "/api/v1/contacts/own", own_contact_list),
    ("POST", "/api/v1/contacts/own", own_contact_add),
    ("GET", "/api/v1/contacts/own/{id}", own_contact_one),
    ("PATCH", "/api/v1/contacts/own/{id}", own_contact_change),
    ("DELETE", "/api/v1/contacts/own/{id}", own_contact_remove),
    ("POST", "/api/v1/contacts/own/{id}/signature", own_contact_reread),
    ("GET", "/api/v1/contacts/own/{id}/versions", own_contact_versions),
    ("GET", "/api/v1/contacts/ignored", ignored_contact_list),
    ("POST", "/api/v1/contacts/ignored", ignored_contact_add),
    ("DELETE", "/api/v1/contacts/ignored", ignored_contact_remove),
    ("GET", "/api/v1/threads", gespraech),
    ("GET", "/api/v1/documents", dokument),
    ("GET", "/api/v1/documents/facts", fakten),
    ("GET", "/api/v1/documents/attachments", anhang),
    ("GET", "/api/v1/documents/versions", item_versions),
    ("GET", "/api/v1/documents/versions/content", version_content),
    ("GET", "/api/v1/documents/versions/diff", version_diff),
    ("GET", "/api/v1/organization", org_top),
    ("GET", "/api/v1/organization/versions", org_versions),
    ("GET", "/api/v1/organization/changes", org_changes),
    ("GET", "/api/v1/organization/changes/series", org_series),
    ("GET", "/api/v1/organization/seniority", org_seniority),
    ("GET", "/api/v1/organization/seniority/titles", org_seniority_titles),
    ("GET", "/api/v1/organization/export/{kind}", org_export),
    ("GET", "/api/v1/organization/roles", org_roles),
    ("GET", "/api/v1/organization/people", org_people),
    ("GET", "/api/v1/organization/people/{id}", org_person),
)
