#!/usr/bin/env python3
"""
evidence.py – what the archive can prove about itself.

Every file an export writes gets its SHA-256 into a chain that only ever
grows: `evidence/chain.jsonl` below the data folder, one JSON line per
event, each line naming the checksum of the line before it (`prev`). A
line changed or taken out afterwards breaks every line after it, so the
chain says what lay in the archive when, and that nothing was swapped
since. What it cannot say is what Microsoft held before the first run
that kept a chain: that run reads the whole archive once and writes it
down as found (`initial`).

The lines, by `kind`:

    genesis   the chain begins
    initial   a file as the first chained run found it
    write     a file an export wrote, as its journal announced it
              (versions.py) – `was` names the version it replaced
    outside   a file that appeared or changed without an export saying so
    removed   an export took a file away (a rename: `to` names the new one)
    moved     an export moved a file unchanged (`from` names the old place)
    missing   a file vanished without an export saying so
    version   a kept version below versions/, as it came to lie there
    case      a case's manifest, written when the case was closed
    stamp     a time-stamp authority (RFC 3161) signed the chain's head –
              or a case's manifest – at that moment

The step (`python evidence.py <folders> --data DIR`) runs after the
exports of every run: it reads the journal, walks the export folders and
the versions folder, hashes what changed since it last looked (by size
and modification time) and writes the lines. With a time-stamp service
configured (EVIDENCE_TSA_URL) it then has the new head stamped; only the
32 bytes of a checksum leave the machine, the answer lands in
`evidence/stamps/`.

`evidence.db` beside the chain is its index – every file's last checksum
and every line per file, for the versions of an item and the check – and
is rebuilt from the chain when it is missing. `verify()` is the archive
check's part (archive_check.py): the chain read end to end, and every
file hashed afresh against its last line.
"""

import argparse
import csv
import io
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, UTC
from pathlib import Path

import versions

EVIDENCE_DIRNAME = versions.EVIDENCE_DIRNAME
CHAIN = "chain.jsonl"
DB = "evidence.db"
STAMPS = "stamps"
CASES = "cases"
LIMIT = 5000                    # findings named per kind

# What a walk never counts as part of the archive: the exports' own
# bookkeeping, sidecars of a write under way, the app's write probe.
_NOT_ARCHIVE_PREFIX = ("state.db", ".")
_NOT_ARCHIVE_SUFFIX = (".tmp", ".teil")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    rel      TEXT PRIMARY KEY,
    sha256   TEXT NOT NULL,
    size     INTEGER,
    mtime_ns INTEGER,
    n        INTEGER,
    at       TEXT
);
CREATE TABLE IF NOT EXISTS history(
    n      INTEGER PRIMARY KEY,
    rel    TEXT,
    sha256 TEXT,
    size   INTEGER,
    at     TEXT,
    kind   TEXT,
    other  TEXT,
    modified TEXT,
    quickxor TEXT,
    ms_quickxor TEXT,
    ms_match INTEGER
);
CREATE INDEX IF NOT EXISTS ix_history_rel ON history(rel);
CREATE INDEX IF NOT EXISTS ix_history_other ON history(other);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def now():
    return versions.now()


def line_hash(line):
    return versions.sha256_bytes(line.encode("utf-8"))


def _mtime_iso(ns):
    return datetime.fromtimestamp(ns / 1e9, UTC).isoformat(timespec="seconds")


def is_archive_file(name):
    return not (name.startswith(_NOT_ARCHIVE_PREFIX) or name.endswith(_NOT_ARCHIVE_SUFFIX))


