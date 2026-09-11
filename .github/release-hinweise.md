## New in 9.0.0

**Every source asks only for what changed.** Mail folders, calendars and
contact folders keep a change token after their first pass, so a run
receives only what was added, changed or removed — a deletion no longer
needs a check per mail, a changed appointment or contact is written again.
Teams channels are read the same way, a chat that moved fetches only the
messages since its last one, and To Do lists keep a token per list.
Runs that used to list a whole mailbox or every channel's history are
minutes now.

**Cadences where you need them.** Mail, calendar and contacts each have
their own sync cadence, so do the four kinds of Teams conversations; OneDrive
and To Do keep theirs per source and gain *Sync now*. A single mail folder,
team, channel, chat, OneDrive or library folder or To Do list can depart
from it: *Pick folders…*, *Pick teams and chats…* and *Pick lists…* open
the tree of the last list sync, and a value set there reaches everything
below it until a deeper one is set — in the mirrors it paces the downloads,
files in a folder not yet due wait. Every gate lives inside the export and
says so in the log when it skips.

**Rules for Teams and To Do, path rules for SharePoint.** *Sync team list*
and *Sync lists* fetch the names, ordered rules decide — `1on1/<title>`,
`group/<title>`, `meeting/<title>`, `channels/<team>/<channel>`, or the
list titles — and *Show export list* spells out the outcome, as for the
mailbox folders. SharePoint libraries take path rules on top of their URL
list and type filters. A changed selection makes the next run read that
library or drive once in full, so newly included files arrive. A first
export can start at a day of your choosing, for mail and for chats.

**The calendar as a window.** After the first export the calendar is read
as a window — a configurable number of months back (one by default) plus
everything ahead; *Read the calendar in full* reads everything once.

**Planner and OneNote.** Chat comments on Planner tasks are re-read at an
interval you set instead of on every *Sync now*; boards and lists are
written only when something changed. OneNote reuses images and attachments
already on disk when a page is fetched again, which spares the hourly
budget.

**A tour for the first days.** The empty archive offers a tour: coach
marks in the real interface walk you through building the archive, setting
up a source in detail and searching, one element at a time. The result of
the first run offers the search chapter, and *Settings › App* starts any
chapter again.

**Under the hood.** One database connection per run instead of one per
write, row-level updates of the mirror inventories, Graph requests bundled
twenty at a time where they used to run one by one, Planner and To Do in
parallel, and the calendar rebuild parsing only the mails that changed.
Legacy Planner comments are frozen since February 2026 and no longer
checked after a board's first export; a button reads them again on request.

## Upgrading

**From 8.x or 7.x:** nothing to do. The first run after the upgrade lists
every mail folder, calendar, contact folder and channel once in full to
obtain its change token — it downloads nothing that is already there, but
it takes as long as a run used to, and every Teams conversation is rendered
once more from its new message store. A Teams cadence set before 9.0
applies to all four kinds until you set them apart.

**Coming from 6.1 or older?** Run the latest 6.x release once first — it
moves the export bookkeeping into each folder's `state.db`; 7.x and 8.x no
longer carry that migration. Only if you had redirected the data folder
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
