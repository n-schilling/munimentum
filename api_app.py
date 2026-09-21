"""
api_app.py – the app itself on /api/v1: status, environment, inventory,
settings, the MCP server, Ollama, updates, the Microsoft access, the
profiles, and quitting.
"""
import threading
from urllib.parse import quote

import api
import folders
import i18n
from api import Ablehnung


def status(h, _p, _q, _data):
    return api.json(h.app.status())


def umgebung(h, _p, _q, _data):
    return api.json(h.app.umgebung())


def bestand(h, _p, _q, _data):
    return api.json(h.app.bestand())


def konfig(h, _p, _q, _data):
    return api.json({"config": h.app.cfg})


def konfig_aendern(h, _p, _q, data):
    """PATCH: the keys the body names, the rest stays – the same merge
    the app's own route does, under the method that says so."""
    return api.json({"config": konfig_speichern(h, data)["config"]})


def konfig_speichern(h, data):
    def uebernehmen(cfg):
        if "outlook_categories" in data:
            cfg["outlook_categories"] = h.M._clean_categories(
                data["outlook_categories"], ["mail", "calendar", "contacts"])
        if "teams_categories" in data:
            cfg["teams_categories"] = h.M._clean_categories(
                data["teams_categories"], ["1on1", "group", "meeting", "channels"])
        for key in ("embed_model",
                    "chat_model", "ollama"):
            if key in data and str(data[key]).strip():
                cfg[key] = str(data[key]).strip()
        # Bounds so a mistyped number cannot cripple the next run: Graph
        # allows 4 concurrent requests per mailbox, anything above mostly
        # produces throttling; ports beyond 65535 do not exist.
        for key, low, high in (("workers", 1, 8), ("mirror_workers", 1, 16),
                               ("mcp_port", 1024, 65535),
                               ("index_batch", 1, 512), ("answer_sources", 1, 20),
                               ("semantic_min", 0, 95),
                               ("onedrive_max_mb", 0, 100000),
                               ("sharepoint_max_mb", 0, 100000),
                               ("sharepoint_pages_image_max_mb", 0, 100),
                               ("onenote_image_max_mb", 0, 100),
                               ("teams_files_max_mb", 0, 100000),
                               # 0 = the whole calendar, every run.
                               ("calendar_months_back", 0, 240),
                               # 0 = never re-read the chat comments.
                               ("planner_sweep_hours", 0, 8760),
                               ("search_results", 5, 100),
                               # Below five seconds the page would ask
                               # more often than anything can change.
                               ("status_poll_seconds", 5, 600),
                               # 0 means: userflow recording off.
                               ("userflow_actions", 0, 50),
                               ("runs_retention_months", 1, 120),
                               ("log_retention_days", 1, 365)):
            if key in data:
                try:
                    cfg[key] = max(low, min(high, int(data[key])))
                except (TypeError, ValueError):
                    pass
        for key in ("mcp_enabled", "mcp_autostart", "update_check", "embed_images", "cache_images",
                    "refresh_channels", "skip_empty_chats", "include_hidden",
                    "calendar_reconstruct", "ollama_enabled", "index_semantic",
                    # Missing since the checkbox exists: the state reached
                    # the run but never survived a page rebuild.
                    "onedrive_enabled", "sharepoint_enabled",
                    "sharepoint_pages_enabled", "planner_enabled",
                    "planner_attachments", "todo_enabled",
                    "onenote_enabled", "teams_attachments",
                    "teams_channel_files", "keep_awake", "mcp_cases_write"):
            if key in data:
                cfg[key] = bool(data[key])
        if "search_history" in data:
            wahl = str(data["search_history"] or "").strip().lower()
            if wahl in h.M.HISTORIE_WAHL:
                cfg["search_history"] = wahl
                # The new rule applies at once – "off" empties the list.
                h.app.faelle.aufraeumen(h.M.historie_tage(cfg))
        if "case_export_dir" in data:
            cfg["case_export_dir"] = str(data["case_export_dir"] or "").strip()
        # Who counts as internal, and who you are – both plain text
        for k in ("internal_domains", "own_name"):
            if k in data:
                cfg[k] = str(data[k] or "").strip()
        # Whoever switches Ollama off no longer means the check from just now.
        if "ollama_enabled" in data:
            h.app._ollama_cache = (0, None)
        if "calendar_rules" in data:
            cfg["calendar_rules"] = folders.schreibe_regeln(
                folders.lies_regeln(str(data["calendar_rules"] or "")))
        if "folder_rules" in data:
            cfg["folder_rules"] = folders.schreibe_regeln(
                folders.lies_regeln(str(data["folder_rules"] or "")))
        for key in ("sharepoint_urls", "sharepoint_pages_urls",
                    "planner_urls"):
            if key in data:
                cfg[key] = "\n".join(
                    z.strip() for z in str(data[key] or "").splitlines()
                    if z.strip())
        for key in ("sharepoint_types_include", "sharepoint_types_exclude"):
            if key in data:
                cfg[key] = ", ".join(
                    e for e in (s.strip().lstrip(".").lower()
                                for s in str(data[key] or "").split(","))
                    if e)
        if "onedrive_rules" in data:
            cfg["onedrive_rules"] = folders.schreibe_regeln(
                folders.lies_regeln(str(data["onedrive_rules"] or "")))
        for key in ("onenote_rules", "teams_rules", "sharepoint_rules",
                    "todo_rules"):
            if key in data:
                cfg[key] = folders.schreibe_regeln(
                    folders.lies_regeln(str(data[key] or "")))
        # A day or nothing: anything else would silently mean "nothing
        # older than never".
        for key in ("outlook_since", "teams_since"):
            if key in data:
                cfg[key] = h.M._clean_datum(data[key])
        if "analytics_skip" in data:
            cfg["analytics_skip"] = h.M._clean_zeilen(data["analytics_skip"])
        if "mcp_enabled" in data and not cfg.get("mcp_enabled", True):
            h.app.mcp.stop()
        if "filetype_hidden" in data:
            cfg["filetype_hidden"] = h.M._clean_endungen(data["filetype_hidden"])
        if "skip_folders" in data:
            cfg["skip_folders"] = h.M._clean_folders(data["skip_folders"])
        if "auth_mode" in data:
            # Anything unknown becomes token mode – the path that works
            # without asking IT.
            cfg["auth_mode"] = ("login" if str(data["auth_mode"]).strip().lower()
                                == "login" else "token")
        for key in ("client_id", "tenant"):
            if key in data:
                cfg[key] = str(data[key] or "").strip()
        if "sync_cadence" in data and isinstance(data["sync_cadence"], dict):
            cfg["sync_cadence"] = {
                str(k): v for k, v in data["sync_cadence"].items()
                if v in ("always", "daily", "weekly", "monthly")}
        if "notifications" in data:
            wert = str(data["notifications"] or "").strip().lower()
            if wert in ("off", "errors", "all"):
                cfg["notifications"] = wert
        if "tour_seen" in data and isinstance(data["tour_seen"], dict):
            cfg["tour_seen"] = {k: bool(v) for k, v in data["tour_seen"].items()
                                if k in ("archiv", "suche", "quelle")}
        if "language" in data:
            # Only known codes – one typo otherwise and the interface would
            # speak the fallback language forever.
            gewuenscht = str(data["language"] or "auto").strip().lower()
            erlaubt = {e["code"] for e in i18n.available(h.M.RES)} | {"auto"}
            if gewuenscht in erlaubt:
                cfg["language"] = gewuenscht
    h.app.konfiguriere(uebernehmen)
    return {"config": h.app.cfg}


