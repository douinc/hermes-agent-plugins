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

Two changes here:

* **Infer from the sole *unmapped* member**, not the sole member. With three
  people and two already mapped, the third is no longer a guess — it is the
  only candidate left. This recovers speakers incrementally as SPEAKING events
  trickle in, rather than all-or-nothing.

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


def install() -> bool:
    global _APPLIED
    if _APPLIED:
        return True

    from .patching import DISCORD_ADAPTER_MODULES, live_attr

    patched = 0
    for _module, cls in live_attr("VoiceReceiver", DISCORD_ADAPTER_MODULES):
        if getattr(cls._infer_user_for_ssrc, "_discord_meeting_wrapped", False):
            patched += 1
            continue

        def _infer(self, ssrc: int) -> int:
            try:
                channel = getattr(self._vc, "channel", None)
                if channel is None:
                    return 0
                bot_id = self._vc.user.id if getattr(self._vc, "user", None) else 0
                allowed = getattr(self, "_allowed_user_ids", set())
                members = [
                    m.id for m in getattr(channel, "members", [])
                    if m.id != bot_id and (not allowed or str(m.id) in allowed)
                ]
                mapped = set(getattr(self, "_ssrc_to_user", {}).values())
                unmapped = [uid for uid in members if uid not in mapped]

                if len(unmapped) == 1:
                    uid = unmapped[0]
                    self._ssrc_to_user[ssrc] = uid
                    logger.info(
                        "discord_meeting: recovered ssrc=%d -> user=%d "
                        "(sole unmapped member of %d)", ssrc, uid, len(members),
                    )
                    return uid

                _report_drop(ssrc)
                return 0
            except Exception as exc:
                logger.warning("discord_meeting: ssrc inference failed: %s", exc)
                return 0

        _infer._discord_meeting_wrapped = True
        cls._infer_user_for_ssrc = _infer
        patched += 1

    logger.info("discord_meeting: ssrc recovery patched on %d class(es)", patched)
    _APPLIED = patched > 0
    return _APPLIED
