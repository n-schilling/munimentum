"""correspondents.py and its two MCP tools – whom the mailbox's owner
writes with, and the footers under their mails.

The module is held against a handful of hand-written mails, where every
count can be read off the list; the tools against the synthetic archive
(testdata/), whose expected numbers are derived from its mail list – and
against the same archive with the `correspondence` table taken away, as an
index from before it has it."""

import json
import shutil
import sqlite3
from datetime import UTC, datetime

import anyio
import pytest
from mcp.client.client import Client

import corpus
import correspondents
import mcp_server
import rag_index
from testdata import build as testdata_build
from testdata import bulk, people, sources

ME, ALIAS = "alice@nordwind.example", "a.beispiel@nordwind.example"
DANA, NEWS, LIST = "dana@example.com", "news@shop.example", "team@nordwind.example"


def _mail(n, sender, name, to, cc="", bcc="", thread="t1", day=1):
    ts = datetime(2026, 3, day, 9, n, tzinfo=UTC).timestamp()
    return {"uid": f"outlook:E-Mail/m{n}.eml:0", "src": "outlook", "root": "outlook",
            "rel": f"E-Mail/m{n}.eml", "who": name, "who_mail": sender, "ppl": "",
            "ts": ts, "date": "", "title": f"Mail {n}", "ctx": "E-Mail", "text": "words",
            "thread": thread, "to_ppl": to, "cc_ppl": cc, "bcc_ppl": bcc}


MAILS = [
    _mail(1, DANA, "Dana Dienstleister", f"alice beispiel {ME}", thread="offer", day=1),
    _mail(2, ME, "Alice Beispiel", f"dana dienstleister sales @ example ({NEWS}) {DANA}", cc=f"{LIST}", thread="offer", day=2),
    _mail(3, DANA, "Dana Dienstleister", f"{ME}", thread="offer", day=3),
    _mail(4, NEWS, "Shop News", f"{ME}", thread="n1", day=4),
    _mail(5, NEWS, "Shop News", f"{ME}", thread="n2", day=5),
    # The owner under her second address, and a blind copy.
    _mail(6, ALIAS, "Alice Beispiel", f"{DANA}", bcc=f"{NEWS}", thread="plan", day=6),
    # A colleague writes to the list: the owner is not named at all.
    _mail(7, "bob@nordwind.example", "Bob Baumeister", f"{LIST}", cc=f"{DANA}", thread="list", day=7),
    _mail(8, "bob@nordwind.example", "Bob Baumeister", f"{LIST}", thread="list", day=8),
    _mail(9, "bob@nordwind.example", "(unbekannt)", f"{LIST} {ME}", thread="list", day=9),
]


@pytest.fixture
def con(tmp_path):
    chunks = corpus.chunk_records([dict(m) for m in MAILS])
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(tmp_path, chunks)
    db = sqlite3.connect(tmp_path / "corpus.db")
    yield db
    db.close()


def _own(db):
    return correspondents.own_addresses(*correspondents.profile(db))


def test_the_index_run_writes_one_slim_row_per_mail(con):
    rows = con.execute("SELECT sender, name, rcpt, thread, rel FROM correspondence "
                       "ORDER BY ts").fetchall()
    assert len(rows) == len(MAILS)
    assert rows[1] == (ME, "Alice Beispiel", f"{DANA} {LIST}", "offer", "E-Mail/m2.eml")
    assert rows[5][2] == f"{DANA} {NEWS}"            # the blind copy is a recipient
    assert rows[8][1] == ""                          # the unknown sender has no name


def test_the_owner_is_the_party_to_the_most_mails_under_every_address(con):
    """The list stands in as many mails as the owner, but never writes:
    it is no person. The second address writes under the owner's name."""
    stats, names = correspondents.profile(con)
    assert stats[LIST] == [0, 4] and stats[ME] == [1, 5]
    assert correspondents.own_addresses(stats, names) == {ME, ALIAS}
    assert names["bob@nordwind.example"] == "Bob Baumeister"
    assert correspondents.own_addresses({}, {}) == set()


def test_the_name_the_user_gave_the_app_counts_as_theirs(con):
    stats, names = correspondents.profile(con)
    names = {**names, ALIAS: "A. Beispiel"}
    assert correspondents.own_addresses(stats, names) == {ME}
    assert correspondents.own_addresses(stats, names, "a. beispiel") == {ME, ALIAS}


