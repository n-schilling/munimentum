"""Tests for teams_export.py – Graph client, rendering, progress and job setup.

All network access is replaced by fakes (SESSION or Graph objects); the
network is never actually touched. The pure helpers (safe, parse_ts, …)
are already covered in test_teams_export_helpers.py.
"""

import json
import re
import threading

import pytest
import requests

import progress
import teams_export as te

GRAPH = te.GRAPH
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")   # local time, no fixed value


# --------------------------------------------------------------------------
# Shared fakes and fixtures
# --------------------------------------------------------------------------
class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, payload=None, headers=None, content=b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Returns prepared responses in order and records the calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):   # simulate a network error
            raise r
        return r


class FakeGraph:
    """Fake for Graph/TokenClient: paged/get from prepared data per URL."""

    channels_enabled = True

    def __init__(self, pages=None, gets=None):
        self.pages = pages or {}
        self.gets = gets or {}
        self.paged_calls = []

    def paged(self, url, params=None):
        self.paged_calls.append(url)
        val = self.pages[url]
        if isinstance(val, Exception):
            raise val
        yield from val

    def get(self, url, params=None):
        return self.gets[url]


@pytest.fixture(autouse=True)
def _clear_stop():
    """STOP is a module-wide event – reset it after every test."""
    yield
    te.STOP.clear()


@pytest.fixture
def sleeps(monkeypatch):
    """Disarm time.sleep and record the waits (gate reset: conftest.py)."""
    import graph_client
    calls = []
    monkeypatch.setattr(graph_client.time, "sleep", lambda s: calls.append(s))
    return calls


def _msg(name, text, ts, ctype="text", **extra):
    m = {
        "messageType": "message",
        "from": {"user": {"displayName": name}},
        "body": {"contentType": ctype, "content": text},
        "createdDateTime": ts,
    }
    m.update(extra)
    return m


# --------------------------------------------------------------------------
# human_time (parse_ts & co. are already in test_teams_export_helpers.py)
# --------------------------------------------------------------------------
def test_human_time_formats_iso_locally():
    out = te.human_time("2025-06-01T09:30:00Z")
    assert TIME_RE.fullmatch(out)   # the exact time depends on the timezone
    # 7-digit fractional seconds (Graph) are tolerated
    assert TIME_RE.fullmatch(te.human_time("2025-06-01T09:30:00.1234567Z"))


def test_human_time_passes_garbage_through():
    assert te.human_time("") == ""
    assert te.human_time(None) == ""
    assert te.human_time("unsinn") == "unsinn"   # unparsable -> returned unchanged


# --------------------------------------------------------------------------
# HOSTED_RE – detection of hostedContents URLs
# --------------------------------------------------------------------------
def test_hosted_re_matches_v1_and_beta():
    u1 = "https://graph.microsoft.com/v1.0/chats/1/messages/2/hostedContents/abc/$value"
    u2 = "https://graph.microsoft.com/beta/teams/x/channels/y/messages/z/hostedContents/q/$value"
    assert te.HOSTED_RE.search(f'<img src="{u1}">').group(0) == u1
    assert te.HOSTED_RE.search(f'<img src="{u2}">').group(0) == u2
    assert te.HOSTED_RE.search('<img src="https://example.com/bild.png">') is None


# --------------------------------------------------------------------------
# get_bytes with image semantics (_BildClient) – the generic client with
# retry/backoff/paging is covered in test_graph_client.py
# --------------------------------------------------------------------------
def _session(monkeypatch, responses):
    import graph_client
    sess = FakeSession(responses)
    monkeypatch.setattr(graph_client, "SESSION", sess)
    return sess


def test_get_bytes_returns_content_and_type(monkeypatch):
    _session(monkeypatch, [FakeResponse(200, headers={"Content-Type": "image/png"},
                                        content=b"\x89PNG")])
    tc = te.TokenClient("tok", channels_enabled=False)
    assert tc.get_bytes("https://x/img") == (b"\x89PNG", "image/png")


def test_get_bytes_5xx_is_image_unavailable(monkeypatch):
    _session(monkeypatch, [FakeResponse(502)])
    tc = te.TokenClient("tok", channels_enabled=False)
    with pytest.raises(te.ImageUnavailable):   # no retry on a server error
        tc.get_bytes("https://x/img")


def test_get_bytes_persistent_429_is_image_unavailable(monkeypatch, sleeps):
    _session(monkeypatch, [FakeResponse(429)] * 4)
    tc = te.TokenClient("tok", channels_enabled=False)
    with pytest.raises(te.ImageUnavailable):
        tc.get_bytes("https://x/img")
    # The wait happens before the follow-up request; not after the last try.
    assert len(sleeps) == 3


def test_get_bytes_netzfehler_is_image_unavailable(monkeypatch, sleeps):
    """Network still gone after all retries -> placeholder, no abort."""
    import graph_client
    fehler = [requests.exceptions.ReadTimeout("weg")] * graph_client.NET_RETRIES
    _session(monkeypatch, fehler)
    tc = te.TokenClient("tok", channels_enabled=False)
    with pytest.raises(te.ImageUnavailable):
        tc.get_bytes("https://x/img")


def test_get_bytes_401_raises_tokenexpired(monkeypatch):
    _session(monkeypatch, [FakeResponse(401)])
    tc = te.TokenClient("tok", channels_enabled=False)
    with pytest.raises(te.TokenExpired):
        tc.get_bytes("https://x/img")


class _StubAnmeldung:
    """Only what the HTTP layer needs from auth.Login."""

    def __init__(self, token="alt"):
        self.token = token

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}


def test_get_bytes_401_refresh_then_ok(monkeypatch):
    _session(monkeypatch, [FakeResponse(401),
                           FakeResponse(200, headers={"Content-Type": "image/gif"},
                                        content=b"GIF")])
    g = object.__new__(te.Graph)
    g.anmeldung = _StubAnmeldung()
    g._refresh_lock = threading.Lock()
    g._refresh = lambda: None
    assert g.get_bytes("https://x/img") == (b"GIF", "image/gif")


