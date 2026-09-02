## New in 6.3.0

**Planner boards are archived — comments included.** List board addresses in
the settings, one per row with its own sync cadence, and every plan becomes
a standalone `board.html`: swimlane chips up top, lanes that open into a
compact task list, cards that unfold into description, checklist and — one
more click — the comments, the legacy ones from the group conversation as
well as the new chat-based ones. A task that leaves the board stays in the
archive, greyed. Files a task references can be downloaded next to the
board on request. Task texts, comments and attachment names are searchable
under their own source, the boards appear in the file browser, and reading
needs Tasks.Read plus Group.Read.All for the legacy comments.

**The Analytics tab opens instantly.** Its numbers are computed once per
index run and stored, instead of being recalculated on every visit — and
communication and files are kept apart now: a mirrored PDF no longer fills
a gap in the message timeline, and the mirrors get their own tiles and
type ranking. Runs and completeness checks live together under Health.

**Every run keeps its log.** The log lines of each run are stored in the
run history and can be unfolded inline under Analytics → Health; how long
they are kept is its own setting (14 days by default), separate from the
run rows themselves.

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