def test_the_signed_in_account_says_whose_mailbox_it_is(con):
    """An archive without the sent folder: the owner never writes, so the
    mailbox alone would name the busiest colleague."""
    stats, names = correspondents.profile(con)
    stats = {a: ([0, got] if a in (ME, ALIAS) else [sent, got]) for a, (sent, got) in stats.items()}
    names = {a: n for a, n in names.items() if a not in (ME, ALIAS)}
    assert ME not in correspondents.own_addresses(stats, names)
    assert correspondents.own_addresses(stats, names, account=ME.upper()) == {ME}
    assert correspondents.own_addresses(stats, names, "Bob Baumeister", ME) == {ME, "bob@nordwind.example"}
    # An account the mailbox does not know says nothing about it.
    assert (correspondents.own_addresses(stats, names, account="carla@nordwind.example")
            == correspondents.own_addresses(stats, names))


def test_a_sign_in_name_that_never_writes_joins_the_address_that_does(con):
    """The account need not be the address the owner writes from: known
    to the mailbox from two mails only, it does not replace her."""
    stats, names = correspondents.profile(con)
    sign_in = "alice@nordwind.onmicrosoft.example"
    stats = {**stats, sign_in: [0, 1]}
    assert correspondents.own_addresses(stats, names, account=sign_in) == {ME, ALIAS, sign_in}
    # One that writes is the owner's word, whatever the counts say.
    assert correspondents.own_addresses({**stats, sign_in: [1, 0]}, names, account=sign_in) == {sign_in}


def test_a_mail_filed_twice_is_one_mail(tmp_path):
    """Written to a list the owner belongs to: the mail lies in Sent and
    in the Inbox, and is one mail to its recipient."""
    def filed(n, folder, ident):
        mail = _mail(n, ME, "Alice Beispiel", DANA, thread="t")
        return {**mail, "rel": f"E-Mail/{folder}/m{n}.eml", "uid": f"outlook:E-Mail/{folder}/m{n}.eml:0",
                "key": ident}
    mails = [filed(1, "Sent", "mail:<a@x>"), filed(1, "Inbox", "mail:<a@x>"),
             filed(2, "Sent", "mail:<b@x>"), {**filed(3, "Sent", None)}, {**filed(4, "Sent", None)},
             _mail(5, DANA, "Dana Dienstleister", ME)]
    chunks = corpus.chunk_records([dict(m) for m in mails])
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(tmp_path, chunks)
    db = sqlite3.connect(tmp_path / "corpus.db")
    try:
        assert correspondents.collect(db, {ME})[DANA]["sent"] == 4
        db.execute("DROP TABLE correspondence")
        assert correspondents.collect(db, {ME})[DANA]["sent"] == 4      # an index from before
    finally:
        db.close()


def test_only_a_word_that_is_an_address_is_a_recipient():
    """A display name may carry an "@" or an address in brackets."""
    line = "sales @ nordwind sales@nordwind.example bob (bob@old.example) <x@old.example> bob@nordwind.example"
    assert correspondents.addresses_of(line, None, "") == {"sales@nordwind.example", "bob@nordwind.example"}
    # … and every word that is one: the local part as mail allows it.
    assert correspondents.addresses_of("o'brien@example.com r&d=x/y@example.com") == {
        "o'brien@example.com", "r&d=x/y@example.com"}


def test_both_directions_are_counted_apart(con):
    found = correspondents.collect(con, _own(con))
    assert ME not in found and ALIAS not in found
    dana = found[DANA]
    assert (dana["received"], dana["sent"], dana["threads"]) == (2, 2, 2)
    assert datetime.fromtimestamp(dana["first"], UTC).day == 1
    assert datetime.fromtimestamp(dana["last"], UTC).day == 6
    # A newsletter: all received – the one "sent" is the blind copy.
    assert (found[NEWS]["received"], found[NEWS]["sent"]) == (2, 1)
    assert (found[LIST]["received"], found[LIST]["sent"]) == (0, 1)


def test_standing_beside_the_owner_in_someone_elses_mail_is_no_contact(con):
    """Bob's mail names Dana in Cc: that counts for Bob, not for Dana."""
    found = correspondents.collect(con, _own(con))
    assert found["bob@nordwind.example"] == {
        "received": 3, "sent": 0, "threads": 1,
        "first": MAILS[6]["ts"], "last": MAILS[8]["ts"]}
    assert found[DANA]["received"] + found[DANA]["sent"] == 4


