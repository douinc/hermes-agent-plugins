"""Meeting transcript sink for Discord voice channels.

Every voice utterance arrives as a synthetic ``MessageEvent`` (``VOICE``) on
the ``pre_gateway_dispatch`` hook — verified, not assumed. This module turns
that stream into a durable, human-readable meeting record and decides whether
the agent should answer.

Layout mirrors the google_meet plugin so the existing "read the transcript and
follow up" habits transfer unchanged::

    $HERMES_HOME/workspace/discord-meetings/<guild>/<channel>-<YYYYMMDD-HHMM>/
        transcript.txt   [HH:MM:SS] 표시명: 발화
        raw.jsonl        one row per utterance, for reprocessing
        meta.json        guild/channel ids, start + last timestamps, speakers

Meetings are cut on an idle gap rather than on voice-state events: a channel
that has been silent for ``HERMES_DISCORD_MEETING_GAP_MIN`` minutes starts a
new folder on the next utterance. Voice-state joins/leaves would be tighter,
but they are not visible from this hook, and an idle gap needs no extra event
source to be correct for the common case.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_NAMES: dict[str, str] = {}
_NAME_PENDING: set[str] = set()

# Variants observed in real transcripts, not guessed: Korean word-initial ㅎ is
# weak enough that both base and large-v3 render "헤르메스" as "에르메스". The
# wake word is a convenience path only — /meeting listen is the reliable one —
# but it should at least survive the mistake the model actually makes.
_DEFAULT_WAKE = "헤르메스,에르메스,허메스,헤르매스,에르매스,허미스,hermes,hermès"
_DEFAULT_GAP_MIN = 30.0


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _root() -> Path:
    p = _home() / "workspace" / "discord-meetings"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _gap_seconds() -> float:
    try:
        return float(os.environ.get("HERMES_DISCORD_MEETING_GAP_MIN", _DEFAULT_GAP_MIN)) * 60.0
    except ValueError:
        return _DEFAULT_GAP_MIN * 60.0


def _wake_words() -> list[str]:
    raw = os.environ.get("HERMES_DISCORD_MEETING_WAKE", _DEFAULT_WAKE)
    return [w.strip().lower() for w in raw.split(",") if w.strip()]


def _gate_enabled() -> bool:
    """False disables the wake-word gate — the bot answers every utterance."""
    return os.environ.get("HERMES_DISCORD_MEETING_GATE", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


# --------------------------------------------------------------------------
# speaker names
# --------------------------------------------------------------------------

def _names_path(guild_id: str) -> Path:
    d = _root() / str(guild_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / "speakers.json"


def _load_names(guild_id: str) -> None:
    if _NAMES:
        return
    try:
        _NAMES.update(json.loads(_names_path(guild_id).read_text(encoding="utf-8")))
    except Exception:
        pass


def _resolve_name_async(guild_id: str, user_id: str) -> None:
    """Fetch a display name in the background.

    The hook runs inline on the dispatch path, so it must never block on the
    network: an unknown speaker is written as their raw id and picked up by
    name on the next utterance once this thread has filled the cache.
    """
    if user_id in _NAMES or user_id in _NAME_PENDING:
        return
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return
    _NAME_PENDING.add(user_id)

    def _work():
        try:
            req = urllib.request.Request(
                f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}",
                headers={
                    "Authorization": f"Bot {token}",
                    "User-Agent": "DiscordBot (https://dou.so, 1.0)",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                m = json.load(r)
            name = m.get("nick") or (m.get("user") or {}).get("global_name") \
                or (m.get("user") or {}).get("username") or user_id
            with _LOCK:
                _NAMES[user_id] = name
                _names_path(guild_id).write_text(
                    json.dumps(_NAMES, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception as exc:
            logger.debug("speaker name lookup failed for %s: %s", user_id, exc)
        finally:
            _NAME_PENDING.discard(user_id)

    threading.Thread(target=_work, daemon=True).start()


def _speaker(guild_id: str, user_id: str) -> str:
    _load_names(guild_id)
    if user_id in _NAMES:
        return _NAMES[user_id]
    _resolve_name_async(guild_id, user_id)
    return str(user_id)


# --------------------------------------------------------------------------
# meeting folder
# --------------------------------------------------------------------------

def _closed_path(guild_id: str, channel_id: str) -> Path:
    d = _root() / str(guild_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"closed-{channel_id}.json"


def _grace_seconds() -> float:
    """How long after a meeting closes a late transcript still belongs to it."""
    try:
        return float(os.environ.get("HERMES_DISCORD_MEETING_GRACE_S", 90.0))
    except ValueError:
        return 90.0


def _pointer_path(guild_id: str, channel_id: str) -> Path:
    d = _root() / str(guild_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"current-{channel_id}.json"


def _meeting_dir(guild_id: str, channel_id: str, now: float) -> Path:
    """Return the folder for the live meeting, starting a new one after a gap."""
    ptr = _pointer_path(guild_id, channel_id)
    try:
        state = json.loads(ptr.read_text(encoding="utf-8"))
        if now - float(state["last_at"]) < _gap_seconds():
            d = Path(state["dir"])
            if d.is_dir():
                state["last_at"] = now
                ptr.write_text(json.dumps(state), encoding="utf-8")
                return d
    except Exception:
        pass

    # Late transcript from the meeting that just ended. Whisper takes seconds
    # (large-v3 more), so an utterance spoken *before* the room emptied can be
    # delivered after it closed. Opening a new meeting for it strands a
    # one-line phantom that never closes — and, being newest, that phantom is
    # what "아까 회의 정리해줘" would resolve to. It belongs to the meeting
    # that was live when it was spoken, so append there and leave it closed.
    try:
        c = json.loads(_closed_path(guild_id, channel_id).read_text(encoding="utf-8"))
        if now - float(c["closed_at"]) < _grace_seconds():
            d = Path(c["dir"])
            if d.is_dir():
                return d
    except Exception:
        pass

    # Seconds, not minutes: with membership-driven boundaries two meetings can
    # legitimately start within the same minute, and a colliding folder name
    # would silently append the second meeting to the first *and* overwrite the
    # first one's finalized meta.json.
    # Never reuse a folder that already holds a meeting: doing so appends the
    # new meeting to the old transcript *and* overwrites the finalized
    # meta.json below. A timestamp alone can't guarantee that — with
    # membership-driven boundaries two meetings can start in the same second —
    # so the name is bumped until it lands on an unused one.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    base = _root() / str(guild_id)
    d = base / f"{channel_id}-{stamp}"
    seq = 2
    while (d / "transcript.txt").exists():
        d = base / f"{channel_id}-{stamp}-{seq}"
        seq += 1
    d.mkdir(parents=True, exist_ok=True)
    ptr.write_text(json.dumps({"dir": str(d), "last_at": now}), encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps(
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("discord_meeting: new meeting folder %s", d)
    return d


# --------------------------------------------------------------------------
# wake word
# --------------------------------------------------------------------------

# Whisper, given silence or room noise, emits fluent sentences it learnt from
# subtitled video: Korean models are especially prone to the "자막 제공 …"
# family. hermes' own is_whisper_hallucination() only exact-matches a phrase
# list, so these variable-length variants sail through and land in the meeting
# record as if someone had said them. Substring hints, because the artifacts
# vary in wording but not in giveaway.
_NOISE_HINTS = (
    "자막 제공",
    "시청해주셔서 감사합니다",
    "시청해 주셔서 감사합니다",
    "구독과 좋아요",
    "구독 좋아요",
    "플러스친구",
    "한글자막",
    "다음 영상에서",
    "subtitles by",
    "amara.org",
    "thanks for watching",
)


def is_noise(text: str) -> bool:
    """True for transcripts that are STT artifacts rather than speech.

    Conservative on purpose: a false positive silently deletes something a
    person actually said, which is worse for a meeting record than one stray
    artifact line surviving.
    """
    s = (text or "").strip()
    if not s:
        return True
    low = s.lower()
    if any(h in low for h in _NOISE_HINTS):
        return True
    # Degenerate repetition ("자막 제공 및 자막 제공 및 …") — three or more
    # copies of the same short chunk is a decode loop, not speech.
    words = low.split()
    if len(words) >= 6:
        for size in (1, 2, 3):
            chunk = tuple(words[:size])
            if len(words) >= size * 3 and all(
                tuple(words[i:i + size]) == chunk
                for i in range(0, size * 3, size)
            ):
                return True
    return False


def is_addressed(text: str) -> bool:
    """True when the utterance is aimed at the bot.

    Only the opening of the sentence counts. Requiring the wake word up front
    is what keeps a meeting that merely *mentions* hermes from triggering a
    reply mid-discussion — the case that makes an always-on bot unusable.
    """
    # Must *open* the utterance, not merely appear in it. "에르메스" is also a
    # brand, so matching anywhere would wake the bot whenever someone mentions
    # one mid-sentence; requiring the very first token keeps the damage to
    # utterances that genuinely start by naming it.
    head = (text or "").strip().lower()
    return any(head.startswith(w) for w in _wake_words())


def strip_wake(text: str) -> str:
    """Drop a leading wake word (+ punctuation) so the agent sees a clean ask."""
    s = (text or "").strip()
    for w in _wake_words():
        # Also eat a vocative particle (에르메스**야**, 헤르메스**님**) so the
        # agent receives "정리해줘", not "야 정리해줘".
        m = re.match(
            rf"^\s*{re.escape(w)}\s*(?:야|아|님|씨)?\s*[,،.!?~…\-]*\s*",
            s,
            flags=re.IGNORECASE,
        )
        if m:
            return s[m.end():].strip() or s
    return s


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def finalize(guild_id: str, channel_id: str) -> str | None:
    """Close the live meeting for a channel. Returns the folder, or None.

    Dropping the pointer is what actually ends the meeting: the next utterance
    finds no live meeting and opens a fresh folder, regardless of how little
    time has passed. That is the whole point of driving boundaries off channel
    membership instead of an idle timer — two meetings ten minutes apart stay
    two meetings.
    """
    ptr = _pointer_path(str(guild_id), str(channel_id))
    try:
        state = json.loads(ptr.read_text(encoding="utf-8"))
        d = Path(state["dir"])
    except Exception:
        return None

    with _LOCK:
        try:
            meta_path = d / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            meta["ended_by"] = "channel_empty"
            speakers: list[str] = []
            for line in (d / "transcript.txt").read_text(encoding="utf-8").splitlines():
                if "] " in line and ": " in line:
                    who = line.split("] ", 1)[1].split(": ", 1)[0]
                    if who not in speakers:
                        speakers.append(who)
            meta["participants"] = speakers
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("finalize: meta update failed for %s: %s", d, exc)
        try:
            ptr.unlink()
        except OSError:
            pass
        # Remembered so a transcript still in flight lands here rather than
        # opening a phantom meeting (see _meeting_dir).
        _closed_path(str(guild_id), str(channel_id)).write_text(
            json.dumps({"dir": str(d), "closed_at": time.time()}), encoding="utf-8"
        )

    logger.info("discord_meeting: meeting closed %s", d)
    return str(d)


def record(guild_id: str, channel_id: str, user_id: str, text: str) -> dict:
    """Append one utterance; report whether the agent should answer it.

    Returns ``{"addressed": bool, "clean_text": str, "dir": str}``.
    """
    now = time.time()
    with _LOCK:
        d = _meeting_dir(str(guild_id), str(channel_id), now)

    name = _speaker(str(guild_id), str(user_id))
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    noise = is_noise(text)
    # An artifact is never an instruction — never let one wake the bot.
    addressed = (not noise) and ((not _gate_enabled()) or is_addressed(text))

    with _LOCK:
        # Artifacts stay out of transcript.txt (the thing humans and the agent
        # read) but are kept in raw.jsonl, so a filter that turns out to be too
        # aggressive can be audited and reversed instead of losing speech.
        if not noise:
            with (d / "transcript.txt").open("a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {name}: {text}\n")
        with (d / "raw.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(
                {
                    "ts": now,
                    "time": stamp,
                    "user_id": str(user_id),
                    "speaker": name,
                    "text": text,
                    "addressed": addressed,
                    "filtered_as_noise": noise,
                },
                ensure_ascii=False,
            ) + "\n")

    return {
        "addressed": addressed,
        "noise": noise,
        "clean_text": strip_wake(text) if addressed else text,
        "dir": str(d),
    }
