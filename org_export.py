#!/usr/bin/env python3
"""
org_export.py – the organization as Teams shows it, kept version by version.

Reads every directory user with their manager through the users' delta
feed (`/users/delta`, `$select` with `manager`): the first round brings
the whole tenant – measured on one with about 15 000 accounts, some 170
users a page at a good second each, so a minute or two – every later
round only who changed since the stored deltaLink, which takes seconds.
Managers the feed does not name are asked for directly afterwards
(settle_managers). The raw users live in the folder's state.db (area
"users"); from them the
organization is built (organization.build) and written as ONE file,
`organization.json`, through versions.py – a run that changed nothing in
it writes nothing, a run that did leaves the earlier organization behind
as a version.

A feed the service no longer remembers (410 Gone, syncStateNotFound,
resyncRequired) is read once in full again, and so is every round of a
"Force full sync" or "Fetch again" (FULL_SYNC, RESYNC). A round that
fails part-way changes nothing: neither the users nor the pointer move,
and nobody is taken for gone.

Runs as a subprogram of app.py: the Teams export folder as the only
argument; the organization lands in its subfolder `organization/`.
SYNC_CADENCE (key "organization") paces it, SYNC_NOW steps over that;
ORG_RULES – ordered include/exclude rules over each person's path of
names from the top (folders.py, organization.build) – narrows what the
file holds, while the users in state.db stay whole, so a changed rule
takes effect on the next run without reading the directory again.
Permission: User.Read.All (or Directory.Read.All) – the manager of other
people is not part of anyone's basic profile. Progress, results and
failures are structured lines (progress.py).
"""

import json
import os
import sys
import time
from pathlib import Path

import auth
import export_util
import folders
import graph_client
import organization
import progress
import state_db

export_util.erzwinge_utf8()

GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "User.Read.All", RES + "User.Read"]
AREA = "users"
# Prefer: odata.maxpagesize on the delta feed (never $top); Graph pages
# users/delta itself below this when it expands the manager.
DELTA_PAGE = 999
_KEPT = {g for g, _ in organization.FIELDS} | {"accountEnabled", "userType"}


class Graph(graph_client.Graph):
    def __init__(self, nur_still=False):
        super().__init__(list(SCOPES), nur_still=nur_still)


class TokenClient(graph_client.TokenClient):
    pass


# ---------------------------------------------------------------------------
# The delta feed
# ---------------------------------------------------------------------------
def _manager_of(item):
    """The manager an answer names: `manager@delta` (a list; an entry with
    `@removed` takes the manager away), or a plain `manager` object.
    Absent means unchanged – `...` says so."""
    if "manager@delta" in item:
        alive = [m for m in item["manager@delta"] or [] if "@removed" not in m and m.get("id")]
        return alive[-1]["id"] if alive else None
    if "manager" in item:
        m = item["manager"]
        return m.get("id") if isinstance(m, dict) else None
    return ...


def apply(users, item):
    """One answer of the feed onto the users (id -> fields): removed,
    new, or only the fields it names changed."""
    uid = item.get("id")
    if not uid:
        return
    if "@removed" in item:
        users.pop(uid, None)
        return
    user = users.setdefault(uid, {"id": uid})
    for key in _KEPT:
        if key in item:
            user[key] = item[key]
    boss = _manager_of(item)
    if boss is not ...:
        user["manager"] = boss


def read_round(graph, url, users):
    """One round of the feed onto `users`: every page, and the deltaLink
    the last one carries. Raises on the first refused page – the caller
    then keeps what it had."""
    head = {"Prefer": f"odata.maxpagesize={DELTA_PAGE}"}
    link, seen = None, 0
    while url:
        page = graph.get(url, extra_headers=head)
        for item in page.get("value") or []:
            apply(users, item)
            seen += 1
        progress.melde(seen, what="users")
        link = page.get("@odata.deltaLink") or link
        url = page.get("@odata.nextLink")
    return link, seen


