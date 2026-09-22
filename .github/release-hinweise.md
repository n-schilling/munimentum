## New in 13.5.1

**A chat keeps its own name.** Whatever someone pastes into a Teams
message arrives with its markup, headings included – and the index took
such a heading for the name of the conversation. Every message of that
chat then stood in the result under the chat's name with the pasted
heading glued to it, and the heading was missing from the text it
belongs to. The name is the file's own heading again, and a heading in a
message is part of that message.

**The update check no longer stumbles over GitHub's limit.** An address
may ask GitHub sixty times an hour without an account, and every start
of the app shares that with everything else on the same connection. The
check now sends the mark of the last answer, so an unchanged answer
costs nothing of it. When the limit refuses anyway, *Settings › App*
says *Update check not possible* instead of *HTTP 403* and names the
code, the limit and the time it opens again on the mouseover.

**The tour remembers every chapter.** *Help* offers six of them, and
only the first three were kept as seen: Cases, Insights and Claude stood
as unseen again after every start.

## Upgrading

**From 13.x:** the first run that updates the index reads every file
once more – that is how the corrected chat names reach what is already
indexed. It costs one pass over the archive; nothing is fetched from
Microsoft, and the embeddings of everything that did not change are kept.

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
