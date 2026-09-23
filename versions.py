#!/usr/bin/env python3
"""
versions.py – every write into the archive, with the version it replaces.

An export used to overwrite a file when Microsoft handed out a newer one:
the archive then knew what a page says today, not what it said when it
mattered. Everything an export writes into its folder now goes through
here, and three things happen on the way:

  * the file is written atomically (a sidecar, renamed at the end), and
    not at all when the bytes are the ones already there;
  * the version it replaces moves to `versions/<path>/<sha256[:16]><ext>`
    below the data folder, beside the export folders – unless keeping
    versions is off or the old file is larger than the cap, then it goes
    and only its checksum stays on record (the evidence chain has it);
  * what was written, removed or moved is journaled for the evidence step
    (evidence.py): `evidence/pending/<process>.jsonl`. The step, after the
    exports, holds the disk against that journal – a change an export
    announced is a write, anything else happened outside the app.

Where things are comes from the environment the app hands every step:

    MUNIMENTUM_VERSIONS_DIR   the versions folder (unset: nothing is kept)
    MUNIMENTUM_EVIDENCE_DIR   the evidence folder (unset: nothing journaled)
    KEEP_VERSIONS             "0" keeps no versions, the checksum still counts
    VERSIONS_MAX_MB           the largest file whose old version is kept

A path is named relative to the data folder (the versions folder's
parent): `onedrive_export/Dateien/Vertrag.docx`. Outside it nothing is
kept or journaled – a test writing into a temporary folder stays a plain
write.

The reading side lives here too: which versions of a file there are
(`history`), where one of them lies (`find`), and a word diff of two
texts (`diff`).
"""

import difflib
import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime, UTC
from pathlib import Path

VERSIONS_DIRNAME = "versions"
EVIDENCE_DIRNAME = "evidence"
PENDING_DIRNAME = "pending"
DEFAULT_MAX_MB = 50

_LOCK = threading.Lock()
_JOURNAL = {"path": None}


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------
def versions_dir():
    raw = os.environ.get("MUNIMENTUM_VERSIONS_DIR")
    return Path(raw) if raw else None


def evidence_dir():
    raw = os.environ.get("MUNIMENTUM_EVIDENCE_DIR")
    return Path(raw) if raw else None


def keeping():
    return os.environ.get("KEEP_VERSIONS", "1").strip().lower() not in ("0", "false", "no", "off", "")


def max_bytes():
    try:
        mb = float(os.environ.get("VERSIONS_MAX_MB", DEFAULT_MAX_MB))
    except ValueError:
        mb = DEFAULT_MAX_MB
    return None if mb <= 0 else int(mb * 1024 * 1024)


def data_root():
    """The data folder every archive path is named against: the parent of
    the versions folder, else of the evidence folder."""
    for d in (versions_dir(), evidence_dir()):
        if d is not None:
            return d.parent
    return None


def rel_of(path, root=None):
    """`path` relative to the data folder, as posix – None outside it."""
    root = root if root is not None else data_root()
    if root is None:
        return None
    try:
        return Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root))).as_posix()
    except ValueError:
        return None


