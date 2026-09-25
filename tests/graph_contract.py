"""Which query options each Microsoft Graph endpoint takes – for the test
fakes, so they answer a request Graph would refuse the way Graph does.

The fakes of the export tests used to take any option they were handed.
That is how `$top` rode along on three listings Graph answers with a 400
(`/me/joinedTeams` and `/teams/{id}/channels` until 13.4.1, a chat's
`/members` until 14.0.3 – there every 1:1 chat whose members were not
expanded became "Unbekannt", was renamed, and had each message embedded
anew). Here every endpoint the exports ask is named once with the options
it accepts: what the Graph documentation lists for it and the app sends
in real runs. A fake that calls `check(url, params)` refuses an option
outside that set with the 400 Graph sends, and refuses an endpoint missing
from the table outright – a new endpoint gets its line here, looked up in
the documentation, before any test of it can pass.

`{}` in a pattern stands for one path segment (an id, a share token);
`$value`, `delta` and the `microsoft.graph.*` casts are literal.
"""

import re
from urllib.parse import parse_qsl, urlsplit

import requests

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
BETA_ROOT = "https://graph.microsoft.com/beta"

# Endpoint -> the query options it takes. Empty means: none at all.
RULES = {
    # Teams
    "/me": {"$select"},
    "/me/chats": {"$top", "$expand", "$orderby", "$select", "$filter"},
    "/me/chats/{}": {"$expand", "$select"},
    "/me/chats/{}/members": set(),
    "/me/chats/{}/messages": {"$top", "$orderby", "$filter"},
    "/me/joinedTeams": set(),
    "/teams/{}/channels": {"$filter", "$select"},
    "/teams/{}/channels/{}/messages": {"$top", "$expand"},
    "/teams/{}/channels/{}/messages/delta": {"$top", "$expand", "$filter", "$deltatoken",
                                             "$skiptoken"},
    "/teams/{}/channels/{}/messages/{}/replies": {"$top"},
    "/teams/{}/channels/{}/filesFolder": set(),
    "/drives/{}": {"$select"},
    "/drives/{}/items/{}": {"$select", "$expand"},
    "/drives/{}/items/{}/children": {"$select", "$top", "$expand"},
    "/drives/{}/items/{}/content": set(),
    "/shares/{}/driveItem": {"$select", "$expand"},
    "/shares/{}/driveItem/content": set(),
    # Outlook
    "/me/mailFolders": {"$top", "$select", "includeHiddenFolders"},
    "/me/mailFolders/{}/childFolders": {"$top", "$select", "includeHiddenFolders"},
    "/me/mailFolders/{}/messages": {"$filter", "$count", "$top", "$select", "$orderby"},
    "/me/messages/{}/$value": set(),
    "/me/calendars": {"$top", "$select"},
    "/me/events/{}": {"$select"},
    "/me/contactFolders": {"$top"},
    "/me/contacts/{}": {"$select"},
    # Planner
    "/planner/plans/{}": set(),
    "/planner/plans/{}/details": set(),
    "/planner/tasks/{}/details": set(),
    # SharePoint
    "/sites/{}": set(),
    "/sites/{}/sites": set(),
    "/sites/{}/drives": set(),
    "/sites/{}/pages/microsoft.graph.sitePage": set(),
    "/sites/{}/pages/{}/microsoft.graph.sitePage": {"$expand"},
    # OneNote
    "/me/onenote/notebooks/{}": {"$expand"},
    "/me/onenote/pages/{}/content": set(),
    # The organization
    "/users/delta": {"$select", "$deltatoken", "$skiptoken"},
    "/users/{}": {"$select"},
    "/users/{}/manager": {"$select"},
}


def _path(url):
    parts = urlsplit(url)
    path = parts.path
    for root in (GRAPH_ROOT, BETA_ROOT):
        prefix = urlsplit(root).path
        if path.startswith(prefix):
            path = path[len(prefix):]
    return path.rstrip("/") or "/", parts.query


def endpoint(url):
    """The table's name for the endpoint `url` asks, or None."""
    path, _query = _path(url)
    for name in sorted(RULES, key=lambda n: (n.count("{}"), -len(n))):
        pattern = "^" + re.escape(name).replace(r"\{\}", "[^/]+") + "$"
        if re.match(pattern, path):
            return name
    return None


def options(url, params=None):
    """Every query option the request carries: in the URL and in params."""
    _path_part, query = _path(url)
    names = {k for k, _v in parse_qsl(query, keep_blank_values=True)}
    names |= set(params or {})
    return {n for n in names if n.startswith("$") or n == "includeHiddenFolders"}


class GraphRefusal(requests.HTTPError):
    """What requests raises for a 400 – with the response on it, so the
    exports' own error handling reads status and body as in a real run."""

    def __init__(self, url, bad):
        message = f"Query option(s) {', '.join(sorted(bad))} not allowed"
        super().__init__(f"400 Client Error: Bad Request for url: {url}")
        text = ('{"error":{"code":"BadRequest","message":"' + message + '"}}')
        self.response = type("Response", (), {"status_code": 400, "text": text,
                                               "json": lambda _self: {"error": {
                                                   "code": "BadRequest", "message": message}}})()


def check(url, params=None):
    """Hold one request against the table: an unknown endpoint fails the
    test, an option the endpoint does not take is Graph's 400."""
    name = endpoint(url)
    if name is None:
        raise AssertionError(f"Graph endpoint not in tests/graph_contract.py: {url} – "
                             "look up the options it takes and add its line")
    bad = options(url, params) - RULES[name]
    if bad:
        raise GraphRefusal(url, bad)
