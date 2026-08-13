## New in 5.4.0

A clean-up release: what had grown as separate scripts now shares one Graph
client, one model-server client and one configuration schema. No new buttons —
but the tidying surfaced and fixed real faults:

**An expired access key stops a OneDrive run cleanly.** It used to end in a
generic error the app could not interpret; now the app notices and asks for a
fresh key, exactly as it does for mail and Teams.

**Appointments from other time zones carry the right time in search.** An
invitation with a Windows time-zone name landed in the search index at local
time; search results and the calendar view now agree.

**Building calendar and contacts is faster.** That step no longer reads the
full text of every mail — only the invitations it is actually after.

**Exports are steadier on a flaky network.** A dropped connection no longer
uses up the retries reserved for Microsoft's throttling, and OneDrive now paces
its requests the way the other exports do.

Removed: the generator for the standalone HTML search page. The app has not
offered that page since 5.2; the script behind it now produces only the
calendar and contact data.

## Which file?

| File | For |
|---|---|
| `Munimentum-macos-arm64.dmg` | Mac with Apple Silicon (M1–M4) |
| `Munimentum-macos-x86_64.dmg` | Mac with an Intel processor |
| `Munimentum-windows-x64.zip` | Windows 10/11 (64-bit) |
| `Munimentum-linux-x64.tar.gz` | Linux (64-bit, glibc 2.35+) |

Not sure which Mac? Apple menu → *About This Mac*: if it says *Apple M…*, take
`arm64`, otherwise `x86_64`.

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
