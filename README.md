# Munimentum

Your own Microsoft 365 data, kept where you can reach it: Teams chats and
channels with the files they share, Outlook mail, calendar, contacts,
OneDrive files, SharePoint libraries and pages, Planner boards, To Do lists
and OneNote notebooks — exported through Microsoft Graph and searchable
offline, in the app or through Claude via MCP.

**The magic:** All via delegated access, no admin consent required.

> **munimentum** *(Latin)* — a rampart; and, in medieval usage, a deed: the
> document you keep because it is the proof of what is yours. The two senses are
> the same idea. This is a walled place for records that would otherwise live
> only in someone else's cloud, and that you would have a hard time getting at
> once your access ends.

Nothing leaves your machine except the calls to Microsoft Graph — see [PRIVACY.md](PRIVACY.md).

---

## Download and run

Bundles for macOS, Windows and Linux are attached to every
[release](../../releases). They contain their own Python and every dependency —
nothing to install. The interface opens in your browser.

| File | For |
|---|---|
| `Munimentum-macos-arm64.dmg` | Mac with Apple Silicon (M1 or newer) |
| `Munimentum-windows-x64.zip` | Windows 10/11, 64-bit |
| `Munimentum-linux-x64.tar.gz` | Linux, 64-bit (glibc 2.35+) |

On macOS, open the disk image and drag the app to *Applications*; on Windows and
Linux, unpack the archive.

On Windows, SmartScreen asks once on first launch, because the build carries no
code-signing certificate: *More info* → *Run anyway*.

The app has no window of its own: it serves a page and lives in your browser.
Quit it with the power button at the top right. Starting it a second time does not
create a second copy — it opens the page of the one already running, which is
also the way back if you closed the tab.

---

## Data Ownership

A brief note on the ownership of the data that is exported: **whether you may
export it is yours to check.** Delegated access makes it technically possible;
establishing that it is permitted in your case is not something this app can do
for you.

---

## Where the data goes

Four places, deliberately separate. The app itself lives where the operating
system puts it. Its **app folder** is fixed and holds the profiles, one
folder each under `profiles/`:

* macOS: `~/Library/Application Support/Munimentum`
* Windows: `%LOCALAPPDATA%\Munimentum`
* Linux: `~/.local/share/Munimentum`

A **profile folder** holds the small things of one archive — settings,
access token, run history. The two heavy parts live below it by default and
can each point elsewhere in *Settings*: the **data folder** (`data/`, all
exports — a mailbox can take tens of gigabytes, a slow disk is fine) and
the **index folder** (`rag_store/`, hot random-access reads — keep it on a
fast disk). Changing a path moves nothing and takes effect after a restart:
**Munimentum never moves your data** — copy or move the folders yourself,
then set the paths. An archive from before the split keeps working
untouched: the app then points the data folder at the profile folder and
says so in the log. For a single run, `--data-dir FOLDER` and
`MUNIMENTUM_DATA_DIR` put everything into one folder.

**Every archive is a profile.** A profile is an archive of its own — its
own access, sources, rules, run history, data folder and index — in
`profiles/<name>/` below the app folder, and nothing crosses between
profiles. The first one is called `standard`; more than one account means
more than one profile, each created empty from *Settings › Profiles* and
set up like a first start: access, sources, build. With more than one
profile the app asks on start which archive to open — a small page before
the app, one card per profile — unless you tick *open without asking*,
which then opens the last one; `--profile NAME` (or `MUNIMENTUM_PROFILE`)
names it outright and skips the question. The header then names the open
profile, and a click on it opens the switch window: the other profile's
row restarts the app with it on the same port. A second profile started
while one is running takes the next free port, so two archives can be
open side by side. A profile that is not open can be renamed —
that is a folder rename, paths inside it follow. Names are short slugs
(`nordwind`, not an e-mail address) because they land in paths and in the
MCP snippet. Two profiles may never share a data or index folder — the
settings refuse it — and nothing is ever copied or moved between them.
Under `--data-dir` there is one archive and no profiles.

