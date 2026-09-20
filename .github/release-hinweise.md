## New in 13.1.0

**Attachments, three ways.** A paperclip on a hit says a mail carries
attachments – their names on hover, the count where it is more than
one – whatever the filters are. With the source set to *Mail*, a new
pill, **With attachment**, narrows the search to mails that have one;
real ones only, an inline signature logo does not count. And the chips
in a mail's detail are downloads now: each attachment comes out of the
archived `.eml` on its own, under its own name. Claude gets the same
filter (`with_attachments`) and sees the names on every hit as before –
whole now: a name with spaces used to come apart at every space, on the
hit and for Claude alike.

**The disk image says which version it is.** The macOS download's window
is titled *Munimentum 13.1.0*, and the foot of its picture carries the
version and the build id – the same pair *Settings › App* shows.

## Upgrading

**From 13.0.0:** nothing to do. Scripts: `/api/v1/search` takes
`attachments=1`, and `GET /api/v1/documents/attachments?uid=…&n=1`
serves a mail's n-th attachment; nothing that was there changed.

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
