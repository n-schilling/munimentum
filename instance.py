"""
instance.py – where the app of a profile answers, if it runs at all.

The MCP server runs without the app: a client starts it over stdio on its
own, and the app may be closed, on another port (a taken one moves it on
to the next, `--port` names any), or open on another profile. A link into
the app that a model hands the user must therefore not be built on a
guess. The app writes `instance.json` into the profile's folder when it
starts serving and takes it away when it stops; whoever wants to link
into it reads the file and then asks the port itself – the file is only a
hint, since a crash leaves it behind.

No process id is asked after: on Windows `os.kill(pid, 0)` ends the
process instead of looking at it. The probe is the proof.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import settings

FILE = "instance.json"
# The probe asks the constants, not the status: the status counts the
# index and asks Ollama, and during an index run it waited past the probe
# while the run held the index – the link said "not running" to a user
# looking at the app. The constants touch neither.
API_PROBE = "/api/v1/app"
LOOPBACK = "127.0.0.1"
# How long a probe waits for an answer. 0.5 s once missed the running app
# and a second start took the next port. A refused port answers at once.
PROBE = 3.0
# profile_at's word for a port that took the connection but answered too
# late: something is there, busy – not nothing.
SLOW = "slow"
# Loopback never goes through a proxy: urllib would follow http_proxy from
# the environment (a corporate VPN shell) and every probe would fail.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def write(home, port, profile, timeout=PROBE):
    """Say where this process serves the profile – atomically, so a
    reader never sees half a file.

    The file belongs to the first instance that still answers: when it
    names another port and an instance of this profile answers there (a
    second start with --port), nothing is written and None returned – the
    first keeps its file, and since remove() only takes this process's
    own, the second's end leaves it in place – a first that answers late
    (slow) is there, busy, and keeps it too. A file of a crashed instance,
    or one whose port now serves another profile, is taken over."""
    home = Path(home)
    old = read(home)
    if (old is not None and old["port"] != int(port)
            and find(home, timeout)[1] in ("running", SLOW)):
        return None
    data = {"port": int(port), "pid": os.getpid(), "profile": profile,
            "started": datetime.now(UTC).isoformat(timespec="seconds")}
    tmp = home / (FILE + ".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, home / FILE)
    except OSError:
        return None
    return home / FILE


def read(home):
    try:
        data = json.loads((Path(home) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("port"), int) else None


def remove(home):
    """Take the file away – only this process's own: a second instance
    (found the first running, or started beside it with --port) wrote
    nothing, and must not take the first one's file with it."""
    data = read(home)
    if data is not None and data.get("pid") == os.getpid():
        try:
            (Path(home) / FILE).unlink()
        except OSError:
            pass


def profile_at(port, host=LOOPBACK, timeout=PROBE):
    """The profile an instance of this app serves on the port – None when
    nothing of ours answers there, SLOW when the port took the connection
    but no answer came within `timeout` (on loopback a refusal is
    immediate, so a timeout means something is there and busy)."""
    try:
        with _OPENER.open(f"http://{host}:{port}{API_PROBE}", timeout=timeout) as r:
            # The header says whose answer this is – every one of ours
            # carries it, and no other server on a free port will.
            ours = r.headers.get("X-Munimentum-Api")
            data = json.loads(r.read().decode("utf-8"))
    except TimeoutError:
        return SLOW
    except urllib.error.URLError as e:
        return SLOW if isinstance(e.reason, TimeoutError) else None
    except Exception:
        return None
    if not ours or not (isinstance(data, dict) and "version" in data and "api_version" in data):
        return None
    return data.get("profile") or settings.STANDARD_PROFIL


def answers(port, host=LOOPBACK, timeout=PROBE, profile=None):
    """Does an instance of this app answer on the port – of this profile,
    when one is named? A slow one did not answer."""
    running = profile_at(port, host, timeout)
    if running is None or running == SLOW:
        return False
    return profile is None or running == profile


def probe(home, timeout=PROBE):
    """(url, state, record) of the app serving the profile in `home`: the
    url and "running", or None and "not_running" (no file, or nothing of
    ours on its port), "slow" (the port took the connection but answered
    too late) or "other_profile" (the port now serves another one). The
    record is what instance.json said, None without a file – read once,
    so a caller's wording cannot disagree with the probe."""
    data = read(home) if home else None
    if data is None:
        return None, "not_running", None
    running = profile_at(data["port"], LOOPBACK, timeout)
    if running is None:
        return None, "not_running", data
    if running == SLOW:
        return None, SLOW, data
    if data.get("profile") and running != data["profile"]:
        return None, "other_profile", data
    return f"http://{LOOPBACK}:{data['port']}/", "running", data


def find(home, timeout=PROBE):
    """(url, state) of the app serving the profile in `home` – probe()
    without the record."""
    return probe(home, timeout)[:2]


def url_of(record):
    """The url an instance.json record points at – for a caller that
    decided a slow instance is the one to open."""
    return f"http://{LOOPBACK}:{record['port']}/"
