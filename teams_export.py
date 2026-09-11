#!/usr/bin/env python3
"""
Teams/chat export via Microsoft Graph (delegated, no admin needed).

Exports one HTML per chat or per channel, divided into:
    1on1/  group/  meeting/  channels/<Team>/

PARALLEL: several chats/channels at once (default 4, via env EXPORT_WORKERS).
  Teams throttling: ~1 request/s per individual chat or channel, 4/s per team,
  and chats live in the mailbox (4 concurrent requests). That is why we
  parallelize ACROSS conversations, not within one. A channel's first read
  fetches its replies inline via $expand=replies (up to 1000, then
  replies@odata.nextLink), which saves a great many calls per channel.
  Throttling (429) is absorbed via Retry-After.

Runs as a subprogram of app.py: output folder as the only argument, every
setting as an environment variable (EXPORT_CATEGORIES, EXPORT_WORKERS,
EMBED_IMAGES, CACHE_IMAGES, REFRESH_CHANNELS, SKIP_EMPTY_CHATS, TEAMS_RULES,
TEAMS_SINCE, SYNC_CADENCE, SYNC_NOW, GRAPH_TOKEN/GRAPH_AUTH; environment
beats app_config.json, see settings.py). There are no prompts; with
"channels" selected, every joined team comes along. `--teams <out>` only
refreshes the stored conversation list (the folders.py tree in state.db:
every chat and every channel with the path the rules see) and exports
nothing. Progress, results and failures are structured lines (progress.py).

Selection: TEAMS_RULES are ordered include/exclude rules over the archive
paths "1on1/<title>", "group/<title>", "meeting/<title>" and
"channels/<team>/<channel>" (folders.gilt – last match wins, empty means
everything). An excluded conversation counts as excluded and is not
touched; one already on disk stays as it is. TEAMS_SINCE (YYYY-MM-DD)
bounds the FIRST fetch of a conversation only: no chat message older than
that day, no channel thread started before it; a chat whose newest
activity is older than the day is left out without a single request.
SYNC_CADENCE carries "teams:<path>" keys along the same paths – a category
(teams:1on1 … teams:channels), a team, a channel, a chat – and the deepest
one wins, a team's value reaching every channel below it. Every
conversation keeps its own stamp (kv last_sync:<id>) and is gated by it;
a category without cadences of its own below it is decided by its
category stamp before any listing. A skipped category is one log line, a
category with some conversations held back one summary line; SYNC_NOW
lifts every gate for one run, and a stamp moves only for a conversation
(or category) that finished without errors.

Resume / incremental: the output folder's state.db keeps one record per
conversation (records area "conversations") and a message store per
conversation (area msgs:<id>, one row per message, slimmed to what the
rendering needs). A chat whose lastMessagePreview moved fetches only the
messages modified after the newest one stored ($filter on
lastModifiedDateTime) and merges them – new and edited messages replace
their row, deleted ones carry deletedDateTime. A channel keeps a delta
link (kv delta:<channel id>, written only after a clean finish): later
runs ask Graph only for the root posts that changed and re-read the replies
of those posts. A reply never moves its root in the delta, so every run
also re-reads the threads active within the last 14 days (newest first,
replies inline, one page in the common case) and once a week the whole
channel (kv replies_full:<channel id>); a 410 drops the link and reads the
channel once in full. The HTML is rendered from the store and
written only when the store changed (or the channel's mirrored files did).
REFRESH_CHANNELS=0 exports channels once and never re-checks them. Delete
the database (or folder) for a full re-export.

Files, both optional and off by default (TEAMS_ATTACHMENTS,
TEAMS_CHANNEL_FILES, TEAMS_FILES_MAX_MB): a file a message references lives
in the sender's OneDrive or the team's library and dies with the access. With
the first switch every such file is fetched next to its conversation
(<kind>/Anhaenge/<conversation>/) and the HTML links the local copy; with
the second, each exported channel's files folder is mirrored through
drive_mirror (channels/<Team>/Dateien/<channel folder>/…) – one delta walk
per team library, its own state.db, tombstones included – and channel posts
link into that mirror instead of fetching twice.
"""

import os
import sys
import re
import json
import time
import base64
import hashlib
import threading
import html as html_lib
from datetime import datetime, timedelta, UTC
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

import auth
import drive_mirror
import export_util
import folders
import state_db
import graph_client
import settings
import progress

try:
    # msal is only needed in auth.py (and only in login mode) – checked here
    # just so the missing-packages message arrives early and in one place.
    import msal  # noqa: F401
    import requests
except ImportError:
    print("Fehlende Pakete. Bitte installieren:  pip install msal requests")
    raise SystemExit(1) from None

export_util.erzwinge_utf8()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES_CHAT = [RES + "Chat.Read", RES + "User.Read"]
SCOPES_FULL = SCOPES_CHAT + [
    RES + "ChannelMessage.Read.All",
    RES + "Team.ReadBasic.All",
    RES + "Channel.ReadBasic.All",
]

# Environment variable > app_config.json > default here (see settings.py)
EMBED_IMAGES = settings.flag("EMBED_IMAGES", "embed_images")
PAGE = 50                   # $top (Graph maximum for messages)

# Incremental runs (e.g. via scheduler): chats with new messages are
# re-exported automatically (detected cheaply via lastMessagePreview).
# Channels are re-checked each run through their delta link and only
# rewritten on change. REFRESH_CHANNELS=0 turns that off (channels then
# export only once).
REFRESH_CHANNELS = settings.flag("REFRESH_CHANNELS", "refresh_channels")

# Cache downloaded inline images (folder .imgcache). When a chat is exported
# again, only NEW images are downloaded instead of all of them. Costs extra
# disk space (images then exist twice: in the cache and embedded in the HTML).
# CACHE_IMAGES=0 turns it off.
CACHE_IMAGES = settings.flag("CACHE_IMAGES", "cache_images")

# Chats containing ONLY system/event messages (joins, calls, membership
# changes, …) and no real message are NOT exported by default and not added
# to the index. SKIP_EMPTY_CHATS=0 exports them anyway.
SKIP_EMPTY_CHATS = settings.flag("SKIP_EMPTY_CHATS", "skip_empty_chats")

# The two file switches, off by default: a chat history is small, the files
# it points at may not be. Both need Files.Read.All – requested only when
# one of them is on, so a plain chat export keeps its small scope.
ATTACHMENTS = settings.flag("TEAMS_ATTACHMENTS", "teams_attachments")
CHANNEL_FILES = settings.flag("TEAMS_CHANNEL_FILES", "teams_channel_files")

ANHANG_DIR = "Anhaenge"          # files a message references, per conversation
DATEI_DIR = drive_mirror.DATEI_DIR   # the mirrored channel folders

KATEGORIEN = ("1on1", "group", "meeting", "channels")

# The delta names root posts only, and a reply never moves its root. So
# after every delta round the newest threads are re-read with their replies
# inline (Graph lists channel posts by the last activity of the whole
# thread, newest first) down to this many days back – one page in the
# common case – and once a week a channel is read in full again, so a
# reply on an older thread arrives too.
ANTWORT_FENSTER_TAGE = 14
ANTWORT_VOLLPASS_TAGE = 7


def files_max_bytes():
    """The size cap for both file switches; 0 means no limit."""
    mb = settings.number("TEAMS_FILES_MAX_MB", "teams_files_max_mb", low=0)
    return int(mb) * 1024 * 1024


def _dateiscopes(scopes):
    if ATTACHMENTS or CHANNEL_FILES:
        return scopes + [RES + "Files.Read.All"]
    return list(scopes)


def teams_regeln():
    """Which conversations get exported – environment beats file; without
    rules every conversation of the ticked categories comes along."""
    roh = os.environ.get("TEAMS_RULES")
    if roh is None:
        roh = settings.value("teams_rules", None)
    return folders.lies_regeln(roh or "")


