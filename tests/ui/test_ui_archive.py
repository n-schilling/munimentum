"""The interface on an archive that holds something.

The other browser tests run against an empty folder and check that every
door opens. These run against the synthetic archive (testdata/) and check
what a user does with one: search across the sources, open a hit, walk the
calendar, the contacts and the files, read the key figures, and put a hit
into a case.

Expected numbers are never written down twice – they are derived from the
module that generated the archive, so adding a mail to testdata/sources.py
does not make a test here wrong.
"""

import re
from urllib.parse import quote

import pytest

from playwright.sync_api import expect

from testdata import history, people, sources
from tests.ui.helpers import hit_title, hits, open_app, open_calendar, texts

pytestmark = pytest.mark.ui

PROJECT = people.PROJECT
EN = texts("en")


def figure(n):
    """A number the way the page writes it: toLocaleString on an English
    page puts a comma every three digits, and the archive counts in
    thousands since it holds a year of traffic."""
    return f"{n:,}"


def search_for(page, archive, term):
    """Type a search and press the button, as a user would."""
    open_app(page, archive, tab="suche")
    page.fill("#q", term)
    page.locator("#sicht-treffer").get_by_role("button", name=EN["search.go"],
                                               exact=True).click()
    expect(page.locator("#treffer-kopf")).to_be_visible()
    return page.locator("#results .hit")


def book_choice(page, pill, key):
    """Tick or pick one row of an address book pill, as a user does: open
    the pill, click the row, and leave the popover."""
    attr = {"origin": "data-book", "mail": "data-book-flag", "hide": "data-book-hide"}[pill]
    if page.locator(f"#book-po-{pill}").is_hidden():
        page.locator(f"#book-pill-{pill} > .wert").click()
    page.locator(f'#book-po-{pill}-list [{attr}="{key}"]').click()
    page.keyboard.press("Escape")
    expect(page.locator(f"#book-po-{pill}")).to_be_hidden()


def test_a_search_finds_the_project_across_the_sources(archive_page, archive):
    hits = search_for(archive_page, archive, PROJECT)
    expect(hits.first).to_be_visible()
    # The project is talked about in mail, in chats, in the calendar and on
    # a page: a search that finds only one kind would mean the index read
    # only one source.
    kinds = set(archive_page.locator("#results .hit .wer .tag").all_inner_texts())
    assert len(kinds) >= 3, kinds


def test_a_chosen_hit_shows_the_facts_of_its_kind(archive_page, archive):
    hits = search_for(archive_page, archive, "budget approved")
    hits.first.click()
    detail = archive_page.locator("#detail")
    expect(detail).to_be_visible()
    # A mail is known by who wrote it, when, and where it lies.
    expect(detail.locator("#detail-fakten")).to_contain_text(people.COLLEAGUES[1][0])
    expect(detail.locator("#detail-fakten")).to_contain_text("2026")
    expect(detail.get_by_role("button", name=EN["cases.add.one"])).to_be_visible()
    expect(detail.locator("#detail-text")).to_contain_text(PROJECT)


