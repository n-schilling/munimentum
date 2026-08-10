Ready-to-run app — no Python, nothing to install.

## What the app does

**Export.** Teams chats and channels, Outlook mail, calendar, contacts and
**OneDrive files** through Microsoft Graph — with your own access, without
anyone in IT having to approve anything. Every run fetches only what is new.
Which folders come along is a list of ordered include/exclude rules, and
*Show export list* spells out what they currently mean.

**Search.** Full text and — with [Ollama](https://ollama.com) — meaning-based
search across everything exported, merged into a single ranking. Filter by
person, period, source and folder. Plus the whole mail thread under a hit, a
calendar including appointments recovered from invitation and cancellation
mails, an address book, and a view for messages that are no longer in the
mailbox.

**Analytics.** What the archive holds — messages per source, conversations,
people, files, period, disk usage. And, on request, a comparison against the
mailbox and the drive: what Microsoft counts per folder against what is here.

**Work with Claude.** A built-in MCP server makes the archive searchable for
Claude — with citations, a folder filter and a `days` shorthand for “the last
seven days”.

Everything stays on your machine. The only connections outbound are Microsoft
Graph and your local Ollama.

## New in 4.0.0

**OneDrive is backed up.** Your own drive is mirrored locally — with the same
include/exclude rules as the mailbox, a size limit and a folder-structure sync.
Renamed and moved files are carried along instead of downloaded again; anything
deleted in OneDrive **stays here** and gets a marker. Earlier versions of a
changed file are not kept — the mirror holds the current one and remembers what
disappeared.

Searchable are **name and folder**, not the contents: `att:pdf` finds a file
just like a mail attachment. Document contents will follow (see `ROADMAP.md`).

**macOS ships as a DMG.** It used to be a ZIP — and on some machines the macOS
Archive Utility gave up halfway and left behind an unusable app. The cause was
the 36 symlinks every PyInstaller bundle contains. A DMG is never unpacked:
double-click, drag the app to *Applications*, done.

**Search finds what you meant.** Meaning-based search had no floor and always
returned its best hits — even when nothing fitted. Narrow the search to a single
day and you got every message of that day. There is now a measured floor (45 %,
adjustable in *Settings*). Previews also show the passage **around the match**
and highlight the term — before, they showed the first 200 characters, so a
correct hit could look like a mistake.

**The AI summary is considerably faster.** The context window was fixed at
32768 tokens, and Ollama reserves memory for it whether the text needs it or
not. On a machine with 24 GB and a large model that pushed it into swapping.
The window now follows the actual text — measured more than twice as fast,
without changing a setting.

**A tidier interface.** The search mask is one search box with a button; the
filters sit below it behind a toggle that shows how many are set. Searching
happens when you ask for it — not while typing. *Deleted* has become a view of
its own next to Results, Calendar and Contacts. Explanations live on an **(i)**
instead of a paragraph beside every button, and figures about the archive are
in *Analytics* only, not repeated in the header.

**Pre-releases say so.** Running a build newer than the latest release used to
report “This is the latest version” — technically true, actually misleading.
Now it says what it is.

## Which file?

| File | For |
|---|---|
| `Microsoft365-Archiv-macos-arm64.dmg` | Mac with Apple Silicon (M1–M4) |
| `Microsoft365-Archiv-macos-x86_64.dmg` | Mac with an Intel processor |
| `Microsoft365-Archiv-windows-x64.zip` | Windows 10/11 (64-bit) |
| `Microsoft365-Archiv-linux-x64.tar.gz` | Linux (64-bit, glibc 2.35+) |

Not sure which Mac? Apple menu → *About This Mac*: if it says *Apple M…*, take
`arm64`, otherwise `x86_64`.

## Getting started

**macOS** — double-click the DMG, drag `Microsoft365-Archiv.app` onto the
*Applications* folder shown next to it, close (eject) the window, and start the
app from *Applications*.

**Windows** — unpack the ZIP (right-click → *Extract All*, not just looking
inside), then double-click `Microsoft365-Archiv.exe` in the extracted folder.

**Linux** — `tar -xzf Microsoft365-Archiv-linux-x64.tar.gz`, then run
`./Microsoft365-Archiv/Microsoft365-Archiv`.

The interface then opens by itself in your default browser. Everything else —
fetching a token, exporting, searching — is explained there.

## “Not verified” / “Windows protected your PC”

The files are **not code-signed** (signing certificates from Apple and
Microsoft cost money). Both systems therefore warn on first launch. This is
expected and happens once:

**macOS** — the dialog reads *“Apple could not verify … is free of
malware”* and offers **“Move to Trash”** as the blue button. Do not click it:

1. Choose **“Done”** (the quiet button underneath).
2. System Settings → *Privacy & Security* → scroll all the way down →
   **“Open Anyway”**. That button only appears for about an hour after the
   blocked attempt; if it is gone, double-click the app once more.
3. Confirm with *“Open”* when asked again. After that you are left alone.

The often-suggested *right-click → Open* has not worked reliably since
macOS 15. The quick and safe way is the Terminal:

```bash
xattr -dr com.apple.quarantine "/Applications/Microsoft365-Archiv.app"
```

What that does: your browser marks every download with a quarantine flag, and
for flagged programs macOS demands notarization by Apple. The app is only
ad-hoc signed, not notarized — the command removes the flag. That is also why a
copy that did not come through a browser starts without any questions.

**Windows** — in the blue SmartScreen window click *“More info”*, then
*“Run anyway”*.

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

* macOS: `~/Library/Application Support/Microsoft365-Archiv`
* Windows: `%LOCALAPPDATA%\Microsoft365-Archiv`
* Linux: `~/.local/share/Microsoft365-Archiv`

The path is also shown in *Settings*. A mailbox can take up tens of gigabytes;
to put it on another disk, change it directly in *Settings* (it takes effect
after a restart and moves nothing). For a single run, `--data-dir FOLDER` and
`OFFICE365_DATA_DIR` still work.

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