def seit_grenze():
    """TEAMS_SINCE as the start of that day (UTC) – None when unset or
    unusable; main() says so once for an unusable value."""
    roh = os.environ.get("TEAMS_SINCE")
    if roh is None:
        roh = settings.value("teams_since", None)
    roh = str(roh or "").strip()
    if not roh:
        return None
    try:
        return datetime.strptime(roh, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


TYPEMAP = {"oneOnOne": "1on1", "group": "group", "meeting": "meeting"}
SUBNAME = {"1on1": "1:1-Chat", "group": "Gruppenchat",
           "meeting": "Meeting-Chat", "other": "Chat"}

# Network, throttling, retry and paging live in graph_client.py – one layer
# shared by all the exports.
STOP = threading.Event()                      # signal: token dead -> start nothing new
STATE_LOCK = threading.Lock()                 # serializes writes to the progress state
PRINT_LOCK = threading.Lock()                 # clean, non-interleaved progress lines

_client = None       # set in main() (for image embedding)
IMGCACHE_DIR = None  # set in main() when CACHE_IMAGES is active
SEIT = None          # set in main(): the TEAMS_SINCE day, an aware datetime


# Sign-in and token mode live in auth.py – shared with outlook_export.py
# instead of being duplicated line for line.
TokenExpired = auth.TokenExpired
load_pasted_token = auth.load_pasted_token


class ImageUnavailable(RuntimeError):
    """An inline image (hostedContent) cannot be downloaded (e.g. 502).
    NOT retried – the export continues with a placeholder."""


# ---------------------------------------------------------------------------
# Graph client: the channel question – the rest lives in graph_client.py
# ---------------------------------------------------------------------------
class _BildClient:
    """get_bytes with image semantics – deliberately more forgiving than the
    shared client: Teams downloads bytes only for inline images
    (hostedContents), and a server error or persistent throttling raises
    ImageUnavailable there, so the export continues with a placeholder
    instead of failing over one image."""

    def get_bytes(self, url, timeout=graph_client.TIMEOUT_BYTES, label=" (Bild)"):
        for versuch in range(4):
            try:
                r = graph_client.fetch(url, self._headers(), timeout=timeout, label=label)
            except requests.exceptions.RequestException as e:
                raise ImageUnavailable(type(e).__name__) from e
            if r.status_code == 401:
                self._erneuern()
                continue
            if r.status_code == 429:          # real throttling -> wait it out
                graph_client.warte_auf(r, versuch, label)
                continue
            if 500 <= r.status_code < 600:    # server error -> image cannot be loaded
                raise ImageUnavailable(r.status_code)
            r.raise_for_status()
            return r.content, r.headers.get("Content-Type", "")
        raise ImageUnavailable("429")


class Graph(_BildClient, drive_mirror.DriveOps, graph_client.Graph):
    """Signed-in access with the channel question.

    Channel permissions are requested first and dropped on failure –
    ChannelMessage.Read.All requires admin consent in many tenants, and a
    chat export must not fail over that. The drive operations serve the
    channel files mirror; drive_base is set per team library."""

    def __init__(self, want_channels, nur_still=False):
        self.want_channels = want_channels
        self.channels_enabled = False

        if want_channels:
            anmeldung = auth.Login(_dateiscopes(SCOPES_FULL))
            if anmeldung.anmelden(nur_still=nur_still, weich=True):
                self.channels_enabled = True
                super().__init__(anmeldung=anmeldung)
                return
            progress.event("run.teams.channels_denied", "warn",
                           error=(anmeldung.fehler or "").splitlines()[0]
                           if anmeldung.fehler else "")
        super().__init__(_dateiscopes(SCOPES_CHAT), nur_still=nur_still)


class TokenClient(_BildClient, drive_mirror.DriveOps, graph_client.TokenClient):
    """Ready-made bearer token; only the caller knows whether channels work."""

    def __init__(self, token, channels_enabled):
        super().__init__(token)
        self.channels_enabled = channels_enabled


def _zugang(want_channels):
    """The configured access path – a pasted token or the silent sign-in."""
    return auth.waehle_zugang(
        lambda tok: TokenClient(tok, channels_enabled=want_channels),
        lambda nur_still=False: Graph(want_channels=want_channels,
                                      nur_still=nur_still))


# ---------------------------------------------------------------------------
# Selection – no prompts: the app is the only caller, and nobody would see
# a question asked by a subprocess.
# ---------------------------------------------------------------------------
def default_categories(options):
    """Default selection: the first three categories (1:1, group, meeting chats)."""
    return {k for k, _ in options[:3]}


env_categories = export_util.env_categories


def selected_categories(options):
    """Which chat kinds to export – from EXPORT_CATEGORIES, else the default.

    The app sets the variable on every run; the fallback keeps a run without
    it deterministic (1:1, group and meeting chats, no channels).
    """
    env = env_categories(options)
    if env is not None:
        return env
    progress.event("run.default_selection")
    return default_categories(options)


def _mit_ausnahmen(kadenzen, cat):
    """Does any team, channel or chat below this category set its own cadence?"""
    return any(k.startswith(f"teams:{cat}/") for k in kadenzen)


def faellige_kategorien(db, categories, kadenzen=None):
    """The categories to list this run – the others are said once and left
    alone. A category without cadences of its own below it is decided here
    by its own stamp, before any listing; one with overrides below is
    always listed and gated per conversation (Taktung). Returns (due,
    number of categories skipped)."""
    kadenzen = export_util.kadenzen() if kadenzen is None else kadenzen
    faellig, uebersprungen = set(), 0
    for cat in KATEGORIEN:
        if cat not in categories:
            continue
        kadenz = kadenzen.get(f"teams:{cat}") or "always"
        if _mit_ausnahmen(kadenzen, cat) or \
                export_util.einheit_faellig(db, kadenz, kv_key=f"last_sync:{cat}"):
            faellig.add(cat)
            continue
        uebersprungen += 1
        progress.event("run.cadence.skip", name=progress.atom(f"export.cat.{cat}"),
                       cadence=progress.atom(f"cadence.{kadenz}"))
    return faellig, uebersprungen


class Taktung:
    """The cadence gate per conversation.

    SYNC_CADENCE carries "teams:<path>" keys along the paths the rules see
    – a category, a team, a channel, a chat – and the deepest one wins
    (export_util.kadenz_fuer). Every conversation keeps its own stamp
    (kv last_sync:<conversation id>) and gets it when it was processed
    without error, changed or found unchanged; SYNC_NOW steps over every
    gate. The log gets one line per category, never one per conversation."""

    def __init__(self, db, kadenzen):
        self.db = db
        self.kadenzen = kadenzen
        self._an = Counter()          # category -> conversations due
        self._aus = Counter()         # category -> conversations held back
        self._takte = {}              # category -> Counter of the cadences that held back

    def faellig(self, cat, pfad, key):
        kadenz = export_util.kadenz_fuer(self.kadenzen, "teams", pfad)
        if export_util.einheit_faellig(self.db, kadenz, kv_key=f"last_sync:{key}"):
            self._an[cat] += 1
            return True
        self._aus[cat] += 1
        self._takte.setdefault(cat, Counter())[kadenz] += 1
        return False

    def stempeln(self, key):
        self.db.kv_schreiben(f"last_sync:{key}", str(time.time()))

    def uebersprungen(self):
        return sum(self._aus.values())

    def melden(self):
        for cat in KATEGORIEN:
            aus = self._aus.get(cat, 0)
            if not aus:
                continue
            if self._an.get(cat, 0):
                progress.event("run.teams.paced", name=progress.atom(f"export.cat.{cat}"), n=aus)
            else:
                kadenz = self._takte[cat].most_common(1)[0][0]
                progress.event("run.cadence.skip", name=progress.atom(f"export.cat.{cat}"),
                               cadence=progress.atom(f"cadence.{kadenz}"))


def kadenz_abschliessen(db, categories, fehler):
    """Record the finish of every category that ran without errors; "*"
    in `fehler` means a failure nobody could assign to a category."""
    if "*" in fehler:
        return
    jetzt = str(time.time())
    for cat in categories:
        if cat not in fehler:
            db.kv_schreiben(f"last_sync:{cat}", jetzt)


def select_teams(graph, fehler=None):
    """Every joined team – channels of all of them come along. A listing
    that fails is said and noted in `fehler`: the category was not
    finished cleanly, its cadence mark must not move."""
    try:
        teams = list(graph.paged(f"{GRAPH}/me/joinedTeams", {"$top": PAGE}))
    except TokenExpired:
        raise
    except Exception as e:
        progress.event("run.teams.list_failed", "warn", error=str(e))
        if fehler is not None:
            fehler.add("channels")
        return []
    teams.sort(key=lambda t: (t.get("displayName") or "").lower())
    if not teams:
        progress.event("run.teams.none")
        return []
    progress.event("run.teams.found", n=len(teams))
    return teams


# ---------------------------------------------------------------------------
# Helpers (shared in export_util.py)
# ---------------------------------------------------------------------------
safe = export_util.safe
short_id = export_util.kuerzel
parse_ts = export_util.graph_zeit


def human_time(iso):
    """ISO-8601 (Graph, UTC) -> local display; unparseable input comes back as is."""
    dt = export_util.graph_zeit(iso)
    if dt is None:
        return iso or ""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def newest_iso(strings):
    """Returns the ISO string with the latest instant (robustly parsed)."""
    best_iso, best_dt = None, None
    for iso in strings:
        dt = parse_ts(iso)
        if dt and (best_dt is None or dt > best_dt):
            best_dt, best_iso = dt, iso
    return best_iso


def graph_zeitstempel(iso):
    """An instant in the form Graph's filters accept – millisecond UTC;
    None when unparseable."""
    dt = parse_ts(iso)
    if dt is None:
        return None
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def chat_pfad(folder, title):
    """The path the rules see for a chat: "<kind>/<title>"."""
    return f"{folder}/{safe(title)}"


def kanal_pfad(tname, cname):
    """The path the rules see for a channel: "channels/<team>/<channel>"."""
    return f"channels/{safe(tname)}/{safe(cname)}"


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def clean_html(s):
    s = s or ""
    s = re.sub(r"<script\b[^>]*>.*?</script>", "", s, flags=re.I | re.S)
    s = re.sub(r'\son\w+\s*=\s*"[^"]*"', "", s, flags=re.I)
    s = re.sub(r"\son\w+\s*=\s*'[^']*'", "", s, flags=re.I)
    return s


HOSTED_RE = re.compile(
    r'https://graph\.microsoft\.com/(?:v1\.0|beta)/[^\s"\'<>]*?hostedContents/[^\s"\'<>]+?/\$value'
)


_PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="90">'
    '<rect width="240" height="90" rx="6" fill="#f3f4f6" stroke="#d6d9dd"/>'
    '<text x="120" y="40" font-family="sans-serif" font-size="13" fill="#8a8f98"'
    ' text-anchor="middle">Bild nicht verfügbar</text>'
    '<text x="120" y="60" font-family="sans-serif" font-size="11" fill="#aab0b8"'
    ' text-anchor="middle">Download fehlgeschlagen</text></svg>'
)
IMG_PLACEHOLDER = ("data:image/svg+xml;base64,"
                   + base64.b64encode(_PLACEHOLDER_SVG.encode("utf-8")).decode())