# --------------------------------------------------------------------------
# Token mode: load_pasted_token
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _eigener_datenordner(tmp_path, monkeypatch):
    """auth.load_pasted_token also looks in the data directory – otherwise it
    would find the repo's gx_token.txt and the tests would hang on run order."""
    monkeypatch.setenv("OFFICE365_DATA_DIR", str(tmp_path))
    import settings
    settings.reset()
    yield
    settings.reset()


def test_load_pasted_token_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)   # don't read a gx_token.txt from the repo
    monkeypatch.setenv("GRAPH_TOKEN", '  "Bearer eyJ0abc"  ')
    assert te.load_pasted_token() == "eyJ0abc"   # quotes + prefix removed


def test_load_pasted_token_from_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_TOKEN", raising=False)
    (tmp_path / "gx_token.txt").write_text("  eyJ0datei \n", encoding="utf-8")
    assert te.load_pasted_token() == "eyJ0datei"


def test_load_pasted_token_missing_or_empty(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_TOKEN", raising=False)
    assert te.load_pasted_token() is None
    (tmp_path / "gx_token.txt").write_text("   \n", encoding="utf-8")
    assert te.load_pasted_token() is None


# --------------------------------------------------------------------------
# Category selection (no prompts – the app is the only caller)
# --------------------------------------------------------------------------
_OPTIONS = [("1on1", "a"), ("group", "b"), ("meeting", "c"), ("channels", "d")]


def test_selected_categories_ohne_variable_nimmt_die_vorgabe(monkeypatch):
    monkeypatch.delenv("EXPORT_CATEGORIES", raising=False)
    assert te.selected_categories(_OPTIONS) == {"1on1", "group", "meeting"}


def test_env_categories_liest_auswahl(monkeypatch):
    monkeypatch.setenv("EXPORT_CATEGORIES", "1on1, CHANNELS ;group")
    assert te.env_categories(_OPTIONS) == {"1on1", "group", "channels"}


def test_env_categories_ohne_variable_oder_ohne_treffer(monkeypatch):
    monkeypatch.delenv("EXPORT_CATEGORIES", raising=False)
    assert te.env_categories(_OPTIONS) is None
    monkeypatch.setenv("EXPORT_CATEGORIES", "")
    assert te.env_categories(_OPTIONS) is None
    monkeypatch.setenv("EXPORT_CATEGORIES", "quatsch,unsinn")
    assert te.env_categories(_OPTIONS) is None          # only unknowns -> normal default


def test_selected_categories_folgt_der_umgebung(monkeypatch):
    """The variable comes from app.py or the schedule – a deliberate choice."""
    monkeypatch.setenv("EXPORT_CATEGORIES", "channels")
    assert te.selected_categories(_OPTIONS) == {"channels"}


def test_select_teams_nimmt_alle_sortiert():
    graph = FakeGraph(pages={f"{GRAPH}/me/joinedTeams": [
        {"displayName": "Beta"}, {"displayName": "alpha"}]})
    teams = te.select_teams(graph)
    assert [t["displayName"] for t in teams] == ["alpha", "Beta"]


def test_select_teams_handles_errors():
    graph = FakeGraph(pages={f"{GRAPH}/me/joinedTeams": RuntimeError("kaputt")})
    assert te.select_teams(graph) == []
    graph = FakeGraph(pages={f"{GRAPH}/me/joinedTeams": te.TokenExpired()})
    with pytest.raises(te.TokenExpired):   # token expiry is passed through
        te.select_teams(graph)


# --------------------------------------------------------------------------
# member_name / chat_title
# --------------------------------------------------------------------------
def test_member_name_prefers_displayname_then_email():
    assert te.member_name({"displayName": " Alice "}) == "Alice"
    assert te.member_name({"email": "a@b.de"}) == "a@b.de"
    assert te.member_name({}) == ""


def test_chat_title_oneonone_uses_other_member():
    chat = {"chatType": "oneOnOne", "topic": "wird ignoriert", "id": "c1",
            "members": [{"userId": "me", "displayName": "Ich"},
                        {"userId": "u2", "displayName": "Alice Example"}]}
    assert te.chat_title(FakeGraph(), chat, "me") == "Alice Example"


def test_chat_title_oneonone_without_partner_is_unknown():
    chat = {"chatType": "oneOnOne", "id": "c1",
            "members": [{"userId": "me", "displayName": "Ich"}]}
    assert te.chat_title(FakeGraph(), chat, "me") == "Unbekannt"


def test_chat_title_group_prefers_topic():
    chat = {"chatType": "group", "topic": "Projekt X", "id": "c1", "members": []}
    assert te.chat_title(FakeGraph(), chat, "me") == "Projekt X"


def test_chat_title_group_joins_members_and_truncates():
    members = [{"userId": f"u{i}", "displayName": f"P{i}"} for i in range(7)]
    chat = {"chatType": "group", "topic": None, "id": "c1", "members": members}
    title = te.chat_title(FakeGraph(), chat, "me")
    assert title == "P0, P1, P2, P3, P4…"   # at most 5 names plus ellipsis


def test_chat_title_loads_members_when_missing():
    chat = {"chatType": "oneOnOne", "id": "c9"}
    graph = FakeGraph(pages={f"{GRAPH}/me/chats/c9/members": [
        {"userId": "me", "displayName": "Ich"},
        {"userId": "u2", "displayName": "Bob"}]})
    assert te.chat_title(graph, chat, "me") == "Bob"
    assert graph.paged_calls == [f"{GRAPH}/me/chats/c9/members"]


def test_chat_title_member_load_failure_falls_back():
    chat = {"chatType": "group", "topic": None, "id": "c9"}
    graph = FakeGraph(pages={f"{GRAPH}/me/chats/c9/members": RuntimeError("nope")})
    assert te.chat_title(graph, chat, "me") == "group"   # topic or ctype or "Chat"


# --------------------------------------------------------------------------
# Rendering: attachments, reactions, messages, conversation
# --------------------------------------------------------------------------
def test_render_attachments_links_and_escapes():
    out = te.render_attachments([
        {"name": "Plan <Q3>.docx", "contentUrl": "https://x/a?b=1&c=2"},
        {"contentType": "reference"},
    ])
    assert 'href="https://x/a?b=1&amp;c=2"' in out
    assert "Plan &lt;Q3&gt;.docx" in out
    assert "reference" in out          # without a URL just the name/type
    assert te.render_attachments([]) == ""
    assert te.render_attachments(None) == ""


def test_render_reactions_counts_types():
    out = te.render_reactions([{"reactionType": "like"}, {"reactionType": "like"},
                               {"reactionType": "heart"}])
    assert "like ×2" in out and "heart ×1" in out
    assert te.render_reactions([]) == ""
    assert te.render_reactions(None) == ""


def test_render_message_text_is_escaped():
    m = _msg("Alice <X>", "<b>kein html</b>", "2025-06-01T09:30:00Z")
    out = te.render_message(m)
    assert 'class="msg text"' in out           # text message -> pre-wrap class
    assert "Alice &lt;X&gt;" in out            # Name escaped
    assert "&lt;b&gt;kein html&lt;/b&gt;" in out
    assert TIME_RE.search(out)                 # local time rendered


def test_render_message_html_is_cleaned():
    m = _msg("Bob", '<div onclick="evil()">Hi</div><script>x()</script>',
             "2025-06-01T09:30:00Z", ctype="html")
    out = te.render_message(m)
    assert "onclick" not in out and "script" not in out
    assert ">Hi</div>" in out                  # HTML is otherwise preserved


def test_render_message_deleted_and_subject_and_reply():
    m = _msg("Bob", "weg", "2025-06-01T09:30:00Z",
             deletedDateTime="2025-06-02T00:00:00Z", subject="Thema <1>")
    out = te.render_message(m, is_reply=True)
    assert "[gelöscht]" in out
    assert "weg" not in out
    assert "<strong>Thema &lt;1&gt;</strong>" in out
    assert 'class="msg reply"' in out


def test_render_message_from_application_and_unknown():
    m = _msg("x", "hi", "2025-06-01T09:30:00Z")
    m["from"] = {"application": {"displayName": "Ein Bot"}}
    assert '<span class="name">Ein Bot</span>' in te.render_message(m)
    m["from"] = None
    assert "Unbekannt" in te.render_message(m)


def test_render_message_system_event_variants():
    sys_msg = {"messageType": "systemEventMessage",
               "createdDateTime": "2025-06-01T09:30:00Z",
               "body": {"content": "<p>Alice wurde hinzugefügt</p>"}}
    out = te.render_message(sys_msg)
    assert 'class="sys"' in out
    assert "Alice wurde hinzugefügt" in out

    sys_msg["body"] = {"content": ""}
    sys_msg["eventDetail"] = {"@odata.type": "#microsoft.graph.membersAddedEventMessageDetail"}
    assert "membersAddedEventMessageDetail" in te.render_message(sys_msg)

    sys_msg["eventDetail"] = {}
    assert "Systemnachricht" in te.render_message(sys_msg)


def test_render_conversation_escapes_and_marks_failed_images():
    html = te.render_conversation("Titel <X>", "1:1-Chat", "3 Nachrichten & mehr",
                                  ["<div>a</div>", f'<img src="{te.IMG_PLACEHOLDER}">',
                                   f'<img src="{te.IMG_PLACEHOLDER}">'])
    assert "<title>Titel &lt;X&gt;</title>" in html
    assert "3 Nachrichten &amp; mehr" in html
    assert "2 Bild(er) konnten nicht geladen werden" in html


def test_render_conversation_empty_blocks():
    html = te.render_conversation("T", "S", "M", [])
    assert "Keine Nachrichten." in html
    assert "konnten nicht geladen" not in html


def test_render_blocks_counts_images():
    msgs = [_msg("A", "eins", "2025-06-01T09:00:00Z"),
            _msg("B", "zwei", "2025-06-01T09:05:00Z")]
    blocks, nimg = te.render_blocks(msgs)
    assert len(blocks) == 2 and nimg == 0
    assert "eins" in blocks[0] and "zwei" in blocks[1]


# --------------------------------------------------------------------------
# embed_hosted_images
# --------------------------------------------------------------------------
HOSTED_URL = "https://graph.microsoft.com/v1.0/chats/1/messages/2/hostedContents/abc/$value"


class FakeImgClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def get_bytes(self, url):
        self.calls += 1
        if self.error:
            raise self.error
        return b"BILD", "image/png"


def test_embed_hosted_images_noop_without_client(monkeypatch):
    monkeypatch.setattr(te, "_client", None)
    html = f'<img src="{HOSTED_URL}">'
    assert te.embed_hosted_images(html) == html


def test_embed_hosted_images_inlines_as_data_uri(monkeypatch):
    client = FakeImgClient()
    monkeypatch.setattr(te, "_client", client)
    monkeypatch.setattr(te, "IMGCACHE_DIR", None)
    counter = [0]
    out = te.embed_hosted_images(f'<img src="{HOSTED_URL}"> und <a href="https://example.com">x</a>',
                                 counter)
    assert "data:image/png;base64,QklMRA==" in out   # base64("BILD")
    assert "hostedContents" not in out
    assert "https://example.com" in out              # foreign URLs stay in place
    assert counter == [1] and client.calls == 1


def test_embed_hosted_images_failure_yields_placeholder(monkeypatch):
    monkeypatch.setattr(te, "_client", FakeImgClient(error=te.ImageUnavailable("502")))
    monkeypatch.setattr(te, "IMGCACHE_DIR", None)
    counter = [0]
    out = te.embed_hosted_images(f'<img src="{HOSTED_URL}">', counter)
    assert te.IMG_PLACEHOLDER in out
    assert counter == [0]   # failed images do not count


def test_embed_hosted_images_token_expired_propagates(monkeypatch):
    monkeypatch.setattr(te, "_client", FakeImgClient(error=te.TokenExpired()))
    monkeypatch.setattr(te, "IMGCACHE_DIR", None)
    with pytest.raises(te.TokenExpired):
        te.embed_hosted_images(f'<img src="{HOSTED_URL}">')


def test_embed_hosted_images_uses_cache(monkeypatch, tmp_path):
    client = FakeImgClient()
    monkeypatch.setattr(te, "_client", client)
    monkeypatch.setattr(te, "IMGCACHE_DIR", tmp_path)
    html = f'<img src="{HOSTED_URL}">'
    out1 = te.embed_hosted_images(html)
    out2 = te.embed_hosted_images(html)   # second run: cache hit, no download
    assert out1 == out2
    assert client.calls == 1
    assert len(list(tmp_path.iterdir())) == 1


# --------------------------------------------------------------------------
# Progress: load_state / save_state / already_done / get_record / cleanup_old
# --------------------------------------------------------------------------
def test_load_state_defaults_and_roundtrip(tmp_path):
    state = te.load_state(tmp_path)
    assert state == {"version": 1, "conversations": {}}
    state["conversations"]["k"] = {"done": True, "rel": "1on1/a.html"}
    te.save_state(tmp_path, state)
    assert te.load_state(tmp_path) == state
    import state_db
    assert (tmp_path / state_db.DB_NAME).exists()   # lives in state.db


def test_load_state_ignores_corrupt_entry(tmp_path):
    import state_db
    state_db.StateDb(tmp_path).kv_schreiben("state", "{kaputt")
    assert te.load_state(tmp_path) == {"version": 1, "conversations": {}}
    state_db.StateDb(tmp_path).kv_schreiben("state", '["falsche form"]')
    assert te.load_state(tmp_path) == {"version": 1, "conversations": {}}


def test_already_done_requires_record_and_file(tmp_path):
    state = {"version": 1, "conversations": {
        "a": {"done": True, "rel": "1on1/a.html"},
        "b": {"done": False, "rel": "1on1/b.html"},
    }}
    assert not te.already_done(tmp_path, state, "fehlt")
    assert not te.already_done(tmp_path, state, "b")       # not finished
    assert not te.already_done(tmp_path, state, "a")       # file still missing
    (tmp_path / "1on1").mkdir()
    (tmp_path / "1on1" / "a.html").write_text("x", encoding="utf-8")
    assert te.already_done(tmp_path, state, "a")


def test_get_record_variants(tmp_path):
    state = {"version": 1, "conversations": {
        "done": {"done": True, "rel": "1on1/a.html", "last_activity": "2025-06-01T00:00:00Z"},
        "empty": {"done": True, "rel": None, "empty": True},
        "open": {"done": False, "rel": "1on1/x.html"},
    }}
    assert te.get_record(tmp_path, state, "fehlt") is None
    assert te.get_record(tmp_path, state, "open") is None
    assert te.get_record(tmp_path, state, "done") is None    # file missing
    (tmp_path / "1on1").mkdir()
    (tmp_path / "1on1" / "a.html").write_text("x", encoding="utf-8")
    assert te.get_record(tmp_path, state, "done")["last_activity"] == "2025-06-01T00:00:00Z"
    # empty chats count as a valid status without a file
    assert te.get_record(tmp_path, state, "empty")["empty"] is True


def test_cleanup_old_removes_renamed_file_only(tmp_path):
    (tmp_path / "1on1").mkdir()
    old = tmp_path / "1on1" / "Unbekannt__1234.html"
    old.write_text("alt", encoding="utf-8")
    te.cleanup_old(tmp_path, {"rel": "1on1/Unbekannt__1234.html"}, "1on1/Unbekannt__1234.html")
    assert old.exists()   # same name -> delete nothing
    te.cleanup_old(tmp_path, {"rel": "1on1/Unbekannt__1234.html"}, "1on1/Alice__1234.html")
    assert not old.exists()
    te.cleanup_old(tmp_path, {"rel": "1on1/weg.html"}, "1on1/neu.html")   # missing -> no error
    te.cleanup_old(tmp_path, None, "1on1/neu.html")


def test_record_done_persists_record(tmp_path):
    state = te.load_state(tmp_path)
    te.record_done(tmp_path, state, "k1", "1on1", "Alice", "1on1/a.html", 7,
                   last_activity="2025-06-01T09:30:00Z")
    import state_db
    roh = state_db.StateDb(tmp_path).kv_lesen("state")
    rec = json.loads(roh)["conversations"]["k1"]
    assert rec["category"] == "1on1" and rec["title"] == "Alice"
    assert rec["rel"] == "1on1/a.html" and rec["count"] == 7
    assert rec["done"] is True and rec["empty"] is False
    assert rec["last_activity"] == "2025-06-01T09:30:00Z"


# --------------------------------------------------------------------------
# Exporting a conversation (chat and channel) with a faked Graph
# --------------------------------------------------------------------------
def _chat_fixture(chat_id="c1"):
    chat = {"id": chat_id, "chatType": "oneOnOne",
            "members": [{"userId": "me", "displayName": "Ich"},
                        {"userId": "u2", "displayName": "Alice Example"}]}
    msgs = [_msg("Alice Example", "zweite", "2025-06-02T08:00:00Z"),
            _msg("Ich", "erste", "2025-06-01T09:30:00Z")]
    graph = FakeGraph(pages={f"{GRAPH}/me/chats/{chat_id}/messages": msgs})
    return chat, graph


def test_export_one_chat_writes_html_and_state(tmp_path):
    chat, graph = _chat_fixture()
    state = te.load_state(tmp_path)
    status, folder, title, count, secs, _z = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert (status, folder, title, count) == ("new", "1on1", "Alice Example", 2)

    fname = f"Alice Example__{te.short_id('c1')}.html"
    html = (tmp_path / "1on1" / fname).read_text(encoding="utf-8")
    idx_erste, idx_zweite = html.index("erste"), html.index("zweite")
    assert idx_erste < idx_zweite            # sorted chronologically
    assert "Alice Example" in html and "Chat-ID c1" in html

    rec = state["conversations"]["c1"]
    assert rec["rel"] == f"1on1/{fname}"
    assert rec["last_activity"] == "2025-06-02T08:00:00Z"
    assert rec["empty"] is False


def test_export_one_chat_second_run_reports_updated_and_renames(tmp_path):
    chat, graph = _chat_fixture()
    state = te.load_state(tmp_path)
    te.export_one_chat(graph, tmp_path, state, "me", chat)
    # Name changes (e.g. 'Unbekannt' -> real name, simulated by a member swap)
    old_rel = state["conversations"]["c1"]["rel"]
    chat["members"][1]["displayName"] = "Alice Umbenannt"
    status, _, title, _, _, _z = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert status == "updated" and title == "Alice Umbenannt"
    assert not (tmp_path / old_rel).exists()   # old file was cleaned up
    assert (tmp_path / state["conversations"]["c1"]["rel"]).exists()


def test_export_one_chat_only_system_messages_is_empty(tmp_path):
    chat = {"id": "c2", "chatType": "meeting", "topic": "Standup"}
    sysmsg = {"messageType": "systemEventMessage",
              "createdDateTime": "2025-06-03T07:00:00Z",
              "body": {"content": "Anruf beendet"}}
    graph = FakeGraph(pages={f"{GRAPH}/me/chats/c2/messages": [sysmsg]})
    state = te.load_state(tmp_path)
    status, folder, title, count, _, _z = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert (status, folder, title, count) == ("empty", "meeting", "Standup", 1)
    rec = state["conversations"]["c2"]
    assert rec["empty"] is True and rec["rel"] is None
    assert rec["last_activity"] == "2025-06-03T07:00:00Z"
    assert not (tmp_path / "meeting").exists()   # no file written


def _channel_fixture(reply_ts="2025-06-05T10:00:00Z"):
    team = {"id": "t1", "displayName": "Team Rakete"}
    ch = {"id": "k1", "displayName": "Allgemein", "membershipType": "standard"}
    root = _msg("Alice", "Wurzelpost", "2025-06-04T09:00:00Z")
    root["replies"] = [_msg("Bob", "Antwort", reply_ts)]
    graph = FakeGraph(pages={f"{GRAPH}/teams/t1/channels/k1/messages": [root]})
    return team, ch, graph


def test_export_one_channel_writes_nested_replies(tmp_path):
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    status, cat, title, count, _, _z = te.export_one_channel(graph, tmp_path, state, team, ch)
    assert (status, cat, title, count) == ("new", "channels", "Team Rakete / Allgemein", 2)

    fname = f"Allgemein__{te.short_id('k1')}.html"
    html = (tmp_path / "channels" / "Team Rakete" / fname).read_text(encoding="utf-8")
    assert "Wurzelpost" in html and "Antwort" in html
    assert 'class="msg reply' in html                    # reply indented
    assert "2 Nachrichten (inkl. Antworten)" in html
    assert state["conversations"]["ch:k1"]["last_activity"] == "2025-06-05T10:00:00Z"


def test_export_one_channel_unchanged_on_second_run(tmp_path):
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch)
    status, _, _, count, _, _z = te.export_one_channel(graph, tmp_path, state, team, ch)
    assert status == "unchanged" and count == 2


