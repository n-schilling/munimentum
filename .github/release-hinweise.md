## New in 6.0.0

**SharePoint sites are mirrored.** List site or folder URLs in the settings —
sharing links included — and the document libraries behind them are kept like
the OneDrive mirror: the current version of every file, deletions stay with a
tombstone note. A URL that points into one folder mirrors exactly that
subtree. Extension filters and a size cap narrow the haul, and a **size
preview** tells you per library what a run would fetch — files and megabytes —
before anything is downloaded. Reading sites needs the Sites.Read.All
permission; the token wizard lists it.

**A file browser joins the search views.** OneDrive and each SharePoint
library, folder by folder straight from the index: originals one click away,
deleted files marked, and *Search here* turns the current folder into a
search filter. In search filters and over MCP, OneDrive and SharePoint are
now separate sources, and Claude can walk the mirrors with the new
`list_files` tool.

**System notifications on macOS.** The end of a run — or an expired access
key — is reported through the notification center even with the browser tab
closed; clicking the notification opens the interface. Off, errors-only or
all runs: your choice in the settings.

**Smaller things.** The schedule can include both mirrors; the OneDrive
checkbox no longer forgets itself on reload; throttling by Microsoft is
answered once per process instead of per connection, which keeps big mirror
runs from collapsing into retry storms.

**Discontinued:** the macOS Intel build. 5.5.0 remains the last x86_64
release; Apple Silicon, Windows and Linux continue.

## Which file?

| File | For |
|---|---|
| `Munimentum-macos-arm64.dmg` | Mac with Apple Silicon (M1–M4) |
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
