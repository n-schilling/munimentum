## New in 13.4.1

**The completeness check asks only what the export fetches.** A row
ticked on the card is checked among the sources in use: a source that
is not ticked under *Build archive*, or has no addresses yet, is greyed
on the card with the reason and never asked – nothing would come of a
run, so nothing is open. For Teams the check takes the ticked kinds
(chats, meetings, channels), as the export does. 13.4.0 asked every
kind of a chosen source, ticked or not.

**The calendar check asks the view plainly.** It selected the change
stamp, which `calendarView` refuses to select; the calendar then stood
as *unreachable* with nothing counted. The view is now asked without
`$select`, and a mailbox listing the check cannot read says why in the
run log instead of only *unreachable* on the card.

**Teams and channel listings are asked without query options.** The
list of joined teams answers `400` to any OData option; the channel
export and the Teams check sent one and failed with *Teams could not be
loaded* on every run since 3.5.0. The listings are now plain.

## Upgrading

**From 13.x:** nothing to do.

**From 12.x or older:** the first run after the update rebuilds the
search index once, and every script against the HTTP interface has to
move to `/api/v1` – both are spelled out in the notes of
[13.0.0](https://github.com/n-schilling/munimentum/releases/tag/v13.0.0).
From 9.x or older see
[10.0.0](https://github.com/n-schilling/munimentum/releases/tag/v10.0.0)
first.

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
