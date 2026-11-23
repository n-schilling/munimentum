"""signature.py – the footer under a person's mails.

The rule under test: a signature is what repeats at the end of someone's
mails. Everything here is written as mails are – a greeting, a few words,
a closing line, the footer – in the shapes mail clients produce: plain
text, HTML with a table, an answer above its quoted history."""

from email.message import EmailMessage

import time

import corpus
import correspondents
import signature

NAME = "Dana Dienstleister"
FOOTER = [NAME, "Project manager",
          "Beispiel GmbH · Beispielweg 1 · Hamburg",
          "Tel. +49 40 000000 77 · Mobile +49 170 0000077",
          "dana.dienstleister@example.com · www.example.com"]


def _mail(body, html=None):
    msg = EmailMessage()
    msg["From"] = f"{NAME} <dana.dienstleister@example.com>"
    msg["Subject"] = "x"
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg


def _write(tmp_path, name, body, html=None):
    path = tmp_path / name
    path.write_bytes(_mail(body, html).as_bytes())
    return path


def test_the_lines_more_than_one_mail_ends_with_are_the_footer():
    first = ["Hello Alice,", "the plan is attached.", "Kind regards", *FOOTER]
    second = ["Hi Alice,", "Thursday works.", "Call me on 0170 0000001 before.",
              "Best regards from Hamburg,", *FOOTER]
    lines, basis, at = signature.footer([first, second], NAME)
    assert (lines, basis, at) == (FOOTER, "repeated", 0)


def test_a_postscript_below_the_footer_does_not_hide_it():
    first = ["Thursday works.", "Kind regards", *FOOTER, "PS: I am out on Friday."]
    second = ["Fine.", "Regards", *FOOTER]
    assert signature.footer([first, second], NAME)[0] == FOOTER


def test_the_words_above_the_closing_line_are_no_footer_even_when_they_repeat():
    """Two mails that say the same thing: what they share above "Kind
    regards" is the message, not the signature."""
    mail = ["Hello Alice,", "thank you.", "Kind regards", *FOOTER]
    assert signature.footer([mail, list(mail)], NAME)[0] == FOOTER


def test_the_full_footer_wins_over_the_short_one_under_answers():
    short = ["Fine.", "Regards", NAME, "Beispiel GmbH"]
    full = ["The plan is attached.", "Kind regards", *FOOTER]
    lines, basis, at = signature.footer([short, list(short), full, list(full)], NAME)
    assert (lines, basis, at) == (FOOTER, "repeated", 2)


def test_a_company_that_begins_like_a_closing_line_stays_in_the_footer():
    for company in ("LG Beispiel Deutschland GmbH", "Best Nordwind Hotel Hamburg", "Best Nordwind",
                    "Dank & Söhne GmbH"):
        footer = ["Bob Baumeister", "Head of Sales", company, "Tel. +49 40 000000 77"]
        mails = [["The plan is attached.", "Kind regards", *footer], ["Thursday works.", "Regards,", *footer]]
        assert signature.footer(mails, "Bob Baumeister")[0] == footer, company
        assert signature.footer(mails[:1], "Bob Baumeister")[0] == footer, company
    # Alone, with the sender's name or a word to the reader they close a mail.
    for closing in ("LG", "VG Bob", "Danke dir!", "Thanks,", "Best", "Thank you"):
        mail = ["Fine with me.", closing, "Bob Baumeister", "Tel. +49 40 000000 77"]
        assert signature.footer([mail], "Bob Baumeister")[0] == mail[2:], closing


def test_a_mail_repeated_whole_shows_no_footer():
    """Two automatic replies, each a mail of its own: what they repeat is
    their text."""
    reply = ["I am out of the office until Monday.", "Erik Einkauf will help you in urgent cases."]
    assert signature.footer([reply, list(reply)], NAME) == ([], "", None)
    # Beside a mail with the footer, the footer is still found – and with
    # a closing line a repeated mail says itself where its words end.
    other = ["The plan is attached.", "Kind regards", *FOOTER]
    assert signature.footer([reply, list(reply), other, ["Fine.", "Regards", *FOOTER]], NAME)[0] == FOOTER


def test_a_single_mail_is_read_by_its_closing_line():
    mail = ["Hello Alice,", "the plan is attached.", "Mit freundlichen Grüßen", *FOOTER]
    assert signature.footer([mail], NAME) == (FOOTER, "single", 0)


def test_a_single_mail_without_a_closing_line_is_read_from_the_name():
    mail = ["Hello Alice,", "the plan is attached.", *FOOTER]
    assert signature.footer([mail], NAME) == (FOOTER, "single", 0)
    assert signature.footer([["Short note.", "-- ", "Beispiel GmbH", "Tel. +49 40 000000 77"]], "")[0] \
        == ["Beispiel GmbH", "Tel. +49 40 000000 77"]


