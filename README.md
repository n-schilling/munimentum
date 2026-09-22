# Munimentum

Your own Microsoft 365 data, kept where you can reach it: Teams chats and
channels with the files they share, Outlook mail, calendar, contacts,
OneDrive files, SharePoint libraries and pages, Planner boards, To Do lists
and OneNote notebooks — exported through Microsoft Graph and searchable
offline, in the app or through Claude via MCP.

**The magic:** all via delegated access, no admin consent required.

> **munimentum** *(Latin)* — a rampart; and, in medieval usage, a deed: the
> document you keep because it is the proof of what is yours. A walled
> place for records that would otherwise live only in someone else's cloud.

Nothing leaves your machine except the calls to Microsoft Graph — see
[PRIVACY.md](PRIVACY.md). Whether you may export the data is yours to check:
delegated access makes it possible, not permitted.

## Download and run

Bundles are attached to every [release](../../releases) — nothing to
install: `Munimentum-macos-arm64.dmg` for Macs with Apple Silicon,
`Munimentum-windows-x64.zip` for Windows 10/11, `Munimentum-linux-x64.tar.gz`
for Linux (glibc 2.35+).

On macOS drag the app to *Applications*; on Windows and Linux unpack the
archive. Windows asks once on first launch, because the build carries no
code-signing certificate: *More info* → *Run anyway*. The app has no window
of its own — it opens a page in your browser, and the power button at the
top right quits it.

## Signing in

Two ways, chosen under *Settings › Microsoft Access*; the first start
points there. Signing in stores a refresh token on this machine, readable
only by you; *Sign out* deletes it, and your password is never seen.

| | Needs | Lasts |
|---|---|---|
| **Access key** | nothing but the [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer) | a few hours, then paste again |
| **Sign in** | one sign-in, optionally your own app registration | weeks — the schedule keeps running unattended |

## What it does

One page with three doors — **Build archive**, **Explore archive** and
**Cases** — two side rooms, *Insights* and *Settings*, and **Help**, which
walks you through the real interface in six chapters.

### Build archive

One line says how current the archive is, one button updates it. Below sit
the sources as cards; tick what should come along — nothing is preselected,
because any one source can mean tens of thousands of items.

| Source | What comes along |
|---|---|
| **Mail, calendar, contacts** | every folder you choose as `.eml`, `.ics`, `.vcf`; the calendar as a window of months back plus everything ahead |
| **Teams** | 1:1, group, meeting and channel chats as readable HTML; on request the files shared in them and the channels' file folders |
| **OneDrive** | a mirror of your files, current version of each |
| **SharePoint** | libraries behind the URLs you list, with type filters, a size cap and a size preview; the sites' pages as standalone HTML |
| **Planner** | one `board.html` per plan: buckets, cards, checklists, comments; referenced files on request |
| **To Do** | one `list.html` per list: open and done tasks with steps, dates, notes, attachments |
| **OneNote** | every page as standalone HTML with its images and attachments, notebook by notebook |

Every run asks Microsoft only for what changed, so the second takes minutes
rather than hours. Deleted items stay in the archive with a marker — that
is the point of keeping one — and nothing in it is ever deleted by a run. A
running job opens its own window with progress, steps and log. Which
folders, chats and lists come along is a list of include and exclude rules
per source, spelled out by *Show export list*; a **schedule** repeats the
run, and a **sync cadence** — always, daily, weekly, monthly — can be set
per source, per part of it or for a single folder.

### Explore archive

Four ways in, one strip under the header: **Search**, **Calendar**,
**Contacts** and **Files**. Only Search searches; the others hand over to
it with their source, month or folder set.

