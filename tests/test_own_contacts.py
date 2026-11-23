"""The contacts added to the address book in the app (own_contacts.py):
the table, the routes, and what an MCP client reads of them."""

import sqlite3

import pytest

import mcp_server
import own_contacts
from tests.test_correspondents import _all, _point_at, built  # noqa: F401 – the built archive
from tests.test_rest import roh, sandbox, server  # noqa: F401 – fixtures
from testdata import people, sources

FOUND = {"address": "erik.einkauf@example.com", "name": "Erik Einkauf",
         "lines": ["Erik Einkauf", "Head of purchasing", "Tel. +49 40 000000 31"],
         "phones": [{"type": "phone", "number": "+49 40 000000 31"}],
         "urls": ["www.example.com"], "emails": [], "basis": "repeated",
         "uid": "outlook:E-Mail/Inbox/a.eml:0", "date": "2026-05-30", "mails_read": 6}


@pytest.fixture
def book(tmp_path):
    return own_contacts.OwnContacts(tmp_path / "faelle.db")


def test_a_card_begins_with_what_the_mail_says(book):
    assert book.all() == []                              # no file, no card, no error
    ident = book.add("Erik.Einkauf@Example.com", "Erik Einkauf", FOUND)
    card = book.one(ident)
    assert (card["address"], card["name"]) == ("erik.einkauf@example.com", "Erik Einkauf")
    assert card["phones"] == FOUND["phones"] and card["urls"] == ["www.example.com"]
    assert card["signature"]["lines"] == FOUND["lines"] and card["signature"]["basis"] == "repeated"
    assert card["signature"]["uid"] == FOUND["uid"] and card["signature"]["read"]
    assert (card["title"], card["company"], card["department"], card["note"]) == ("", "", "", "")
    assert book.all() == [card]
    # One card per address, whatever its spelling.
    with pytest.raises(own_contacts.Exists):
        book.add("erik.einkauf@example.com")
    # Without a name the address stands for it; without a footer nothing is made up.
    plain = book.one(book.add("greta.gast@example.com"))
    assert plain["name"] == "greta.gast@example.com" and plain["phones"] == []
    assert plain["signature"] == {"lines": []}            # nothing was read: no day of a reading
    for bad in ("", "no address", "a@", None, 7, "<greta.gast@example.com>", "a@example.com;x"):
        with pytest.raises(own_contacts.BadValue):
            book.add(bad)
    # Whatever the mail lines call an address can get a card.
    for odd in ("a&b@example.com", "sales/support@example.com", "bounce=1@example.com", "root@server01"):
        assert book.add(odd)


def test_a_card_is_changed_whole_or_not_at_all(book):
    ident = book.add("erik.einkauf@example.com", "Erik Einkauf", FOUND)
    assert book.change(ident, {"title": " Head of  purchasing ", "company": "Nordwind GmbH",
                               "phones": [{"type": "mobile", "number": "+49 170 0000031"},
                                          {"type": "phone", "number": "  "}],
                               "urls": ["www.example.com", "www.example.com", ""], "note": "met in Kiel\n"})
    card = book.one(ident)
    assert (card["title"], card["company"], card["note"]) == ("Head of purchasing", "Nordwind GmbH", "met in Kiel")
    assert card["phones"] == [{"type": "mobile", "number": "+49 170 0000031"}]
    assert card["urls"] == ["www.example.com"]
    for bad, field in (({"name": ""}, "name"), ({"title": 7}, "title"), ({"address": "x@example.com"}, "address"),
                       ({"phones": [{"type": "pager", "number": "1"}]}, "phones"), ({"phones": "none"}, "phones"),
                       ({"urls": [7]}, "urls"), ({"company": "x" * 201}, "company"),
                       ({"title": "kept?", "note": "x" * 4001}, "note")):
        with pytest.raises(own_contacts.BadValue) as e:
            book.change(ident, bad)
        assert e.value.field == field
    assert book.one(ident)["title"] == "Head of purchasing"          # the bad change changed nothing
    # No control characters and no marks that turn the writing around.
    book.change(ident, {"name": "Erik\r\nEin\x00kauf\u202e", "note": "one\r\ntwo\x00\tthree"})
    assert (book.one(ident)["name"], book.one(ident)["note"]) == ("Erik Ein kauf", "one\ntwo three")
    book.change(ident, {"name": "Erik Einkauf", "note": "met in Kiel"})
    assert book.change(999, {"title": "x"}) is False
    assert book.remove(ident) is True and book.remove(ident) is False and book.one(ident) is None


