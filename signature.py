"""
signature.py – the footer under a person's mails, and what it says.

A signature is what repeats at the end of someone's mails. That is the one
definition that needs no list of languages, layouts or mail clients, so it
is the one used here: the last lines of a sender's newest mails are held
against each other, and the run of lines at the end that more than one
mail carries is the footer. Only a sender with a single mail falls back to
anchors inside that mail – the closing line ("Kind regards"), the "-- "
separator, the line that is the sender's name – and the answer says which
of the two it rests on (`basis`: "repeated" or "single").

Read from the .eml itself, not from the index: the index keeps a mail's
words on one line (corpus.collapse), and a footer without its lines is a
sentence nobody can take apart. The plain part is preferred – its lines
are the sender's –, the HTML part is turned into lines at its block
elements, a table row a line, its cells side by side.

What comes out is split by how sure a reading can be:

* `lines` – the footer as it stands, at most MAX_LINES lines, without
  disclaimers. Name, job title, company and postal address are in here;
  which line is which differs from footer to footer, and reading that is
  the client's part.
* `phones`, `emails`, `urls` – what has a shape of its own and is taken
  out here, each number with the kind its label names ("phone", "mobile",
  "fax").

Stdlib only.
"""

import html
import re
from email import policy
from email.parser import BytesParser

import corpus

TAIL = 40           # the last lines of a mail that can be footer at all
MAX_LINES = 20      # a footer longer than this is a disclaimer or a banner
MAX_LINE = 200      # … and so is a single line longer than this
SKIP_BELOW = 3      # lines under the footer only this mail has (a postscript)
SMALL_PRINT = 15    # small print this long is taken off before the tail is
LEGAL_WITHIN = 1000 # of a line this much is read for the register's words: a longer one is no footer line
NAME_WITHIN = 6     # the sender's name this near a block's top is where the footer begins

# Every pattern over markup is bounded: a tag that is never closed must
# not be walked to the end once per opening bracket (a mail is foreign
# text, and a long one took seconds). A tag with its attributes: 1 000
# characters at most.
_DROP_OPEN = re.compile(r"(?i)<(script|style|head|title)\b|<!--")
_DROP_CLOSE = {kind: re.compile(rf"(?i)</{kind}\s*>") for kind in ("script", "style", "head", "title")}
_DROP_CLOSE["--"] = re.compile("-->")
_BREAK = re.compile(r"(?is)<br\b[^>]{0,1000}>|</?(?:p|div|tr|li|h[1-6]|table|blockquote|hr)\b[^>]{0,1000}>")
_CELL = re.compile(r"(?is)</t[dh]\s*>")
_TAG = re.compile(r"(?s)<[^>]{1,1000}>")
# What a plain part writes where the HTML shows a picture: "[cid:image001.png@…]"
# with an id of its own in every mail – a line that would cut a footer in two.
_PICTURE = re.compile(r"(?i)^\[(cid:|image|bild|logo|inline)[^\]]*\]$")
# The same where the picture is a link, or stands behind its alternative
# text: "[https://…/logo.png]<https://…>", "[Logo] [cid:…]".
_PICTURE_LINK = re.compile(r"(?i)^\[https?://[^\]]*\](\s*<[^>]*>?)?$")
_PICTURE_ID = re.compile(r"(?i)\s*\[cid:[^\]]*\]")
# What a mail gateway writes above a mail from outside – the same line in
# every mail it lets through, and so "repeated" like a footer.
_BANNER = re.compile(
    r"^\[?(CAUTION|WARNING|ATTENTION|EXTERNAL|EXTERN|EXTERNE|ACHTUNG|VORSICHT|WARNUNG)"
    r"( E-?MAIL| MAIL| SENDER| MESSAGE| NACHRICHT)?\]?\s*[:!–-]")
_INVISIBLE = re.compile("[\\u200b\\u200c\\u200d\\u2060\\ufeff\\u00ad]")

# What a phone or a mail client appends: no part of anyone's footer.
_DEVICE = re.compile(
    r"(?i)^(sent from (my |outlook|mail)|get outlook for|von meinem .{0,30}gesendet"
    r"|gesendet von (meinem|outlook|mail)|envoyé (de|depuis) mon|enviado desde mi)")

