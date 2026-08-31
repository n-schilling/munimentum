## New in 6.1.0

**A sync cadence per source.** Not everything needs syncing every run:
OneDrive, Teams and every single SharePoint URL can be set to always, daily,
weekly or monthly. The cadence applies to scheduled and manual runs alike —
below its interval the source is skipped with a clear log line saying so.

**URL tables for SharePoint.** Libraries and pages are configured as a small
table instead of a text field: one row per URL, with its cadence and a *Sync
now* button that runs exactly that one immediately, cadence ignored.

**Cheaper folder mirrors.** A URL that points into one folder now rides the
library's change feed: the first run still walks the whole library once —
and says what it is doing while it walks — but every later run asks
Microsoft only for what has changed since. An unchanged library costs about
one request.

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
