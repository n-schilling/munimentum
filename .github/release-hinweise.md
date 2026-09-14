## New in 10.2.0

**The completeness balance.** *Check now* in Insights asks one
question per source in use: would a run with today's settings fetch
anything now that is not here yet? Each row answers in plain words with
four numbers — here, not fetched yet, deliberately excluded, deleted at
Microsoft but kept — and now covers every source: mail, calendar, contacts,
Teams, OneDrive, SharePoint libraries and pages, Planner, To Do, OneNote.
Excluded is what your own rules, filters and start days leave out, counted
and never named; a file waiting for its folder cadence waits rather than
counting as missing; Teams is judged by conversations. A row with
something open offers *Fetch now*, which fetches only what the row found
open: the mailbox reads just the folders with something open, OneDrive
and SharePoint fetch the open files by id without walking the library
again, every other source runs its regular run – then the index, and the
row is checked again in the same run. OneNote says "not checked" when
its hourly budget is spent instead of inventing a gap.

**Archive and bookkeeping.** A second card in Insights looks inward:
*Check archive* holds each export's own bookkeeping against the files on
disk without asking Microsoft — files the bookkeeping knows that are not
there (*Fetch again* brings them back), files here that nothing knows
(they stay), mirrored files shorter than recorded, files a tombstone lists
as kept that are gone — and the index against the archive: how many files
changed, arrived or vanished since it last read them, with *Index only*
one click away. Each kind of finding has one explicit action: *Findings…*
lists the files, *Fetch again* fetches exactly the missing files through
the source's own bookkeeping – a mail by its id, a file by its drive
item, a Teams page by its conversation – whether or not the source is
ticked, and ends by judging the row afresh; *Note as lost*
records a tombstone whose file is gone – and settles files still missing
after a fetch, which the row then calls by that name – *Set aside* moves
files no bookkeeping knows into a `_fremd/` folder and *Put back*
reverses it, *Rebuild bookkeeping* sets a damaged `state.db` aside and
lets the next run fill a fresh one. Nothing deletes; what moves comes
back. Every action, every check and every fetch is a run: the run window
opens with it and its log says what happened.

**Keep awake during a run.** A new switch under *Settings › App*, on by
default, holds the machine off idle sleep while a run is on — macOS
`caffeinate`, a Windows power request, `systemd-inhibit` on Linux — so a
laptop no longer dozes off halfway through an export. The lid still
sleeps.

**Insights.** *Overview* is now *Insights*, with a side navigation like
the settings' – one entry per card – and a dot on the two checks that
says at a glance whether something is open or found.

**Smaller.** The header stays at the top while the page scrolls. *Force
full sync* has an (i) per source that says what its full read takes
along. *Sync now*, *Force full sync*, *Read the calendar in full* and
*Read legacy comments again* now index what they fetched, and every
one of them opens the run window. *Sync now*, *Force full sync* and the
balance's *Fetch now* on a source that is not ticked in the settings say
so instead of running nothing.

## Upgrading

**From 10.1 or 10.0:** nothing to do.

**From 9.x:** the first start moves your archive from the app folder into
`profiles/standard/` — a rename on the same disk, instant whatever the
size, all or nothing; data or index folders you pointed elsewhere stay
where they are, folders inside the app folder move along and the settings
follow them. The log says what moved. **Claude Desktop:** copy the stdio
MCP snippet again from *Settings* — it names only the profile now; the
HTTP snippet for Claude Code is unchanged. Windows only: if the move is refused because a
program holds the index open (Claude Desktop's MCP server, for one), the
archive runs where it was for that start, and the next start tries again
once that program is closed.

**From 8.x or 7.x:** the first run after the upgrade lists every mail
folder, calendar, contact folder and channel once in full to obtain its
change token — it downloads nothing that is already there, but it takes
as long as a run used to, and every Teams conversation is rendered once
more from its new message store. A Teams cadence set before 9.0 applies
to all four kinds until you set them apart.

**Coming from 6.1 or older?** Run the latest 6.x release once first — it
moves the export bookkeeping into each folder's `state.db`; 7.x and later
no longer carry that migration. Only if you had redirected the data folder
with the old pointer file (`datenordner.txt`): it is no longer read. Move
`app_config.json`, `gx_token.txt`, `msal_cache.bin` and `runs.db` into the
app folder (macOS `~/Library/Application Support/Munimentum`, Windows
`%LOCALAPPDATA%\Munimentum`, Linux `~/.local/share/Munimentum`), then set
the data and index paths in *Settings* to where your archive lives. Claude
Desktop users: the stdio MCP snippet changed in 7.0 (it names the app
folder) — copy it again from *Settings*.

## Which file?

| File | For |
|---|---|
| `Munimentum-macos-arm64.dmg` | Mac with Apple Silicon (M1 or newer) |
| `Munimentum-windows-x64.zip` | Windows 10/11 (64-bit) |
| `Munimentum-linux-x64.tar.gz` | Linux (64-bit, glibc 2.35+) |


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

**macOS is signed and notarized by Apple** — it opens on a double-click, with
no warning and nothing to click past.

**Windows is not code-signed.** A certificate costs considerably more there, and
SmartScreen additionally wants to see a download count before it goes quiet. So
on first launch you get the blue window: click *“More info”*, then *“Run
anyway”*. It happens once.

If you would rather not, run it from source instead (`python3 app.py`, see the
README) — the function and the result are identical.

## Optional: Ollama

Without Ollama everything works except *meaning-based* search — export,
full-text search and the MCP server for Claude run normally. The app asks at
startup and explains the installation if you want it.

With Ollama, *Search archive* gains two more kinds of search: *Similar search*
and *AI answer*. Both run on your machine; nothing leaves it. See the README.

## Checksums

`SHA256SUMS.txt` is attached. Verify with `shasum -a 256 -c SHA256SUMS.txt`
(macOS/Linux) or `Get-FileHash file.zip` (PowerShell).

What comes next is in `ROADMAP.md`.
