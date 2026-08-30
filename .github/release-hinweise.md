## New in 5.5.0

**Analytics has a new Runs view.** Every export now leaves an entry: when it
ran, scheduled or by hand, which elements were enabled, how long each step took
and what it produced — the new pieces broken down by source on hover. The
history lives in a small database next to the exports and holds nothing
personal, only counts, durations and switches; how long it is kept is a
setting (24 months by default).

**Reporting a problem got easier.** *Report a problem* now opens the matching
GitHub issue form with description, system details and log filled in — you
review and edit everything before sending. The report also names which
settings differ from their defaults (rules and name lists only as their size,
paths not at all) and, if enabled, the kind of your last steps in the
interface (tab, search, run — never content).

**The log speaks one language — yours.** The exports report events, the app
puts them into words in the interface language; the mixed German/English lines
are gone. The log is also quieter: settings the app already shows are no
longer echoed at the start of every run.

**Quit tidies up.** Closing the app from the interface now clears the whole
page instead of leaving dead controls behind.

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