def test_a_name_alone_is_no_footer():
    """It says nothing the From line did not."""
    assert signature.footer([["Fine.", "Kind regards", NAME]], NAME) == ([], "", None)
    assert signature.footer([["Fine.", "Regards", NAME], ["Yes.", "Regards", NAME]], NAME) \
        == ([], "", None)
    assert signature.footer([["Just a sentence without anything below it."]], NAME) == ([], "", None)


def test_the_small_print_is_left_out():
    legal = "This e-mail is confidential and intended for the addressee only."
    mail = ["Fine.", "Kind regards", *FOOTER, legal, "It may contain privileged material."]
    assert signature.footer([mail, list(mail)], NAME)[0] == FOOTER
    banner = "x" * (signature.MAX_LINE + 1)
    assert signature.footer([["Fine.", "Regards", *FOOTER, banner]], NAME)[0] == FOOTER


def test_numbers_carry_the_kind_their_label_names():
    found = signature.fields([
        "T +49 30 000000-0 F +49 30 000000-99",
        "M: (0170) 0000012",
        "Phone (direct): +1 (555) 010-0199",
        "Mobil: +49 170 0000077 | Fax: +49 40 000000 78",
        "0049 30 000000 12"])
    assert found["phones"] == [
        {"type": "phone", "number": "+49 30 000000-0"},
        {"type": "fax", "number": "+49 30 000000-99"},
        {"type": "mobile", "number": "(0170) 0000012"},
        {"type": "phone", "number": "+1 (555) 010-0199"},
        {"type": "mobile", "number": "+49 170 0000077"},
        {"type": "fax", "number": "+49 40 000000 78"},
        {"type": "phone", "number": "0049 30 000000 12"}]


def test_numbers_that_are_not_for_calling_are_no_phone_numbers():
    found = signature.fields([
        "Amtsgericht Hamburg HRB 0000000",
        "USt-IdNr. DE000000000 · IBAN DE00 0000 0000 0000 0000 00",
        "20095 Hamburg, 01.02.2026",
        "https://www.example.com/id/004917000000",
        "Open 08:00-17:00"])
    assert found["phones"] == []


def test_a_job_title_beside_the_number_does_not_hide_it():
    """Words of the bank and the register are also words of job titles:
    only what stands right before a number says it is not for calling."""
    found = signature.fields(["Dana Dienstleister | Key Account Manager | T +49 40 000000 77",
                              "Tax Advisor Tel. +49 40 000000 78",
                              "HRB 0123456 · Tel +49 40 000000 79 · Account no. 0123456789",
                              "Steuernummer 040 123 45678"])
    assert [p["number"] for p in found["phones"]] == [
        "+49 40 000000 77", "+49 40 000000 78", "+49 40 000000 79"]


def test_behind_a_label_a_number_may_start_with_any_digit():
    found = signature.fields(["Phone: (555) 010-0001", "Tel: 555-010-0002", "Cell 555 010 0003",
                              "T: 555 010 0004", "D 20095 Hamburg, Beispielweg 1234567",
                              "Order 555-010-0005 of 2026"])
    assert found["phones"] == [{"type": "phone", "number": "(555) 010-0001"},
                               {"type": "phone", "number": "555-010-0002"},
                               {"type": "mobile", "number": "555 010 0003"},
                               {"type": "phone", "number": "555 010 0004"}]


def test_hours_and_numbers_of_orders_are_no_phone_numbers():
    found = signature.fields(["Mo-Fr 08.00-17.00 Uhr", "Kundennummer: 0012345678", "Angebot 0815-4711-22",
                              "Office 2019 3654321", "ab 0800 1234567 Uhr", "Order 0044 123 4567",
                              "Office +49 40 000000 77", "Tel.-Nr.: 040 000000 78", "Büro: 040 000000 79"])
    assert [p["number"] for p in found["phones"]] == [
        "+49 40 000000 77", "040 000000 78", "040 000000 79"]


def test_the_invisible_characters_are_written_as_escapes():
    """Zero-width characters typed into the source survive no formatter."""
    from pathlib import Path
    source = Path(signature.__file__).read_text(encoding="utf-8")
    assert not signature._INVISIBLE.search(source)
    assert signature._INVISIBLE.sub("", "Da\u200bna\ufeff") == "Dana"


def test_dates_are_no_phone_numbers():
    found = signature.fields(["Abwesend 01.08.2026 – 15.08.2026", "Out of office 01.08.26",
                              "Tel. 040 000000 77"])
    assert found["phones"] == [{"type": "phone", "number": "040 000000 77"}]


