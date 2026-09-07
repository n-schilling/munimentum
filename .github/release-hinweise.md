## New in 7.0.0

**The storage layout is split — this release needs one manual step.** The
app folder is now fixed and holds only the small things: settings, access
token, run history. The two heavy parts live below it and can each point
to another disk in *Settings*: the **data folder** (`data/`, all exports)
and the **index folder** (`rag_store/` — hot reads, keep it fast).
Munimentum never moves your data, so after updating do it yourself, once:

1. Quit Munimentum.
2. In the app folder (macOS `~/Library/Application Support/Munimentum`,
   Windows `%LOCALAPPDATA%\Munimentum`, Linux `~/.local/share/Munimentum`)
   create `data/` and move the export folders into it: `teams_export`,
   `outlook_export`, `onedrive_export`, `sharepoint_export`,
   `sharepoint_pages`, `planner_export`. `rag_store` and everything else
   stay where they are.
3. Only if you had redirected the data folder (the old pointer file): move
   `app_config.json`, `gx_token.txt`, `msal_cache.bin` and `runs.db` back
   into the app folder, then set the data and index paths in *Settings* to
   wherever your exports and index actually live. The pointer itself is no
   longer honoured — the log says so until you delete the file.

Alternatively, set the data folder in *Settings* to the folder your exports
already live in — then nothing needs to move at all.

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