def test_a_window_counts_only_what_lies_in_it(con):
    since, until = MAILS[1]["ts"], MAILS[3]["ts"]
    found = correspondents.collect(con, _own(con), since, until)
    assert set(found) == {DANA, NEWS, LIST}
    assert (found[DANA]["received"], found[DANA]["sent"]) == (1, 1)
    assert found[NEWS]["received"] == 1


def test_an_index_from_before_the_table_gives_the_same_answer(con):
    own = _own(con)
    with_table = (correspondents.profile(con), correspondents.collect(con, own),
                  correspondents.signature_mails(con, [DANA, NEWS], 5))
    con.execute("DROP TABLE correspondence")
    assert correspondents.readable(con)
    assert (correspondents.profile(con), correspondents.collect(con, own),
            correspondents.signature_mails(con, [DANA, NEWS], 5)) == with_table


def test_the_mails_that_open_a_conversation_come_before_the_answers(tmp_path):
    """Dana opens two conversations and answers four times in the first:
    the two openers take turns with the newest answers."""
    mails = [_mail(1, DANA, "Dana", ME, thread="a", day=1),
             *[_mail(n, DANA, "Dana", ME, thread="a", day=n) for n in (2, 3, 4, 5)],
             _mail(6, DANA, "Dana", ME, thread="b", day=6),
             _mail(7, DANA, "Dana", ME, thread="b", day=7)]
    chunks = corpus.chunk_records(mails)
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(tmp_path, chunks)
    db = sqlite3.connect(tmp_path / "corpus.db")
    try:
        picked = correspondents.signature_mails(db, [DANA.upper()], 4)[DANA]
    finally:
        db.close()
    assert [rel for rel, _ts in picked] == ["E-Mail/m6.eml", "E-Mail/m7.eml",
                                            "E-Mail/m1.eml", "E-Mail/m5.eml"]


def _exchange(n, address, name, day, mine=False, me=ME):
    """One mail between the owner and `address` – from it, or to it."""
    return (_mail(n, me, "Alice Beispiel", address, thread=f"t{n}", day=day) if mine
            else _mail(n, address, name, me, thread=f"t{n}", day=day))


def _people(tmp_path, written, book=None, **rule):
    chunks = corpus.chunk_records(written)
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(tmp_path, chunks)
    db = sqlite3.connect(tmp_path / "corpus.db")
    try:
        _stats, names = correspondents.profile(db)
        return {p["name"]: p for p in correspondents.people(db, {ME}, names, book or {}, **rule)}
    finally:
        db.close()


def test_a_person_is_a_card_or_an_address_never_a_name(tmp_path):
    """Dana's card lists two addresses: one person, whatever spelling they
    write under. Her third address is not on the card, and two people
    called Erik Einkauf write from two companies: each address is a
    person of its own – a name is shared often enough."""
    card = {"uid": "kontakte:kontakte/dana.vcf:0", "name": "Dana Dienstleister"}
    mails = [
        _exchange(1, "dana@example.com", "Dana Dienstleister", 1),
        _exchange(2, "d.dienstleister@example.com", "Dienstleister, Dana", 2),
        _exchange(3, "d.dienstleister@example.com", "Dienstleister, Dana", 3),
        _exchange(4, "dana@home.example", "Dana Dienstleister", 4),
        _exchange(5, "dana@example.com", "", 5, mine=True),
        _exchange(6, "erik@example.com", "Erik Einkauf", 6),
        _exchange(7, "erik@nordwind.example", "Erik Einkauf", 7),
        _exchange(8, "erik@nordwind.example", "Erik Einkauf", 8),
        _exchange(9, "news@shop.example", "", 9),
    ]
    book = {"dana@example.com": card, "d.dienstleister@example.com": card}
    chunks = corpus.chunk_records(mails)
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(tmp_path, chunks)
    db = sqlite3.connect(tmp_path / "corpus.db")
    try:
        _stats, names = correspondents.profile(db)
        found = correspondents.people(db, {ME}, names, book)
    finally:
        db.close()
    by_address = {p["addresses"][0]: p for p in found}
    assert len(found) == 5 and all(len(p["addresses"]) == 1 or p["card"] for p in found)
    dana = by_address["d.dienstleister@example.com"]
    assert dana["name"] == "Dana Dienstleister" and dana["card"] is True
    assert dana["addresses"] == ["d.dienstleister@example.com", "dana@example.com"]
    assert (dana["received"], dana["sent"], dana["also"]) == (3, 1, ["Dienstleister, Dana"])
    assert datetime.fromtimestamp(dana["last"], UTC).day == 5
    home = by_address["dana@home.example"]
    assert (home["name"], home["card"], home["received"]) == ("Dana Dienstleister", False, 1)
    eriks = [by_address["erik@nordwind.example"], by_address["erik@example.com"]]
    assert [(p["name"], p["received"]) for p in eriks] == [("Erik Einkauf", 2), ("Erik Einkauf", 1)]
    assert by_address["news@shop.example"]["name"] == "news@shop.example"


