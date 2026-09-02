#!/usr/bin/env python3
"""
probe_planner.py – one-off diagnostic: can THIS tenant's token read Planner
boards and, above all, their comments?

Planner has two comment worlds:
  * legacy: comments live as posts in the owning M365 group's Exchange
    conversation (task.conversationThreadId -> /groups/{gid}/threads/{tid}/
    posts). Reading them needs Group.Read.All – often admin-consent land.
  * new (chat-based, 2025+): a dedicated endpoint
    GET /beta/planner/tasks/{id}/messages, delegated Tasks.Read(Write) –
    no group scope needed.

This probe reads a handful of plans/tasks, tries BOTH paths and reports per
capability what worked and which scope was missing. It prints counts and
senders only, never comment bodies. Not wired into the app; run by hand:

    python3 probe_planner.py
"""

import base64
import json
import sys

import auth
import export_util
import graph_client

export_util.erzwinge_utf8()

GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Tasks.Read", RES + "Group.Read.All", RES + "User.Read"]

MAX_PLAENE = 5
MAX_TASKS_JE_PFAD = 3


class Graph(graph_client.Graph):
    def __init__(self, nur_still=False):
        super().__init__(SCOPES, nur_still=nur_still)


def scopes_im_token(graph):
    try:
        token = graph.token if hasattr(graph, "token") else None
        if not token:
            return None
        mitte = token.split(".")[1]
        mitte += "=" * (-len(mitte) % 4)
        return (json.loads(base64.urlsafe_b64decode(mitte)).get("scp") or "")
    except Exception:
        return None


def alle_seiten(graph, url):
    daten = []
    while url:
        d = graph.get(url)
        daten += d.get("value", [])
        url = d.get("@odata.nextLink")
    return daten


def versuch(name, fn):
    """Run one probe step; return (ok, result-or-error-text)."""
    try:
        return True, fn()
    except Exception as e:
        text = f"{type(e).__name__}: {e}"
        antwort = getattr(getattr(e, "response", None), "text", "")
        if antwort:
            try:
                fehler = json.loads(antwort).get("error", {})
                text = f"HTTP {e.response.status_code} · {fehler.get('code')}: " \
                       f"{(fehler.get('message') or '')[:140]}"
            except Exception:
                text = f"HTTP {getattr(e.response, 'status_code', '?')}"
        print(f"    ✗ {name}: {text}")
        return False, text


def main():
    graph = auth.waehle_zugang(lambda tok: graph_client.TokenClient(tok), Graph)
    scp = scopes_im_token(graph)
    if scp is not None:
        print("Token-Scopes:", " ".join(sorted(
            s for s in scp.split() if s.split("/")[-1] in (
                "Tasks.Read", "Tasks.ReadWrite", "Group.Read.All",
                "Group.ReadWrite.All", "User.Read"))) or "(keine relevanten)")
        print()

    ok, plaene = versuch("Pläne listen (/me/planner/plans)",
                         lambda: alle_seiten(graph, f"{GRAPH}/me/planner/plans"))
    if not ok:
        print("\nOhne Planliste geht nichts weiter – fehlt Tasks.Read?")
        sys.exit(1)
    print(f"{len(plaene)} Pläne sichtbar" +
          (" (Premium-Pläne des neuen Planner erscheinen hier NICHT)."
           if plaene else " – auch: keine Basic-Pläne oder keine Berechtigung."))

    legacy_ok = legacy_fehler = neu_ok = neu_fehler = 0
    neu_kommentare = 0
    for plan in plaene[:MAX_PLAENE]:
        container = plan.get("container") or {}
        gruppe = container.get("containerId") if \
            container.get("type") in ("group", "Group") else plan.get("owner")
        print(f"\nPlan „{plan.get('title')}“ "
              f"(Container: {container.get('type', 'owner-Gruppe')})")
        ok, tasks = versuch("Tasks listen", lambda p=plan: alle_seiten(
            graph, f"{GRAPH}/planner/plans/{p['id']}/tasks"))
        if not ok:
            continue
        mit_thread = [t for t in tasks if t.get("conversationThreadId")]
        print(f"  {len(tasks)} Tasks, davon {len(mit_thread)} mit "
              f"Legacy-Kommentarfaden (conversationThreadId)")

        # -- Weg 1: Legacy – Gruppen-Konversation ---------------------------
        for t in mit_thread[:MAX_TASKS_JE_PFAD]:
            ok, posts = versuch(
                f"Legacy-Posts zu „{(t.get('title') or '')[:40]}“",
                lambda t=t, g=gruppe: graph.get(
                    f"{GRAPH}/groups/{g}/threads/"
                    f"{t['conversationThreadId']}/posts?$top=10"))
            if ok:
                legacy_ok += 1
                n = len(posts.get("value", []))
                print(f"    ✓ Legacy lesbar: {n} Post(s)")
            else:
                legacy_fehler += 1

        # -- Weg 2: Neu – chatbasierte Kommentare (beta) --------------------
        for t in tasks[:MAX_TASKS_JE_PFAD]:
            ok, msgs = versuch(
                f"Neue Kommentare zu „{(t.get('title') or '')[:40]}“",
                lambda t=t: graph.get(
                    f"{RES}beta/planner/tasks/{t['id']}/messages"))
            if ok:
                neu_ok += 1
                n = len(msgs.get("value", []))
                neu_kommentare += n
                if n:
                    print(f"    ✓ Neuer Endpunkt lesbar: {n} Kommentar(e)")
            else:
                neu_fehler += 1

    print("\n" + "=" * 60)
    print("Fazit:")
    print(f"  Legacy (Gruppen-Konversation): {legacy_ok} lesbar, "
          f"{legacy_fehler} verweigert/fehlgeschlagen")
    print(f"  Neu (/beta/planner/tasks/…/messages): {neu_ok} Tasks abfragbar "
          f"({neu_kommentare} Kommentare), {neu_fehler} fehlgeschlagen")


if __name__ == "__main__":
    main()
