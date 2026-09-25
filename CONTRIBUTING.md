# Contributing

Thank you for looking. This is short on purpose, and reading it will save you
time.

## Bug reports: yes, please

The most useful thing you can send me is a bug report.

Use **Report a problem** in the app — under *Settings → App*, and in the run
window while a run is on. It fills in a GitHub issue with the log and the details I would
otherwise have to ask for (version, operating system, cores, what the index
holds), replaces e-mail addresses and user names in paths, and shows you the
whole text to edit before anything happens. You submit it yourself; the app
sends nothing.

A report written that way usually needs no follow-up questions. One written
without the log usually needs two rounds of them.

Security issues do **not** belong in a public issue — see
[SECURITY.md](SECURITY.md).

## Ideas and feature requests: yes, as an issue

Open an issue and describe what you are trying to do, rather than the solution
you have in mind. What is planned and what is deliberately left out is in
[ROADMAP.md](ROADMAP.md) — worth a look first, because some of the gaps are
decisions rather than omissions.

## Pull requests: please ask first

**I am not taking unsolicited pull requests right now.**

This is a spare-time project with a single maintainer and a fairly opinionated
shape: how the exports are laid out, what goes into the index, what the
interface does and pointedly does not do. Reviewing a change I did not expect
costs more time than I have, and turning down work someone spent an evening on
is worse for both of us than saying this plainly, here, before you start.

So: open an issue first and let us agree on the approach. If we do, I am glad to
take the patch.

## If you run it from source anyway

Setup is in the README under *From source*. Once that is done:

```
pip3 install -r requirements-dev.txt   # test and lint tools, pinned
python3 app.py                         # the app; it opens in your browser
pytest -q                              # the tests
ruff check .                           # lint
```

Both the tests and the lint have to pass before anything is released; CI runs
them on Python 3.12 and 3.13, and every bundle goes through
`packaging/smoke_test.py` before it becomes a download.

A second, smaller set drives the real page in a browser – the app with an
empty data folder, walked through as a new user sees it, the help tour
included – and it needs a browser:

```
pip3 install -r requirements-ui.txt        # Playwright, on top of the dev tools
python3 -m playwright install chromium     # once; an installed Google Chrome serves as well
pytest -q tests/ui                         # the browser tests alone
```

Without Playwright these tests skip themselves, so `pytest -q` stays as it
is; with it, they ride along. The *UI* workflow runs them on `main` and on
every pull request. A failed one leaves a screenshot in `tests/ui/output/`.

Most of them need an archive with something in it, and that archive is
generated – `testdata/` writes a whole profile with all eight sources
and an organization, every name and file in it invented. It is not a handful of files but a
year of traffic: some two and a half thousand of them, a few thousand
items, enough that a result runs over pages and a timeline needs its
band. You can open it yourself:

```
python3 -m testdata.build --profile testdaten   # writes profiles/testdaten/
python3 app.py --profile testdaten              # and look at it
```

It is the quickest way to see the interface with data in it without touching
a Microsoft account. It has a past, too (`testdata/history.py`): files with
an earlier version, an organization a week older than today's, an edited and
a deleted Teams message, a case whose items changed since they came in, a
file changed by hand – and the chain of checksums over all of it. The
browser tests use the same archive, and that is what lets them go deeper
than "every page renders": they set the search filters one by one and count
what comes back, fill a case from the search, order it into folders, write a
note and a remark, remove an item and close the case, and open an earlier
version with its changes.

Code, comments and documentation are English throughout; the interface texts
live in `lang/`, one JSON file per language.

Where things are: `app.py` is the server, the page and the three routes that
stream; the rest of `/api/v1` lives in `api_cases.py`, `api_explore.py`,
`api_archive.py` and `api_app.py`, one module per door, with what they share
in `api.py`; `openapi.yaml` describes it all and changes in the same commit.
The organization is one file `org_export.py` writes and `organization.py`
reads, walks and compares. Every export writes through `versions.py`, which
keeps what it replaces and journals the write; `evidence.py` turns that into
the chain of checksums after every run and checks it. `mcp_server.py` is the
MCP server; the app's search calls its tools in-process, so what leaves over
MCP passes `_tool()`, which keeps the answer compact, brief and within a
budget. Its prompts and case resources sit beside the tools; `instance.py`
says where a running app answers, so a citation can link into it. Every
Microsoft Graph endpoint the exports ask has its line in
`tests/graph_contract.py` with the query options it takes; the test fakes
answer anything else with Graph's own 400.
