"""Headed Google sign-in against the Meet bot's persistent Chromium profile.

Same effect as ``hermes meet auth``, except it *polls* for sign-in completion
instead of blocking on ``input()`` — so it can be driven entirely from a VNC
window, with no second terminal to return to. Reuses the plugin's own context
and export helpers, so there is no divergence from the blessed path.

Not meant to be run directly: ``meet-auth-vnc`` sets up the display and invokes
this. See that script.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

# <plugins>/google_meet/tools/_meet_auth_driver.py  ->  <plugins>
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright is not installed — run: hermes meet install")

from google_meet.meet_bot import (  # noqa: E402
    _export_storage_state,
    _meet_home,
    _open_persistent_context,
    _profile_dir,
)

AUTH_PATH = _meet_home() / "auth.json"
DEADLINE = float(os.environ.get("HERMES_MEET_AUTH_MINUTES", "45")) * 60
LOCALE = os.environ.get("HERMES_MEET_LOCALE", "en-US")

# URL fragments that mean "still inside the sign-in flow"
SIGNIN_RE = re.compile(
    r"/signin|ServiceLogin|challenge|confirmidentifier|speedbump|rejected", re.I
)
LANDED = ("https://accounts.google.com", "https://accounts.google.com/b/0")


def say(msg: str) -> None:
    print(msg, flush=True)


say(f"profile  : {_profile_dir()}")
say(f"auth.json: {AUTH_PATH}")

with sync_playwright() as pw:
    ctx = _open_persistent_context(
        pw,
        headless=False,  # headed, on the virtual display
        args=["--disable-blink-features=AutomationControlled"],
        context_args={"locale": LOCALE},
        seed_auth=str(AUTH_PATH),
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://accounts.google.com/", wait_until="domcontentloaded")

    start, signed_in, last = time.time(), False, ""
    while time.time() - start < DEADLINE:
        time.sleep(3)
        try:
            # Ask the browser for the URL; do NOT read `page.url`. That property
            # returns Playwright's client-side cached main-frame URL, which has
            # been observed going stale — it still reported the pre-login URL
            # long after sign-in had completed, so the loop never fired.
            # evaluate() forces a round-trip. Re-resolve the page too: Google
            # may finish the flow in a tab other than the one we opened.
            pages = [p for p in ctx.pages if not p.is_closed()]
            if not pages:
                say("browser window closed")
                break
            page = pages[-1]
            url = page.evaluate("() => location.href")
        except Exception as e:  # closed mid-poll, or evaluate raced a navigation
            say(f"  (poll retry: {e})")
            continue

        if url != last:
            say(f"  url: {url[:100]}")
            last = url
        if SIGNIN_RE.search(url):
            continue
        if "myaccount.google.com" in url or url.rstrip("/") in LANDED:
            time.sleep(2)  # let the landing page settle
            signed_in = True
            break

    if not signed_in:
        say("\n✗ timed out / aborted — not signed in. auth.json left untouched.")
        ctx.close()
        sys.exit(1)

    # Warm the Meet origin so its cookies land in the profile too, then mirror
    # the live session out to auth.json (the browser meet_create backend reads
    # that snapshot).
    try:
        page.goto("https://meet.google.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        say(f"  meet: {page.evaluate('() => location.href')[:100]}")
    except Exception as e:
        say(f"  (meet warm-up failed, non-fatal: {e})")

    _export_storage_state(ctx, str(AUTH_PATH))
    ctx.close()

say("\n✓ signed in — session saved to the persistent profile and auth.json.")