def test_reading_the_footer_again_adds_and_never_overwrites(book):
    ident = book.add("erik.einkauf@example.com", "Erik Einkauf", FOUND)
    book.change(ident, {"phones": [{"type": "mobile", "number": "+49 40 000000-31"}], "title": "Buyer"})
    later = {**FOUND, "lines": ["Erik Einkauf", "Director"], "date": "2026-07-01",
             "phones": [{"type": "phone", "number": "+49 40 000000 31"}, {"type": "fax", "number": "+49 40 000000 39"}],
             "urls": ["www.example.com", "www.nordwind.example"]}
    assert book.reread(ident, later) is True and book.reread(999, later) is False
    card = book.one(ident)
    # The number typed stays as typed (the same digits are the same number); the new one joins.
    assert card["phones"] == [{"type": "mobile", "number": "+49 40 000000-31"},
                              {"type": "fax", "number": "+49 40 000000 39"}]
    assert card["urls"] == ["www.example.com", "www.nordwind.example"]
    assert card["title"] == "Buyer" and card["signature"]["lines"] == ["Erik Einkauf", "Director"]
    assert card["signature"]["date"] == "2026-07-01"
    # A reading that finds no footer leaves the one that was found before.
    book.reread(ident, {**FOUND, "lines": [], "phones": [], "urls": []})
    assert book.one(ident)["signature"]["lines"] == ["Erik Einkauf", "Director"]


def test_what_a_card_said_before_is_kept(book):
    ident = book.add("erik.einkauf@example.com", "Erik Einkauf", FOUND)
    assert book.versions(ident) == [] and book.versions(999) is None
    assert book.one(ident)["versions"] == 0
    book.change(ident, {"title": "Buyer"})
    book.change(ident, {"title": "Buyer", "name": "Erik Einkauf"})       # saved as it stood: no version
    assert [v["changed"] for v in book.versions(ident)] == [["title"]]
    updated = book.one(ident)["updated"]
    book.change(ident, {"title": " Buyer "})
    assert book.one(ident)["updated"] == updated
    book.change(ident, {"title": "Head of purchasing", "note": "met in Kiel"})
    # The same footer read again is no version; another one is.
    book.reread(ident, FOUND)
    later = {**FOUND, "lines": ["Erik Einkauf", "Director"], "urls": ["www.nordwind.example"]}
    book.reread(ident, later)
    book.reread(ident, {**later, "date": "2026-09-09", "uid": "another mail"})      # the same lines from a newer mail
    kept = book.versions(ident)
    assert [(v["reason"], v["changed"]) for v in kept] == [
        ("reread", ["urls", "signature"]), ("changed", ["title", "note"]), ("changed", ["title"])]
    # Each version is the card as it was – newest first.
    assert [v["card"]["title"] for v in kept] == ["Head of purchasing", "Buyer", ""]
    assert kept[0]["card"]["signature"]["lines"] == FOUND["lines"] and kept[0]["card"]["note"] == "met in Kiel"
    assert set(kept[0]["card"]) == {"name", "title", "company", "department", "phones", "urls", "note", "signature"}
    assert all(v["replaced"].endswith("Z") for v in kept)
    # A bad change keeps nothing.
    with pytest.raises(own_contacts.BadValue):
        book.change(ident, {"title": "x", "note": "x" * 4001})
    assert len(book.versions(ident)) == 3
    # The card says how many there are – in the list and alone.
    assert book.one(ident)["versions"] == 3 and book.all()[0]["versions"] == 3
    # A removed card leaves what it said last; a card added again for the address goes on from there.
    book.remove(ident)
    assert book.versions(ident) is None
    again = book.add("erik.einkauf@example.com", "Erik Einkauf")
    book.change(again, {"company": "Nordwind GmbH"})
    kept = book.versions(again)
    # What the removed card said went as a whole: every field that held something is named.
    assert [(v["reason"], v["changed"]) for v in kept[:2]] == [
        ("changed", ["company"]), ("removed", ["name", "title", "phones", "urls", "note", "signature"])]
    assert kept[1]["card"]["title"] == "Head of purchasing" and len(kept) == 5
    # The version before the removal differs from the card as it was removed, not from the new one.
    assert kept[2]["changed"] == ["urls", "signature"]
    # An id is never given twice: what a client kept for the removed card names nobody else.
    assert again > ident
    last = book.add("lars.logistik@example.com")
    book.remove(last)
    assert book.add("mona.montage@example.com") > last
    # Another address has a history of its own.
    assert book.versions(book.add("greta.gast@example.com")) == []


