## New in 10.1.0

**Force full sync.** Change tracking has one blind spot: a setting that
only applies when a unit is touched — *Download shared files* for Teams,
the channel folders, Planner references — does not reach conversations or
boards that have not moved since. Every source's settings now carry
*Force full sync* under *Advanced*: the source forgets its stored change
pointers and is read again as on its first export, everything fetched and
written over, attachments and files included, cadences stepping aside.
Nothing in the archive is deleted — what Microsoft no longer has stays,
tombstone and all. It costs what a first export costs, so the button asks
once, and the log says in one line that the run reads everything.

## Upgrading

**From 10.0:** nothing to do.

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
