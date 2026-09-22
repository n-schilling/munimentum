## New in 13.6.0

**A search result in three views.** Above the hits stands the strip a
case has: *List*, *Timeline* and *People*. The list is the page as
before. The timeline puts every hit the search finds in the order it
happened, with the band of months above it – a click on a bar narrows
the rows to that month, a row opens the hit at the right as the list
does. People places everyone the hits name by their last contact, the
circle sized by how many hits name them, *external* marked; a person's
card leads on to their timeline or their list. Both views take the
whole result, up to 5 000 hits, and say so when that cap cut.

**Contacts as a picture.** The address book shows its people as a list
or as the same picture, over the whole archive; a person's card carries
company, role and number from the address book, and *Show communication*
opens their timeline.

**A conversation's messages open in place.** A message in the fold
under a mail or a chat message opens as the detail; the original stays
one click away under *Open original*.

**Two routes for scripts.** `/api/v1/search/timeline` answers the whole
result of a search in date order, `/api/v1/search/people` who it names
with counts, addresses and dates – the criteria of `/api/v1/search`, no
paging.

**An archive to look at, without an account.** Running from source,
`python3 -m testdata.build --profile testdaten` writes an invented
archive – all eight sources, a year of traffic, some 2 400 files – and
`python3 app.py --profile testdaten` opens it. Nothing in it comes from
Microsoft, and it is the archive the browser tests run against.

## Upgrading

**From 13.5.0 or older:** the first run that updates the index reads
every file once more – described in the notes of
[13.5.1](https://github.com/n-schilling/munimentum/releases/tag/v13.5.1).

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
