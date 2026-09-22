#!/usr/bin/env python3
"""
mcp_server.py – expose the Teams + Outlook exports to Claude as an MCP server.

Instead of generating answers with a local LLM, this server hands the
*retrieval* to Claude as MCP tools and lets Claude be the reasoning/answer
layer. It reads the store built by rag_index.py:

    corpus.db     SQLite with all chunks + an FTS5 (BM25) full-text index and a
                  precomputed people table. Queried on demand – the server keeps
                  (almost) nothing in RAM and starts instantly.
    vectors-N.npy float16 embedding matrix, memory-mapped – the OS pages in only
                  what a query touches. info.json names the current one; a
                  new index run writes a new file instead of replacing this
                  one, which is what lets it run while this server holds the
                  mapping (see store_layout.py).

Ranking backends, per query:
  • hybrid   – default when embeddings are available: FTS5/BM25 and semantic
               cosine ranking run side by side and are merged with Reciprocal
               Rank Fusion. Exact tokens (invoice numbers, names) and
               paraphrases both hit.
  • semantic – cosine only (needs numpy + Ollama for the query embedding).
  • lexical  – FTS5/BM25 only, standard library, no Ollama needed. Automatic
               fallback when Ollama is down.

Tools: search_messages, browse_messages, get_document, list_people,
read_source_file, corpus_stats. Every hit carries an o365:// resource URI;
the corresponding MCP resource returns the raw source file.

Install (SDK required; numpy/requests only for semantic/hybrid ranking):
    pip install -r requirements.txt   # pinned; mcp 2.x (MCPServer API)

Run (HTTP, default – one shared server for all Claude sessions):
    python3 mcp_server.py --store rag_store \
        --teams teams_export --outlook outlook_export
    # → MCP endpoint at http://127.0.0.1:8365/mcp

    Register in Claude Code (.mcp.json):
        {"mcpServers": {"munimentum":
            {"type": "http", "url": "http://127.0.0.1:8365/mcp"}}}

    The server binds to 127.0.0.1 and has no authentication – it serves your
    complete mail and chat history, so keep it local. On loopback the SDK
    validates the Host and Origin headers, which stops a web page you happen
    to visit from talking to the server through your browser (DNS rebinding).

    That protection does not apply to any other bind address, so binding one
    requires naming the hostnames clients will use; the server refuses to
    start otherwise:
        python3 mcp_server.py --host 0.0.0.0 --allowed-host nas.local

Run (stdio – auto-launched per client, the classic setup):
    python3 mcp_server.py --transport stdio [--store …]

Switched off in the app (Settings → MCP server), this program serves nothing –
over stdio as well as HTTP. It does not exit: it runs a server that offers a
single tool saying so, which the client's model reads and passes on. Exiting
would leave the user with a failed connection and the reason in a log file. The
check sits in main() and nowhere else: the app calls the same functions
in-process for its own search, and what is switched off is the SERVER, not
reading the index. --force serves anyway.
"""

import os
import re
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta
from urllib.parse import quote, unquote

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import analytics_db
import detail
import export_util
import completeness
import corpus
import faelle
import i18n
import ollama_client
import run_history
import settings
import state_db
import steps
import store_layout
import version

# Windows consoles default to a legacy code page; force UTF-8 so logging the
# Unicode in messages never raises (no-op on macOS/Linux).
export_util.erzwinge_utf8()

STATE = {}          # populated in main(): db path, V (mmap), np, dirs, flags

# Server-level guidance, handed to the client together with the tool list –
# i.e. *before* a tool is picked, unlike the per-tool docstrings below. Keep it
# about choosing between the tools; details belong in the docstrings.
_INSTRUCTIONS = """\
Offline archive of the user's own Teams chats, Outlook mail, calendar and
contacts, OneDrive and SharePoint files, SharePoint pages, Planner boards,
To Do lists and OneNote notebooks. Everything is local and read-only; there is no live mailbox access, so
anything not exported is simply absent – and the archive has edges: it
starts and ends somewhere, sources sync on their own cadence, and months can
be empty. corpus_stats knows those edges; ask it before concluding that
something does not exist.

Which tool to use:
  • list_sources    – START HERE once per session: which sources this archive
    holds, what is indexed in each (mail, Teams, pages, OneNote pages,
    Planner and To Do tasks by full text; calendar and contacts by their
    text; OneDrive, SharePoint and Teams files by NAME, PATH and TYPE only
    – contents are not indexed), what `folder` and `person` mean per
    source, and what was never exported.
  • search_messages – the default entry point for content. Ranking depends on this archive:
    with embeddings it fuses BM25 and semantic scoring, so exact tokens and
    paraphrases both work; without them it is BM25 only, and a paraphrase will
    miss. Every result says which was used in its "backend" field ("hybrid",
    "semantic" or "lexical") – when it reads "lexical", search with the words
    that would literally appear in the text rather than describing the idea.
    corpus_stats says the same thing up front.
  • browse_messages – when there is no query, only filters ("everything from
    Bob in June"). Newest first.
  • list_cases / get_case / case_timeline / case_people – the user's CASES:
    matters they collect items, result lists and saved searches around.
    "Summarise the Nordwind case" → get_case for the casebook, the folders
    and what it holds, case_timeline for the chronology with excerpts,
    case_people for who is involved; search_messages(case=…, case_folder=…)
    searches inside one case or one of its folders. Every hit carries
    `cases` – where it already sits, folder included. case_new_hits says
    what the attached searches find that the case lacks; add_to_case (into
    a folder the user made, with a `remark` saying why) and add_case_note
    write, but only when the user allowed that in Munimentum – what they
    wrote is marked "via MCP". Every item of a case can carry a remark;
    get_case shows them.
  • list_saved_searches / run_saved_search – the user's saved searches, run
    exactly as saved.
  • get_document    – full text of one hit, via the uid from a search/browse
    result. For chats, context_before/context_after return the neighbouring
    messages of the conversation.
  • list_people     – resolve a name before filtering; the person filter is a
    substring match over names and addresses. Files and pages carry none.
  • list_folders / list_filetypes – what the folder and filetype filters can
    take, per source: mailbox folders, calendars, Teams conversation kinds,
    OneDrive folders, SharePoint site/library, Planner boards, To Do lists,
    OneNote notebooks, pages sites.
  • list_events     – appointments structured (start, end, location,
    attendees), including those recovered from invitation and cancellation
    mails; the search knows them only as text. Upcoming ones need
    date_from/date_to.
  • lookup_contact  – the address book, structured: e-mail, phone,
    organisation.
  • list_files      – browse OneDrive, the SharePoint libraries, the files
    shared in Teams and mirrored channel folders, and the Planner
    attachments folder by folder; the files' contents are not indexed, only
    their names.
  • corpus_stats    – what is indexed, which ranking backend is live, and how
    far the archive reaches: coverage (first and last message), gaps (months
    without a single message) and when each source last exported
    successfully. "No hits" in an archive that ends in May says nothing
    about June.
  • archive_analytics – the archive about itself: messages per source,
    timeline and gaps, attachment and file types, the largest files, the
    people the user exchanges the most with – the same block the app's
    Analytics tab shows. For questions about the archive rather than its
    contents.
  • read_source_file – last resort: the raw .eml/.html/.ics/.vcf file, a
    rendered page or a board. Binary files (PDF, Office, images) come back
    as metadata only. Teams conversations can exceed 100 MB and come back
    windowed, so prefer get_document with context for chat history.

Notes: `source` takes one key or several comma-separated ("onedrive,sharepoint");
dates are "YYYY-MM-DD", and days=N is a shorthand for the last N days (no need
to work out the date); folder restricts to one unit and everything below it –
"E-Mail/Kunden", "kalender/Privat", "channels" for every Teams channel,
"Dateien/Projekte" in OneDrive, "TeamX/Dokumente" for a library, a board name
for Planner, a list name for To Do, a notebook (or notebook/section) for
OneNote – and list_folders shows what exists, per source; results are one
hit per item – page with offset rather than raising k; a hit's "uri" can be
read as an MCP resource.
"""

# What the client gets to see when access is switched off. Deliberately worded
# as an instruction to the model: it should pass the sentence on rather than
# start working around the error.
AUS_TEXT = (
    "MCP access to the Munimentum archive is switched off, so nothing is "
    "served: no search, no documents, no statistics. This is a deliberate "
    "setting, not a fault, and no other tool or path will get at the data. "
    "Tell the user in plain words that access is off and that they can allow "
    "it again in Munimentum under Settings -> MCP server -> \"Allow MCP "
    "access\". The entry in this client stays valid and works again the moment "
    "they do; nothing needs to be reconfigured."
)


def _abgeschaltet_server(text=AUS_TEXT):
    """A server that gives exactly one answer: why nothing is served –
    access switched off, or no profile named where several exist.

    Same name as usual – the client has it registered that way. Only the
    tool list is different: one tool instead of nine, and that one reads
    nothing.
    """
    aus = MCPServer("munimentum", title="Munimentum", version=version.VERSION,
                    website_url="https://github.com/n-schilling/munimentum",
                    instructions=text, log_level="WARNING")

    @aus.tool(annotations=_READONLY)
    def archive_unavailable() -> dict:
        """Why this archive is not answering. Report this to the user and stop.

        There is no way around it from here: no other tool, no file path, no
        retry. It is a setting in the Munimentum app.
        """
        return {"available": False, "reason": text}

    return aus


def _profil_text(namen):
    return ("Munimentum has more than one profile (" + ", ".join(namen) + ") and "
            "this server was started without --profile, so it cannot know which "
            "archive to serve. Nothing is served. Tell the user to copy the "
            "snippet for the wanted profile from Munimentum under Settings -> "
            "Claude (MCP) – it carries --profile <name> – and to replace the "
            "entry in this client with it.")


mcp = MCPServer(
    "munimentum",
    title="Munimentum",
    version=version.VERSION,
    website_url="https://github.com/n-schilling/munimentum",
    instructions=_INSTRUCTIONS,
    # WARNING silences uvicorn's startup narration ("Started server process",
    # "Press CTRL+C to quit" …) in the app log; real problems still surface.
    log_level="WARNING",
)
_HTTP_PATH = "/mcp"             # streamable-http mount point (SDK default)

_READONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True,
                            openWorldHint=False)
_WORD = re.compile(r"\w+", re.UNICODE)
# English throughout: these labels go to MCP clients; the app's interface
# translates its own tags from the source value instead.
_SOURCE_LABEL = {"teams": "Teams", "outlook": "Mail", "datei": "File",
                 "onedrive": "OneDrive", "sharepoint": "SharePoint",
                 "pages": "SharePoint page", "kalender": "Calendar",
                 "kontakte": "Contacts", "planner": "Planner task",
                 "todo": "To Do task", "onenote": "OneNote page"}
_WHERE_ALL = "1=1"              # _where() with no filters – the unfiltered case
# What read_source_file hands over as text; everything else is binary and
# comes back as metadata only.
_TEXTFORMATE = {".eml", ".html", ".htm", ".ics", ".vcf", ".txt", ".md",
                ".json", ".csv", ".xml", ".log"}
_RRF_K = 60                     # standard reciprocal-rank-fusion constant
_POOL_MIN, _POOL_MAX = 100, 1000  # candidate pool per backend before merging

# Lower bound for the semantic search (cosine, normalized vectors).
#
# Without it, it ALWAYS returns the best k within the filter – even when
# nothing matches. Narrow the search to one day and look for a word, and you
# get every message of that day, sorted by similarity. Reported exactly like
# that: 18 hits, 2 of which contained the word.
#
# Measured on a real index (bge-m3): a nonsense query never gets past 0.435,
# while real queries still sit at 0.50–0.63 by the 40th hit. There is room in
# between. Anyone using a different model adjusts it.
def _sem_min():
    """Configured as a percentage (0–95), used here as a cosine value.

    Percent because the interface can then use a plain number field and
    nobody trips over a decimal separator. A number above 1 is therefore read
    as a percentage – even when someone puts it into the file by hand.
    """
    roh = os.environ.get("SEMANTIC_MIN")
    if roh is None:
        roh = settings.value("semantic_min")
    try:
        wert = float(roh)
    except (TypeError, ValueError):
        return 0.45
    if wert > 1:
        wert /= 100.0
    return min(max(wert, 0.0), 0.99)


SEM_MIN = _sem_min()


