"""Per-channel response mode — the reliable way to summon the bot.

A spoken wake word has to survive speech-to-text, and it does not: "헤르메스"
came back as "에르메스" and the bot stayed silent, which is the failure mode
that makes a voice assistant useless. A slash command never passes through
Whisper, so it always means exactly what it says.

Modes:
    quiet  (default)  transcript only, but the wake word still summons it
    listen            answers every utterance until switched back
    mute              answers nothing at all, wake word included

``quiet`` leaves a door open on purpose; ``mute`` is for when the bot must not
speak under any circumstance — a customer on the call, a recorded session —
and it drops any follow-up window that is already open so it takes effect on
the next word rather than 45 seconds later.

Plugin slash-command handlers are called as ``handler(raw_args)`` with no
channel context, so the toggle itself is applied from ``pre_gateway_dispatch``
(which does see ``source.chat_id``) and the handler only reports what that
already decided.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()
_LAST_REPLY: dict[str, str] = {}

QUIET = "quiet"
LISTEN = "listen"
MUTE = "mute"


def _path() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    d = Path(home) / "workspace" / "discord-meetings"
    d.mkdir(parents=True, exist_ok=True)
    return d / "modes.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def get(channel_id: str) -> str:
    return _load().get(str(channel_id), QUIET)


def set_mode(channel_id: str, mode: str) -> str:
    with _LOCK:
        data = _load()
        data[str(channel_id)] = mode
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return mode


def autojoin(channel_id: str) -> bool:
    """Whether the bot follows people into this voice channel. Default: yes.

    Defaulting to on is the point — a meeting nobody remembered to invite the
    bot to is a meeting with no transcript, and the failure is silent until
    someone asks for the summary afterwards.
    """
    return bool(_load().get(f"autojoin:{channel_id}", True))


def set_autojoin(channel_id: str, enabled: bool) -> None:
    with _LOCK:
        data = _load()
        data[f"autojoin:{channel_id}"] = bool(enabled)
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_command(channel_id: str, raw: str) -> str | None:
    """Interpret a ``/meeting …`` invocation. Returns the reply, or None.

    Returning None means "not ours" — the command falls through untouched.
    """
    arg = (raw or "").strip().lower()
    for prefix in ("/meeting", "meeting"):
        if arg.startswith(prefix):
            arg = arg[len(prefix):].strip()
            break
    arg = arg.split()[0] if arg.split() else ""

    if arg in {"autojoin", "자동입장"}:
        rest = (raw or "").strip().lower().split()
        want = rest[-1] if rest else ""
        if want in {"off", "no", "끄기", "해제"}:
            set_autojoin(channel_id, False)
            reply = "🚪 **자동 입장 해제** — 이 채널은 `/voice channel`로 직접 불러야 합니다."
        elif want in {"on", "yes", "켜기"}:
            set_autojoin(channel_id, True)
            reply = "🚪 **자동 입장 켜짐** — 누군가 들어오면 봇도 따라 들어갑니다."
        else:
            reply = (
                f"자동 입장: **{'켜짐' if autojoin(channel_id) else '꺼짐'}**  "
                "(`/meeting autojoin on` | `off`)"
            )
    elif arg in {"mute", "silent", "묵음", "무시"}:
        set_mode(channel_id, MUTE)
        close_all_windows(channel_id)
        reply = (
            "🤐 **mute** — 어떤 경우에도 응답하지 않습니다. 웨이크워드도 무시합니다.\n"
            "전사는 계속 기록됩니다. 해제는 `/meeting quiet`."
        )
    elif arg in {"listen", "on", "듣기", "응답"}:
        set_mode(channel_id, LISTEN)
        reply = (
            "🎙️ **listen** — 이제 이 채널의 모든 발화에 응답합니다.\n"
            "회의로 돌아가려면 `/meeting quiet`."
        )
    elif arg in {"quiet", "off", "조용", "회의"}:
        set_mode(channel_id, QUIET)
        reply = (
            "🔇 **quiet** — 전사만 기록하고 응답하지 않습니다.\n"
            "부르려면 `/meeting listen`, 또는 발화 앞에 웨이크워드."
        )
    elif arg in {"status", "", "상태"}:
        reply = (
            f"응답 모드: **{get(channel_id)}**  "
            "(`listen` = 전부 응답, `quiet` = 부를 때만, `mute` = 절대 응답 안 함)\n"
            f"자동 입장: **{'켜짐' if autojoin(channel_id) else '꺼짐'}**"
        )
    else:
        return None

    _LAST_REPLY[str(channel_id)] = reply
    _LAST_REPLY["_latest"] = reply
    return reply


# --------------------------------------------------------------------------
# follow-up window
# --------------------------------------------------------------------------
# Utterances are cut after 1.5s of silence (VoiceReceiver.SILENCE_THRESHOLD),
# so "에르메스야" <pause> "방금 내용 정리해줘" arrives as *two* events. Only
# the first carries the wake word, so without a follow-up window the actual
# request is filed as meeting chatter and the bot answers a summons with no
# content. Addressing the bot therefore opens a short window in which that
# same speaker keeps its ear without repeating the wake word — and each
# further utterance extends it, so a real back-and-forth doesn't time out
# mid-conversation.
#
# NOTE: extension is unbounded. 30s is short enough that a natural pause
# closes it, but a speaker who keeps talking at <30s intervals — normal in
# a meeting — holds it open indefinitely. /meeting mute is the manual
# escape; a hard cap on total window life is the structural fix, not yet
# implemented.

_WINDOWS: dict[str, float] = {}
_DEFAULT_WINDOW_S = 30.0


def _window_seconds() -> float:
    try:
        return float(os.environ.get("HERMES_DISCORD_MEETING_FOLLOWUP_S", _DEFAULT_WINDOW_S))
    except ValueError:
        return _DEFAULT_WINDOW_S


def _key(channel_id: str, user_id: str) -> str:
    # Per speaker, not per channel: in a meeting the people who did *not*
    # summon the bot are still just talking to each other.
    return f"{channel_id}:{user_id}"


def open_window(channel_id: str, user_id: str) -> None:
    import time

    with _LOCK:
        _WINDOWS[_key(channel_id, user_id)] = time.monotonic() + _window_seconds()


def in_window(channel_id: str, user_id: str) -> bool:
    import time

    k = _key(channel_id, user_id)
    with _LOCK:
        until = _WINDOWS.get(k)
        if until is None:
            return False
        if time.monotonic() > until:
            _WINDOWS.pop(k, None)
            return False
    return True


def close_window(channel_id: str, user_id: str) -> None:
    with _LOCK:
        _WINDOWS.pop(_key(channel_id, user_id), None)


def close_all_windows(channel_id: str) -> None:
    """Drop every open follow-up window in a channel — used by mute.

    Muting has to take effect on the next word, not after whatever window a
    previous summons happened to leave open.
    """
    prefix = f"{channel_id}:"
    with _LOCK:
        for k in [k for k in _WINDOWS if k.startswith(prefix)]:
            _WINDOWS.pop(k, None)


def should_engage(channel_id: str, user_id: str, text: str) -> bool:
    """Would this utterance be handed to the agent? Pure — never mutates state.

    Shared by the dispatch hook and the echo filter so the two can never
    disagree about what counts as talking to the bot. The hook additionally
    opens the follow-up window; this function only reads.
    """
    from . import sink

    if sink.is_noise(text):
        return False
    # mute outranks everything, wake word included — that is the whole point of
    # having it separate from quiet.
    if get(channel_id) == MUTE:
        return False
    if sink.is_addressed(text):
        return True
    if get(channel_id) == LISTEN:
        return True
    return in_window(channel_id, str(user_id))


def pop_reply() -> str:
    """Reply text for the slash-command handler, which has no channel context."""
    return _LAST_REPLY.pop("_latest", None) or (
        "사용법: `/meeting listen` | `quiet` | `mute` | `autojoin on|off` | `status`"
    )
