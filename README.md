# Munimentum

Your own Microsoft 365 data, kept where you can reach it: Teams chats and
channels with the files they share, Outlook mail, calendar, contacts,
OneDrive files, SharePoint libraries and pages, Planner boards, To Do lists
and OneNote notebooks — exported through Microsoft Graph and searchable
offline, in the app or through Claude via MCP.

**The magic:** all via delegated access, no admin consent required.

> **munimentum** *(Latin)* — a rampart; and, in medieval usage, a deed: the
> document you keep because it is the proof of what is yours. This is a
> walled place for records that would otherwise live only in someone else's
> cloud, and that you would have a hard time getting at once your access ends.

Nothing leaves your machine except the calls to Microsoft Graph — see
[PRIVACY.md](PRIVACY.md). Whether you may export the data is yours to check:
delegated access makes it possible, not permitted.

---

## Download and run

Bundles for macOS, Windows and Linux are attached to every
[release](../../releases). They contain everything — nothing to install.

| File | For |
|---|---|
| `Munimentum-macos-arm64.dmg` | Mac with Apple Silicon (M1 or newer) |
| `Munimentum-windows-x64.zip` | Windows 10/11, 64-bit |
| `Munimentum-linux-x64.tar.gz` | Linux, 64-bit (glibc 2.35+) |

On macOS drag the app to *Applications* – the disk image's window names
the version it carries; on Windows and Linux unpack the
archive. Windows asks once on first launch because the build carries no
code-signing certificate: *More info* → *Run anyway*.

The app has no window of its own: it opens a page in your browser. Quit it
with the power button at the top right; starting it again while it runs
just reopens that page.

## Signing in

Two ways, chosen under *Settings › Microsoft Access*; the first start
points there.

| | Needs | Lasts |
|---|---|---|
| **Access key** | nothing but the [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer) | a few hours, then paste again |
| **Sign in** | one sign-in, optionally your own app registration | weeks — the schedule keeps running unattended |

Signing in stores a refresh token on this machine, readable only by you;
*Sign out* deletes it. Your password is never seen.

---

## What it does

One page with three doors — **Build archive**, **Explore archive** and
**Cases** — and two side rooms, *Insights* and *Settings*. **Help** in the
header opens a tour: coach marks in the real interface, six chapters —
the archive, a source's settings, the search, cases, Insights, Claude.

### Build archive

One line says how current the archive is, one button updates it. Below sit
the sources as cards; tick what should come along — nothing is
preselected, because any one source can mean tens of thousands of items.

| Source | What comes along |
|---|---|
| **Mail, calendar, contacts** | every folder you choose as `.eml`, `.ics`, `.vcf`; the calendar as a window of months back plus everything ahead |
| **Teams** | 1:1, group, meeting and channel chats as readable HTML; on request the files shared in them and the channels' file folders |
| **OneDrive** | a mirror of your files, current version of each |
| **SharePoint** | libraries behind the URLs you list, with type filters, a size cap and a size preview; the sites' pages as standalone HTML |
| **Planner** | one `board.html` per plan: buckets, cards, checklists, comments; referenced files on request |
| **To Do** | one `list.html` per list: open and done tasks with steps, dates, notes, attachments |
| **OneNote** | every page as standalone HTML with its images and attachments, notebook by notebook |

Every run asks Microsoft only for what changed since the last one, so the
second run takes minutes rather than hours. Deleted items stay in the
archive with a marker — that is the point of keeping one. A running job
opens its own window with progress, steps and log; *Minimise* turns it into
a pill in the header so you can search meanwhile.

Which folders, chats, lists and notebooks come along is a short list of
include and exclude rules per source; *Show export list* spells out what
they mean before a run. A first export can start at a day of your choosing.
A **schedule** repeats the run while the app is open, and a **sync
cadence** — always, daily, weekly, monthly — can be set per source, per
part of it, or for a single folder, chat or list. *Force full sync* under
*Advanced* reads a source again as on its first export when a changed
setting must reach what is already archived. Nothing in the archive is
ever deleted by a run.

### Explore archive

Four ways into the archive, one strip right under the header: **Search**,
**Calendar**, **Contacts** and **Files**. Only Search searches; the other
three have their own controls and each carries one button that hands over
to Search with its source set — the calendar with the shown month or week
as the date range, the file browser with the folder you are in.