def _db():
    """Fresh read-only connection per call – safe across MCP worker threads."""
    con = sqlite3.connect(f'file:{STATE["db"]}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.create_function("extern", 2, _extern_sql, deterministic=True)
    # SQLite's LIKE folds ASCII only. Everything the indexer writes is
    # already lower-case – `who` is the exception, it keeps the display
    # name as it stands, so a comparison against it folds here.
    con.create_function("py_lower", 1,
                        lambda s: s.lower() if isinstance(s, str) else s,
                        deterministic=True)
    return con


def _intern_domains():
    """The user's own mail domains, lower-case: what the app hands over
    (the setting, else the signed-in account's domain), or – started by
    hand – the setting alone."""
    roh = STATE.get("internal_domains")
    if roh is None:
        roh = settings.value("internal_domains", "")
    return sorted({d.strip().lower() for d in str(roh or "").replace(",", " ").split() if d.strip()})


def _extern_sql(domains, intern):
    """SQL function `extern(domains, intern)`: 1 when one of the item's
    party domains lies outside the internal ones, 0 when all lie inside,
    NULL when the item names no party – so it drops out of both sides."""
    if not domains:
        return None
    drinnen = set(str(intern or "").split())
    return 1 if any(d not in drinnen for d in str(domains).split()) else 0


# --------------------------------------------------------------------------
# Filters (SQL WHERE fragments shared by all query tools)
# --------------------------------------------------------------------------
def _hat_spalte(con, name):
    """Does the index know this column yet?

    An index from an older build does not have it. Without this check, a
    click on "Deleted only" ended in a SQL error instead of a hint.
    """
    return any(r[1] == name for r in con.execute("PRAGMA table_info(chunks)"))


# Which sources offer a folder selection – all whose ctx is a path.
_LISTBAR = ("outlook", "datei", "onedrive", "sharepoint", "pages",
            "kalender", "teams", "kontakte", "planner", "todo", "onenote")


def _quellen(text):
    """The source filter as a list: "onedrive,sharepoint" names two sources.
    "all", empty and whitespace are dropped; an empty list means no filter."""
    return [q for q in (s.strip().lower() for s in str(text or "").split(","))
            if q and q != "all"]


def _quelle_cond(quelle):
    """One or more sources as SQL condition – the mirrors are told apart.

    "onedrive" and "sharepoint" are both src='datei' rows; the stored root
    column separates them. "datei" stays as the umbrella for every file
    mirror. "teams" means the conversations AND the files next to them –
    what a chat shared belongs to the chat. Several sources
    ("onedrive,sharepoint") become an OR; no source means no filter.
    """
    teile, werte = [], []
    for q in _quellen(quelle):
        if q in ("onedrive", "sharepoint"):
            teile.append("(src = 'datei' AND root = ?)")
        elif q == "teams":
            teile.append("(src = 'teams' OR (src = 'datei' AND root = 'teams'))")
            continue
        else:
            teile.append("src = ?")
        werte.append(q)
    if not teile:
        return "1=1", []
    if len(teile) == 1:
        return teile[0], werte
    return "(" + " OR ".join(teile) + ")", werte


# What each source holds and how the filters read there – the same words the
# tool descriptions use, handed to the client by list_sources so it never has
# to guess whether a person filter or a folder means anything for a source.
_QUELLEN_INFO = {
    "outlook": {"label": "Mail",
                "indexed": "full text of every mail, plus attachment names "
                           "and types (attachment contents are not indexed)",
                "folder": "mailbox folder path, e.g. E-Mail/Kunden – the "
                          "folder and everything below it",
                "person": "sender and recipients", "step": "outlook"},
    "teams": {"label": "Teams",
              "indexed": "full text of chats and channel messages; the files "
                         "a message shared and the mirrored channel folders "
                         "by NAME, PATH and TYPE only (when the export fetches "
                         "them – list_files root \"teams\")",
              "folder": "the kind of conversation: 1on1, group, meeting, "
                        "channels",
              "person": "the author (files carry none)", "step": "teams"},
    "kalender": {"label": "Calendar",
                 "indexed": "appointments of the exported calendars – title, "
                            "description, location; list_events returns them "
                            "structured and adds the appointments recovered "
                            "from invitation and cancellation mails",
                 "folder": "the calendar, e.g. kalender/Arbeit",
                 "person": "organizer and attendees", "step": "outlook"},
    "kontakte": {"label": "Contacts",
                 "indexed": "the address book – name, organisation, e-mail, "
                            "phone; lookup_contact returns it structured",
                 "folder": "the contact folder, e.g. kontakte/Team",
                 "person": "the contact's name and addresses",
                 "step": "outlook"},
    "onedrive": {"label": "OneDrive files",
                 "indexed": "NAME, PATH and TYPE only – file contents are "
                            "not indexed and cannot be searched",
                 "folder": "folder path in the mirror, e.g. Dateien/Projekte",
                 "person": None, "step": "onedrive"},
    "sharepoint": {"label": "SharePoint files",
                   "indexed": "NAME, PATH and TYPE only – file contents are "
                              "not indexed and cannot be searched",
                   "folder": "site/library, optionally deeper: "
                             "TeamX/Dokumente/Projekte",
                   "person": None, "step": "sharepoint"},
    "pages": {"label": "SharePoint pages",
              "indexed": "full text of the rendered site pages",
              "folder": "the site", "person": None,
              "step": "sharepoint_pages"},
    "planner": {"label": "Planner tasks",
                "indexed": "task title, description, checklist, comments and "
                           "attachment NAMES; the attachments themselves via "
                           "list_files and read_source_file",
                "folder": "the board", "person": "the assignees",
                "step": "planner"},
    "todo": {"label": "To Do tasks",
             "indexed": "task title, notes, steps, linked resources and "
                        "attachment NAMES; the attachments themselves via "
                        "read_source_file",
             "folder": "the list", "person": None, "step": "todo"},
    "onenote": {"label": "OneNote pages",
                "indexed": "full text of every page",
                "folder": "the notebook, optionally deeper: "
                          "Projekte/2026/Q3 (notebook/section group/section)",
                "person": None, "step": "onenote"},
}
# Channels are folded into one entry: a team easily has twenty of them, and
# "which channel" is rarely the question – "channels rather than chats" often
# is. The filter handles that for free, since a path always means everything
# below it too.
# The folder DROPDOWN unit per source: chat kinds, Planner boards and pages
# sites are the unit, not their sub-paths; a SharePoint library is
# "site/library" (two segments). OneDrive and the mailbox keep their full
# folder tree – there the folders themselves are the point.
_OBERSTE_EINHEIT = (
    "CASE "
    "WHEN src IN ('teams', 'planner', 'pages', 'onenote') AND instr(ctx, '/') > 0 "
    "THEN substr(ctx, 1, instr(ctx, '/') - 1) "
    "WHEN src = 'datei' AND root = 'teams' AND instr(ctx, '/') > 0 "
    "THEN substr(ctx, 1, instr(ctx, '/') - 1) "
    "WHEN src = 'datei' AND root = 'sharepoint' "
    "AND instr(substr(ctx, instr(ctx, '/') + 1), '/') > 0 "
    "THEN substr(ctx, 1, instr(ctx, '/') + "
    "instr(substr(ctx, instr(ctx, '/') + 1), '/') - 1) "
    "ELSE ctx END")


def _like_fest(text):
    """Escape SQL-LIKE specials – one helper, because the sequence is
    correctness-sensitive: a folder named "a%b" must not act as a wildcard."""
    return (str(text).replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_"))


def _wie(text):
    """A search term as a LIKE pattern: `*` is the wildcard, nothing else.

    The person search has always been a substring search – there was just no
    way to steer it. Typing `*` searched for the literal star and found
    nothing. Conversely, `%` and `_` acted as unintended wildcards, because
    that is what they are to SQL: "a_b" also matched "axb". Both are
    straightened out here.
    """
    roh = (text or "").strip().lower()
    return "%" + _like_fest(roh).replace("*", "%") + "%"


def _fallbuch():
    """The profile's case book (faelle.py), or None when the server was
    started without a home – then the case tools say so."""
    pfad = STATE.get("faelle_db")
    if not pfad:
        return None
    return faelle.Fallbuch(pfad)


def _fall_finden(buch, case):
    """A case by id or by name (exact, then case-insensitive). Returns
    (case dict, error text)."""
    text = str(case or "").strip()
    if not text:
        return None, "Name a case – list_cases shows them."
    alle = buch.faelle()
    if text.isdigit():
        treffer = [f for f in alle if f["id"] == int(text)]
    else:
        treffer = [f for f in alle if f["name"] == text] or \
                  [f for f in alle if f["name"].lower() == text.lower()]
    if not treffer:
        return None, f"No case named {text!r} – list_cases shows what exists."
    return buch.fall(treffer[0]["id"]), None


def _keys_tabelle(con, keys):
    """The case's keys as a temp table the WHERE fragment joins against –
    thousands of keys, one query, no variable limit."""
    con.execute("CREATE TEMP TABLE IF NOT EXISTS fallkeys(key TEXT PRIMARY KEY)")
    con.execute("DELETE FROM fallkeys")
    con.executemany("INSERT OR IGNORE INTO fallkeys(key) VALUES(?)", ((k,) for k in keys))


def _ordner_finden(fall, folder):
    """A folder of the case by id or by name (exact, then case-insensitive).
    Returns (folder dict or None, error text)."""
    text = str(folder or "").strip()
    if not text:
        return None, None
    alle = fall.get("ordner_liste") or []
    treffer = ([o for o in alle if str(o["id"]) == text] if text.isdigit() else []) or \
              [o for o in alle if o["name"] == text] or \
              [o for o in alle if o["name"].lower() == text.lower()]
    if not treffer:
        namen = ", ".join(o["name"] for o in alle) or "none"
        return None, f"The case {fall['name']!r} has no folder {text!r} – its folders: {namen}."
    return treffer[0], None


def _im_fall(con, case, case_folder=""):
    """Resolve a `case` (and `case_folder`) argument for a query tool:
    fills the temp table and returns (True, None), (False, None) without a
    case, or (False, error)."""
    if not str(case or "").strip():
        return False, None
    buch = _fallbuch()
    if buch is None:
        return False, "This server knows no case book (started without a profile)."
    fall, fehler = _fall_finden(buch, case)
    if fehler:
        return False, fehler
    ordner, fehler = _ordner_finden(fall, case_folder)
    if fehler:
        return False, fehler
    if not _hat_spalte(con, "key"):
        return False, ("This index predates item keys. Rebuild it (Build archive → "
                       "Update archive now) to search inside a case.")
    _keys_tabelle(con, buch.keys(fall["id"], ordner["id"] if ordner else None))
    return True, None


def _where(person, dfrom, dto, src, only_gone=False, folder="", filetype="",
           im_fall=False, party="all", mail=None, with_attachments=False):
    conds, params = [], []
    if with_attachments:
        # Only what carries an attachment – `att` holds the names of a
        # mail's real ones (inline images never), and of the files a
        # card or a page refers to. The page offers it for mail.
        conds.append("(att IS NOT NULL AND att != '')")
    # The mail lines (13.0): who stood in From, To, Cc, Bcc. A line only
    # mail has means mail: an appointment or a file has no such line, and
    # a list that let them through unnarrowed would answer "everything
    # from her" with things that are not from her.
    zeilen, mail_werte = [], []
    for rolle, wert in (mail or {}).items():
        if not str(wert or "").strip():
            continue
        # From is the sender's name and address together: one types either.
        # The name is the one column the indexer leaves as it found it, so
        # it is folded here – "Ülker" must find "Ülker Demir".
        feld = ("(py_lower(COALESCE(who, '')) || ' ' || COALESCE(who_mail, ''))"
                if rolle == "from"
                else f"COALESCE({store_layout.MAIL_SPALTEN[rolle]}, '')")
        zeilen.append(f"{feld} LIKE ? ESCAPE '\\'")
        mail_werte.append(_wie(str(wert).strip()))
    if zeilen:
        conds.append("(src = 'outlook' AND (" + " AND ".join(zeilen) + "))")
        params.extend(mail_werte)
    if party in ("internal", "external"):
        # Whose mail is it: all parties inside the user's own domains, or
        # one of them outside. Items without parties are on neither side.
        conds.append("extern(domains, ?) = ?")
        params.extend([" ".join(_intern_domains()), 1 if party == "external" else 0])
    if im_fall:
        # Only what the case holds: its keys sit in the temp table
        # (_keys_tabelle) for the length of this connection.
        conds.append("key IN (SELECT key FROM fallkeys)")
    if filetype:
        # ext holds a message's extensions separated by spaces ("pdf xlsx").
        # Wrapped in spaces, LIKE matches exactly one of them – "doc" would
        # otherwise also match "docx". Rows without an attachment have NULL
        # and thus drop out on their own.
        conds.append("(' ' || ext || ' ') LIKE ?")
        params.append(f"% {str(filetype).strip().lower().lstrip('.')} %")
    if folder:
        # The folder is stored in the index as ctx. A folder always means
        # its subfolders too: whoever picks a branch does not want to tick
        # hundreds of checkboxes.
        pfad = str(folder).strip().strip("/")
        conds.append("(ctx = ? OR ctx LIKE ?)")
        params.extend([pfad, pfad + "/%"])
    if only_gone:
        # Only what is no longer in the mailbox. That is the question one
        # keeps an archive for in the first place – and there is no other
        # way to answer it.
        conds.append("gone IS NOT NULL")
    if src and src != "all":
        cond, werte = _quelle_cond(src)
        conds.append(cond)
        params.extend(werte)
    if person:
        conds.append("ppl LIKE ? ESCAPE '\\'")
        params.append(_wie(person))
    if dfrom is not None:
        conds.append("ts >= ?")                # also excludes NULL timestamps
        params.append(dfrom)
    if dto is not None:
        conds.append("ts <= ?")
        params.append(dto)
    return (" AND ".join(conds) or _WHERE_ALL), params


def _party_pruefen(con, party):
    """Can the `party` filter be answered? An index without the parties'
    domains cannot, nor one without known internal domains."""
    if party not in ("internal", "external"):
        return None
    if not _hat_spalte(con, "domains"):
        return ("This index predates party tracking. Rebuild it (Export tab → "
                "“Index only”) to use party.")
    if not _intern_domains():
        return ("No internal domains known – set them in Munimentum under "
                "Settings › App, or sign in so the account's domain counts.")
    return None


def _mail_pruefen(con, mail):
    """Can the lines that were asked about be asked about? Only the columns
    a filter actually reads have to be there: `from` sits in `who_mail`,
    which every index since 11.1 carries, while to, cc and bcc came with
    13.0. Refusing `from` on an 11.1 index would refuse a question that
    index can answer."""
    gefragt = [rolle for rolle, wert in (mail or {}).items()
               if str(wert or "").strip()]
    if not gefragt:
        return None
    fehlt = sorted({store_layout.MAIL_SPALTEN[r] for r in gefragt
                    if not _hat_spalte(con, store_layout.MAIL_SPALTEN[r])})
    if fehlt:
        return ("This index predates the mail lines. Rebuild it (Build archive "
                "→ “Index only”) to filter by "
                + ", ".join(r for r in gefragt
                            if store_layout.MAIL_SPALTEN[r] in fehlt) + ".")
    return None


def adressen(role="", q="", limit=12):
    """The mail addresses of one line, most used first – what the Mail
    filter offers while one types.

    Counted at index time (rag_index builds the table), so a keystroke
    costs one small query instead of a walk through every message."""
    rolle = str(role or "").strip().lower()
    if rolle not in store_layout.MAIL_SPALTEN:
        return {"error": f'Unknown line: "{role}" (from, to, cc, bcc).',
                "count": 0, "addresses": []}
    con = _db()
    try:
        if not any(r[0] == "adressen" for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")):
            # An index from before the addresses: no list, and the interface
            # does not offer the filter either.
            return {"count": 0, "role": rolle, "addresses": []}
        wo, params = "role = ?", [rolle]
        if str(q or "").strip():
            wo += " AND addr LIKE ? ESCAPE '\\'"
            params.append(_wie(str(q).strip()))
        rows = con.execute(f"SELECT addr, messages FROM adressen WHERE {wo} "
                           "ORDER BY messages DESC, addr LIMIT ?",
                           [*params, max(1, min(int(limit or 12), 200))]).fetchall()
        return {"count": len(rows), "role": rolle,
                "addresses": [{"address": a, "messages": n} for a, n in rows]}
    finally:
        con.close()


def _to_ts(s, end):
    """"YYYY-MM-DD" as a timestamp; None when nothing was given.

    A date that was given but cannot be read ("2021-06-31" – it does not
    exist) is an error, not a missing value. Dropping it silently would mean
    searching without that bound and presenting the result as the answer to
    the question that was asked.
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f'Kein gültiges Datum: "{s}" (erwartet: YYYY-MM-DD)') from None
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.timestamp()


def _seit_tagen(tage, heute=None):
    """"The last N days" as a timestamp – N=7 means today and the six days
    before it, each starting at midnight.

    The reason this parameter exists: "the last 7 days" is the most common
    question put to the archive, and working out a date for it is a chance
    to get it wrong – above all at the start of a month.

    Calendar days, not a rolling 7×24-hour window: otherwise the same
    question would show a mail from 8 a.m. seven days ago or not, depending
    on the time of day it was asked. The same answer throughout the day is
    worth more than accuracy to the hour.
    """
    try:
        tage = int(tage)
    except (TypeError, ValueError):
        return None
    if tage <= 0:
        return None
    start = (heute or date.today()) - timedelta(days=tage - 1)
    return datetime(start.year, start.month, start.day).timestamp()


def _zeitraum(date_from, date_to, tage):
    """(from, to) from explicit dates and/or the `days` shorthand.

    Two decisions are baked in here:

    An explicit date_from beats the shorthand – and switches it off entirely.
    Whoever writes down a date meant something by it; erroring when both are
    given would cost the caller a round trip over something that can be
    decided unambiguously.

    `days` sets BOTH bounds, not just the lower one. "The last seven days"
    is a window, not a starting point – and without an upper bound the
    question would also pull the coming months' appointments out of the
    calendar, since those lie past the start date as well.
    """
    von, bis = _to_ts(date_from, False), _to_ts(date_to, True)
    if von is not None or not tage:
        return von, bis
    seit = _seit_tagen(tage)
    if seit is None:
        return von, bis
    heute = date.today()
    return seit, (bis if bis is not None else
                  datetime(heute.year, heute.month, heute.day,
                           23, 59, 59).timestamp())


# --------------------------------------------------------------------------
# Lexical backend: FTS5 / BM25
# --------------------------------------------------------------------------
_PHRASE = re.compile(r'"([^"]*)"')


def _fts_match(query):
    """Free text into an FTS5 query: words outside quotes match any of
    them (OR); a quoted "phrase" must occur as such – adjacent, in order –
    every phrase is required, and beside a phrase at least one of the
    loose words must occur too. Nothing of FTS5's own syntax gets
    through: every word is quoted, every phrase is one quoted run of
    words, and a stray quote is just dropped."""
    query = query or ""
    phrasen = []
    for roh in _PHRASE.findall(query):
        toks = _WORD.findall(roh.lower())
        if toks:
            phrasen.append('"' + " ".join(toks) + '"')
    woerter = [f'"{t}"' for t in _WORD.findall(_PHRASE.sub(" ", query).lower())]
    teile = list(phrasen)
    if woerter:
        lose = " OR ".join(woerter)
        teile.append(f"({lose})" if phrasen else lose)
    return " AND ".join(teile)


def _lexical_rank(con, query, where, params, limit):
    match = _fts_match(query)
    if not match:
        return []
    # The full-text table carries `att` as chunks does: matched in a
    # subquery, so every column the filter names resolves to chunks alone.
    sql = (f"SELECT c.id, f.r FROM (SELECT rowid AS fid, bm25(chunks_fts) AS r "
           f"FROM chunks_fts WHERE chunks_fts MATCH ?) f "
           f"JOIN chunks c ON c.id = f.fid WHERE {where} ORDER BY f.r LIMIT ?")
    # bm25(): smaller = better; negate so every backend reports higher = better
    return [(row[0], -row[1]) for row in con.execute(sql, [match, *params, limit])]


# --------------------------------------------------------------------------
# Semantic backend: mmap'd float16 matrix, block-wise cosine scoring
# --------------------------------------------------------------------------
def _embed_query(text):
    np = STATE["np"]
    vec = ollama_client.embed([text], STATE["embed_model"], STATE["ollama"],
                              timeout=120)[0]
    v = np.asarray(vec, dtype="float32")
    nrm = np.linalg.norm(v)
    return v / nrm if nrm else v


def _semantic_rank(con, query, where, params, limit):
    np, V = STATE["np"], STATE["V"]
    B = 32768                                        # ~64 MB float16 per block

    # Unfiltered – the default for search_messages – means "every chunk", so
    # asking SQLite for the id list only to get back 1..n is pure overhead, and
    # gathering those rows copies what is already contiguous. Scoring the matrix
    # in slices instead is ~2.7x faster on a 270k-chunk corpus (105 ms → 39 ms).
    # Safe because chunks.id is a contiguous INTEGER PRIMARY KEY starting at 1
    # (vector row = id - 1) and _open_vectors() has already refused to load a
    # matrix whose row count disagrees with the chunk count.
    if where == _WHERE_ALL and not params:
        n = V.shape[0]
        if n == 0:
            return []
        qvec = _embed_query(query)                   # may raise (Ollama down)
        sims = np.empty(n, dtype=np.float32)
        for s in range(0, n, B):
            sims[s:s + B] = V[s:s + B].astype(np.float32) @ qvec
        take = min(limit, n)
        order = np.argpartition(-sims, take - 1)[:take]
        order = order[np.argsort(-sims[order])]
        return [(int(o) + 1, float(sims[o])) for o in order if sims[o] >= SEM_MIN]

    ids = np.fromiter((r[0] for r in
                       con.execute(f"SELECT id FROM chunks WHERE {where}", params)),
                      dtype=np.int64)
    if ids.size == 0:
        return []
    qvec = _embed_query(query)                       # may raise (Ollama down)
    sims = np.empty(ids.size, dtype=np.float32)
    for s in range(0, ids.size, B):
        block = ids[s:s + B] - 1                     # chunks.id → vector row
        sims[s:s + B] = V[block].astype(np.float32) @ qvec
    take = min(limit, ids.size)
    order = np.argpartition(-sims, take - 1)[:take]
    order = order[np.argsort(-sims[order])]
    return [(int(ids[o]), float(sims[o])) for o in order if sims[o] >= SEM_MIN]


def _rank_wie(con, cid, where, params, limit):
    """The most similar chunks to one that is already in the index.

    The difference from _semantic_rank is the starting vector: here it
    already sits finished in the matrix. Nothing needs embedding, so this
    needs no Ollama – and it works even while semantic search through the
    input field happens to be unavailable.
    """
    np, V = STATE["np"], STATE["V"]
    zeile = cid - 1
    if zeile < 0 or zeile >= V.shape[0]:
        return []
    qvec = V[zeile].astype(np.float32)
    nrm = np.linalg.norm(qvec)
    if nrm:
        qvec = qvec / nrm

    ids = np.fromiter((r[0] for r in
                       con.execute(f"SELECT id FROM chunks WHERE {where}", params)),
                      dtype=np.int64) if where != _WHERE_ALL or params else None
    if ids is None:
        sims = np.empty(V.shape[0], dtype=np.float32)
        for s in range(0, V.shape[0], 32768):
            sims[s:s + 32768] = V[s:s + 32768].astype(np.float32) @ qvec
        ids = np.arange(1, V.shape[0] + 1)
    else:
        if ids.size == 0:
            return []
        sims = np.empty(ids.size, dtype=np.float32)
        for s in range(0, ids.size, 32768):
            sims[s:s + 32768] = V[ids[s:s + 32768] - 1].astype(np.float32) @ qvec
    take = min(limit + 1, ids.size)                  # +1: the hit itself
    order = np.argpartition(-sims, take - 1)[:take]
    order = order[np.argsort(-sims[order])]
    # Returning itself would be the most trivial and useless answer.
    return [(int(ids[o]), float(sims[o])) for o in order
            if int(ids[o]) != cid and sims[o] >= SEM_MIN][:limit]


# --------------------------------------------------------------------------
# Fusion, dedupe, result shaping
# --------------------------------------------------------------------------
def _rrf_merge(*ranked_lists):
    """Reciprocal Rank Fusion: score = Σ 1/(K + rank). Ignores raw scales."""
    scores = {}
    for lst in ranked_lists:
        for rank, (cid, _) in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def _rank(con, query, where, params, k, offset, mode):
    """Ranked (chunk_id, score) list + the backend actually used."""
    pool = min(_POOL_MAX, max(_POOL_MIN, (offset + k) * 5))
    lex = sem = None
    if mode in ("auto", "hybrid", "semantic") and STATE.get("semantic"):
        try:
            sem = _semantic_rank(con, query, where, params, pool)
        except Exception as e:                       # Ollama down, timeout, …
            STATE["last_semantic_error"] = str(e)
            if mode == "semantic":
                raise
    if mode in ("auto", "hybrid", "lexical") or sem is None:
        lex = _lexical_rank(con, query, where, params, pool)
    if sem is not None and lex is not None:
        return _rrf_merge(sem, lex), "hybrid"
    if sem is not None:
        return sem, "semantic"
    return lex or [], "lexical"


def _dedupe_page(con, pairs, k, offset):
    """Collapse chunk hits to messages (best chunk wins), then page."""
    if not pairs:
        return []
    ids = [cid for cid, _ in pairs]
    uid_of = {}
    CHUNK = 500                                      # SQLite variable limit safety
    for s in range(0, len(ids), CHUNK):
        part = ids[s:s + CHUNK]
        q = ",".join("?" * len(part))
        uid_of.update((r[0], r[1]) for r in con.execute(
            f"SELECT id, uid FROM chunks WHERE id IN ({q})", part))
    seen, page = set(), []
    for cid, score in pairs:
        uid = uid_of.get(cid)
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        if len(seen) > offset:
            page.append((cid, score))
            if len(page) >= k:
                break
    return page


def _source_uri(root, rel):
    """MCP resource URI for a source file (rel path percent-encoded)."""
    return f"o365://{root}/{quote(rel, safe='')}"


def _hit(row, score, preview_chars, woerter=()):
    h = {
        "uid": row["uid"],
        # The row in the index. Only with it can "similar to this hit" be
        # asked later without embedding the query again.
        "cid": row["id"] if "id" in row.keys() else None,
        "source": row["src"],
        # Files name their mirror: a hit from a SharePoint library should not
        # wear the same tag as one from the personal drive.
        "source_label": _SOURCE_LABEL.get(
            row["root"] if row["src"] == "datei" else row["src"], row["src"]),
        "root": row["root"],
        "who": row["who"],
        "date": row["date"],
        "title": row["title"],
        "context": row["ctx"],
        "path": row["rel"],
        "uri": _source_uri(row["root"], row["rel"]),
        "score": round(score, 4) if score is not None else None,
        # Id of the conversation the hit belongs to – with it the whole
        # history can be fetched instead of reading just the one message.
        "thread": (row["thread"] if "thread" in row.keys() else None),
        # Names of the attachments – the reason a contract in the archive
        # can now be found rather than just lying there.
        "attachments": corpus.anhang_namen(row["att"]) if "att" in row.keys() else [],
        # Since when the message has been gone from the mailbox. Empty means:
        # it is still there. The file sits in the archive either way.
        "gone": (row["gone"] if "gone" in row.keys() else None),
        # The stable key (schluessel.py) – what a case remembers a hit by.
        # An index from before 11.0 has none.
        "key": (row["key"] if "key" in row.keys() else None),
        # The sender's address (mail, appointments) – an index from before
        # 11.1 has none.
        "who_mail": (row["who_mail"] if "who_mail" in row.keys() else None),
        # Every party's domain – the page marks a hit "external" by it.
        "domains": ((row["domains"] or "").split() if "domains" in row.keys() else []),
    }
    if preview_chars > 0:
        h["preview"] = _ausschnitt(row["text"], woerter, preview_chars)
    return h


def _ausschnitt(text, woerter, laenge):
    """A piece of text around the first match – not stubbornly the beginning.

    The reason is feedback from real use: a search for "Betriebsrat"
    returned mails in which the word first appears after 900 characters. The
    preview showed the first 200 and therefore none of it, and the hit
    looked like a miss although it was spot on.

    Without a match position – e.g. when browsing with no query – it stays
    the beginning; that is then the best information there is.
    """
    text = text or ""
    if not woerter or laenge <= 0:
        return text[:laenge]
    tief = text.lower()
    treffer = [i for i in (tief.find(w) for w in woerter) if i >= 0]
    if not treffer:
        return text[:laenge]
    # Some lead-in so the match does not stick to the left edge.
    start = max(0, min(treffer) - laenge // 4)
    if not start:
        return text[:laenge]
    # The ellipsis counts toward the total: preview_chars is a promise about
    # the length, and a caller expecting 200 characters should get 200.
    return "…" + text[start:start + laenge - 1].lstrip()


def _mit_faellen(hits):
    """Every hit says which cases it already sits in – one lookup for the
    page, nothing when there is no case book or no keys."""
    buch = _fallbuch()
    if buch is None or not hits:
        return hits
    zug = buch.zugehoerigkeit([h.get("key") for h in hits if h.get("key")])
    for h in hits:
        h["cases"] = zug.get(h.get("key"), [])
    return hits


def _rows_for(con, pairs, preview_chars, woerter=()):
    hits = []
    for cid, score in pairs:
        row = con.execute("SELECT * FROM chunks WHERE id = ?", (cid,)).fetchone()
        if row is not None:
            hits.append(_hit(row, score, preview_chars, woerter))
    return hits


def _join_chunks(rows):
    """Reassemble a message's full text from its overlapping chunks (by seq)."""
    text = ""
    for row in rows:
        piece = row["text"] or ""
        if not text:
            text = piece
            continue
        cut = 0
        for L in range(min(len(text), len(piece), 300), 0, -1):  # drop overlap
            if text[-L:] == piece[:L]:
                cut = L
                break
        text += piece[cut:]
    return text


def _message_text(con, uid):
    rows = con.execute("SELECT * FROM chunks WHERE uid = ? ORDER BY seq",
                       (uid,)).fetchall()
    return (rows[0] if rows else None), _join_chunks(rows)


# The export folders by the index's own source names – the `root` of a
# hit, the file tools' `source_root` – and the STATE key each lives
# under, read off settings.QUELLEN, the one table of sources. It also
# says what the step registry calls each of them, which differs for the
# SharePoint pages alone.
QUELLE_ORDNER = {index: f"{index}_dir" for _ordner, index in settings.QUELLEN.values()}
REGISTRY_QUELLE = {quelle: index for quelle, (_ordner, index) in settings.QUELLEN.items()}


def _exportordner():
    """The export folders by the index's source names – the one map, for a
    file's root and for the balance alike."""
    return {quelle: STATE.get(schluessel) for quelle, schluessel in QUELLE_ORDNER.items()}


def _resolve_source(source_root, rel):
    """Sandboxed path resolution for an export file. Returns (Path, error_str)."""
    base = _exportordner().get(source_root)
    if not base:
        namen = [f"'{n}'" for n in QUELLE_ORDNER]
        return None, "source_root must be " + ", ".join(namen[:-1]) + f" or {namen[-1]}."
    base = Path(base).resolve()
    target = (base / rel).resolve()
    if base != target and base not in target.parents:      # prevent path escape
        return None, "Path outside the export directory."
    if not target.is_file():
        return None, f"File not found: {rel}"
    return target, None


def _read_window(target, offset, max_chars):
    """Read a byte window of a file without ever loading the whole file.

    Some exported Teams conversations exceed 100 MB; decoding them entirely
    would pin gigabytes in a long-lived server process.
    """
    total = target.stat().st_size
    start = max(0, offset)
    n = max(1, min(max_chars, 500000))
    with open(target, "rb") as f:
        f.seek(start)
        data = f.read(n)
    # A window may split a multi-byte UTF-8 sequence at either edge;
    # errors="replace" turns the clipped bytes into a replacement char.
    return data.decode("utf-8", errors="replace"), total, start, start + len(data) < total


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
@mcp.tool(annotations=_READONLY)
def search_messages(query: str, person: str = "", date_from: str = "",
                    date_to: str = "", days: int = 0, source: str = "all",
                    k: int = 12, offset: int = 0, mode: str = "auto",
                    preview_chars: int = 200, only_gone: bool = False,
                    folder: str = "", filetype: str = "", case: str = "",
                    case_folder: str = "", party: str = "all",
                    mail_from: str = "", mail_to: str = "", mail_cc: str = "",
                    mail_bcc: str = "", with_attachments: bool = False) -> dict:
    """Search the whole archive – mail, Teams, calendar, contacts, OneDrive and
    SharePoint files, SharePoint pages, Planner tasks, To Do tasks, OneNote
    pages – or any subset of it, or only what one case holds.

    Hybrid ranking (BM25 + semantic embeddings, fused) when available. One
    hit per message/file/task/page; get_document(uid) returns the full
    text, offset pages through more results. What is searchable differs by
    source – list_sources says it per source; in short: mail, Teams, pages,
    OneNote pages, Planner and To Do tasks by their full text; calendar and
    contacts by their text; OneDrive, SharePoint and Teams files by NAME,
    PATH and TYPE only – file contents are not indexed, so a query about
    what a document says will not find it, a query for its name or a
    `filetype` will.

    Args:
        query: Natural-language query or keywords (German or English).
            Words match any of them; a quoted "phrase" must occur as it
            stands – adjacent, in order – and every phrase is required.
            That holds for the lexical search and the lexical half of the
            hybrid one; the semantic half reads the whole text.
        person: Optional. Only items involving this name or e-mail –
            sender/recipients (mail), author (Teams), organizer/attendees
            (calendar), assignees (Planner). Files and pages carry no person;
            the filter finds nothing there.
        date_from: Optional. Inclusive lower bound, "YYYY-MM-DD". A date that
            does not exist is an error, not an omission.
        date_to: Optional. Inclusive upper bound, "YYYY-MM-DD".
        days: Shorthand for a date range: only the last N days, counting
            today (7 = today and the six days before). Bounds the range at
            both ends, so upcoming calendar entries stay out – use date_from/
            date_to for those. Ignored when date_from is given.
        source: One key or several comma-separated: "outlook", "teams",
            "kalender", "kontakte", "onedrive", "sharepoint", "pages",
            "planner", "todo", "onenote" – "datei" means every file mirror
            (OneDrive, SharePoint, Teams files), "all" or empty means
            everything. Example: "onedrive,sharepoint" for files only.
        k: Number of results per page (default 12).
        offset: Results to skip, for pagination (default 0).
        mode: "auto" (hybrid if embeddings available, else lexical),
              "hybrid", "semantic", or "lexical".
        preview_chars: Preview length per hit (default 200; 0 disables previews).
        only_gone: Only items no longer at Microsoft – mails deleted from the
            mailbox, files removed from the drive, tasks removed from the
            board. Everything stays on disk either way.
        folder: Restrict to one unit and everything below it. The unit
            depends on the source – mailbox folder ("E-Mail/Kunden"),
            calendar ("kalender/Privat"), Teams conversation kind ("channels"),
            OneDrive folder ("Dateien/Projekte"), SharePoint site/library
            ("TeamX/Dokumente"), Planner board, To Do list, OneNote
            notebook ("Projekte" or "Projekte/2026/Q3"), pages site.
            list_folders lists what exists, per source.
        filetype: Restrict to messages carrying an attachment of this type,
            or to mirrored files of it – "pdf", "xlsx". One type; use
            list_filetypes to see what exists.
        case: Restrict to the items of one case (its name or id, see
            list_cases): what the user collected there, from every source.
            Every hit carries `key` and `cases` – the cases it already sits
            in, each with the folder inside the case.
        case_folder: With `case`: only the items of one of its folders
            (name or id; get_case lists them).
        party: "all" (default), "internal" – mails and appointments whose
            every party lies inside the user's own mail domains – or
            "external" – at least one party outside them. Items without
            addresses (chat, tasks, files) are on neither side. Every hit
            carries `domains`.
        mail_from: Optional. Only mails whose From line holds this name or
            address; `*` is the wildcard ("*@nordwind.example").
        mail_to: Optional. The same for the To line, mail_cc for Cc.
        mail_cc: See mail_to.
        mail_bcc: Optional. The Bcc line – which only mails the user sent
            themselves carry; a received mail has no blind copy to show.
        with_attachments: Only items that carry an attachment – a mail's
            real ones (inline images such as signature logos do not
            count), the files a card or a page refers to. The hit's
            `attachments` names them.
        Any of the four means mail: an appointment or a file has no such
            line, so with one set nothing but mail answers – whatever
            `source` says. list_addresses says which addresses stand in
            which line.
    """
    con = _db()
    try:
        if only_gone and not _hat_spalte(con, "gone"):
            return {"error": "This index predates deletion tracking. Rebuild it "
                             "(Export tab → “Index only”) to use only_gone.",
                    "count": 0, "results": []}
        fehler = _party_pruefen(con, party)
        if fehler:
            return {"error": fehler, "count": 0, "results": []}
        mail = {"from": mail_from, "to": mail_to, "cc": mail_cc, "bcc": mail_bcc}
        fehler = _mail_pruefen(con, mail)
        if fehler:
            return {"error": fehler, "count": 0, "results": []}
        im_fall, fehler = _im_fall(con, case, case_folder)
        if fehler:
            return {"error": fehler, "count": 0, "results": []}
        von, bis = _zeitraum(date_from, date_to, days)
        where, params = _where(person.strip(), von, bis, source, only_gone, folder,
                               filetype, im_fall, party, mail, with_attachments)
        try:
            pairs, used = _rank(con, query.strip(), where, params,
                                max(1, k), max(0, offset), mode)
        except Exception as e:
            return {"error": f"Semantic ranking failed: {e}. "
                             f"Is Ollama running? Try mode='lexical'."}
        page = _dedupe_page(con, pairs, max(1, k), max(0, offset))
        return {"backend": used, "count": len(page), "offset": max(0, offset),
                "results": _mit_faellen(_rows_for(con, page, max(0, min(preview_chars, 2000)),
                                                  _WORD.findall(query.lower())))}
    finally:
        con.close()


def similar_messages(cid: int, k: int = 12, preview_chars: int = 200):
    """Messages that resemble this one – without a new query.

    Deliberately not an MCP tool, only for the interface: Claude phrases its
    own queries and does not need this detour. The value is for the person
    who has a hit in front of them and wants "more like this".

    And it works without Ollama: the starting vector is already in the matrix.
    """
    if not STATE.get("semantic"):
        return {"error": "This index has no embeddings.", "count": 0, "results": []}
    con = _db()
    try:
        pairs = _rank_wie(con, int(cid), _WHERE_ALL, [], max(1, k) * 3)
        page = _dedupe_page(con, pairs, max(1, k), 0)
        return {"backend": "semantic", "count": len(page), "offset": 0,
                "results": _rows_for(con, page,
                                     max(0, min(preview_chars, 2000)), ())}
    finally:
        con.close()


def _browse_where(con, person, date_from, date_to, days, source, only_gone,
                  folder, filetype, case, case_folder, party, mail_from,
                  mail_to, mail_cc, mail_bcc, with_attachments):
    """The WHERE of a listing without a query – browse_messages and the
    people count share it: the checks that refuse what this index cannot
    answer, then _where. (where, params, error)."""
    if only_gone and not _hat_spalte(con, "gone"):
        return "", [], ("This index predates deletion tracking. Rebuild it "
                        "(Export tab → “Index only”) to use only_gone.")
    fehler = _party_pruefen(con, party)
    if fehler:
        return "", [], fehler
    mail = {"from": mail_from, "to": mail_to, "cc": mail_cc, "bcc": mail_bcc}
    fehler = _mail_pruefen(con, mail)
    if fehler:
        return "", [], fehler
    im_fall, fehler = _im_fall(con, case, case_folder)
    if fehler:
        return "", [], fehler
    von, bis = _zeitraum(date_from, date_to, days)
    where, params = _where(person.strip(), von, bis, source, only_gone, folder,
                           filetype, im_fall, party, mail, with_attachments)
    return where, params, None


def facet_rows(person: str = "", date_from: str = "", date_to: str = "",
               source: str = "all", only_gone: bool = False, folder: str = "",
               filetype: str = "", case: str = "", case_folder: str = "",
               party: str = "all", mail_from: str = "", mail_to: str = "",
               mail_cc: str = "", mail_bcc: str = "",
               with_attachments: bool = False) -> dict:
    """What a people count needs of every item the filters admit – who,
    the sender's address, the date, the source – with no query and no
    cap. `/api/v1/search/people` reads it when there is no search term:
    the whole archive counted honestly, where paging through hits would
    stop at the result cap. Not a tool: Claude has list_people."""
    con = _db()
    try:
        where, params, fehler = _browse_where(
            con, person, date_from, date_to, 0, source, only_gone, folder,
            filetype, case, case_folder, party, mail_from, mail_to, mail_cc,
            mail_bcc, with_attachments)
        if fehler:
            return {"error": fehler, "rows": []}
        # An index from before 11.1 carries no addresses – the count
        # still works, only the external mark stays away (as on the page).
        adresse = "who_mail" if _hat_spalte(con, "who_mail") else "NULL"
        rows = con.execute(
            f"SELECT who, {adresse}, date, src FROM chunks WHERE seq = 0 AND {where}",
            params).fetchall()
        return {"rows": [(r[0], r[1], r[2], r[3]) for r in rows]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def browse_messages(person: str = "", date_from: str = "", date_to: str = "",
                    days: int = 0, source: str = "all", k: int = 30,
                    offset: int = 0, preview_chars: int = 200,
                    only_gone: bool = False, folder: str = "",
                    filetype: str = "", case: str = "", case_folder: str = "",
                    party: str = "all", mail_from: str = "", mail_to: str = "",
                    mail_cc: str = "", mail_bcc: str = "",
                    with_attachments: bool = False) -> dict:
    """List items by filter, newest first, without a search query.

    For "everything from <person> in <month>", "the last week in <folder>",
    "the newest files in <library>" or scanning a source. Same sources and
    filters as search_messages – files by name, path and type only, their
    contents are not indexed. Pass offset to page through more results.

    Args:
        person: Optional name or e-mail – see search_messages for what it
            means per source (nothing for files and pages).
        date_from: Optional inclusive "YYYY-MM-DD" lower bound.
        date_to: Optional inclusive "YYYY-MM-DD" upper bound.
        days: Shorthand for a date range: only the last N days, counting
            today. Bounds the range at both ends; ignored when date_from is
            given.
        source: One key or several comma-separated – "outlook", "teams",
            "kalender", "kontakte", "onedrive", "sharepoint", "pages",
            "planner", "todo", "onenote"; "datei" every file mirror, "all"
            or empty everything.
        k: Max results per page (default 30).
        offset: Results to skip, for pagination (default 0).
        preview_chars: Preview length per hit (default 200; 0 disables previews).
        only_gone: Only items no longer at Microsoft (deleted after they were
            archived). Everything stays on disk either way.
        folder: Restrict to one unit and everything below it – mailbox
            folder, calendar, Teams conversation kind, OneDrive folder,
            SharePoint site/library, Planner board, To Do list, OneNote
            notebook, pages site. Use list_folders to see what exists.
        filetype: Restrict to messages carrying an attachment of this type,
            or to mirrored files of it – "pdf", "xlsx".
        case: Restrict to the items of one case (name or id) – "everything
            in the case, newest first" is the timeline of the matter.
        case_folder: With `case`: only one of its folders (name or id).
        party: "all", "internal" or "external" – see search_messages.
        mail_from: Optional. Only mails with this name or address in the
            From line; mail_to, mail_cc, mail_bcc the same for the other
            lines. Any of them means mail: nothing else has such a line,
            so nothing else answers. See search_messages.
        mail_to: See mail_from.
        mail_cc: See mail_from.
        mail_bcc: See mail_from – only in mail one sent oneself.
        with_attachments: Only items with an attachment – see
            search_messages.
    """
    con = _db()
    try:
        where, params, fehler = _browse_where(
            con, person, date_from, date_to, days, source, only_gone, folder,
            filetype, case, case_folder, party, mail_from, mail_to, mail_cc,
            mail_bcc, with_attachments)
        if fehler:
            return {"error": fehler, "count": 0, "results": []}
        # Plain "ts DESC" rather than "(ts IS NULL), ts DESC": SQLite sorts NULL
        # below every value, so DESC already puts undated messages last – same
        # order, but ix_chunks_msg_ts can serve it without a temp sort.
        rows = con.execute(
            f"SELECT * FROM chunks WHERE seq = 0 AND {where} "
            f"ORDER BY ts DESC LIMIT ? OFFSET ?",
            [*params, max(1, k), max(0, offset)]).fetchall()
        pc = max(0, min(preview_chars, 2000))
        return {"count": len(rows), "offset": max(0, offset),
                "results": _mit_faellen([_hit(r, None, pc) for r in rows])}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def get_thread(thread: str, limit: int = 50) -> dict:
    """All messages of one conversation, in chronological order – mail and
    Teams only.

    A single hit often says too little: "Yes, let's do it that way" only
    becomes a statement together with the question before it. `thread` is
    the value a hit carries in its "thread" field: for mail the
    conversation (replies and forwards), for Teams the chat or channel. Other
    sources have no threads – a Planner task already carries its comments in
    its own text, a page or file stands alone.

    Args:
        thread: The conversation key from a search or browse hit.
        limit: Max number of messages (default 50, cap 500).
    """
    if not thread:
        return {"thread": "", "count": 0, "messages": []}
    con = _db()
    try:
        if not _hat_spalte(con, "thread"):
            return {"thread": thread, "count": 0, "messages": [],
                    "error": "This index predates conversation grouping. "
                             "Rebuild it (Export tab → “Index only”)."}
        rows = con.execute(
            "SELECT * FROM chunks WHERE thread = ? AND seq = 0 "
            "ORDER BY ts IS NULL, ts LIMIT ?",
            (thread, max(1, min(int(limit), 500)))).fetchall()
        return {"thread": thread, "count": len(rows),
                "messages": [_hit(r, None, 400) for r in rows]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def get_document(uid: str, context_before: int = 0, context_after: int = 0) -> dict:
    """Full text and metadata of one item by its uid – mail, chat message,
    appointment, contact, Planner or To Do task, SharePoint or OneNote
    page, or a mirrored file.

    The uid comes from a search/browse hit. For chat messages,
    context_before/context_after also return the neighbouring messages of
    the conversation. For OneDrive and SharePoint files the text is only
    name and path – contents are not indexed – and a `file` block adds
    size, modification date, whether it is gone at the source, and the path
    read_source_file would take.

    `facts` carries, per kind, what the item is known by – already
    extracted, so nothing has to be parsed out of the text or read from
    the source file: a mail its `from`, `to`, `cc` (name and mail each),
    `date`, `folder` and `attachments` (name, size); a chat message its
    `from`, `date`, `chat` and `folder`; an appointment its `start`,
    `end`, `allday`, `location`, `organiser`, `attendees` and `calendar`;
    a contact its `org`, `role`, `emails`, `phones`, `note`; a file its
    `ext`, `size`, `modified`, `folder`; a page its `folder` and
    `modified`; a Planner task its `plan`, `bucket`, `assigned`, `due`,
    `state` (notstarted, inprogress, done), `checklist` (done, total),
    `attachments`, `comments` (who, when, text); a To Do task its `list`,
    `due`, `state` (notstarted, inprogress, completed), `completed`,
    `steps`, `linked`, `attachments`. An empty value means the item has
    none of it.

    Args:
        uid: The item's uid from a search or browse hit.
        context_before: Chat messages before this one (0–20).
        context_after: Chat messages after this one (0–20).
    """
    con = _db()
    try:
        row, text = _message_text(con, uid)
        if row is None:
            return {"error": f"No message with uid {uid!r}."}
        out = {
            "uid": row["uid"],
            "source": row["src"],
            "source_label": _SOURCE_LABEL.get(row["src"], row["src"]),
            "who": row["who"],
            "date": row["date"],
            "title": row["title"],
            "context": row["ctx"],
            "path": row["rel"],
            "uri": _source_uri(row["root"], row["rel"]),
            "text": text,
        }
        # The facts the search page's detail shows, from the same code –
        # without the text, which stands above already.
        ziel, _fehler = _resolve_source(row["root"], row["rel"])
        fakten = detail.fakten(row, text, ziel, STATE)
        for k in ("uid", "kind", "text"):
            fakten.pop(k, None)
        out["facts"] = fakten
        if row["src"] == "datei":
            # The text is name and path; what else is known about a file
            # sits on disk, and the size says whether reading it is worth it.
            ziel, fehler = _resolve_source(row["root"], row["rel"])
            datei = {"name": Path(row["rel"]).name, "gone": row["gone"],
                     "content_indexed": False, "path": row["rel"]}
            if not fehler and ziel.exists():
                st = ziel.stat()
                datei.update(size_bytes=st.st_size,
                             modified=datetime.fromtimestamp(
                                 st.st_mtime).isoformat(timespec="seconds"),
                             binary=ziel.suffix.lower() not in _TEXTFORMATE)
            out["file"] = datei
        before = max(0, min(context_before, 20))
        after = max(0, min(context_after, 20))
        if before or after:
            idx = row["msg_idx"]
            nb = con.execute(
                "SELECT DISTINCT uid FROM chunks WHERE root = ? AND rel = ? "
                "AND msg_idx BETWEEN ? AND ? AND uid != ? ORDER BY msg_idx",
                (row["root"], row["rel"], idx - before, idx + after, uid)).fetchall()
            ctx_b, ctx_a = [], []
            for (n_uid,) in nb:
                n_row, n_text = _message_text(con, n_uid)
                if n_row is None:
                    continue
                entry = {"uid": n_uid, "who": n_row["who"], "date": n_row["date"],
                         "text": n_text[:800]}
                (ctx_b if n_row["msg_idx"] < idx else ctx_a).append(entry)
            out["context_before"] = ctx_b
            out["context_after"] = ctx_a
        return out
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def list_people(source: str = "all", contains: str = "", limit: int = 100) -> dict:
    """List the people in the archive (senders, chat authors, organizers,
    assignees) with item counts – resolve a name before filtering by it.

    The `person` filter is a substring match over names and addresses;
    this tells you the spelling the archive uses. Mirrored files and
    SharePoint pages record no person, so those sources return nothing here
    and the person filter finds nothing there. For the address book itself
    use lookup_contact.

    Args:
        source: One key or several comma-separated – "outlook", "teams",
            "kalender", "kontakte", "planner"; "all" or empty for every
            source.
        contains: Optional. Only people whose name or e-mail contains this
            text; use it to resolve a first name to the full entry.
        limit: Max number of people (default 100, most frequent first).
    """
    con = _db()
    try:
        conds = ["who != '' AND who != '(unbekannt)'"]
        params = []
        quellen = _quellen(source)
        if quellen:
            # The people table has no root column – and mirrored files carry
            # no people anyway, so both mirrors fold into their src here.
            srcs = sorted({"datei" if q in ("onedrive", "sharepoint") else q
                           for q in quellen})
            conds.append(f"src IN ({','.join('?' * len(srcs))})")
            params.extend(srcs)
        if contains.strip():
            # py_lower comes with the connection (_db): SQLite's LIKE folds
            # ASCII only, and `who` is the one column the indexer leaves as
            # it found it.
            conds.append("(py_lower(who) LIKE ? ESCAPE '\\' "
                         "OR ppl LIKE ? ESCAPE '\\')")
            pat = _wie(contains)
            params += [pat, pat]
        where = " AND ".join(conds)
        rows = con.execute(
            f"SELECT who, SUM(messages) AS m FROM people WHERE {where} "
            f"GROUP BY who ORDER BY m DESC, who LIMIT ?",
            [*params, max(1, limit)]).fetchall()
        # The sum as well: the interface offers "everyone with this name
        # part" as a row of its own and must state the same quantity as the
        # rows above it – otherwise messages would sit next to people in
        # one list.
        total, nachrichten = con.execute(
            f"SELECT COUNT(DISTINCT who), COALESCE(SUM(messages), 0) "
            f"FROM people WHERE {where}", params).fetchone()
        return {"count": len(rows), "total_distinct": total,
                "total_messages": nachrichten,
                "people": [{"name": r[0], "messages": r[1]} for r in rows]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def read_source_file(source_root: str, path: str, max_chars: int = 100000,
                     offset: int = 0) -> dict:
    """Read a raw exported source file in windows – the .eml, the Teams
    conversation, an .ics or .vcf, a rendered SharePoint or OneNote page, a
    Planner board.html or To Do list.html, an attachment next to them, or
    a mirrored file.

    Text formats come back as text; binary files (PDF, Office documents,
    images – anything not a text format) come back as metadata with
    binary=true and no content: their contents are not indexed, and there
    is nothing readable to hand over. Large files are windowed: the reply
    carries total_bytes and truncated – pass offset for the next window.
    Prefer get_document with context for chat history; it is far cheaper.

    Args:
        source_root: "teams", "outlook", "onedrive", "sharepoint", "pages",
            "planner", "todo" or "onenote" (the export the file belongs to).
        path: Relative path within that export, as returned in a hit's "path"
            or a list_files entry's "rel".
        max_chars: Max bytes to return (default 100000, cap 500000).
        offset: Byte position to start reading from (default 0).
    """
    target, err = _resolve_source(source_root, path)
    if err:
        return {"error": err}
    if target.suffix.lower() not in _TEXTFORMATE:
        # A PDF read as UTF-8 is a window of replacement characters – worse
        # than nothing, because it looks like an answer.
        return {"source_root": source_root, "path": path,
                "suffix": target.suffix, "binary": True,
                "total_bytes": target.stat().st_size, "content": "",
                "note": "Binary file: its contents are not indexed and not "
                        "returned. The archive holds name, path and type."}
    content, total, start, truncated = _read_window(target, offset, max_chars)
    return {"source_root": source_root, "path": path, "suffix": target.suffix,
            "total_bytes": total, "offset": start, "truncated": truncated,
            "content": content}


@mcp.tool(annotations=_READONLY)
def list_folders(contains: str = "", limit: int = 200, source: str = "") -> dict:
    """List the units the `folder` filter can take, with item counts.

    The unit depends on the source, and it is the same the app's search
    offers: mailbox folders and calendars (full path, e.g. E-Mail/Kunden,
    kalender/Privat), the kind of Teams conversation (1on1, group, meeting,
    channels), OneDrive folders (Dateien/…), SharePoint site/library,
    Planner boards, To Do lists, OneNote notebooks, and SharePoint pages
    sites. A folder always means everything below it as well.

    Args:
        contains: Only folders whose path contains this text.
        limit: Max number of folders (default 200, most items first).
        source: One key or several comma-separated – "outlook", "teams",
            "kalender", "kontakte", "onedrive", "sharepoint", "pages",
            "planner", "todo", "onenote"; "datei" every file mirror. Empty
            or "all" lists every source.
    """
    con = _db()
    try:
        wo, params = "", []
        if contains.strip():
            wo = "AND ordner LIKE ?"
            params.append(f"%{contains.strip()}%")
        quellen = _quellen(source)
        if quellen:
            if any(q not in _LISTBAR for q in quellen):
                return {"count": 0, "folders": []}
            cond, werte = _quelle_cond(source)
            wo += f" AND {cond}"
            params.extend(werte)
        rows = con.execute(
            f"SELECT ordner, COUNT(DISTINCT uid) FROM "
            f"(SELECT uid, src, root, {_OBERSTE_EINHEIT} AS ordner FROM chunks "
            f" WHERE src IN ('outlook', 'datei', 'pages', 'kalender', 'teams',"
            f" 'kontakte', 'planner', 'todo', 'onenote')"
            f" AND ctx IS NOT NULL AND ctx != '') "
            f"WHERE 1=1 {wo} "
            f"GROUP BY ordner ORDER BY 2 DESC LIMIT ?",
            [*params, max(1, min(int(limit), 2000))]).fetchall()
        return {"count": len(rows),
                "folders": [{"path": r[0], "messages": r[1]} for r in rows]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def list_filetypes(limit: int = 40, source: str = "") -> dict:
    """List the attachment and file types present, with counts.

    The counterpart to the `filetype` filter. A count is the number of mails
    carrying at least one attachment of that type, of Planner or To Do tasks
    with such an attachment, or of mirrored OneDrive/SharePoint/Teams files
    of that type. Types are extensions without the dot ("pdf", "xlsx").

    Args:
        limit: Max number of types (default 40, most frequent first).
        source: One key or several comma-separated – "outlook", "planner",
            "todo", "teams", "onedrive", "sharepoint" ("datei" = every file
            mirror). Empty lists every source.
    """
    con = _db()
    try:
        wo, params = "", []
        quelle = (source or "").strip().lower()
        if quelle and quelle != "all":
            cond, werte = _quelle_cond(quelle)
            wo = f"AND {cond}"
            params.extend(werte)
        # A row carries all of its extensions ("pdf xlsx"); the count per
        # type is therefore built here and not in SQL. There are only a few
        # hundred distinct combinations, so this is cheaper than it looks.
        zahl = {}
        for ext, n in con.execute(
                f"SELECT ext, COUNT(DISTINCT uid) FROM chunks "
                f"WHERE ext IS NOT NULL AND ext != '' {wo} GROUP BY ext", params):
            for e in ext.split(" "):
                if e:
                    zahl[e] = zahl.get(e, 0) + n
        oben = sorted(zahl.items(), key=lambda x: (-x[1], x[0]))
        grenze = max(1, min(int(limit), 500))
        return {"count": min(len(oben), grenze), "total_distinct": len(oben),
                "filetypes": [{"type": e, "messages": n} for e, n in oben[:grenze]]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def list_addresses(role: str = "from", limit: int = 20, contains: str = "") -> dict:
    """List the mail addresses of one line, most used first, with counts.

    The counterpart to the `mail_from`, `mail_to`, `mail_cc` and `mail_bcc`
    filters: who actually wrote, who was written to, who was copied in.
    Mail only – no other source has such lines.

    Args:
        role: Which line – "from", "to", "cc" or "bcc" (default "from").
        limit: Max number of addresses (default 20, cap 200).
        contains: Optional. Only addresses holding this text; `*` is the
            wildcard, so "*@nordwind.example" lists one domain's.
    """
    return adressen(role, contains, limit)


def _archiv_stand():
    """How far the archive reaches and how fresh it is.

    Coverage and gaps come from the analytics block that every index run
    materialises into corpus.db; the last successful run per source from
    runs.db, opened read-only. Both are optional: an index from before the
    block existed, or a server started without the app's home folder,
    simply reports None and {} – never an error, this is context, not data.
    """
    block = analytics_db.lies(Path(STATE["db"]).parent) or {}
    komm = block.get("komm") or {}

    def tag(ts):
        return date.fromtimestamp(ts).isoformat() if ts else None
    laeufe = {}
    pfad = STATE.get("runs_db")
    if pfad and Path(pfad).exists():
        try:
            con = sqlite3.connect(f"file:{pfad}?mode=ro", uri=True)
            try:
                for key, ts in con.execute(
                        "SELECT key, MAX(started_at) FROM steps "
                        "WHERE ok = 1 GROUP BY key"):
                    laeufe[key] = datetime.fromtimestamp(ts).isoformat(
                        timespec="seconds")
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return {
        "index_built_at": block.get("built_at"),
        "coverage": {"from": tag(komm.get("von")), "to": tag(komm.get("bis"))},
        "gaps": [{"from": g["von"], "to": g["bis"], "months": g["monate"]}
                 for g in (block.get("luecken") or [])],
        "last_successful_runs": laeufe,
    }


@mcp.tool(annotations=_READONLY)
def list_events(date_from: str = "", date_to: str = "", days: int = 0,
                calendar: str = "", include_recovered: bool = True,
                k: int = 100, offset: int = 0) -> dict:
    """Appointments as the app's calendar view shows them – structured, and
    including the ones recovered from invitation and cancellation mails.

    The search tools know appointments only as text; this returns start,
    end, all-day flag, status, calendar, location, organizer and attendees.
    Recovered appointments are those no longer in any exported calendar:
    status "deleted" (a cancellation was found) or "gone" (merely invited or
    accepted). Upcoming appointments need date_from/date_to – `days` looks
    back, not ahead.

    Args:
        date_from: Inclusive "YYYY-MM-DD" lower bound (an appointment counts
            when any part of it lies in the window).
        date_to: Inclusive "YYYY-MM-DD" upper bound.
        days: The last N days, counting today. Ignored when date_from is given.
        calendar: Only calendars whose name contains this text.
        include_recovered: Also the appointments recovered from mails
            (default true).
        k: Max appointments (default 100).
        offset: Appointments to skip, for pagination.
    """
    datei = Path(STATE["db"]).parent / "calendar.json"
    if not datei.exists():
        return {"error": "No calendar data yet – it is built by the calendar "
                         "step of an export run.", "events": [], "count": 0}
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"error": f"Calendar data unreadable: {e}", "events": [],
                "count": 0}
    von, bis = _zeitraum(date_from, date_to, days)
    aus = []
    for r in daten.get("recs") or []:
        if r.get("src") != "kalender":
            continue
        st = r.get("st") or ""
        if st in ("deleted", "gone") and not include_recovered:
            continue
        if calendar and calendar.lower() not in str(r.get("cal") or "").lower():
            continue
        ts, te = r.get("ts"), r.get("te") or r.get("ts")
        if ts is None:
            continue
        if von is not None and te < von:
            continue
        if bis is not None and ts > bis:
            continue

        def zeit(x):
            return datetime.fromtimestamp(x).isoformat(timespec="minutes") if x else None
        aus.append({"title": r.get("title"), "start": zeit(ts),
                    "end": zeit(r.get("te")), "all_day": bool(r.get("ad")),
                    "status": st, "recovered": st in ("deleted", "gone"),
                    "calendar": r.get("cal"), "location": r.get("loc") or "",
                    "organizer": r.get("who"), "attendees": r.get("att") or [],
                    "description": r.get("x") or "", "uid": r.get("uid"),
                    "path": r.get("p")})
    aus.sort(key=lambda e: e["start"] or "")
    k = max(1, min(int(k), 500))
    offset = max(0, int(offset))
    return {"count": len(aus), "offset": offset,
            "reconstruction_ran": bool(daten.get("reconstruct")),
            "events": aus[offset:offset + k]}


@mcp.tool(annotations=_READONLY)
def lookup_contact(query: str, limit: int = 20) -> dict:
    """Look a person up in the exported address book – structured: name,
    organisation, e-mail addresses, phone numbers, contact folder.

    list_people knows who wrote messages; this knows the contact cards.
    The query is a substring match over name, organisation and addresses.

    Args:
        query: Part of a name, organisation or e-mail address.
        limit: Max contacts (default 20).
    """
    con = _db()
    try:
        pat = _wie(query)
        rows = con.execute(
            "SELECT uid, title, who, ctx, rel, text, ppl FROM chunks "
            "WHERE src = 'kontakte' AND seq = 0 AND (py_lower(title) LIKE ? "
            "ESCAPE '\\' OR ppl LIKE ? ESCAPE '\\' OR py_lower(text) LIKE ? "
            "ESCAPE '\\') ORDER BY title LIMIT ?",
            (pat, pat, pat, max(1, min(int(limit), 200)))).fetchall()
    finally:
        con.close()
    kontakte = []
    for uid, name, org, ctx, rel, text, _ppl in rows:
        text = text or ""
        mails = sorted(set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)))
        telefone = [m.strip() for m in re.findall(
            r"(?<![\w@.])\+?\d[\d /().-]{5,}\d", text)]
        kontakte.append({"uid": uid, "name": name, "organisation": org or "",
                         "emails": mails, "phones": telefone,
                         "folder": ctx, "path": rel, "text": text})
    return {"count": len(kontakte), "contacts": kontakte}


@mcp.tool(annotations=_READONLY)
def list_sources() -> dict:
    """Which sources this archive holds, and how to search each – start here.

    One entry per source present in the index: its key (the value for every
    `source` filter), what is indexed there (full text, or for OneDrive and
    SharePoint files NAME, PATH and TYPE only), what `folder` means for it,
    whether `person` applies, how many items it holds and when it last
    exported successfully. `not_in_archive` lists the sources that were never
    exported – asking about them is pointless, and worth telling the user.
    """
    con = _db()
    try:
        zaehler = {}
        for src, root, n in con.execute(
                "SELECT src, root, COUNT(DISTINCT uid) FROM chunks "
                "GROUP BY src, root"):
            key = root if src == "datei" else src
            zaehler[key] = zaehler.get(key, 0) + n
    finally:
        con.close()
    laeufe = _archiv_stand()["last_successful_runs"]
    quellen, fehlt = [], []
    for key, info in _QUELLEN_INFO.items():
        if not zaehler.get(key):
            fehlt.append(key)
            continue
        quellen.append({
            "key": key, "label": info["label"], "items": zaehler[key],
            "indexed": info["indexed"],
            "folder_filter": info["folder"],
            "person_filter": info["person"] or "not applicable – no person "
                                               "is recorded for this source",
            "last_successful_run": laeufe.get(info["step"]),
        })
    return {"sources": quellen, "not_in_archive": fehlt,
            "source_filter": "one key, or several comma-separated: "
                             "\"onedrive,sharepoint\"; \"datei\" means both "
                             "file mirrors, \"all\" or empty means every source"}


@mcp.tool(annotations=_READONLY)
def archive_analytics() -> dict:
    """The archive about itself, as the app's Analytics tab shows it.

    The block every index run materialises: messages per source (quellen),
    the communication summary (komm: messages, conversations, people, first
    and last timestamp), the mirrored files (dateien), the Planner boards
    (planner), To Do lists (todo) and OneNote pages (onenote), the monthly
    timeline (verlauf) and its gaps (luecken),
    attachment and file types (anhang_typen, datei_typen), the largest files
    (grosse_dateien), disk usage per source (groesse) and the people the
    user exchanges the most with (top_personen). Keys are the app's own
    (German) names; timestamps are epoch seconds, months "YYYY-MM". Returns
    {"error": …} when the index carries no block yet.
    """
    block = analytics_db.lies(Path(STATE["db"]).parent)
    if not block:
        return {"error": "No analytics block in this index yet – it is "
                         "written at the end of an index run."}
    return block


@mcp.tool(annotations=_READONLY)
def corpus_stats() -> dict:
    """Corpus size, per-source counts, the active ranking backend – and the
    archive's edges: coverage, gaps, last successful run per source."""
    con = _db()
    try:
        by_src = {r[0]: {"chunks": r[1], "messages": r[2]} for r in con.execute(
            "SELECT src, COUNT(*), COUNT(DISTINCT uid) FROM chunks GROUP BY src")}
        n = sum(v["chunks"] for v in by_src.values())
        dateien = {r[0]: r[1] for r in con.execute(
            "SELECT root, COUNT(DISTINCT uid) FROM chunks "
            "WHERE src = 'datei' GROUP BY root")}
        return {
            "chunks": n,
            "by_source": by_src,
            "files_by_mirror": dateien,
            "default_backend": "hybrid" if STATE.get("semantic") else "lexical",
            "semantic_available": bool(STATE.get("semantic")),
            "embed_model": STATE.get("embed_model") if STATE.get("semantic") else None,
            "vector_dtype": STATE.get("vector_dtype"),
            "last_semantic_error": STATE.get("last_semantic_error"),
            **{schluessel: STATE.get(schluessel) for schluessel in QUELLE_ORDNER.values()},
            **_archiv_stand(),
        }
    finally:
        con.close()


def _laeufe():
    """The run history, read-only: it belongs to the app, which writes it
    (run_history.py) – one reader for both, this server only renders. A
    server started without the app's home folder, or an archive that
    never ran, simply has none."""
    pfad = STATE.get("runs_db")
    if not pfad or not Path(pfad).exists():
        return None
    return run_history.RunHistory(pfad, readonly=True)


def _zeit(ts):
    try:
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _satz(text):
    """A log line as a sentence. The app stores what it can translate as a
    text key with placeholders; this renders it in English, which is the
    language of everything this server says."""
    if isinstance(text, dict) and text.get("k"):
        return i18n.satz("en", text["k"], export_util.resource_dir(), text.get("v")) or text["k"]
    return text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)


@mcp.tool(annotations=_READONLY)
def list_runs(limit: int = 20) -> dict:
    """What the archive's runs did, newest first – worth asking before
    concluding from the archive that something does not exist.

    An archive is only as complete as its last run: a source whose run
    failed, was cancelled or never happened is missing from every search,
    and nothing in a hit says so. One entry per run with `started`,
    `finished`, `result` – `done`, `error`, `aborted` (cancelled),
    `token_expired`, or `running` while it is still on – what it was
    (`job`: Export, Fetch now, …), where it came from (`origin`: manual,
    schedule) and the app version it ran under, plus one row per step
    with its `label` and what it brought in: `new`, `unchanged`,
    `excluded`, `errors`, `skipped`. `ok: 0` on a step is the one to
    look at – `run_log` then says why.

    Returns `{"runs": [], "note": …}` when this archive has no history –
    a server started without the app's home folder, or an archive that
    never ran.
    """
    hist = _laeufe()
    if hist is None:
        return {"runs": [], "count": 0, "has_more": False,
                "note": "This archive has no run history yet."}
    wieviele = max(1, min(int(limit or 20), 100))
    # Asked for one more than the cap: whether it came says exactly
    # whether something was cut off. Labels are stored as text keys;
    # Claude reads the English sentence, like every log line.
    laeufe = [
        {"id": lauf["id"], "started": _zeit(lauf["started_at"]),
         "finished": _zeit(lauf["finished_at"]), "result": lauf["result"] or "running",
         "job": _satz({"k": lauf["job_type"], "v": {}}), "origin": lauf["origin"],
         "app_version": lauf["app_version"],
         "steps": [{"step": s["key"], "label": _satz({"k": s["label"], "v": {}}),
                    "seconds": s["duration_s"], "new": s["new"], "unchanged": s["unchanged"],
                    "excluded": s["excluded"], "errors": s["errors"],
                    "skipped": bool(s["skipped"]), "ok": s["ok"]}
                   for s in lauf["steps"]]}
        for lauf in hist.list_runs(wieviele + 1)]
    return {"runs": laeufe[:wieviele], "count": min(len(laeufe), wieviele),
            "has_more": len(laeufe) > wieviele}


@mcp.tool(annotations=_READONLY)
def run_log(run: int, limit: int = 200) -> dict:
    """The log one run wrote, oldest first – why a step failed, in words.

    `run` is the `id` from `list_runs`. Lines carry `level` (`info`,
    `warn`, `err`) and an English sentence. Old logs are pruned on the
    schedule the app is set to, so a run from last month may have its
    counts but no lines left.
    """
    hist = _laeufe()
    if hist is None:
        return {"lines": [], "count": 0, "has_more": False,
                "note": "This archive has no run history yet."}
    try:
        nummer = int(run)
    except (TypeError, ValueError):
        return {"lines": [], "count": 0, "has_more": False,
                "error": f"run must be the id from list_runs, not {run!r}."}
    wieviele = max(1, min(int(limit or 200), 2000))
    zeilen = [{"at": _zeit(z["ts"]), "level": z["level"], "text": _satz(z["text"])}
              for z in hist.run_log(nummer, wieviele + 1)]
    if not zeilen:
        # No lines: pruned, or never such a run – two different answers.
        return {"lines": [], "count": 0, "has_more": False,
                "note": (f"No stored lines for run {run} – pruned, or it wrote none."
                         if hist.has_run(nummer) else f"No run with id {run}.")}
    return {"lines": zeilen[:wieviele], "count": min(len(zeilen), wieviele),
            "has_more": len(zeilen) > wieviele}


@mcp.tool(annotations=_READONLY)
def source_completeness(source: str = "") -> dict:
    """What a source's last check found against Microsoft: what is here,
    what was never fetched, what the rules leave out, and what is gone at
    Microsoft but kept here.

    The counterpart to `list_runs`: that one says whether a run worked,
    this one whether the result is complete. Without `source` every row
    of the balance answers; the check itself runs in the app (*Insights →
    completeness balance*), so a row that was never checked has no report
    – which is not the same as "nothing missing".

    Keys are the app's own (German): `da` here, `offen` not fetched yet,
    `ausgeschlossen` left out by the rules, `behalten` deleted at Microsoft
    but kept, `wartend` waiting for the next run, `verweigert` refused by
    Microsoft and `weg` gone before a copy came (both recorded by the
    export, never open), `einheit` what is being
    counted (messages, files, …), `geprueft` when the check ran, `stand`
    its verdict, `grund` why it could not be complete, `zeilen` the same
    numbers per folder or library (`pfad`, `da`, `offen`).

    Args:
        source: One row of the balance – outlook_mail, outlook_calendar,
            outlook_contacts, teams, onedrive, sharepoint, sharepoint_pages,
            planner, todo or onenote – or "" for all of them. The mailbox
            is three rows, because mail, calendar and contacts are fetched
            and checked apart.
    """
    # The rows the check writes, in the registry's order: the mailbox is
    # three of them, and each report sits under its row's name – there is
    # no report called "outlook".
    ordner = _exportordner()
    zeilen = {e["quelle"]: ordner.get(REGISTRY_QUELLE[e["ordner"]]) for e in steps.PRUEFUNGEN}
    gewuenscht = (source or "").strip().lower()
    if gewuenscht and gewuenscht not in zeilen:
        return {"error": f"Unknown source {source!r} – one of: " + ", ".join(zeilen)}
    berichte, ohne = {}, []
    for quelle, pfad in zeilen.items():
        if gewuenscht and quelle != gewuenscht:
            continue
        if not pfad or not Path(pfad).exists():
            continue
        # Read-only: the file is the export's, and a step may be writing it.
        db = state_db.StateDb(pfad, readonly=True)
        try:
            bericht = completeness.lesen(db, quelle)
        except Exception:                  # noqa: BLE001 – a check is context
            bericht = None
        finally:
            db.close()
        if bericht:
            berichte[quelle] = bericht
        else:
            ohne.append(quelle)
    return {"reports": berichte, "never_checked": ohne}


@mcp.tool(annotations=_READONLY)
def list_files(root: str = "", path: str = "") -> dict:
    """Browse the mirrored drives, the files next to Teams conversations and
    the Planner attachments one folder level at a time.

    Without arguments: the entry points – "onedrive", one per mirrored
    SharePoint site/library, "teams" once per chat kind that carries shared
    files and once per team with mirrored channel folders, and "planner"
    once per board that carries attachments. With root and a folder path:
    the immediate subfolders with their file counts, and the files sitting
    right there – name, date, tombstone (gone = no longer at Microsoft).
    File contents are not indexed; a text file can be read via
    read_source_file with the matching root, a binary one only named.

    Args:
        root: "" for the entry points, else "onedrive", "sharepoint",
            "teams" or "planner".
        path: Folder inside that root, as returned by this tool ("" for the
            top level; for planner the board name).
    """
    con = _db()
    try:
        if not root:
            rows = con.execute("SELECT root, rel FROM chunks "
                               "WHERE src = 'datei' AND seq = 0").fetchall()
            eigene = sum(1 for r in rows if r[0] == "onedrive")
            bibliotheken, teams = {}, {}
            for wurzel, rel in rows:
                teile = rel.split("/")
                if wurzel == "sharepoint" and len(teile) >= 2:
                    k = "/".join(teile[:2])
                    bibliotheken[k] = bibliotheken.get(k, 0) + 1
                elif wurzel == "teams" and len(teile) >= 2:
                    k = "/".join(teile[:2])
                    teams[k] = teams.get(k, 0) + 1
            wurzeln = []
            if eigene:
                wurzeln.append({"root": "onedrive", "path": "",
                                "label": "OneDrive", "files": eigene})
            for k in sorted(bibliotheken):
                wurzeln.append({"root": "sharepoint", "path": k,
                                "label": f"SharePoint: {k}", "files": bibliotheken[k]})
            for k in sorted(teams):          # chat kinds first, then teams
                wurzeln.append({"root": "teams", "path": k,
                                "label": _teams_wurzel_label(k),
                                "files": teams[k]})
            for board, dateien in _planner_anhaenge().items():
                wurzeln.append({"root": "planner", "path": board,
                                "label": f"Planner: {board}",
                                "files": len(dateien)})
            return {"roots": wurzeln}
        if root == "planner":
            # Attachments are downloaded next to the board, not indexed as
            # chunks – the listing comes from disk, one flat folder per board.
            board = (path or "").strip("/")
            alle = _planner_anhaenge()
            if board not in alle:
                return {"root": root, "path": board, "base": 1, "label": board,
                        "dirs": [], "files": []}
            return {"root": root, "path": board, "base": 1,
                    "label": f"Planner: {board}", "dirs": [],
                    "files": alle[board][:2000]}
        praefix = (path or "").strip("/")
        # The fixed prefix depth and the level label travel with the answer:
        # the client renders breadcrumbs generically instead of knowing that
        # SharePoint paths start with site/library.
        basis = 2 if root in ("sharepoint", "teams") else 0
        teile = praefix.split("/") if praefix else []
        label = ("/".join(teile[:basis]) if basis and len(teile) >= basis
                 else "OneDrive" if root == "onedrive" else root)
        if root == "teams" and len(teile) >= basis:
            label = _teams_wurzel_label("/".join(teile[:basis]))
        # Every root names its source in front – the browser lists the
        # sites next to OneDrive, Teams and Planner, and a site called
        # "Documents" alone would say nothing.
        if root == "sharepoint" and len(teile) >= basis:
            label = f"SharePoint: {label}"
        wo, params = "src = 'datei' AND seq = 0 AND root = ?", [root]
        if praefix:
            wo += " AND rel LIKE ? ESCAPE '\\'"
            params.append(_like_fest(praefix) + "/%")
        rows = con.execute(
            f"SELECT rel, date, gone FROM chunks WHERE {wo}", params).fetchall()
        schnitt = len(praefix) + 1 if praefix else 0
        ordner, dateien = {}, []
        for rel, datum, weg in rows:
            rest = rel[schnitt:]
            if "/" in rest:
                kopf = rest.split("/", 1)[0]
                d = ordner.setdefault(kopf, {"name": kopf,
                                             "path": f"{praefix}/{kopf}".strip("/"),
                                             "files": 0})
                d["files"] += 1
            else:
                dateien.append({"name": rest, "rel": rel, "date": datum,
                                "gone": weg})
        dateien.sort(key=lambda e: e["name"].lower())
        return {"root": root, "path": praefix, "base": basis, "label": label,
                "dirs": sorted(ordner.values(), key=lambda e: e["name"].lower()),
                "files": dateien[:2000]}
    finally:
        con.close()


_TEAMS_ARTEN = {"1on1": "1:1 chats", "group": "group chats",
                "meeting": "meeting chats"}


def _teams_wurzel_label(pfad):
    """The entry point's name: a chat kind's shared files, or a team."""
    art, _, rest = pfad.partition("/")
    if art in _TEAMS_ARTEN:
        return f"Teams: files shared in {_TEAMS_ARTEN[art]}"
    return f"Teams: {rest or art}"


def _planner_anhaenge():
    """board -> its downloaded attachments, straight from the export folder."""
    wurzel = STATE.get("planner_dir")
    if not wurzel or not Path(wurzel).is_dir():
        return {}
    import planner_export
    out = {}
    for board in sorted(p for p in Path(wurzel).iterdir() if p.is_dir()):
        ordner = board / planner_export.ANHANG_DIR
        if not ordner.is_dir():
            continue
        dateien = []
        for p in sorted(ordner.iterdir(), key=lambda p: p.name.lower()):
            if not p.is_file():
                continue
            st = p.stat()
            dateien.append({"name": p.name,
                            "rel": f"{board.name}/{planner_export.ANHANG_DIR}/{p.name}",
                            "date": datetime.fromtimestamp(st.st_mtime)
                            .strftime("%Y-%m-%d %H:%M"),
                            "gone": None, "size": st.st_size})
        if dateien:
            out[board.name] = dateien
    return out


# --------------------------------------------------------------------------
# MCP resources – fetch a source file by its URI (as advertised in each hit)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Cases and saved searches (faelle.py) – what the user collected
# --------------------------------------------------------------------------
_KEIN_FALLBUCH = ("This server knows no case book: it was started without a "
                  "profile, so cases and saved searches are out of reach.")
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                         idempotentHint=False, openWorldHint=False)


def _fall_kurz(f):
    return {"id": f["id"], "name": f["name"], "description": f["beschreibung"],
            "status": "open" if f["status"] == "offen" else "closed",
            "created": f["angelegt"], "changed": f["geaendert"], "closed_at": f["geschlossen"],
            "items": f["eintraege"], "items_per_source": f["je_quelle"],
            "result_lists": f["listen"], "notes": f["notizen"], "saved_searches": f["suchen"],
            "folders": f["ordner"]}


def _eintrag_aussen(e):
    return {"key": e["key"], "source": e["src"],
            "source_label": _SOURCE_LABEL.get(e["root"] if e["src"] == "datei" else e["src"],
                                              e["src"]),
            "root": e["root"], "path": e["rel"],
            "uri": _source_uri(e["root"], e["rel"]) if e.get("root") and e.get("rel") else None,
            "title": e["titel"], "date": e["datum"], "who": e["wer"],
            "added": e["hinzugefuegt"], "from_result_list": e["liste"],
            "folder": e.get("ordner_name"), "origin": _herkunft(e.get("quelle")),
            "remark": e.get("bemerkung") or ""}


def _herkunft(quelle):
    """Who wrote an item or a note: the page, Claude through MCP, or an
    automatic search of the case (case_collect)."""
    return "mcp" if quelle == faelle.MCP else "auto" if quelle == faelle.AUTO else "page"


def _kriterien_aussen(k):
    return {"query": k["q"], "mode": k["mode"], "person": k["person"], "source": k["source"],
            "date_from": k["from"], "date_to": k["to"], "folder": k["folder"],
            "filetype": k["filetype"], "only_gone": k["gone"], "case": k["fall"],
            "party": k.get("party", "all")}


def _suche_aussen(g):
    return {"id": g["id"], "name": g["name"], "criteria": _kriterien_aussen(g["kriterien"]),
            "created": g["angelegt"], "last_run": g["zuletzt"], "hits_then": g["treffer"],
            "case": g["fall_name"], "folder": g.get("ordner_name"),
            # Switched to automatic: a run files its new hits into the case
            # by itself; what the last such run did.
            "auto": bool(g.get("automatisch")), "auto_last_run": g.get("auto_zuletzt"),
            "auto_added": g.get("auto_neu"), "auto_skipped": g.get("auto_uebersprungen")}


def _mit_kriterien(k, kk, offset=0, preview_chars=200):
    """Run a search with stored criteria – a query searches, none browses."""
    modus = {"text": "lexical", "aehnlich": "semantic", "ki": "hybrid"}.get(k["mode"], "auto")
    gemeinsam = dict(person=k["person"], date_from=k["from"], date_to=k["to"],
                     source=k["source"], k=kk, offset=offset, preview_chars=preview_chars,
                     only_gone=k["gone"], folder=k["folder"], filetype=k["filetype"],
                     case=str(k["fall"]) if k["fall"] else "",
                     case_folder=str(k["ordner"]) if k["fall"] and k["ordner"] else "",
                     party=k.get("party", "all"))
    if k["q"]:
        return search_messages(query=k["q"], mode=modus, **gemeinsam)
    return browse_messages(**gemeinsam)


@mcp.tool(annotations=_READONLY)
def list_cases(include_closed: bool = True) -> dict:
    """The user's cases – the matters they collect archive items, result
    lists and saved searches around. Start here before anything about a
    case; a case is named by its `name` or `id` in the other case tools
    and in the `case` filter of search_messages and browse_messages.

    Args:
        include_closed: Also list closed cases (read-only ones). Default true.
    """
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH, "count": 0, "cases": []}
    faelle_ = [_fall_kurz(f) for f in buch.faelle(mit_geschlossenen=include_closed)]
    return {"count": len(faelle_), "cases": faelle_}


@mcp.tool(annotations=_READONLY)
def get_case(case: str) -> dict:
    """One case in full: its description, the casebook (the user's notes,
    newest first), its folders, every item it holds with source, title,
    date, people, the folder it sits in and the uri read_source_file takes,
    the result lists it stores (a search as it stood at one moment) and the
    saved searches attached to it. Items say `from_result_list` when they
    came with a list; `origin` says who wrote an item or a note – "page"
    (the user) or "mcp" (Claude, through add_to_case / add_case_note).

    Args:
        case: The case's name or id (list_cases).
    """
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH}
    fall, fehler = _fall_finden(buch, case)
    if fehler:
        return {"error": fehler}
    out = _fall_kurz(fall)
    ordner = {o["id"]: o["name"] for o in fall["ordner_liste"]}
    out["folders"] = [{"id": o["id"], "name": o["name"], "items": o["anzahl"]}
                      for o in fall["ordner_liste"]]
    out["notes"] = [{"id": n["id"], "when": n["wann"], "text": n["text"],
                     "origin": _herkunft(n.get("quelle"))} for n in fall["notizen_liste"]]
    out["items"] = [_eintrag_aussen({**e, "ordner_name": ordner.get(e.get("ordner"))})
                    for e in fall["eintraege_liste"]]
    out["result_lists"] = [{"id": li["id"], "taken": li["wann"], "hits": li["anzahl"],
                            "criteria": _kriterien_aussen(li["kriterien"]),
                            "folder": ordner.get(li.get("ordner"))}
                           for li in fall["listen_liste"]]
    out["saved_searches"] = [_suche_aussen(g) for g in fall["suchen_liste"]]
    return out


@mcp.tool(annotations=_READONLY)
def case_timeline(case: str, limit: int = 200, preview_chars: int = 160) -> dict:
    """The items of a case in the order they happened – oldest first, each
    with a short excerpt from the index: the chronology of the matter, the
    tool for "what happened" and "summarise this case". Items the index no
    longer holds are listed at the end with what the case remembers.

    Args:
        case: The case's name or id.
        limit: Items at most (default 200).
        preview_chars: Excerpt length per item (0 disables).
    """
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH}
    fall, fehler = _fall_finden(buch, case)
    if fehler:
        return {"error": fehler}
    con = _db()
    try:
        if not _hat_spalte(con, "key"):
            return {"error": "This index predates item keys – rebuild it first."}
        keys = buch.keys(fall["id"])
        _keys_tabelle(con, keys)
        rows = con.execute(
            "SELECT * FROM chunks WHERE seq = 0 AND key IN (SELECT key FROM fallkeys) "
            "ORDER BY (ts IS NULL), ts LIMIT ?", (max(1, limit),)).fetchall()
        pc = max(0, min(preview_chars, 2000))
        im_index = [_hit(r, None, pc) for r in rows]
        gesehen = {h["key"] for h in im_index}
        weg = [_eintrag_aussen(e) for e in fall["eintraege_liste"] if e["key"] not in gesehen]
        return {"case": fall["name"], "count": len(im_index), "items": im_index,
                "not_in_index": weg}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def case_people(case: str, limit: int = 50) -> dict:
    """Who appears in a case – the people behind its items (senders,
    authors, organisers, assignees) with how many items each has, most
    frequent first, and the address the archive knows for them (mail and
    appointments; null for chat authors and assignees). Files and pages
    carry no person.

    Args:
        case: The case's name or id.
        limit: Names at most (default 50).
    """
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH}
    fall, fehler = _fall_finden(buch, case)
    if fehler:
        return {"error": fehler}
    con = _db()
    try:
        if not _hat_spalte(con, "key"):
            return {"error": "This index predates item keys – rebuild it first."}
        _keys_tabelle(con, buch.keys(fall["id"]))
        zaehler, adressen = {}, {}
        felder = "who, who_mail" if _hat_spalte(con, "who_mail") else "who, NULL"
        for who, who_mail in con.execute(f"SELECT {felder} FROM chunks WHERE seq = 0 AND who IS NOT NULL "
                                         "AND who != '' AND key IN (SELECT key FROM fallkeys)"):
            namen = [n.strip() for n in str(who).split(", ")]
            for name in namen:
                if name and name != "(unbekannt)":
                    zaehler[name] = zaehler.get(name, 0) + 1
            # One name, one address: an assignee list names several people
            # for one address, which would pin it on the wrong one.
            if who_mail and len(namen) == 1 and namen[0] not in adressen:
                adressen[namen[0]] = who_mail
        leute = sorted(zaehler.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:max(1, limit)]
        return {"case": fall["name"], "count": len(leute),
                "people": [{"name": n, "items": c, "email": adressen.get(n)} for n, c in leute]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def case_new_hits(case: str, k: int = 50) -> dict:
    """What the case's attached saved searches find today that the case
    does not hold yet – one block per search. The way to keep a case
    current by hand: run this, read the new hits, and (if allowed)
    add_to_case; a search marked `auto` in get_case does that by itself
    with every run (collect_case runs it now). What the user took out
    of the case is not offered again.

    Args:
        case: The case's name or id.
        k: New hits at most per search (default 50).
    """
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH}
    fall, fehler = _fall_finden(buch, case)
    if fehler:
        return {"error": fehler}
    keys = buch.keys(fall["id"]) | buch.entfernte(fall["id"])
    bloecke = []
    for g in fall["suchen_liste"]:
        res = _mit_kriterien(g["kriterien"], min(max(1, k) * 4, 200))
        if res.get("error"):
            bloecke.append({"search": g["name"], "error": res["error"], "new": []})
            continue
        neu = [h for h in res.get("results") or () if h.get("key") and h["key"] not in keys]
        bloecke.append({"search": g["name"], "id": g["id"], "new_count": len(neu[:max(1, k)]),
                        "new": neu[:max(1, k)]})
    return {"case": fall["name"], "searches": bloecke}


