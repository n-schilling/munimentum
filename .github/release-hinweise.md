## New in 5.3.0

**The header says what is actually on.** It used to read *Claude access off* —
wrong twice over. MCP is a protocol, not one program; and what the app switches
was only its own HTTP endpoint, while a client that launches the server itself
kept full access. Three states now, and the transport is named only when it is
the only thing missing: **MCP on**, **MCP HTTP off**, **MCP off**.

**Moved mail is no longer counted as deleted.** *Deleted* showed messages that
had merely been dragged into another folder — in one real archive, 16 of 19
entries were wrong. Exchange gives a message a new id when it moves, so asking
Microsoft about the old one answers "not found", and the export drew the wrong
conclusion. It now compares the message id from the mail header instead, which
survives a move and comes along with the folder listing at no extra cost.

Old entries repair themselves, but not by themselves: *run one export with
**Mail** ticked*. That withdraws the wrong markers — the log says how many — and
the index run that follows carries the correction into search and analytics.
Indexing alone does not do it; the markers are written during the export. A run
in which a folder could not be listed completely skips the check entirely, on
purpose: better no statement than a wrong one.

**macOS no longer asks about the local network.** On first launch, macOS 15
asked whether Munimentum may look for devices on local networks — a question
this app has no business raising: it listens on 127.0.0.1 and otherwise talks
only to Microsoft Graph. The cause was in Python's HTTP server, which resolves
a name for its own address while binding; the result is never used. It no
longer does, and startup is a fraction faster for it.

**The AI tile says on or off, and the settings say why.** It used to read *AI
search ready* / *model missing* / *off* — three labels for one question, none of
them saying what to do. It now reads **AI on** or **AI off**, with the reason in
the tooltip; and clicking it goes to the settings, where the address, the
embedding model and the answer model each carry a small indicator of their own:
reachable, loaded, not loaded. That replaces a window which explained what was
missing but let you change nothing. The section itself is now called **AI**
rather than *Ollama* — Ollama is what runs the models today, not the only thing
that could.

**One switch that really means off.** *Settings → MCP server → Allow MCP
access*: with it off, the server refuses to serve — over the HTTP endpoint and
over the subprocess route (stdio) that Claude Desktop uses, whether or not this
app is running. It does not simply die: it answers with a single tool that
explains it is switched off, so your client tells you in plain words instead of
leaving a failed connection and a reason buried in a log file. No archive data
is served either way; the index is not even opened.

The entry you pasted into your client stays valid and works again the moment you
allow access; nothing needs reconfiguring. And it is a safeguard against
accident, not an access barrier — anyone who reaches this machine can switch it
back.

*After updating, run one export with **Mail** ticked.* That is what withdraws
the wrong *Deleted* markers, and the index run that follows carries the
correction into search and analytics. Since 5.2.0 nothing is preselected, so
tick what you want first.

## New in 5.2.0

**Calendars are chosen like folders.** *Settings → Calendars* holds the same
kind of include/exclude rules the mailbox folders use, with *Show export list*
as a preview. Without a rule only your own calendar comes along — a mailbox
usually carries birthdays, holidays and calendars other people shared, and
nobody meant those.

This grew out of a bug: on Windows an export could stop at a question about
which calendars to fetch, printed into a log nobody could answer. The run looked
frozen, then carried on by itself. Nothing launched from the app asks questions
any more.

**The filters follow each other.** Pick a source and the folder list narrows to
it: calendars for the calendar, the four kinds of conversation for Teams,
mailbox folders for mail. Where there is nothing to choose, the field is gone
instead of sitting there empty.

**A file type filter.** Find the mails that carry a PDF, or the spreadsheets in
your OneDrive mirror. It works in all three kinds of search, and it needs one
index run before it appears. *Settings* lists the types not worth offering —
prefilled with the signature blocks mail programs attach by themselves, visible
and editable.

**The person field suggests names** as you type, with the number of messages
behind each, so a typo is not mistaken for someone who is not there. `*` is a
wildcard: `schmi*` searches everyone matching rather than pinning you to one
name, and the list offers that as its last row.

**Nothing is preselected in the export any more.** Mail, calendar, contacts and
the kinds of chat all start unticked — each of them can mean tens of thousands
of items, and what gets fetched should be a decision. An empty selection now
produces no export step at all; it used to reach the script as “not set”, which
it read as “everything”.

