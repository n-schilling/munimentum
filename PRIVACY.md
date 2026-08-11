# Privacy

Munimentum has no backend. There is no account, no sign-up, no telemetry and no
analytics, and nothing you export ever reaches the author of this software.

## What leaves your machine

| To | Why | Optional |
|---|---|---|
| Microsoft Graph | fetching your own data — this *is* the export | no |
| `api.github.com` | one check at startup for a newer release | yes, *Settings* |
| your local Ollama | semantic search and the AI summary | it never leaves the machine |

That is the complete list. No crash reports, no usage statistics, no error
tracking, no fonts or scripts loaded from a CDN.

## What stays here

Exports, search index, settings and the access token live in a folder in your
user profile — the path is shown in *Settings*. The app serves its interface on
`127.0.0.1` and is not reachable from your network. Delete the folder and
nothing of it remains.

## Two places worth knowing about

**Report a problem** builds a GitHub issue from the log and some system details.
The app sends nothing: e-mail addresses and user names in paths are replaced,
the full text is shown for you to edit, and you submit the form yourself.
Folder names and subject lines are beyond what a pattern can catch, so read it
before posting.

**The MCP server** hands your archive to Claude on `127.0.0.1`. What Claude does
with the passages it reads is governed by your agreement with Anthropic, not by
this app.
