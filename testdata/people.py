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
# internal (the setting internal_domains names nordwind.example). The
# first two carry the story the browser tests read; the rest are the
# volume behind it – everyone of them writes, so the people view shows
# the whole cast and no node stands for someone the archive never heard.
COLLEAGUES = [
    ("Bob Baumeister", "bob.baumeister@nordwind.example"),
    ("Carla Chef", "carla.chef@nordwind.example"),
    ("Frida Finanz", "frida.finanz@nordwind.example"),
    ("Hanno Helpdesk", "hanno.helpdesk@nordwind.example"),
    ("Ines Innendienst", "ines.innendienst@nordwind.example"),
    ("Kai Kalkulation", "kai.kalkulation@nordwind.example"),
    ("Lena Lager", "lena.lager@nordwind.example"),
    ("Malte Marketing", "malte.marketing@nordwind.example"),
    ("Nina Netzwerk", "nina.netzwerk@nordwind.example"),
    ("Olaf Organisation", "olaf.organisation@nordwind.example"),
]

# Outside it: a service provider, a buyer, a guest, and the partners the
# bulk of the archive is written with. They are what makes "external"
# visible in the people view and in a case.
EXTERNALS = [
    ("Dana Dienstleister", "dana.dienstleister@example.com"),
    ("Erik Einkauf", "erik.einkauf@example.com"),
    ("Greta Gast", "greta.gast@example.com"),
    ("Hanna Handel", "hanna.handel@example.com"),
    ("Ingo Import", "ingo.import@example.com"),
    ("Lars Logistik", "lars.logistik@example.com"),
    ("Mona Montage", "mona.montage@example.com"),
    ("Nils Notar", "nils.notar@example.com"),
    ("Petra Planung", "petra.planung@example.com"),
    ("Rita Revision", "rita.revision@example.com"),
    ("Sven Schulung", "sven.schulung@example.com"),
    ("Tina Technik", "tina.technik@example.com"),
    ("Vera Vertrieb", "vera.vertrieb@example.com"),
]

# The interface draws 24 people per period of the picture before it
# offers "and n more": a cast that stays under that shows itself whole,
# which is what the browser tests hold it to.
assert len(COLLEAGUES) + len(EXTERNALS) <= 24

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
