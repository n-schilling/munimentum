## New in 7.0.0

**The storage layout is split.** The app folder is now fixed and holds only
the small things: settings, access token, run history. The two heavy parts
can each point to another disk in *Settings*: the **data folder** (all
exports) and the **index folder** (`rag_store/` — hot reads, keep it fast).
On a fresh install they are separate folders (`data/` next to `rag_store/`).

**Upgrades just keep working.** When the app finds an existing archive laid
out the old way, it points the data folder at the app folder and says so in
the log — nothing is moved, everything is found where it always was.
Munimentum never moves your data: to split storage, quit the app, move the
export folders yourself (into `data/`, or onto another disk), then set the
paths in *Settings*.

Only if you had redirected the data folder with the old pointer file
(`datenordner.txt`): it is no longer read. Move `app_config.json`,
`gx_token.txt`, `msal_cache.bin` and `runs.db` into the app folder (macOS
`~/Library/Application Support/Munimentum`, Windows
`%LOCALAPPDATA%\Munimentum`, Linux `~/.local/share/Munimentum`), then set
the data and index paths in *Settings* to where your archive lives.

**Coming from 6.1 or older?** Run the latest 6.x release once first — it
moves the export bookkeeping into each folder's `state.db`. 7.0 no longer
carries that migration and expects the new format.

**Expert mode.** The individual steps (index only, calendar rebuild) moved
from the *Export* tab into a box at the end of *Settings* — next to something
new there: the complete HTTP API of the interface as an OpenAPI description,
for scripts that talk to the backend directly.

**Faster index runs.** The index reads only the files that changed since
its last run; with nothing new, an archive of a few hundred thousand pieces
is indexed in seconds instead of minutes.

**Settings, tidied.** One shape for every card: switches for every on/off
setting, the index kind as a plain choice, action buttons at the right —
and a single *Save settings* button for the whole tab, the schedule included.

**Claude Desktop users:** the stdio MCP snippet changed (it now names the
app folder) — copy it again from *Settings*.

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

With Ollama, *Search data* gains two more kinds of search: *Similar search* and
*AI summary*. Both run on your machine; nothing leaves it. See the README.

## Checksums

`SHA256SUMS.txt` is attached. Verify with `shasum -a 256 -c SHA256SUMS.txt`
(macOS/Linux) or `Get-FileHash file.zip` (PowerShell).

What comes next is in `ROADMAP.md`.