**Search.** Three kinds, chosen in the field: *Text search* finds the words
that actually occur, any of them, the best matches first — a phrase in
quotes, `"budget frame"`, must occur as it stands; *Similar search* finds
related wording; *AI answer* answers a question in a paragraph with source
numbers. The last two need [Ollama](#optional-ollama). Filters — person,
source, date range, folder, file type, case, internal or external parties,
items no longer at Microsoft, and, with *Mail*, only mails with an
attachment — are a row of pills; a pill opens a small
window with its control, a set pill carries its value and a × that clears
it, and nothing searches until you press *Search*. One of them is the
**mail filter**: *From*, *To*, *Cc* and *Bcc*, each with the addresses the
archive actually holds. It stands there only while the source is *Mail* —
a line only mail has would otherwise quietly turn every search into a
mail search — and only once the index knows those lines: the run after an
update writes them. Bcc exists only in mail you sent yourself, and the
hit's detail shows it wherever it is there. Before the first search
your last searches and the saved ones stand as rows under the field, one
click runs them. A paperclip on a hit marks a mail with attachments. A
chosen hit opens on the right with the facts its kind is
known by — a mail its from, to, date, folder and attachments, each one a
download; a file its
type, size and place; a task its board, due date and state — then *Open
original*, *Add to case*, *Find similar*, and the content; a mail or chat
message names its conversation in one fold below. **History** keeps every
search by its criteria, never its hits; **Saved** keeps searches under a
name and can attach one to a case.

**Calendar** shows the exported appointments by week or month — the
month's name opens a picker over the years the archive spans, *Today* leads
back; appointments recovered from invitation and cancellation mails sit in
the same grid, marked with the mail they came from. **Contacts** merges
the Outlook address book with everyone found in the communication.
**Files** walks the mirrored drives folder by folder: originals one click
away, deleted files marked; *Search all files* at the sources searches
every mirror at once.

### Cases

A case collects what belongs to one matter — hits, whole result lists and
saved searches, from every source — without copying anything: it points at
items in the archive by a key that survives a rename or a move. Every hit
carries a tick; the chosen one offers *Add to case*, the ticked ones a bar
above the list, and *Add all … to a case* takes the whole result. The
window that adds anything asks for a folder in the case and offers the
rest of a mail's or chat's conversation.

A case has a **casebook** of short dated notes, **folders** one level deep,
a **filter field**, and three views: *Folders*, a *Timeline* of every item
and note in the order they happened with the case's activity as a band
above, and *People* — everyone the items name, by their last contact, with
*external* marked for addresses outside your organisation. Every item can
carry a **remark** on why it is there. A saved search attached to the case
can say what it finds today that the case lacks.

**Export case…** writes one ZIP with the originals of every item sorted by
folder and source, an `index.html`, a `timeline.html`, `items.csv` and the
casebook — a run like any other, under the folder you set in *Settings ›
App*. *Close case* makes it read-only and keeps it; *Delete case* drops
the case and touches nothing in the archive.

### Insights

What the archive holds, computed once per index run: messages, people and
period, the mirrored files, disk usage, and the **gaps** — months with no
message at all. On request a **completeness balance** against Microsoft:
per source, what is here, not fetched yet, deliberately excluded, and
deleted at Microsoft but kept; *Fetch now* fetches only what a row found
open. **Archive and bookkeeping** checks each export against the files on
disk and the index against the archive without asking anyone, and offers
one explicit action per finding — fetch again, note as lost, set aside,
rebuild bookkeeping; every action asks first and none deletes. **Runs**
keeps every run with its log.

### Settings

Microsoft Access, Sources, Schedule, AI (Ollama), Claude (MCP), Profiles,
App, Expert mode — one card each on one running page, the list beside it
jumps to the one you want, and every setting carries an **(i)** that says
what changes. The
App card holds system notifications, how long the search history is kept,
where case exports land, your organisation's mail domains (what *external*
means) and your name, and *Report a problem*, which fills in a GitHub
issue with the log — addresses and user names replaced, shown for you to
edit, sent by nobody but you. *Keep awake during a run* holds the machine
off idle sleep while an export runs. *Expert mode* collects what almost
nobody needs day to day, among it the complete HTTP API of the interface
as an OpenAPI description (`openapi.yaml`, served at `/api/v1/openapi`).
That is `/api/v1`, the versioned surface meant for scripts of your own,
with the usual courtesies — a case carries an `ETag`, a write can be
asked for the status alone (`Prefer: return=minimal`) —
since 13.0 the whole app runs on it, the page included, and the App card
shows its version beside the program's.

---

## Search with Claude

A built-in MCP server hands the archive to Claude Code, Claude Desktop or
any other MCP client — on this machine only: it searches every source with
the same filters the search page offers, the mail lines among them, browses
the mirrored drives, reads the sources and answers with citations. An item
it opens comes with its facts already extracted — a mail's from, to and
attachments, an appointment's time and attendees, a task's due date and
state. It can ask the archive about itself — how far it reaches, which
months are empty, when each source last ran and whether that run worked,
and what a completeness check found missing against Microsoft — and read
your cases and saved searches; it changes a case only when *Claude may
change cases* is on under *Settings › Claude (MCP)*, and then only adds,
marked *via MCP*. *Settings* prints the exact snippet for your
client; the HTTP endpoint is started from the app, the stdio route lets a
client launch the server itself, and *Allow MCP access* switches both.

> The server has **no authentication**. It binds to `127.0.0.1` only and
> checks the `Host` and `Origin` headers, so a web page you visit cannot
> reach it. Leave it that way.

## Optional: Ollama

Without [Ollama](https://ollama.com) the app exports, indexes and searches
by text — everything but two features. With it, *Similar search* and the
*AI answer* become available, both running on your machine. The app offers
to help you install it, and the switch under *Settings › AI* turns it off
for good if you would rather not.

---

## Where the data goes

The app folder is fixed — macOS `~/Library/Application Support/Munimentum`,
Windows `%LOCALAPPDATA%\Munimentum`, Linux `~/.local/share/Munimentum` —
and holds one **profile** per archive under `profiles/`, the first one
called `standard`: its settings, access token and run history, and below
it the **data folder** (all exports; a slow disk is fine) and the **index
folder** (keep it on a fast disk), each of which can point elsewhere in
*Settings*. Changing a path moves nothing: **Munimentum never moves your
data** — copy or move the folders yourself, then set the paths.

More than one account means more than one profile, created from
*Settings › Profiles*; the app then asks on start which archive to open,
and the header names the open one. Nothing crosses between profiles.
Delete the folders and nothing of the app remains.

## From source

Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate          # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python3 app.py                      # or: python3 app.py --profile nordwind
```

The project folder is the app folder then. Bug reports are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the fastest way to send one, and
[SECURITY.md](SECURITY.md) if it is a security issue.