def test_export_one_channel_updates_on_new_reply(tmp_path):
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch)
    team, ch, graph = _channel_fixture(reply_ts="2025-06-06T12:00:00Z")   # newer reply
    status, _, _, _, _, _z = te.export_one_channel(graph, tmp_path, state, team, ch)
    assert status == "updated"
    assert state["conversations"]["ch:k1"]["last_activity"] == "2025-06-06T12:00:00Z"


# --------------------------------------------------------------------------
# Job setup: build_chat_jobs / build_channel_jobs
# --------------------------------------------------------------------------
def test_build_chat_jobs_new_updated_and_skipped(tmp_path):
    chats = [
        {"id": "neu", "chatType": "oneOnOne",
         "lastMessagePreview": {"createdDateTime": "2025-06-01T00:00:00Z"}},
        {"id": "upd", "chatType": "group",
         "lastMessagePreview": {"createdDateTime": "2025-06-05T00:00:00Z"}},
        {"id": "alt", "chatType": "meeting",
         "lastMessagePreview": {"createdDateTime": "2025-06-01T00:00:00Z"}},
        {"id": "fremd", "chatType": "unknownType",
         "lastMessagePreview": {"createdDateTime": "2025-06-05T00:00:00Z"}},
    ]
    graph = FakeGraph(pages={f"{GRAPH}/me/chats": chats})
    state = {"version": 1, "conversations": {
        "upd": {"done": True, "rel": "group/upd.html", "count": 1, "empty": False,
                "last_activity": "2025-06-02T00:00:00Z"},
        "alt": {"done": True, "rel": "meeting/alt.html", "count": 1, "empty": False,
                "last_activity": "2025-06-02T00:00:00Z"},
    }}
    for rel in ("group/upd.html", "meeting/alt.html"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")

    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_chat_jobs(graph, tmp_path, state, stats, "me",
                              {"1on1", "group", "meeting"})
    assert [(k, c["id"]) for k, c, _ in jobs] == [("chat", "neu"), ("chat", "upd")]
    assert stats["skipped"] == 1   # 'alt' unchanged; 'fremd' drops out of the categories


def test_build_channel_jobs_refresh_mode_and_error_team(monkeypatch, tmp_path):
    monkeypatch.setattr(te, "REFRESH_CHANNELS", True)
    graph = FakeGraph(pages={
        f"{GRAPH}/teams/t1/channels": [{"id": "k1", "displayName": "A"},
                                       {"id": "k2", "displayName": "B"}],
        f"{GRAPH}/teams/t2/channels": RuntimeError("keine Rechte"),
    })
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_channel_jobs(graph, tmp_path, {"version": 1, "conversations": {}}, stats,
                                 [{"id": "t1", "displayName": "T1"},
                                  {"id": "t2", "displayName": "T2"}])
    # The failing team is skipped, refresh mode re-checks all channels
    assert [(k, ch["id"]) for k, _t, ch in jobs] == [("channel", "k1"), ("channel", "k2")]


def test_build_channel_jobs_without_refresh_skips_done(monkeypatch, tmp_path):
    monkeypatch.setattr(te, "REFRESH_CHANNELS", False)
    graph = FakeGraph(pages={
        f"{GRAPH}/teams/t1/channels": [{"id": "k1", "displayName": "A"},
                                       {"id": "k2", "displayName": "B"}]})
    state = {"version": 1, "conversations": {
        "ch:k1": {"done": True, "rel": "channels/T1/a.html"}}}
    (tmp_path / "channels" / "T1").mkdir(parents=True)
    (tmp_path / "channels" / "T1" / "a.html").write_text("x", encoding="utf-8")
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_channel_jobs(graph, tmp_path, state, stats,
                                 [{"id": "t1", "displayName": "T1"}])
    assert [ch["id"] for _k, _t, ch in jobs] == ["k2"]
    assert stats["skipped"] == 1


def test_build_channel_jobs_token_expired_propagates(tmp_path):
    graph = FakeGraph(pages={f"{GRAPH}/teams/t1/channels": te.TokenExpired()})
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    with pytest.raises(te.TokenExpired):
        te.build_channel_jobs(graph, tmp_path, {"version": 1, "conversations": {}}, stats,
                              [{"id": "t1", "displayName": "T1"}])


# --------------------------------------------------------------------------
# make_runner / run_parallel
# --------------------------------------------------------------------------
def test_make_runner_dispatches_and_maps_errors(monkeypatch, tmp_path):
    sentinel = ("new", "1on1", "Alice", 2, 0.1)
    monkeypatch.setattr(te, "export_one_chat",
                        lambda graph, out, state, my_id, chat: sentinel)
    run = te.make_runner("g", tmp_path, {}, "me", "chat", {"id": "c"}, None)
    assert run() == sentinel

    def boom(*a, **kw):
        raise ValueError("kaputt")
    monkeypatch.setattr(te, "export_one_channel", boom)
    run = te.make_runner("g", tmp_path, {}, "me", "channel", {"id": "t"}, {"id": "k"})
    assert run() == ("error", None, "kaputt", 0, 0.0)
    assert not te.STOP.is_set()


def test_make_runner_token_expired_sets_stop(monkeypatch, tmp_path):
    def expired(*a, **kw):
        raise te.TokenExpired()
    monkeypatch.setattr(te, "export_one_chat", expired)
    run = te.make_runner("g", tmp_path, {}, "me", "chat", {"id": "c"}, None)
    assert run() == ("expired", None, None, 0, 0.0)
    assert te.STOP.is_set()
    # once STOP is set, further runners no longer start at all
    run2 = te.make_runner("g", tmp_path, {}, "me", "chat", {"id": "c2"}, None)
    assert run2() == ("stopped", None, None, 0, 0.0)


def test_run_parallel_counts_statuses():
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    runners = [
        lambda: ("new", "1on1", "A", 1, 0.5),
        lambda: ("updated", "group", "B", 2, 1.5),
        lambda: ("unchanged", "channels", "C", 3, 0.1),
        lambda: ("empty", "meeting", "D", 0, 0.1),
        lambda: ("error", None, "kaputt", 0, 0.0),
        lambda: ("stopped", None, None, 0, 0.0),
    ]
    assert te.run_parallel(runners, stats, workers=2) == "done"
    assert stats == {"new": 1, "updated": 1, "skipped": 1, "empty": 1}


def test_run_parallel_reports_expired():
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    runners = [lambda: ("new", "1on1", "A", 1, 0.5),
               lambda: ("expired", None, None, 0, 0.0)]
    assert te.run_parallel(runners, stats, workers=1) == "expired"
    assert stats["new"] == 1


def test_run_parallel_catches_raising_runner():
    def boom():
        raise RuntimeError("crash im Worker")
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    assert te.run_parallel([boom], stats, workers=1) == "done"
    assert stats == {"new": 0, "updated": 0, "skipped": 0, "empty": 0}


def test_run_parallel_empty_list_is_done():
    assert te.run_parallel([], {}, workers=4) == "done"


# --------------------------------------------------------------------------
# Files: referenced attachments next to the conversation, channel folders
# --------------------------------------------------------------------------
class _StreamAntwort:
    def __init__(self, daten):
        self.daten = daten

    def iter_content(self, chunk_size=0):
        yield self.daten


class _DateiGraph(FakeGraph):
    """FakeGraph plus the two calls the file download makes: the sharing
    lookup (get) and the content stream."""

    def __init__(self, pages=None, gets=None, ctag="c-1", size=3, kaputt=False):
        super().__init__(pages, gets)
        self.ctag, self.size, self.kaputt = ctag, size, kaputt
        self.geladen = []
        self.gefragt = []

    def get(self, url, params=None):
        if "/shares/u!" in url:
            self.gefragt.append(url)
            if self.kaputt:
                raise RuntimeError("403")
            return {"name": "Angebot.pdf", "cTag": self.ctag, "size": self.size}
        return super().get(url, params)

    def stream(self, url, timeout=None, label=""):
        self.geladen.append(url)
        return _StreamAntwort(b"PDF")


DATEI_URL = "https://firma.sharepoint.com/sites/x/Freigegebene%20Dokumente/Angebot.pdf"


def _chat_mit_datei(chat_id="c1", **graph_kw):
    chat = {"id": chat_id, "chatType": "oneOnOne",
            "members": [{"userId": "me", "displayName": "Ich"},
                        {"userId": "u2", "displayName": "Alice Example"}]}
    msgs = [_msg("Alice Example", "hier die Datei", "2025-06-02T08:00:00Z",
                 attachments=[{"id": "a1", "contentType": "reference",
                               "contentUrl": DATEI_URL, "name": "Angebot.pdf"},
                              {"id": "a2", "contentType": "application/vnd.microsoft.card.adaptive",
                               "content": "{}"}])]
    graph = _DateiGraph(pages={f"{GRAPH}/me/chats/{chat_id}/messages": msgs}, **graph_kw)
    return chat, graph


def test_anhaenge_bleiben_ohne_schalter_online_links(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "ATTACHMENTS", False)
    chat, graph = _chat_mit_datei()
    state = te.load_state(tmp_path)
    te.export_one_chat(graph, tmp_path, state, "me", chat)
    html = (tmp_path / state["conversations"]["c1"]["rel"]).read_text(encoding="utf-8")
    assert f'href="{DATEI_URL}"' in html and graph.geladen == []
    assert not (tmp_path / "1on1" / te.ANHANG_DIR).exists()


def test_anhaenge_landen_neben_dem_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "ATTACHMENTS", True)
    chat, graph = _chat_mit_datei()
    state = te.load_state(tmp_path)
    status, _f, _t, _n, _s, zahlen = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert zahlen["files"] == 1 and zahlen["excluded"] == 0
    rel = state["conversations"]["c1"]["rel"]
    html = (tmp_path / rel).read_text(encoding="utf-8")
    ordner = tmp_path / te.anhang_ordner(rel)
    dateien = list(ordner.iterdir())
    assert len(dateien) == 1 and dateien[0].read_bytes() == b"PDF"
    assert dateien[0].name.startswith("Angebot__") and dateien[0].name.endswith(".pdf")
    assert f'href="{te.ANHANG_DIR}/{ordner.name}/{dateien[0].name}"' in html
    assert DATEI_URL not in html
    assert "Angebot.pdf" in html

    # Second run, same cTag: the file is not fetched again.
    chat2, graph2 = _chat_mit_datei()
    te.export_one_chat(graph2, tmp_path, state, "me", chat2)
    assert graph2.gefragt and graph2.geladen == []


