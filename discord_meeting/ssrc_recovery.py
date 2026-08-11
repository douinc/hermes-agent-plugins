"""Stop losing whole speakers when the bot reconnects mid-meeting.

Discord announces who an audio stream belongs to with a SPEAKING event, and it
only sends one when someone *starts* speaking while the bot is connected. If
the bot joins (or rejoins) a channel where people are already talking, their
SSRCs are never announced — their audio arrives unattributable and is dropped
on the floor. The failure is silent and total: someone already talking when
the bot arrives can be missing from the entire transcript, while whoever
happens to leave and rejoin is recorded normally.

hermes has a fallback (``_infer_user_for_ssrc``) but it only fires when exactly
one allowed member is in the channel, so it does nothing for an actual meeting.

Three changes here:

* **Infer from the sole *unmapped* member**, not the sole member. With three
  people and two already mapped, the third is no longer a guess — it is the
  only candidate left. This recovers speakers incrementally as SPEAKING events
  trickle in, rather than all-or-nothing.

* **Infer from who was in the room when the audio arrived**, not who is in it
  when the buffer closes. Attribution runs at close time, and for the last
  utterance before someone walks out that is *after* they are gone:
  ``flush_pending`` is called from ``leave_voice_channel``, by which point the
  channel no longer lists the speaker and there is nobody left to infer from,
  so the final sentence of every meeting was discarded. Worse, when one person
  of two leaves, the live channel offers the *remaining* person as the sole
  candidate — attributing the departed speaker's words to whoever stayed. The
  candidate set is now captured while the audio is still arriving and consulted
  first, which fixes the loss and that misattribution together.

* **Make the remaining losses loud.** When attribution genuinely is ambiguous
  the audio is still discarded — guessing between three people would put words
  in someone's mouth, which is worse in a meeting record than a gap. But it now
  reports how much was dropped and for whom, instead of failing silently.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_APPLIED = False
_LOCK = threading.Lock()
_dropped: dict[int, int] = {}
_last_report = 0.0
_REPORT_EVERY_S = 60.0


def _report_drop(ssrc: int) -> None:
    """Count an unattributable stream and warn periodically."""
    global _last_report
    now = time.time()
    with _LOCK:
        _dropped[ssrc] = _dropped.get(ssrc, 0) + 1
        if now - _last_report < _REPORT_EVERY_S:
            return
        _last_report = now
        summary = ", ".join(f"ssrc={s}×{n}" for s, n in sorted(_dropped.items()))
        total = sum(_dropped.values())
        _dropped.clear()
    logger.warning(
        "discord_meeting: dropped %d unattributable utterance(s) — no SPEAKING "
        "event for these streams, so the speaker is unknown and the audio is "
        "not recorded (%s). Usually means the bot joined after they were "
        "already talking.",
        total, summary,
    )


def _candidates(receiver) -> list:
    """Non-bot, allowed members currently in the receiver's channel."""
    vc = getattr(receiver, "_vc", None)
    channel = getattr(vc, "channel", None)
    if channel is None:
        return []
    bot_id = vc.user.id if getattr(vc, "user", None) else 0
    allowed = getattr(receiver, "_allowed_user_ids", set())
    return [
        m.id for m in getattr(channel, "members", [])
        if m.id != bot_id and (not allowed or str(m.id) in allowed)
    ]


def _remember_rosters(receiver) -> None:
    """Record who was in the room for each stream that is currently arriving.

    Only streams whose packet clock has advanced since the last snapshot are
    refreshed. That is the whole trick: a buffer sitting out its silence
    threshold after the speaker left stops being updated, so it keeps the
    roster from when the speaker was still there instead of being overwritten
    by the emptied room.
    """
    rosters = receiver.__dict__.setdefault("_dm_rosters", {})
    try:
        live = None
        for ssrc, seen_at in list(getattr(receiver, "_last_packet_time", {}).items()):
            known = rosters.get(ssrc)
            if known is not None and known[1] >= seen_at:
                continue
            if live is None:
                live = _candidates(receiver)
            if live:
                rosters[ssrc] = (tuple(live), seen_at)
    except Exception as exc:
        logger.debug("discord_meeting: roster snapshot skipped: %s", exc)


def install() -> bool:
    global _APPLIED
    if _APPLIED:
        return True

    from .patching import DISCORD_ADAPTER_MODULES, live_attr

    patched = 0
    for _module, cls in live_attr("VoiceReceiver", DISCORD_ADAPTER_MODULES):
        if not getattr(cls._infer_user_for_ssrc, "_discord_meeting_wrapped", False):

            def _infer(self, ssrc: int) -> int:
                try:
                    mapped = set(getattr(self, "_ssrc_to_user", {}).values())
                    remembered = self.__dict__.get("_dm_rosters", {}).get(ssrc)
                    # The remembered roster is the accurate candidate set for
                    # *this* audio: taken while it was arriving, so it still
                    # holds a speaker who has since walked out, and excludes
                    # anyone who only turned up afterwards. When there is one it
                    # is used *instead of* the live channel, never as a first
                    # try — falling through on an ambiguous roster would land on
                    # the live list, which after a departure is exactly the set
                    # of people who did NOT speak, and confidently credit them.
                    if remembered is not None:
                        source, members = "present when speaking", list(remembered[0])
                    else:
                        source, members = "in the channel now", _candidates(self)

                    unmapped = [uid for uid in members if uid not in mapped]
                    if len(unmapped) == 1:
                        uid = unmapped[0]
                        self._ssrc_to_user[ssrc] = uid
                        logger.info(
                            "discord_meeting: recovered ssrc=%d -> user=%d "
                            "(sole unmapped member %s, of %d)",
                            ssrc, uid, source, len(members),
                        )
                        return uid

                    _report_drop(ssrc)
                    return 0
                except Exception as exc:
                    logger.warning("discord_meeting: ssrc inference failed: %s", exc)
                    return 0

            _infer._discord_meeting_wrapped = True
            cls._infer_user_for_ssrc = _infer

        # Separate marker: stt_tuning wraps check_silence too, and chaining the
        # two is fine — this one only has to run before attribution does.
        if not getattr(cls.check_silence, "_discord_meeting_roster_wrapped", False):
            original = cls.check_silence

            def check_silence(self, *args, __original=original, **kwargs):
                _remember_rosters(self)
                return __original(self, *args, **kwargs)

            check_silence._discord_meeting_roster_wrapped = True
            cls.check_silence = check_silence

        patched += 1

    logger.info("discord_meeting: ssrc recovery patched on %d class(es)", patched)
    _APPLIED = patched > 0
    return _APPLIED
