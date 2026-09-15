## New in 11.0.0

**Cases.** A third door beside *Build archive* and *Search archive*: a
case collects what belongs to one matter — hits, whole result lists and
saved searches, from every source — without copying anything. Every hit
now carries a tick and, once it sits in a case, a small mark naming it;
the chosen hit offers *Add to case*, the ticked ones a bar above the
list, *Add all … to a case* takes the whole result, every page of it, as
it stands now. A case has a **casebook** of short dated notes, stores
**result lists** (a search as it stood at one moment, criteria and
exactly those hits — searchable again), lists its items by source with a
link into the original, and holds the **saved searches** attached to it:
*Check for new hits* shows what they find today that the case lacks,
*Add the new ones* takes them in. The **Cases** filter, there once a
case exists, searches inside one case across every source; *Search in
this case* sets it. A closed
case is read-only, stays searchable and exportable, and reopens any time.
**Export case…** is a run: one folder with the originals of every item
under their source's name and path — mails as `.eml`, chats with their
attachments, files, pages and boards as HTML, contacts and appointments
— plus `index.html` (every item, linked into the original down to the
message, card or task), `items.csv` and `casebook.md`, optionally as a
zip; where it lands is a setting, *Munimentum cases* in your Documents
folder by default.

**Search history and saved searches.** *History* next to *Search* lists
every search by its criteria — words, kind of search, filters — never by
its hits; a click runs it again, another saves it. How long it is kept
is a setting under *Settings › App* (30, 90, 365 days, forever, or not at
all). *Saved* holds searches under a name: run, rename, delete, or attach
to a case; each remembers when it last ran and how many hits it had.

**Stable keys.** The index gives every item a key that survives a
rename or a move — the mail's Message-ID, the calendar and contact UID,
the Teams message id, the drive item, the page, the task — so a case can
point at it for months. The index assigns them on its next run; until
then the ticks say so.

**Smaller.** *Settings › App* names the build next to the version —
the commit a bundle was made from — and the bug report carries it.

**Claude.** New MCP tools: `list_cases`, `get_case`, `case_timeline`,
`case_people`, `case_new_hits`, `list_saved_searches`, `run_saved_search`;
`search_messages` and `browse_messages` take a `case`, and every hit says
which cases it sits in. Claude changes nothing unless *Claude may change
cases* is on under *Settings › Claude (MCP)* — then `add_to_case` and
`add_case_note` can add to open cases, never delete or close one.

## Upgrading

**From 10.x:** nothing to do. The next run — scheduled or by hand,
*Index only* is the quickest — rebuilds the index once with every item's
key, whether or not the exports brought anything new; until then hits
cannot be put into cases and the page says so. Claude Desktop: the case
tools appear once the MCP server is restarted.

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
