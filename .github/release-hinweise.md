## New in 11.3.0

**Explore archive.** The second door is named for what it is: four ways
into the archive — *Search*, *Calendar*, *Contacts*, *Files* — as one
strip right under the header. Only Search searches; the search row and
its filter pills belong to it alone. Calendar, Contacts and Files keep
their own controls and each carries one button that hands over to Search
with its source set — the calendar with the shown month or week as the
date range, the file browser with the folder you are in, or every mirror
at once from its sources — so cases, saved searches and the history have
one home. The address book's own field is
gone for the same reason.

**Somewhere to start.** Before the first search, Search shows the last
three searches and the saved ones as rows under the field, each with its
criteria; one click runs it. Nothing new is stored — the history and the
saved searches hold all of it.

**The calendar knows any month.** The month's name is a button: it opens
a picker with one year and its twelve months, a dot on every month the
archive holds appointments for, months outside the archive greyed. A
click goes there; *Today* leads back. Appointments recovered from mails
sit in the grid, marked with the mail they came from — the separate
*Reconstructed* list is gone. The week's title is short enough that the
row no longer moves between week and month.

## Upgrading

**From 11.x:** nothing to do. **From 10.x:** the next run rebuilds the
index once — see the notes of
[11.0.0](https://github.com/n-schilling/munimentum/releases/tag/v11.0.0)
and [11.1.0](https://github.com/n-schilling/munimentum/releases/tag/v11.1.0).
**From 9.x or older:** the first start moves the archive into
`profiles/standard/` — see
[10.0.0](https://github.com/n-schilling/munimentum/releases/tag/v10.0.0);
from 6.1 or older run the latest 6.x once first — see
[7.0.0](https://github.com/n-schilling/munimentum/releases/tag/v7.0.0).

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

## Checksums

`SHA256SUMS.txt` is attached. Verify with `shasum -a 256 -c SHA256SUMS.txt`
(macOS/Linux) or `Get-FileHash file.zip` (PowerShell).

What comes next is in `ROADMAP.md`.
