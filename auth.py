#!/usr/bin/env python3
"""
auth.py – signing in to Microsoft Graph, once for all scripts.

There are two paths, and they differ in exactly one property that means
everything for the schedule:

  Access key  A ready-made access token, fetched by hand from Graph Explorer.
  (default)   Needs nobody from IT, but is valid for only a few hours and
              cannot be renewed. Once it expires, every run stalls until
              someone pastes a new one.

  Sign-in     A real sign-in via MSAL. The on-disk cache holds a refresh
              token from which fresh access tokens can be issued for weeks –
              so the schedule survives even a reboot. The default is
              Microsoft's own public application "Graph Command Line Tools",
              which needs no registration; anyone with their own app
              registration enters its client ID and tenant.

Both are configurable via settings.py, so they apply equally to the app and
to a manual call in the terminal. This module does not know app.py and must
never know it – the scripts have to run without the app.

What does NOT live here: the HTTP layer. The scripts have different timeouts,
throttling rules and retry counters, and merging those would mean hiding real
differences. What is shared is what truly was the same: reading the key and
signing in.
"""

import os
import sys
from pathlib import Path

import progress
import settings

# Microsoft's own public application. It is pre-approved in practically every
# tenant – which is why the sign-in path works without a single question to
# IT. A custom registration is only needed if the tenant explicitly demands
# one.
STANDARD_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
STANDARD_TENANT = "organizations"

TOKEN_DATEI = "gx_token.txt"
CACHE_DATEI = "msal_cache.bin"

RES = "https://graph.microsoft.com/"


