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
        self.get_calls = []        # (url, params) of every single get

    def paged(self, url, params=None):
        self.paged_calls.append(url)
        val = self.pages[url]
        if isinstance(val, Exception):
            raise val
        yield from val

    def get(self, url, params=None):
        self.get_calls.append((url, params))
        val = self.gets[url]
        if isinstance(val, Exception):
            raise val
        return val


def _http_error(status):
    """An HTTPError the way requests raises it – with the status on it."""
    antwort = FakeResponse(status)
    err = requests.HTTPError(f"HTTP {status}")
    err.response = antwort
    return err


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
        # Every Graph message carries an id – the store keys on it; a
        # stable one from the content keeps the fixtures short.
        "id": te.short_id(f"{name}|{text}|{ts}"),
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
# Progress: load_state / record_done / already_done / get_record / cleanup_old
# --------------------------------------------------------------------------
def test_load_state_defaults_and_roundtrip(tmp_path):
    state = te.load_state(tmp_path)
    assert state == {"version": 1, "conversations": {}}
    te.record_done(tmp_path, state, "k", "1on1", "Alice", "1on1/a.html", 1)
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
    """One row per conversation in the records area – no blob any more."""
    state = te.load_state(tmp_path)
    te.record_done(tmp_path, state, "k1", "1on1", "Alice", "1on1/a.html", 7,
                   last_activity="2025-06-01T09:30:00Z")
    import state_db
    db = state_db.StateDb(tmp_path)
    rec = json.loads(db.satz_lesen("conversations", "k1"))
    assert rec["category"] == "1on1" and rec["title"] == "Alice"
    assert rec["rel"] == "1on1/a.html" and rec["count"] == 7
    assert rec["done"] is True and rec["empty"] is False
    assert rec["last_activity"] == "2025-06-01T09:30:00Z"
    assert db.kv_lesen("state") is None
    # a second record leaves the first row untouched
    te.record_done(tmp_path, state, "k2", "group", "Bob", "group/b.html", 1)
    assert set(db.saetze_lesen("conversations")) == {"k1", "k2"}
    assert te.load_state(tmp_path)["conversations"]["k1"]["title"] == "Alice"


def test_load_state_carries_the_old_blob_into_rows(tmp_path):
    """An archive from before 9.0: the kv blob is read once and becomes
    rows, so nothing is exported twice after the upgrade."""
    import state_db
    db = state_db.StateDb(tmp_path)
    db.kv_schreiben("state", json.dumps({"version": 1, "conversations": {
        "alt": {"done": True, "rel": "1on1/alt.html", "last_activity": "2025-06-01T00:00:00Z"}}}))
    state = te.load_state(tmp_path)
    assert state["conversations"]["alt"]["rel"] == "1on1/alt.html"
    assert set(db.saetze_lesen("conversations")) == {"alt"}
    # rows present -> the blob is not consulted again
    db.kv_schreiben("state", json.dumps({"version": 1, "conversations": {"anders": {}}}))
    assert set(te.load_state(tmp_path)["conversations"]) == {"alt"}


# --------------------------------------------------------------------------
# Exporting a conversation (chat and channel) with a faked Graph
# --------------------------------------------------------------------------
def _chat_fixture(chat_id="c1", danach=()):
    """A chat with two messages; `danach` is what a later, incremental
    fetch (filtered at the newest stored instant) brings."""
    chat = {"id": chat_id, "chatType": "oneOnOne",
            "members": [{"userId": "me", "displayName": "Ich"},
                        {"userId": "u2", "displayName": "Alice Example"}]}
    msgs = [_msg("Alice Example", "zweite", "2025-06-02T08:00:00Z"),
            _msg("Ich", "erste", "2025-06-01T09:30:00Z")]
    graph = FakeGraph(pages={f"{GRAPH}/me/chats/{chat_id}/messages": msgs,
                             te.chat_delta_url(chat_id, "2025-06-02T08:00:00.000Z"): list(danach)})
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


KANAL = f"{GRAPH}/teams/t1/channels/k1/messages"
DELTA = f"{KANAL}/delta"
LINK1 = f"{DELTA}?$deltatoken=d1"
LINK2 = f"{DELTA}?$deltatoken=d2"


def _delta_antworten(geaendert=(), link=LINK1):
    """What the delta endpoint answers: the initial call (after a full
    read) and the stored link both hand back `link`."""
    return {DELTA: {"value": [], "@odata.deltaLink": link},
            LINK1: {"value": list(geaendert), "@odata.deltaLink": link}}