def test_a_number_is_named_once_and_addresses_and_links_are_taken_out():
    found = signature.fields([
        "Tel. +49 40 000000 77", "Phone: +49 40 000000-77",
        "Dana.Dienstleister@example.com | www.example.com.",
        "Web: https://example.com/team"])
    assert found == {"phones": [{"type": "phone", "number": "+49 40 000000 77"}],
                     "emails": ["dana.dienstleister@example.com"],
                     "urls": ["www.example.com", "https://example.com/team"]}


def test_a_pictures_placeholder_does_not_cut_the_footer():
    """Outlook's plain part writes an inline logo as [cid:…], with an id
    of its own in every mail."""
    def mail(words, ident):
        return signature.own_lines(_mail(
            f"{words}\n\nKind regards\n{NAME}\nProject manager\nTel. +49 40 000000 77\n"
            f"[cid:image001.png@01DA.{ident}]\nBeispiel GmbH\nBeispielweg 1\nwww.example.com\n"))
    lines = signature.footer([mail("Thursday works.", "1234"), mail("The plan is attached.", "5678")], NAME)[0]
    assert lines == [NAME, "Project manager", "Tel. +49 40 000000 77", "Beispiel GmbH", "Beispielweg 1",
                     "www.example.com"]


def test_what_html_quotes_is_not_the_senders():
    """A <blockquote>, and the block a client opens its history with: the
    element is the same in every language."""
    quoted = "<div>Alice Beispiel</div><div>Nordwind AG</div><div>Tel +49 40 000000 11</div>"
    for markup in (
            f"<div>ok.</div><div>Bob</div><div>W dniu 4 maja Alice pisze:</div><blockquote>{quoted}"
            f"<blockquote><div>deeper</div></blockquote></blockquote><div>below</div>",
            f'<div>ok.</div><div>Bob</div><div class="gmail_quote gmail_quote_container">{quoted}</div>',
            f'<div>ok.</div><div>Bob</div><div id="divRplyFwdMsg"><b>Od:</b> Alice</div><div>{quoted}</div>',
            f"<div>ok.</div><div>Bob</div><blockquote>{quoted}"):
        lines = [x.strip() for x in signature.html_lines(markup).split("\n") if x.strip()]
        assert lines[:2] == ["ok.", "Bob"] and "Nordwind AG" not in lines and "deeper" not in lines, markup
    assert "below" in signature.html_lines("<blockquote>x</blockquote><div>below</div>")


def test_the_owners_own_footer_is_nobodys_signature():
    """Quoted under an answer in a shape no cut knows, it still names the
    owner's address – and with that it is not the sender's."""
    quoted = ["Alice Beispiel", "Nordwind AG", "alice@nordwind.example", "Tel +49 40 000000 11"]
    tails = [["ok.", "Bob", "Earlier:", *quoted], ["fine.", "Bob", "Earlier:", *quoted]]
    assert signature.footer(tails, "Bob Baumeister")[0][-4:] == quoted
    assert signature.footer(tails, "Bob Baumeister", foreign={"alice@nordwind.example"}) == ([], "", None)
    # A name first and a date in it: that line opens a quote too.
    for line in ("Alice Beispiel wrote on 4 May 2026:", "Alice Beispiel <alice@nordwind.example> schrieb:",
                 "W dniu 4.05.2026 Alice Beispiel pisze:"):
        own = signature.own_lines(_mail("ok.\nBob\n\n" + line + "\n" + "\n".join(quoted) + "\n"))
        assert own == ["ok.", "Bob"], line
    # … and a sentence is none.
    assert signature.own_lines(_mail("As I wrote, the plan holds:\nsee below.\n"))[-1] == "see below."


def test_a_break_with_attributes_is_a_break():
    """Apple Mail writes <br class="">, Gmail <br clear="all">."""
    lines = signature.html_lines('<div>Kind regards<br class="">Dana Dienstleister'
                                 '<br clear="all">Project manager<br/>Tel 1</div>')
    assert [x for x in lines.split("\n") if x] == [
        "Kind regards", "Dana Dienstleister", "Project manager", "Tel 1"]


def test_a_number_right_behind_the_labels_dot_is_read():
    assert signature.fields(["Tel.+49 40 000000 77", "Fax:+49 40 000000 78"])["phones"] == [
        {"type": "phone", "number": "+49 40 000000 77"}, {"type": "fax", "number": "+49 40 000000 78"}]


