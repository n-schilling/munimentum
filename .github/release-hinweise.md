## New in 13.4.1

**Fetch now fetches only what the check named.** The Teams check names
the open conversations by key, the calendar and contacts checks their
open events and contacts by id; *Fetch now* takes exactly those – no
chat list, no calendar or folder read again – and takes them off the
balance, so the row is right without a second check. Seconds instead of
minutes for one conversation with newer messages. The mirrors did this
already; mail is counted, not listed, by its check, so its *Fetch now*
stays a read of the folders with something open, fetching only the
mails not here.

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
