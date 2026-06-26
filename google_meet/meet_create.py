"""Create a Google Meet link using the bot's signed-in browser session.

Standalone subprocess (run as ``python -m google_meet.meet_create``). Reuses the
same authenticated session as the join bot (``HERMES_MEET_AUTH_STATE``, default
``$HERMES_HOME/workspace/meetings/auth.json``) — no GCP project or OAuth needed.

Flow: open meet.google.com signed in → "새 회의 / New meeting" →
"나중에 진행할 회의 만들기 / Create a meeting for later" → read the generated link.
Prints ``MEET_URL=https://meet.google.com/...`` to stdout on success.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

_CODE_RE = re.compile(r"meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}")


def _auth_state() -> str:
    auth = os.environ.get("HERMES_MEET_AUTH_STATE", "").strip()
    if auth:
        return auth
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    cand = Path(home) / "workspace" / "meetings" / "auth.json"
    return str(cand) if cand.is_file() else ""


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


def main() -> int:
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
                return 2
            time.sleep(1.5)
            if not _click(page, ("나중에 진행할 회의 만들기", "Create a meeting for later")):
                print("MEET_CREATE_FAILED: 'Create a meeting for later' not found", file=sys.stderr)
                return 3

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
                return 4
            print("MEET_URL=" + url)
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