def version_path(vroot, rel, sha, name=None):
    """Where the version with checksum `sha` of the file `rel` lies."""
    ext = Path(name or rel).suffix.lower()
    return Path(vroot) / rel / f"{sha[:16]}{ext}"


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------
def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class QuickXor:
    """Microsoft's quickXorHash – the checksum OneDrive for Business and
    SharePoint hand out with every file (`file.hashes.quickXorHash`),
    so a download can be held against what Microsoft says it holds.

    The algorithm as Microsoft documents it: every byte is XORed into a
    160-bit vector at a position that moves 11 bits per byte, the length
    is XORed into the last 64 bits, the result is Base64. A byte at offset
    k lands at bit (11·k) mod 160, so the bytes 160 apart share a position:
    each block XORs its rows of 160 bytes together first (C speed, via
    int.from_bytes) and places the 160 results one by one."""

    WIDTH = 160
    SHIFT = 11

    def __init__(self):
        self.cells = [0, 0, 0]          # three 64-bit cells, the last uses 32 bits
        self.shift_so_far = 0
        self.length = 0

    def update(self, data):
        data = memoryview(data)
        size = len(data)
        if not size:
            return self
        folded = 0
        full = size - size % self.WIDTH
        for k in range(0, full, self.WIDTH):
            folded ^= int.from_bytes(data[k:k + self.WIDTH], "little")
        folded ^= int.from_bytes(data[full:], "little")
        column = folded.to_bytes(self.WIDTH, "little")
        index, offset = self.shift_so_far // 64, self.shift_so_far % 64
        for i in range(min(size, self.WIDTH)):
            last = index == 2
            bits = 32 if last else 64
            byte = column[i]
            if offset <= bits - 8:
                self.cells[index] ^= (byte << offset) & 0xFFFFFFFFFFFFFFFF
            else:
                self.cells[index] ^= (byte << offset) & 0xFFFFFFFFFFFFFFFF
                self.cells[0 if last else index + 1] ^= byte >> (bits - offset)
            offset += self.SHIFT
            while offset >= bits:
                index = 0 if last else index + 1
                offset -= bits
                last = index == 2
                bits = 32 if last else 64
        self.shift_so_far = (self.shift_so_far + self.SHIFT * (size % self.WIDTH)) % self.WIDTH
        self.length += size
        return self

    def digest(self):
        out = bytearray(self.cells[0].to_bytes(8, "little") + self.cells[1].to_bytes(8, "little")
                        + (self.cells[2] & 0xFFFFFFFF).to_bytes(4, "little"))
        for i, b in enumerate(self.length.to_bytes(8, "little")):
            out[12 + i] ^= b
        return bytes(out)

    def b64(self):
        import base64
        return base64.b64encode(self.digest()).decode("ascii")


def quick_xor_file(path):
    q = QuickXor()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            q.update(block)
    return q.b64()


# ---------------------------------------------------------------------------
# The journal the evidence step reads
# ---------------------------------------------------------------------------
def _journal_path():
    ev = evidence_dir()
    if ev is None:
        return None
    if _JOURNAL["path"] is None or _JOURNAL["path"].parent.parent != ev:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        _JOURNAL["path"] = ev / PENDING_DIRNAME / f"{stamp}-{os.getpid()}.jsonl"
    return _JOURNAL["path"]


def journal(entry):
    """One line for the evidence step; silently nothing without a folder."""
    path = _journal_path()
    if path is None:
        return
    line = json.dumps({"at": now(), **entry}, ensure_ascii=False, sort_keys=True)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def _set_aside(path, rel, old_sha, keep):
    """The file at `path` is about to be replaced or removed: move it into
    the versions folder when versions are kept and it is small enough.
    Returns whether it was kept."""
    vroot = versions_dir()
    if not keep or vroot is None or rel is None or not keeping():
        return False
    cap = max_bytes()
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if cap is not None and size > cap:
        return False
    target = version_path(vroot, rel, old_sha, path.name)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return True          # the same bytes are kept already
        shutil.copy2(path, target)
        return True
    except OSError:
        return False


def replace(tmp, path, keep=True, sha=None, extra=None):
    """Put the finished sidecar `tmp` in the place of `path`. The version
    it replaces is set aside first; identical bytes leave `path` alone
    (and drop the sidecar). `extra` travels into the journal – a mirror's
    quickXorHash and Microsoft's for it. Returns the new checksum."""
    tmp, path = Path(tmp), Path(path)
    new_sha = sha or sha256_file(tmp)
    rel = rel_of(path)
    entry = {"op": "write", "rel": rel, "sha256": new_sha, **(extra or {})}
    if path.exists():
        try:
            old_sha = sha256_file(path)
        except OSError:
            old_sha = None
        if old_sha == new_sha:
            try:
                tmp.unlink()
            except OSError:
                pass
            return new_sha
        if old_sha and rel is not None:
            entry["was"] = old_sha
            entry["kept"] = _set_aside(path, rel, old_sha, keep)
    os.replace(tmp, path)
    if rel is not None:
        try:
            entry["size"] = path.stat().st_size
        except OSError:
            pass
        journal(entry)
    return new_sha


