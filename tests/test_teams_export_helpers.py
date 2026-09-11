"""Tests for the pure helpers in teams_export.py (no Graph/network calls)."""

import teams_export as te


def test_safe_filenames():
    assert te.safe('a/b\\c:d*e?"f<g>h|i') == "a_b_c_d_e_f_g_h_i"
    assert te.safe("  viel   Leerraum  ") == "viel Leerraum"
    assert te.safe("x" * 200, maxlen=10) == "x" * 10
    assert te.safe("") == "unbenannt"
    assert te.safe(None) == "unbenannt"
    assert te.safe("...") == "unbenannt"                # dots only -> empty


def test_short_id_is_stable_hex():
    assert te.short_id("abc") == te.short_id("abc")
    assert te.short_id("abc") != te.short_id("abd")
    assert len(te.short_id("abc")) == 8
    int(te.short_id("abc"), 16)  # hex


def test_parse_ts_handles_graph_timestamps():
    dt = te.parse_ts("2025-06-01T09:30:00Z")
    assert dt is not None and dt.tzinfo is not None
    # Graph sometimes delivers 7-digit fractional seconds
    assert te.parse_ts("2025-06-01T09:30:00.1234567Z") is not None
    assert te.parse_ts("unsinn") is None
    assert te.parse_ts("") is None
    assert te.parse_ts(None) is None


def test_newest_iso_picks_latest_and_ignores_garbage():
    strings = ["2025-06-01T09:30:00Z", "kaputt", "2025-06-02T08:00:00Z", ""]
    assert te.newest_iso(strings) == "2025-06-02T08:00:00Z"
    assert te.newest_iso(["kaputt", ""]) is None
    assert te.newest_iso([]) is None


def test_strip_tags():
    assert te.strip_tags("<p>Hallo <b>Welt</b></p>") == "Hallo Welt"
    assert te.strip_tags(None) == ""


def test_clean_html_removes_scripts_and_event_handlers():
    s = '<div onclick="evil()" onmouseover=\'evil()\'>ok</div><script>alert(1)</script>'
    out = te.clean_html(s)
    assert "script" not in out
    assert "onclick" not in out
    assert "onmouseover" not in out
    assert ">ok</div>" in out


def test_default_categories():
    options = [("1on1", "a"), ("group", "b"), ("meeting", "c"), ("channels", "d")]
    assert te.default_categories(options) == {"1on1", "group", "meeting"}


def test_graph_zeitstempel_is_millisecond_utc():
    assert te.graph_zeitstempel("2025-06-02T08:00:00Z") == "2025-06-02T08:00:00.000Z"
    assert te.graph_zeitstempel("2025-06-02T08:00:00.1234567Z") == "2025-06-02T08:00:00.123Z"
    assert te.graph_zeitstempel("2025-06-02T10:00:00+02:00") == "2025-06-02T08:00:00.000Z"
    assert te.graph_zeitstempel("unsinn") is None
    assert te.graph_zeitstempel(None) is None


def test_conversation_paths_use_safe_names():
    assert te.chat_pfad("1on1", "Alice/Beispiel") == "1on1/Alice_Beispiel"
    assert te.chat_pfad("group", "  Nordwind  ") == "group/Nordwind"
    assert te.kanal_pfad("Team: Rakete", "Allgemein") == "channels/Team_ Rakete/Allgemein"


def test_schlank_keeps_exactly_what_rendering_needs():
    m = {
        "id": "1", "replyToId": None, "etag": "1", "messageType": "message",
        "createdDateTime": "2025-06-01T09:30:00Z",
        "lastModifiedDateTime": "2025-06-01T09:31:00Z",
        "deletedDateTime": None, "subject": "Thema", "webUrl": "https://example.com/m/1",
        "policyViolation": None, "locale": "de-de", "importance": "normal",
        "from": {"application": None, "device": None,
                 "user": {"id": "u1", "displayName": "Alice Beispiel",
                          "userIdentityType": "aadUser", "tenantId": "t"}},
        "body": {"contentType": "html", "content": "<p>Hallo</p>"},
        "attachments": [{"id": "a1", "contentType": "reference", "thumbnailUrl": None,
                         "contentUrl": "https://example.com/x.pdf", "name": "x.pdf",
                         "content": None, "teamsAppId": None}],
        "mentions": [{"id": 0, "mentionText": "Bob"}],
        "reactions": [{"reactionType": "like", "createdDateTime": "…", "user": {"id": "u2"}}],
        "eventDetail": {"@odata.type": "#microsoft.graph.membersAddedEventMessageDetail",
                        "initiator": {"user": {"id": "u1"}}},
        "messageHistory": [],
    }
    s = te.schlank(m)
    assert set(s) == {"id", "messageType", "createdDateTime", "lastModifiedDateTime",
                      "subject", "body", "from", "attachments", "reactions", "eventDetail"}
    assert s["from"] == {"user": {"displayName": "Alice Beispiel"}}
    assert s["attachments"] == [{"id": "a1", "contentType": "reference",
                                 "contentUrl": "https://example.com/x.pdf", "name": "x.pdf"}]
    assert s["reactions"] == [{"reactionType": "like"}]
    assert s["eventDetail"] == {"@odata.type": "#microsoft.graph.membersAddedEventMessageDetail"}
    # the slim form renders exactly like the full one
    assert te.render_message(s) == te.render_message(m)
    bot = {"id": "2", "from": {"application": {"id": "b", "displayName": "Ein Bot"}},
           "body": {"contentType": "text", "content": "hi"}, "createdDateTime": "2025-06-01T09:30:00Z"}
    assert te.schlank(bot)["from"] == {"application": {"displayName": "Ein Bot"}}
    assert "attachments" not in te.schlank(bot) and "eventDetail" not in te.schlank(bot)


def test_kanal_reihe_orders_threads_and_keeps_orphan_replies():
    msgs = [{"id": "r2", "createdDateTime": "2025-06-02T00:00:00Z"},
            {"id": "a2", "replyToId": "r1", "createdDateTime": "2025-06-03T00:00:00Z"},
            {"id": "r1", "createdDateTime": "2025-06-01T00:00:00Z"},
            {"id": "a1", "replyToId": "r1", "createdDateTime": "2025-06-01T10:00:00Z"},
            {"id": "x", "replyToId": "weg", "createdDateTime": "2025-06-04T00:00:00Z"}]
    reihe = te.kanal_reihe(msgs)
    assert [(m["id"], r) for m, r in reihe] == [("r1", False), ("a1", True), ("a2", True),
                                                ("r2", False), ("x", True)]