# The line a mail closes with, alone on its line (a few words may follow:
# "Viele Grüße aus Hamburg").
_CLOSING = re.compile(
    r"(?i)^(mit (den )?(freundlichen?|besten|herzlichen?|lieben) gr(ü|ue)(ß|ss)en?"
    r"|(mit )?(freundliche[mn]|beste[mn]|herzliche[mn]|liebe[mn]|sch(ö|oe)ne[mn]|viele[mn]) gru(ß|ss)"
    r"|(mit )?(ganz |viele |sehr )?(freundlich|best|herzlich|lieb|sch(ö|oe)n|viel|sonnig)e[mnrs]? gr(u|ü|ue)(ß|ss)e?n?"
    r"|herzlichst|hochachtungsvoll|all the best|take care|thanks (&|and) regards"
    r"|(freundliche|viele|beste|liebe|herzliche|sch(ö|oe)ne|sonnige) gr(ü|ue)(ß|ss)e"
    r"|gr(ü|ue)(ß|ss)e?|gru(ß|ss)"
    r"|(best|kind|kindest|warm|warmest|many) (regards|wishes)"
    r"|many thanks|thanks (a lot|and (best |kind )?regards)|thank you very much"
    r"|(yours )?(sincerely|faithfully|truly)|sincerely yours"
    r"|(vielen|besten|herzlichen) dank|dank und (viele |beste )?gr(ü|ue)(ß|ss)e"
    r"|(bien )?cordialement|bien à vous|salutations( distinguées)?"
    r"|(un )?saludos?( cordiales)?|cordiali saluti|distinti saluti"
    r"|met vriendelijke groet(en)?|med venlig hilsen|vänliga hälsningar)"
    r"\b[^.!?:]{0,30}[,.!]?$")
# The short ones are also how a company's name begins ("LG Beispiel
# Deutschland GmbH", "Best Nordwind Hotel Hamburg"): a closing line only
# alone, or with one word after it that is the sender's name or said to
# the reader ("LG Dana", "Danke dir").
_SHORT_CLOSING = re.compile(
    r"(?i)^(mfg|vg|lg|bg|br|regards|best|cheers|thanks|thank you|dank|danke|groet(en)?)"
    r"(?:[,!]?\s+(?P<word>[\w-]+))?\s*[,.!]?$")
_TO_THE_READER = {"dir", "euch", "ihnen", "you", "all", "again", "vorab", "sehr", "schön", "schoen"}

# Where the small print begins: nothing below it is about the person.
_DISCLAIMER = re.compile(
    r"(?i)^(this (e-?mail|message|communication)|diese (e-?mail|nachricht)"
    r"|der inhalt dieser|the (content|information) (of|in|contained in) this"
    r"|confidential(ity)?\b|vertraulich(keit)?\b|disclaimer|haftungsausschluss"
    r"|please consider the environment|bitte denken sie an die umwelt"
    r"|ce (message|courriel)|if you are not the (intended |named )?(recipient|addressee)"
    # … and what a machine says about its own mail.
    r"|[*(]?\s?(please )?note[,:]? (that )?this (e-?mail|message)|this is an? (system|auto\w*)[- ]generated"
    r"|(please )?do not reply"
    r"|dies ist eine (automatisch|system)\w*|bitte antworten sie nicht)")

# The line a mail opens with: nothing above it, and not the line itself,
# is anyone's footer.
_SALUTATION = re.compile(
    r"(?i)^(hi|hello|hey|hallo|dear|moin|servus|bonjour|hola|ciao"
    r"|liebe[rs]?|sehr geehrte[rs]?|guten (morgen|tag|abend)|good (morning|afternoon|evening))"
    r"\b[^.!?:]{0,40}[,!]?$")
# A header of a mail: a block with one is a quoted or generated mail.
_HEADER_LINE = re.compile(r"(?i)^(subject|betreff|objet|from|sent|gesendet)\s?:")
# Where the register's lines begin (seat, court, board): about the
# company, not the person – and long enough to push the rest out.
_LEGAL = re.compile(
    # With the mark of a label where the word is also somebody's job
    # ("Vorstandsvorsitzende", "Mitglied des Aufsichtsrats", "HRA Team").
    # … and a court or a register where a number or a label's colon
    # says so: "Richterin am Amtsgericht" is a job, "Amtsgericht
    # Musterstadt" an employer.
    # A label may come in two languages at once ("Sitz/Domicile: …").
    r"(?i)\b(sitz der gesellschaft|sitz\s?[:/]|registergericht\s?:"
    r"|(?:registergericht|amtsgericht|handelsregister)\b(?=[^\n]{0,200}\b(?:hr[ab]|[vpg]r|gnr)\b)|handelsregister\s?:"
    r"|hr[ab][\s.:-]*(?:nr\.?[\s:]*)?\d"
    r"|gesch(ä|ae)ftsf(ü|ue)hr(er|erin|ung|ende\w*)\s?:|vorstand\s?:|vorstandsvorsitz\w*\s?:"
    r"|(vorsitz\w* des )?aufsichtsrats?\s?[:/]|(chairman of the )?supervisory board\s?:|registered (office|in|number|no)\b(?=\s?:|[^\n]{0,200}\d)"
    r"|managing directors?\s?:|board of (directors|management)\s?:|company (no|number|registration)\b(?=\s?:|[^\n]{0,200}\d))")
