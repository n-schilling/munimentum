"""Shared guards for every test module.

System notifications are silenced globally: JobRunner tests finish runs, and
without this every failing fake run would pop a real notification on a
developer's Mac (and wait up to ten seconds for osascript each time).
tests/test_notify.py re-enables the real function explicitly.
"""

import pytest

import notify


@pytest.fixture(autouse=True)
def _no_system_notifications(monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a, **kw: None)