def _channel_fixture(reply_ts="2025-06-05T10:00:00Z", geaendert=False, root_edit=None):
    """A channel with one post and one reply. With `geaendert` the stored
    delta link reports the post as changed, and the replies listing brings
    the reply with `reply_ts` – how a later run learns about it."""
    team = {"id": "t1", "displayName": "Team Rakete"}
    ch = {"id": "k1", "displayName": "Allgemein", "membershipType": "standard"}
    root = _msg("Alice", root_edit or "Wurzelpost", "2025-06-04T09:00:00Z", id="r1")
    reply = _msg("Bob", "Antwort", reply_ts, id="a1", replyToId="r1")
    root["replies"] = [reply]
    pages = {KANAL: [root], f"{KANAL}/r1/replies": [reply]}
    gets = _delta_antworten(geaendert=[root] if geaendert else (),
                            link=LINK2 if geaendert else LINK1)
    return team, ch, FakeGraph(pages=pages, gets=gets)


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
    # the delta reports the post; its replies listing carries the newer reply
    team, ch, graph = _channel_fixture(reply_ts="2025-06-06T12:00:00Z", geaendert=True)
    status, _, _, _, _, _z = te.export_one_channel(graph, tmp_path, state, team, ch)
    assert status == "updated"
    assert state["conversations"]["ch:k1"]["last_activity"] == "2025-06-06T12:00:00Z"
    # the named post's replies, then the one page of newest threads –
    # never the whole channel
    assert graph.paged_calls == [f"{KANAL}/r1/replies", KANAL]


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
    lookup (one batch per conversation) and the content stream."""

    def __init__(self, pages=None, gets=None, ctag="c-1", size=3, kaputt=False):
        super().__init__(pages, gets)
        self.ctag, self.size, self.kaputt = ctag, size, kaputt
        self.geladen = []
        self.gefragt = []      # the metadata URLs, one list per batch

    def batch_get(self, urls, extra_headers=None):
        urls = list(urls)
        self.gefragt.append(urls)
        if self.kaputt:
            return {u: (403, {"error": {"code": "accessDenied"}}) for u in urls}
        return {u: (200, {"name": "Angebot.pdf", "cTag": self.ctag, "size": self.size})
                for u in urls}

    def stream(self, url, timeout=None, label=""):
        self.geladen.append(url)
        return _StreamAntwort(b"PDF")


DATEI_URL = "https://firma.sharepoint.com/sites/x/Freigegebene%20Dokumente/Angebot.pdf"


def _chat_mit_datei(chat_id="c1", danach=(), **graph_kw):
    chat = {"id": chat_id, "chatType": "oneOnOne",
            "members": [{"userId": "me", "displayName": "Ich"},
                        {"userId": "u2", "displayName": "Alice Example"}]}
    msgs = [_msg("Alice Example", "hier die Datei", "2025-06-02T08:00:00Z",
                 attachments=[{"id": "a1", "contentType": "reference",
                               "contentUrl": DATEI_URL, "name": "Angebot.pdf"},
                              {"id": "a2", "contentType": "application/vnd.microsoft.card.adaptive",
                               "content": "{}"}])]
    graph = _DateiGraph(pages={f"{GRAPH}/me/chats/{chat_id}/messages": msgs,
                               te.chat_delta_url(chat_id, "2025-06-02T08:00:00.000Z"): list(danach)},
                        **graph_kw)
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

    # Second run with a new message pointing at the same file: its
    # metadata is asked once more (one batch), same cTag -> not fetched.
    neu = _msg("Ich", "nochmal", "2025-06-03T08:00:00Z",
               attachments=[{"id": "a3", "contentType": "reference",
                             "contentUrl": DATEI_URL, "name": "Angebot.pdf"}])
    chat2, graph2 = _chat_mit_datei(danach=[neu])
    te.export_one_chat(graph2, tmp_path, state, "me", chat2)
    assert len(graph2.gefragt) == 1 and graph2.geladen == []
    html = (tmp_path / rel).read_text(encoding="utf-8")
    assert "hier die Datei" in html and "nochmal" in html


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
    graph = _DateiGraph(pages={KANAL: [root]}, gets=_delta_antworten())
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
                             "weburl": "https://x/sites/r/Shared Documents/Allgemein", "neu": False}
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


# --------------------------------------------------------------------------
# 9.0: the message store, category cadences, rules, the start day
# --------------------------------------------------------------------------
def _events(capsys):
    return [e for e in (progress.lies_event(z) for z in capsys.readouterr().out.splitlines()) if e]


def test_nachrichtenspeicher_merges_replaces_and_persists(tmp_path):
    import state_db
    db = state_db.StateDb(tmp_path)
    m1 = _msg("Alice", "eins", "2025-06-01T09:00:00Z", id="1")
    m2 = _msg("Bob", "zwei", "2025-06-02T09:00:00Z", id="2",
              lastModifiedDateTime="2025-06-02T09:05:00Z")
    sp = te.Nachrichtenspeicher(db, "c1")
    assert sp.leer() and not sp.geaendert()
    sp.merge([m1, m2])
    assert sp.geaendert() and len(sp) == 2
    sp.sichern()
    assert set(db.saetze_lesen("msgs:c1")) == {"1", "2"}

    sp2 = te.Nachrichtenspeicher(db, "c1")
    assert len(sp2) == 2 and not sp2.geaendert()
    sp2.merge([m1])                              # identical -> no change
    assert not sp2.geaendert()
    assert sp2.wasserzeichen() == "2025-06-02T09:05:00Z"   # lastModified beats created
    sp2.ersetze([m1])                            # a full read without m2 -> gone
    assert sp2.geaendert()
    sp2.sichern()
    assert set(db.saetze_lesen("msgs:c1")) == {"1"}
    assert [m["id"] for m in te.Nachrichtenspeicher(db, "c1").nachrichten()] == ["1"]


def test_nachrichtenspeicher_ersetze_antworten_drops_vanished_replies(tmp_path):
    import state_db
    sp = te.Nachrichtenspeicher(state_db.StateDb(tmp_path), "k1")
    root = _msg("Alice", "Wurzel", "2025-06-01T09:00:00Z", id="r1")
    a1 = _msg("Bob", "eins", "2025-06-01T10:00:00Z", id="a1", replyToId="r1")
    a2 = _msg("Bob", "zwei", "2025-06-01T11:00:00Z", id="a2", replyToId="r1")
    sp.merge([root, a1, a2])
    a3 = _msg("Carol", "drei", "2025-06-01T12:00:00Z", id="a3", replyToId="r1")
    sp.ersetze_antworten("r1", [a1, a3])
    assert {m["id"] for m in sp.nachrichten()} == {"r1", "a1", "a3"}


# --- category cadences ------------------------------------------------------
def test_faellige_kategorien_skips_below_cadence(tmp_path, monkeypatch, capsys):
    import state_db
    import time
    db = state_db.StateDb(tmp_path)
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"teams:1on1": "daily",
                                                   "teams:channels": "weekly"}))
    monkeypatch.delenv("SYNC_NOW", raising=False)
    db.kv_schreiben("last_sync:1on1", str(time.time()))                 # just now
    db.kv_schreiben("last_sync:channels", str(time.time() - 8 * 86400))  # a week ago
    faellig, n = te.faellige_kategorien(db, {"1on1", "group", "channels"})
    assert faellig == {"group", "channels"} and n == 1
    skips = [e for e in _events(capsys) if e["k"] == "run.cadence.skip"]
    assert len(skips) == 1
    assert skips[0]["v"]["name"] == {"k": "export.cat.1on1", "v": {}}
    assert skips[0]["v"]["cadence"] == {"k": "cadence.daily", "v": {}}


def test_faellige_kategorien_sync_now_lifts_every_gate(tmp_path, monkeypatch, capsys):
    import state_db
    import time
    db = state_db.StateDb(tmp_path)
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"teams:group": "monthly"}))
    monkeypatch.setenv("SYNC_NOW", "1")
    db.kv_schreiben("last_sync:group", str(time.time()))
    assert te.faellige_kategorien(db, {"group"}) == ({"group"}, 0)
    assert not [e for e in _events(capsys) if e["k"] == "run.cadence.skip"]


def test_kadenz_abschliessen_marks_only_clean_categories(tmp_path):
    import state_db
    db = state_db.StateDb(tmp_path)
    te.kadenz_abschliessen(db, {"1on1", "group"}, {"group"})
    assert db.kv_lesen("last_sync:1on1") and db.kv_lesen("last_sync:group") is None
    te.kadenz_abschliessen(db, {"meeting"}, {"*"})       # a failure nobody could place
    assert db.kv_lesen("last_sync:meeting") is None


def test_make_runner_and_run_parallel_collect_failed_categories(monkeypatch, tmp_path):
    def boom(*a, **kw):
        raise ValueError("kaputt")
    monkeypatch.setattr(te, "export_one_chat", boom)
    fehler = set()
    run = te.make_runner("g", tmp_path, {}, "me", "chat", {"id": "c", "chatType": "group"},
                         None, fehler=fehler)
    assert run()[0] == "error" and fehler == {"group"}
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    runners = [lambda: ("new", "1on1", "A", 1, 0.1, {"files": 0, "excluded": 0, "file_errors": 1})]
    te.run_parallel(runners, stats, workers=1, fehler=fehler)
    assert fehler == {"group", "1on1"}      # a file that failed keeps the mark off too


# --- rules and the start day -----------------------------------------------
def _chat_eintrag(cid, ctype, name=None, topic=None, ts="2025-06-05T00:00:00Z"):
    c = {"id": cid, "chatType": ctype, "topic": topic,
         "lastMessagePreview": {"createdDateTime": ts}}
    if name:
        c["members"] = [{"userId": "me", "displayName": "Ich"},
                        {"userId": "u2", "displayName": name}]
    return c


def test_build_chat_jobs_rules_exclude_by_path(tmp_path):
    import folders
    chats = [_chat_eintrag("a", "oneOnOne", name="Alice Beispiel"),
             _chat_eintrag("b", "group", topic="Nordwind"),
             _chat_eintrag("c", "meeting", topic="Standup")]
    graph = FakeGraph(pages={f"{GRAPH}/me/chats": chats})
    regeln = folders.lies_regeln("- 1on1/Alice*\n- meeting/**")
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_chat_jobs(graph, tmp_path, te.load_state(tmp_path), stats, "me",
                              {"1on1", "group", "meeting"}, regeln=regeln)
    assert [c["id"] for _k, c, _ in jobs] == ["b"]
    assert stats["excluded"] == 2 and stats["skipped"] == 0


def test_build_channel_jobs_rules_exclude_and_leave_the_file_alone(monkeypatch, tmp_path):
    import folders
    monkeypatch.setattr(te, "REFRESH_CHANNELS", True)
    graph = FakeGraph(pages={f"{GRAPH}/teams/t1/channels": [
        {"id": "k1", "displayName": "Allgemein"}, {"id": "k2", "displayName": "Geheim"}]})
    state = {"version": 1, "conversations": {
        "ch:k2": {"done": True, "rel": "channels/Nordwind/Geheim__x.html"}}}
    alt = tmp_path / "channels" / "Nordwind" / "Geheim__x.html"
    alt.parent.mkdir(parents=True)
    alt.write_text("bleibt", encoding="utf-8")
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_channel_jobs(graph, tmp_path, state, stats,
                                 [{"id": "t1", "displayName": "Nordwind"}],
                                 regeln=folders.lies_regeln("- channels/Nordwind/Geheim"))
    assert [ch["id"] for _k, _t, ch in jobs] == ["k1"]
    assert stats["excluded"] == 1
    assert alt.read_text(encoding="utf-8") == "bleibt"


def test_build_channel_jobs_reports_a_failed_team(tmp_path):
    graph = FakeGraph(pages={f"{GRAPH}/teams/t2/channels": RuntimeError("keine Rechte")})
    fehler = set()
    te.build_channel_jobs(graph, tmp_path, te.load_state(tmp_path),
                          {"new": 0, "updated": 0, "skipped": 0, "empty": 0},
                          [{"id": "t2", "displayName": "T2"}], fehler=fehler)
    assert fehler == {"channels"}


def test_seit_grenze_reads_the_day(monkeypatch):
    from datetime import datetime, UTC
    monkeypatch.setenv("TEAMS_SINCE", "2025-06-02")
    assert te.seit_grenze() == datetime(2025, 6, 2, tzinfo=UTC)
    monkeypatch.setenv("TEAMS_SINCE", " ")
    assert te.seit_grenze() is None
    monkeypatch.setenv("TEAMS_SINCE", "gestern")
    assert te.seit_grenze() is None
    monkeypatch.delenv("TEAMS_SINCE")
    assert te.seit_grenze() is None          # the default is empty


def test_build_chat_jobs_since_leaves_old_chats_out_without_a_request(tmp_path, monkeypatch):
    from datetime import datetime, UTC
    monkeypatch.setattr(te, "SEIT", datetime(2025, 6, 3, tzinfo=UTC))
    chats = [_chat_eintrag("alt", "group", topic="Altes", ts="2025-06-01T00:00:00Z"),
             _chat_eintrag("neu", "group", topic="Neues", ts="2025-06-05T00:00:00Z")]
    graph = FakeGraph(pages={f"{GRAPH}/me/chats": chats})
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_chat_jobs(graph, tmp_path, te.load_state(tmp_path), stats, "me", {"group"})
    assert [c["id"] for _k, c, _ in jobs] == ["neu"]
    assert stats["excluded"] == 1


def test_export_one_chat_first_fetch_is_bounded_by_since(tmp_path, monkeypatch):
    from datetime import datetime, UTC
    monkeypatch.setattr(te, "SEIT", datetime(2025, 6, 2, tzinfo=UTC))
    chat, graph = _chat_fixture(danach=[_msg("Ich", "dritte", "2025-06-03T08:00:00Z")])
    state = te.load_state(tmp_path)
    status, _f, _t, count, _s, _z = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert status == "new" and count == 1
    html = (tmp_path / state["conversations"]["c1"]["rel"]).read_text(encoding="utf-8")
    assert "zweite" in html and "erste" not in html
    # later fetches are incremental – the day no longer matters
    status, _f, _t, count, _s, _z = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert status == "updated" and count == 2


def test_export_one_chat_since_spares_an_archive_from_before(tmp_path, monkeypatch):
    """A record without a store (8.x) fetches its history in full – the
    day would otherwise cut what is already on disk."""
    from datetime import datetime, UTC
    monkeypatch.setattr(te, "SEIT", datetime(2025, 6, 2, tzinfo=UTC))
    chat, graph = _chat_fixture()
    state = te.load_state(tmp_path)
    state["conversations"]["c1"] = {"done": True, "rel": "1on1/weg.html", "count": 2,
                                    "empty": False, "last_activity": "2025-06-01T09:30:00Z"}
    status, _f, _t, count, _s, _z = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert count == 2


def test_export_one_channel_since_leaves_old_threads_out(tmp_path, monkeypatch):
    from datetime import datetime, UTC
    monkeypatch.setattr(te, "SEIT", datetime(2025, 6, 10, tzinfo=UTC))
    team, ch, graph = _channel_fixture()
    status, _c, _t, count, _s, _z = te.export_one_channel(graph, tmp_path, te.load_state(tmp_path),
                                                          team, ch)
    assert status == "new" and count == 0
    monkeypatch.setattr(te, "SEIT", datetime(2025, 6, 4, tzinfo=UTC))   # the thread's day
    team, ch, graph = _channel_fixture()
    status, _c, _t, count, _s, _z = te.export_one_channel(graph, tmp_path, te.load_state(tmp_path / "b"),
                                                          team, ch)
    assert count == 2


# --- the conversation list (--teams) ----------------------------------------
def _listen_graph(kanaele=("Allgemein",), channels_enabled=True):
    chats = [_chat_eintrag("a", "oneOnOne", name="Alice Beispiel"),
             _chat_eintrag("b", "group", topic="Nordwind"),
             _chat_eintrag("c", "meeting", topic="Standup"),
             _chat_eintrag("d", "unknownType", topic="fremd")]
    graph = FakeGraph(pages={f"{GRAPH}/me/chats": chats,
                             f"{GRAPH}/me/joinedTeams": [{"id": "t1", "displayName": "Team Rakete"}],
                             f"{GRAPH}/teams/t1/channels": [
                                 {"id": f"k{i}", "displayName": n} for i, n in enumerate(kanaele)]},
                      gets={f"{GRAPH}/me": {"id": "me"}})
    graph.channels_enabled = channels_enabled
    return graph


def test_gleiche_liste_ab_writes_the_tree_and_reports_new_entries(tmp_path, capsys):
    import folders
    te.gleiche_liste_ab(_listen_graph(), tmp_path)
    baum = folders.lade(tmp_path)
    assert [e["pfad"] for e in baum["ordner"]] == [
        "1on1/Alice Beispiel", "group/Nordwind", "meeting/Standup",
        "channels/Team Rakete/Allgemein"]
    assert [e["id"] for e in baum["ordner"]] == ["a", "b", "c", "k0"]
    assert all(e["elemente"] == 0 for e in baum["ordner"])
    assert baum["neu"] == []                       # the first sync knows no "new"
    assert not (tmp_path / "1on1").exists()        # nothing exported
    zeilen = capsys.readouterr().out.splitlines()
    ereignisse = [e for e in (progress.lies_event(z) for z in zeilen) if e]
    ergebnis = [r for r in (progress.lies_ergebnis(z) for z in zeilen) if r]
    treffer = [e for e in ereignisse if e["k"] == "run.sync.result"]
    assert treffer and treffer[0]["v"]["total"] == 4 and treffer[0]["v"]["chosen"] == 4
    assert treffer[0]["v"]["unit"] == {"k": "progress.unit.conversations", "v": {}}
    assert ergebnis[-1]["new"] == 0

    te.gleiche_liste_ab(_listen_graph(kanaele=("Allgemein", "Projekt X")), tmp_path)
    baum = folders.lade(tmp_path)
    assert baum["neu"] == ["channels/Team Rakete/Projekt X"]
    zeilen = capsys.readouterr().out.splitlines()
    ergebnis = [r for r in (progress.lies_ergebnis(z) for z in zeilen) if r]
    assert ergebnis[-1]["new"] == 1 and ergebnis[-1]["extra"]["total"] == 5


def test_gleiche_liste_ab_without_channel_access_lists_chats_only(tmp_path):
    import folders
    te.gleiche_liste_ab(_listen_graph(channels_enabled=False), tmp_path)
    assert [e["pfad"] for e in folders.lade(tmp_path)["ordner"]] == [
        "1on1/Alice Beispiel", "group/Nordwind", "meeting/Standup"]


# --- channels by delta ------------------------------------------------------
def test_channel_delta_link_is_stored_only_after_a_clean_finish(tmp_path, monkeypatch):
    import state_db
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    db = state_db.StateDb(tmp_path)
    echt, kaputt = te.render_conversation, [True]

    def wackelig(*a, **kw):
        if kaputt[0]:
            raise OSError("Platte voll")
        return echt(*a, **kw)
    monkeypatch.setattr(te, "render_conversation", wackelig)
    with pytest.raises(OSError):
        te.export_one_channel(graph, tmp_path, state, team, ch)
    assert db.kv_lesen("delta:k1") is None
    assert db.saetze_lesen("msgs:k1") == {}      # the store waits for the clean finish too
    kaputt[0] = False
    status, _c, _t, count, _s, _z = te.export_one_channel(graph, tmp_path, state, team, ch)
    assert status == "new" and count == 2
    assert db.kv_lesen("delta:k1") == LINK1
    assert set(db.saetze_lesen("msgs:k1")) == {"r1", "a1"}
    # the first read: the listing with replies inline, then one delta call
    assert graph.paged_calls == [KANAL, KANAL]


def test_channel_delta_merges_an_edit_and_a_new_reply(tmp_path):
    import state_db
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch)

    root2 = _msg("Alice", "Wurzelpost (bearbeitet)", "2025-06-04T09:00:00Z", id="r1",
                 lastModifiedDateTime="2025-06-07T09:00:00Z")
    alt = _msg("Bob", "Antwort", "2025-06-05T10:00:00Z", id="a1", replyToId="r1")
    neu = _msg("Carol", "Noch eine Antwort", "2025-06-07T10:00:00Z", id="a2", replyToId="r1")
    graph2 = FakeGraph(pages={f"{KANAL}/r1/replies": [alt, neu], KANAL: []},
                       gets={LINK1: {"value": [root2], "@odata.deltaLink": LINK2}})
    status, _c, _t, count, _s, _z = te.export_one_channel(graph2, tmp_path, state, team, ch)
    assert status == "updated" and count == 3
    assert graph2.paged_calls == [f"{KANAL}/r1/replies", KANAL]
    html = (tmp_path / state["conversations"]["ch:k1"]["rel"]).read_text(encoding="utf-8")
    assert "Wurzelpost (bearbeitet)" in html and "Noch eine Antwort" in html
    assert html.index("Antwort</div>") < html.index("Noch eine Antwort")
    db = state_db.StateDb(tmp_path)
    assert db.kv_lesen("delta:k1") == LINK2
    assert set(db.saetze_lesen("msgs:k1")) == {"r1", "a1", "a2"}

    # nothing since -> unchanged; only the page of newest threads is read
    graph3 = FakeGraph(pages={KANAL: []}, gets={LINK2: {"value": [], "@odata.deltaLink": LINK2}})
    status, _c, _t, count, _s, _z = te.export_one_channel(graph3, tmp_path, state, team, ch)
    assert status == "unchanged" and count == 3 and graph3.paged_calls == [KANAL]


def test_channel_delta_410_restarts_with_one_full_read(tmp_path, capsys):
    import state_db
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch)
    capsys.readouterr()

    root = _msg("Alice", "Wurzelpost", "2025-06-04T09:00:00Z", id="r1")
    root["replies"] = [_msg("Bob", "Antwort", "2025-06-05T10:00:00Z", id="a1", replyToId="r1"),
                       _msg("Carol", "Späte Antwort", "2025-06-09T10:00:00Z", id="a2", replyToId="r1")]
    graph2 = FakeGraph(pages={KANAL: [root]},
                       gets={LINK1: _http_error(410),
                             DELTA: {"value": [], "@odata.deltaLink": LINK2}})
    status, _c, _t, count, _s, _z = te.export_one_channel(graph2, tmp_path, state, team, ch)
    assert status == "updated" and count == 3
    assert graph2.paged_calls == [KANAL]
    assert state_db.StateDb(tmp_path).kv_lesen("delta:k1") == LINK2
    resets = [e for e in _events(capsys) if e["k"] == "run.teams.delta_reset"]
    assert len(resets) == 1 and resets[0]["v"]["name"] == "Team Rakete / Allgemein"


def test_channel_delta_refused_keeps_the_export_and_says_so(tmp_path, capsys):
    import state_db
    team, ch, graph = _channel_fixture()
    graph.gets[DELTA] = _http_error(400)
    state = te.load_state(tmp_path)
    status, _c, _t, count, _s, _z = te.export_one_channel(graph, tmp_path, state, team, ch)
    assert status == "new" and count == 2
    assert (tmp_path / state["conversations"]["ch:k1"]["rel"]).exists()
    assert state_db.StateDb(tmp_path).kv_lesen("delta:k1") is None
    assert [e["k"] for e in _events(capsys) if e["k"].startswith("run.teams.delta")] == \
        ["run.teams.delta_failed"]


def test_channel_delta_other_errors_fail_the_channel(tmp_path):
    import state_db
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch)
    graph2 = FakeGraph(gets={LINK1: _http_error(503)})
    with pytest.raises(requests.HTTPError):
        te.export_one_channel(graph2, tmp_path, state, team, ch)
    assert state_db.StateDb(tmp_path).kv_lesen("delta:k1") == LINK1   # the old link stands


# --- chats incrementally ----------------------------------------------------
def test_export_one_chat_incremental_fetch_uses_the_watermark(tmp_path):
    import state_db
    chat, graph = _chat_fixture(danach=[_msg("Alice Example", "dritte", "2025-06-03T08:00:00Z")])
    state = te.load_state(tmp_path)
    te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert graph.paged_calls == [f"{GRAPH}/me/chats/c1/messages"]
    status, _f, _t, count, _s, _z = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert status == "updated" and count == 3
    url = graph.paged_calls[-1]
    assert "$filter=lastModifiedDateTime gt 2025-06-02T08:00:00.000Z" in url
    assert "$orderby=lastModifiedDateTime desc" in url
    html = (tmp_path / state["conversations"]["c1"]["rel"]).read_text(encoding="utf-8")
    assert html.index("erste") < html.index("zweite") < html.index("dritte")
    assert state["conversations"]["c1"]["last_activity"] == "2025-06-03T08:00:00Z"
    assert len(state_db.StateDb(tmp_path).saetze_lesen("msgs:c1")) == 3


def test_export_one_chat_incremental_edit_and_deletion(tmp_path):
    chat, graph = _chat_fixture()
    alt = graph.pages[f"{GRAPH}/me/chats/c1/messages"]
    bearbeitet = _msg("Alice Example", "zweite (neu)", "2025-06-02T08:00:00Z", id=alt[0]["id"],
                      lastModifiedDateTime="2025-06-04T08:00:00Z")
    geloescht = _msg("Ich", "erste", "2025-06-01T09:30:00Z", id=alt[1]["id"],
                     lastModifiedDateTime="2025-06-04T08:01:00Z",
                     deletedDateTime="2025-06-04T08:01:00Z")
    graph.pages[te.chat_delta_url("c1", "2025-06-02T08:00:00.000Z")] = [bearbeitet, geloescht]
    state = te.load_state(tmp_path)
    te.export_one_chat(graph, tmp_path, state, "me", chat)
    status, _f, _t, count, _s, _z = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert status == "updated" and count == 2
    html = (tmp_path / state["conversations"]["c1"]["rel"]).read_text(encoding="utf-8")
    assert "zweite (neu)" in html and "[gelöscht]" in html and ">erste<" not in html


# --- the per-channel file flag ----------------------------------------------
def _spiegel_gets():
    return {
        f"{GRAPH}/teams/t1/channels/k1/filesFolder": {"id": "f1", "parentReference": {"driveId": "d1"}},
        f"{GRAPH}/teams/t1/channels/k2/filesFolder": {"id": "f2", "parentReference": {"driveId": "d1"}},
        f"{GRAPH}/drives/d1/items/f1?$select=id,name,parentReference,root,webUrl": {
            "id": "f1", "name": "Allgemein", "webUrl": "https://x/sites/r/Shared%20Documents/Allgemein",
            "parentReference": {"driveId": "d1", "path": "/drives/d1/root:"}},
        f"{GRAPH}/drives/d1/items/f2?$select=id,name,parentReference,root,webUrl": {
            "id": "f2", "name": "Projekt X", "webUrl": "https://x/sites/r/Shared%20Documents/Projekt%20X",
            "parentReference": {"driveId": "d1", "path": "/drives/d1/root:"}},
    }


def test_kanal_dateien_spiegeln_flags_only_the_channel_whose_files_moved(tmp_path, monkeypatch):
    import drive_mirror
    import state_db
    team = {"id": "t1", "displayName": "Team Rakete"}
    jobs = [("channel", team, {"id": "k1", "displayName": "Allgemein", "membershipType": "standard"}),
            ("channel", team, {"id": "k2", "displayName": "Projekt X", "membershipType": "standard"})]
    aenderung = {}

    def fake_lauf(g, ziel, auswahl, arbeiter, still=False, zustand=None):
        if aenderung:
            state_db.StateDb(ziel).bestand_aktualisieren(aenderung)
        return {"new": len(aenderung), "excluded": 0, "errors": 0, "moved": 0, "gone": 0}
    monkeypatch.setattr(drive_mirror, "lauf", fake_lauf)

    aenderung.update({"f-a": {"rel": "Dateien/Allgemein/a.pdf", "ctag": "c1", "size": 1}})
    spiegel, _s = te.kanal_dateien_spiegeln(FakeGraph(gets=_spiegel_gets()), tmp_path, jobs)
    assert spiegel["k1"]["neu"] is True and spiegel["k2"]["neu"] is False
    aenderung.clear()                                  # a walk that brings nothing
    spiegel, _s = te.kanal_dateien_spiegeln(FakeGraph(gets=_spiegel_gets()), tmp_path, jobs)
    assert spiegel["k1"]["neu"] is False and spiegel["k2"]["neu"] is False
    aenderung.update({"f-a": {"rel": "Dateien/Allgemein/a.pdf", "ctag": "c2", "size": 1}})
    spiegel, _s = te.kanal_dateien_spiegeln(FakeGraph(gets=_spiegel_gets()), tmp_path, jobs)
    assert spiegel["k1"]["neu"] is True and spiegel["k2"]["neu"] is False   # content changed


def test_kanalpost_unveraendert_wird_nur_bei_neuen_dateien_geschrieben(tmp_path):
    """A mirror flag re-renders the channel although its posts did not
    move; without the flag the channel stays untouched."""
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    info = {"wurzel": "channels/Team Rakete", "praefix": "", "rel": "Dateien/Allgemein",
            "weburl": "https://x/sites/r/Shared Documents/Allgemein", "neu": False}
    te.export_one_channel(graph, tmp_path, state, team, ch, spiegel=info)
    assert te.export_one_channel(graph, tmp_path, state, team, ch, spiegel=info)[0] == "unchanged"
    info["neu"] = True
    assert te.export_one_channel(graph, tmp_path, state, team, ch, spiegel=info)[0] == "updated"


# --- attachment metadata in one batch ----------------------------------------
def test_anhaenge_metadata_comes_in_one_batch_and_is_skipped_when_unchanged(tmp_path):
    url2 = "https://firma.sharepoint.com/sites/x/Freigegebene%20Dokumente/Plan.pdf"
    msgs = [_msg("Alice Example", "eins", "2025-06-02T08:00:00Z",
                 attachments=[{"id": "a1", "contentType": "reference",
                               "contentUrl": DATEI_URL, "name": "Angebot.pdf"}]),
            _msg("Bob Baumeister", "zwei", "2025-06-02T09:00:00Z",
                 attachments=[{"id": "a2", "contentType": "reference",
                               "contentUrl": url2, "name": "Plan.pdf"},
                              {"id": "a3", "contentType": "reference",
                               "contentUrl": DATEI_URL, "name": "Angebot.pdf"}])]
    rel = "1on1/Alice Example__x.html"
    graph = _DateiGraph()
    lokal, geladen, ausgelassen, fehler = te.anhaenge_laden(graph, tmp_path, "c1", rel, msgs)
    assert len(graph.gefragt) == 1 and len(graph.gefragt[0]) == 2   # one batch, two files
    assert geladen == 2 and fehler == 0 and set(lokal) == {DATEI_URL, url2}

    # the conversation did not change: no question at all, the links come
    # from the record
    graph2 = _DateiGraph()
    lokal2, geladen2, _a, _f = te.anhaenge_laden(graph2, tmp_path, "c1", rel, msgs, unveraendert=True)
    assert graph2.gefragt == [] and geladen2 == 0 and lokal2 == lokal

    # a copy that vanished is asked about again even then
    (tmp_path / te.anhang_ordner(rel) / lokal[url2].rsplit("/", 1)[1]).unlink()
    graph3 = _DateiGraph()
    lokal3, geladen3, _a, _f = te.anhaenge_laden(graph3, tmp_path, "c1", rel, msgs, unveraendert=True)
    assert len(graph3.gefragt) == 1 and len(graph3.gefragt[0]) == 1 and geladen3 == 1
    assert lokal3 == lokal

    # with a changed conversation the cTag is asked again, the copy kept
    graph4 = _DateiGraph()
    _l, geladen4, _a, _f = te.anhaenge_laden(graph4, tmp_path, "c1", rel, msgs)
    assert len(graph4.gefragt[0]) == 2 and geladen4 == 0 and graph4.geladen == []


def test_anhaenge_batch_failure_is_one_line_per_file(tmp_path, capsys):
    class _Ausfall(_DateiGraph):
        def batch_get(self, urls, extra_headers=None):
            raise RuntimeError("Zu viele Fehlversuche")
    msgs = [_msg("Alice Example", "eins", "2025-06-02T08:00:00Z",
                 attachments=[{"id": "a1", "contentType": "reference",
                               "contentUrl": DATEI_URL, "name": "Angebot.pdf"}])]
    lokal, geladen, _a, fehler = te.anhaenge_laden(_Ausfall(), tmp_path, "c1", "1on1/A__x.html", msgs)
    assert lokal == {} and geladen == 0 and fehler == 1
    assert [e["k"] for e in _events(capsys)] == ["run.teams.file_failed"]


# --------------------------------------------------------------------------
# Review round: replies the delta never names, clean-category marks, bounds
# --------------------------------------------------------------------------
def _vor_tagen(tage):
    from datetime import datetime, timedelta, UTC
    return (datetime.now(UTC) - timedelta(days=tage)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _alter_thread(*weitere):
    """The fixture's thread, as the channel listing brings it, plus more replies."""
    root = _msg("Alice", "Wurzelpost", "2025-06-04T09:00:00Z", id="r1")
    root["replies"] = [_msg("Bob", "Antwort", "2025-06-05T10:00:00Z", id="a1", replyToId="r1"),
                       *weitere]
    return root


