"""google_meet plugin — let the agent join a Meet call, transcribe it, follow up.

v1: transcribe-only. Spawns a headless Chromium via Playwright, joins the Meet
URL, enables live captions, scrapes them into a transcript file. The agent then
has the transcript in its workspace and can do whatever followup work it needs
using its regular tools.

v2 (not in this PR): realtime duplex audio so the agent can speak in the
meeting, via OpenAI Realtime / Gemini Live + BlackHole / PulseAudio null-sink.
``meet_say`` exists as a stub today so the tool surface is stable.

Explicit-by-design: only joins ``https://meet.google.com/`` URLs explicitly
passed in. No calendar scanning, no auto-dial, no consent announcement.
"""

from __future__ import annotations

import logging
import platform

from . import process_manager as pm
from .cli import register_cli as _register_meet_cli
from .cli import meet_command as _meet_command
from .tools import (
    MEET_CREATE_SCHEMA,
    MEET_JOIN_SCHEMA,
    MEET_LEAVE_SCHEMA,
    MEET_SAY_SCHEMA,
    MEET_STATUS_SCHEMA,
    MEET_TRANSCRIPT_SCHEMA,
    check_meet_requirements,
    handle_meet_create,
    handle_meet_join,
    handle_meet_leave,
    handle_meet_say,
    handle_meet_status,
    handle_meet_transcript,
)

logger = logging.getLogger(__name__)


_TOOLS = (
    ("meet_create",     MEET_CREATE_SCHEMA,     handle_meet_create,     "🆕"),
    ("meet_join",       MEET_JOIN_SCHEMA,       handle_meet_join,       "📞"),
    ("meet_status",     MEET_STATUS_SCHEMA,     handle_meet_status,     "🟢"),
    ("meet_transcript", MEET_TRANSCRIPT_SCHEMA, handle_meet_transcript, "📝"),
    ("meet_leave",      MEET_LEAVE_SCHEMA,      handle_meet_leave,      "👋"),
    ("meet_say",        MEET_SAY_SCHEMA,        handle_meet_say,        "🗣️"),
)


def _on_session_end(**kwargs) -> None:
    """Intentionally does NOT stop the meet bot.

    A meeting recording must survive agent-session transitions: the user keeps
    chatting (each message ends/restores a session) while the bot stays in the
    call recording. The original cleanup called ``pm.stop`` here, which killed
    the bot on every session end → the "bot suddenly leaves" bug (status shows
    ``exited: true`` with ``leaveReason: null``, i.e. a SIGTERM, not our own
    end-of-meeting detection).

    Orphaning is already covered without this: the bot self-terminates via its
    own detection (all_left / silence / duration), and systemd reaps the whole
    service cgroup — including the detached bot — on a real gateway shutdown.
    """
    return


def register(ctx) -> None:
    """Register tools, CLI, and lifecycle hooks.

    Called once by the plugin loader when the plugin is enabled via
    ``plugins.enabled`` in config.yaml.
    """
    # Windows is not supported in v1 — audio routing for v2 doesn't have a
    # tested path there and guest-join Chromium is flakier. Refuse to register
    # rather than half-working.
    system = platform.system().lower()
    if system not in {"linux", "darwin"}:
        logger.info(
            "google_meet plugin: platform=%s not supported (linux/macos only)",
            system,
        )
        return

    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="google_meet",
            schema=schema,
            handler=handler,
            check_fn=check_meet_requirements,
            emoji=emoji,
        )

    ctx.register_cli_command(
        name="meet",
        help="Google Meet bot (join, transcribe, follow up)",
        setup_fn=_register_meet_cli,
        handler_fn=_meet_command,
        description=(
            "Let the hermes agent join a Google Meet call and scrape live "
            "captions into a transcript. See: hermes meet setup"
        ),
    )

    ctx.register_hook("on_session_end", _on_session_end)
