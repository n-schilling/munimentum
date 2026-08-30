#!/usr/bin/env python3
"""
notify.py – native system notifications, fire-and-forget.

The one caller is the JobRunner in app.py: it tells the user that a run
finished or failed while the browser tab may well be closed – which is the
normal state when the scheduler does the work. Everything stays on the
machine; nothing is sent anywhere.

Only macOS is implemented, deliberately in two tiers:

  * Bundled app (Munimentum.app has a bundle identifier): the notification
    center API (UNUserNotificationCenter via PyObjC). Proper attribution,
    app icon, the system asks for permission with the first notification –
    and a click on the notification opens the interface in the browser.
    Click delivery needs the process main thread to run the system event
    loop; app.py hands it over via install_click_handler()/run_loop().
  * Run from source: `osascript` – no dependencies, generic icon, no click
    action. Good enough for development; the DMG never takes this path.

Windows and Linux are conscious no-ops for now: the plumbing (setting,
translations, JobRunner hook) is platform-neutral, only this module needs
a backend per OS.

send() never raises – a missed notification must never break a run.
"""

import subprocess
import sys

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
        if sys.platform != "darwin":
            return          # Windows/Linux: see module docstring
        title, body = str(title), str(body)
        if not _mac_native(title, body):
            _mac_osascript(title, body)
    except Exception:
        pass
