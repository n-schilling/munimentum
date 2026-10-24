"""
org_csv.py – the organization as CSV: the people of one version, or the
changes over a span of versions, for Excel, Power BI or draw.io.

Two dialects: `excel` (UTF-8 with a byte order mark, `;` between fields,
what Excel in a German locale opens with a double click) and `rfc4180`
(`,`, no byte order mark – RFC 4180, for everything else). Both end
lines with CRLF and quote what needs it.

A title is free text, and a spreadsheet takes a cell that starts with
`=`, `+`, `-` or `@` for a formula: such a cell gets a `'` in front
(`defuse`, OWASP "CSV Injection"). Numbers are written as numbers.

The people's columns come in groups. The base – id, name, title,
department, company (the legal entity, where a group has several), the
manager's id and name, how many report directly and in all – is always
there; `place` adds office, city and country,
`seniority` the cleaned title, the level by the role model, why, and the
depth in the tree, `path` the names from the top down, `mail` the
address, `disabled` the accounts the directory has switched off (as rows,
with a column saying so). All groups together are everything the
archive holds of a person.
"""
import csv
import io

import organization
import seniority

GROUPS = ("place", "seniority", "path", "mail", "disabled")
DEFAULT_GROUPS = ("place", "seniority", "path")
DIALECTS = ("excel", "rfc4180")
KINDS = ("people", "changes")

_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def groups_of(raw):
    """The groups a `columns` value names, in their fixed order; `none`
    is the base alone (an empty value never reaches a route); unknown
    names are an error (None)."""
    wanted = [g.strip() for g in str(raw or "").split(",") if g.strip() and g.strip() != "none"]
    if any(g not in GROUPS for g in wanted):
        return None
    return tuple(g for g in GROUPS if g in wanted)


def defuse(value):
    """A cell as text a spreadsheet shows and never computes."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    return "'" + text if text.startswith(_FORMULA) else text


def render(header, rows, dialect="rfc4180"):
    """The file's bytes."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";" if dialect == "excel" else ",", lineterminator="\r\n")
    w.writerow(header)
    for row in rows:
        w.writerow([defuse(v) for v in row])
    text = buf.getvalue()
    return ("\ufeff" + text if dialect == "excel" else text).encode("utf-8")


def people(org, ids, groups=DEFAULT_GROUPS, model=None, overrides=None):
    """(header, rows) for the people `ids` of one version, in that order;
    disabled accounts only with the group `disabled`."""
    header = ["id", "name", "title", "department", "company"]
    if "place" in groups:
        header += ["office", "city", "country"]
    if "mail" in groups:
        header += ["mail"]
    header += ["manager_id", "manager_name", "direct_reports", "total_below"]
    if "seniority" in groups:
        header += ["title_group", "level", "level_source", "depth"]
    if "path" in groups:
        header += ["path"]
    if "disabled" in groups:
        header += ["disabled"]
    m = seniority.Model(model, overrides) if "seniority" in groups else None
    reports = seniority.direct_reports(org)
    rows = []
    for pid in ids:
        if pid not in org.people or (pid in org.disabled and "disabled" not in groups):
            continue
        p = org.people[pid]
        boss = org.manager(pid)
        row = [pid, p.get("name") or "", p.get("title"), p.get("department"), p.get("company")]
        if "place" in groups:
            row += [p.get("office"), p.get("city"), p.get("country")]
        if "mail" in groups:
            row += [p.get("mail")]
        # Both counts are of active people, as the line stands: a
        # switched-off manager in between hands their reports up – the
        # count the level below reads, and the pyramid and org_health too.
        led = seniority.leads(org, reports, pid)
        row += [boss, (org.people.get(boss) or {}).get("name") if boss else None,
                led, org.total.get(pid, 0)]
        if m:
            title = p.get("title")
            level, source = m.place(title, bool(led))
            row += [m.normalize(title) if str(title or "").strip() else None, level, source,
                    seniority.depth(org, pid)]
        if "path" in groups:
            row += [" / ".join(org.people[i].get("name") or i for i in org.chain(pid) + [pid])]
        if "disabled" in groups:
            row += [pid in org.disabled]
        rows.append(row)
    return header, rows


def everyone(org):
    """Everyone of a version, top down: each root, then its tree level by
    level – the order an organization chart reads."""
    out = []
    for root in org.roots():
        out.append(root)
        out += [pid for pid, _lv in org.below(root)]
    return out


def part(org, root):
    """`root` and everyone below."""
    return [root] + [pid for pid, _lv in org.below(root)]


CHANGE_HEADER = ["version_date", "version", "rules_changed", "kind", "id", "name",
                 "field", "before", "after"]


def changes(steps, below=None):
    """(header, rows): one row per changed field – a joiner and a leaver
    one row each, with the title as the field. `steps` are (stamp,
    rules_changed, old Org, new Org), oldest first – any iterable, read
    once, so a long span never holds more than one pair. `below` keeps
    the people of that person's part, in either version of a step."""
    rows = []
    for stamp, rules_changed, old, new in steps:
        when = str(stamp.get("captured") or "")[:10]
        version = stamp["sha256"][:16]
        only = None
        if below is not None:
            only = {pid for o in (old, new) if below in o.people for pid in part(o, below)}
        for c in organization.changes(old, new):
            if only is not None and c["id"] not in only:
                continue
            head = [when, version, rules_changed, c["kind"], c["id"], c["name"]]
            if c["kind"] == "joined":
                rows.append(head + ["title", None, c.get("title")])
            elif c["kind"] == "left":
                rows.append(head + ["title", c.get("title"), None])
            else:
                for field, (before, after) in c["fields"].items():
                    rows.append(head + [field, before, after])
    return CHANGE_HEADER, rows


def filename(kind, stamp, since=None):
    """`organization-<day>-<checksum, 8>-people.csv`, or for changes
    `organization-<first day>-<last day>-changes.csv`."""
    day = str(stamp.get("captured") or "")[:10] or "undated"
    if kind == "changes":
        first = str((since or {}).get("captured") or "")[:10] or "undated"
        return f"organization-{first}-{day}-changes.csv"
    return f"organization-{day}-{stamp['sha256'][:8]}-people.csv"
