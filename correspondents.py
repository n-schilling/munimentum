"""
correspondents.py – who the mailbox's owner writes with, counted per address.

The question "whom did I have contact with, how often, and since when" is
an aggregation over every mail: who sent it, who stood in To, Cc and Bcc.
The index keeps those lines on the chunks, beside each mail's text – asking
them there means walking the fattest table of the store. So every index
run writes one slim row per mail into `correspondence` (index_rows, called
by rag_index.write_db), and the count reads that; an index from before the
table is read from the chunks instead – the same rows, only slower.

Two directions are counted apart, because together they say what a plain
count cannot: `received` is the mail an address sent, `sent` the mail the
owner sent it. A newsletter has hundreds received and none sent; someone
the owner works with has both.

Who the owner is, is read from the mailbox (own_addresses): the address
that is a party to the most mails, and every address writing under the
same name. A caller that knows better names the addresses itself.

Stdlib only; the callers hand in the connection.
"""

import re
import time

MAIL_LINES = ("to_ppl", "cc_ppl", "bcc_ppl")
# The local part as RFC 5322 allows it: "o'brien@example.com" is an address.
# … and as long: 64 characters, which also keeps a long line without "@"
# from being walked once per character.
_ADDRESS = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]{1,64}@[\w-]+(?:\.[\w-]+)+")
UNKNOWN = "(unbekannt)"


def addresses_of(*lines):
    """The addresses in mail lines as the index keeps them – names and
    addresses side by side, lower-case – each once. Only a word that is
    an address as it stands: a display name may hold an "@" of its own
    ("Sales @ Nordwind") or an address in brackets, and neither is a
    recipient."""
    found = set()
    for line in lines:
        for word in str(line or "").split():
            if _ADDRESS.fullmatch(word):
                found.add(word.lower())
    return found


def index_rows(chunks):
    """One row per mail for the `correspondence` table: when, from which
    address under which name, to which addresses (To, Cc and Bcc alike),
    in which conversation, in which file. A mail filed twice – in Sent
    and in the Inbox of someone who writes to a list they belong to – is
    one mail: the first file of a Message-ID (the chunk's key) counts."""
    seen = set()
    for c in chunks:
        if c.get("src") != "outlook" or not c["cid"].endswith("#0"):
            continue
        if c.get("key"):
            if c["key"] in seen:
                continue
            seen.add(c["key"])
        name, sender = (c.get("who") or "").strip(), (c.get("who_mail") or "").lower()
        # A From without a display name is indexed under its address:
        # that is no name the address writes under.
        yield (c.get("ts"), sender,
               "" if name == UNKNOWN or name.lower() == sender else name,
               " ".join(sorted(addresses_of(*(c.get(line) for line in MAIL_LINES)))),
               c.get("thread") or "", c["rel"])


def _has_table(con):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                       "AND name = 'correspondence'").fetchone() is not None


def readable(con):
    """Can this index answer at all? One from before the mail lines (13.0)
    cannot."""
    if _has_table(con):
        return True
    columns = {row[1] for row in con.execute("PRAGMA table_info(chunks)")}
    return {"who_mail", *MAIL_LINES} <= columns


def rows(con):
    """(ts, sender, name, recipients, thread, rel) of every mail."""
    if _has_table(con):
        yield from con.execute("SELECT ts, sender, name, rcpt, thread, rel FROM correspondence")
        return
    columns = {row[1] for row in con.execute("PRAGMA table_info(chunks)")}
    key = "key" if "key" in columns else "NULL"
    seen = set()
    for ts, sender, name, to, cc, bcc, thread, rel, ident in con.execute(
            f"SELECT ts, who_mail, who, to_ppl, cc_ppl, bcc_ppl, thread, rel, {key} "
            "FROM chunks WHERE seq = 0 AND src = 'outlook' ORDER BY rowid"):
        if ident:
            if ident in seen:
                continue
            seen.add(ident)
        name, sender = (name or "").strip(), (sender or "").lower()
        yield (ts, sender, "" if name == UNKNOWN or name.lower() == sender else name,
               " ".join(sorted(addresses_of(to, cc, bcc))), thread or "", rel)


def typed_addresses(text):
    """The addresses in what a user typed into a field – whatever stands
    between them (commas, semicolons, brackets, line breaks) – lower-case,
    each once, in their order."""
    return list(dict.fromkeys(_ADDRESS.findall(str(text or "").lower())))


