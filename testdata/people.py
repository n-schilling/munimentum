"""The cast and the calendar of the synthetic archive – one place.

Everything the generated archive says about people, companies and dates
comes from here, so a name never has to be hunted through ten writers. The
world is the one this repository uses everywhere: Nordwind as the
fictional employer, `nordwind.example` as its mail domain, `example.com`
for everyone outside it. No real person, company, domain or address
appears in the generated archive – that is what makes it fit for a public
repository.

The day the archive is built around is fixed and does not move: every date
the generator writes is derived from TODAY, so a repeated run produces the
same archive, and a test that names a month or a gap keeps meaning what it
meant. Appointments before that day are past, the ones after it are what
the calendar shows as upcoming.
"""

from datetime import date, datetime

TODAY = date(2026, 6, 15)
YEAR = TODAY.year

# The archive's owner – the mailbox, the drive and the cases are hers.
ME = ("Alice Beispiel", "alice.beispiel@nordwind.example")

# Colleagues: inside the organisation, so the interface counts them as
# internal (the setting internal_domains names nordwind.example).
COLLEAGUES = [
    ("Bob Baumeister", "bob.baumeister@nordwind.example"),
    ("Carla Chef", "carla.chef@nordwind.example"),
]

# Outside it: a service provider, a buyer, a guest. They are what makes
# "external" visible in the people view and in a case.
EXTERNALS = [
    ("Dana Dienstleister", "dana.dienstleister@example.com"),
    ("Erik Einkauf", "erik.einkauf@example.com"),
    ("Greta Gast", "greta.gast@example.com"),
]

EVERYONE = [ME, *COLLEAGUES, *EXTERNALS]
ADDRESS = {name: mail for name, mail in EVERYONE}
NAME_OF = {mail: name for name, mail in EVERYONE}

COMPANY = "Nordwind"
DOMAIN = "nordwind.example"
SHAREPOINT_HOST = "firma.sharepoint.com"
SHAREPOINT_SITE = f"https://{SHAREPOINT_HOST}/sites/nordwind"

# The two matters everything in the archive belongs to: they give the
# search something to find that is more than a single hit, and a case
# something to collect.
PROJECT = "Ostwind"          # the running project: mails, chats, tasks, files
OFFER = "Offer 4711"         # what is being negotiated with the provider


def day(month, number, hour=9, minute=0):
    """A timestamp in the archive's year – the one date helper."""
    return datetime(YEAR, month, number, hour, minute)
