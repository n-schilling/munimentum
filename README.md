# Munimentum

Your own Microsoft 365 data, kept where you can reach it: Teams chats and
channels, Outlook mail, calendar, contacts and OneDrive files — exported through
Microsoft Graph and searchable offline, in the app or through Claude via MCP.

**The magic:** All via delegated access, no admin consent required.

> **munimentum** *(Latin)* — a rampart; and, in medieval usage, a deed: the
> document you keep because it is the proof of what is yours. The two senses are
> the same idea. This is a walled place for records that would otherwise live
> only in someone else's cloud, and that you would have a hard time getting at
> once your access ends.

Nothing leaves your machine except the calls to Microsoft Graph — see [PRIVACY.md](PRIVACY.md).

---

## Download and run

Bundles for macOS, Windows and Linux are attached to every
[release](../../releases). They contain their own Python and every dependency —
nothing to install. The interface opens in your browser.

| File | For |
|---|---|
| `Munimentum-macos-arm64.dmg` | Mac with Apple Silicon |
| `Munimentum-macos-x86_64.dmg` | Mac with an Intel CPU |
| `Munimentum-windows-x64.zip` | Windows 10/11, 64-bit |
| `Munimentum-linux-x64.tar.gz` | Linux, 64-bit (glibc 2.35+) |

On macOS, open the disk image and drag the app to *Applications*; on Windows and
Linux, unpack the archive.

On Windows, SmartScreen asks once on first launch, because the build carries no
code-signing certificate: *More info* → *Run anyway*.

Your data lives in your user folder, not in the app, so an update overwrites
nothing. The path is shown in *Settings*.

---

## Data Ownership

A brief note on the ownership of the data that is exported: **whether you may
export it is yours to check.** Delegated access makes it technically possible;
establishing that it is permitted in your case is not something this app can do
for you.

---

## What it does

One browser page with four tabs.

### Export data

Pick what to fetch — mail, calendar, contacts; 1:1, group, meeting and channel
chats; OneDrive files — and start. Nothing is preselected: any one of these can
mean tens of thousands of items. Every run fetches only what is new, so the
second one takes minutes rather than hours. Deleted messages **stay in the
archive** and get a marker; that is the point of keeping one.

Which folders come along is a list of ordered include/exclude rules, and *Show
export list* spells out what they currently mean: what comes along, what is left
out and why, and what is only in your archive because it is gone from the
source. Calendars work the same way — a mailbox usually carries birthdays,
holidays and calendars other people shared, so by default only your own comes
along until you say otherwise. A schedule can repeat the whole thing while the
app is open.

### Search data

Three kinds of search, chosen above the results. **Text search** is the default
and always available: it finds the words that actually occur, ranked by
relevance. **Similar search** finds related wording even when your words do not
appear. **AI summary** answers a question in a paragraph with source numbers and
keeps the underlying hits one click away.

Filters — person, source, date range, folder, file type, and messages no longer
in the mailbox — sit behind a toggle that shows how many are set, and nothing
searches until you ask for it. The
person field suggests names that actually occur, so a typo is not mistaken for
an absence, and `*` stands for any run of characters when one name is too
narrow. Picking a source narrows what the other two offer: calendars for
the calendar, the four kinds of Teams conversation for Teams, attachment types
for mail, and nothing at all where there is only one thing to choose from. Every
hit offers *Find similar*, the whole conversation it belongs to, and the
original file.
Three views live here: results, calendar (including appointments recovered from
invitation and cancellation mails), and the address book.

The last two kinds of search need [Ollama](https://ollama.com). Without it they
are visibly switched off rather than hidden, and everything else works
unchanged.

### Analytics

What the archive holds, all computed from the index without asking Microsoft:
messages per source, conversations, people, period, disk usage — plus the
timeline. Messages per month by source show when your communication moved from
mail to chat; the growth curve shows how the archive filled up; and **gaps** —
months with no message at all between your first and your last — are named
outright, which is the one question an archive should answer about itself.
Below that: attachments by file type, the largest single files, and who you
exchange the most with.

On request there is also a completeness check against Microsoft: what it counts
per folder against what is here, for both the mailbox and the drive.

### Settings

Export options per source, the schedule, Ollama, the MCP server and the app
itself. Each
setting is one line with an **(i)** that explains what it does and what happens
if you change it. Ollama has a switch of its own: turned off, the app stops
looking for it, the index is built as full text only, and the header says so.

The log bar at the bottom is open from every tab. *Copy* puts it on the
clipboard; *Report a problem* fills in a GitHub issue with the log and the
details a bug report otherwise costs two rounds of e-mail to collect. The app
sends nothing itself — addresses and user names in paths are replaced, the whole
text is shown for editing, and you submit the form.

---

## Signing in

Two ways, chosen in the assistant. Pasting a key stays the default.

| | Needs | Lasts |
|---|---|---|
| **Access key** | nothing but the [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer) | a few hours, then paste again |
| **Sign in** | one sign-in, optionally your own app registration | weeks — the schedule keeps running unattended |

Signing in stores a refresh token on this machine, readable only by you; *Sign
out* deletes it. Your password is never seen. Without your own registration it
uses Microsoft's public *Graph Command Line Tools* application, which is
pre-approved in almost every tenant.

---

## Search with Claude

A built-in MCP server hands the archive to Claude Code or Claude Desktop — or
any other MCP client: it searches, reads the sources and answers with citations
— over your own mail and chats, not over the open web. *Settings* prints the
exact snippet to paste into your client.

There are two routes, and the app controls them differently. It runs the **HTTP
endpoint** itself; *Start* / *Stop* and the autostart apply to that one. A client
can also launch the server **as a subprocess** (stdio) — that is how Claude
Desktop does it, and it works whether or not this app is running. One switch
covers both: *Allow MCP access*, off, makes the server refuse to serve either
way.

> The server serves your complete mail and chat history and has **no
> authentication**. It binds to `127.0.0.1` only and checks the `Host` and
> `Origin` headers, so a web page you happen to visit cannot reach it through
> your browser. Leave it that way.

---

## Optional: Ollama

Without [Ollama](https://ollama.com), Munimentum exports, indexes and searches
by text — that is the whole app minus two features. With it, similar search and
the AI summary become available, both running on your machine; nothing is sent
anywhere. The app offers to help you install it, and you can switch it off for
good in *Settings* if you would rather not.

---

## From source

Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate          # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python3 app.py
```

That is the whole app. The individual export and index scripts still run on
their own, and each explains itself with `--help`; how the pieces fit together
is written in their headers rather than repeated here.

Bug reports are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the fastest
way to send one, and [SECURITY.md](SECURITY.md) if it is a security issue.