**“Deleted” is a filter, not a view.** It never was a view of its own — it was
this same result list with one filter set. It now sits in the filter row with
the others, counts towards the number on the toggle, and clears with them. Its
**(i)** is also honest about a limit: Exchange gives a message a new id when it
is moved, so a message that merely changed folders can show up here.

**A date that does not exist is now reported.** The 31st of June was accepted by
the browser, handed over empty, and searched without that limit — the results
looked wrong for no visible reason.

**Analytics.** People are counted once each; whoever wrote both in Teams and by
mail was listed twice before, each time with part of their messages. You can
leave people out of the ranking in *Settings* — usually yourself, whose messages
otherwise sit far at the top and say nothing about the exchange with others.
All bars in a list start at the same place.

**Removed: the portable search page.** The app never used the file, and on a
grown archive it reached several hundred megabytes — a page a browser has to
read whole before anything appears. The archive is already readable without this
app: `.eml`, `.ics`, `.vcf` and one index page per Teams folder. The script is
still there for anyone who wants it.

*After updating, run the index once* — the file type filter and the calendar
entries in the folder list appear with it.

## New in 5.1.0

**Search asks you what kind.** Until now every search quietly mixed exact
matching and meaning into one ranking, which made both worse: a search for an
invoice number came back padded with things that merely sounded similar. There
are now three kinds, chosen above the results.

*Text search* is the default and always available — the words that actually
occur, ranked by relevance, no Ollama needed. *Similar search* finds related
wording even when your words do not appear. *AI summary* answers a question in
a paragraph with source numbers and keeps the hits it used one click away.

The AI no longer runs on every search. It was a checkbox that made each search
wait for a language model; now it is a kind of search you pick when you want it.
Every hit also offers **Find similar** — more like this one — which works even
without Ollama, because that message's vector is already in the index.

The result list got out of its own way: two lines per hit instead of four, dates
in a column of their own so the list can be scanned down the edge, and the
actions moved into a menu at the end of the row.

**Ollama is optional, and you can say so.** It was always possible to run without
it, but the app kept looking: every ten seconds, forever, on machines that had
none. There is now a switch in *Settings*. Turned off, the app stops looking, the
header says *Ollama off*, the two kinds of search that need it are visibly
switched off rather than hidden, and the index is built as full text only. You
can also keep Ollama and still choose a full-text index — embedding a real
archive costs an hour, and if you only ever search exactly, that hour buys you
nothing. Switching back later re-uses the vectors that were set aside, so it
costs one index run, not another hour.

**Settings went from twelve cards to seven.** Every setting is one line now:
what it is on the left, the control on the right, and an **(i)** that explains
what it does and what happens if you change it — instead of a paragraph of grey
text under each field. Index and AI sit indented under the Ollama switch,
because that is what they depend on.

The Teams, Outlook and index folder names are no longer settable. They were a
leftover from when this was a handful of scripts someone ran by hand. *If you
ever changed those names*, the app will look in the standard folders after this
update and find nothing — your data is untouched, but you will want to rename
the folders back or point the data folder at them.

**Analytics can now show you time.** Messages per month, stacked by source, show
when your communication moved from mail to chat. A growth curve shows how the
archive filled up. And **gaps** are named outright: months between your first and
your last message with nothing in them at all — the one question an archive
should be able to answer about itself without asking Microsoft. Below that:
attachments by file type, the largest single files, who you exchange the most
with, and when messages disappeared from the mailbox.