def test_a_mail_from_the_future_does_not_move_the_window(tmp_path):
    """One piece of spam dated 2037: the frequent contacts are still
    counted back from the newest mail that is not after today."""
    written = []
    for i in range(12):
        mail = _exchange(i, DANA, "Dana Dienstleister", 1 + i, mine=i == 0)
        mail["ts"] = datetime(2026, 3, 1 + i, tzinfo=UTC).timestamp()
        written.append(mail)
    spam = _exchange(50, NEWS, "Shop News", 1)
    spam["ts"] = datetime(2037, 1, 1, tzinfo=UTC).timestamp()
    chunks = corpus.chunk_records([*written, spam])
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(tmp_path, chunks)
    db = sqlite3.connect(tmp_path / "corpus.db")
    try:
        _stats, names = correspondents.profile(db)
        now = datetime(2026, 11, 21, tzinfo=UTC).timestamp()
        found = {p["name"]: p for p in correspondents.people(db, {ME}, names, {}, now=now)}
        assert found["Dana Dienstleister"]["frequent"] is True
        # Before the fix's rule: with the clock in 2038 the spam is the newest mail.
        later = {p["name"]: p for p in correspondents.people(
            db, {ME}, names, {}, now=datetime(2038, 1, 1, tzinfo=UTC).timestamp())}
        assert later["Dana Dienstleister"]["frequent"] is False
        # … and counts for nobody: twelve mails dated 2037 and one answer
        # make no frequent contact today.
        assert found["Shop News"]["frequent"] is False
    finally:
        db.close()


def test_a_relay_writing_under_everybodys_name_is_nobodys_address(tmp_path):
    """A sharing notification writes under the name of whoever shared: it
    is not Bob's second address because Bob shares most – and a sender
    that is an Exchange path is no address at all."""
    relay, bob = "no-reply@share.example", "bob@nordwind.example"
    mails = [_exchange(n, bob, "Bob Baumeister", 1 + n, mine=n % 2 == 0) for n in range(6)]
    sharers = ["Bob Baumeister"] * 4 + ["Carla Chef"] * 3 + ["Erik Einkauf"] * 3
    mails += [_exchange(10 + n, relay, name, 10 + n) for n, name in enumerate(sharers)]
    mails += [_exchange(30 + n, "/o=exchangelabs/ou=x/cn=recipients/cn=bob", "Bob Baumeister", 20 + n)
              for n in range(3)]
    found = _people(tmp_path, mails)
    assert found["Bob Baumeister"]["addresses"] == [bob]
    assert (found["Bob Baumeister"]["received"], found["Bob Baumeister"]["writes_from"]) == (3, bob)
    # The relay has no name of its own, and is named by its address.
    assert found[relay]["received"] == 10 and found[relay]["also"] == []
    assert all("@" in a for p in found.values() for a in p["addresses"])


def test_a_mail_to_both_addresses_of_a_card_is_one_mail(tmp_path):
    """Six mails to Dana's two addresses at once and one answer: seven
    mails, not thirteen – and no frequent contact by a rule of twelve."""
    card = {"uid": "kontakte:kontakte/dana.vcf:0", "name": "Dana Dienstleister"}
    both = "dana@example.com dana@home.example"
    mails = [_exchange(n, both, "", 1 + n, mine=True) for n in range(6)]
    mails.append(_exchange(7, "dana@home.example", "Dana Dienstleister", 8))
    dana = _people(tmp_path, mails, {a: card for a in both.split()})["Dana Dienstleister"]
    assert (dana["received"], dana["sent"], dana["frequent"]) == (1, 6, False)
    assert datetime.fromtimestamp(dana["last"], UTC).day == 8


