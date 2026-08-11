"""discord_meeting — probe build.

Question this build exists to answer: does a Discord **voice-channel**
transcript reach plugin land at all?

The voice path does not go through the normal ``discord.Message`` parsing.
``gateway/run.py:_handle_voice_channel_input`` builds a *synthetic*
``MessageEvent`` (``message_type=VOICE``) and hands it straight to
``adapter.handle_message``. Whether that synthetic event still reaches
``_handle_message``'s ``pre_gateway_dispatch`` call is the single assumption
the whole design rests on: if it does, the transcript sink, the wake-word gate
(``{"action": "skip"}``) and the summary tools all live in this plugin and
survive ``hermes update``. If it does not, they have to be patched into core.

So this build only *watches*. It never returns an action — every event is
allowed through exactly as before — and it appends one JSON line per event to
``$HERMES_HOME/workspace/discord-meetings/hook_probe.jsonl`` recording the
fields the real sink will need (speaker, guild, channel, text, type).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_TEXT = 300


def _probe_path() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    p = Path(home) / "workspace" / "discord-meetings"
    p.mkdir(parents=True, exist_ok=True)
    return p / "hook_probe.jsonl"


def _describe(value):
    """Best-effort JSON-safe rendering of an arbitrary attribute."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)[:200]


def _on_pre_gateway_dispatch(event=None, gateway=None, session_store=None, **_kw):
    """Record voice utterances; stay silent unless the bot was addressed."""
    try:
        # The client only exists once the adapter has connected, so the
        # boundary listener is attached from the first event that carries a
        # gateway reference rather than at register() time.
        if gateway is not None:
            from . import boundary

            boundary.ensure_installed(gateway)
    except Exception as exc:
        logger.warning("discord_meeting: boundary install failed: %s", exc)

    try:
        mtype = getattr(getattr(event, "message_type", None), "name", None)
        if mtype == "COMMAND":
            # Apply /meeting here, not in the slash-command handler: this is
            # the only place the invoking channel is visible.
            from . import mode

            source = getattr(event, "source", None)
            chat_id = getattr(source, "chat_id", None)
            if chat_id and mode.apply_command(chat_id, getattr(event, "text", "")):
                return None  # let the registered handler deliver the reply
        if mtype == "VOICE":
            result = _handle_voice(event)
            if result is not None:
                return result
    except Exception as exc:  # never let the sink break dispatch
        logger.warning("discord_meeting sink failed: %s", exc)

    try:
        source = getattr(event, "source", None)
        platform = getattr(source, "platform", None)
        mtype = getattr(event, "message_type", None)
        raw = getattr(event, "raw_message", None)

        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            # MessageType.VOICE is the marker that this came from a voice
            # channel utterance rather than a typed message.
            "message_type": _describe(getattr(mtype, "name", mtype)),
            "platform": _describe(getattr(platform, "value", platform)),
            "chat_id": _describe(getattr(source, "chat_id", None)),
            "chat_type": _describe(getattr(source, "chat_type", None)),
            "user_id": _describe(getattr(source, "user_id", None)),
            "user_name": _describe(getattr(source, "user_name", None)),
            "text": (getattr(event, "text", "") or "")[:_MAX_TEXT],
            # _handle_voice_channel_input stashes the guild on a SimpleNamespace
            # so the TTS reply knows which voice channel to play into — it is
            # also how the sink will key a meeting.
            "raw_guild_id": _describe(getattr(raw, "guild_id", None)),
            "raw_type": type(raw).__name__ if raw is not None else None,
        }
        with _probe_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # never let a probe break dispatch
        logger.warning("discord_meeting probe failed: %s", exc)
    return None


def _handle_voice(event):
    """Persist one utterance and decide whether the agent answers it.

    Returning ``{"action": "skip"}`` is what makes the bot usable in a real
    meeting: every word still lands in the transcript, but the agent only
    speaks when someone actually calls it. Returning ``rewrite`` on an
    addressed utterance hands the agent the request without the wake word.
    """
    from . import sink

    source = getattr(event, "source", None)
    raw = getattr(event, "raw_message", None)
    guild_id = getattr(raw, "guild_id", None)
    channel_id = getattr(source, "chat_id", None)
    user_id = getattr(source, "user_id", None)
    text = getattr(event, "text", "") or ""

    if not (guild_id and channel_id and user_id and text.strip()):
        return None

    from . import mode

    res = sink.record(guild_id, channel_id, user_id, text)

    # One predicate decides, here and in the echo filter. Re-deriving it from
    # its parts (wake word / listen / follow-up window) is how `mute` came to
    # be honoured by the echo filter but ignored by dispatch — the bot would
    # have kept answering the wake word while claiming to be muted.
    if not mode.should_engage(channel_id, user_id, text):
        return {"action": "skip", "reason": "discord_meeting: transcript-only (not addressed)"}

    # Engaging arms the follow-up window so the sentence *after* a summons
    # reaches the agent without re-saying the name.
    mode.open_window(channel_id, user_id)

    if res["clean_text"] and res["clean_text"] != text:
        return {"action": "rewrite", "text": res["clean_text"]}
    return None


_TOOLS = (
    ("meeting_list",       "MEETING_LIST_SCHEMA",       "handle_meeting_list",       "📋"),
    ("meeting_transcript", "MEETING_TRANSCRIPT_SCHEMA", "handle_meeting_transcript", "📝"),
    ("meeting_search",     "MEETING_SEARCH_SCHEMA",     "handle_meeting_search",     "🔎"),
)


def register(ctx) -> None:
    """Register the dispatch hook, the echo filter, and the transcript tools."""
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)

    from . import boundary, echo_filter, key_refresh, mode, ssrc_recovery, stt_tuning, tools

    echo_filter.install()
    stt_tuning.install()
    ssrc_recovery.install()
    key_refresh.install()
    # Attach the voice-state listener at connect time. Doing it only from the
    # dispatch hook meant a freshly restarted gateway had no listener until
    # somebody typed something — so walking into a voice channel did nothing.
    boundary.install_on_connect()
    boundary.start_attach_watcher()

    ctx.register_command(
        name="meeting",
        handler=lambda raw_args="": mode.pop_reply(),
        description="회의 응답 모드 — listen(전부 응답) / quiet(전사만) / status",
        args_hint="listen | quiet | status",
    )

    for name, schema_attr, handler_attr, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="discord_meeting",
            schema=getattr(tools, schema_attr),
            handler=getattr(tools, handler_attr),
            check_fn=tools.check_meeting_requirements,
            emoji=emoji,
        )

    logger.info("discord_meeting registered (hook + echo filter + %d tools)", len(_TOOLS))