# A number of the North American shape in a cell of its own.
_BARE_PHONE = re.compile(r"^\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}$")

_MAIL = re.compile(r"[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+")     # bounded: a long line without "@" stays cheap
_URL = re.compile(r"(?i)(?<![\w@])(?:https?://|www\.)[^\s<>\"')\]|]+")
_CONTACT = re.compile(r"(?i)\d|@|www\.|https?://")
_DATE = re.compile(r"(?<!\d)\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})(?!\d)")
# A number that is not for calling: what stands between it and the number
# before it (or the cell's edge) says so. Words of the register and the
# bank only – "Key Account Manager" and "Tax Advisor" are job titles.
_NOT_A_PHONE = re.compile(
    r"(?i)\b(hrb|hra|ust|vat|iban|bic|swift|amtsgericht|registergericht|handelsregister"
    r"|steuer-?(nummer|nr|id)|st\.?-?nr|tax (id|no|number)|konto(nummer|-?nr)?"
    r"|account (no|nr|number)|reg\.?\s?(nr|no)|registered|company (no|number|registration)|siret|kvk"
    r"|kunden-?(nummer|nr)|kd\.?-?nr|customer (no|number|id)|angebot\w*|auftrag\w*"
    r"|bestell\w*|rechnung\w*|invoice|order|quote|ticket|ref)\b")
# Opening hours: "08.00-17.00", "8:00 – 17:00", and a number "Uhr" follows.
_HOURS = re.compile(r"\d{1,2}[.:]\d{2}\s*[-–]\s*\d{1,2}[.:]\d{2}")
_CLOCK = re.compile(r"(?i)\s*(uhr|h|am|pm)\b")
_CELL_EDGE = re.compile(r"[|·•;]")
_LABEL = r"""(?: (?<![\w@]) (?P<label>
            mobiltelefon | mobilfunk | mobile | mobil | mob | handy | cell
          | telefax | fax
          | telefon | tel | phone | fon | direct | direkt | durchwahl
          | zentrale | office | büro | t | p | m | f | d )
        \b (?P<mark> \s* [.:]? ) \s* (?: \( [^)\d]{1,12} \) \s* )? [.:]? \s* )"""
# Without a label a number is one that starts as numbers for calling do
# ("+", "0", "(0"); behind a label that means a telephone any digit starts
# one – "Tel: 555-123-4567", "Phone: (555) 123-4567". "Office 2019" does not.
_PHONE = re.compile(r"(?ix)" + _LABEL + r"""?
    (?<![\w@.,/-]) (?P<number> (?: \+ | \(0 | 0 ) [\d\s()/.\-–]{5,} \d )
  | """ + _LABEL.replace("<label>", "<label2>").replace("<mark>", "<mark2>").replace(
      "| zentrale | office | büro ", "").replace("| direct | direkt ", "") + r"""
    (?P<number2> (?: \+ | \d | \(\d ) [\d\s()/.\-–]{5,} \d )
""")
_KINDS = ({"mobiltelefon": "mobile", "mobilfunk": "mobile", "mobile": "mobile",
           "mobil": "mobile", "mob": "mobile", "handy": "mobile", "cell": "mobile",
           "m": "mobile", "telefax": "fax", "fax": "fax", "f": "fax"})


_QUOTE_OPEN = re.compile(r"(?i)<blockquote\b|<div[^>]{0,1000}\b(?:class=\"[^\">]{0,200}gmail_quote|id=\"?divRplyFwdMsg)")
_BLOCKQUOTE = re.compile(r"(?i)<(/?)blockquote\b[^>]{0,1000}>")


