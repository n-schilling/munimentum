"""notify.py – native notifications, and the JobRunner's decision to send one.

The system side is mocked throughout: tests must pass on every OS, and no
test may ever pop a real notification on a developer's machine.
"""

import sys

import notify

# conftest.py silences notify.send for the whole suite – these tests are the
# exception and put the real function back first.
_REAL_SEND = notify.send


def _reset():
    notify._center = "unresolved"


def test_send_picks_the_backend_by_platform(monkeypatch):
    monkeypatch.setattr(notify, "send", _REAL_SEND)
    rufe = []
    monkeypatch.setattr(notify, "_mac_native", lambda *a: rufe.append("mac"))
    monkeypatch.setattr(notify, "_mac_osascript", lambda *a: rufe.append("osa"))
    monkeypatch.setattr(notify, "_win_toast", lambda *a: rufe.append("win"))
    monkeypatch.setattr(notify, "_linux_notify", lambda *a: rufe.append("lin"))
    monkeypatch.setattr(sys, "platform", "win32")
    notify.send("t", "b")
    monkeypatch.setattr(sys, "platform", "linux")
    notify.send("t", "b")
    monkeypatch.setattr(sys, "platform", "openbsd7")
    notify.send("t", "b")          # no backend: quietly nothing
    assert rufe == ["win", "lin"]


def test_windows_toast_carries_values_by_env(monkeypatch):
    """Titles with quotes must not become PowerShell, and the click target
    rides along as MUNI_URL – the script escapes and embeds it itself."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env") or {}

    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    monkeypatch.setattr(notify, "_open_url", "http://127.0.0.1:8700")
    notify._win_toast('T "quote"', "B & <xml>")
    assert seen["cmd"][0] == "powershell"
    assert "-NonInteractive" in seen["cmd"]
    assert seen["env"]["MUNI_TITLE"] == 'T "quote"'
    assert seen["env"]["MUNI_BODY"] == "B & <xml>"
    assert seen["env"]["MUNI_URL"] == "http://127.0.0.1:8700"


def test_linux_uses_notify_send_when_present(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify.shutil, "which",
                        lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda cmd, **kw: seen.setdefault("cmd", cmd))
    notify._linux_notify("Titel", "Text")
    assert seen["cmd"] == ["/usr/bin/notify-send", "--app-name=Munimentum",
                           "Titel", "Text"]


def test_linux_without_notify_send_stays_quiet(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    def boom(*a, **kw):
        raise AssertionError("darf nicht aufgerufen werden")
    monkeypatch.setattr(notify.subprocess, "run", boom)
    notify._linux_notify("t", "b")


def test_send_falls_back_to_osascript(monkeypatch):
    monkeypatch.setattr(notify, "send", _REAL_SEND)
    seen = {}
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(notify, "_mac_native", lambda *a: False)
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda cmd, **kw: seen.setdefault("cmd", cmd))
    notify.send("Title", 'Body with "quotes"')
    # Arguments travel as argv – quoting in the text must not matter.
    assert seen["cmd"][0] == "osascript"
    assert seen["cmd"][-2:] == ["Title", 'Body with "quotes"']


def test_send_never_raises(monkeypatch):
    monkeypatch.setattr(notify, "send", _REAL_SEND)
    def boom(*a):
        raise RuntimeError("kaputt")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(notify, "_mac_native", boom)
    monkeypatch.setattr(notify, "_mac_osascript", boom)
    notify.send("t", "b")          # must swallow everything


def test_center_unavailable_outside_the_bundle(monkeypatch):
    """No bundle identifier (run from source): the center resolves to None
    exactly once, then stays None – osascript takes over."""
    import types

    bundle = types.SimpleNamespace(bundleIdentifier=lambda: None)
    foundation = types.SimpleNamespace(
        NSBundle=types.SimpleNamespace(mainBundle=lambda: bundle))
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setitem(sys.modules, "UserNotifications", types.SimpleNamespace())
    _reset()
    try:
        assert notify._mac_center() is None
        assert notify._center is None
    finally:
        _reset()


def test_click_handler_unavailable_outside_the_bundle(monkeypatch):
    """From source there is no native center – the caller must keep the
    plain serve_forever path, so this has to say False, not raise."""
    import types

    bundle = types.SimpleNamespace(bundleIdentifier=lambda: None)
    foundation = types.SimpleNamespace(
        NSBundle=types.SimpleNamespace(mainBundle=lambda: bundle))
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setitem(sys.modules, "UserNotifications", types.SimpleNamespace())
    _reset()
    try:
        assert notify.install_click_handler(lambda: None) is False
    finally:
        _reset()