def test_anhaenge_ueber_der_grenze_bleiben_links(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "ATTACHMENTS", True)
    monkeypatch.setenv("TEAMS_FILES_MAX_MB", "1")
    chat, graph = _chat_mit_datei(size=5 * 1024 * 1024)
    state = te.load_state(tmp_path)
    _s, _f, _t, _n, _d, zahlen = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert zahlen == {"files": 0, "excluded": 1, "file_errors": 0}
    html = (tmp_path / state["conversations"]["c1"]["rel"]).read_text(encoding="utf-8")
    assert f'href="{DATEI_URL}"' in html and graph.geladen == []


def test_anhang_der_nicht_kommt_meldet_sich_einmal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(te, "ATTACHMENTS", True)
    chat, graph = _chat_mit_datei(kaputt=True)
    state = te.load_state(tmp_path)
    _s, _f, _t, _n, _d, zahlen = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert zahlen["file_errors"] == 1
    html = (tmp_path / state["conversations"]["c1"]["rel"]).read_text(encoding="utf-8")
    assert f'href="{DATEI_URL}"' in html
    ereignisse = [progress.lies_event(z) for z in capsys.readouterr().out.splitlines()]
    assert any(e and e["k"] == "run.teams.file_failed" for e in ereignisse)


def test_umbenannter_chat_nimmt_seine_dateien_mit(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "ATTACHMENTS", True)
    chat, graph = _chat_mit_datei()
    state = te.load_state(tmp_path)
    te.export_one_chat(graph, tmp_path, state, "me", chat)
    alt = state["conversations"]["c1"]["rel"]
    chat["members"][1]["displayName"] = "Alice Umbenannt"
    chat2, graph2 = _chat_mit_datei()
    chat2["members"][1]["displayName"] = "Alice Umbenannt"
    te.export_one_chat(graph2, tmp_path, state, "me", chat2)
    neu = state["conversations"]["c1"]["rel"]
    assert neu != alt
    assert not (tmp_path / te.anhang_ordner(alt)).exists()
    assert list((tmp_path / te.anhang_ordner(neu)).iterdir())
    assert graph2.geladen == [], "nach dem Umzug erneut geladen"