**Upgrading from 9.x or older:** an archive that sits in the app folder
itself is moved into `profiles/standard/` on the first start — a rename on
the same disk, instant whatever the size, all or nothing. Data or index
folders you pointed elsewhere stay where they are; folders inside the app
folder move along and the settings follow them. That is the one move this
app ever makes — and should a program hold a file open at that moment
(Windows refuses to move it then), that start runs the archive where it
is, without profiles, and the next start tries again. Claude Desktop users
copy the stdio MCP snippet again afterwards, because its paths changed.

---

## What it does

One browser page with three doors — **Build archive**, **Search archive**
and **Cases** — and two side rooms, *Insights* and *Settings*. The header carries nothing
else but one frame with the states — your access, a run while it is
minimised, and the open profile once there is more than one, a click on it
switches — and the power button to quit; it stays at the top while the
page scrolls.

### Build archive

The page opens with one line — how current the archive is — and one
button, *Update archive now*. An empty archive shows three steps instead:
set up access, choose sources, build — and offers a **tour**: coach marks
in the real interface that walk you through building the archive, setting
up a source in detail and searching, one element at a time; the result of
the first run offers the search chapter, and *Settings › App* starts any
chapter again. Below sit the sources as cards —
mail, calendar, contacts; 1:1, group, meeting and channel chats; OneDrive
files; SharePoint libraries and pages; Planner boards; To Do lists; OneNote
notebooks — each with its categories as chips to tick, an **(i)** that says
what is chosen and when it last ran, and a gear leading straight to its
settings. Nothing is preselected:
any one of these can mean tens of thousands of items. Every run asks
Microsoft only for what changed since the last one — mail folders,
calendars, contacts, chats, channels, lists and drives each keep a change
token — so the second run takes minutes rather than hours, and the index
that follows reads only the files that changed since its last run.
Deleted items **stay in the archive** and get a marker; that is the point of
keeping one. A running job opens its own window over the page: progress,
the steps, the log, *Cancel*. It stays until the run is done and shows the
result until you close it; *Minimise* turns it into a pill in the header,
so you can search meanwhile, and a click on the pill brings it back. Open
the page while a run is on — started by hand or by the schedule — and you
land in that window. Every run with its log is in Insights afterwards.

Every export keeps its bookkeeping in a single `state.db` inside its output
folder — change tokens, inventories, and for Teams the messages the HTML is
rendered from; the calendar rebuild keeps its own `kalender.db` next to the
mailbox's. Updating from 6.1 or older? Run the latest 6.x release once first —
it moves the older loose bookkeeping files into this format; 7.x and 8.x no
longer carry that migration.

Which folders come along is a list of ordered include/exclude rules, and *Show
export list* spells out what they currently mean: what comes along, what is left
out and why, and what is only in your archive because it is gone from the
source. Calendars work the same way — a mailbox usually carries birthdays,
holidays and calendars other people shared, so by default only your own comes
along until you say otherwise. So do Teams conversations and To Do lists:
*Sync team list* and *Sync lists* fetch the names, rules decide over
`1on1/<title>`, `group/<title>`, `meeting/<title>` and
`channels/<team>/<channel>`, or over the list titles, and *Show export
list* spells out the outcome. SharePoint libraries take path rules on top of
their URL list and type filters. A first export can start at a day of your
choosing — nothing older comes along for mail and chats; what is already
exported is not touched. A schedule can repeat the whole thing — the
mirrors and the pages export included, each with its own toggle — while the
app is open.

Not everything needs syncing every run: a **sync cadence** (always, daily,
weekly, monthly) can be set per part of a source — mail, calendar and
contacts each have their own, so do the four kinds of Teams conversations —
per source for OneDrive and To Do, and per URL or notebook for SharePoint,
Planner and OneNote. Libraries, pages and boards are configured as a small
table, one row per URL with its cadence and a *Sync now* button; OneDrive
has the same button in its settings. A single mail folder, team, channel,
chat, OneDrive or library folder or To Do list can depart from its
cadence: *Pick folders…*, *Pick teams and chats…* and *Pick lists…* open
the tree of the last list sync, drillable to any depth, and a value set
there reaches everything below it until a deeper one is set — an archive
folder monthly, one project folder inside it every run. In the mirrors that
paces the downloads: the listing stays one pass, and files in a folder not
yet due wait until it is. Cadences
apply to scheduled and manual runs alike; below its interval a part is
skipped with a clear log line, the export list shows the cadence each
folder or conversation has, and *Sync now* lets every gate of that run
step aside once.

