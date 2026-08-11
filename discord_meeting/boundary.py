"""Meeting boundaries from voice-channel membership, not from a timer.

The idle-gap rule the sink falls back on has to pick a threshold, and every
threshold is wrong twice: a long break inside one meeting splits it, and two
meetings scheduled close together merge. Channel membership has no such
ambiguity — a meeting is over when the last person leaves the room.

The dispatch hook only sees message events, so the signal is taken straight
from discord.py instead: hermes' client is a ``commands.Bot``, which accepts
additional listeners for an event it already handles, so this attaches its own
``on_voice_state_update`` without touching the adapter's.

Attachment must not wait for a message event. It originally did, and after a
gateway restart nobody had typed anything — so no listener existed at the exact
moment someone walked into a voice channel, and auto-join silently never fired.
Instead ``connect()`` is wrapped (the client exists from then on) and a short
background watcher covers the orderings that misses: the Discord plugin loading
after this one, or having already connected. If all of that fails nothing
breaks — the sink falls back to cutting meetings on the idle gap.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)



def _find_adapter(gateway):
    """Locate the Discord adapter (which owns the client), or None."""
    adapters = getattr(gateway, "adapters", None) or {}
    for adapter in adapters.values():
        client = getattr(adapter, "_client", None)
        if client is not None and hasattr(client, "add_listener"):
            return adapter
    return None


def _non_bot_members(channel) -> int:
    try:
        return sum(1 for m in getattr(channel, "members", []) if not getattr(m, "bot", False))
    except Exception:
        return -1


_KEEP = object()  # "this event says nothing about where the bot belongs"

# A follow costs two hops (leave + join), so this allows a handful of queued
# switches before the worker gives up and lets the next event start fresh.
_MAX_FOLLOW_STEPS = 12

# guild_id -> {"target": channel|None, "active": bool, "lock": asyncio.Lock}
_FOLLOW: dict[int, dict] = {}


def _slot(guild_id: int) -> dict:
    slot = _FOLLOW.get(guild_id)
    if slot is None:
        slot = {"target": None, "active": False, "lock": asyncio.Lock()}
        _FOLLOW[guild_id] = slot
    return slot


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return getattr(a, "id", None) == getattr(b, "id", None)


def _bot_channel(adapter, guild_id):
    """The voice channel the bot is connected to in this guild, or None.

    ``_voice_clients`` is keyed by guild, so "connected" says nothing about
    *where*: every decision phrased as "the room the bot is in" has to read the
    connection's channel, or it acts on a different room of the same guild.
    """
    vc = getattr(adapter, "_voice_clients", {}).get(guild_id)
    if vc is None:
        return None
    return getattr(vc, "channel", None)


def _committed_channel(adapter, guild_id):
    """Where the bot is *or is on its way to* — whichever is newer.

    A join takes about a second and people switch rooms faster than that.
    Judging against the live connection alone means every event that lands
    mid-join reads the room the bot is leaving, concludes no follow is needed,
    and drops it — so the bot completes the stale move and sits alone in a room
    the person already walked out of.
    """
    slot = _FOLLOW.get(guild_id)
    if slot is not None and slot["active"]:
        return slot["target"]
    return _bot_channel(adapter, guild_id)


async def _join_channel(gateway, adapter, channel) -> bool:
    """Join a voice channel, wiring it up the way /voice channel does.

    Joining alone is not enough: ``_handle_voice_channel_input`` bails out when
    ``_voice_text_channels[guild]`` is unset, so a bare ``join_voice_channel``
    would connect a bot that transcribes nothing. The callbacks and that
    binding are what the /voice channel handler sets up, and they have to be in
    place *before* the join or the first utterance is lost.
    """
    guild_id = channel.guild.id

    if hasattr(adapter, "_voice_input_callback"):
        adapter._voice_input_callback = gateway._handle_voice_channel_input
    if hasattr(adapter, "_on_voice_disconnect"):
        adapter._on_voice_disconnect = gateway._handle_voice_timeout_cleanup
    # /voice channel sets this so the inactivity timer can see the reply mode;
    # auto-join skipped it, which is half of why the bot was dropping out of
    # meetings after 5 minutes. Leaving is this plugin's job (channel empty),
    # not a timer's — and a timer-driven rejoin costs every speaker whose SSRC
    # was mapped, since Discord never re-announces them.
    if hasattr(adapter, "_voice_mode_getter"):
        adapter._voice_mode_getter = lambda chat_id: gateway._voice_mode.get(
            gateway._voice_key(adapter.platform, str(chat_id)), "off"
        )

    try:
        ok = await adapter.join_voice_channel(channel)
    except Exception as exc:
        logger.warning("discord_meeting: auto-join failed: %s", exc)
        return False
    if not ok:
        return False

    # Discord voice channels carry their own text chat, so the channel is its
    # own binding target — which is also what /voice channel resolved to in
    # practice.
    try:
        adapter._voice_text_channels[guild_id] = int(channel.id)
    except (TypeError, ValueError):
        adapter._voice_text_channels[guild_id] = channel.id
    except Exception as exc:
        logger.warning("discord_meeting: text-channel binding failed: %s", exc)
        return False

    # Speak answers to spoken questions, but don't start narrating replies to
    # typed messages in the room. /voice channel sets "all"; for a meeting the
    # bot was never explicitly invited to, voice_only is the quieter default.
    try:
        key = gateway._voice_key(adapter.platform, str(channel.id))
        if gateway._voice_mode.get(key) is None:
            gateway._voice_mode[key] = "voice_only"
            gateway._save_voice_modes()
    except Exception as exc:
        logger.debug("discord_meeting: voice mode default skipped: %s", exc)

    logger.info(
        "discord_meeting: auto-joined %s", getattr(channel, "name", channel.id)
    )
    return True


async def _follow(gateway, adapter, guild_id: int, target) -> None:
    """Drive the bot to ``target`` (None = leave), newest destination wins.

    Events do not wait for each other — discord.py dispatches every listener as
    its own task — so two room changes three seconds apart overlap, and the
    second one used to read the connection state the first had not finished
    changing yet and do nothing at all. So the destination is a slot rather
    than an argument: late events overwrite it, and the one worker holding the
    lock re-reads it after every hop instead of acting on what was true when it
    started.

    Reconnecting rather than moving is deliberate. ``join_voice_channel`` calls
    ``VoiceClient.move_to`` when a connection already exists, and that can fail
    to complete at all: Discord closes the old voice socket with 4014 ("you
    were moved"), discord.py's ``_potential_reconnect`` waits for a voice server
    update that never arrives, and ``move_to`` swallows its own timeout — so
    every move burns the full 30s timeout and still reports success. A fresh
    ``connect()`` after a real disconnect takes well under a second, which is
    why the bot is taken out of the old room first even though that costs the
    SSRC map.
    """
    slot = _slot(guild_id)
    slot["target"] = target
    slot["active"] = True
    if slot["lock"].locked():
        return  # a worker is already driving, and it re-reads the slot

    async with slot["lock"]:
        try:
            for _ in range(_MAX_FOLLOW_STEPS):
                want = slot["target"]
                vc = getattr(adapter, "_voice_clients", {}).get(guild_id)
                here = getattr(vc, "channel", None) if vc is not None else None
                # No await between this check and leaving the block, so no
                # event can slip in and be dropped: it either updated the slot
                # before the check, or it finds the lock free afterwards.
                if want is None and vc is None:
                    return
                if want is not None and _same(here, want):
                    return
                if vc is not None:
                    try:
                        await adapter.leave_voice_channel(guild_id)
                    except Exception as exc:
                        logger.warning("discord_meeting: leave failed: %s", exc)
                        return
                    if want is None:
                        logger.info(
                            "discord_meeting: left empty voice channel %s",
                            getattr(here, "name", here),
                        )
                    continue
                if not await _join_channel(gateway, adapter, want):
                    return
            logger.warning(
                "discord_meeting: gave up following after %d hops (guild %s)",
                _MAX_FOLLOW_STEPS,
                guild_id,
            )
        finally:
            slot["active"] = False


def _desired_channel(adapter, guild_id, before_ch, after_ch, emptied):
    """Where this event says the bot belongs, or ``_KEEP`` for "not my call".

    Most voice events are about rooms the bot has no stake in; only a room it
    is in (or committed to) emptying, or a person arriving somewhere while the
    bot is unattached, should move it.
    """
    from . import mode

    committed = _committed_channel(adapter, guild_id)
    vacated = emptied and _same(committed, before_ch)

    if after_ch is not None and (committed is None or vacated):
        if mode.autojoin(str(after_ch.id)):
            return after_ch
        # Auto-join is off for the destination, but the room the bot is in
        # still just emptied — it should not stay there talking to nobody.
        return None if vacated else _KEEP
    if vacated:
        return None
    return _KEEP


def install_on_connect() -> bool:
    """Attach the listener when the adapter connects, not on first message.

    Waiting for a dispatched message to find the client meant that after a
    restart nobody had spoken yet, so the listener was absent exactly when it
    was needed: walking into a voice channel produced no event, and auto-join
    silently never happened. ``connect()`` is where the adapter builds its
    client, so wrapping it attaches the listener the moment there is one.
    """
    from .patching import DISCORD_ADAPTER_MODULES, live_attr

    patched = 0
    for _module, cls in live_attr("DiscordAdapter", DISCORD_ADAPTER_MODULES):
        if getattr(cls.connect, "_discord_meeting_wrapped", False):
            patched += 1
            continue

        original = cls.connect

        async def connect(self, *args, __original=original, **kwargs):
            result = await __original(self, *args, **kwargs)
            try:
                attach(self)
            except Exception as exc:
                logger.warning("discord_meeting: listener attach failed: %s", exc)
            return result

        connect._discord_meeting_wrapped = True
        cls.connect = connect
        patched += 1

    logger.info("discord_meeting: connect() wrapped on %d adapter class(es)", patched)
    return patched > 0


def start_attach_watcher(interval: float = 3.0, attempts: int = 100) -> None:
    """Keep trying to attach until the live adapter exists, in the background.

    Two orderings have to work and neither is under our control: the Discord
    platform plugin may load *after* this one (so ``connect`` cannot be wrapped
    at register time), and it may have already connected (so wrapping is too
    late and the live instance needs attaching directly). A short polling
    thread covers both without waiting for a message event — which was the
    original bug: nothing had been typed since the restart, so the listener
    never attached and walking into a voice channel did nothing.
    """
    import gc
    import threading
    import time

    from .patching import DISCORD_ADAPTER_MODULES, live_attr

    def _work():
        for _ in range(attempts):
            try:
                install_on_connect()
                # These patch classes in the same not-yet-imported module, so
                # they need the same retry — running them once at register()
                # left MIN_SPEECH_DURATION and ssrc recovery silently unapplied.
                from . import key_refresh, ssrc_recovery, stt_tuning

                stt_tuning.install()
                ssrc_recovery.install()
                key_refresh.install()
                classes = [cls for _m, cls in live_attr("DiscordAdapter", DISCORD_ADAPTER_MODULES)]
                for cls in classes:
                    for obj in gc.get_objects():
                        try:
                            if isinstance(obj, cls) and getattr(obj, "_client", None) is not None:
                                if attach(obj):
                                    logger.info("discord_meeting: attached to live adapter")
                                    return
                        except ReferenceError:
                            continue
            except Exception as exc:
                logger.debug("discord_meeting: attach watcher retry: %s", exc)
            time.sleep(interval)
        logger.warning("discord_meeting: gave up attaching voice-state listener")

    threading.Thread(target=_work, name="discord-meeting-attach", daemon=True).start()


def ensure_installed(gateway) -> bool:
    """Fallback path: attach from a dispatched event if connect() was missed."""
    adapter = _find_adapter(gateway)
    if adapter is None:
        return False
    return attach(adapter)


def attach(adapter) -> bool:
    """Attach the voice-state listener to this adapter's client. Idempotent."""
    client = getattr(adapter, "_client", None)
    if client is None or not hasattr(client, "add_listener"):
        return False
    if getattr(adapter, "_discord_meeting_attached", False):
        return True

    async def _on_voice_state_update(member, before, after):
        try:
            # Resolved per event, not captured at attach time: connect() runs
            # before gateway/run.py sets the backref, so capturing it early
            # would pin None forever.
            gateway = getattr(adapter, "gateway_runner", None)

            # The bot's own moves (a restart, /voice leave, being dragged) are
            # not a meeting starting or ending — only the humans decide that.
            if getattr(member, "bot", False):
                return

            before_ch = before.channel
            after_ch = after.channel
            if before_ch == after_ch:
                return  # mute/deafen/stream toggles carry no room change

            # A move arrives as ONE event with both sides set, so the room
            # being left has to be settled before the room being entered:
            # deciding the destination first read the guild's voice slot while
            # it still pointed at the old room, refused the follow, and then
            # tore down whatever connection was in the slot.
            emptied = before_ch is not None and _non_bot_members(before_ch) == 0
            if emptied:
                from . import sink

                closed = sink.finalize(str(before_ch.guild.id), str(before_ch.id))
                logger.info(
                    "discord_meeting: %s emptied — meeting closed (%s)",
                    getattr(before_ch, "name", before_ch.id),
                    closed or "none live",
                )

            if gateway is None:
                return
            guild_id = (before_ch or after_ch).guild.id
            target = _desired_channel(adapter, guild_id, before_ch, after_ch, emptied)
            # Nobody left to hear or be heard: holding the connection open
            # leaves the bot alone decoding silence into hallucinations, which
            # is exactly the junk the noise filter exists to fight.
            if target is not _KEEP:
                await _follow(gateway, adapter, guild_id, target)
        except Exception as exc:  # a listener must never break the client
            logger.warning("discord_meeting boundary listener failed: %s", exc)

    client.add_listener(_on_voice_state_update, "on_voice_state_update")
    adapter._discord_meeting_attached = True
    logger.info("discord_meeting: voice-state boundary listener attached")
    return True
