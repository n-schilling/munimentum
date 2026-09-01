#!/usr/bin/env python3
"""
notify.py – native system notifications, fire-and-forget.

The one caller is the JobRunner in app.py: it tells the user that a run
finished or failed while the browser tab may well be closed – which is the
normal state when the scheduler does the work. Everything stays on the
machine; nothing is sent anywhere.

One backend per OS:

  * macOS, bundled app (Munimentum.app has a bundle identifier): the
    notification center API (UNUserNotificationCenter via PyObjC). Proper
    attribution, app icon, the system asks for permission with the first
    notification – and a click on the notification opens the interface.
    Click delivery needs the process main thread to run the system event
    loop; app.py hands it over via install_click_handler()/run_loop().
  * macOS, run from source: `osascript` – no dependencies, generic icon,
    no click action. Good enough for development; the DMG never takes this
    path.
  * Windows: a toast via PowerShell and WinRT – no dependency, no COM
    registration. The toast declares protocol activation, so a click opens
    the interface in the browser; attribution is PowerShell's (Windows
    shows unpackaged apps under the posting host's identity).
  * Linux: `notify-send` (libnotify) when present, silently nothing when
    not. No click action – portable actions would need a D-Bus listener.

send() never raises – a missed notification must never break a run.
"""

import os
import shutil
import subprocess
import sys

# The interface address a clicked notification should open. macOS delivers
# clicks through install_click_handler(); Windows bakes the URL into the
# toast itself, so app.py deposits it here once at startup.
_open_url = ""


def set_open_url(url):
    global _open_url
    _open_url = str(url or "")

# Lazily resolved once: a UNUserNotificationCenter, or None after a failed
# attempt (not bundled, PyObjC missing) – then osascript takes over.
_center = "unresolved"
_authed = False
_delegate = None                 # keeps the click delegate alive


def _mac_center():
    global _center
    if _center == "unresolved":
        _center = None
        try:
            import Foundation
            import UserNotifications as UN     # noqa: F401 – probe the import
            if Foundation.NSBundle.mainBundle().bundleIdentifier():
                _center = (UN.UNUserNotificationCenter
                           .currentNotificationCenter())
        except Exception:
            _center = None
    return _center


def _mac_native(title, body):
    """Post via the notification center; True when handed to the system."""
    center = _mac_center()
    if center is None:
        return False
    try:
        import UserNotifications as UN
        global _authed
        if not _authed:
            # Permission dialog with the first notification, not at launch –
            # declining means the system drops later adds silently, which is
            # the wish expressed.
            center.requestAuthorizationWithOptions_completionHandler_(
                UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound,
                lambda granted, error: None)
            _authed = True
    except Exception:
        return False
    try:
        import Foundation
        import UserNotifications as UN
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            Foundation.NSUUID.UUID().UUIDString(), content, None)
        center.addNotificationRequest_withCompletionHandler_(request, None)
        return True
    except Exception:
        return False


def _mac_osascript(title, body):
    # Arguments travel as argv, not interpolated into the script – titles
    # and bodies can then contain quotes without becoming AppleScript.
    script = ("on run argv\n"
              "display notification (item 2 of argv) with title (item 1 of argv)\n"
              "end run")
    subprocess.run(["osascript", "-e", script, title, body],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=10, check=False)


# Values travel as environment variables, never interpolated into the
# script: titles can then carry quotes without becoming PowerShell, and the
# XML escape happens on the receiving side.
_WIN_TOAST = r"""
$esc = [System.Security.SecurityElement]
$titel = $esc::Escape($env:MUNI_TITLE)
$text = $esc::Escape($env:MUNI_BODY)
$url = $esc::Escape($env:MUNI_URL)
$aktion = if ($env:MUNI_URL) { " activationType=`"protocol`" launch=`"$url`"" } else { "" }
$xml = "<toast$aktion><visual><binding template=`"ToastGeneric`"><text>$titel</text><text>$text</text></binding></visual></toast>"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$aumid = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid).Show(
    [Windows.UI.Notifications.ToastNotification]::new($doc))
"""


def _win_toast(title, body):
    umgebung = {**os.environ, "MUNI_TITLE": title, "MUNI_BODY": body,
                "MUNI_URL": _open_url}
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_TOAST],
        env=umgebung, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=15, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _linux_notify(title, body):
    werkzeug = shutil.which("notify-send")
    if not werkzeug:
        return                     # headless or minimal system: stay quiet
    subprocess.run([werkzeug, "--app-name=Munimentum", title, body],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=10, check=False)


def install_click_handler(callback):
    """Open-the-app on click: register `callback` for notification clicks.

    Returns True only on the native macOS path (bundled app). The caller must
    then keep the main thread in run_loop() – without a system event loop the
    click would never reach this process.
    """
    global _delegate
    center = _mac_center()
    if center is None:
        return False
    if _delegate is not None:      # registering twice would redefine the class
        return True
    try:
        import Foundation
        import UserNotifications as UN

        class _ClickDelegate(Foundation.NSObject):
            def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
                    self, center, response, handler):
                try:
                    callback()
                except Exception:
                    pass
                handler()

            def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                    self, center, notification, handler):
                # Also show banners while the app counts as active.
                banner = getattr(
                    UN, "UNNotificationPresentationOptionBanner",
                    getattr(UN, "UNNotificationPresentationOptionAlert", 4))
                handler(banner | UN.UNNotificationPresentationOptionSound)

        _delegate = _ClickDelegate.alloc().init()
        center.setDelegate_(_delegate)
        return True
    except Exception:
        return False


def run_loop():
    """Run the system event loop on the main thread (blocks until stop_loop).

    Foundation's console loop, no NSApplication – the app stays without a
    permanent Dock icon, but the main queue drains and click callbacks
    arrive."""
    from PyObjCTools import AppHelper
    AppHelper.runConsoleEventLoop(installInterrupt=True)


def stop_loop():
    """Stop run_loop(); safe to call from any thread, and when not running."""
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(AppHelper.stopEventLoop)
    except Exception:
        pass


def send(title, body):
    """Show one system notification; silently does nothing where unsupported."""
    try:
        title, body = str(title), str(body)
        if sys.platform == "darwin":
            if not _mac_native(title, body):
                _mac_osascript(title, body)
        elif sys.platform == "win32":
            _win_toast(title, body)
        elif sys.platform.startswith("linux"):
            _linux_notify(title, body)
    except Exception:
        pass