def test_channel_recent_threads_bring_replies_the_delta_never_names(tmp_path):
    import state_db
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch)

    # a reply from yesterday on the old, unchanged post: the delta names
    # nothing, the page of newest threads carries it
    neu = _msg("Carol", "Späte Antwort", _vor_tagen(1), id="a2", replyToId="r1")
    graph2 = FakeGraph(pages={KANAL: [_alter_thread(neu)]},
                       gets={LINK1: {"value": [], "@odata.deltaLink": LINK1}})
    status, _c, _t, count, _s, _z = te.export_one_channel(graph2, tmp_path, state, team, ch)
    assert status == "updated" and count == 3
    assert graph2.paged_calls == [KANAL]               # one page, no per-post calls
    html = (tmp_path / state["conversations"]["ch:k1"]["rel"]).read_text(encoding="utf-8")
    assert "Späte Antwort" in html
    assert set(state_db.StateDb(tmp_path).saetze_lesen("msgs:k1")) == {"r1", "a1", "a2"}

    # the same reply deleted today arrives the same way
    weg = dict(neu, deletedDateTime=_vor_tagen(0), lastModifiedDateTime=_vor_tagen(0))
    graph3 = FakeGraph(pages={KANAL: [_alter_thread(weg)]},
                       gets={LINK1: {"value": [], "@odata.deltaLink": LINK1}})
    status, _c, _t, count, _s, _z = te.export_one_channel(graph3, tmp_path, state, team, ch)
    assert status == "updated" and count == 3
    html = (tmp_path / state["conversations"]["ch:k1"]["rel"]).read_text(encoding="utf-8")
    assert "[gelöscht]" in html and "Späte Antwort" not in html