def test_the_calendar_shows_what_the_export_holds(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    open_calendar(archive_page)
    sicht = archive_page.locator("#sicht-kalender")
    expect(sicht).to_be_visible()
    # The line above the grid counts the appointments in the export – every
    # date of a series one, so more than the files.
    shown = int(re.search(r"(\d[\d,.\u202f]*) ", sicht.locator("#kalStats").inner_text()).group(1).replace(",", "").replace(".", "").replace("\u202f", ""))
    assert shown > len(sources.EVENTS)
    grid = archive_page.locator("#kalBox")
    expect(grid).to_be_visible()
    # The week opens on today, and the weekday lunch – a series without an
    # end – has a date in every week of the year.
    expect(grid).to_contain_text("Team lunch")


def test_the_contacts_hold_everyone_the_archive_knows(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    archive_page.click('#sichten [data-sicht="adressbuch"]')
    liste = archive_page.locator("#kbBox")
    expect(liste).to_be_visible()
    for name, _mail in [*people.COLLEAGUES, *people.EXTERNALS]:
        expect(liste).to_contain_text(name)


def test_the_file_browser_lists_every_mirror_and_walks_into_one(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    archive_page.click('#sichten [data-sicht="dateien"]')
    liste = archive_page.locator("#dateien-liste")
    expect(liste).to_be_visible()
    for name in ("OneDrive", "SharePoint", "Teams", "Planner"):
        expect(liste).to_contain_text(name)
    liste.get_by_text("OneDrive", exact=False).first.click()
    # One level in: the mirror's own top folder, and no file lost on the way.
    expect(liste).to_contain_text("Dateien")
    liste.get_by_text("Dateien", exact=False).first.click()
    expect(liste).to_contain_text("Documents")


def test_the_key_figures_count_what_was_written(archive_page, archive):
    open_app(archive_page, archive, tab="analytics")
    karte = archive_page.locator("#ana-zahlen-karte")
    expect(karte).to_be_visible()
    messages = len(sources.MAILS) + sum(len(c["messages"]) for c in sources.CONVERSATIONS)
    expect(karte).to_contain_text(figure(messages))
    expect(karte).to_contain_text(figure(len(sources.PAGES)))
    expect(karte).to_contain_text(figure(len(sources.ONENOTE_PAGES)))
    expect(karte).to_contain_text("2026")


def test_a_hit_goes_into_a_new_case(archive_page, archive):
    name = f"{PROJECT} rollout"
    hits = search_for(archive_page, archive, f"{PROJECT} rollout plan")
    titel = hit_title(hits.first)
    hits.first.click()
    archive_page.locator("#detail").get_by_role(
        "button", name=EN["cases.add.one"]).click()
    dialog = archive_page.locator("#modal")
    expect(dialog).to_be_visible()
    # The dialog can make the case on the spot – type a name, add.
    dialog.locator("#fall-wahl-neu").fill(name)
    dialog.get_by_role("button", name=EN["cases.add.do"], exact=True).click()
    expect(dialog).to_be_hidden()
    # The third door now has the case, and it holds that one item.
    archive_page.click('nav [data-tab="faelle"]')
    leiste = archive_page.locator("#faelle-leiste")
    expect(leiste).to_contain_text(name)
    leiste.get_by_text(name, exact=False).first.click()
    expect(archive_page.locator("#fall-kopf")).to_contain_text(name)
    # The item is in there by its own title, filed under its source.
    expect(archive_page.locator("#fall-inhalt")).to_contain_text(titel)


def test_the_contacts_show_the_picture_over_the_archive(archive_page, archive):
    """The address book as the case's picture (13.6): everyone the
    communication names, the origin chips absent, the card with what the
    address book knows, the way over into the timeline of the search."""
    open_app(archive_page, archive, tab="suche")
    archive_page.click('#sichten [data-sicht="adressbuch"]')
    expect(archive_page.locator("#kbBox")).to_contain_text(people.COLLEAGUES[0][0])
    with archive_page.expect_response(lambda r: "/api/v1/search/people" in r.url):
        archive_page.locator('#sicht-adressbuch [data-book-mode="picture"]').click()
    picture = archive_page.locator("#kbBox .bild")
    expect(picture).to_be_visible()
    for name, _mail in [*people.COLLEAGUES, *people.EXTERNALS]:
        expect(picture).to_contain_text(name)
    # Me: the circle at the left, no node; the address book's own
    # entries are no nodes either – a card names a company, not a person.
    expect(picture.locator(".knoten")).to_have_count(len(people.COLLEAGUES) + len(people.EXTERNALS))
    expect(picture.locator(".ich")).to_contain_text(people.ME[0])
    expect(archive_page.locator("#book-pill-origin")).to_be_hidden()
    # The card: address, and company, role and number from the address book.
    dana = people.EXTERNALS[0]
    contact = [c for c in sources.CONTACTS if c["displayName"] == dana[0]][0]
    picture.locator(".knoten", has_text=dana[0]).click()
    card = archive_page.locator("#kbBox .person-karte")
    expect(card).to_contain_text(dana[1])
    expect(card).to_contain_text(contact["companyName"])
    expect(card).to_contain_text(contact["businessPhones"][0])
    # The way over: the timeline of the person in Search, the pill set.
    with archive_page.expect_response(lambda r: "/api/v1/search/timeline" in r.url):
        card.get_by_role("button", name=EN["people.timeline"]).click()
    expect(archive_page.locator("#sicht-treffer")).to_be_visible()
    expect(archive_page.locator('#result-views [data-result-view="timeline"]')).to_have_class("sicht on")
    expect(archive_page.locator("#pw-person")).to_contain_text(dana[0])
    expect(archive_page.locator("#results .zeit")).not_to_have_count(0)
    # Back in the book, the list and its chips are back.
    archive_page.click('#sichten [data-sicht="adressbuch"]')
    archive_page.locator('#sicht-adressbuch [data-book-mode="list"]').click()
    expect(archive_page.locator("#book-pill-origin")).to_be_visible()
    expect(archive_page.locator("#kbBox .card2").first).to_be_visible()


def test_the_contacts_say_what_the_mail_says_about_a_person(archive_page, archive):
    """On a card: mails received and sent, the frequent contact's mark,
    the address book's fields one by one, and the footer under the
    person's mails in a fold. Above the list: three switches that narrow it
    and combine with the origin."""
    me = people.ME[1]

    def counts(address):
        received = sum(1 for m in sources.MAILS if m["frm"][1] == address)
        sent = sum(1 for m in sources.MAILS if m["frm"][1] == me and address in
                   {p[1] for line in ("to", "cc", "bcc") for p in m.get(line) or []})
        return received, sent

    open_app(archive_page, archive, tab="suche")
    with archive_page.expect_response(lambda r: r.url.endswith("/api/v1/correspondents")):
        archive_page.click('#sichten [data-sicht="adressbuch"]')
    cards = archive_page.locator("#kbBox .card2")
    dana, greta, lars = people.EXTERNALS[0], people.EXTERNALS[2], people.EXTERNALS[5]
    card = cards.filter(has_text=dana[0])
    expect(card.locator(".tag.frequent")).to_have_text(EN["book.tag.frequent"])
    received, sent = counts(dana[1])
    # The marks close the card; what the archive says of Dana is a window of its own.
    expect(card.locator(".ctags .tag")).to_have_text([EN["book.tag.frequent"], EN["book.tag.footer"]])
    expect(card.locator("details.book-sig")).to_have_count(0)
    window = archive_page.locator("#modal")
    card.locator(".card-menu summary").click()
    card.get_by_role("button", name=EN["book.info"]).click()
    expect(window.locator(".modal-kopf")).to_contain_text(dana[0])
    facts = window.locator("dl.fakten")
    expect(facts.locator("dt", has_text=EN["book.info.received"]).locator("+ dd")).to_have_text(f"{received:,}")
    expect(facts.locator("dt", has_text=EN["book.info.sent"]).locator("+ dd")).to_have_text(f"{sent:,}")
    window.locator(".modal-zu").click()
    expect(window).to_be_hidden()
    # Outlook changed her card once: both versions are listed, and what changed stands below.
    card.locator(".card-menu summary").click()
    with archive_page.expect_response(lambda r: "/api/v1/documents/versions/diff" in r.url):
        card.get_by_role("button", name=EN["book.versions"]).click()
    expect(window.locator(".book-versions-list .vzeile")).to_have_count(2)
    # … field by field, as a reader knows a contact – not as a vCard's lines; the file is a download.
    view = window.locator(".book-versions-view")
    expect(view.locator("dl.fakten dt", has_text=EN["book.own.jobtitle"])).to_be_visible()
    expect(view).not_to_contain_text("BEGIN:VCARD")
    expect(view.get_by_role("link", name=EN["book.card.download"])).to_have_attribute("download", "")
    expect(window.locator(".book-versions-view del")).to_have_text(history.CONTACT_TITLE_BEFORE)
    expect(window.locator(".book-versions-view ins")).to_contain_text(
        next(c for c in sources.CONTACTS if c["displayName"] == dana[0])["jobTitle"])
    window.locator(".modal-zu").click()
    expect(window).to_be_hidden()
    contact = [c for c in sources.CONTACTS if c["displayName"] == dana[0]][0]
    expect(card.locator(".crole").nth(0)).to_have_text(contact["jobTitle"])
    expect(card.locator(".crole").nth(1)).to_have_text(contact["companyName"])
    expect(card.locator(".cline", has_text=contact["mobilePhone"])).to_be_visible()
    everyone = cards.count()

    # Frequent contacts only: every card left carries the mark.
    # Three pills: where an entry comes from, what the mail says, what is left out.
    expect(archive_page.locator("#sicht-adressbuch .calbar .pill:visible")).to_have_count(3)
    expect(archive_page.locator("#book-po-mail-list button")).to_have_count(4)
    # Greta's footer names a number her card lacks: the card says so, and the Mail pill lists such cards alone.
    expect(cards.filter(has_text=greta[0]).locator(".tag.update")).to_have_text(EN["book.tag.update"])
    expect(cards.filter(has_text=dana[0]).locator(".tag.update")).to_have_count(0)
    book_choice(archive_page, "mail", "update")
    expect(cards.filter(has_text=greta[0])).to_be_visible()
    expect(cards.filter(has_text=dana[0])).to_have_count(0)
    expect(archive_page.locator("#kbBox .card2 .tag.update")).to_have_count(cards.count())
    book_choice(archive_page, "mail", "update")
    expect(archive_page.locator("#book-pill-mail .wert")).to_have_text(EN["book.pill.mail"])
    book_choice(archive_page, "mail", "frequent")
    expect(cards.filter(has_text=dana[0])).to_be_visible()
    frequent = cards.count()
    assert 0 < frequent < everyone
    expect(archive_page.locator("#kbBox .card2 .tag.frequent")).to_have_count(frequent)
    # … of them, those the address book lacks.
    book_choice(archive_page, "mail", "missing")
    expect(cards.filter(has_text=lars[0])).to_be_visible()
    expect(cards.filter(has_text=dana[0])).to_have_count(0)
    assert 0 < cards.count() < frequent
    book_choice(archive_page, "mail", "frequent")
    book_choice(archive_page, "mail", "missing")
    book_choice(archive_page, "mail", "exchange")
    assert frequent <= cards.count() < everyone
    book_choice(archive_page, "mail", "exchange")
    expect(cards).to_have_count(everyone)

    # The footer: Greta's card has no number, the fold under it has.
    card = cards.filter(has_text=greta[0])
    expect(card.locator(".cline a[href^='tel:']")).to_have_count(0)
    card.locator(".card-menu summary").click()
    with archive_page.expect_response(lambda r: "/api/v1/correspondents/signature" in r.url):
        card.get_by_role("button", name=EN["book.info"]).click()
    fold = window.locator(".book-sig")
    expect(fold).to_contain_text("+49 170 0000042")
    expect(fold).to_contain_text("Trainer")
    # What the footer names and her card lacks is said apart: the number.
    expect(window.locator(".info-head", has_text=EN["book.sig.missing"])).to_be_visible()
    expect(window.locator(".sig-missing .cline")).to_have_text(["+49 170 0000042"])
    # … and the mail it was read from is one click away.
    with archive_page.expect_response(lambda r: "/api/v1/documents" in r.url):
        fold.get_by_role("button").click()
    expect(archive_page.locator("#sicht-treffer")).to_be_visible()
    expect(archive_page.locator("#detail")).to_contain_text(greta[0])

    # In the picture the switches are absent, like the origin chips.
    archive_page.click('nav [data-tab="suche"]')         # the door, back from the one item
    archive_page.click('#sichten [data-sicht="adressbuch"]')
    archive_page.locator('#sicht-adressbuch [data-book-mode="picture"]').click()
    expect(archive_page.locator("#book-pill-mail")).to_be_hidden()
    expect(archive_page.locator("#kbHidden")).to_be_hidden()


def test_a_person_from_the_mail_is_added_to_the_address_book(archive_page, archive):
    """The keeper's own card stands marked among the contacts; a person
    the mail knows and no card names gets one with a click, begun from
    the footer under their mails, filled in by hand – and can go again."""
    open_app(archive_page, archive, tab="suche")
    with archive_page.expect_response(lambda r: r.url.endswith("/api/v1/correspondents")):
        archive_page.click('#sichten [data-sicht="adressbuch"]')
    cards = archive_page.locator("#kbBox .card2")
    kept = history.OWN_CONTACT
    card = cards.filter(has_text=kept["person"][0])
    expect(card.locator(".tag.added")).to_have_text(EN["book.tag.own"])
    expect(card.locator(".crole").nth(0)).to_have_text(kept["title"])
    expect(card.locator(".crole").nth(1)).to_have_text(kept["company"])
    expect(card.locator(".cline", has_text=kept["mobile"])).to_be_visible()
    expect(card).to_contain_text(kept["person"][1])
    # The origin "Added here" shows the app's own cards alone – the pill says so; the keeper's is no longer missing.
    book_choice(archive_page, "origin", "own")
    expect(archive_page.locator("#book-pill-origin .wert")).to_have_text(EN["book.f.own"])
    expect(cards).to_have_count(1)
    book_choice(archive_page, "origin", "all")
    book_choice(archive_page, "mail", "missing")
    expect(cards.filter(has_text=kept["person"][0])).to_have_count(0)

    lars = people.EXTERNALS[5]

    def forget_lars():
        for kept_card in archive.get("/api/v1/contacts/own")["items"]:
            if kept_card["address"] == lars[1]:
                archive.send("DELETE", f"/api/v1/contacts/own/{kept_card['id']}", {})
        archive.send("DELETE", "/api/v1/contacts/ignored?address=" + lars[1].replace("@", "%40"), {})

    # Whatever happens below, the archive is what the other tests expect.
    forget_lars()
    try:
        # Somebody the address book lacks can be set to be ignored: he leaves that list …
        card = cards.filter(has_text=lars[0])
        # The index run read his footer: the card says so, the window says when.
        expect(card.locator(".ctags .tag", has_text=EN["book.tag.footer"])).to_be_visible()
        card.locator(".card-menu summary").click()
        card.get_by_role("button", name=EN["book.info"]).click()
        expect(archive_page.locator("#modal .signote")).to_contain_text(EN["book.sig.read"].split(". ")[-1])
        archive_page.locator("#modal .modal-zu").click()
        card.locator(".card-menu summary").click()
        with archive_page.expect_response(lambda r: r.url.endswith("/api/v1/contacts/ignored")):
            card.get_by_role("button", name=EN["book.ignore"], exact=True).click()
        expect(cards.filter(has_text=lars[0])).to_have_count(0)
        # … and every other, until "Ignored" is no longer left out: there he stands, marked, and is taken back.
        book_choice(archive_page, "mail", "missing")
        expect(cards.filter(has_text=lars[0])).to_have_count(0)
        expect(archive_page.locator("#book-pill-hide .wert")).to_have_text(EN["book.pill.hide.n"].replace("{n}", "2"))
        book_choice(archive_page, "hide", "ignored")
        card = cards.filter(has_text=lars[0])
        expect(card.locator(".ctags .tag", has_text=EN["book.tag.ignored"])).to_be_visible()
        card.locator(".card-menu summary").click()
        with archive_page.expect_response(lambda r: "/api/v1/contacts/ignored?address=" in r.url):
            card.get_by_role("button", name=EN["book.ignore.stop"]).click()
        expect(card.locator(".ctags .tag", has_text=EN["book.tag.ignored"])).to_have_count(0)
        # Nobody is ignored any more: the row for them is gone from the pill.
        expect(archive_page.locator('#book-po-hide-list [data-book-hide="ignored"]')).to_have_count(0)
        book_choice(archive_page, "mail", "missing")
        # Lars writes often and has no card: one click makes him one.
        card = cards.filter(has_text=lars[0])
        with archive_page.expect_response(lambda r: r.url.endswith("/api/v1/contacts/own") and r.status == 201):
            card.locator(".card-menu summary").click()
            card.get_by_role("button", name=EN["book.add"]).click()
        dialog = archive_page.locator("#modal")
        expect(dialog).to_contain_text(EN["book.own.title"])
        expect(dialog.locator("#own-name")).to_have_value(lars[0])
        # The numbers of his footer are in it already – nobody typed them.
        numbers = [dialog.locator(f"#own-phone-number-{i}").input_value() for i in range(2)]
        assert all(n.startswith("+49") for n in numbers), numbers
        dialog.locator("#own-title").fill("Dispatcher")
        with archive_page.expect_response(lambda r: "/api/v1/contacts/own/" in r.url and r.request.method == "PATCH"):
            dialog.get_by_role("button", name=EN["book.own.save"]).click()
        expect(dialog).to_be_hidden()
        # With a card he is no longer missing: all contacts again.
        expect(cards.filter(has_text=lars[0])).to_have_count(0)
        book_choice(archive_page, "mail", "missing")
        card = cards.filter(has_text=lars[0])
        expect(card.locator(".tag.added")).to_be_visible()
        expect(card.locator(".crole").first).to_have_text("Dispatcher")
        expect(card.locator(".cline", has_text=numbers[0])).to_be_visible()

        # … and out again, so the archive is what the other tests expect.
        # What the change replaced is kept: the title was empty before.
        card.locator(".card-menu summary").click()
        card.get_by_role("button", name=EN["book.versions"]).click()
        expect(dialog.locator(".modal-kopf")).to_contain_text(lars[0])
        expect(dialog.locator("#own-history dt", has_text=EN["book.own.jobtitle"]).first.locator("+ dd")).to_have_text(
            EN["book.own.versions.empty"])
        dialog.locator(".modal-zu").click()
        card.locator(".card-menu summary").click()
        card.get_by_role("button", name=EN["book.own.edit"]).click()
        archive_page.once("dialog", lambda d: d.accept())
        with archive_page.expect_response(lambda r: "/api/v1/contacts/own/" in r.url and r.request.method == "DELETE"):
            dialog.get_by_role("button", name=EN["book.own.remove"]).click()
        expect(cards.filter(has_text=lars[0]).locator(".tag.added")).to_have_count(0)
        cards.filter(has_text=lars[0]).locator(".card-menu summary").click()
        expect(cards.filter(has_text=lars[0]).get_by_role("button", name=EN["book.add"])).to_be_visible()
    finally:
        forget_lars()


def test_a_citation_link_opens_the_item_and_nothing_else(archive_page, archive):
    """The link a citation over MCP carries (#item=<key>) opens the item –
    and only the item: nobody searched, so no search row, no filters, no
    list. The address is cleared, so a reload does not open it again."""
    title = next(p[2] for p in sources.ONENOTE_PAGES if p[0] == "page-steering")
    key = next(h["key"] for h in hits(archive, title, 20) if h["title"] == title)
    archive_page.goto(archive.base + "/#item=" + quote(key, safe=""))
    detail = archive_page.locator("#detail")
    expect(detail.locator(".dtitel")).to_be_visible()
    expect(detail.locator(".dtitel")).to_have_text(title)
    assert "#item" not in archive_page.url
    door = ("#sichten", "#q", "#filter", "#results")
    for part in door:
        expect(archive_page.locator(part)).to_be_hidden()
    # "Explore archive" brings the door back as it was – nothing searched.
    archive_page.click('nav [data-tab="suche"]')
    for part in ("#sichten", "#q", "#filter"):
        expect(archive_page.locator(part)).to_be_visible()
    expect(detail).to_be_hidden()
    expect(archive_page.locator("#q")).to_have_value("")


def test_a_hit_chosen_before_comes_back_after_a_link(archive_page, archive):
    hits_ = search_for(archive_page, archive, "budget approved")
    hits_.first.click()
    before = archive_page.locator("#detail .dtitel").inner_text()
    title = next(p[2] for p in sources.ONENOTE_PAGES if p[0] == "page-steering")
    key = next(h["key"] for h in hits(archive, title, 20) if h["title"] == title)
    archive_page.evaluate("k => { location.hash = 'item=' + encodeURIComponent(k); }", key)
    expect(archive_page.locator("#detail .dtitel")).to_be_visible()
    expect(archive_page.locator("#detail .dtitel")).to_have_text(title)
    expect(archive_page.locator("#results")).to_be_hidden()
    archive_page.click('nav [data-tab="suche"]')
    expect(archive_page.locator("#results")).to_be_visible()
    expect(archive_page.locator("#detail .dtitel")).to_have_text(before)


def test_list_on_an_open_hit_steps_the_list_aside_and_back(archive_page, archive):
    hits_ = search_for(archive_page, archive, "budget approved")
    hits_.first.click()
    detail, results = archive_page.locator("#detail"), archive_page.locator("#results")
    expect(detail).to_be_visible()
    title = detail.locator(".dtitel").inner_text()
    liste = archive_page.locator("#result-views").get_by_role("button", name=EN["search.view.list"])
    liste.click()
    expect(results).to_be_hidden()
    expect(detail.locator(".dtitel")).to_have_text(title)
    expect(archive_page.locator("#filter")).to_be_visible()      # set by hand: they stay
    liste.click()
    expect(results).to_be_visible()
    expect(detail.locator(".dtitel")).to_have_text(title)


def test_a_fact_of_an_item_opened_alone_searches_with_the_whole_door(archive_page, archive):
    """A fact of the detail is a filter: clicked on an item a link opened,
    the door comes back with the pill set and the result drawn."""
    key = next(h["key"] for h in hits(archive, "rollout plan", 20)
               if h["uid"].endswith("rollout-plan.md:0"))
    archive_page.goto(archive.base + "/#item=" + quote(key, safe=""))
    detail = archive_page.locator("#detail")
    expect(detail.locator(".dtitel")).to_be_visible()
    with archive_page.expect_response(lambda r: "/api/v1/search?" in r.url):
        detail.locator("#detail-fakten a", has_text="md").first.click()
    expect(archive_page.locator("#filter")).to_be_visible()
    expect(archive_page.locator("#p-typ")).to_have_class(re.compile(r"\bon\b"))
    expect(archive_page.locator("#results .hit").first).to_be_visible()


def test_the_organization_overview_opens_on_demand_and_leads_to_the_changes(archive_page, archive):
    """Closed at first – the view opens on the chosen version. Opened, it
    draws people and changes for the archive's fifteen weeks of versions,
    one point a day (history.org_timeline, a few days with two runs); a
    click keeps a point's card, and a number in it opens exactly those
    changes."""
    page, en = archive_page, texts("en")
    open_app(page, archive, tab="suche")
    page.click('#sichten [data-sicht="org"]')
    fold = page.locator("#org-overview")
    expect(fold).to_be_visible()
    expect(fold).not_to_have_attribute("open", "")
    with page.expect_response(lambda r: "/organization/changes/series" in r.url):
        fold.locator("summary").click()
    chart = page.locator("#org-chart")
    days = len({when.date() for when, _text in history.org_timeline()}) + 1   # + today's
    expect(chart.locator("circle.org-ser-people")).to_have_count(days)
    expect(chart.locator("circle.org-ser-left")).to_have_count(days - 1)    # the first compares nothing
    expect(page.locator("#org-overview-sum")).to_contain_text(str(history.ORG_TIMELINE_LEN + 1))
    chart.get_by_role("button", name=en["org.change.left"]).click()
    expect(chart.locator("circle.org-ser-people")).to_have_count(days)
    expect(chart.locator("circle.org-ser-left")).to_have_count(0)
    chart.get_by_role("button", name=en["org.change.left"]).click()
    # Keep today's card – the last point –, then click its "left" number.
    box = chart.locator("#org-svg").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.985, box["y"] + box["height"] * 0.6)
    tip = chart.locator(".org-tip")
    expect(tip).to_contain_text(en["org.chart.show"])
    tip.locator("button.org-ser-left").click()
    changes = page.locator("#org-changes-fold")
    expect(changes).to_have_attribute("open", "")
    expect(changes).to_contain_text(en["org.changes.all"])