Change tracking has one blind spot: a setting that only applies when a unit
is touched — attachments for Teams, channel files, Planner references — does
not reach conversations or boards that have not moved since. *Force full
sync*, under *Advanced* in every source's settings, closes it: the source
forgets its stored change pointers and is read again as on its first export,
everything fetched and written over, cadences stepping aside. Nothing in the
archive is deleted — what Microsoft no longer has stays, tombstone and all —
and it costs what a first export costs, so the button asks once.

**Mail, calendar and contacts** are read by change tracking: after the
first pass every folder, calendar and contact folder keeps a token, and the
next run receives only what was added, changed or removed — a deletion no
longer needs a check per mail, and a changed appointment or contact is
written again. The calendar is read as a window after the first export: a
number of months back that you set (one by default) plus everything ahead;
what was archived before stays, and *Read the calendar in full* in the
settings reads everything once, window or not.

**SharePoint libraries** mirror the document libraries behind the site or
folder URLs you list in the settings (sharing links work too; a folder URL
mirrors exactly that subtree) — same promises as the OneDrive mirror: the
current version of every file, deletions stay with a tombstone note. Because
team sites grow large, filters come along: only certain file extensions,
never certain ones, a size cap — and a **size preview** that enumerates
without downloading and tells you per library what a run would fetch, in
files and megabytes, before you commit. The first run walks the library
once, every later run asks Microsoft only for what has changed since — and
an interrupted run resumes where it stopped instead of starting over. A
changed selection — a rule, a size cap, a type filter — makes the next run
read that library (or the OneDrive) once in full, so what is newly included
arrives instead of waiting for the file to change.

**Planner boards** are archived one standalone `board.html` per plan:
buckets, cards with labels, assignees, checklists, descriptions — and the
comments, the legacy ones from the group conversation as well as the new
chat-based ones. Planner has taken no new legacy comments since February
2026, so after a board's first export they are kept as they are and no
longer checked; a button in the Planner settings, *Read legacy comments
again*, reads them once more, should someone still have replied from
Outlook. Chat comments carry no change signal of their own, so the export
re-reads them at an interval you set — a day by default. List the board
addresses in the settings, one per row with its
own sync cadence; a task that disappears from the board stays in the
archive, greyed, in a section of its own. Files a task references can be
downloaded next to the board on request — the cards then link the local
copies — since the library behind a board is rarely mirrored on its own.
Task texts, comments and attachment names are full-text searchable under
their own source. Reading needs Tasks.Read plus Group.Read.All for the
legacy comments; the token wizard lists both.

**Teams chats and channels** keep their messages in the export's
bookkeeping: a chat that moved fetches only the messages since its last
one, and a channel is read by change tracking instead of its whole history
on every run — replies on posts of the last two weeks are checked every
run, older threads once a week — and the HTML is rendered from what is
stored.

**Files shared in Teams** are, by default, links — into the sender's
OneDrive or the team's library, both gone with the access. Two switches in
the Teams settings, off until you turn them on, change that. *Download
shared files* fetches every file a chat or channel post references next to
its conversation and the HTML links the local copy, kept current by change
tag. *Mirror the channel folders* takes the Files tab of every exported
channel the way a SharePoint library is mirrored — one walk per team
library, current version of every file, deletions with a tombstone — and
channel posts then link into that mirror instead of fetching twice. A size
cap applies to both. The files land in the index by name, path and type
under the Teams source, and the file browser lists them per chat kind and
per team. Both need Files.Read.All; the token wizard lists it.

**To Do lists** — every list the account sees, own, shared and the
built-in ones, or the ones your rules pick — one folder per list with a
standalone `list.html`: open and completed tasks with their steps, due
dates, reminders, recurrence, notes, linked resources and attachments,
downloaded next to the list. A task that leaves a list stays, greyed. Each
list keeps a change token, so a run fetches only the tasks that moved.
Title, notes, steps and linked resources are full-text searchable under
their own source, the list being the folder. Needs Tasks.Read.