def test_channel_weekly_full_pass_brings_replies_on_old_threads(tmp_path):
    import state_db
    import time
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch)
    db = state_db.StateDb(tmp_path)
    assert float(db.kv_lesen("replies_full:k1")) > time.time() - 60   # the first read counts

    # a reply older than the window on an old post
    alt = _msg("Carol", "Alte Antwort", "2025-06-20T10:00:00Z", id="a2", replyToId="r1")

    def frisch():
        return FakeGraph(pages={KANAL: [_alter_thread(alt)]},
                         gets={LINK1: {"value": [], "@odata.deltaLink": LINK1},
                               DELTA: {"value": [], "@odata.deltaLink": LINK2}})

    # within the week: the newest-threads page stops at the old thread,
    # nothing else is fetched, nothing changes
    graph2 = frisch()
    status, _c, _t, count, _s, _z = te.export_one_channel(graph2, tmp_path, state, team, ch)
    assert status == "unchanged" and count == 2
    assert graph2.paged_calls == [KANAL]
    assert [u for u, _p in graph2.get_calls] == [LINK1]

    # a week later: the full pass reads the channel again and finds it
    db.kv_schreiben("replies_full:k1", str(time.time() - 8 * 86400))
    graph3 = frisch()
    status, _c, _t, count, _s, _z = te.export_one_channel(graph3, tmp_path, state, team, ch)
    assert status == "updated" and count == 3
    assert graph3.paged_calls == [KANAL]
    assert [u for u, _p in graph3.get_calls] == [DELTA]
    assert db.kv_lesen("delta:k1") == LINK2
    assert float(db.kv_lesen("replies_full:k1")) > time.time() - 60
    html = (tmp_path / state["conversations"]["ch:k1"]["rel"]).read_text(encoding="utf-8")
    assert "Alte Antwort" in html