def test_a_read_creates_nothing(tmp_path):
    path = tmp_path / "faelle.db"
    sqlite3.connect(path).close()                        # a profile from before the table
    assert own_contacts.OwnContacts(path).all() == []
    con = sqlite3.connect(path)
    assert con.execute("SELECT name FROM sqlite_master").fetchall() == []
    con.close()


# -- the routes ---------------------------------------------------------------
class FakeSearch:
    asked = []
    STATE = {}

    @classmethod
    def get_signatures(cls, addresses):
        cls.asked.append(addresses)
        return {"count": 1, "signatures": [FOUND]}


def test_a_contact_goes_through_the_methods(server, monkeypatch):  # noqa: F811
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSearch)
    assert roh(port, "GET", "/api/v1/contacts/own")[2] == {"items": [], "count": 0}
    code, head, card = roh(port, "POST", "/api/v1/contacts/own", {"address": "Erik.Einkauf@example.com"})
    assert code == 201 and head["Location"] == f"/api/v1/contacts/own/{card['id']}"
    # The name the mail says, the footer's numbers and its lines.
    assert card["name"] == "Erik Einkauf" and card["phones"] == FOUND["phones"]
    assert card["signature"]["lines"] == FOUND["lines"] and FakeSearch.asked[-1] == ["erik.einkauf@example.com"]
    # What Location names answers the card; the owner is known before a footer is read.
    assert roh(port, "GET", head["Location"])[2] == card and "own_account" in FakeSearch.STATE
    code, _, r = roh(port, "POST", "/api/v1/contacts/own", {"address": "erik.einkauf@example.com"})
    assert code == 409 and r["error"]["k"] == "srv.contact.exists"
    for body in ({}, {"address": "nobody"}, {"address": "x@example.com", "name": 7},
                 {"address": "x@example.com", "title": "filled in with PATCH, not here"}):
        code, _, r = roh(port, "POST", "/api/v1/contacts/own", body)
        assert code == 400 and r["error"]["k"] == "srv.contact.badvalue", body
    path = f"/api/v1/contacts/own/{card['id']}"
    code, _, changed = roh(port, "PATCH", path, {"title": "Head of purchasing", "company": "Nordwind GmbH"})
    assert code == 200 and (changed["title"], changed["company"], changed["name"]) \
        == ("Head of purchasing", "Nordwind GmbH", "Erik Einkauf")
    code, _, r = roh(port, "PATCH", path, {"title": "x", "phones": [{"type": "pager", "number": "1"}]})
    assert code == 400 and r["error"] == {"k": "srv.contact.badvalue", "v": {"field": "phones"}}
    assert roh(port, "PATCH", path, {})[0] == 400
    # What the change replaced is an earlier version.
    code, _, kept = roh(port, "GET", path + "/versions")
    assert code == 200 and kept["count"] == 1 and kept["items"][0]["changed"] == ["title", "company"]
    assert kept["items"][0]["reason"] == "changed" and kept["items"][0]["card"]["title"] == ""
    assert set(kept["items"][0]) == {"replaced", "reason", "card", "changed"}
    assert roh(port, "GET", "/api/v1/contacts/own")[2]["items"][0]["title"] == "Head of purchasing"
    # A refusal of the reading says why, as the card's fold does.
    monkeypatch.setattr(FakeSearch, "get_signatures", classmethod(lambda cls, a: {"error": "old", "signatures": []}))
    FakeSearch._NO_MAIL_LINES = "old"
    code, _, r = roh(port, "POST", path + "/signature")
    assert code == 409 and r["error"]["k"] == "srv.signature.oldindex"
    monkeypatch.undo()
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSearch)
    code, _, again = roh(port, "POST", path + "/signature")
    assert code == 200 and again["title"] == "Head of purchasing" and again["signature"]["lines"] == FOUND["lines"]
    for method, where in (("GET", "/api/v1/contacts/own/999"), ("GET", "/api/v1/contacts/own/" + "9" * 30), ("GET", "/api/v1/contacts/own/999/versions"), ("PATCH", "/api/v1/contacts/own/999"), ("DELETE", "/api/v1/contacts/own/999"),
                          ("POST", "/api/v1/contacts/own/999/signature"), ("DELETE", "/api/v1/contacts/own/x")):
        code, _, r = roh(port, method, where, {"title": "x"} if method == "PATCH" else None)
        assert code == 404 and r["error"]["k"] == "srv.contact.unknown", where
    assert roh(port, "DELETE", path)[0] == 204
    assert roh(port, "GET", "/api/v1/contacts/own")[2]["count"] == 0


