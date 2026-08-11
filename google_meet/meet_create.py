"""Create a Google Meet link.

Standalone subprocess (run as ``python -m google_meet.meet_create``). Prints
``MEET_URL=https://meet.google.com/...`` to stdout on success.

Two backends, selected by ``HERMES_MEET_CREATE_MODE`` (default ``auto``):

* **api** — one ``POST`` to the Google Meet REST API (``v2/spaces``) with an
  OAuth credential. Needs a GCP project with the Meet API enabled and a token
  carrying the ``meetings.space.created`` scope: run ``hermes meet oauth`` once.
* **browser** — the zero-config original: drive ``meet.google.com`` with the
  bot's signed-in cookie session (``HERMES_MEET_AUTH_STATE``, default
  ``$HERMES_HOME/workspace/meetings/auth.json``). No GCP project, no OAuth.

``auto`` uses **api** when a usable OAuth credential is present and falls back
to **browser** otherwise — so an install that never opted into OAuth keeps
working exactly as before.

The api backend is faster (~2s vs ~9s) and does not touch the cookie session.
The browser backend must inject that session into a *fresh, throwaway* Chromium
— it cannot reuse the join bot's persistent profile, which is locked while the
bot is in a call — and a virgin automated browser presenting long-lived auth
cookies is the shape of a stolen session, which Google is known to revoke on.
**That link is unmeasured here:** we have not shown that dropping the browser
backend lengthens session life. Treat api as removing a plausible risk, not a
proven fix.

Note what this does *not* fix: joining a call and scraping captions has no API,
so the join bot still needs the cookie session. If a Workspace session-length
policy expires that session every N days, it still will. See the README.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

_CODE_RE = re.compile(r"meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}")

#: Scope required by ``spaces.create``. Granted by ``hermes meet oauth``.
MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.created"

_SPACES_ENDPOINT = "https://meet.googleapis.com/v2/spaces"

_ENABLE_URL = "https://console.cloud.google.com/apis/library/meet.googleapis.com"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _auth_state() -> str:
    """Cookie storage_state used by the browser backend."""
    auth = os.environ.get("HERMES_MEET_AUTH_STATE", "").strip()
    if auth:
        return auth
    cand = _hermes_home() / "workspace" / "meetings" / "auth.json"
    return str(cand) if cand.is_file() else ""


def _oauth_token_path() -> str:
    """Authorized-user JSON used by the api backend.

    Defaults to hermes' shared Google token, which is where ``hermes meet
    oauth`` adds the Meet scope.
    """
    tok = os.environ.get("HERMES_MEET_OAUTH_TOKEN", "").strip()
    if tok:
        return tok
    cand = _hermes_home() / "google_token.json"
    return str(cand) if cand.is_file() else ""


def _load_api_credentials(verbose: bool = True):
    """Return refreshed OAuth credentials carrying the Meet scope, or ``None``.

    ``None`` means "api backend unavailable". Every reason below is a normal,
    expected state for an install that never opted into OAuth — hence the
    fallback rather than an error.
    """

    def unavailable(reason: str):
        if verbose:
            print(f"meet_create: api backend unavailable — {reason}", file=sys.stderr)
        return None

    path = _oauth_token_path()
    if not path:
        return unavailable("no OAuth token (run: hermes meet oauth)")

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return unavailable("google-auth not installed")

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return unavailable(f"unreadable OAuth token: {e}")

    if MEET_SCOPE not in (data.get("scopes") or []):
        return unavailable(f"token lacks {MEET_SCOPE} (run: hermes meet oauth)")

    try:
        creds = Credentials.from_authorized_user_info(data)
        if not creds.valid:
            creds.refresh(Request())
    except Exception as e:
        return unavailable(f"token refresh failed: {e}")

    # NB: deliberately NOT written back. This token file is shared with other
    # hermes components (Gmail/Calendar); a short-lived subprocess rewriting it
    # could clobber a concurrent writer. Refreshing in memory costs one cheap
    # HTTP round-trip and leaves the file owned by whoever created it.
    return creds


def _create_via_api(verbose: bool = True):
    """Create a space via the Meet REST API. Returns a URL string, or ``None``."""
    creds = _load_api_credentials(verbose=verbose)
    if creds is None:
        return None

    try:
        import requests
    except ImportError:
        if verbose:
            print(
                "meet_create: api backend unavailable — requests not installed",
                file=sys.stderr,
            )
        return None

    try:
        resp = requests.post(
            _SPACES_ENDPOINT,
            headers={"Authorization": f"Bearer {creds.token}"},
            json={},
            timeout=30,
        )
    except Exception as e:
        if verbose:
            print(f"meet_create: api call failed — {e}", file=sys.stderr)
        return None

    if resp.status_code != 200:
        try:
            err = (resp.json() or {}).get("error", {})
            detail = err.get("message", "") or resp.text[:200]
            # "API not enabled on the project" is the one failure worth spelling
            # out — it is a two-click fix and otherwise reads as a scope problem.
            if "disabled" in detail.lower() or err.get("status") == "FAILED_PRECONDITION":
                detail += f"  → enable the Meet API: {_ENABLE_URL}"
        except Exception:
            detail = resp.text[:200]
        if verbose:
            print(f"meet_create: api HTTP {resp.status_code} — {detail}", file=sys.stderr)
        return None

    url = ((resp.json() or {}).get("meetingUri") or "").strip()
    if not _CODE_RE.search(url):
        if verbose:
            print(f"meet_create: api returned no usable meetingUri ({url!r})", file=sys.stderr)
        return None
    return url


def _click(page, labels, roles=("menuitem", "button", "link")) -> bool:
    for label in labels:
        for role in roles:
            try:
                el = page.get_by_role(role, name=label, exact=False).first
                if el.count() and el.is_visible():
                    el.click(timeout=4000)
                    return True
            except Exception:
                pass
        try:
            el = page.locator(f'[aria-label="{label}"]').first
            if el.count():
                el.click(timeout=4000)
                return True
        except Exception:
            pass
    return False


def _create_via_browser():
    """Original flow: drive meet.google.com with the signed-in cookie session.

    Returns ``(exit_code, url)``; ``url`` is empty on failure.
    """
    from playwright.sync_api import sync_playwright

    auth = _auth_state()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx_args = {
            "viewport": {"width": 1280, "height": 900},
            "locale": os.environ.get("HERMES_MEET_LOCALE", "en-US"),
        }
        if auth and Path(auth).is_file():
            ctx_args["storage_state"] = auth
        ctx = browser.new_context(**ctx_args)
        page = ctx.new_page()
        try:
            page.goto("https://meet.google.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)

            if not _click(page, ("새 회의", "New meeting")):
                print("MEET_CREATE_FAILED: 'New meeting' button not found", file=sys.stderr)
                return 2, ""
            time.sleep(1.5)
            if not _click(page, ("나중에 진행할 회의 만들기", "Create a meeting for later")):
                print("MEET_CREATE_FAILED: 'Create a meeting for later' not found", file=sys.stderr)
                return 3, ""

            url = None
            for _ in range(25):
                time.sleep(1)
                url = page.evaluate(
                    r"""
                    () => {
                      const re = /meet\.google\.com\/[a-z]{3}-[a-z]{4}-[a-z]{3}/;
                      for (const inp of document.querySelectorAll('input,textarea')) {
                        const m = (inp.value || '').match(re);
                        if (m) return 'https://' + m[0];
                      }
                      const m = (document.body.innerText || '').match(re);
                      return m ? 'https://' + m[0] : null;
                    }
                    """
                )
                if url:
                    break
            if not url:
                print("MEET_CREATE_FAILED: link did not appear", file=sys.stderr)
                return 4, ""
            return 0, url
        finally:
            browser.close()


def main() -> int:
    mode = (os.environ.get("HERMES_MEET_CREATE_MODE") or "auto").strip().lower()
    if mode not in {"auto", "api", "browser"}:
        print(
            f"MEET_CREATE_FAILED: bad HERMES_MEET_CREATE_MODE={mode!r} "
            "(expected auto|api|browser)",
            file=sys.stderr,
        )
        return 5

    if mode in {"auto", "api"}:
        url = _create_via_api(verbose=True)
        if url:
            print("MEET_URL=" + url)
            return 0
        if mode == "api":
            # Explicitly requested — do not silently do something else.
            print(
                "MEET_CREATE_FAILED: api backend requested but unavailable "
                "(reason above; run `hermes meet oauth`)",
                file=sys.stderr,
            )
            return 6
        print("meet_create: falling back to the browser backend", file=sys.stderr)

    code, url = _create_via_browser()
    if code == 0:
        print("MEET_URL=" + url)
    return code


if __name__ == "__main__":
    sys.exit(main())