def _forgotten(e):
    """Does the service no longer know the stored link?"""
    answer = getattr(e, "response", None)
    status = int(getattr(answer, "status_code", 0) or 0)
    if status == 410:
        return True
    text = str(getattr(answer, "text", "") or "").lower()
    return 400 <= status < 500 and ("syncstatenotfound" in text or "resyncrequired" in text)


def _first_url():
    return f"{GRAPH}/users/delta?$select={organization.SELECT}"


# ---------------------------------------------------------------------------
# Managers the feed did not name
# ---------------------------------------------------------------------------
# The delta feed does not name every manager: on a real tenant (about 5 000
# people) dozens of colleagues whom Teams shows in a proper line came
# without one and stood at the top. Whoever is left without a manager is
# therefore asked once more, directly (`/users/{id}/manager`, twenty to a
# $batch); a 404 there is the answer "nobody above", kept for a week before
# it is asked again. A manager the feed never named as a user is fetched
# the same way, so the line above does not end at a stranger.
RECHECK_S = 7 * 86400
_USER_FIELDS = organization.SELECT.replace(",manager", "")


def settle_managers(graph, users, now=None, rounds=12):
    """Ask Graph directly for the manager of every member the feed left
    without one (unless asked within RECHECK_S), and for every manager the
    feed named but never described – round after round, since a manager
    found that way may lack one too. Changes `users` in place; returns 1
    when a question failed (that person is asked again next run), else 0."""
    now = time.time() if now is None else now
    total = {"asked": 0, "found": 0, "fetched": 0}
    failed, tried, first = 0, set(), None
    for _ in range(rounds):
        # Every member without a manager – a disabled one too once someone
        # reports to them, since they may still hold a line together.
        bosses = {u["manager"] for u in users.values() if u.get("manager")}
        asked = {uid: f"{GRAPH}/users/{uid}/manager?$select={_USER_FIELDS}"
                 for uid, u in users.items()
                 if organization.person_of(u, links=uid in bosses) is not None
                 and not u.get("manager")
                 and now - float(u.get("manager_checked") or 0) >= RECHECK_S}
        unknown = {u["manager"] for u in users.values()
                   if u.get("manager") and u["manager"] not in users} - tried
        tried |= unknown
        fetched = {mid: f"{GRAPH}/users/{mid}?$select={_USER_FIELDS}" for mid in unknown}
        if not asked and not fetched:
            break
        try:
            answers = graph.batch_get(list(asked.values()) + list(fetched.values()))
        except auth.TokenExpired:
            raise
        except Exception as e:                      # noqa: BLE001 – next run asks again
            progress.event("run.org.managers_failed", "warn", error=export_util.fehlertext(e)[:200])
            return 1
        for uid, url in asked.items():
            status, body = answers.get(url, (0, None))
            if status == 200 and isinstance(body, dict) and body.get("id"):
                boss = body["id"]
                users[uid]["manager"] = boss
                if boss not in users:
                    users[boss] = {"id": boss, **{k: v for k, v in body.items() if k in _KEPT}}
                total["found"] += 1
            else:
                # A 404 says "nobody above" for a week; anything else is
                # asked again on the next run – but not again in this one.
                users[uid]["manager_checked"] = now if status == 404 else now - RECHECK_S + 1
                if status != 404:
                    failed += 1
                    first = first or _answer_text(status, body)
        for mid, url in fetched.items():
            status, body = answers.get(url, (0, None))
            if status == 200 and isinstance(body, dict):
                users.setdefault(mid, {"id": mid}).update(
                    {k: v for k, v in body.items() if k in _KEPT})
            elif status in (403, 404):
                # Not to be described (gone, or a contact): the line ends here.
                users.setdefault(mid, {"id": mid, "userType": "Unknown"})
            else:
                failed += 1
                first = first or _answer_text(status, body)
        total["asked"] += len(asked)
        total["fetched"] += len(fetched)
    progress.event("run.org.managers", **total)
    if failed:
        # Which answer came, in Graph's own words – the count alone does
        # not say whether a permission is missing or the service hiccupped.
        progress.event("run.org.managers_refused", "warn", n=failed, error=first or "?")
    return 1 if failed else 0