@mcp.tool(annotations=_READONLY)
def list_saved_searches() -> dict:
    """The user's saved searches – criteria under a name, with when each
    last ran and how many hits it had then, and the case it is attached
    to, if any. run_saved_search runs one."""
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH, "count": 0, "searches": []}
    suchen = [_suche_aussen(g) for g in buch.gespeicherte()]
    return {"count": len(suchen), "searches": suchen}


@mcp.tool(annotations=_READONLY)
def run_saved_search(search: str, k: int = 12, offset: int = 0,
                     preview_chars: int = 200) -> dict:
    """Run a saved search by its name or id – exactly the criteria the user
    saved, through search_messages (or browse_messages when it has no
    words). Hits look like search hits.

    Args:
        search: Name or id of the saved search (list_saved_searches).
        k: Results per page (default 12).
        offset: Results to skip.
        preview_chars: Preview length per hit.
    """
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH}
    text = str(search or "").strip()
    alle = buch.gespeicherte()
    treffer = ([g for g in alle if str(g["id"]) == text] if text.isdigit() else []) or \
              [g for g in alle if g["name"] == text] or \
              [g for g in alle if g["name"].lower() == text.lower()]
    if not treffer:
        return {"error": f"No saved search named {text!r} – list_saved_searches shows them."}
    g = treffer[0]
    res = _mit_kriterien(g["kriterien"], max(1, k), max(0, offset), preview_chars)
    if not res.get("error") and offset == 0:
        buch.gelaufen(g["id"], res.get("count", 0))
    res["search"] = g["name"]
    return res