def test_a_contact_is_added_without_an_index_and_read_again_only_with_one(server, monkeypatch):  # noqa: F811
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: None)
    code, _, card = roh(port, "POST", "/api/v1/contacts/own", {"address": "greta.gast@example.com", "name": "Greta Gast"})
    assert code == 201 and card["name"] == "Greta Gast" and card["signature"]["lines"] == []
    assert roh(port, "POST", f"/api/v1/contacts/own/{card['id']}/signature")[0] == 503


# -- what an MCP client reads -----------------------------------------------
@pytest.fixture
def archive_with_book(built, tmp_path):  # noqa: F811
    home, written = built
    old = dict(mcp_server.STATE)
    _point_at(written["store"], home)
    mcp_server.STATE["faelle_db"] = str(tmp_path / "faelle.db")
    yield own_contacts.OwnContacts(tmp_path / "faelle.db")
    mcp_server.STATE.clear()
    mcp_server.STATE.update(old)


def _without_card():
    known = {e["address"].lower() for c in sources.CONTACTS for e in c["emailAddresses"]}
    return next(r for r in _all(party="external", address_book="no") if r["address"] not in known)


def test_an_added_contact_is_an_address_book_contact_for_a_client(archive_with_book):
    row = _without_card()
    ident = archive_with_book.add(row["address"], row["name"],
                                  {"phones": [{"type": "mobile", "number": "+49 170 0000099"}]})
    archive_with_book.change(ident, {"title": "Buyer", "company": "Nordwind GmbH"})
    (now,) = _all(contains=row["address"])
    assert now["in_address_book"] is True
    card = now["contact"]
    assert card["origin"] == "munimentum" and card["uid"] == f"munimentum:contact:{ident}"
    assert (card["name"], card["title"], card["organisation"]) == (row["name"], "Buyer", "Nordwind GmbH")
    assert card["phones"] == ["+49 170 0000099"] and card["mobile"] == ["+49 170 0000099"]
    assert "department" not in card and "note" not in card             # only what it has
    assert row["address"] not in {r["address"] for r in _all(party="external", address_book="no")}
    (found,) = mcp_server.lookup_contact(row["address"])["contacts"]
    assert found["origin"] == "munimentum" and found["emails"] == [row["address"]]
    assert [c["uid"] for c in mcp_server.lookup_contact("0000099")["contacts"]] == [card["uid"]]   # by its number too
    assert [c["uid"] for c in mcp_server.lookup_contact("nordw*gmbh")["contacts"]] == [card["uid"]]  # the wildcard
    # The app's address book knows the person as one with a card.
    person = next(p for p in mcp_server.correspondent_people() if row["address"] in p["addresses"])
    assert person["card"]
    # A card renamed is a person renamed – the list is not kept under the old name.
    archive_with_book.change(ident, {"name": "Renamed By Hand"})
    assert next(p for p in mcp_server.correspondent_people() if row["address"] in p["addresses"])["name"] \
        == "Renamed By Hand"
    archive_with_book.remove(ident)
    assert _all(contains=row["address"])[0]["in_address_book"] is False
    assert not next(p for p in mcp_server.correspondent_people() if row["address"] in p["addresses"])["card"]


def test_the_outlook_card_always_wins(archive_with_book):
    dana = next(e["address"].lower() for c in sources.CONTACTS for e in c["emailAddresses"]
                if "dana" in e["address"].lower())
    archive_with_book.add(dana, "Somebody Else")
    (row,) = _all(contains=dana)
    assert "origin" not in row["contact"] and row["contact"]["family"] == "Dienstleister"
    assert all("origin" not in c for c in mcp_server.lookup_contact(dana)["contacts"])
    assert people.DOMAIN                                 # the cast is the archive's