**OneNote notebooks** are exported page by page: one folder per notebook,
the section groups and sections below it, one standalone HTML per page as
Graph renders it — images embedded up to a configurable size, larger ones
and every attachment in a folder next to the page — so each page opens
offline. Only pages that changed are fetched again — images and
attachments already on disk are reused, which spares the hourly budget —
and a page that leaves its section keeps its file with a marker at the top.
Which notebooks come
along works like the mailbox folders: *Sync notebook list* fetches the
list, ordered include/exclude rules decide over it (empty means every
notebook), and *Show export list* spells out what comes along, what is
left out and why, and what is only in the archive because it is gone. Each
notebook has its own sync cadence and a *Sync now* button in the settings.
The notebook, section group and section are the folder filter; the page
text is full-text searchable. One thing to know: OneNote allows 400
requests an hour per user, and a page costs one plus its images — so the
export paces itself below that limit, stops a run cleanly when the hour is
spent and continues with the next run, which fetches only what never
arrived. The bookkeeping is written page by page, so a cancelled run keeps
every page that arrived too, and a run that would start into a spent hour
says so and waits instead of collecting refusals. A first export of a large
notebook therefore takes several runs; that is the API's limit, not a
fault. The OneDrive mirror backs up the raw
`.one` files as well — this export is what makes them readable. Needs
Notes.Read.

**SharePoint pages** are a separate export with their own settings section
and URL list: the modern pages (news included) of the listed sites and all
their subsites, rendered to standalone HTML — text kept, images embedded up
to a configurable size, other web parts as named placeholders. Unlike the
mirrored files, the page text itself lands in the index and is full-text
searchable. Both SharePoint exports need the Sites.Read.All permission; the
token wizard lists it.

### Search archive

Three kinds of search, chosen in the search field. **Text search** is the default
and always available: it finds the words that actually occur, ranked by
relevance. **Similar search** finds related wording even when your words do not
appear. **AI answer** answers a question in a paragraph with source numbers and
keeps the underlying hits one click away.

Filters — person, source, date range, folder, file type, and messages no longer
in the mailbox — sit in a row below the field, a set one highlighted where it
stands, and nothing searches until you ask for it. The
person field suggests names that actually occur, so a typo is not mistaken for
an absence, and `*` stands for any run of characters when one name is too
narrow. Picking a source narrows what the other two offer: calendars for
the calendar, the four kinds of Teams conversation for Teams, attachment types
for mail, and nothing at all where there is only one thing to choose from. The
hits stand on the left; a chosen one opens on the right with its origin, the
original file, the whole conversation it belongs to, *Find similar*, and a
link to everything with that person — the archive's own HTML shown in place,
for everything else the excerpt the index holds.

Four views live here: results, calendar (including appointments recovered
from invitation and cancellation mails), the address book, and a **file
browser** that walks the mirrored drives — OneDrive, each SharePoint
library, the files next to Teams conversations and the Planner boards —
folder by folder, straight from the index: originals one click away,
deleted files marked, and *Search here* turns the current folder into a
search filter. In the filters, OneDrive, SharePoint files, SharePoint
pages, Planner, To Do and OneNote are each their own source, and their
folder filter lists whole units — a board, a list, a notebook, a library,
a site — rather than every sub-path; the Teams source covers the
conversations and the files shared in them alike.