def test_channel_initial_delta_is_bounded_at_the_listing_start(tmp_path):
    from datetime import datetime, timedelta, UTC
    team, ch, graph = _channel_fixture()          # its newest message is from 2025
    te.export_one_channel(graph, tmp_path, te.load_state(tmp_path), team, ch)
    (_url, params), = [c for c in graph.get_calls if c[0] == DELTA]
    assert params["$top"] == 50
    grenze = te.parse_ts(params["$filter"].split(" gt ")[1])
    # the listing's start less the skew margin – not the 2025 stamp
    assert timedelta(minutes=4, seconds=50) <= datetime.now(UTC) - grenze <= timedelta(minutes=5, seconds=30)


def test_channel_410_with_refused_delta_clears_the_link(tmp_path, capsys):
    import state_db
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch)
    db = state_db.StateDb(tmp_path)

    graph2 = FakeGraph(pages=dict(graph.pages),
                       gets={LINK1: _http_error(410), DELTA: _http_error(400)})
    status, _c, _t, count, _s, _z = te.export_one_channel(graph2, tmp_path, state, team, ch)
    assert status == "unchanged" and count == 2
    assert db.kv_lesen("delta:k1") == ""              # cleared, although nothing was written
    ereignisse = [e["k"] for e in _events(capsys)]
    assert "run.teams.delta_reset" in ereignisse and "run.teams.delta_failed" in ereignisse

    # the next run does not pay the 410 again
    graph3 = FakeGraph(pages=dict(graph.pages),
                       gets={DELTA: {"value": [], "@odata.deltaLink": LINK2}})
    te.export_one_channel(graph3, tmp_path, state, team, ch)
    assert [u for u, _p in graph3.get_calls] == [DELTA]
    assert db.kv_lesen("delta:k1") == LINK2