def test_html_keeps_its_lines_and_a_table_row_is_one_line():
    markup = ("<html><head><style>p { margin: 0 }</style></head><body>\n"
              "<p>Fine.</p><p>Kind regards<br>Dana\nDienstleister</p>"
              "<table><tr><td><b>Tel</b></td><td>+49&nbsp;40 000000 77</td></tr>"
              "<tr><td>Web</td><td><a href='https://www.example.com'>www.example.com</a></td></tr>"
              "</table></body></html>")
    lines = [" ".join(line.split()) for line in signature.html_lines(markup).split("\n")]
    assert [line for line in lines if line] == [
        "Fine.", "Kind regards", "Dana Dienstleister", "Tel +49 40 000000 77",
        "Web www.example.com"]


def test_a_mail_is_read_from_its_plain_part_without_quote_and_device_line():
    body = "\n".join(["Fine with me.", "", "Kind regards", *FOOTER, "",
                      "Sent from my iPhone", "",
                      "-----Original Message-----", "From: Alice Beispiel",
                      "Tel. +49 30 000000 01"])
    assert signature.own_lines(_mail(body)) == ["Fine with me.", "Kind regards", *FOOTER]


def test_a_line_of_underscores_above_the_company_lines_is_no_quote():
    """Many footers divide name and company with a line of underscores –
    the same line Outlook puts above a quoted mail. Only a quoted mail's
    header block below it makes it the quote's start."""
    rule = "_" * 32
    own = signature.own_lines(_mail(
        f"Thursday works.\n\nKind regards\nDana\n{rule}\nBeispiel GmbH\nTel. +49 40 000000 77\n"))
    assert own[-2:] == ["Beispiel GmbH", "Tel. +49 40 000000 77"]
    for head in ("From: Alice Beispiel <alice@nordwind.example>\nSent: Monday\n",
                 "Von: Alice Beispiel\nGesendet: Montag\n",
                 "\nDe : Alice Beispiel\nEnvoyé : lundi\n"):
        quoted = signature.own_lines(_mail(
            f"Thursday works.\n\nKind regards\nDana\n{rule}\nBeispiel GmbH\n{rule}\n{head}"
            "Subject: Plan\n\nHello Dana,\nKind regards\nAlice\nNordwind AG\n"))
        assert quoted[-1] == "Beispiel GmbH", head


def test_a_quoted_header_in_another_language_ends_the_senders_words():
    """An HTML reply has no line of underscores: the header block of the
    quoted mail is all that says where it begins – and below it stands the
    quoted person's footer, never to be taken for the sender's."""
    quoted = "Alice Beispiel\nNordwind AG\nTel. +49 40 000000 11\n"
    for head in ("De : Alice Beispiel\nEnvoyé : lundi 1 juin 2026\nÀ : Bob\nObjet : Plan\n",
                 "Da: Alice Beispiel\nInviato: lunedì\nA: Bob\nOggetto: Piano\n",
                 "Van: Alice Beispiel\nVerzonden: maandag\nAan: Bob\n",
                 "De: Alice Beispiel\nEnviado: lunes\nPara: Bob\n",
                 "De: Alice Beispiel\nEnviado el: lunes, 1 de junio\nPara: Bob\n",
                 "De: Alice Beispiel\nEnviada em: segunda-feira\nPara: Bob\n",
                 "Le lun. 1 juin 2026 à 10:00, Alice Beispiel <alice@nordwind.example> a écrit :\n",
                 "El lun, 1 jun 2026 a las 10:00, Alice Beispiel escribió:\n",
                 "Op ma 1 jun 2026 om 10:00 schreef Alice Beispiel <alice@nordwind.example>:\n",
                 "Il giorno lun 1 giu 2026 Alice Beispiel ha scritto:\n",
                 "Op 1 jun 2026, om 10:00 heeft Alice Beispiel <alice@nordwind.example> het volgende geschreven:\n",
                 "No dia 1/06/2026, às 10:00, Alice Beispiel escreveu:\n",
                 "1 juni 2026 kl. 10:00 skrev Alice Beispiel <alice@nordwind.example>:\n",
                 "Von: Alice Beispiel\nBetreff: Plan\n"
                 "Datum: 1. Juni 2026\nAn: Bob\n",
                 "De : Alice Beispiel\nObjet : Plan\nDate : 1 juin 2026\nÀ : Bob\n",
                 "From: Alice Beispiel\nTo: Bob\nSubject: Plan\n"):
        mails = [signature.own_lines(_mail(f"{words}\n\nCordialement\nBob\n\n{head}\nBonjour,\n{quoted}"))
                 for words in ("Jeudi me convient.", "Merci pour le plan.")]
        assert mails[0] == ["Jeudi me convient.", "Cordialement", "Bob"], head
        assert signature.footer(mails, "Bob Baumeister")[0] == [], head