The last two kinds of search need [Ollama](https://ollama.com). Without it they
are visibly switched off rather than hidden, and everything else works
unchanged.

Two buttons next to *Search* keep what you searched for. **History** lists
every search by its criteria — words, kind of search, filters — never by
its hits; one click runs it again, another saves it. The setting under
*Settings › App* says how long it is kept (30, 90, 365 days, forever, or
not at all); the choice applies at once. **Saved** holds searches under a
name: run one any time, rename it, delete it, or attach it to a case, so
the case can tell you what the search finds today that it does not hold
yet. A saved search remembers when it last ran and how many hits it had.

Every hit carries a tick and, when it already sits in a case, a small
mark naming the case; the chosen hit offers *Add to case* beside its other
actions, the ticked ones a bar above the list, and *Add all … to a case*
takes the whole result — every page of it. The **Cases** filter in the
filter row — there once a case exists — searches only inside one case,
across every source; *Search in this case* on a case sets it for you.

### Cases

A case collects what belongs to one matter — hits, whole result lists and
saved searches, from every source — without copying anything: the case
points at items in the archive by a key that survives a rename or a
move (the mail's Message-ID, the file's drive item, the Teams message,
the task); an index from before 11.0 is rebuilt with those keys on the
next run, whatever the exports brought. The door
lists the cases, open ones first; a case opens on the right with its
description, the **casebook** (short dated notes: what was found, what is
open, what was decided), the **result lists** it stores — a search as it
stood at one moment, criteria and exactly the hits of that moment, which
count as items of the case and can be searched again —, its items grouped
by source with a link into the original, and the **saved searches**
attached to it: *Check for new hits* runs them and shows what the case
lacks, *Add the new ones* takes them in. Nothing is added on its own.

*Close case* makes a case read-only — it stays in the list, can still be
searched in and exported, and reopens any time; *Delete case* drops the
case, its items, lists and notes and detaches its searches, and touches
nothing in the archive. **Export case…** is a run like any other: the
run window opens with it and its log says what was copied. It writes one
folder — `<case>_<date>` under *Settings › App › Case exports go to*, by
default *Munimentum cases* in your Documents folder — with the originals
of every item under their source's name and path (mails as `.eml`, the
chat's HTML with its attachments, files, pages and boards as HTML,
contacts as `.vcf`, appointments as `.ics`), an `index.html` that lists
every item with source, date, people and a link that lands on the
message, card or task, the list as `items.csv`, and `casebook.md` with
the notes; optionally the whole folder as a zip beside it. An item the
archive no longer holds is listed all the same, marked. The case remembers
where its last export went; *Show export folder* opens it.

### Insights

What the archive holds, computed once per index run without asking
Microsoft, so the page opens instantly — with a side navigation like the
settings', one entry per card, and a dot on the two checks that says at a
glance whether something is open or found. Communication and files are kept
apart: messages, conversations, people and period on one row; the mirrored
files, pages and disk usage on their own. The timeline covers mail and chat
only — a mirrored PDF must not fill a communication gap — and **gaps**,
months with no message at all between your first and your last, are named
outright, which is the one question an archive should answer about itself.
Below that: attachments by type, the mirrored files by type, the largest
single files, and who you exchange the most with.

On request there is also a **completeness balance** against Microsoft.
It answers one question: would a run with today's settings fetch anything
now that is not here yet? One row per source in use — mail, calendar,
contacts, Teams, OneDrive, SharePoint libraries and pages, Planner, To Do,
OneNote — with four numbers in plain words: here, not fetched yet,
deliberately excluded, and deleted at Microsoft but kept. Excluded is what
your own rules, filters and start days leave out, counted, never named; a
file waiting for its folder cadence waits rather than counting as missing;
Teams is judged by conversations, since Graph counts no messages. A row
with something open offers *Fetch now*, which fetches only what the row
found open, the cheapest way the source allows: the mailbox reads just
the folders with something open in full and fetches the mails it lacks —
26 open mails in nine folders cost nine listings, not the whole mailbox;
OneDrive and SharePoint fetch the open files one by one through the ids
the check noted, without walking the library again; every other source
runs its regular run, which fetches what changed anyway. Then the index
runs over what arrived and the row is checked again in the same run, so
the result shows without a second click.
The check asks Microsoft for the state of now and
changes nothing; large libraries take a while, and OneNote's hourly budget
applies, in which case the row says "not checked" instead of inventing a
gap.