**A privacy document.** [PRIVACY.md](https://github.com/n-schilling/munimentum/blob/main/PRIVACY.md)
states plainly what leaves this machine: Microsoft Graph, your local Ollama, and
one update check you can switch off. Nothing else — no telemetry, no analytics,
no account.

## New in 5.0.0 — it becomes an app with a name of its own

**The project is now called Munimentum.** *Munimentum* is Latin for a rampart,
and in medieval usage it also meant a deed — the document you keep because it is
the proof of what is yours. The two senses are one idea, and it is what this app
does: a walled place for records that would otherwise live only in someone
else's cloud, and that you would have a hard time getting at once your access
ends. The old name described the source of the data; this one describes the
point.

**Two things change for you, and neither happens by itself:**

*The data folder is new.* The app now uses `Munimentum` where it used to use
`Microsoft365-Archiv`. It will not find your existing archive on its own — after
updating, rename the folder once and everything is back, including exports,
index and settings:

* macOS: `~/Library/Application Support/Microsoft365-Archiv` → `…/Munimentum`
* Windows: `%LOCALAPPDATA%\Microsoft365-Archiv` → `%LOCALAPPDATA%\Munimentum`
* Linux: `~/.local/share/Microsoft365-Archiv` → `~/.local/share/Munimentum`

Alternatively, leave the folder where it is and point *Settings → Data folder* at
it. Nothing is deleted either way; a fresh start would only mean exporting again.

*The MCP server is called `munimentum`.* If you registered it with Claude, the
entry now reads `{"mcpServers": {"munimentum": …}}` — the snippet under
*Settings → MCP* is already correct, so copy it across once.

The downloads are named `Munimentum-*` accordingly, and on a Mac the app is
`Munimentum.app`. The environment variable `MUNIMENTUM_DATA_DIR` replaces
`OFFICE365_DATA_DIR`; the old name keeps working.

## New in 4.2.0

**macOS opens without a warning.** The bundles are signed with a Developer ID
certificate and notarized by Apple, and the ticket is stapled to both the app
and the DMG. Double-click, drag to *Applications*, done — no detour through
*Privacy & Security*, no Terminal command. Because the ticket travels inside the
file, it also works on a machine that is offline.

The signature covers what it should: it is timestamped, so it stays valid after
the certificate eventually expires, and the app runs under Apple's Hardened
Runtime. Every release build verifies all of that on the finished DMG before it
becomes a download.

**Windows is unchanged** — still unsigned, still one SmartScreen prompt on first
launch. A certificate costs considerably more there, and SmartScreen wants to
see downloads before it goes quiet.

## New in 4.1.1

**Indexing no longer fails while Claude is connected.** With the MCP server
running, every index run on Windows ended in “access denied” — after the
embedding, so after all of the waiting, and reliably on every attempt. Every
reader keeps the vector file memory-mapped, and Windows will not let a mapped
file be replaced. It hit any run in which the MCP server was up, the search in
the app had been used, or Claude Desktop had started a server of its own. macOS
and Linux were never affected: there the rename simply works.

A run now writes a new file instead of replacing the existing one. Nothing has
to be embedded again — an index built by an earlier version is carried over as
it is. Two things follow from it: inside `rag_store` the file is now called
`vectors-N.npy` rather than `vectors.npy`, and on Windows the superseded one
stays behind until the next index run, because that is the earliest it can be
deleted.

Everything below is unchanged from 4.1.0 — if you are coming from there, this
is the only difference.

## New in 4.1.0

**Indexing works again on a real archive.** As soon as one source held more than
200 files, the index step ended in `BrokenProcessPool` — before it had read a
single one. Nothing was wrong with the files, and no setting helped.

Reading is spread across all cores, and a worker process starts by launching the
app's own program file a second time. That call was not recognised as such, so
every worker died the moment it started. This has been in every download since
3.5.0 and affects mail, chats, the calendar and — since 4.0.0 — the OneDrive
mirror alike: whichever source crossed 200 files first. Small archives stayed
below the line and never noticed. Running from source was never affected.

If a worker cannot start for some other reason, reading now falls back to a
single core and says so in the log, rather than abandoning the run.

**The log can be copied.** *Copy* in the log bar at the bottom puts the whole
log on the clipboard, one line per line. Until now it could only be selected by
hand out of a scrolling box.

**Report a problem.** Next to it — and in *Settings* — *Report a problem* fills
in a GitHub issue with the log and the details that a bug report otherwise costs
two rounds of e-mail to collect: version, operating system, cores, what the
index holds. The app sends nothing itself. E-mail addresses and user names in
paths are replaced, the whole text is shown for editing, and you are the one who
submits the form. Folder names and subject lines are beyond what a pattern can
spot, so read it before you post.

Coming from 3.5.0? Everything 4.0.0 brought is in this build too — the OneDrive
mirror, the DMG for macOS, a similarity floor for meaning-based search and a
faster AI summary. The 4.0.0 release notes describe it.

## Which file?

| File | For |
|---|---|
| `Munimentum-macos-arm64.dmg` | Mac with Apple Silicon (M1–M4) |
| `Munimentum-macos-x86_64.dmg` | Mac with an Intel processor |
| `Munimentum-windows-x64.zip` | Windows 10/11 (64-bit) |
| `Munimentum-linux-x64.tar.gz` | Linux (64-bit, glibc 2.35+) |

Not sure which Mac? Apple menu → *About This Mac*: if it says *Apple M…*, take
`arm64`, otherwise `x86_64`.

## Getting started

**macOS** — double-click the DMG, drag `Munimentum.app` onto the
*Applications* folder shown next to it, close (eject) the window, and start the
app from *Applications*.

**Windows** — unpack the ZIP (right-click → *Extract All*, not just looking
inside), then double-click `Munimentum.exe` in the extracted folder.

**Linux** — `tar -xzf Munimentum-linux-x64.tar.gz`, then run
`./Munimentum/Munimentum`.

The interface then opens by itself in your default browser. Everything else —
fetching a token, exporting, searching — is explained there.

## “Windows protected your PC”

**macOS is signed and notarized by Apple** — it opens on a double-click, with no
warning and nothing to click past. That is new in this release; earlier versions
needed a detour through *Privacy & Security*.

**Windows is not code-signed.** A certificate costs considerably more there, and
SmartScreen additionally wants to see a download count before it goes quiet. So
on first launch you get the blue window: click *“More info”*, then *“Run
anyway”*. It happens once.

If you would rather not, run it from source instead (`python3 app.py`, see the
README) — the function and the result are identical.

## Starting and quitting

The app has no window of its own — it serves a page and lives in your browser.
On a Mac it therefore does not stay in the Dock. Quit it with **“Quit”** at
the top right; the MCP server goes with it.

Starting it a second time does not create a second copy: the app notices one is
already running and just opens its page. That is also the way back if you closed
the tab — simply start the app again.

## Where does the data go?

Not into the app, but into your user folder — so an update overwrites nothing:

* macOS: `~/Library/Application Support/Munimentum`
* Windows: `%LOCALAPPDATA%\Munimentum`
* Linux: `~/.local/share/Munimentum`

The path is also shown in *Settings*. A mailbox can take up tens of gigabytes;
to put it on another disk, change it directly in *Settings* (it takes effect
after a restart and moves nothing). For a single run, `--data-dir FOLDER` and
`MUNIMENTUM_DATA_DIR` work.

## Language

The interface comes in German, English and French and follows your browser's
language by default. You can change it in *Settings*. Exported content is never
touched by this.

## Optional: Ollama

Without Ollama everything works except *meaning-based* search — export,
full-text search and the MCP server for Claude run normally. The app asks at
startup and explains the installation if you want it.

With Ollama and a language model loaded, the *Search data* tab gains an
**“AI summary (Ollama)”** checkbox: a model in your Ollama condenses the hits
into a paragraph with citations. The box says so as well — the summary does not
come from your archive but from the AI, and it relies solely on the hits below
it. Nothing leaves your machine. The model and the number of sources are set in
*Settings*.

## Updates

At startup the app asks GitHub once whether a newer version exists, and then
just leaves a note with a link — nothing is downloaded or replaced. You can
turn this off in *Settings*; it is the only connection the app makes apart from
Microsoft Graph and your local Ollama.

## Checksums

`SHA256SUMS.txt` is attached. Verify with `shasum -a 256 -c SHA256SUMS.txt`
(macOS/Linux) or `Get-FileHash file.zip` (PowerShell).

## How it got here

Before 3.5.0 there was a series of earlier stages that never appeared as a
download — briefly, in one paragraph:

A fixed list of folder names to leave out became ordered rules on paths where
the last match wins; the folder tree was separated from the export and has since
been kept as a file, which takes the start of a run from minutes to a fraction
of a second. Search learned the folder as a criterion, the names of attachments,
the conversation thread under each hit, and a filter for deleted messages —
because an archive that only grows cannot answer the most important question:
what was here once and is gone now? Then came an Analytics tab with a
completeness check, an address book from two sources, the choice between a
pasted access key and a real sign-in (which is what lets the schedule run
unattended), three interface languages, and runs that do nothing when there is
nothing to do.

What comes next is in `ROADMAP.md`.
