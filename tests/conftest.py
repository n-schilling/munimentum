"""Shared guards for every test module.

System notifications are silenced globally: JobRunner tests finish runs, and
without this every failing fake run would pop a real notification on a
developer's Mac (and wait up to ten seconds for osascript each time).
tests/test_notify.py re-enables the real function explicitly.
"""

import os
from pathlib import Path

import pytest

import app as app_mod
import graph_client
import notify

# The project folder is the app folder of a source run – and so it holds
# the developer's REAL profiles. No test may leave an archive file or a
# profile there; the layout move at start would turn a stray runs.db into
# a profile of its own.
PROJEKT = Path(__file__).resolve().parents[1]
ARCHIV_NAMEN = ("app_config.json", "gx_token.txt", "msal_cache.bin", "runs.db",
                "data", "rag_store", "profiles.json")


def _projekt_stand():
    profile = PROJEKT / "profiles"
    return ({n: (PROJEKT / n).exists() for n in ARCHIV_NAMEN},
            sorted(p.name for p in profile.iterdir()) if profile.is_dir() else None)


@pytest.fixture(autouse=True)
def _no_system_notifications(monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _throttle_gate_reset():
    """The throttle gate is process-global real-clock state: without this,
    any test that fakes a 429 leaves a gate the NEXT test waits out – a
    flaky-order hang of up to 300 s."""
    graph_client._DROSSEL["bis"] = 0.0
    graph_client.TAKT = None
    yield
    graph_client._DROSSEL["bis"] = 0.0
    graph_client.TAKT = None


@pytest.fixture(autouse=True)
def _home_env_reset():
    """app.set_profil() points the process at a profile through
    MUNIMENTUM_HOME, app.set_data_dir() at one folder through
    MUNIMENTUM_DATA_DIR – the same variables every subprocess gets – so a
    test that runs main() would leave every later settings reader looking
    into the wrong folder, or believing there are no profiles. Put the
    variables back the way they were."""
    namen = ("MUNIMENTUM_HOME", "MUNIMENTUM_PROFILE",
             "MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR")
    vorher = {n: os.environ.get(n) for n in namen}
    yield
    for n, wert in vorher.items():
        if wert is None:
            os.environ.pop(n, None)
        else:
            os.environ[n] = wert


@pytest.fixture(autouse=True)
def _nichts_im_projektordner():
    vorher = _projekt_stand()
    yield
    nachher = _projekt_stand()
    assert nachher == vorher, (
        "the test wrote into the project folder – the app folder of a source "
        f"run: {vorher} -> {nachher}")


@pytest.fixture
def with_ollama(monkeypatch):
    """A reachable Ollama with both models – so a status never probes the
    network and never waits for it."""
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": True, "models": [model], "has_model": True,
                            "has_chat_model": True, "error": None, "model": model,
                            "chat_model": chat_model, "url": url})


@pytest.fixture(autouse=True)
def _kurzer_poll(monkeypatch):
    """serve_forever() notices shutdown() only once per poll interval –
    half a second of idle waiting per server in the app, 50 ms in tests."""
    monkeypatch.setattr(app_mod, "POLL", 0.05)