def test_select_teams_failure_marks_the_category():
    fehler = set()
    graph = FakeGraph(pages={f"{GRAPH}/me/joinedTeams": RuntimeError("kaputt")})
    assert te.select_teams(graph, fehler=fehler) == []
    assert fehler == {"channels"}


def test_kanal_dateien_spiegeln_scope_does_not_depend_on_listing_order(tmp_path, monkeypatch):
    import drive_mirror
    team = {"id": "t1", "displayName": "Team Rakete"}
    allgemein = ("channel", team, {"id": "k1", "displayName": "Allgemein", "membershipType": "standard"})
    projekt = ("channel", team, {"id": "k2", "displayName": "Projekt X", "membershipType": "standard"})
    kennzeichen = []

    def fake_lauf(g, ziel, auswahl, arbeiter, still=False, zustand=None):
        kennzeichen.append(auswahl.kennzeichen())
        return {"new": 0, "excluded": 0, "errors": 0, "moved": 0, "gone": 0}
    monkeypatch.setattr(drive_mirror, "lauf", fake_lauf)
    te.kanal_dateien_spiegeln(FakeGraph(gets=_spiegel_gets()), tmp_path, [allgemein, projekt])
    te.kanal_dateien_spiegeln(FakeGraph(gets=_spiegel_gets()), tmp_path, [projekt, allgemein])
    assert kennzeichen[0] == kennzeichen[1]