def _schreiben_erlaubt():
    if not STATE.get("cases_write"):
        return ("Changing cases through MCP is switched off. The user can allow "
                "it under Settings → Claude (MCP) → “Claude may change cases”.")
    return None


@mcp.tool(annotations=_WRITE)
def add_to_case(case: str, uids: list[str] | None = None,
                keys: list[str] | None = None, folder: str = "",
                remark: str = "") -> dict:
    """Add items to a case – hits by their `uid` (from a search) or by their
    `key` – into one of its folders, or unsorted, with a remark on why they
    belong there. Only when the user allowed changes through MCP; nothing
    is copied, the case merely points at the items, and they carry the
    mark "via MCP" in Munimentum. A closed case refuses; folders are made
    by the user, not here.

    Args:
        case: The case's name or id.
        uids: Hit uids to add.
        keys: Item keys to add (a hit's `key`).
        folder: The folder inside the case (name or id, get_case lists
            them); empty puts the items in unsorted.
        remark: One or two sentences on why these items are in the case –
            set on the items this call adds, shown under each in Munimentum
            and in the export. Items already in the case keep theirs.
    """
    gesperrt = _schreiben_erlaubt()
    if gesperrt:
        return {"error": gesperrt}
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH}
    fall, fehler = _fall_finden(buch, case)
    if fehler:
        return {"error": fehler}
    ordner, fehler = _ordner_finden(fall, folder)
    if fehler:
        return {"error": fehler}
    con = _db()
    try:
        if not _hat_spalte(con, "key"):
            return {"error": "This index predates item keys – rebuild it first."}
        rows = []
        for uid in uids or ():
            r = con.execute("SELECT * FROM chunks WHERE uid = ? AND seq = 0", (uid,)).fetchone()
            if r is not None:
                rows.append(r)
        for key in keys or ():
            r = con.execute("SELECT * FROM chunks WHERE key = ? AND seq = 0 LIMIT 1",
                            (key,)).fetchone()
            if r is not None:
                rows.append(r)
    finally:
        con.close()
    eintraege = [{"key": r["key"], "src": r["src"], "root": r["root"], "rel": r["rel"],
                  "titel": r["title"], "datum": r["date"], "wer": r["who"],
                  "bemerkung": str(remark or "").strip()}
                 for r in rows if r["key"]]
    try:
        neu = buch.hinzufuegen(fall["id"], eintraege, ordner_id=ordner["id"] if ordner else None,
                               quelle=faelle.MCP)
    except faelle.FallGeschlossen:
        return {"error": f"The case {fall['name']!r} is closed – reopen it in Munimentum first."}
    return {"case": fall["name"], "folder": ordner["name"] if ordner else None,
            "added": neu, "already_there": len(eintraege) - neu,
            "not_found": len(list(uids or ())) + len(list(keys or ())) - len(eintraege)}