def test_kanalpost_verlinkt_in_den_spiegel(tmp_path, monkeypatch):
    """A channel post pointing into the mirrored channel folder links the
    mirror copy – and is not fetched a second time even with the
    attachment switch on."""
    monkeypatch.setattr(te, "ATTACHMENTS", True)
    team = {"id": "t1", "displayName": "Team Rakete"}
    ch = {"id": "k1", "displayName": "Allgemein", "membershipType": "standard"}
    url = "https://firma.sharepoint.com/sites/rakete/Freigegebene%20Dokumente/Allgemein/Plan%202026.xlsx"
    root = _msg("Alice", "Plan anbei", "2025-06-04T09:00:00Z",
                attachments=[{"id": "a1", "contentType": "reference",
                              "contentUrl": url, "name": "Plan 2026.xlsx"}])
    graph = _DateiGraph(pages={f"{GRAPH}/teams/t1/channels/k1/messages": [root]})
    spiegel = {"wurzel": "channels/Team Rakete", "praefix": "",
               "rel": "Dateien/Allgemein",
               "weburl": "https://firma.sharepoint.com/sites/rakete/Freigegebene Dokumente/Allgemein",
               "neu": 1}
    kopie = tmp_path / "channels" / "Team Rakete" / "Dateien" / "Allgemein" / "Plan 2026.xlsx"
    kopie.parent.mkdir(parents=True)
    kopie.write_bytes(b"XLSX")
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch, spiegel=spiegel)
    html = (tmp_path / state["conversations"]["ch:k1"]["rel"]).read_text(encoding="utf-8")
    assert 'href="Dateien/Allgemein/Plan 2026.xlsx"' in html
    assert graph.geladen == [] and graph.gefragt == []


