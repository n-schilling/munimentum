#!/usr/bin/env python3
"""
awake.py – keep the machine from idle sleep while a run is on.

A laptop that dozes off halfway through an export leaves the run hanging
until someone opens the lid; the next run resumes, but the night is gone.
The runner holds this guard for the length of one run when the setting
"keep awake" is on, and lets go the moment the run ends – cancelled or
crashed alike, the release sits in a finally.

Three backends, one per platform, each the operating system's own way:

    macOS    caffeinate -i -w <our pid>: no idle sleep while the assertion
             stands; tied to our process, so a killed app takes it along.
    Windows  SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED),
             a power request on the calling thread – the run thread – and
             cleared on it; a thread that ends drops it anyway.
    Linux    systemd-inhibit --what=idle:sleep around `tail --pid <our pid>`,
             which ends when we do; without systemd there is nothing to do.

Only idle sleep is held off: the display may still go dark, and a lid that
closes still sleeps – that is a choice, not inactivity. Nothing here can
fail a run: an unsupported platform or a missing tool says so once in the
log and the export runs as before.
"""

import os
import shutil
import subprocess
import sys

_STILL = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL}
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class Wachhalter:
    """One run's guard against idle sleep: an() before the steps, aus()
    after them – both on the same thread."""

    def __init__(self, grund="Munimentum export running"):
        self.grund = grund
        self._proc = None
        self._windows = False

    @property
    def aktiv(self):
        return self._windows or (self._proc is not None and self._proc.poll() is None)

    def an(self):
        """Take the guard. Returns True when the platform holds it, False
        when there is nothing to hold it with – never raises."""
        try:
            if sys.platform == "darwin":
                return self._starte(["caffeinate", "-i", "-w", str(os.getpid())])
            if sys.platform == "win32":
                return self._windows_an()
            if sys.platform.startswith("linux"):
                if not shutil.which("systemd-inhibit"):
                    return False
                return self._starte([
                    "systemd-inhibit", "--what=idle:sleep", "--who=Munimentum",
                    f"--why={self.grund}", "--mode=block",
                    "tail", "--pid", str(os.getpid()), "-f", "/dev/null"])
        except Exception:
            return False
        return False

    def aus(self):
        """Let go. Safe to call twice, and without an() before it."""
        if self._windows:
            try:
                _kernel32().SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass
            self._windows = False
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    def _starte(self, argv):
        if not shutil.which(argv[0]):
            return False
        self._proc = subprocess.Popen(argv, **_STILL)
        return True

    def _windows_an(self):
        # A zero return means the request was refused – nothing to release.
        stand = _kernel32().SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        self._windows = bool(stand)
        return self._windows


def _kernel32():
    import ctypes
    return ctypes.windll.kernel32