def mcp(h, _p, _q, data):
    """The endpoint Claude talks to: `running` says whether it should
    be on – a boolean, nothing that merely reads like one."""
    if not isinstance(data.get("running"), bool):
        raise Ablehnung(400, "srv.mcp.badaction")
    return api.json(mcp_schalten(h, data["running"]))


def mcp_schalten(h, an):
    """Start or stop the MCP server – what `running` on PATCH /mcp
    asks for. A refused start still answers the server's state, so
    the page can draw it."""
    if an:
        ok, why = h.app.mcp.start(h.app.cfg)
        stand = h.app.mcp.status(h.app.cfg)
        if not ok:
            raise Ablehnung(409, why, mcp=stand)
        return {"message": why, "mcp": stand}
    h.app.mcp.stop()
    return {"mcp": h.app.mcp.status(h.app.cfg)}


def ollama(h, _p, _q, _data):
    return api.json({"ollama": h.app.ollama(force=True)})


def update(h, _p, _q, _data):
    return api.json({"update": h.app.check_updates(blockierend=True)})


def token(h, _p, _q, data):
    return api.json(token_speichern(h, data))


def token_speichern(h, data):
    token = h.M.normalize_token(data.get("token"))
    if not token:
        raise Ablehnung(400, "srv.token.empty")
    if len(token) < 40:
        raise Ablehnung(400, "srv.token.short")
    h.M.write_token(token)
    h.app.jobs.token_expired = False
    st = h.M.token_status(token, needed=h.app.selected_categories())
    if st["expired"]:
        raise Ablehnung(400, "srv.token.stale", token=st)
    msg = ({"k": "srv.token.saved.scopes", "v": {"list": ", ".join(st["missing"])}}
           if st["missing"] else {"k": "srv.token.saved", "v": {}})
    h.app.jobs.log(msg, "warn" if st["missing"] else "ok")
    return {"message": msg, "token": st}


def anmelden(h, _p, _q, _data):
    ok, daten = h.app.login_starten()
    if not ok:
        raise Ablehnung(500, "srv.login.failed",
                        {"detail": str(daten.get("error") or "")})
    return api.json({"device": daten})


def abmelden(h, _p, _q, _data):
    h.app.abmelden()
    return api.leer()


def hinweis_weg(h, _p, _q, _data):
    """"Later": the note about a key that died mid-run goes, or the
    wizard would reopen on every poll."""
    h.app.jobs.token_expired = False
    return api.leer()


def profile(h, _p, _q, _data):
    return api.json(h.M.profil_status())


