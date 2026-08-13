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

**To make it take effect:** run one export with **Mail** ticked. That withdraws
the wrong markers — the log says how many — and the index run that follows
carries the correction into search. **Not needed:** a fresh export, a rebuild,
deleting anything. Indexing alone will not do it; the markers are written during
the export.

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

What comes next is in `ROADMAP.md`.