The second look points inward: **Archive and bookkeeping** holds each
export's own `state.db` against the files on disk without asking anyone —
a file the bookkeeping knows that is not there (*Fetch again* brings it
back), a file here that no bookkeeping knows (it stays), a mirrored file
shorter than recorded, a file a tombstone lists as kept that is gone — and
the index against the archive: how many files changed, arrived or vanished
since it last read them, with *Index only* one click away. It repairs,
moves and deletes nothing on its own; it says where a look is worth it,
and offers one explicit action per kind of finding: *Findings…* lists the
files behind the numbers, to copy or to open the folder; *Fetch again*
fetches exactly the missing and incomplete files, each through the
source's own bookkeeping — a mail by the id the resume log keeps, a
mirrored file by its drive item, a Teams page by its conversation, a
board, list or notebook by the unit that holds it — whether or not the
source is ticked in the settings, without listing a folder or writing
anything current over; the run ends by judging the row afresh, so a file
Microsoft no longer has is named in the log and the row then says "still
missing after a fetch"; *Note as lost* records a tombstone whose file is
gone, the tombstone itself stays — and settles those stubborn files the
same way: the bookkeeping keeps the entries, the check stops counting
them, and a file that does come back counts as agreeing again; *Set
aside* moves files no
bookkeeping knows into a `_fremd/` folder inside the export folder, path
kept, with a list of what moved, and *Put back* reverses it — deleting is
yours to do in the file manager; *Rebuild bookkeeping* sets a damaged
`state.db` aside under a dated name, salvages what it still yields and
lets the next run fill a fresh one. Every action asks before it moves
anything, none deletes, and each is a run of its own: the run window
opens with it and its log says what moved — as it does for the checks,
*Fetch now* and *Fetch again*. Only the balance's *Fetch now* (a resync
of a source in use) is refused, with a message naming the source, when
that source is not ticked in the settings.

**Runs** keeps the history of every export: when it ran, scheduled or by hand,
which elements were enabled, how long each step took and what it produced —
with the new pieces broken down by source on hover, and each run's full log
stored alongside, shown inline on demand. The history lives in a small
database next to the exports; how long the run rows and the log lines are
kept are two separate settings (24 months and 14 days by default).

### Settings

A navigation on the left — Sources, Schedule, AI (Ollama), Claude (MCP),
Profiles, App, Expert mode — and one card per topic on the right.
*Profiles* lists the profiles with their accounts, switches, creates and
renames them, and holds the folders of the current profile below. Each
source
is a block with its essentials open and everything else under *Advanced*;
the gear on the archive page leads straight to it. Each setting is one line
with an **(i)** that explains what it does and what happens if you change
it, and the save bar appears only once something is unsaved. The **AI**
card holds everything that needs a local model server — currently Ollama —
and it has a switch of its own: turned off, the app stops looking, the index
is built as full text only, and the dot next to *AI (Ollama)* in the
navigation says so. Next to the address and the two model names, a small
indicator says whether each is actually there.

**System notifications** can report the end of a run through the operating
system — useful when the schedule exports with no tab open. On macOS and
Windows, clicking one opens the interface; on Linux they go through
`notify-send`, plain. By default only failures and an expired access key are
reported; "all runs" and "off" are a setting away. Everything stays on the
machine.

