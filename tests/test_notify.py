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


def test_send_is_a_noop_off_macos(monkeypatch):
    monkeypatch.setattr(notify, "send", _REAL_SEND)
    called = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(notify, "_mac_native", lambda *a: called.append(a))
    monkeypatch.setattr(notify, "_mac_osascript", lambda *a: called.append(a))
    notify.send("t", "b")
    assert called == []


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