def _dropped(markup):
    """Markup without what is no text: script, style, head and title with
    what they hold, and comments. One walk – an element that is never
    closed is looked for once, not once per opening."""
    out, at, open_ended = [], 0, set()
    for m in _DROP_OPEN.finditer(markup):
        kind = (m.group(1) or "--").lower()
        if m.start() < at or kind in open_ended:
            continue
        end = _DROP_CLOSE[kind].search(markup, m.end())
        if end is None:
            open_ended.add(kind)
            continue
        out.append(markup[at:m.start()])
        out.append(" ")
        at = end.end()
    out.append(markup[at:])
    return "".join(out)


def _without_quotes(markup):
    """HTML without what it quotes: a <blockquote> with all it holds, and
    everything from the block a mail client opens its quoted history with
    (Gmail's gmail_quote, Outlook's divRplyFwdMsg). The line above a quote
    comes in every language; the element is the same in all of them."""
    out, at = [], 0
    while True:
        m = _QUOTE_OPEN.search(markup, at)
        if m is None:
            return "".join(out) + markup[at:]
        out.append(markup[at:m.start()])
        if not m.group(0).lower().startswith("<blockquote"):
            return "".join(out)
        depth, at = 0, m.start()
        for tag in _BLOCKQUOTE.finditer(markup, m.start()):
            depth += -1 if tag.group(1) else 1
            at = tag.end()
            if depth == 0:
                break
        else:
            return "".join(out)                  # never closed: quoted to the end
        out.append("\n<br>")


def html_lines(markup):
    """An HTML body as text with its lines: a block element ends a line,
    a table row is one line with its cells side by side."""
    text = _dropped(_without_quotes(markup or ""))
    text = " ".join(text.split())               # a line break in the source is none
    text = _BREAK.sub("\n", text)
    text = _CELL.sub(" ", text)
    return html.unescape(_TAG.sub("", text))


def _body(msg, kind):
    try:
        part = msg.get_body(preferencelist=(kind,))
    except Exception:                            # noqa: BLE001 – a broken part is no part
        return ""
    return corpus.decode_part(part) if part is not None else ""


def own_lines(msg):
    """The last lines of what the sender wrote in this mail – without the
    quoted history, without empty lines, each with single spaces."""
    text = _body(msg, "plain")
    if not text.strip():
        text = html_lines(_body(msg, "html"))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text[:corpus.quote_start(text, dividers=False)]
    lines = []
    for raw in text.split("\n"):
        line = " ".join(_PICTURE_ID.sub("", _INVISIBLE.sub("", raw)).split())
        if (line and not line.startswith(">") and not _DEVICE.match(line)
                and not _PICTURE.match(line) and not _PICTURE_LINK.match(line)
                and not _BANNER.match(line)):
            lines.append(line)
    # Small print is longer than a footer at times: taken off before the
    # last lines are, or it would be all of them. Only where it is that
    # long, and begins within a footer's length below a closing line –
    # "This e-mail confirms …" opens many a mail, and a short notice is
    # _trim's to cut.
    closings = [i for i, line in enumerate(lines) if _is_closing(line)]
    for j, line in enumerate(lines):
        if (len(lines) - j >= SMALL_PRINT and _DISCLAIMER.match(line)
                and any(j - MAX_LINES - 5 <= i < j for i in closings)):
            lines = lines[:j]
            break
    return lines[-TAIL:]


def _read(path):
    """(Message-ID, own_lines) of one .eml file."""
    with open(path, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)
    try:
        ident = str(msg.get("Message-ID") or "").strip()
    except Exception:                            # noqa: BLE001 – a broken header is none
        ident = ""
    return ident, own_lines(msg)


def _is_closing(line, name=""):
    if len(line) > 70:
        return False
    if _CLOSING.match(line):
        return True
    m = _SHORT_CLOSING.match(line)
    if not m:
        return False
    word = (m.group("word") or "").casefold()
    return not word or word in _TO_THE_READER or word in re.findall(r"\w+", name.casefold())


def _is_name(line, name):
    """Is this line the sender's name – as written in From, or its words in
    another order ("Baumeister, Bob"), a degree in front allowed?"""
    words = set(re.findall(r"\w+", name.casefold()))
    # "bob.baumeister@nordwind.example" holds the name's words and is no name.
    if len(words) < 2 or len(line) > len(name) + 20 or "@" in line:
        return False
    return words <= set(re.findall(r"\w+", line.casefold()))