def embed_hosted_images(html_content, counter=None):
    if not _client:
        return html_content

    def repl(m):
        url = m.group(0)
        cf = None
        if IMGCACHE_DIR is not None:
            cf = IMGCACHE_DIR / hashlib.sha1(url.encode()).hexdigest()
            if cf.exists():
                try:
                    return cf.read_text(encoding="utf-8")   # cache hit: no download
                except OSError:
                    pass
        try:
            content, ctype = _client.get_bytes(url)
            data_uri = f"data:{ctype or 'image/png'};base64," + base64.b64encode(content).decode()
            if counter is not None:
                counter[0] += 1
            if cf is not None:
                try:
                    cf.write_text(data_uri, encoding="utf-8")
                except OSError:
                    pass
            return data_uri
        except TokenExpired:
            raise   # token dead -> don't half-write the conversation
        except Exception:
            return IMG_PLACEHOLDER   # 502 or similar -> visible placeholder, carry on

    return HOSTED_RE.sub(repl, html_content)


def render_attachments(atts, lokal=None):
    """The attachment line; `lokal` maps a file URL to the relative path of
    its copy next to the conversation – then the link stays usable once
    the access is gone."""
    items = []
    for a in atts or []:
        nm = a.get("name") or a.get("contentType") or "Anhang"
        url = a.get("contentUrl")
        if url:
            ziel = (lokal or {}).get(url, url)
            items.append(f'📎 <a href="{html_lib.escape(ziel)}">{html_lib.escape(nm)}</a>')
        else:
            items.append(f"📎 {html_lib.escape(nm)}")
    return f'<div class="att">{"<br>".join(items)}</div>' if items else ""


def _referenzen(msgs):
    """(url, name) of every file the messages point at – reference
    attachments only; cards, code snippets and quoted messages carry no
    file."""
    for m in msgs:
        for a in m.get("attachments") or []:
            url = a.get("contentUrl")
            if url and (a.get("contentType") or "") == "reference":
                yield url, str(a.get("name") or "datei")


def render_reactions(rs):
    if not rs:
        return ""
    c = Counter(r.get("reactionType", "?") for r in rs)
    return '<div class="react">' + html_lib.escape(
        " ".join(f"{k} ×{v}" for k, v in c.items())) + "</div>"


def render_message(msg, is_reply=False, img_counter=None, lokal=None):
    when = human_time(msg.get("createdDateTime"))
    if msg.get("messageType", "message") != "message":   # system event
        ed = msg.get("eventDetail") or {}
        label = strip_tags(html_lib.unescape((msg.get("body") or {}).get("content", ""))) \
            or ed.get("@odata.type", "").split(".")[-1] or "Systemnachricht"
        return f'<div class="sys">{html_lib.escape(label)} · {when}</div>'

    frm = msg.get("from") or {}
    user = frm.get("user") or {}
    app = frm.get("application") or {}
    name = user.get("displayName") or app.get("displayName") or "Unbekannt"

    cls = "msg reply" if is_reply else "msg"
    body = msg.get("body") or {}
    if msg.get("deletedDateTime"):
        body_html = "<em>[gelöscht]</em>"
    elif (body.get("contentType") or "text") == "html":
        body_html = clean_html(body.get("content", ""))
        if EMBED_IMAGES:
            body_html = embed_hosted_images(body_html, img_counter)
    else:
        cls += " text"
        body_html = html_lib.escape(body.get("content", ""))

    subj = msg.get("subject")
    subj_html = f'<div class="subj"><strong>{html_lib.escape(subj)}</strong></div>' if subj else ""

    return (f'<div class="{cls}"><div class="head">'
            f'<span class="name">{html_lib.escape(name)}</span>'
            f'<span class="time">{when}</span></div>{subj_html}'
            f'<div class="body">{body_html}</div>'
            f'{render_attachments(msg.get("attachments"), lokal)}'
            f'{render_reactions(msg.get("reactions"))}</div>')


def member_name(m):
    return (m.get("displayName") or m.get("email") or "").strip()


def chat_title(graph, chat, my_id):
    ctype = chat.get("chatType")
    topic = chat.get("topic")
    if ctype != "oneOnOne" and topic:
        return topic
    members = chat.get("members")            # usually already present via $expand
    if not members:
        try:   # fallback: load members separately
            members = list(graph.paged(f"{GRAPH}/me/chats/{chat['id']}/members", {"$top": PAGE}))
        except TokenExpired:
            raise
        except Exception:
            members = []
    others = [member_name(m) for m in members
              if m.get("userId") != my_id and member_name(m)]
    if ctype == "oneOnOne":
        return others[0] if others else "Unbekannt"
    if others:
        return ", ".join(others[:5]) + ("…" if len(others) > 5 else "")
    return topic or ctype or "Chat"


CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#1b1b1f;background:#f6f7f9}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #e3e5e8;padding:16px 24px;z-index:1}
header h1{margin:0 0 2px;font-size:18px}
header .sub{margin:0;color:#5b5f66;font-size:13px}
header .meta{margin:6px 0 0;color:#8a8f98;font-size:12px}
header .warn{margin:6px 0 0;color:#b5651d;font-size:12px}
main{max-width:860px;margin:0 auto;padding:20px 16px 60px}
.msg{padding:10px 14px;margin:2px 0;background:#fff;border:1px solid #ececef;border-radius:10px}
.msg .head{display:flex;gap:8px;align-items:baseline;margin-bottom:3px}
.msg .name{font-weight:600}
.msg .time{color:#9aa0a6;font-size:12px}
.msg .subj{margin-bottom:4px}
.msg .body{word-wrap:break-word;overflow-wrap:anywhere}
.msg .body img{max-width:100%;height:auto;border-radius:6px}
.msg.text .body{white-space:pre-wrap}
.reply{margin-left:28px;border-left:3px solid #d7dadf;border-radius:0 10px 10px 0}
.sys{background:transparent;border:none;text-align:center;color:#9aa0a6;font-size:12px;padding:6px}
.att{margin-top:6px;font-size:13px}
.att a{color:#2b6cb0;text-decoration:none}
.react{margin-top:4px;color:#8a8f98;font-size:12px}
.empty{color:#9aa0a6}
"""


def render_conversation(title, subtitle, meta, blocks):
    body = "".join(blocks) or '<p class="empty">Keine Nachrichten.</p>'
    n_fail = body.count(IMG_PLACEHOLDER)
    warn = (f'<p class="warn">&#9888; {n_fail} Bild(er) konnten nicht geladen werden '
            f'und sind als Platzhalter markiert.</p>') if n_fail else ""
    return (f'<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html_lib.escape(title)}</title><style>{CSS}</style></head><body>'
            f'<header><h1>{html_lib.escape(title)}</h1>'
            f'<p class="sub">{html_lib.escape(subtitle)}</p>'
            f'<p class="meta">{html_lib.escape(meta)}</p>{warn}</header>'
            f'<main>{body}</main></body></html>')


# ---------------------------------------------------------------------------
# The message store: one row per message in the folder's state.db
# ---------------------------------------------------------------------------
_FELDER = ("id", "replyToId", "messageType", "createdDateTime",
           "lastModifiedDateTime", "deletedDateTime", "subject", "body")


def schlank(m):
    """The message reduced to what the rendering needs – what the store
    keeps. Mentions, policy data, web URLs and identities beyond the
    display name are dropped: they never reach the HTML."""
    s = {k: m.get(k) for k in _FELDER if m.get(k) is not None}
    von = {}
    for art in ("user", "application"):
        name = ((m.get("from") or {}).get(art) or {}).get("displayName")
        if name:
            von[art] = {"displayName": name}
    if von:
        s["from"] = von
    atts = [{k: a.get(k) for k in ("id", "contentType", "contentUrl", "name")
             if a.get(k) is not None} for a in (m.get("attachments") or [])]
    if atts:
        s["attachments"] = atts
    reaktionen = [{"reactionType": r.get("reactionType")}
                  for r in (m.get("reactions") or [])]
    if reaktionen:
        s["reactions"] = reaktionen
    art = ((m.get("eventDetail") or {}).get("@odata.type")
           if isinstance(m.get("eventDetail"), dict) else None)
    if art:
        s["eventDetail"] = {"@odata.type": art}
    return s


def _nachrichten_id(m):
    return str(m.get("id") or f'{m.get("createdDateTime") or ""}:'
               f'{short_id(json.dumps(m, sort_keys=True, ensure_ascii=False))}')


class Nachrichtenspeicher:
    """The messages of one conversation, one row each (area msgs:<id>).

    Loaded once, changed in memory, written at the end: rows that differ
    are upserted, rows that vanished are deleted, nothing else is touched.
    `geaendert()` is the question the HTML write hangs on."""

    def __init__(self, db, kennung):
        self.db = db
        self.bereich = f"msgs:{kennung}"
        self.zeilen = dict(db.saetze_lesen(self.bereich))   # id -> JSON
        self._neu = {}
        self._weg = set()

    def leer(self):
        return not self.zeilen

    def __len__(self):
        return len(self.zeilen)

    def geaendert(self):
        return bool(self._neu or self._weg)

    def merge(self, msgs):
        """New and edited messages replace their row by id."""
        for m in msgs:
            sid = _nachrichten_id(m)
            roh = json.dumps(schlank(m), ensure_ascii=False)
            self._weg.discard(sid)
            if self.zeilen.get(sid) != roh:
                self.zeilen[sid] = roh
                self._neu[sid] = roh

    def entferne(self, ids):
        for sid in ids:
            if self.zeilen.pop(sid, None) is not None:
                self._neu.pop(sid, None)
                self._weg.add(sid)

    def ersetze(self, msgs):
        """A full read: everything not in it is gone."""
        jetzt = {_nachrichten_id(m) for m in msgs}
        self.entferne([sid for sid in list(self.zeilen) if sid not in jetzt])
        self.merge(msgs)

    def ersetze_antworten(self, root_id, replies):
        """The replies of one root post, freshly read: the stored ones the
        listing no longer knows are gone."""
        jetzt = {_nachrichten_id(r) for r in replies}
        # A channel holds thousands of rows and a delta round may name many
        # roots: the substring test spares parsing every row per root.
        marke = json.dumps({"replyToId": root_id}, ensure_ascii=False)[1:-1]
        alt = []
        for sid, roh in self.zeilen.items():
            if sid in jetzt or marke not in roh:
                continue
            try:
                if json.loads(roh).get("replyToId") == root_id:
                    alt.append(sid)
            except (ValueError, AttributeError):
                continue
        self.entferne(alt)
        self.merge(replies)

    def nachrichten(self, mit_id=False):
        """Every stored message, parsed – (id, message) pairs on request."""
        aus = []
        for sid, roh in self.zeilen.items():
            try:
                m = json.loads(roh)
            except ValueError:
                continue
            if isinstance(m, dict):
                aus.append((sid, m) if mit_id else m)
        return aus

    def wasserzeichen(self):
        """The newest lastModifiedDateTime stored – the chat's filter bound."""
        return newest_iso(m.get("lastModifiedDateTime") or m.get("createdDateTime")
                          for m in self.nachrichten())

    def sichern(self):
        self.db.saetze_loeschen(self.bereich, self._weg)
        self.db.saetze_schreiben(self.bereich, self._neu)
        self._neu, self._weg = {}, set()


# ---------------------------------------------------------------------------
# Files: referenced attachments next to the conversation
# ---------------------------------------------------------------------------
def anhang_ordner(rel):
    """Where a conversation's files live, relative to the output folder:
    1on1/Name__id.html -> 1on1/Anhaenge/Name__id."""
    ordner, _, datei = rel.rpartition("/")
    stamm = datei[:-5] if datei.endswith(".html") else datei
    return f"{ordner}/{ANHANG_DIR}/{stamm}" if ordner else f"{ANHANG_DIR}/{stamm}"


def _anhang_name(url, name):
    """A file name the file system and the /source route can trust; the
    URL tag keeps two same-named files from two messages apart."""
    roh = re.sub(r"[&#%?]", "_", safe(str(name or "datei")))
    stamm, punkt, endung = roh.rpartition(".")
    kurz = short_id(url)
    return f"{stamm}__{kurz}.{endung}" if punkt and stamm else f"{roh}__{kurz}"


def _lade_datei(graph, url, ziel):
    """Stream a file to `ziel` – sidecar first, renamed at the end, so an
    abort never leaves a half file that would pass as complete."""
    r = graph.stream(url, timeout=drive_mirror.TIMEOUT_BYTES, label=" (Datei)")
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(ziel.name + ".teil")
    with open(tmp, "wb") as f:
        for stueck in r.iter_content(chunk_size=1 << 20):
            if stueck:
                f.write(stueck)
    os.replace(tmp, ziel)


def anhaenge_umziehen(out, prior, new_rel):
    """A renamed conversation takes its files folder along – before the
    run looks for them under the new name."""
    if not prior or not prior.get("rel") or prior["rel"] == new_rel:
        return
    alt, neu = out / anhang_ordner(prior["rel"]), out / anhang_ordner(new_rel)
    if alt.is_dir() and not neu.exists():
        neu.parent.mkdir(parents=True, exist_ok=True)
        try:
            alt.replace(neu)
        except OSError:
            pass


def _freigabe(url):
    """The sharing token Graph resolves a file URL with."""
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode().rstrip("=")


def _batch_fehler(status, meta):
    """The one line a refused batch part gets."""
    code = (meta or {}).get("error", {}).get("code") if isinstance(meta, dict) else None
    return f"HTTP {status}" + (f" {code}" if code else "")


def anhaenge_laden(graph, out, key, rel, msgs, ausser=None, unveraendert=False):
    """The files the messages reference, fetched next to the conversation.

    Returns ({url: href relative to the HTML}, files fetched, left out by
    size, failed). The metadata of every file (name, cTag, size) comes in
    ONE batch per conversation; a file whose copy lies here with the cTag
    from the last run is not fetched again. With `unveraendert` – the
    conversation's messages did not change – a file already on disk with a
    known cTag skips even the metadata question. A file that will not come
    keeps its cloud link and says so once in the log. `ausser` names URLs
    the channel mirror already holds – linked, never fetched twice."""
    db = state_db.StateDb(out)
    kv = f"files:{key}"
    try:
        stand = json.loads(db.kv_lesen(kv) or "{}")
    except ValueError:
        stand = {}
    ordner_rel = anhang_ordner(rel)
    href_basis = ordner_rel.split("/", 1)[1] if "/" in rel else ordner_rel
    grenze = files_max_bytes()
    lokal, geladen, ausgelassen, fehler = {}, 0, 0, 0
    offen = {}
    for url, name in _referenzen(msgs):
        if url in lokal or url in offen:
            continue
        if ausser and url in ausser:
            lokal[url] = ausser[url]
            continue
        alt = stand.get(url) or {}
        if unveraendert and alt.get("ctag") and alt.get("rel"):
            dateiname = alt["rel"].rsplit("/", 1)[-1]
            if (out / ordner_rel / dateiname).exists():
                lokal[url] = f"{href_basis}/{dateiname}"
                continue
        offen[url] = name
    if not offen:
        return lokal, geladen, ausgelassen, fehler

    meta_urls = {f"{GRAPH}/shares/u!{_freigabe(url)}/driveItem?$select=name,cTag,size": url
                 for url in offen}
    try:
        antworten = graph.batch_get(list(meta_urls))
    except TokenExpired:
        raise
    except Exception as e:
        grund = {"error": {"code": f"{type(e).__name__}: {e}"}}
        antworten = {u: (0, grund) for u in meta_urls}
    for meta_url, url in meta_urls.items():
        name = offen[url]
        status, meta = antworten.get(meta_url, (0, None))
        alt = stand.get(url) or {}
        try:
            if status != 200 or not isinstance(meta, dict):
                raise RuntimeError(_batch_fehler(status, meta))
            groesse = int(meta.get("size") or 0)
            if grenze and groesse > grenze:
                ausgelassen += 1
                continue
            dateiname = _anhang_name(url, meta.get("name") or name)
            ziel_rel = f"{ordner_rel}/{dateiname}"
            href = f"{href_basis}/{dateiname}"
            if alt.get("ctag") == (meta.get("cTag") or "") and (out / ziel_rel).exists():
                lokal[url] = href
                continue
            _lade_datei(graph, f"{GRAPH}/shares/u!{_freigabe(url)}/driveItem/content",
                        out / ziel_rel)
            stand[url] = {"rel": ziel_rel, "ctag": meta.get("cTag") or "",
                          "size": groesse}
            lokal[url] = href
            geladen += 1
        except TokenExpired:
            raise
        except Exception as e:
            fehler += 1
            progress.event("run.teams.file_failed", "warn", name=name[:60],
                           error=f"{type(e).__name__}: {e}")
    db.kv_schreiben(kv, json.dumps(stand, ensure_ascii=False))
    return lokal, geladen, ausgelassen, fehler


# ---------------------------------------------------------------------------
# Files: the channel folders, mirrored
# ---------------------------------------------------------------------------
def spiegel_links(out, info, msgs):
    """URL -> href for the files of channel posts that the mirror holds –
    the post links into the mirror instead of fetching the file twice."""
    lokal = {}
    basis = info["weburl"].rstrip("/").lower() + "/"
    for url, _name in _referenzen(msgs):
        u = unquote(url)
        if not u.lower().startswith(basis):
            continue
        rest = [drive_mirror.safe(s) for s in u[len(basis):].split("/") if s]
        if not rest:
            continue
        rel = "/".join([info["rel"], *rest])
        if (out / info["wurzel"] / rel).exists():
            lokal[url] = f'{info["praefix"]}/{rel}' if info["praefix"] else rel
    return lokal


def _dateien_unter(bestand, rel):
    """The mirror inventory below one channel folder: rel -> cTag."""
    praefix = rel.rstrip("/") + "/"
    return {e["rel"]: e.get("ctag") for e in bestand.values()
            if str(e.get("rel") or "").startswith(praefix)}


def kanal_dateien_spiegeln(graph, out, channel_jobs):
    """Mirror the files folder of every channel being exported.

    Standard channels share the team's library, so one delta walk per team
    serves them all, scoped to their folders and written below
    channels/<Team>/Dateien/. A private or shared channel brings a library
    of its own and lands in a folder of its own. Every channel's info says
    in "neu" whether the files of ITS folder changed – only then does a post
    that links into the mirror need a rewrite. Returns ({channel id: mirror
    info}, the summed mirror counts)."""
    je_drive = {}
    for _kind, team, ch in channel_jobs:
        tname = team.get("displayName", "Team")
        cname = ch.get("displayName", "Kanal")
        try:
            ordner = graph.get(f"{GRAPH}/teams/{team['id']}/channels/{ch['id']}"
                               "/filesFolder")
            drive_id = (ordner.get("parentReference") or {}).get("driveId")
            if not drive_id or not ordner.get("id"):
                raise ValueError("files folder without a drive")
            item = graph.get(f"{GRAPH}/drives/{drive_id}/items/{ordner['id']}"
                             "?$select=id,name,parentReference,root,webUrl")
        except TokenExpired:
            raise
        except Exception as e:
            progress.event("run.teams.files_failed", "warn",
                           name=f"{tname} / {cname}",
                           error=f"{type(e).__name__}: {e}")
            continue
        eintrag = je_drive.setdefault(drive_id, {"team": team, "kanaele": []})
        eintrag["kanaele"].append((ch, item))

    spiegel = {}
    summe = {"new": 0, "excluded": 0, "errors": 0, "moved": 0, "gone": 0}
    grenze = files_max_bytes()
    for drive_id, d in je_drive.items():
        team = d["team"]
        tname = safe(team.get("displayName", "Team"))
        standard = all((ch.get("membershipType") or "standard") == "standard"
                       for ch, _i in d["kanaele"])
        if standard:
            wurzel_rel, praefix = f"channels/{tname}", ""
        else:
            ch0, _i = d["kanaele"][0]
            praefix = f"{safe(ch0.get('displayName', 'Kanal'))}__{short_id(ch0['id'])}"
            wurzel_rel = f"channels/{tname}/{praefix}"
        regeln, ganz = [(False, "**")], False
        for ch, item in d["kanaele"]:
            if "root" in item:
                ganz, rel = True, DATEI_DIR
            else:
                rel = drive_mirror.rel_pfad(item)
                regeln.append((True, f"{rel}/**"))
            spiegel[ch["id"]] = {"wurzel": wurzel_rel, "praefix": praefix,
                                 "rel": rel,
                                 "weburl": unquote(item.get("webUrl") or ""),
                                 "neu": False}
        # Includes only, so their order says nothing – sorted, because the
        # mirror fingerprints the rules verbatim and a re-ordered channel
        # listing would otherwise cost a full walk of the library.
        regeln = [regeln[0], *sorted(set(regeln[1:]))]
        auswahl = drive_mirror.Selection(scope=None if ganz else regeln,
                                         max_bytes=grenze)
        graph.drive_base = f"{GRAPH}/drives/{drive_id}"
        progress.event("run.teams.files", name=team.get("displayName", "Team"),
                       n=len(d["kanaele"]))
        ziel = out / wurzel_rel
        vorher = state_db.StateDb(ziel).bestand_lesen()
        try:
            zahlen = drive_mirror.lauf(graph, ziel, auswahl, drive_mirror.workers(),
                                       still=True,
                                       zustand=state_db.DbZustand(ziel))
        except TokenExpired:
            raise
        except Exception as e:
            progress.event("run.teams.files_failed", "warn",
                           name=team.get("displayName", "Team"),
                           error=f"{type(e).__name__}: {e}")
            summe["errors"] += 1
            continue
        for k in summe:
            summe[k] += int(zahlen.get(k) or 0)
        nachher = state_db.StateDb(ziel).bestand_lesen()
        for ch, _item in d["kanaele"]:
            info = spiegel[ch["id"]]
            info["neu"] = (_dateien_unter(vorher, info["rel"])
                           != _dateien_unter(nachher, info["rel"]))
    return spiegel, summe


# ---------------------------------------------------------------------------
# Progress (thread-safe)
# ---------------------------------------------------------------------------
def _satz(rec):
    return json.dumps(rec, ensure_ascii=False)


def load_state(out):
    """The conversation records – one row each in the records area
    "conversations". An archive from before 9.0 still carries them as one
    kv blob ("state"): read once and carried over into rows, so nothing is
    exported twice after the upgrade."""
    db = state_db.StateDb(out)
    zeilen = db.saetze_lesen("conversations")
    conversations = {}
    for key, roh in zeilen.items():
        try:
            rec = json.loads(roh)
        except ValueError:
            continue
        if isinstance(rec, dict):
            conversations[key] = rec
    if not zeilen:
        roh = db.kv_lesen("state")
        if roh:
            try:
                data = json.loads(roh)
                if isinstance(data, dict) and isinstance(data.get("conversations"), dict):
                    conversations = data["conversations"]
                    db.saetze_schreiben("conversations",
                                        {k: _satz(v) for k, v in conversations.items()})
            except Exception:
                progress.event("run.state_unreadable", "warn")
    return {"version": 1, "conversations": conversations}


def already_done(out, state, key):
    rec = state["conversations"].get(key)
    if not rec or not rec.get("done"):
        return False
    return (out / rec["rel"]).exists()   # only skip while the file is still there


def get_record(out, state, key):
    """Read an existing, completed record (including last_activity) – or None
    if not exported or the file is missing. Thread-safe."""
    with STATE_LOCK:
        rec = state["conversations"].get(key)
    if not rec or not rec.get("done"):
        return None
    if rec.get("empty"):
        return rec   # marked empty: no file, but a valid status (for the incremental check)
    if not (out / rec["rel"]).exists():
        return None
    return rec


def _bekannt(state, key):
    """Was this conversation ever recorded – file or not? Decides whether
    TEAMS_SINCE still applies to its next fetch."""
    with STATE_LOCK:
        return state["conversations"].get(key)


def cleanup_old(out, prior, new_rel):
    """On a rename (e.g. 'Unbekannt' -> real name) remove the orphaned old
    file so no duplicate is left behind."""
    if prior and prior.get("rel") and prior["rel"] != new_rel:
        try:
            (out / prior["rel"]).unlink()
        except OSError:
            pass


def record_done(out, state, key, category, title, rel, count, last_activity=None, empty=False):
    rec = {
        "category": category, "title": title, "rel": rel,
        "count": count, "done": True, "empty": empty,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "last_activity": last_activity,   # newest message -> basis for incremental runs
        "v": 2,                           # written with a message store behind it
    }
    with STATE_LOCK:   # several workers write -> serialize
        state["conversations"][key] = rec
        state_db.StateDb(out).saetze_schreiben("conversations", {key: _satz(rec)})


# ---------------------------------------------------------------------------
# Export of ONE conversation (runs in a worker thread)
# ---------------------------------------------------------------------------
def render_blocks(msgs, lokal=None):
    """Render every message block; returns (blocks, embedded image count)."""
    img, blocks = [0], []
    for m in msgs:
        blocks.append(render_message(m, img_counter=img, lokal=lokal))
    return blocks, img[0]


_KEINE_DATEIEN = {"files": 0, "excluded": 0, "file_errors": 0}


def _seit_gilt(state, key, speicher):
    """TEAMS_SINCE bounds the first fetch into an empty store – not the
    first incremental read of an archive from before 9.0, whose record
    exists without a store: its history is on disk and must stay."""
    if SEIT is None or not speicher.leer():
        return None
    rec = _bekannt(state, key)
    return SEIT if rec is None or rec.get("v") == 2 else None


def chat_delta_url(key, wasserzeichen):
    """Only what moved since the newest stored message – Graph honours the
    filter solely alongside an $orderby on the same property."""
    return (f"{GRAPH}/me/chats/{key}/messages?$top={PAGE}"
            f"&$orderby=lastModifiedDateTime desc"
            f"&$filter=lastModifiedDateTime gt {wasserzeichen}")


def lade_chat_nachrichten(graph, key, speicher, seit=None):
    """Bring a chat's store up to date: the first fetch lists everything
    (bounded by `seit`), every later one only what was modified after the
    stored watermark."""
    base = f"{GRAPH}/me/chats/{key}/messages"
    wasser = None if speicher.leer() else graph_zeitstempel(speicher.wasserzeichen())
    if wasser:
        speicher.merge(list(graph.paged(chat_delta_url(key, wasser))))
        return
    params = {"$top": PAGE}
    if seit is not None:
        params["$orderby"] = "createdDateTime desc"   # newest first: stop at the day
    msgs = []
    for m in graph.paged(base, params):
        if seit is not None:
            dt = parse_ts(m.get("createdDateTime"))
            if dt is not None and dt < seit:
                break
        msgs.append(m)
    speicher.ersetze(msgs)


def export_one_chat(graph, out, state, my_id, chat):
    t0 = time.monotonic()
    key = chat["id"]
    folder = TYPEMAP.get(chat.get("chatType"), "other")
    title = chat_title(graph, chat, my_id)
    prior = get_record(out, state, key)
    db = state_db.StateDb(out)
    speicher = Nachrichtenspeicher(db, key)
    lade_chat_nachrichten(graph, key, speicher, _seit_gilt(state, key, speicher))
    msgs = speicher.nachrichten()
    msgs.sort(key=lambda m: m.get("createdDateTime") or "")

    # Only system/event messages and no real message? -> not exported by default
    real = sum(1 for m in msgs if m.get("messageType", "message") == "message")
    if SKIP_EMPTY_CHATS and real == 0:
        cleanup_old(out, prior, None)   # remove a file possibly written earlier
        last_act = newest_iso(m.get("createdDateTime") for m in msgs)
        speicher.sichern()
        record_done(out, state, key, folder, title, None, len(msgs),
                    last_activity=last_act, empty=True)
        return ("empty", folder, title, len(msgs), time.monotonic() - t0,
                dict(_KEINE_DATEIEN))

    meta = f"{len(msgs)} Nachrichten · Chat-ID {key}"
    fname = f"{safe(title)}__{short_id(key)}.html"
    new_rel = f"{folder}/{fname}"
    lokal, zahlen = None, dict(_KEINE_DATEIEN)
    if ATTACHMENTS:
        anhaenge_umziehen(out, prior, new_rel)
        lokal, zahlen["files"], zahlen["excluded"], zahlen["file_errors"] = \
            anhaenge_laden(graph, out, key, new_rel, msgs,
                           unveraendert=not speicher.geaendert())
    blocks, nimg = render_blocks(msgs, lokal)
    (out / folder).mkdir(parents=True, exist_ok=True)
    (out / folder / fname).write_text(
        render_conversation(title, SUBNAME.get(folder, "Chat"), meta, blocks),
        encoding="utf-8")
    cleanup_old(out, prior, new_rel)   # remove old 'Unbekannt__…' file if renamed
    speicher.sichern()
    last_act = newest_iso(m.get("createdDateTime") for m in msgs)
    record_done(out, state, key, folder, title, new_rel, len(msgs),
                last_activity=last_act)
    return ("updated" if prior else "new", folder, title, len(msgs),
            time.monotonic() - t0, zahlen)


def _ist_status(e, status):
    return getattr(getattr(e, "response", None), "status_code", None) == status


def _delta_runde(graph, url, params=None):
    """One round of change tracking: (messages, the new deltaLink)."""
    msgs = []
    data = graph.get(url, params)
    while True:
        msgs.extend(data.get("value") or [])
        nxt = data.get("@odata.nextLink")
        if not nxt:
            break
        data = graph.get(nxt)
    return msgs, data.get("@odata.deltaLink")


def _antworten(graph, root):
    """The replies of one root post, in full – the inline ones first, the
    rest (beyond 1000) via replies@odata.nextLink."""
    replies = list(root.get("replies") or [])
    nxt = root.get("replies@odata.nextLink")
    while nxt:
        data = graph.get(nxt)
        replies.extend(data.get("value") or [])
        nxt = data.get("@odata.nextLink")
    return replies


def _uebernehmen(graph, base, speicher, roots):
    """Merge the root posts a delta round reported and re-read the replies
    of exactly those posts – the delta never carries replies."""
    for m in roots:
        speicher.merge([m])
        if m.get("replyToId") or not m.get("id"):
            continue
        try:
            replies = list(graph.paged(f"{base}/{m['id']}/replies", {"$top": PAGE}))
        except requests.HTTPError as e:
            if _ist_status(e, 404):
                continue          # the thread is gone – its stored replies stay
            raise
        speicher.ersetze_antworten(str(m["id"]), replies)


def _thread_stempel(root, replies):
    """The last activity of a whole thread – a deletion counts too."""
    return newest_iso(t for m in (root, *replies)
                      for t in (m.get("createdDateTime"), m.get("lastModifiedDateTime"),
                                m.get("deletedDateTime")))


def _juengste_antworten(graph, base, speicher):
    """The threads active within the window, replies inline – Graph lists
    channel posts by the last activity of the whole thread, newest first,
    so the walk stops at the first thread older than the window. This is
    how a reply on an unchanged post gets in: the delta never names it."""
    grenze = datetime.now(UTC) - timedelta(days=ANTWORT_FENSTER_TAGE)
    for root in graph.paged(base, {"$top": PAGE, "$expand": "replies"}):
        replies = _antworten(graph, root)
        dt = parse_ts(_thread_stempel(root, replies))
        if dt is not None and dt < grenze:
            break
        speicher.merge([root])
        if root.get("id"):
            speicher.ersetze_antworten(str(root["id"]), replies)


def _vollpass_faellig(db, ch_id):
    """Is the weekly full read of this channel due?"""
    roh = db.kv_lesen(f"replies_full:{ch_id}")
    try:
        return not roh or time.time() - float(roh) >= ANTWORT_VOLLPASS_TAGE * 86400
    except ValueError:
        return True


def lade_kanal_nachrichten(graph, team_id, ch_id, speicher, link, seit=None,
                          vollpass=False):
    """Bring a channel's store up to date.

    With a delta link (and a store behind it) only the changed root posts
    come, plus the replies of those posts and of every thread active within
    the window. Without one – first run, empty store, a 410, or the weekly
    full pass – the channel is read in full with its replies inline, and an
    initial delta call bounded at the instant the listing started hands
    over the link for the next run at the price of a page or two; whatever
    moved while the listing ran is reported once more and merges
    idempotently. Returns (the link to keep, whether change tracking had
    to restart, the error when Graph refused that initial call – the
    listing itself stands then, whether the full read ran)."""
    base = f"{GRAPH}/teams/{team_id}/channels/{ch_id}/messages"
    zurueckgesetzt = False
    if link and not speicher.leer() and not vollpass:
        try:
            roots, neuer_link = _delta_runde(graph, link)
        except requests.HTTPError as e:
            if not _ist_status(e, 410):
                raise
            zurueckgesetzt = True
        else:
            _uebernehmen(graph, base, speicher, roots)
            _juengste_antworten(graph, base, speicher)
            return neuer_link or link, False, None, False

    start = datetime.now(UTC) - timedelta(minutes=5)   # clock skew margin
    alle = []
    for root in graph.paged(base, {"$top": PAGE, "$expand": "replies"}):
        if seit is not None:
            dt = parse_ts(root.get("createdDateTime"))
            if dt is not None and dt < seit:
                continue          # a thread started before the day stays out
        alle.append(root)
        alle.extend(_antworten(graph, root))
    speicher.ersetze(alle)
    grenze = graph_zeitstempel(start.isoformat())
    try:
        roots, neuer_link = _delta_runde(graph, f"{base}/delta",
                                         {"$top": PAGE,
                                          "$filter": f"lastModifiedDateTime gt {grenze}"})
    except requests.HTTPError as e:
        # The listing came, only the change tracking refused (Graph knows
        # channels whose delta answers 400): the channel is complete, the
        # next run reads it in full again.
        return None, zurueckgesetzt, f"{type(e).__name__}: {e}", True
    _uebernehmen(graph, base, speicher, roots)
    return neuer_link, zurueckgesetzt, None, True


def kanal_reihe(msgs):
    """Root posts in order, each followed by its replies – (message, is
    reply). A reply whose root is not stored still appears, at the end."""
    roots = sorted((m for m in msgs if not m.get("replyToId")),
                   key=lambda m: m.get("createdDateTime") or "")
    je_root = {}
    for m in msgs:
        if m.get("replyToId"):
            je_root.setdefault(str(m["replyToId"]), []).append(m)
    reihe = []
    for root in roots:
        reihe.append((root, False))
        for rep in sorted(je_root.pop(str(root.get("id")), []),
                          key=lambda m: m.get("createdDateTime") or ""):
            reihe.append((rep, True))
    for rest in je_root.values():
        reihe.extend((rep, True) for rep in
                     sorted(rest, key=lambda m: m.get("createdDateTime") or ""))
    return reihe


def export_one_channel(graph, out, state, team, ch, spiegel=None):
    t0 = time.monotonic()
    tname = team.get("displayName", "Team")
    cname = ch.get("displayName", "Kanal")
    title = f"{tname} / {cname}"
    key = f"ch:{ch['id']}"
    prior = get_record(out, state, key)
    db = state_db.StateDb(out)
    speicher = Nachrichtenspeicher(db, ch["id"])
    delta_kv = f"delta:{ch['id']}"
    link = db.kv_lesen(delta_kv) or None
    neuer_link, zurueckgesetzt, delta_fehler, voll = lade_kanal_nachrichten(
        graph, team["id"], ch["id"], speicher, link,
        seit=_seit_gilt(state, key, speicher), vollpass=_vollpass_faellig(db, ch["id"]))
    if zurueckgesetzt:
        progress.event("run.teams.delta_reset", name=title)
    if delta_fehler:
        progress.event("run.teams.delta_failed", "warn", name=title, error=delta_fehler)

    def stand_merken():
        """The channel finished cleanly: advance the link (or clear one
        the 410 invalidated) and date the full pass."""
        if neuer_link and neuer_link != link:
            db.kv_schreiben(delta_kv, neuer_link)
        elif zurueckgesetzt and link:
            db.kv_schreiben(delta_kv, "")
        if voll:
            db.kv_schreiben(f"replies_full:{ch['id']}", str(time.time()))

    reihe = kanal_reihe(speicher.nachrichten())
    count = len(reihe)
    fp = newest_iso(t for m, _r in reihe
                    for t in (m.get("createdDateTime"), m.get("lastModifiedDateTime")))
    fname = f"{safe(cname)}__{short_id(ch['id'])}.html"
    new_rel = f"channels/{safe(tname)}/{fname}"
    unveraendert = not speicher.geaendert()

    # Nothing moved since the last run? -> don't rewrite. A mirror that just
    # brought files the posts point at is a change too: the links must move
    # from the cloud to the copy.
    if (prior and unveraendert and prior.get("rel") == new_rel
            and not (spiegel and spiegel.get("neu"))):
        stand_merken()
        return ("unchanged", "channels", title, count,
                time.monotonic() - t0, dict(_KEINE_DATEIEN))

    meta = f"{count} Nachrichten (inkl. Antworten) · {ch.get('membershipType', 'standard')}"
    msgs = [m for m, _r in reihe]
    lokal, zahlen = {}, dict(_KEINE_DATEIEN)
    if spiegel:
        lokal = spiegel_links(out, spiegel, msgs)
    if ATTACHMENTS:
        anhaenge_umziehen(out, prior, new_rel)
        lokal, zahlen["files"], zahlen["excluded"], zahlen["file_errors"] = \
            anhaenge_laden(graph, out, key, new_rel, msgs, ausser=lokal,
                           unveraendert=unveraendert)
    img, blocks = [0], []
    for m, antwort in reihe:
        blocks.append(render_message(m, is_reply=antwort, img_counter=img,
                                     lokal=lokal or None))
    tdir = out / "channels" / safe(tname)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / fname).write_text(
        render_conversation(title, "Team-Kanal", meta, blocks), encoding="utf-8")
    cleanup_old(out, prior, new_rel)
    speicher.sichern()
    stand_merken()                     # only now: the channel is clean
    record_done(out, state, key, "channels", title, new_rel, count, last_activity=fp)
    return ("updated" if prior else "new", "channels", title, count,
            time.monotonic() - t0, zahlen)


_SAUBER = ("new", "updated", "unchanged", "empty")


def make_runner(graph, out, state, my_id, kind, a, b, spiegel=None, fehler=None, takt=None):
    """The unit of work of one worker; `fehler` collects the categories
    whose run was not clean – they keep their cadence mark – and `takt`
    stamps a conversation that finished without error."""
    cat = TYPEMAP.get(a.get("chatType"), "other") if kind == "chat" else "channels"
    key = a["id"] if kind == "chat" else b["id"]

    def run():
        if STOP.is_set():
            return ("stopped", None, None, 0, 0.0)
        try:
            if kind == "chat":
                res = export_one_chat(graph, out, state, my_id, a)
            else:
                res = export_one_channel(graph, out, state, a, b,
                                         spiegel=(spiegel or {}).get(b["id"]))
            zahlen = res[5] if len(res) > 5 else {}
            if takt is not None and res[0] in _SAUBER and not zahlen.get("file_errors"):
                takt.stempeln(key)
            return res
        except TokenExpired:
            STOP.set()
            if fehler is not None:
                fehler.add(cat)
            return ("expired", None, None, 0, 0.0)
        except Exception as e:
            if fehler is not None:
                fehler.add(cat)
            return ("error", None, f"{e}", 0, 0.0)
    return run


# ---------------------------------------------------------------------------
# Job building (in the main thread) + parallel driver
# ---------------------------------------------------------------------------
def _ausgeschlossen(stats):
    stats["excluded"] = stats.get("excluded", 0) + 1


def build_chat_jobs(graph, out, state, stats, my_id, chat_cats, regeln=None, takt=None):
    progress.event("run.teams.chats_loading")
    chats = []
    # members + lastMessagePreview inline -> correct 1:1 names without an extra
    # call, and the per-chat activity timestamp for incremental runs
    for c in graph.paged(f"{GRAPH}/me/chats",
                         {"$top": PAGE, "$expand": "members,lastMessagePreview"}):
        chats.append(c)
        if len(chats) % 50 == 0:
            progress.melde(len(chats), what="chats")
    wanted = [c for c in chats if TYPEMAP.get(c.get("chatType"), "other") in chat_cats]
    jobs, new, upd, same = [], 0, 0, 0
    for chat in wanted:
        folder = TYPEMAP.get(chat.get("chatType"), "other")
        if regeln or takt is not None:
            pfad = chat_pfad(folder, chat_title(graph, chat, my_id))
            if regeln and not folders.gilt(pfad, regeln):
                _ausgeschlossen(stats)
                continue
            if takt is not None and not takt.faellig(folder, pfad, chat["id"]):
                continue                      # counted and said once per category
        cur = (chat.get("lastMessagePreview") or {}).get("createdDateTime")
        rec = get_record(out, state, chat["id"])
        if rec is None:                       # never exported (or the file is missing)
            cs = parse_ts(cur)
            if (SEIT is not None and cs is not None and cs < SEIT
                    and _bekannt(state, chat["id"]) is None):
                _ausgeschlossen(stats)        # nothing newer than the start day
                continue
            jobs.append(("chat", chat, None))
            new += 1
            continue
        ps, cs = parse_ts(rec.get("last_activity")), parse_ts(cur)
        if cs is not None and (ps is None or cs > ps):
            jobs.append(("chat", chat, None))   # new messages -> export again
            upd += 1
        else:
            stats["skipped"] += 1               # unchanged
            same += 1
            if takt is not None:
                takt.stempeln(chat["id"])       # found unchanged, without error
    progress.event("run.teams.chats", n=len(wanted), new=new, updated=upd,
                   unchanged=same)
    return jobs


def build_channel_jobs(graph, out, state, stats, selected_teams, regeln=None, fehler=None,
                       takt=None):
    jobs = []
    for team in selected_teams:
        tname = team.get("displayName", "Team")
        try:
            channels = list(graph.paged(f"{GRAPH}/teams/{team['id']}/channels", {"$top": PAGE}))
        except TokenExpired:
            raise
        except Exception as e:
            progress.event("run.teams.channels_failed", "warn",
                           name=tname, error=str(e))
            if fehler is not None:
                fehler.add("channels")
            continue
        for ch in channels:
            pfad = kanal_pfad(tname, ch.get("displayName", "Kanal"))
            if regeln and not folders.gilt(pfad, regeln):
                _ausgeschlossen(stats)
            elif takt is not None and not takt.faellig("channels", pfad, ch["id"]):
                continue                      # counted and said once per category
            elif REFRESH_CHANNELS:
                # the delta link says what changed; the worker only
                # rewrites on an actual change
                jobs.append(("channel", team, ch))
            elif already_done(out, state, f"ch:{ch['id']}"):
                stats["skipped"] += 1
                if takt is not None:
                    takt.stempeln(ch["id"])
            else:
                jobs.append(("channel", team, ch))
    if REFRESH_CHANNELS:
        progress.event("run.teams.channels_check", n=len(jobs))
    else:
        progress.event("run.teams.channels_export", n=len(jobs))
    return jobs


def run_parallel(runners, stats, workers, fehler=None):
    if not runners:
        return "done"
    expired = False
    total = len(runners)
    done_count = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(r) for r in runners]
        progress.melde(0, total, "chats")
        for fut in as_completed(futs):
            done_count += 1
            progress.melde(done_count, total, "chats")
            try:
                res = fut.result()
            except Exception as e:
                res = ("error", None, str(e), 0, 0.0)
                if fehler is not None:
                    fehler.add("*")
            status, cat, label, count = res[0], res[1], res[2], res[3]
            secs = res[4] if len(res) > 4 else 0.0
            zahlen = res[5] if len(res) > 5 else {}
            for k, v in zahlen.items():
                stats[k] = stats.get(k, 0) + int(v or 0)
            if fehler is not None and cat and zahlen.get("file_errors"):
                fehler.add(cat)
            dur = f"{secs:.0f}s" if secs >= 1 else f"{secs * 1000:.0f}ms"
            kind = progress.atom("export.cat." + cat) if cat else ""
            with PRINT_LOCK:
                # Thread-safe: one unbroken event line per conversation.
                if status in ("new", "ok"):
                    stats["new"] += 1
                    progress.event("run.conv.new", i=done_count, total=total,
                                   kind=kind, name=label, n=count, dur=dur)
                elif status == "updated":
                    stats["updated"] += 1
                    progress.event("run.conv.updated", i=done_count, total=total,
                                   kind=kind, name=label, n=count, dur=dur)
                elif status == "unchanged":
                    stats["skipped"] += 1   # checked, but no change
                    progress.event("run.conv.same", i=done_count, total=total,
                                   kind=kind, name=label)
                elif status == "empty":
                    stats["empty"] += 1
                    progress.event("run.conv.empty", i=done_count, total=total,
                                   kind=kind, name=label)
                elif status == "error":
                    progress.event("run.conv.failed", "err", i=done_count,
                                   total=total, name=label)
            if status == "expired":
                expired = True
            # "stopped" -> ignore
    return "expired" if expired else "done"


# ---------------------------------------------------------------------------
# The conversation list (--teams): what the rules can choose from
# ---------------------------------------------------------------------------
def liste_eintraege(graph, my_id):
    """Every chat of every category and every channel of every joined team,
    as the tree entries the rules see – the path is the archive path
    without the id tag and the .html, the name the display title."""
    progress.event("run.teams.chats_loading")
    eintraege = []
    for c in graph.paged(f"{GRAPH}/me/chats",
                         {"$top": PAGE, "$expand": "members,lastMessagePreview"}):
        folder = TYPEMAP.get(c.get("chatType"))
        if not folder or not c.get("id"):
            continue
        title = chat_title(graph, c, my_id)
        eintrag = {"id": c["id"], "pfad": chat_pfad(folder, title),
                   "name": title, "elemente": 0}
        zuletzt = (c.get("lastMessagePreview") or {}).get("createdDateTime")
        if zuletzt:
            eintrag["zuletzt"] = zuletzt      # the last activity, for the settings
        eintraege.append(eintrag)
    if not getattr(graph, "channels_enabled", False):
        return eintraege
    for team in select_teams(graph):
        tname = team.get("displayName", "Team")
        try:
            channels = list(graph.paged(f"{GRAPH}/teams/{team['id']}/channels", {"$top": PAGE}))
        except TokenExpired:
            raise
        except Exception as e:
            progress.event("run.teams.channels_failed", "warn", name=tname, error=str(e))
            continue
        for ch in channels:
            if not ch.get("id"):
                continue
            cname = ch.get("displayName", "Kanal")
            eintraege.append({"id": ch["id"], "pfad": kanal_pfad(tname, cname),
                              "name": cname, "elemente": 0})
    return eintraege


def gleiche_liste_ab(graph, out):
    """--teams: only fetch and store the conversation list, export nothing."""
    my_id = graph.get(f"{GRAPH}/me").get("id")
    vorher = folders.lade(out)
    daten = folders.speichere(out, liste_eintraege(graph, my_id), vorher)
    gewaehlt = folders.gewaehlt(daten, teams_regeln())
    progress.event("run.sync.result", total=len(daten["ordner"]), chosen=len(gewaehlt),
                   unit=progress.atom("progress.unit.conversations"))
    if daten["neu"] or daten["verschwunden"] or daten["umbenannt"]:
        progress.event("run.sync.changed", new=len(daten["neu"]),
                       gone=len(daten["verschwunden"]),
                       renamed=len(daten["umbenannt"]))
    progress.ergebnis(len(daten["neu"]),
                      extra={"total": len(daten["ordner"]),
                             "chosen": len(gewaehlt),
                             "gone": len(daten["verschwunden"]),
                             "renamed": len(daten["umbenannt"])})
    return daten


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
_hilfe_gewuenscht = export_util.hilfe_gewuenscht


def main():
    if _hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__.strip())
        return

    global _client, IMGCACHE_DIR, SEIT
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    workers = settings.number("EXPORT_WORKERS", "workers")
    graph_client.konfiguriere(workers)
    out = export_util.ausgabeordner(argv)
    out.mkdir(parents=True, exist_ok=True)

    if "--teams" in sys.argv[1:]:
        graph = _zugang(want_channels=True)
        _client = graph
        try:
            gleiche_liste_ab(graph, out)
        except TokenExpired:
            progress.fehler("token_expired")
            sys.exit(1)
        return

    # 1) Determine categories (before the login, so the channel scope is
    #    only requested when needed) and their cadences
    cat_options = [("1on1", "1:1-Chats"), ("group", "Gruppenchats"),
                   ("meeting", "Meeting-Chats"), ("channels", "Team-Kanäle")]
    root_db = state_db.StateDb(out)
    kadenzen = export_util.kadenzen()
    categories, uebersprungen = faellige_kategorien(root_db, selected_categories(cat_options),
                                                    kadenzen)
    if not categories:
        progress.ergebnis(0, extra={"skipped": uebersprungen} if uebersprungen else None)
        return
    want_channels = "channels" in categories

    # 2) Login or token mode
    graph = _zugang(want_channels)
    _client = graph
    if want_channels and not graph.channels_enabled:
        progress.event("run.teams.channels_denied", "warn", error="")
        categories.discard("channels")
        want_channels = False

    if EMBED_IMAGES and CACHE_IMAGES:
        IMGCACHE_DIR = out / ".imgcache"
        IMGCACHE_DIR.mkdir(parents=True, exist_ok=True)
    SEIT = seit_grenze()
    seit_roh = (os.environ.get("TEAMS_SINCE") or "").strip()
    if seit_roh and SEIT is None:
        progress.event("run.teams.since_invalid", "warn", value=seit_roh)
    regeln = teams_regeln()
    state = load_state(out)
    stats = {"new": 0, "updated": 0, "skipped": 0, "empty": 0,
             "files": 0, "excluded": 0, "file_errors": 0, "gone": 0}
    fehler_kats = set()
    takt = Taktung(root_db, kadenzen)
    result = "done"

    try:
        my_id = graph.get(f"{GRAPH}/me").get("id")

        selected_teams = select_teams(graph, fehler=fehler_kats) if want_channels else []

        chat_cats = categories & {"1on1", "group", "meeting"}
        chat_jobs = (build_chat_jobs(graph, out, state, stats, my_id, chat_cats,
                                     regeln=regeln, takt=takt)
                     if chat_cats else [])
        if not chat_cats:
            progress.event("run.teams.chats_skipped")
        channel_jobs = (build_channel_jobs(graph, out, state, stats, selected_teams,
                                           regeln=regeln, fehler=fehler_kats, takt=takt)
                        if (want_channels and selected_teams) else [])
        takt.melden()
        if stats["excluded"]:
            progress.event("run.teams.excluded", n=stats["excluded"])

        # The channel folders first, in this thread: the posts then link
        # into the mirror, and drive_base is touched by nobody else.
        spiegel = {}
        if CHANNEL_FILES and channel_jobs:
            spiegel, zahlen = kanal_dateien_spiegeln(graph, out, channel_jobs)
            stats["files"] += zahlen["new"]
            stats["excluded"] += zahlen["excluded"]
            stats["file_errors"] += zahlen["errors"]
            stats["gone"] += zahlen["gone"]
            if zahlen["errors"]:
                fehler_kats.add("channels")

        runners = [make_runner(graph, out, state, my_id, k, a, b, spiegel,
                               fehler=fehler_kats, takt=takt)
                   for (k, a, b) in (chat_jobs + channel_jobs)]
        if runners:
            progress.event("run.teams.exporting", n=len(runners))
        result = run_parallel(runners, stats, workers, fehler=fehler_kats)
    except TokenExpired:
        result = "expired"

    if result == "expired":
        progress.fehler("token_expired")
        sys.exit(1)
    kadenz_abschliessen(root_db, categories, fehler_kats)

    # Updated conversations count as well: their files have changed, so the
    # index knows them only in the old version. Files fetched count too –
    # they are new archive content the index has not seen.
    extra = {"updated": stats["updated"], "empty": stats["empty"]}
    if ATTACHMENTS or CHANNEL_FILES:
        extra.update(files=stats["files"], gone=stats["gone"])
    uebersprungen += takt.uebersprungen()
    if uebersprungen:
        extra["skipped"] = uebersprungen
    mit_dateien = ATTACHMENTS or CHANNEL_FILES
    progress.ergebnis(stats["new"] + stats["updated"] + stats["files"],
                      unchanged=stats["skipped"],
                      excluded=stats["excluded"] if (mit_dateien or stats["excluded"]) else None,
                      errors=stats["file_errors"] if mit_dateien else None,
                      extra=extra)


if __name__ == "__main__":
    main()