# --------------------------------------------------------------------------
# Cadences per team, channel and chat – gated per conversation
# --------------------------------------------------------------------------
def _stempel(db, keys, alter_s):
    import time
    for key in keys:
        db.kv_schreiben(f"last_sync:{key}", str(time.time() - alter_s))


def test_channel_override_runs_while_its_team_waits(tmp_path, monkeypatch, capsys):
    import state_db
    monkeypatch.delenv("SYNC_NOW", raising=False)
    monkeypatch.setattr(te, "REFRESH_CHANNELS", True)
    db = state_db.StateDb(tmp_path)
    kadenzen = {"teams:channels": "monthly", "teams:channels/Nordwind": "monthly",
                "teams:channels/Nordwind/Releases": "daily"}
    _stempel(db, ("channels", "k1", "k2", "k3"), 2 * 86400)   # two days: only daily is due
    # cadences below the category: it is listed although its own stamp is fresh
    assert te.faellige_kategorien(db, {"channels"}, kadenzen) == ({"channels"}, 0)
    takt = te.Taktung(db, kadenzen)
    graph = FakeGraph(pages={f"{GRAPH}/teams/t1/channels": [
        {"id": "k1", "displayName": "Allgemein"}, {"id": "k2", "displayName": "Releases"},
        {"id": "k3", "displayName": "Support"}]})
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_channel_jobs(graph, tmp_path, te.load_state(tmp_path), stats,
                                 [{"id": "t1", "displayName": "Nordwind"}], takt=takt)
    assert [ch["id"] for _k, _t, ch in jobs] == ["k2"]        # the team's cadence holds the rest
    takt.melden()
    ereignisse = _events(capsys)
    assert [e for e in ereignisse if e["k"] == "run.cadence.skip"] == []
    paced = [e["v"] for e in ereignisse if e["k"] == "run.teams.paced"]
    assert paced == [{"name": {"k": "export.cat.channels", "v": {}}, "n": 2}]
    assert takt.uebersprungen() == 2


def test_chat_override_is_skipped_although_the_category_is_due(tmp_path, monkeypatch, capsys):
    import state_db
    monkeypatch.delenv("SYNC_NOW", raising=False)
    db = state_db.StateDb(tmp_path)
    kadenzen = {"teams:group/Nordwind": "weekly"}            # the category itself: always
    _stempel(db, ("b",), 86400)                                # yesterday: weekly is not due
    chats = [_chat_eintrag("a", "group", topic="Alltag"), _chat_eintrag("b", "group", topic="Nordwind")]
    takt = te.Taktung(db, kadenzen)
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_chat_jobs(FakeGraph(pages={f"{GRAPH}/me/chats": chats}), tmp_path,
                              te.load_state(tmp_path), stats, "me", {"group"}, takt=takt)
    assert [c["id"] for _k, c, _ in jobs] == ["a"]
    takt.melden()
    paced = [e["v"] for e in _events(capsys) if e["k"] == "run.teams.paced"]
    assert paced == [{"name": {"k": "export.cat.group", "v": {}}, "n": 1}]


def test_category_without_overrides_skips_before_listing(tmp_path, monkeypatch, capsys):
    import state_db
    monkeypatch.delenv("SYNC_NOW", raising=False)
    db = state_db.StateDb(tmp_path)
    _stempel(db, ("group",), 60)
    assert te.faellige_kategorien(db, {"group"}, {"teams:group": "daily"}) == (set(), 1)
    skips = [e["v"] for e in _events(capsys) if e["k"] == "run.cadence.skip"]
    assert skips == [{"name": {"k": "export.cat.group", "v": {}},
                      "cadence": {"k": "cadence.daily", "v": {}}}]


def test_taktung_sync_now_runs_everything(tmp_path, monkeypatch):
    import state_db
    monkeypatch.setenv("SYNC_NOW", "1")
    db = state_db.StateDb(tmp_path)
    _stempel(db, ("a", "k1", "channels"), 60)
    kadenzen = {"teams:1on1": "monthly", "teams:1on1/Alice Beispiel": "monthly",
                "teams:channels": "monthly"}
    assert te.faellige_kategorien(db, {"1on1", "channels"}, kadenzen) == ({"1on1", "channels"}, 0)
    takt = te.Taktung(db, kadenzen)
    assert takt.faellig("1on1", "1on1/Alice Beispiel", "a")
    assert takt.faellig("channels", "channels/Nordwind/Allgemein", "k1")
    assert takt.uebersprungen() == 0


def test_taktung_says_the_category_line_when_nothing_is_due(tmp_path, monkeypatch, capsys):
    import state_db
    monkeypatch.delenv("SYNC_NOW", raising=False)
    db = state_db.StateDb(tmp_path)
    _stempel(db, ("m1", "m2"), 60)
    takt = te.Taktung(db, {"teams:meeting": "weekly", "teams:meeting/Standup": "daily"})
    assert not takt.faellig("meeting", "meeting/Standup", "m1")
    assert not takt.faellig("meeting", "meeting/Planung", "m2")
    takt.melden()
    ereignisse = _events(capsys)
    assert [e["k"] for e in ereignisse] == ["run.cadence.skip"]
    assert ereignisse[0]["v"]["name"] == {"k": "export.cat.meeting", "v": {}}
    assert ereignisse[0]["v"]["cadence"]["k"] in ("cadence.weekly", "cadence.daily")