def _answer_text(status, body):
    code = export_util.graph_code(body) if isinstance(body, dict) else ""
    message = ((body or {}).get("error") or {}).get("message") if isinstance(body, dict) else ""
    return f"HTTP {status} {code} {message or ''}".strip()[:200]


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def _load(db):
    users = {}
    for uid, raw in db.saetze_lesen(AREA).items():
        try:
            users[uid] = json.loads(raw)
        except ValueError:
            continue
    return users


def _store(db, users, before, link, whole):
    """The users and the link, the link last: a run cut short between the
    two leaves no link, and the next one reads everything again."""
    db.kv_loeschen("delta")
    if whole:
        db.saetze_leeren(AREA)
        changed = users
    else:
        changed = {uid: u for uid, u in users.items() if before.get(uid) != u}
        db.saetze_loeschen(AREA, [uid for uid in before if uid not in users])
    db.saetze_schreiben(AREA, {uid: json.dumps(u, ensure_ascii=False, sort_keys=True)
                               for uid, u in changed.items()})
    if link:
        db.kv_schreiben("delta", link)


def rules():
    """ORG_RULES from the app, else the setting – empty: everyone."""
    raw = os.environ.get("ORG_RULES")
    if raw is None:
        import settings
        raw = settings.value("org_rules") or ""
    return folders.lies_regeln(raw)


def run(graph, out):
    """One run: the feed, the users, the file. Returns (changes, people,
    errors) and says them as the step's result."""
    folder = Path(out) / organization.ORG_DIR
    folder.mkdir(parents=True, exist_ok=True)
    db = state_db.StateDb(folder)
    cadence = export_util.kadenzen().get("organization") or "always"
    if not export_util.einheit_faellig(db, cadence):
        progress.event("run.org.paced", cadence=cadence)
        progress.ergebnis(0, extra={"skipped": 1})
        return 0, 0, 0
    link = None if export_util.abgleich() else db.kv_lesen("delta")
    if export_util.abgleich():
        progress.event("run.resync")
    before = _load(db) if link else {}
    users = {uid: dict(u) for uid, u in before.items()}
    whole = not link
    try:
        try:
            new_link, seen = read_round(graph, link or _first_url(), users)
        except Exception as e:                      # noqa: BLE001 – judged below
            if not link or not _forgotten(e):
                raise
            progress.event("run.org.delta_reset")
            users, whole = {}, True
            new_link, seen = read_round(graph, _first_url(), users)
        me = (graph.get(f"{GRAPH}/me?$select=id") or {}).get("id") or db.kv_lesen("me")
    except auth.TokenExpired:
        raise
    except Exception as e:                          # noqa: BLE001 – one line, no half state
        progress.event("run.org.failed", "err", error=export_util.fehlertext(e)[:300])
        progress.ergebnis(0, errors=1)
        return 0, 0, 1
    errors = settle_managers(graph, users)
    _store(db, users, before, new_link, whole)
    if me:
        db.kv_schreiben("me", me)
    db.kv_schreiben("last_sync", str(time.time()))
    org = organization.build(users, me=me, rules=rules())
    path = folder / organization.FILE
    text = organization.text_of(org)
    old = path.read_bytes() if path.is_file() else None
    n = 0
    if old != text.encode("utf-8"):
        try:
            earlier = organization.Org(old) if old else None
        except ValueError:
            earlier = None
        # What changed, by person; the first version counts everyone in it.
        n = len(organization.changes(earlier, organization.Org(org))) if earlier \
            else sum(1 for p in org["people"] if not p.get("disabled"))
        n = max(n, 1)
        export_util.schreibe_atomar(path, text)
    count = sum(1 for p in org["people"] if not p.get("disabled"))
    progress.event("run.org.done", n=count, users=seen, changes=n)
    progress.ergebnis(n, unchanged=max(0, count - n), errors=errors, extra={"people": count})
    return n, count, errors


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if export_util.hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__)
        return
    out = export_util.ausgabeordner(argv)
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    try:
        run(graph, out)
    except auth.TokenExpired:
        progress.fehler("token_expired")
        sys.exit(1)


if __name__ == "__main__":
    main()