@mcp.tool(annotations=_WRITE)
def collect_case(case: str, search: str = "") -> dict:
    """Run the case's automatic searches now and file what they find into
    the case – the same thing every indexing run does by itself for a
    search marked `auto` (get_case, `saved_searches`). With `search`, that
    one attached search runs, marked automatic or not: a one-off. Only a
    text search collects; what the user took out of the case stays out;
    the items carry the origin "auto" and the search that found them.
    Only when the user allowed changes through MCP; a closed case refuses.

    Args:
        case: The case's name or id.
        search: Optional – the id or name of one of the case's attached
            searches (get_case lists them). Empty: every automatic one.
    """
    gesperrt = _schreiben_erlaubt()
    if gesperrt:
        return {"error": gesperrt}
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH}
    fall, fehler = _fall_finden(buch, case)
    if fehler:
        return {"error": fehler}
    if fall["status"] != faelle.OFFEN:
        return {"error": f"The case {fall['name']!r} is closed – reopen it in Munimentum first."}
    wunsch = str(search or "").strip()
    if wunsch:
        g = next((g for g in fall["suchen_liste"]
                  if str(g["id"]) == wunsch or g["name"].lower() == wunsch.lower()), None)
        if g is None:
            return {"error": f"No saved search {wunsch!r} is attached to the case {fall['name']!r}."}
        if g["kriterien"].get("mode") != "text":
            return {"error": "Only a text search collects – the other kinds rank by likeness."}
        suchen = [g]
    else:
        suchen = buch.automatische(fall["id"])
        if not suchen:
            return {"case": fall["name"], "searches": [], "added": 0,
                    "note": "No automatic search on this case – switch one on in Munimentum, or name one."}
    import case_collect
    bericht, summe = case_collect.einsammeln(
        buch, suchen, lambda k, n: _mit_kriterien(k, n, preview_chars=0))
    return {"case": fall["name"], "added": summe["new"], "skipped_removed": summe["skipped"],
            "searches": [{k: v for k, v in z.items() if k not in ("case", "case_id")} for z in bericht]}


