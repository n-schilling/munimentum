"""footers.py – the footer under each contact's mails, read by the index
run and kept in the profile: the store, who is read, and that a second
run reads nobody again."""

import sqlite3

import pytest

import faelle
import footers
import mcp_server
from testdata import build as testdata_build
from testdata import bulk, people

FOUND = {"address": "dana@example.com", "lines": ["Dana Dienstleister", "Tel. +49 40 000000 77"],
         "basis": "repeated", "uid": "outlook:E-Mail/m1.eml:0", "date": "2026-03-01"}


def test_a_footer_is_kept_with_the_moment_it_was_read(tmp_path):
    book = footers.Footers(tmp_path / "faelle.db")
    # Nothing is created by asking.
    assert book.found() == set() and book.one("dana@example.com") is None
    assert not (tmp_path / "faelle.db").exists()
    assert book.keep("Dana@Example.com", FOUND, (5.0, 2)) is True
    kept = book.one("dana@example.com")
    assert kept.pop("read").endswith("Z")
    assert kept == {k: FOUND[k] for k in ("lines", "basis", "uid", "date")}
    assert book.found() == {"dana@example.com"} and book.stamps() == {"dana@example.com": (5.0, 2)}
    # The same lines read again are no change; other lines are.
    assert book.keep("dana@example.com", FOUND, (6.0, 3)) is False
    assert book.stamps() == {"dana@example.com": (6.0, 3)}
    assert book.keep("dana@example.com", {**FOUND, "lines": ["Dana"]}) is True


def test_no_footer_is_kept_too_and_is_no_footer(tmp_path):
    book = footers.Footers(tmp_path / "faelle.db")
    assert book.keep("bob@nordwind.example", {"lines": [], "basis": "single", "uid": "x"}, (1.0, 1)) is False
    assert set(book.one("bob@nordwind.example")) == {"lines", "read"}
    assert book.found() == set() and "bob@nordwind.example" in book.stamps()


def test_the_address_book_s_people_are_the_ones_read():
    def person(n, card=False, frequent=False, received=1):
        return {"name": f"P{n}", "addresses": [f"p{n}@example.com", f"q{n}@example.com"],
                "writes_from": f"q{n}@example.com" if received else None,
                "received": received, "sent": 1, "frequent": frequent, "card": card}
    everyone = [person(1), person(2, card=True), person(3), person(4, frequent=True), person(5)]
    # The cap counts those without a card; a frequent contact passes it.
    assert [p["name"] for p in footers.listed(everyone, 1)] == ["P1", "P2", "P4"]
    assert footers.asked(everyone[0]) == "q1@example.com"
    # Someone who never wrote has no footer to read.
    assert footers.asked(person(6, received=0)) is None


def test_a_footer_is_held_against_a_card():
    shaped = {"phones": [{"type": "phone", "number": "+49 (0) 40 000000 77"}, {"type": "mobile", "number": "+49 170 0000077"}],
              "emails": ["office@example.com", "Dana@Example.com"], "urls": ["https://www.example.com/"]}
    # The number the card has under another spelling is the same number; an Outlook card's links are not compared.
    outlook = {"emails": ["dana@example.com"], "phones": ["040 000000 77"], "urls": None}
    assert footers.missing(shaped, outlook) == {"phones": [shaped["phones"][1]], "emails": ["office@example.com"]}
    # A card of the app's own names links: one it has in another spelling is not missing.
    own = {"emails": ["dana@example.com", "office@example.com"], "phones": ["+49 40 000000 77", "0170 0000077"],
           "urls": ["example.com"]}
    assert footers.missing(shaped, own) == {}
    assert footers.missing(shaped, {**own, "urls": ["nordwind.example"]}) == {"urls": ["https://www.example.com/"]}
    # Two numbers that merely end alike on a few digits are two numbers.
    assert not footers.same_phone("+49 40 12345", "2345") and not footers.same_phone("", "")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    home = tmp_path_factory.mktemp("footers")
    return home, testdata_build.build(home, flat=True)


@pytest.fixture
def state():
    old = dict(mcp_server.STATE)
    yield
    mcp_server.STATE.clear()
    mcp_server.STATE.update(old)
    mcp_server._MAILBOX.clear()


def test_the_index_run_keeps_the_footers_and_the_next_reads_nobody(built, state):
    home, written = built
    book = footers.Footers(home / faelle.DB_NAME)
    dana, bob = people.EXTERNALS[0], people.COLLEAGUES[0]
    # The build's index run read them (rag_index.main).
    kept = book.one(dana[1])
    assert kept["lines"] == bulk.footer(dana).split("\n")[:-1] and kept["basis"] == "repeated"
    assert kept["uid"].startswith("outlook:E-Mail/") and kept["read"]
    assert dana[1] in book.found() and bob[1] not in book.found()
    # A colleague signs with the name alone: read, kept as none, not asked again.
    assert book.one(bob[1])["lines"] == []
    assert footers.fill(home, written["store"], home / "outlook_export") == (0, 0, 0)
    # New mail since the reading: that address alone is read again.
    con = sqlite3.connect(book.path)
    con.execute("UPDATE footers SET mails = mails - 1, read = 'then' WHERE address = ?", (dana[1],))
    con.commit()
    con.close()
    assert footers.fill(home, written["store"], home / "outlook_export") == (1, 0, 1)
    assert book.one(dana[1])["read"] != "then"


def test_the_archive_s_cards_are_held_against_their_footers(built, state):
    """Greta's Outlook card has no number, her footer has: a possible update."""
    home, written = built
    mcp_server.STATE.clear()
    mcp_server.STATE.update(db=str(written["store"] / "corpus.db"), outlook_dir=str(home / "outlook_export"),
                            faelle_db=str(home / faelle.DB_NAME), home=str(home))
    greta, lars = people.EXTERNALS[2][1], people.EXTERNALS[5][1]
    cards = mcp_server.cards_shaped()
    assert cards[greta] == {"emails": [greta], "phones": [], "urls": None} and lars not in cards
    kept = footers.Footers(home / faelle.DB_NAME).lines()
    lacking = footers.missing(mcp_server.signature.fields(kept[greta]), cards[greta])
    assert lacking == {"phones": [{"type": "mobile", "number": "+49 170 0000042"}]}


def test_nothing_is_read_without_an_index(tmp_path):
    assert footers.fill(tmp_path, tmp_path / "store", tmp_path / "outlook") is None
    assert not (tmp_path / faelle.DB_NAME).exists()