def test_an_empty_plain_part_hands_over_to_the_html():
    html = "<p>Fine.</p><p>Kind regards</p>" + "".join(f"<div>{line}</div>" for line in FOOTER)
    assert signature.own_lines(_mail("", html)) == ["Fine.", "Kind regards", *FOOTER]


def test_the_same_mail_in_two_folders_is_read_once(tmp_path):
    """A mail filed twice repeats every line of itself: its words are not
    the signature, the lines another mail shares are."""
    def filed(name, body, ident):
        msg = _mail(body)
        msg["Message-ID"] = ident
        (tmp_path / name).write_bytes(msg.as_bytes())
        return tmp_path / name
    words = "\n".join(f"Sentence {n} of a long report without a closing line." for n in range(30))
    report = words + "\n" + "\n".join(FOOTER)
    twice = [filed("inbox.eml", report, "<report@example.com>"),
             filed("project.eml", report, "<report@example.com>")]
    found = signature.read(twice, NAME)
    assert (found["lines"], found["basis"], found["mails"]) == (FOOTER, "single", 1)
    other = filed("plan.eml", "The plan is attached.\n\nKind regards\n" + "\n".join(FOOTER),
                  "<plan@example.com>")
    found = signature.read([*twice, other], NAME)
    assert (found["lines"], found["basis"], found["mails"]) == (FOOTER, "repeated", 2)


def test_reading_stops_at_the_round_that_shows_a_footer(tmp_path):
    answer = "Fine.\n\nRegards\n" + NAME
    opener = "The plan is attached.\n\nKind regards\n" + "\n".join(FOOTER)
    paths = [_write(tmp_path, f"answer-{i}.eml", answer) for i in range(2)]
    paths += [_write(tmp_path, f"opener-{i}.eml", opener) for i in range(2)]
    paths += [tmp_path / "missing.eml", _write(tmp_path, "later.eml", answer)]
    # The first round holds two answers: nothing. The second adds the two
    # mails with the footer, and the rest is never opened.
    found = signature.read(paths, NAME, step=2)
    assert found["lines"] == FOOTER and found["basis"] == "repeated"
    assert found["mails"] == 4 and found["path"] == paths[2]
    assert found["phones"][1] == {"type": "mobile", "number": "+49 170 0000077"}
    nothing = signature.read(paths[:2], NAME, step=2)
    assert nothing["lines"] == [] and nothing["path"] is None and nothing["mails"] == 2


def test_the_register_lines_of_a_company_are_not_the_persons_footer():
    """Seat, court and board come under every mail of a company and push
    the person's lines out of the twenty a footer may have. Below their
    first line only a way to reach someone stays."""
    block = ["Mit freundlichen Grüßen", "Dana Dienstleister", "Head of sales",
             "Nordwind AG, Beispielweg 1, 20095 Hamburg", "Mobil: +49 170 0000077",
             "Nordwind Aktiengesellschaft", "Sitz der Gesellschaft: Hamburg",
             "Registergericht: Amtsgericht Hamburg HRB-Nr. 000000",
             "Vorsitzender des Aufsichtsrats: Carla Chef", "Vorstand: Erik Einkauf, Vorsitzender",
             "Greta Gast, stellvertretende Vorsitzende",
             "Zentrale: +49 40 000000 0"]
    assert signature._trim(block, "Dana Dienstleister") == [
        "Dana Dienstleister", "Head of sales", "Nordwind AG, Beispielweg 1, 20095 Hamburg",
        "Mobil: +49 170 0000077", "Nordwind Aktiengesellschaft", "Zentrale: +49 40 000000 0"]
    assert signature._trim(["Bob Baumeister", "Nordwind GmbH | Sitz: Hamburg | Registergericht: Hamburg, HRA 000000",
                            "Managing director: Carla Chef", "Registered office: Beispielweg 1"],
                           "Bob Baumeister") == ["Bob Baumeister", "Nordwind GmbH"]
    # A word of the register that is somebody's job is no register line.
    chair = ["Carla Chef", "Vorstandsvorsitzende", "Nordwind AG", "Beispielweg 1, 20095 Hamburg",
             "Tel. +49 40 000000 10", "www.nordwind.example"]
    assert signature._trim(chair, "Carla Chef") == chair
    assert signature._trim(["Carla Chef", "Mitglied des Aufsichtsrats", "HR Business Partner | HRA Team"],
                           "Carla Chef") == ["Carla Chef", "Mitglied des Aufsichtsrats", "HR Business Partner | HRA Team"]
    assert signature._trim(["Bob Baumeister", "Beispiel GmbH, Sitz: Hamburg, Amtsgericht Hamburg HRB 000012"],
                           "Bob Baumeister") == ["Bob Baumeister", "Beispiel GmbH"]