@mcp.tool(annotations=_WRITE)
def add_case_note(case: str, text: str) -> dict:
    """Add a note to a case's casebook – a short, dated remark, marked
    "via MCP" in Munimentum. Only when the user allowed changes through
    MCP; a closed case refuses.

    Args:
        case: The case's name or id.
        text: The note.
    """
    gesperrt = _schreiben_erlaubt()
    if gesperrt:
        return {"error": gesperrt}
    buch = _fallbuch()
    if buch is None:
        return {"error": _KEIN_FALLBUCH}
    fall, fehler = _fall_finden(buch, case)
    if fehler:
        return {"error": fehler}
    try:
        kennung = buch.notiz(fall["id"], text, quelle=faelle.MCP)
    except faelle.FallGeschlossen:
        return {"error": f"The case {fall['name']!r} is closed – reopen it in Munimentum first."}
    except ValueError:
        return {"error": "The note is empty."}
    return {"case": fall["name"], "note_id": kennung}


@mcp.resource("o365://{root}/{path}")
def source_resource(root: str, path: str) -> str:
    """Return a raw exported source file by URI.

    URI form: o365://{root}/{path}, where {root} is "teams", "outlook",
    "onedrive" or "sharepoint" and {path} is the export-relative file path,
    percent-encoded (slashes as %2F).
    This is the "uri" field returned with every search/browse hit. Files larger
    than 500k characters are truncated – use the read_source_file tool with
    offset to page through the rest.
    """
    target, err = _resolve_source(root, unquote(path))
    if err:
        raise ValueError(err)
    content, total, _, truncated = _read_window(target, 0, 500000)
    if truncated:
        content += (f"\n\n[truncated: {total} bytes total – use the "
                    f"read_source_file tool with offset to read more]")
    return content


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _with_port(host, port):
    """Append the default port unless the value already carries one.

    Written so an IPv6 literal ("[fe80::1]") is not mistaken for host:port –
    only a trailing all-digit segment counts as a port.
    """
    _, sep, tail = host.rpartition(":")
    return host if sep and tail.isdigit() else f"{host}:{port}"