def _trim(block, name=""):
    """A found block without what is not footer: everything up to its
    closing line or its salutation, what stands above the sender's name
    (a first name, a note of absence), the register's lines and the small
    print, separators. Below the first line of the register only a line
    with a number to call or an address stays."""
    for i in range(len(block) - 1, -1, -1):
        if _is_closing(block[i], name):
            block = block[i + 1:]
            break
    else:
        # No closing line: the line a mail opens with, said to someone
        # ("Hi Bob,"), is where its words begin. With its comma only –
        # "Hi-Tech Nordwind GmbH" is a company.
        for i in range(len(block) - 1, -1, -1):
            if len(block[i]) <= 60 and block[i].endswith(",") and _SALUTATION.match(block[i]):
                block = block[i + 1:]
                break
    if not (name and block and _is_name(block[0], name)):
        for i in range(1, min(len(block), NAME_WITHIN)):
            if _is_name(block[i], name):
                if not any(_reaches(line) for line in block[:i]):
                    block = block[i:]
                break
    out, legal = [], False
    for line in block:
        if _DISCLAIMER.match(line):
            break
        hits = list(_LEGAL.finditer(line[:LEGAL_WITHIN]))
        if hits and not legal:
            # The register's block begins at a line that starts with one
            # of its words or holds several. One such word behind a
            # company's name ("Nordwind GmbH, Sitz: Hamburg") ends that
            # line only – the street and the website below it stay.
            legal = len(hits) >= 2 or not line[:hits[0].start()].strip(" |·•,;/(–-")
            # What stands before the word is the company's name: it stays.
            if not _reaches(line):
                head = line[:hits[0].start()].rstrip(" |·•,;/(–-")
                if any(ch.isalnum() for ch in head) and len(head) <= MAX_LINE:
                    out.append(head)
                continue
        if legal and not _reaches(line):
            continue
        if len(line) <= MAX_LINE and any(ch.isalnum() for ch in line):
            out.append(line)
    return out[:MAX_LINES]


def _reaches(line):
    """Does this line hold a way to reach someone – a number, an address?"""
    return bool(_MAIL.search(line)) or bool(fields([line])["phones"])


def _says_something(block, name=""):
    """Worth handing out: more than the name the From line had anyway."""
    rest = [line for line in block if not (name and _is_name(line, name))]
    return len(rest) >= 2 or any(_CONTACT.search(line) for line in rest)


def _repeated(tail, others):
    """The run of lines at the end of `tail` that another mail carries
    too. A few lines below it that only this mail has are passed over."""
    end = len(tail)
    while end > 0 and len(tail) - end < SKIP_BELOW and tail[end - 1].casefold() not in others:
        end -= 1
    start = end
    while start > 0 and tail[start - 1].casefold() in others:
        start -= 1
    return tail[start:end]


def _anchored(tail, name):
    """The footer of a single mail: what follows its closing line or its
    "-- " separator, else what begins with the sender's name."""
    for i in range(len(tail) - 1, -1, -1):
        if _is_closing(tail[i], name) or tail[i] in ("--", "-- "):
            return tail[i + 1:]
    if name:
        for i in range(len(tail) - 1, max(-1, len(tail) - 16), -1):
            if _is_name(tail[i], name):
                return tail[i:]
    return []


def _names(block, foreign):
    """Does this block carry one of the `foreign` addresses? Then it is
    somebody else's footer – the owner's own, quoted below an answer in a
    shape no cut knew. A block with a mail header's line is no footer
    either."""
    if any(_HEADER_LINE.match(line) for line in block):
        return True
    return bool(foreign) and any(found.lower() in foreign
                                 for line in block for found in _MAIL.findall(line))


def _addresses_someone(block, names):
    """Does a block say one of `names` in full? A notification does ("…
    mentioned Alice Beispiel"); a footer found in one mail only is none
    then."""
    text = " ".join(" ".join(block).casefold().split())
    return any(len(n.split()) >= 2 and " ".join(n.casefold().split()) in text for n in names)