The **App** card also holds how long the search history is kept and
where case exports land; the **Claude (MCP)** card has *Claude may change
cases*, off by default — see [Search with Claude](#search-with-claude).

**Keep awake during a run** holds the machine off idle sleep while an
export or index runs — macOS through `caffeinate`, Windows through a power
request, Linux through `systemd-inhibit` — and lets go the moment the run
ends. On by default, because a laptop that dozes off leaves the run
hanging until someone comes back; the display may still go dark, and a
closed lid still sleeps.

An **Expert mode** card at the end collects what almost nobody needs day to
day: how many requests run in parallel, running the index or the calendar
rebuild as a single step, and the complete HTTP API of the interface as an OpenAPI description — for scripts
that talk to the backend directly, on `127.0.0.1` only, like everything else.

The log sits in the run window, and it speaks the interface language: the
exports report events, the app puts them into words; afterwards every
run's log is in Insights. *Copy* puts the log on the clipboard;
*Report a problem* — there and under *Settings → App* — opens the
matching GitHub issue
form with description, system details and log filled in — including which
settings differ from their defaults (rules and name lists only as their size,
paths not at all) and, if enabled, the kind of your last steps in the
interface (tab, search, run — never content). The app sends nothing itself —
addresses and user names in
paths are replaced, the whole text is shown for editing, and you submit the
form.

---

## Signing in

Two ways, chosen in the assistant. Pasting a key stays the default.

| | Needs | Lasts |
|---|---|---|
| **Access key** | nothing but the [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer) | a few hours, then paste again |
| **Sign in** | one sign-in, optionally your own app registration | weeks — the schedule keeps running unattended |

Signing in stores a refresh token on this machine, readable only by you; *Sign
out* deletes it. Your password is never seen. Without your own registration it
uses Microsoft's public *Graph Command Line Tools* application, which is
pre-approved in almost every tenant.

---

## Search with Claude

A built-in MCP server hands the archive to Claude Code or Claude Desktop — or
any other MCP client: it searches, browses the mirrored drives, reads the
sources and answers with citations
— over your own mail and chats, not over the open web. Every source is
reachable the same way — mail, Teams and the files shared there, calendar,
contacts, OneDrive and SharePoint files, SharePoint pages, Planner boards,
To Do lists, OneNote notebooks — with the same filters the search page
offers, and the server says up front what each source holds: files by name,
path and type only, since their contents are not indexed. Appointments come
structured, including the ones recovered from mails; the address book can
be looked up; the files next to Teams conversations and the Planner
attachments can be browsed. It can
also ask the archive about itself — how far it reaches, which months are
empty, when each source last synced, and the same figures Insights
shows — so an answer can say what the archive does not cover instead of
guessing. Cases and saved searches are there too: `list_cases` and
`get_case` give a case with its casebook, items, lists and searches,
`case_timeline` its items in the order they happened with excerpts,
`case_people` who is involved, `case_new_hits` what the attached searches
find that the case lacks, `list_saved_searches` and `run_saved_search`
run a search exactly as saved, and every search or browse takes a `case`
to look only inside one — every hit says which cases it already sits in.
Claude reads cases but changes nothing unless *Claude may change cases*
is on under *Settings › Claude (MCP)*: then `add_to_case` and
`add_case_note` can put items and notes into open cases, never delete or
close one. *Settings* prints the exact snippet to paste into your client.
The stdio snippet names nothing but the profile — `--profile <name>` —
and the server takes folders, model, Ollama address and port from that
profile's settings, so nothing in the snippet goes stale. Its entry is
named after the profile — `munimentum` for the first, `munimentum-<name>`
for every other — so a client can hold two archives apart; a server
started without the flag where several profiles exist serves nothing but
a sentence saying so, and so does a stdio entry from before 10.0. The
HTTP endpoint is the app's and serves whichever profile is open, so its
entry stays `munimentum`.

There are two routes, and the app controls them differently. It runs the **HTTP
endpoint** itself; *Start* / *Stop* and the autostart apply to that one. A client
can also launch the server **as a subprocess** (stdio) — that is how Claude
Desktop does it, and it works whether or not this app is running. One switch
covers both: *Allow MCP access*, off, makes the server refuse to serve either
way.

> The server serves your complete mail and chat history and has **no
> authentication**. It binds to `127.0.0.1` only and checks the `Host` and
> `Origin` headers, so a web page you happen to visit cannot reach it through
> your browser. Leave it that way.

---

## Optional: Ollama

Without [Ollama](https://ollama.com), Munimentum exports, indexes and searches
by text — that is the whole app minus two features. With it, similar search and
the AI answer become available, both running on your machine; nothing is sent
anywhere. The app offers to help you install it, and you can switch it off for
good in *Settings* if you would rather not.

---

## From source

Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate          # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python3 app.py                      # or: python3 app.py --profile nordwind
```

That is the whole app — the project folder is its app folder then, and
the profiles land in `profiles/` below it, which git ignores. The
export and index scripts are its subprograms: the
app starts them itself and hands them their settings, and they never ask
questions of their own. Each still explains itself with `--help`; how the
pieces fit together is written in their headers rather than repeated here.

Bug reports are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the fastest
way to send one, and [SECURITY.md](SECURITY.md) if it is a security issue.