def _transport_security(host, port, allowed):
    """Origin/Host validation (DNS-rebinding protection) for the HTTP transport.

    The SDK switches this on by itself for loopback binds, and there the
    defaults are exactly right. It does *not* for any other address – which is
    the one case where it matters: a server reachable from the network, with no
    authentication, serving the complete mail and chat history. Without Host
    and Origin checks, any web page the user happens to open can POST to this
    endpoint and read the archive out through the browser.

    Guessing the legitimate hostnames is not possible, so they have to be named
    with --allowed-host. Refusing to start beats starting unprotected.
    """
    if host in _LOOPBACK:
        return None                     # SDK default already validates these
    if not allowed:
        raise SystemExit(
            f"Refusing to bind {host} without --allowed-host.\n"
            f"This server has no authentication and serves your whole mail and\n"
            f"chat history. Off the loopback interface, Host/Origin validation\n"
            f"is the only thing standing between it and any web page you open.\n"
            f"  • keep it local:  drop --host (defaults to 127.0.0.1)\n"
            f"  • or name the hostnames clients will use:\n"
            f"      --host {host} --allowed-host myhost.local --allowed-host 192.168.1.5")
    hosts = [_with_port(h, port) for h in allowed]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"{scheme}://{h}" for h in hosts
                         for scheme in ("http", "https")])


