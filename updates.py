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
"""

import re

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


def check(current, repo, timeout=4.0, enabled=True):
    """Look once. Never raises – a failure here must not hold anything up.

    Hence a single catch branch around everything: not just around the
    request. An unexpectedly shaped answer is as little a reason to fail the
    app's startup as a missing network.
    """
    out = {"status": "off", "current": current, "latest": None,
           "url": None, "newer": False, "ahead": False, "error": None}
    if not enabled:
        return out
    try:
        import requests
        r = requests.get(API.format(repo=repo), timeout=timeout,
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 404:
            # No release published yet – or only drafts and pre-releases,
            # which this endpoint does not count. Not an error case.
            out["status"] = "none"
            return out
        if r.status_code != 200:
            out["status"], out["error"] = "error", f"HTTP {r.status_code}"
            return out
        daten = r.json()
        tag = (daten.get("tag_name") or daten.get("name") or "").strip()
        if not tag:
            out["status"] = "none"
            return out
        out["status"] = "ok"
        out["latest"] = tag.lstrip("vV")
        out["url"] = daten.get("html_url")
        out["newer"] = is_newer(tag, current)
        # Asked the other way round – and deliberately not derived as "not
        # newer": with equal versions and with incomparable numbers both are
        # False, and rightly so.
        out["ahead"] = is_newer(current, tag)
    except Exception as e:
        out.update(status="error", latest=None, url=None, newer=False,
                   ahead=False, error=f"{type(e).__name__}: {e}")
    return out