def test_an_address_in_from_is_no_name(tmp_path):
    """A From without a display name is indexed under its address."""
    card = {"uid": "kontakte:kontakte/erik.vcf:0", "name": "Erik Einkauf"}
    mails = [_exchange(1, "erik@example.com", "Erik@Example.com", 1),
             _exchange(2, "greta@example.com", "greta@example.com", 2)]
    found = _people(tmp_path, mails, {"erik@example.com": card})
    assert found["Erik Einkauf"]["also"] == []
    assert found["greta@example.com"]["also"] == []


def test_the_footer_is_asked_from_the_address_that_writes(tmp_path):
    """Of a card's addresses the one most of the person's mail comes from."""
    card = {"uid": "kontakte:kontakte/dana.vcf:0", "name": "Dana Dienstleister"}
    mails = [_exchange(1, "dana@example.com", "", 1, mine=True),
             _exchange(2, "dana@example.com", "", 2, mine=True),
             _exchange(3, "dana@example.com", "", 3, mine=True),
             _exchange(4, "dana@home.example", "Dana Dienstleister", 4)]
    book = {"dana@example.com": card, "dana@home.example": card}
    dana = _people(tmp_path, mails, book)["Dana Dienstleister"]
    assert dana["addresses"][0] == "dana@example.com" and dana["writes_from"] == "dana@home.example"


def test_a_frequent_contact_writes_back_and_often_and_lately(tmp_path):
    """Twelve mails in the archive's last twelve months, both directions:
    frequent. Eleven: not. Twelve without an answer: not. Twelve two years
    before the archive's newest mail: not."""
    def mails(address, name, count, answered, year=2026):
        out = []
        for i in range(count):
            mail = _exchange(i, address, name, 1 + i, mine=answered and i == 0)
            mail["ts"] = datetime(year, 3, 1 + i, tzinfo=UTC).timestamp()
            out.append(mail)
        return out
    everyone = (mails("often@example.com", "Often", 12, True)
                + mails("almost@example.com", "Almost", 11, True)
                + mails("oneway@example.com", "One Way", 12, False)
                + mails("longago@example.com", "Long Ago", 12, True, year=2024))
    for n, mail in enumerate(everyone):
        mail["rel"] = f"E-Mail/f{n}.eml"
        mail["uid"] = f"outlook:E-Mail/f{n}.eml:0"
    found = _people(tmp_path, everyone)
    assert {name: p["frequent"] for name, p in found.items()} == {
        "Often": True, "Almost": False, "One Way": False, "Long Ago": False}
    # Both numbers are settings: eleven mails are enough when eleven are
    # asked, three years reach the old exchange – and no number makes a
    # contact of someone who was never written to.
    wide = _people(tmp_path, everyone, months=36, mails=11)
    assert {name: p["frequent"] for name, p in wide.items()} == {
        "Often": True, "Almost": True, "One Way": False, "Long Ago": True}


# --------------------------------------------------------------------------
# The tools, on the synthetic archive
# --------------------------------------------------------------------------
MY_ADDRESS = people.ME[1]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    home = tmp_path_factory.mktemp("correspondents")
    return home, testdata_build.build(home, flat=True)


def _point_at(store, home):
    mcp_server.STATE.clear()
    mcp_server.STATE.update(db=str(store / "corpus.db"),
                            outlook_dir=str(home / "outlook_export"),
                            internal_domains=people.DOMAIN, own_name="")


@pytest.fixture
def archive(built):
    home, written = built
    old = dict(mcp_server.STATE)
    _point_at(written["store"], home)
    yield home
    mcp_server.STATE.clear()
    mcp_server.STATE.update(old)


def _stamp(mail):
    return mail["when"].replace(tzinfo=UTC).timestamp()


def _expected(since=None, until=None):
    """address → [received, sent], counted from the list the archive was
    written from."""
    out = {}
    for mail in sources.MAILS:
        ts = _stamp(mail)
        if (since is not None and ts < since) or (until is not None and ts > until):
            continue
        sender = mail["frm"][1]
        if sender == MY_ADDRESS:
            named = {p[1] for line in ("to", "cc", "bcc") for p in mail.get(line) or []}
            for address in named - {MY_ADDRESS}:
                out.setdefault(address, [0, 0])[1] += 1
        else:
            out.setdefault(sender, [0, 0])[0] += 1
    return out


def _all(**filters):
    rows, offset = [], 0
    while offset is not None:
        page = mcp_server.list_correspondents(limit=7, offset=offset, **filters)
        assert "error" not in page
        rows += page["correspondents"]
        offset = page["next_offset"]
    assert len(rows) == page["total"]
    return rows