def _open_vectors(store, n_chunks):
    """Memory-map the vector file if numpy + the file are present.

    Its name comes from info.json and changes with every index run: the mapping
    below stays open for as long as this process lives, and on Windows a mapped
    file cannot be replaced. Writing a new one each time is what keeps indexing
    possible while a reader is running (see store_layout).
    """
    vp = store_layout.vectors_path(store)
    if not vp:
        return None, None
    try:
        import numpy as np
    except ImportError:
        print("numpy not installed – semantic/hybrid ranking disabled.",
              file=sys.stderr)
        return None, None
    V = np.load(vp, mmap_mode="r")
    if V.shape[0] != n_chunks:
        print(f"Index/DB mismatch ({V.shape[0]} vectors vs {n_chunks} chunks) – "
              f"rebuild with rag_index.py. Lexical ranking only.", file=sys.stderr)
        return None, None
    return np, V


def _argumente(a):
    """Resolve what the command line left open. Returns (args, answer).

    --profile alone is a complete address: the profile's folder holds the
    configuration, and from it come the data folder, the index, the
    embedding model, the Ollama address, the port and whether Ollama is
    used at all. Explicit flags still win. Without a profile the
    configuration is the one MUNIMENTUM_HOME (or the module folder) names,
    as before – and with several profiles and nothing named, `answer` is
    the one sentence the server will give instead of an archive.
    """
    auskunft = None
    if a.profile:
        try:
            heim = settings.profil_ordner(a.profile)
        except ValueError:
            raise SystemExit(f"Not a profile name: {a.profile}") from None
        if not heim.is_dir():
            raise SystemExit(f"Unknown profile: {a.profile}. Existing: "
                             + ", ".join(settings.profil_namen()))
        # The profile's own files: configuration, key, history – for this
        # process and its readers alike.
        os.environ["MUNIMENTUM_HOME"] = str(heim)
        settings.reset()
        daten, store = settings.datenpfade(heim, settings.load(heim / settings.CONFIG_NAME))
        a.data_dir = a.data_dir or str(daten)
        a.store = a.store or str(store)
    elif not a.data_dir and not settings.home_env() and len(settings.profil_namen()) > 1:
        auskunft = _profil_text(settings.profil_namen())
    a.embed_model = a.embed_model or settings.value("embed_model")
    a.ollama = a.ollama or settings.value("ollama")
    a.port = int(a.port or settings.value("mcp_port"))
    if not settings.flag("OLLAMA_ENABLED", "ollama_enabled"):
        a.no_ollama = True
    return a, auskunft


def _nur_auskunft(a, text):
    """Run the one-answer server over the chosen transport."""
    print(text, file=sys.stderr)
    server = _abgeschaltet_server(text)
    if a.transport == "http":
        server.run(transport="streamable-http", host=a.host, port=a.port,
                   streamable_http_path=_HTTP_PATH,
                   transport_security=_transport_security(
                       a.host, a.port, a.allowed_host))
    else:
        server.run(transport="stdio")


def _alter_schnipsel(a):
    """A stdio entry from before 10.0 names the folders the archive had
    in the app folder; since the move they are empty. Recognised by what
    it carries: an app folder in MUNIMENTUM_HOME that holds profiles but
    no configuration of its own."""
    heim = settings.home_env()
    if not heim or not a.data_dir:
        return False
    heim = Path(heim).expanduser()
    return (not (heim / settings.CONFIG_NAME).exists()
            and len(settings.profil_namen(heim)) >= 1
            and (heim / settings.PROFIL_ORDNER).is_dir())


ALT_TEXT = ("This Munimentum entry is from before version 10.0: it names folders "
            "that the archive has left – it lives in a profile folder now. "
            "Nothing is served. Tell the user to copy the stdio snippet again "
            "from Munimentum under Settings -> Claude (MCP) and replace this "
            "entry with it.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Defaults come from app_config.json when it exists; flags win (settings.py).
    # One directory instead of three. The subdirectories have fixed names,
    # as everywhere in the project – Claude starts the server in an unknown
    # working directory, so this one path must come along absolute.
    ap.add_argument("--data-dir", metavar="FOLDER",
                    help="Data folder holding the export folders. The index "
                         "has a path of its own (--store). Without it the "
                         "current directory applies.")
    ap.add_argument("--profile", metavar="NAME",
                    help="Profile – its own archive inside the app folder. "
                         "Required once more than one exists; sets the data "
                         "folder and the index from that profile's settings.")
    ap.add_argument("--store", help=argparse.SUPPRESS)
    ap.add_argument("--teams", help=argparse.SUPPRESS)
    ap.add_argument("--outlook", help=argparse.SUPPRESS)
    ap.add_argument("--onedrive", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--sharepoint", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--pages", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--planner", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--todo", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--onenote", default=None, help=argparse.SUPPRESS)
    # Model, address and port default to the configuration – resolved in
    # _argumente(), after --profile has said WHICH configuration.
    ap.add_argument("--embed-model", default=None)
    ap.add_argument("--ollama", default=None)
    # Switched off means: do not even try. Without this, the server decides
    # anew per request and runs into the same error every time.
    ap.add_argument("--no-ollama", action="store_true",
                    help="Do not embed, even when vectors exist. "
                         "Ranks purely lexically.")
    ap.add_argument("--transport", choices=["http", "stdio"], default="http",
                    help="http: one shared server, register its URL in Claude "
                         "(default). stdio: launched per client via command.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="HTTP bind address. Keep 127.0.0.1 – the server has no "
                         "auth and serves your mail/chat history.")
    ap.add_argument("--port", type=int, default=None)
    # For manual invocation: whoever starts the program themselves does not
    # have the switch in front of them and should not have to puzzle out why
    # nothing works.
    ap.add_argument("--force", action="store_true",
                    help="Serve even when MCP access is switched off in the app.")
    ap.add_argument("--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
                    help="Hostname clients may use in the Host/Origin header. "
                         "Required when --host is not the loopback interface; "
                         "repeat for several. Port defaults to --port.")
    a, auskunft = _argumente(ap.parse_args())
    if auskunft:
        # Several archives, none named: one answer instead of a guess.
        return _nur_auskunft(a, auskunft)
    # --store/--teams/--outlook stay accepted as hidden flags: an old Claude
    # configuration that still names them should not run into nothing.
    basis = Path(a.data_dir).expanduser() if a.data_dir else Path(".")
    a.store = a.store or str(basis / settings.STORE_DIR)
    a.teams = a.teams or str(basis / settings.TEAMS_DIR)
    a.outlook = a.outlook or str(basis / settings.OUTLOOK_DIR)
    a.onedrive = a.onedrive or str(basis / settings.ONEDRIVE_DIR)
    a.sharepoint = a.sharepoint or str(basis / settings.SHAREPOINT_DIR)
    a.pages = a.pages or str(basis / settings.SHAREPOINT_PAGES_DIR)
    a.planner = a.planner or str(basis / settings.PLANNER_DIR)
    a.todo = a.todo or str(basis / settings.TODO_DIR)
    a.onenote = a.onenote or str(basis / settings.ONENOTE_DIR)

    # The hard switch. It sits here and not in the tools because the app
    # calls the same functions in-process for its own search – what is
    # switched off is the SERVER, not reading the index. And it sits before
    # both transports: over stdio the client launches this program itself,
    # without the app running at all. A switch that only stopped the HTTP
    # endpoint would be exactly the promise it does not keep.
    #
    # Exiting would be the obvious and the worse answer: the client would
    # only see a server that fails to start, with the reason in a log file.
    # Instead, a server runs that gives exactly one answer – the language
    # model reads it and tells the human in plain words. Nothing is served
    # in the process: no tool that reads data, and the index is not even
    # opened.
    if not a.force and not settings.flag("MCP_ENABLED", "mcp_enabled"):
        return _nur_auskunft(a, AUS_TEXT)

    dbp = store_layout.db_path(a.store)
    if not dbp.exists() and _alter_schnipsel(a):
        # The same rule as the switch: an entry that would only fail at
        # every start says once, through the model, what to do instead.
        return _nur_auskunft(a, ALT_TEXT)
    if not dbp.exists():
        raise SystemExit(f"No store at '{dbp}'. Build the index in "
                         f"Munimentum first (Export tab, or Settings -> "
                         f"expert mode -> index only).")

    con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()

    np, V = (None, None) if a.no_ollama else _open_vectors(a.store, n_chunks)
    if a.no_ollama:
        print("Ollama abgeschaltet – es wird rein lexikalisch gerankt.",
              file=sys.stderr)
    STATE.update(db=str(dbp), V=V, np=np, semantic=(np is not None),
                 vector_dtype=str(V.dtype) if V is not None else None,
                 teams_dir=a.teams, outlook_dir=a.outlook,
                 onedrive_dir=a.onedrive, sharepoint_dir=a.sharepoint,
                 pages_dir=a.pages, planner_dir=a.planner,
                 todo_dir=a.todo, onenote_dir=a.onenote,
                 embed_model=a.embed_model, ollama=a.ollama,
                 # The run history and the case book live in the app's home
                 # folder, which the app hands to every subprocess; started
                 # by hand there is neither.
                 runs_db=(str(Path(settings.home_env()) / run_history.DB_NAME)
                          if settings.home_env() else None),
                 faelle_db=(str(Path(settings.home_env()) / faelle.DB_NAME)
                            if settings.home_env() else None),
                 cases_write=settings.flag("MCP_CASES_WRITE", "mcp_cases_write"))

    backend = ("hybrid (BM25 + semantic, RRF)" if np is not None
               else "lexical (FTS5/BM25) only")
    print(f"munimentum MCP: {n_chunks} chunks · {backend}", file=sys.stderr)
    if a.transport == "http":
        security = _transport_security(a.host, a.port, a.allowed_host)
        # No endpoint echo here – the app already logs "MCP server started
        # on port N", and the settings show the exact snippet to paste.
        if security is not None:
            print(f"Host/Origin restricted to: {', '.join(security.allowed_hosts)}",
                  file=sys.stderr)
        mcp.run(transport="streamable-http", host=a.host, port=a.port,
                streamable_http_path=_HTTP_PATH, transport_security=security)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