class TokenExpired(RuntimeError):
    """A 401 in key mode – no renewal is possible there."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def modus():
    """"token" (default) or "login".

    Anything unknown yields "token": the path that always works is also the
    one a typo should fall back to.
    """
    roh = (os.environ.get("GRAPH_AUTH")
           or settings.value("auth_mode") or "token")
    roh = str(roh).strip().lower()
    return "login" if roh == "login" else "token"


def client_id():
    return (os.environ.get("GRAPH_CLIENT_ID")
            or settings.value("client_id") or STANDARD_CLIENT_ID)


def tenant():
    return (os.environ.get("GRAPH_TENANT")
            or settings.value("tenant") or STANDARD_TENANT)


def authority():
    return f"https://login.microsoftonline.com/{tenant()}"


def eigene_registrierung():
    """Does the sign-in point at a custom app registration?"""
    return (client_id(), tenant()) != (STANDARD_CLIENT_ID, STANDARD_TENANT)


def device_code():
    """Code login instead of a browser – for machines without a display."""
    return settings.flag("GRAPH_DEVICE_CODE", "device_code")


# --------------------------------------------------------------------------
# Key mode
# --------------------------------------------------------------------------
def token_datei():
    return settings.config_path().parent / TOKEN_DATEI


def load_pasted_token():
    """Read the pasted access key – environment beats file.

    Tolerates whatever comes along when copying from Graph Explorer: quotes,
    a leading "Bearer ", line breaks.
    """
    val = os.environ.get("GRAPH_TOKEN")
    if not val:
        # First next to the call, then in the data directory. The order is
        # the documented one: "put gx_token.txt next to this script" –
        # whoever does that should win with it.
        for p in (Path(TOKEN_DATEI), token_datei()):
            try:
                if p.exists():
                    val = p.read_text(encoding="utf-8")
                    break
            except OSError:
                continue
    if not val:
        return None
    val = val.strip().strip('"').strip("'").strip()
    if val.lower().startswith("bearer "):
        val = val[7:].strip()
    return val or None


# --------------------------------------------------------------------------
# Login mode
# --------------------------------------------------------------------------
def cache_datei():
    return settings.config_path().parent / CACHE_DATEI


class _Cache:
    """MSAL cache on disk – the reason the schedule keeps going.

    Without it the refresh token lives only in memory: one app restart, and
    the next sign-in is manual again. The file contains exactly this refresh
    token and is therefore created readable only by its owner.
    """

    def __init__(self, pfad):
        import msal
        self.pfad = Path(pfad)
        self.cache = msal.SerializableTokenCache()
        try:
            if self.pfad.exists():
                self.cache.deserialize(self.pfad.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass                      # broken cache = sign in once more

    def sichern(self):
        if not self.cache.has_state_changed:
            return
        try:
            self.pfad.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.pfad.with_name(self.pfad.name + ".tmp")
            tmp.write_text(self.cache.serialize(), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass                  # Windows does not know the mode
            tmp.replace(self.pfad)
        except OSError:
            pass                      # no write permission: fresh per run then


def cache_leeren():
    """Sign out: discard the refresh token."""
    try:
        cache_datei().unlink(missing_ok=True)
        return True
    except OSError:
        return False


class Login:
    """Sign-in via MSAL, with silent renewal.

    `scopes` comes from the calling script – Outlook needs different
    permissions than Teams, and requesting more than necessary would be poor
    style towards whoever has to consent.
    """

    def __init__(self, scopes, ausgabe=print, client=None, mandant=None):
        # `client`/`mandant` is passed through by the app: it manages its own
        # configuration and does not first write it into a file that
        # settings.py then reads back. Without them, whatever is configured
        # applies – so a manual call in the terminal stays unchanged.
        import msal
        self.scopes = list(scopes)
        self.ausgabe = ausgabe
        self._cache = _Cache(cache_datei())
        self.app = msal.PublicClientApplication(
            client or client_id(),
            authority=f"https://login.microsoftonline.com/{mandant or tenant()}",
            token_cache=self._cache.cache)
        self.account = None
        self.token = None
        self.fehler = ""

    # -- internal ---------------------------------------------------------
    def _fertig(self, res):
        if not res or "access_token" not in res:
            return False
        self.token = res["access_token"]
        accs = self.app.get_accounts()
        self.account = accs[0] if accs else self.account
        self._cache.sichern()
        return True

    def _still(self):
        """Renew from the cache without bothering the user."""
        for acc in ([self.account] if self.account else []) or self.app.get_accounts():
            if not acc:
                continue
            self.account = acc
            if self._fertig(self.app.acquire_token_silent(self.scopes, account=acc)):
                return True
        return False

    def _interaktiv(self):
        if device_code():
            flow = self.app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in flow:
                raise RuntimeError("Device-Flow fehlgeschlagen: "
                                   + str(flow.get("error_description")))
            self.ausgabe("\n" + flow["message"] + "\n")
            return self.app.acquire_token_by_device_flow(flow)
        return self.app.acquire_token_interactive(scopes=self.scopes,
                                                  prompt="select_account")

    # -- external ---------------------------------------------------------
    def anmelden(self, nur_still=False, weich=False):
        """Obtain a token.

        `nur_still` does not ask the user – the schedule sets this: nobody
        is sitting at the screen there, and a sign-in window that opens at
        three in the morning and waits until dawn helps no one.

        `weich` returns False on failure instead of aborting. The Teams
        export needs this: it first requests channel permissions and, if
        nobody grants them, signs in again with chat-only access.
        """
        if self._still():
            return True
        if nur_still:
            return False
        res = self._interaktiv()
        if self._fertig(res):
            return True
        if weich:
            self.fehler = (res or {}).get("error_description", "") or ""
            return False
        raise SystemExit("Anmeldung fehlgeschlagen: "
                         + ((res or {}).get("error_description") or "unbekannt"))

    def erneuern(self):
        """After a 401: renew silently, otherwise sign in again."""
        if self._still():
            return
        if not self._fertig(self._interaktiv()):
            raise SystemExit("Token-Erneuerung fehlgeschlagen.")

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}


class DeviceLogin:
    """Sign-in via device code, in two steps.

    For the UI in the browser this is the only sensible path: a native
    sign-in window (acquire_token_interactive) belongs to an application
    with its own window – this one has none. Instead the page shows a code
    to enter on a Microsoft page, and waits.

    Split into `start` and `warten` because `warten` blocks until the user
    is done: the caller puts it into a thread of its own and can display
    the code in the meantime.
    """

    def __init__(self, scopes, client=None, mandant=None):
        self.login = Login(scopes, ausgabe=lambda *_: None,
                           client=client, mandant=mandant)
        self.flow = None

    def start(self):
        self.flow = self.login.app.initiate_device_flow(scopes=self.login.scopes)
        if "user_code" not in self.flow:
            raise RuntimeError(str(self.flow.get("error_description")
                                   or "Gerätecode nicht erhalten"))
        return {"code": self.flow["user_code"],
                "url": self.flow.get("verification_uri")
                or "https://microsoft.com/devicelogin",
                "expires_in": int(self.flow.get("expires_in") or 900)}

    def warten(self):
        """Blocks until consent. Returns (ok, message)."""
        try:
            res = self.login.app.acquire_token_by_device_flow(self.flow)
        except Exception as e:                   # noqa: BLE001 – never kill the thread
            return False, f"{type(e).__name__}: {e}"
        if self.login._fertig(res):
            return True, ""
        return False, ((res or {}).get("error_description") or "abgebrochen")


def angemeldet(client=None, mandant=None):
    """Is a usable cache present? Without asking the user.

    For the UI: it should be able to show the state without ripping open a
    sign-in window.
    """
    try:
        import msal                              # noqa: F401
    except ImportError:
        return None
    if not cache_datei().exists():
        return None
    try:
        anmeldung = Login([RES + "User.Read"], ausgabe=lambda *_: None,
                          client=client, mandant=mandant)
        for acc in anmeldung.app.get_accounts():
            return acc.get("username") or True
    except Exception:                            # noqa: BLE001 – display only
        return None
    return None


# --------------------------------------------------------------------------
# For the scripts: pick a path and say which one it was
# --------------------------------------------------------------------------
def waehle_zugang(mit_schluessel, mit_login, ausgabe=print):
    """Pick the configured access path – silently, or fail with a clear reason.

    `mit_schluessel(token)` and `mit_login()` build the respective client;
    the scripts differ (Teams negotiates the channel scope), so they come
    from there.

    The scripts only ever run as app subprocesses, so nothing here may open
    a login window: the silent cache is tried first, a pasted key is the
    fallback, and if neither carries, the run ends with a structured
    token_expired event – the app then shows its token wizard.
    """
    gewaehlt = modus()
    if gewaehlt == "login":
        try:
            return mit_login(nur_still=True)        # no prompting
        except SystemExit:
            pass
        schluessel = load_pasted_token()
        if schluessel:
            ausgabe("Keine gültige Anmeldung im Zwischenspeicher – nutze den "
                    "hinterlegten Zugangsschlüssel für diesen Lauf.")
            return mit_schluessel(schluessel)
        progress.fehler("token_expired")
        raise SystemExit("Keine gültige Anmeldung und kein Zugangsschlüssel – "
                         "in der App anmelden oder einen Schlüssel einfügen.")
    schluessel = load_pasted_token()
    if schluessel:
        return mit_schluessel(schluessel)
    progress.fehler("token_expired")
    raise SystemExit("Kein Zugangsschlüssel hinterlegt – in der App einen "
                     "Schlüssel einfügen oder auf Anmeldung umstellen.")



def beschreibe(ausgabe=print):
    """One line about how this run signs in."""
    if modus() == "login":
        wo = ("eigene App-Registrierung" if eigene_registrierung()
              else "Microsofts öffentliche Anwendung")
        ausgabe(f"Anmeldemodus: Login ({wo}, Tenant {tenant()}).")
    else:
        ausgabe("Token-Modus aktiv – nutze Access Token aus Graph Explorer (kein Login).")


def main():
    """Self-report: which path applies, what is present.

    Also the bundle counterpart (`--run auth`): there it is the only way to
    verify, without network, that this module and msal really ship in the
    bundle. An export would otherwise notice only at the user's end.
    """
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    beschreibe()
    print(f"Client-ID: {client_id()}")
    print(f"Schlüssel: {'gefunden' if load_pasted_token() else 'keiner hinterlegt'}"
          f" ({token_datei()})")
    print(f"Cache:     {'vorhanden' if cache_datei().exists() else 'keiner'}"
          f" ({cache_datei()})")
    konto = angemeldet()
    print(f"Angemeldet: {konto if konto else 'nein'}")


if __name__ == "__main__":
    main()
