"""Shared guards for every test module.

System notifications are silenced globally: JobRunner tests finish runs, and
without this every failing fake run would pop a real notification on a
developer's Mac (and wait up to ten seconds for osascript each time).
tests/test_notify.py re-enables the real function explicitly.
"""

import pytest

import graph_client
import notify


@pytest.fixture(autouse=True)
def _no_system_notifications(monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _throttle_gate_reset():
    """The throttle gate is process-global real-clock state: without this,
    any test that fakes a 429 leaves a gate the NEXT test waits out – a
    flaky-order hang of up to 300 s."""
    graph_client._DROSSEL["bis"] = 0.0
    yield
    graph_client._DROSSEL["bis"] = 0.0
