## New in 7.0.1

**The folders you set are honoured after a restart.** The bundled app read
its configuration from the wrong place and fell back to the default data and
index folders on every start — a folder set in *Settings* looked saved and
was ignored. Running from source was not affected.

**Storage paths save with the settings.** The *Apply* buttons are gone:
*Save settings* applies both folders, *Default* fills in the default path.
The log names the folder that changed (an index change used to be announced
as the data folder), says at startup which data and index folder are in use,
and warns when the index folder holds no index.

**An existing archive stays where it is.** Found in the app folder, data and
index keep living there — splitting storage is on offer, never demanded.

What 7.0.0 brought — the split storage layout, expert mode with the OpenAPI
description, faster index runs, the tidied settings — is in the notes of
that release.

## Upgrading from 6.x

**Coming from 6.1 or older?** Run the latest 6.x release once first — it
moves the export bookkeeping into each folder's `state.db`; 7.x no longer
carries that migration.

Only if you had redirected the data folder with the old pointer file
(`datenordner.txt`): it is no longer read. Move `app_config.json`,
`gx_token.txt`, `msal_cache.bin` and `runs.db` into the app folder (macOS
`~/Library/Application Support/Munimentum`, Windows
`%LOCALAPPDATA%\Munimentum`, Linux `~/.local/share/Munimentum`), then set
the data and index paths in *Settings* to where your archive lives.

**Claude Desktop users:** the stdio MCP snippet changed in 7.0 (it names the
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
