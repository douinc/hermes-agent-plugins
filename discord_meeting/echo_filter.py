"""Suppress the per-utterance ``**[Voice]**`` echo — without touching core.

``gateway/run.py:_handle_voice_channel_input`` posts every transcribed
utterance into the bound text channel *before* it dispatches the event, so the
``pre_gateway_dispatch`` hook (which is how this plugin stays silent when it
isn't addressed) runs far too late to stop it. In a real meeting that echo is
noise: the transcript is already being persisted to disk by ``sink.py``.

There is no hook for outbound sends, so the interception has to happen in the
discord.py layer. Two rejected approaches, recorded so they don't get retried:

* *Reimplement ``_handle_voice_channel_input`` in the plugin* — duplicates ~45
  lines of core logic (auth, dedup, session binding) that would silently drift
  out of sync on the next hermes update.
* *Make ``get_channel`` return None for the duration of the call* — the echo
  and the agent's own reply share that channel lookup across an ``await``, so
  a concurrent utterance could lose its reply.

What is patched instead is ``discord.abc.Messageable.send``, filtered on the
exact literal the echo is built from. Nothing else in hermes emits a message
starting with ``**[Voice]**``, so the blast radius is that one call site even
though the patch is on a shared class.
"""

from __future__ import annotations

import logging
import re
import os

logger = logging.getLogger(__name__)

_MARKER = "**[Voice]**"
_PATCHED = False

# "**[Voice]** <@123456789012345678>: 방금 내용 정리해줘"
_ECHO_RE = re.compile(r"^\*\*\[Voice\]\*\*\s*<@!?(\d+)>:\s*(.*)$", re.S)


def _is_for_the_bot(channel, content: str) -> bool:
    """True when this echo is the user's *request*, not meeting chatter.

    Suppressing the echo wholesale hides what the bot actually heard, which is
    the only way to tell a wrong answer from a misheard question. So the echo
    survives exactly for utterances that are being handed to the agent.

    The send happens before the dispatch hook, but the ordering still works:
    the wake-word utterance matches on its own text, and by the time a
    follow-up is echoed the previous utterance's hook has already opened the
    window.
    """
    m = _ECHO_RE.match(content)
    if not m:
        return False
    user_id, text = m.group(1), m.group(2)
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return False
    try:
        from . import mode

        return mode.should_engage(str(channel_id), user_id, text)
    except Exception as exc:
        # Unsure → keep the message. Losing a request is worse than one echo.
        logger.warning("discord_meeting: echo decision failed (%s), keeping", exc)
        return True


def _enabled() -> bool:
    """True when the echo should be suppressed (default: suppress)."""
    return os.environ.get("HERMES_DISCORD_VOICE_ECHO", "0").strip().lower() in {
        "0", "false", "no", "off",
    }


def install() -> bool:
    """Patch ``Messageable.send`` once. Returns True when the patch is live."""
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import discord.abc
    except Exception as exc:
        logger.warning("discord_meeting: discord.py unavailable, echo filter off: %s", exc)
        return False

    original = discord.abc.Messageable.send

    async def _send(self, content=None, *args, **kwargs):
        if (
            _enabled()
            and isinstance(content, str)
            and content.startswith(_MARKER)
            and not _is_for_the_bot(self, content)
        ):
            # Caller (`await channel.send(...)` inside a try/except) ignores the
            # return value, so dropping the message needs no stand-in Message.
            return None
        return await original(self, content, *args, **kwargs)

    _send.__name__ = "send"
    _send._discord_meeting_patched = True  # idempotence marker across reloads
    discord.abc.Messageable.send = _send
    _PATCHED = True
    logger.info("discord_meeting: [Voice] echo filter installed")
    return True