def test_every_correspondent_is_listed_with_both_counts(archive):
    first = mcp_server.list_correspondents(min_sent=0)
    assert first["me"] == [MY_ADDRESS]
    rows = _all(min_sent=0)
    assert {r["address"]: [r["received"], r["sent"]] for r in rows} == _expected()
    volume = [r["received"] + r["sent"] for r in rows]
    assert volume == sorted(volume, reverse=True)
    assert all(r["first"] <= r["last"] and r["threads"] >= 1 for r in rows)


def test_the_default_lists_only_whom_the_user_wrote_to(archive):
    expected = {a for a, (_got, sent) in _expected().items() if sent}
    assert {r["address"] for r in _all()} == expected
    assert {r["address"] for r in _all(min_sent=0, min_received=1)} \
        == {a for a, (got, _sent) in _expected().items() if got}


def test_party_address_book_and_domains_narrow_the_list(archive):
    outside = {mail for _name, mail in people.EXTERNALS}
    external = _all(party="external")
    assert {r["address"] for r in external} == outside & set(_expected())
    assert all(r["external"] and r["domain"] == "example.com" for r in external)
    assert not {r["address"] for r in _all(party="internal")} & outside
    assert _all(exclude_domains="example.com, " + people.DOMAIN) == []
    known = {e["address"].lower() for c in sources.CONTACTS for e in c["emailAddresses"]}
    missing = _all(party="external", address_book="no")
    assert missing and not {r["address"] for r in missing} & known
    assert all(not r["in_address_book"] and "contact" not in r for r in missing)
    assert [r["name"] for r in _all(contains="DIENSTL")] == ["Dana Dienstleister"]


def test_a_card_from_the_address_book_comes_field_by_field(archive):
    (dana,) = _all(contains="dana.dienstleister")
    assert dana["in_address_book"] is True
    card = dana["contact"]
    assert (card["given"], card["family"], card["organisation"], card["title"]) \
        == ("Dana", "Dienstleister", "Beispiel GmbH", "Project manager")
    assert card["phones"] == ["+49 40 000000 77", "+49 170 0000077"]
    assert card["mobile"] == ["+49 170 0000077"]
    assert "department" not in card and "emails" not in card      # only what it has
    assert mcp_server.get_document(card["uid"])["title"] == "Dana Dienstleister"
    (bob,) = mcp_server.lookup_contact("bob.baumeister")["contacts"]
    assert (bob["department"], bob["title"], bob["mobile"]) == ("IT", "Systems engineer", [])


def test_a_window_counts_the_mails_of_its_days(archive):
    since = datetime(people.YEAR, 2, 1).timestamp()
    until = datetime(people.YEAR, 3, 31, 23, 59, 59).timestamp()
    rows = _all(min_sent=0, date_from=f"{people.YEAR}-02-01", date_to=f"{people.YEAR}-03-31")
    assert {r["address"]: [r["received"], r["sent"]] for r in rows} == _expected(since, until)
    assert rows and len(rows) <= len(_expected())


def test_the_other_orders_and_the_refusals(archive):
    recent = [r["last"] for r in _all(sort="recent")]
    assert recent == sorted(recent, reverse=True)
    threads = [r["threads"] for r in _all(sort="threads")]
    assert threads == sorted(threads, reverse=True)
    for wrong in ({"party": "foreign"}, {"sort": "name"}, {"address_book": "maybe"},
                  {"date_from": "2026-02-31"}):
        answer = mcp_server.list_correspondents(**wrong)
        assert answer["error"] and answer["correspondents"] == []
    mcp_server.STATE["internal_domains"] = ""
    assert "internal domains" in mcp_server.list_correspondents(party="external")["error"]
    assert "external" not in mcp_server.list_correspondents(limit=1)["correspondents"][0]


def test_a_day_the_platform_cannot_say_is_no_day(monkeypatch):
    """Windows refuses a Date header from before 1970: one such mail must
    not take the whole list down."""
    assert mcp_server._day(None) is None and mcp_server._day(86400 * 365.25 * 30).startswith("2000")

    class Refusing:
        @staticmethod
        def fromtimestamp(ts):
            raise OSError(22, "Invalid argument")
    monkeypatch.setattr(mcp_server, "date", Refusing)
    assert mcp_server._day(-3600) is None