def test_spiegel_links_kennt_nur_vorhandene_dateien(tmp_path):
    info = {"wurzel": "channels/T/Privat__abc", "praefix": "Privat__abc", "rel": "Dateien",
            "weburl": "https://firma.sharepoint.com/sites/privat/Dokumente"}
    msgs = [_msg("A", "x", "2025-06-04T09:00:00Z", attachments=[
        {"contentType": "reference", "contentUrl":
         "https://firma.sharepoint.com/sites/privat/Dokumente/Ordner/a.pdf"},
        {"contentType": "reference", "contentUrl":
         "https://firma.sharepoint.com/sites/anders/Dokumente/b.pdf"}])]
    (tmp_path / "channels/T/Privat__abc/Dateien/Ordner").mkdir(parents=True)
    (tmp_path / "channels/T/Privat__abc/Dateien/Ordner/a.pdf").write_bytes(b"x")
    lokal = te.spiegel_links(tmp_path, info, msgs)
    assert lokal == {"https://firma.sharepoint.com/sites/privat/Dokumente/Ordner/a.pdf":
                     "Privat__abc/Dateien/Ordner/a.pdf"}


def test_kanal_dateien_spiegeln_gruppiert_nach_bibliothek(tmp_path, monkeypatch):
    """Standard channels share the team library: one walk, scoped to their
    folders; a private channel gets its own folder and the whole drive."""
    import drive_mirror
    team = {"id": "t1", "displayName": "Team Rakete"}
    allgemein = {"id": "k1", "displayName": "Allgemein", "membershipType": "standard"}
    projekt = {"id": "k2", "displayName": "Projekt X", "membershipType": "standard"}
    privat = {"id": "k3", "displayName": "Geheim", "membershipType": "private"}
    gets = {
        f"{GRAPH}/teams/t1/channels/k1/filesFolder": {"id": "f1", "parentReference": {"driveId": "d1"}},
        f"{GRAPH}/teams/t1/channels/k2/filesFolder": {"id": "f2", "parentReference": {"driveId": "d1"}},
        f"{GRAPH}/teams/t1/channels/k3/filesFolder": {"id": "f3", "parentReference": {"driveId": "d2"}},
        f"{GRAPH}/drives/d1/items/f1?$select=id,name,parentReference,root,webUrl": {
            "id": "f1", "name": "Allgemein", "webUrl": "https://x/sites/r/Shared%20Documents/Allgemein",
            "parentReference": {"driveId": "d1", "path": "/drives/d1/root:"}},
        f"{GRAPH}/drives/d1/items/f2?$select=id,name,parentReference,root,webUrl": {
            "id": "f2", "name": "Projekt X", "webUrl": "https://x/sites/r/Shared%20Documents/Projekt%20X",
            "parentReference": {"driveId": "d1", "path": "/drives/d1/root:"}},
        f"{GRAPH}/drives/d2/items/f3?$select=id,name,parentReference,root,webUrl": {
            "id": "f3", "name": "root", "root": {}, "webUrl": "https://x/sites/r-geheim/Shared%20Documents",
            "parentReference": {"driveId": "d2"}},
    }
    graph = FakeGraph(gets=gets)
    laeufe = []

    def fake_lauf(g, ziel, auswahl, arbeiter, still=False, zustand=None):
        laeufe.append((g.drive_base, ziel, auswahl))
        return {"new": 2, "excluded": 1, "errors": 0, "moved": 0, "gone": 0}

    monkeypatch.setattr(drive_mirror, "lauf", fake_lauf)
    jobs = [("channel", team, allgemein), ("channel", team, projekt), ("channel", team, privat)]
    spiegel, summe = te.kanal_dateien_spiegeln(graph, tmp_path, jobs)
    assert summe == {"new": 4, "excluded": 2, "errors": 0, "moved": 0, "gone": 0}
    assert len(laeufe) == 2
    basis1, ziel1, wahl1 = laeufe[0]
    assert basis1.endswith("/drives/d1") and ziel1 == tmp_path / "channels" / "Team Rakete"
    assert wahl1.im_scope("Dateien/Allgemein/a.pdf") and wahl1.im_scope("Dateien/Projekt X/b.pdf")
    assert not wahl1.im_scope("Dateien/Forms/c.pdf")
    basis2, ziel2, wahl2 = laeufe[1]
    assert basis2.endswith("/drives/d2")
    assert ziel2 == tmp_path / "channels" / "Team Rakete" / f"Geheim__{te.short_id('k3')}"
    assert wahl2.im_scope("Dateien/irgendwas/x.pdf")
    assert spiegel["k1"] == {"wurzel": "channels/Team Rakete", "praefix": "", "rel": "Dateien/Allgemein",
                             "weburl": "https://x/sites/r/Shared Documents/Allgemein"}
    assert spiegel["k3"]["praefix"] == f"Geheim__{te.short_id('k3')}" and spiegel["k3"]["rel"] == "Dateien"