def profil(h, p, _q, _data):
    """One profile – what `Location` names when one is created: its
    account, its folder, whether it is the one this instance runs."""
    name = str(p.get("name") or "").strip().lower()
    # One profile's files, not every profile's: profil_status reads
    # them all, which is why it is kept out of anything frequent.
    treffer = next((n for n in h.M.profil_namen() if n.lower() == name), None)
    if treffer is None:
        raise Ablehnung(404, "srv.profile.unknown", {"name": name})
    return api.json({"profile": h.M.profil_info(treffer)})


def profil_anlegen(h, _p, _q, data):
    info, fehler = h.M.profil_anlegen(data.get("name"))
    if fehler:
        raise Ablehnung(400, fehler)
    h.app.jobs.logk("srv.profile.created", "info", name=info["name"])
    return api.json({"profile": info, "profiles": h.M.profil_status()}, 201,
                      extra={"Location": f"{api.API_V1}/profiles/{quote(info['name'])}"})


def profile_einstellen(h, _p, _q, data):
    """A property of the register, not of one profile: whether the app
    asks which archive to open at start."""
    if "ask_at_start" not in data:
        raise Ablehnung(400, "srv.profile.nofield", {"name": "ask_at_start"})
    if not isinstance(data["ask_at_start"], bool):
        raise Ablehnung(400, "srv.profile.badvalue", {"name": "ask_at_start"})
    reg = h.M.profil_register_schreiben(ohne_nachfrage=not data["ask_at_start"])
    return api.json({"ask_at_start": not reg.get("ohne_nachfrage"),
                       "profiles": h.M.profil_status()})


def profil_umbenennen(h, p, _q, data):
    if h.app.jobs.busy:
        raise Ablehnung(409, "srv.busy")
    alt = str(p.get("name") or "").strip().lower()
    neu, fehler = h.M.profil_umbenennen(alt, data.get("name"),
                                    port=h.server.server_address[1])
    if fehler:
        raise Ablehnung(400, fehler)
    h.app.jobs.logk("srv.profile.renamed", "info", old=alt, name=neu)
    return api.json({"name": neu, "profiles": h.M.profil_status()})


def profil_oeffnen(h, p, _q, _data):
    return profil_wechseln(h, str(p.get("name") or ""))


def profil_wechseln(h, name):
    """Open this archive: the one that is already open answers at once,
    one open next door hands over its address, and otherwise this
    server restarts with it (serve() brings it back up)."""
    name = name.strip().lower()
    if not h.M.profile_moeglich():
        raise Ablehnung(400, "srv.profile.impossible")
    if name not in h.M.profil_namen():
        raise Ablehnung(404, "srv.profile.unknown", {"name": name})
    port = h.server.server_address[1]
    if name == h.M.PROFIL:
        return api.json({"name": name, "url": "/"})
    lauft = h.M.eigene_instanz(port, profil=name)
    if lauft:
        return api.json({"name": name, "url": f"http://127.0.0.1:{lauft}/"})
    if h.app.jobs.busy:
        raise Ablehnung(409, "srv.busy")
    h.M.profil_register_schreiben(zuletzt=name)
    # The answer first, then the server stops (serve() restarts). No
    # `url` here: that field means "it is already open elsewhere, go
    # there" – the page would reload at once, against a port that is
    # just shutting down, instead of waiting for it to come back.
    return api.json({"name": name},
                    nachher=lambda: h.M.neustart_mit_profil(h.server, name, port))


def beenden(h, _p, _q, _data):
    threading.Thread(target=h.server.shutdown, daemon=True).start()
    return api.leer()


def openapi(h, _p, _q, _data):
    """The contract itself: this file, as it is shipped."""
    text = (h.M.RES / "openapi.yaml").read_text(encoding="utf-8")
    return api.roh(200, text, "text/yaml; charset=utf-8")


# The routes of this door, in the order the table in app.py lists them.
ROUTEN = (
    ("GET", "/api/v1/status", status),
    ("GET", "/api/v1/app", umgebung),
    ("GET", "/api/v1/inventory", bestand),
    ("GET", "/api/v1/config", konfig),
    ("PATCH", "/api/v1/config", konfig_aendern),
    ("PATCH", "/api/v1/mcp", mcp),
    ("POST", "/api/v1/ollama/recheck", ollama),
    ("POST", "/api/v1/updates/check", update),
    ("PUT", "/api/v1/access/token", token),
    ("POST", "/api/v1/access/session", anmelden),
    ("DELETE", "/api/v1/access/session", abmelden),
    ("DELETE", "/api/v1/access/notice", hinweis_weg),
    ("GET", "/api/v1/profiles", profile),
    ("POST", "/api/v1/profiles", profil_anlegen),
    ("PATCH", "/api/v1/profiles", profile_einstellen),
    ("GET", "/api/v1/profiles/{name}", profil),
    ("PATCH", "/api/v1/profiles/{name}", profil_umbenennen),
    ("POST", "/api/v1/profiles/{name}/open", profil_oeffnen),
    ("POST", "/api/v1/quit", beenden),
    ("GET", "/api/v1/openapi", openapi),
)