def test_own_addresses_are_read_as_the_index_reads_addresses(archive):
    """An apostrophe is part of an address – and a `me` that names none
    is refused instead of being guessed around."""
    assert mcp_server._named_addresses("O'Brien@Example.com, x@example.com; nobody") == {
        "o'brien@example.com", "x@example.com"}
    assert mcp_server.list_correspondents(me="o'brien@example.com")["me"] == ["o'brien@example.com"]
    assert "No mail address" in mcp_server.list_correspondents(me="Alice Beispiel")["error"]


def test_internal_is_the_domain_itself_as_the_search_tells_it(archive):
    """A subdomain of an internal domain is another domain – for the
    search's party filter, the page and this list alike."""
    mcp_server.STATE["internal_domains"] = "example"
    assert all(r["external"] for r in mcp_server.list_correspondents(min_sent=0)["correspondents"])


def test_named_own_addresses_replace_the_ones_read_from_the_mailbox(archive):
    bob = people.COLLEAGUES[0][1]
    as_bob = mcp_server.list_correspondents(me=f"{bob}, nobody", limit=200)
    assert as_bob["me"] == [bob]
    addresses = {r["address"] for r in as_bob["correspondents"]}
    assert bob not in addresses and MY_ADDRESS in addresses


def test_a_footer_is_read_from_the_mails_that_carry_it(archive):
    dana, greta, bob = people.EXTERNALS[0], people.EXTERNALS[2], people.COLLEAGUES[0]
    answer = mcp_server.get_signatures([dana[1].upper(), greta[1], bob[1], dana[1]])
    assert answer["count"] == 3
    by = {s["address"]: s for s in answer["signatures"]}
    found = by[dana[1]]
    # … without the register's line, which is about the company.
    assert found["lines"] == bulk.footer(dana).split("\n")[:-1] and found["basis"] == "repeated"
    assert found["phones"] == [{"type": "phone", "number": "+49 40 000000 77"},
                               {"type": "mobile", "number": "+49 170 0000077"}]
    assert found["emails"] == [] and found["urls"] == ["www.example.com"]
    # The mail named is one of hers, and the footer stands in it.
    mail = mcp_server.get_document(found["uid"])
    assert mail["who"] == dana[0] and "Beispielweg 1" in mail["text"]
    assert found["date"] == mail["date"][:10]
    # What the address book lacks: Greta's card has no number, her footer has.
    assert by[greta[1]]["phones"] == [{"type": "mobile", "number": "+49 170 0000042"}]
    (card,) = mcp_server.lookup_contact(greta[1])["contacts"]
    assert card["phones"] == []
    # A colleague signs with the name alone: no footer, said as such.
    assert by[bob[1]]["lines"] == [] and by[bob[1]]["mails_read"] > 0
    assert "basis" not in by[bob[1]]


def test_signatures_are_asked_in_batches_and_need_the_files(archive):
    many = [f"person{n}@example.com" for n in range(26)]
    assert "At most 25" in mcp_server.get_signatures(many)["error"]
    assert mcp_server.get_signatures(["no address"])["error"]
    unknown = mcp_server.get_signatures(["nobody@example.org"])["signatures"]
    assert unknown == [{"address": "nobody@example.org", "name": "", "lines": [], "mails_read": 0}]
    mcp_server.STATE["outlook_dir"] = None
    assert "not at hand" in mcp_server.get_signatures(many[:1])["error"]


def test_mails_in_the_index_and_no_file_at_hand_is_not_no_signature(archive, tmp_path):
    """The export folder moved, the index stayed: that is said, instead of
    "this person signs without a footer"."""
    address = mcp_server.list_correspondents(limit=1)["correspondents"][0]["address"]
    assert "error" not in mcp_server.get_signatures([address])
    mcp_server.STATE["outlook_dir"] = str(tmp_path)
    assert mcp_server.get_signatures([address])["error"] == mcp_server._NO_MAIL_FILES
    # Someone the index has no mail of is still just someone without one.
    assert mcp_server.get_signatures(["nobody@example.org"])["signatures"][0]["lines"] == []