def test_stamps_go_only_to_conversations_without_error(tmp_path, monkeypatch):
    import state_db
    db = state_db.StateDb(tmp_path)
    takt = te.Taktung(db, {})
    sauber = dict(te._KEINE_DATEIEN)

    def ok(*a, **kw):
        return ("new", "1on1", "A", 1, 0.1, sauber)

    def boom(*a, **kw):
        raise ValueError("kaputt")

    def datei_fehlt(*a, **kw):
        return ("updated", "1on1", "A", 1, 0.1, {"files": 0, "excluded": 0, "file_errors": 1})
    monkeypatch.setattr(te, "export_one_chat", ok)
    te.make_runner("g", tmp_path, {}, "me", "chat", {"id": "gut", "chatType": "oneOnOne"},
                   None, takt=takt)()
    monkeypatch.setattr(te, "export_one_chat", boom)
    te.make_runner("g", tmp_path, {}, "me", "chat", {"id": "kaputt", "chatType": "oneOnOne"},
                   None, takt=takt)()
    monkeypatch.setattr(te, "export_one_chat", datei_fehlt)
    te.make_runner("g", tmp_path, {}, "me", "chat", {"id": "datei", "chatType": "oneOnOne"},
                   None, takt=takt)()
    monkeypatch.setattr(te, "export_one_channel",
                        lambda *a, **kw: ("unchanged", "channels", "T / K", 2, 0.1, sauber))
    te.make_runner("g", tmp_path, {}, "me", "channel", {"id": "t1"}, {"id": "k1"}, takt=takt)()
    assert db.kv_lesen("last_sync:gut") and db.kv_lesen("last_sync:k1")
    assert db.kv_lesen("last_sync:kaputt") is None and db.kv_lesen("last_sync:datei") is None

    # a chat the listing finds unchanged is stamped as well
    state = te.load_state(tmp_path)
    state["conversations"]["alt"] = {"done": True, "rel": "group/alt.html", "count": 1,
                                     "empty": False, "last_activity": "2025-06-09T00:00:00Z"}
    (tmp_path / "group").mkdir()
    (tmp_path / "group" / "alt.html").write_text("x", encoding="utf-8")
    chats = [_chat_eintrag("alt", "group", topic="Altes", ts="2025-06-01T00:00:00Z")]
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_chat_jobs(FakeGraph(pages={f"{GRAPH}/me/chats": chats}), tmp_path, state,
                              stats, "me", {"group"}, takt=takt)
    assert jobs == [] and stats["skipped"] == 1
    assert db.kv_lesen("last_sync:alt")


def test_gleiche_liste_ab_entries_carry_the_last_activity(tmp_path):
    import folders
    te.gleiche_liste_ab(_listen_graph(), tmp_path)
    ordner = folders.lade(tmp_path)["ordner"]
    assert [e.get("zuletzt") for e in ordner] == ["2025-06-05T00:00:00Z"] * 3 + [None]


# --- "Force full sync": every conversation again, every file again ----------
def test_full_sync_nimmt_jeden_chat_und_kanal_mit(monkeypatch, tmp_path):
    monkeypatch.setenv("FULL_SYNC", "1")
    monkeypatch.setattr(te, "REFRESH_CHANNELS", False)
    chats = [{"id": "alt", "chatType": "meeting",
              "lastMessagePreview": {"createdDateTime": "2025-06-01T00:00:00Z"}}]
    graph = FakeGraph(pages={f"{GRAPH}/me/chats": chats,
                             f"{GRAPH}/teams/t1/channels": [{"id": "k1", "displayName": "A"}]})
    state = {"version": 1, "conversations": {
        "alt": {"done": True, "rel": "meeting/alt.html", "count": 1, "empty": False,
                "last_activity": "2025-06-02T00:00:00Z"},
        "ch:k1": {"done": True, "rel": "channels/T1/a.html"}}}
    for rel in ("meeting/alt.html", "channels/T1/a.html"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0}
    jobs = te.build_chat_jobs(graph, tmp_path, state, stats, "me", {"meeting"})
    assert [c["id"] for _k, c, _ in jobs] == ["alt"] and stats["skipped"] == 0
    jobs = te.build_channel_jobs(graph, tmp_path, state, stats,
                                 [{"id": "t1", "displayName": "T1"}])
    assert [ch["id"] for _k, _t, ch in jobs] == ["k1"] and stats["skipped"] == 0


def test_full_sync_liest_den_chat_ohne_wasserzeichen(tmp_path, monkeypatch):
    chat, graph = _chat_fixture(danach=[_msg("Alice Example", "dritte", "2025-06-03T08:00:00Z")])
    state = te.load_state(tmp_path)
    te.export_one_chat(graph, tmp_path, state, "me", chat)
    monkeypatch.setenv("FULL_SYNC", "1")
    status, _f, _t, count, _s, _z = te.export_one_chat(graph, tmp_path, state, "me", chat)
    assert graph.paged_calls[-1] == f"{GRAPH}/me/chats/c1/messages", \
        "listed in full, not from the watermark"
    assert status == "updated" and count == 2


def test_full_sync_liest_den_kanal_ohne_link(tmp_path, monkeypatch):
    """The stored link is not offered: the channel comes as on its first
    read, replies inline, and the fresh link replaces the old one."""
    import state_db
    team, ch, graph = _channel_fixture()
    state = te.load_state(tmp_path)
    te.export_one_channel(graph, tmp_path, state, team, ch)
    db = state_db.StateDb(tmp_path)
    db.kv_schreiben("delta:k1", "https://example.invalid/never-offered")
    graph.paged_calls.clear()
    graph.get_calls.clear()
    monkeypatch.setenv("FULL_SYNC", "1")
    status, _c, _t, count, _s, _z = te.export_one_channel(graph, tmp_path, state, team, ch)
    assert count == 2
    assert KANAL in graph.paged_calls, "the listing with replies inline"
    assert all("never-offered" not in url for url, _p in graph.get_calls)
    assert db.kv_lesen("delta:k1") == LINK1


def test_full_sync_holt_jede_datei_erneut(tmp_path, monkeypatch):
    msgs = [_msg("Alice Example", "eins", "2025-06-02T08:00:00Z",
                 attachments=[{"id": "a1", "contentType": "reference",
                               "contentUrl": DATEI_URL, "name": "Angebot.pdf"}])]
    rel = "1on1/Alice Example__x.html"
    graph = _DateiGraph()
    _l, geladen, _a, _f = te.anhaenge_laden(graph, tmp_path, "c1", rel, msgs)
    assert geladen == 1
    monkeypatch.setenv("FULL_SYNC", "1")
    graph2 = _DateiGraph()
    lokal, geladen2, _a, _f = te.anhaenge_laden(graph2, tmp_path, "c1", rel, msgs,
                                                unveraendert=True)
    assert len(graph2.gefragt) == 1 and geladen2 == 1 and len(graph2.geladen) == 1
    assert DATEI_URL in lokal