def _sidecar(path):
    return path.with_name(path.name + ".tmp")


def write_bytes(path, data, keep=True, extra=None):
    """Write `data` to `path` the one way (see the module docstring). The
    folder must exist – as with a plain write, a missing one is an error."""
    path = Path(path)
    tmp = _sidecar(path)
    tmp.write_bytes(data)
    return replace(tmp, path, keep=keep, sha=sha256_bytes(data), extra=extra)


def write_text(path, text, keep=True):
    return write_bytes(path, text.encode("utf-8"), keep=keep)


def remove(path, successor=None, keep=True):
    """A file the export takes away (a rename, a folder gone): its last
    version is kept like a replaced one, then it goes. `successor` names
    the file that takes its place, so the history follows the item."""
    path = Path(path)
    if not path.is_file():
        return False
    rel = rel_of(path)
    if rel is not None:
        try:
            old_sha = sha256_file(path)
        except OSError:
            old_sha = None
        if old_sha:
            kept = _set_aside(path, rel, old_sha, keep)
            entry = {"op": "removed", "rel": rel, "sha256": old_sha, "kept": kept}
            if successor is not None:
                entry["to"] = rel_of(successor)
            journal(entry)
    try:
        path.unlink()
    except OSError:
        return False
    return True


def moved(old, new):
    """A file (or a folder of files) the export moved without changing it."""
    old, new = Path(old), Path(new)
    if new.is_dir():
        for p in sorted(new.rglob("*")):
            if p.is_file():
                moved(old / p.relative_to(new), p)
        return
    a, b = rel_of(old), rel_of(new)
    if a is not None and b is not None:
        journal({"op": "moved", "rel": a, "to": b})


# ---------------------------------------------------------------------------
# Reading: which versions a file has, and where each lies
# ---------------------------------------------------------------------------
def stored(vroot, rel):
    """sha prefix -> path of every kept version of `rel`."""
    folder = Path(vroot) / rel if vroot else None
    if folder is None or not folder.is_dir():
        return {}
    return {p.stem: p for p in folder.iterdir() if p.is_file()}


def find(vroot, rels, sha):
    """The kept file with checksum `sha` under any of the names `rels`."""
    for rel in rels:
        p = stored(vroot, rel).get(sha[:16])
        if p is not None:
            return p
    return None


# ---------------------------------------------------------------------------
# Texts and their difference
# ---------------------------------------------------------------------------
TEXT_TYPES = {".html", ".htm", ".ics", ".vcf", ".txt", ".md", ".csv", ".json", ".eml", ".xml"}
HTML_TYPES = {".html", ".htm"}
_TOKENS = re.compile(r"\s+|[^\s]+")
MAX_TOKENS = 40000


def is_text(name):
    return Path(str(name)).suffix.lower() in TEXT_TYPES


def plain_text(raw, name):
    """The readable text of a stored file: HTML without its markup, the
    rest as it is. Line breaks stay where blocks end."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    if Path(str(name)).suffix.lower() in HTML_TYPES:
        text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", text)
        text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>|</summary>|</details>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        import html as html_lib
        text = html_lib.unescape(text)
        text = "\n".join(" ".join(z.split()) for z in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text.replace("\r\n", "\n")


def diff(old, new):
    """A word diff: [["=", text], ["-", text], ["+", text]]. Whitespace
    travels with the words; a piece that only swaps whitespace for
    whitespace is no change a reader looks for and reads as the new text –
    so the pieces without the "-" ones always give the new text back."""
    a, b = _TOKENS.findall(old or ""), _TOKENS.findall(new or "")
    if len(a) + len(b) > MAX_TOKENS:
        return [["-", old or ""], ["+", new or ""]]
    ops = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        before, after = "".join(a[i1:i2]), "".join(b[j1:j2])
        if tag == "equal" or (not before.strip() and not after.strip()):
            ops.append(["=", after])
            continue
        if before:
            ops.append(["-", before])
        if after:
            ops.append(["+", after])
    merged = []
    for op in ops:
        if merged and merged[-1][0] == op[0]:
            merged[-1][1] += op[1]
        elif op[1]:
            merged.append(op)
    return merged
