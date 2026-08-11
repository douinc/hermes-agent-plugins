"""Keep decryption keyed to the *current* voice session, not the first one.

``VoiceReceiver.start()`` snapshots ``conn.secret_key``, ``conn.ssrc`` and
``conn.dave_session`` once. Discord issues a fresh secret key with every
SESSION_DESCRIPTION, and discord.py sends one on every (re)connect — including
the ``_potential_reconnect`` it performs silently after Discord closes the
voice socket with 4014 ("you were moved"). From that moment the receiver is
decrypting live packets with a dead key, so every MAC check fails and the
entire meeting is dropped on the floor.

The correlation is exact: a receiver that sees a forced reconnect fails on
every packet from that moment on, and one that does not is fine for the life of
the call.

Worse, it is nearly invisible: ``_on_packet`` only warns for a receiver's first
ten packets, so a re-key three seconds in produces nine warnings and then hours
of silence that reads exactly like nobody talking.

The stale SSRC is the same bug wearing a different hat. ``_bot_ssrc`` is how
the receiver skips the bot's own audio; once it points at a previous session
the bot starts recording itself, which the dead key was accidentally hiding.

Rather than reimplement ``_on_packet`` (200 lines that hermes owns and will
keep changing), the three snapshot attributes are replaced with descriptors
that read through to the live connection. ``start()`` still assigns them and
the assigned value is kept as a fallback, so a connection that has not yet
published a key behaves exactly as before.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_APPLIED = False


class _LiveFromConnection:
    """Read an attribute from the live voice connection, not a stale copy.

    A data descriptor, so it wins over anything ``start()`` writes into the
    instance ``__dict__`` — the snapshot is redirected to a shadow slot and
    used only when the connection cannot answer.
    """

    def __init__(self, attr: str, slot: str, convert=None, default=None):
        self._attr = attr
        self._slot = slot
        self._convert = convert
        self._default = default

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        fallback = obj.__dict__.get(self._slot, self._default)
        conn = getattr(getattr(obj, "_vc", None), "_connection", None)
        if conn is None:
            return fallback
        live = getattr(conn, self._attr, None)
        # discord.py leaves these as MISSING (a sentinel, not None) until the
        # handshake completes; anything not convertible is treated as absent.
        if live is None:
            return fallback
        try:
            value = self._convert(live) if self._convert else live
        except Exception:
            return fallback
        if fallback is not None and value != fallback:
            # The one line that would have made this bug self-reporting.
            logger.info(
                "discord_meeting: voice session re-keyed mid-capture (%s changed) "
                "— decryption follows the new session",
                self._attr,
            )
            obj.__dict__[self._slot] = value
        return value

    def __set__(self, obj, value):
        obj.__dict__[self._slot] = value


_DESCRIPTORS = {
    "_secret_key": _LiveFromConnection(
        "secret_key", "_dm_secret_key", convert=bytes
    ),
    "_bot_ssrc": _LiveFromConnection(
        "ssrc", "_dm_bot_ssrc", convert=int, default=0
    ),
    "_dave_session": _LiveFromConnection("dave_session", "_dm_dave_session"),
}


def install() -> bool:
    """Swap the snapshot attributes for live reads. Returns True if applied."""
    global _APPLIED
    if _APPLIED:
        return True

    from .patching import DISCORD_ADAPTER_MODULES, live_attr

    patched = 0
    for _module, cls in live_attr("VoiceReceiver", DISCORD_ADAPTER_MODULES):
        if getattr(cls, "_discord_meeting_key_refresh", False):
            patched += 1
            continue
        for name, descriptor in _DESCRIPTORS.items():
            setattr(cls, name, descriptor)
        cls._discord_meeting_key_refresh = True
        patched += 1
        logger.info(
            "discord_meeting: decryption now follows voice re-keys (%s)",
            getattr(_module, "__name__", "?"),
        )

    _APPLIED = patched > 0
    return _APPLIED