def test_kanal_ohne_dateiordner_kostet_die_anderen_nichts(tmp_path, monkeypatch, capsys):
    import drive_mirror
    team = {"id": "t1", "displayName": "Team Rakete"}
    ok = {"id": "k1", "displayName": "Allgemein", "membershipType": "standard"}
    kaputt = {"id": "k9", "displayName": "Kaputt", "membershipType": "standard"}
    gets = {
        f"{GRAPH}/teams/t1/channels/k1/filesFolder": {"id": "f1", "parentReference": {"driveId": "d1"}},
        f"{GRAPH}/drives/d1/items/f1?$select=id,name,parentReference,root,webUrl": {
            "id": "f1", "name": "Allgemein", "webUrl": "https://x/s/Shared%20Documents/Allgemein",
            "parentReference": {"driveId": "d1", "path": "/drives/d1/root:"}},
    }
    graph = FakeGraph(gets=gets)
    monkeypatch.setattr(drive_mirror, "lauf", lambda *a, **k: {"new": 1, "excluded": 0, "errors": 0, "moved": 0, "gone": 0})
    spiegel, summe = te.kanal_dateien_spiegeln(
        graph, tmp_path, [("channel", team, kaputt), ("channel", team, ok)])
    assert set(spiegel) == {"k1"} and summe["new"] == 1
    ereignisse = [progress.lies_event(z) for z in capsys.readouterr().out.splitlines()]
    assert any(e and e["k"] == "run.teams.files_failed" and "Kaputt" in e["v"]["name"]
               for e in ereignisse)


def test_run_parallel_summiert_dateizahlen():
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0,
             "files": 0, "excluded": 0, "file_errors": 0, "gone": 0}
    runners = [lambda: ("new", "1on1", "A", 1, 0.1, {"files": 2, "excluded": 1, "file_errors": 0}),
               lambda: ("updated", "channels", "B", 1, 0.1, {"files": 1, "excluded": 0, "file_errors": 1})]
    assert te.run_parallel(runners, stats, workers=2) == "done"
    assert (stats["files"], stats["excluded"], stats["file_errors"]) == (3, 1, 1)
