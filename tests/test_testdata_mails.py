"""The mails whose words are not in a plain text part – an invitation, an
empty text part beside HTML, a forward without own words, a picture with
no text – as the synthetic archive holds them (testdata/sources.py) and
as the index has to take them: findable, and not read again on the next
run. On a real mailbox nearly one mail in five was of these kinds, and
none of them reached the index before PARSER 4."""

import json
import sqlite3

import pytest

import corpus
import rag_index
from testdata import build as testdata_build
from testdata import sources


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return testdata_build.build(tmp_path_factory.mktemp("mail-shapes"), flat=True)


def _mail(key):
    return next(m for m in sources.MAILS if m["key"] == key)


def _found(store, word):
    con = sqlite3.connect(store / "corpus.db")
    try:
        return {rel for (rel,) in con.execute(
            "SELECT rel FROM chunks WHERE id IN "
            "(SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?)", (word,))}
    finally:
        con.close()


@pytest.mark.parametrize("word, key", [
    ("canteen", "invite-steering"),     # the invitation's description
    ("blueprint", "site-plan"),         # the HTML beside an empty text part
    ("warranty", "fwd-terms"),          # a forward that is all quote
    ("whiteboard", "photo"),            # no text at all: found by its subject
])
def test_every_kind_of_mail_is_found_by_its_words(built, word, key):
    assert _found(built["store"], word) == {sources.mail_rel(_mail(key))}


def test_an_invitation_names_its_place(built):
    con = sqlite3.connect(built["store"] / "corpus.db")
    try:
        (text,) = con.execute("SELECT text FROM chunks WHERE rel = ?",
                              (sources.mail_rel(_mail("invite-steering")),)).fetchone()
    finally:
        con.close()
    assert text.startswith("Ort: Board room.")


def test_the_invitation_rebuilds_no_appointment(built):
    """Its UID is the steering meeting's, which the calendar holds."""
    data = json.loads((built["store"] / "calendar.json").read_text(encoding="utf-8"))
    rebuilt = [r for r in data["recs"] if "rekonstruiert" in (r.get("ctx") or "")]
    assert not any("steering" in (r.get("title") or "").lower() for r in rebuilt)


def test_the_next_index_run_reads_no_mail_again(built):
    """Every mail has a chunk now, so an unchanged one is known."""
    manifest, chunks = rag_index._alter_bestand(built["store"])
    now = corpus.manifest("outlook", built["exports"] / "outlook_export")
    again = [rel for rel, sig in now.items()
             if manifest.get(("outlook", rel)) != sig or ("outlook", rel) not in chunks]
    assert again == []