class Stats(dict):
    """address → [mails sent, mails written to] – and, beside the counts,
    how often each address wrote to which other (`wrote_to`): what tells
    an owner's second address from a namesake who writes to them."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.wrote_to = {}


def profile(con):
    """The mailbox as a whole: per address how many mails it sent and in
    how many it was written to, and the name it writes under – the one
    at least half of its mails carry; an address without such a name has
    none."""
    stats, names = Stats(), {}
    for _ts, sender, name, rcpt, _thread, _rel in rows(con):
        if sender:
            stats.setdefault(sender, [0, 0])[0] += 1
            if name:
                seen = names.setdefault(sender, {})
                seen[name] = seen.get(name, 0) + 1
        for address in rcpt.split():
            stats.setdefault(address, [0, 0])[1] += 1
            if sender:
                to = stats.wrote_to.setdefault(sender, {})
                to[address] = to.get(address, 0) + 1
    # The name an address writes under – when it has one: a relay (a
    # sharing notification, a ticket system) writes under everybody's, and
    # the one it uses most would make it that person's second address.
    name_of = {}
    for address, seen in names.items():
        top = max(seen, key=lambda n: (seen[n], n))
        if seen[top] * 2 >= sum(seen.values()):
            name_of[address] = top
    return stats, name_of


def own_addresses(stats, name_of, own_name="", account="", mine=()):
    """The owner's addresses: `account` – the address the user is signed
    in with – when the mailbox knows it, else read from the mailbox: the
    address that is a party to the most mails – of those that both write
    and are written to, so a distribution list everything arrives through
    is not taken for a person. Beside it every address that writes under
    the same name and hardly ever to the owner, and every address at all
    that writes under `own_name`, the name the user gave the app.

    The guess needs the owner's own mails: an archive without the sent
    folder, or a shared mailbox, would name the busiest colleague – there
    the account is the only thing that knows. But the account need not
    be the address the owner writes from (a sign-in name beside the
    primary address): one that never wrote stands alone only when more
    mail is written to it than the guessed address is a party to, and
    joins the guess otherwise. `mine` – the addresses the user named as
    their own – always count."""
    account = str(account or "").strip().lower()
    candidates = [a for a, (sent, got) in stats.items() if sent and got] or list(stats)
    if not candidates:
        return {a for a in mine if a}
    top = max(candidates, key=lambda a: (sum(stats[a]), a))
    own = {top}
    if account in stats:
        sent, got = stats[account]
        own = {account} if sent or got >= sum(stats[top]) else {account, top}
    # What the user named as their own addresses (the setting
    # own_addresses) is theirs, whatever the mail looks like – a private
    # address one writes to and from is no stranger.
    own |= {a for a in mine if a}
    # Under the name the owner's addresses write under: their other
    # addresses. A person is an address, never a name – a namesake is
    # somebody else, and shows it: a fair share of their mail goes TO the
    # owner. An address of the owner's own writes to others; a mail
    # forwarded to oneself now and then does not make it a stranger. Only
    # the name the user gave the app says "that is me" for every address
    # that writes under it – the way to settle it where the mail cannot.
    written = {name_of[a].casefold() for a in own if name_of.get(a, "").strip()}
    given = (own_name or "").strip().casefold()
    wrote_to = getattr(stats, "wrote_to", {})

    def somebody_else(address):
        to_owner = sum(n for to, n in wrote_to.get(address, {}).items() if to in own)
        return to_owner >= 2 and to_owner * 5 >= stats[address][0]
    return own | {a for a, n in name_of.items()
                  if (given and n.casefold() == given)
                  or (n.casefold() in written and not somebody_else(a))}


def collect(con, me, since=None, until=None, key=None):
    """address → {received, sent, threads, first, last} for everyone but
    the owner, inside the window (timestamps, either may be None). With
    `key` – a function from an address to whom it belongs – the counts
    are per that: a mail to two addresses of one person is one mail.

    `received` counts the mails the address sent, `sent` the mails one of
    `me` sent with the address in To, Cc or Bcc. A mail of a third party
    that merely names the address beside the owner counts for neither:
    standing in the same Cc line is no contact. `threads` are the
    conversations either kind of mail belongs to."""
    bounded = since is not None or until is not None
    found = {}

    def note(address, direction, ts, thread):
        entry = found.get(address)
        if entry is None:
            entry = found[address] = {"received": 0, "sent": 0, "threads": set(),
                                      "first": None, "last": None}
        entry[direction] += 1
        if thread:
            entry["threads"].add(thread)
        if ts is not None:
            if entry["first"] is None or ts < entry["first"]:
                entry["first"] = ts
            if entry["last"] is None or ts > entry["last"]:
                entry["last"] = ts

    for ts, sender, _name, rcpt, thread, _rel in rows(con):
        if bounded and (ts is None or (since is not None and ts < since)
                        or (until is not None and ts > until)):
            continue
        if sender in me:
            others = [a for a in rcpt.split() if a not in me]
            for whom in (others if key is None else {key(a) for a in others}):
                note(whom, "sent", ts, thread)
        elif "@" in sender:             # an Exchange path in From is no address
            note(sender if key is None else key(sender), "received", ts, thread)
    for entry in found.values():
        entry["threads"] = len(entry["threads"])
    return found


# "Frequent contact": mail in both directions and at least `mails` of them
# in the archive's last `months` months – by default about one a month, and
# somebody who writes back; both numbers are settings (frequent_mails,
# frequent_months). Counted back from the archive's newest mail, not from
# today: an archive that was last updated in spring still says who the
# frequent contacts were then.
_MONTH = 365.25 / 12 * 86400


def people(con, me, names, book, months=12, mails=12, now=None):
    """The correspondents person by person, for the address book: an
    address book card's addresses are one person, every other address is
    one of its own. Each with `name` (the card's, else the sender's, else
    the address), `also` (other names its addresses write under),
    `addresses` (most mail first), `writes_from` (the address most of its
    mail comes from, "" for someone who never wrote), `received`, `sent`,
    `last` (a timestamp), `card` (the address book knows the person) and `frequent` – by `months` and `mails`, see above.
    `book` maps an address to its card."""
    whole = collect(con, me)
    # The archive's newest mail – of those that are not dated after today
    # (`now`): one piece of spam with a Date header in 2037 would move the
    # window to where no mail is, and nobody would be a frequent contact.
    now = time.time() if now is None else now
    newest = max((row[0] for row in rows(con) if row[0] is not None and row[0] <= now),
                 default=None)
    # Who a person is, is said by the address – never by the name: two
    # people of one name are common, in one company and across several,
    # and merged they would share counts, a frequent mark and a footer.
    # Only an address book card joins addresses: the user put them there.
    def person_of(address):
        card = book.get(address)
        return ("card", card["uid"]) if card else ("address", address)

    # Counted per person, not added up over the addresses: a mail to both
    # addresses of a card is one mail.
    counted = collect(con, me, key=person_of)
    recent = (collect(con, me, since=newest - months * _MONTH, until=now, key=person_of)
              if newest is not None else {})
    groups = {}
    for address, counts in whole.items():
        card = book.get(address)
        name = names.get(address, "")
        key = person_of(address)
        person = groups.get(key)
        if person is None:
            lately = recent.get(key) or {"received": 0, "sent": 0}
            person = groups[key] = {"name": name or address, "also": set(), "addresses": [],
                                    "writes_from": (0, ""), "card": False,
                                    "received": counted[key]["received"],
                                    "sent": counted[key]["sent"], "last": counted[key]["last"],
                                    "recent": [lately["received"], lately["sent"]]}
        if card:
            person["card"] = True
        if card and card.get("name"):
            person["name"] = card["name"]
        if name:
            person["also"].add(name)
        person["addresses"].append((counts["received"] + counts["sent"], address))
        if counts["received"] and (counts["received"], address) > person["writes_from"]:
            person["writes_from"] = (counts["received"], address)
    out = []
    for person in groups.values():
        got, sent = person.pop("recent")
        person["frequent"] = bool(got and sent and got + sent >= mails)
        person["writes_from"] = person["writes_from"][1]
        person["also"] = sorted(person["also"] - {person["name"]})
        person["addresses"] = [a for _n, a in sorted(person["addresses"], key=lambda x: (-x[0], x[1]))]
        out.append(person)
    out.sort(key=lambda p: (-(p["received"] + p["sent"]), p["name"].casefold()))
    return out


def signature_mails(con, addresses, limit):
    """address → the files to read its signature from, at most `limit`
    each: (rel, ts), the likeliest first.

    Mail clients put the full signature under a new mail and a short one,
    or none, under an answer. So the mails a sender OPENED a conversation
    with take turns with its newest ones: the newest alone would, for
    someone the owner is in a long exchange with, be a row of answers."""
    wanted = sorted({a.lower() for a in addresses})
    if not wanted:
        return {}
    marks = ",".join("?" * len(wanted))
    if _has_table(con):
        query = (f"SELECT sender, rel, ts, thread FROM correspondence "
                 f"WHERE sender IN ({marks}) ORDER BY ts DESC")
    else:
        query = (f"SELECT who_mail, rel, ts, thread FROM chunks WHERE seq = 0 "
                 f"AND src = 'outlook' AND who_mail IN ({marks}) ORDER BY ts DESC")
    mails = {a: [] for a in wanted}
    for sender, rel, ts, thread in con.execute(query, wanted):
        mails[sender].append((rel, ts, thread or rel))
    out = {}
    for address, newest in mails.items():
        # Newest first, so the last mail seen of a conversation is the
        # sender's first in it.
        first_in = {thread: (rel, ts) for rel, ts, thread in newest}
        opened = sorted(first_in.values(), key=lambda m: -(m[1] or 0))
        picked = {}
        for i in range(len(newest)):
            if len(picked) >= limit:
                break
            if i < len(opened):
                picked.setdefault(opened[i][0], opened[i][1])
            if len(picked) < limit:
                picked.setdefault(newest[i][0], newest[i][1])
        out[address] = list(picked.items())
    return out
