"""awake.py – the guard against idle sleep, one backend per platform."""

import os
import sys

import pytest

import awake

# conftest stubs the guard for every test; these cases need the real one.
_ECHT = {"an": awake.Wachhalter.an, "aktiv": awake.Wachhalter.aktiv,
         "aus": awake.Wachhalter.aus}


@pytest.fixture(autouse=True)
def _echter_wachhalter(monkeypatch):
    for name, wert in _ECHT.items():
        monkeypatch.setattr(awake.Wachhalter, name, wert)


class _Prozess:
    def __init__(self, argv, **kw):
        self.argv, self.kw, self.lebt, self.beendet = argv, kw, True, 0

    def poll(self):
        return None if self.lebt else 0

    def terminate(self):
        self.beendet += 1
        self.lebt = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.lebt = False


def _mit_prozess(monkeypatch, plattform, werkzeug="/usr/bin/x"):
    gestartet = []
    monkeypatch.setattr(sys, "platform", plattform)
    monkeypatch.setattr(awake.shutil, "which", lambda name: werkzeug)

    def popen(argv, **kw):
        p = _Prozess(argv, **kw)
        gestartet.append(p)
        return p
    monkeypatch.setattr(awake.subprocess, "Popen", popen)
    return gestartet


def test_mac_haelt_ueber_caffeinate_an_unseren_prozess_gebunden(monkeypatch):
    gestartet = _mit_prozess(monkeypatch, "darwin")
    w = awake.Wachhalter()
    assert w.an() and w.aktiv
    (p,) = gestartet
    assert p.argv == ["caffeinate", "-i", "-w", str(os.getpid())]
    assert p.kw["stdout"] == awake.subprocess.DEVNULL
    w.aus()
    assert p.beendet == 1 and not w.aktiv
    w.aus()                                    # a second release is harmless
    assert p.beendet == 1


def test_mac_ohne_caffeinate_sagt_nein(monkeypatch):
    gestartet = _mit_prozess(monkeypatch, "darwin", werkzeug=None)
    w = awake.Wachhalter()
    assert not w.an() and not w.aktiv and gestartet == []
    w.aus()


def test_linux_nutzt_systemd_inhibit_um_einen_wartenden_tail(monkeypatch):
    gestartet = _mit_prozess(monkeypatch, "linux")
    w = awake.Wachhalter(grund="Export")
    assert w.an()
    (p,) = gestartet
    assert p.argv[:5] == ["systemd-inhibit", "--what=idle:sleep",
                          "--who=Munimentum", "--why=Export", "--mode=block"]
    assert p.argv[5:] == ["tail", "--pid", str(os.getpid()), "-f", "/dev/null"]
    w.aus()
    assert p.beendet == 1


def test_linux_ohne_systemd_sagt_nein(monkeypatch):
    gestartet = _mit_prozess(monkeypatch, "linux", werkzeug=None)
    assert not awake.Wachhalter().an() and gestartet == []


def test_windows_setzt_und_loest_die_energieanforderung(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    aufrufe = []

    class Kernel:
        def SetThreadExecutionState(self, flags):
            aufrufe.append(flags)
            return 0x80000000
    monkeypatch.setattr(awake, "_kernel32", lambda: Kernel())
    w = awake.Wachhalter()
    assert w.an() and w.aktiv
    w.aus()
    assert not w.aktiv
    assert aufrufe == [awake.ES_CONTINUOUS | awake.ES_SYSTEM_REQUIRED,
                       awake.ES_CONTINUOUS]


def test_windows_abgelehnte_anforderung_wird_nicht_geloest(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    aufrufe = []

    class Kernel:
        def SetThreadExecutionState(self, flags):
            aufrufe.append(flags)
            return 0
    monkeypatch.setattr(awake, "_kernel32", lambda: Kernel())
    w = awake.Wachhalter()
    assert not w.an() and not w.aktiv
    w.aus()
    assert len(aufrufe) == 1


def test_fremde_plattform_und_fehler_sagen_nein(monkeypatch):
    monkeypatch.setattr(sys, "platform", "openbsd7")
    assert not awake.Wachhalter().an()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(awake.shutil, "which", lambda name: "/usr/bin/caffeinate")

    def boom(*a, **kw):
        raise OSError("no fork")
    monkeypatch.setattr(awake.subprocess, "Popen", boom)
    assert not awake.Wachhalter().an()          # never raises into the run
