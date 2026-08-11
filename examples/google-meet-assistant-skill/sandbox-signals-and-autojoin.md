# Google Meet Auto-Join & Sandbox Lifecycle Integration

## 1. Organization Account Auto-Join (No Guest Name)
When joining a Google Meet call within the same organization, the bot can leverage the authenticated Google profile state (configured via `hermes meet auth`) to bypass the host manual admission queue (the lobby) and enter the meeting room instantly.

To achieve this:
* **DO NOT** provide the `guest_name` parameter (or `--guest-name` CLI argument).
* Omitting the guest name forces the underlying Chromium browser to launch using the logged-in session state rather than guest mode.
* Providing any guest name forces a guest-mode join, which redirects the bot to the lobby where it must wait for manual host approval.

## 2. Container Sandbox & Lifecycle SIGTERM Behavior
In certain stateless container sandbox environments (such as remote workspace runtimes or chat gateways like Discord):
* **Symptom**: The background bot exits immediately or is found to be terminated (`exited: true`, `error: null`, `leaveReason: null`) when a new user message/turn is processed.
* **Root Cause**: The container sandbox runtime or session gateway automatically suspends or sends a `SIGTERM` signal to all background subprocesses spawned during a previous turn whenever a new message starts a new execution turn.
* **Workaround/Handling**:
  1. Do not treat this as a software bug or crash (as indicated by the lack of error stacks).
  2. Explain the environment lifecycle constraints clearly to the user.
  3. Be prepared to immediately run `meet_join` or `hermes meet join <URL>` to re-join the meeting whenever the user asks the bot to enter the meeting again.
