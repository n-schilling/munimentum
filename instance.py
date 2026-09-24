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
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import settings

FILE = "instance.json"
API_STATUS = "/api/v1/status"
LOOPBACK = "127.0.0.1"


def write(home, port, profile):
    """Say where this process serves the profile – atomically, so a
    reader never sees half a file."""
    home = Path(home)
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
    """Take the file away – only this process's own: a second start that
    found the first one running wrote nothing, and must not take the
    first one's file with it."""
    data = read(home)
    if data is not None and data.get("pid") == os.getpid():
        try:
            (Path(home) / FILE).unlink()
        except OSError:
            pass


def profile_at(port, host=LOOPBACK, timeout=1.5):
    """The profile an instance of this app serves on the port – or None
    when nothing of ours answers there."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{API_STATUS}", timeout=timeout) as r:
            # The header says whose answer this is – every one of ours
            # carries it, and no other server on a free port will.
            ours = r.headers.get("X-Munimentum-Api")
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not ours or not (isinstance(data, dict) and "token" in data and "jobs" in data):
        return None
    return (data.get("profile") or {}).get("name") or settings.STANDARD_PROFIL


def answers(port, host=LOOPBACK, timeout=1.5, profile=None):
    """Does an instance of this app answer on the port – of this profile,
    when one is named?"""
    running = profile_at(port, host, timeout)
    if running is None:
        return False
    return profile is None or running == profile


def find(home, timeout=0.5):
    """(url, state) of the app serving the profile in `home`: the url and
    "running", or None and "not_running" (no file, or nothing of ours on
    its port) or "other_profile" (the port now serves another one)."""
    data = read(home) if home else None
    if data is None:
        return None, "not_running"
    running = profile_at(data["port"], LOOPBACK, timeout)
    if running is None:
        return None, "not_running"
    if data.get("profile") and running != data["profile"]:
        return None, "other_profile"
    return f"http://{LOOPBACK}:{data['port']}/", "running"
