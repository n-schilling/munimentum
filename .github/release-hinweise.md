## New in 13.5.0

**Save a search from its result.** The result's header carries *Save
the current search…* beside *Add all to a case* – the same dialog as
under *Saved*, without the way there. It stays away while a saved search
runs: that one is kept already.

**The save dialog proposes a title.** With Ollama running and its chat
model there, *Save search* fills the name in from the criteria – a few
words in the page's language, worded by the local model, yours to
overwrite; a faint line under the field says so until you type, and a
click on it asks once more. Nothing leaves the machine. Without Ollama
the field stays as it was.

**A fetch of a few folders no longer questions the rest.** *Fetch now*
on the mail row reads the folders with something open; the mails of
their subfolders, which were not read, then counted as suspects for
deletion and were checked with Microsoft one by one (*Checking n mails
that were no longer in the mailbox*) – a wasted round of requests,
never a wrong tombstone. Folders the fetch leaves out are now excluded
by name, as folders a cadence leaves out always were.

**The run says what it runs.** *Selected: Outlook (…)* under the run's
heading named the categories ticked under *Build archive*; a fetch of
the mail row with only contacts ticked said *Contacts* while reading
mail. It now names the categories the step actually runs.

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
