## New in 8.0.0

**A new front: two doors.** The page now has two main tabs, *Build
archive* and *Search archive*, with *Overview* and *Settings* at the side.
The archive page shows one line — how current the archive is — and one
button, the sources as cards with their categories as chips, an (i) with
their state and a gear into their settings; a running job opens its own
window with progress, steps and log, stays until you close it and can be
minimised into the header; an empty archive starts with three steps. The search keeps its filters in view
and opens a chosen hit on the right with its actions. Settings have a
navigation on the left, one block per source with the rest under
*Advanced*, and a save bar that appears only with unsaved changes. Access,
AI and MCP show their state where they are fixed: one state frame in the
header, dots in the settings navigation. `DESIGN.md` in the repository
says how the page is built and where a new element goes.

**Two more sources, both off until you switch them on.** *To Do* exports
every list the account sees — one folder per list with a standalone
`list.html`: open and completed tasks with steps, due dates, reminders,
notes, linked resources and attachments; a task that leaves a list stays,
greyed. *OneNote* exports every notebook page by page — one HTML per page
with its images and attachments, so it opens offline; only pages that
changed are fetched again, and a page that leaves its section keeps its
file with a marker. Which notebooks come along works like the mailbox
folders — sync the list, include/exclude rules, an export list — and each
notebook has its own sync cadence and *Sync now*. OneNote allows 400
requests an hour, so the export paces itself and a large notebook takes
several runs, each continuing where the last stopped — a cancelled one
included. Both sources are
full-text searchable, in the app and via MCP, with the list or the notebook
as the folder filter. To Do needs Tasks.Read, OneNote Notes.Read; the token
wizard lists them.

**The files shared in Teams, kept.** Two switches in the Teams settings,
off by default: *Download shared files* fetches every file a chat or
channel post references next to its conversation and links the local copy;
*Mirror the channel folders* takes the Files tab of every exported channel
the way a SharePoint library is mirrored, tombstones included, and channel
posts link into that mirror. A size cap applies to both. The files are
searchable by name, path and type under the Teams source, the file browser
lists them per chat kind and per team, and the MCP `list_files` tool has a
`teams` root. Needs Files.Read.All; the token wizard lists it.

**Archive HTML opens from the app.** Every board, list, page and
conversation served through the app now has its relative links —
attachments, mirrored files, a page's files folder — routed back through
the app, so what works on disk works in the browser too.

## Upgrading

**From 7.x:** nothing to do. Archive, settings and index are used as they
are; only the interface is new. What you will look for: the log bar and the
header pills are gone — the log lives in the run window, AI and MCP show
their state in the settings navigation, and the notice about a newer
release sits under *Settings → App*.

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
