## New in 11.1.0

**Folders in a case.** A case sorts its items one level deep: *New
folder…* in the tool row, ticked rows move with *Move to* (an existing
folder, a new one, or *Unsorted*), a folder's heading offers *Search
here*, *Rename* and `×`. A result list and a saved search file into one
folder as a whole, so a search's new hits land where its earlier ones
went; the window that puts anything into a case asks for the folder.
The *Cases* filter lists every folder under its case, and a hit's mark
names the folder.

**Everything folds.** Every heading in a case — casebook, lists, each
folder, the sources inside it, the searches — is a fold, closed when the
case opens, its closed line carrying the count; two buttons expand or
collapse them all. A **filter field** narrows the case to the rows
matching a title, a person, a date or a remark, and opens every fold as
it is typed.

**The overview steps aside.** The case list is a *Case overview* with
names, status, counts and the last change; while a case is open it
narrows to the names, so the case takes the width, and the arrow in its
head widens it again. Closed cases sit behind one link. The case's head
is three lines — name and status, the facts, the description — then one
tool row; the foot is two groups: the export at the left, *Close* and
*Delete* at the right.

**Three views of a case.** Under the head: *Folders*; *Timeline* —
every item and every note in the order they happened, month by month,
the folder as a tag, and above the rows the case's activity as one soft
bar per month (per week for a short case), the notes as dots, a click
narrowing the rows to that time; and *People* as a picture — you at the
left, everyone the items name in four columns by their last contact,
each a circle sized by how many items name them, initials instead of
photos. A person outside your organisation's mail domains (a new
setting under *App*, else the domain you signed in with) has a dashed
ring and the *external* mark; you are left out yourself. A click on a
person opens a card with the address, the counts, the span of dates and
the two ways on: *Timeline* shows their items, *Search* asks the
archive for them inside the case. The filter field narrows whichever
view is open.

**A remark on every item.** One or two sentences on why the item is in
the case, from the pen on its row; shown under the title, exported in
`index.html`, `timeline.html` and `items.csv`.

**The whole conversation.** The window that adds a hit offers the rest
of its conversation with the numbers the archive knows; a row whose
conversation the case lacks part of shows *Thread +n* and fetches it
into the row's folder.

**External, in the search.** A mail or appointment with a party outside
your organisation's mail domains carries *external* in the hit list and
the detail, and a new filter narrows to *internal only* or *with
external parties* — for Claude the `party` argument on every search.

**Via MCP, on the item.** What Claude put into a case — an item, a note
— carries *via MCP* on that item or note, nowhere else. For Claude:
every search or browse takes `case_folder`, `get_case` lists the
folders and each item's folder, origin and remark, `add_to_case` takes a
`folder` and a `remark`.

**Smaller.** The hit's detail no longer prints where the file is stored
— the original is one click away.

**One ZIP.** *Export case…* shows what the export will hold and always
writes one ZIP — the originals under `<folder>/<source>/<path>`, an
index page with one section per folder, `timeline.html`, `items.csv`
with folder, remark and origin columns, the casebook. The switch and
the folder beside the ZIP are gone.

## Upgrading

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