def test_a_footer_begins_at_the_senders_name_and_below_the_salutation():
    """A first name or a note of absence above the full name is not the
    footer; neither is the line a mail opens with, repeated in short
    mails."""
    assert signature._trim(["Dana", "Dana Dienstleister", "Head of sales", "Tel. +49 40 000000 77"],
                           "Dana Dienstleister") == [
        "Dana Dienstleister", "Head of sales", "Tel. +49 40 000000 77"]
    assert signature._trim(["On leave", "3 to 7 March", "Dana Dienstleister", "Head of sales"],
                           "Dana Dienstleister") == ["Dana Dienstleister", "Head of sales"]
    # A line that reaches someone stays where it is.
    assert signature._trim(["Tel. +49 40 000000 77", "Dana Dienstleister", "Head of sales"],
                           "Dana Dienstleister")[0] == "Tel. +49 40 000000 77"
    assert signature._trim(["Nordwind note", "Hi Bob,"], "Dana Dienstleister") == []
    assert signature._trim(["Hallo Bob,", "Dana Dienstleister", "Head of sales"], "") == [
        "Dana Dienstleister", "Head of sales"]


def test_a_gateway_banner_and_a_linked_picture_are_no_lines(tmp_path):
    body = ("CAUTION: This message originated externally, use caution with links!\n"
            "Hi Bob,\nsee you there.\nCheers\nDana Dienstleister\nHead of sales [cid:image001.png@01DC0000.00000000]\n"
            "[https://www.example.com/a/logo.png]<https://www.example.com/>\n"
            "[EXTERNAL] - also a banner\nTel. +49 40 000000 77\n")
    path = tmp_path / "m.eml"
    path.write_text("From: Dana Dienstleister <dana@partner.example>\nMessage-ID: <b1@example.com>\n"
                    "Content-Type: text/plain; charset=utf-8\n\n" + body, encoding="utf-8")
    assert signature._read(path)[1] == ["Hi Bob,", "see you there.", "Cheers", "Dana Dienstleister",
                                        "Head of sales", "Tel. +49 40 000000 77"]


def test_a_notification_is_no_footer():
    """A block with a mail header's line is generated or quoted; one mail
    alone that says the owner's full name is a notification about them."""
    tails = [["Subject: Alice Beispiel", "Click here to view the conversation."],
             ["Subject: Alice Beispiel", "Click here to view the conversation."],
             ["other", "Subject: Alice Beispiel", "Click here to view the conversation."]]
    assert signature.footer(tails, "Nordwind Portal")[0] == []
    single = [["Best regards", "Nordwind Portal mentioned Alice Beispiel in a chat", "Open it: www.example.com"]]
    assert signature.footer(single, "Nordwind Portal")[1] == "single"
    assert signature.footer(single, "Nordwind Portal", foreign_names={"Alice Beispiel"})[0] == []


def test_a_bare_number_in_a_cell_of_its_own_is_a_phone_number():
    found = signature.fields(["dana@partner.example | 555-010-0199", "Room 555-010-0199 x",
                              "(555) 010-0142"])
    assert found["phones"] == [{"type": "phone", "number": "555-010-0199"},
                               {"type": "phone", "number": "(555) 010-0142"}]


def test_what_a_machine_says_about_its_mail_is_small_print():
    for line in ("This is a system generated email, please do not reply to it.",
                 "*Please note: this e-mail was sent from an address that accepts no mail.",
                 "Dies ist eine automatisch erzeugte Nachricht.", "Bitte antworten Sie nicht auf diese Mail."):
        assert signature._trim(["Nordwind Portal Team", line, "Nordwind, Inc. is a company."]) == [
            "Nordwind Portal Team"], line
    assert signature.footer([["Best regards", "Nordwind Portal Team",
                              "This is a system generated email, do not reply."]], "Nordwind Portal")[0] == []


def test_the_name_line_is_no_address_and_the_footer_above_one_stays():
    block = ["Bob Baumeister", "Head of purchasing", "Nordwind GmbH",
             "bob.baumeister@nordwind.example", "Tel. +49 40 000000 12"]
    assert signature._trim(block, "Bob Baumeister") == block
    assert signature._trim(["Head of purchasing", "Nordwind GmbH", "bob.baumeister@nordwind.example"],
                           "Bob Baumeister")[0] == "Head of purchasing"


def test_a_company_that_begins_like_a_greeting_stays_and_the_closing_line_decides():
    assert signature._trim(["Kind regards", "Bob Baumeister", "Hi-Tech Nordwind GmbH", "Tel. +49 40 000000 12"],
                           "Bob Baumeister") == ["Bob Baumeister", "Hi-Tech Nordwind GmbH", "Tel. +49 40 000000 12"]
    assert signature._trim(["Bob Baumeister", "Servus Handels GmbH", "Hello Nordwind!"], "Bob Baumeister") == [
        "Bob Baumeister", "Servus Handels GmbH", "Hello Nordwind!"]


