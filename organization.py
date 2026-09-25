#!/usr/bin/env python3
"""
organization.py – the organization as one file, and how to read it.

The Teams profile card shows where someone stands: the managers above,
the people below. Microsoft keeps that in the directory (every user's
`manager`); org_export.py reads it through the users' delta feed and
writes the organization as ONE file, `teams_export/organization/
organization.json`. Every write goes through versions.py, so each change
of the organization is a version of that file – with its place in the
evidence chain – and a run that changed nothing writes nothing.

The file is canonical: people sorted by id, one person per line, empty
fields left out, no time of its own. The same organization always gives
the same bytes, which is what lets "changed" mean changed.

    {"format": 1, "me": "<id>", "people": [
    {"id": "…", "name": "…", "title": "…", "department": "…", "manager": "<id>"},
    …
    ]}

Who is in it: enabled member accounts, narrowed by the rules of the
settings (`org_rules`, over each person's path of names from the top),
that have a place in the tree – a manager among them, or someone
reporting to them. Guests and accounts standing alone (service accounts,
rooms) stay out, and so do disabled accounts (which is also what shared
and resource mailboxes are) – except where one still stands in someone's
line: a manager who left keeps the line together as Teams shows it, with
`"disabled": true`, counted as nobody. Without that, everyone below such
a manager stood at the top of the tree.

The reading side (`Org`) answers what the view asks: the top, one person
with the chain above and the reports below, and what changed between
two versions.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import folders

FORMAT = 1
ORG_DIR = "organization"
FILE = "organization.json"

# Graph's user property -> our field, in the order a person's line lists them.
FIELDS = (("displayName", "name"), ("jobTitle", "title"), ("department", "department"),
          ("officeLocation", "office"), ("companyName", "company"), ("city", "city"),
          ("country", "country"), ("mail", "mail"))
SELECT = ",".join([g for g, _ in FIELDS] + ["accountEnabled", "userType", "manager"])
# What a change of a person names; `manager` is a move, not a change.
COMPARED = tuple(f for _, f in FIELDS)


# ---------------------------------------------------------------------------
# Writing: Graph's users -> the canonical text
# ---------------------------------------------------------------------------
def person_of(user, links=False):
    """One directory user as the file keeps them, or None when the account
    does not belong to the organization (a guest; a disabled account,
    unless `links` – then it comes marked, for the line it may hold)."""
    if (user.get("userType") or "Member") != "Member":
        return None
    disabled = user.get("accountEnabled") is False
    if disabled and not links:
        return None
    p = {"id": user["id"]}
    for graph, field in FIELDS:
        value = str(user.get(graph) or "").strip()
        if value:
            p[field] = value
    if user.get("manager"):
        p["manager"] = user["manager"]
    if disabled:
        p["disabled"] = True
    return p


def build(users, me=None, rules=None):
    """The organization from every directory user (id -> Graph's fields
    plus `manager` as an id): the members, a manager only when they are
    one too, and nobody without a place in the tree. `rules` (folders.py,
    the last matching one wins) narrow it over each person's path – the
    names from the top down to them, "Greta Gast/Bob Baumeister": a rule
    ending in `/**` takes or leaves a whole part of the tree."""
    cand = {}
    for user in users.values():
        p = person_of(user, links=True)
        if p is not None:
            cand[p["id"]] = p
    for p in cand.values():
        if p.get("manager") not in cand or p.get("manager") == p["id"]:
            p.pop("manager", None)
    active = {pid for pid, p in cand.items() if not p.get("disabled")}
    if rules:
        tree = Org({"people": list(cand.values())})
        active = {pid for pid in active if folders.gilt(path_of(tree, pid), rules)}
    # The disabled accounts that still stand in the line of someone kept.
    links = set()
    for pid in active:
        seen, boss = {pid}, cand[pid].get("manager")
        while boss and boss not in seen and boss not in active:
            seen.add(boss)
            if cand[boss].get("disabled"):
                links.add(boss)
            boss = cand[boss].get("manager")
    people = {pid: cand[pid] for pid in active | links}
    for p in people.values():
        if p.get("manager") not in people:
            p.pop("manager", None)
    managers = {p["manager"] for p in people.values() if p.get("manager")}
    kept = [p for p in people.values() if p.get("manager") or p["id"] in managers]
    return {"format": FORMAT, "me": me if me in active else None,
            "people": sorted(kept, key=lambda p: p["id"])}


def path_of(org, pid):
    """The names from the top down to `pid`, as the rules see them."""
    names = [(org.people[i].get("name") or i).replace("/", "-") for i in org.chain(pid) + [pid]]
    return "/".join(names)


def text_of(org):
    """The canonical bytes: one person per line, keys in a fixed order."""
    order = ["id"] + [f for _, f in FIELDS] + ["manager", "disabled"]
    lines = [json.dumps({k: p[k] for k in order if k in p}, ensure_ascii=False,
                        separators=(",", ":")) for p in org["people"]]
    head = json.dumps({"format": org.get("format", FORMAT), "me": org.get("me")})[:-1]
    return head + ', "people": [\n' + ",\n".join(lines) + "\n]}\n"


# ---------------------------------------------------------------------------
# Reading: one version, as the view walks it
# ---------------------------------------------------------------------------
class Org:
    """One version of the organization, with the tree built once."""

    def __init__(self, raw):
        data = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
        self.me = data.get("me")
        self.people = {p["id"]: p for p in data.get("people") or [] if p.get("id")}
        self.disabled = {pid for pid, p in self.people.items() if p.get("disabled")}
        self.active = len(self.people) - len(self.disabled)
        self.parent = {pid: p.get("manager") for pid, p in self.people.items()
                       if p.get("manager") in self.people and p.get("manager") != pid}
        self._cut_circles()
        self.reports = {}
        for pid, boss in self.parent.items():
            self.reports.setdefault(boss, []).append(pid)
        for ids in self.reports.values():
            ids.sort(key=lambda i: _name_key(self.people[i]))
        self.total = self._totals()

    def _cut_circles(self):
        """A directory can name managers in a circle (the top reporting to
        someone below). Walk up from everyone once; a walk that meets its
        own path cuts the circle at its smallest id, which then stands at
        the top – the same cut for the same file."""
        done = set()
        for start in sorted(self.people):
            path, on_path, cur = [], set(), start
            while cur is not None and cur not in done and cur not in on_path:
                path.append(cur)
                on_path.add(cur)
                cur = self.parent.get(cur)
            if cur is not None and cur in on_path:
                self.parent.pop(min(path[path.index(cur):]), None)
            done.update(path)

    def _totals(self):
        """Everyone below each person – leaves first, without recursion."""
        order, stack = [], [pid for pid in self.people if pid not in self.parent]
        while stack:
            pid = stack.pop()
            order.append(pid)
            stack.extend(self.reports.get(pid, ()))
        total = {}
        for pid in reversed(order):
            total[pid] = sum((c not in self.disabled) + total[c] for c in self.reports.get(pid, ()))
        return total

    def manager(self, pid):
        return self.parent.get(pid)

    def roots(self):
        """The people at the top, the largest part of the tree first."""
        tops = [pid for pid in self.people if self.manager(pid) is None]
        return sorted(tops, key=lambda i: (-self.total.get(i, 0), _name_key(self.people[i])))

    def chain(self, pid):
        """The managers above `pid`, top first."""
        out, seen = [], {pid}
        boss = self.manager(pid)
        while boss and boss not in seen:
            out.append(boss)
            seen.add(boss)
            boss = self.manager(boss)
        return out[::-1]

    def card(self, pid):
        """A person as a card names them: who, what, and how many below."""
        p = self.people[pid]
        out = {"id": pid, "name": p.get("name") or "", "title": p.get("title"),
               "department": p.get("department"),
               "reports": len(self.reports.get(pid, ())), "total": self.total.get(pid, 0)}
        if pid in self.disabled:
            out["disabled"] = True
        return out

    def person(self, pid):
        """One person with everything the file knows of them."""
        p = self.people[pid]
        out = self.card(pid)
        for _, field in FIELDS[3:]:
            if p.get(field):
                out[field] = p[field]
        return out

    def roles(self, contains="", department=""):
        """Every title the version knows, with how many hold it – the most
        held first. `contains` and `department` narrow, case-insensitively."""
        low, dept = str(contains or "").casefold(), str(department or "").casefold()
        count = {}
        for pid, p in self.people.items():
            title = p.get("title")
            if pid in self.disabled or not title or low not in title.casefold():
                continue
            if dept and dept not in (p.get("department") or "").casefold():
                continue
            count[title] = count.get(title, 0) + 1
        return sorted(({"title": t, "count": n} for t, n in count.items()),
                      key=lambda r: (-r["count"], r["title"].casefold()))

    def with_role(self, role, exact=False, department=""):
        """The people whose title is `role` (`exact`), or contains it –
        by name."""
        want, dept = str(role or "").casefold(), str(department or "").casefold()
        out = []
        for pid, p in self.people.items():
            title = (p.get("title") or "").casefold()
            if pid in self.disabled or not title or (title != want if exact else want not in title):
                continue
            if dept and dept not in (p.get("department") or "").casefold():
                continue
            out.append(pid)
        return sorted(out, key=lambda i: _name_key(self.people[i]))

    def below(self, pid):
        """Everyone below `pid`, level by level: [(id, level)], level 1 the
        direct reports."""
        out, level, depth = [], list(self.reports.get(pid, ())), 1
        while level:
            out += [(i, depth) for i in level]
            level = [c for i in level for c in self.reports.get(i, ())]
            depth += 1
        return out

    def find(self, wanted):
        """A person by id, exact name or part of a name: (id, None) for one,
        (None, [ids]) for several or none."""
        wanted = str(wanted or "").strip()
        if wanted in self.people:
            return wanted, None
        low = wanted.casefold()
        hits = [i for i, p in self.people.items() if low in (p.get("name") or "").casefold()]
        exact = [i for i in hits if (self.people[i].get("name") or "").casefold() == low]
        if len(exact) == 1 or len(hits) == 1:
            return (exact or hits)[0], None
        return None, sorted(hits, key=lambda i: _name_key(self.people[i]))


def _name_key(p):
    return ((p.get("name") or "").casefold(), p.get("id") or "")


def changes(old, new):
    """What differs between two versions: who joined, who left, who moved
    to another manager, whose fields changed – each with the fields that
    did. `old` may be None (the first version: nothing to compare)."""
    if old is None:
        return []
    out = []
    was = {pid for pid in old.people if pid not in old.disabled}
    for pid, p in new.people.items():
        if pid in new.disabled:
            continue
        q = old.people.get(pid)
        if pid not in was:
            out.append({"kind": "joined", "id": pid, "name": p.get("name") or "",
                        "title": p.get("title")})
            continue
        fields = {f: [q.get(f), p.get(f)] for f in COMPARED if q.get(f) != p.get(f)}
        moved = old.manager(pid) != new.manager(pid)
        if moved:
            fields["manager"] = [_name_of(old, old.manager(pid)), _name_of(new, new.manager(pid))]
        if fields:
            out.append({"kind": "moved" if moved else "changed", "id": pid,
                        "name": p.get("name") or "", "title": p.get("title"), "fields": fields})
    for pid in was:
        if pid not in new.people or pid in new.disabled:
            q = old.people[pid]
            out.append({"kind": "left", "id": pid, "name": q.get("name") or "",
                        "title": q.get("title")})
    rank = {"joined": 0, "moved": 1, "changed": 2, "left": 3}
    return sorted(out, key=lambda c: (rank[c["kind"]], c["name"].casefold(), c["id"]))


def _name_of(org, pid):
    return (org.people.get(pid) or {}).get("name") if pid else None


# ---------------------------------------------------------------------------
# The versions the archive holds (evidence.py, versions.py)
# ---------------------------------------------------------------------------
def file_path(teams_dir):
    return Path(teams_dir) / ORG_DIR / FILE


def versions_of(data_dir, teams_dir):
    """Every version of the organization file the archive still holds,
    newest first – {sha256, captured, current}: what the evidence chain
    knows of it, else the file alone (a run the chain has not seen yet)."""
    import evidence
    import versions
    path = file_path(teams_dir)
    if not path.is_file():
        return []
    data = Path(data_dir)
    rel = versions.rel_of(path, data)
    found = []
    ev = evidence.Evidence(data)
    try:
        if rel is not None and ev.exists():
            found = [v for v in ev.history(rel) if v.get("available")]
    finally:
        ev.close()
    if not any(v.get("current") for v in found):
        written = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")
        found.insert(0, {"sha256": versions.sha256_file(path), "captured": written,
                         "current": True})
    return [{"sha256": v["sha256"], "captured": v.get("captured") or v.get("modified"),
             "current": bool(v.get("current"))} for v in found]


def read_version(data_dir, teams_dir, version):
    """The bytes of one version (an entry of versions_of), None when gone."""
    path = file_path(teams_dir)
    if version.get("current"):
        return path.read_bytes() if path.is_file() else None
    import evidence
    import versions
    data = Path(data_dir)
    ev = evidence.Evidence(data)
    try:
        return ev.bytes_of(versions.rel_of(path, data), version["sha256"])
    finally:
        ev.close()


class Archive:
    """The organization's versions of one archive, parsed on demand – the
    few most recent ones kept, since a whole tenant is megabytes of JSON."""

    def __init__(self, data_dir, teams_dir, keep=3):
        self.data_dir, self.teams_dir, self.keep = data_dir, teams_dir, keep
        self._parsed = {}

    def versions(self):
        return versions_of(self.data_dir, self.teams_dir)

    def org(self, version):
        sha = version["sha256"]
        if sha not in self._parsed:
            raw = read_version(self.data_dir, self.teams_dir, version)
            if raw is None:
                return None
            while len(self._parsed) >= self.keep:
                self._parsed.pop(next(iter(self._parsed)))
            self._parsed[sha] = Org(raw)
        return self._parsed[sha]
