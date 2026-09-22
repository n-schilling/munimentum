#!/usr/bin/env python3
"""
updates.py – check whether a newer release exists.

Asks the GitHub API once at startup and leaves a note if warranted. Nothing
more: nothing is downloaded, nothing replaced. Whoever wants to update
fetches the file themselves from the releases page – for an unsigned app, a
silent self-replacement is nothing one should want anyway.

Can be turned off (setting "update_check"). That is no formality: the app
otherwise talks to nothing but Microsoft Graph and the local Ollama, and
nobody should get this one outward connection unasked.

Four outcomes, all four normal:

    ok      Release found. "newer" says whether it is newer than one's own;
            "ahead" says the opposite – one's own is HIGHER than anything
            published, so this is a self-built version.
    none    There is no release at all yet (GitHub then answers 404)
    off     The check is turned off
    error   No network, rate-limited, and the like

Two cases are worth reporting: newer=True ("something newer exists") and
ahead=True ("you are ahead"). The second is no error, but "you are up to
date" would simply be untrue there – and whoever runs an unpublished
version should know it.

GitHub allows an address 60 anonymous requests an hour, shared by every
start of this app and by everything else on the same connection that
talks to api.github.com without a token. Two things keep the check out of
that limit: the ETag of the last answer goes back as If-None-Match, and a
304 costs nothing (once the limit IS used up, even that request is
refused – the ETag keeps the check from reaching it, it does not get past
it); and when the limit refuses the check anyway, the refusal's headers
say when it opens again – `retry_at` carries that time so the page can
say it instead of a bare "HTTP 403".
"""

import re
from datetime import datetime, timedelta

API = "https://api.github.com/repos/{repo}/releases/latest"

_TEIL = re.compile(r"\d+")


def parse_version(text):
    """"v1.12.0" -> (1, 12, 0). Unreadable input yields an empty tuple.

    Deliberately lenient: pre-releases like "1.2.0-beta.1" are reduced to
    their numbers. A tag from which no number can be read at all counts as
    incomparable – then better to report nothing than something wrong.
    """
    kern = str(text or "").strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
    return tuple(int(x) for x in _TEIL.findall(kern)[:4])


def is_newer(latest, current):
    """Is `latest` a higher version than `current`?"""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False                     # incomparable -> do not claim it
    laenge = max(len(a), len(b))
    a += (0,) * (laenge - len(a))        # 1.2 and 1.2.0 are the same version
    b += (0,) * (laenge - len(b))
    return a > b


def _headers(r):
    """The headers of an answer – {} when the object has none."""
    return getattr(r, "headers", None) or {}


def retry_at(r):
    """When GitHub answers again, read from the headers of its refusal:
    the reset of the hourly window when that is used up, otherwise a
    Retry-After. None when the answer names no time – then the refusal
    is something else than a limit. Local time, seconds, no zone – like
    every other timestamp the page gets."""
    h = _headers(r)
    try:
        if h.get("X-RateLimit-Remaining") == "0" and h.get("X-RateLimit-Reset"):
            when = datetime.fromtimestamp(int(h["X-RateLimit-Reset"]))
        elif h.get("Retry-After"):
            when = datetime.now() + timedelta(seconds=int(h["Retry-After"]))
        else:
            return None
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return when.isoformat(timespec="seconds")


def check(current, repo, timeout=4.0, enabled=True, cache=None):
    """Look once. Never raises – a failure here must not hold anything up.

    Hence a single catch branch around everything: not just around the
    request. An unexpectedly shaped answer is as little a reason to fail the
    app's startup as a missing network.

    `cache` is a dict the caller keeps between checks – the ETag of the
    last answer and what it said (`etag`, `tag`, `url`). It is sent as
    If-None-Match, a 304 is answered from it, a 200 replaces it in place.
    """
    out = {"status": "off", "current": current, "latest": None,
           "url": None, "newer": False, "ahead": False, "error": None,
           "retry_at": None}
    if not enabled:
        return out
    cache = cache if cache is not None else {}
    try:
        import requests
        headers = {"Accept": "application/vnd.github+json"}
        if cache.get("etag") and cache.get("tag"):
            headers["If-None-Match"] = cache["etag"]
        r = requests.get(API.format(repo=repo), timeout=timeout, headers=headers)
        if r.status_code == 404:
            # No release published yet – or only drafts and pre-releases,
            # which this endpoint does not count. Not an error case.
            out["status"] = "none"
            return out
        if r.status_code == 304:
            # Unchanged since the cached answer – and not counted by GitHub.
            tag, url = str(cache.get("tag") or ""), cache.get("url")
        elif r.status_code != 200:
            out["status"], out["error"] = "error", f"HTTP {r.status_code}"
            out["retry_at"] = retry_at(r)
            return out
        else:
            daten = r.json()
            tag = (daten.get("tag_name") or daten.get("name") or "").strip()
            url = daten.get("html_url")
            etag = _headers(r).get("ETag")
            if tag and etag:
                cache.update(etag=etag, tag=tag, url=url)
        if not tag:
            out["status"] = "none"
            return out
        out["status"] = "ok"
        out["latest"] = tag.lstrip("vV")
        out["url"] = url
        out["newer"] = is_newer(tag, current)
        # Asked the other way round – and deliberately not derived as "not
        # newer": with equal versions and with incomparable numbers both are
        # False, and rightly so.
        out["ahead"] = is_newer(current, tag)
    except Exception as e:
        out.update(status="error", latest=None, url=None, newer=False,
                   ahead=False, error=f"{type(e).__name__}: {e}")
    return out