def footer(tails, name="", foreign=(), foreign_names=()):
    """(lines, basis, index) out of the tails of a sender's mails, newest
    first: the footer, what it rests on ("repeated" – the same lines end
    more than one mail –, "single" – one mail's closing line, separator or
    name line –, "" for none) and which tail it was read from.

    Of several repeated footers the longest counts, of equally long ones
    the newest: a short one under answers and a full one under new mails
    are both someone's signature, and the full one says more."""
    keys = [{line.casefold() for line in tail} for tail in tails]
    best, at = [], None
    for i, tail in enumerate(tails):
        others = set().union(*(k for j, k in enumerate(keys) if j != i)) if len(keys) > 1 else set()
        run = _repeated(tail, others) if others else []
        # A mail another one repeats whole – an automatic reply, a
        # notification – shows no footer by repeating: only a closing
        # line in it says where its words end.
        if len(run) == len(tail) and not any(_is_closing(line, name) for line in run):
            run = []
        block = _trim(run, name)
        if len(block) > len(best) and _says_something(block, name) and not _names(block, foreign):
            best, at = block, i
    if best:
        return best, "repeated", at
    for i, tail in enumerate(tails):
        block = _trim(_anchored(tail, name), name)
        if (block and _says_something(block, name) and not _names(block, foreign)
                and not _addresses_someone(block, foreign_names)):
            return block, "single", i
    return [], "", None


def _numbers(line):
    after = 0
    for m in _PHONE.finditer(line):
        before, after = _CELL_EDGE.split(line[after:m.start()])[-1], m.end()
        label = (m.group("label") or m.group("label2") or "").lower()
        number = m.group("number") or m.group("number2")
        # A single letter is a label for a number of any start only with
        # its mark ("T: 555 …"): "D 20095 Hamburg" is a postcode.
        if m.group("number2") and len(label) == 1 and not m.group("mark2").strip():
            continue
        # A word of the register or of an order further left ("Order
        # Management Tel …", "Rechnungswesen - Tel. …") says nothing once a
        # telephone's own word stands right before the number.
        if len(label) < 2 and _NOT_A_PHONE.search(
                before + line[m.start():m.start("number") if m.group("number") else m.start("number2")]):
            continue
        number = " ".join(number.split())
        digits = sum(ch.isdigit() for ch in number)
        if (not 7 <= digits <= 16 or _DATE.search(number) or _HOURS.search(number)
                or _CLOCK.match(line, m.end())):
            continue
        yield _KINDS.get(label, "phone"), number


def fields(lines):
    """What in a footer has a shape of its own: the numbers with their
    kind, the addresses, the links – each once, in the order they stand."""
    phones, emails, urls, seen = [], [], [], set()
    for line in lines:
        for found in _MAIL.findall(line):
            if found.lower() not in emails:
                emails.append(found.lower())
        for found in _URL.findall(line):
            found = found.rstrip(".,;:")
            if found not in urls:
                urls.append(found)
        # A number inside a link or an address is no phone number.
        plain = _URL.sub(" ", _MAIL.sub(" ", line))
        # A cell that is a number of the North American shape and nothing
        # else is one whole, not the part of it that starts with a zero.
        bare = [cell.strip() for cell in _CELL_EDGE.split(plain) if _BARE_PHONE.match(cell.strip())]
        found = [("phone", cell) for cell in bare]
        whole = ["".join(ch for ch in cell if ch.isdigit()) for cell in bare]
        found += [(kind, number) for kind, number in _numbers(plain)
                  if not any("".join(ch for ch in number if ch.isdigit()) in w for w in whole)]
        for kind, number in found:
            digits = "".join(ch for ch in number if ch.isdigit())
            if digits not in seen:
                seen.add(digits)
                phones.append({"type": kind, "number": number})
    return {"phones": phones, "emails": emails, "urls": urls}


def read(paths, name="", step=0, foreign=(), foreign_names=()):
    """The footer of the mails at `paths` (one sender's, the likeliest
    first): its lines, its fields, its basis, how many mails were read and
    the path of the mail it stands in – None when no footer was found.

    Read `step` mails at a time, and no further once a footer is found:
    most senders show theirs in the first few, and the rest of the list
    is only there for those who do not. A file that cannot be read is
    passed over – and so is a mail read already: the same mail filed in
    two folders repeats every line of itself, and would hand out its
    words as the footer."""
    read_from, tails, seen = [], [], set()
    lines, basis, at = [], "", None
    step = step if step > 0 else max(1, len(paths))
    for begin in range(0, len(paths), step):
        for path in paths[begin:begin + step]:
            try:
                ident, tail = _read(path)
            except Exception:                    # noqa: BLE001 – one broken file is not the answer
                continue
            if ident and ident in seen:
                continue
            seen.add(ident)
            if tail:
                read_from.append(path)
                tails.append(tail)
        lines, basis, at = footer(tails, name, foreign, foreign_names)
        if lines:
            break
    return {"lines": lines, **fields(lines), "basis": basis, "mails": len(tails),
            "path": read_from[at] if at is not None else None}