**Search.** Three kinds, chosen in the field: *Text search* finds the words
that actually occur, best matches first — a phrase in quotes must occur as
it stands; *Similar search* finds related wording; *AI answer* answers a
question in a paragraph with source numbers. The last two need
[Ollama](#optional-ollama). Filters are a row of pills: person, source,
date, folder, file type, case, internal or external parties, items no
longer at Microsoft, mails with an attachment, and the lines *From*, *To*,
*Cc*, *Bcc* for mail. Nothing searches until you press *Search*.

A chosen hit opens on the right with the facts its kind is known by — a
mail its lines, folder and attachments, each a download — then *Open
original*, *Add to case*, *Find similar* and the content, the rest of a
conversation in a fold below, each message a click away. **History** keeps every search by its
criteria, never its hits; **Saved** keeps them under a name, with the case
a search files into and whether it collects by itself.

A result has **three views**: *List*, the page the search answered;
*Timeline*, every hit in the order it happened under a band of months that
narrows the rows to one; and *People*, everyone the hits name by their last
contact, *external* marked. The last two take the whole result.

**Calendar** shows the appointments by week or month, those recovered from
invitation mails among them. **Contacts** merges the Outlook address book
with everyone found in the communication, as a list or as that picture.
**Files** walks the mirrored drives, originals one click away.

### Cases

A case collects what belongs to one matter — hits, whole result lists and
saved searches, from every source — without copying anything: it points at
items by a key that survives a rename or a move. Every hit carries a tick;
the chosen one offers *Add to case*, the ticked ones a bar above the list,
and *Whole result into a case…* takes all of them.

A case has a **casebook** of short dated notes, **folders** one level deep,
a filter field, three views — *Folders*, *Timeline*, *People* — and a
**remark** on every item saying
why it is there. A saved search attached to it says what it finds today
that the case lacks — or, switched to **automatic**, files that by itself
with every run that updates the index. **Export case…** writes one ZIP with
the originals sorted by folder and source, an `index.html`, a
`timeline.html`, `items.csv` and the casebook. *Close case* makes it
read-only; *Delete case* touches nothing in the archive.

### Insights

What the archive holds, computed once per index run: messages, people and
period, the mirrored files, disk usage, and the **gaps** — months with no
message at all. On request a **completeness balance** against Microsoft for
the sources you tick: what is here, still open, excluded, deleted but kept,
and what Microsoft refuses to hand out; *Fetch now* fetches what a row
found open. **Archive and bookkeeping** checks each export against the
files on disk, asking nobody, and offers one action per finding — each asks
first, none deletes. **Runs** keeps every run with its log.

### Settings

Microsoft Access, Sources, Schedule, AI (Ollama), Claude (MCP), Profiles,
App, Expert mode — one card each on one running page, every setting with an
**(i)** that says what changes. The App card holds notifications, how long
the search history is kept, where case exports land, your organisation's
mail domains (what *external* means), and *Report a problem*, which fills
in a GitHub issue with the log — addresses and user names replaced, shown
for you to edit, sent by nobody but you.

## Search with Claude

A built-in MCP server hands the archive to Claude Code, Claude Desktop or
any other MCP client — on this machine only. It searches every source with
the same filters the search page offers, browses the mirrored drives and
answers with citations; it can ask the archive about itself — how far it
reaches, which months are empty, when each source last ran — and read your
cases. It changes one only when *Claude may change cases* is on under
*Settings › Claude (MCP)*, and then only adds, marked *via MCP*.
*Settings* prints the exact snippet for your client.

> The server has **no authentication**. It binds to `127.0.0.1` only and
> checks the `Host` and `Origin` headers, so a web page you visit cannot
> reach it. Leave it that way.

## Optional: Ollama

Without [Ollama](https://ollama.com) the app exports, indexes and searches
by text. With it, *Similar search*, the *AI answer* and the proposed name
for a saved search become available, all on your machine. The app offers to
help you install it; *Settings › AI* turns it off for good.

## Where the data goes

The app folder is fixed — macOS `~/Library/Application Support/Munimentum`,
Windows `%LOCALAPPDATA%\Munimentum`, Linux `~/.local/share/Munimentum` —
and holds one **profile** per archive under `profiles/`: its settings,
access token and run history, and below it the **data folder** (all
exports) and the **index folder** (keep it on a fast disk), each able to
point elsewhere in *Settings*. Changing a path moves nothing: **Munimentum
never moves your data** — copy the folders yourself, then set the paths.
More than one account means more than one profile, created from *Settings ›
Profiles*; the app asks on start which to open, nothing crosses between
them, and deleting the folders leaves nothing behind.

## From source

Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate          # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python3 app.py

python3 -m testdata.build --profile testdaten   # an invented archive…
python3 app.py --profile testdaten              # …to look at without an account
```

Bug reports are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md), or
[SECURITY.md](SECURITY.md) for a security issue.
