# Contributing

Thank you for looking. This is short on purpose, and reading it will save you
time.

## Bug reports: yes, please

The most useful thing you can send me is a bug report.

Use **Report a problem** in the app — in the log bar at the bottom, and again in
*Settings*. It fills in a GitHub issue with the log and the details I would
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

One thing worth knowing up front: **the code comments are German.** The
interface, the README and the release notes are English, and the interface texts
live in `lang/`.