def test_a_note_in_a_footer_is_no_small_print():
    block = ["Bob Baumeister", "Nordwind GmbH", "Please note my new mobile number", "Mobile +49 170 0000000"]
    assert signature._trim(block, "Bob Baumeister") == block


def test_a_register_word_behind_the_company_ends_that_line_only():
    assert signature._trim(["Bob Baumeister", "Nordwind GmbH, Sitz: Hamburg", "Beispielweg 1, 20095 Hamburg",
                            "www.nordwind.example", "Tel. +49 40 000000 12"], "Bob Baumeister") == [
        "Bob Baumeister", "Nordwind GmbH", "Beispielweg 1, 20095 Hamburg", "www.nordwind.example",
        "Tel. +49 40 000000 12"]


def test_a_sentence_with_wrote_in_it_quotes_nothing():
    for line in ("Bob Baumeister <bob@example.com> wrote on Mon, 4 May 2026 at 10:00:",
                 "Bob Baumeister <bob@example.com> schrieb am Mo., 4. Mai 2026 um 10:00:",
                 "John I. Smith <john@example.com> wrote on 4 May 2026:",
                 "Bob Baumeister schrieb am Mo., 4. Mai 2026, 10:00:"):
        text = "My words.\n\n" + line + "\nquoted\n"
        assert corpus.quote_start(text, dividers=False) == text.index(line), line
    # A sentence in the other languages is no reply line: those begin with the date.
    for line in ("Le client a écrit le 3 mai que les points ouverts sont :",
                 "El cliente escribió ayer lo siguiente:", "Il cliente ha scritto quanto segue:",
                 "Op verzoek heeft Alice het volgende geschreven:", "No dia seguinte o cliente escreveu:",
                 "Igår skrev Alice följande:"):
        text = "Bonjour,\n" + line + "\n- un\n"
        assert corpus.quote_start(text, dividers=False) == len(text), line
    # A long line without an address costs no time either – in a footer's block or a mail line.
    run = "x" * 120000
    began = time.perf_counter()
    assert signature.fields([run]) is not None and not correspondents.addresses_of(run)
    assert time.perf_counter() - began < 1
    # … nor does the register's word said over and over in one line, or markup that never closes a tag.
    began = time.perf_counter()
    assert signature.footer([["Kind regards", "Dana Dienstleister", "Amtsgericht " * 10000]] * 2, "Dana Dienstleister") is not None
    for opener in ('<div class="a ', "<br ", "<!-- ", "<script ", "<blockquote "):
        assert isinstance(signature.html_lines("<p>Dana</p>" + opener * 20000), str)
    assert time.perf_counter() - began < 2
    # What is no text is still dropped, each element with what it holds.
    assert signature.html_lines("a<style>p{}</style>b<!-- c -->d<SCRIPT>x()</script >e<title>t") == "a b d et"
    # A long line of prose costs no time (it once took seconds).
    long = "as I wrote in 2026, " + "we you ich wir " * 20000 + ":"
    began = time.perf_counter()
    assert corpus.quote_start(long, dividers=False) == len(long)
    assert time.perf_counter() - began < 1
    for line in ("Bob von Baumeister schrieb am 04.05.2026 um 10:00:",
                 "-------- Weitergeleitete Nachricht --------", "---------- Forwarded message ---------",
                 "Begin forwarded message:", "Gesendet: Montag, 04. Mai 2026 um 10:00 Uhr\nVon: Alice\nAn: Bob"):
        text = "My words.\n\n" + line + "\nquoted\n"
        assert corpus.quote_start(text, dividers=False) == text.index(line), line
    for text in ("Hello Alice,\n\nas I wrote on 3 May, the open points are:\n- one\nKind regards\nBob",
                 "Herr Baumeister schrieb am Montag Folgendes:\n- eins", "Der Kunde schrieb am 3. Mai:\n- eins",
                 "Alice Beispiel wrote:\nthe plan holds", "Kontakt: bob@example.com schrieb uns dazu:\n- eins",
                 "Please mail bob@example.com, he wrote the following:\n- one",
                 "Dana wrote on 3 May that the open points are:\n- one",
                 "Erik schrieb am 3.5. folgende Punkte:\n- eins",
                 "Hallo Alice,\nwie ich am 3.5. schrieb, gilt Folgendes:\n- eins\nGruß\nBob",
                 "Carla wrote on 3 May, and the list is:\n- one"):
        assert corpus.quote_start(text, dividers=False) == len(text), text