# ---------------------------------------------------------------------------
# The chain and its index
# ---------------------------------------------------------------------------
class Evidence:
    """The evidence folder of one data folder."""

    def __init__(self, data):
        self.data = Path(data)
        self.dir = self.data / EVIDENCE_DIRNAME
        self.chain = self.dir / CHAIN
        self.db_path = self.dir / DB
        self.versions = self.data / versions.VERSIONS_DIRNAME
        self._con = None
        self._head = None
        self._count = None

    # -- the index ----------------------------------------------------------
    def exists(self):
        return self.chain.is_file()

    def db(self):
        if self._con is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            # The index is the chain's cache: one from an older schema is
            # dropped and read afresh from the chain.
            have = {r[1] for r in con.execute("PRAGMA table_info(history)")}
            if have and "ms_match" not in have:
                con.executescript("DROP TABLE IF EXISTS history; DROP TABLE IF EXISTS files; "
                                  "DROP TABLE IF EXISTS meta;")
            con.executescript(_SCHEMA)
            self._con = con
            if self._meta("lines") != str(self.lines_on_disk()):
                self.rebuild()
        return self._con

    def close(self):
        if self._con is not None:
            self._con.commit()
            self._con.close()
            self._con = None

    def _meta(self, key, value=None):
        con = self._con
        if value is None:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))

    def lines_on_disk(self):
        if not self.chain.is_file():
            return 0
        with open(self.chain, "rb") as f:
            return sum(1 for z in f if z.strip())

    def read_lines(self):
        """(text, parsed) of every line of the chain, in order."""
        if not self.chain.is_file():
            return
        with open(self.chain, encoding="utf-8") as f:
            for raw in f:
                text = raw.rstrip("\n")
                if not text.strip():
                    continue
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
                yield text, parsed

    def rebuild(self):
        """The index from the chain – after it went missing, or when the two
        disagree on how many lines there are."""
        con = self._con
        con.execute("DELETE FROM files")
        con.execute("DELETE FROM history")
        head, n = None, 0
        for text, d in self.read_lines():
            n += 1
            head = line_hash(text)
            if isinstance(d, dict):
                self._index(d)
        self._meta("lines", n)
        self._meta("head", head or "")
        con.commit()

    def _index(self, d):
        """What one line means for the index."""
        con, kind, rel = self._con, d.get("kind"), d.get("rel")
        other = d.get("to") or d.get("from") or d.get("for")
        match = d.get("ms_match")
        con.execute("INSERT OR REPLACE INTO history(n, rel, sha256, size, at, kind, other, modified, "
                    "quickxor, ms_quickxor, ms_match) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (d.get("n"), rel, d.get("sha256"), d.get("size"), d.get("at"), kind, other,
                     d.get("modified"), d.get("quickxor"), d.get("ms_quickxor"),
                     None if match is None else int(bool(match))))
        if not rel or kind in ("case", "stamp", "genesis"):
            return
        if kind in ("removed", "missing"):
            con.execute("DELETE FROM files WHERE rel = ?", (rel,))
        elif d.get("sha256"):
            con.execute("INSERT OR REPLACE INTO files(rel, sha256, size, mtime_ns, n, at) "
                        "VALUES(?,?,?,?,?,?)",
                        (rel, d["sha256"], d.get("size"), d.get("mtime_ns"), d.get("n"), d.get("at")))

    def head(self):
        """(number of lines, checksum of the last one)."""
        if self._head is None:
            self.db()
            self._count = int(self._meta("lines") or 0)
            self._head = self._meta("head") or ""
        return self._count, self._head

    def append(self, entry):
        """One line onto the chain, and into the index."""
        n, head = self.head()
        d = {"n": n + 1, "at": entry.pop("at", None) or now(), "prev": head or None, **entry}
        d = {k: v for k, v in d.items() if v is not None}
        text = json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.chain, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        self._count, self._head = n + 1, line_hash(text)
        self._index(d)
        self._meta("lines", self._count)
        self._meta("head", self._head)
        return d

    def flush(self):
        if self._con is not None:
            self._con.commit()
        if self.chain.is_file():
            with open(self.chain, "a", encoding="utf-8") as f:
                f.flush()
                os.fsync(f.fileno())

    # -- reading ------------------------------------------------------------
    def recorded(self, rel):
        row = self.db().execute("SELECT * FROM files WHERE rel = ?", (rel,)).fetchone()
        return dict(row) if row else None

    def current_sha(self, rel):
        """The checksum of the file as it lies now – from the index while
        size and time agree, else read afresh. None when it is not there."""
        path = self.data / rel
        try:
            st = path.stat()
        except OSError:
            return None
        rec = self.recorded(rel)
        if rec and rec["size"] == st.st_size and rec["mtime_ns"] == st.st_mtime_ns:
            return rec["sha256"]
        try:
            return versions.sha256_file(path)
        except OSError:
            return None

    def names_of(self, rel):
        """`rel` and every earlier name of the same file – a rename or a
        move names its successor, the history follows it back."""
        con = self.db()
        names, todo = [rel], [rel]
        while todo:
            r = todo.pop()
            for row in con.execute("SELECT rel FROM history WHERE other = ? AND kind IN ('removed', 'moved')",
                                   (r,)):
                if row[0] and row[0] not in names:
                    names.append(row[0])
                    todo.append(row[0])
            for row in con.execute("SELECT other FROM history WHERE rel = ? AND kind = 'moved'", (r,)):
                if row[0] and row[0] not in names:
                    names.append(row[0])
                    todo.append(row[0])
        return names

    def history(self, rel):
        """Every version of the file at `rel` the chain knows, newest
        first: sha256, size, captured (when the chain first saw it under
        one of its names), kind, and whether its bytes are still here
        (the file itself, or a kept version)."""
        names = self.names_of(rel)
        con = self.db()
        q = ",".join("?" * len(names))
        rows = con.execute(f"SELECT * FROM history WHERE rel IN ({q}) AND sha256 IS NOT NULL "
                           f"AND kind NOT IN ('removed', 'missing', 'version') ORDER BY n",
                           names).fetchall()
        current = self.current_sha(rel)
        seen, out = {}, []
        for r in rows:
            if r["sha256"] in seen:
                continue
            seen[r["sha256"]] = True
            out.append({"sha256": r["sha256"], "size": r["size"], "captured": r["at"],
                        "modified": r["modified"], "kind": r["kind"], "rel": r["rel"], "n": r["n"],
                        "quickxor": r["quickxor"], "ms_quickxor": r["ms_quickxor"],
                        "ms_match": None if r["ms_match"] is None else bool(r["ms_match"])})
        if current and current not in seen:
            # Changed since the step last looked: the bytes are here, the
            # chain will have them after the next run.
            path = self.data / rel
            st = path.stat()
            out.append({"sha256": current, "size": st.st_size, "captured": None,
                        "modified": _mtime_iso(st.st_mtime_ns), "kind": "unrecorded",
                        "rel": rel, "n": None})
        # A kept version never recorded under its name (its first sight was
        # already the kept copy) still counts.
        for name in names:
            for stem, p in versions.stored(self.versions, name).items():
                if not any(v["sha256"].startswith(stem) for v in out):
                    vrel = versions.rel_of(p, self.data)
                    row = con.execute("SELECT sha256, at FROM history WHERE rel = ? ORDER BY n DESC LIMIT 1",
                                      (vrel,)).fetchone()
                    sha = row[0] if row else versions.sha256_file(p)
                    out.insert(0, {"sha256": sha, "size": p.stat().st_size,
                                   "captured": row[1] if row else None,
                                   "modified": _mtime_iso(p.stat().st_mtime_ns),
                                   "kind": "version", "rel": name, "n": None})
        out.reverse()
        for v in out:
            v["current"] = v["sha256"] == current
            v["available"] = v["current"] or versions.find(self.versions, names, v["sha256"]) is not None
        return out

    def bytes_of(self, rel, sha):
        """The bytes of one version of `rel`: the file itself when it is the
        current one, else the kept copy – None when neither is here."""
        path = self.data / rel
        if self.current_sha(rel) == sha:
            return path.read_bytes()
        kept = versions.find(self.versions, self.names_of(rel), sha)
        return kept.read_bytes() if kept is not None else None

    def last_stamp(self):
        row = self.db().execute("SELECT at, other FROM history WHERE kind = 'stamp' "
                                "AND (other IS NULL OR other NOT LIKE 'cases/%') "
                                "ORDER BY n DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def summary(self):
        """What the settings card says about the chain: how long, its head,
        since when it runs, when it was last stamped."""
        if not self.exists():
            return {"lines": 0, "head": None, "since": None, "stamped": None,
                    "versions": 0, "versions_bytes": 0}
        n, head = self.head()
        row = self.db().execute("SELECT at FROM history ORDER BY n LIMIT 1").fetchone()
        count, size = kept_versions(self.versions)
        return {"lines": n, "head": head, "since": row[0] if row else None,
                "stamped": self.last_stamp(), "versions": count, "versions_bytes": size}


def kept_versions(vroot):
    count = size = 0
    if vroot and Path(vroot).is_dir():
        for p in Path(vroot).rglob("*"):
            if p.is_file():
                count += 1
                size += p.stat().st_size
    return count, size


# ---------------------------------------------------------------------------
# The journal the exports left
# ---------------------------------------------------------------------------
def read_pending(ev):
    """(rel -> last write entry, rel -> removal/move entry, case entries,
    the files read) from evidence/pending/."""
    writes, gone, cases, files = {}, {}, [], []
    folder = ev.dir / versions.PENDING_DIRNAME
    if not folder.is_dir():
        return writes, gone, cases, files
    for p in sorted(folder.glob("*.jsonl")):
        files.append(p)
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            op = d.get("op")
            if op == "write" and d.get("rel"):
                writes[d["rel"]] = d
                gone.pop(d["rel"], None)
            elif op in ("removed", "moved") and d.get("rel"):
                gone[d["rel"]] = d
            elif op == "case":
                cases.append(d)
    return writes, gone, cases, files


# ---------------------------------------------------------------------------
# The step: walk, hash, chain
# ---------------------------------------------------------------------------
def walk(data, roots):
    """rel -> stat of every archive file below the given folders."""
    out = {}
    data = Path(data)
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for folder, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if not is_archive_file(name):
                    continue
                p = Path(folder) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = versions.rel_of(p, data)
                if rel is not None:
                    out[rel] = st
    return out


def sweep(data, roots, tsa=None, events=None):
    """One pass of the step – returns the counts. `roots` are the export
    folders; the versions folder is walked with them. `events` receives
    (key, fields) for the log."""
    say = events or (lambda key, **fields: None)
    data = Path(data)
    ev = Evidence(data)
    first = not ev.exists()
    ev.db()
    writes, gone, cases, pending_files = read_pending(ev)
    counts = {"initial": 0, "write": 0, "outside": 0, "missing": 0, "removed": 0,
              "moved": 0, "version": 0, "case": 0}
    if first:
        ev.append({"kind": "genesis", "note": "munimentum evidence chain"})
    vroot = ev.versions
    on_disk = walk(data, [*roots, vroot])
    moved_to = {d["to"]: rel for rel, d in gone.items() if d.get("op") == "moved" and d.get("to")}
    known = {r["rel"]: dict(r) for r in ev.db().execute("SELECT * FROM files")}
    vprefix = versions.VERSIONS_DIRNAME + "/"
    for rel in sorted(on_disk):
        st = on_disk[rel]
        rec = known.get(rel)
        if rec and rec["size"] == st.st_size and rec["mtime_ns"] == st.st_mtime_ns:
            continue
        try:
            sha = versions.sha256_file(data / rel)
        except OSError:
            continue
        if rec and rec["sha256"] == sha:
            ev.db().execute("UPDATE files SET size = ?, mtime_ns = ? WHERE rel = ?",
                            (st.st_size, st.st_mtime_ns, rel))
            continue
        entry = {"rel": rel, "sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                 "modified": _mtime_iso(st.st_mtime_ns)}
        if rel.startswith(vprefix):
            kind = "version"
        elif first:
            kind = "initial"
        elif rel in moved_to and not rec:
            kind = "moved"
            entry["from"] = moved_to[rel]
        elif writes.get(rel, {}).get("sha256") == sha:
            kind = "write"
            # What the export knew beside the bytes: the quickXorHash it
            # computed, the one Microsoft gave, whether they agree.
            for field in ("was", "quickxor", "ms_quickxor", "ms_match"):
                if writes[rel].get(field) is not None:
                    entry[field] = writes[rel][field]
        else:
            kind = "outside"
        entry["kind"] = kind
        ev.append(entry)
        counts[kind] += 1
    for rel in sorted(set(known) - set(on_disk)):
        g = gone.get(rel)
        if g and g.get("op") == "moved":
            ev.append({"kind": "removed", "rel": rel, "sha256": known[rel]["sha256"],
                       "to": g.get("to")})
            counts["removed"] += 1
        elif g:
            ev.append({"kind": "removed", "rel": rel, "sha256": known[rel]["sha256"],
                       "to": g.get("to"), "kept": g.get("kept")})
            counts["removed"] += 1
        else:
            ev.append({"kind": "missing", "rel": rel, "sha256": known[rel]["sha256"]})
            counts["missing"] += 1
    stamped_cases = []
    for c in cases:
        line = ev.append({"kind": "case", "case": c.get("case"), "for": c.get("file"),
                          "sha256": c.get("sha256")})
        stamped_cases.append((line, c))
        counts["case"] += 1
    ev.flush()
    changed = any(counts.values()) or first
    stamp = None
    if tsa:
        n, head = ev.head()
        if changed or not ev.last_stamp():
            stamp = stamp_head(ev, tsa, say)
        for _line, c in stamped_cases:
            stamp_case(ev, tsa, c, say)
    ev.flush()
    ev.close()
    # The journal is spent only once its content is on the chain.
    for p in pending_files:
        try:
            p.unlink()
        except OSError:
            pass
    counts["first"] = first
    counts["stamp"] = stamp
    return counts


# ---------------------------------------------------------------------------
# Checking: the chain end to end, every file hashed afresh
# ---------------------------------------------------------------------------
def verify(data, roots=None, events=None):
    """The archive check's part. Reads the chain line by line (every
    `prev` must be the checksum of the line before it), then hashes every
    file the index records and holds it against its last line."""
    data = Path(data)
    ev = Evidence(data)
    if not ev.exists():
        return {"exists": False}
    chain_ok, broken_at, n, prev = True, None, 0, None
    first_at = None
    for text, d in ev.read_lines():
        n += 1
        if not isinstance(d, dict) or d.get("n") != n or d.get("prev") != prev:
            if chain_ok:
                chain_ok, broken_at = False, n
        if first_at is None and isinstance(d, dict):
            first_at = d.get("at")
        prev = line_hash(text)
    ev.db()
    # The last line has no successor to name it: its checksum is held
    # against the head the index recorded when the line was written.
    if chain_ok and prev and ev._meta("head") and ev._meta("head") != prev:
        chain_ok, broken_at = False, n
    unchanged, changed, missing = 0, [], []
    for row in ev.db().execute("SELECT * FROM files ORDER BY rel"):
        path = ev.data / row["rel"]
        if not path.is_file():
            missing.append({"rel": row["rel"], "sha256": row["sha256"], "captured": row["at"]})
            continue
        try:
            sha = versions.sha256_file(path)
        except OSError:
            sha = None
        if sha == row["sha256"]:
            unchanged += 1
            continue
        st = path.stat()
        changed.append({"rel": row["rel"], "sha256": row["sha256"], "now": sha,
                        "captured": row["at"], "modified": _mtime_iso(st.st_mtime_ns)})
    outside = [dict(r) for r in ev.db().execute(
        "SELECT n, rel, sha256, at FROM history WHERE kind IN ('outside', 'missing') "
        "ORDER BY n DESC LIMIT ?", (LIMIT,))]
    # Files whose bytes, as they lie today, differ from the quickXorHash
    # Microsoft gave for them when they were fetched – and how many agree.
    against = ("SELECT h.rel, h.sha256, h.quickxor, h.ms_quickxor, h.at FROM history h "
               "JOIN files f ON f.rel = h.rel AND f.sha256 = h.sha256 WHERE h.ms_match = ?")
    ms_mismatch = [dict(r) for r in ev.db().execute(against + " ORDER BY h.rel", (0,))]
    ms_confirmed = len(ev.db().execute(against, (1,)).fetchall())
    count, size = kept_versions(ev.versions)
    out = {"exists": True, "lines": n, "since": first_at, "chain_ok": chain_ok,
           "ms_mismatch": ms_mismatch[:LIMIT], "ms_mismatch_n": len(ms_mismatch),
           "ms_confirmed": ms_confirmed,
           "broken_at": broken_at, "unchanged": unchanged,
           "changed": changed[:LIMIT], "changed_n": len(changed),
           "missing": missing[:LIMIT], "missing_n": len(missing),
           "outside": outside, "versions": count, "versions_bytes": size,
           "stamped": ev.last_stamp()}
    ev.close()
    return out


# ---------------------------------------------------------------------------
# A Teams message: its versions live in the export's message store
# ---------------------------------------------------------------------------
EARLIER = "earlierVersions"           # teams_export.EARLIER – the store's field


def message_key(key):
    """teams:<conversation>#<message id> -> (conversation, message id)."""
    rest = str(key or "")
    if not rest.startswith("teams:") or "#" not in rest:
        return None, None
    conversation, _, mid = rest[len("teams:"):].rpartition("#")
    return (conversation or None), (mid or None)


def _said_sha(body, subject):
    return versions.sha256_bytes(json.dumps([(body or {}).get("content") or "", subject or ""],
                                            ensure_ascii=False).encode("utf-8"))


def message_rows(teams_dir, conversation):
    """message id -> stored JSON of one conversation."""
    import state_db
    if not conversation or not teams_dir or not Path(teams_dir).is_dir():
        return {}
    return state_db.StateDb(teams_dir).saetze_lesen(f"msgs:{conversation}")


def message_versions(teams_dir, key, rows=None):
    """Every version of one Teams message the store holds, newest first:
    what it says now, then what it said before each edit. Each carries its
    text as the store keeps it (`body`, `subject`), the time it was made
    (`modified`) and a checksum of the words, so a case can tell whether
    the message changed since it was taken in. None when the store does
    not know the message."""
    conversation, mid = message_key(key)
    if rows is None:
        rows = message_rows(teams_dir, conversation)
    raw = rows.get(mid) if mid else None
    try:
        m = json.loads(raw) if raw else None
    except ValueError:
        m = None
    if not isinstance(m, dict):
        return None
    out = []
    for e in m.get(EARLIER) or []:
        out.append({"sha256": _said_sha(e.get("body"), e.get("subject")),
                    "modified": e.get("at"), "body": e.get("body"), "subject": e.get("subject"),
                    "captured": None, "current": False, "available": True, "kind": "earlier"})
    out.append({"sha256": _said_sha(m.get("body"), m.get("subject")),
                "modified": m.get("lastModifiedDateTime") or m.get("createdDateTime"),
                "body": m.get("body"), "subject": m.get("subject"), "captured": None,
                "current": True, "available": True,
                "kind": "deleted" if m.get("deletedDateTime") else "current"})
    out.reverse()
    return out


def message_text(version):
    body = version.get("body") or {}
    text = body.get("content") or ""
    if (body.get("contentType") or "text") == "html":
        text = versions.plain_text(text, "x.html")
    subject = version.get("subject")
    return f"{subject}\n\n{text}" if subject else text


# ---------------------------------------------------------------------------
# What a case pins: the checksum of the item's own version
# ---------------------------------------------------------------------------
def _task_section(text, key):
    """A Planner card or a To Do task out of its board or list – the part
    of the file that is the item, so another card's change is not its."""
    import re
    kind, _, tid = str(key).partition(":")
    anchor = {"planner": "k", "todo": "t"}.get(kind)
    if not anchor or not tid:
        return None
    m = re.search(r'<details class="karte[^"]*" id="' + anchor + "-" + re.escape(tid)
                  + r'">(.*?)(?=<details class="karte|</main>|\Z)', text, re.S)
    return m.group(1) if m else None


class Fingerprints:
    """The checksum of the version an item has now – a Teams message's
    words, a Planner card's or To Do task's part of its file, any other
    item's file. A case keeps it when the item is taken in and holds it
    against today's to say "changed since"."""

    def __init__(self, data, dirs):
        self.ev = Evidence(data) if data else None
        self.dirs = {k: v for k, v in (dirs or {}).items() if v}
        self._texts = {}
        self._rows = {}

    def close(self):
        if self.ev is not None:
            self.ev.close()

    def of(self, item):
        root, rel, key = item.get("root"), str(item.get("rel") or ""), str(item.get("key") or "")
        base = self.dirs.get(root)
        if not base or not rel:
            return None
        path = Path(base) / rel
        if root == "teams" and "#" in key:
            conversation = message_key(key)[0]
            if conversation not in self._rows:
                self._rows[conversation] = message_rows(self.dirs.get("teams"), conversation)
            found = message_versions(self.dirs.get("teams"), key, self._rows[conversation])
            return found[0]["sha256"] if found else None
        if key.startswith(("planner:", "todo:")):
            if path not in self._texts:
                try:
                    self._texts[path] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    self._texts[path] = None
            section = _task_section(self._texts[path] or "", key)
            return versions.sha256_bytes(section.encode("utf-8")) if section is not None else None
        if self.ev is not None and self.ev.exists():
            rel_data = versions.rel_of(path, self.ev.data)
            if rel_data is not None:
                return self.ev.current_sha(rel_data)
        try:
            return versions.sha256_file(path)
        except OSError:
            return None


def export_dirs(data):
    """The export folders below a data folder, by the index's source names
    (settings.QUELLEN) – the `root` a case's item carries."""
    import settings
    return {index: Path(data) / folder for folder, index in settings.QUELLEN.values()}


def pinner(data):
    """What a case book calls when items come in: key -> the checksum of
    the version each has now (faelle.Fallbuch.pinner)."""
    def pin(items):
        prints = Fingerprints(data, export_dirs(data))
        try:
            return {str(e.get("key")): prints.of(e) for e in items if e.get("key")}
        finally:
            prints.close()
    return pin


# ---------------------------------------------------------------------------
# RFC 3161: a time-stamp authority signs a checksum
# ---------------------------------------------------------------------------
_SHA256_OID = bytes.fromhex("0609608648016503040201")


def _der(tag, body):
    n = len(body)
    if n < 0x80:
        size = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        size = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + size + body


def _der_int(value):
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der(0x02, raw)


def timestamp_request(digest, nonce=None):
    """A DER TimeStampReq for a SHA-256 digest, certificate requested."""
    algo = _der(0x30, _SHA256_OID + b"\x05\x00")
    imprint = _der(0x30, algo + _der(0x04, digest))
    body = _der_int(1) + imprint
    if nonce is not None:
        body += _der_int(nonce)
    body += b"\x01\x01\xff"
    return _der(0x30, body)


def _der_read(buf, pos):
    tag = buf[pos]
    size = buf[pos + 1]
    pos += 2
    if size & 0x80:
        k = size & 0x7F
        size = int.from_bytes(buf[pos:pos + k], "big")
        pos += k
    return tag, buf[pos:pos + size], pos + size


def response_status(resp):
    """The PKIStatus of a TimeStampResp: 0 granted, 1 granted with
    modifications, anything else a refusal – None when unreadable."""
    try:
        tag, body, _ = _der_read(resp, 0)
        if tag != 0x30:
            return None
        tag, info, _ = _der_read(body, 0)
        if tag != 0x30:
            return None
        tag, status, _ = _der_read(info, 0)
        return int.from_bytes(status, "big") if tag == 0x02 else None
    except (IndexError, ValueError):
        return None


def request_stamp(url, data, timeout=20):
    """Ask the authority at `url` to stamp sha256(data): the response's
    bytes, or an exception naming what went wrong."""
    import requests
    digest = versions.sha256_bytes(data)
    nonce = secrets.randbits(63)
    r = requests.post(url, data=timestamp_request(bytes.fromhex(digest), nonce),
                      headers={"Content-Type": "application/timestamp-query",
                               "Accept": "application/timestamp-reply"},
                      timeout=timeout)
    r.raise_for_status()
    status = response_status(r.content)
    if status not in (0, 1):
        raise ValueError(f"time-stamp refused (status {status})")
    if bytes.fromhex(digest) not in r.content:
        raise ValueError("time-stamp answer names another checksum")
    return r.content


def head_text(n, head, at):
    return f"munimentum evidence chain\nlines: {n}\nhead: {head}\nat: {at}\n"


def stamp_head(ev, url, say=None):
    """Have the chain's head stamped: `stamps/<n>.head` (the text that is
    stamped) and `stamps/<n>.tsr` (the authority's answer), then a stamp
    line. A refusal is said in the log and leaves the chain as it is."""
    say = say or (lambda key, **fields: None)
    n, head = ev.head()
    at = now()
    text = head_text(n, head, at).encode("utf-8")
    try:
        resp = request_stamp(url, text)
    except Exception as e:   # noqa: BLE001 – any failure is one log line
        say("run.evidence.stamp_failed", error=f"{type(e).__name__}: {e}")
        return None
    folder = ev.dir / STAMPS
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{n}.head").write_bytes(text)
    (folder / f"{n}.tsr").write_bytes(resp)
    ev.append({"kind": "stamp", "head": head, "lines": n, "file": f"{STAMPS}/{n}.tsr",
               "sha256": versions.sha256_bytes(text), "tsa": url})
    say("run.evidence.stamped", n=n)
    return f"{STAMPS}/{n}.tsr"


def stamp_case(ev, url, case, say=None):
    """Have a closed case's manifest stamped: `cases/<name>.tsr` beside it."""
    say = say or (lambda key, **fields: None)
    rel = case.get("file") or ""
    path = ev.dir / rel
    if not rel or not path.is_file():
        return None
    try:
        resp = request_stamp(url, path.read_bytes())
    except Exception as e:   # noqa: BLE001
        say("run.evidence.stamp_failed", error=f"{type(e).__name__}: {e}")
        return None
    tsr = path.with_suffix(".tsr")
    tsr.write_bytes(resp)
    ev.append({"kind": "stamp", "for": rel, "file": versions.rel_of(tsr, ev.dir),
               "sha256": case.get("sha256"), "tsa": url})
    return tsr


# ---------------------------------------------------------------------------
# A case's manifest
# ---------------------------------------------------------------------------
MANIFEST_FIELDS = ("file", "source", "key", "sha256", "item_sha256", "quickxor",
                   "microsoft_quickxor", "size", "captured", "modified")


def manifest_rows(ev, entries):
    """One row per file a case points at: its place in the archive, the
    checksum it has now and when the chain first saw these bytes."""
    rows = []
    for e in entries:
        rel = e.get("archive_rel")
        if not rel:
            continue
        path = ev.data / rel
        sha = ev.current_sha(rel)
        if sha is None:
            continue
        captured, known = None, {}
        for v in ev.history(rel):
            if v["sha256"] == sha:
                captured, known = v["captured"], v
        st = path.stat()
        rows.append({"file": e.get("file") or rel, "source": e.get("src") or "",
                     "key": e.get("key") or "", "sha256": sha, "item_sha256": e.get("item") or "",
                     "quickxor": known.get("quickxor") or "",
                     "microsoft_quickxor": known.get("ms_quickxor") or "",
                     "size": st.st_size,
                     "captured": captured or "", "modified": _mtime_iso(st.st_mtime_ns)})
    return rows


def manifest_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def record_case(data, case_id, rows):
    """A case was closed: its manifest goes next to the chain and into the
    journal, so the next run chains – and, with a service, stamps – it.
    Returns the manifest's name below evidence/."""
    ev = Evidence(data)
    text = manifest_csv(rows).encode("utf-8")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    rel = f"{CASES}/case-{case_id}-{stamp}.csv"
    path = ev.dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text)
    sha = versions.sha256_bytes(text)
    line = json.dumps({"at": now(), "op": "case", "case": case_id, "file": rel, "sha256": sha},
                      ensure_ascii=False, sort_keys=True)
    pending = ev.dir / versions.PENDING_DIRNAME
    pending.mkdir(parents=True, exist_ok=True)
    (pending / f"case-{case_id}-{stamp}.jsonl").write_text(line + "\n", encoding="utf-8")
    return rel


def case_records(data, case_id):
    """The manifests of one case, newest first: name, checksum, when, the
    stamp beside it (or None) and whether the chain holds it yet."""
    ev = Evidence(data)
    folder = ev.dir / CASES
    out = []
    if not folder.is_dir():
        return out
    chained = {}
    if ev.exists():
        for row in ev.db().execute("SELECT other, at FROM history WHERE kind = 'case'"):
            chained[row[0]] = row[1]
    for p in sorted(folder.glob(f"case-{case_id}-*.csv"), reverse=True):
        rel = versions.rel_of(p, ev.dir)
        tsr = p.with_suffix(".tsr")
        out.append({"file": rel, "sha256": versions.sha256_file(p),
                    "at": datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat(timespec="seconds"),
                    "chained": chained.get(rel), "stamp": tsr.name if tsr.is_file() else None})
    ev.close()
    return out


# ---------------------------------------------------------------------------
# The run step
# ---------------------------------------------------------------------------
def main(argv=None):
    import export_util
    import progress
    export_util.erzwinge_utf8()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="*", help="the export folders")
    ap.add_argument("--data", required=True, help="the data folder")
    a = ap.parse_args(argv)
    tsa = (os.environ.get("EVIDENCE_TSA_URL") or "").strip() or None

    def say(key, **fields):
        progress.event(key, "warn" if key.endswith("failed") else "info", **fields)

    ev = Evidence(a.data)
    if not ev.exists():
        progress.event("run.evidence.first", "info")
    c = sweep(a.data, a.folders, tsa=tsa, events=say)
    if c["first"]:
        progress.event("run.evidence.initial", "info", n=c["initial"])
    progress.event("run.evidence.result", "info", written=c["write"], moved=c["moved"],
                   removed=c["removed"], versions=c["version"])
    if c["outside"] or c["missing"]:
        progress.event("run.evidence.outside", "warn", changed=c["outside"], missing=c["missing"])
    progress.ergebnis(0, extra={"initial": c["initial"], "written": c["write"],
                                "outside": c["outside"], "missing": c["missing"],
                                "versions": c["version"]})


if __name__ == "__main__":
    sys.exit(main())
