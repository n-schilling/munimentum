## New in 11.2.0

**The search row, tidied.** Every filter is a pill — person, source,
date, file type, folder, case, parties, deleted only — and opens its own
small window under it: the source and folder lists with their counts,
the date with four quick ranges, the person field with its suggestions.
A set pill carries its value and a × that clears it; *Clear filters* and
the count of set filters stand at the end of the row. History and saved
searches are two icons beside *Search*.

**One detail per kind of item.** The chosen hit shows only the facts its
kind is known by: a mail its from, to, cc, date, folder and attachments
with their sizes; a chat message who, when and which chat; an
appointment when, where, organiser and attendees; a contact its
organisation, role, addresses and phones; a file its type, size, modified
and where — and nothing more; a page where it lies and when it changed; a
Planner task its plan, assignees, due date, state and checklist; a To Do
task its list, due date, state and steps. A person, a folder or a file
type among the facts is a link that sets the filter. The head says where
the hit sits in the list, with arrows (and ↑ ↓) to walk it; the actions
are one row — *Open original*, *Add to case*, *Find similar*. For mail
and chat the conversation is one quiet fold under the content: closed,
its length; open, every message, the ones the case already holds marked,
and *Add the other n* right there. The *Show conversation* button is
gone. Claude gets the same facts: `get_document` (and `/api/document`)
carries a `facts` block per kind, so an address, an attendee or a due
date no longer has to be parsed out of the text.

**The parties filter shows up.** The page learns what an index can do
from the status, and the status did not name the new address columns —
so the *internal · external* filter stayed hidden and the People view
kept saying "after the next index run", whatever the index held. Now
it does; nothing to rebuild.

**Phrases in the text search.** Words still match any of them, the best
hits first — and a phrase in quotes, `"budget frame"`, now has to occur
as it stands, adjacent and in order; every such phrase is required, and
a loose word beside it must occur as well. The preview marks the phrase
as one piece. Same for Claude's searches; the semantic search reads the
whole text and knows no phrases.

## Upgrading

**From 11.1.x:** nothing to do.

**From 11.0.x:** the case book gets its new columns on the first start.
The next index run reads the archive once in full — it adds the
addresses the People view, the *external* mark and the parties filter
need — and runs whether or not the exports brought anything; until then
the People view says so and shows names only, and the filter stays
hidden. Claude Desktop: `case_folder`, the folder and the remark on
`add_to_case` appear once the MCP server is restarted.

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