def test_an_index_from_before_the_table_answers_the_same(archive, built, tmp_path):
    dana = people.EXTERNALS[0][1]
    before = (_all(min_sent=0), mcp_server.get_signatures([dana]))
    store = tmp_path / "old_store"
    shutil.copytree(built[1]["store"], store,
                    ignore=shutil.ignore_patterns("*-wal", "*-shm"))   # a reader's passing files
    con = sqlite3.connect(store / "corpus.db")
    con.execute("DROP TABLE correspondence")
    con.commit()
    con.close()
    _point_at(store, archive)
    assert (_all(min_sent=0), mcp_server.get_signatures([dana])) == before


def test_a_client_asks_both_tools_over_mcp(archive):
    """As a client does: the list first, then the footers of the people in
    it – two calls for what took a call per person."""
    async def ask():
        async with Client(mcp_server.mcp) as client:
            listed = await client.call_tool("list_correspondents",
                                            {"party": "external", "limit": 3})
            rows = json.loads(listed.content[0].text)["correspondents"]
            read = await client.call_tool("get_signatures",
                                          {"addresses": [r["address"] for r in rows]})
            return rows, json.loads(read.content[0].text)
    rows, footers = anyio.run(ask)
    assert len(rows) == 3 and footers["count"] == 3
    assert [s["address"] for s in footers["signatures"]] == [r["address"] for r in rows]
    assert all(s["lines"] and s["phones"] for s in footers["signatures"])


def test_a_stricter_rule_marks_fewer_people_on_the_same_index(archive):
    usual = [p["name"] for p in mcp_server.correspondent_people() if p["frequent"]]
    strict = [p["name"] for p in mcp_server.correspondent_people(months=1, mails=5) if p["frequent"]]
    assert usual and len(strict) < len(usual) and set(strict) <= set(usual)
    assert [p["name"] for p in mcp_server.correspondent_people() if p["frequent"]] == usual


def test_the_address_book_gets_the_people_with_the_frequent_ones_marked(archive):
    """Per person, as the page reads it: every external of the cast writes
    and is written to all year – frequent; the card's name leads."""
    found = {p["name"]: p for p in mcp_server.correspondent_people()}
    counts = _expected()
    for name, address in people.EXTERNALS:
        person = found[name]
        assert person["addresses"] == [address]
        assert [person["received"], person["sent"]] == counts[address]
        assert person["frequent"] is (sum(counts[address]) >= 12 and all(counts[address]))
    assert any(p["frequent"] for p in found.values())
    assert people.ME[0] not in found


def test_a_namesake_of_the_owner_is_somebody_else():
    """A person is an address: an address under the owner's name is the
    owner's only while it hardly ever writes to the owner – a namesake does, on
    whatever domain, a colleague as well as a customer. Only the name the
    user gave the app says "me" for every address under it."""
    stats = correspondents.Stats({
        "alex@nordwind.example": [50, 60], "alex@nordwind.onmicrosoft.example": [5, 2],
        "alex.beispiel2@nordwind.example": [8, 3], "alex@example.co.uk": [4, 1],
        "dana@example.com": [4, 9]})
    # The second address forwarded one mail to the first: still the owner's.
    stats.wrote_to = {"alex@nordwind.example": {"dana@example.com": 40, "alex.beispiel2@nordwind.example": 3},
                      "alex@nordwind.onmicrosoft.example": {"dana@example.com": 4, "alex@nordwind.example": 1},
                      "alex.beispiel2@nordwind.example": {"alex@nordwind.example": 8},
                      "alex@example.co.uk": {"alex@nordwind.example": 3, "dana@example.com": 1}}
    names = {a: "Alex Beispiel" for a in stats}
    names["dana@example.com"] = "Dana Dienstleister"
    mine = {"alex@nordwind.example", "alex@nordwind.onmicrosoft.example"}
    assert correspondents.own_addresses(stats, names) == mine
    assert correspondents.own_addresses(stats, names, account="alex@nordwind.example") == mine
    assert correspondents.own_addresses(stats, names, own_name="alex beispiel") == set(stats) - {"dana@example.com"}
    # What the user names as their own is theirs, whatever the mail looks like –
    # the private address one writes to and from – and nothing else changes.
    assert correspondents.own_addresses(stats, names, mine={"alex@example.co.uk"}) == mine | {"alex@example.co.uk"}
    assert correspondents.own_addresses({}, {}, mine={"alex@home.example"}) == {"alex@home.example"}


def test_the_mailbox_knows_who_wrote_to_whom(con):
    stats, _names = correspondents.profile(con)
    assert stats.wrote_to and all(sender in stats for sender in stats.wrote_to)
    assert any(_own(con) & set(to) for to in stats.wrote_to.values())
