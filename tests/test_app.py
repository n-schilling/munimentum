"""Tests for app.py – UI, wizards, runs, schedule, MCP, search.

Nothing ever goes out to the network: Graph is not contacted at all (the app
only starts the export scripts as subprocesses, replaced here by short
python -c calls), Ollama is mocked. The search part runs against a real small
store written by rag_index.py – so the schema is guaranteed to match.

app.BASE, app.CONFIG_FILE and app.TOKEN_FILE point at tmp_path in every test
so nothing lands in the project folder.
"""

import base64
import gzip
import http.client
import json
import state_db
import os
import re
import types
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import app as app_mod
import settings
import runner as runner_mod
from hilfen import call
import i18n
import corpus
import folders as folders_mod
import rag_index


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def schluessel(m):
    """Text key of a message. Server-side messages are {k, v} –
    translation happens only in the UI."""
    return m.get("k") if isinstance(m, dict) else m


def werte(m):
    return (m or {}).get("v", {}) if isinstance(m, dict) else {}


def make_jwt(exp=None, scp="Mail.Read User.Read", upn="a@example.com", name="A B"):
    """JWT without a valid signature – app.decode_jwt does not check it either."""
    claims = {"scp": scp, "upn": upn, "name": name}
    if exp is not None:
        claims["exp"] = exp
    def seg(d):
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return seg({"alg": "none"}) + "." + seg(claims) + "." + "x" * 43


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Bend app.py so that all paths live in tmp_path."""
    monkeypatch.setattr(app_mod, "WURZEL", tmp_path)
    monkeypatch.setattr(app_mod, "HEIM", tmp_path)
    monkeypatch.setattr(app_mod, "BASE", tmp_path)
    monkeypatch.setattr(app_mod, "STORE_PFAD", tmp_path / app_mod.STORE_DIR)
    monkeypatch.setattr(app_mod, "CONFIG_FILE", tmp_path / "app_config.json")
    monkeypatch.setattr(app_mod, "TOKEN_FILE", tmp_path / "gx_token.txt")
    return tmp_path


def _cfg_mit_kategorien(**extra):
    """Configuration with categories selected.

    The default deliberately selects nothing: every category can mean tens of
    thousands of items. A test that expects an export step must therefore say
    what should be exported – just like a user would.
    """
    cfg = app_mod.load_config()
    cfg["outlook_categories"] = ["mail", "calendar", "contacts"]
    cfg["teams_categories"] = ["1on1", "group", "meeting"]
    cfg.update(extra)
    return cfg


@pytest.fixture
def no_ollama(monkeypatch):
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": False, "models": [], "has_model": False,
                            "has_chat_model": False, "error": "ConnectionError",
                            "model": model, "chat_model": chat_model, "url": url})


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def test_load_config_ergaenzt_fehlende_schluessel(sandbox):
    (sandbox / "app_config.json").write_text(
        json.dumps({"workers": 2, "schedule": {"enabled": True}}), encoding="utf-8")
    cfg = app_mod.load_config()
    assert cfg["workers"] == 2
    assert cfg["schedule"]["enabled"] is True
    assert cfg["schedule"]["interval_minutes"] == 60      # default is preserved


def test_load_config_bei_kaputter_datei(sandbox):
    (sandbox / "app_config.json").write_text("{kein json", encoding="utf-8")
    assert app_mod.load_config() == app_mod.DEFAULT_CONFIG


def test_load_config_ignoriert_unbekannte_schluessel(sandbox):
    (sandbox / "app_config.json").write_text(
        json.dumps({"boesartig": "x"}), encoding="utf-8")
    assert "boesartig" not in app_mod.load_config()


def test_save_config_roundtrip(sandbox):
    cfg = app_mod.load_config()
    cfg["mcp_port"] = 9999
    app_mod.save_config(cfg)
    assert app_mod.load_config()["mcp_port"] == 9999
    assert not (sandbox / "app_config.json.tmp").exists()   # atomic swap


def test_skip_folders_default_ist_mit_outlook_export_deckungsgleich():
    """app.py mirrors the list instead of importing outlook_export (the module
    aborts without msal/requests). So the copy does not drift away, this test
    keeps the two together."""
    import outlook_export
    assert app_mod.SKIP_FOLDERS_DEFAULT == outlook_export.BUILTIN_SKIP_FOLDERS


@pytest.mark.parametrize("eingabe,erwartet", [
    (["Archiv", "archiv", " Drafts "], ["archiv", "drafts"]),
    ("Archiv, Drafts", ["archiv", "drafts"]),
    ("Archiv\nDrafts\n\n", ["archiv", "drafts"]),      # text field with lines
    ([], []),
    (None, []),
    ("  ,  ", []),
])
def test_clean_folders(eingabe, erwartet):
    assert app_mod._clean_folders(eingabe) == erwartet


def test_clean_categories_filtert_und_sortiert():
    erlaubt = ["mail", "calendar", "contacts"]
    assert app_mod._clean_categories(["CONTACTS", "mail", "quatsch"], erlaubt) \
        == ["mail", "contacts"]                             # order follows `erlaubt`
    assert app_mod._clean_categories(None, erlaubt) == []


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------
@pytest.mark.parametrize("roh,erwartet", [
    ("  eyJabc  ", "eyJabc"),
    ('"eyJabc"', "eyJabc"),
    ("Bearer eyJabc", "eyJabc"),
    ("bearer  eyJabc", "eyJabc"),
    ("eyJ\nabc\n def", "eyJabcdef"),                        # line breaks from copying
    ("", ""),
    (None, ""),
])
def test_normalize_token(roh, erwartet):
    assert app_mod.normalize_token(roh) == erwartet


def test_decode_jwt_liest_claims():
    claims = app_mod.decode_jwt(make_jwt(exp=1234, scp="Mail.Read"))
    assert claims["exp"] == 1234 and claims["scp"] == "Mail.Read"


@pytest.mark.parametrize("kaputt", ["", "abc", "a.b", "a.@@@@.c"])
def test_decode_jwt_bei_unlesbarem_token(kaputt):
    assert app_mod.decode_jwt(kaputt) == {}


def test_token_status_gueltig_mit_allen_rechten():
    now = 1_000_000
    tok = make_jwt(exp=now + 3600, scp="Mail.Read Chat.Read User.Read")
    st = app_mod.token_status(tok, now=now, needed=["mail", "1on1"])
    assert st["present"] and st["valid"] and not st["expired"]
    assert st["readable"] and st["account"] == "a@example.com"
    assert st["expires_in_minutes"] == 60
    assert st["missing"] == []


def test_token_status_abgelaufen():
    now = 1_000_000
    st = app_mod.token_status(make_jwt(exp=now - 60), now=now)
    assert st["expired"] and not st["valid"]
    assert st["expires_in_minutes"] == -1


def test_token_status_meldet_fehlende_rechte():
    now = 1_000_000
    tok = make_jwt(exp=now + 600, scp="Mail.Read User.Read")
    st = app_mod.token_status(tok, now=now, needed=["mail", "calendar", "channels"])
    assert st["missing"] == ["Calendars.Read", "ChannelMessage.Read.All"]


def test_token_status_akzeptiert_umfassendere_berechtigung():
    """The Graph Explorer often grants the write variant right away. Whoever
    has Mail.ReadWrite may certainly read – but Mail.Read then never appears
    in the token, and the wizard reported rights as missing that are there."""
    now = 1_000_000
    tok = make_jwt(exp=now + 600,
                   scp="Mail.ReadWrite Contacts.ReadWrite Calendars.Read "
                       "Chat.ReadWrite Group.Read.All User.Read")
    st = app_mod.token_status(tok, now=now, needed=["mail", "contacts", "calendar",
                                                    "1on1", "channels"])
    assert st["missing"] == []


@pytest.mark.parametrize("haben,fehlt", [
    (["Mail.ReadWrite"], []),
    (["Mail.Read.Shared"], []),
    (["Mail.ReadWrite.Shared"], []),
    (["Mail.Read"], []),
    # ReadBasic returns no message bodies – does not cover the export
    (["Mail.ReadBasic"], ["Mail.Read"]),
    (["Calendars.Read"], ["Mail.Read"]),
    ([], ["Mail.Read"]),
])
def test_scope_missing_mail(haben, fehlt):
    assert app_mod.scope_missing({"Mail.Read"}, haben) == fehlt


def test_scope_missing_kanalnachrichten_ueber_gruppenrechte():
    assert app_mod.scope_missing({"ChannelMessage.Read.All"}, ["Group.Read.All"]) == []
    assert app_mod.scope_missing({"ChannelMessage.Read.All"}, ["Chat.Read"]) \
        == ["ChannelMessage.Read.All"]


def test_scope_missing_ohne_ersatz_bleibt_streng():
    """Chat.ReadBasic reads no message bodies – not a valid substitute."""
    assert app_mod.scope_missing({"Chat.Read"}, ["Chat.ReadBasic"]) == ["Chat.Read"]


def test_jede_noetige_berechtigung_hat_eine_beispielabfrage():
    """For every right the wizard names the query that makes it visible in the
    Graph Explorer in the first place – otherwise you look for it in vain."""
    noetig = set(app_mod.SCOPE_FOR.values()) | {"User.Read"}
    assert noetig <= set(app_mod.SCOPE_QUERY)


def test_status_liefert_die_beispielabfragen(sandbox, with_ollama):
    u = app_mod.App(app_mod.load_config()).umgebung()
    assert all(u["scope_queries"].get(x, "").startswith("https://graph.microsoft.com/")
               for x in u["scopes_needed"])


def test_token_status_ohne_token():
    st = app_mod.token_status("")
    assert not st["present"] and not st["valid"] and st["missing"] == []


def test_token_status_unlesbar_gilt_als_vorhanden():
    """Graph tokens are officially opaque – a token that cannot be decoded is
    tried out instead of being prematurely reported as broken."""
    st = app_mod.token_status("undurchsichtig-aber-da", needed=["mail"])
    assert st["present"] and st["valid"] and not st["readable"]
    assert st["missing"] == []                              # no false alarm


def test_write_und_read_token(sandbox):
    app_mod.write_token("Bearer  eyJtest\n")
    assert (sandbox / "gx_token.txt").read_text(encoding="utf-8") == "eyJtest\n"
    assert app_mod.read_token() == "eyJtest"


def test_write_token_setzt_enge_rechte(sandbox):
    app_mod.write_token("eyJtest")
    if sys.platform != "win32":
        assert (sandbox / "gx_token.txt").stat().st_mode & 0o077 == 0


def test_read_token_ohne_datei(sandbox):
    assert app_mod.read_token() == ""


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------
def test_check_ollama_erkennt_modell_ohne_tag(monkeypatch):
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "bge-m3:latest"}, {"name": "qwen:7b"}]}
    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    out = app_mod.check_ollama("http://x", "bge-m3", "qwen:7b")
    assert out["running"] and out["has_model"]
    assert out["has_chat_model"] is True            # even without the exact tag
    assert out["models"] == ["bge-m3:latest", "qwen:7b"]


def test_check_ollama_modell_fehlt(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "qwen:7b"}]}
    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    out = app_mod.check_ollama("http://x", "bge-m3")
    assert out["running"] and not out["has_model"]
    assert out["has_chat_model"] is False           # no name, no model


def test_check_ollama_nicht_erreichbar(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr("requests.get", boom)
    out = app_mod.check_ollama("http://x", "bge-m3")
    assert not out["running"] and "connection refused" in out["error"]


@pytest.mark.parametrize("system,erwartet", [
    ("Darwin", "macOS"), ("Windows", "Windows"), ("Linux", "Linux")])
def test_ollama_hint_je_betriebssystem(monkeypatch, system, erwartet):
    monkeypatch.setattr(app_mod.platform, "system", lambda: system)
    h = app_mod.ollama_hint()
    assert h["os"] == erwartet and h["steps"]


# --------------------------------------------------------------------------
# State of exports and index
# --------------------------------------------------------------------------
def test_export_status_ohne_ordner(sandbox):
    st = app_mod.export_status(app_mod.load_config())
    assert not st["teams"]["exists"] and st["teams"]["last_run"] is None
    assert not st["outlook"]["exists"]


def test_export_status_mit_fortschrittsdateien(sandbox):
    import state_db
    state_db.StateDb(sandbox / "teams_export").kv_schreiben("state", "{}")
    log = state_db.DbDoneLog(state_db.StateDb(sandbox / "outlook_export"))
    log.mark("m", "a.eml")
    log.close()
    st = app_mod.export_status(app_mod.load_config())
    assert st["teams"]["exists"] and st["teams"]["last_run"]
    assert st["outlook"]["exists"] and st["outlook"]["last_run"]


def test_store_status_ohne_index(sandbox):
    st = app_mod.store_status(app_mod.load_config())
    assert not st["exists"] and st["chunks"] == 0 and not st["semantic"]
    assert st["messages"] == 0


def _index_bauen(sandbox, uids, chunks_je=3):
    """A tiny index: uids messages with chunks_je chunks each."""
    store = sandbox / "rag_store"
    store.mkdir(exist_ok=True)
    app_mod._ZAEHLUNG.clear()
    con = sqlite3.connect(store / "corpus.db")
    con.execute("CREATE TABLE IF NOT EXISTS chunks(uid TEXT, seq INTEGER)")
    con.executemany("INSERT INTO chunks(uid, seq) VALUES (?, ?)",
                    [(f"m{u}", s) for u in range(uids) for s in range(chunks_je)])
    con.commit()
    con.close()
    return store


def test_store_status_zaehlt_nachrichten_nicht_nur_textstellen(sandbox):
    """The tile names messages – the unit in which someone thinks of their
    archive. Long mails sit in the index as several chunks; the row count
    would thus be noticeably higher than what they expect to find again.
    """
    _index_bauen(sandbox, uids=4, chunks_je=3)
    st = app_mod.store_status(app_mod.load_config())
    assert st["chunks"] == 12 and st["messages"] == 4


def test_store_status_puffert_die_zaehlung(sandbox, monkeypatch):
    """The UI asks every few seconds – counting across the whole index must
    not happen every time."""
    _index_bauen(sandbox, uids=2)
    cfg = app_mod.load_config()

    abfragen = []
    echt = sqlite3.connect

    def mitzaehlen(*a, **kw):
        con = echt(*a, **kw)
        con.set_trace_callback(abfragen.append)
        return con
    monkeypatch.setattr(sqlite3, "connect", mitzaehlen)

    assert app_mod.store_status(cfg)["messages"] == 2
    erste = len(abfragen)
    assert erste >= 2                       # chunks and messages
    for _ in range(5):
        assert app_mod.store_status(cfg)["messages"] == 2
    assert len(abfragen) == erste, "zählt trotz unveränderter Datei erneut"


def test_store_status_zaehlt_nach_einer_aenderung_neu(sandbox):
    """The cache must not make a fresh index look old."""
    _index_bauen(sandbox, uids=2)
    cfg = app_mod.load_config()
    assert app_mod.store_status(cfg)["messages"] == 2

    store = sandbox / "rag_store"
    con = sqlite3.connect(store / "corpus.db")
    con.execute("INSERT INTO chunks(uid, seq) VALUES ('m99', 0)")
    con.commit()
    con.close()
    assert app_mod.store_status(cfg)["messages"] == 3


# --------------------------------------------------------------------------
# Calendar step: when it runs at all, and when with mail evaluation
#
# Reported from the field: a run with only "Contacts" still kicked off the
# reconstruction of deleted appointments – every one of the 45,000 mails was
# read, for minutes, for a result that could not possibly have changed.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cats,noetig,mit_mails", [
    (["mail", "calendar", "contacts"], True, True),
    (["calendar"], True, False),          # appointments yes, mails were not fetched
    (["contacts"], True, False),          # exactly the reported case
    (["mail"], False, True),              # nothing to build: no calendar, no contacts
    ([], False, False),
    (["mail", "contacts"], True, True),
])
def test_calendar_plan(sandbox, cats, noetig, mit_mails):
    cfg = app_mod.load_config()
    cfg["outlook_categories"] = cats
    assert app_mod.calendar_plan(cfg) == (noetig, mit_mails)


def test_build_steps_laesst_die_wiederherstellung_weg(sandbox):
    """Without mail evaluation the expensive part is dropped – visible in the
    switch and in the step carrying a different name."""
    cfg = app_mod.load_config()
    schritt = [s for s in app_mod.build_steps(cfg, {"calendar": True}, reconstruct=False)
               if s["key"] == "calendar"][0]
    assert "--no-reconstruct" in schritt["argv"]
    assert schritt["label"] == "job.step.calendar.plain"


def test_build_steps_folgt_der_einstellung(sandbox):
    """Without an explicit argument, app_config.json decides."""
    cfg = app_mod.load_config()
    voll = [s for s in app_mod.build_steps(cfg, {"calendar": True}) if s["key"] == "calendar"][0]
    assert "--no-reconstruct" not in voll["argv"]      # default: on

    cfg["calendar_reconstruct"] = False
    aus = [s for s in app_mod.build_steps(cfg, {"calendar": True}) if s["key"] == "calendar"][0]
    assert "--no-reconstruct" in aus["argv"]


def test_lauf_mit_nur_kontakten_liest_keine_mails(sandbox, monkeypatch, no_ollama):
    """The reported case, once through the whole path: /api/v1/runs -> build_steps."""
    gesehen = {}

    def merken(steps, label, **kw):
        gesehen["steps"] = steps
        return True
    app = app_mod.App()
    app.cfg["outlook_categories"] = ["contacts"]
    monkeypatch.setattr(app.jobs, "start", merken)
    monkeypatch.setattr(app_mod, "read_token", lambda *a, **kw: "tok")

    ok, _ = app.launch({"outlook": True, "index": True, "calendar": True}, reconstruct=False, label="job.export")
    assert ok
    kal = [s for s in gesehen["steps"] if s["key"] == "calendar"]
    assert kal and "--no-reconstruct" in kal[0]["argv"]


# --------------------------------------------------------------------------
# Steps of a run
# --------------------------------------------------------------------------
def test_build_steps_setzt_kategorien_und_token(sandbox):
    cfg = app_mod.load_config()
    cfg["outlook_categories"] = ["mail", "contacts"]
    cfg["teams_categories"] = ["1on1", "channels"]
    steps = app_mod.build_steps(cfg, {"outlook": True, "teams": True, "index": True}, token="tok")

    assert [s["key"] for s in steps] == ["outlook", "teams", "index"]
    assert steps[0]["env"]["EXPORT_CATEGORIES"] == "mail,contacts"
    assert steps[1]["env"]["EXPORT_CATEGORIES"] == "1on1,channels"
    assert all(s["env"]["GRAPH_TOKEN"] == "tok" for s in steps)
    assert all(s["env"]["PYTHONUNBUFFERED"] == "1" for s in steps)
    assert steps[0]["argv"][1].endswith("outlook_export.py")
    assert "outlook_export" in steps[0]["argv"][2:]       # output folder
    assert "--no-embeddings" not in steps[2]["argv"]
    assert steps[1]["env"]["MUNIMENTUM_LANG"] == i18n.FALLBACK


def test_build_steps_hands_the_run_language_to_the_exports(sandbox):
    """A Teams export writes its deletion marks in the page's language."""
    cfg = app_mod.load_config()
    cfg["teams_categories"] = ["1on1"]
    steps = app_mod.build_steps(cfg, {"teams": True}, lang="fr")
    assert steps[0]["env"]["MUNIMENTUM_LANG"] == "fr"


def test_vorgabe_waehlt_nichts_aus(sandbox):
    """Every category can mean tens of thousands of items – what gets fetched
    should be a decision, not whatever happened to be checked."""
    cfg = app_mod.load_config()
    assert cfg["outlook_categories"] == [] and cfg["teams_categories"] == []
    assert cfg["onedrive_enabled"] is False


def test_api_files_ohne_index_meldet_den_grund(sandbox, server):
    _, port = server
    code, r = call(port, "GET", "/api/v1/files")
    if code == 503:                                   # no index in this sandbox
        assert r["roots"] == [] and r["error"]["k"] == "srv.noindex"
    else:
        assert code == 200 and "roots" in r


def test_pages_schritt_traegt_die_eigene_urlliste(sandbox):
    cfg = app_mod.load_config()
    cfg["sharepoint_pages_urls"] = "https://firma.sharepoint.com/sites/TeamX"
    steps = app_mod.build_steps(cfg, {"sharepoint_pages": True})
    assert [s["key"] for s in steps] == ["sharepoint_pages"]
    assert "--pages" in steps[0]["argv"]
    assert steps[0]["env"]["SHAREPOINT_PAGES_URLS"].startswith("https://")
    assert steps[0]["env"]["SHAREPOINT_PAGES_IMAGE_MAX_MB"] == "4"

    check = app_mod.build_steps(cfg, {"check_pages": True})
    assert "--check-pages" in check[0]["argv"]
    assert check[0]["env"]["SHAREPOINT_PAGES_URLS"].startswith("https://")

    url = "https://firma.sharepoint.com/sites/TeamX"
    cfg["sync_cadence"] = {f"sharepoint-url:{url}": "weekly"}
    nur = app_mod.build_steps(cfg, {"sharepoint": True}, nur_einheit=url)
    assert nur[0]["env"]["SHAREPOINT_URLS"] == url
    assert nur[0]["env"]["SYNC_NOW"] == "1"
    assert '"weekly"' in nur[0]["env"]["SYNC_CADENCE"]
    assert steps[0]["corpus"] is True


def test_planner_schritt_wird_gebaut(sandbox):
    """Regression: the subprogram was not in RUNNABLE – clicking "Start
    export" ended in an empty alert instead of a run."""
    cfg = app_mod.load_config()
    url = "https://planner.cloud.microsoft/webui/v1/plan/abcdefID123/view"
    cfg["planner_urls"] = url
    cfg["planner_attachments"] = True
    steps = app_mod.build_steps(cfg, {"planner": True})
    assert [s["key"] for s in steps] == ["planner"]
    assert steps[0]["env"]["PLANNER_URLS"] == url
    assert steps[0]["env"]["PLANNER_ATTACHMENTS"] == "1"
    assert steps[0]["corpus"] is True

    nur = app_mod.build_steps(cfg, {"planner": True}, nur_einheit=url)
    assert nur[0]["env"]["SYNC_NOW"] == "1"

    # "Read legacy comments again": only on request, never by default.
    assert "PLANNER_LEGACY_SYNC" not in steps[0]["env"]
    erneut = app_mod.build_steps(cfg, {"planner": True}, legacy_comments=True)
    assert erneut[0]["env"]["PLANNER_LEGACY_SYNC"] == "1"

    index = app_mod.build_steps(cfg, {"index": True})
    assert "--planner" in index[0]["argv"]


def test_index_schritt_traegt_den_absoluten_store(sandbox):
    """The index can live on another disk (index_dir) – the step receives the
    resolved path, not the folder name; and every subprocess finds the
    configuration via the fixed home folder."""
    steps = app_mod.build_steps(app_mod.load_config(), {"index": True})
    argv = [str(a) for a in steps[0]["argv"]]
    assert argv[argv.index("--store") + 1] == str(sandbox / app_mod.STORE_DIR)
    assert steps[0]["env"]["MUNIMENTUM_HOME"] == str(sandbox)


def test_index_schritt_kennt_den_sharepoint_ordner(sandbox):
    cfg = app_mod.load_config()
    steps = app_mod.build_steps(cfg, {"index": True})
    argv = steps[0]["argv"]
    assert "--sharepoint" in argv
    assert argv[argv.index("--sharepoint") + 1] == app_mod.SHAREPOINT_DIR


def test_sharepoint_schritt_traegt_urls_und_filter(sandbox):
    cfg = app_mod.load_config()
    cfg["sharepoint_urls"] = "https://firma.sharepoint.com/sites/TeamX"
    cfg["sharepoint_types_include"] = "pdf, docx"
    cfg["sharepoint_types_exclude"] = "mp4"
    cfg["sharepoint_max_mb"] = 200
    steps = app_mod.build_steps(cfg, {"sharepoint": True})
    assert [s["key"] for s in steps] == ["sharepoint"]
    env = steps[0]["env"]
    assert env["SHAREPOINT_URLS"].startswith("https://firma.sharepoint.com")
    assert env["SHAREPOINT_TYPES_INCLUDE"] == "pdf, docx"
    assert env["SHAREPOINT_TYPES_EXCLUDE"] == "mp4"
    assert env["SHAREPOINT_MAX_MB"] == "200"
    assert steps[0]["corpus"] is True

    sync = app_mod.build_steps(cfg, {"sync_sharepoint": True})
    check = app_mod.build_steps(cfg, {"check_sharepoint": True})
    assert "--folders" in sync[0]["argv"] and "--check" in check[0]["argv"]


def test_save_config_uebernimmt_spiegel_haken_und_sharepoint(sandbox, server):
    """Regression: onedrive_enabled reached the run but never survived a
    page rebuild – the checkbox was never saved."""
    _, port = server
    code, r = call(port, "PATCH", "/api/v1/config", {
        "onedrive_enabled": True, "sharepoint_enabled": True,
        "sharepoint_urls": "  https://firma.sharepoint.com/sites/TeamX  \n\n",
        "sharepoint_types_include": " .PDF, docx ,",
        "sharepoint_types_exclude": "MP4",
        "sharepoint_max_mb": 250})
    assert code == 200
    cfg = r["config"]
    assert cfg["onedrive_enabled"] is True
    assert cfg["sharepoint_enabled"] is True
    assert cfg["sharepoint_urls"] == "https://firma.sharepoint.com/sites/TeamX"
    assert cfg["sharepoint_types_include"] == "pdf, docx"
    assert cfg["sharepoint_types_exclude"] == "mp4"
    assert cfg["sharepoint_max_mb"] == 250


def test_ohne_kategorie_kein_schritt(sandbox):
    """The script reads an empty EXPORT_CATEGORIES as "not set" and then
    fetched everything. The schedule would thus bypass the selection."""
    cfg = app_mod.load_config()
    assert app_mod.build_steps(cfg, {"outlook": True, "teams": True}) == []

    cfg["outlook_categories"] = ["contacts"]
    steps = app_mod.build_steps(cfg, {"outlook": True, "teams": True})
    assert [s["key"] for s in steps] == ["outlook"]
    assert steps[0]["env"]["EXPORT_CATEGORIES"] == "contacts"


def test_build_steps_setzt_die_kategorien_immer(sandbox):
    """No export step may be able to ask questions when run from the app.

    The scripts no longer prompt; the promise now is that the app passes its
    selection completely via EXPORT_CATEGORIES – otherwise a step silently
    exported the default instead of the setting.
    """
    steps = app_mod.build_steps(_cfg_mit_kategorien(), {"outlook": True, "teams": True})
    assert [s["key"] for s in steps] == ["outlook", "teams"]
    for s in steps:
        assert s["env"]["EXPORT_CATEGORIES"], s["key"]


def test_build_steps_ohne_embeddings(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), {"index": True}, embeddings=False)
    assert steps[0]["argv"][-1] == "--no-embeddings"
    assert steps[0]["label"] == "job.step.index.lexical"


def test_build_steps_ohne_token_setzt_keine_variable(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), {"index": True})
    assert "GRAPH_TOKEN" not in steps[0]["env"]


def test_build_steps_leere_auswahl(sandbox):
    assert app_mod.build_steps(app_mod.load_config(), {}) == []


def test_build_steps_reicht_die_schalter_durch(sandbox):
    """Everything shown in the UI must also arrive at the script – otherwise
    a click changes only the file and not the run."""
    cfg = _cfg_mit_kategorien()
    cfg.update(embed_images=False, cache_images=False, refresh_channels=False,
               skip_empty_chats=False, include_hidden=True,
               skip_folders=["archiv", "drafts"], workers=2, index_batch=8)
    steps = {s["key"]: s for s in app_mod.build_steps(cfg, {"outlook": True, "teams": True, "index": True}, token="t")}

    o = steps["outlook"]["env"]
    assert o["INCLUDE_HIDDEN"] == "1" and o["SKIP_FOLDERS"] == "archiv,drafts"
    assert o["EXPORT_WORKERS"] == "2"

    t = steps["teams"]["env"]
    assert t["EMBED_IMAGES"] == "0" and t["CACHE_IMAGES"] == "0"
    assert t["REFRESH_CHANNELS"] == "0" and t["SKIP_EMPTY_CHATS"] == "0"

    argv = steps["index"]["argv"]
    assert argv[argv.index("--batch") + 1] == "8"


def test_build_steps_leere_ordnerliste_wird_gesetzt(sandbox):
    """Empty means "skip nothing". The variable must still be set – unset
    would mean "use your default" to outlook_export.py."""
    cfg = _cfg_mit_kategorien(skip_folders=[])
    env = app_mod.build_steps(cfg, {"outlook": True}, token="t")[0]["env"]
    assert env["SKIP_FOLDERS"] == "" and "SKIP_FOLDERS" in env


def test_build_steps_vorgaben_schalten_nichts_ab(sandbox):
    cfg = _cfg_mit_kategorien()
    steps = {s["key"]: s for s in app_mod.build_steps(cfg, {"outlook": True, "teams": True}, token="t")}
    assert steps["teams"]["env"]["EMBED_IMAGES"] == "1"
    assert steps["outlook"]["env"]["INCLUDE_HIDDEN"] == "0"
    assert steps["outlook"]["env"]["SKIP_FOLDERS"].split(",") \
        == sorted(app_mod.SKIP_FOLDERS_DEFAULT)


def _env_namen(modul):
    """Environment variables a script reads via settings – from the source.

    Self-sustaining: when a script gains a new setting, the test below fails
    as long as app.py does not pass it along.
    """
    quelle = (Path(app_mod.__file__).parent / f"{modul}.py").read_text(encoding="utf-8")
    return set(re.findall(r'settings\.(?:flag|folders|number)\(\s*"([A-Z_0-9]+)"', quelle))


@pytest.mark.parametrize("modul,key", [("teams_export", "teams"),
                                       ("outlook_export", "outlook")])
def test_app_setzt_alles_was_die_skripte_sonst_aus_der_datei_laesen(sandbox, modul, key):
    """For a run from the app the environment must be complete – otherwise
    partly the UI would apply, partly app_config.json, and the script would
    report "taken from app_config.json" in the middle of an app run."""
    noetig = _env_namen(modul)
    assert noetig, f"keine settings-Aufrufe in {modul}.py gefunden"
    steps = {s["key"]: s for s in app_mod.build_steps(_cfg_mit_kategorien(), {"outlook": True, "teams": True}, token="t")}
    assert noetig <= set(steps[key]["env"])


def test_build_steps_kalender(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), {"calendar": True})
    assert steps[0]["key"] == "calendar"
    assert steps[0]["argv"][1].endswith("combined_search.py")
    assert "--json" in steps[0]["argv"]
    ziel = steps[0]["argv"][steps[0]["argv"].index("--json") + 1]
    assert ziel.endswith("calendar.json") and "rag_store" in ziel


def test_kalender_wird_dorthin_geschrieben_wo_alle_ihn_lesen(sandbox,
                                                             monkeypatch):
    """One file, one path. Spelled relative, the step wrote it under the
    subprocess cwd (the data folder) while the skip target, the status and
    /api/v1/calendar looked in the index folder – with data and index apart
    the calendar tab then stayed empty forever."""
    monkeypatch.setattr(app_mod, "BASE", sandbox / "data")
    monkeypatch.setattr(app_mod, "STORE_PFAD", sandbox / "woanders")
    schritt = app_mod.build_steps(app_mod.load_config(), {"calendar": True})[0]
    geschrieben = schritt["argv"][schritt["argv"].index("--json") + 1]
    assert Path(geschrieben).is_absolute(), "relativ = relativ zum cwd des Laufs"
    gelesen = app_mod.calendar_file(app_mod.load_config())
    assert Path(geschrieben) == gelesen == schritt["ziel"]


def test_build_steps_reihenfolge_export_index_kalender(sandbox):
    steps = app_mod.build_steps(_cfg_mit_kategorien(), {"outlook": True, "teams": True, "index": True, "calendar": True}, token="t")
    assert [s["key"] for s in steps] == ["outlook", "teams", "index", "calendar"]


@pytest.mark.parametrize("last,jetzt,faellig", [
    (None, 1000, True),               # never ran before
    (1000, 1000 + 59 * 60, False),
    (1000, 1000 + 60 * 60, True),
    (1000, 1000 + 61 * 60, True),
])
def test_due_now(last, jetzt, faellig):
    assert app_mod.due_now(last, 60, jetzt) is faellig


# --------------------------------------------------------------------------
# Output of the subprocesses
# --------------------------------------------------------------------------
def test_stream_lines_trennt_auch_an_wagenruecklauf():
    """rag_index.py overwrites its progress line with \\r instead of \\n –
    readline() would block until the end of the step."""
    import io
    roh = b"start\n  1/9 fertig\r  2/9 fertig\rende\n"
    assert list(app_mod._stream_lines(io.BytesIO(roh))) == \
        ["start", "  1/9 fertig", "  2/9 fertig", "ende"]


def test_stream_lines_haelt_rest_ohne_zeilenende():
    import io
    assert list(app_mod._stream_lines(io.BytesIO(b"abc"))) == ["abc"]


def test_stream_lines_ueberspringt_leerzeilen():
    import io
    assert list(app_mod._stream_lines(io.BytesIO(b"a\n\n\nb\n"))) == ["a", "b"]


# --------------------------------------------------------------------------
# JobRunner – real subprocesses, but tiny ones
# --------------------------------------------------------------------------
def _py_step(code, label="Schritt"):
    return {"key": "t", "label": label, "argv": [sys.executable, "-c", code],
            "env": {}}


def _warte(runner, sekunden=15):
    ende = time.time() + sekunden
    while runner.busy and time.time() < ende:
        time.sleep(0.02)
    assert not runner.busy, "Lauf wurde nicht fertig"


def test_jobrunner_fuehrt_schritte_der_reihe_nach_aus(sandbox):
    r = app_mod.JobRunner()
    assert r.start([_py_step("print('eins')", "A"), _py_step("print('zwei')", "B")], "Lauf")
    _warte(r)
    text = "\n".join(str(ln["text"]) for ln in r.lines)
    assert "eins" in text and "zwei" in text
    assert r.last["ok"] and r.last["label"] == "Lauf"
    assert text.index("eins") < text.index("zwei")
    # Regression: the notify refactor swallowed this cleanup – with default
    # notification mode ("errors"), job and proc must still reset to None.
    assert r.job is None and r.proc is None


def test_job_nennt_den_logstand_vor_seiner_ersten_zeile(sandbox):
    """The run window shows the run's lines only: the job carries the log
    cursor from before its first line, and the run log from there on yields
    nothing of what the app logged earlier."""
    r = app_mod.JobRunner()
    r.logk("srv.token.ok")
    r.logk("srv.token.ok")
    vorher = r.seq
    assert r.start([_py_step("print('eins')", "A")], "Lauf")
    assert r.snapshot()["job"]["log_seq"] == vorher
    _warte(r)
    danach = [str(ln["text"]) for ln in r.log_since(vorher)[0]]
    assert danach and all("srv.token.ok" not in t for t in danach)
    assert any("eins" in t for t in danach)


def test_jobrunner_haelt_den_rechner_wach_und_laesst_wieder_los(monkeypatch, sandbox):
    """"Keep awake" is taken before the first step and released after the
    last – also after a failed one – and the log says so; off means the
    guard is never asked."""
    import runner
    folge = []

    class Fake:
        def __init__(self):
            self.aktiv = False

        def an(self):
            folge.append("an")
            self.aktiv = True
            return True

        def aus(self):
            folge.append("aus")
            self.aktiv = False
    monkeypatch.setattr(runner.awake, "Wachhalter", Fake)
    r = app_mod.JobRunner()
    r.start([_py_step("import sys; sys.exit(3)", "A"), _py_step("print('x')", "B")],
            "Lauf", context={"keep_awake": True})
    _warte(r)
    assert folge == ["an", "aus"] and not r.last["ok"]
    keys = [z["text"]["k"] for z in r.lines if isinstance(z["text"], dict)]
    assert keys.index("srv.awake.on") < keys.index("srv.job.step")
    folge.clear()
    r.start([_py_step("print('x')", "A")], "Lauf", context={"keep_awake": False})
    _warte(r)
    assert folge == []
    assert not any(isinstance(z["text"], dict) and z["text"]["k"].startswith("srv.awake")
                   for z in list(r.lines)[-6:])


def test_jobrunner_meldet_wenn_wachhalten_nicht_geht(monkeypatch, sandbox):
    import runner

    class Nein:
        aktiv = False

        def an(self):
            return False

        def aus(self):
            pass
    monkeypatch.setattr(runner.awake, "Wachhalter", Nein)
    r = app_mod.JobRunner()
    r.start([_py_step("print('x')", "A")], "Lauf", context={"keep_awake": True})
    _warte(r)
    zeilen = [z for z in r.lines if isinstance(z["text"], dict) and z["text"]["k"] == "srv.awake.fail"]
    assert len(zeilen) == 1 and zeilen[0]["level"] == "warn" and r.last["ok"]


def test_notify_user_mode_decides(monkeypatch, sandbox):
    """"errors" keeps quiet on success, "all" reports it, cancelled is never
    reported – and every body arrives translated, without raw keys."""
    sent = []
    monkeypatch.setattr(app_mod.notify, "send", lambda t, b: sent.append(b))
    r = app_mod.JobRunner()
    r._context = {"notify": "errors", "lang": "en"}
    r._notify_user("done", "job.export")           # quiet on success
    r._notify_user("aborted", "job.export")        # the user did that
    r._notify_user("token_expired", "job.export")
    r._notify_user("error", "job.export")
    r._context["notify"] = "all"
    r._notify_user("done", "job.export")
    r._context["notify"] = "off"
    r._notify_user("error", "job.export")          # off silences everything
    assert len(sent) == 3
    assert all("{label}" not in b and "srv." not in b for b in sent)
    assert any("expired" in b for b in sent)


def test_jobrunner_bricht_bei_fehler_ab(sandbox):
    r = app_mod.JobRunner()
    r.start([_py_step("raise SystemExit(3)", "Kaputt"), _py_step("print('nie')", "B")], "Lauf")
    _warte(r)
    text = "\n".join(str(ln["text"]) for ln in r.lines)
    assert "nie" not in text                              # second step did not run
    assert not r.last["ok"]
    assert schluessel(r.last["detail"]) == "srv.job.exitcode"
    assert werte(r.last["detail"])["code"] == 3


def test_jobrunner_erkennt_abgelaufenen_token(sandbox):
    """Via the structured event – no longer via the message text."""
    wurzel = str(Path(app_mod.__file__).resolve().parent)
    r = app_mod.JobRunner()
    r.start([_py_step(f"import sys; sys.path.insert(0, {wurzel!r}); import progress; "
                      f"progress.fehler('token_expired'); "
                      f"print('Abgebrochen: Token abgelaufen.'); raise SystemExit(1)")],
            "Lauf")
    _warte(r)
    assert r.token_expired is True
    texte = "\n".join(str(ln["text"]) for ln in r.lines)
    assert "@@ERROR@@" not in texte, "das Ereignis selbst ist kein Protokoll"


def test_jobrunner_prosa_allein_setzt_kein_token_flag(sandbox):
    """A script that merely prints the sentence reports nothing – the regex is gone."""
    r = app_mod.JobRunner()
    r.start([_py_step("print('Abgebrochen: Token abgelaufen.'); raise SystemExit(1)")], "Lauf")
    _warte(r)
    assert r.token_expired is False


def test_jobrunner_nimmt_nur_einen_lauf_gleichzeitig(sandbox):
    r = app_mod.JobRunner()
    assert r.start([_py_step("import time; time.sleep(2)")], "Erster")
    # Wait for the run to be under way: under load the subprocess spawn can
    # fail (EAGAIN), the thread ends at once, and the refusal below would
    # then read as a broken lock instead of a machine out of processes.
    ende = time.time() + 5
    while not r.busy and time.time() < ende:
        time.sleep(0.02)
    assert r.busy, "der erste Lauf kam nicht in Gang"
    assert r.start([_py_step("print('x')")], "Zweiter") is False
    r.cancel()
    _warte(r)


def test_jobrunner_abbruch(sandbox):
    r = app_mod.JobRunner()
    r.start([_py_step("import time; time.sleep(30)")], "Lang")
    time.sleep(0.4)
    assert r.cancel() is True
    _warte(r)
    assert not r.last["ok"]


def test_jobrunner_leere_schrittliste(sandbox):
    assert app_mod.JobRunner().start([], "Nichts") is False


def test_jobrunner_meldet_nicht_startbaren_befehl(sandbox):
    r = app_mod.JobRunner()
    r.start([{"key": "x", "label": "Weg", "argv": ["/gibt/es/nicht"], "env": {}}], "Lauf")
    _warte(r)
    assert not r.last["ok"]
    assert any(schluessel(ln["text"]) == "srv.job.spawnfail" for ln in r.lines)


def test_jobrunner_nimmt_fortschritt_auf_und_haelt_ihn_aus_dem_protokoll(sandbox):
    """The numbers drive the bar; in the log they would be mere noise."""
    r = app_mod.JobRunner()
    # progress lives in the project folder, not in the sandbox
    wurzel = str(Path(app_mod.__file__).resolve().parent)
    skript = (f"import sys, time; sys.path.insert(0, {wurzel!r}); import progress; "
              "[(progress.melde(i, 3, 'chats'), time.sleep(0.05)) for i in range(4)]; "
              "print('fertig')")
    r.start([_py_step(skript)], "Lauf")
    gesehen = []
    while r.busy:
        p = (r.job or {}).get("progress")
        if p and p not in gesehen:
            gesehen.append(p)
        time.sleep(0.02)
    assert {"done": 0, "total": 3, "what": "chats"} in gesehen
    assert gesehen[-1]["done"] == 3
    texte = [ln["text"] for ln in r.lines if isinstance(ln["text"], str)]
    assert "fertig" in texte
    assert not [x for x in texte if "PROGRESS" in x], "Fortschritt landete im Protokoll"


def test_jobrunner_setzt_den_fortschritt_je_schritt_zurueck(sandbox):
    """Otherwise the second step briefly showed the first one's progress."""
    r = app_mod.JobRunner()
    staende = []
    echtes = app_mod.JobRunner._exec

    def merke(self, step):
        staende.append((step["label"], (self.job or {}).get("progress")))
        return echtes(self, step)

    app_mod.JobRunner._exec = merke
    try:
        r.start([_py_step("print(1)", "job.step.outlook"),
                 _py_step("print(2)", "job.step.teams")], "Lauf")
        _warte(r)
    finally:
        app_mod.JobRunner._exec = echtes
    assert [p for _, p in staende] == [None, None]


# --------------------------------------------------------------------------
# Nothing new exported -> index and calendar are skipped
#
# Reported from the field: a run with only "Contacts" reported "Newly
# exported: 0" and then spent two minutes indexing the same unchanged corpus.
# --------------------------------------------------------------------------
def _melde_step(neu, label="job.step.outlook"):
    wurzel = str(Path(app_mod.__file__).resolve().parent)
    schritt = _py_step(f"import sys; sys.path.insert(0, {wurzel!r}); import progress; "
                       f"print('Fertig.'); progress.ergebnis({neu})", label)
    # As in build_steps: only export steps count for the skip logic.
    schritt["corpus"] = True
    return schritt


def _folge(sandbox, neu, ziel_da=True, steps_extra=None):
    """Export step with `neu` new items, then a marked follow-up step."""
    ziel = sandbox / "corpus.db"
    if ziel_da:
        ziel.write_text("x", encoding="utf-8")
    folge = _py_step("print('INDIZIERT')", "job.step.index")
    folge.update(nur_bei_neuem=True, ziel=ziel)
    r = app_mod.JobRunner()
    r.start([_melde_step(neu)] + (steps_extra or []) + [folge], "job.export")
    _warte(r)
    return r, "\n".join(str(ln["text"]) for ln in r.lines)


def test_jobrunner_ueberspringt_index_wenn_nichts_neu_ist(sandbox):
    r, text = _folge(sandbox, neu=0)
    assert "INDIZIERT" not in text, "der Index lief trotz unverändertem Bestand"
    assert r.last["ok"]
    assert "srv.job.skipped" in text          # and says why, too
    assert "@@RESULT@@" not in text           # the notice itself is not a log line
    # The step name is a nested message – only then does the UI translate
    # it; as a bare string, "job.step.index" would end up in the log.
    eintraege = [ln["text"] for ln in r.lines if isinstance(ln["text"], dict)]
    uebersprungen = [e for e in eintraege if e["k"] == "srv.job.skipped"][0]
    assert uebersprungen["v"]["step"] == {"k": "job.step.index", "v": {}}


def test_jobrunner_indiziert_wenn_es_etwas_neues_gibt(sandbox):
    r, text = _folge(sandbox, neu=1)
    assert "INDIZIERT" in text
    # The result line comes from the app, out of the structured event.
    eintraege = [ln["text"] for ln in r.lines if isinstance(ln["text"], dict)]
    ergebnis = [e for e in eintraege if e["k"] == "srv.job.result"]
    assert ergebnis and ergebnis[0]["v"]["ergebnis"]["new"] == 1


@pytest.mark.parametrize("extra", ["updated", "moved", "gone", "gone_new"])
def test_a_rewritten_or_tombstoned_item_counts_as_a_change(sandbox, extra):
    """Outlook said "new: 0 · updated 1" for a moved appointment, and the
    calendar and the index were skipped – the change never reached them."""
    ziel = sandbox / "corpus.db"
    ziel.write_text("x", encoding="utf-8")
    folge = _py_step("print('INDIZIERT')", "job.step.index")
    folge.update(nur_bei_neuem=True, ziel=ziel)
    melde = _melde_step(f"0, extra={{{extra!r}: 1}}")
    r = app_mod.JobRunner()
    r.start([melde, folge], "job.export")
    _warte(r)
    assert "INDIZIERT" in "\n".join(str(ln["text"]) for ln in r.lines)


def test_jobrunner_indiziert_ohne_vorhandenen_index(sandbox):
    """Otherwise a first run with an unchanged corpus would never get one."""
    _, text = _folge(sandbox, neu=0, ziel_da=False)
    assert "INDIZIERT" in text


def test_jobrunner_zaehlt_ueber_alle_exportschritte(sandbox):
    """Teams brings something, Outlook does not – then indexing must happen."""
    _, text = _folge(sandbox, neu=0,
                     steps_extra=[_melde_step(3, "job.step.teams")])
    assert "INDIZIERT" in text


def test_jobrunner_indiziert_wenn_der_export_nichts_meldet(sandbox):
    """Not knowing is no reason to save – say, with an older script."""
    ziel = sandbox / "corpus.db"
    ziel.write_text("x", encoding="utf-8")
    folge = _py_step("print('INDIZIERT')", "job.step.index")
    folge.update(nur_bei_neuem=True, ziel=ziel)
    r = app_mod.JobRunner()
    r.start([_py_step("print('Fertig.')"), folge], "job.export")
    _warte(r)
    assert "INDIZIERT" in "\n".join(str(ln["text"]) for ln in r.lines)


def test_jobrunner_schreibt_die_lauf_historie(sandbox, tmp_path):
    import run_history
    h = run_history.RunHistory(tmp_path / "runs.db")
    ziel = sandbox / "corpus.db"
    ziel.write_text("x", encoding="utf-8")
    folge = _py_step("print('x')", "job.step.index")
    folge.update(nur_bei_neuem=True, ziel=ziel)
    r = app_mod.JobRunner(h)
    r.start([_melde_step(0), folge], "job.export", origin="schedule",
            context={"elements": {"onedrive": True}, "semantic": False,
                     "workers": 2})
    _warte(r)

    runs = h.list_runs()
    assert len(runs) == 1
    lauf = runs[0]
    assert lauf["job_type"] == "job.export" and lauf["origin"] == "schedule"
    assert lauf["result"] == "done" and lauf["workers"] == 2
    assert lauf["semantic"] == 0 and lauf["elements"] == {"onedrive": True}
    export, index = lauf["steps"]
    assert export["label"] == "job.step.outlook"
    assert export["new"] == 0 and export["ok"] == 1
    assert export["duration_s"] is not None
    assert index["label"] == "job.step.index" and index["skipped"] == 1


def test_jobrunner_historie_nennt_den_fehlschlag(sandbox, tmp_path):
    import run_history
    h = run_history.RunHistory(tmp_path / "runs.db")
    r = app_mod.JobRunner(h)
    r.start([_py_step("import sys; sys.exit(3)", "job.step.outlook")],
            "job.export")
    _warte(r)
    lauf = h.list_runs()[0]
    assert lauf["result"] == "error"
    assert lauf["steps"][0]["ok"] == 0


def test_jobrunner_ohne_historie_laeuft_wie_immer(sandbox):
    """Tests and edge cases: no runs.db, no difference in behavior."""
    r = app_mod.JobRunner()
    r.start([_py_step("print('ok')")], "job.export")
    _warte(r)
    assert r.last["ok"]


def test_jobrunner_indiziert_wenn_gar_kein_export_lief(sandbox):
    """The "Index only" button must always index."""
    ziel = sandbox / "corpus.db"
    ziel.write_text("x", encoding="utf-8")
    folge = _py_step("print('INDIZIERT')", "job.step.index")
    folge.update(nur_bei_neuem=True, ziel=ziel)
    r = app_mod.JobRunner()
    r.start([folge], "job.index")
    _warte(r)
    assert "INDIZIERT" in "\n".join(str(ln["text"]) for ln in r.lines)


def test_build_steps_markiert_index_und_kalender(sandbox):
    """The marker plus result file must come from build_steps – without it
    the saving never kicks in."""
    cfg = _cfg_mit_kategorien()
    steps = {s["key"]: s for s in
             app_mod.build_steps(cfg, {"outlook": True, "index": True, "calendar": True})}
    assert steps["index"]["nur_bei_neuem"] and steps["index"]["ziel"].name == "corpus.db"
    assert steps["calendar"]["nur_bei_neuem"]
    assert steps["calendar"]["ziel"].name == "calendar.json"
    # The export steps themselves never carry the marker.
    assert not steps["outlook"].get("nur_bei_neuem")


def test_jobrunner_log_since(sandbox):
    r = app_mod.JobRunner()
    r.log("eins")
    r.log("zwei")
    lines, seq = r.log_since(0)
    assert [ln["text"] for ln in lines] == ["eins", "zwei"] and seq == 2
    lines, seq = r.log_since(1)
    assert [ln["text"] for ln in lines] == ["zwei"]


def test_jobrunner_ringpuffer_begrenzt(sandbox, monkeypatch):
    monkeypatch.setattr(app_mod.JobRunner, "MAX_LINES", 5)
    r = app_mod.JobRunner()
    for i in range(20):
        r.log(str(i))
    assert len(r.lines) == 5 and r.seq == 20             # numbering keeps counting


# --------------------------------------------------------------------------
# App: starting a run
# --------------------------------------------------------------------------
def test_launch_ohne_token_wird_abgelehnt(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    ok, why = a.launch({"outlook": True})
    assert not ok and schluessel(why) == "srv.notoken"


def test_launch_ohne_auswahl(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    ok, why = a.launch({})
    assert not ok and schluessel(why) == "srv.nothing"


def test_launch_waehlt_ohne_ollama_den_volltextindex(sandbox, no_ollama, monkeypatch):
    """Exactly the requested case: no Ollama -> work happens anyway, just
    without embeddings, and the reason is in the log."""
    gesehen = {}
    monkeypatch.setattr(app_mod.JobRunner, "start",
                        lambda self, steps, label, **kw: gesehen.update(steps=steps) or True)
    a = app_mod.App(app_mod.load_config())
    ok, _ = a.launch({"index": True}, label="Index")
    assert ok
    assert "--no-embeddings" in gesehen["steps"][0]["argv"]
    assert any(schluessel(ln["text"]) == "srv.lexical.noollama" for ln in a.jobs.lines)


def test_launch_ohne_embeddings_auf_wunsch_nennt_den_richtigen_grund(
        sandbox, with_ollama, monkeypatch):
    """Ollama is running – the full-text index is then a decision, not a lack."""
    monkeypatch.setattr(app_mod.JobRunner, "start", lambda self, steps, label, **kw: True)
    a = app_mod.App(app_mod.load_config())
    assert a.launch({"index": True}, embeddings=False)[0]
    assert any(schluessel(ln["text"]) == "srv.lexical.choice" for ln in a.jobs.lines)


def test_launch_mit_ollama_baut_embeddings(sandbox, with_ollama, monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod.JobRunner, "start",
                        lambda self, steps, label, **kw: gesehen.update(steps=steps) or True)
    a = app_mod.App(app_mod.load_config())
    assert a.launch({"index": True})[0]
    assert "--no-embeddings" not in gesehen["steps"][0]["argv"]


def test_launch_lehnt_zweiten_lauf_ab(sandbox, with_ollama, monkeypatch):
    monkeypatch.setattr(app_mod.JobRunner, "busy", property(lambda self: True))
    a = app_mod.App(app_mod.load_config())
    ok, why = a.launch({"index": True})
    assert not ok and schluessel(why) == "srv.busy"


def test_ollama_ergebnis_wird_kurz_zwischengespeichert(sandbox, monkeypatch):
    """The status is polled every second – a network call per poll would be nonsense."""
    aufrufe = []
    monkeypatch.setattr(app_mod, "check_ollama", lambda url, model, timeout=1.5:
                        aufrufe.append(1) or {"running": True, "has_model": True,
                                              "models": [], "error": None,
                                              "model": model, "url": url})
    a = app_mod.App(app_mod.load_config())
    a.ollama()
    a.ollama()
    a.ollama()
    assert len(aufrufe) == 1
    a.ollama(force=True)
    assert len(aufrufe) == 2


# --------------------------------------------------------------------------
# Update check
# --------------------------------------------------------------------------
def test_update_check_meldet_nur_neuere_versionen(sandbox, with_ollama, monkeypatch):
    monkeypatch.setattr(app_mod.updates, "check", lambda *a, **k: {
        "status": "ok", "current": "1.0.0", "latest": "1.4.0",
        "url": "https://example.invalid/v1.4.0", "newer": True, "error": None})
    a = app_mod.App(app_mod.load_config())
    a.check_updates(blockierend=True)
    zeile = a.jobs.lines[-1]
    assert schluessel(zeile["text"]) == "srv.update.available"
    assert werte(zeile["text"])["version"] == "1.4.0"
    assert a.status()["update"]["newer"] is True


@pytest.mark.parametrize("zustand", [
    {"status": "none", "newer": False},           # no release yet
    {"status": "error", "newer": False, "error": "kein Netz"},
    {"status": "off", "newer": False},
    {"status": "ok", "newer": False, "latest": "1.0.0"},
])
def test_update_check_schweigt_sonst(sandbox, with_ollama, monkeypatch, zustand):
    """No release, no network, or already up to date are normal states – no
    reason to bother anyone in the log."""
    voll = {"current": "1.0.0", "latest": None, "url": None, "error": None, **zustand}
    monkeypatch.setattr(app_mod.updates, "check", lambda *a, **k: voll)
    a = app_mod.App(app_mod.load_config())
    a.check_updates(blockierend=True)
    assert list(a.jobs.lines) == []          # deque
    assert a.status()["update"]["status"] == zustand["status"]


def test_update_check_reicht_die_einstellung_durch(sandbox, with_ollama, monkeypatch):
    """Off means off – updates.check must not even go out; it checks that
    itself against this switch."""
    gesehen = {}
    monkeypatch.setattr(app_mod.updates, "check",
                        lambda current, repo, enabled=True, cache=None: gesehen.update(
                            current=current, repo=repo, enabled=enabled) or
                        {"status": "off", "current": current, "latest": None,
                         "url": None, "newer": False, "error": None})
    cfg = app_mod.load_config()
    cfg["update_check"] = False
    app_mod.App(cfg).check_updates(blockierend=True)
    assert gesehen["enabled"] is False
    assert gesehen["current"] == app_mod.version.VERSION
    assert gesehen["repo"] == app_mod.version.REPO


def test_update_check_keeps_the_etag_in_the_app_folder(sandbox, with_ollama, monkeypatch):
    """What updates.check notes in its cache survives a restart: it lies
    in the app folder, beside the profiles, and goes back on the next look."""
    gesehen = []

    def fake(current, repo, enabled=True, cache=None):
        gesehen.append(dict(cache))
        if not cache:
            cache.update(etag='W/"abc"', tag="v9.9.9", url="https://example.invalid/v9.9.9")
        return {"status": "ok", "current": current, "latest": "9.9.9",
                "url": "https://example.invalid/v9.9.9", "newer": True,
                "ahead": False, "error": None, "retry_at": None}

    monkeypatch.setattr(app_mod.updates, "check", fake)
    app_mod.App(app_mod.load_config()).check_updates(blockierend=True)
    ort = app_mod.WURZEL / app_mod.UPDATE_CACHE
    gemerkt = {"etag": 'W/"abc"', "tag": "v9.9.9", "url": "https://example.invalid/v9.9.9"}
    assert json.loads(ort.read_text(encoding="utf-8")) == gemerkt
    stand = ort.stat().st_mtime_ns
    app_mod.App(app_mod.load_config()).check_updates(blockierend=True)
    assert gesehen == [{}, gemerkt]
    assert ort.stat().st_mtime_ns == stand           # unchanged: not written again


def test_update_check_carries_when_github_answers_again(server, monkeypatch):
    """A refusal by GitHub's hourly limit travels with the time it opens
    again – on the check's answer and in the status alike."""
    monkeypatch.setattr(app_mod.updates, "check", lambda *a, **k: {
        "status": "error", "current": "1.0.0", "latest": None, "url": None,
        "newer": False, "ahead": False, "error": "HTTP 403",
        "retry_at": "2026-09-22T10:53:29"})
    code, r = call(server[1], "POST", "/api/v1/updates/check")
    assert code == 200 and r["update"]["error"] == "HTTP 403"
    assert r["update"]["retry_at"] == "2026-09-22T10:53:29"
    code, r = call(server[1], "GET", "/api/v1/status")
    assert code == 200 and r["update"]["retry_at"] == "2026-09-22T10:53:29"


def test_status_kennt_die_version_vor_der_pruefung(sandbox, with_ollama):
    """The first status poll arrives before the background check finishes."""
    a = app_mod.App(app_mod.load_config())
    assert a.status()["update"]["newer"] is False
    # Which version runs is no state: it stands with the rest of what does
    # not change while the app runs.
    u = a.umgebung()
    assert u["version"] == app_mod.version.VERSION
    assert u["releases_url"].startswith("https://github.com/")
    assert u["build"] == app_mod.version.build() and u["build"]


def test_update_check_laeuft_im_hintergrund(sandbox, with_ollama, monkeypatch):
    """Startup must not wait for a network response."""
    los = threading.Event()
    def langsam(*a, **k):
        los.wait(5)
        return {"status": "none", "current": "1.0.0", "latest": None,
                "url": None, "newer": False, "error": None}

    monkeypatch.setattr(app_mod.updates, "check", langsam)
    a = app_mod.App(app_mod.load_config())
    a.check_updates()                       # returns immediately
    assert a.status()["update"]["status"] == "off"
    los.set()


def test_http_update_check(server, monkeypatch):
    monkeypatch.setattr(app_mod.updates, "check", lambda *a, **k: {
        "status": "ok", "current": "1.0.0", "latest": "2.0.0",
        "url": "u", "newer": True, "error": None})
    code, r = call(server[1], "POST", "/api/v1/updates/check")
    assert code == 200 and r["update"]["newer"] is True and r["update"]["latest"] == "2.0.0"


def test_http_config_keeps_every_chapter_of_the_tour_seen(server, sandbox):
    """Every chapter the help window lists can be marked seen. The server's
    list once stopped at the first three, so Cases, Insights and Claude came
    back as unseen forever – the browser tests found it."""
    chapters = list(settings.TOUR_CHAPTERS)
    assert len(chapters) == 6
    code, r = call(server[1], "PATCH", "/api/v1/config",
                   {"tour_seen": dict.fromkeys(chapters, True)})
    assert code == 200 and r["config"]["tour_seen"] == dict.fromkeys(chapters, True)
    code, r = call(server[1], "PATCH", "/api/v1/config",
                   {"tour_seen": {"insights": True, "nonsense": True}})
    assert code == 200 and r["config"]["tour_seen"] == {"insights": True}


def test_the_page_and_the_server_name_the_same_chapters():
    seite = app_mod.seite()
    reihe = re.search(r"var TOUR_REIHE = \[(.*?)\];", seite).group(1)
    assert [x.strip("' ") for x in reihe.split(",")] == list(settings.TOUR_CHAPTERS)


def test_http_config_schaltet_die_pruefung_ab(server, sandbox):
    code, r = call(server[1], "PATCH", "/api/v1/config", {"update_check": False})
    assert code == 200 and r["config"]["update_check"] is False
    assert app_mod.load_config()["update_check"] is False


# --------------------------------------------------------------------------
# Status and wizard control
# --------------------------------------------------------------------------
def test_status_zeigt_token_assistenten_ohne_token(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    assert a.status()["wizard"] == "token"


ALLE_RECHTE = "Mail.Read Calendars.Read Contacts.Read Chat.Read"


def test_status_laesst_gueltigen_token_in_ruhe(sandbox, with_ollama):
    """A still-valid token must not ask for a new one at startup – its
    lifetime depends on the tenant and easily spans a working day."""
    app_mod.write_token(make_jwt(exp=time.time() + 12 * 3600, scp=ALLE_RECHTE))
    a = app_mod.App(app_mod.load_config())
    assert a.status()["wizard"] is None


def test_status_zeigt_assistenten_bei_abgelaufenem_token(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() - 60, scp=ALLE_RECHTE))
    assert app_mod.App(app_mod.load_config()).status()["wizard"] == "token"


def test_status_fragt_bei_fehlenden_rechten_nicht_von_selbst(sandbox, with_ollama):
    """Missing rights are reported by tile and log; the wizard should not pop
    up unasked because of them – the token itself is valid, after all."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600, scp="Chat.Read"))
    a = app_mod.App(_cfg_mit_kategorien())
    s = a.status()
    assert s["wizard"] is None
    assert s["token"]["missing"]


def test_status_zeigt_ollama_assistenten_wenn_token_passt(sandbox, no_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600, scp=ALLE_RECHTE))
    a = app_mod.App(app_mod.load_config())
    assert a.status()["wizard"] == "ollama"


def test_status_zeigt_token_assistenten_nach_abgelaufenem_lauf(sandbox, with_ollama):
    """A run that failed on the token brings the wizard back – even when exp
    formally still lies in the future (a revoked token)."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600, scp=ALLE_RECHTE))
    a = app_mod.App(app_mod.load_config())
    assert a.status()["wizard"] is None
    a.jobs.token_expired = True
    assert a.status()["wizard"] == "token"


# --------------------------------------------------------------------------
# Feedback at startup (the wizard now stays quiet in the normal case)
# --------------------------------------------------------------------------
def test_log_token_state_bei_gueltigem_token(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 12 * 3600 + 60,
                                 scp=ALLE_RECHTE, upn="chef@example.com"))
    a = app_mod.App(app_mod.load_config())
    a.log_token_state()
    zeile = a.jobs.lines[-1]
    assert zeile["level"] == "ok"
    assert schluessel(zeile["text"]) == "srv.token.found"
    assert werte(zeile["text"])["account"] == "chef@example.com"
    assert werte(zeile["text"])["minutes"] == 12 * 60      # formatting is the UI's job


def test_log_token_state_ohne_token(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    a.log_token_state()
    assert a.jobs.lines[-1]["level"] == "warn"
    assert schluessel(a.jobs.lines[-1]["text"]) == "srv.token.none"


def test_log_token_state_bei_abgelaufenem_token(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() - 60))
    a = app_mod.App(app_mod.load_config())
    a.log_token_state()
    assert a.jobs.lines[-1]["level"] == "warn"
    assert schluessel(a.jobs.lines[-1]["text"]) == "srv.token.expired"


def test_log_token_state_nennt_fehlende_rechte(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600, scp="Chat.Read"))
    a = app_mod.App(_cfg_mit_kategorien())
    a.log_token_state()
    assert a.jobs.lines[-1]["level"] == "warn"
    assert schluessel(a.jobs.lines[-1]["text"]) == "srv.token.scopes"
    assert "Mail.Read" in werte(a.jobs.lines[-1]["text"])["list"]


def test_token_status_liefert_die_restminuten():
    """Formatting happens in the UI – only there the language is known."""
    now = 1_000_000
    st = app_mod.token_status(make_jwt(exp=now + 620 * 60), now=now)
    assert st["expires_in_minutes"] == 620
    assert "expires_text" not in st


def test_status_nennt_die_noetigen_berechtigungen(sandbox, with_ollama):
    cfg = app_mod.load_config()
    cfg["outlook_categories"] = ["mail"]
    cfg["teams_categories"] = ["channels"]
    u = app_mod.App(cfg).umgebung()
    assert u["scopes_needed"] == ["ChannelMessage.Read.All", "Mail.Read", "User.Read"]


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------
def test_scheduler_startet_lauf_wenn_faellig(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    a.cfg["schedule"].update(enabled=True, interval_minutes=5,
                             outlook=True, teams=False, index=True)
    gestartet = {}
    a.launch = (lambda anfrage, **kw:
                gestartet.update(anfrage, **kw) or (True, "gestartet"))
    a.scheduler._tick()
    assert gestartet["outlook"] is True and gestartet["teams"] is False
    assert gestartet["index"] is True and gestartet["label"] == "job.scheduled"


def test_scope_pruefung_kennt_die_schreibvarianten_der_spiegel():
    """The reported case: runs worked, the check still warned – the key
    carried the ReadWrite variants, not the exact names."""
    fehlt = app_mod.scope_missing(
        ["Files.Read.All", "Sites.Read.All"],
        ["Files.ReadWrite.All", "Sites.ReadWrite.All"])
    assert fehlt == []
    assert app_mod.scope_missing(["Sites.Read.All"], ["Files.Read.All"]) == [
        "Sites.Read.All"]


def test_cadence_faellig_rechnet_mit_periode_und_slack():
    jetzt = 1_000_000.0
    assert app_mod.cadence_faellig("always", jetzt - 1, jetzt)
    assert app_mod.cadence_faellig("daily", None, jetzt)
    assert app_mod.cadence_faellig("daily", jetzt - 86400, jetzt)
    assert app_mod.cadence_faellig("daily", jetzt - 86400 + 30, jetzt)  # Slack
    assert not app_mod.cadence_faellig("daily", jetzt - 3600, jetzt)
    assert not app_mod.cadence_faellig("monthly", jetzt - 86400, jetzt)


def test_lauf_protokoll_landet_in_der_runs_db(sandbox):
    """Every line of a run sits in the log table with the run's id – written
    in batches, the rest at the end of the run; app lines outside a run go
    immediately and without a run id."""
    import run_history as rh
    hist = rh.RunHistory(sandbox / "runs.db")
    jobs = app_mod.JobRunner(hist)
    jobs.logk("srv.mcp.started", "ok", port=1)         # outside of a run
    jobs._run([], "job.export")
    runs = hist.list_runs()
    assert runs and runs[0]["result"] == "done"
    schluessel = [z["text"].get("k") for z in hist.run_log(runs[0]["id"])]
    assert schluessel[0] == "srv.job.start"
    assert "srv.job.done" in schluessel
    assert "srv.mcp.started" not in schluessel, "App-Zeile klebt am Lauf"


def test_api_run_log_liefert_das_gespeicherte_protokoll(server):
    a, port = server
    lauf = a.history.start_run("job.export", "manual")
    a.history.log_lines([(lauf, 1.0, "info", '"zeile"')])
    code, r = call(port, "GET", f"/api/v1/runs/{lauf}/log")
    assert code == 200 and r["items"][0]["text"] == "zeile"
    # An id that is not a number names no run at all – nor does one that
    # was never recorded; a run whose lines were pruned answers [].
    for weg in ("abc", "999999"):
        code, r = call(port, "GET", f"/api/v1/runs/{weg}/log")
        assert code == 404 and r["error"]["k"] == "srv.run.unknown", weg
    leer = a.history.start_run("job.export", "manual")
    assert call(port, "GET", f"/api/v1/runs/{leer}/log") == (200, {"items": []})


def test_planner_board_anhaenge_gehen_durch_die_source_route(server, sandbox):
    """Regression: the board's relative attachment link, viewed via /source,
    resolved against the app root ("Unknown path"). On serving, it is
    rewritten onto the route itself – the file on disk stays
    offline-friendly and relative."""
    a, port = server
    ordner = sandbox / app_mod.PLANNER_DIR / "Team_X__abc123"
    (ordner / "Anhaenge").mkdir(parents=True)
    (ordner / "Anhaenge" / "Angebot 1__k.pdf").write_bytes(b"PDF")
    (ordner / "board.html").write_text(
        '<html><a href="Anhaenge/Angebot 1__k.pdf">Angebot</a></html>',
        encoding="utf-8")
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams")])

    code, roh = call_roh(port, "/api/v1/files/content?root=planner&path=Team_X__abc123/board.html")
    assert code == 200
    assert b'href="/api/v1/files/content?root=planner&path=Team_X__abc123%2FAnhaenge%2F' in roh
    code, roh = call_roh(
        port, "/api/v1/files/content?root=planner&path=Team_X__abc123%2FAnhaenge%2F"
              "Angebot%201__k.pdf")
    assert code == 200 and roh == b"PDF"


def test_kadenz_reist_in_den_export(sandbox):
    """Since 9.0 every cadence gate lives inside the export it paces – per
    category, URL or notebook – so the app hands the table over instead of
    dropping a step itself. An old whole-source "teams" entry stands in
    for the four category keys until each has its own."""
    cfg = app_mod.load_config()
    cfg["outlook_categories"] = ["mail"]
    cfg["teams_categories"] = ["1on1", "channels"]
    cfg["onedrive_enabled"] = True
    cfg["sync_cadence"] = {"onedrive": "weekly", "teams": "daily",
                           "teams:channels": "monthly", "outlook:mail": "daily"}
    steps = {s["key"]: s for s in app_mod.build_steps(
        cfg, {"outlook": True, "onedrive": True, "teams": True})}
    assert not any("auslassen" in s for s in steps.values())
    for key in ("outlook", "onedrive", "teams"):
        kad = json.loads(steps[key]["env"]["SYNC_CADENCE"])
        assert kad["onedrive"] == "weekly" and kad["outlook:mail"] == "daily"
        assert kad["teams:1on1"] == "daily" and kad["teams:channels"] == "monthly"
        assert "teams" not in kad
        assert "SYNC_NOW" not in steps[key]["env"]
    # "Sync now" for a whole source: the cadences step aside once.
    jetzt = app_mod.build_steps(cfg, {"onedrive": True}, sync_now=True)
    assert jetzt[0]["env"]["SYNC_NOW"] == "1"


def test_jobrunner_protokoll_liest_sich_wie_der_lauf(sandbox):
    """Heading, selection, then every step under its own heading – also the
    ones that do not run. Reported from the field: the cadence line stood
    BEFORE the run heading, and the skipped index had no heading at all."""
    ziel = sandbox / "corpus.db"
    ziel.write_text("x", encoding="utf-8")
    gated = _py_step("print('DARF NICHT LAUFEN')", "job.step.onedrive")
    # A step the app decided not to run, with its reason attached – the
    # runner prints the reason under the step's own heading.
    gated.update(corpus=True, auslassen={
        "k": "srv.job.skipped",
        "v": {"step": {"k": "job.step.onedrive", "v": {}}}})
    index = _py_step("print('INDIZIERT')", "job.step.index")
    index.update(nur_bei_neuem=True, ziel=ziel)
    r = app_mod.JobRunner()
    r.start([gated, index], "job.export",
            context={"nichts_neues": True,
                     "elements": {"outlook": [], "teams": [],
                                  "onedrive": True}})
    _warte(r)
    folge = [z["text"]["k"] for z in r.lines if isinstance(z["text"], dict)]
    assert folge == ["srv.job.start", "srv.job.elements",
                     "srv.job.step", "srv.job.skipped",
                     "srv.job.step", "srv.job.skipped",
                     "srv.job.done"], folge
    text = "\n".join(str(z["text"]) for z in r.lines)
    assert "DARF NICHT LAUFEN" not in text and "INDIZIERT" not in text


def test_alter_state_sperrt_den_export(sandbox, with_ollama):
    """7.0 no longer migrates the pre-6.2 state – so it must not export onto
    it either: an empty state.db looks like a first run, which would fetch
    the whole mailbox again and orphan the write-once tombstones."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    ordner = sandbox / app_mod.OUTLOOK_DIR
    ordner.mkdir(parents=True, exist_ok=True)
    (ordner / "exported.tsv").write_text("m1\ta.eml\n", encoding="utf-8")
    assert app_mod.altbestand_state() == ["outlook"]
    a = app_mod.App(app_mod.load_config())
    ok, why = a.launch({"outlook": True}, label="job.export")
    assert not ok and why["k"] == "srv.legacy.state"
    # A folder that already carries its state.db is done – the 6.x migration
    # leaves the originals as .bak, so an old name next to it means nothing.
    import state_db
    state_db.StateDb(ordner).kv_schreiben("x", "1")
    assert app_mod.altbestand_state() == []


def test_alter_zeiger_sperrt_bis_die_pfade_stehen(standardort, tmp_path,
                                                  monkeypatch):
    """Up to 6.3.1 a pointer file could send the whole archive elsewhere.
    7.0 does not follow it – but running anyway would start a SECOND archive
    beside the real one and fetch everything again, so it blocks and says
    where the data is."""
    platte = tmp_path / "platte"
    (platte / app_mod.OUTLOOK_DIR).mkdir(parents=True)
    (standardort / app_mod.ZEIGER_DATEI).write_text(str(platte),
                                                    encoding="utf-8")
    monkeypatch.setattr(app_mod, "CONFIG_FILE",
                        standardort / "app_config.json")
    # Read and write the same file: MUNIMENTUM_HOME steers settings without
    # touching data_dir_env(), which the guard checks first.
    monkeypatch.setenv("MUNIMENTUM_HOME", str(standardort))
    import settings
    settings.reset()
    assert app_mod.alter_zeiger() == platte
    a = app_mod.App(app_mod.load_config())
    ok, why = a.launch({"index": True}, label="job.index")
    assert not ok and why["k"] == "srv.layout.pointer"

    # The trap: save_config writes the whole schema, so data_dir exists as
    # "" after any settings save. Asking whether the key is PRESENT would
    # disarm the guard from then on.
    app_mod.save_config(app_mod.load_config())
    settings.reset()
    assert app_mod.alter_zeiger() == platte, "Speichern hat die Sperre entschärft"

    # A folder the user really chose wins – that is the way out.
    cfg = app_mod.load_config()
    cfg["data_dir"] = str(platte)
    app_mod.save_config(cfg)
    settings.reset()
    assert app_mod.alter_zeiger() is None


def test_konfiguration_aendert_sich_nur_durch_eine_tuer(sandbox, monkeypatch):
    """Memory, file and settings.py's cache move together – and two changes
    at once lose nothing. Before the door existed, one handler could
    serialise the dict before the other's change and rename its file into
    place after it; the setting then silently reverted on the next start."""
    import json
    import settings
    import threading
    # settings.py must read the same file the app writes – the sandbox only
    # repoints app_mod.CONFIG_FILE, so steer settings' path the same way.
    monkeypatch.setenv("MUNIMENTUM_HOME", str(sandbox))
    settings.reset()
    a = app_mod.App(app_mod.load_config())
    settings.load()                                  # warm the cache
    fertig = threading.Barrier(2)

    def dreher(key, n):
        fertig.wait()
        for i in range(n):
            a.konfiguriere(lambda cfg, i=i: cfg.__setitem__(key, i))

    t1 = threading.Thread(target=dreher, args=("workers", 200))
    t2 = threading.Thread(target=dreher, args=("mcp_port", 200))
    for th in (t1, t2):
        th.start()
    for th in (t1, t2):
        th.join()
    datei = json.loads(app_mod.CONFIG_FILE.read_text(encoding="utf-8"))
    assert datei["workers"] == 199 and datei["mcp_port"] == 199
    assert a.cfg["workers"] == 199 and a.cfg["mcp_port"] == 199
    # the subprocess-side cache saw the change without a restart
    assert settings.load()["workers"] == 199


def test_lauf_sperren_kennt_jede_upgrade_lage(standardort, tmp_path,
                                              monkeypatch):
    """One answer to "may a run start here?" – every upgrade leftover the
    app recognises lands in this list, so the startup log and the run gate
    cannot disagree, and the block lifts as the user sorts things out."""
    import settings
    import state_db
    monkeypatch.setenv("MUNIMENTUM_HOME", str(standardort))
    monkeypatch.setattr(app_mod, "CONFIG_FILE",
                        standardort / "app_config.json")
    monkeypatch.setattr(app_mod, "BASE", standardort / "data")
    settings.reset()
    assert app_mod.lauf_sperren() == []                # fresh install

    ordner = standardort / "data" / app_mod.OUTLOOK_DIR   # 6.1 state files
    ordner.mkdir(parents=True)
    (ordner / "exported.tsv").write_text("m\ta.eml\n", encoding="utf-8")
    assert [s["k"] for s in app_mod.lauf_sperren()] == ["srv.legacy.state"]

    platte = tmp_path / "platte"                      # plus a 6.x pointer
    (platte / app_mod.OUTLOOK_DIR).mkdir(parents=True)
    (standardort / app_mod.ZEIGER_DATEI).write_text(str(platte),
                                                    encoding="utf-8")
    assert [s["k"] for s in app_mod.lauf_sperren()] == [
        "srv.legacy.state", "srv.layout.pointer"]

    # The user runs 6.x once (state.db appears) and chooses the folder.
    state_db.StateDb(ordner).kv_schreiben("x", "1")
    cfg = app_mod.load_config()
    cfg["data_dir"] = str(platte)
    app_mod.save_config(cfg)
    settings.reset()
    assert app_mod.lauf_sperren() == []


def test_nur_uebersprungene_exporte_lassen_den_index_aus(sandbox, with_ollama):
    """Every requested export dropped before the run (an empty category
    list) means nothing new by definition – the run must not re-read the
    whole archive for the index. An index-only run asked for no export and
    keeps running. A cadence-skipped export reports zero new items itself,
    which the runner turns into the same skip."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    a.cfg["outlook_categories"] = []
    jetzt = time.time()
    # Exported an hour ago, indexed afterwards: the index is current.
    a.history.last_step_ok = lambda key: (jetzt - 60 if key == "index"
                                          else jetzt - 3600)
    a.history.last_step_started = lambda key: (jetzt - 60 if key == "index"
                                               else jetzt - 3600)
    gestartet = {}
    a.jobs.start = lambda steps, label, **kw: gestartet.update(kw) or True
    a.launch({"outlook": True, "index": True}, label="job.export")
    assert gestartet["context"]["nichts_neues"] is True
    a.launch({"index": True}, label="job.index")
    assert gestartet["context"]["nichts_neues"] is False


def test_ausgelassener_index_holt_einen_fehlgeschlagenen_lauf_nach(sandbox,
                                                                   with_ollama):
    """"No export ran" only means "nothing to do" while the index is really
    newer than the last export. After a failed or cancelled index step the
    archive has moved on without it – then a run whose exports are all
    dropped must still catch up instead of skipping for good."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    a.cfg["outlook_categories"] = []
    jetzt = time.time()
    # Last successful index BEFORE the last export: stale.
    a.history.last_step_ok = lambda key: (jetzt - 7200 if key == "index"
                                          else jetzt - 3600)
    a.history.last_step_started = lambda key: (jetzt - 7200 if key == "index"
                                               else jetzt - 3600)
    gestartet = {}
    a.jobs.start = lambda steps, label, **kw: gestartet.update(kw) or True
    a.launch({"outlook": True, "index": True}, label="job.export")
    assert gestartet["context"]["nichts_neues"] is False

    # Never indexed at all: nothing to be current about.
    a.history.last_step_ok = lambda key: None if key == "index" else jetzt
    a.history.last_step_started = lambda key: None if key == "index" else jetzt
    a.launch({"outlook": True, "index": True}, label="job.export")
    assert gestartet["context"]["nichts_neues"] is False

    # The one an "ok only" reading would miss: an export that DIED part-way
    # still wrote what it had fetched, so the older index is stale even
    # though no successful export row is newer than it.
    a.history.last_step_ok = lambda key: jetzt - 7200      # index, and the
    a.history.last_step_started = lambda key: (jetzt - 7200 if key == "index"
                                               else jetzt - 60)
    a.launch({"outlook": True, "index": True}, label="job.export")
    assert gestartet["context"]["nichts_neues"] is False


def test_lauf_historie_kennt_jede_korpus_quelle(sandbox, with_ollama):
    """The runs table names the sources of a run from `elements`; a corpus
    step missing there is invisible in the history even though it ran."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    gestartet = {}
    a.jobs.start = lambda steps, label, **kw: gestartet.update(kw) or True
    a.launch({"planner": True}, label="job.export")
    elemente = gestartet["context"]["elements"]
    import steps as steps_mod
    for e in steps_mod.REGISTRY:
        if e.get("corpus"):
            assert e["key"] in elemente, f"{e['key']} fehlt in der Historie"
    assert elemente["planner"] is True


def test_jobrunner_ueberspringt_index_ohne_gelaufenen_export(sandbox):
    """The runner side of it: told that nothing new can exist, it skips the
    follow-up steps even though no export step reported anything."""
    ziel = sandbox / "corpus.db"
    ziel.write_text("x", encoding="utf-8")
    folge = _py_step("print('INDIZIERT')", "job.step.index")
    folge.update(nur_bei_neuem=True, ziel=ziel)
    r = app_mod.JobRunner()
    r.start([folge], "job.export", context={"nichts_neues": True})
    _warte(r)
    text = "\n".join(str(ln["text"]) for ln in r.lines)
    assert "INDIZIERT" not in text and "srv.job.skipped" in text
    r = app_mod.JobRunner()
    r.start([folge], "job.index")             # nothing known: it runs
    _warte(r)
    assert "INDIZIERT" in "\n".join(str(ln["text"]) for ln in r.lines)


def test_scheduler_spiegelt_nur_mit_master_schalter(sandbox, with_ollama):
    """The schedule toggle narrows, it does not switch a source on: without
    the Export-tab checkbox the schedule does not mirror either."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    a.cfg["schedule"].update(enabled=True, interval_minutes=5)
    gestartet = {}
    a.launch = (lambda anfrage, **kw:
                gestartet.update(anfrage, **kw) or (True, "gestartet"))
    a.scheduler._tick()
    assert gestartet["onedrive"] is False and gestartet["sharepoint"] is False

    a.cfg["onedrive_enabled"] = True
    a.cfg["sharepoint_enabled"] = True
    a.cfg["sharepoint_pages_enabled"] = True
    a.scheduler.last_run = None
    a.scheduler._tick()
    assert gestartet["onedrive"] is True and gestartet["sharepoint"] is True
    assert gestartet["sharepoint_pages"] is True

    a.cfg["schedule"].update(onedrive=False, sharepoint=False)
    a.scheduler.last_run = None
    a.scheduler._tick()
    assert gestartet["onedrive"] is False and gestartet["sharepoint"] is False
    # Pages have their own schedule toggle, independent of the libraries.
    assert gestartet["sharepoint_pages"] is True
    a.cfg["schedule"].update(sharepoint_pages=False)
    a.scheduler.last_run = None
    a.scheduler._tick()
    assert gestartet["sharepoint_pages"] is False


@pytest.mark.parametrize("cats,kalender,rekonstruktion", [
    (["mail", "calendar"], True, None),    # None = as configured
    (["contacts"], True, False),           # build yes, read mails no
    (["mail"], False, None),               # nothing to build – then it does not matter
])
def test_scheduler_stimmt_den_kalenderschritt_ab(sandbox, with_ollama, cats,
                                                 kalender, rekonstruktion):
    """The same bug sat in the schedule – unnoticed there because it runs at night."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    a.cfg["outlook_categories"] = cats
    a.cfg["schedule"].update(enabled=True, interval_minutes=5, outlook=True,
                             teams=False, index=True, calendar=True)
    gestartet = {}
    a.launch = (lambda anfrage, **kw:
                gestartet.update(anfrage, **kw) or (True, "gestartet"))
    a.scheduler._tick()
    assert gestartet["calendar"] is kalender
    assert gestartet["reconstruct"] is rekonstruktion


def test_scheduler_wartet_bis_zum_intervall(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    a.cfg["schedule"].update(enabled=True, interval_minutes=60)
    laeufe = []
    a.launch = (lambda anfrage, **kw:
                laeufe.append(dict(anfrage, **kw)) or (True, "gestartet"))
    a.scheduler._tick()
    a.scheduler._tick()                                   # right afterwards: not due
    assert len(laeufe) == 1


def test_scheduler_ueberspringt_ohne_gueltigen_token(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() - 60))    # expired
    a = app_mod.App(app_mod.load_config())
    a.cfg["schedule"]["enabled"] = True
    a.launch = lambda anfrage, **kw: pytest.fail("darf nicht starten")
    a.scheduler._tick()
    assert a.jobs.token_expired is True
    assert any(schluessel(ln["text"]) == "srv.sched.notoken" for ln in a.jobs.lines)


def test_scheduler_tut_nichts_wenn_aus(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    a.launch = lambda anfrage, **kw: pytest.fail("darf nicht starten")
    a.scheduler._tick()


def test_scheduler_tut_nichts_waehrend_ein_lauf_laeuft(sandbox, with_ollama, monkeypatch):
    a = app_mod.App(app_mod.load_config())
    a.cfg["schedule"]["enabled"] = True
    monkeypatch.setattr(app_mod.JobRunner, "busy", property(lambda self: True))
    a.launch = lambda anfrage, **kw: pytest.fail("darf nicht starten")
    a.scheduler._tick()


def test_scheduler_next_due(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    assert a.scheduler.next_due() is None                  # off
    a.cfg["schedule"].update(enabled=True, interval_minutes=30)
    assert a.scheduler.next_due() <= time.time()           # never ran before -> right away
    a.scheduler.last_run = 1000
    assert a.scheduler.next_due() == 1000 + 1800


# --------------------------------------------------------------------------
# MCP process
# --------------------------------------------------------------------------
def test_mcp_ohne_index_startet_nicht(sandbox):
    a = app_mod.App(app_mod.load_config())
    ok, why = a.mcp.start(a.cfg)
    assert not ok and schluessel(why) == "srv.mcp.noindex"
    assert a.mcp.status(a.cfg)["running"] is False


def test_mcp_status_nennt_die_url(sandbox):
    a = app_mod.App(app_mod.load_config())
    a.cfg["mcp_port"] = 8899
    assert a.mcp.status(a.cfg)["url"] == "http://127.0.0.1:8899/mcp"


def test_autostart_mcp_meldet_fehlenden_index(sandbox):
    a = app_mod.App(app_mod.load_config())
    a.autostart_mcp()
    assert any(schluessel(ln["text"]) == "srv.mcp.notstarted" for ln in a.jobs.lines)


def test_autostart_mcp_kann_abgeschaltet_werden(sandbox):
    cfg = app_mod.load_config()
    cfg["mcp_autostart"] = False
    a = app_mod.App(cfg)
    a.autostart_mcp()
    assert not a.jobs.lines


class FakePopen:
    """Stand-in for the mcp_server.py subprocess – without a real port.

    Keeps running like the original until terminate() arrives: otherwise the
    process would already be gone once the log thread has read the output.
    """

    def __init__(self, argv, **kw):
        import io
        self.argv = argv
        self.kw = kw
        self.stdout = io.BytesIO(b"munimentum MCP: 3 chunks\n")
        self._ende = threading.Event()
        self._code = None

    def poll(self):
        return self._code

    def terminate(self):
        self._code = -15
        self._ende.set()

    def kill(self):
        self._code = -9
        self._ende.set()

    def wait(self, timeout=None):
        self._ende.wait(timeout if timeout is not None else 30)
        return self._code if self._code is not None else 0


@pytest.fixture
def fake_popen(monkeypatch):
    gestartet = []
    monkeypatch.setattr(runner_mod.subprocess, "Popen",
                        lambda argv, **kw: gestartet.append(FakePopen(argv, **kw))
                        or gestartet[-1])
    return gestartet


def test_mcp_start_und_stop(sandbox, store, fake_popen):
    a = app_mod.App(app_mod.load_config())
    ok, why = a.mcp.start(a.cfg)
    assert ok and schluessel(why) == "srv.mcp.startok"
    assert a.mcp.running and a.mcp.status(a.cfg)["port"] == a.cfg["mcp_port"]

    argv = fake_popen[0].argv
    assert argv[1].endswith("mcp_server.py")
    assert "--data-dir" in argv
    assert "--port" in argv

    assert schluessel(a.mcp.start(a.cfg)[1]) == "srv.mcp.running"   # no second process
    assert len(fake_popen) == 1

    assert a.mcp.stop() is True
    assert not a.mcp.running
    assert a.mcp.stop() is False                            # already stopped


def test_mcp_leitet_ausgabe_ins_protokoll(sandbox, store, fake_popen):
    a = app_mod.App(app_mod.load_config())
    a.mcp.start(a.cfg)
    ende = time.time() + 5
    while time.time() < ende and not any("[MCP]" in ln["text"] for ln in a.jobs.lines):
        time.sleep(0.02)
    assert any("[MCP] munimentum MCP" in ln["text"] for ln in a.jobs.lines)
    a.mcp.stop()


def test_mcp_start_scheitert_am_betriebssystem(sandbox, store, monkeypatch):
    def boom(*a, **k):
        raise OSError("kein Python")
    monkeypatch.setattr(runner_mod.subprocess, "Popen", boom)
    a = app_mod.App(app_mod.load_config())
    ok, why = a.mcp.start(a.cfg)
    assert not ok and schluessel(why) == "srv.mcp.spawnfail"


def test_autostart_mcp_startet_bei_vorhandenem_index(sandbox, store, fake_popen):
    a = app_mod.App(app_mod.load_config())
    a.autostart_mcp()
    assert a.mcp.running
    a.shutdown()
    assert not a.mcp.running                                # shutdown cleans up


# --------------------------------------------------------------------------
# Search over a real small store
# --------------------------------------------------------------------------
TEAMS_HTML = """<html><body>
<h1>Projekt Alpha</h1>
<div class="msg">
  <span class="name">Alice Example</span>
  <span class="time">2025-06-01 09:30</span>
  <div class="body">Die Rechnung 4711 ist bezahlt.</div>
</div>
</body></html>"""


@pytest.fixture
def store(sandbox):
    """Small real store – written with the helpers from rag_index.py."""
    teams = sandbox / "teams_export" / "1on1"
    teams.mkdir(parents=True)
    (teams / "alice__abc.html").write_text(TEAMS_HTML, encoding="utf-8")
    recs = corpus.load_records(str(sandbox / "teams_export"), str(sandbox / "fehlt"))
    chunks = corpus.chunk_records(recs)
    assert chunks, "Testkorpus ist leer"
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    (sandbox / "rag_store").mkdir()
    rag_index.write_db(sandbox / "rag_store", chunks)
    rag_index.write_info(sandbox / "rag_store", None, 0, len(chunks))
    return sandbox / "rag_store"


def test_searchbridge_ohne_index(sandbox):
    b = app_mod.SearchBridge()
    assert b.ensure(app_mod.load_config()) is None
    assert schluessel(b.error) == "srv.noindex"


def test_searchbridge_sucht_lexikalisch(sandbox, store):
    b = app_mod.SearchBridge()
    mod = b.ensure(app_mod.load_config())
    assert mod is not None and mod.STATE["semantic"] is False
    res = mod.search_messages("Rechnung", mode="lexical")
    assert res["count"] >= 1
    assert "4711" in res["results"][0]["preview"]


def test_searchbridge_laedt_nach_neuem_index_neu(sandbox, store):
    b = app_mod.SearchBridge()
    cfg = app_mod.load_config()
    b.ensure(cfg)
    erster = b.stamp
    time.sleep(0.01)
    con = sqlite3.connect(store / "corpus.db")
    con.execute("UPDATE chunks SET text = 'anderer Inhalt'")
    con.commit()
    con.close()
    b.ensure(cfg)
    assert b.stamp != erster                              # new stamp -> reloaded


# --------------------------------------------------------------------------
# Generated answer: uses the search hits, does not search on its own
# --------------------------------------------------------------------------
def _antwort(port, body, kopf=None):
    """QUERY /api/v1/answer and collect the NDJSON lines."""
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    con.request("QUERY", "/api/v1/answer", json.dumps(body),
                {"Content-Type": "application/json", **(kopf or {})})
    r = con.getresponse()
    roh = r.read().decode("utf-8")
    con.close()
    if r.getheader("Content-Type", "").startswith("application/x-ndjson"):
        return r.status, [json.loads(z) for z in roh.splitlines() if z.strip()]
    return r.status, json.loads(roh)


def test_antwort_nutzt_die_treffer_der_suche(sandbox, with_ollama, store, monkeypatch):
    """No second retrieval: the answer sees exactly the hits that are in the
    list – otherwise it could cite the unfindable."""
    gesehen = {}
    monkeypatch.setattr(app_mod.answer, "stream",
                        lambda q, quellen, model, ollama, lang="de", **kw:
                        gesehen.update(query=q, quellen=quellen, model=model,
                                       lang=lang) or iter([{"text": "Antwort [1]."}]))
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, zeilen = _antwort(httpd.server_address[1], {"q": "Rechnung"})
        assert code == 200
        assert zeilen[0]["sources"][0]["n"] == 1          # numbering starts at 1
        assert zeilen[0]["model"] == a.cfg["chat_model"]
        assert {"text": "Antwort [1]."} in zeilen
        assert zeilen[-1] == {"done": True}
        # Full text instead of preview – 200 characters cannot answer anything
        assert "4711" in gesehen["quellen"][0]["text"]
        assert gesehen["query"] == "Rechnung"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_antwort_ohne_chat_modell(sandbox, store, monkeypatch):
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": True, "models": [], "has_model": True,
                            "has_chat_model": False, "error": None,
                            "model": model, "chat_model": chat_model, "url": url})
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, d = _antwort(httpd.server_address[1], {"q": "Rechnung"})
        assert code == 503 and schluessel(d["error"]) == "srv.answer.nomodel"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_antwort_ohne_suchbegriff_und_ohne_treffer(sandbox, with_ollama, store):
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        code, d = _antwort(port, {"q": "   "})
        assert code == 400 and schluessel(d["error"]) == "srv.answer.noquery"
        code, d = _antwort(port, {"q": "xyzzyplugh"})
        assert code == 404 and schluessel(d["error"]) == "srv.answer.nohits"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_antwort_folgt_der_spracheinstellung(sandbox, with_ollama, store, monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod.answer, "stream",
                        lambda q, quellen, model, ollama, lang="de", **kw:
                        gesehen.update(lang=lang) or iter([]))
    cfg = app_mod.load_config()
    cfg["language"] = "fr"
    a = app_mod.App(cfg)
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _antwort(httpd.server_address[1], {"q": "Rechnung"})
        assert gesehen["lang"] == "fr"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_antwort_begrenzt_die_quellenzahl(sandbox, with_ollama, store, monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod.answer, "stream",
                        lambda q, quellen, *a, **kw:
                        gesehen.update(n=len(quellen)) or iter([]))
    cfg = app_mod.load_config()
    cfg["answer_sources"] = 99                   # beyond the limit
    a = app_mod.App(cfg)
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _antwort(httpd.server_address[1], {"q": "Rechnung"})
        assert gesehen["n"] <= 20
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
@pytest.fixture
def server(sandbox, with_ollama):
    # With categories selected: the default deliberately selects nothing, but
    # a running server belongs to someone who has made a choice.
    a = app_mod.App(_cfg_mit_kategorien())
    httpd = app_mod.make_server(a, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield a, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call_roh(port, path):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", path)
    r = con.getresponse()
    raw = r.read()
    con.close()
    return r.status, raw


def test_http_liefert_die_oberflaeche(server):
    _, port = server
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", "/")
    r = con.getresponse()
    body = r.read().decode("utf-8")
    con.close()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/html")
    assert "Munimentum" in body


def test_http_status(server):
    """What is polled, and what is not: the settings have their own route
    since 12.0 – they change when someone saves them, the status every few
    seconds, and together they made every poll a third heavier."""
    _, port = server
    code, s = call(port, "GET", "/api/v1/status")
    assert code == 200
    assert set(["token", "ollama", "jobs", "mcp"]) <= set(s)
    assert "config" not in s
    # The index's state moved to the inventory with 13.0: it changes with a
    # run, and a poll is for what changes on its own.
    assert "store" not in s
    code, r = call(port, "GET", "/api/v1/inventory")
    assert code == 200 and r["store"]["exists"] is False
    code, r = call(port, "GET", "/api/v1/config")
    assert code == 200 and r["config"]["workers"] == app_mod.DEFAULT_CONFIG["workers"]


def test_http_fremder_host_wird_abgewiesen(server):
    """Guards against DNS rebinding: a name pointing at 127.0.0.1 would
    otherwise suffice for any website to query the whole mail archive."""
    _, port = server
    code, _ = call(port, "GET", "/api/v1/status", host="angreifer.example.com")
    assert code == 403
    code, _ = call(port, "POST", "/api/v1/runs", {"index": True}, host="angreifer.example.com")
    assert code == 403


def test_http_unbekannter_pfad(server):
    _, port = server
    assert call(port, "GET", "/api/gibtsnicht")[0] == 404
    assert call(port, "POST", "/api/gibtsnicht", {})[0] == 404


def test_jede_ablehnung_traegt_dieselbe_huelle(server, monkeypatch, capsys):
    """One shape for every refusal (11.4), whatever the route and the
    status: ok false, `error` with the key, its placeholders and the
    sentence in the request's language, `message` as 11.3 sent it – a
    script reads one field, the page one more."""
    a, port = server
    monkeypatch.setitem(a.cfg, "language", "de")   # the page's language, not the error's
    code, r = call(port, "GET", "/api/gibtsnicht")
    assert code == 404 and r["ok"] is False
    assert r["error"] == {"k": "srv.notfound", "v": {"path": "/api/gibtsnicht"}}
    assert r["type"] == "urn:munimentum:error:srv.notfound"
    assert r["title"] == "Not Found" and r["status"] == 404
    assert r["detail"] == "No such route: /api/gibtsnicht"
    assert r["instance"] == "/api/gibtsnicht"
    assert "message" not in r          # das zweite Wort für dasselbe, seit 13.0 weg
    code, r = call(port, "GET", "/api/v1/status", host="angreifer.example.com")
    assert code == 403 and r["error"]["k"] == "srv.forbidden" and "127.0.0.1" in r["detail"]
    code, r = call(port, "PUT", "/api/v1/access/token", {"token": ""})
    assert code == 400 and r["error"]["k"] == "srv.token.empty" and r["error"]["k"] == "srv.token.empty"
    code, r = call(port, "QUERY", "/api/v1/sources/outlook/folder-plan", {})
    assert code == 404 and r["error"]["k"] == "srv.plan.nolist" and r["leer"] is True
    monkeypatch.setattr(a, "status", lambda: 1 / 0)
    capsys.readouterr()
    code, r = call(port, "GET", "/api/v1/status")
    assert code == 500 and r["error"]["k"] == "srv.internal"
    assert r["error"]["v"]["error"].startswith("ZeroDivisionError")
    assert "ZeroDivisionError" in r["detail"]
    # the frame it came from goes to stderr – the console, or app.log in a bundle
    err = capsys.readouterr().err
    assert "internal error on GET /api/v1/status" in err and "Traceback" in err
    assert "ZeroDivisionError" in err and 'lambda: 1 / 0' in err


def test_die_ablehnung_spricht_immer_englisch(server, monkeypatch):
    """`error.text` is for whoever has no strings – a script, a log, a bug
    report – and is therefore always English, whatever the page speaks.
    The page reads `k` and `v` and renders the user's language itself."""
    a, port = server
    monkeypatch.setitem(a.cfg, "language", "de")
    code, r = call(port, "GET", "/api/gibtsnicht")
    assert code == 404 and r["detail"] == "No such route: /api/gibtsnicht"
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", "/api/gibtsnicht", headers={"Accept-Language": "de-CH, fr;q=0.5"})
    r = json.loads(con.getresponse().read())
    con.close()
    assert r["detail"] == "No such route: /api/gibtsnicht"
    assert r["error"]["k"] == "srv.notfound"          # the page translates this


def test_kein_fehler_wird_von_hand_gebaut():
    """Every refusal goes through Handler._fehler: no route builds an
    `{"ok": False, …}` or `{"error": …}` answer of its own – that is what
    keeps the shape one."""
    quelle = Path(app_mod.__file__).read_text(encoding="utf-8")
    handler = quelle[quelle.index("class Handler("):]
    ohne_helfer = re.sub(r"    def _fehler\(.*?\n(?=    def )", "", handler, flags=re.S)
    assert '"ok": False' not in ohne_helfer
    assert not re.search(r'_json\(\{"error"', ohne_helfer)
    assert not re.search(r'return \{"error"', ohne_helfer)
    assert "Unbekannter Pfad" not in quelle


def test_http_token_speichern(server, sandbox):
    a, port = server
    tok = make_jwt(exp=time.time() + 3600,
                   scp="Mail.Read Calendars.Read Contacts.Read Chat.Read")
    code, r = call(port, "PUT", "/api/v1/access/token", {"token": "Bearer " + tok})
    assert code == 200 and r["token"]["valid"] is True
    assert app_mod.read_token() == tok
    assert a.status()["wizard"] is None            # valid -> no wizard anymore


def test_http_token_abgelaufen_wird_gemeldet(server):
    _, port = server
    code, r = call(port, "PUT", "/api/v1/access/token", {"token": make_jwt(exp=time.time() - 10)})
    assert schluessel(r["error"]) == "srv.token.stale"


def test_http_token_fehlende_rechte_werden_benannt(server):
    _, port = server
    code, r = call(port, "PUT", "/api/v1/access/token",
                   {"token": make_jwt(exp=time.time() + 3600, scp="Mail.Read")})
    assert schluessel(r["message"]) == "srv.token.saved.scopes"
    assert "Calendars.Read" in werte(r["message"])["list"]


def test_http_token_muell_wird_abgelehnt(server):
    _, port = server
    assert call(port, "PUT", "/api/v1/access/token", {"token": ""})[1]["ok"] is False
    assert call(port, "PUT", "/api/v1/access/token", {"token": "zu-kurz"})[1]["ok"] is False


def test_http_wizard_seen(server):
    """The "Later" button resets the memory of a dead token – otherwise the
    wizard would reopen immediately on the next status poll."""
    a, port = server
    a.jobs.token_expired = True
    call(port, "DELETE", "/api/v1/access/notice")
    assert a.jobs.token_expired is False


def test_http_run_ohne_token(server):
    _, port = server
    code, r = call(port, "POST", "/api/v1/runs", {"outlook": True})
    assert code == 409 and not r["ok"]


@pytest.mark.parametrize("cats,erwartet", [
    # (does the calendar step exist, does it read the mails)
    (["mail", "calendar"], (True, True)),
    (["contacts"], (True, False)),        # the reported case
    (["calendar"], (True, False)),
    (["mail"], (False, False)),           # nothing to build
])
def test_http_run_stimmt_den_kalenderschritt_ab(server, monkeypatch, cats, erwartet):
    """The path the UI actually takes. It still sends calendar=true with
    every Outlook run; refinement happens server-side so the rule lives in
    one place only."""
    a, port = server
    a.cfg["outlook_categories"] = cats
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.setdefault("steps", steps) or True)

    code, r = call(port, "POST", "/api/v1/runs",
                   {"outlook": True, "index": True, "calendar": True})
    assert code == 202 and r["run"]
    kal = [s for s in gesehen["steps"] if s["key"] == "calendar"]
    assert (bool(kal), bool(kal) and "--no-reconstruct" not in kal[0]["argv"]) == erwartet


def test_http_legacy_kommentare_neu_lesen(server, monkeypatch):
    """The Planner settings' "Read legacy comments again" button: the flag
    travels from the request into the step's environment, and the page
    wires the button through the ABGLEICH map like every other sync."""
    a, port = server
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.setdefault("steps", steps) or True)

    code, r = call(port, "POST", "/api/v1/runs",
                   {"planner": True, "legacy_comments": True,
                    "label": "job.planner.legacy"})
    assert code == 202 and r["run"]
    (schritt, kette) = gesehen["steps"]
    assert schritt["key"] == "planner" and kette["key"] == "evidence"
    assert schritt["env"]["PLANNER_LEGACY_SYNC"] == "1"

    seite = app_mod.seite()
    assert "gleicheOrdnerAb('planner_legacy')" in seite
    assert "legacy_comments: true" in seite
    assert 'data-i18n="settings.planner.legacy"' in seite


def test_http_kalenderknopf_bleibt_vollstaendig(server, monkeypatch):
    """"Build calendar & contacts" comes without outlook – whoever presses it
    wants the evaluation, regardless of what was last exported."""
    a, port = server
    a.cfg["outlook_categories"] = ["contacts"]
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.setdefault("steps", steps) or True)

    code, r = call(port, "POST", "/api/v1/runs", {"calendar": True})
    assert code == 202 and r["run"]
    kal = [s for s in gesehen["steps"] if s["key"] == "calendar"]
    assert kal and "--no-reconstruct" not in kal[0]["argv"]


def test_http_config_speichern(server, sandbox):
    a, port = server
    code, r = call(port, "PATCH", "/api/v1/config",
                   {"outlook_categories": ["contacts", "quatsch"], "workers": 2,
                    "mcp_port": "nonsense", "unbekannt": "x"})
    assert code == 200
    assert r["config"]["outlook_categories"] == ["contacts"]
    assert r["config"]["workers"] == 2
    assert r["config"]["mcp_port"] == app_mod.DEFAULT_CONFIG["mcp_port"]   # unchanged
    assert "unbekannt" not in r["config"]
    assert app_mod.load_config()["workers"] == 2                          # persisted


def test_http_config_userflow_grenzen(server, sandbox):
    """0 means off and stays 0; the ceiling is 50."""
    _, port = server
    code, r = call(port, "PATCH", "/api/v1/config", {"userflow_actions": 99})
    assert code == 200 and r["config"]["userflow_actions"] == 50
    code, r = call(port, "PATCH", "/api/v1/config", {"userflow_actions": 0})
    assert code == 200 and r["config"]["userflow_actions"] == 0


def test_http_config_schalter_und_ordner(server, sandbox):
    a, port = server
    code, r = call(port, "PATCH", "/api/v1/config",
                   {"embed_images": False, "include_hidden": True,
                    "skip_folders": "Archiv\nDrafts", "index_batch": 16})
    assert code == 200
    cfg = r["config"]
    assert cfg["embed_images"] is False and cfg["include_hidden"] is True
    assert cfg["skip_folders"] == ["archiv", "drafts"]
    assert cfg["index_batch"] == 16
    assert app_mod.load_config()["skip_folders"] == ["archiv", "drafts"]


@pytest.mark.parametrize("key,eingabe,erwartet", [
    ("workers", 99, 8),          # Graph allows 4 in parallel, more means throttling
    ("workers", 0, 1),
    ("mcp_port", 80, 1024),      # privileged ports are not among them
    ("mcp_port", 99999, 65535),
    ("index_batch", 9999, 512),
    ("index_batch", -3, 1),
])
def test_http_config_begrenzt_zahlen(server, key, eingabe, erwartet):
    """A mistyped number must not cripple the next run."""
    code, r = call(server[1], "PATCH", "/api/v1/config", {key: eingabe})
    assert r["config"][key] == erwartet


def test_http_config_ignoriert_unsinnige_zahlen(server):
    vorher = call(server[1], "GET", "/api/v1/config")[1]["config"]["workers"]
    r = call(server[1], "PATCH", "/api/v1/config", {"workers": "vier"})[1]
    assert r["config"]["workers"] == vorher


def test_status_nennt_die_ordner_vorgabe(server):
    """The reset button in the UI fills itself from this."""
    s = call(server[1], "GET", "/api/v1/app")[1]
    assert s["skip_folders_default"] == sorted(app_mod.SKIP_FOLDERS_DEFAULT)


def test_http_zeitplan_speichern(server, sandbox):
    a, port = server
    code, r = call(port, "PATCH", "/api/v1/schedule",
                   {"enabled": True, "interval_minutes": 1, "teams": False})
    assert code == 200
    assert r["schedule"]["enabled"] is True
    assert r["schedule"]["interval_minutes"] == 5          # lower bound kicks in
    assert r["schedule"]["teams"] is False
    assert app_mod.load_config()["schedule"]["enabled"] is True
    assert a.scheduler.last_run is not None                # interval counts from now


def test_http_mcp_ohne_index(server):
    _, port = server
    code, r = call(port, "PATCH", "/api/v1/mcp", {"running": True})
    assert schluessel(r["error"]) == "srv.mcp.noindex"
    # `running` says what it should be; a body that says nothing, or says
    # it in a word, is a 400 – and the sentence names the field.
    for body in ({}, {"running": "true"}, {"running": 1}):
        code, r = call(port, "PATCH", "/api/v1/mcp", body)
        assert code == 400 and "running" in r["detail"], (body, r)


def test_http_log(server):
    a, port = server
    a.jobs.log("hallo")
    code, r = call(port, "GET", "/api/v1/log?since=0")
    assert code == 200 and r["items"][-1]["text"] == "hallo"
    assert call(port, "GET", f"/api/v1/log?since={r['seq']}")[1]["items"] == []


def test_http_kalender_fehlt(server):
    code, r = call(server[1], "GET", "/api/v1/calendar")
    assert code == 404 and r["recs"] == [] and schluessel(r["error"]) == "cal.missing"


def test_http_kalender_wird_gepackt_ausgeliefert(server, sandbox):
    """Around 5 MB of JSON – uncompressed, that would be waste on every tab switch."""
    a, port = server
    daten = {"generated": "2026-08-07T10:00:00", "counts": {"kalender": 1},
             "recs": [{"src": "kalender", "title": "Regelrunde", "ts": 1.0,
                       "st": "deleted", "root": "outlook", "rel": "E-Mail/x.eml"}]}
    ziel = app_mod.calendar_file(a.cfg)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")

    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", "/api/v1/calendar", None, {"Accept-Encoding": "gzip"})
    r = con.getresponse()
    roh = r.read()
    con.close()
    assert r.status == 200 and r.getheader("Content-Encoding") == "gzip"
    assert json.loads(gzip.decompress(roh))["recs"][0]["title"] == "Regelrunde"

    # Without Accept-Encoding: pass through unchanged
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", "/api/v1/calendar", None, {"Accept-Encoding": "identity"})
    r2 = con.getresponse()
    klar = r2.read()
    con.close()
    assert r2.getheader("Content-Encoding") is None
    assert json.loads(klar)["counts"]["kalender"] == 1


def test_kalender_puffer_erkennt_neue_daten(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    ziel = app_mod.calendar_file(a.cfg)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text('{"recs": [], "counts": {"kalender": 1}}', encoding="utf-8")
    erst, _ = a.calendar_payload()
    assert a.calendar_payload()[0] is erst          # cached, not re-read
    time.sleep(0.01)
    ziel.write_text('{"recs": [], "counts": {"kalender": 2}}', encoding="utf-8")
    zweit, _ = a.calendar_payload()
    assert b'"kalender": 2' in zweit                # read in again


def test_http_suche_ohne_index_meldet_das(server):
    _, port = server
    code, r = call(port, "GET", "/api/v1/search?q=test")
    # Die Ablehnung traegt die Form der Antwort, die sie ersetzt – und die
    # heisst auf dieser Oberflaeche `items`, nicht wie die Maschine dahinter.
    assert code == 503 and r["items"] == [] and schluessel(r["error"]) == "srv.noindex"
    assert "hits" not in r


def test_http_ollama_recheck(server):
    _, port = server
    code, r = call(port, "POST", "/api/v1/ollama/recheck")
    assert code == 200 and r["ollama"]["running"] is True


def test_http_kaputter_body_wird_abgelehnt(server):
    """A body that is there has to be JSON, and has to say so – an absent
    one stays fine, every route falls back to its defaults."""
    _, port = server
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("PATCH", "/api/v1/config", "{kein json",
                {"Content-Type": "application/json"})
    r = con.getresponse()
    koerper = json.loads(r.read())
    con.close()
    assert r.status == 400 and koerper["error"]["k"] == "srv.badjson"
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("PATCH", "/api/v1/config", "name=x", {"Content-Type": "text/plain"})
    r = con.getresponse()
    koerper = json.loads(r.read())
    con.close()
    assert r.status == 415 and koerper["error"]["k"] == "srv.mediatype"
    assert call(port, "DELETE", "/api/v1/access/notice")[0] == 204      # no body at all


def test_http_suche_und_quelldatei(sandbox, with_ollama, store):
    """Search and source-file delivery through the server, against the real store."""
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        code, r = call(port, "GET", "/api/v1/search?q=Rechnung&limit=5")
        assert code == 200 and len(r["items"]) >= 1
        assert r["semantic"] is False                      # without vectors.npy
        uri = r["items"][0]["uri"]
        assert uri.startswith("o365://teams/")

        code, r2 = call(port, "GET", "/api/v1/people?limit=5")
        assert "Alice Example" in [p["name"] for p in r2["items"]]

        code, r3 = call(port, "GET", "/api/v1/documents?uid=" + r["items"][0]["uid"])
        assert "4711" in json.dumps(r3)

        con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        con.request("GET", "/api/v1/files/content?root=teams&path=1on1/alice__abc.html")
        resp = con.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert resp.getheader("Content-Security-Policy") == "sandbox"
        assert "4711" in body
        # Teams exports are made for reading and stay in the browser.
        assert resp.getheader("Content-Disposition") is None
        assert resp.getheader("Content-Type").startswith("text/html")
        con.close()

        # Breaking out of the export folder is rejected
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        con.request("GET", "/api/v1/files/content?root=teams&path=../../etc/passwd")
        resp = con.getresponse()
        resp.read()
        assert resp.status == 404
        con.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# Bundled operation (PyInstaller): self-invocation instead of .py files
# --------------------------------------------------------------------------
@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Pretends app.py runs as a bundled binary."""
    monkeypatch.setattr(app_mod, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Munimentum"))
    return tmp_path


def test_script_argv_als_skript(sandbox):
    argv = app_mod.script_argv("teams_export", "ordner")
    assert argv[0] == sys.executable
    assert argv[1].endswith("teams_export.py")
    assert argv[2] == "ordner"


def test_script_argv_gebuendelt(sandbox, frozen):
    """In the bundle there is neither an interpreter nor .py files – the
    executable calls itself with --run."""
    assert app_mod.script_argv("teams_export", "ordner") == \
        [sys.executable, "--run", "teams_export", "ordner"]


def test_script_argv_wandelt_argumente_in_text(sandbox):
    assert app_mod.script_argv("rag_index", "--port", 8365)[-1] == "8365"


def test_script_argv_lehnt_unbekanntes_teilprogramm_ab(sandbox):
    with pytest.raises(ValueError, match="Unbekanntes Teilprogramm"):
        app_mod.script_argv("rm", "-rf")


def test_build_steps_gebuendelt(sandbox, frozen):
    steps = app_mod.build_steps(_cfg_mit_kategorien(), {"outlook": True, "index": True}, token="tok")
    assert steps[0]["argv"][:3] == [sys.executable, "--run", "outlook_export"]
    assert steps[1]["argv"][:3] == [sys.executable, "--run", "rag_index"]


def test_run_bundled_startet_teilprogramm(sandbox, monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod.importlib, "import_module",
                        lambda name: gesehen.update(name=name)
                        or type("M", (), {"main": staticmethod(
                            lambda: gesehen.update(argv=list(sys.argv)))}))
    app_mod.run_bundled("rag_index", ["--store", "s"])
    assert gesehen["name"] == "rag_index"
    assert gesehen["argv"] == ["rag_index.py", "--store", "s"]


def test_run_bundled_lehnt_unbekanntes_ab(sandbox):
    with pytest.raises(SystemExit, match="Unbekanntes Teilprogramm"):
        app_mod.run_bundled("boese", [])


def test_main_leitet_run_weiter(monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod, "run_bundled",
                        lambda name, argv: gesehen.update(name=name, argv=argv))
    monkeypatch.setattr(app_mod, "serve", lambda *a, **k: pytest.fail("darf nicht"))
    app_mod.main(["--run", "mcp_server", "--transport", "stdio"])
    assert gesehen == {"name": "mcp_server", "argv": ["--transport", "stdio"]}


def test_main_run_ohne_namen(monkeypatch):
    with pytest.raises(SystemExit, match="braucht einen Namen"):
        app_mod.main(["--run"])


def test_main_data_dir_haengt_die_pfade_um(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "serve", lambda *a, **k: None)
    ziel = tmp_path / "woanders"
    app_mod.main(["--data-dir", str(ziel), "--no-browser"])
    assert app_mod.BASE == ziel.resolve()
    assert app_mod.TOKEN_FILE == ziel.resolve() / "gx_token.txt"
    assert ziel.is_dir()                                    # gets created


def test_data_dir_je_betriebssystem(monkeypatch):
    monkeypatch.setattr(app_mod, "FROZEN", True)
    for _n in ("MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR"):
        monkeypatch.delenv(_n, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert app_mod.data_dir().parts[-3:] == ("Library", "Application Support",
                                             app_mod.APP_DIRNAME)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\x\\AppData\\Local")
    assert app_mod.data_dir().name == app_mod.APP_DIRNAME
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg")
    assert app_mod.data_dir() == Path("/tmp/xdg") / app_mod.APP_DIRNAME


def test_data_dir_per_umgebungsvariable(monkeypatch, tmp_path):
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(tmp_path))
    assert app_mod.data_dir() == tmp_path.resolve()


def test_data_dir_als_skript_ist_der_projektordner(monkeypatch):
    monkeypatch.setattr(app_mod, "FROZEN", False)
    for _n in ("MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR"):
        monkeypatch.delenv(_n, raising=False)
    assert app_mod.data_dir() == Path(app_mod.__file__).resolve().parent


def test_mcp_client_config_nennt_nur_das_profil(sandbox):
    """The profile is the whole address: the server reads everything else
    from that profile's settings. No paths, no environment, nothing that
    goes stale when a setting changes."""
    cfg = app_mod.load_config()
    conf = app_mod.mcp_client_config(cfg, 8365)
    assert conf["http"]["mcpServers"]["munimentum"]["url"] \
        == "http://127.0.0.1:8365/mcp"
    eintrag = conf["stdio"]["mcpServers"]["munimentum"]
    assert eintrag["args"][-4:] == ["--transport", "stdio", "--profile", "standard"]
    assert "--data-dir" not in eintrag["args"] and "env" not in eintrag


def test_mcp_client_config_unter_data_dir_nennt_die_pfade(sandbox, monkeypatch):
    """No profiles under the all-in-one override – so the paths and the
    home folder go along, absolute: Claude starts the command anywhere."""
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(sandbox))
    conf = app_mod.mcp_client_config(app_mod.load_config(), 8365)
    eintrag = conf["stdio"]["mcpServers"]["munimentum"]
    args = eintrag["args"]
    assert "--profile" not in args and "--data-dir" in args
    ordner = args[args.index("--data-dir") + 1]
    assert Path(ordner).is_absolute() and ordner.startswith(str(sandbox))
    assert eintrag["env"] == {"MUNIMENTUM_HOME": str(sandbox)}


def test_mcp_client_config_gebuendelt(sandbox, frozen):
    conf = app_mod.mcp_client_config(app_mod.load_config(), 8365)
    eintrag = conf["stdio"]["mcpServers"]["munimentum"]
    assert eintrag["command"] == sys.executable          # the app itself
    assert eintrag["args"][:2] == ["--run", "mcp_server"]


def test_ensure_streams_faengt_fehlende_konsole_ab(sandbox, monkeypatch):
    """Windows bundle without a console: sys.stdout is None, every print() would throw."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    f = app_mod.ensure_streams()
    try:
        assert sys.stdout is not None and sys.stderr is not None
        print("Testzeile")
    finally:
        f.close()
    assert "Testzeile" in (sandbox / "app.log").read_text(encoding="utf-8")


def test_ensure_streams_laesst_vorhandene_konsole_in_ruhe(sandbox):
    vorher = sys.stdout
    assert app_mod.ensure_streams() is None
    assert sys.stdout is vorher


def test_server_schweigt_bei_verbindungsabbruch(capsys):
    """Reloads and closed tabs reset sockets all the time – the full
    traceback for that buried real errors in the noise."""
    srv = app_mod.Server.__new__(app_mod.Server)
    try:
        raise ConnectionResetError(54, "Connection reset by peer")
    except ConnectionResetError:
        srv.handle_error(None, ("127.0.0.1", 1234))
    assert capsys.readouterr().err == ""

    try:
        raise ValueError("echter Fehler")
    except ValueError:
        srv.handle_error(None, ("127.0.0.1", 1234))
    assert "ValueError" in capsys.readouterr().err


def test_make_server_weicht_auf_den_naechsten_port_aus(sandbox, with_ollama):
    """Second start or occupied port: a double-click must not end in a
    traceback that nobody sees in a windowless app."""
    a = app_mod.App(app_mod.load_config())
    erster = app_mod.make_server(a, 0)
    port = erster.server_address[1]
    try:
        zweiter = app_mod.make_server(a, port)
        try:
            # Not exactly port+1: a neighbouring port may be busy on CI
            # runners – any free port within the search range is correct.
            assert port < zweiter.server_address[1] < port + 12
        finally:
            zweiter.server_close()
    finally:
        erster.server_close()


# --------------------------------------------------------------------------
# UI: run the embedded JavaScript in node
# --------------------------------------------------------------------------
DOM_STUMMEL = """
process.on('unhandledRejection', function(){});
// Beim Laden ruft die Seite einmal /api/v1/status. Kaeme dort {} zurueck, wuerde
// renderStatus mittendrin scheitern und ein halb gesetztes S hinterlassen -
// ein Zustand, den es im Betrieb nicht gibt. Also ein vollstaendiger Status.
global.fetch = function(){
  return Promise.resolve({json: function(){
    return Promise.resolve(typeof statusGeruest === 'function' ? statusGeruest() : {});
  }});
};
var knoten = {};
function mk(id){ return {id: id, innerHTML: '', textContent: '', className: '', value: '',
  scrollTop: 0, clientHeight: 0, scrollHeight: 0, childElementCount: 0, dataset: {},
  // classList merkt sich wirklich etwas. Eine Attrappe, die nur nickt, laesst
  // genau die Fehler durch, um die es hier geht ("Knopf bleibt sichtbar").
  classList: (function(){
    var drin = {};
    return {
      add: function(c){ drin[c] = true; },
      remove: function(c){ delete drin[c]; },
      contains: function(c){ return !!drin[c]; },
      toggle: function(c, an){
        if(an === undefined) an = !drin[c];
        if(an) drin[c] = true; else delete drin[c];
        return !!drin[c];
      }};
  })(),
  appendChild: function(){}, removeChild: function(){}, firstChild: null,
  style: {},                     // reicht: der Code setzt darauf nur Werte
  addEventListener: function(){}, scrollIntoView: function(){},
  // Attribute wirklich merken: ein Stummel, der nur nickt, liesse genau die
  // Fehler durch, um die es hier geht ("aria-expanded folgt dem Zustand nicht").
  attrs: {},
  setAttribute: function(k, v){ this.attrs[k] = String(v); },
  getAttribute: function(k){ return k in this.attrs ? this.attrs[k] : null; },
  focus: function(){ global.document.activeElement = this; },
  select: function(){},          // der Rückfall beim Kopieren markiert das Feld
  querySelector: function(){ return null; },
  querySelectorAll: function(){ return []; }}; }

// Der Assistent liegt in #modal. Ein Zuweisen von innerHTML ersetzt im Browser
// samtliche Kindknoten - das Textfeld #tok ist danach ein NEUES, leeres
// Element. Ohne dieses Verhalten koennte der Test gar nicht zeigen, ob eine
// Eingabe verloren geht, und waere wertlos.
global.zaehlerNeuzeichnen = 0;
var modalRoh = mk('modal');
var modal = {
  get innerHTML(){ return modalRoh.innerHTML; },
  set innerHTML(v){ modalRoh.innerHTML = v; global.zaehlerNeuzeichnen++;
                    delete knoten['tok']; },
  classList: modalRoh.classList,
};
knoten['modal'] = modal;

// Dieselbe Buchfuehrung fuer die Zugangskarte: sie zeichnet ihr Inneres
// genauso als Zeichenkette, und auch dort muss ein halb eingetippter
// Schluessel einen Statusabruf ueberleben.
global.zaehlerZugang = 0;
var zugangRoh = mk('zugang-inhalt');
knoten['zugang-inhalt'] = {
  get innerHTML(){ return zugangRoh.innerHTML; },
  set innerHTML(v){ zugangRoh.innerHTML = v; global.zaehlerZugang++;
                    delete knoten['tok']; },
  classList: zugangRoh.classList,
};

// Der Assistent baut sein Inneres als HTML-Zeichenkette. Fuer Tastatur und
// Fokus braucht es daraus echte Knoten - sonst koennte kein Test zeigen, dass
// ESC schliesst oder Tab im Fenster bleibt. Gemerkt je Zeichenkette, damit
// zwei Abfragen dieselben Objekte liefern (im Browser ist es derselbe Knoten;
// ohne das schluege jeder Vergleich mit activeElement fehl).
var knotenCache = {};
function ausHtml(html){
  if(knotenCache[html]) return knotenCache[html];
  var out = [], re = /<(button|textarea|a|summary|input|select)\\b([^>]*)>/g, m;
  while((m = re.exec(html))){
    (function(tag, attr){
      function A(name){
        var tr = new RegExp(name + '="([^"]*)"').exec(attr);
        return tr ? tr[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '';
      }
      out.push({tag: tag, className: A('class'), id: A('id'), href: A('href'),
                onclickCode: A('onclick'),
                focus: function(){ global.document.activeElement = this; },
                click: function(){ (0, eval)(this.onclickCode); }});
    })(m[1], m[2]);
  }
  knotenCache[html] = out;
  return out;
}
function passt(n, sel){
  sel = sel.trim();
  if(sel === '[href]') return n.tag === 'a' && !!n.href;
  if(sel.indexOf('[tabindex]') === 0) return false;
  var teile = sel.split('.'), tag = teile.shift();
  if(tag && n.tag !== tag) return false;
  return teile.every(function(c){ return (' ' + n.className + ' ').indexOf(' ' + c + ' ') >= 0; });
}
modal.querySelectorAll = function(sel){
  var teile = String(sel).split(',');
  return ausHtml(modalRoh.innerHTML).filter(function(n){
    return teile.some(function(s){ return passt(n, s); });
  });
};
modal.querySelector = function(sel){ return modal.querySelectorAll(sel)[0] || null; };
// Die Zugangskarte wird genauso befragt wie das Fenster.
knoten['zugang-inhalt'].querySelectorAll = function(sel){
  var teile = String(sel).split(',');
  return ausHtml(zugangRoh.innerHTML).filter(function(n){
    return teile.some(function(s){ return passt(n, s); });
  });
};
knoten['zugang-inhalt'].querySelector = function(sel){
  return knoten['zugang-inhalt'].querySelectorAll(sel)[0] || null;
};

global.document = {
  documentElement: {},
  title: '',
  activeElement: null,
  // Die Seite haengt ihre Tastaturbehandlung hier ein; `taste()` loest sie aus.
  handler: {},
  addEventListener: function(art, fn){ (this.handler[art] = this.handler[art] || []).push(fn); },
  getElementById: function(id){
    // Die Seite liest ihre Texte aus diesem eingebetteten JSON-Block.
    if(id === 'i18n') return {textContent: global.I18N_ROH};
    if(id === 'schritte') return {textContent: global.SCHRITTE_ROH};
    if(id === 'pruefungen') return {textContent: global.PRUEFUNGEN_ROH};
    // Kindknoten des Assistenten und der Zugangskarte gibt es nur, solange
    // sie in deren HTML stehen.
    if(id === 'tok' && !knoten['tok']){
      if(modalRoh.innerHTML.indexOf('id="tok"') < 0 &&
         zugangRoh.innerHTML.indexOf('id="tok"') < 0) return null;
      knoten['tok'] = mk('tok');
    }
    return knoten[id] || (knoten[id] = mk(id));
  },
  // Welcher Reiter offen ist, liest der Code ueber 'nav [data-tab].on'.
  // Sonst null: der Code prueft damit, ob ein Element schon existiert
  // ('#kalBox [data-rb]'), und ein immer wahrer Stummel liesse ihn den Aufbau
  // ueberspringen. Was es wirklich gibt, steht in `vorhanden`.
  querySelector: function(sel){
    sel = String(sel);
    if(sel.indexOf('data-tab') >= 0){
      var n = mk('tabbtn'); n.dataset = {tab: global.aktiverTab || 'export'}; return n;
    }
    for(var muster in global.vorhanden){
      if(sel.indexOf(muster) >= 0) return global.vorhanden[muster];
    }
    return null;
  },
  querySelectorAll: function(){ return []; },
  createElement: function(){ return mk('x'); },
  // Der Rueckfall beim Kopieren haengt ein Feld voruebergehend in die Seite.
  body: mk('body'),
};
// Einen Tastendruck ausloesen - wie im Browser, samt preventDefault.
global.taste = function(key, opt){
  var e = Object.assign({key: key, shiftKey: false, metaKey: false, ctrlKey: false,
                         verhindert: false}, opt || {});
  e.preventDefault = function(){ e.verhindert = true; };
  (global.document.handler.keydown || []).forEach(function(fn){ fn(e); });
  return e;
};
global.aktiverTab = 'export';
global.vorhanden = {'.rbcount': mk('rbcount'), 'main': mk('main'),
                    'nav': mk('nav'), '.balken': mk('balken')};
global.setInterval = function(){ return 0; };
// setTimeout echt lassen: die Kalenderpruefung wartet auf Promises.
global.alert = function(){};
"""

GRUNDZUSTAND = """
S = {token: {present: true, valid: true, expired: false, missing: [],
             account: 'a@example.com', expires_in_minutes: 620},
     ollama: {running: true, has_model: false, has_chat_model: false,
              model: 'bge-m3', chat_model: 'qwen2.5:7b', models: []},
     ollama_hint: {os: 'macOS', steps: ['Schritt eins'], brew: 'brew install ollama'},
     scopes_needed: ['Mail.Read', 'User.Read'],
     scope_queries: {'Mail.Read': 'https://graph.microsoft.com/v1.0/me/messages'},
     graph_explorer: 'https://example.invalid'};
var modal = document.getElementById('modal');
function pruefe(bedingung, text){ if(!bedingung) throw new Error(text); }
// Die drei Antworten, die nicht gepollt werden: einmal geholt, in
// renderStatus untergemischt. Die Tests fuellen sie wie die Seite.
UMGEBUNG = {version: '1.0.1', build: 'abc1234', api_version: 'v1',
            releases_url: 'https://x',
            default_client_id: 'std', data_dir: '/tmp/daten', frozen: false,
            ollama_hint: S.ollama_hint, scopes_needed: S.scopes_needed,
            scope_queries: S.scope_queries, graph_explorer: S.graph_explorer,
            mcp_client: {http: {}, stdio: {}}, case_export_dir: '/tmp/exporte',
            skip_folders_default: [], filetype_hidden_default: []};
BESTAND = {exports: {teams: {last_run: null}, outlook: {last_run: null}},
           store: {exists: true, semantic: false, built_at: null,
                   features: ['thread', 'gone']}};
KONFIG = {outlook_categories: [], teams_categories: [], store_dir: 'rag_store',
          language: 'auto', auth_mode: 'token', client_id: 'std',
          tenant: 'organizations', embed_model: 'bge-m3', chat_model: 'qwen2.5:7b',
          schedule: {enabled: false, interval_minutes: 60,
                     outlook: true, teams: true, index: true}};
"""

# The token wizard must not throw away a half-finished input.
PRUEFUNG_EINGABE = GRUNDZUSTAND + """
var karte = document.getElementById('zugang-inhalt');
zeichneZugang();
pruefe(karte.innerHTML.indexOf('id="tok"') >= 0, 'Zugangskarte nicht gezeichnet');
pruefe(karte.innerHTML.indexOf('me/messages') >= 0, 'Beispielabfrage fehlt');
pruefe(zaehlerZugang === 1, 'Erwartet: einmal gezeichnet');

// Jemand fuegt den Token ein. Jeder Statusabruf zeichnet die Karte neu -
// ohne Zustandsaenderung darf dabei nichts passieren.
document.getElementById('tok').value = 'EINGEFUEGTER-TOKEN';
zeichneZugang();
pruefe(zaehlerZugang === 1, 'Ohne Aenderung neu gezeichnet');
pruefe(document.getElementById('tok').value === 'EINGEFUEGTER-TOKEN',
       'Eingabe wurde beim Statusabruf geloescht');

// Aendert sich der Zustand, MUSS neu gezeichnet werden - die Eingabe darf
// trotzdem nicht verloren gehen.
S.token.missing = ['Mail.Read'];
zeichneZugang();
pruefe(zaehlerZugang === 2, 'Zustandswechsel loeste kein Neuzeichnen aus');
pruefe(karte.innerHTML.indexOf('fehlen noch Berechtigungen') >= 0,
       'Zustandswechsel kam im Text nicht an');
pruefe(document.getElementById('tok').value === 'EINGEFUEGTER-TOKEN',
       'Eingabe ging beim Neuzeichnen verloren');

// Und die Karte ist kein Fenster mehr: nichts oeffnet sich von selbst.
pruefe(wizardOffen === null, 'Zugang oeffnet immer noch ein Fenster');
console.log('OK');
"""

# The Ollama wizard must notice when "ollama pull" ran on the side.
PRUEFUNG_OLLAMA = GRUNDZUSTAND + """
openWizard('ollama');
pruefe(modal.innerHTML.indexOf('fehlt noch') >= 0, 'Fehlendes Modell nicht gemeldet');

// Nichts geaendert: nicht neu zeichnen (sonst flackert es im Sekundentakt).
modal.innerHTML = 'UNVERAENDERT';
openWizard('ollama');
pruefe(modal.innerHTML === 'UNVERAENDERT', 'Ohne Aenderung neu gezeichnet');

// Modell ist da. Der Server verlangt jetzt KEINEN Assistenten mehr
// (wizard === null) – renderStatus muss den offenen trotzdem auffrischen.
S.ollama.has_model = true;
renderStatus(Object.assign({}, statusGeruest(), {wizard: null}));
pruefe(modal.innerHTML.indexOf('Ollama ist bereit') >= 0,
       'Offener Assistent blieb auf altem Stand: ' + modal.innerHTML.slice(0, 80));
console.log('OK');
"""

# Calendar data is fetched only when the tab is opened, and discarded after
# a rebuild. The state comes from the status (file mtime) – if the
# "generated" from the JSON were remembered instead, the two values would
# never be equal and the data would be reloaded on every status poll.
PRUEFUNG_KALENDER = GRUNDZUSTAND + """
var geholt = 0;
global.fetch = function(pfad){
  if(String(pfad).indexOf('/api/v1/calendar') >= 0){
    geholt++;
    return Promise.resolve({json: function(){ return Promise.resolve(
      {generated: '2020-01-01T00:00:00', counts: {kalender: 1, rekonstruiert: 1},
       recs: [{src:'kalender', ts: 1750000000, te: 1750003600, st:'deleted', ad:0,
               title:'Jour Fixe', who:'Alice', d:'2025-06-15 14:00', ctx:'rekonstruiert',
               ppl:'alice', x:'', root:'outlook', rel:'E-Mail/absage.eml'},
              {src:'kontakte', title:'Alice Example', em:['a@example.com'], tel:[],
               org:'Firma', role:'Chefin', root:'outlook', rel:'kontakte/a.vcf'}]}); }});
  }
  return Promise.resolve({json: function(){ return Promise.resolve({}); }});
};

var status = statusGeruest();
status.calendar = {exists: true, built_at: '2026-08-07T10:00:00'};

// Erst den Start abwarten: die Seite ruft beim Laden selbst /api/v1/status auf.
setTimeout(function(){
aktiverTab = 'suche';
offeneSicht = 'kalender';
renderStatus(status);
ladeKalender('kalender');
setTimeout(function(){
  pruefe(geholt === 1, 'Kalenderdaten nicht geholt');
  // A reconstructed appointment sits in the grid, marked – no list of its own
  calMode = 'week'; cursor = new Date(1750000000 * 1000); drawCal();
  pruefe(document.getElementById('kalBox').innerHTML.indexOf('ev deleted') >= 0, 'Rekonstruierter Termin fehlt im Raster');
  pruefe(contacts.length === 1, 'Kontakt fehlt');

  // Unveraenderter Stand: nicht erneut holen.
  renderStatus(status);
  renderStatus(status);
  pruefe(geholt === 1, 'Ohne Neuaufbau erneut geholt (Stand falsch gemerkt)');

  // Neuer Aufbau: Daten verwerfen und neu holen.
  var neu = Object.assign({}, status, {calendar: {exists: true, built_at: '2026-08-07T12:00:00'}});
  renderStatus(neu);
  setTimeout(function(){
    pruefe(geholt === 2, 'Nach Neuaufbau nicht neu geholt');
    console.log('OK');
  }, 10);
}, 10);
}, 0);
"""

# Actually draw the address book and the reconstructed-appointments list –
# both filter by search terms AND translate while doing so. Right there a
# local variable `t` once shadowed the translation function t(), and the view
# silently stayed at "Loading…" because the promise swallowed the error.
PRUEFUNG_ANSICHTEN = GRUNDZUSTAND + """
global.fetch = function(pfad){
  if(String(pfad).indexOf('/api/v1/calendar') >= 0){
    return Promise.resolve({json: function(){ return Promise.resolve(
      {generated: '2026-08-07T09:00:00', counts: {kalender: 2, rekonstruiert: 1},
       recs: [
        {src:'kontakte', title:'Alice Example', em:['alice@example.com'], tel:['+49 1'],
         org:'Firma GmbH', role:'Chefin', root:'outlook', rel:'kontakte/a.vcf'},
        {src:'kontakte', title:'Bob Builder', em:['bob@example.com'], tel:[],
         org:'Bau AG', role:'', root:'outlook', rel:'kontakte/b.vcf'},
        {src:'kalender', ts: 1750000000, te: 1750003600, ad:0, st:'confirmed',
         title:'Regelrunde', who:'Alice', d:'2025-06-15 14:00', ctx:'Kalender',
         loc:'Raum 7', att:['Bob'], root:'outlook', rel:'kalender/x.ics'},
        {src:'kalender', ts: 1750086400, te: 1750090000, ad:0, st:'deleted',
         title:'Jour Fixe', who:'Alice', d:'2025-06-16 14:00', ctx:'rekonstruiert',
         ppl:'alice', x:'Agenda', root:'outlook', rel:'E-Mail/absage.eml'}]}); }});
  }
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};

setTimeout(function(){
  var status = statusGeruest();
  status.calendar = {exists: true, built_at: '2026-08-07T10:00:00'};
  renderStatus(status);
  aktiverTab = 'suche';
  offeneSicht = 'adressbuch';
  ladeKalender('adressbuch');

  setTimeout(function(){
    var buch = document.getElementById('kbBox').innerHTML;
    pruefe(buch.indexOf('Alice Example') >= 0, 'Adressbuch leer: ' + JSON.stringify(buch.slice(0,90)));
    pruefe(buch.indexOf('alice@example.com') >= 0, 'Mailadresse fehlt');
    pruefe(document.getElementById('kbStats').textContent.indexOf('2') >= 0, 'Zaehlung fehlt');

    // The book searches nothing itself (11.3): its button hands over to
    // Search with the address book as source.
    global.gesucht = [];
    var echteSuche = doSearch;
    doSearch = function(){ gesucht.push(document.getElementById('f-source').value); };
    kontakteSuchen();
    pruefe(gesucht.length === 1 && gesucht[0] === 'kontakte', 'Kontakte nicht an die Suche uebergeben: ' + JSON.stringify(gesucht));
    pruefe(offeneSicht === 'treffer', 'Nicht zur Suche gewechselt');
    doSearch = echteSuche;

    // The week view draws the ordinary appointment and the reconstructed
    // one alike – the latter marked, with the mail it came from
    calMode = 'week';
    cursor = new Date(1750000000 * 1000);
    drawCal();
    var woche = document.getElementById('kalBox').innerHTML;
    pruefe(woche.indexOf('Regelrunde') >= 0, 'Wochenansicht leer');
    // the reconstructed one falls on the Monday after: the month shows both
    calMode = 'month'; drawCal();
    var monat = document.getElementById('kalBox').innerHTML;
    pruefe(monat.indexOf('ev deleted') >= 0 && monat.indexOf('Jour Fixe') >= 0, 'Rekonstruierter Termin fehlt im Raster');
    pruefe(monat.indexOf('Gelöscht') >= 0, 'Zustand fehlt im Tooltip');
    calMode = 'week'; drawCal();
    // the week's title stays short: no week number in it, the number in the tooltip
    var titel = document.getElementById('kalTitle').innerHTML;
    pruefe(titel.indexOf('Woche') < 0 && titel.indexOf('2025') >= 0, 'Wochentitel: ' + titel);
    pruefe(String(document.getElementById('kalTitle').title).indexOf('Woche 2') >= 0, 'Wochennummer fehlt im Tooltip');
    console.log('OK');
  }, 20);
}, 0);
"""

# If loading fails, that must be visible. Without a catch, the view silently
# stays at "Loading…" – and nobody knows why.
PRUEFUNG_LADEFEHLER = GRUNDZUSTAND + """
global.fetch = function(pfad){
  if(String(pfad).indexOf('/api/v1/calendar') >= 0)
    return Promise.reject(new Error('Netz weg'));
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};
setTimeout(function(){
  ladeKalender('adressbuch');
  setTimeout(function(){
    var buch = document.getElementById('kbBox').innerHTML;
    pruefe(buch.indexOf('Netz weg') >= 0,
           'Ladefehler wird verschluckt, Ansicht bleibt leer: ' + JSON.stringify(buch));
    pruefe(kalGeladen === false, 'Nach dem Fehler wird kein zweiter Versuch erlaubt');
    console.log('OK');
  }, 20);
}, 0);
"""

# The checkbox for the generated answer may only exist when a model can
# actually produce it – otherwise the UI promises something that never
# arrives. And the answer box must stand out visibly from the hits.
PRUEFUNG_KI = GRUNDZUSTAND + """
function zustand(chat, index){
  var st = statusGeruest();
  st.ollama = {running: true, has_model: true, has_chat_model: chat,
               model: 'bge-m3', chat_model: 'qwen2.5:7b', models: []};
  st.store = {exists: index, chunks: 5, semantic: true, built_at: null, model: null};
  return st;
}
// Die beiden hinteren Suchvarianten haengen an Ollama UND an einem Index mit
// Embeddings. Fehlt eines davon, sind sie ausgegraut - aber sichtbar.
function gesperrt(){
  return document.getElementById('m-aehnlich').disabled &&
         document.getElementById('m-ki').disabled;
}
renderStatus(zustand(false, true));
pruefe(gesperrt(), 'Varianten trotz fehlendem Sprachmodell waehlbar');
renderStatus(zustand(true, false));
pruefe(gesperrt(), 'Varianten trotz fehlendem Index waehlbar');
renderStatus(zustand(true, true));
pruefe(!gesperrt(), 'Varianten gesperrt, obwohl alles da ist');
pruefe(document.getElementById('modus-fehlt').classList.contains('hide'),
       'Hinweis auf Ollama steht da, obwohl es laeuft');

// Wer in einer gesperrten Variante steht, faellt auf die Textsuche zurueck.
suchmodus('ki');
renderStatus(zustand(false, true));
pruefe(SUCHMODUS === 'text', 'Gesperrte Variante blieb aktiv: ' + SUCHMODUS);

// Fussnoten: [1] verweist auf den ersten Treffer, Unbekanntes bleibt Text
kiQuellen = [{n: 1, uid: 'a'}, {n: 2, uid: 'b'}];
var h = mitFussnoten('Bezahlt [1], offen [2], erfunden [9].');
pruefe(h.indexOf('href="#treffer-1"') >= 0, 'Fussnote 1 verweist nicht');
pruefe(h.indexOf('href="#treffer-2"') >= 0, 'Fussnote 2 verweist nicht');
pruefe(h.indexOf('href="#treffer-9"') < 0, 'Erfundene Fussnote wurde verlinkt');
pruefe(h.indexOf('[9]') >= 0, 'Erfundene Fussnote verschwand ganz');

// Welche Treffer zitiert wurden – fuer die Hervorhebung in der Liste
pruefe(zitierte('a [2] b [1] c [2]').join(',') === '2,1', 'Zitate falsch erkannt');
pruefe(zitierte('ohne').length === 0, 'Zitate erfunden');
console.log('OK');
"""

# Three tabs on top, three views below. The test cycles through and checks
# what is visible – and whether the last chosen view survives a tab
# switch.
PRUEFUNG_NAV = GRUNDZUSTAND + """
var sichtbar = {};
['export','suche','einstellungen'].forEach(function(t){
  document.getElementById('tab-'+t).classList.toggle = function(c, an){ sichtbar[t] = !an; };
});
['treffer','kalender','adressbuch'].forEach(function(v){
  document.getElementById('sicht-'+v).classList.toggle = function(c, an){
    sichtbar['sicht:'+v] = !an; };
});
function offen(){ return Object.keys(sichtbar).filter(function(k){ return sichtbar[k]; }); }
function hat(x){ return offen().indexOf(x) >= 0; }

tab('export');
pruefe(hat('export') && !hat('suche') && !hat('einstellungen'), 'Export: ' + offen());

tab('suche');
pruefe(hat('suche') && !hat('export'), 'Suche: ' + offen());
pruefe(hat('sicht:treffer'), 'Suche zeigt nicht die Trefferliste: ' + offen());

sicht('kalender');
pruefe(hat('sicht:kalender') && !hat('sicht:treffer'), 'Kalender: ' + offen());
sicht('adressbuch');
pruefe(hat('sicht:adressbuch') && !hat('sicht:kalender'), 'Adressbuch: ' + offen());

// Reiter wechseln und zurueck: die gewaehlte Sicht bleibt
tab('einstellungen');
pruefe(hat('einstellungen') && !hat('suche'), 'Einstellungen: ' + offen());
tab('suche');
pruefe(hat('sicht:adressbuch'), 'Sicht nach Reiterwechsel vergessen: ' + offen());

// Die Kachel im Kopf fuehrt weiter zu ihrem Thema, jetzt in den Einstellungen
zeigeEinstellung('mcp-karte');
pruefe(hat('einstellungen'), 'MCP-Kachel fuehrt nicht in die Einstellungen');
console.log('OK');
"""

# The bar has two levels: step i of n, and within that as precise as the
# script knows. Where no total exists (Outlook discovers its mails only
# while running), no percentage may be invented.
PRUEFUNG_BALKEN = GRUNDZUSTAND + """
var breite = null, unbekannt = null;
document.getElementById('balken-fuell').style = {set width(v){ breite = v; }};
global.vorhanden['.balken'].classList.toggle = function(c, an){
  if(c === 'unbekannt') unbekannt = an; };

function lauf(index, n, progress){
  return {busy: true, last: null, token_expired: false, seq: 0,
          job: {label: 'job.export', step: 'job.step.teams', index: index,
                steps: new Array(n), progress: progress}};
}

// Schritt 1 von 4, darin 25 % -> 6 %
zeigeFortschritt(lauf(0, 4, {done: 25, total: 100, what: 'chats'}));
pruefe(unbekannt === false, 'Balken als unbekannt markiert, obwohl Gesamtzahl da');
pruefe(breite === '6%', 'Breite bei Schritt 1/4 und 25%: ' + breite);
pruefe(document.getElementById('fortschritt-text').textContent.indexOf('25') >= 0,
       'Zahl fehlt in der Zeile');

// Schritt 3 von 4, darin halb -> (2 + 0,5) / 4 = 63 %
zeigeFortschritt(lauf(2, 4, {done: 50, total: 100, what: 'chats'}));
pruefe(breite === '63%', 'Breite bei Schritt 3/4 und 50%: ' + breite);

// Ohne Gesamtzahl: gestreift, keine erfundene Breite
breite = null;
zeigeFortschritt(lauf(1, 4, {done: 1234, what: 'mails'}));
pruefe(unbekannt === true, 'Ohne Gesamtzahl nicht als unbekannt markiert');
pruefe(breite === null, 'Ohne Gesamtzahl wurde eine Breite gesetzt: ' + breite);
var zeile = document.getElementById('fortschritt-text').textContent;
pruefe(zeile.indexOf('1.234') >= 0 || zeile.indexOf('1,234') >= 0,
       'Zahl fehlt: ' + zeile);

// Fertig: Balken weg, letzte Meldung in die Protokollleiste
var versteckt = null;
document.getElementById('fortschritt').classList.toggle = function(c, an){ versteckt = an; };
zeigeFortschritt({busy: false, job: null, seq: 0, token_expired: false,
                  last: {label: 'job.export', ok: true, detail: '',
                         finished: '2026-08-08T10:00:00'}});
pruefe(versteckt === true, 'Balken bleibt nach dem Lauf stehen');
pruefe(document.getElementById('log-letzte').textContent.length > 3,
       'Letzte Meldung fehlt in der Protokollleiste');
console.log('OK');
"""

# renderStatus reads far more from the status than the wizards do – a
# complete scaffold so the call above goes through.
STATUS_GERUEST = """
/* Genau die Felder, die /api/v1/status seit 12.0 liefert - nicht mehr, plus
   den Index-Knoten, den die Seite seit 13.0 aus dem Bestand bekommt (in
   dieser Umgebung beantwortet der Stummel jede Anfrage). Ein
   grosszuegigerer Stummel haette den Fehler verdeckt, dass die Seite den
   MCP-Schnipsel noch im Status suchte, obwohl er in der Umgebung liegt. */
function statusGeruest(){
  return {token: S.token, ollama: S.ollama,
          auth: {signed_in: false, account: null, device: null,
                 own_registration: false},
          update: {status: 'off', latest: null, url: null, newer: false,
                   ahead: false, error: null, retry_at: null},
          jobs: {busy: false, job: null, last: null, token_expired: false, seq: 0},
          mcp: {running: false, url: 'http://127.0.0.1:8365/mcp', error: null},
          calendar: {built_at: null},
          profile: {name: 'standard', moeglich: false, mehrere: false},
          schedule_next: null, wizard: null};
}
"""


def _seiten_js():
    treffer = re.search(r"<script>(.*?)</script>", app_mod.seite(), re.S)
    assert treffer, "Kein <script>-Block in der Seite"
    # As when serving: insert the registry's step metadata.
    import steps as steps_mod
    return ("global.SCHRITTE_ROH = " + json.dumps(json.dumps(
        steps_mod.ui_metadaten())) + ";\n" +
            "global.PRUEFUNGEN_ROH = " + json.dumps(json.dumps(
        steps_mod.pruef_metadaten())) + ";\n" + treffer.group(1))


# Step headings arrive as a nested message and are resolved in the browser
# – otherwise "job.step.outlook" would appear verbatim in the log.
PRUEFUNG_SCHRITTKOPF = GRUNDZUSTAND + """
var zeile = mtext({k: 'srv.job.step', v: {step: {k: 'job.step.outlook', v: {}}}});
pruefe(zeile.indexOf('Outlook') >= 0, 'Schritt nicht uebersetzt: ' + zeile);
pruefe(zeile.indexOf('job.step.') < 0, 'Schluessel im Protokoll: ' + zeile);

// Die Ergebniszeile entsteht aus dem strukturierten Ereignis – uebersetzt,
// mit den Atomen der Lauf-Historie; Extras behalten ihre technischen Namen.
var erg = mtext({k: 'srv.job.result',
                 v: {ergebnis: {new: 0, unchanged: 67, extra: {moved: 2}}}});
pruefe(erg.indexOf('neu: 0') >= 0, 'neu fehlt: ' + erg);
pruefe(erg.indexOf('unver') >= 0 && erg.indexOf('67') >= 0,
       'unveraendert fehlt: ' + erg);
pruefe(erg.indexOf('moved 2') >= 0, 'Extra fehlt: ' + erg);

// Die Auswahlzeile unter der Ueberschrift nutzt die Worte der Lauf-Historie.
var wahl = mtext({k: 'srv.job.elements',
                  v: {elements: {outlook: ['mail'], teams: [], onedrive: true}}});
pruefe(wahl.indexOf('OneDrive (alle)') >= 0 && wahl.indexOf('Outlook') >= 0,
       'Auswahl nicht wie in der Historie: ' + wahl);
console.log('OK');
"""


def test_schrittkopf_wird_uebersetzt():
    _in_node(PRUEFUNG_SCHRITTKOPF)


PRUEFUNG_BEENDEN = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push(String(pfad));
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};
global.confirm = function(text){ global.gefragt = text; return true; };

renderStatus(statusGeruest());
beenden();
pruefe(String(global.gefragt).length > 10, 'Es wurde nicht rueckgefragt');
pruefe(gesendet.indexOf('/api/v1/quit') >= 0, 'Kein Beenden an den Server: ' + gesendet.join(','));
pruefe(beendet === true, 'Zustand nicht gesetzt');

// Pillen und Protokoll verschwinden mit: sie zeigten sonst eingefrorene
// Zustaende, und "Fehler melden" riefe eine tote API.
pruefe(el('pills').classList.contains('hide'), 'Pillen bleiben stehen');
pruefe(el('protokoll').classList.contains('hide'), 'Protokoll bleibt stehen');

// Danach darf nicht weiter abgefragt werden – sonst Fehler ohne Ende.
var vorher = gesendet.length;
refresh(); pullLog();
pruefe(gesendet.length === vorher, 'Fragt nach dem Beenden weiter');
console.log('OK');
"""


def test_beenden_fragt_zurueck_und_hoert_auf_zu_fragen():
    """Without the button only Activity Monitor would remain – the app has no window."""
    _in_node(PRUEFUNG_BEENDEN)


PRUEFUNG_BEENDEN_LAUF = GRUNDZUSTAND + """
global.confirm = function(text){ global.gefragt = text; return false; };
var st = statusGeruest();
st.jobs = {busy: true, job: {label: 'job.export', step: 'job.step.outlook', index: 0,
                             steps: ['a']}, last: null, token_expired: false, seq: 0};
renderStatus(st);
beenden();
pruefe(String(global.gefragt).indexOf('abgebrochen') >= 0,
       'Warnt nicht vor dem Abbruch: ' + global.gefragt);
pruefe(beendet === false, 'Trotz Abbruch der Rueckfrage beendet');
console.log('OK');
"""


def test_beenden_warnt_bei_laufendem_auftrag():
    _in_node(PRUEFUNG_BEENDEN_LAUF)


# The states used to stand as tiles in the header, named after their
# building blocks: "Token", "Ollama", "MCP running". Now the account chip
# in the header and the two dots in the settings navigation say what the
# state means in everyday words; the technical term stays in the tooltip.
PRUEFUNG_KACHELN = GRUNDZUSTAND + """
function kachel(id){ return document.getElementById('p-' + id + '-t').textContent; }
function hinweis(id){ return document.getElementById('pill-' + id).title || ''; }

var st = statusGeruest();
st.store = {exists: true, chunks: 269744, messages: 238408, semantic: true,
            built_at: '2026-08-07T09:00:00', model: 'bge-m3'};
st.ollama = {running: true, has_model: true, has_chat_model: true,
             model: 'bge-m3', chat_model: 'q', models: []};
st.mcp = {running: true, url: 'http://127.0.0.1:8365/mcp', error: null,
          config: {http: {}, stdio: {}}};
renderStatus(st);

var SYSTEMWORT = ['Chunk', 'chunk', 'Token', 'token', 'Ollama', 'Index'];
['token', 'ollama', 'mcp'].forEach(function(id){
  var text = kachel(id);
  pruefe(text.length > 0, 'Kachel ' + id + ' ist leer');
  SYSTEMWORT.forEach(function(w){
    pruefe(text.indexOf(w) < 0,
           'Kachel ' + id + ' spricht Systemsprache: "' + text + '"');
  });
});
// "MCP" ist hier die Ausnahme und steht nur diesem einen Eintrag zu: er nennt
// einen Endpunkt, und die buergerliche Umschreibung ("Zugriff fuer Claude")
// war schlicht falsch - MCP koennen auch andere Programme, und abgeschaltet
// war nur der HTTP-Weg. Das Wort steht in der festen Beschriftung des
// Navigationspunkts; das Zustandswort daneben sagt nur an oder aus.
pruefe(t('settings.nav.mcp').indexOf('MCP') >= 0, 'Eintrag nennt das Protokoll nicht');
pruefe(kachel('token').indexOf('MCP') < 0, 'Andere Kacheln bleiben ohne Fachwort');

// Der Zustand des Index steht im Analytics-Reiter, nicht im Kopf: zweimal
// dieselbe Zahl an zwei Orten widerspricht sich irgendwann.
pruefe(document.getElementById('pill-index') === null ||
       modal.innerHTML.indexOf('pill-index') < 0, 'Kachel wieder im Kopf');

// Der Fachbegriff bleibt erreichbar - eine Mausbewegung entfernt.
pruefe(hinweis('token').indexOf('Access Token') >= 0, 'Tooltip nennt den Token nicht');
pruefe(hinweis('token').indexOf('a@example.com') >= 0, 'Tooltip nennt das Konto nicht');
pruefe(hinweis('ollama').indexOf('Ollama') >= 0, 'Tooltip nennt Ollama nicht');
pruefe(hinweis('mcp').indexOf('MCP') >= 0, 'Tooltip nennt MCP nicht');

// Drei Lagen, drei Aussagen. Den Transport nennt nur die mittlere: laeuft der
// Endpunkt, sind beide Wege offen, und "MCP HTTP an" laese sich, als waere
// stdio ausgenommen.
pruefe(kachel('mcp').indexOf('HTTP') < 0, 'Laufend wird der Transport genannt: ' + kachel('mcp'));

st.mcp.running = false;
renderStatus(st);
pruefe(kachel('mcp').indexOf('HTTP') >= 0, 'Der abgeschaltete Transport wird nicht benannt');
pruefe(hinweis('mcp').indexOf('stdio') >= 0, 'Tooltip verschweigt den anderen Weg');

KONFIG = Object.assign({}, KONFIG, {mcp_enabled: false});
renderStatus(st);
pruefe(kachel('mcp').indexOf('HTTP') < 0, 'Ganz aus, aber der Transport steht da');
pruefe(document.getElementById('p-mcp').className.indexOf('ok') < 0,
       'Abgeschaltet und trotzdem gruen');

// Ohne Index darf der Kopf nicht stolpern - er zeigt den Zustand nicht mehr,
// aber renderStatus rechnet weiter damit (KI-Kasten, Sicht "Geloeschtes").
st.store = {exists: false, chunks: 0, messages: 0, semantic: false,
            built_at: null, model: null, features: []};
renderStatus(st);
pruefe(document.getElementById('m-ki').disabled,
       'KI-Variante trotz fehlendem Index waehlbar');
console.log('OK');
"""


def test_kacheln_sagen_die_bedeutung_und_nennen_den_begriff_im_tooltip():
    _in_node(PRUEFUNG_KACHELN)


def test_navigation_drei_reiter_drei_sichten():
    """Calendar and address book live under the search, not beside it – and
    the last chosen view survives a tab switch."""
    _in_node(PRUEFUNG_NAV)


def test_fortschrittsbalken_zwei_ebenen():
    """Step i of n times progress within the step – and without a total no
    invented percentage, but a striped bar with the number."""
    _in_node(PRUEFUNG_BALKEN)


def test_ki_checkbox_und_fussnoten():
    """The checkbox appears only with model AND index; footnotes link into
    the hit list, invented numbers stay unlinked text."""
    _in_node(PRUEFUNG_KI)


def test_jeder_reiter_liegt_im_hauptbereich():
    """Regression: the settings section sat behind </main> and thus got
    neither padding nor max width – its cards stuck to the window edge,
    unlike every other tab."""
    seite = app_mod.seite()
    haupt = seite[seite.index("<main>"):seite.index("</main>")]
    for reiter in ("export", "suche", "analytics", "einstellungen"):
        assert f'<section id="tab-{reiter}"' in haupt, f"{reiter} liegt außerhalb <main>"


# The profile in the header only once there is more than one; the storage
# card lists them all, the switch window offers only the others.
PRUEFUNG_PROFIL = GRUNDZUSTAND + """
var pille = document.getElementById('pill-profil');
// The status carries the light state; the list arrives on demand.
zeigeProfile({name: 'standard', moeglich: true, mehrere: false});
pruefe(pille.classList.contains('hide'), 'Ein Profil: die Kopfzeile schweigt');
var std = {name: 'standard', aktiv: false, ordner: '/x', konto: 'a@example.com',
           last_run: null, index: false};
var nw = {name: 'nordwind', aktiv: true, ordner: '/x/profiles/nordwind',
          konto: null, last_run: null, index: false};
var zwei = {name: 'nordwind', moeglich: true, mehrere: true, ohne_nachfrage: true, alle: [std, nw]};
zeigeProfile(zwei);
pruefe(!pille.classList.contains('hide'), 'Zwei Profile: die Kopfzeile nennt das offene');
pruefe(document.getElementById('p-profil-t').textContent === 'Profil: nordwind',
       'Kopfzeile: ' + document.getElementById('p-profil-t').textContent);
zeigeProfilliste(zwei);
pruefe(document.getElementById('profil-dies').textContent === 'nordwind', 'Das aktuelle Profil steht nicht in der Gruppe');
pruefe(document.getElementById('profil-ohne-nachfrage').checked === true, 'Schalter nicht uebernommen');
var liste = document.getElementById('profil-liste').innerHTML;
pruefe(liste.indexOf('a@example.com') >= 0 && liste.indexOf('nordwind') >= 0, 'Liste unvollstaendig');
pruefe(liste.indexOf('/x/profiles/nordwind') < 0, 'Der Ordner steht in der Liste – er gehoert zum aktuellen Profil darunter');
pruefe(liste.indexOf('profilUmbenennenFenster(&quot;standard&quot;)') >= 0, 'Kein Umbenennen fuer das andere Profil');
pruefe(liste.indexOf('profilUmbenennenFenster(&quot;nordwind&quot;)') < 0, 'Das offene Profil bekommt ein Umbenennen');
profilUmbenennenFenster('standard');
pruefe(modal.innerHTML.indexOf('value="standard"') >= 0, 'Name nicht vorbelegt');
pruefe(modal.innerHTML.indexOf('profilUmbenennen(&quot;standard&quot;)') >= 0, 'Kein Umbenennen-Knopf');
closeWizard('profil');
profilWechselnFenster();
pruefe(modal.innerHTML.indexOf('<div class="hit" tabindex="0" role="button" onclick="profilWechseln(&quot;standard&quot;)"') >= 0, 'Die Zeile des anderen Profils ist kein Schalter');
pruefe(modal.innerHTML.indexOf('<div class="hit on fest">') >= 0, 'Das offene Profil ist nicht als solches gezeigt');
pruefe(modal.innerHTML.indexOf('profilWechseln(&quot;nordwind&quot;)') < 0, 'Das offene Profil bekommt einen Schalter');
pruefe(modal.innerHTML.indexOf('profilEinstellungen()') >= 0, 'Kein Zahnrad zu den Einstellungen');
closeWizard('profil');
profilAnlegenFenster();
pruefe(modal.innerHTML.indexOf('id="profil-neu"') >= 0, 'Kein Namensfeld');
pruefe(modal.innerHTML.indexOf('profilAnlegen()') >= 0, 'Kein Anlegen-Knopf');
closeWizard('profil');
zeigeProfile({name: 'standard', moeglich: false, mehrere: false});
pruefe(document.getElementById('profil-gruppe').classList.contains('hide'),
       'Unter --data-dir bleibt die Gruppe weg');
console.log('OK');
"""


def test_profil_in_kopfzeile_und_speicherorten():
    _in_node(PRUEFUNG_PROFIL)


def test_die_reiterzeile_bleibt_kurz():
    """Fetch data, view data, judge the corpus, configure. More levels on top
    confuse more than they order.

    Analytics joined as the fourth because it answers a question of its own –
    "what is there and is it complete?" instead of "where is this one
    thing?". Calendar and address book, by contrast, are views of the same
    corpus as the search and live below it; schedule and MCP are settings.

    Cases became the third door in 11.0 because a case is neither building
    nor searching: it is what someone keeps around one matter, across
    sources, for months – a place of its own, with its own primary action.

    The number stands here as a brake: whoever adds a sixth should have to
    read this rationale."""
    seite = app_mod.seite()
    nav = seite[seite.index("<nav>"):seite.index("</nav>")]
    assert nav.count("data-tab=") == 5, "Die Reiterzeile ist wieder gewachsen"
    assert 'data-tab="faelle"' in nav, "die dritte Tür fehlt"
    # Help sits beside the side rooms as a button that opens the tour's window – no tab, no section
    assert 'id="nav-hilfe" onclick="hilfeFenster()"' in nav and "data-tab" not in nav.split('id="nav-hilfe"')[1]
    assert "tour.again" not in seite, "the chapters live in the help window now, not under Settings › App"
    for weg in ("kalender", "adressbuch", "zeitplan", "mcp"):
        assert f'data-tab="{weg}"' not in nav, f"{weg} ist wieder ein eigener Reiter"
    for sicht in ("treffer", "kalender", "adressbuch"):
        assert f'id="sicht-{sicht}"' in seite, f"Sicht {sicht} fehlt unter der Suche"
    # Schedule and MCP must have ended up in the settings, not vanished
    einst = seite[seite.index('<section id="tab-einstellungen"'):seite.index("</main>")]
    assert 'data-i18n="sched.title"' in einst and 'data-i18n="mcp.title"' in einst


def test_seite_enthaelt_gueltiges_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node nicht vorhanden")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(_seiten_js())
        pfad = f.name
    try:
        r = subprocess.run([node, "--check", pfad], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    finally:
        os.unlink(pfad)


def _in_node(pruefung, sprache="de"):
    """Run the embedded JavaScript plus test code in node.

    With the real language data: the tests thus check the same path the
    browser takes – texts come from lang/*.json, not from the source.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node nicht vorhanden")
    kopf = ("global.I18N_ROH = " + json.dumps(json.dumps(
        {"lang": sprache, "strings": i18n.strings(sprache),
         "languages": i18n.available()}, ensure_ascii=False)) + ";\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(kopf + DOM_STUMMEL + _seiten_js() + STATUS_GERUEST + pruefung)
        pfad = f.name
    try:
        r = subprocess.run([node, pfad], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert "OK" in r.stdout
    finally:
        os.unlink(pfad)


def test_assistent_ueberschreibt_die_eingabe_nicht():
    """Regression: the wizard was redrawn on every status poll and thereby
    wiped the just-pasted token from the text field again."""
    _in_node(PRUEFUNG_EINGABE)


def test_assistent_merkt_wenn_das_modell_nachgeladen_wurde():
    """Regression: after 'ollama pull' the traffic light in the header turned
    green, but the open wizard still said 'model missing'. Once the model is
    there, the server no longer requests a wizard at all – an already open
    one must be refreshed anyway."""
    _in_node(PRUEFUNG_OLLAMA)


# The permissions block is the most technical part of the dialog – names
# like Contacts.Read along with Graph URLs. Usually it is long done and was
# only in the way; but when a permission really is missing, it is the topic.
PRUEFUNG_RECHTE = GRUNDZUSTAND + """
S.token.missing = [];
zugangNeu();
var html = document.getElementById('zugang-inhalt').innerHTML;
pruefe(html.indexOf('<details class="rechte">') >= 0,
       'Berechtigungen stehen nicht in einem einklappbaren Block');
pruefe(html.indexOf('Mail.Read') >= 0, 'Berechtigungen fehlen ganz');

// Eingeklappt heisst: die Schritte kommen ohne sie aus. Drei statt vier.
var liste = html.split('<ol>')[1].split('</ol>')[0];
var schritte = liste.split('<li>').length - 1;
pruefe(schritte === 3, 'Erwartet drei Schritte, gezaehlt: ' + schritte);
pruefe(liste.indexOf('Mail.Read') < 0, 'Berechtigungen stehen noch in den Schritten');

// Fehlt wirklich etwas, muss der Block von selbst offen stehen.
S.token.missing = ['Mail.Read'];
zugangNeu();
pruefe(document.getElementById('zugang-inhalt').innerHTML.indexOf('<details class="rechte" open>') >= 0,
       'Fehlende Berechtigung, Block aber zugeklappt');

// Und der Dialog spricht nicht mehr von Graph oder Tenant - ausser im Link
// auf die Seite, die tatsaechlich so heisst.
S.token.missing = [];
zugangNeu();
var ohneLink = document.getElementById('zugang-inhalt').innerHTML.replace(/<a [^>]*>.*?<\\/a>/g, '');
['Tenant', 'Microsoft Graph', 'Access Token holen'].forEach(function(w){
  pruefe(ohneLink.indexOf(w) < 0, 'Die Karte sagt noch "' + w + '"');
});
console.log('OK');
"""


def test_berechtigungen_sind_eingeklappt_solange_sie_nicht_fehlen():
    _in_node(PRUEFUNG_RECHTE)


# Every wizard once had a different number of buttons - two, three - and in
# the finished Ollama window, of all things "Close" was the primary button,
# while the actual action stood pale beside it.
PRUEFUNG_MODALE = GRUNDZUSTAND + """
function zaehle(html, muster){ return html.split(muster).length - 1; }
// Tut der Knopf mehr, als den Dialog zu schliessen?
function handelt(knopf){
  return knopf.onclickCode.replace(/closeWizard\\([^)]*\\);?\\s*/g, '').length > 0;
}

// Die beiden Zustaende, die es noch als Fenster gibt - der Zugang ist
// seit 12.0 eine Karte in den Einstellungen und kein Assistent mehr.
var faelle = [
  ['ollama', function(){ S.ollama.running = true; S.ollama.has_model = false; }],
  ['ollama', function(){ S.ollama.running = true; S.ollama.has_model = true; }]
];
faelle.forEach(function(f, i){
  S.token = {present: true, valid: true, expired: false, missing: [],
             account: 'a@example.com', expires_in_minutes: 620};
  f[1]();
  closeWizard(f[0]);
  openWizard(f[0]);
  var html = modal.innerHTML, wo = 'Fall ' + i + ': ';

  pruefe(zaehle(html, 'class="modal-zu"') === 1, wo + 'kein oder mehrfaches Schliesskreuz');
  pruefe(zaehle(html, 'class="act"') === 1, wo + 'nicht genau ein primaerer Knopf');
  pruefe(zaehle(html, 'class="ghost"') <= 1, wo + 'mehr als ein sekundaerer Knopf');

  // Es gibt genau einen Ausgang: das Kreuz. Jeder andere Knopf muss etwas tun -
  // ein zweiter Knopf, der nur schliesst, ist derselbe Ausgang zweimal.
  modal.querySelectorAll('button.act, button.ghost').forEach(function(k){
    pruefe(handelt(k), wo + 'Knopf schliesst nur: "' + k.onclickCode + '"');
  });

  // Und wo es einen sekundaeren gibt, steht der primaere davor.
  if(zaehle(html, 'class="ghost"'))
    pruefe(html.indexOf('class="act"') < html.indexOf('class="ghost"'),
           wo + 'sekundaerer Knopf steht vor dem primaeren');
});
console.log('OK');
"""


def test_alle_assistenten_tragen_dieselben_knoepfe():
    _in_node(PRUEFUNG_MODALE)


# A modal window takes over the page. Whoever uses no mouse must still get
# in, around, and back out.
PRUEFUNG_TASTATUR = GRUNDZUSTAND + """
var ausloeser = {focus: function(){ document.activeElement = this; }, name: 'Kachel'};
document.activeElement = ausloeser;

// Der Zugang ist seit 12.0 eine Karte; Fenster gibt es nur noch fuer
// Ollama - an ihm haengt also die Tastaturbedienung.
S.ollama.has_model = false;
openWizard('ollama');
pruefe(document.activeElement !== ausloeser, 'Fokus blieb ausserhalb des Dialogs');
pruefe(document.activeElement.className.indexOf('act') >= 0, 'Fokus nicht auf der Handlung');

// Neuzeichnen darf den Fokus nicht wegreissen.
var drin = document.activeElement;
S.ollama.running = true;
openWizard('ollama');
pruefe(document.activeElement === drin, 'Neuzeichnen riss den Fokus weg');

// Tab am Ende springt an den Anfang, Shift+Tab am Anfang ans Ende.
var liste = modal.querySelectorAll(
  'button, [href], textarea, input, select, summary, [tabindex]:not([tabindex="-1"])');
pruefe(liste.length >= 3, 'Zu wenige fokussierbare Elemente: ' + liste.length);
liste[liste.length - 1].focus();
pruefe(taste('Tab').verhindert, 'Tab am Ende nicht abgefangen');
pruefe(document.activeElement === liste[0], 'Tab am Ende verliess den Dialog');
pruefe(taste('Tab', {shiftKey: true}).verhindert, 'Shift+Tab am Anfang nicht abgefangen');
pruefe(document.activeElement === liste[liste.length - 1], 'Shift+Tab verliess den Dialog');

// Strg+Enter loest die primaere Handlung aus, ohne dorthin tabben zu muessen.
global.gespeichert = false;
global.recheckOllama = function(){ global.gespeichert = true; };
taste('Enter', {ctrlKey: true});
pruefe(global.gespeichert === true, 'Strg+Enter loeste die Handlung nicht aus');

// ESC schliesst - und gibt den Fokus zurueck, wo er herkam.
pruefe(taste('Escape').verhindert, 'ESC nicht abgefangen');
pruefe(wizardOffen === null, 'ESC schloss den Dialog nicht');
pruefe(document.activeElement === ausloeser, 'Fokus kam nicht zurueck');

// Ist keiner offen, darf ESC nichts anfassen.
document.activeElement = ausloeser;
taste('Escape');
pruefe(document.activeElement === ausloeser, 'ESC wirkte ohne offenen Dialog');
console.log('OK');
"""


def test_assistent_ist_mit_der_tastatur_bedienbar():
    _in_node(PRUEFUNG_TASTATUR)


# The wizard offers both paths – the key stays preselected because it
# works without having to ask IT first.
PRUEFUNG_ANMELDEWAHL = GRUNDZUSTAND + """
// Der Status sagt, was folgt; der Weg und die eigenen Kennungen stehen in
// den Einstellungen, die Vorgabe-Kennung in der Umgebung.
S.auth = {signed_in: false, account: null, own_registration: false, device: null};
S.default_client_id = 'std';
KONFIG = Object.assign({}, KONFIG, {auth_mode: 'token', client_id: 'std',
                                    tenant: 'organizations'});
S.token.present = false;
zugangNeu();
var karte = document.getElementById('zugang-inhalt');
var html = karte.innerHTML;
pruefe(html.indexOf('name="authmode"') >= 0, 'Keine Auswahl der Anmeldewege');
pruefe(html.indexOf('value="token" checked') >= 0, 'Schluessel ist nicht vorausgewaehlt');
pruefe(html.indexOf('id="tok"') >= 0, 'Textfeld fuer den Schluessel fehlt');
pruefe(html.indexOf('Graph Explorer') >= 0, 'Der Schluesselweg wird nicht erklaert');

// Umschalten: dieselbe Karte, anderer Inhalt. Der Weg steht in den
// Einstellungen, nicht im Status - der sagt nur, was daraus folgt.
KONFIG = Object.assign({}, KONFIG, {auth_mode: 'login'});
zugangNeu();
html = karte.innerHTML;
pruefe(html.indexOf('value="login" checked') >= 0, 'Login nicht vorausgewaehlt');
pruefe(html.indexOf('id="tok"') < 0, 'Textfeld steht noch da');
pruefe(html.indexOf('id="au-client"') >= 0, 'Eigene Registrierung nicht erreichbar');

// Ein laufender Gerätecode ist das Einzige, was dann zaehlt.
S.auth.device = {code: 'ABCD-1234', url: 'https://ms.example/dev', done: false};
zugangNeu();
pruefe(karte.innerHTML.indexOf('ABCD-1234') >= 0, 'Der Code wird nicht angezeigt');

// Angemeldet: die Abmeldung ist der sekundaere Knopf, nicht der primaere.
S.auth.device = null; S.auth.signed_in = true; S.auth.account = 'a@b.c';
zugangNeu();
pruefe(karte.innerHTML.indexOf('a@b.c') >= 0, 'Konto wird nicht genannt');
var act = karte.querySelector('button.act'), ghost = karte.querySelector('button.ghost');
pruefe(act.onclickCode.indexOf('starteLogin') >= 0, 'Primaer ist nicht das Anmelden');
pruefe(ghost && ghost.onclickCode.indexOf('abmelden') >= 0, 'Abmelden fehlt');

// Eigene Registrierung: der Block steht offen, wenn eine eingetragen ist.
S.auth.own_registration = true;
KONFIG = Object.assign({}, KONFIG, {client_id: 'eigene-id'});
zugangNeu();
pruefe(karte.innerHTML.indexOf('value="eigene-id"') >= 0, 'Eigene Client-ID fehlt');
console.log('OK');
"""


def test_assistent_bietet_beide_anmeldewege():
    _in_node(PRUEFUNG_ANMELDEWAHL)


def test_adressbuch_und_rekonstruierte_termine_zeichnen():
    """Regression: a local variable `t` shadowed the translation function,
    drawBook threw, and because the promise had no catch, the address book
    stayed at "Loading…" forever."""
    _in_node(PRUEFUNG_ANSICHTEN)


def test_ladefehler_wird_angezeigt_statt_verschluckt():
    """Regression: the promise had no catch – an error during loading left
    the view at "Loading…" forever, without any hint."""
    _in_node(PRUEFUNG_LADEFEHLER)


def test_kalender_wird_erst_bei_bedarf_und_nach_neuaufbau_geholt():
    """The calendar data is several megabytes: fetch once, then again only
    when the build step has actually rewritten it."""
    _in_node(PRUEFUNG_KALENDER)


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------
def test_serve_oeffnet_den_browser_und_raeumt_auf(sandbox, with_ollama, monkeypatch):
    """serve() binds, starts schedule and MCP, opens the browser and cleans
    up again on quitting."""
    geoeffnet = []
    monkeypatch.setattr(app_mod.webbrowser, "open", lambda url: geoeffnet.append(url))
    a = app_mod.App(app_mod.load_config())

    def stop_gleich():
        ende = time.time() + 5
        while time.time() < ende and not geoeffnet:
            time.sleep(0.02)
        httpd_box[0].shutdown()

    httpd_box = []
    echtes_make = app_mod.make_server
    monkeypatch.setattr(app_mod, "make_server",
                        lambda app, port, host="127.0.0.1":
                        httpd_box.append(echtes_make(app, port, host)) or httpd_box[0])
    threading.Timer(0.05, stop_gleich).start()
    app_mod.serve(a, 0, open_browser=True)

    assert geoeffnet and geoeffnet[0].startswith("http://127.0.0.1:")
    assert a.scheduler.ident is not None                    # schedule thread ran
    assert a.scheduler.stop_event.is_set()                  # shutdown() cleaned up


# --------------------------------------------------------------------------
# Only one instance – and a way to quit it
# --------------------------------------------------------------------------
def test_laeuft_bereits_erkennt_die_eigene_instanz(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        assert app_mod.laeuft_bereits(port) is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_laeuft_bereits_bei_freiem_port(sandbox):
    import socket as _s
    with _s.socket() as sock:            # find a port and release it right away
        sock.bind(("127.0.0.1", 0))
        frei = sock.getsockname()[1]
    assert app_mod.laeuft_bereits(frei, timeout=0.5) is False


def test_laeuft_bereits_bei_fremdem_dienst(sandbox):
    """Something else can be listening on the port – that is not a second instance."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Fremd(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"hallo": "ich bin etwas anderes"}')

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Fremd)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert app_mod.laeuft_bereits(httpd.server_address[1], timeout=2) is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_serve_startet_keine_zweite_instanz(sandbox, with_ollama, monkeypatch):
    """Regression: every further double-click spawned a second instance on
    the next port – invisibly, because the app has no window."""
    a = app_mod.App(app_mod.load_config())
    erste = app_mod.make_server(a, 0)
    threading.Thread(target=erste.serve_forever, daemon=True).start()
    port = erste.server_address[1]
    geoeffnet = []
    monkeypatch.setattr(app_mod.webbrowser, "open", lambda url: geoeffnet.append(url))
    monkeypatch.setattr(app_mod, "make_server",
                        lambda *a, **k: pytest.fail("zweite Instanz gestartet"))
    try:
        zweite = app_mod.App(app_mod.load_config())
        assert app_mod.serve(zweite, port, open_browser=True) is None
        assert geoeffnet == [f"http://127.0.0.1:{port}/"]
    finally:
        erste.shutdown()
        erste.server_close()


def test_serve_mit_port_null_prueft_nicht(sandbox, with_ollama, monkeypatch):
    """Port 0 means "any free one" – there is nothing to detect there."""
    monkeypatch.setattr(app_mod, "laeuft_bereits",
                        lambda *a, **k: pytest.fail("darf nicht gefragt werden"))
    monkeypatch.setattr(app_mod.webbrowser, "open", lambda url: None)
    a = app_mod.App(app_mod.load_config())
    box = []
    echtes = app_mod.make_server
    monkeypatch.setattr(app_mod, "make_server",
                        lambda app, port, host="127.0.0.1":
                        box.append(echtes(app, port, host)) or box[0])
    threading.Timer(0.05, lambda: box[0].shutdown()).start()
    app_mod.serve(a, 0, open_browser=False)
    assert box


def test_main_reicht_argumente_an_serve_weiter(sandbox, monkeypatch):
    # sandbox: main() without --data-dir runs the layout move against the
    # app folder – never the real one from a test.
    gesehen = {}
    monkeypatch.setattr(app_mod, "serve",
                        lambda a, port, open_browser=True: gesehen.update(
                            port=port, browser=open_browser))
    monkeypatch.setattr(sys, "argv", ["app.py", "--port", "9001", "--no-browser"])
    app_mod.main()
    assert gesehen == {"port": 9001, "browser": False}


# --------------------------------------------------------------------------
# Login mode over HTTP
# --------------------------------------------------------------------------
def test_http_status_nennt_den_anmeldemodus(server):
    _, port = server
    code, r = call(port, "GET", "/api/v1/status")
    assert code == 200
    au = r["auth"]
    assert au["own_registration"] is False
    # The mode and the ids are settings, not state – the status says only
    # what follows from them.
    assert "mode" not in au and "client_id" not in au
    assert call(port, "GET", "/api/v1/config")[1]["config"]["auth_mode"] == "token"
    assert call(port, "GET", "/api/v1/app")[1]["default_client_id"] == app_mod.auth.STANDARD_CLIENT_ID


def test_http_modus_umschalten(server):
    a, port = server
    code, r = call(port, "PATCH", "/api/v1/config", {"auth_mode": "login"})
    assert code == 200 and r["config"]["auth_mode"] == "login"
    assert call(port, "GET", "/api/v1/config")[1]["config"]["auth_mode"] == "login"

    # Unknown values fall back to the path that always works.
    call(port, "PATCH", "/api/v1/config", {"auth_mode": "quatsch"})
    assert a.cfg["auth_mode"] == "token"


def test_http_eigene_registrierung_speichern(server):
    a, port = server
    code, r = call(port, "PATCH", "/api/v1/config",
                   {"client_id": " eigene-id ", "tenant": "contoso.example"})
    assert code == 200 and r["config"]["client_id"] == "eigene-id"
    st = call(port, "GET", "/api/v1/status")[1]["auth"]
    assert st["own_registration"] is True
    assert call(port, "GET", "/api/v1/config")[1]["config"]["tenant"] == "contoso.example"
    assert a.cfg["tenant"] == "contoso.example"


def test_http_abmelden_loescht_den_cache(server, sandbox, monkeypatch):
    a, port = server
    geleert = []
    monkeypatch.setattr(app_mod.auth, "cache_leeren",
                        lambda: geleert.append(True) or True)
    a.device_login = {"code": "X", "done": False}
    code, r = call(port, "DELETE", "/api/v1/access/session")
    assert code == 204
    assert geleert == [True]
    assert a.device_login is None, "abgebrochene Anmeldung blieb stehen"


def test_login_starten_meldet_code_und_wartet(sandbox, monkeypatch, no_ollama):
    """The code must be there immediately – the waiting runs alongside,
    otherwise the UI would stall until someone is done on their phone."""
    fertig = threading.Event()

    class FakeDevice:
        def __init__(self, scopes, client=None, mandant=None):
            self.scopes = scopes

        def start(self):
            return {"code": "ABCD-1234", "url": "https://ms.example/dev",
                    "expires_in": 900}

        def warten(self):
            fertig.wait(5)
            return True, ""

    monkeypatch.setattr(app_mod.auth, "DeviceLogin", FakeDevice)
    a = app_mod.App()
    ok, daten = a.login_starten()
    assert ok and daten["code"] == "ABCD-1234" and daten["done"] is False
    assert a.auth_status()["device"]["code"] == "ABCD-1234"

    fertig.set()
    ende = time.time() + 5
    while not (a.device_login or {}).get("done") and time.time() < ende:
        time.sleep(0.02)
    assert a.device_login["done"] and a.device_login["ok"]


def test_login_fordert_nur_noetige_rechte(sandbox, monkeypatch, no_ollama):
    """Demanding more than the export needs would be bad style towards the
    person who is supposed to consent."""
    gesehen = {}

    class FakeDevice:
        def __init__(self, scopes, client=None, mandant=None):
            gesehen["scopes"] = scopes

        def start(self):
            return {"code": "X", "url": "u", "expires_in": 60}

        def warten(self):
            return True, ""

    monkeypatch.setattr(app_mod.auth, "DeviceLogin", FakeDevice)
    a = app_mod.App()
    a.cfg["outlook_categories"] = ["mail"]
    a.cfg["teams_categories"] = []
    a.login_starten()
    kurz = {s.rsplit("/", 1)[-1] for s in gesehen["scopes"]}
    assert kurz == {"Mail.Read", "User.Read"}


def test_anmeldemodus_geht_an_die_unterprozesse(sandbox):
    """Otherwise the app would keep a setting the export knows nothing about."""
    cfg = _cfg_mit_kategorien(auth_mode="login", client_id="eigene-id",
                              tenant="contoso.example")
    env = app_mod.build_steps(cfg, {"outlook": True})[0]["env"]
    assert env["GRAPH_AUTH"] == "login"
    assert env["GRAPH_CLIENT_ID"] == "eigene-id"
    assert env["GRAPH_TENANT"] == "contoso.example"


def test_leere_registrierung_wird_nicht_weitergereicht(sandbox):
    """An empty field means "Microsoft's application", not "client id is ''"."""
    cfg = _cfg_mit_kategorien()
    env = app_mod.build_steps(cfg, {"outlook": True})[0]["env"]
    assert env["GRAPH_AUTH"] == "token"
    assert "GRAPH_CLIENT_ID" not in env and "GRAPH_TENANT" not in env


def test_login_modus_laeuft_ohne_eingefuegten_schluessel(sandbox, no_ollama, monkeypatch):
    """In login mode the cache carries – a missing key must no longer
    prevent a run."""
    a = app_mod.App(_cfg_mit_kategorien())
    monkeypatch.setattr(a.jobs, "start", lambda steps, label, **kw: True)
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "")

    ok, why = a.launch({"outlook": True}, label="job.export")
    assert not ok and schluessel(why) == "srv.notoken"

    a.cfg["auth_mode"] = "login"
    ok, _ = a.launch({"outlook": True}, label="job.export")
    assert ok, "Login-Modus verlangt weiterhin einen Schlüssel"


# --------------------------------------------------------------------------
# Conversation history: a single hit often says too little
# --------------------------------------------------------------------------
def test_http_thread_reicht_die_auswertung_durch(server, monkeypatch):
    a, port = server

    class FakeSuche:
        STATE = {"semantic": False}

        @staticmethod
        def get_thread(thread, limit=50):
            return {"thread": thread, "count": 2, "limit": limit,
                    "messages": [{"uid": "u1"}, {"uid": "u2"}]}

    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    code, r = call(port, "GET", "/api/v1/threads?key=tix:abc")
    assert code == 200 and r["count"] == 2 and r["thread"] == "tix:abc"

    # An excessive limit must not fetch half the database.
    assert call(port, "GET", "/api/v1/threads?key=x&limit=9999")[1]["limit"] == 200


def test_http_thread_ohne_index(server, monkeypatch):
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: None)
    a.search.error = {"k": "cal.missing", "v": {}}
    code, r = call(port, "GET", "/api/v1/threads?key=x")
    # Auch hier: die Sammlung heisst `items`, wie in der Antwort, die
    # diese Ablehnung ersetzt.
    assert code == 503 and r["items"] == [] and r["error"]


PRUEFUNG_VERLAUF = GRUNDZUSTAND + """
KANN_VERLAUF = true;      // otherwise set from store.features
// The conversation comes with one fetch and stands as one fold under the
// content; the facts beyond the hit come with another and are drawn on
// arrival. A page can never have a conversation – nothing is fetched.
global.ANTWORT = {count: 3, items: [
  {uid: 'a', date: '2025-06-01', who: 'Alice', title: 'Frage', uri: 'o365://outlook/a.eml'},
  {uid: 'b', date: '2025-06-02', who: 'Bob', title: 'RE: Frage', uri: 'o365://outlook/b.eml'},
  {uid: 'c', date: '2025-06-03', who: 'Alice', title: 'AW: Frage', uri: 'o365://outlook/c.eml'}]};
var geholt = [];
global.fetch = function(pfad){
  geholt.push(String(pfad));
  return Promise.resolve({json: function(){
    if(String(pfad).indexOf('/api/v1/threads') === 0) return Promise.resolve(global.ANTWORT);
    if(String(pfad).indexOf('/api/v1/documents/facts') === 0) return Promise.resolve({kind: 'outlook', text: 'Voller Text',
      from: {name: 'Alice', mail: 'alice@example.com'}, to: [{name: 'Bob', mail: 'bob@example.com'}], cc: [],
      attachments: [{name: 'Rechnung.pdf', size: 2048}]});
    return Promise.resolve(statusGeruest());
  }});
};
renderHits({results: [
  {uid: 'a', key: 'mail:a', title: 'Frage', who: 'Alice', date: '2025-06-01 09:00', source: 'outlook', source_label: 'Mail',
   context: 'Inbox/Kunden', preview: 'Text', uri: 'o365://outlook/a.eml', thread: 'tix:abc', cases: [{id: 1, name: 'Nordwind'}]},
  {uid: 'b', title: 'Einzeln', who: 'Bob', date: '2025-06-02', source: 'outlook', source_label: 'Mail',
   preview: 'Text', uri: 'o365://outlook/b.eml', thread: null},
  {uid: 'c', title: 'Seite', who: '', date: '2025-06-03', source: 'onenote', source_label: 'OneNote',
   preview: 'Text', uri: 'o365://onenote/n/Seite.html', thread: 'chat:x'}
], count: 3, backend: 'bm25'});
waehleTreffer(0);
var html = document.getElementById('detail-inhalt').innerHTML;
pruefe(html.indexOf('class="zaehler"') >= 0 && html.indexOf('waehleTreffer(1)') >= 0, 'Kein Blaettern im Kopf');
pruefe(html.indexOf('zeigeVerlauf') < 0, 'Der alte Verlaufsknopf steht noch da');
pruefe(html.indexOf("filterPerson('Alice')") >= 0 && html.indexOf("filterOrdner('outlook', 'Inbox/Kunden')") >= 0,
       'Person oder Ordner sind keine Filterlinks: ' + html.slice(0, 400));
setTimeout(function(){
  var kasten = document.getElementById('detail-verlauf').innerHTML;
  pruefe(kasten.indexOf('<details class="verlauf">') === 0, 'Das Gespraech ist keine Falte: ' + kasten.slice(0, 80));
  pruefe(kasten.indexOf('RE: Frage') >= 0, 'Antwort fehlt im Verlauf');
  pruefe(kasten.indexOf('2025-06-01') < kasten.indexOf('2025-06-03'), 'Verlauf steht nicht in zeitlicher Reihenfolge');
  pruefe(kasten.indexOf('vzeile dies') >= 0, 'Die offene Nachricht ist nicht markiert');
  pruefe(kasten.indexOf('gespraechInFall(1, 0)') >= 0, 'Der Rest des Gespraechs laesst sich nicht in den Fall holen');
  // The facts and the text are redrawn in place once the fetch is in – the
  // DOM stub keeps them under their own ids.
  var d = document.getElementById('detail-fakten').innerHTML;
  pruefe(d.indexOf('bob@example.com') >= 0 && d.indexOf('Rechnung.pdf') >= 0, 'Fakten aus /api/v1/documents/facts fehlen: ' + d.slice(0, 300));
  pruefe(document.getElementById('detail-text').innerHTML.indexOf('Voller Text') >= 0, 'Der volle Text fehlt');
  // no fold without a conversation, and none at a page even with a thread id
  waehleTreffer(1);
  var vorher = geholt.length;
  waehleTreffer(2);
  setTimeout(function(){
    pruefe(document.getElementById('detail-verlauf').innerHTML === '', 'Gespraech an einer Seite');
    pruefe(!geholt.slice(vorher).some(function(p){ return p.indexOf('/api/v1/threads') === 0; }), 'Die Seite fragt nach dem Gespraech');
    console.log('OK');
  }, 20);
}, 20);
"""


def test_verlauf_klappt_im_detail_auf():
    _in_node(PRUEFUNG_VERLAUF)


PRUEFUNG_PILLEN = GRUNDZUSTAND + """
// Every filter is a pill: it says its name, then its value, and a × clears it.
zeigeFilterstand();
pruefe(document.getElementById('pw-person').textContent === t('search.pill.person'), 'Leere Pille nennt nicht den Filter');
pruefe(document.getElementById('px-person').classList.contains('hide'), 'Leere Pille traegt ein ×');
document.getElementById('f-person').value = 'Alice';
document.getElementById('f-source').value = 'outlook';
document.getElementById('f-from').value = '2026-09-01';
document.getElementById('f-gone').checked = true;
zeigeFilterstand();
pruefe(document.getElementById('p-person').classList.contains('on'), 'Personenpille leuchtet nicht');
pruefe(document.getElementById('pw-person').textContent === 'Alice', 'Personenpille nennt den Wert nicht');
pruefe(!document.getElementById('px-person').classList.contains('hide'), 'Gesetzte Pille ohne ×');
pruefe(document.getElementById('pf-person').classList.contains('hide'), 'Gesetzte Pille zeigt noch den Pfeil');
pruefe(document.getElementById('p-source').classList.contains('on'), 'Quellenpille leuchtet nicht');
pruefe(document.getElementById('pw-date').textContent.indexOf('2026') >= 0, 'Datumspille ohne Datum: ' + document.getElementById('pw-date').textContent);
pruefe(document.getElementById('p-weg').classList.contains('on') && document.getElementById('px-weg').classList.contains('hide'), 'Geloescht-Pille');
pruefe(document.getElementById('filter-stand').textContent.indexOf('4') >= 0, 'Zahl der Filter fehlt: ' + document.getElementById('filter-stand').textContent);
// × clears one filter, the others stay
pillLeeren('person');
pruefe(document.getElementById('f-person').value === '' && document.getElementById('f-source').value === 'outlook', '× leert den falschen Filter');
pillLeeren('date');
pruefe(document.getElementById('f-from').value === '' && document.getElementById('pw-date').textContent === t('search.pill.date'), 'Datum nicht geleert');
pillLeeren('source');
pruefe(document.getElementById('f-source').value === 'all' && !document.getElementById('p-source').classList.contains('on'), 'Quelle nicht geleert');
// a quick range fills both dates and closes the popover
datumSchnell(7);
pruefe(document.getElementById('f-from').value < document.getElementById('f-to').value, 'Schnellwahl setzt kein Intervall');
pruefe(document.getElementById('po-date').classList.contains('hide'), 'Popover bleibt offen');
// one popover at a time, Escape closes it
pillAuf('source');
pruefe(!document.getElementById('po-source').classList.contains('hide') && document.getElementById('p-source').classList.contains('offen'), 'Popover geht nicht auf');
pillAuf('typ');
pruefe(document.getElementById('po-source').classList.contains('hide') && !document.getElementById('po-typ').classList.contains('hide'), 'Zwei Popover offen');
taste('Escape');
pruefe(document.getElementById('po-typ').classList.contains('hide'), 'Escape schliesst nicht');
// a filter the index cannot offer hides with its control
document.getElementById('f-party').classList.add('hide');
zeigeFilterstand();
pruefe(document.getElementById('p-party').classList.contains('hide'), 'Pille ohne Filter sichtbar');
document.getElementById('f-party').classList.remove('hide');
document.getElementById('f-party').value = 'external';
zeigeFilterstand();
pruefe(!document.getElementById('p-party').classList.contains('hide') && document.getElementById('pw-party').textContent === t('search.party.external'), 'Beteiligtenpille');
// choosing in the popover sets the control and searches nothing
var gesucht = 0;
global.fetch = function(pfad){ if(String(pfad).indexOf('/search?') >= 0) gesucht++; return Promise.resolve({json: function(){ return Promise.resolve({}); }}); };
pillSetzen('party', 'internal');
pruefe(document.getElementById('f-party').value === 'internal' && gesucht === 0, 'Pille sucht von selbst');
pruefe(document.getElementById('po-party').classList.contains('hide'), 'Popover bleibt nach der Wahl offen');
console.log('OK');
"""


PRUEFUNG_MAILFILTER = GRUNDZUSTAND + """
// The four lines of a mail are one pill – and it only stands where it can
// be answered: with Mail or all sources, and an index that knows the lines.
KANN_MAIL = true;
// With every source a line only mail has would quietly turn the search
// into a mail search – so the pill stands only with Mail.
document.getElementById('f-source').value = 'all';
zeigeFilterstand();
pruefe(document.getElementById('p-mail').classList.contains('hide'), 'Mailpille steht bei allen Quellen');
document.getElementById('f-source').value = 'outlook';
zeigeFilterstand();
pruefe(!document.getElementById('p-mail').classList.contains('hide'), 'Mailpille fehlt bei der Quelle Mail');
document.getElementById('f-mail-from').value = 'alice@nordwind.example';
document.getElementById('f-mail-cc').value = 'carla';
zeigeFilterstand();
pruefe(document.getElementById('p-mail').classList.contains('on'), 'Gesetzte Mailpille leuchtet nicht');
pruefe(document.getElementById('pw-mail').textContent.indexOf('alice@nordwind.example') >= 0 &&
       document.getElementById('pw-mail').textContent.indexOf('carla') >= 0,
       'Mailpille nennt ihre Zeilen nicht: ' + document.getElementById('pw-mail').textContent);
pruefe(document.getElementById('mi-from').classList.contains('on') &&
       !document.getElementById('mi-to').classList.contains('on'), 'Die gesetzte Zeile leuchtet nicht');
// Two lines are two filters – plus the source that makes them possible.
pruefe(document.getElementById('filter-stand').textContent.indexOf('3') >= 0,
       'Zahl der Filter: ' + document.getElementById('filter-stand').textContent);
// Another source: the pill is gone, not greyed – and nothing of it is used.
document.getElementById('f-source').value = 'onedrive';
zeigeFilterstand();
pruefe(document.getElementById('p-mail').classList.contains('hide'), 'Mailpille bleibt bei fremder Quelle stehen');
pruefe(document.getElementById('f-mail-from').value === 'alice@nordwind.example', 'Die Eingabe wurde weggeworfen');
pruefe(kriterienAusForm().mail_from === '', 'Eine unerreichbare Zeile steht in den Kriterien');
pruefe(document.getElementById('filter-stand').textContent.indexOf('1') >= 0,
       'Die versteckte Zeile zaehlt mit: ' + document.getElementById('filter-stand').textContent);
// Back to Mail: what was typed is back, too.
document.getElementById('f-source').value = 'outlook';
zeigeFilterstand();
pruefe(!document.getElementById('p-mail').classList.contains('hide'), 'Mailpille kommt nicht zurueck');
pruefe(kriterienAusForm().mail_cc === 'carla', 'Kriterien ohne die Kopie-Zeile');
// The search asks the versioned surface, with the lines as their own names.
var gefragt = '';
global.fetch = function(pfad){
  gefragt = String(pfad);
  return Promise.resolve({ok: true, status: 200, json: function(){ return Promise.resolve({items: []}); }});
};
doSearch(0);
pruefe(gefragt.indexOf('/api/v1/search?') >= 0, 'Die Suche geht nicht ueber v1: ' + gefragt);
pruefe(gefragt.indexOf('mail_from=alice%40nordwind.example') >= 0 &&
       gefragt.indexOf('mail_cc=carla') >= 0, 'Die Mailzeilen fehlen in der Anfrage: ' + gefragt);
// The × clears all four at once, they are one filter.
pillLeeren('mail');
pruefe(document.getElementById('f-mail-from').value === '' && document.getElementById('f-mail-cc').value === '',
       'Das × leert nur eine Zeile');
pruefe(!document.getElementById('p-mail').classList.contains('on'), 'Geleerte Mailpille leuchtet weiter');
// Stored criteria come back into the fields.
kriterienAnwenden({source: 'outlook', mail_to: 'bob@nordwind.example', mail_bcc: 'dana'});
pruefe(document.getElementById('f-mail-to').value === 'bob@nordwind.example' &&
       document.getElementById('f-mail-bcc').value === 'dana', 'Kriterien kommen nicht in die Felder');
console.log('OK');
"""


def test_mailfilter_steht_nur_wo_er_beissen_kann():
    _in_node(PRUEFUNG_MAILFILTER)


def test_filterpillen_nennen_wert_und_leeren_sich():
    _in_node(PRUEFUNG_PILLEN)


PRUEFUNG_DETAIL_ARTEN = GRUNDZUSTAND + """
// One detail per kind: only the facts that kind is known by, a value that
// is also a filter as a link, nothing about what the kind cannot have.
KANN_TYP = true;
var FAKTEN = {
  'k:1': {kind: 'kalender', start: '2026-09-16 14:00', end: '2026-09-16 15:00', location: 'Raum 3.12',
          organiser: {name: 'Alice Beispiel', mail: 'alice@example.com'}, attendees: [{name: 'Dana', mail: 'dana@nordwind.example'}], text: 'Agenda'},
  'c:1': {kind: 'kontakte', org: 'Nordwind GmbH', role: 'Einkauf', emails: ['dana@nordwind.example'], phones: ['+49 30 000000'], text: ''},
  'd:1': {kind: 'datei', ext: 'xlsx', size: 86016, modified: '2026-09-14 11:20'},
  'p:1': {kind: 'planner', assigned: ['Bob Baumeister'], due: '2026-09-20', state: 'inprogress', checklist: {done: 3, total: 5},
          attachments: ['Angebot.pdf'], text: 'Beschreibung', comments: [{who: 'Bob', when: '2026-09-14 09:15', text: 'Kommentar'}]},
  't:1': {kind: 'todo', due: '2026-09-11', state: 'completed', completed: '2026-09-11', steps: {done: 2, total: 2}, linked: ['Mail: Re: Angebot'], text: 'Notiz'},
  'm:1': {kind: 'outlook', from: {name: 'Alice Beispiel', mail: 'alice@nordwind.example'},
          to: [{name: 'Bob Baumeister', mail: 'bob@nordwind.example'}], cc: [],
          bcc: [{name: 'Erik Einkauf', mail: 'erik@nordwind.example'}],
          date: '2026-09-15 09:40', folder: 'E-Mail/Gesendete Elemente', attachments: [], text: 'Angebot'}
};
// The status poll must not take the type filter away again
function geruest(){ var st = statusGeruest();
  BESTAND = Object.assign({}, BESTAND, {store: {exists: true, semantic: false,
    built_at: null, features: ['ext', 'key']}});
  return st; }
global.fetch = function(pfad){
  var m = /uid=([^&]+)/.exec(String(pfad));
  return Promise.resolve({json: function(){
    return Promise.resolve(String(pfad).indexOf('/api/v1/documents/facts') === 0 ? (FAKTEN[decodeURIComponent(m[1])] || {}) : geruest()); }});
};
renderHits({results: [
  {uid: 'k:1', title: 'Budget', who: 'Alice Beispiel', who_mail: 'alice@example.com', date: '2026-09-16 14:00', source: 'kalender', context: 'kalender/Arbeit', preview: 'x', uri: 'o365://outlook/kalender/Arbeit/a.ics'},
  {uid: 'c:1', title: 'Dana Dienstleister', who: 'Nordwind GmbH', date: '', source: 'kontakte', context: 'kontakte', preview: 'x', uri: 'o365://outlook/kontakte/d.vcf'},
  {uid: 'd:1', title: 'Angebot_2026.xlsx', who: '', date: '2026-09-14 11:20', source: 'datei', root: 'sharepoint', context: 'Nordwind/Dokumente', preview: 'x', uri: 'o365://sharepoint/Nordwind/Dokumente/Angebot_2026.xlsx'},
  {uid: 'p:1', title: 'Angebot vorbereiten', who: 'Bob Baumeister', date: '2026-09-14 09:15', source: 'planner', context: 'Nordwind board/Angebote', preview: 'x', uri: 'o365://planner/x/board.html'},
  {uid: 't:1', title: 'Dana anrufen', who: '', date: '2026-09-11 16:40', source: 'todo', context: 'Aufgaben', preview: 'x', uri: 'o365://todo/x/list.html', gone: '2026-09-12T09:00:00'},
  {uid: 'm:1', title: 'Angebot', who: 'Alice Beispiel', who_mail: 'alice@nordwind.example', date: '2026-09-15 09:40', source: 'outlook', context: 'E-Mail/Gesendete Elemente', preview: 'x', uri: 'o365://outlook/inbox/a.eml'}
], count: 6, backend: 'bm25'});
// Head and actions from the hit, the facts and the text redrawn in place
// once the fetch is in – the DOM stub keeps those under their own ids.
function detail(i){
  KANN_TYP = true;          // the first status poll may have reset it meanwhile
  waehleTreffer(i);
  return new Promise(function(f){ setTimeout(function(){
    f(document.getElementById('detail-inhalt').innerHTML + document.getElementById('detail-fakten').innerHTML +
      document.getElementById('detail-text').innerHTML); }, 10); });
}
detail(0).then(function(d){
  pruefe(d.indexOf(t('search.fact.when')) >= 0 && d.indexOf('Raum 3.12') >= 0 && d.indexOf('dana@nordwind.example') >= 0, 'Terminfakten: ' + d.slice(0, 400));
  pruefe(d.indexOf('tag extern') >= 0, 'Externer Teilnehmer nicht markiert');
  pruefe(d.indexOf('15:00') >= 0, 'Das Ende fehlt');
  pruefe(d.indexOf(t('search.fact.from')) < 0, 'Termin traegt eine Mailzeile');
  return detail(1);
}).then(function(d){
  pruefe(d.indexOf('Nordwind GmbH') >= 0 && d.indexOf('+49 30 000000') >= 0, 'Kontaktfakten: ' + d.slice(0, 400));
  pruefe(d.indexOf('<dt>' + t('search.fact.date') + '</dt>') < 0, 'Kontakt zeigt fremde Fakten');
  return detail(2);
}).then(function(d){
  pruefe(d.indexOf("filterTyp('xlsx')") >= 0, 'Dateityp ist kein Filterlink');
  pruefe(d.indexOf(t('search.fact.size')) >= 0 && d.indexOf("filterOrdner('sharepoint'") >= 0, 'Dateifakten: ' + d.slice(0, 400));
  pruefe(d.indexOf('indexiert') < 0 && d.indexOf('indexed') < 0, 'Die Datei erklaert, was sie nicht kann');
  return detail(3);
}).then(function(d){
  pruefe(d.indexOf(t('search.state.inprogress')) >= 0 && d.indexOf('Angebot.pdf') >= 0, 'Plannerfakten: ' + d.slice(0, 400));
  pruefe(d.indexOf('class="kommentar"') >= 0 && d.indexOf('Kommentar') >= 0, 'Kommentare fehlen');
  return detail(4);
}).then(function(d){
  pruefe(d.indexOf(t('search.state.completed')) >= 0 && d.indexOf('Mail: Re: Angebot') >= 0, 'To-Do-Fakten: ' + d.slice(0, 400));
  pruefe(d.indexOf('tag weg') >= 0, 'Geloescht-Marke fehlt im Kopf');
  pruefe(d.indexOf('waehleTreffer(3)') >= 0 && d.indexOf(t('search.detail.next')) >= 0, 'Kopf ohne Blaettern');
  return detail(5);
}).then(function(d){
  // The blind copy: the search filters by it, so the mail has to show it –
  // and the empty Cc line stays away, as every empty fact does.
  pruefe(d.indexOf('<dt>' + t('search.fact.bcc') + '</dt>') >= 0 && d.indexOf('erik@nordwind.example') >= 0,
         'Blindkopie fehlt im Detail: ' + d.slice(0, 400));
  pruefe(d.indexOf('<dt>' + t('search.fact.cc') + '</dt>') < 0, 'Leere Kopie-Zeile steht da');
  console.log('OK');
});
"""


def test_jede_art_hat_ihr_eigenes_detail():
    _in_node(PRUEFUNG_DETAIL_ARTEN)


PRUEFUNG_TUER = GRUNDZUSTAND + """
// The door (11.3): Search opens with the last searches and the saved ones
// as rows; a search run takes their place; the calendar hands over with
// the shown month or week as the date range; its month name is a picker.
var geholt = [];
global.fetch = function(pfad){
  geholt.push(String(pfad));
  return Promise.resolve({json: function(){
    if(String(pfad).indexOf('/api/v1/searches/history') === 0) return Promise.resolve({retention: '90', items: [
      {when: '2026-09-16T09:12:00', hits: 3, criteria: {q: '"budget frame" 2026', mode: 'text', source: 'outlook'}},
      {when: '2026-09-15T17:40:00', hits: 42, criteria: {q: 'Nordwind', mode: 'text', source: 'all'}},
      {when: '2026-09-14T10:00:00', hits: 4, criteria: {q: '', mode: 'text', source: 'kalender'}},
      {when: '2026-09-13T10:00:00', hits: 1, criteria: {q: 'vier', mode: 'text', source: 'all'}}]});
    if(String(pfad).indexOf('/api/v1/searches/saved') === 0) return Promise.resolve({items: [
      {id: 1, name: 'Invoices 2026', criteria: {q: 'Rechnung', mode: 'text', source: 'outlook'}, last_run: '2026-09-14T10:00:00', hits: 61, case: null}]});
    return Promise.resolve(statusGeruest());
  }});
};
aktiverTab = 'suche';
sicht('treffer');
pruefe(!document.getElementById('suche-anfang').classList.contains('hide'), 'Leerzustand fehlt vor der ersten Suche');
pruefe(document.getElementById('treffer-split').classList.contains('hide'), 'Trefferliste steht neben dem Leerzustand');
setTimeout(function(){
  var h = document.getElementById('anfang-historie').innerHTML, g = document.getElementById('anfang-gespeichert').innerHTML;
  pruefe(h.split('class="hist"').length - 1 === 3, 'Nicht die letzten drei Suchen: ' + (h.split('class="hist"').length - 1));
  pruefe(h.indexOf('historieLauf(0)') >= 0 && h.indexOf('budget frame') >= 0 && h.indexOf('42') >= 0, 'Zeile ohne Lauf oder Zahl: ' + h.slice(0, 300));
  pruefe(h.indexOf(t('search.criteria.browse')) >= 0, 'Suche ohne Woerter nicht benannt');
  pruefe(g.indexOf('gespeichertLauf(0)') >= 0 && g.indexOf('Invoices 2026') >= 0 && g.indexOf('61') >= 0, 'Gespeicherte Zeile fehlt: ' + g.slice(0, 300));
  // a search ran: the list takes the place, for good
  doSearch(0);
  pruefe(document.getElementById('suche-anfang').classList.contains('hide'), 'Leerzustand bleibt nach der Suche');
  pruefe(!document.getElementById('treffer-split').classList.contains('hide'), 'Trefferliste fehlt nach der Suche');
  sicht('kalender'); sicht('treffer');
  pruefe(document.getElementById('suche-anfang').classList.contains('hide'), 'Leerzustand kommt zurueck');
  // the calendar hands over with the shown month as the date range
  events = [{ts: 1750000000, te: 1750003600, st: 'confirmed', title: 'x'}];
  calMode = 'month'; cursor = new Date(2026, 8, 16);
  kalenderSuchen();
  pruefe(document.getElementById('f-source').value === 'kalender', 'Quelle nicht gesetzt');
  pruefe(document.getElementById('f-from').value === '2026-09-01' && document.getElementById('f-to').value === '2026-09-30',
         'Monat nicht als Datum gesetzt: ' + document.getElementById('f-from').value + ' ' + document.getElementById('f-to').value);
  pruefe(geholt.some(function(p){ return p.indexOf('/api/v1/search') === 0 && p.indexOf('source=kalender') > 0; }), 'Suche nicht gestartet');
  calMode = 'week'; cursor = new Date(2026, 8, 16);   // a Wednesday
  kalenderSuchen();
  pruefe(document.getElementById('f-from').value === '2026-09-14' && document.getElementById('f-to').value === '2026-09-20', 'Woche nicht als Datum gesetzt');
  // the picker: a dot on months with appointments, months outside the archive greyed
  events = [{ts: Date.UTC(2025, 5, 15) / 1000, st: 'confirmed', title: 'a'}, {ts: Date.UTC(2026, 8, 16) / 1000, st: 'confirmed', title: 'b'}];
  cursor = new Date(2026, 8, 1);
  kalWahlAuf();
  var w = document.getElementById('kalWahl').innerHTML;
  pruefe(!document.getElementById('kalWahl').classList.contains('hide'), 'Picker geht nicht auf');
  pruefe(w.split('<button').length - 1 === 14, 'Nicht zwoelf Monate und zwei Pfeile: ' + (w.split('<button').length - 1));
  pruefe(w.indexOf('kalWaehle(2026, 8)') >= 0 && w.indexOf('class="on') >= 0, 'Der gezeigte Monat ist nicht markiert');
  pruefe((w.match(/class="punkt"/g) || []).length === 1, 'Nicht genau ein Monat mit Termin in 2026: ' + (w.match(/class="punkt"/g) || []).length);
  pruefe(w.indexOf('kalWaehle(2026, 11)') < 0, 'Ein Monat nach dem Archivende ist waehlbar');
  kalWahlJahrSchritt(-1);
  w = document.getElementById('kalWahl').innerHTML;
  pruefe(w.indexOf('2025') >= 0 && w.indexOf('kalWaehle(2025, 5)') >= 0 && w.indexOf('kalWaehle(2025, 4)') < 0, 'Jahr 2025: Juni fehlt oder Mai waehlbar');
  kalWaehle(2025, 5);
  pruefe(cursor.getFullYear() === 2025 && cursor.getMonth() === 5, 'Monat nicht gewaehlt');
  pruefe(document.getElementById('kalWahl').classList.contains('hide'), 'Picker bleibt offen');
  console.log('OK');
}, 20);
"""


def test_die_tuer_beginnt_mit_dem_letzten_und_der_kalender_uebergibt():
    _in_node(PRUEFUNG_TUER)


def test_die_tuer_hat_vier_wege_und_eine_suche():
    """Explore archive (11.3): the strip of ways first, the search row inside
    the Search way, and the three views without a search of their own – one
    hand-over button each."""
    seite = app_mod.seite()
    block = seite[seite.index('<section id="tab-suche"'):seite.index('<section id="tab-faelle"')]
    assert block.index('id="sichten"') < block.index('id="sicht-treffer"') < block.index('class="suchzeile"')
    assert block.count('data-sicht=') == 4 and 'treffer-zahl' not in block
    for kennung in ('kalSuchen', 'kbSuchen', 'dateien-suchen', 'kalWahl', 'kalMonat', 'suche-anfang'):
        assert f'id="{kennung}"' in block, kennung
    assert 'id="kbQ"' not in block, "the address book grew a search of its own again"


PRUEFUNG_GELOESCHT = GRUNDZUSTAND + """
// Gelöschtes ist am Treffer erkennbar, ohne die Liste zu erschlagen.
renderHits({results: [
  {uid: 'a', title: 'Weg', who: 'Alice', date: '2025-06-01', source_label: 'Mail',
   preview: 'Text', uri: 'o365://outlook/a.eml', gone: '2026-03-12T09:00:00'},
  {uid: 'b', title: 'Da', who: 'Bob', date: '2025-06-02', source_label: 'Mail',
   preview: 'Text', uri: 'o365://outlook/b.eml', gone: null}
], count: 2, backend: 'bm25'});
var html = document.getElementById('results').innerHTML;
var marken = html.split('tag weg').length - 1;
pruefe(marken === 1, 'Erwartet genau eine Markierung, gezaehlt: ' + marken);
pruefe(html.indexOf('12.03.26') >= 0, 'Der Zeitpunkt fehlt im Tooltip: ' + html.slice(0,300));
console.log('OK');
"""


def test_geloeschtes_ist_am_treffer_erkennbar():
    _in_node(PRUEFUNG_GELOESCHT)


def test_http_search_reicht_den_filter_durch(server, monkeypatch):
    a, port = server
    gesehen = {}

    class FakeSuche:
        STATE = {"semantic": False}

        @staticmethod
        def browse_messages(**kw):
            gesehen.update(kw)
            return {"count": 0, "results": []}

    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    call(port, "GET", "/api/v1/search?gone=1")
    assert gesehen["only_gone"] is True
    call(port, "GET", "/api/v1/search")
    assert gesehen["only_gone"] is False
    # the attachment switch (13.1) travels the same way
    call(port, "GET", "/api/v1/search?attachments=1")
    assert gesehen["with_attachments"] is True
    call(port, "GET", "/api/v1/search")
    assert gesehen["with_attachments"] is False
    # the parties filter travels too, with the internal domains the engine
    # needs – the setting, else the signed-in account's domain
    a.cfg["internal_domains"] = "nordwind.example"
    call(port, "GET", "/api/v1/search?party=external")
    assert gesehen["party"] == "external" and FakeSuche.STATE["internal_domains"] == "nordwind.example"
    call(port, "GET", "/api/v1/search?party=x")
    assert gesehen["party"] == "all"


PRUEFUNG_ALTER_INDEX = GRUNDZUSTAND + """
// Ein Index aus einer aelteren Fassung kennt Verlauf und Loeschungen nicht.
// Dann bietet die Oberflaeche sie gar nicht erst an, statt in einen Fehler
// laufen zu lassen.
var st = statusGeruest();
BESTAND = Object.assign({}, BESTAND, {store: Object.assign(
  {}, BESTAND.store, {features: []})});
renderStatus(st);
pruefe(document.getElementById('p-weg').classList.contains('hide'),
       'Filter wird trotz altem Index angeboten');
pruefe(KANN_VERLAUF === false, 'Verlauf gilt trotz altem Index als moeglich');
pruefe(document.getElementById('f-party').classList.contains('hide') && KANN_ADRESSEN === false,
       'Beteiligtenfilter oder Adressen trotz altem Index angeboten');

BESTAND = Object.assign({}, BESTAND, {store: Object.assign(
  {}, BESTAND.store, {features: ['gone', 'thread']})});
renderStatus(st);
pruefe(!document.getElementById('p-weg').classList.contains('hide'),
       'Filter fehlt trotz passendem Index');
pruefe(KANN_VERLAUF === true, 'Verlauf fehlt trotz passendem Index');
// The address columns of 11.1: the parties filter and the People view's addresses hang on them.
BESTAND = Object.assign({}, BESTAND, {store: Object.assign(
  {}, BESTAND.store, {features: ['gone', 'thread', 'key', 'who_mail', 'domains']})});
renderStatus(st);
pruefe(!document.getElementById('f-party').classList.contains('hide') && KANN_ADRESSEN === true,
       'Beteiligtenfilter oder Adressen fehlen trotz passendem Index');
// The mail lines of 13.0: without them the pill is absent, not greyed.
document.getElementById('f-source').value = 'outlook';
zeigeFilterstand();
pruefe(KANN_MAIL === false && document.getElementById('p-mail').classList.contains('hide'),
       'Mailfilter trotz Index ohne Zeilen angeboten');
BESTAND = Object.assign({}, BESTAND, {store: Object.assign(
  {}, BESTAND.store, {features: ['gone', 'thread', 'key', 'who_mail', 'domains', 'mail_lines']})});
renderStatus(st);
pruefe(KANN_MAIL === true && !document.getElementById('p-mail').classList.contains('hide'),
       'Mailfilter fehlt trotz passendem Index');
console.log('OK');
"""


def test_alter_index_bietet_die_neuen_filter_nicht_an():
    _in_node(PRUEFUNG_ALTER_INDEX)


# --------------------------------------------------------------------------
# Source files: what belongs in the browser and what in the program next to it
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name,inhalt,ctype", [
    ("mail.eml", b"From: a@b.c\nSubject: X\n\nText\n", "message/rfc822"),
    ("termin.ics", b"BEGIN:VCALENDAR\nEND:VCALENDAR\n", "text/calendar; charset=utf-8"),
    ("alice.vcf", b"BEGIN:VCARD\nEND:VCARD\n", "text/vcard; charset=utf-8"),
])
def test_quelldatei_wird_heruntergeladen(sandbox, monkeypatch, name, inhalt, ctype):
    """An .eml as raw text in a browser window is of no use to anyone – in
    the mail program, though, it is a mail with attachments."""
    ordner = sandbox / "outlook_export"
    ordner.mkdir()
    (ordner / name).write_bytes(inhalt)

    class FakeSuche:
        STATE = {}

        @staticmethod
        def _resolve_source(root, pfad):
            return (ordner / pfad), None

    a = app_mod.App(app_mod.load_config())
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    httpd = app_mod.make_server(a, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        con = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        con.request("GET", f"/api/v1/files/content?root=outlook&path={name}")
        resp = con.getresponse()
        koerper = resp.read()
        assert resp.status == 200 and koerper == inhalt
        assert resp.getheader("Content-Type") == ctype
        disp = resp.getheader("Content-Disposition")
        assert disp == f'attachment; filename="{name}"', disp
        con.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("roh,erwartet", [
    ('a"b.eml', "a_b.eml"),
    ("x\r\ny.eml", "x__y.eml"),
    ("../../etc/passwd", "passwd"),
    ("", "datei"),
    ("normal.eml", "normal.eml"),
])
def test_dateiname_im_header_ist_unbedenklich(roh, erwartet):
    """The name ends up in a header – a quotation mark or a line break in it
    would let it break apart."""
    assert app_mod._sicherer_name(roh) == erwartet


# --------------------------------------------------------------------------
# Hits per page
# --------------------------------------------------------------------------
def test_http_trefferzahl_wird_begrenzt(server, monkeypatch):
    a, port = server
    gesehen = {}

    class FakeSuche:
        STATE = {"semantic": False}

        @staticmethod
        def browse_messages(**kw):
            gesehen.update(kw)
            return {"count": 0, "results": []}

    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    # `limit` is the name the whole API pages with; the engine's own is `k`.
    call(port, "GET", "/api/v1/search?limit=50")
    assert gesehen["k"] == 51         # one above the page: that is has_more
    call(port, "GET", "/api/v1/search?limit=99999")     # not half the database
    assert gesehen["k"] == 101
    call(port, "GET", "/api/v1/search?limit=7")
    assert gesehen["k"] == 8


@pytest.mark.parametrize("wert,erwartet", [
    (50, 50), (5, 5), (100, 100),
    (1, 5), (500, 100),          # out of range: clamped to the edge
    ("quatsch", 20),             # unusable: default stays
])
def test_config_trefferzahl(server, wert, erwartet):
    a, port = server
    call(port, "PATCH", "/api/v1/config", {"search_results": wert})
    assert a.cfg["search_results"] == erwartet


PRUEFUNG_SEITENGROESSE = GRUNDZUSTAND + """
KANN_VERLAUF = false;
var gefragt = [];
global.fetch = function(pfad){
  gefragt.push(String(pfad));
  return Promise.resolve({json: function(){
    return Promise.resolve(String(pfad).indexOf('/api/v1/search') === 0
      ? {results: [], count: 0, backend: 'bm25'} : statusGeruest());
  }});
};
S.config = {search_results: 50};
doSearch(0);
pruefe(gefragt[0].indexOf('limit=50') >= 0, 'Einstellung wirkt nicht: ' + gefragt[0]);

// Und das Blaettern springt genauso weit – sonst uebersprungen oder doppelt.
var treffer = [];
for(var i = 0; i < 50; i++) treffer.push({uid: 'u' + i, title: 'T', who: 'W',
  date: '2025-06-01', source_label: 'Mail', preview: 'x', uri: 'o365://outlook/a.eml'});
offset = 50;
renderHits({results: treffer, count: 50, backend: 'bm25'});
var pager = document.getElementById('pager').innerHTML;
pruefe(pager.indexOf('doSearch(0)') >= 0, 'Zurueck springt falsch: ' + pager);
pruefe(pager.indexOf('doSearch(100)') >= 0, 'Weiter springt falsch: ' + pager);

// Ohne Angabe bleibt es bei 20.
S.config = {};
pruefe(trefferProSeite() === 20, 'Vorgabe ist nicht 20');
console.log('OK');
"""


def test_seitengroesse_wirkt_auf_suche_und_blaettern():
    _in_node(PRUEFUNG_SEITENGROESSE)


# --------------------------------------------------------------------------
# Moving the data folder
#
# It cannot be set in app_config.json – that file itself lives inside it.
# Hence a pointer at the default location.
# --------------------------------------------------------------------------
@pytest.fixture
def standardort(tmp_path, monkeypatch):
    """Bend standard_data_dir() into the sandbox."""
    ort = tmp_path / "standard"
    ort.mkdir()
    monkeypatch.setattr(app_mod, "standard_data_dir", lambda: ort)
    # Everything the app would touch lives there too – an App() built in
    # such a test must never write its runs.db into the project folder.
    monkeypatch.setattr(app_mod, "WURZEL", ort)
    monkeypatch.setattr(app_mod, "HEIM", ort)
    monkeypatch.setattr(app_mod, "BASE", ort)
    monkeypatch.setattr(app_mod, "STORE_PFAD", ort / app_mod.STORE_DIR)
    monkeypatch.setattr(app_mod, "CONFIG_FILE", ort / "app_config.json")
    monkeypatch.setattr(app_mod, "TOKEN_FILE", ort / "gx_token.txt")
    for _n in ("MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR"):
        monkeypatch.delenv(_n, raising=False)
    return ort


def test_standardort_ist_der_heimatordner(standardort):
    assert app_mod.data_dir() == standardort


def test_altbestand_pinnt_den_datenordner(standardort, monkeypatch):
    """Upgrade from 6.x: if the export folders still sit flat in the app
    folder and a data_dir was never decided, the configuration points there
    from now on – nothing is moved. Fresh installs get the separation."""
    monkeypatch.setattr(app_mod, "HEIM", standardort)
    monkeypatch.setattr(app_mod, "CONFIG_FILE",
                        standardort / "app_config.json")
    # The function reassigns module globals – monkeypatch restores them.
    monkeypatch.setattr(app_mod, "BASE", app_mod.BASE)
    monkeypatch.setattr(app_mod, "STORE_PFAD", app_mod.STORE_PFAD)
    monkeypatch.setattr(app_mod, "_ALT_GEPINNT", False)
    monkeypatch.setattr(app_mod.settings, "load",
                        lambda path=None: json.loads(
                            (standardort / "app_config.json").read_text(
                                encoding="utf-8"))
                        if (standardort / "app_config.json").exists() else {})

    # Fresh install: no old folders, nothing gets pinned.
    assert app_mod.altbestand_pinnen() is False

    (standardort / app_mod.TEAMS_DIR).mkdir()
    assert app_mod.altbestand_pinnen() is True
    cfg = json.loads((standardort / "app_config.json").read_text(
        encoding="utf-8"))
    assert cfg["data_dir"] == str(standardort)
    assert app_mod.BASE == standardort

    # Idempotent – and an explicit "default" (empty value) wins.
    assert app_mod.altbestand_pinnen() is False
    cfg["data_dir"] = ""
    (standardort / "app_config.json").write_text(json.dumps(cfg),
                                                 encoding="utf-8")
    assert app_mod.altbestand_pinnen() is False,         "ausdrücklicher Standard wurde überstimmt"


def test_app_liest_seine_pfade_aus_dem_heimatordner(standardort, tmp_path,
                                                    monkeypatch):
    """The bundled app read its configuration from wherever the modules
    sit – the unpacked archive – and never saw the data and index folders
    the user had set: both fell back to the default on every restart. The
    source checkout hid it, because there module folder and home folder are
    the same. Every in-process read must name the home folder's file."""
    import settings
    for n in ("MUNIMENTUM_HOME", "MUNIMENTUM_DATA_DIR"):
        monkeypatch.delenv(n, raising=False)
    settings.reset()
    monkeypatch.setattr(app_mod, "CONFIG_FILE", standardort / "app_config.json")
    (standardort / "app_config.json").write_text(json.dumps(
        {"data_dir": str(tmp_path / "bulk"), "index_dir": str(tmp_path / "ix")}),
        encoding="utf-8")
    # No stubbing of settings.load here on purpose – that is the point.
    assert settings.config_path() != standardort / "app_config.json"
    daten, store = app_mod._split_pfade(standardort)
    assert daten == (tmp_path / "bulk").resolve()
    assert store == (tmp_path / "ix").resolve()
    assert app_mod.alter_zeiger() is None           # data_dir is set – no pointer talk
    monkeypatch.setattr(app_mod, "HEIM", standardort)
    (standardort / app_mod.TEAMS_DIR).mkdir()
    assert app_mod.altbestand_pinnen() is False     # decided already – no pin


def test_split_pfade_vorgaben_und_konfiguration(standardort, monkeypatch,
                                                tmp_path):
    """Without override: data/ and rag_store/ under the home folder, freely
    configurable; with override (tests, --data-dir): flat in one place."""
    monkeypatch.setattr(app_mod.settings, "load", lambda path=None: {})
    daten, store = app_mod._split_pfade(standardort)
    assert daten == standardort / app_mod.DATEN_UNTERORDNER
    assert store == standardort / app_mod.STORE_DIR

    monkeypatch.setattr(app_mod.settings, "load", lambda path=None: {
        "data_dir": str(tmp_path / "bulk"), "index_dir": str(tmp_path / "ix")})
    daten, store = app_mod._split_pfade(standardort)
    assert daten == (tmp_path / "bulk").resolve()
    assert store == (tmp_path / "ix").resolve()

    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(tmp_path))
    daten, store = app_mod._split_pfade(tmp_path)
    assert daten == tmp_path and store == tmp_path / app_mod.STORE_DIR


def test_umgebung_schlaegt_den_standardort(standardort, tmp_path, monkeypatch):
    anders = tmp_path / "env"
    anders.mkdir()
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(anders))
    assert app_mod.data_dir() == anders.resolve()


def test_ordner_ohne_schreibrecht_wird_abgelehnt(tmp_path):
    """Better to refuse now than at the next start: the setting one would
    use to take it back would live exactly there."""
    gesperrt = tmp_path / "gesperrt"
    gesperrt.mkdir()
    gesperrt.chmod(0o500)
    try:
        ziel, fehler = app_mod.pruefe_datenordner(gesperrt / "unten")
        assert ziel is None and fehler
    finally:
        gesperrt.chmod(0o700)


def test_leerer_ordner_wird_abgelehnt():
    assert app_mod.pruefe_datenordner("  ")[0] is None


def test_ordner_wird_angelegt(tmp_path):
    ziel, fehler = app_mod.pruefe_datenordner(tmp_path / "neu" / "tiefer")
    assert fehler is None and ziel.is_dir()
    assert not (ziel / ".schreibprobe").exists(), "Probe blieb liegen"


def test_http_datenordner_setzen(server, standardort, tmp_path):
    a, port = server
    ziel = tmp_path / "extern"
    code, r = call(port, "PATCH", "/api/v1/storage", {"data_dir": str(ziel)})
    assert code == 200 and r["restart_required"] is True
    assert a.cfg["data_dir"] == str(ziel.resolve())
    # The app does NOT switch over while running – BASE goes to every
    # subprocess as its working directory, possibly mid-export.
    assert call(port, "GET", "/api/v1/app")[1]["data_dir"] != str(ziel)

    # The index has its own path; empty means back to the default.
    code, r = call(port, "PATCH", "/api/v1/storage",
                   {"index_dir": str(tmp_path / "ix")})
    assert code == 200 and r["restart_required"] is True
    assert a.cfg["index_dir"] == str((tmp_path / "ix").resolve())
    # The log names the folder that changed – an index change was logged
    # as the data folder, which read as if the wrong one had been saved.
    zeilen = [z["text"] for z in a.jobs.lines if isinstance(z["text"], dict)]
    assert zeilen[-1]["k"] == "srv.indexdir.set"
    assert zeilen[-1]["v"]["path"] == str((tmp_path / "ix").resolve())
    code, r = call(port, "PATCH", "/api/v1/storage", {"index_dir": ""})
    assert code == 200 and a.cfg["index_dir"] == ""
    # Saving the same values again says nothing – the settings save posts
    # both paths on every click.
    n = len(a.jobs.lines)
    call(port, "PATCH", "/api/v1/storage", {"data_dir": str(ziel), "index_dir": ""})
    assert len(a.jobs.lines) == n


def test_http_datenordner_ablehnen(server, standardort, tmp_path):
    a, port = server
    kaputt = tmp_path / "datei-statt-ordner"
    kaputt.write_text("x", encoding="utf-8")
    code, r = call(port, "PATCH", "/api/v1/storage", {"data_dir": str(kaputt)})
    assert code == 400 and r["error"]
    assert not a.cfg.get("data_dir"), "kaputter Wert wurde trotzdem gemerkt"
    # The refusal reaches the log too, not only the small field.
    letzte = [z for z in a.jobs.lines if isinstance(z["text"], dict)][-1]
    assert letzte["level"] == "err" and letzte["text"]["k"] == r["error"]["k"]


def test_vorhandener_ordner_ohne_schreibrecht_wird_abgelehnt(tmp_path):
    """Only the write probe covers this case: mkdir(exist_ok=True) succeeds
    on an existing folder even when nobody may write into it. Without the
    probe the pointer would aim there and the app could no longer save
    anything at the next start."""
    gesperrt = tmp_path / "nur_lesen"
    gesperrt.mkdir()
    gesperrt.chmod(0o500)
    try:
        ziel, fehler = app_mod.pruefe_datenordner(gesperrt)
        assert ziel is None and fehler, "Ordner ohne Schreibrecht durchgewinkt"
    finally:
        gesperrt.chmod(0o700)


# --------------------------------------------------------------------------
# The answer speaks the language of the UI
#
# It is negotiated exactly like the page itself: setting beats browser
# language. Without a test this would break silently – the answer would
# still arrive, just in German for someone who reads French.
# --------------------------------------------------------------------------
def _antwort_sprache(server, monkeypatch, accept=None, eingestellt="auto"):
    a, port = server
    a.cfg["language"] = eingestellt
    gesehen = {}

    class FakeSuche:
        STATE = {"semantic": True}

        @staticmethod
        def search_messages(**kw):
            return {"results": [{"uid": "u1", "title": "T", "who": "W",
                                 "date": "2025-06-01", "uri": "o365://outlook/a.eml"}]}

        @staticmethod
        def get_document(uid):
            return {"text": "Inhalt"}

    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    monkeypatch.setattr(a, "ollama", lambda: {"running": True, "has_chat_model": True})

    def fake_stream(query, quellen, model, ollama, lang="de", *rest, **kw):
        gesehen["lang"] = lang
        yield {"text": "ok"}

    monkeypatch.setattr(app_mod.answer, "stream", fake_stream)

    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    kopf = {"Content-Type": "application/json"}
    if accept:
        kopf["Accept-Language"] = accept
    con.request("QUERY", "/api/v1/answer", json.dumps({"q": "Frage"}), kopf)
    con.getresponse().read()
    con.close()
    return gesehen.get("lang")


@pytest.mark.parametrize("accept,erwartet", [
    ("fr-CH,fr;q=0.9", "fr"),
    ("en-US,en;q=0.9", "en"),
    ("de-DE,de;q=0.9", "de"),
    (None, "de"),                      # no header, the default
    ("kl-GL", "de"),                   # unknown: do not guess
])
def test_antwort_folgt_der_browsersprache(server, monkeypatch, accept, erwartet):
    assert _antwort_sprache(server, monkeypatch, accept) == erwartet


def test_eingestellte_sprache_schlaegt_den_browser(server, monkeypatch):
    """Whoever sets the UI to French does not want the answer in English
    just because the browser says so."""
    assert _antwort_sprache(server, monkeypatch, "en-US,en;q=0.9", "fr") == "fr"


def test_prompt_verlangt_die_passende_sprache():
    """The rule is written in the same language as the desired answer – a
    small model then follows it more reliably."""
    import answer
    assert "Deutsch" in answer.system_prompt("de")
    assert "English" in answer.system_prompt("en")
    assert "français" in answer.system_prompt("fr")


# --------------------------------------------------------------------------
# Every ID selector must hit an element
#
# Reported from the field: "Month" and "Reconstructed" in the calendar could
# no longer be clicked. The cause was the tab rework – #tab-kalender became
# #sicht-kalender, but two querySelectorAll kept the old name. The selection
# hit nothing, no click handler was ever attached, and "Week" only looked
# active because the class sits in the markup. No error in the console, no
# message: the button simply did nothing.
# --------------------------------------------------------------------------
def test_seite_liegt_als_datei_neben_dem_code():
    """The interface is a data file read from RES – the same place the
    bundle unpacks lang/ and openapi.yaml to. A wrong path here means the
    app starts blank, so the test reads it the way app.py does."""
    datei = Path(app_mod.RES) / "page.html"
    assert datei.exists()
    assert app_mod.seite() == datei.read_text(encoding="utf-8")
    assert app_mod.seite() is app_mod.seite()          # read once
    assert app_mod.seite().lstrip().lower().startswith("<!doctype html>")
    assert "/*__I18N__*/" in app_mod.seite() and "/*__STEPS__*/" in app_mod.seite()


def test_jeder_id_selektor_trifft_ein_element():
    # The page lives in page.html – that is where the JS is.
    quelle = app_mod.seite()
    # All '#id' selectors used in the JavaScript. Only the fully spelled-out
    # ones: '#cat-' + name is only assembled at runtime, nothing could be
    # said about it here.
    selektoren = set(re.findall(
        r"""querySelector(?:All)?\('#([\w-]+)[^']*'\s*\)""", quelle))
    # el('x') is the more common access and went unchecked. That is exactly
    # where it happened: after the search form rework, el('search-sub')
    # pointed at an element that no longer existed. In the browser that
    # throws, and renderStatus aborts mid-build – the tests' DOM stub, by
    # contrast, creates every ID on demand and noticed nothing.
    selektoren |= set(re.findall(r"""\bel\('([\w-]+)'\)""", quelle))
    assert selektoren, "keine ID-Selektoren gefunden – Muster kaputt?"
    # … against the IDs in the markup.
    vorhanden = set(re.findall(r'id="([\w-]+)"', quelle))
    fehlt = sorted(selektoren - vorhanden)
    # Checked statically rather than re-enacted in the DOM stub: that would
    # have to parse real markup, and this check covers every selector on the
    # page anyway instead of just the three calendar buttons.
    assert not fehlt, (
        f"Selektor trifft kein Element: {fehlt}. Der zugehörige Knopf tut dann "
        f"nichts, ohne dass irgendwo ein Fehler auftaucht.")


def test_seite_bringt_ihr_eigenes_symbol_mit():
    """Without it every browser collects a 404 on /favicon.ico – and in the
    bundle there would be no file to serve instead."""
    seite = app_mod.seite()
    assert 'rel="icon"' in seite
    assert "data:image/svg+xml" in seite, "Symbol als Datei statt eingebettet"


# --------------------------------------------------------------------------
# MCP entry: copy it, and the paths follow the data folder
# --------------------------------------------------------------------------
def test_mcp_eintrag_folgt_dem_datenordner(sandbox, monkeypatch, tmp_path):
    """The entry carries absolute paths – Claude starts it in an unknown
    working directory. So they have to move along."""
    for ordner in (tmp_path / "platte-a", tmp_path / "platte-b"):
        app_mod.set_data_dir(ordner)
        conf = app_mod.mcp_client_config(app_mod.load_config(), 8365)
        args = conf["stdio"]["mcpServers"]["munimentum"]["args"]
        genannt = args[args.index("--data-dir") + 1]
        assert Path(genannt).is_absolute()
        assert Path(genannt) == ordner.resolve(), args


def test_mcp_programmpfad_folgt_dem_datenordner_nicht(sandbox, tmp_path):
    """The program stays where it is – only the data moves."""
    app_mod.set_data_dir(tmp_path / "woanders")
    conf = app_mod.mcp_client_config(app_mod.load_config(), 8365)
    eintrag = conf["stdio"]["mcpServers"]["munimentum"]
    alles = " ".join([eintrag["command"], *eintrag["args"]])
    assert "mcp_server" in alles
    assert str(tmp_path / "woanders") not in alles.split("--data-dir")[0]


PRUEFUNG_KOPIEREN = GRUNDZUSTAND + """
var kopiert = [];
// node bringt ein eigenes navigator mit, und zwar nur mit Getter - eine
// schlichte Zuweisung liefe ins Leere.
var zwischenablage = {writeText: function(t){
  kopiert.push(t); return Promise.resolve();
}};
Object.defineProperty(global, 'navigator',
  {value: {clipboard: zwischenablage}, configurable: true, writable: true});
var kasten = document.getElementById('mcp-json');
kasten.textContent = '{"mcpServers": {}}';
var knopf = {textContent: 'Kopieren'};
kopiere('mcp-json', knopf);
pruefe(kopiert.length === 1, 'Nichts kopiert');
pruefe(kopiert[0] === '{"mcpServers": {}}', 'Falscher Inhalt: ' + kopiert[0]);

setTimeout(function(){
  pruefe(knopf.textContent === 'Kopiert', 'Keine Rueckmeldung: ' + knopf.textContent);

  // Schlaegt die Zwischenablage fehl, wird der alte Weg versucht statt still
  // nichts zu tun.
  var versucht = false;
  zwischenablage.writeText = function(){ return Promise.reject(new Error('nein')); };
  document.execCommand = function(){ versucht = true; return true; };
  kopiere('mcp-json', knopf);
  setTimeout(function(){
    pruefe(versucht, 'Kein Rueckfall auf den alten Weg');
    console.log('OK');
  }, 20);
}, 20);
"""


def test_mcp_eintrag_laesst_sich_kopieren():
    _in_node(PRUEFUNG_KOPIEREN)


# --------------------------------------------------------------------------
# Address book: two sources for the same question "who is this?"
# --------------------------------------------------------------------------
PRUEFUNG_ADRESSBUCH = GRUNDZUSTAND + """
contacts = [
  {title: 'Alice Example', org: 'Contoso', em: ['a@contoso.test'], tel: ['+49 1'],
   src: 'kontakte', root: 'outlook', rel: 'kontakte/a.vcf'},
  {title: 'Nur im Buch', org: 'Fabrikam', em: [], tel: [],
   src: 'kontakte', root: 'outlook', rel: 'kontakte/n.vcf'}
];
personen = [{name: 'Alice Example', messages: 42},
            {name: 'Bob Ausdemchat', messages: 7}];
personenGeladen = true;

function namen(){
  return (document.getElementById('kbBox').innerHTML.match(/Alice Example|Nur im Buch|Bob Ausdemchat/g) || [])
    .filter(function(v, i, a){ return a.indexOf(v) === i; }).sort();
}

bookF = 'all'; drawBook();
pruefe(namen().join(',') === 'Alice Example,Bob Ausdemchat,Nur im Buch',
       'Alle: ' + namen());
// Wer in beidem vorkommt, erscheint EINMAL – sonst stuende Alice doppelt da.
var html = document.getElementById('kbBox').innerHTML;
pruefe(html.split('Alice Example').length - 1 <= 2, 'Alice mehrfach im Buch');
pruefe(html.indexOf('42') >= 0, 'Nachrichtenzahl fehlt bei Alice');

bookF = 'contacts'; drawBook();
pruefe(namen().join(',') === 'Alice Example,Nur im Buch', 'Kontakte: ' + namen());

bookF = 'comm'; drawBook();
pruefe(namen().join(',') === 'Alice Example,Bob Ausdemchat', 'Kommunikation: ' + namen());
// Nur wer ausschliesslich aus der Kommunikation kommt, wird als solcher markiert.
var nurKomm = document.getElementById('kbBox').innerHTML;
pruefe(nurKomm.indexOf('herkunft') >= 0, 'Herkunft nicht gekennzeichnet');

// Klick auf eine Person fuehrt zu ihrer Kommunikation.
global.gesucht = [];
doSearch = function(off){ gesucht.push([document.getElementById('f-person').value, off]); };
zeigeKommunikation('Bob Ausdemchat');
pruefe(gesucht.length === 1 && gesucht[0][0] === 'Bob Ausdemchat',
       'Personenfilter nicht gesetzt: ' + JSON.stringify(gesucht));
pruefe(offeneSicht === 'treffer', 'Nicht zur Trefferliste gewechselt');
console.log('OK');
"""


def test_adressbuch_trennt_die_beiden_quellen():
    _in_node(PRUEFUNG_ADRESSBUCH)


# --------------------------------------------------------------------------
# Analytics: what the archive holds – and what is missing
# --------------------------------------------------------------------------
def _analytics_db(sandbox, spalten_neu=True):
    store = sandbox / "rag_store"
    store.mkdir(exist_ok=True)
    app_mod._ZAEHLUNG.clear()
    con = sqlite3.connect(store / "corpus.db")
    extra = ", thread TEXT, gone TEXT, att TEXT" if spalten_neu else ""
    con.execute(f"CREATE TABLE chunks(uid TEXT, seq INTEGER, src TEXT, ts REAL{extra})")
    con.execute("CREATE TABLE people(src TEXT, who TEXT, messages INTEGER, ppl TEXT)")
    werte = [("outlook:a:0", 0, "outlook", 1_000_000),
             ("outlook:a:0", 1, "outlook", 1_000_000),
             ("outlook:b:0", 0, "outlook", 2_000_000),
             ("teams:c:0", 0, "teams", 1_500_000)]
    if spalten_neu:
        con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                        [(*w, "tix:1" if i < 3 else "chat:x",
                          "2026-01-01" if i == 2 else None,
                          "Vertrag.pdf" if i == 0 else "")
                         for i, w in enumerate(werte)])
    else:
        con.executemany("INSERT INTO chunks VALUES (?,?,?,?)", werte)
    con.executemany("INSERT INTO people VALUES (?,?,?,?)",
                    [("outlook", "Alice", 3, ""), ("teams", "Bob", 1, "")])
    con.commit()
    con.close()
    return store


def test_analytics_zaehlt_nachrichten_nicht_textstellen(sandbox):
    import analytics_db
    store = _analytics_db(sandbox)
    (sandbox / "spiegel").mkdir(exist_ok=True)
    (sandbox / "spiegel" / "a.bin").write_bytes(b"x" * 7)
    a = analytics_db.baue(store, {"onedrive": sandbox / "spiegel"})
    assert a["groesse"]["onedrive"] == 7 and "index" in a["groesse"]
    assert a["grosse_dateien"][0]["pfad"] == "a.bin"
    assert a["komm"]["nachrichten"] == 3                # not 4 chunks
    assert {q["src"]: q["n"] for q in a["quellen"]} == {"outlook": 2, "teams": 1}
    assert a["komm"]["personen"] == 2
    assert a["komm"]["gespraeche"] == 2
    assert a["komm"]["mit_anhang"] == 1
    assert a["komm"]["verschwunden"] == 1
    assert a["komm"]["von"] == 1_000_000 and a["komm"]["bis"] == 2_000_000
    # Materialized: the next call only reads.
    assert analytics_db.lies(store)["komm"]["nachrichten"] == 3


def test_top_personen_zaehlen_ueber_die_quellen_hinweg(sandbox):
    """Reported: the same name appeared twice in the list.

    The people table keeps one row per (source, person). Whoever writes in
    Teams AND by mail therefore appeared twice – with a split count, which
    made both look wrong.
    """
    import analytics_db
    store = _analytics_db(sandbox)
    con = sqlite3.connect(store / "corpus.db")
    con.execute("INSERT INTO people VALUES ('teams', 'Alice', 40, '')")
    con.commit()
    con.close()
    analytics_db.baue(store, {})

    k = app_mod.analytics_daten(app_mod.load_config())
    namen = [pe["who"] for pe in k["top_personen"]]
    assert namen == ["Alice", "Bob"]                   # each person once
    assert namen.count("Alice") == 1
    assert k["top_personen"][0]["n"] == 43             # 3 from mail + 40 from Teams


def test_ausgelassene_personen_fehlen_in_der_auswertung(sandbox):
    """Otherwise you yourself sit at the top by a wide margin and say
    nothing about the exchange with others."""
    import analytics_db
    analytics_db.baue(_analytics_db(sandbox), {})
    cfg = app_mod.load_config()
    assert [pe["who"] for pe in
            app_mod.analytics_daten(cfg)["top_personen"]] == ["Alice", "Bob"]

    # Lowercase and padded with whitespace: the comparison ignores both.
    # Filtering happens on READ – a changed setting must not have to wait
    # for the next index run.
    cfg["analytics_skip"] = ["  alice "]
    assert [pe["who"] for pe in
            app_mod.analytics_daten(cfg)["top_personen"]] == ["Bob"]
    cfg["analytics_skip"] = []
    assert [pe["who"] for pe in
            app_mod.analytics_daten(cfg)["top_personen"]] == ["Alice", "Bob"]


def test_namensliste_je_zeile(sandbox):
    """Comma-separated would be wrong: "Beispiel, Alice" is not two people."""
    assert app_mod._clean_zeilen("Beispiel, Alice\nBob\n\n  Bob  ") \
        == ["beispiel, alice", "bob"]
    assert app_mod._clean_zeilen(["A", "a", ""]) == ["a"]
    assert app_mod._clean_zeilen("") == []


def test_analytics_sagt_weiss_ich_nicht_statt_null(sandbox):
    """An index from an older version does not know the columns. "0 with
    attachment" would be a claim, None is an answer."""
    import analytics_db
    a = analytics_db.baue(_analytics_db(sandbox, spalten_neu=False), {})
    assert a["komm"]["nachrichten"] == 3                # this still works
    assert a["komm"]["gespraeche"] is None
    assert a["komm"]["mit_anhang"] is None
    assert a["komm"]["verschwunden"] is None


def test_analytics_ohne_index(sandbox):
    k = app_mod.analytics_daten(app_mod.load_config())
    assert k["exists"] is False and k["top_personen"] == []


def test_groesse_zaehlt_und_findet_die_groessten(sandbox):
    import analytics_db
    ordner = sandbox / "gross"
    (ordner / "tief").mkdir(parents=True)
    (ordner / "a.bin").write_bytes(b"x" * 1000)
    (ordner / "tief" / "b.bin").write_bytes(b"y" * 500)
    gesamt, groesste = analytics_db._groesse(ordner)
    assert gesamt == 1500
    assert [(n, rel) for n, rel in groesste] == [(1000, "a.bin"),
                                                 (500, "tief/b.bin")]
    assert analytics_db._groesse(sandbox / "gibtsnicht") == (0, [])


def test_pruefschritt_braucht_einen_zugang(sandbox, no_ollama, monkeypatch):
    """The check queries the mailbox – without access it must not start."""
    a = app_mod.App()
    monkeypatch.setattr(a.jobs, "start", lambda steps, label, **kw: True)
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "")
    ok, why = a.launch({"check": True}, label="job.check")
    assert not ok and schluessel(why) == "srv.notoken"


def test_pruefschritt_ruft_outlook_mit_check(sandbox):
    cfg = _cfg_mit_kategorien()
    steps = app_mod.build_steps(cfg, {"check": True})
    assert [s["key"] for s in steps] == ["check"]
    assert "--check" in steps[0]["argv"]
    # The check gets the export's own environment – so "excluded" means
    # exactly what the export would leave out.
    assert steps[0]["env"]["EXPORT_CATEGORIES"] == "mail,calendar,contacts"
    assert "OUTLOOK_SINCE" in steps[0]["env"] and "SKIP_FOLDERS" in steps[0]["env"]


def test_pruefschritte_folgen_der_nutzung(sandbox):
    """Every check is asked for; the registry keeps only the ones whose
    source is in use – a source nobody exports costs no request."""
    alle = {e["anfrage"]: True for e in app_mod.steps_mod.REGISTRY
            if e["anfrage"].startswith("check") and e["anfrage"] != "check_archive"}
    keys = [s["key"] for s in app_mod.build_steps(_cfg_mit_kategorien(), alle)]
    assert keys == ["check", "check_teams"]
    cfg = _cfg_mit_kategorien(onedrive_enabled=True, todo_enabled=True,
                              onenote_enabled=True, planner_enabled=True,
                              planner_urls="https://planner.cloud.microsoft/webui/v1/plan/abc/view",
                              sharepoint_enabled=True,
                              sharepoint_urls="https://nordwind.sharepoint.com/sites/x",
                              sharepoint_pages_enabled=True,
                              sharepoint_pages_urls="https://nordwind.sharepoint.com/sites/x")
    keys = [s["key"] for s in app_mod.build_steps(cfg, alle)]
    assert keys == ["check", "check_teams", "check_onedrive", "check_sharepoint",
                    "check_pages", "check_planner", "check_todo", "check_onenote"]
    for s in app_mod.build_steps(cfg, alle):
        assert "--check" in s["argv"] or "--check-pages" in s["argv"]


PRUEFUNG_ANALYTICS = GRUNDZUSTAND + """
zeigeAnalytics({exists: true, built_at: '2026-09-01T10:00:00+00:00',
  komm: {nachrichten: 238408, gespraeche: 16545, mit_anhang: null,
         personen: 3860, verschwunden: 12,
         von: 1568851200, bis: 1789603200},
  quellen: [{src: 'teams', n: 196668}, {src: 'outlook', n: 36827}],
  dateien: {n: 0, pages: 0, onedrive: 0, sharepoint: 0, verschwunden: null},
  groesse: {teams: 3758096384, outlook: 28879134720, index: 966367641},
  pruefungen: {outlook_mail: {genutzt: true, bericht: {
    quelle: 'outlook_mail', einheit: 'mails', geprueft: '2026-08-10T20:00:00',
    stand: 'ganz', grund: null, da: 39994, offen: 6, ausgeschlossen: 15000,
    ausgeschlossen_einheit: 'mails', behalten: 12, wartend: 0,
    zeilen: [{pfad: 'E-Mail/Gesendete Elemente', da: 97, offen: 3}], fehler: []}}}});

var kpi = document.getElementById('ana-kpi').innerHTML;
pruefe(kpi.indexOf('238.408') >= 0, 'Nachrichtenzahl fehlt');
pruefe(kpi.indexOf('16.545') >= 0, 'Gespraeche fehlen');
// null heisst „weiss ich nicht“ – keinesfalls 0.
pruefe(kpi.indexOf('>0<') < 0, 'Unbekanntes wurde als 0 gezeigt');
pruefe(kpi.indexOf('–') >= 0, 'Unbekanntes nicht als Strich gezeigt');
var dat = document.getElementById('ana-kpi-dateien').innerHTML;
pruefe(dat.indexOf('GB') >= 0, 'Groesse fehlt: ' + dat.slice(0, 200));
// Ohne Spiegel: nur die Groessenkachel, keine leeren Datei-Kacheln.
pruefe((dat.match(/kpi-wert/g) || []).length === 1,
       'Leere Datei-Kacheln werden gezeigt: ' + dat.slice(0, 200));
pruefe(document.getElementById('ana-stand').textContent.length > 0,
       'Stand-Zeile fehlt');

var pruef = document.getElementById('ana-checks').innerHTML;
pruefe(pruef.indexOf('15.000') >= 0, 'Ausgeschlossenes nicht erklaert');
pruefe(pruef.indexOf('Archiv') < 0, 'Ausgeschlossener Ordner beim Namen genannt');
pruefe(pruef.indexOf('6 Mails noch nicht geholt') >= 0, 'Offenes nicht in Alltagssprache: ' + pruef.slice(0, 300));
// Nur der Ordner mit Offenem steht in der eingeklappten Tabelle.
var tab = pruef.split('<tbody>')[1] || '';
pruefe(tab.indexOf('Gesendete Elemente') >= 0, 'Offener Ordner fehlt in der Tabelle');
console.log('OK');
"""


def test_analytics_hat_einen_aktualisieren_knopf():
    """ladeAnalytics(true) bypasses the cache – exactly what the button
    calls; without it the tab showed stale numbers until a page reload."""
    assert 'onclick="ladeAnalytics(true)"' in app_mod.seite()
    assert 'data-i18n="ana.reload"' in app_mod.seite()


def test_analytics_zeigt_kennzahlen_und_trennt_ausgelassenes():
    _in_node(PRUEFUNG_ANALYTICS)


# Reported: the bars started at a different position in every row because
# each row was its own grid. Nothing can be compared that way - which is
# what bars are for.
PRUEFUNG_RANGLISTE = GRUNDZUSTAND + r"""
var h = rangListe([{name: 'kurz', n: 100},
                   {name: 'ein deutlich laengerer Name als der davor', n: 50},
                   {name: 'mittel', n: 25}]);

// EIN Raster fuer die ganze Liste, nicht eins je Zeile.
pruefe(h.split('class="rangliste"').length - 1 === 1, 'Mehr als ein Raster: ' + h);
pruefe(h.indexOf('class="rang"') < 0, 'Zeile bringt noch ihr eigenes Raster mit');
pruefe((h.match(/class="name"/g) || []).length === 3, 'Nicht drei Zeilen');
pruefe((h.match(/class="bal"/g) || []).length === 3, 'Nicht drei Balken');

// Die Breiten stehen weiter im Verhaeltnis zum groessten Wert.
var breiten = (h.match(/width:([\d.]+)%/g) || []).join(' ');
pruefe(breiten.indexOf('100.0%') >= 0, 'Der groesste Balken ist nicht voll');
pruefe(breiten.indexOf('50.0%') >= 0, 'Haelfte falsch berechnet: ' + breiten);
pruefe(breiten.indexOf('25.0%') >= 0, 'Viertel falsch berechnet: ' + breiten);

// Der volle Text bleibt am title, auch wenn die Spalte ihn abschneidet.
pruefe(h.indexOf('title="ein deutlich laengerer Name als der davor"') >= 0,
       'Voller Name nicht am title');
pruefe(rangListe([]) === '', 'Leere Liste erzeugt ein leeres Raster');
console.log('OK');
"""


def test_rangliste_teilt_sich_ein_raster():
    _in_node(PRUEFUNG_RANGLISTE)


def test_verschwundenes_ueber_die_zeit_ist_weg():
    """A bar for a single month says nothing – the tile with the total and
    the "Deleted" view remain."""
    assert "ana.geloescht.sub" not in app_mod.seite()
    # What remains: the tile with the total and the view in the search.
    assert "ana.gone" in app_mod.seite()
    assert 'id="f-gone"' in app_mod.seite()


# --------------------------------------------------------------------------
# Export list: what the next run would do, without starting it
# --------------------------------------------------------------------------
def _baum(sandbox, cfg, eintraege, mails=()):
    ordner = sandbox / app_mod.OUTLOOK_DIR
    folders_mod.speichere(ordner, eintraege)
    for pfad, anzahl in mails:
        (ordner / pfad).mkdir(parents=True, exist_ok=True)
        for i in range(anzahl):
            (ordner / pfad / f"m{i}.eml").write_text("x", encoding="utf-8")
    return ordner


def test_exportliste_rechnet_mit_den_regeln_aus_dem_formular(server, sandbox):
    """Whoever types a rule wants to check it before saving – the preview
    must not show the last saved state."""
    a, port = server
    _baum(sandbox, a.cfg,
          [{"id": "1", "pfad": "E-Mail/Posteingang", "name": "P", "elemente": 100},
           {"id": "2", "pfad": "E-Mail/Archiv", "name": "A", "elemente": 14000}],
          [("E-Mail/Archiv", 3), ("E-Mail/Weg", 4)])

    code, r = call(port, "QUERY", "/api/v1/sources/outlook/folder-plan",
                   {"folder_rules": "- E-Mail/Archiv/**", "skip_folders": ""})
    assert code == 200 and r["regeln"]
    assert [z["pfad"] for z in r["an"]] == ["E-Mail/Posteingang"]
    assert [z["pfad"] for z in r["aus"]] == ["E-Mail/Archiv"]
    assert r["aus"][0]["regel"] == "- E-Mail/Archiv/**"
    assert r["aus"][0]["archiv"] == 3            # skipped does not mean empty
    assert r["weg"] == [{"pfad": "E-Mail/Weg", "archiv": 4}]
    # Nothing was saved – the preview is a question, not a change.
    assert not a.cfg["folder_rules"]


def test_build_steps_traegt_die_neuen_umgebungen(sandbox):
    """9.0: window, start dates, rules, sweep interval and the two list
    syncs travel from the settings into the scripts' environment."""
    cfg = app_mod.load_config()
    cfg.update(outlook_categories=["mail", "calendar"], teams_categories=["group"],
               onedrive_enabled=True, sharepoint_enabled=True,
               planner_enabled=True, todo_enabled=True,
               outlook_since="2025-01-01", teams_since="2025-06-01",
               calendar_months_back=3, teams_rules="- group/Alt**",
               sharepoint_rules="- Nordwind/Dokumente/Archiv/**",
               todo_rules="+ Einkauf", planner_sweep_hours=12,
               sharepoint_urls="https://nordwind.sharepoint.com/sites/x",
               planner_urls="https://planner.cloud.microsoft/webui/v1/plan/abc/view")
    steps = {s["key"]: s for s in app_mod.build_steps(
        cfg, {"outlook": True, "teams": True, "onedrive": True, "sharepoint": True,
              "planner": True, "todo": True, "sync_teams": True, "sync_todo": True})}
    o = steps["outlook"]["env"]
    assert o["OUTLOOK_SINCE"] == "2025-01-01" and o["CALENDAR_MONTHS_BACK"] == "3"
    assert o["CALENDAR_FULL"] == "0" and o["EXPORT_CATEGORIES"] == "mail,calendar"
    assert steps["teams"]["env"]["TEAMS_RULES"] == "- group/Alt**"
    assert steps["teams"]["env"]["TEAMS_SINCE"] == "2025-06-01"
    assert steps["sharepoint"]["env"]["SHAREPOINT_RULES"].startswith("- Nordwind")
    assert steps["todo"]["env"]["TODO_RULES"] == "+ Einkauf"
    assert steps["planner"]["env"]["PLANNER_SWEEP_HOURS"] == "12"
    assert steps["teams_list"]["argv"][-2:] == ["--teams", app_mod.TEAMS_DIR]
    assert steps["teams_list"]["env"]["EXPORT_CATEGORIES"] == "group"
    assert steps["todo_lists"]["argv"][-2:] == ["--lists", app_mod.TODO_DIR]
    assert steps["todo_lists"]["env"]["TODO_RULES"] == "+ Einkauf"
    # The calendar's "read in full" button: only the calendar, window and
    # tokens ignored once – even when the category is not ticked.
    cfg["outlook_categories"] = ["mail"]
    voll = app_mod.build_steps(cfg, {"outlook": True}, calendar_full=True)
    assert voll[0]["env"]["EXPORT_CATEGORIES"] == "calendar"
    assert voll[0]["env"]["CALENDAR_FULL"] == "1" and voll[0]["env"]["SYNC_NOW"] == "1"


def test_http_run_kalender_vollstaendig(server, monkeypatch):
    """The button posts outlook + calendar_full; the run record then names
    the calendar as the only category that ran."""
    a, port = server
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.update(steps=steps, **kw) or True)
    code, r = call(port, "POST", "/api/v1/runs",
                   {"outlook": True, "calendar_full": True, "label": "job.calendar.full"})
    assert code == 202 and r["run"]
    (schritt,) = [s for s in gesehen["steps"] if s["key"] == "outlook"]
    assert schritt["env"]["CALENDAR_FULL"] == "1"
    assert gesehen["context"]["elements"]["outlook"] == ["calendar"]


QUELLEN_MIT_VOLLSYNC = ("outlook", "teams", "onedrive", "sharepoint", "planner",
                        "todo", "onenote")


def test_build_steps_full_sync_setzt_die_flags(sandbox):
    """"Force full sync": FULL_SYNC with SYNC_NOW alongside for every export
    step; Outlook reads the calendar in full too, with every configured
    category – not the calendar alone as the calendar button does."""
    cfg = app_mod.load_config()
    cfg.update(outlook_categories=["mail", "contacts"], teams_categories=["group"])
    steps = {s["key"]: s for s in app_mod.build_steps(
        cfg, {"outlook": True, "onedrive": True, "teams": True, "todo": True,
              "planner": True, "onenote": True, "sharepoint": True,
              "sharepoint_pages": True}, full_sync=True)}
    for key in ("outlook", "onedrive", "teams", "todo", "planner", "onenote",
                "sharepoint", "sharepoint_pages"):
        assert steps[key]["env"]["FULL_SYNC"] == "1", key
        assert steps[key]["env"]["SYNC_NOW"] == "1", key
    assert steps["outlook"]["env"]["CALENDAR_FULL"] == "1"
    assert steps["outlook"]["env"]["EXPORT_CATEGORIES"] == "mail,contacts"
    ohne = {s["key"]: s for s in app_mod.build_steps(cfg, {"onedrive": True}, sync_now=True)}
    assert "FULL_SYNC" not in ohne["onedrive"]["env"]
    assert "RESYNC" not in ohne["onedrive"]["env"]


def test_build_steps_resync_setzt_die_flags(sandbox):
    """"Fetch now" / "Fetch again": RESYNC with SYNC_NOW alongside for every
    export step – and never FULL_SYNC, a resync writes nothing over."""
    cfg = app_mod.load_config()
    cfg.update(outlook_categories=["mail", "contacts"], teams_categories=["group"])
    steps = {s["key"]: s for s in app_mod.build_steps(
        cfg, {"outlook": True, "onedrive": True, "teams": True, "todo": True,
              "planner": True, "onenote": True, "sharepoint": True,
              "sharepoint_pages": True}, resync=True)}
    for key in ("outlook", "onedrive", "teams", "todo", "planner", "onenote",
                "sharepoint", "sharepoint_pages"):
        assert steps[key]["env"]["RESYNC"] == "1", key
        assert steps[key]["env"]["SYNC_NOW"] == "1", key
        assert "FULL_SYNC" not in steps[key]["env"], key
    assert steps["outlook"]["env"].get("CALENDAR_FULL", "") != "1", "the window stays"


def test_http_run_resync(server, monkeypatch):
    """"Fetch again" posts the source with resync; the step carries the flag."""
    a, port = server
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.update(steps=steps, label=label, **kw) or True)
    code, r = call(port, "POST", "/api/v1/runs",
                   {"outlook": True, "resync": True, "index": True, "label": "job.resync"})
    assert code == 202 and r["run"]
    schritte = {s["key"]: s for s in gesehen["steps"]}
    assert schritte["outlook"]["env"]["RESYNC"] == "1"
    assert "FULL_SYNC" not in schritte["outlook"]["env"]
    assert "index" in schritte and gesehen["label"] == "job.resync"


def test_http_run_full_sync(server, monkeypatch):
    """The button posts the source with full_sync and its own label; the
    step carries the flag."""
    a, port = server
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.update(steps=steps, label=label, **kw) or True)
    code, r = call(port, "POST", "/api/v1/runs",
                   {"todo": True, "full_sync": True, "label": "job.full"})
    assert code == 202 and r["run"]
    (schritt, kette) = gesehen["steps"]
    assert schritt["key"] == "todo" and schritt["env"]["FULL_SYNC"] == "1"
    assert kette["key"] == "evidence"
    assert gesehen["label"] == "job.full"


def test_jede_quelle_hat_den_vollsync_knopf_unter_erweitert():
    """DESIGN.md §6: one *Force full sync* per source, a `.mini` in the
    `.aktionen` row of its *Advanced* group with an (i) of its own – never
    in the open part, never twice, and every source that tracks changes
    has one."""
    seite = app_mod.seite()
    assert seite.count('data-i18n="settings.full_sync"') == len(QUELLEN_MIT_VOLLSYNC)
    for key in QUELLEN_MIT_VOLLSYNC:
        start = seite.index(f'id="q-{key}"')
        knopf = seite.index(f"vollSync('{key}')", start)
        erweitert = seite.rfind('<details class="erweitert"', start, knopf)
        assert erweitert > start, f"{key}: the button is not under Advanced"
        naechster_block = seite.find('<details class="quelle-einst"', start + 1)
        assert naechster_block == -1 or knopf < naechster_block, f"{key}: button in another block"
        zeile = seite[seite.rfind('<div class="aktionen">', start, knopf):knopf]
        assert "</div>" not in zeile, f"{key}: the button is outside an .aktionen row"
        # Its (i) sits right beside it and speaks for this source alone.
        assert (f'data-i18n-title="settings.full_sync.i.{key}"'
                in seite[knopf:knopf + 260]), f"{key}: no (i) of its own"
    assert "full_sync: true" in seite and "'job.full'" in seite


PRUEFUNG_VOLLSYNC = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body ? JSON.parse(opt.body) : null});
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/v1/status') >= 0 ? statusGeruest() : {ok: true}); }});
};
global.confirm = function(text){ global.gefragt = text; return false; };
S.config = {};
vollSync('teams');
pruefe(String(global.gefragt).indexOf('Teams') >= 0, 'Rueckfrage nennt die Quelle nicht: ' + global.gefragt);
pruefe(!gesendet.some(function(g){ return g.pfad.indexOf('/api/v1/runs') >= 0; }),
       'Abgelehnt und trotzdem gestartet');
global.confirm = function(){ return true; };
vollSync('sharepoint');
var lauf = gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') >= 0; })[0];
pruefe(lauf && lauf.body.sharepoint === true && lauf.body.full_sync === true, 'Lauf nicht gestartet: ' + JSON.stringify(lauf));
pruefe(lauf.body.index === true, 'Vollsync ohne Index-Schritt');
pruefe(lauf.body.label === 'job.full', 'falsches Etikett: ' + lauf.body.label);
pruefe(!lauf.body.sharepoint_pages, 'Seiten ohne eine URL mitgeschickt');
pruefe(!lauf.body.onedrive && !lauf.body.outlook, 'andere Quellen mitgeschickt');
S.config = {sharepoint_pages_enabled: true,
            sharepoint_pages_urls: 'https://firma.sharepoint.com/sites/x'};
vollSync('sharepoint');
lauf = gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') >= 0; })[1];
pruefe(lauf.body.sharepoint_pages === true, 'Seiten trotz URL nicht mitgeschickt');
console.log('OK');
"""


def test_vollsync_fragt_zurueck_und_startet_nur_die_quelle():
    _in_node(PRUEFUNG_VOLLSYNC)


def test_config_nimmt_regeln_daten_und_zahlen_der_neun(server, sandbox):
    a, port = server
    code, r = call(port, "PATCH", "/api/v1/config",
                   {"teams_rules": "- channels/Nordwind/**\n", "todo_rules": "+ Einkauf",
                    "sharepoint_rules": "- Nordwind/Dokumente/Archiv/**",
                    "outlook_since": "2025-01-15", "teams_since": "gestern",
                    "calendar_months_back": 3, "planner_sweep_hours": 99999})
    assert code == 200
    assert r["config"]["teams_rules"] == "- channels/Nordwind/**"
    assert r["config"]["todo_rules"] == "+ Einkauf"
    assert r["config"]["sharepoint_rules"] == "- Nordwind/Dokumente/Archiv/**"
    assert r["config"]["outlook_since"] == "2025-01-15"
    assert r["config"]["teams_since"] == ""             # not a day: nothing
    assert r["config"]["calendar_months_back"] == 3
    assert r["config"]["planner_sweep_hours"] == 8760   # clamped


def test_exportliste_kennt_teams_und_todo(server, sandbox):
    """Teams conversations and To Do lists get the same export list as the
    folders: the stored list against the rules from the form, what lies in
    the archive counted per title (the files carry an id suffix)."""
    import folders
    a, port = server
    teams = sandbox / app_mod.TEAMS_DIR
    folders.speichere(teams, [
        {"id": "c1", "pfad": "group/Projekt Nordwind", "name": "Projekt Nordwind", "elemente": 0},
        {"id": "c2", "pfad": "channels/Nordwind/Allgemein", "name": "Allgemein", "elemente": 0}])
    (teams / "group").mkdir(parents=True)
    (teams / "group" / "Projekt Nordwind__k1.html").write_text("x", encoding="utf-8")
    (teams / "group" / "Alt__k2.html").write_text("x", encoding="utf-8")
    code, r = call(port, "QUERY", "/api/v1/sources/teams/folder-plan",
                   {"teams_rules": "- channels/**"})
    assert code == 200 and r["regeln"]
    assert [z["pfad"] for z in r["an"]] == ["group/Projekt Nordwind"]
    assert r["an"][0]["archiv"] == 1
    assert [z["pfad"] for z in r["aus"]] == ["channels/Nordwind/Allgemein"]
    assert r["weg"] == [{"pfad": "group/Alt", "archiv": 1}]

    todo = sandbox / app_mod.TODO_DIR
    folders.speichere(todo, [
        {"id": "l1", "pfad": "Einkauf", "name": "Einkauf", "elemente": 3, "ordner": "Einkauf__l1"},
        {"id": "l2", "pfad": "Aufgaben", "name": "Aufgaben", "elemente": 0, "ordner": "Aufgaben__l2"}])
    (todo / "Einkauf__l1").mkdir(parents=True)
    state_db.StateDb(todo / "Einkauf__l1").kv_schreiben(
        "tasks", json.dumps({"t1": {}, "t2": {}}))
    code, r = call(port, "QUERY", "/api/v1/sources/todo/folder-plan",
                   {"todo_rules": "- Aufgaben"})
    assert code == 200 and r["regeln"]
    assert [z["pfad"] for z in r["an"]] == ["Einkauf"] and r["an"][0]["archiv"] == 2
    assert [z["pfad"] for z in r["aus"]] == ["Aufgaben"]


def test_exportliste_faellt_auf_die_alte_namensliste_zurueck(server, sandbox):
    """Without rules, whatever is in the folder list still applies – exactly
    as in the export (outlook_export.aktuelle_regeln)."""
    a, port = server
    _baum(sandbox, a.cfg,
          [{"id": "1", "pfad": "E-Mail/Posteingang", "name": "P", "elemente": 1},
           {"id": "2", "pfad": "E-Mail/Archiv/Alt", "name": "Alt", "elemente": 2}])
    r = call(port, "QUERY", "/api/v1/sources/outlook/folder-plan",
             {"folder_rules": "", "skip_folders": "Archiv"})[1]
    assert [z["pfad"] for z in r["aus"]] == ["E-Mail/Archiv/Alt"]
    # Lowercased because the comparison is case-insensitive – the rule shown
    # is thus exactly the one that made the decision.
    assert r["aus"][0]["regel"] == "- E-Mail/archiv/**"


def test_exportliste_ohne_abgeglichenen_baum(server):
    a, port = server
    code, r = call(port, "QUERY", "/api/v1/sources/outlook/folder-plan", {})
    assert code == 404 and r["leer"] is True and r["error"]["k"] == "srv.plan.nolist"


# --------------------------------------------------------------------------
# Calendars: the same mechanics as the mailbox folders
# --------------------------------------------------------------------------
def _kalenderliste(sandbox, eintraege, termine=()):
    ordner = sandbox / app_mod.OUTLOOK_DIR
    folders_mod.speichere(ordner, eintraege, datei=folders_mod.KALENDER)
    for pfad, anzahl in termine:
        (ordner / pfad).mkdir(parents=True, exist_ok=True)
        for i in range(anzahl):
            (ordner / pfad / f"t{i}.ics").write_text("x", encoding="utf-8")
    return ordner


KALENDER = [{"id": "a", "pfad": "kalender/Privat", "name": "Privat", "standard": False,
             "elemente": 0},
            {"id": "b", "pfad": "kalender/Arbeit", "name": "Arbeit", "standard": True,
             "elemente": 0}]


def test_kalenderregeln_ohne_eintrag_nur_der_standard(sandbox):
    """A mailbox often has birthdays and foreign shares – nobody meant those."""
    daten = {"ordner": KALENDER}
    regeln = app_mod.kalenderregeln({"calendar_rules": ""}, daten)
    assert [e["name"] for e in folders_mod.gewaehlt(daten, regeln)] == ["Arbeit"]
    eigene = app_mod.kalenderregeln({"calendar_rules": "- kalender/**\n+ kalender/Privat"}, daten)
    assert [e["name"] for e in folders_mod.gewaehlt(daten, eigene)] == ["Privat"]


def test_kalenderliste_rechnet_mit_den_regeln_aus_dem_formular(server, sandbox):
    a, port = server
    _kalenderliste(sandbox, KALENDER, [("kalender/Privat", 3), ("kalender/Weg", 2)])

    code, r = call(port, "QUERY", "/api/v1/sources/outlook/folder-plan",
                   {"unit": "calendar", "calendar_rules": "- kalender/**\n+ kalender/Privat"})
    assert code == 200 and r["regeln"]
    assert [z["pfad"] for z in r["an"]] == ["kalender/Privat"]
    assert [z["pfad"] for z in r["aus"]] == ["kalender/Arbeit"]
    # Counted is what sits on disk: Graph does not count events when listing.
    assert r["an"][0]["archiv"] == 3
    assert r["weg"] == [{"pfad": "kalender/Weg", "archiv": 2}]
    assert not a.cfg["calendar_rules"]


def test_kalenderstand_nennt_die_gewaehlten_namen(server, sandbox):
    a, port = server
    _kalenderliste(sandbox, KALENDER)
    c = call(port, "GET", "/api/v1/inventory")[1]["calendars"]
    assert (c["gesamt"], c["gewaehlt"], c["namen"]) == (2, 1, ["Arbeit"])
    assert c["abgeglichen"]


def test_kalenderstand_ohne_liste(server):
    c = call(server[1], "GET", "/api/v1/inventory")[1]["calendars"]
    assert c == {"abgeglichen": None, "gesamt": 0, "gewaehlt": 0, "namen": [], "neu": []}


def test_kalenderregeln_werden_gespeichert_und_weitergereicht(server, sandbox):
    a, port = server
    call(port, "PATCH", "/api/v1/config", {"calendar_rules": "kalender/Privat"})
    # No sign means include – the spelled-out rule is what gets saved.
    assert a.cfg["calendar_rules"] == "+ kalender/Privat"
    schritt = [s for s in app_mod.build_steps(a.cfg, {"outlook": True}) if s["key"] == "outlook"][0]
    assert schritt["env"]["CALENDAR_RULES"] == "+ kalender/Privat"


def test_build_steps_kalenderabgleich(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), {"sync_calendars": True})
    assert [s["key"] for s in steps] == ["calendars"]
    assert "--calendars" in steps[0]["argv"]


def test_ausgeblendete_dateitypen_nur_in_der_liste(server, sandbox, monkeypatch):
    """Hidden means: not offered. Not: not there.

    The tool should keep saying what the archive holds – to Claude too.
    Only the list the UI offers gets trimmed.
    """
    a, port = server
    alle = {"count": 3, "total_distinct": 3, "filetypes": [
        {"type": "pdf", "messages": 40}, {"type": "p7s", "messages": 12},
        {"type": "xlsx", "messages": 3}]}
    mod = types.SimpleNamespace(list_filetypes=lambda limit=40, source="": alle)
    monkeypatch.setattr(a.search, "ensure", lambda cfg: mod)

    call(port, "PATCH", "/api/v1/config", {"filetype_hidden": ".P7S, xlsx ,, "})
    assert a.cfg["filetype_hidden"] == ["p7s", "xlsx"]      # lowercase, no dot

    r = call(port, "GET", "/api/v1/filetypes")[1]
    assert [e["type"] for e in r["items"]] == ["pdf"]
    assert r["hidden"] == ["p7s", "xlsx"]
    # The corpus is not talked down: there are still three types.
    assert r["total_distinct"] == 3
    # And searching for them stays possible – only the offer is hidden.
    assert mod.list_filetypes()["filetypes"] == alle["filetypes"]


def test_dateitypen_vorgabe_ist_sichtbar(server):
    """The default is visible in the field, not a silent rule in the code."""
    s = call(server[1], "GET", "/api/v1/app")[1]
    cfg = call(server[1], "GET", "/api/v1/config")[1]["config"]
    assert s["filetype_hidden_default"] == sorted(app_mod.FILETYPE_HIDDEN_DEFAULT)
    assert cfg["filetype_hidden"] == s["filetype_hidden_default"]
    assert 'id="c-filetype_hidden"' in app_mod.seite()


def test_abgeschalteter_mcp_zugriff_startet_nichts(sandbox, monkeypatch):
    """The hard switch also applies to the HTTP endpoint the app itself
    runs – and to the autostart at program start."""
    cfg = app_mod.load_config()
    cfg["mcp_enabled"] = False
    a = app_mod.App(cfg)
    gestartet = []
    monkeypatch.setattr(runner_mod.subprocess, "Popen",
                        lambda *x, **kw: gestartet.append(x) or (_ for _ in ()).throw(
                            AssertionError("Prozess trotzdem gestartet")))

    ok, why = a.mcp.start(cfg)
    assert not ok and schluessel(why) == "srv.mcp.disabled"
    cfg["mcp_autostart"] = True
    a.autostart_mcp()                      # must not start anything either
    assert not gestartet


def test_abschalten_haelt_den_laufenden_endpunkt_an(server, monkeypatch):
    """Whoever switches off access also means the one currently running."""
    a, port = server
    angehalten = []
    monkeypatch.setattr(a.mcp, "stop", lambda: angehalten.append(True))
    call(port, "PATCH", "/api/v1/config", {"mcp_enabled": False})
    assert a.cfg["mcp_enabled"] is False
    assert angehalten, "Der laufende Endpunkt lief weiter"


def test_auswahlregeln_regeln_schlagen_die_namensliste():
    cfg = {"folder_rules": "+ E-Mail/Nur/**", "skip_folders": ["archiv"]}
    assert app_mod.auswahlregeln(cfg) == [(True, "E-Mail/Nur/**")]
    assert app_mod.auswahlregeln({"folder_rules": "", "skip_folders": ["archiv"]}) \
        == [(False, "E-Mail/archiv/**")]


# The export list in the browser: three groups, one filter – and a window
# that the status poll every 2.5 seconds must NOT wipe away.
PRUEFUNG_EXPORTLISTE = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body,
                 methode: (opt && opt.method) || 'GET'});
  if(String(pfad).indexOf('/folder-plan') < 0){
    return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
  }
  return Promise.resolve({json: function(){ return Promise.resolve({
    abgeglichen: '2026-08-10T09:33:51+00:00',
    an:  [{pfad: 'E-Mail/Posteingang', elemente: 1, archiv: 1, regel: null},
          {pfad: 'E-Mail/Kunden/Beispiel AG', elemente: 240, archiv: 238,
           regel: '+ E-Mail/Kunden/Beispiel AG/**'}],
    aus: [{pfad: 'E-Mail/Archiv', elemente: 19650, archiv: 4711,
           regel: '- E-Mail/Archiv/**'}],
    weg: [{pfad: 'E-Mail/Alter Kunde', archiv: 812}],
    mails_an: 241, mails_aus: 19650, mails_weg: 812}); }});
};

document.getElementById('c-folder_rules').value = '- E-Mail/Archiv/**';
document.getElementById('c-skip_folders').value = 'junk';
zeigeExportliste();

setTimeout(function(){
  // Gefragt wird mit dem, was IM FELD steht - nicht mit dem Gespeicherten.
  var frage = gesendet.filter(function(g){ return g.pfad.indexOf('folder-plan') >= 0; })[0];
  pruefe(frage, 'Keine Anfrage an folder-plan');
  // Eine Frage, kein Schreibzugriff: QUERY (RFC 10008), nicht POST.
  pruefe(frage.methode === 'QUERY', 'Ordnerplan nicht per QUERY geholt: ' + frage.methode);
  pruefe(JSON.parse(frage.body).folder_rules === '- E-Mail/Archiv/**',
         'Regeln aus dem Feld nicht mitgeschickt: ' + frage.body);
  pruefe(JSON.parse(frage.body).skip_folders === 'junk', 'Ordnerliste fehlt');

  var h = document.getElementById('plan-listen').innerHTML;
  pruefe(h.indexOf('E-Mail/Posteingang') >= 0, 'Gewaehlter Ordner fehlt');
  pruefe(h.indexOf('E-Mail/Archiv') >= 0, 'Ausgelassener Ordner fehlt');
  pruefe(h.indexOf('E-Mail/Alter Kunde') >= 0, 'Nur-noch-im-Archiv fehlt');
  pruefe(h.indexOf('- E-Mail/Archiv/**') >= 0, 'Entscheidende Regel nicht genannt');
  pruefe(h.indexOf('19.650') >= 0, 'Zahlen nicht lesbar gruppiert');
  // Ausgelassen heisst nicht leer: der Bestand muss dabeistehen.
  pruefe(h.indexOf('4.711') >= 0, 'Bestand des ausgelassenen Ordners fehlt');
  // Beim Gewaehlten waere dieselbe Zahl nur Laerm.
  pruefe(h.indexOf('238') < 0 || h.indexOf('im Archiv') > 0, 'unerwartet');

  // Filter: nur noch die passenden Zeilen, die Gruppen bleiben stehen.
  document.getElementById('plan-filter').value = 'beispiel';
  planListen();
  h = document.getElementById('plan-listen').innerHTML;
  pruefe(h.indexOf('Beispiel AG') >= 0, 'Treffer weggefiltert');
  pruefe(h.indexOf('E-Mail/Posteingang') < 0, 'Filter greift nicht');
  pruefe(h.split('plangruppe').length - 1 === 3, 'Nicht mehr drei Gruppen');

  // Der Statusabruf darf das Fenster nicht ersetzen - es hat keine Kennung,
  // die er vergleichen koennte, und wuerde sonst im Sekundentakt verschwinden.
  var vorher = modal.innerHTML;
  renderStatus(Object.assign({}, statusGeruest(), {wizard: 'token'}));
  pruefe(modal.innerHTML === vorher, 'Statusabruf hat die Exportliste ueberschrieben');

  // ESC schliesst - und meldet dem Server KEINEN gesehenen Assistenten.
  var vorZahl = gesendet.length;
  taste('Escape');
  pruefe(wizardOffen === null, 'ESC schliesst nicht');
  pruefe(gesendet.slice(vorZahl).every(function(g){
           return g.pfad.indexOf('wizard-seen') < 0; }),
         'Eigenes Fenster als Assistent quittiert');
  console.log('OK');
}, 20);
"""


def test_exportliste_zeigt_drei_gruppen_und_ueberlebt_den_statusabruf():
    _in_node(PRUEFUNG_EXPORTLISTE)


# Calendars are the same list with the same preview - except here what
# already sits on disk counts, because Graph counts no events when listing.
PRUEFUNG_KALENDERLISTE = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body,
                 methode: (opt && opt.method) || 'GET'});
  if(String(pfad).indexOf('/folder-plan') < 0){
    return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
  }
  return Promise.resolve({json: function(){ return Promise.resolve({
    abgeglichen: '2026-08-10T09:33:51+00:00',
    an:  [{pfad: 'kalender/Arbeit', elemente: 0, archiv: 1200, regel: null}],
    aus: [{pfad: 'kalender/Geburtstage', elemente: 0, archiv: 0,
           regel: '- kalender/**'}],
    weg: [{pfad: 'kalender/Alt', archiv: 40}],
    mails_an: 0, mails_aus: 0, mails_weg: 40}); }});
};

// Der Stand in den Einstellungen nennt die Kalender beim Namen: bei einer
// Handvoll sagt das mehr als jede Zahl.
zeigeKalenderstand({abgeglichen: '2026-08-10T09:33:51+00:00', gesamt: 3,
                    gewaehlt: 1, namen: ['Arbeit'], neu: []});
var stand = document.getElementById('cal-state').textContent;
pruefe(stand.indexOf('Arbeit') >= 0, 'Kalendername fehlt: ' + stand);
pruefe(stand.indexOf('3') >= 0, 'Gesamtzahl fehlt: ' + stand);

document.getElementById('c-calendar_rules').value = '- kalender/**';
zeigeExportliste('calendar');

setTimeout(function(){
  var frage = gesendet.filter(function(g){ return g.pfad.indexOf('folder-plan') >= 0; })[0];
  pruefe(frage, 'Keine Anfrage an folder-plan');
  var b = JSON.parse(frage.body);
  pruefe(frage.pfad.indexOf('/sources/outlook/folder-plan') >= 0 && b.unit === 'calendar',
         'Kalender nicht als Einheit des Postfachs gefragt: ' + frage.pfad + ' ' + frage.body);
  pruefe(b.calendar_rules === '- kalender/**', 'Regeln aus dem Feld nicht mitgeschickt');

  var h = document.getElementById('plan-listen').innerHTML;
  pruefe(h.indexOf('kalender/Arbeit') >= 0, 'Gewaehlter Kalender fehlt');
  pruefe(h.indexOf('kalender/Geburtstage') >= 0, 'Ausgelassener Kalender fehlt');
  pruefe(h.indexOf('kalender/Alt') >= 0, 'Nur-noch-im-Archiv fehlt');
  // Gezaehlt wird der Bestand, nicht die immer leere Elementzahl.
  pruefe(h.indexOf('1.200') >= 0, 'Bestand des Kalenders fehlt');
  pruefe(h.split('plangruppe').length - 1 === 3, 'Nicht drei Gruppen');
  console.log('OK');
}, 20);
"""


def test_kalenderliste_zeigt_bestand_statt_leerer_zahlen():
    _in_node(PRUEFUNG_KALENDERLISTE)


# The source up front decides what is on offer behind it. A select with a
# single entry is no choice - then it sits there greyed out.
PRUEFUNG_ORDNERAUSWAHL = GRUNDZUSTAND + r"""
S.store = {exists: true, built_at: '2026-08-12T10:00:00+00:00'};
var ORDNER = {
  outlook:  [{path:'E-Mail/Posteingang', messages:12480},
             {path:'E-Mail/Kunden', messages:8102}],
  kalender: [{path:'kalender/Arbeit', messages:4854},
             {path:'kalender/Privat', messages:912}],
  teams:    [{path:'1on1', messages:31204}, {path:'channels', messages:15302}],
  kontakte: [{path:'kontakte/Team', messages:64}]
};
var gefragt = [];
global.fetch = function(pfad){
  gefragt.push(String(pfad));
  var m = String(pfad).match(/source=(\w+)/);
  return Promise.resolve({json: function(){
    return Promise.resolve({items: ORDNER[m ? m[1] : 'all'] || []}); }});
};

var feld = document.getElementById('f-folder');
function optionen(){
  return (feld.innerHTML.match(/<option[^>]*>[^<]*/g) || []).map(function(o){
    return o.replace(/^<option[^>]*>/, ''); });
}
function warte(schritte, fertig){
  if(schritte <= 0) return fertig();
  setTimeout(function(){ warte(schritte - 1, fertig); }, 0);
}
function waehle(quelle, dann){
  document.getElementById('f-source').value = quelle;
  ladeOrdner();
  warte(4, dann);
}

waehle('outlook', function(){
  pruefe(gefragt.some(function(p){ return p.indexOf('source=outlook') >= 0; }),
         'Quelle nicht mitgefragt: ' + gefragt.join(' '));
  pruefe(optionen().length === 3, 'Erwartet: Vorgabe + zwei Ordner, ist ' + optionen());
  pruefe(!feld.disabled, 'Zwei Ordner sind eine Wahl');
  feld.value = 'E-Mail/Kunden';

  waehle('kalender', function(){
    // Die Wahl von vorhin gibt es in dieser Quelle nicht - sie faellt weg,
    // sonst suchte man in einem Ordner, den diese Quelle nicht kennt.
    pruefe(feld.value === '', 'Unpassende Ordnerwahl blieb stehen: ' + feld.value);
    pruefe(optionen()[0] === 'Alle Kalender', 'Falsche Beschriftung: ' + optionen()[0]);

    waehle('teams', function(){
      var namen = optionen().map(function(o){ return o.split(' (')[0]; });
      pruefe(namen.indexOf('Kan\u00e4le') > 0, 'Kanaele nicht lesbar benannt: ' + namen);
      // Ohne das gezaehlte Wort im Namen: "1:1-Chats (89.273)" liest sich als
      // Zahl der Chats, gezaehlt werden aber die Nachrichten - wie ueberall
      // sonst in dieser Liste auch.
      pruefe(namen.indexOf('1:1') > 0, 'Chatart nicht lesbar benannt: ' + namen);
      pruefe(namen.join(' ').indexOf('Chats') < 0,
             'Name zaehlt etwas anderes als die Zahl: ' + namen);
      pruefe(namen[0] === 'Alle Chatarten', 'Falsche Beschriftung: ' + namen[0]);
      // Gefiltert wird weiter mit dem Ablagepfad, nicht mit dem Anzeigenamen.
      pruefe(feld.innerHTML.indexOf('value="channels"') >= 0, 'Falscher Filterwert');

      waehle('kontakte', function(){
        // Ein einziger Ordner filtert nichts weg. Ausgegraut sah das aus, als
        // sei etwas kaputt - also verschwindet das Feld.
        pruefe(feld.classList.contains('hide'),
               'Feld bleibt stehen, obwohl es nichts zu waehlen gibt');
        feld.value = 'kontakte/Team';   // von Hand gesetzt: darf nicht bleiben

        // Zweimal dieselbe Quelle fragt den Server nicht noch einmal.
        var vorher = gefragt.length;
        waehle('kontakte', function(){
          pruefe(gefragt.length === vorher, 'Ordnerliste ohne Not neu geholt');
          pruefe(feld.value === '', 'Versteckte Wahl filtert weiter mit');

          // Zurueck zu einer Quelle mit echter Auswahl: das Feld kommt wieder.
          waehle('kalender', function(){
            pruefe(!feld.classList.contains('hide'), 'Feld kommt nicht zurueck');
            console.log('OK');
          });
        });
      });
    });
  });
});
"""


def test_ordnerauswahl_folgt_der_quelle():
    _in_node(PRUEFUNG_ORDNERAUSWAHL)


# The person field is free-text input against a fixed corpus. Whoever types
# a name that does not exist should learn that before the search.
PRUEFUNG_PERSONENVORSCHLAG = GRUNDZUSTAND + r"""
var LEUTE = {
  bei: {items: [{name:'Alice Beispiel', messages:1240},
                 {name:'Bob Beispiel', messages:87}],
        total_distinct: 2, total_messages: 1327},
  viele: {items: [{name:'A', messages:9}, {name:'B', messages:8},
                   {name:'C', messages:7}, {name:'D', messages:6},
                   {name:'E', messages:5}], total_distinct: 31, total_messages: 900},
  einer: {items: [{name:'Nur Eine', messages:4}], total_distinct: 1, total_messages: 4},
  xyz: {items: [], total_distinct: 0}
};
var gefragt = [];
global.fetch = function(pfad){
  gefragt.push(String(pfad));
  var m = String(pfad).match(/contains=([^&]*)/);
  return Promise.resolve({json: function(){
    return Promise.resolve(LEUTE[m ? decodeURIComponent(m[1]) : ''] || {items: []}); }});
};

var feld = document.getElementById('f-person'), kasten = document.getElementById('personliste');
function tippe(wort, dann){
  feld.value = wort;
  personVorschlagen();
  setTimeout(function(){ setTimeout(dann, 0); }, 200);   // Wartezeit + Antwort
}

// Ein einzelner Buchstabe fragt noch nichts - das waere halb das Archiv.
feld.value = 'b';
personVorschlagen();
setTimeout(function(){
  pruefe(gefragt.length === 0, 'Bei einem Zeichen schon gefragt: ' + gefragt.join(' | '));

  tippe('bei', function(){
    pruefe(gefragt[0].indexOf('limit=5') >= 0, 'Nicht auf fuenf begrenzt: ' + gefragt[0]);
    pruefe(gefragt[0].indexOf('contains=bei') >= 0, 'Suchwort nicht mitgeschickt');
    pruefe(kasten.innerHTML.indexOf('Alice Beispiel') >= 0, 'Vorschlag fehlt');
    pruefe(kasten.innerHTML.indexOf('1.240') >= 0, 'Zahl der Nachrichten fehlt');
    pruefe(!kasten.classList.contains('hide'), 'Liste bleibt zu');
    // Die Liste zwingt zu keiner Wahl: unten steht das Muster selbst, mit der
    // Summe aller Treffer - derselben Groesse wie in den Zeilen darueber.
    pruefe(kasten.innerHTML.indexOf('bei*') >= 0, 'Sternzeile fehlt');
    pruefe(kasten.innerHTML.indexOf('1.327') >= 0, 'Summe fehlt');

    // Tastatur: runter, Enter - der Name steht im Feld, die Liste ist zu.
    personTaste({key: 'ArrowDown', preventDefault: function(){}});
    personTaste({key: 'Enter', preventDefault: function(){}});
    pruefe(feld.value === 'Alice Beispiel', 'Enter uebernimmt nicht: ' + feld.value);
    pruefe(kasten.classList.contains('hide'), 'Liste bleibt nach der Wahl offen');

    tippe('viele', function(){
      // Mehr Namen als Plaetze: sagen statt still abschneiden.
      pruefe(kasten.innerHTML.indexOf('26') >= 0,
             'Die uebrigen Namen werden verschwiegen: ' + kasten.innerHTML);

      // Die letzte Zeile uebernimmt das Muster, nicht einen Namen.
      personTaste({key: 'ArrowUp', preventDefault: function(){}});
      personTaste({key: 'Enter', preventDefault: function(){}});
      pruefe(feld.value === 'viele*', 'Sternzeile uebernimmt nicht: ' + feld.value);

      tippe('einer', function(){
        // Bei genau einem Treffer waere "alle" derselbe Treffer noch einmal.
        pruefe(kasten.innerHTML.indexOf('einer*') < 0,
               'Sternzeile bei einem einzigen Treffer: ' + kasten.innerHTML);

      tippe('xyz', function(){
        // Der eigentliche Zweck: einen Namen, den es nicht gibt, vor der
        // Suche als solchen erkennen.
        pruefe(kasten.innerHTML.indexOf('Niemanden') >= 0,
               'Kein Hinweis auf den leeren Bestand: ' + kasten.innerHTML);
        pruefe(kasten.innerHTML.indexOf('<button') < 0, 'Leere Liste bietet Wahl an');
        console.log('OK');
      });
      });
    });
  });
}, 200);
"""


def test_personenfeld_schlaegt_vor_und_meldet_unbekannte():
    _in_node(PRUEFUNG_PERSONENVORSCHLAG)


# File types exist only where attachments or files live - and only when
# the index knows the column at all.
PRUEFUNG_DATEITYP = GRUNDZUSTAND + r"""
S.store = {exists: true, built_at: '2026-08-12T10:00:00+00:00', features: ['ext']};
KANN_TYP = true;
var TYPEN = {
  all:     [{type:'pdf', messages:4120}, {type:'xlsx', messages:880}],
  outlook: [{type:'pdf', messages:4120}, {type:'xlsx', messages:880}],
  datei:   [{type:'pdf', messages:12}]
};
var gefragt = [];
global.fetch = function(pfad){
  gefragt.push(String(pfad));
  var m = String(pfad).match(/source=(\w+)/), quelle = m ? m[1] : 'all';
  if(String(pfad).indexOf('/api/v1/filetypes') >= 0){
    return Promise.resolve({json: function(){
      return Promise.resolve({items: TYPEN[quelle] || []}); }});
  }
  return Promise.resolve({json: function(){
    return Promise.resolve({items: [{path:'E-Mail/A', messages:2},
                                    {path:'E-Mail/B', messages:1}]}); }});
};

var feld = document.getElementById('f-typ');
function warte(n, fertig){
  if(n <= 0) return fertig();
  setTimeout(function(){ warte(n - 1, fertig); }, 0);
}
function waehle(quelle, dann){
  document.getElementById('f-source').value = quelle;
  ladeOrdner();
  warte(4, dann);
}

waehle('outlook', function(){
  pruefe(gefragt.some(function(p){ return p.indexOf('/api/v1/filetypes') >= 0; }),
         'Dateitypen nicht geholt');
  pruefe(feld.innerHTML.indexOf('PDF') >= 0, 'Typ fehlt: ' + feld.innerHTML);
  pruefe(feld.innerHTML.indexOf('4.120') >= 0, 'Zahl fehlt');
  pruefe(feld.innerHTML.indexOf('value="pdf"') >= 0, 'Gefiltert wird mit der Endung');
  pruefe(!feld.classList.contains('hide'), 'Feld versteckt, obwohl es Typen gibt');
  feld.value = 'pdf';
  pruefe(filterFelder().length > 0, 'Dateityp zaehlt nicht als Filter');

  // Chats haben keine Anhaenge - dort waere das Feld eine leere Verheissung.
  waehle('teams', function(){
    pruefe(feld.classList.contains('hide'), 'Feld bleibt bei Teams stehen');
    pruefe(feld.value === '', 'Versteckter Dateityp filtert weiter mit');

    // Ein einziger Typ ist keine Wahl - dieselbe Regel wie beim Ordner.
    waehle('datei', function(){
      pruefe(feld.classList.contains('hide'), 'Ein einziger Typ ist keine Wahl');

      // Kennt der Index die Spalte nicht, gibt es das Feld gar nicht.
      KANN_TYP = false;
      var vorher = gefragt.length;
      typenJeQuelle = {};
      waehle('outlook', function(){
        pruefe(feld.classList.contains('hide'), 'Feld ohne Spalte im Index');
        pruefe(!gefragt.slice(vorher).some(function(p){
                 return p.indexOf('/api/v1/filetypes') >= 0; }),
               'Ohne Spalte trotzdem gefragt');
        console.log('OK');
      });
    });
  });
});
"""


def test_dateitypfilter_folgt_quelle_und_index():
    _in_node(PRUEFUNG_DATEITYP)


# Reported: "from 01.01.2019 to 31.06.2021" returned hits from 2026. June
# 31st does not exist, the field then yields an EMPTY value - and the
# search carried on without that bound, without saying so.
PRUEFUNG_DATUMSPRUEFUNG = GRUNDZUSTAND + r"""
var gesucht = [];
global.fetch = function(pfad){
  gesucht.push(String(pfad));
  return Promise.resolve({json: function(){
    return Promise.resolve({results: [], count: 0}); }});
};
var von = document.getElementById('f-from'), bis = document.getElementById('f-to');
var treffer = document.getElementById('results');

// Der Browser hat den 31.06. entgegengenommen und gibt nichts heraus.
von.value = '2019-01-01';
bis.value = '';
bis.validity = {badInput: true};
doSearch(0);
pruefe(gesucht.length === 0, 'Trotz unlesbarem Datum gesucht');
pruefe(treffer.innerHTML.indexOf('gibt es nicht') >= 0,
       'Kein Hinweis auf das unmoegliche Datum: ' + treffer.innerHTML);
pruefe(bis.classList.contains('fehler'), 'Das falsche Feld ist nicht markiert');
pruefe(!von.classList.contains('fehler'), 'Das richtige Feld ist markiert');

// Berichtigt: die Markierung geht weg und es wird gesucht.
bis.validity = {badInput: false};
bis.value = '2021-06-30';
doSearch(0);
pruefe(!bis.classList.contains('fehler'), 'Markierung bleibt nach der Korrektur');
pruefe(gesucht.length === 1, 'Nach der Korrektur nicht gesucht');
pruefe(gesucht[0].indexOf('to=2021-06-30') >= 0, 'Obergrenze nicht mitgeschickt');

// Vertauscht: liefert zuverlaessig nichts und sieht aus wie ein leeres Archiv.
von.value = '2026-01-01';
doSearch(0);
pruefe(gesucht.length === 1, 'Bei vertauschtem Zeitraum trotzdem gesucht');
pruefe(treffer.innerHTML.indexOf('bis') >= 0, 'Kein Hinweis auf den vertauschten Zeitraum');
console.log('OK');
"""


def test_unmoegliches_datum_sucht_nicht_stillschweigend_ohne():
    _in_node(PRUEFUNG_DATUMSPRUEFUNG)


# --------------------------------------------------------------------------
# OneDrive in the UI
# --------------------------------------------------------------------------
def test_onedrive_ist_aus_bis_jemand_es_einschaltet(sandbox):
    """A drive can hold tens of gigabytes – nobody pulls that in by accident
    with the first click."""
    assert app_mod.load_config()["onedrive_enabled"] is False


def test_onedrive_schritt_bekommt_regeln_und_grenze(sandbox):
    cfg = app_mod.load_config()
    cfg["onedrive_rules"] = "- Dateien/Fotos/**"
    cfg["onedrive_max_mb"] = 50
    schritt = next(s for s in app_mod.build_steps(cfg, {"onedrive": True})
                   if s["key"] == "onedrive")
    assert "onedrive_export" in " ".join(str(a) for a in schritt["argv"])
    assert schritt["env"]["ONEDRIVE_RULES"] == "- Dateien/Fotos/**"
    assert schritt["env"]["ONEDRIVE_MAX_MB"] == "50"


def test_onedrive_regeln_werden_beim_speichern_normalisiert(server):
    a, port = server
    call(port, "PATCH", "/api/v1/config",
         {"onedrive_rules": "Dateien/A\n\n# Kommentar\n- Dateien/B"})
    assert a.cfg["onedrive_rules"] == "+ Dateien/A\n- Dateien/B"


def test_onedrive_leerer_schalter_setzt_die_variable_trotzdem(sandbox):
    """Empty means "take everything". Unset would mean "use what is in
    app_config.json" – and the script would run differently from what the
    app shows."""
    schritt = next(s for s in app_mod.build_steps(app_mod.load_config(), {"onedrive": True})
                   if s["key"] == "onedrive")
    assert schritt["env"]["ONEDRIVE_RULES"] == ""
    assert schritt["env"]["ONEDRIVE_MAX_MB"] == "0"


def test_onedrive_braucht_einen_zugang(sandbox, no_ollama, monkeypatch):
    a = app_mod.App()
    monkeypatch.setattr(a.jobs, "start", lambda steps, label, **kw: True)
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "")
    ok, why = a.launch({"onedrive": True}, label="job.export")
    assert not ok and schluessel(why) == "srv.notoken"


def test_index_sieht_den_onedrive_ordner(sandbox):
    schritt = next(s for s in app_mod.build_steps(app_mod.load_config(), {"index": True})
                   if s["key"] == "index")
    argv = [str(x) for x in schritt["argv"]]
    assert "onedrive_export" in argv, "der Index findet die Dateien sonst nicht"


PRUEFUNG_ONEDRIVE = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body,
                 methode: (opt && opt.method) || 'GET'});
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};
var status = statusGeruest();
KONFIG = Object.assign({}, KONFIG, {outlook_categories: ['mail'], teams_categories: [],
                                    onedrive_enabled: true});
// Die Auswahl wird NUR beim ersten Aufbau gesetzt - danach wuerde der Status
// alle 2,5 Sekunden ein gerade gesetztes Haekchen wieder wegnehmen.
S = null;
renderStatus(status);

pruefe(document.getElementById('c-onedrive_enabled').checked === true,
       'Haekchen nicht aus dem Status gesetzt');

// Genau das darf der naechste Statusabruf nicht rueckgaengig machen.
document.getElementById('c-onedrive_enabled').checked = false;
renderStatus(status);
pruefe(document.getElementById('c-onedrive_enabled').checked === false,
       'Statusabruf hat das Haekchen ueberschrieben');
document.getElementById('c-onedrive_enabled').checked = true;

// Der Lauf muss OneDrive mitschicken.
runExport();
var lauf = gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') >= 0; })[0];
pruefe(lauf, 'Kein Lauf gestartet');
pruefe(JSON.parse(lauf.body).onedrive === true, 'OneDrive fehlt im Lauf: ' + lauf.body);

// Nur OneDrive, ohne Outlook und Teams: muss trotzdem starten.
gesendet = [];
document.querySelectorAll = function(){ return []; };   // keine Outlook-Haken mehr
// Die Seite warnt nicht mehr mit alert, sondern mit ihrer einen Meldung
// (DESIGN.md §8) – die steht im DOM und laesst sich dort ablesen.
function gemeckert(){ return document.getElementById('meldung').textContent.length > 0; }
document.getElementById('meldung').textContent = '';
runExport();
pruefe(!gemeckert(), 'Nur OneDrive wurde als "nichts gewaehlt" abgelehnt');
var nur = gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') >= 0; })[0];
pruefe(JSON.parse(nur.body).onedrive === true && JSON.parse(nur.body).outlook === false,
       'Falscher Lauf: ' + nur.body);

// Gar nichts gewaehlt: kein Lauf.
gesendet = [];
document.getElementById('c-onedrive_enabled').checked = false;
document.getElementById('meldung').textContent = '';
runExport();
pruefe(gemeckert(), 'Ohne Auswahl wurde nicht gewarnt');
pruefe(document.getElementById('meldung').textContent === t('export.nothing'),
       'Die Warnung sagt etwas anderes: ' + document.getElementById('meldung').textContent);
pruefe(gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') >= 0; }).length === 0,
       'Ohne Auswahl trotzdem gestartet');
console.log('OK');
"""


def test_onedrive_haekchen_startet_den_lauf():
    _in_node(PRUEFUNG_ONEDRIVE)


def test_onedrive_abgleich_ist_ein_eigener_schritt(sandbox):
    schritt = next(s for s in app_mod.build_steps(app_mod.load_config(), {"sync_onedrive": True})
                   if s["key"] == "onedrive_folders")
    argv = [str(x) for x in schritt["argv"]]
    assert "--folders" in argv and "onedrive_export" in " ".join(argv)
    # Without access it must not start – it queries the drive.
    assert "ONEDRIVE_RULES" in schritt["env"]


def test_onedrive_abgleich_braucht_einen_zugang(sandbox, no_ollama, monkeypatch):
    a = app_mod.App()
    monkeypatch.setattr(a.jobs, "start", lambda steps, label, **kw: True)
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "")
    ok, why = a.launch({"sync_onedrive": True}, label="job.folders")
    assert not ok and schluessel(why) == "srv.notoken"


def test_exportliste_kennt_beide_quellen(server, sandbox):
    """The same evaluation, two folders – for the mailbox the .eml files
    count, for the mirror all files."""
    a, port = server
    _baum(sandbox, a.cfg,
          [{"id": "1", "pfad": "Dateien/Kunden", "name": "Kunden", "elemente": 3}])
    od = sandbox / app_mod.ONEDRIVE_DIR
    folders_mod.speichere(od, [{"id": "1", "pfad": "Dateien/Kunden",
                                "name": "Kunden", "elemente": 3}])
    (od / "Dateien" / "Kunden").mkdir(parents=True, exist_ok=True)
    for name in ("a.pdf", "b.docx"):
        (od / "Dateien" / "Kunden" / name).write_text("x", encoding="utf-8")

    r = call(port, "QUERY", "/api/v1/sources/onedrive/folder-plan",
             {"onedrive_rules": ""})[1]
    assert [z["pfad"] for z in r["an"]] == ["Dateien/Kunden"]
    assert r["an"][0]["archiv"] == 2, "beim Spiegel zählen alle Dateien, nicht nur .eml"

    r = call(port, "QUERY", "/api/v1/sources/onedrive/folder-plan",
             {"onedrive_rules": "- Dateien/Kunden/**"})[1]
    assert [z["pfad"] for z in r["aus"]] == ["Dateien/Kunden"]
    assert r["aus"][0]["regel"] == "- Dateien/Kunden/**"


def test_exportliste_sharepoint_mit_regeln_und_urls(server, sandbox):
    """The library list is judged by the path rules on top of the URL list,
    and every entry names the URLs its library came from – the cadence
    window maps a library to its URL rows with that."""
    a, port = server
    lib = sandbox / app_mod.SHAREPOINT_DIR / "Nordwind" / "Dokumente"
    folders_mod.speichere(lib, [
        {"id": "1", "pfad": "Dateien", "name": "Dateien", "elemente": 5},
        {"id": "2", "pfad": "Dateien/Archiv", "name": "Archiv", "elemente": 3}])
    state_db.StateDb(lib).kv_schreiben(
        "urls", json.dumps(["https://nordwind.sharepoint.com/sites/x"]))
    r = call(port, "QUERY", "/api/v1/sources/sharepoint/folder-plan",
             {"sharepoint_rules": "- Nordwind/Dokumente/Dateien/Archiv/**"})[1]
    assert [z["pfad"] for z in r["an"]] == ["Nordwind/Dokumente/Dateien"]
    assert r["an"][0]["urls"] == ["https://nordwind.sharepoint.com/sites/x"]
    assert [z["pfad"] for z in r["aus"]] == ["Nordwind/Dokumente/Dateien/Archiv"]
    assert r["aus"][0]["regel"] == "- Nordwind/Dokumente/Dateien/Archiv/**"


PRUEFUNG_OD_ORDNER = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body,
                 methode: (opt && opt.method) || 'GET'});
  if(String(pfad).indexOf('/folder-plan') < 0)
    return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
  return Promise.resolve({json: function(){ return Promise.resolve({
    abgeglichen: '2026-08-10T12:00:00',
    an: [{pfad: 'Dateien/Kunden', elemente: 3, archiv: 3, regel: null}],
    aus: [], weg: [], mails_an: 3, mails_aus: 0, mails_weg: 0}); }});
};
document.getElementById('c-onedrive_rules').value = '- Dateien/Fotos/**';

// Abgleich: eigener Lauf im Lauffenster – kein Zustandstext an einer Karte.
LAUF.eigener = false;
gleicheOrdnerAb('onedrive');
var lauf = gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') >= 0; })[0];
pruefe(JSON.parse(lauf.body).sync_onedrive === true, 'Falscher Abgleich: ' + lauf.body);
pruefe(JSON.parse(lauf.body).label === 'job.folders', 'Etikett fehlt: ' + lauf.body);
pruefe(LAUF.eigener === true, 'Der Abgleich oeffnet das Lauffenster nicht');
pruefe(document.getElementById('od-folders-msg').textContent === '' &&
       document.getElementById('folders-msg').textContent === '',
       'Ein Zustandstext an einer Karte');

// Exportliste: mit den OneDrive-Regeln, nicht mit denen des Postfachs.
gesendet = [];
zeigeExportliste('onedrive');
setTimeout(function(){
  var frage = gesendet.filter(function(g){ return g.pfad.indexOf('folder-plan') >= 0; })[0];
  var b = JSON.parse(frage.body);
  pruefe(frage.pfad.indexOf('/sources/onedrive/folder-plan') >= 0, 'Falsche Quelle: ' + frage.pfad);
  pruefe(b.onedrive_rules === '- Dateien/Fotos/**', 'Regeln aus dem falschen Feld');
  pruefe(b.folder_rules === undefined, 'Postfach-Regeln mitgeschickt');
  pruefe(document.getElementById('plan-listen').innerHTML.indexOf('Dateien/Kunden') >= 0,
         'Liste nicht gezeichnet');
  // Der Knopf im Fenster muss den OneDrive-Abgleich starten, nicht den anderen.
  pruefe(modal.innerHTML.indexOf('planAbgleichen(&quot;onedrive&quot;)') >= 0,
         'Abgleich im Fenster zeigt auf die falsche Quelle');

  // Beim Spiegel sind es Dateien. "Mails" waere schlicht falsch.
  var h = document.getElementById('plan-listen').innerHTML;
  pruefe(h.indexOf('Mails') < 0 && h.indexOf('mails') < 0,
         'Gruppenkopf spricht von Mails: ' + h.slice(0, 200));
  pruefe(h.indexOf('Dateien') >= 0, 'Gruppenkopf nennt die Einheit nicht');
  console.log('OK');
}, 20);
"""


def test_onedrive_ordnerknoepfe_wirken_auf_die_eigene_quelle():
    _in_node(PRUEFUNG_OD_ORDNER)


PRUEFUNG_LAUF_UNGESEHEN = GRUNDZUSTAND + """
// Ein Lauf, der zwischen zwei Abfragen begann und endete – der Zeitplan
// auf einem kleinen Archiv – war nie `busy` zu sehen. Der Bestand muss
// trotzdem neu geholt werden: an ihm haengen Ordner, Typen und Faehigkeiten.
// Jede Statusantwort traegt den jeweils letzten Lauf, wie beim Server.
var geholt = [], LETZTER = null, geruestEcht = statusGeruest;
statusGeruest = function(){ var s = geruestEcht(); s.jobs.last = LETZTER; return s; };
global.fetch = function(pfad, opt){
  geholt.push(String(pfad));
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/v1/inventory') >= 0 ? {store: {exists: true}} : statusGeruest()); }});
};
function bestand(){ return geholt.filter(function(p){ return p.indexOf('/api/v1/inventory') >= 0; }).length; }
renderStatus(statusGeruest());
var n0 = bestand();
LETZTER = {label: 'job.index', ok: true, finished: '2026-09-20T10:00:00', detail: ''};
renderStatus(statusGeruest());
setTimeout(function(){
  pruefe(bestand() === n0 + 1, 'Bestand nach ungesehenem Lauf nicht geholt: ' + geholt.join(' '));
  renderStatus(statusGeruest());
  setTimeout(function(){
    pruefe(bestand() === n0 + 1, 'Derselbe Lauf holt den Bestand noch einmal');
    LETZTER = {label: 'job.index', ok: true, finished: '2026-09-20T11:00:00', detail: ''};
    renderStatus(statusGeruest());
    setTimeout(function(){
      pruefe(bestand() === n0 + 2, 'Ein weiterer Lauf holt den Bestand nicht');
      console.log('OK');
    }, 20);
  }, 20);
}, 20);
"""


def test_ein_ungesehener_lauf_holt_den_bestand_neu():
    _in_node(PRUEFUNG_LAUF_UNGESEHEN)


PRUEFUNG_ANHANG = GRUNDZUSTAND + """
// Der Anhang-Filter (13.1): eine Pille wie "Nur Geloeschtes", nur bei Quelle
// Mail; ein Haken unter anderer Quelle zaehlt nicht und geht nicht raus.
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push(String(pfad));
  return Promise.resolve({ok: true, status: 200, json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/v1/search') >= 0
      ? {items: [], count: 0, limit: 20, offset: 0, has_more: false, backend: 'bm25'} : statusGeruest()); }});
};
function gefragt(){ return gesendet.filter(function(p){ return p.indexOf('/api/v1/search?') >= 0; }).pop() || ''; }
document.getElementById('f-source').value = 'all';
zeigeFilterstand();
pruefe(document.getElementById('p-anhang').classList.contains('hide'), 'Anhang-Pille steht bei allen Quellen');
document.getElementById('f-source').value = 'outlook';
zeigeFilterstand();
pruefe(!document.getElementById('p-anhang').classList.contains('hide'), 'Anhang-Pille fehlt bei Mail');
document.getElementById('f-attach').checked = true;
zeigeFilterstand();
pruefe(document.getElementById('p-anhang').classList.contains('on'), 'Gesetzte Pille leuchtet nicht');
pruefe(kriterienAusForm().attachments === true, 'Kriterium fehlt');
pruefe(document.getElementById('filter-stand').textContent.indexOf('2') >= 0,
       'Zahl der Filter: ' + document.getElementById('filter-stand').textContent);
doSearch(0);
pruefe(gefragt().indexOf('attachments=1') >= 0, 'attachments=1 nicht geschickt: ' + gefragt());
document.getElementById('f-source').value = 'all';
zeigeFilterstand();
pruefe(document.getElementById('p-anhang').classList.contains('hide') && kriterienAusForm().attachments === false,
       'Der Haken zaehlt trotz anderer Quelle');
gesendet = [];
doSearch(0);
pruefe(gefragt().indexOf('attachments=1') < 0, 'attachments=1 trotz anderer Quelle geschickt: ' + gefragt());
// Gespeicherte Kriterien: als Tag, und zurueck ins Formular.
pruefe(kriterienTags({attachments: true}).indexOf(t('search.pill.anhang')) >= 0, 'Tag fehlt');
kriterienAnwenden({source: 'outlook', attachments: true});
pruefe(document.getElementById('f-attach').checked === true, 'Kriterium kommt nicht ins Formular');
pillLeeren('anhang');
pruefe(document.getElementById('f-attach').checked === false, 'Das x leert die Pille nicht');
// Die Bueroklammer in der Liste – unabhaengig vom Filter, mit Namen und Zahl.
TREFFER = [{uid: 'u1', title: 'A', source: 'outlook', attachments: ['Rechnung.pdf', 'Anlage.xlsx']},
           {uid: 'u2', title: 'B', source: 'outlook', attachments: []},
           {uid: 'u3', title: 'C', source: 'teams'}];
zeichneTrefferListe();
var h = document.getElementById('results').innerHTML;
pruefe(h.split('tag anhang').length - 1 === 1, 'Bueroklammer nicht genau einmal: ' + h.slice(0, 400));
pruefe(h.indexOf('Rechnung.pdf, Anlage.xlsx') >= 0, 'Namen fehlen im Tooltip');
// Die Chips im Detail: mit uid und Groesse ein Download, sonst ein Chip.
var c = detailChips([{name: 'Rechnung.pdf', size: 2048}, {name: 'nur-im-index.pdf', size: null}], 'mail:1');
pruefe(c.indexOf('href="/api/v1/documents/attachments?uid=mail%3A1&n=1"') >= 0, 'Download-Link fehlt: ' + c);
pruefe(c.split('<a ').length - 1 === 1, 'Ein Chip ohne Groesse ist ein Link: ' + c);
pruefe(detailChips([{name: 'x.pdf'}]).indexOf('<a ') < 0, 'Ohne uid ein Link');
console.log('OK');
"""


def test_der_anhangfilter_steht_nur_bei_mail_und_die_klammer_immer():
    _in_node(PRUEFUNG_ANHANG)


PRUEFUNG_UPDATE_ABGELEHNT = GRUNDZUSTAND + """
// Eine Ablehnung traegt kein `update`. Die Zeile nennt den Grund, statt
// fuer immer bei "wird geprueft" zu bleiben – und nichts wirft im Stillen.
var geschickt = [];
global.fetch = function(pfad, opt){
  geschickt.push(String(pfad));
  return Promise.resolve({json: function(){ return Promise.resolve(
    {ok: false, status: 409, title: 'Conflict', detail: 'A run is in progress.',
     error: {k: 'srv.busy', v: {}}}); }});
};
process.on('unhandledRejection', function(e){ console.log('REJECTION ' + e); process.exit(1); });
pruefeUpdate();
setTimeout(function(){
  var text = document.getElementById('update-state').textContent;
  pruefe(geschickt.length === 1 && geschickt[0].indexOf('/api/v1/updates/check') >= 0,
         'Keine Anfrage: ' + geschickt);
  pruefe(text.length > 0 && text !== t('update.checking'),
         'Zeile bleibt bei "wird geprueft": ' + text);
  console.log('OK');
}, 20);
"""


def test_eine_abgelehnte_updatepruefung_bleibt_nicht_haengen():
    _in_node(PRUEFUNG_UPDATE_ABGELEHNT)


PRUEFUNG_UPDATE_LIMIT = GRUNDZUSTAND + """
// A failed check says only that it failed; what GitHub answered – and when
// its hourly limit opens again – stands on the mouseover of that line.
var voll = {current: '13.5.0', latest: null, url: null, newer: false, ahead: false};
zeigeUpdate(Object.assign({}, voll, {status: 'error', error: 'HTTP 403',
                                     retry_at: '2026-09-22T10:53:29'}), 'v1');
var zeile = document.getElementById('update-state');
pruefe(zeile.textContent === t('update.error'), 'Zeile: ' + zeile.textContent);
pruefe(zeile.textContent.indexOf('403') < 0, 'Der Code steht in der Zeile: ' + zeile.textContent);
pruefe(zeile.title.indexOf('HTTP 403') >= 0 && zeile.title.indexOf('GitHub') >= 0 &&
       zeile.title.indexOf('10:53') >= 0, 'Mouseover: ' + zeile.title);
zeigeUpdate(Object.assign({}, voll, {status: 'error', error: 'ConnectionError: kein Netz',
                                     retry_at: null}), 'v1');
pruefe(zeile.textContent === t('update.error'), 'Zeile: ' + zeile.textContent);
pruefe(zeile.title === 'ConnectionError: kein Netz', 'Mouseover ohne Limit: ' + zeile.title);
zeigeUpdate(Object.assign({}, voll, {status: 'ok', latest: '13.5.0'}), 'v1');
pruefe(zeile.textContent === t('update.uptodate'), 'Zeile: ' + zeile.textContent);
pruefe(zeile.title === '', 'Der Mouseover bleibt stehen: ' + zeile.title);
console.log('OK');
"""


def test_a_failed_update_check_keeps_its_reason_on_the_mouseover():
    _in_node(PRUEFUNG_UPDATE_LIMIT)


def test_die_mailzeilen_sind_eine_faehigkeit_nicht_eine_spalte():
    """`mail_lines` stands for all of store_layout.MAIL_NEU – the set the
    run gate rebuilds the index for – not for one column of them; the
    page keys the filter on that one word."""
    import store_layout
    alle = {"key", "gone", *store_layout.MAIL_NEU}
    assert app_mod._faehigkeiten(alle) == ["gone", "key", "mail_lines"]
    assert app_mod._faehigkeiten(alle - {store_layout.MAIL_NEU[-1]}) == ["gone", "key"]
    assert "'mail_lines'" in app_mod.seite() and "'to_ppl'" not in app_mod.seite()


def test_die_ki_antwort_steht_so_weit_von_den_pillen_wie_die_treffer():
    """In the AI variant the answer stands where the hits would. It used
    to sit right under the pills while the hit list kept its 16px."""
    css = app_mod.seite()
    assert "#sicht-treffer .treffer-split{margin-top:16px}" in css
    assert "#sicht-treffer .answer{margin-top:16px}" in css


PRUEFUNG_VORABVERSION = GRUNDZUSTAND + """
function lage(u, api){
  // Beide Felder zuruecksetzen: der Browser ersetzt beim Setzen von innerHTML
  // auch den Text, die Attrappe hier nicht - sonst schleppte ein Fall den
  // Inhalt des vorigen mit.
  var b = document.getElementById('update-banner');
  b.textContent = ''; b.innerHTML = '';
  zeigeUpdate(Object.assign({current: '4.0.0', releases_url: 'https://r'}, u), api);
  return {text: document.getElementById('update-state').textContent, banner: b,
          text_inhalt: b.textContent, html_inhalt: b.innerHTML};
}

// 1) Eigene Version ist hoeher als das neueste Release.
var a = lage({status: 'ok', latest: '3.5.0', newer: false, ahead: true});
pruefe(a.text.indexOf('3.5.0') >= 0, 'Nennt die veroeffentlichte Version nicht: ' + a.text);
pruefe(a.text.toLowerCase().indexOf('latest version.') < 0,
       'Behauptet weiterhin "auf dem neuesten Stand": ' + a.text);
pruefe(!a.banner.classList.contains('hide'), 'Kein Hinweis eingeblendet');
pruefe(a.banner.classList.contains('warn'), 'Hinweis ist nicht als Warnung erkennbar');
pruefe(a.text_inhalt.length > 40, 'Hinweistext fehlt');

// Die Build-Kennung steht neben der Version – und fehlt, wo es keine gibt.
lage({status: 'ok', latest: '4.0.0', newer: false, ahead: false, build: 'e727567'});
var zeile = document.getElementById('update-current').textContent;
pruefe(zeile.indexOf('4.0.0') >= 0 && zeile.indexOf('e727567') >= 0, 'Build fehlt neben der Version: ' + zeile);
lage({status: 'ok', latest: '4.0.0', newer: false, ahead: false, build: ''});
zeile = document.getElementById('update-current').textContent;
pruefe(zeile.indexOf('4.0.0') >= 0 && zeile.toLowerCase().indexOf('build') < 0, 'Leere Build-Kennung wird gezeigt: ' + zeile);

// Drei Angaben in einer Zeile: Programm, Build und der Vertrag, unter dem
// die API antwortet – der ist fuer eigene Skripte die wichtigste.
lage({status: 'ok', latest: '4.0.0', newer: false, ahead: false, build: 'e727567'}, 'v1');
zeile = document.getElementById('update-current').textContent;
pruefe(zeile.indexOf('4.0.0') >= 0 && zeile.indexOf('e727567') >= 0
       && zeile.indexOf('API v1') >= 0, 'API-Version fehlt in der Zeile: ' + zeile);

// 2) Normales Update: unveraendert, und KEINE Warnfarbe.
var b = lage({status: 'ok', latest: '5.0.0', newer: true, ahead: false});
pruefe(!b.banner.classList.contains('hide'), 'Update-Hinweis fehlt');
pruefe(!b.banner.classList.contains('warn'), 'Update faelschlich als Warnung');
pruefe(b.html_inhalt.indexOf('5.0.0') >= 0, 'Neue Version nicht genannt');
pruefe(b.html_inhalt.indexOf('<a href') >= 0, 'Link zum Release fehlt');

// 3) Gleichstand: kein Hinweis, und der alte Text bleibt.
var c = lage({status: 'ok', latest: '4.0.0', newer: false, ahead: false});
pruefe(c.banner.classList.contains('hide'), 'Hinweis bei Gleichstand');
pruefe(c.text.length > 0, 'Zustand gar nicht gemeldet');

// 4) Kein Netz: nichts behaupten.
var d = lage({status: 'error', latest: null, newer: false, ahead: false, error: 'weg'});
pruefe(d.banner.classList.contains('hide'), 'Hinweis trotz Fehler');
console.log('OK');
"""


def test_vorabversion_wird_nicht_als_aktuell_ausgegeben():
    _in_node(PRUEFUNG_VORABVERSION)


def test_onedrive_pruefschritt(sandbox):
    cfg = _cfg_mit_kategorien(onedrive_enabled=True)
    schritt = next(s for s in app_mod.build_steps(cfg, {"check_onedrive": True})
                   if s["key"] == "check_onedrive")
    argv = [str(x) for x in schritt["argv"]]
    assert "--check" in argv and "onedrive_export" in " ".join(argv)


def test_ein_pruefknopf_prueft_beides_in_einem_lauf(sandbox):
    """The mailbox first: it is the main source and belongs at the top of
    the log."""
    cfg = _cfg_mit_kategorien(onedrive_enabled=True)
    keys = [s["key"] for s in app_mod.build_steps(cfg, {"check": True, "check_onedrive": True})]
    assert keys == ["check", "check_onedrive"]


def test_vollstaendigkeit_hat_genau_einen_knopf():
    """Two buttons first forced the user to decide what they actually want
    to know – "check" is a question to the archive, not to a source."""
    assert 'onclick="pruefeVollstaendigkeit()"' in app_mod.seite()
    assert "pruefeVollstaendigkeit('onedrive')" not in app_mod.seite()
    assert app_mod.seite().count("pruefeVollstaendigkeit(") == 2   # call + definition


def test_analytics_liefert_die_bilanz_je_quelle(server, sandbox):
    """One entry per balance row: the last report (null before the first
    check) and whether the source is in use."""
    a, port = server
    import completeness
    import state_db
    completeness.schreiben(state_db.StateDb(sandbox / app_mod.OUTLOOK_DIR),
                           completeness.bilanz("outlook_mail", "mails", da=5, offen=1))
    completeness.schreiben(state_db.StateDb(sandbox / app_mod.ONEDRIVE_DIR),
                           completeness.bilanz("onedrive", "files", da=9))
    r = call(port, "GET", "/api/v1/analytics")[1]["pruefungen"]
    assert list(r) == [e["quelle"] for e in app_mod.steps_mod.PRUEFUNGEN]
    assert r["outlook_mail"]["bericht"]["offen"] == 1 and r["outlook_mail"]["genutzt"]
    assert r["onedrive"]["bericht"]["da"] == 9 and not r["onedrive"]["genutzt"]
    assert r["outlook_calendar"]["bericht"] is None and r["outlook_calendar"]["genutzt"]
    assert r["todo"]["bericht"] is None and not r["todo"]["genutzt"]


def test_die_bilanz_hat_ihren_eigenen_ort_und_ihre_zeilen(server, sandbox):
    """`/api/v1/balance/{row}` takes the balance row, as its `/fetch` does:
    the mailbox check writes `outlook_mail`, never `outlook`. A route
    under /sources resolving `outlook` could only ever have answered
    null – which is why the rows have a path of their own."""
    _, port = server
    import completeness
    import state_db
    with state_db.StateDb(sandbox / app_mod.OUTLOOK_DIR) as db:
        completeness.schreiben(db, completeness.bilanz("outlook_mail", "mails", da=5, offen=1))
    code, r = call(port, "GET", "/api/v1/balance/outlook_mail")
    assert code == 200 and r["report"]["offen"] == 1 and r["report"]["quelle"] == "outlook_mail"
    code, r = call(port, "GET", "/api/v1/balance/outlook_calendar")
    assert code == 200 and r["report"] is None
    for weg in ("outlook", "nonesuch"):
        code, r = call(port, "GET", f"/api/v1/balance/{weg}")
        assert code == 404 and r["error"]["k"] == "srv.archiv.unknown", weg
        assert r["error"]["v"]["source"] == weg
    # And nothing of it is left under /sources.
    assert call(port, "GET", "/api/v1/sources/sharepoint/completeness")[0] == 404
    assert call(port, "POST", "/api/v1/sources/onedrive/fetch", {})[0] == 404


def test_der_ordnerplan_kennt_die_acht_quellen_und_das_postfach_zwei_einheiten(server):
    """`{source}` under /sources means the same eight everywhere. A source
    without rules says so, a name that is none is unknown – and the
    mailbox's calendars are its second unit, not a source of their own.
    A name outside the list used to fall through to the mailbox plan."""
    _, port = server
    for quelle in ("planner", "sharepoint_pages"):
        code, r = call(port, "QUERY", f"/api/v1/sources/{quelle}/folder-plan", {})
        assert code == 404 and r["error"]["k"] == "srv.plan.nosource", (quelle, r)
    code, r = call(port, "QUERY", "/api/v1/sources/bogus/folder-plan", {})
    assert code == 404 and r["error"]["k"] == "srv.archiv.unknown", r
    for quelle, unit in (("onedrive", "calendar"), ("outlook", "contacts"), ("outlook", 1)):
        code, r = call(port, "QUERY", f"/api/v1/sources/{quelle}/folder-plan", {"unit": unit})
        assert code == 400 and r["error"]["k"] == "srv.plan.badunit", (quelle, unit, r)


PRUEFUNG_PRUEFKNOPF = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body,
                 methode: (opt && opt.method) || 'GET'});
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};
// The card's own selection: every row chosen, every source with its
// addresses set – the button asks every check, and names the rows.
var ALLE = ['outlook_mail', 'outlook_calendar', 'outlook_contacts', 'teams', 'onedrive',
            'sharepoint', 'sharepoint_pages', 'planner', 'todo', 'onenote'];
KONFIG = Object.assign({}, KONFIG, {check_sources: ALLE.slice(), sharepoint_urls: 'https://firma.sharepoint.com/sites/x',
                                    sharepoint_pages_urls: 'https://firma.sharepoint.com/sites/x', planner_urls: 'https://tasks.example/plan'});
pruefeVollstaendigkeit();
var a = JSON.parse(gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') >= 0; })[0].body);
['check', 'check_teams', 'check_onedrive', 'check_sharepoint', 'check_pages',
 'check_planner', 'check_todo', 'check_onenote'].forEach(function(k){
  pruefe(a[k] === true, 'Pruefung nicht angefragt: ' + k + ' ' + JSON.stringify(a));
});
pruefe(JSON.stringify(a.check_rows) === JSON.stringify(ALLE), 'Zeilen nicht genannt: ' + JSON.stringify(a.check_rows));
pruefe(a.label === 'job.check', 'falsches Etikett');
pruefe(!a.outlook && !a.index, 'Export oder Index mitgestartet');
console.log('OK');
"""


def test_ein_pruefknopf_fragt_jede_pruefung_an():
    _in_node(PRUEFUNG_PRUEFKNOPF)


CHECK_SOURCE_CHOICE = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  var body = opt && opt.body ? JSON.parse(opt.body) : null;
  gesendet.push({pfad: String(pfad), body: body, methode: (opt && opt.method) || 'GET'});
  // The configuration answers as the server does: the whole configuration, as saved
  var antwort = String(pfad).indexOf('/api/v1/config') >= 0 && body
    ? {config: Object.assign({}, KONFIG, body)} : statusGeruest();
  return Promise.resolve({json: function(){ return Promise.resolve(antwort); }});
};
function lastCall(path, method){
  return gesendet.filter(function(g){ return g.pfad.indexOf(path) >= 0 && g.methode === method; }).pop();
}
// Only mail, calendar and OneDrive chosen; SharePoint has no addresses yet
KONFIG = Object.assign({}, KONFIG, {check_sources: ['outlook_mail', 'outlook_calendar', 'onedrive', 'sharepoint'],
                                    sharepoint_urls: '', planner_urls: 'https://tasks.example/plan'});
zeigeBerichte({pruefungen: {}});
var html = el('ana-check-quellen').innerHTML;
pruefe(html.split('class="chip').length - 1 === 10, 'nicht zehn Chips: ' + html);
pruefe(html.indexOf("checkSourceToggle('onedrive', this.checked)") >= 0, 'Chip ohne Schalter');
// a source without addresses is greyed with the reason, and never asked
pruefe(html.indexOf('class="chip aus"') >= 0 && html.indexOf(t('ana.check.needs.urls')) >= 0, 'SharePoint ohne Adressen nicht gegraut');
pruefe(JSON.stringify(checkRowsChosen()) === JSON.stringify(['outlook_mail', 'outlook_calendar', 'onedrive']), 'gewaehlte Zeilen: ' + JSON.stringify(checkRowsChosen()));
pruefeVollstaendigkeit();
var a = lastCall('/api/v1/runs', 'POST').body;
pruefe(a.check === true && a.check_onedrive === true && !a.check_teams && !a.check_sharepoint && !a.check_todo, 'nur die gewaehlten Pruefungen: ' + JSON.stringify(a));
pruefe(JSON.stringify(a.check_rows) === JSON.stringify(['outlook_mail', 'outlook_calendar', 'onedrive']), 'Zeilen: ' + JSON.stringify(a.check_rows));
// a chip off: the setting travels, the button follows
checkSourceToggle('onedrive', false);
var c = lastCall('/api/v1/config', 'PATCH').body;
pruefe(JSON.stringify(c.check_sources) === JSON.stringify(['outlook_mail', 'outlook_calendar', 'sharepoint']), 'Einstellung nicht geschickt: ' + JSON.stringify(c));
gesendet.length = 0;
pruefeVollstaendigkeit();
a = lastCall('/api/v1/runs', 'POST').body;
pruefe(a.check === true && !a.check_onedrive, 'OneDrive trotz Abwahl geprueft: ' + JSON.stringify(a));
// none: the button is off and asks nothing; all: every possible row
checkSourcesAll(false);
pruefe(el('ana-check').disabled === true, 'Knopf ohne Auswahl nicht aus');
gesendet.length = 0;
pruefeVollstaendigkeit();
pruefe(!lastCall('/api/v1/runs', 'POST'), 'Lauf ohne Auswahl gestartet');
checkSourcesAll(true);
c = lastCall('/api/v1/config', 'PATCH').body;
// eight: SharePoint's files and pages have no addresses here, Planner has
pruefe(c.check_sources.length === 8 && c.check_sources.indexOf('sharepoint') < 0 && c.check_sources.indexOf('sharepoint_pages') < 0 && c.check_sources.indexOf('planner') >= 0, '"Alle" nimmt Unmoegliches mit: ' + JSON.stringify(c.check_sources));
pruefe(el('ana-check').disabled === false, 'Knopf nach "Alle" noch aus');
// a source the export does not fetch stays a choice of the card – never greyed for that
zeigeBerichte({pruefungen: {todo: {genutzt: false, bericht: null}, onedrive: {genutzt: true, bericht: null}}});
html = el('ana-check-quellen').innerHTML;
pruefe(html.split('class="chip aus"').length - 1 === 2, 'Unbenutztes gegraut: ' + html);
pruefe(checkRowsChosen().indexOf('todo') >= 0, 'To-Do ohne Export nicht waehlbar: ' + JSON.stringify(checkRowsChosen()));
console.log('OK');
"""


def test_the_check_has_its_own_source_choice():
    _in_node(CHECK_SOURCE_CHOICE)


def test_the_check_follows_the_rows_of_the_card(sandbox):
    """The card chooses on its own: a row ticked there is checked whether
    or not the export fetches the source – only a source that needs
    addresses waits for them. The mailbox check takes exactly the rows'
    categories, the Teams check the ticked kinds (every kind when none
    is ticked). Without rows the export's switches decide alone, as the
    size preview expects. Every step names its source."""
    every = {k: True for k in ("check", "check_teams", "check_onedrive", "check_sharepoint",
                              "check_pages", "check_planner", "check_todo", "check_onenote")}
    rows = ["outlook_calendar", "outlook_contacts", "teams", "onedrive", "sharepoint", "todo"]
    cfg = app_mod.load_config()          # nothing ticked at all
    steps = app_mod.build_steps(cfg, every, check_rows=rows)
    assert [s["key"] for s in steps] == ["check", "check_teams", "check_onedrive", "check_todo"]
    assert steps[0]["env"]["EXPORT_CATEGORIES"] == "calendar,contacts"
    assert steps[1]["env"]["EXPORT_CATEGORIES"] == "1on1,group,meeting,channels", "no kind ticked: every kind"
    assert [s["label"] for s in steps] == ["job.step.check.outlook", "job.step.check.teams",
                                           "job.step.check.onedrive", "job.step.check.todo"]
    cfg = _cfg_mit_kategorien()          # three Teams kinds ticked: the check takes those
    steps = app_mod.build_steps(cfg, every, check_rows=["teams"])
    assert [s["env"]["EXPORT_CATEGORIES"] for s in steps] == ["1on1,group,meeting"]
    # a URL source asked for by row needs its addresses and nothing else
    cfg["sharepoint_urls"] = "https://firma.sharepoint.com/sites/x"
    steps = app_mod.build_steps(cfg, every, check_rows=rows)
    assert "check_sharepoint" in [s["key"] for s in steps]
    # rows that name no mailbox category: no mailbox step
    steps = app_mod.build_steps(cfg, every, check_rows=["todo"])
    assert [s["key"] for s in steps] == ["check_todo"]
    # without rows: the export's switches decide, as before – and the
    # addresses alone, which is what the size preview needs
    cfg["todo_enabled"] = True
    steps = app_mod.build_steps(cfg, every)
    assert [s["key"] for s in steps] == ["check", "check_teams", "check_sharepoint", "check_todo"]
    assert steps[0]["env"]["EXPORT_CATEGORIES"] == "mail,calendar,contacts"
    assert steps[1]["env"]["EXPORT_CATEGORIES"] == "1on1,group,meeting"


def test_check_rows_over_the_api(server, sandbox, monkeypatch):
    """The setting keeps only known rows, in the registry's order; a run
    request hands the rows to the steps."""
    a, port = server
    code, r = call(port, "PATCH", "/api/v1/config", {"check_sources": ["nix", "onedrive", "outlook_mail"]})
    assert code == 200 and r["config"]["check_sources"] == ["outlook_mail", "onedrive"]
    seen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: seen.update(steps=steps, label=label) or True)
    monkeypatch.setattr(app_mod, "read_token", lambda path=None: "tok")   # a check talks to Graph
    code, r = call(port, "POST", "/api/v1/runs",
                   {"check": True, "check_onedrive": True, "check_todo": True,
                    "check_rows": ["outlook_mail", "onedrive", "todo"], "label": "job.check"})
    assert code == 202, r
    assert [s["key"] for s in seen["steps"]] == ["check", "check_onedrive", "check_todo"]
    assert seen["steps"][0]["env"]["EXPORT_CATEGORIES"] == "mail"
    assert seen["label"] == "job.check"


SETTINGS_BAR_ACCESS = GRUNDZUSTAND + """
// The access card saves itself: a key, the mode or the registration typed
// there is never an unsaved setting, so the settings bar stays down.
cfgGefuellt = true;
el('speichern').classList.add('hide');
markiereGeaendert({target: {id: 'tok', parentNode: {id: 'zugang-inhalt', parentNode: null}}});
pruefe(el('speichern').classList.contains('hide'), 'the key raises the settings bar');
markiereGeaendert({target: {id: 'au-client', parentNode: {id: 'au-form',
                   parentNode: {id: 'zugang-inhalt', parentNode: null}}}});
pruefe(el('speichern').classList.contains('hide'), 'the registration raises the settings bar');
markiereGeaendert({target: {id: 'c-port', parentNode: {id: 'tab-einstellungen', parentNode: null}}});
pruefe(!el('speichern').classList.contains('hide'), 'a setting does not raise the bar');
console.log('OK');
"""


def test_the_access_card_never_raises_the_settings_bar():
    _in_node(SETTINGS_BAR_ACCESS)


PRUEFUNG_BILANZ = GRUNDZUSTAND + """
function b(extra){
  return Object.assign({quelle: 'onedrive', einheit: 'files', geprueft: '2026-08-10T18:46:00',
    stand: 'ganz', grund: null, da: 420, offen: 0, ausgeschlossen: 0,
    ausgeschlossen_einheit: 'files', behalten: 0, wartend: 0, zeilen: [], fehler: []}, extra);
}
// All here: one sentence, unit word from the report, nothing about zeros.
var s = bilanzSatz(b());
pruefe(s.dot === 'ok' && s.text.indexOf('420') >= 0 && s.text.indexOf('Dateien') >= 0, 'Alles-da-Satz: ' + s.text);
pruefe(s.text.indexOf('ausgeschlossen') < 0 && s.text.indexOf('behalten') < 0, 'Nullen genannt: ' + s.text);
// Refused or gone: named, never open, nothing to warn about.
s = bilanzSatz(b({verweigert: 2, weg: 1}));
pruefe(s.dot === 'ok' && s.text.indexOf('2 von Microsoft verweigert') >= 0 && s.text.indexOf('1 vor dem Holen verschwunden') >= 0,
       'Urteile fehlen im Satz: ' + s.text);
// Open, excluded, kept, waiting – a number each, no names.
s = bilanzSatz(b({offen: 12, ausgeschlossen: 208, behalten: 3, wartend: 40}));
pruefe(s.dot === 'warn', 'offen ohne Warnpunkt');
['12', '208', '3', '40', 'noch nicht geholt', 'bewusst ausgeschlossen', 'behalten', 'Häufigkeit'].forEach(function(w){
  pruefe(s.text.indexOf(w) >= 0, 'fehlt im Satz: ' + w + ' – ' + s.text);
});
pruefe(s.text.indexOf('Fotos') < 0, 'Ordnername im Satz');
// Excluded in another unit: calendars, not events.
s = bilanzSatz(b({quelle: 'outlook_calendar', einheit: 'events', ausgeschlossen: 4, ausgeschlossen_einheit: 'calendars'}));
pruefe(s.text.indexOf('4 Kalender') >= 0, 'Einheit der Ausgeschlossenen: ' + s.text);
// Teams speaks of newer messages, not of "not fetched".
s = bilanzSatz(b({quelle: 'teams', einheit: 'conversations', offen: 3}));
pruefe(s.text.indexOf('neueren Nachrichten') >= 0, 'Teams-Satz: ' + s.text);
// Partly checked and not checked at all.
s = bilanzSatz(b({stand: 'teilweise', fehler: [{pfad: 'S/B', grund: 'x'}]}));
pruefe(s.text.indexOf('Nicht ganz geprüft: 1') >= 0, 'teilweise: ' + s.text);
s = bilanzSatz(b({stand: 'nicht', grund: 'ana.check.reason.budget'}));
pruefe(s.dot === 'aus' && s.text.indexOf('Kontingent') >= 0, 'nicht geprüft: ' + s.text);

// The rows: a used source without a report says so, an unused one without
// a report is not drawn, "fetch now" only where something is open.
zeigeBerichte({pruefungen: {
  outlook_mail: {bericht: b({quelle: 'outlook_mail', einheit: 'mails', offen: 12,
                             zeilen: [{pfad: 'E-Mail/Posteingang', da: 100, offen: 12}]}), genutzt: true},
  outlook_calendar: {bericht: null, genutzt: true},
  onedrive: {bericht: null, genutzt: false},
  teams: {bericht: b({quelle: 'teams', einheit: 'conversations'}), genutzt: false}
}});
var html = el('ana-checks').innerHTML;
pruefe(html.indexOf('Postfach') >= 0 && html.indexOf('Kalender') >= 0, 'Zeilen fehlen');
pruefe(html.indexOf('OneDrive') < 0, 'Unbenutzte Quelle ohne Bericht gezeichnet');
pruefe(html.indexOf('Teams') >= 0, 'Bericht einer nicht mehr genutzten Quelle verschwunden');
pruefe(html.indexOf('Noch nicht geprüft') >= 0, 'Genutzte Quelle ohne Bericht sagt nichts');
pruefe(html.split('holeQuelle(').length === 2, 'Jetzt holen nicht genau einmal: ' + html.split('holeQuelle(').length);
pruefe(html.indexOf('E-Mail/Posteingang') >= 0 && html.indexOf('<details') >= 0, 'Offene Ordner nicht eingeklappt');
pruefe(html.indexOf('erwartet') < 0 && html.indexOf('vorhanden') < 0, 'Buchhaltungswort auf der Seite');

// "Fetch now" hands the row to the server – it knows the cheapest way –
// and the window opens with the run.
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body ? JSON.parse(opt.body) : null});
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/v1/status') >= 0 ? statusGeruest() : {ok: true, message: null}); }});
};
pruefe(html.indexOf('holeQuelle(&quot;outlook_mail&quot;)') >= 0, 'Knopf nennt die Zeile nicht: ' + html);
LAUF.eigener = false;
holeQuelle('outlook_mail');
pruefe(gesendet[0].pfad === '/api/v1/balance/outlook_mail/fetch',
       'Jetzt holen: ' + JSON.stringify(gesendet[0]));
pruefe(LAUF.eigener === true, 'Jetzt holen oeffnet kein Fenster');
// The navigation dot: warn while something is open, ok when all agrees.
pruefe(el('p-ana-check').className.indexOf('warn') >= 0, 'Punkt nicht warn: ' + el('p-ana-check').className);
zeigeBerichte({pruefungen: {outlook_mail: {genutzt: true, bericht: {quelle: 'outlook_mail', einheit: 'mails', stand: 'ganz', da: 5, offen: 0, ausgeschlossen: 0, behalten: 0, wartend: 0, zeilen: [], fehler: [], geprueft: 'x'}}}});
pruefe(el('p-ana-check').className === 'dot ok', 'Punkt nicht ok: ' + el('p-ana-check').className);
zeigeInsight('ana-check-karte');
console.log('OK');
"""


def test_bilanzzeile_spricht_in_saetzen_und_zahlen():
    _in_node(PRUEFUNG_BILANZ)


def test_archivpruefung_ist_ein_schritt_ohne_zugang(sandbox):
    """The inward check needs no Graph: the step runs without a token and
    writes its report next to the settings, never into an export folder."""
    cfg = _cfg_mit_kategorien()
    (schritt,) = app_mod.build_steps(cfg, {"check_archive": True})
    argv = [str(x) for x in schritt["argv"]]
    assert "archive_check" in " ".join(argv) and "--report" in argv
    assert argv[argv.index("--report") + 1] == str(app_mod.archiv_bericht_pfad())
    assert str(sandbox) in argv[argv.index("--report") + 1], "the report leaves the profile"
    assert not app_mod.steps_mod.braucht_zugang({"check_archive": True})
    assert schritt.get("corpus") is not True


def test_analytics_traegt_die_archivpruefung(server, sandbox):
    a, port = server
    assert call(port, "GET", "/api/v1/analytics")[1]["archiv"] is None
    app_mod.archiv_bericht_pfad().write_text('{"geprueft": "2026-09-13T10:00:00+00:00", "quellen": [], '
                                      '"index": {"stand": "nicht", "grund": "ana.archiv.reason.noindex"}}',
                                      encoding="utf-8")
    r = call(port, "GET", "/api/v1/analytics")[1]["archiv"]
    assert r["index"]["grund"] == "ana.archiv.reason.noindex"


def test_uebersicht_hat_eine_hauptaktion_und_die_archivpruefung_ist_keine():
    """DESIGN.md §1: one primary action per screen – *Check now*. The
    inward check is a secondary button beside it."""
    seite = app_mod.seite()
    block = seite[seite.index('<section id="tab-analytics"'):seite.index('<section id="tab-einstellungen"')]
    assert block.count('class="act"') == 1 and 'id="ana-check"' in block
    assert 'class="ghost" id="ana-archiv" onclick="pruefeArchiv()"' in block


PRUEFUNG_ARCHIV = GRUNDZUSTAND + """
function q(extra){
  return Object.assign({quelle: 'onedrive', einheit: 'files', stand: 'ganz', grund: null,
    stimmig: 4200, fehlt: 0, fremd: 0, unvollstaendig: 0, verloren: 0, zeilen: [], fehler: []}, extra);
}
var s = archivSatz(q());
pruefe(s.dot === 'ok' && s.text.indexOf('4.200') >= 0 && s.text.indexOf('stimmen') >= 0, 'Alles-gut-Satz: ' + s.text);
s = archivSatz(q({fehlt: 3, fremd: 2, unvollstaendig: 1, verloren: 4}));
pruefe(s.dot === 'warn', 'Befund ohne Warnpunkt');
['3 Dateien fehlen', '2 ohne Buchhaltung', '1 unvollständig', '4 verloren', 'Nachholen holt sie'].forEach(function(w){
  pruefe(s.text.indexOf(w) >= 0, 'fehlt im Satz: ' + w + ' – ' + s.text);
});
s = archivSatz(q({stand: 'nicht', grund: 'ana.archiv.reason.db'}));
pruefe(s.dot === 'aus' && s.text.indexOf('beschädigt') >= 0, 'nicht geprüft: ' + s.text);
// refused by Microsoft, or gone before fetched: their own numbers, nothing to warn about
s = archivSatz(q({verweigert: 2, weg: 3}));
pruefe(s.dot === 'ok' && s.text.indexOf('2 Zugriff verweigert') >= 0 && s.text.indexOf('stimmen') >= 0, 'Verweigerte fehlen im Satz oder warnen: ' + s.text);
pruefe(s.text.indexOf('3 vor dem Holen verschwunden') >= 0, 'Verschwundene fehlen im Satz: ' + s.text);

zeigeArchiv({geprueft: '2026-09-13T10:00:00+00:00',
  quellen: [q({quelle: 'outlook', fehlt: 1, zeilen: [{pfad: 'E-Mail/Posteingang', fehlt: 1, fremd: 0, unvollstaendig: 0, verloren: 0}]}),
            q({quelle: 'sharepoint_pages'})],
  index: {stand: 'ganz', grund: null, geaendert: 40, neu: 3, weg: 1, gebaut: '2026-09-12T20:00:00+00:00'}});
var html = el('ana-archiv-zeilen').innerHTML;
pruefe(html.indexOf('Outlook') >= 0 && html.indexOf('SharePoint-Seiten') >= 0, 'Zeilen fehlen');
pruefe(html.indexOf('E-Mail/Posteingang') >= 0 && html.indexOf('<details') >= 0, 'Befunde nicht eingeklappt');
pruefe(html.indexOf('40') >= 0 && html.indexOf('geändert') >= 0, 'Index-Zeile fehlt: ' + html.slice(-400));
pruefe(html.indexOf('run({index:true}') >= 0, 'Nur Index nicht angeboten');
pruefe(html.indexOf('&quot;nachholen&quot;') >= 0 && html.indexOf('Nachholen') >= 0, 'Nachholen fehlt bei fehlenden Dateien');
pruefe(html.indexOf('Jetzt holen') < 0, 'Die Archivpruefung spricht wie die Bilanz');
zeigeArchiv({geprueft: 'x', quellen: [], index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0, gebaut: null}});
pruefe(el('ana-archiv-zeilen').innerHTML.indexOf('run({index:true}') < 0, 'Nur Index trotz aktuellem Index');
zeigeArchiv(null);
pruefe(el('ana-archiv-zeilen').innerHTML.indexOf('Noch nicht geprüft') >= 0, 'Ohne Bericht kein Hinweis');

var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body ? JSON.parse(opt.body) : null});
  return Promise.resolve({json: function(){ return Promise.resolve({ok: true}); }});
};
pruefeArchiv();
var lauf = gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') >= 0; })[0].body;
pruefe(lauf.check_archive === true && lauf.label === 'job.archive_check' && !lauf.check, 'Archivpruefung: ' + JSON.stringify(lauf));
console.log('OK');
"""


def test_archivzeile_spricht_in_saetzen():
    _in_node(PRUEFUNG_ARCHIV)


EVIDENCE_ON_THE_PAGE = GRUNDZUSTAND + """
// The evidence row: no chain yet, all well, deviations, a broken chain
var row = evidenceRow(null);
pruefe(row.indexOf('dot aus') >= 0 && row.indexOf('nächsten Lauf') >= 0, 'no chain: ' + row);
row = evidenceRow({exists: true, chain_ok: true, unchanged: 4200, changed_n: 0, missing_n: 0,
                   since: '2026-09-01T10:00:00+00:00', versions: 3, versions_bytes: 2048, outside: []});
pruefe(row.indexOf('dot ok') >= 0 && row.indexOf('4.200') >= 0, 'all well: ' + row);
pruefe(row.indexOf('evidenceFindings') < 0, 'a findings button with nothing to show');
pruefe(row.indexOf('3 frühere Fassungen') >= 0, 'kept versions missing: ' + row);
row = evidenceRow({exists: true, chain_ok: true, unchanged: 10, changed_n: 2, missing_n: 1,
                   changed: [{rel: 'onedrive_export/a.txt'}], missing: [{rel: 'teams_export/b.html'}],
                   outside: [], versions: 0, versions_bytes: 0, stamped: '2026-09-02T10:00:00+00:00'});
pruefe(row.indexOf('dot warn') >= 0 && row.indexOf('2 verändert') >= 0 && row.indexOf('1 fehlen') >= 0, 'deviations: ' + row);
pruefe(row.indexOf('evidenceFindings()') >= 0 && row.indexOf('gestempelt') >= 0, 'findings or stamp missing: ' + row);
row = evidenceRow({exists: true, chain_ok: false, broken_at: 17, unchanged: 1, changed_n: 0, missing_n: 0, outside: []});
pruefe(row.indexOf('dot err') >= 0 && row.indexOf('17') >= 0, 'broken chain: ' + row);
pruefe(evidenceSource('onedrive_export/Dateien/a.txt') === 'onedrive' && evidenceSource('sharepoint_pages/x.html') === 'sharepoint_pages',
       'the source of an evidence path');
// The Insights dot follows the chain as well
zeigeArchiv({geprueft: 'x', quellen: [], index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0},
             nachweis: {exists: true, chain_ok: true, unchanged: 1, changed_n: 1, missing_n: 0, outside: []}});
pruefe(el('ana-archiv-zeilen').innerHTML.indexOf('evidence-row') >= 0, 'the evidence row is not drawn');

// A case item that changed since it came in: the mark and its compare button
var e = {key: 'file:1', title: 'plan.md', changed: true, added: '2026-09-01T10:00:00+00:00', pinned: 'ab'};
pruefe(changedMark(e).indexOf('seit Aufnahme geändert') >= 0, 'changed mark: ' + changedMark(e));
pruefe(changedMark({key: 'x'}) === '', 'a mark on an unchanged item');
pruefe(eintragAktionen(e, true).indexOf("compareVersions('file:1')") >= 0, 'compare button missing');
pruefe(eintragAktionen({key: 'x'}, true).indexOf('compareVersions') < 0, 'compare on an unchanged item');
// A closed case's manifest line
pruefe(caseEvidenceLine({status: 'open', evidence: {at: 'x'}}) === '', 'an open case has no manifest line');
pruefe(caseEvidenceLine({status: 'closed', evidence: {at: '2026-09-01T10:00:00+00:00', chained: null, stamp: null}})
       .indexOf('nächsten Lauf') >= 0, 'not yet chained');
pruefe(caseEvidenceLine({status: 'closed', evidence: {at: '2026-09-01T10:00:00+00:00', chained: 'x', stamp: 'a.tsr'}})
       .indexOf('Zeitstempel') >= 0, 'stamped');

// The versions fold and a version shown with its difference
var asked = [];
global.fetch = function(path){
  asked.push(String(path));
  var answer = String(path).indexOf('/diff') >= 0
    ? {from: 'b', to: 'a', ops: [['=', 'Draft '], ['-', '1'], ['+', '2']]}
    : {unit: 'file', format: 'text', items: [
        {sha256: 'aaaaaaaaaaaaaaaa', size: 10, captured: '2026-09-02T10:00:00+00:00', modified: '2026-09-02T09:00:00+00:00', current: true, available: true},
        {sha256: 'bbbbbbbbbbbbbbbb', size: 9, captured: '2026-09-01T10:00:00+00:00', modified: '2026-09-01T09:00:00+00:00', current: false, available: true},
        {sha256: 'cccccccccccccccc', size: 9, captured: null, modified: null, current: false, available: false}]};
  return Promise.resolve({ok: true, status: 200, headers: {get: function(){ return null; }},
                          json: function(){ return Promise.resolve(answer); }});
};
DETAIL_STAND = 1;
el('detail-versions').innerHTML = '';
el('detail-text').innerHTML = 'today';
DETAIL_BODY = 'today';
loadVersions({uid: 'datei:a.txt:0'}, 1, {list: 'detail-versions', view: 'detail-text', back: 'detail'}).then(function(){
  var fold = el('detail-versions').innerHTML;
  pruefe(asked[0].indexOf('/documents/versions?uid=datei%3Aa.txt%3A0') >= 0, 'versions asked for: ' + asked[0]);
  pruefe(fold.indexOf('Fassungen: 3') >= 0 && fold.indexOf('aktuell') >= 0 && fold.indexOf('nur Prüfsumme') >= 0, 'fold: ' + fold);
  pruefe(fold.indexOf('showVersion(2)') < 0, 'a version without bytes is clickable');
  showVersion(1);
  return new Promise(function(r){ setTimeout(r, 10); });
}).then(function(){
  pruefe(asked[1].indexOf('/documents/versions/diff?uid=') >= 0 && asked[1].indexOf('&sha=bbbbbbbbbbbbbbbb') >= 0, 'diff asked for: ' + asked[1]);
  // The stub keeps a redrawn part under its own id (see AGENTS.md)
  var view = document.getElementById('detail-text-body').innerHTML;
  pruefe(view.indexOf('<del>1</del><ins>2</ins>') >= 0, 'the difference is not drawn: ' + view);
  pruefe(el('detail-text').innerHTML.indexOf('version-bar') >= 0, 'the version bar is missing');
  pruefe(versionShown(), 'a chosen version does not count as shown');
  versionBack();
  pruefe(el('detail-text').innerHTML === 'today' && !versionShown(), 'back to current');
  console.log('OK');
});
"""


def test_evidence_and_versions_on_the_page():
    _in_node(EVIDENCE_ON_THE_PAGE)


def _outlook_mit_befunden(sandbox):
    """An Outlook folder with one mail the log knows, one foreign file and
    one lost tombstone."""
    import state_db
    out = sandbox / app_mod.OUTLOOK_DIR
    db = state_db.StateDb(out)
    done = state_db.DbDoneLog(db)
    (out / "E-Mail/Posteingang").mkdir(parents=True, exist_ok=True)
    (out / "E-Mail/Posteingang/a.eml").write_bytes(b"x")
    done.mark("m1", "E-Mail/Posteingang/a.eml")
    done.close()
    (out / "E-Mail/Posteingang/fremd.eml").write_bytes(b"f")
    db.verschwunden_ergaenzen(["E-Mail/Alt/weg.eml"], "2026-01-01")
    return out


def test_http_archivaktionen_bewegen_nur_beiseite_und_loeschen_nie(server, sandbox, monkeypatch):
    """Every action over HTTP is a run of its own: the route starts one
    step of archive_check with the source, the note with its kinds – and
    the step, run here by hand, updates the source's row in the stored
    report, deletes no file, and none of it starts while a job is on."""
    import json
    import archive_check
    a, port = server
    out = _outlook_mit_befunden(sandbox)
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.update(steps=steps, label=label, **kw) or True)

    ZUSTAND = {"vermerken": "noted", "beiseitelegen": "set-aside", "zurueckholen": "open"}

    def schritt(aktion, koerper=None):
        code, r = call(port, "PATCH", "/api/v1/sources/outlook/findings",
                       {"state": ZUSTAND[aktion], **(koerper or {})})
        assert code == 202 and r["run"] and "error" not in r, r
        # The archive's action, and the evidence step that chains what it moved.
        (s, kette) = gesehen["steps"]
        assert s["key"] == "archiv_" + aktion.replace("-", "_") and kette["key"] == "evidence"
        assert gesehen["label"] == "job.archiv." + aktion
        argv = s["argv"]
        assert argv[argv.index("--aktion") + 1] == aktion
        assert argv[argv.index("--quelle") + 1] == "outlook"
        # The step as the run would execute it – in this process.
        pfade = {"outlook": str(out)}
        arten = argv[argv.index("--arten") + 1] if "--arten" in argv else "verloren"
        archive_check.aktion(aktion, "outlook", pfade, app_mod.archiv_bericht_pfad(), arten)
        return [q for q in json.loads(app_mod.archiv_bericht_pfad().read_text(encoding="utf-8"))["quellen"]
                if q["quelle"] == "outlook"][0]

    zeile = schritt("vermerken")
    assert (zeile["verloren"], zeile["fremd"], zeile["vermerkt"]) == (0, 1, 0)
    zeile = schritt("beiseitelegen")
    assert not (out / "E-Mail/Posteingang/fremd.eml").exists()
    assert list((out / "_fremd").glob("*/E-Mail/Posteingang/fremd.eml"))
    assert (out / "E-Mail/Posteingang/a.eml").exists()
    assert (zeile["fremd"], zeile["beiseite"]) == (0, 1)
    zeile = schritt("zurueckholen")
    assert (out / "E-Mail/Posteingang/fremd.eml").read_bytes() == b"f"
    assert not (out / "_fremd").exists() and zeile["fremd"] == 1
    # The note takes its kinds from the request …
    call(port, "PATCH", "/api/v1/sources/outlook/findings",
         {"state": "noted", "kinds": ["missing"]})
    argv = gesehen["steps"][0]["argv"]
    assert argv[argv.index("--arten") + 1] == "fehlt"
    # … und eine Art, die niemand kennt, wird abgelehnt statt verworfen:
    # sie fiele sonst downstream auf "verloren" zurueck, und wer eine Art
    # vermerken wollte, bekaeme einen Grabstein fuer eine andere.
    code, r = call(port, "PATCH", "/api/v1/sources/outlook/findings",
                   {"state": "noted", "kinds": ["missing", "egal"]})
    assert code == 400 and r["error"]["k"] == "srv.archiv.badkind"
    # A source that does not exist, and a state that is none.
    assert call(port, "PATCH", "/api/v1/sources/nirgends/findings",
                {"state": "noted"})[0] == 404
    assert call(port, "PATCH", "/api/v1/sources/outlook/findings",
                {"state": "irgendwas"})[0] == 400
    a.jobs.thread = __import__("threading").Thread(target=lambda: __import__("time").sleep(0.3))
    a.jobs.thread.start()
    assert call(port, "PATCH", "/api/v1/sources/outlook/findings",
                {"state": "set-aside"})[0] == 409
    a.jobs.thread.join()


def test_http_neu_aufbauen_ist_ein_lauf_aus_drei_schritten(server, sandbox, monkeypatch):
    """A damaged bookkeeping: the route starts ONE run – the archive step
    first, then the source's sync-now export, then the index – and says
    "nothing to do" without a run when no state.db is damaged."""
    a, port = server
    out = sandbox / app_mod.OUTLOOK_DIR
    out.mkdir(parents=True, exist_ok=True)
    (out / "state.db").write_bytes(b"kein sqlite")
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.update(steps=steps, label=label, **kw) or True)
    code, r = call(port, "POST", "/api/v1/sources/outlook/rebuild", {})
    assert code == 202 and r["run"] and "error" not in r
    keys = [s["key"] for s in gesehen["steps"]]
    assert keys == ["archiv_neu_aufbauen", "outlook", "evidence", "index"], keys
    assert gesehen["steps"][1]["env"]["SYNC_NOW"] == "1"
    assert gesehen["label"] == "job.archiv.neu-aufbauen"
    assert list(out.glob("state.db")) and not list(out.glob("state.db.beschaedigt-*")), \
        "the route moves nothing – the step does, inside the run"
    (out / "state.db").unlink()
    code, r = call(port, "POST", "/api/v1/sources/outlook/rebuild", {})
    assert code == 200 and r["message"]["k"] == "srv.archiv.nothing"


def test_build_steps_resync_traegt_die_ordnerliste_nur_mit_abgleich(sandbox):
    """The balance's folders travel as RESYNC_FOLDERS in the Outlook step,
    and only with a resync – a plain run never narrows itself."""
    cfg = app_mod.load_config()
    cfg.update(outlook_categories=["mail"])
    (schritt,) = [s for s in app_mod.build_steps(
        cfg, {"outlook": True}, resync=True, resync_ordner=["E-Mail/Posteingang", " ", "kalender/Arbeit"])
        if s["key"] == "outlook"]
    assert json.loads(schritt["env"]["RESYNC_FOLDERS"]) == ["E-Mail/Posteingang", "kalender/Arbeit"]
    (ohne,) = [s for s in app_mod.build_steps(cfg, {"outlook": True}, sync_now=True,
                                              resync_ordner=["E-Mail/Posteingang"])
               if s["key"] == "outlook"]
    assert "RESYNC_FOLDERS" not in ohne["env"]


def test_build_steps_nachholen_erzwingt_die_quelle_mit_der_liste(sandbox):
    """"Fetch again": the source's step runs whether or not the settings
    tick it, with the list in FETCH_LIST and every cadence aside – and the
    row is judged afresh by the archive step at the end."""
    cfg = app_mod.load_config()
    cfg.update(outlook_categories=[])
    steps = app_mod.build_steps(
        cfg, {"outlook": True, "index": True, "archiv_pruefen": True},
        nachholen={"quelle": "outlook", "liste": "/tmp/nachholen-outlook.json"},
        archiv={"quelle": "outlook"})
    assert [s["key"] for s in steps] == ["outlook", "index", "archiv_pruefen"]
    assert steps[0]["env"]["FETCH_LIST"] == "/tmp/nachholen-outlook.json"
    assert steps[0]["env"]["SYNC_NOW"] == "1"
    argv = steps[2]["argv"]
    assert argv[argv.index("--aktion") + 1] == "pruefen"
    assert argv[argv.index("--quelle") + 1] == "outlook"
    # Without the list the unticked source stays out, as before.
    ohne = app_mod.build_steps(cfg, {"outlook": True, "index": True})
    assert [s["key"] for s in ohne] == ["index"]


def test_http_nachholen_schreibt_die_liste_und_startet_den_lauf(server, sandbox, monkeypatch):
    """"Fetch again" on the archive card: the files the stored report found
    missing go to a list, and one run starts – the source (unticked or
    not), the index, the row judged afresh. Nothing to do without a
    finding."""
    import json
    a, port = server
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    a.cfg["outlook_categories"] = []
    out = sandbox / app_mod.OUTLOOK_DIR
    out.mkdir(parents=True, exist_ok=True)
    zeile = {"quelle": "outlook", "fehlt": 2, "unvollstaendig": 0,
             "befunde": {"fehlt": ["E-Mail/a.eml", "E-Mail/b.eml"], "fremd": [],
                         "unvollstaendig": [], "verloren": []}}
    app_mod.archiv_bericht_pfad().write_text(json.dumps(
        {"geprueft": "x", "quellen": [zeile], "index": {}}), encoding="utf-8")
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.update(steps=steps, label=label, **kw) or True)
    code, r = call(port, "POST", "/api/v1/sources/outlook/refetch", {})
    assert code == 202 and r["run"] and "error" not in r
    assert [s["key"] for s in gesehen["steps"]] == ["outlook", "evidence", "index", "archiv_pruefen"]
    assert gesehen["label"] == "job.archiv.nachholen"
    liste = sandbox / "nachholen-outlook.json"
    assert gesehen["steps"][0]["env"]["FETCH_LIST"] == str(liste)
    assert json.loads(liste.read_text(encoding="utf-8"))["dateien"] == ["E-Mail/a.eml", "E-Mail/b.eml"]
    zeile["befunde"]["fehlt"] = []
    app_mod.archiv_bericht_pfad().write_text(json.dumps(
        {"geprueft": "x", "quellen": [zeile], "index": {}}), encoding="utf-8")
    code, r = call(port, "POST", "/api/v1/sources/outlook/refetch", {})
    assert code == 200 and r["message"]["k"] == "srv.archiv.nothing"


def test_http_saved_search_title_comes_from_the_model(server, monkeypatch):
    """QUERY /searches/saved/title: the criteria in, a title out – asked of
    the local model in the page's language; refused without criteria
    (400), without Ollama's chat model (503), and when the model gives
    nothing (502)."""
    import answer
    a, port = server
    gesehen = {}

    def fake(kriterien, model, ollama, lang="de", timeout=30):
        gesehen.update(kriterien=kriterien, model=model, ollama=ollama, lang=lang)
        return gesehen.get("titel", "Rechnungen Nordwind")
    monkeypatch.setattr(answer, "suchtitel", fake)
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": True, "models": [model], "has_model": True,
                            "has_chat_model": True, "error": None, "model": model,
                            "chat_model": chat_model, "url": url})
    a._ollama_cache = (0, None)
    code, r = call(port, "QUERY", "/api/v1/searches/saved/title",
                   {"criteria": {"q": "Rechnung", "source": "outlook", "mode": "text"}})
    assert code == 200 and r == {"title": "Rechnungen Nordwind"}
    assert gesehen["kriterien"]["q"] == "Rechnung" and gesehen["model"] == a.cfg["chat_model"]
    assert gesehen["ollama"] == a.cfg["ollama"] and gesehen["lang"] in ("de", "en", "fr")
    assert (gesehen["kriterien"]["case_name"], gesehen["kriterien"]["attach_case"]) == ("", "")
    # the case and folder the criteria name, and the case the search goes into – by name
    fall = a.faelle.fall_anlegen("Nordwind")
    ordner = a.faelle.ordner_anlegen(fall, "Belege")
    ziel = a.faelle.fall_anlegen("Rakete")
    code, r = call(port, "QUERY", "/api/v1/searches/saved/title",
                   {"criteria": {"q": "Rechnung", "case": fall, "case_folder": ordner}, "case": ziel})
    assert code == 200
    k = gesehen["kriterien"]
    assert (k["case_name"], k["case_folder_name"], k["attach_case"]) == ("Nordwind", "Belege", "Rakete")
    code, r = call(port, "QUERY", "/api/v1/searches/saved/title", {"criteria": {"q": "x"}, "case": 999})
    assert code == 404 and r["error"]["k"] == "srv.case.unknown"
    code, r = call(port, "QUERY", "/api/v1/searches/saved/title", {"criteria": {"source": "all"}})
    assert code == 400 and r["error"]["k"] == "srv.title.nocriteria"
    code, r = call(port, "QUERY", "/api/v1/searches/saved/title", {})
    assert code == 400
    gesehen["titel"] = ""
    code, r = call(port, "QUERY", "/api/v1/searches/saved/title", {"criteria": {"q": "x"}})
    assert code == 502 and r["error"]["k"] == "srv.title.failed"
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": False, "models": [], "has_model": False,
                            "has_chat_model": False, "error": "x", "model": model,
                            "chat_model": chat_model, "url": url})
    a._ollama_cache = (0, None)
    code, r = call(port, "QUERY", "/api/v1/searches/saved/title", {"criteria": {"q": "x"}})
    assert code == 503 and r["error"]["k"] == "srv.title.nomodel"


def test_http_bilanz_holen_holt_je_quelle_nur_das_offene(server, sandbox, monkeypatch):
    """"Fetch now" on a balance row: the mailbox as a resync of the open
    folders with its check behind the index; a mirror by the open files'
    ids from the stored report, without the check (the fetch adjusts the
    row itself) – or as a resync when the report capped them; every other
    source as its regular run with its check."""
    import json
    import completeness
    import state_db
    a, port = server
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    a.cfg.update(outlook_categories=["mail"], teams_categories=["group"], onedrive_enabled=True)
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.update(steps=steps, label=label, **kw) or True)
    completeness.schreiben(state_db.StateDb(sandbox / app_mod.OUTLOOK_DIR), completeness.bilanz(
        "outlook_mail", "mails", da=5, offen=2,
        zeilen=[completeness.zeile("E-Mail/Posteingang", 3, 1), completeness.zeile("E-Mail/Archiv", 2, 1)]))
    code, r = call(port, "POST", "/api/v1/balance/outlook_mail/fetch", {})
    assert code == 202 and gesehen["label"] == "job.holen"
    schritte = {s["key"]: s for s in gesehen["steps"]}
    assert list(schritte) == ["outlook", "evidence", "index", "check"]
    assert schritte["outlook"]["env"]["RESYNC"] == "1"
    assert sorted(json.loads(schritte["outlook"]["env"]["RESYNC_FOLDERS"])) == ["E-Mail/Archiv", "E-Mail/Posteingang"]
    assert schritte["outlook"]["env"]["EXPORT_CATEGORIES"] == "mail"
    assert schritte["check"]["env"]["EXPORT_CATEGORIES"] == "mail", "the check must judge the row fetched"
    # The row's category runs even when the export ticks another one –
    # the row was checked, so the row is fetched, and judged afterwards.
    a.cfg["outlook_categories"] = ["contacts"]
    call(port, "POST", "/api/v1/balance/outlook_mail/fetch", {})
    schritte = {s["key"]: s for s in gesehen["steps"]}
    assert list(schritte) == ["outlook", "evidence", "index", "check"]
    assert schritte["outlook"]["env"]["EXPORT_CATEGORIES"] == "contacts,mail"
    assert schritte["check"]["env"]["EXPORT_CATEGORIES"] == "mail"
    # and the run says what it runs, not what the settings tick
    assert gesehen["context"]["elements"]["outlook"] == ["mail", "contacts"]
    a.cfg["outlook_categories"] = ["mail"]
    # A mirror with the open files by id: the list, no walk, no check step.
    completeness.schreiben(state_db.StateDb(sandbox / app_mod.ONEDRIVE_DIR), completeness.bilanz(
        "onedrive", "files", da=1, offen=1, zeilen=[completeness.zeile("Dateien", 1, 1)],
        extra={"offene": [{"id": "i1", "rel": "Dateien/a.pdf"}], "offene_gekappt": False}))
    code, r = call(port, "POST", "/api/v1/balance/onedrive/fetch", {})
    assert code == 202 and r["run"]
    schritte = {s["key"]: s for s in gesehen["steps"]}
    assert list(schritte) == ["onedrive", "evidence", "index"]
    liste = sandbox / "nachholen-onedrive.json"
    assert schritte["onedrive"]["env"]["FETCH_LIST"] == str(liste)
    assert json.loads(liste.read_text(encoding="utf-8"))["dateien"] == [{"id": "i1", "rel": "Dateien/a.pdf"}]
    assert "RESYNC" not in schritte["onedrive"]["env"]
    # Capped: the resync walks, the check follows.
    completeness.schreiben(state_db.StateDb(sandbox / app_mod.ONEDRIVE_DIR), completeness.bilanz(
        "onedrive", "files", da=1, offen=9, extra={"offene": [{"id": "i1", "rel": "Dateien/a.pdf"}],
                                                   "offene_gekappt": True}))
    call(port, "POST", "/api/v1/balance/onedrive/fetch", {})
    schritte = {s["key"]: s for s in gesehen["steps"]}
    assert list(schritte) == ["onedrive", "evidence", "index", "check_onedrive"]
    assert schritte["onedrive"]["env"]["RESYNC"] == "1" and "FETCH_LIST" not in schritte["onedrive"]["env"]
    # The calendar with the open events by id: the list, no calendar read, no check step.
    completeness.schreiben(state_db.StateDb(sandbox / app_mod.OUTLOOK_DIR), completeness.bilanz(
        "outlook_calendar", "events", da=3, offen=1, zeilen=[completeness.zeile("kalender/Arbeit", 3, 1)],
        extra={"offene": [{"id": "e7", "rel": "", "pfad": "kalender/Arbeit", "art": "event"}],
               "offene_gekappt": False}))
    call(port, "POST", "/api/v1/balance/outlook_calendar/fetch", {})
    schritte = {s["key"]: s for s in gesehen["steps"]}
    assert list(schritte) == ["outlook", "evidence", "index"]
    assert schritte["outlook"]["env"]["FETCH_LIST"] == str(sandbox / "nachholen-outlook.json")
    assert "RESYNC" not in schritte["outlook"]["env"]
    assert json.loads((sandbox / "nachholen-outlook.json").read_text(encoding="utf-8"))["dateien"] == \
        [{"id": "e7", "rel": "", "pfad": "kalender/Arbeit", "art": "event"}]
    # Teams without a stored list: the regular run is the diff already.
    call(port, "POST", "/api/v1/balance/teams/fetch", {})
    schritte = {s["key"]: s for s in gesehen["steps"]}
    assert list(schritte) == ["teams", "evidence", "index", "check_teams"]
    assert "RESYNC" not in schritte["teams"]["env"] and schritte["teams"]["env"]["SYNC_NOW"] == "1"
    # Teams with the open conversations by key: the list, no chat listing, no check step.
    completeness.schreiben(state_db.StateDb(sandbox / app_mod.TEAMS_DIR), completeness.bilanz(
        "teams", "conversations", da=3, offen=1, zeilen=[completeness.zeile("1on1", 3, 1)],
        extra={"offene": [{"id": "c7", "rel": "", "pfad": "1on1"}], "offene_gekappt": False}))
    call(port, "POST", "/api/v1/balance/teams/fetch", {})
    schritte = {s["key"]: s for s in gesehen["steps"]}
    assert list(schritte) == ["teams", "evidence", "index"]
    liste = sandbox / "nachholen-teams.json"
    assert schritte["teams"]["env"]["FETCH_LIST"] == str(liste)
    assert json.loads(liste.read_text(encoding="utf-8"))["dateien"] == [{"id": "c7", "rel": "", "pfad": "1on1"}]
    # Eine Quelle, die es nicht gibt, ist ein Pfad ins Leere – 404 wie
    # bei jeder anderen Route unter /sources (13.0).
    assert call(port, "POST", "/api/v1/balance/nirgends/fetch", {})[0] == 404


def test_http_run_lehnt_eine_nicht_angehakte_quelle_ab(server, monkeypatch):
    """"Fetch again" on a source the settings do not tick: no run that
    carries nothing but the index – the answer names the source."""
    a, port = server
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    a.cfg["outlook_categories"] = []
    code, r = call(port, "POST", "/api/v1/runs",
                   {"outlook": True, "resync": True, "index": True, "label": "job.resync"})
    assert code == 409 and not r["ok"]
    assert r["error"]["k"] == "srv.inactive" and r["error"]["v"]["source"] == "Outlook"


PRUEFUNG_ARCHIV_AKTIONEN = GRUNDZUSTAND + """
function q(extra){
  return Object.assign({quelle: 'outlook', einheit: 'files', stand: 'ganz', grund: null,
    stimmig: 10, fehlt: 0, fremd: 0, unvollstaendig: 0, verloren: 0, beiseite: 0, gekappt: false,
    zeilen: [], fehler: [], befunde: {fehlt: [], fremd: [], unvollstaendig: [], verloren: []}}, extra);
}
// Nothing to do: no button but none. Findings: one button per kind.
zeigeArchiv({geprueft: 'x', quellen: [q()], index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0}});
var html = el('ana-archiv-zeilen').innerHTML;
pruefe(html.indexOf('archivAktion(') < 0 && html.indexOf('befundeFenster(') < 0, 'Knoepfe ohne Befund: ' + html);
zeigeArchiv({geprueft: 'x', quellen: [q({fehlt: 1, fremd: 2, verloren: 3, beiseite: 4,
  befunde: {fehlt: ['E-Mail/a.eml'], fremd: ['E-Mail/f1.eml', 'E-Mail/f2.eml'], unvollstaendig: [], verloren: ['E-Mail/w.eml']}})],
  index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0}});
html = el('ana-archiv-zeilen').innerHTML;
['befundeFenster(', '&quot;nachholen&quot;', '&quot;vermerken&quot;', '&quot;beiseitelegen&quot;', '&quot;zurueckholen&quot;'].forEach(function(w){
  pruefe(html.indexOf(w) >= 0, 'Knopf fehlt: ' + w);
});
pruefe(html.indexOf('neu-aufbauen') < 0, 'Neu aufbauen ohne kaputte Buchhaltung');
pruefe(html.indexOf('4 beiseitegelegt') >= 0, 'Beiseitegelegtes nicht im Satz: ' + html);
// Only set aside, nothing found: "Put back" alone – no window with nothing in it.
zeigeArchiv({geprueft: 'x', quellen: [q({beiseite: 4})], index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0}});
html = el('ana-archiv-zeilen').innerHTML;
pruefe(html.indexOf('befundeFenster(') < 0 && html.indexOf('&quot;zurueckholen&quot;') >= 0,
       'Befunde-Knopf ohne Befund oder Zurücklegen fehlt: ' + html);
zeigeArchiv({geprueft: 'x', quellen: [q({stand: 'nicht', grund: 'ana.archiv.reason.db'})], index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0}});
pruefe(el('ana-archiv-zeilen').innerHTML.indexOf('neu-aufbauen') >= 0, 'Neu aufbauen fehlt bei kaputter Buchhaltung');

// The findings window lists the files per kind and offers copy and open.
zeigeArchiv({geprueft: 'x', quellen: [q({fremd: 2, verloren: 1,
  befunde: {fehlt: [], fremd: ['E-Mail/f1.eml', 'E-Mail/f2.eml'], unvollstaendig: [], verloren: ['E-Mail/w.eml']}})],
  index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0}});
befundeFenster('outlook');
var m = modal.innerHTML;
pruefe(m.indexOf('E-Mail/f1.eml') >= 0 && m.indexOf('E-Mail/w.eml') >= 0, 'Befunde nicht gelistet');
pruefe(m.indexOf('Fehlt (') < 0, 'Leere Gruppe gezeichnet');
pruefe(m.indexOf('inZwischenablage(') >= 0 && m.indexOf('&quot;ordner&quot;') >= 0, 'Kopieren oder Ordner fehlt');
var zeilen = BEFUNDE_TEXT.split(String.fromCharCode(10));
pruefe(zeilen.length === 3 && zeilen[0] === 'E-Mail/f1.eml' && zeilen[2] === 'E-Mail/w.eml', 'Kopiertext: ' + BEFUNDE_TEXT);
closeWizard('befunde');

// Set aside asks first, with the count; a no sends nothing.
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body ? JSON.parse(opt.body) : null});
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/v1/status') >= 0 ? statusGeruest() : {ok: true, message: null}); }});
};
global.confirm = function(text){ global.gefragt = text; return false; };
archivAktion('beiseitelegen', 'outlook');
pruefe(String(global.gefragt).indexOf('2 Dateien') >= 0, 'Rueckfrage ohne Zahl: ' + global.gefragt);
pruefe(!gesendet.length, 'Abgelehnt und trotzdem gesendet');
global.confirm = function(){ return true; };
archivAktion('beiseitelegen', 'outlook');
pruefe(LAUF.eigener === true, 'Die Aktion oeffnet das Lauffenster nicht');
archivAktion('vermerken', 'outlook');
var pfade = gesendet.map(function(g){ return g.pfad; });
pruefe(pfade.filter(function(p){ return p.indexOf('/findings') >= 0; }).length === 2, 'Aktionen nicht gesendet: ' + pfade.join(','));
// The source stands in the path, the state in the body.
pruefe(gesendet.every(function(g){ return g.pfad.indexOf('/sources/outlook/') >= 0; }), 'Quelle fehlt im Pfad');
pruefe(gesendet.map(function(g){ return g.body.state; }).sort().join(',') === 'noted,set-aside',
       'Zustand fehlt: ' + JSON.stringify(gesendet.map(function(g){ return g.body; })));
var vermerkt = gesendet.filter(function(g){ return g.body.state === 'noted'; })[0].body;
pruefe(JSON.stringify(vermerkt.kinds) === '["lost"]', 'Vermerken ohne Nachholen nimmt Fehlendes mit: ' + JSON.stringify(vermerkt));

// A file still missing after a resync that ran since the finding: the
// sentence says so, the note offers itself and takes the missing along.
var tot = q({fehlt: 2, fehlt_seit: '2026-09-13T10:00:00+00:00', nachgeholt: '2026-09-14T08:00:00+00:00',
             befunde: {fehlt: ['E-Mail/a.eml', 'E-Mail/b.eml'], fremd: [], unvollstaendig: [], verloren: []}});
zeigeArchiv({geprueft: 'x', quellen: [tot], index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0}});
html = el('ana-archiv-zeilen').innerHTML;
pruefe(html.indexOf('auch nach dem Nachholen') >= 0, 'Satz nennt das erschoepfte Nachholen nicht: ' + html);
pruefe(html.indexOf('&quot;vermerken&quot;') >= 0, 'Vermerken fehlt bei totem Eintrag');
gesendet = []; global.gefragt = null;
global.confirm = function(text){ global.gefragt = text; return true; };
archivAktion('vermerken', 'outlook');
pruefe(String(global.gefragt).indexOf('2 Dateien') >= 0, 'Rueckfrage zum Vermerken ohne Zahl: ' + global.gefragt);
pruefe(JSON.stringify(gesendet[0].body.kinds) === '["lost","missing"]', 'Fehlendes nicht vermerkt: ' + JSON.stringify(gesendet[0].body));
// Before a resync ran, a missing file is the fetch's job – no note.
var frisch = q({fehlt: 2, fehlt_seit: '2026-09-14T09:00:00+00:00', nachgeholt: '2026-09-14T08:00:00+00:00'});
zeigeArchiv({geprueft: 'x', quellen: [frisch], index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0}});
html = el('ana-archiv-zeilen').innerHTML;
pruefe(html.indexOf('&quot;vermerken&quot;') < 0 && html.indexOf('Nachholen holt sie') >= 0, 'Vermerken vor dem Nachholen: ' + html);
// What was noted stands in the sentence, quietly.
zeigeArchiv({geprueft: 'x', quellen: [q({vermerkt: 3})], index: {stand: 'ganz', geaendert: 0, neu: 0, weg: 0}});
pruefe(el('ana-archiv-zeilen').innerHTML.indexOf('3 als verloren vermerkt') >= 0, 'Vermerktes fehlt im Satz');

// "Fetch again" is an action of its own: no question, the window opens.
gesendet = []; LAUF.eigener = false; global.gefragt = null;
archivAktion('nachholen', 'outlook');
pruefe(global.gefragt === null, 'Nachholen fragt zurueck');
pruefe(gesendet[0].pfad === '/api/v1/sources/outlook/refetch',
       'Nachholen nicht gesendet: ' + JSON.stringify(gesendet[0]));
pruefe(LAUF.eigener === true, 'Nachholen oeffnet kein Fenster');

// The checks and the fetch start through run(): the window opens with them.
gesendet = []; LAUF.eigener = false;
pruefeArchiv();
pruefe(LAUF.eigener === true && gesendet[0].body.check_archive === true && gesendet[0].body.label === 'job.archive_check',
       'Archiv pruefen oeffnet kein Fenster oder schickt nichts: ' + JSON.stringify(gesendet[0]));
LAUF.eigener = false; gesendet = [];
KONFIG = Object.assign({}, KONFIG, {check_sources: ['outlook_mail']});   // the card's own choice
pruefeVollstaendigkeit();
pruefe(LAUF.eigener === true && gesendet[0].body.label === 'job.check', 'Jetzt pruefen ohne Fenster');
LAUF.eigener = false; gesendet = [];
holeQuelle('outlook_mail');
pruefe(LAUF.eigener === true && gesendet[0].pfad.indexOf('/fetch') >= 0, 'Jetzt holen ohne Fenster');
console.log('OK');
"""


def test_archivaktionen_auf_der_seite():
    _in_node(PRUEFUNG_ARCHIV_AKTIONEN)
    # No state text of its own beside a button: the run window says what
    # runs (DESIGN.md §2).
    seite = app_mod.seite()
    assert 'id="ana-archiv-state"' not in seite and 'id="ana-check-state"' not in seite


def test_export_status_kennt_onedrive(sandbox):
    """state.db dates the last run – every export writes it at the end,
    even when nothing new arrived."""
    import state_db
    cfg = app_mod.load_config()
    od = sandbox / app_mod.ONEDRIVE_DIR
    od.mkdir(parents=True, exist_ok=True)
    assert app_mod.export_status(cfg)["onedrive"]["last_run"] is None
    state_db.StateDb(od).bestand_schreiben(
        {"a": {"rel": "Dateien/a.pdf", "ctag": "c", "size": 1}})
    assert app_mod.export_status(cfg)["onedrive"]["last_run"]


def test_archivseite_zeigt_zeiten_je_quelle_und_keinen_datenordner():
    """When a source last ran stands once, in the (i) of its card on the
    archive page – not on the card itself and not as a summary line in the
    overview. The data folder lives in the settings. The same thing in two
    places goes stale in one of them; the archive page stays bare."""
    seite = app_mod.seite()
    archiv = seite.split('<section id="tab-suche"')[0]
    for quelle in ("outlook", "teams", "onedrive", "sharepoint", "planner",
                   "todo", "onenote"):
        assert f'id="q-{quelle}-info"' in archiv, f"{quelle}: kein (i) mit dem Stand"
        assert f'id="q-{quelle}-fuss"' not in archiv, f"{quelle}: Zustandszeile auf der Karte"
    assert 'id="export-state"' not in seite, "Sammelzeile mit Zeiten ist zurueck"
    # Neither the last runs nor the update notice belong on the archive page.
    assert 'id="letzte-laeufe"' not in seite, "Letzte Laeufe sind zurueck"
    assert 'id="update-banner"' not in archiv, "Versionshinweis auf der Archivseite"
    # A running job lives in its own window, the log with it – the archive
    # page keeps the headline and the button.
    assert 'id="protokoll"' not in archiv and 'id="fortschritt"' not in archiv
    fenster = seite[seite.index('id="lauf-fenster"'):seite.index("</body>")]
    for teil in ('id="fortschritt"', 'id="protokoll"', 'id="btn-cancel"', 'id="lauf-fertig"'):
        assert teil in fenster, f"{teil} steht nicht im Lauf-Fenster"
    kopf = seite.split("</header>")[0]
    assert 'id="lauf-pille"' in kopf, "die Pille des minimierten Laufs fehlt in der Kopfzeile"
    assert 'id="c-data-dir"' not in archiv
    # The value sits in the input field itself; the fixed locations (app
    # folder, application) stand below as their own immutable rows.
    assert 'id="data-dir2"' not in seite, "doppelte Pfadanzeige ist zurueck"
    assert 'id="c-data-dir"' in seite
    assert 'id="home-dir"' in seite and 'id="app-ort"' in seite


PRUEFUNG_SCHRITTNAME = GRUNDZUSTAND + """
var status = statusGeruest();
status.jobs = {busy: true, seq: 1, token_expired: false, last: null,
               job: {label: 'Export', steps: ['job.step.outlook', 'job.step.index'],
                     step: 'job.step.index', index: 1, progress: null}};
S = null;
renderStatus(status);
var zeile = document.getElementById('fortschritt-text').textContent;
pruefe(zeile.indexOf('job.step.') < 0, 'Schluessel statt Text: ' + zeile);
pruefe(zeile.indexOf('Index') >= 0, 'Schrittname fehlt: ' + zeile);
pruefe(zeile.indexOf('Export') >= 0, 'Etikett des Laufs fehlt: ' + zeile);
console.log('OK');
"""


def test_schrittname_wird_uebersetzt():
    _in_node(PRUEFUNG_SCHRITTNAME)


def test_startknopf_erklaert_sich_selbst():
    """The one button of the archive page says what it does in its label;
    neither prose nor an (i) sits next to it. The status card is meant to
    stay bare: one line, one button."""
    seite = app_mod.seite()
    knoepfe = seite[seite.index('id="stand-knoepfe"'):seite.index('id="erststart"')]
    assert 'class="info"' not in knoepfe, "wieder ein (i) am Startknopf"
    assert "export.start.hint" not in seite


# search.gone.note sits in the *Deleted only* pill's popover since 11.2 –
# on demand as well, one sentence under the switch, no (i) per pill.
ERKLAERUNGEN_ALS_INFO = ["export.what.sub",
                         "export.index.only.when", "export.calendar.build.when"]


@pytest.mark.parametrize("schluessel", ERKLAERUNGEN_ALS_INFO)
def test_erklaerungen_im_exportreiter_stehen_am_infozeichen(schluessel):
    """Paragraphs next to buttons make the UI restless; the explanation
    belongs on demand. The text itself stays – only its form changes."""
    assert f'data-i18n="{schluessel}"' not in app_mod.seite(), "steht wieder als Fließtext da"
    assert f'data-i18n-title="{schluessel}"' in app_mod.seite()
    assert i18n.strings("de")[schluessel]


def test_jedes_infozeichen_ist_erreichbar():
    """An (i) that only the mouse knows is, for the keyboard, a letter
    without meaning."""
    zeichen = app_mod.seite().count('class="info"')
    assert zeichen >= 5, f"nur {zeichen} (i) gefunden"
    for stueck in app_mod.seite().split('<span class="info"')[1:]:
        block = stueck[:220]
        assert 'tabindex="0"' in block and "aria-label=" in block


def test_infozeichen_ist_erreichbar_und_erklaert_sich():
    """An (i) that only the mouse knows is, for the keyboard, a letter
    without meaning."""
    i = app_mod.seite().index('data-i18n-title="export.what.sub"')
    block = app_mod.seite()[i - 200:i + 200]
    assert 'tabindex="0"' in block, "mit der Tastatur nicht erreichbar"
    assert 'aria-label=' in block, "ohne Namen für den Screenreader"
    assert ".info{" in app_mod.seite() and "cursor:help" in app_mod.seite()


PRUEFUNG_ANALYTICS_KACHELN = GRUNDZUSTAND + """
var a = {exists: true, built_at: '2026-09-01T10:00:00+00:00',
  komm: {nachrichten: 123456, gespraeche: 12000, mit_anhang: 3400,
         personen: 2500, verschwunden: 18, von: 1551398400, bis: 1788134400},
  quellen: [{src:'teams',n:100000},{src:'outlook',n:23000}],
  dateien: {n: 629, onedrive: 600, sharepoint: 29, pages: 3, verschwunden: 2},
  groesse: {teams: 1000, outlook: 2000, onedrive: 3000, index: 500},
  vollstaendigkeit: null, vollstaendigkeit_onedrive: null};
zeigeAnalytics(a);
var h = document.getElementById('ana-kpi').innerHTML;
var dat = document.getElementById('ana-kpi-dateien').innerHTML;

// Die Dateiwelt steht in der eigenen Reihe, mit Aufteilung nach Spiegeln.
pruefe(dat.indexOf('>629<') >= 0, 'Dateizahl fehlt: ' + dat.slice(0, 200));
pruefe(dat.indexOf('OneDrive 600') >= 0, 'Aufteilung nach Spiegeln fehlt');
pruefe(dat.indexOf('>3<') >= 0, 'Seitenzahl fehlt');
pruefe(dat.indexOf('>2<') >= 0, 'Verschwundene Dateien fehlen');

// Der Knopf ist weg; die Kachel selbst fuehrt zur Suche.
pruefe(h.indexOf('kpi-fuss') < 0, '"Show these" steht noch da');
pruefe((h.match(/klickbar/g) || []).length === 1, 'Genau eine Kachel soll klickbar sein');
pruefe(h.indexOf('role="button"') >= 0 && h.indexOf('onkeydown=') >= 0,
       'Klickbare Kachel ist nicht mit der Tastatur bedienbar');

// Erklaerungen am (i), Zahlen sichtbar.
pruefe(h.indexOf('Related mails') < 0, 'Erklaerung steht noch als Text da');
pruefe((h.match(/class="info"/g) || []).length === 3, 'Falsche Zahl an Infozeichen');
// Ohne Tausendertrennzeichen geprueft: das haengt an der Sprache.
pruefe(h.indexOf('Teams 100') >= 0, 'Aufteilung nach Quellen ist verschwunden');
pruefe(h.indexOf('kpi-hint') >= 0, 'Sichtbare Zahlenzeile ganz weg');

// Ohne Verschwundenes fuehrt die Kachel nirgendwohin – eine Sackgasse waere schlechter.
a.komm.verschwunden = 0;
// Und ohne Spiegel gibt es die Datei-Kacheln gar nicht: "0" waere fuer alle,
// die keinen Spiegel nutzen, Zeilen, die nichts sagen.
a.dateien = {n: 0, onedrive: 0, sharepoint: 0, pages: 0, verschwunden: null};
zeigeAnalytics(a);
var h2 = document.getElementById('ana-kpi').innerHTML;
var dat2 = document.getElementById('ana-kpi-dateien').innerHTML;
pruefe((h2.match(/klickbar/g) || []).length === 0, 'Kachel ohne Treffer trotzdem klickbar');
pruefe((dat2.match(/kpi-wert/g) || []).length === 1,
       'Leere Datei-Kacheln werden gezeigt: ' + dat2.slice(0, 200));
console.log('OK');
"""


def test_analytics_kacheln_zeigen_zahlen_und_erklaeren_am_infozeichen():
    _in_node(PRUEFUNG_ANALYTICS_KACHELN)


def test_kein_stylesheet_zieht_ein_infozeichen_auseinander():
    """Regression: `.schritt span{flex:1;min-width:240px}` came from the
    explanation text that once sat there. After the rework it hit the (i) –
    the circle became a 240-pixel-wide ellipse across the row.

    Hence the general check: no rule that stretches *every* span in a
    container may match a container holding an (i).
    """
    # Comments go first: this very one quotes the old rule verbatim, and the
    # test should look at the stylesheet, not at its rationale.
    css = re.sub(r"/\*.*?\*/", "",
                 app_mod.seite().split("<style>")[1].split("</style>")[0], flags=re.S)
    markup = app_mod.seite().split("</style>")[1]
    gefaehrlich = re.findall(r"\.([\w-]+) span\{([^}]*)\}", css)
    for klasse, regel in gefaehrlich:
        if not re.search(r"flex:\s*1|min-width|width:", regel):
            continue
        for stueck in markup.split(f'class="{klasse}"')[1:]:
            bis_ende = stueck.split("</div>")[0]
            assert 'class="info"' not in bis_ende, (
                f'.{klasse} span{{{regel}}} trifft das (i) darin')


def test_infozeichen_behaelt_seine_groesse():
    css = re.sub(r"/\*.*?\*/", "",
                 app_mod.seite().split("<style>")[1].split("</style>")[0], flags=re.S)
    regel = re.search(r"\.info\{([^}]*)\}", css).group(1)
    assert "width:17px" in regel and "height:17px" in regel
    assert "flex:0 0 auto" in regel, "sonst zieht der nächste Flex-Behälter daran"


def test_kopfleiste_zeigt_nur_den_zugang():
    """State is shown where it is fixed (DESIGN.md §1): the access and the
    open profile as states in the header's one frame, because their
    windows open from there; AI and MCP as dots next to their entries in
    the settings navigation. Nothing else goes up there, and nothing sits
    beside the frame – the index state lives in the overview."""
    seite = app_mod.seite()
    kopf = seite.split("</header>")[0]
    assert 'id="pill-token"' in kopf, "Der Zugang ist aus der Kopfzeile verschwunden"
    for weg in ('id="pill-index"', 'id="pill-ollama"', 'id="pill-mcp"'):
        assert weg not in kopf, f"{weg} steht wieder in der Kopfzeile"
    rahmen = kopf[kopf.index('id="zustaende"'):]
    rahmen = rahmen[:rahmen.index("</div>")]
    for drin in ('id="lauf-pille"', 'id="pill-profil"', 'id="pill-token"'):
        assert drin in rahmen, f"{drin} steht neben dem Rahmen statt darin"
    assert rahmen.count('class="zustand') == 3, "Ein Zustand mehr im Rahmen – bewusst?"
    einst = seite[seite.index('<section id="tab-einstellungen"'):]
    for erwartet in ('id="pill-ollama"', 'id="pill-mcp"'):
        assert erwartet in einst, f"{erwartet} fehlt in der Einstellungs-Navigation"
    # The number still appears somewhere – just in the key figures now.
    assert 'id="ana-kpi"' in seite


PRUEFUNG_SUCHMASKE = GRUNDZUSTAND + """
var gesucht = [];
global.fetch = function(pfad){
  gesucht.push(String(pfad));
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/search?') >= 0 ? {results: [], total: 0}
                                             : statusGeruest()); }});
};

// Ein gesetzter Filter leuchtet an Ort und Stelle; "Zuruecksetzen" erscheint
// erst, wenn es etwas zurueckzusetzen gibt.
document.getElementById('f-person').value = 'Alice';
document.getElementById('f-source').value = 'outlook';
zeigeFilterstand();
pruefe(filterFelder().length === 2, 'Zahl der Filter falsch: ' + filterFelder().length);
pruefe(document.getElementById('f-person').classList.contains('on'), 'Person leuchtet nicht');
pruefe(document.getElementById('f-source').classList.contains('on'), 'Quelle leuchtet nicht');
pruefe(!document.getElementById('filter-weg').classList.contains('hide'),
       '"Zuruecksetzen" fehlt trotz Filter');

// "Alle Quellen" ist kein Filter.
document.getElementById('f-source').value = 'all';
zeigeFilterstand();
pruefe(filterFelder().length === 1, '"Alle Quellen" wurde mitgezaehlt');
pruefe(!document.getElementById('f-source').classList.contains('on'),
       '"Alle Quellen" leuchtet wie ein Filter');

filterLeeren();
pruefe(document.getElementById('f-person').value === '', 'Nicht geleert');
pruefe(!document.getElementById('f-person').classList.contains('on'), 'Leuchtet noch');
pruefe(document.getElementById('filter-weg').classList.contains('hide'),
       '"Zuruecksetzen" bleibt ohne Filter stehen');

// Geloeschtes ist ein Filter wie die anderen: er zaehlt mit, sucht nicht von
// selbst und geht beim Zuruecksetzen weg. Als eigene Sicht neben Kalender und
// Adressbuch stand er fuer eine Frage an dieselbe Trefferliste.
gesucht = [];
document.getElementById('f-gone').checked = true;
zeigeFilterstand();
pruefe(gesucht.length === 0, 'Der Filter sucht von selbst');
pruefe(filterFelder().length === 1, 'Geloeschtes wird nicht mitgezaehlt');
pruefe(document.getElementById('gone-feld').classList.contains('on'),
       'Geloeschtes leuchtet nicht');
doSearch(0);
pruefe(gesucht.filter(function(u){ return u.indexOf('gone=1') >= 0; }).length === 1,
       'Es wurde nicht mit gone=1 gesucht: ' + gesucht.join(' '));

filterLeeren();
pruefe(document.getElementById('f-gone').checked === false, 'Filter blieb haengen');

// Von der Kachel in der Auswertung aus: der Filter ist gesetzt und leuchtet.
zeigeVerschwundene();
pruefe(document.getElementById('f-gone').checked === true, 'Kachel setzt nichts');
pruefe(document.getElementById('gone-feld').classList.contains('on'),
       'Filter wirkt, ohne sichtbar zu sein');

// Zweimal dieselbe Sicht sucht nicht doppelt.
gesucht = [];
sicht('treffer');
pruefe(gesucht.length === 0, 'Ueberfluessige Suche bei gleicher Sicht');
console.log('OK');
"""


def test_suchmaske_filter_und_geloeschtes_als_filter():
    _in_node(PRUEFUNG_SUCHMASKE)


def test_filter_stehen_offen_und_zuruecksetzen_erst_mit_filter():
    """The filters are always in view – a folded-away filter that still acts
    on the search is a trap. "Clear filters" appears only once there is
    something to clear. Checked in the markup: the JS tests' DOM stub reads
    no class attributes."""
    i = app_mod.seite().index('id="filter"')
    assert 'hide' not in app_mod.seite()[i - 60:i], "Filter stehen zugeklappt da"
    j = app_mod.seite().index('id="filter-weg"')
    assert 'class="hide"' in app_mod.seite()[j - 60:j], "„Zurücksetzen“ ohne Filter sichtbar"


def test_suchkarte_hat_weder_ueberschrift_noch_systemsprache():
    """The tab already says where you are; the state of the index lives in
    Analytics. "BM25" and "embeddings" do not belong in the form anyway."""
    i = app_mod.seite().index('class="suchzeile"')
    karte = app_mod.seite()[i - 300:i]
    assert 'data-i18n="nav.search"' not in karte, "Überschrift wiederholt den Reiter"
    assert 'id="search-sub"' not in app_mod.seite(), "Statuszeile in der Maske"


def test_kein_feld_sucht_von_selbst():
    """A search runs when someone asks for one. You should be able to enter
    term, person, period and folder in peace without a search taking off
    after every change – the filters used to do that, and the hit list then
    belonged to a half-filled form."""
    i = app_mod.seite().index('id="filter"')
    # Up to the last field of the row, not guessed as a character count.
    block = app_mod.seite()[i:app_mod.seite().index('id="f-gone"', i) + 200]
    for feld in ('id="f-person"', 'id="f-source"', 'id="f-from"',
                 'id="f-to"', 'id="f-folder"', 'id="f-typ"', 'id="f-gone"'):
        j = block.index(feld)
        # The whole element, from the start of the tag to its end: a fixed
        # character count missed as soon as a field gained more attributes.
        umfeld = block[block.rfind("<", 0, j):block.index(">", j) + 1]
        assert "doSearch" not in umfeld, f"{feld} sucht von selbst"
        # The source field additionally reloads the folder list – even then
        # no search runs, but counting does happen.
        assert "zeigeFilterstand()" in umfeld, f"{feld} zählt nicht mit"
    q = app_mod.seite()[app_mod.seite().index('id="q"'):][:260]
    assert "oninput" not in q, "das Suchfeld sucht beim Tippen"


PRUEFUNG_KI_UND_PAGER = GRUNDZUSTAND + """
var st = statusGeruest();
st.store = {exists: true, chunks: 5, messages: 2, semantic: true,
            built_at: '2026-08-10T09:00:00', model: 'bge-m3', features: ['gone','thread']};
st.ollama = {running: true, has_model: true, has_chat_model: true,
             model: 'bge-m3', chat_model: 'q', models: []};
S = null;
renderStatus(st);

// renderStatus muss BIS ANS ENDE laufen. Zeigt eine Zeile darin auf ein
// Element, das es nicht gibt, wirft der Browser - und alles danach unterbleibt.
pruefe(!document.getElementById('m-ki').disabled,
       'KI-Variante gesperrt, obwohl ein Modell da ist');
pruefe(document.getElementById('mcp-json').textContent.length > 0,
       'renderStatus ist vorher abgebrochen');

// Ohne Modell wird sie wieder gesperrt.
st.ollama.has_chat_model = false;
renderStatus(st);
pruefe(document.getElementById('m-ki').disabled,
       'KI-Variante trotz fehlendem Modell waehlbar');

// Der Blaetterbereich nennt kein "Ranking" mehr - bei einer Suche ohne
// Begriff gibt es keines, und "hybrid" ist ein Wort fuer Entwickler.
renderHits({results: [], total: 0, backend: 'hybrid'});
var p = document.getElementById('pager').innerHTML;
pruefe(p.toLowerCase().indexOf('ranking') < 0, 'Ranking steht wieder da: ' + p);
pruefe(p.indexOf('hybrid') < 0, 'Systemwort im Blaetterbereich');
console.log('OK');
"""


def test_ki_kasten_erscheint_und_pager_bleibt_stumm():
    _in_node(PRUEFUNG_KI_UND_PAGER)


PRUEFUNG_SUCHFELD_LOEST_AUS = GRUNDZUSTAND + """
var gesucht = [];
global.fetch = function(pfad){
  gesucht.push(String(pfad));
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/search?') >= 0 ? {results: [], total: 0}
                                             : statusGeruest()); }});
};
// Filter setzen und tippen loest KEINE Suche aus - man soll in Ruhe alles
// eingeben koennen.
document.getElementById('f-from').value = '2026-08-10';
document.getElementById('f-person').value = 'Alice';
zeigeFilterstand();
document.getElementById('q').value = 'Betriebsrat';
pruefe(gesucht.filter(function(u){ return u.indexOf('/search?') >= 0; }).length === 0,
       'Es wurde beim Ausfuellen schon gesucht: ' + gesucht.join(' '));

// Erst der Knopf sucht - und zwar mit allem, was im Formular steht.
gesucht = [];
sofortSuchen();
var u = gesucht.filter(function(x){ return x.indexOf('/search?') >= 0; });
pruefe(u.length === 1, 'Knopf sucht nicht');
pruefe(u[0].indexOf('Betriebsrat') >= 0 && u[0].indexOf('Alice') >= 0 &&
       u[0].indexOf('2026-08-10') >= 0, 'Formular nicht vollstaendig uebernommen: ' + u[0]);

// "Zuruecksetzen" leert nur - gesucht wird auch dann erst auf Wunsch.
gesucht = [];
filterLeeren();
pruefe(gesucht.filter(function(x){ return x.indexOf('/search?') >= 0; }).length === 0,
       'Zuruecksetzen hat gesucht');

// Der Begriff wird in der Vorschau markiert - sonst sieht man nicht, warum
// ein Treffer einer ist.
var markiert = hervor('Hier steht Betriebsrat mittendrin');
pruefe(markiert.indexOf('<mark>Betriebsrat</mark>') >= 0, 'Nicht markiert: ' + markiert);
// A quoted phrase is marked as one piece, the loose word beside it on its own.
var qVorher = el('q').value;
el('q').value = '"Betriebsrat mittendrin" Hier';
var phrase = hervor('Hier steht Betriebsrat mittendrin');
pruefe(phrase.indexOf('<mark>Betriebsrat mittendrin</mark>') >= 0 && phrase.indexOf('<mark>Hier</mark>') >= 0, 'Phrase nicht am Stueck markiert: ' + phrase);
el('q').value = qVorher;
// Maskiert wird trotzdem: sonst waere die Vorschau ein Einfallstor.
pruefe(hervor('<b>x</b>').indexOf('&lt;b&gt;') >= 0, 'Vorschau nicht maskiert');
console.log('OK');
"""


def test_suchfeld_loest_aus_und_markiert():
    _in_node(PRUEFUNG_SUCHFELD_LOEST_AUS)


def test_suchfeld_und_markierung_sind_verdrahtet():
    """Checking the functions one by one is not enough: the counter-checks
    passed because the test called them directly instead of via the page.
    So the wiring itself is checked."""
    i = app_mod.seite().index('id="q"')
    feld = app_mod.seite()[i:i + 260]
    assert "sofortSuchen()" in feld, "Enter sucht nicht"
    assert 'onclick="sofortSuchen()"' in app_mod.seite(), "Der Knopf wartet auf die Verzögerung"
    # The preview goes through hervor(), not around it.
    j = app_mod.seite().index('class="prev"')
    assert "hervor(h.preview" in app_mod.seite()[j:j + 120], "Begriff wird nicht markiert"


@pytest.mark.parametrize("wert,erwartet", [
    (60, 60), (0, 0), (95, 95),
    (200, 95),      # past the edge: clamped to the edge
    (-5, 0),
])
def test_untergrenze_ist_einstellbar(server, wert, erwartet):
    a, port = server
    call(port, "PATCH", "/api/v1/config", {"semantic_min": wert})
    assert a.cfg["semantic_min"] == erwartet


def test_unbrauchbare_untergrenze_laesst_den_wert_stehen(server):
    """No falling back to the default: whoever set 60 and then mistypes
    should not silently land at 45 again."""
    a, port = server
    call(port, "PATCH", "/api/v1/config", {"semantic_min": 60})
    call(port, "PATCH", "/api/v1/config", {"semantic_min": "unsinn"})
    assert a.cfg["semantic_min"] == 60


def test_untergrenze_wird_erklaert():
    """A number without an explanation is one nobody changes – and whoever
    does change it should know what is too high and what too low."""
    # The explanation now sits in the row's (i) instead of prose below it –
    # it stays thorough all the same: this one number changes what the
    # search shows at all.
    text = i18n.strings("de")["settings.semantic_min.i"]
    assert len(text) > 400, "zu knapp für eine Einstellung, die die Suche verändert"
    for stichwort in ("45", "0", "Volltextsuche"):
        assert stichwort in text, f"„{stichwort}“ fehlt in der Erklärung"
    for code in ("de", "en", "fr"):
        assert i18n.strings(code)["settings.semantic_min.i"] != text or code == "de"


# --------------------------------------------------------------------------
# Error report
#
# This app's corpus is mail and chat. A report that ends up on a public
# page must therefore not carry along who writes with whom and what the
# user is called. Two safeguards, both tested: what is machine-detectable
# gets replaced – and the rest lies open for editing before sending (see
# the JS checks further down).
# --------------------------------------------------------------------------
def test_anonymisiere_nimmt_mailadressen_heraus():
    text = app_mod.anonymisiere(
        "Access found for vorname.nachname@example.com, valid for 11 h.")
    assert "vorname.nachname" not in text and "example.com" not in text
    assert "Access found for" in text, "der Rest der Zeile muss lesbar bleiben"


def test_anonymisiere_nimmt_die_domaene_mit():
    """Replacing only the part before the @ was not enough: the domain is
    the employer, and that is at least as telltale as the name."""
    assert "contoso" not in app_mod.anonymisiere("a@contoso.example").lower()


def test_anonymisiere_nimmt_den_benutzernamen_aus_pfaden():
    """The login name sits in almost every path a log mentions."""
    aus = app_mod.anonymisiere(
        r"OneDrive-Spiegel: C:\Users\pmustermann\AppData\Local\Archiv\onedrive")
    assert "pmustermann" not in aus
    # What follows is technical and must stay – otherwise the path would be
    # worthless as information.
    assert r"AppData\Local\Archiv\onedrive" in aus


@pytest.mark.parametrize("pfad, weg", [
    ("/Users/pmustermann/Library/Archiv", "pmustermann"),
    ("/home/pmustermann/.local/share/Archiv", "pmustermann"),
])
def test_anonymisiere_kennt_auch_die_unix_pfade(pfad, weg):
    assert weg not in app_mod.anonymisiere(f"Datenordner: {pfad}")
    assert "Archiv" in app_mod.anonymisiere(f"Datenordner: {pfad}")


def test_anonymisiere_vertraegt_leeres():
    assert app_mod.anonymisiere(None) == ""


def test_gekuerzt_behaelt_das_ende():
    """The app start is at the front, the crash at the back. Whoever trims, trims the front."""
    text = "\n".join(f"zeile {i}" for i in range(500))
    aus = app_mod.gekuerzt(text, zeilen=10, zeichen=10_000)
    assert "zeile 499" in aus and "zeile 490" in aus
    assert "zeile 100" not in aus
    assert "ausgelassen" in aus, "das Kürzen muss sichtbar sein"


def test_gekuerzt_laesst_kurzes_unangetastet():
    assert app_mod.gekuerzt("a\nb") == "a\nb"


def test_gekuerzt_haelt_die_zeichengrenze():
    aus = app_mod.gekuerzt("x" * 9000, zeilen=100, zeichen=1000)
    assert len(aus) < 1200 and "ausgelassen" in aus


def test_systemangaben_nennen_was_zur_einordnung_noetig_ist(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    angaben = {z["k"]: z["v"] for z in app_mod.systemangaben(a.status(), "de")}
    # Exactly the questions that would otherwise need a follow-up.
    assert {"version", "os", "python", "cores", "lang", "auth",
            "categories", "index", "model", "ollama"} <= set(angaben)
    assert app_mod.version.VERSION in angaben["version"]
    assert "Skript" in angaben["version"], "gebündelt oder nicht ist die halbe Miete"
    assert app_mod.version.build() in angaben["version"], "which commit – or the report needs a follow-up"
    assert angaben["lang"] == "de"
    assert angaben["cores"] == str(os.cpu_count())


def test_systemangaben_nennen_den_datenordner_nur_wenn_er_abweicht(sandbox, with_ollama):
    """The default folder is fixed anyway and would only carry a username
    along – the deviating one, though, explains a whole class of errors."""
    a = app_mod.App(app_mod.load_config())
    st = a.status()
    assert "datadir" not in {z["k"] for z in app_mod.systemangaben(
        st, datenordner="/irgendwo", vorgabe="/irgendwo")}

    zeilen = {z["k"]: z["v"] for z in app_mod.systemangaben(
        st, datenordner=r"C:\Users\pmustermann\Woanders", vorgabe="/irgendwo")}
    assert "Woanders" in zeilen["datadir"]
    assert "pmustermann" not in zeilen["datadir"], "auch hier wird ersetzt"


def test_systemangaben_sind_reine_schluesselruempfe(sandbox, with_ollama):
    """Translation happens in the UI – no finished sentence may stand here."""
    a = app_mod.App(app_mod.load_config())
    for z in app_mod.systemangaben(a.status()):
        assert "." not in z["k"] and z["k"].islower(), z


def test_einstellungs_abweichungen_bei_vorgabe_leer(sandbox):
    assert app_mod.einstellungs_abweichungen(app_mod.load_config()) == []
    a = app_mod.App(app_mod.load_config())
    assert "settings" not in {z["k"] for z in app_mod.systemangaben(a.status())}


def test_einstellungs_abweichungen_nennen_werte_aber_keine_inhalte(sandbox):
    """Changed numbers and switches go into the report; whatever names
    someone (folder names, one's own name, the tenant) shrinks to its size."""
    cfg = app_mod.load_config()
    cfg.update(workers=8, embed_images=False,
               folder_rules="+ E-Mail/Kunden/**\n- E-Mail/Privat/**",
               analytics_skip=["beispiel, alice"],
               tenant="contoso.example",
               schedule={**cfg["schedule"], "enabled": True, "interval_minutes": 30})
    aus = "; ".join(app_mod.einstellungs_abweichungen(cfg))
    assert "workers=8" in aus and "embed_images=false" in aus
    assert "folder_rules: 2 Zeilen" in aus
    assert "analytics_skip: 1 Eintrag" in aus
    assert "tenant: gesetzt" in aus
    assert "enabled=true" in aus and "interval_minutes=30" in aus
    # the contents themselves appear nowhere
    for privat in ("Kunden", "Privat", "beispiel", "contoso"):
        assert privat not in aus


def test_einstellungs_abweichungen_stehen_im_bericht(sandbox, with_ollama):
    cfg = app_mod.load_config()
    cfg["workers"] = 2
    a = app_mod.App(cfg)
    zeilen = {z["k"]: z["v"] for z in app_mod.systemangaben(a.status(), cfg=a.cfg)}
    assert "workers=2" in zeilen["settings"]


def test_fehlerbericht_reicht_nichts_ungefiltert_durch(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    b = app_mod.fehlerbericht(
        a.status(),
        log_text="Access found for chef@contoso.example\n"
                 r"Spiegel: C:\Users\pmustermann\AppData",
        hint="Fehler bei chef@contoso.example")
    assert "chef@contoso.example" not in b["log"]
    assert "pmustermann" not in b["log"]
    assert "chef@contoso.example" not in b["title"], "auch der Betreff"
    assert b["url"].startswith("https://github.com/")
    assert b["url"].endswith("/issues/new")
    assert app_mod.version.REPO in b["url"]


def test_fehlerbericht_kappt_einen_endlosen_betreff(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    b = app_mod.fehlerbericht(a.status(), hint="x" * 400)
    assert len(b["title"]) <= 120


def test_http_runs(server):
    """The run history for the analytics section, newest first."""
    a, port = server
    run_id = a.history.start_run("job.export", "manual", workers=4)
    a.history.record_step(run_id, "outlook", "job.step.outlook", 0.0, 1.0,
                          result={"new": 2})
    a.history.finish_run(run_id, "done")
    code, r = call(port, "GET", "/api/v1/runs?limit=10")
    assert code == 200
    lauf = r["items"][0]
    assert lauf["job_type"] == "job.export" and lauf["result"] == "done"
    assert lauf["steps"][0]["new"] == 2


def test_http_report(server):
    """The path the UI takes."""
    a, port = server
    code, b = call(port, "QUERY", "/api/v1/reports",
                   {"log": "09:00:00  Hallo welt@example.com", "hint": "Absturz"})
    assert code == 200
    assert b["title"] == "Absturz"
    assert "welt@example.com" not in b["log"] and "Hallo" in b["log"]
    assert [z["k"] for z in b["system"]][:2] == ["version", "os"]


def test_http_report_ohne_angaben(server):
    """The button in the settings gets pressed even with an empty log."""
    _, port = server
    code, b = call(port, "QUERY", "/api/v1/reports", {})
    assert code == 200 and b["log"] == "" and b["title"] == ""
    assert b["system"], "die Systemangaben stehen immer zur Verfügung"


def test_http_report_folgt_der_browsersprache(server):
    """The language is part of the report because it explains which texts
    the reporter has seen."""
    _, port = server
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("QUERY", "/api/v1/reports", "{}",
                {"Content-Type": "application/json", "Accept-Language": "fr-CH,fr;q=0.9"})
    b = json.loads(con.getresponse().read())
    con.close()
    assert {z["k"]: z["v"] for z in b["system"]}["lang"] == "fr"


# --------------------------------------------------------------------------
# Copying the log
#
# The box has one child per line. textContent glued them together without
# a break – a log that lands in the clipboard as one single line is
# worthless as an error report.
# --------------------------------------------------------------------------
PRUEFUNG_LOG_KOPIEREN = GRUNDZUSTAND + """
var kopiert = [];
Object.defineProperty(global, 'navigator', {configurable: true, writable: true,
  value: {clipboard: {writeText: function(t){ kopiert.push(t);
                                              return Promise.resolve(); }}}});
var kasten = document.getElementById('log');
kasten.children = [{textContent: '09:00:00  erste Zeile'},
                   {textContent: '09:00:01  zweite Zeile'}];
kopiere('log', {textContent: 'Kopieren'});
pruefe(kopiert.length === 1, 'Nichts kopiert');
pruefe(kopiert[0] === '09:00:00  erste Zeile\\n09:00:01  zweite Zeile',
       'Zeilen nicht getrennt: ' + JSON.stringify(kopiert[0]));
console.log('OK');
"""


def test_protokoll_laesst_sich_zeilenweise_kopieren():
    _in_node(PRUEFUNG_LOG_KOPIEREN)


def test_kopierknopf_klappt_das_protokoll_nicht_zu():
    """The buttons sit in the header row, which itself folds open and shut.
    Without stopPropagation the log collapsed on every copy – you would lose
    sight of the result at exactly the moment you need it."""
    seite = app_mod.seite()
    kopf = seite.split('class="pkopf"')[1].split("</div>")[0]
    for knopf in ("kopiere('log', this)", "fehlerMelden()"):
        assert knopf in kopf, f"{knopf} steht nicht in der Protokollkopfzeile"
    assert kopf.count("event.stopPropagation()") == 2, (
        "Nicht jeder Knopf in der Kopfzeile hält sein Klickereignis an")


# --------------------------------------------------------------------------
# Reporting an error – the part of this flow that runs in the browser
# --------------------------------------------------------------------------
BERICHT_GERUEST = GRUNDZUSTAND + """
global.geoeffnet = [];
// Im Browser IST window das globale Objekt; node bringt keines mit.
global.window = {open: function(u){ geoeffnet.push(u); }};
// Zwei Endpunkte, zwei Antworten: erst das Protokoll, dann der Bericht.
global.gesendet = [];
global.fetch = function(pfad, opt){
  var antwort;
  if(String(pfad).indexOf('/api/v1/log') === 0){
    antwort = {seq: 3, items: [
      {n: 1, level: 'info', t: '09:00:00', text: 'Export gestartet'},
      {n: 2, level: 'err',  t: '09:24:36', text: 'BrokenProcessPool: abrupt beendet'}]};
  } else if(String(pfad) === '/api/v1/reports'){
    global.berichtMethode = opt.method;
    gesendet.push(JSON.parse(opt.body));
    antwort = {system: [{k: 'version', v: '4.0.0 (Skript)'},
                        {k: 'cores', v: '8'}],
               log: '09:00:00  Export gestartet\\n09:24:36  BrokenProcessPool',
               title: 'BrokenProcessPool: abrupt beendet',
               url: 'https://github.com/beispiel/repo/issues/new'};
  } else {
    antwort = statusGeruest();
  }
  return Promise.resolve({json: function(){ return Promise.resolve(antwort); }});
};
"""

PRUEFUNG_BERICHT = BERICHT_GERUEST + """
fehlerMelden();
// Solange nichts da ist, steht der Dialog trotzdem schon offen.
pruefe(wizardOffen === 'report', 'Fenster nicht geoeffnet');

setTimeout(function(){
  var html = modal.innerHTML;
  // Vier Felder, dieselben wie im Bug-Formular auf GitHub.
  ['rep-was', 'rep-system', 'rep-ablauf', 'rep-log'].forEach(function(id){
    pruefe(html.indexOf('id="' + id + '"') >= 0, id + ' fehlt: ' + html.slice(0, 200));
  });

  // Was mitgeschickt wurde: das uebersetzte Protokoll und die letzte
  // Fehlerzeile als Betreffvorschlag.
  pruefe(gesendet.length === 1, 'Kein Bericht angefordert');
  // Eine Frage, kein Schreibzugriff: QUERY (RFC 10008), nicht POST.
  pruefe(berichtMethode === 'QUERY', 'Bericht nicht per QUERY geholt: ' + berichtMethode);
  pruefe(gesendet[0].log.indexOf('Export gestartet') >= 0, 'Protokoll fehlt');
  pruefe(gesendet[0].hint.indexOf('BrokenProcessPool') >= 0,
         'Betreffvorschlag kommt nicht aus der Fehlerzeile: ' + gesendet[0].hint);

  // Der Text, den der Mensch vor sich hat: Angaben und Protokoll, beides drin.
  // Geprueft wird am gezeichneten HTML - der DOM-Stummel zerlegt innerHTML
  // nicht in Knoten, im Browser steht genau dieser Text in den Feldern.
  pruefe(html.indexOf('4.0.0 (Skript)') >= 0, 'Systemangaben fehlen');
  pruefe(html.indexOf('BrokenProcessPool') >= 0, 'Protokoll fehlt im Bericht');
  pruefe(html.indexOf('Kerne: 8') >= 0,
         'Die Angaben sind nicht uebersetzt: ' + html);
  pruefe(html.indexOf('value="BrokenProcessPool: abrupt beendet"') >= 0,
         'Betreff nicht vorbelegt');

  // Geaendert wird vor dem Absenden - und die Aenderung muss ankommen,
  // Feld fuer Feld im richtigen Parameter. (Die Werte von Hand setzen: der
  // DOM-Stummel liest vorbefuellte Textfelder nicht aus dem HTML.)
  document.getElementById('rep-was').value = 'Beim Export passiert';
  document.getElementById('rep-system').value = 'Von Hand umgeschrieben';
  document.getElementById('rep-ablauf').value = 'Reiter: suche';
  document.getElementById('rep-log').value = '09:00:00  Export gestartet';
  document.getElementById('rep-titel').value = 'Eigener Betreff';
  berichtOeffnen();
  pruefe(geoeffnet.length === 1, 'Kein Formular geoeffnet');
  var u = geoeffnet[0];
  pruefe(u.indexOf('https://github.com/beispiel/repo/issues/new?template=bug.yml') === 0,
         'Nicht das Bug-Formular: ' + u);
  pruefe(u.indexOf('title=Eigener%20Betreff') >= 0, 'Eigener Betreff fehlt: ' + u);
  pruefe(u.indexOf('what=' + encodeURIComponent('Beim Export passiert')) >= 0,
         'Beschreibung nicht im what-Feld: ' + u);
  pruefe(u.indexOf('system=' + encodeURIComponent('Von Hand umgeschrieben')) >= 0,
         'Die Aenderung wurde nicht uebernommen: ' + u);
  pruefe(u.indexOf('actions=' + encodeURIComponent('Reiter: suche')) >= 0,
         'Ablauf nicht im actions-Feld: ' + u);
  pruefe(u.indexOf('log=' + encodeURIComponent('09:00:00')) >= 0,
         'Protokoll nicht im log-Feld: ' + u);
  pruefe(u.indexOf('4.0.0') < 0,
         'Der ersetzte Text steht trotzdem in der Adresse');
  console.log('OK');
}, 30);
"""


def test_fehlerbericht_zeigt_alles_und_laesst_es_aendern():
    _in_node(PRUEFUNG_BERICHT)


# The userflow recording: only the kind of steps, bounded, can be turned off.
PRUEFUNG_ABLAUF = GRUNDZUSTAND + """
if(!S.config) S.config = {};
S.config.userflow_actions = 3;
merke('flow.tab', 'export');
merke('flow.search', 'hybrid +2');
merke('flow.run', 'job.export');
merke('flow.mcp', 'start');
pruefe(ablauf.length === 3, 'Grenze nicht angewendet: ' + ablauf.length);
pruefe(ablauf[0].k === 'flow.search', 'Nicht die aeltesten verworfen');
var text = ablaufText();
pruefe(text.indexOf('Suche: hybrid +2') >= 0, 'Nicht uebersetzt: ' + text);
pruefe(/\\d\\d:\\d\\d:\\d\\d/.test(text), 'Kein Zeitstempel: ' + text);
// 0 heisst aus - und raeumt auch schon Gesammeltes weg.
S.config.userflow_actions = 0;
merke('flow.tab', 'suche');
pruefe(ablauf.length === 0, 'Aus, aber es wird weiter gesammelt');
pruefe(ablaufText() === '', 'Aus, aber der Bericht bekaeme etwas');
console.log('OK');
"""


def test_userflow_aufzeichnung_begrenzt_und_abschaltbar():
    _in_node(PRUEFUNG_ABLAUF)


# The run history in the analytics section: a row per run, details per step.
PRUEFUNG_LAEUFE = GRUNDZUSTAND + """
renderRuns([]);
pruefe(el('ana-runs').innerHTML.indexOf('Noch keine') >= 0,
       'Leerer Zustand fehlt: ' + el('ana-runs').innerHTML);

renderRuns([{started_at: 1755000000, finished_at: 1755000065,
  origin: 'schedule', result: 'done',
  elements: {outlook: ['mail'], teams: [], onedrive: true},
  steps: [
    {key: 'outlook', label: 'job.step.outlook', started_at: 1755000000,
     duration_s: 42, new: 3, unchanged: 10, excluded: null, errors: null,
     skipped: 0, ok: 1, extra: null},
    {key: 'onedrive', label: 'job.step.onedrive', started_at: 1755000020,
     duration_s: 12, new: 5, unchanged: 90, excluded: null, errors: null,
     skipped: 0, ok: 1, extra: null},
    // Der Kalenderschritt meldet den GANZEN Neuaufbau – der zaehlt nicht
    // als neu Exportiertes und darf weder Summe noch Aufteilung verzerren.
    {key: 'calendar', label: 'job.step.calendar', started_at: 1755000040,
     duration_s: 2, new: 5860, unchanged: null, excluded: null, errors: null,
     skipped: 0, ok: 1, extra: null},
    {key: 'index', label: 'job.step.index', started_at: 1755000042,
     duration_s: null, new: null, unchanged: null, excluded: null,
     errors: null, skipped: 1, ok: null, extra: null}
  ]}]);
var html = el('ana-runs').innerHTML;
// Kategorien in Klammern, uebersetzt; alles aktiviert heisst "(alle)".
pruefe(html.indexOf('Outlook (E-Mail), OneDrive (alle)') >= 0,
       'Elemente ohne Kategorien: ' + html);
pruefe(html.indexOf('Zeitplan') >= 0, 'Ausloeser nicht uebersetzt: ' + html);
pruefe(runElements({elements: {outlook: ['mail', 'calendar', 'contacts'],
                               teams: ['1on1', 'group'], onedrive: false}})
       === 'Outlook (alle), Teams (1:1-Chats, Gruppenchats)',
       'Vollauswahl nicht als "alle": ' +
       runElements({elements: {outlook: ['mail', 'calendar', 'contacts'],
                               teams: ['1on1', 'group'], onedrive: false}}));
pruefe(html.indexOf('fertig') >= 0, 'Ergebnis nicht uebersetzt: ' + html);
pruefe(html.indexOf('42 s') >= 0, 'Schrittdauer fehlt: ' + html);
pruefe(html.indexOf('bersprungen') >= 0, 'Uebersprungener Schritt fehlt: ' + html);
pruefe(html.indexOf('1 min') >= 0, 'Gesamtdauer fehlt: ' + html);
// "New" zaehlt nur die Exporte; das Mouseover teilt sie je Quelle auf.
pruefe(html.indexOf('title="Outlook: 3\\nOneDrive: 5"') >= 0,
       'Aufteilung je Quelle fehlt: ' + html);
pruefe(html.indexOf('>8<') >= 0, 'Summe stimmt nicht (nur Exporte): ' + html);
pruefe(html.indexOf('>5.868<') < 0 && html.indexOf('>5868<') < 0,
       'Der Kalender-Neuaufbau steht in der Summe: ' + html);

// A run without a source is named after its step – a row saying "–" for
// the calendar rebuild says nothing.
renderRuns([{started_at: 1755000000, finished_at: 1755000294, origin: 'manual',
  result: 'done', elements: {outlook: [], teams: []},
  steps: [{key: 'calendar', label: 'job.step.calendar', started_at: 1755000000,
           duration_s: 294, new: 8625, unchanged: null, excluded: null, errors: null,
           skipped: 0, ok: 1, extra: null}]}]);
html = el('ana-runs').innerHTML;
pruefe(html.indexOf('Kalender &amp; Kontakte') >= 0, 'Kalenderlauf ohne Namen: ' + html);
pruefe(html.indexOf('>–<') >= 0, 'Der Neuaufbau zaehlt als neu');
console.log('OK');
"""


def test_lauf_historie_wird_gerendert():
    _in_node(PRUEFUNG_LAEUFE)


PRUEFUNG_BERICHT_LEER = BERICHT_GERUEST + """
fehlerMelden();
setTimeout(function(){
  document.getElementById('rep-titel').value = '   ';
  berichtOeffnen();
  // Ein Entwurf ganz ohne Betreff waere auf GitHub nicht abzuschicken.
  pruefe(geoeffnet[0].indexOf('title=') >= 0 &&
         geoeffnet[0].indexOf('title=&') < 0, 'Leerer Betreff: ' + geoeffnet[0]);
  console.log('OK');
}, 30);
"""


def test_fehlerbericht_faellt_auf_einen_betreff_zurueck():
    _in_node(PRUEFUNG_BERICHT_LEER)


PRUEFUNG_BERICHT_LANG = BERICHT_GERUEST + """
fehlerMelden();
setTimeout(function(){
  // Jemand fuegt ein sehr langes Protokoll ein. GitHub bekommt den Entwurf in
  // der Adresse; zu lange Adressen weist der Server ab - mit einer leeren
  // Seite, nicht mit einer Erklaerung.
  var zeilen = [];
  for(var i = 0; i < 900; i++) zeilen.push('09:00:00  Zeile ' + i + ' mit etwas Text');
  zeilen.push('09:59:59  DAS HIER IST DER ABSTURZ');
  document.getElementById('rep-system').value = 'Version: 4.0.0';
  document.getElementById('rep-log').value = zeilen.join('\\n');
  berichtOeffnen();

  var u = geoeffnet[0];
  pruefe(u.length <= 7000, 'Adresse zu lang: ' + u.length);
  // Gekuerzt wird VORNE: die letzten Zeilen sind die, um die es geht.
  pruefe(u.indexOf(encodeURIComponent('DAS HIER IST DER ABSTURZ')) >= 0,
         'Der Absturz fehlt im gekuerzten Bericht');
  pruefe(u.indexOf(encodeURIComponent('Zeile 0 mit')) < 0,
         'Die aeltesten Zeilen stehen noch drin');
  pruefe(u.indexOf(encodeURIComponent('Version: 4.0.0')) >= 0,
         'Die Systemangaben wurden mit weggekuerzt');
  // Und es wird gesagt, statt es stillschweigend zu tun.
  pruefe(document.getElementById('rep-hinweis').textContent.length > 0,
         'Kein Hinweis auf das Kuerzen');
  console.log('OK');
}, 30);
"""


def test_fehlerbericht_kuerzt_vorne_und_sagt_es():
    _in_node(PRUEFUNG_BERICHT_LANG)


# --------------------------------------------------------------------------
# The three search modes
#
# Text search is the default and purely lexical; the semantic and the AI
# variant are a deliberate detour. No search mixes BM25 and vectors behind
# the user's back any more, and none spins up a model unasked – the answer
# is a choice, not a checkbox on every query.
# --------------------------------------------------------------------------
PRUEFUNG_MODI = GRUNDZUSTAND + """
var gesucht = [], gefragt = 0;
global.fetch = function(pfad, opt){
  var s = String(pfad);
  if(s.indexOf('/search?') >= 0){ gesucht.push(s); }
  if(s.indexOf('/answer') >= 0){ gefragt++; return Promise.resolve({ok: false,
    json: function(){ return Promise.resolve({error: 'x'}); }}); }
  return Promise.resolve({json: function(){ return Promise.resolve(
    s.indexOf('/search?') >= 0
      ? {count: 1, results: [{uid: 'u:1', cid: 7, title: 'T', who: 'Alice',
                              date: '2026-03-04', source_label: 'Mail',
                              preview: 'p', uri: 'o365://outlook/a.eml'}]}
      : statusGeruest()); }});
};
document.getElementById('q').value = 'Rechnung';

// Vorgabe ist die Textsuche, und die geht als lexical an den Server.
pruefe(SUCHMODUS === 'text', 'Vorgabe ist nicht die Textsuche: ' + SUCHMODUS);
sofortSuchen();
pruefe(gesucht[0].indexOf('mode=lexical') >= 0, 'Textsuche nicht lexical: ' + gesucht[0]);

// Jede Variante hat ihren eigenen Server-Modus.
gesucht = []; suchmodus('aehnlich');
pruefe(gesucht[0].indexOf('mode=semantic') >= 0, 'Aehnliche nicht semantic: ' + gesucht[0]);
gesucht = []; suchmodus('ki');
pruefe(gesucht[0].indexOf('mode=hybrid') >= 0, 'KI nicht hybrid: ' + gesucht[0]);

// Der Platzhalter wechselt mit - er ist die einzige Erklaerung, die es gibt.
var platz = {};
['text','aehnlich','ki'].forEach(function(a){
  suchmodus(a); platz[a] = document.getElementById('q').placeholder;
});
pruefe(platz.text && platz.aehnlich && platz.ki, 'Platzhalter fehlt');
pruefe(platz.text !== platz.aehnlich && platz.aehnlich !== platz.ki,
       'Platzhalter unterscheiden sich nicht');
console.log('OK');
"""


def test_suchvarianten_gehen_an_den_richtigen_backend():
    _in_node(PRUEFUNG_MODI)


PRUEFUNG_KI_NUR_AUF_WUNSCH = GRUNDZUSTAND + """
var gefragt = 0;
global.fetch = function(pfad){
  var s = String(pfad);
  if(s.indexOf('/answer') >= 0){ gefragt++;
    return Promise.resolve({ok: false, json: function(){
      return Promise.resolve({error: 'x'}); }}); }
  return Promise.resolve({json: function(){ return Promise.resolve(
    s.indexOf('/search?') >= 0
      ? {count: 1, results: [{uid: 'u:1', cid: 7, title: 'T', who: 'A',
                              date: '2026-03-04', source_label: 'Mail', preview: 'p'}]}
      : statusGeruest()); }});
};
document.getElementById('q').value = 'Rechnung';

// Das war die Klage: die Suche wartete auf das Modell. Text und Aehnliche
// fragen es gar nicht erst.
suchmodus('text'); sofortSuchen();
suchmodus('aehnlich'); sofortSuchen();
pruefe(gefragt === 0, 'Das Modell lief ungefragt: ' + gefragt);

setTimeout(function(){
  pruefe(gefragt === 0, 'Das Modell lief verspaetet doch: ' + gefragt);
  console.log('OK');
}, 20);
"""


def test_die_ki_laeuft_nur_in_ihrer_eigenen_variante():
    _in_node(PRUEFUNG_KI_NUR_AUF_WUNSCH)


PRUEFUNG_TAKT = GRUNDZUSTAND + """
// Der Takt: waehrend eines Laufs eng, sonst weit, und bei verborgenem
// Tab gar nicht. Jede eigene Aktion holt den Status ohnehin selbst.
var geholt = [];
global.fetch = function(pfad){
  geholt.push(String(pfad));
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/v1/log') === 0 ? {items: [], seq: 0} : statusGeruest()); }});
};
function schlaege(n, versatz){
  for(var i = 0; i < n; i++){
    TAKT_STATUS.status -= versatz; TAKT_STATUS.log -= versatz;
    // Eine Antwort ist in Wirklichkeit laengst da, bevor der naechste
    // Schlag kommt; die Sperre gegen zwei gleichzeitige Abrufe faellt.
    LOG_LAEUFT = false;
    takt();
  }
}
// Die Einstellungen sind schon geholt; dieser Test geht um den Takt.
KONFIG = {};
// Leerlauf: in neun Schlaegen zu je einer Sekunde kein Status (Vorgabe
// sind 30 s) und gar kein Protokoll - das steht nur im Lauf-Fenster.
S.jobs = {busy: false};
LAUF_OFFEN = false;
TAKT_STATUS.status = TAKT_STATUS.log = Date.now();
geholt = [];
schlaege(9, 1000);
var status = geholt.filter(function(p){ return p.indexOf('/api/v1/status') === 0; }).length;
var prot = geholt.filter(function(p){ return p.indexOf('/api/v1/log') === 0; }).length;
pruefe(status === 0 && prot === 0, 'Leerlauf fragt zu oft: ' + status + ' Status, ' + prot + ' Protokoll');

// Waehrend eines Laufs mit geschlossenem Fenster: Status ja, Protokoll
// nein - das Protokoll steht nur im Lauf-Fenster.
S.jobs = {busy: true};
LAUF_OFFEN = false;
TAKT_STATUS.status = TAKT_STATUS.log = Date.now();
geholt = [];
schlaege(9, 1000);
status = geholt.filter(function(p){ return p.indexOf('/api/v1/status') === 0; }).length;
prot = geholt.filter(function(p){ return p.indexOf('/api/v1/log') === 0; }).length;
pruefe(status >= 3, 'Lauf wird zu selten verfolgt: ' + status);
pruefe(prot === 0, 'Protokoll geholt, obwohl es niemand sieht: ' + prot);

// Mit offenem Fenster: jede Sekunde.
LAUF_OFFEN = true;
TAKT_STATUS.status = TAKT_STATUS.log = Date.now();
geholt = [];
schlaege(9, 1000);
prot = geholt.filter(function(p){ return p.indexOf('/api/v1/log') === 0; }).length;
pruefe(prot >= 8, 'Offenes Fenster sieht dem Lauf nicht zu: ' + prot);
LAUF_OFFEN = false;

// Der Leerlauftakt ist eine Einstellung (Expertenmodus) - und gilt nur
// im Leerlauf, also ohne Lauf.
S.jobs = {busy: false};
KONFIG = Object.assign({}, KONFIG, {status_poll_seconds: 5});
TAKT_STATUS.status = Date.now();
geholt = [];
schlaege(9, 1000);
pruefe(geholt.filter(function(p){ return p.indexOf('/api/v1/status') === 0; }).length === 1,
       'Eingestellter Takt wirkt nicht');
KONFIG = Object.assign({}, KONFIG, {status_poll_seconds: 600});
TAKT_STATUS.status = Date.now();
geholt = [];
schlaege(9, 1000);
pruefe(geholt.length === 0, 'Langer Takt fragt trotzdem');
delete KONFIG.status_poll_seconds;

// Verborgener Tab: nichts.
document.visibilityState = 'hidden';
geholt = [];
schlaege(9, 1000);
pruefe(geholt.length === 0, 'Verborgener Tab fragt trotzdem: ' + geholt.length);
document.visibilityState = 'visible';
console.log('OK');
"""


PRUEFUNG_FEHLERMELDUNG = GRUNDZUSTAND + """
// Jede Ablehnung der API landet an einer Stelle: der einen Meldung der
// Seite, in der Fehlerfarbe und laenger stehend als eine gute Nachricht.
// Kein alert mehr – DESIGN.md §8 verbietet ihn, wo die Seite einen Platz hat.
var m = document.getElementById('meldung');
pruefe(apiFehler({ok: false, status: 409, detail: 'A job is already running.',
                  error: {k: 'srv.busy', v: {}}}) === true, 'Ablehnung nicht erkannt');
pruefe(m.textContent === t('srv.busy'), 'Grund fehlt: ' + m.textContent);
pruefe(m.classList.contains('err'), 'Nicht als Fehler gezeichnet');

// Ohne Schluessel bleibt der englische Satz des Servers.
apiFehler({ok: false, status: 409, detail: 'This index predates deletion tracking.'});
pruefe(m.textContent.indexOf('predates') >= 0, 'Klartextgrund fehlt: ' + m.textContent);

// Eine gelungene Antwort meldet nichts.
m.textContent = '';
pruefe(apiFehler({ok: true, case: {}}) === false, 'Erfolg als Fehler gemeldet');
pruefe(apiFehler({cases: []}) === false, 'Leseantwort als Fehler gemeldet');
pruefe(m.textContent === '', 'Erfolg hat gemeldet: ' + m.textContent);

// Und die gute Nachricht nutzt dieselbe Stelle, nur ohne Fehlerfarbe.
meldung('fertig');
pruefe(m.textContent === 'fertig' && !m.classList.contains('err'), 'Gute Nachricht in Fehlerfarbe');
console.log('OK');
"""


PRUEFUNG_ABLEHNUNG = GRUNDZUSTAND + """
// Eine Ablehnung ohne Textschluessel – die Suchmaschine sagt ihren Grund
// selbst – traegt ihn nur in `detail`. Die Trefferliste muss ihn zeigen,
// nicht „keine Treffer".
renderHits({ok: false, status: 409, detail: 'This index predates deletion tracking.',
            hits: [], count: 0});
var h = document.getElementById('results').innerHTML;
pruefe(h.indexOf('predates deletion tracking') >= 0, 'Grund der Absage fehlt: ' + h);
pruefe(h.indexOf(t('search.nohits')) < 0, 'zeigt „keine Treffer" statt des Grundes');

// Mit Schluessel uebersetzt die Seite weiter selbst.
renderHits({ok: false, status: 503, detail: 'No index available.',
            error: {k: 'srv.noindex', v: {}}, hits: [], count: 0});
h = document.getElementById('results').innerHTML;
pruefe(h.indexOf(t('srv.noindex')) >= 0, 'uebersetzter Grund fehlt: ' + h);

// Und eine Antwort ohne alles bleibt eine Trefferliste.
renderHits({count: 0, results: []});
pruefe(document.getElementById('results').innerHTML.indexOf(t('search.nohits')) >= 0,
       'Leerzustand der Suche fehlt');
console.log('OK');
"""


PRUEFUNG_TREFFERZEILE = GRUNDZUSTAND + """
// „Ähnliche finden" haengt an Vektoren im Index – ohne die waere der Eintrag
// zu Recht gesperrt, und dieser Test prueft die Zeile, nicht die Sperre.
BESTAND = Object.assign({}, BESTAND, {store: Object.assign({}, BESTAND.store, {semantic: true})});
S = Object.assign({}, BESTAND, statusGeruest());   // wie renderStatus mischt
KANN_VERLAUF = true;      // otherwise set from store.features on the status poll
renderHits({count: 2, results: [
  {uid: 'u:1', cid: 7, title: 'Rechnung 4711', who: 'Alice', date: '2026-03-04',
   source_label: 'Mail', preview: 'Text', uri: 'o365://outlook/a.eml', thread: 'x'},
  {uid: 'u:2', cid: 8, title: 'Notiz.pdf', who: '', date: '2026-03-01',
   source_label: 'Datei', preview: 'Pfad'}
]});
var h = document.getElementById('results').innerHTML;

// Datum in eigener Spalte: nur so stehen die Daten untereinander.
pruefe(h.indexOf('class="wann"') >= 0, 'Datum hat keine eigene Spalte');
pruefe(h.indexOf('2026-03-04') >= 0, 'Datum fehlt');

// Keine Knopfreihe und kein Menue in der Liste: die Aktionen stehen im
// Detail des gewaehlten Treffers.
pruefe(h.indexOf('aehnlicheZu(') < 0 && h.indexOf('punkte-knopf') < 0,
       'Aktionen stehen in der Liste');
waehleTreffer(0);
var d = document.getElementById('detail-inhalt').innerHTML;
pruefe(d.indexOf('aehnlicheZu(') >= 0, 'Aehnliche finden fehlt im Detail');
pruefe(d.indexOf('class="fakten"') >= 0 && d.indexOf('class="daktionen"') >= 0, 'Fakten oder Aktionen fehlen im Detail');
pruefe(d.indexOf('Rechnung 4711') >= 0, 'Detail zeigt nicht den gewaehlten Treffer');

// Was fuer diesen Treffer nicht geht, steht ausgegraut drin statt zu fehlen -
// sonst wandern die Knoepfe je Treffer an andere Stellen.
waehleTreffer(1);
d = document.getElementById('detail-inhalt').innerHTML;
pruefe(d.indexOf('disabled') >= 0, 'Unmoegliches fehlt statt ausgegraut zu sein');
console.log('OK');
"""


def test_trefferzeile_ist_kompakt_und_die_aktionen_stehen_im_detail():
    _in_node(PRUEFUNG_TREFFERZEILE)


def test_die_trefferliste_zeigt_den_grund_einer_absage():
    """A refusal whose reason has no text key carries it in `detail` –
    before 11.4 the same reason came back in `error` at HTTP 200, and the
    list must not silently turn either into „no hits"."""
    _in_node(PRUEFUNG_ABLEHNUNG)


def test_jede_absage_landet_in_der_einen_meldung():
    """One place for every refusal the API sends – the page's own message,
    in the error colour. `alert` is gone from the page (DESIGN.md §8)."""
    _in_node(PRUEFUNG_FEHLERMELDUNG)


def test_der_takt_folgt_dem_lauf_und_schweigt_im_hintergrund():
    """Polling costs someone's machine something. During a run the page
    follows closely; idle it asks rarely, because every action of its own
    refreshes anyway; hidden it asks nothing."""
    _in_node(PRUEFUNG_TAKT)


PRUEFUNG_ANTWORTEN_13 = GRUNDZUSTAND + """
// Drei Antworten, die 13.0 umgeformt hat – die Seite las noch die alten
// Felder und tat dann nichts, ohne etwas zu sagen.
var gesendet = [];
global.fetch = function(pfad, opt){
  var s = String(pfad);
  gesendet.push({pfad: s, methode: (opt && opt.method) || 'GET'});
  var antwort = statusGeruest();
  if(s.indexOf('/api/v1/storage') === 0)
    antwort = {data_dir: '/Users/beispiel/Archiv', index_dir: '/Users/beispiel/Index',
               restart_required: true};                    // kein ok mehr
  else if(s.indexOf('/api/v1/runs') === 0 && opt && opt.method === 'POST')
    antwort = {run: '/api/v1/runs/current'};               // 202 statt ok
  else if(s.indexOf('/api/v1/inventory') === 0)
    antwort = {error: {k: 'srv.noindex', v: {}}};          // Bestand bleibt leer
  return Promise.resolve({json: function(){ return Promise.resolve(antwort); }});
};

function warte(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }

(async function(){
  // 1) Der Speicherort kommt aufgeloest zurueck und gehoert ins Feld.
  document.getElementById('c-data-dir').value = '~/Archiv';
  document.getElementById('c-index-dir').value = '';
  await speichereAblage();
  await warte(10);
  pruefe(document.getElementById('c-data-dir').value === '/Users/beispiel/Archiv',
         'Aufgeloester Datenordner nicht uebernommen: ' + document.getElementById('c-data-dir').value);
  pruefe(document.getElementById('c-index-dir').value === '/Users/beispiel/Index',
         'Indexordner nicht uebernommen');

  // 2) Ein gestarteter Lauf nennt sich `run` – die Vorschau darf nicht
  //    stumm abbrechen.
  gesendet.length = 0;
  sharepointVorschau();
  await warte(20);
  pruefe(gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') === 0 && g.methode === 'POST'; }).length === 1,
         'Vorschau hat keinen Lauf gestartet');
  pruefe(document.getElementById('sp-msg').textContent !== '',
         'Die Meldung wurde leer geschrieben – der Lauf gilt als gescheitert');

  // 3) Ein leerer Bestand darf das Zeichnen nicht abbrechen.
  BESTAND = {};
  KANN_TYP = true;
  var s = statusGeruest();
  delete s.store;
  try { renderStatus(s); } catch(e){ pruefe(false, 'renderStatus wirft: ' + e.message); }
  // KANN_TYP wird NACH dem store-Block gesetzt: steht es hinterher auf
  // false, ist die Zeichnung durchgelaufen statt auf halbem Weg zu enden.
  pruefe(KANN_TYP === false, 'renderStatus ist im store-Block abgebrochen');
  console.log('OK');
})().catch(function(e){ console.log('FEHLER ' + (e.stack || e)); process.exit(1); });
"""


PRUEFUNG_FELDER_13 = GRUNDZUSTAND + """
// Felder, die 13.0 umbenannt hat, und die Stellen, die sie lesen.
var gesendet = [];
global.fetch = function(pfad, opt){
  var s = String(pfad);
  gesendet.push({pfad: s, methode: (opt && opt.method) || 'GET',
                 body: opt && opt.body ? JSON.parse(opt.body) : null});
  var antwort = statusGeruest();
  if(s.indexOf('/api/v1/search?') === 0)
    antwort = {items: [{uid: 'outlook:a.eml:0', key: 'mail:<a>', source: 'outlook',
                        root: 'outlook', path: 'a.eml', title: 'Rechnung',
                        date: '2026-03-04', who: 'Alice', preview: 'x', cases: []}],
               limit: 20, offset: 0, has_more: false, backend: 'bm25'};
  else if(s.indexOf('/api/v1/inventory') === 0)
    antwort = {store: {exists: true, semantic: false, built_at: '2026-03-05T06:10:00',
                       features: ['thread', 'gone', 'who_mail', 'mail_lines', 'domains', 'ext']},
               exports: {outlook: {mails: 4711}}};
  return Promise.resolve({json: function(){ return Promise.resolve(antwort); },
                          ok: true, body: null});
};

function warte(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }

(async function(){
  // 1) Der KI-Modus fragt das Modell – er haengt an `items`, nicht an `results`.
  document.getElementById('q').value = 'Rechnung';
  suchmodus('ki');
  sofortSuchen();
  await warte(60);
  // suchmodus() sucht selbst noch einmal – gezaehlt wird, DASS gefragt wurde.
  pruefe(gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/answer') === 0; }).length >= 1,
         'Der KI-Modus hat das Modell nicht gefragt. Gesendet: ' +
         gesendet.map(function(g){ return g.pfad.split('?')[0]; }).join(', '));

  // 2) Eine einzelne Einheit heisst `unit`.
  gesendet.length = 0;
  run({onenote: true, unit: 'abc', index: true}, 'job.export', 'sync_now');
  await warte(10);
  var lauf = gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/runs') === 0 && g.methode === 'POST'; })[0];
  pruefe(lauf && lauf.body.unit === 'abc', 'Die Einheit reist nicht als `unit`: ' + JSON.stringify(lauf && lauf.body));
  pruefe(!(lauf && 'nur_einheit' in lauf.body), 'Der alte Schluessel reist noch mit');

  // 4) Die Mail-Zeilen stehen als Marken in Verlauf und gespeicherten Suchen.
  var marken = kriterienTags({q: '', mode: 'text', mail_to: 'bob.baumeister@nordwind.example'});
  pruefe(marken.indexOf('bob.baumeister@nordwind.example') >= 0,
         'Die Mail-Zeile fehlt in den Marken: ' + marken);

  // 6) Nach einem Lauf gewinnt der frische Bestand, nicht die alte Kopie.
  renderStatus(statusGeruest());
  KANN_MAIL = false;
  await ladeBestand();
  renderStatus(S_ROH);
  pruefe(KANN_MAIL === true, 'Der frische Bestand kam nicht durch');
  console.log('OK');
})().catch(function(e){ console.log('FEHLER ' + (e.stack || e)); process.exit(1); });
"""


def test_die_seite_liest_die_umbenannten_felder():
    """Four fields changed name or shape with 13.0 and the page still read
    the old ones: the AI mode gated on `results`, a single unit travelled
    as `nur_einheit`, the mail lines were missing from the criteria tags,
    and the render after a run passed the merged copy back in, so the
    fresh inventory lost against the stale one."""
    _in_node(PRUEFUNG_FELDER_13)


def test_eine_gesetzte_mailzeile_leuchtet():
    """`zeigeFilterstand` marks a set line with `on` – the stylesheet has
    to have a rule for it, or the tour's promise ("a set filter lights up
    where it stands") is not kept inside the popover."""
    seite = app_mod.seite()
    assert ".popover .zeile.on" in seite


def test_die_seite_liest_die_neuen_antworten():
    """Three answers changed shape with 13.0 – storage, a started run, the
    inventory – and the page still read `ok`, `path` and `index`. Every one
    of them failed silently: a field that stays unfilled, a message that
    blanks out, a render that stops halfway."""
    _in_node(PRUEFUNG_ANTWORTEN_13)


def test_die_seite_meldet_ohne_alert():
    """A guard, not a taste: the browser dialog stops everything and looks
    like nothing else in the interface."""
    assert "alert(" not in app_mod.seite()


PRUEFUNG_MARKIERUNG = GRUNDZUSTAND + """
document.getElementById('q').value = 'Rechnung';
suchmodus('text');
pruefe(hervor('Die Rechnung liegt vor').indexOf('<mark>') >= 0,
       'Textsuche markiert die Fundstelle nicht');
// Bei der Bedeutungssuche waere eine Markierung eine Behauptung: dort passt
// der Sinn, nicht das Wort.
SUCHMODUS = 'aehnlich';
pruefe(hervor('Die Rechnung liegt vor').indexOf('<mark>') < 0,
       'Bedeutungssuche markiert woertlich');
console.log('OK');
"""


def test_markiert_wird_nur_wo_woertlich_getroffen_wurde():
    _in_node(PRUEFUNG_MARKIERUNG)


# --------------------------------------------------------------------------
# Ollama is optional – throughout the system
#
# Ollama used to be a silent prerequisite: absent, the app still polled it
# every ten seconds, the wizard pushed for the install, and the index run
# decided on its own. Now it is a decision.
# --------------------------------------------------------------------------
def test_abgeschaltet_wird_gar_nicht_erst_gefragt(sandbox, monkeypatch):
    """The actual win: without Ollama, a connection attempt into the void
    used to run every ten seconds, permanently."""
    gefragt = []
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda *a, **k: gefragt.append(1) or {})
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    for _ in range(3):
        zustand = a.ollama(force=True)
    assert gefragt == [], "es wurde trotzdem nach Ollama gesucht"
    assert zustand["disabled"] is True
    assert zustand["running"] is False and zustand["has_model"] is False


def test_abgeschaltet_kein_assistent(sandbox, with_ollama):
    """Whoever deselects Ollama does not want to be asked at every start
    whether they would like to install it after all."""
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    assert a.status()["wizard"] != "ollama"


def test_abgeschaltet_baut_den_volltextindex(sandbox, with_ollama, monkeypatch, no_ollama):
    """Even if Ollama were running: deselected is deselected."""
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    assert a.semantisch_gewollt() is False


def test_volltext_auch_mit_laufendem_ollama(sandbox, with_ollama):
    """Embedding costs an hour on a real corpus. Whoever only searches
    exactly should not have to pay it."""
    a = app_mod.App(app_mod.load_config())
    a.cfg["index_semantic"] = False
    assert a.semantisch_gewollt() is False
    a.cfg["index_semantic"] = True
    assert a.semantisch_gewollt() is True


def test_mcp_bekommt_den_verzicht_mitgeteilt(sandbox, monkeypatch):
    """Otherwise the server retries on every request and runs into the same
    error every time. With a profile the server reads the switch from that
    profile's settings itself (mcp_server._argumente), so the snippet
    carries nothing; the app's own launch and the --data-dir form say it."""
    cfg = app_mod.load_config()
    cfg["ollama_enabled"] = False
    args = app_mod.mcp_client_config(cfg, 8365)["stdio"]["mcpServers"]["munimentum"]["args"]
    assert "--no-ollama" not in args and "--profile" in args
    assert "--no-ollama" in app_mod._mcp_befehl(cfg)["argv"]
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(sandbox))
    args = app_mod.mcp_client_config(cfg, 8365)["stdio"]["mcpServers"]["munimentum"]["args"]
    assert "--no-ollama" in args
    cfg["ollama_enabled"] = True
    args = app_mod.mcp_client_config(cfg, 8365)["stdio"]["mcpServers"]["munimentum"]["args"]
    assert "--no-ollama" not in args


def test_schalter_werden_gespeichert(server):
    _, port = server
    code, r = call(port, "PATCH", "/api/v1/config",
                   {"ollama_enabled": False, "index_semantic": False})
    assert code == 200
    code, cfg = call(port, "GET", "/api/v1/config")
    assert cfg["config"]["ollama_enabled"] is False
    assert cfg["config"]["index_semantic"] is False
    s = call(port, "GET", "/api/v1/status")[1]
    assert s["ollama"]["disabled"] is True, "die Prüfung von vorher wirkt nach"


PRUEFUNG_OLLAMA_AUS = GRUNDZUSTAND + """
var st = statusGeruest();
st.ollama.disabled = true;
st.ollama.running = false; st.ollama.has_model = false; st.ollama.has_chat_model = false;
renderStatus(st);

// Abgeschaltet ist kein Fehler, sondern eine Entscheidung: grau statt rot.
var punkt = document.getElementById('p-ollama');
pruefe(!punkt.className.match(/err|warn/), 'Abgeschaltet wird als Fehler gezeigt: ' + punkt.className);
pruefe(document.getElementById('p-ollama-t').textContent.length > 0, 'Kachel ohne Text');

// Und die Suchvarianten nennen die Ursache, nicht nur die Bedingung.
pruefe(document.getElementById('m-ki').disabled, 'KI-Variante trotz Abschaltung waehlbar');
var hinweis = document.getElementById('modus-fehlt').textContent;
pruefe(hinweis.indexOf('Ollama') >= 0, 'Hinweis nennt Ollama nicht: ' + hinweis);
console.log('OK');
"""


def test_kopfzeile_zeigt_abgeschaltet_nicht_als_fehler():
    _in_node(PRUEFUNG_OLLAMA_AUS)


PRUEFUNG_EINSTELLUNGEN = GRUNDZUSTAND + """
// Der Schalter graut aus, was ohne ihn keine Wirkung hat - versteckt es aber
// nicht: wer die Moeglichkeit nie sieht, vermisst sie auch nie.
document.getElementById('c-ollama_enabled').checked = true;
indexart(true); ollamaSchalter();
pruefe(!document.getElementById('ollama-kinder').classList.contains('aus'),
       'Untergruppen ausgegraut, obwohl Ollama an ist');
pruefe(INDEX_SEMANTISCH === true, 'Indexart nicht gesetzt');

document.getElementById('c-ollama_enabled').checked = false;
ollamaSchalter();
pruefe(document.getElementById('ollama-kinder').classList.contains('aus'),
       'Untergruppen bleiben bedienbar, obwohl Ollama aus ist');
pruefe(document.getElementById('ix-beides').disabled, 'Bedeutung trotz Abschaltung waehlbar');
pruefe(INDEX_SEMANTISCH === false, 'Indexart nicht auf Volltext gefallen');
pruefe(document.getElementById('ollama-folgen').textContent.length > 20,
       'Es wird nicht gesagt, was das Abschalten bedeutet');
console.log('OK');
"""


def test_einstellungen_ollama_schalter():
    _in_node(PRUEFUNG_EINSTELLUNGEN)


def test_jede_einstellung_hat_eine_erklaerung():
    """The page now lives off the (i): a row without an explanation is a
    number nobody touches – or worse, changes blindly."""
    seite = app_mod.seite()
    abschnitt = seite[seite.index('<section id="tab-einstellungen"'):seite.index("</section>\n</main>")]
    # "feldzeile breit" is the textarea below its title row – the explanation
    # sits on the title, not on the input field.
    zeilen = abschnitt.count('class="feldzeile "')
    mit_info = abschnitt.count('class="info"')
    assert zeilen >= 25, f"nur {zeilen} Einstellungszeilen gefunden"
    assert mit_info >= zeilen, f"{zeilen} Zeilen, aber nur {mit_info} Erklärungen"


# --------------------------------------------------------------------------
# Evaluations for the analytics page
# --------------------------------------------------------------------------
def _index_mit_zeitpunkten(sandbox, monate):
    """A small store whose messages fall on specific months."""
    from datetime import UTC, datetime

    import corpus
    import rag_index
    chunks = []
    for i, (monat, quelle) in enumerate(monate):
        ts = datetime.strptime(monat + "-15", "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        c = {"uid": f"u:{i}", "cid": f"u:{i}#0", "src": quelle, "root": quelle,
             "rel": f"{i}.eml", "who": f"Person {i % 2}", "ppl": "p",
             "ts": ts, "date": monat, "title": f"T{i}", "ctx": "x",
             "text": "Inhalt", "att": "vertrag.pdf bild.png" if i % 3 == 0 else None}
        c["hash"] = corpus.chunk_hash(c)
        chunks.append(c)
    rag_index.write_db(sandbox / app_mod.STORE_DIR, chunks)
    return chunks


def _analytics(sandbox):
    import analytics_db
    return analytics_db.baue(sandbox / app_mod.STORE_DIR, {})


def test_verlauf_enthaelt_auch_die_leeren_monate(sandbox):
    """Otherwise a gap would not even show – it simply would not be there."""
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams"), ("2025-04", "outlook")])
    k = _analytics(sandbox)
    monate = [r["m"] for r in k["verlauf"]]
    assert monate == ["2025-01", "2025-02", "2025-03", "2025-04"]
    assert k["verlauf"][1]["gesamt"] == 0
    # Summed up – that is the growth curve.
    assert [r["summe"] for r in k["verlauf"]] == [1, 1, 1, 2]


def test_luecken_nur_innerhalb_des_bestands(sandbox):
    """Before the first and after the last message there is nothing to miss."""
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams"), ("2025-05", "teams")])
    k = _analytics(sandbox)
    assert k["luecken"] == [{"von": "2025-02", "bis": "2025-04", "monate": 3}]


def test_verlauf_zaehlt_nur_kommunikation(sandbox):
    """Mail and chat – calendar does not count here."""
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams"), ("2025-01", "outlook"),
                                     ("2025-01", "kalender")])
    zeile = _analytics(sandbox)["verlauf"][0]
    assert (zeile["teams"], zeile["outlook"]) == (1, 1)
    assert zeile["gesamt"] == 2, "Kalender darf den Stapel nicht tragen"


def test_dateien_verfaelschen_verlauf_und_luecken_nicht(sandbox):
    """The core flaw of the old page: a mirrored file carries its file
    modification date as timestamp and thus filled communication gaps – a
    PDF from 2025-03 made the mail-empty March look "full"."""
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams"), ("2025-05", "teams"),
                                     ("2025-03", "datei")])
    k = _analytics(sandbox)
    assert [r["gesamt"] for r in k["verlauf"]] == [1, 0, 0, 0, 1]
    assert k["luecken"] == [{"von": "2025-02", "bis": "2025-04", "monate": 3}]
    assert k["komm"]["nachrichten"] == 2
    assert k["dateien"]["n"] == 1


def test_anhangstypen_werden_gezaehlt(sandbox):
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "outlook")] * 3)
    typen = {x["typ"]: x["n"] for x in _analytics(sandbox)["anhang_typen"]}
    assert typen.get("pdf") == 1 and typen.get("png") == 1


def test_analytics_liest_nur_und_aktualisieren_baut_neu(sandbox, monkeypatch):
    """The tab must not cost seconds any more: /api/v1/analytics reads the
    materialized block; only "Refresh" (and the first call after an update)
    computes."""
    import analytics_db
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams")])
    cfg = app_mod.load_config()
    laeufe = []
    echt = analytics_db.baue
    monkeypatch.setattr(analytics_db, "baue",
                        lambda *a, **kw: laeufe.append(1) or echt(*a, **kw))
    app_mod.analytics_daten(cfg)           # block still missing: compute once
    app_mod.analytics_daten(cfg)           # now it only reads
    assert len(laeufe) == 1
    app_mod.analytics_daten(cfg, neu=True)
    assert len(laeufe) == 2


# --------------------------------------------------------------------------
# The contract between UI and form fields
#
# The settings page was once rewritten from scratch. That is exactly where
# the error no other test sees happens: one mistyped id, and a setting can
# silently no longer be filled or saved – noticed only when someone changes
# it and after the reload the value sits there again as before.
# --------------------------------------------------------------------------
def _feldlisten():
    """The three lists from which the UI reads form fields."""
    quelle = app_mod.seite()
    listen = {}
    for name in ("SCHALTER", "ZAHLEN", "TEXTE"):
        m = re.search(rf"var {name}\s*=\s*\[(.*?)\];", quelle, re.S)
        assert m, f"{name} nicht gefunden"
        listen[name] = re.findall(r"'([\w_]+)'", m.group(1))
    return listen


def test_jedes_gelistete_feld_gibt_es_auch(sandbox):
    """Every id in SCHALTER/ZAHLEN/TEXTE must have an element."""
    fehlt = [k for liste in _feldlisten().values() for k in liste
             if f'id="c-{k}"' not in app_mod.seite()]
    assert not fehlt, f"kein Bedienelement für: {fehlt}"


def test_jedes_feld_ist_auch_gelistet():
    """And the other way round: an element that is in no list is never
    saved – it looks operable and is not."""
    gelistet = {k for liste in _feldlisten().values() for k in liste}
    # Handled by hand, each for its own reason.
    ausnahmen = {"skip_folders",      # multi-line text, handled separately
                 "filetype_hidden",   # one-line list, handled separately
                 "analytics_skip",    # multi-line text, handled separately
                 "language",          # its own select, fuelleSprachen()
                 "notifications",     # its own select, saved by hand
                 "search_history",    # likewise: a select with fixed choices
                 "data-dir",          # sent to /api/v1/storage by the save
                 "index-dir",         # likewise
                 "ollama_enabled",    # toggle, see ollamaSchalter()
                 "index_kind",        # select, mirrors INDEX_SEMANTISCH via indexart()
                 "onedrive_enabled",  # lives in the "Export" tab, saveCats()
                 "sharepoint_enabled",   # likewise, saveCats()
                 "sharepoint_pages_enabled",  # likewise, saveCats()
                 "planner_enabled",   # likewise, saveCats()
                 "todo_enabled",      # likewise, saveCats()
                 "onenote_enabled",   # likewise, saveCats()
                 "sharepoint_urls",      # multi-line text, handled separately
                 "planner_urls",         # URL table, liesUrlTabelle()
                 "sharepoint_pages_urls",     # likewise
                 # cadence selects, leseKadenzen() – a whole source or one
                 # category of it
                 "cadence-onedrive", "cadence-todo",
                 "cadence-outlook-mail", "cadence-outlook-calendar",
                 "cadence-outlook-contacts",
                 "cadence-teams-1on1", "cadence-teams-group",
                 "cadence-teams-meeting", "cadence-teams-channels"}
    im_markup = set(re.findall(r'id="c-([\w_-]+)"', app_mod.seite()))
    verwaist = im_markup - gelistet - ausnahmen
    assert not verwaist, f"Bedienelemente, die niemand speichert: {sorted(verwaist)}"


def test_jedes_gelistete_feld_wird_auch_serverseitig_angenommen(sandbox, server):
    """The path does not end in the browser: what the UI sends, the
    configuration must also accept."""
    _, port = server
    listen = _feldlisten()
    body = {}
    for k in listen["SCHALTER"]:
        body[k] = False
    for k in listen["ZAHLEN"]:
        body[k] = 7 if k != "mcp_port" else 8400
    code, _ = call(port, "PATCH", "/api/v1/config", body)
    assert code == 200
    cfg = call(port, "GET", "/api/v1/config")[1]["config"]
    nicht_uebernommen = [k for k in listen["SCHALTER"] if cfg.get(k) is not False]
    assert not nicht_uebernommen, f"Schalter ignoriert: {nicht_uebernommen}"
    nicht_uebernommen = [k for k in listen["ZAHLEN"]
                         if cfg.get(k) not in (7, 8400)]
    assert not nicht_uebernommen, f"Zahlen ignoriert: {nicht_uebernommen}"


# --------------------------------------------------------------------------
# The Ollama switch, all the way through
# --------------------------------------------------------------------------
def test_indexschritt_bekommt_ohne_ollama_den_volltextschalter(sandbox, with_ollama,
                                                               monkeypatch):
    """Intent alone does not count – the subprocess must carry the switch.
    Ollama IS running in this test; deselected is deselected."""
    monkeypatch.setattr(app_mod, "read_token", lambda: make_jwt(exp=time.time() + 3600, scp='Mail.Read User.Read'))
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    schritte = app_mod.build_steps(a.cfg, {"index": True}, embeddings=a.semantisch_gewollt())
    index = [s for s in schritte if s["key"] == "index"][0]
    assert "--no-embeddings" in index["argv"]


def test_indexschritt_mit_ollama_bettet_ein(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    schritte = app_mod.build_steps(a.cfg, {"index": True}, embeddings=a.semantisch_gewollt())
    index = [s for s in schritte if s["key"] == "index"][0]
    assert "--no-embeddings" not in index["argv"]


def test_zeitplan_laeuft_auch_ohne_ollama(sandbox, with_ollama, monkeypatch):
    """A nightly run should build the full-text index instead of failing."""
    monkeypatch.setattr(app_mod, "read_token", lambda: make_jwt(exp=time.time() + 3600, scp='Mail.Read User.Read'))
    gestartet = {}
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    a.cfg["schedule"].update(enabled=True, interval_minutes=5, index=True,
                             outlook=False, teams=False, calendar=False)
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gestartet.update(steps=steps) or True)
    a.scheduler.letzter = 0
    a.scheduler._tick()
    index = [s for s in gestartet.get("steps", []) if s["key"] == "index"]
    assert index, "der Zeitplan hat gar nicht indiziert"
    assert "--no-embeddings" in index[0]["argv"]


PRUEFUNG_AUSGEGRAUT = GRUNDZUSTAND + """
// Ausgegraut, nicht versteckt: wer die Moeglichkeit nie sieht, erfaehrt auch
// nie, dass es sie gibt - und sucht sie beim naechsten Mal woanders.
document.getElementById('c-ollama_enabled').checked = false;
ollamaSchalter();
['ollama-kinder', 'ix-beides', 'c-embed_model', 'c-chat_model'].forEach(function(id){
  var e = document.getElementById(id);
  pruefe(!e.classList.contains('hide'), id + ' wurde versteckt statt ausgegraut');
});
pruefe(document.getElementById('ollama-kinder').classList.contains('aus'),
       'nicht ausgegraut');

// Und wieder an: alles zurueck, ohne Neuladen.
document.getElementById('c-ollama_enabled').checked = true;
ollamaSchalter();
pruefe(!document.getElementById('ollama-kinder').classList.contains('aus'),
       'bleibt ausgegraut, obwohl Ollama wieder an ist');
pruefe(!document.getElementById('ix-beides').disabled, 'Bedeutung bleibt gesperrt');
console.log('OK');
"""


def test_ausgegraut_statt_versteckt():
    _in_node(PRUEFUNG_AUSGEGRAUT)


PRUEFUNG_KACHEL_ZIEL = GRUNDZUSTAND + """
// Die Kachel fuehrt immer dorthin, wo etwas zu aendern ist. Vorher oeffnete
// sie ein Fenster, das erklaerte, was fehlt - aendern liess sich dort nichts.
var gewechselt = [];
tab = function(n){ gewechselt.push(n); };
S = statusGeruest();
[true, false].forEach(function(aus){
  gewechselt = []; S.ollama.disabled = aus;
  ollamaKachel();
  pruefe(gewechselt.indexOf('einstellungen') >= 0,
         'Kachel fuehrt nicht zu den Einstellungen: ' + gewechselt.join(','));
  pruefe(!wizardOffen, 'Assistent ging trotzdem auf');
});


// Und die Kachel selbst sagt nur an oder aus - warum, steht im Mouseover und
// neben dem Feld, in dem man es richtet.
function lage(o){
  var st = statusGeruest();
  st.ollama = o;
  renderStatus(st);
  return {text: document.getElementById('p-ollama-t').textContent,
          tip: document.getElementById('pill-ollama').title || '',
          // innerHTML, nicht textContent: die DOM-Attrappe zerlegt gesetztes
          // Markup nicht in Kindknoten.
          adresse: document.getElementById('st-ollama').innerHTML,
          modell: document.getElementById('st-embed_model').innerHTML};
}

var an = lage({running: true, has_model: true, has_chat_model: true, models: []});
pruefe(an.text === 'an', 'Laufend nicht als an bezeichnet: ' + an.text);
pruefe(an.adresse.length > 0, 'Kein Stand neben der Adresse');
pruefe(an.modell.length > 0, 'Kein Stand neben dem Modell');

var fehlt = lage({running: true, has_model: false, has_chat_model: false, models: []});
pruefe(fehlt.text === 'aus', 'Fehlendes Modell nicht als aus bezeichnet: ' + fehlt.text);
pruefe(fehlt.tip.indexOf('Modell') >= 0, 'Mouseover nennt den Grund nicht: ' + fehlt.tip);
pruefe(fehlt.adresse.length > 0, 'Adresse ist erreichbar, sagt es aber nicht');

var weg = lage({running: false, has_model: false, has_chat_model: false, models: []});
pruefe(weg.text === 'aus', 'Nicht erreichbar, aber nicht als aus bezeichnet');
// Ohne erreichbares Ollama ist "Modell fehlt" eine zweite Meldung ueber
// dieselbe Ursache - dann steht dort nichts.
pruefe(weg.modell === '', 'Zweite Meldung ueber dieselbe Ursache: ' + weg.modell);
console.log('OK');
"""


def test_ollama_kachel_fuehrt_ans_richtige_ziel():
    _in_node(PRUEFUNG_KACHEL_ZIEL)


def test_binden_loest_keine_namen_auf(sandbox, monkeypatch):
    """Reported: macOS asked at startup whether the app may search the local
    network.

    http.server does a reverse lookup for its own address when binding
    (`socket.getfqdn`); the result lands in `server_name` and is needed
    nowhere. The app listens on 127.0.0.1 – it has no business on the
    network and should not ask about it either.
    """
    def verboten(*a, **kw):
        raise AssertionError("Namensauflösung beim Binden")

    import socket
    monkeypatch.setattr(socket, "getfqdn", verboten)
    httpd = app_mod.make_server(app_mod.App(app_mod.load_config()), 0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
        assert httpd.server_name == "127.0.0.1"      # instead of a resolved name
    finally:
        httpd.server_close()


def test_insights_hat_eine_seitennavigation_je_karte():
    """DESIGN.md §2: Insights is shaped like Settings – one `.snav` entry per
    card, each pointing at a card that exists, the two checks with a dot."""
    seite = app_mod.seite()
    block = seite[seite.index('<section id="tab-analytics"'):seite.index('<section id="tab-einstellungen"')]
    assert 'id="ana-nav"' in block and 'class="einst"' in block
    ziele = re.findall(r'data-ziel="([^"]+)"', block[:block.index('class="einst-inhalt"')])
    assert ziele == ["ana-zahlen-karte", "ana-verlauf-karte", "ana-check-karte",
                     "ana-archiv-karte", "ana-runs-karte"]
    for ziel in ziele:
        assert f'id="{ziel}"' in block, f"Sprungziel {ziel} fehlt"
        assert f"zeigeInsight('{ziel}')" in block
    assert 'id="p-ana-check"' in block and 'id="p-ana-archiv"' in block


def test_kacheln_springen_an_eine_stelle_die_es_gibt():
    """Without the id in the markup zeigeEinstellung finds nothing and stays
    at the top of the settings – reported for the AI tile."""
    for ziel in ("ki-karte", "mcp-karte"):
        assert f'id="{ziel}"' in app_mod.seite(), f"Sprungziel {ziel} fehlt"
        assert f"zeigeEinstellung('{ziel}')" in app_mod.seite(), f"{ziel} wird nicht angesprungen"


PRUEFUNG_RUNDREISE = GRUNDZUSTAND + """
// Eine Konfiguration hineingeben und wieder herausholen: was die Oberfléche
// nicht zurueckgibt, kann der Nutzer nicht speichern.
var cfg = Object.assign({}, KONFIG);
cfg.ollama_enabled = true; cfg.index_semantic = false;
cfg.workers = 6; cfg.embed_model = 'bge-m3'; cfg.chat_model = 'qwen3.5:9b';
cfg.ollama = 'http://x:1'; cfg.search_results = 25; cfg.semantic_min = 55;
cfg.answer_sources = 3; cfg.index_batch = 32; cfg.mcp_port = 8400;
cfgGefuellt = false;
fuelleEinstellungen(cfg);

// Der DOM-Stummel wandelt beim Zuweisen nicht in Text um, der Browser schon.
pruefe(String(document.getElementById('c-workers').value) === '6', 'Zahl nicht gefuellt');
pruefe(document.getElementById('c-embed_model').value === 'bge-m3', 'Text nicht gefuellt');
pruefe(document.getElementById('c-ollama_enabled').checked === true, 'Schalter nicht gefuellt');
pruefe(INDEX_SEMANTISCH === false, 'Indexart nicht uebernommen: ' + INDEX_SEMANTISCH);
pruefe(document.getElementById('c-index_kind').value === 'text',
       'Auswahl zeigt die falsche Indexart');

// Und zurueck: speichern muss jeden Wert mitschicken – als PATCH auf die
// Einstellungen, die seit 12.0 ihre eigene Ressource sind.
var geschickt = null, gepatcht = '';
patch = function(pfad, body){ gepatcht = pfad; geschickt = body;
                              return Promise.resolve({config: body}); };
S = statusGeruest();
speichereEinstellungen();
['workers','embed_model','ollama_enabled','index_semantic','mcp_port','semantic_min']
  .forEach(function(k){
    pruefe(geschickt[k] !== undefined, 'nicht mitgeschickt: ' + k);
  });
pruefe(geschickt.index_semantic === false, 'Indexart falsch gespeichert');
pruefe(gepatcht === '/api/v1/config', 'Einstellungen gehen woanders hin: ' + gepatcht);
console.log('OK');
"""


def test_einstellungen_hin_und_zurueck():
    _in_node(PRUEFUNG_RUNDREISE)


PRUEFUNG_AEHNLICHE_GESPERRT = GRUNDZUSTAND + """
function zeichne(semantisch){
  S = statusGeruest();
  BESTAND = Object.assign({}, BESTAND, {store: Object.assign({}, BESTAND.store, {semantic: semantisch})});
  S = Object.assign({}, BESTAND, S);
  renderHits({count: 1, results: [{uid: 'u:1', cid: 7, title: 'T', who: 'A',
    date: '2026-03-04', source_label: 'Datei', preview: 'p'}]});
  waehleTreffer(0);
  return document.getElementById('detail-inhalt').innerHTML;
}

// Mit Vektoren im Index ist der Eintrag bedienbar - auch wenn Ollama gerade
// abgeschaltet ist: eingebettet wird dabei nichts, der Vektor liegt schon da.
var mit = zeichne(true);
pruefe(mit.indexOf('aehnlicheZu(') >= 0, 'Aehnliche finden fehlt trotz Vektoren');

// Ohne Vektoren liefe der Aufruf ins Leere. Ausgegraut statt verschwunden -
// sonst sucht man den Eintrag beim naechsten Mal an anderer Stelle.
var ohne = zeichne(false);
var menue = ohne.split('class="daktionen"')[1].split('</div>')[0];
pruefe(menue.indexOf('aehnlicheZu(') < 0, 'Aehnliche finden ist noch anklickbar');
pruefe(menue.indexOf('Find similar') >= 0 || menue.indexOf('hnliche finden') >= 0,
       'Der Eintrag verschwand ganz statt auszugrauen');
pruefe(menue.indexOf('disabled') >= 0, 'nicht gesperrt');
pruefe(menue.indexOf('title=') >= 0, 'kein Grund genannt');
console.log('OK');
"""


def test_aehnliche_finden_haengt_an_den_vektoren():
    """Not on Ollama: the chunk's vector sits in the index. Without vectors
    – a pure full-text index – the entry would lead nowhere."""
    _in_node(PRUEFUNG_AEHNLICHE_GESPERRT)


def test_links_umleiten_schickt_nur_relative_pfade_durch_die_route():
    """Every archive HTML links its files relatively; served through /source
    those links must come back through the route – absolute ones, anchors
    and embedded images untouched, ".." resolved inside the same root."""
    roh = (b'<a href="Anhaenge/Bon__1.pdf">x</a>'
           b'<a href="../Dateien/Ordner/a.pdf#s">y</a>'
           b'<img src="data:image/png;base64,AAAA">'
           b'<a href="https://example.com/x">z</a>'
           b'<a href="#oben">o</a><a href="/api/v1/files/content?root=teams&path=q">q</a>'
           b'<a href="Besprechung__a.files/Protokoll%20A.pdf">p</a>')
    neu = app_mod._links_umleiten(roh, "todo", "Einkauf__x/list.html")
    assert b'href="/api/v1/files/content?root=todo&path=Einkauf__x%2FAnhaenge%2FBon__1.pdf"' in neu
    assert b'href="/api/v1/files/content?root=todo&path=Dateien%2FOrdner%2Fa.pdf#s"' in neu
    assert b'src="data:image/png;base64,AAAA"' in neu
    assert b'href="https://example.com/x"' in neu and b'href="#oben"' in neu
    assert b'href="/api/v1/files/content?root=teams&path=q"' in neu
    assert b'path=Einkauf__x%2FBesprechung__a.files%2FProtokoll%20A.pdf"' in neu
    # A page at the root of its export: no folder to prepend.
    assert b'path=Anhaenge%2FBon__1.pdf' in app_mod._links_umleiten(
        b'<a href="Anhaenge/Bon__1.pdf">', "planner", "board.html")


# --------------------------------------------------------------------------
# OneNote notebooks: the same mechanics as the mailbox folders
# --------------------------------------------------------------------------
def _notizbuchliste(sandbox, eintraege, seiten=()):
    ordner = sandbox / app_mod.ONENOTE_DIR
    folders_mod.speichere(ordner, eintraege, datei=folders_mod.NOTIZBUECHER)
    for pfad, anzahl in seiten:
        (ordner / pfad).mkdir(parents=True, exist_ok=True)
        for i in range(anzahl):
            (ordner / pfad / f"s{i}__ab.html").write_text("x", encoding="utf-8")
    return ordner


NOTIZBUECHER = [{"id": "n1", "pfad": "Projekte", "name": "Projekte", "standard": False,
                 "elemente": 0},
                {"id": "n2", "pfad": "Privat", "name": "Privat", "standard": True,
                 "elemente": 0}]


def test_notizbuchregeln_ohne_eintrag_alle(sandbox):
    daten = {"ordner": NOTIZBUECHER}
    assert len(folders_mod.gewaehlt(daten, app_mod.notizbuchregeln({"onenote_rules": ""}))) == 2
    eigene = app_mod.notizbuchregeln({"onenote_rules": "- **\n+ Projekte"})
    assert [e["name"] for e in folders_mod.gewaehlt(daten, eigene)] == ["Projekte"]


def test_notizbuchliste_zaehlt_seiten_je_notizbuch(server, sandbox):
    """Pages sit in section folders below the notebook – the export list
    sums them per notebook, and a notebook only on disk shows as such."""
    a, port = server
    _notizbuchliste(sandbox, NOTIZBUECHER,
                    [("Projekte/Allgemein", 2), ("Projekte/2026/Q3", 3),
                     ("Privat/Ideen", 1), ("Weg/Alt", 4)])
    code, r = call(port, "QUERY", "/api/v1/sources/onenote/folder-plan",
                   {"onenote_rules": "- **\n+ Projekte"})
    assert code == 200 and r["regeln"]
    assert [z["pfad"] for z in r["an"]] == ["Projekte"] and r["an"][0]["archiv"] == 5
    assert [z["pfad"] for z in r["aus"]] == ["Privat"] and r["aus"][0]["archiv"] == 1
    assert r["aus"][0]["regel"] == "- **"
    assert r["weg"] == [{"pfad": "Weg", "archiv": 4}]
    assert not a.cfg["onenote_rules"]


def test_notizbuchstand_nennt_die_eintraege_mit_auswahl(server, sandbox):
    a, port = server
    _notizbuchliste(sandbox, NOTIZBUECHER)
    call(port, "PATCH", "/api/v1/config", {"onenote_rules": "- **\n+ Privat"})
    assert a.cfg["onenote_rules"] == "- **\n+ Privat"
    c = call(port, "GET", "/api/v1/inventory")[1]["notebooks"]
    assert (c["gesamt"], c["gewaehlt"], c["namen"]) == (2, 1, ["Privat"])
    assert c["abgeglichen"]
    assert [(e["id"], e["an"]) for e in c["eintraege"]] == [("n1", False), ("n2", True)]


def test_notizbuchstand_ohne_liste(server):
    c = call(server[1], "GET", "/api/v1/inventory")[1]["notebooks"]
    assert c == {"abgeglichen": None, "gesamt": 0, "gewaehlt": 0, "namen": [],
                 "neu": [], "eintraege": []}


def test_notizbuchregeln_und_kadenz_erreichen_den_export(sandbox):
    cfg = dict(app_mod.load_config(), onenote_rules="+ Projekte",
               sync_cadence={"onenote:n1": "weekly"})
    schritt = [s for s in app_mod.build_steps(cfg, {"onenote": True}) if s["key"] == "onenote"][0]
    assert schritt["env"]["ONENOTE_RULES"] == "+ Projekte"
    assert json.loads(schritt["env"]["SYNC_CADENCE"]) == {"onenote:n1": "weekly"}
    assert "ONENOTE_ONLY" not in schritt["env"]
    einzeln = [s for s in app_mod.build_steps(cfg, {"onenote": True}, nur_einheit="n1")
               if s["key"] == "onenote"][0]
    assert einzeln["env"]["ONENOTE_ONLY"] == "n1" and einzeln["env"]["SYNC_NOW"] == "1"
    liste = [s for s in app_mod.build_steps(cfg, {"sync_notebooks": True})]
    assert [s["key"] for s in liste] == ["notebooks"]
    assert liste[0]["argv"][-2:] == ["--notebooks", app_mod.ONENOTE_DIR]
    assert liste[0]["env"]["ONENOTE_RULES"] == "+ Projekte"


# The run window: opens with the run, stays until the run is done, is
# closed by hand; minimised it becomes a pill in the header.
PRUEFUNG_LAUFFENSTER = GRUNDZUSTAND + """
function laeuft(i){
  var st = statusGeruest();
  st.jobs = {busy: true, seq: 1, token_expired: false, last: null,
             job: {label: 'Export', steps: ['job.step.outlook', 'job.step.index'],
                   step: 'job.step.outlook', index: i, progress: {done: 50, total: 100, what: 'mails'},
                   started: '2026-09-10T15:07:00'}};
  return st;
}
function offen(){ return !el('lauf-overlay').classList.contains('hide'); }
function pille(){ return !el('lauf-pille').classList.contains('hide'); }

// Opening the page while a run is on: straight into the window.
var erster = laeuft(0);
S = null;
renderStatus(erster);
pruefe(offen() && !pille(), 'Beim Oeffnen der Seite nicht im Fenster');
pruefe(el('lauf-sub').textContent.indexOf('1') >= 0, 'Schritt fehlt in der Unterzeile');
pruefe(!el('btn-cancel').classList.contains('hide'), 'Abbrechen fehlt waehrend des Laufs');
pruefe(el('lauf-fertig').classList.contains('hide'), 'Schliessen steht schon waehrend des Laufs da');

// Minimised: the pill in the header, the window gone – and the status poll
// must not reopen it.
laufMinimieren();
renderStatus(laeuft(1));
pruefe(!offen() && pille(), 'Minimiert, aber Fenster wieder offen oder Pille fehlt');
pruefe(el('lauf-pille-t').textContent.indexOf('2/2') >= 0,
       'Pille nennt den Schritt nicht: ' + el('lauf-pille-t').textContent);
laufOeffnen();
pruefe(offen() && !pille(), 'Klick auf die Pille oeffnet nicht');

// Done: the window stays, shows the result, and only "Close" removes it.
var fertig = statusGeruest();
fertig.jobs = {busy: false, seq: 2, token_expired: false,
               job: null, last: {label: 'Export', ok: true, detail: '', finished: '2026-09-10T15:11:00'}};
renderStatus(fertig);
pruefe(offen(), 'Fenster verschwand von selbst');
pruefe(!el('lauf-fertig').classList.contains('hide'), 'Schliessen fehlt am Ende');
pruefe(el('btn-cancel').classList.contains('hide'), 'Abbrechen nach dem Lauf');
renderStatus(fertig);
pruefe(offen(), 'Statusabruf schloss das Fenster');
laufSchliessen();
pruefe(!offen() && !pille(), 'Schliessen raeumt nicht auf');
renderStatus(fertig);
pruefe(!offen(), 'Ein alter Lauf oeffnet das Fenster erneut');

// A scheduled run that starts while someone works here: the pill first,
// no window over the search.
renderStatus(laeuft(0));
pruefe(!offen() && pille(), 'Zeitplan-Lauf springt ueber die Seite');
laufSchliessen();

// A run started here opens the window with it.
LAUF.eigener = true;
renderStatus(laeuft(0));
pruefe(offen(), 'Eigener Lauf oeffnet das Fenster nicht');

// Only this run's lines: what the app logged before the run stays out.
laufSchliessen();
var mitCursor = laeuft(0); mitCursor.jobs.job.log_seq = 7;
var vorher = null;
global.fetch = function(pfad){
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/v1/log') >= 0
      ? {seq: 9, items: [{n: 7, level: 'info', t: '1', text: 'alt'},
                          {n: 8, level: 'head', t: '2', text: 'neu'},
                          {n: 9, level: 'info', t: '3', text: 'neu'}]}
      : statusGeruest()); }});
};
var angehaengt = [];
el('log').appendChild = function(d){ angehaengt.push(d.textContent); };
LAUF.eigener = true;
renderStatus(mitCursor);
pruefe(LAUF.abSeq === 7, 'Logstand des Laufs nicht uebernommen: ' + LAUF.abSeq);
seen = 0;
pullLog();
setTimeout(function(){
  pruefe(angehaengt.length === 2 && angehaengt.every(function(z){ return z.indexOf('neu') >= 0; }),
         'Alte Zeilen im Lauf-Fenster: ' + JSON.stringify(angehaengt));
  console.log('OK');
}, 20);
"""


def test_lauffenster_bleibt_bis_zum_schliessen():
    _in_node(PRUEFUNG_LAUFFENSTER)


# --------------------------------------------------------------------------
# The cadence window: the tree and the inheritance rule, pure functions
# --------------------------------------------------------------------------
PRUEFUNG_KADENZ = r"""
var eintraege = [
  {pfad: 'E-Mail/Posteingang', elemente: 100, an: true},
  {pfad: 'E-Mail/Posteingang/Kunden/Nordwind', elemente: 40, an: true},
  {pfad: 'E-Mail/Archiv', elemente: 900, an: false},
  {pfad: 'E-Mail/Archiv/2025', elemente: 300, an: false}];
var baum = kadenzKnoten(eintraege, null);
var wurzeln = baum.wurzeln.map(function(k){ return k.pfad; });
if(wurzeln.join() !== 'E-Mail') throw new Error('Wurzel: ' + wurzeln.join());
// The list never named "E-Mail/Posteingang/Kunden": made up so Nordwind hangs somewhere.
var kunden = baum.knoten['E-Mail/Posteingang/Kunden'];
if(!kunden || kunden.echt || kunden.kinder.length !== 1) throw new Error('Zwischenordner fehlt');
if(baum.knoten['E-Mail/Archiv'].an !== false) throw new Error('ausgelassen nicht markiert');
if(baum.knoten['E-Mail'].kinder.length !== 2) throw new Error('Kinder der Wurzel');
// Inheritance: the deepest departure on the path wins, else the general one.
var abw = {'E-Mail/Archiv': 'monthly', 'E-Mail/Archiv/2025': 'always'};
if(kadenzWirksam('E-Mail/Posteingang', abw, 'daily').wert !== 'daily') throw new Error('allgemein');
if(kadenzWirksam('E-Mail/Archiv/2024', abw, 'daily').von !== 'E-Mail/Archiv') throw new Error('erbt');
if(kadenzWirksam('E-Mail/Archiv/2025/Q1', abw, 'daily').wert !== 'always') throw new Error('tiefer gewinnt');
if(kadenzWirksam('E-Mail/Archivar', abw, 'daily').wert !== 'daily') throw new Error('Namenspräfix zählt nicht');
// Teams: the four kinds are the roots even when the list is empty.
var teams = kadenzKnoten([{pfad: 'channels/Nordwind/Releases', an: true, zuletzt: '2026-09-10T10:00:00Z'}],
                         ['1on1', 'group', 'meeting', 'channels']);
if(teams.wurzeln.length !== 4) throw new Error('vier Wurzeln');
if(teams.knoten['channels/Nordwind'].kinder[0].zuletzt !== '2026-09-10T10:00:00Z') throw new Error('zuletzt');
console.log('OK');
"""


def test_kadenzfenster_baum_und_vererbung():
    """The window's tree is built from the export list; parents the list
    does not name are made up, and the cadence of a unit is the deepest
    departure on its path – the rule the exports apply too."""
    _in_node(PRUEFUNG_KADENZ)


# --------------------------------------------------------------------------
# The tour: every step's element exists, and the chapters run through
# --------------------------------------------------------------------------
def test_rundgang_ziele_existieren():
    """A coach mark pointing at nothing would dim the page and explain the
    void. Every step's element (the id part of its selector) is in the
    markup; a step without an element is the centred card by design."""
    seite = app_mod.seite()
    block = seite[seite.index("var TOUR = {"):seite.index("var TOURSTAND")]
    ziele = re.findall(r"ziel: '#([\w-]+)", block)
    assert len(ziele) >= 19, ziele
    vorhanden = set(re.findall(r'id="([\w-]+)"', seite))
    fehlt = sorted(set(ziele) - vorhanden)
    assert not fehlt, f"Rundgang zeigt ins Leere: {fehlt}"
    # Every step names its texts as literals, so the i18n test sees them.
    for k in re.findall(r"(?:titel|text): '([\w.]+)'", block):
        assert k.startswith("tour."), k


PRUEFUNG_TOUR = GRUNDZUSTAND + """
var gesendet = [];
var altFetch = global.fetch;
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body,
                 methode: (opt && opt.method) || 'GET'});
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/v1/status') >= 0 ? statusGeruest() : {ok: true}); }});
};
S.config = S.config || {}; S.config.tour_seen = {};
S.store = S.store || {exists: true};
// The source chapter: nine steps, the last one without an element.
tourStart('quelle');
pruefe(TOURSTAND.kap === 'quelle' && TOURSTAND.i === 0, 'Kapitel nicht gestartet');
pruefe(!el('tour').classList.contains('hide'), 'Schicht nicht sichtbar');
for(var i = 0; i < 8; i++) tourWeiter();
pruefe(TOURSTAND.i === 8, 'nicht beim letzten Schritt: ' + TOURSTAND.i);
pruefe(el('tour').classList.contains('frei'), 'letzter Schritt ohne Element muss die Karte frei stellen');
tourZurueck();
pruefe(TOURSTAND.i === 7, 'Zurück');
tourWeiter(); tourWeiter();
pruefe(TOURSTAND.kap === null && el('tour').classList.contains('hide'), 'Kapitel nicht beendet');
var speicherung = gesendet.filter(function(g){ return g.pfad.indexOf('/api/v1/config') >= 0; }).pop();
pruefe(speicherung && JSON.parse(speicherung.body).tour_seen.quelle === true, 'Kapitel nicht als gesehen gemerkt');
pruefe(S.config.tour_seen.quelle === true, 'Stand nicht übernommen');
// Skipping marks the chapter seen as well – it never comes back on its own.
tourStart('archiv');
tourEnde(false);
pruefe(S.config.tour_seen.archiv === true, 'Überspringen gilt als gesehen');
// The search chapter before the first run: one card, nothing marked seen.
S.store = {exists: false};
tourStart('suche');
pruefe(TOURSTAND.kap === 'suche' && !el('tour').classList.contains('hide'), 'Hinweis vor dem ersten Lauf');
tourEnde(false);
pruefe(!S.config.tour_seen.suche, 'ohne Index darf die Suche nicht als gesehen gelten');
// The case chapter: fourteen steps from the search into the case, the
// searches that work for it, and on to the settings; without a case it
// still runs through, cards centred.
S.store = {exists: true};
tourStart('faelle');
pruefe(TOURSTAND.kap === 'faelle' && TOUR.faelle.length === 14, 'Fallkapitel nicht gestartet');
pruefe(el('tour-karte').innerHTML.indexOf('Mit Fällen arbeiten') >= 0, 'Kapitelname fehlt auf der Karte');
// The chip step is optional – skipped where nothing was collected yet
for(var j = 0; j < 13 && TOURSTAND.i < 13; j++) tourWeiter();
pruefe(TOURSTAND.i === 13, 'nicht beim letzten Schritt des Fallkapitels: ' + TOURSTAND.i);
tourWeiter();
pruefe(TOURSTAND.kap === null && S.config.tour_seen.faelle === true, 'Fallkapitel nicht beendet oder nicht gemerkt');
// Insights and Claude: five and four steps, all with an element.
tourStart('insights');
pruefe(TOURSTAND.kap === 'insights' && TOUR.insights.length === 5, 'Insights-Kapitel nicht gestartet');
for(var k = 0; k < 5; k++) tourWeiter();
pruefe(TOURSTAND.kap === null && S.config.tour_seen.insights === true, 'Insights-Kapitel nicht beendet');
tourStart('claude');
pruefe(TOURSTAND.kap === 'claude' && TOUR.claude.length === 4, 'Claude-Kapitel nicht gestartet');
for(var c = 0; c < 4; c++) tourWeiter();
pruefe(TOURSTAND.kap === null && S.config.tour_seen.claude === true, 'Claude-Kapitel nicht beendet');
// The source chapter names the full sync now, one step before the save.
pruefe(TOUR.quelle[TOUR.quelle.length - 2].titel === 'tour.quelle.vollsync', 'Vollsync-Schritt fehlt im Quellenkapitel');
// The help window: one row per chapter, the first unseen one marked next, the full tour below.
S.config.tour_seen = {archiv: true};
hilfeFenster();
var hilfe = modal.innerHTML;
pruefe(hilfe.split('class="hilfe-kapitel').length - 1 === 6, 'nicht sechs Kapitel im Hilfefenster');
pruefe(hilfe.indexOf("tourStart('insights')") >= 0 && hilfe.indexOf('tourAlle()') >= 0, 'Kapitel oder ganzer Rundgang fehlen');
pruefe(hilfe.indexOf('hilfe-kapitel gesehen') >= 0 && hilfe.indexOf('gesehen') >= 0, 'gesehenes Kapitel nicht markiert');
pruefe(hilfe.split('stand naechst').length - 1 === 1 && hilfe.indexOf('als Nächstes') > hilfe.indexOf("tourStart('quelle')"), 'das erste ungesehene Kapitel ist nicht das naechste');
pruefe(hilfe.split('class="act"').length - 1 === 1 && hilfe.indexOf('6 Kapitel') >= 0, 'ein primaerer Knopf mit der Summe fehlt');
closeWizard('hilfe');
// The full tour: chapter after chapter in order, the card says which part; skipping ends it all.
S.store = {exists: false};
tourAlle();
pruefe(TOURSTAND.kap === 'archiv' && TOURSTAND.kette && TOURSTAND.kette.n === 6, 'ganzer Rundgang startet nicht beim Archiv');
pruefe(el('tour-karte').innerHTML.indexOf('Ganzer Rundgang 1 von 6') >= 0, 'Karte nennt den Teil nicht');
for(var a = 0; a < 6; a++) tourWeiter();
pruefe(TOURSTAND.kap === 'quelle' && TOURSTAND.kette.nr === 2, 'geht nicht ins zweite Kapitel weiter: ' + TOURSTAND.kap);
for(var q = 0; q < 9; q++) tourWeiter();
pruefe(TOURSTAND.kap === 'faelle' && TOURSTAND.kette.nr === 4, 'ohne Index muss die Suche uebersprungen werden: ' + TOURSTAND.kap);
tourEnde(false);
pruefe(TOURSTAND.kap === null, 'Ueberspringen beendet den ganzen Rundgang nicht');
console.log('OK');
"""


def test_rundgang_kapitel_laufen_durch():
    """Next, back, done and skip: the chapter ends, is marked seen once,
    and the search chapter waits for an index."""
    _in_node(PRUEFUNG_TOUR)


# --------------------------------------------------------------------------
# The result in three views (13.6): list, timeline, people
# --------------------------------------------------------------------------
PRUEFUNG_ERGEBNIS_SICHTEN = GRUNDZUSTAND + """
process.on('unhandledRejection', function(e){ console.error('REJECTION', e); process.exit(1); });
var HITS = [
  {uid: 'u1', key: 'k1', cid: 1, source: 'outlook', root: 'outlook', path: 'a.eml', who: 'Carla Chef', who_mail: 'carla@example.com',
   date: '2026-06-10 08:00', title: 'Rechnung 4711', preview: 'x', cases: []},
  {uid: 'u2', key: 'k2', cid: 2, source: 'teams', root: 'teams', path: 'b.html', who: 'Bob Baumeister', who_mail: null,
   date: '2026-06-01 09:35', title: 'Projekt Alpha', preview: 'y', cases: []}
];
// The whole result: older than the page, and one item without a date.
var GANZ = [
  {uid: 'u3', key: 'k3', cid: 3, source: 'teams', root: 'teams', path: 'c.html', who: 'Bob Baumeister', who_mail: null,
   date: '2025-06-01 09:35', title: 'Alte Nachricht', preview: '', cases: []},
  HITS[1], HITS[0],
  {uid: 'u4', key: 'k4', cid: 4, source: 'kontakte', root: 'outlook', path: 'd.vcf', who: '', who_mail: null,
   date: '', title: 'Ohne Datum', preview: '', cases: []}
];
var LEUTE = [
  {name: 'Carla Chef', address: 'carla@example.com', items: 1, by_source: {outlook: 1}, first: '2026-06-10 08:00', last: '2026-06-10 08:00'},
  {name: 'Bob Baumeister', address: '', items: 2, by_source: {teams: 2}, first: '2025-06-01 09:35', last: '2026-06-01 09:35'},
  // The account of GRUNDZUSTAND: that is me, and I am not a node.
  {name: 'A B', address: 'a@example.com', items: 1, by_source: {outlook: 1}, first: '2026-01-01 08:00', last: '2026-01-01 08:00'}
];
var gefragt = [];
function kopie(liste){ return liste.map(function(h){ return Object.assign({}, h); }); }
global.fetch = function(pfad){
  pfad = String(pfad); gefragt.push(pfad);
  var antwort;
  if(pfad.indexOf('/api/v1/search/timeline?') >= 0) antwort = {items: kopie(GANZ), count: 4, capped: false, limit: 5000};
  else if(pfad.indexOf('/api/v1/search/people?') >= 0) antwort = {items: LEUTE, count: 3, hits: 4, capped: false};
  else if(pfad.indexOf('/api/v1/search?') >= 0) antwort = {items: kopie(HITS), limit: 20, offset: 0, has_more: false};
  else if(pfad.indexOf('/api/v1/similar?') >= 0) antwort = {items: kopie(HITS).slice(0, 1), limit: 20, offset: 0, has_more: false};
  else if(pfad.indexOf('/api/v1/documents/facts') >= 0) antwort = {};
  else antwort = statusGeruest();
  return Promise.resolve({ok: true, status: 200, json: function(){ return Promise.resolve(antwort); }});
};
function zahlDer(muster){ return gefragt.filter(function(p){ return p.indexOf(muster) >= 0; }).length; }

setTimeout(function(){
  aktiverTab = 'suche';
  pruefe(!SUCHE_LIEF && RESULT_VIEW === 'list', 'Vor der ersten Suche schon ein Ergebnis');
  el('q').value = 'Rechnung';
  doSearch(0);
  setTimeout(function(){
    pruefe(!el('result-views').classList.contains('hide'), 'Streifen fehlt nach der Suche');
    pruefe(el('results').innerHTML.indexOf('class="hit') >= 0, 'Liste nicht gezeichnet');
    pruefe(el('treffer-stand').textContent.indexOf('2') >= 0, 'Kopf zaehlt nicht die Seite: ' + el('treffer-stand').textContent);
    pruefe(zahlDer('/search/timeline?') === 0 && zahlDer('/search/people?') === 0, 'Das ganze Ergebnis wurde ungefragt geholt');

    // The timeline: the whole result, fetched once, in date order
    resultView('timeline');
    pruefe(zahlDer('/search/timeline?q=Rechnung') === 1, 'Zeitleiste fragt nicht das ganze Ergebnis: ' + gefragt.join(' '));
    setTimeout(function(){
      var html = el('results').innerHTML;
      pruefe(html.indexOf('class="zeit') >= 0 && html.indexOf('class="hit') < 0, 'Zeitleiste zeichnet keine Zeilen: ' + html.slice(0, 200));
      pruefe(html.indexOf('Alte Nachricht') < html.indexOf('Projekt Alpha') && html.indexOf('Projekt Alpha') < html.indexOf('Rechnung 4711'), 'Reihenfolge nicht nach Datum');
      pruefe(html.indexOf('Ohne Datum') > html.indexOf('Rechnung 4711'), 'Undatiertes nicht zuletzt');
      pruefe(!el('result-activity').classList.contains('hide') && el('result-activity').innerHTML.indexOf("resultBucket('2026-06')") >= 0, 'Band fehlt: ' + el('result-activity').innerHTML.slice(0, 200));
      pruefe(el('treffer-stand').textContent.indexOf('4') >= 0, 'Kopf nennt nicht das ganze Ergebnis: ' + el('treffer-stand').textContent);
      pruefe(el('pager').classList.contains('hide'), 'Der Pager bleibt in der Zeitleiste');
      resultBucket('2026-06');
      pruefe(el('results').innerHTML.indexOf('Alte Nachricht') < 0 && el('results').innerHTML.indexOf('Rechnung 4711') >= 0, 'Monatswahl grenzt nicht ein');
      pruefe(el('result-activity').innerHTML.indexOf("resultBucket(null)") >= 0, 'gewaehlter Monat ohne Rueckweg');
      resultBucket('2026-06');
      pruefe(RESULT_BUCKET === null && el('results').innerHTML.indexOf('Alte Nachricht') >= 0, 'zweiter Klick laesst nicht los');
      pruefe(zahlDer('/search/timeline?') === 1, 'Das ganze Ergebnis wurde erneut geholt');
      // A row opens the detail; its counter and its arrows count the timeline
      waehleTreffer(0);
      pruefe(!el('detail').classList.contains('hide') && el('detail-inhalt').innerHTML.indexOf('Alte Nachricht') >= 0, 'Zeile oeffnet das Detail nicht');
      pruefe(el('detail-inhalt').innerHTML.indexOf('1 von 4') >= 0, 'Zaehler zaehlt nicht die Zeitleiste: ' + el('detail-inhalt').innerHTML.slice(0, 300));
      // The tick works on the timeline's rows, and clearing keeps the view
      trefferWahl(0, true);
      pruefe(AUSWAHL['k3'], 'Haekchen greift nicht');
      auswahlLeeren(true);
      pruefe(el('results').innerHTML.indexOf('class="zeit') >= 0, 'Auswahl leeren zeichnet die Liste statt der Zeitleiste');

      // The people: the picture over the whole result, me left out
      resultView('people');
      pruefe(zahlDer('/search/people?q=Rechnung') === 1, 'Personen fragen nicht das ganze Ergebnis');
      setTimeout(function(){
        var html = el('results').innerHTML;
        pruefe(html.split('class="knoten').length - 1 === 2 && html.indexOf('resultPerson(') >= 0, 'Personenbild fehlt: ' + html.slice(0, 200));
        pruefe(html.indexOf('A B') < 0, 'ich selbst nicht ausgenommen');
        pruefe(el('result-people-count').textContent === '2', 'Zahl im Streifen: ' + el('result-people-count').textContent);
        pruefe(html.indexOf('2 Personen in 4') >= 0, 'Kopf: ' + html.slice(0, 200));
        pruefe(el('detail').classList.contains('hide') && el('result-activity').classList.contains('hide'), 'Detail oder Band bleiben neben dem Bild');
        resultPerson('Bob Baumeister');
        html = el('results').innerHTML;
        pruefe(html.indexOf('person-karte') >= 0 && html.indexOf("resultPersonSearch('Bob Baumeister', 'timeline')") >= 0 &&
               html.indexOf("resultPersonSearch('Bob Baumeister', 'list')") >= 0, 'Karte oder Wege fehlen');
        // The way on: the person pill set, the search run again in the timeline
        resultPersonSearch('Bob Baumeister', 'timeline');
        pruefe(el('f-person').value === 'Bob Baumeister' && gefragt[gefragt.length - 1].indexOf('person=Bob+Baumeister') >= 0, 'Person nicht in der Suche: ' + gefragt[gefragt.length - 1]);
        setTimeout(function(){
          pruefe(RESULT_VIEW === 'timeline' && zahlDer('/search/timeline?') === 2 && gefragt[gefragt.length - 1].indexOf('/search/timeline?') >= 0 &&
                 gefragt[gefragt.length - 1].indexOf('person=Bob+Baumeister') >= 0, 'Neue Kriterien holen die Zeitleiste nicht neu: ' + gefragt.slice(-3).join(' '));
          setTimeout(function(){
            pruefe(el('results').innerHTML.indexOf('class="zeit') >= 0, 'Zeitleiste der Person nicht gezeichnet');
            pruefe(el('result-people-count').textContent === '', 'Alte Personenzahl bleibt stehen');
            // Back to the list: the page, its pager, its count
            resultView('list');
            pruefe(el('results').innerHTML.indexOf('class="hit') >= 0 && !el('pager').classList.contains('hide') &&
                   el('treffer-stand').textContent.indexOf('2') >= 0, 'Liste kommt nicht zurueck');
            // Similar hits have no whole result: no strip
            aehnlicheZu('1');
            setTimeout(function(){
              pruefe(el('result-views').classList.contains('hide') && RESULT_VIEW === 'list', 'Streifen bei aehnlichen Treffern');
              console.log('OK');
            }, 20);
          }, 20);
        }, 20);
      }, 20);
    }, 20);
  }, 20);
}, 0);
"""


def test_das_ergebnis_hat_drei_sichten_ueber_das_ganze_ergebnis():
    """List, timeline and people over a search (13.6): the strip appears
    after a search, the whole result is fetched once per view and only
    when asked for, the timeline orders by date and narrows by month, the
    detail counts its rows, the people leave me out and hand over with the
    person pill set."""
    _in_node(PRUEFUNG_ERGEBNIS_SICHTEN)


PRUEFUNG_GESPRAECH_OEFFNEN = GRUNDZUSTAND + """
process.on('unhandledRejection', function(e){ console.error('REJECTION', e); process.exit(1); });
KANN_VERLAUF = true;
function nachricht(uid, date, who, title, preview){
  return {uid: uid, key: 'mail:' + uid, cid: 1, date: date, who: who, title: title, source: 'outlook', root: 'outlook',
          path: uid + '.eml', uri: 'o365://outlook/' + uid + '.eml', thread: 'tix:abc', preview: preview, cases: []};
}
var FADEN = {count: 3, items: [nachricht('a', '2025-06-01 09:00', 'Alice', 'Frage', 'Erste'),
                               nachricht('x', '2025-06-02 10:00', 'Bob', 'RE: Frage', 'Zweite'),
                               nachricht('y', '2025-06-03 11:00', 'Alice', 'AW: Frage', 'Dritte')]};
global.fetch = function(pfad){
  return Promise.resolve({json: function(){
    if(String(pfad).indexOf('/api/v1/threads') === 0) return Promise.resolve(FADEN);
    if(String(pfad).indexOf('/api/v1/documents/facts') === 0) return Promise.resolve({});
    // The poll's inventory must keep the index's features, or the fold vanishes
    if(String(pfad).indexOf('/api/v1/inventory') === 0) return Promise.resolve(BESTAND);
    return Promise.resolve(statusGeruest());
  }});
};
renderHits({results: [nachricht('a', '2025-06-01 09:00', 'Alice', 'Frage', 'Erste'),
                      Object.assign(nachricht('z', '2025-07-01 09:00', 'Carla', 'Anderes', 'Vierte'), {thread: null})], count: 2});
waehleTreffer(0);
setTimeout(function(){
  var falte = el('detail-verlauf').innerHTML;
  pruefe(falte.indexOf('files/content') < 0, 'Eine Zeile des Gespraechs laedt noch das Original');
  pruefe(falte.indexOf('openThreadMessage(&quot;tix:abc&quot;, 1)') >= 0 && falte.indexOf('role="button"') >= 0, 'Zeile fuehrt nicht ins Detail: ' + falte.slice(0, 300));
  // A message that is no hit of the list: the detail on its own, without the list's counter
  KANN_VERLAUF = true;      // the stub's poll carries no index – set as the head does
  openThreadMessage('tix:abc', 1);
  setTimeout(function(){
    var html = el('detail-inhalt').innerHTML;
    pruefe(html.indexOf('RE: Frage') >= 0 && html.indexOf('Zweite') >= 0, 'Nachricht aus dem Gespraech nicht im Detail: ' + html.slice(0, 300));
    pruefe(html.indexOf('class="zaehler"') < 0, 'Zaehler der Liste an einer Nachricht, die nicht in der Liste ist');
    pruefe(html.indexOf("fallWahl('einer', 'detail')") >= 0, 'In einen Fall geht von hier nicht');
    pruefe(TREFFER_GEWAEHLT === 0, 'Die Listenwahl sprang');
    setTimeout(function(){
      var falte2 = el('detail-verlauf').innerHTML;
      pruefe(falte2.indexOf('vzeile dies') >= 0 && falte2.indexOf('RE: Frage') >= 0, 'Die Falte folgt der Nachricht nicht: ' + falte2.slice(0, 400));
      pruefe(falte2.indexOf("fallWahl('einer', 'detail')") >= 0, 'Das Gespraech laesst sich von hier nicht hinzufuegen');
      FALL_WAHL = {art: 'einer', daten: 'detail'};
      pruefe(fallWahlTitel().indexOf('RE: Frage') >= 0, 'Das Fallfenster nennt nicht die gezeigte Nachricht: ' + fallWahlTitel());
      // A message that is a hit of the list opens through the list, the selection follows
      openThreadMessage('tix:abc', 0);
      setTimeout(function(){
        pruefe(TREFFER_GEWAEHLT === 0 && el('detail-inhalt').innerHTML.indexOf('class="zaehler"') >= 0 &&
               el('detail-inhalt').innerHTML.indexOf('Erste') >= 0, 'Treffer der Liste ohne Zaehler');
        console.log('OK');
      }, 20);
    }, 20);
  }, 20);
}, 20);
"""


def test_eine_nachricht_des_gespraechs_oeffnet_sich_als_detail():
    """A row of the conversation fold opens its message as the detail
    (13.6) instead of the original: through the list when the message is
    a hit there, on its own – without the list's counter, with "add to
    case" still working – when it is not."""
    _in_node(PRUEFUNG_GESPRAECH_OEFFNEN)


def test_das_ergebnis_traegt_seinen_streifen_im_markup():
    seite = app_mod.seite()
    block = seite[seite.index('<section id="tab-suche"'):seite.index('<section id="tab-faelle"')]
    assert block.count('data-result-view=') == 3
    assert block.index('id="ki-klappe"') < block.index('id="result-views"') < block.index('id="treffer-split"')
    assert block.index('id="result-activity"') < block.index('id="results"')
    assert 'id="result-people-count"' in block
    # No tool in the strip: the band narrows the timeline, the person pill the people.
    for kennung in ("result-people-filter", "result-direction"):
        assert kennung not in block, kennung
    # The address book: list or picture, before the origin chips
    assert block.count('data-book-mode=') == 2
    assert block.index('data-book-mode="list"') < block.index('data-book="all"')
    assert block.index('id="book-origin"') < block.index('id="kbBox"')


PRUEFUNG_ADRESSBUCH_BILD = GRUNDZUSTAND + """
process.on('unhandledRejection', function(e){ console.error('REJECTION', e); process.exit(1); });
var LEUTE = [
  {name: 'Dana Dienstleister', address: 'dana@example.org', items: 30, by_source: {outlook: 30}, first: '2026-02-01 08:00', last: '2026-06-10 08:00'},
  {name: 'Bob Baumeister', address: '', items: 12, by_source: {teams: 12}, first: '2026-03-01 08:00', last: '2026-06-01 09:35'},
  {name: 'A B', address: 'a@example.com', items: 4, by_source: {outlook: 4}, first: '2026-01-01 08:00', last: '2026-01-01 08:00'}
];
var gefragt = [];
global.fetch = function(pfad){
  pfad = String(pfad); gefragt.push(pfad);
  var antwort;
  if(pfad.indexOf('/api/v1/calendar') >= 0)
    antwort = {generated: '2026-08-07T09:00:00', counts: {kalender: 0, rekonstruiert: 0},
               recs: [{src: 'kontakte', title: 'Dana Dienstleister', em: ['dana@example.org'], tel: ['+49 40 000 000'],
                       org: 'Dienstleister GmbH', role: 'Projektleitung', root: 'outlook', rel: 'kontakte/d.vcf'}]};
  else if(pfad.indexOf('/api/v1/search/people') >= 0) antwort = {items: LEUTE, count: 3, hits: 46, capped: false};
  else antwort = statusGeruest();
  return Promise.resolve({ok: true, status: 200, json: function(){ return Promise.resolve(antwort); }});
};
function zahlDer(muster){ return gefragt.filter(function(p){ return p.indexOf(muster) >= 0; }).length; }

setTimeout(function(){
  var status = statusGeruest();
  status.calendar = {exists: true, built_at: '2026-08-07T10:00:00'};
  renderStatus(status);
  aktiverTab = 'suche';
  offeneSicht = 'adressbuch';
  ladeKalender('adressbuch');
  setTimeout(function(){
    pruefe(el('kbBox').innerHTML.indexOf('Dana Dienstleister') >= 0 && el('kbBox').innerHTML.indexOf('card2') >= 0, 'Liste leer');
    pruefe(zahlDer('/search/people') === 0, 'Das Archiv-Bild wurde ungefragt geholt');
    bookMode('picture');
    pruefe(zahlDer('/search/people') === 1, 'Bild fragt nicht das Archiv: ' + gefragt.join(' '));
    setTimeout(function(){
      var html = el('kbBox').innerHTML;
      pruefe(html.split('class="knoten').length - 1 === 2 && html.indexOf('bookPerson(') >= 0, 'Bild ohne Knoten: ' + html.slice(0, 200));
      pruefe(html.indexOf('A B') < 0, 'ich selbst nicht ausgenommen');
      pruefe(html.indexOf('2 Personen in 46') >= 0, 'Kopf: ' + html.slice(0, 200));
      pruefe(el('book-origin').classList.contains('hide') && el('book-note').classList.contains('hide'), 'Herkunfts-Chips bleiben im Bild');
      pruefe(el('kbStats').textContent === '', 'Zaehlzeile der Liste bleibt stehen');
      bookPerson('Dana Dienstleister');
      html = el('kbBox').innerHTML;
      pruefe(html.indexOf('person-karte') >= 0 && html.indexOf('+49 40 000 000') >= 0 && html.indexOf('Dienstleister GmbH') >= 0, 'Karte ohne die Fakten des Adressbuchs: ' + html.slice(html.indexOf('person-karte'), html.indexOf('person-karte') + 400));
      pruefe(html.indexOf('zeigeKommunikation(&quot;Dana Dienstleister&quot;, \\'timeline\\')') >= 0, 'Wege fehlen');
      pruefe(zahlDer('/search/people') === 1, 'Das Bild wurde fuer die Karte erneut geholt');
      // The way over lands in the timeline, with the person pill set
      var echteSuche = doSearch, gesucht = null;
      doSearch = function(){ gesucht = {person: el('f-person').value, view: RESULT_VIEW}; };
      zeigeKommunikation('Dana Dienstleister', 'timeline');
      pruefe(gesucht && gesucht.person === 'Dana Dienstleister' && gesucht.view === 'timeline' && offeneSicht === 'treffer', 'Uebergabe landet nicht in der Zeitleiste: ' + JSON.stringify(gesucht));
      zeigeKommunikation('Dana Dienstleister');
      pruefe(gesucht.view === 'timeline', 'Kommunikation anzeigen landet nicht in der Zeitleiste');
      doSearch = echteSuche;
      // Back to the list: the chips are back
      offeneSicht = 'adressbuch';
      bookMode('list');
      pruefe(!el('book-origin').classList.contains('hide') && el('kbBox').innerHTML.indexOf('card2') >= 0, 'Liste kommt nicht zurueck');
      console.log('OK');
    }, 20);
  }, 20);
}, 0);
"""


def test_das_adressbuch_zeigt_das_bild_ueber_das_archiv():
    """Contacts as list or picture (13.6): the picture is the case's, over
    everyone the index names, fetched once when first asked for; the origin
    chips are absent there; the card carries what the address book knows;
    the way over lands in the timeline of the search."""
    _in_node(PRUEFUNG_ADRESSBUCH_BILD)


def test_teams_exclude_list_is_preset_normalised_and_reaches_the_step(sandbox, server):
    """"aspx" is the preset; the list is stored like SharePoint's and an
    emptied one travels as empty, not as the preset."""
    assert app_mod.load_config()["teams_files_exclude"] == "aspx"
    _, port = server
    code, r = call(port, "PATCH", "/api/v1/config", {"teams_files_exclude": " .ASPX, mp4 ,"})
    assert code == 200 and r["config"]["teams_files_exclude"] == "aspx, mp4"
    cfg = app_mod.load_config()
    assert app_mod.build_steps(cfg, {"teams": True})[0]["env"]["TEAMS_FILES_EXCLUDE"] == "aspx, mp4"
    cfg["teams_files_exclude"] = ""
    assert app_mod.build_steps(cfg, {"teams": True})[0]["env"]["TEAMS_FILES_EXCLUDE"] == ""
