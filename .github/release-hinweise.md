## New in 13.4.0

**The completeness check chooses its own sources.** The card under
*Insights › Completeness* carries the ten balance rows as chips, with
*All* and *None*; the check asks exactly those, ticked for the export
or not – a source that needs addresses waits until they are entered.
Until now the check followed the export's switches, and SharePoint
ran whenever its addresses were set. Every step of the run names its
source (*Completeness: OneDrive*), and the run window names the run.

**The access card no longer raises *Unsaved changes*.** A key pasted
under *Settings › Microsoft Access* is saved by *Save access* alone, with
its own answer; the settings bar used to appear beside it although
*Save settings* never touched the key.

**What Microsoft refuses or no longer has is recorded, not retried.**
A `403` or a `404` on a mail, a file, a task, a page, an attachment or
an inline image used to be an error on every run – and one such file
froze a drive's delta pointer, one mail its folder's link, one task its
list's cadence. Now every export records the verdict in its
bookkeeping, says it once, and moves on with pointers and cadences
intact; it asks again only when the item changes or you force a full
sync of the source. The completeness balance and *Archive and
bookkeeping* count these items as *refused* and *gone before fetched* –
their own numbers, never *missing* or *open*, listed by name under
*Findings*. A copy fetched before the verdict keeps its place. A file
that fails for a passing reason (a `502`, a lock) is asked again next
run – To Do and Planner attachments, OneNote images and SharePoint page
images used to be forgotten after one failure.

**Fewer false *incomplete* on Teams files.** The bookkeeping now
records the size of the copy as written, not the size Graph announced
– the two differ for some files behind a sharing link – and corrects
older records the next time the conversation is read.

**The API description** had one response key twice in the sign-in
operation, which strict viewers refused; a test now forbids it.

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