def test_the_singular_closings_close_a_mail_too():
    for line in ("Mit freundlichem Gruß", "Mit bestem Gruß", "Besten Gruß", "Lieben Gruß", "Herzlichst",
                 "All the best", "BR"):
        assert signature._is_closing(line), line
    assert not signature._is_closing("BR Nordwind GmbH")


def test_small_print_longer_than_a_footer_does_not_push_it_out():
    """Forty lines are read at a mail's end; a disclaimer below the closing
    line is taken off before, however long it is."""
    small = ["This e-mail is confidential."] + [f"Paragraph {n} of the small print, at length." for n in range(45)]
    footer = ["Dana Dienstleister", "Head of sales", "Nordwind GmbH", "Tel. +49 40 000000 77"]
    tails = [signature.own_lines(_mail("\n".join([f"Text {n}.", "Kind regards", *footer, *small]) + "\n"))
             for n in range(3)]
    assert signature.footer(tails, "Dana Dienstleister")[:2] == (footer, "repeated")
    # Above a closing line such a sentence is the mail's own.
    own = signature.own_lines(_mail("This e-mail confirms the date.\nSee you.\nKind regards\nDana\n"))
    assert own[0] == "This e-mail confirms the date."


def test_a_court_as_an_employer_is_no_register_line():
    judge = ["Rita Revision", "Richterin am Amtsgericht", "Amtsgericht Hamburg", "Beispielweg 1, 20095 Hamburg",
             "Tel. 040 000000 77"]
    assert signature._trim(judge, "Rita Revision") == judge
    assert signature._trim(["Erik Einkauf", "Registered Nurse, Company Number One Clinic"], "Erik Einkauf") == [
        "Erik Einkauf", "Registered Nurse, Company Number One Clinic"]
    assert signature._trim(["Bob Baumeister", "Nordwind GmbH", "Amtsgericht Hamburg HRB 000012",
                            "Registered in England No. 0000000"], "Bob Baumeister") == ["Bob Baumeister", "Nordwind GmbH"]


def test_a_telephone_word_before_the_number_beats_a_word_further_left():
    found = signature.fields(["Auftragsabwicklung, Tel. +49 40 000000 12", "Head of Order Management Tel +49 40 000000 13",
                              "Registered Nurse, Phone: 555-010-0199", "Rechnung 0401234567"])
    assert [p["number"] for p in found["phones"]] == ["+49 40 000000 12", "+49 40 000000 13", "555-010-0199"]


def test_every_case_of_gruss_closes_a_mail():
    for line in ("Lieber Gruß", "Herzlicher Gruß", "Bester Gruß", "Schöner Gruß", "Viele liebe Grüße",
                 "Ganz liebe Grüße", "Thanks & regards", "Hochachtungsvoll", "Take care"):
        assert signature._is_closing(line), line


def test_only_long_small_print_near_a_closing_line_is_taken_off_early():
    # A mail that opens with thanks and goes on "This email …" keeps its footer.
    own = signature.own_lines(_mail("Vielen Dank,\n\nThis email thread is getting long.\n\n"
                                    "Bob Baumeister\nNordwind GmbH\nTel. +49 40 000000 12\n"))
    assert own[-3:] == ["Bob Baumeister", "Nordwind GmbH", "Tel. +49 40 000000 12"]
    # Small print with a "Thank you." of its own is still taken off below the real closing.
    small = ["This e-mail is confidential."] + [f"Paragraph {n}." for n in range(20)] + ["Thank you."] + ["More."] * 20
    own = signature.own_lines(_mail("\n".join(["Hi", "Kind regards", "Dana Dienstleister", "Tel. +49 40 000000 77", *small]) + "\n"))
    assert own[-1] == "Tel. +49 40 000000 77"
    judge = ["Rita Revision", "Richterin am Amtsgericht Hamburg, Zimmer 12"]
    assert signature._trim(judge, "Rita Revision") == judge


def test_a_label_in_two_languages_and_a_machines_note_are_seen():
    block = ["Dana Dienstleister", "Head of sales", "Nordwind AG", "Mobil: +49 170 0000077",
             "Nordwind AG, Hamburg", "Sitz/Domicile: Hamburg",
             "Vorsitzender des Aufsichtsrats/Chairman of the Supervisory Board: Carla Chef", "Vorstand",
             "If you are not the recipient, please delete this mail."]
    assert signature._trim(block, "Dana Dienstleister") == block[:5]
    assert signature._trim(["Nordwind Portal", "Note, this e-mail was sent from an address that takes no mail.",
                            "Nordwind, Inc. is a company."]) == ["Nordwind Portal"]
