"""Agent-facing tools over the Discord meeting transcripts.

Deliberately three read-only tools and no ``meeting_summary``: summarising is
the agent's own job once it can *read* the transcript, and a tool that wraps an
LLM call would only bury that reasoning behind a second, weaker model. The
google_meet plugin makes the same call (``meet_transcript`` + the agent's own
follow-up), so the habits transfer.

    meeting_list        what meetings exist, newest first
    meeting_transcript  read one (or the latest) meeting
    meeting_search      find which meeting mentioned something
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

MEETING_LIST_SCHEMA: Dict[str, Any] = {
    "name": "meeting_list",
    "description": (
        "List recorded Discord voice-channel meetings, newest first. Use this "
        "first when the user refers to a meeting without saying which one, so "
        "you can pick the right meeting_id before reading a transcript."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max meetings to return (default 10).",
                "minimum": 1,
            },
        },
        "required": [],
    },
}

MEETING_TRANSCRIPT_SCHEMA: Dict[str, Any] = {
    "name": "meeting_transcript",
    "description": (
        "Read a Discord voice-channel meeting transcript, as timestamped "
        "'[HH:MM:SS] speaker: text' lines. Defaults to the most recent meeting "
        "when meeting_id is omitted — that is usually what 'the meeting we just "
        "had' means. Read this before summarising, filing issues, or answering "
        "questions about what was said."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "meeting_id": {
                "type": "string",
                "description": "Meeting id from meeting_list. Omit for the latest.",
            },
            "last": {
                "type": "integer",
                "description": "Return only the last N lines instead of the whole transcript.",
                "minimum": 1,
            },
        },
        "required": [],
    },
}

MEETING_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "meeting_search",
    "description": (
        "Search across all recorded meeting transcripts for a substring "
        "(case-insensitive). Use when the user remembers a topic but not which "
        "meeting it was in; then read that meeting with meeting_transcript."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to look for."},
            "limit": {
                "type": "integer",
                "description": "Max matching lines to return (default 30).",
                "minimum": 1,
            },
        },
        "required": ["query"],
    },
}


def _root() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "workspace" / "discord-meetings"


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _err(msg: str) -> str:
    return _json({"success": False, "error": msg})


def _started_at(d: Path) -> str:
    """Sort key: the meeting's own recorded start.

    Not parsed out of the folder name — a de-duplicating suffix
    (``…-195322-2``) shifts the fields and silently sorts a *newer* meeting
    below an older one, which makes "the meeting we just had" return the wrong
    transcript. meta.json states the start directly; mtime is the fallback.
    """
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta.get("started_at"):
            return str(meta["started_at"])
    except Exception:
        pass
    try:
        return f"mtime:{d.stat().st_mtime:.0f}"
    except OSError:
        return ""


def _meetings() -> List[Path]:
    """All meeting folders, newest first."""
    root = _root()
    if not root.is_dir():
        return []
    out = [
        d for guild in root.iterdir() if guild.is_dir()
        for d in guild.iterdir()
        if d.is_dir() and (d / "transcript.txt").is_file()
    ]
    return sorted(out, key=lambda p: (_started_at(p), p.name), reverse=True)


def _describe(d: Path) -> Dict[str, Any]:
    lines = _lines(d)
    speakers: List[str] = []
    for ln in lines:
        if "] " in ln and ": " in ln:
            who = ln.split("] ", 1)[1].split(": ", 1)[0]
            if who not in speakers:
                speakers.append(who)
    meta = {}
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "meeting_id": d.name,
        "guild_id": meta.get("guild_id"),
        "channel_id": meta.get("channel_id"),
        "started_at": meta.get("started_at"),
        "lines": len(lines),
        "speakers": speakers,
    }


def _lines(d: Path) -> List[str]:
    try:
        return [l for l in (d / "transcript.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []


def _find(meeting_id: str) -> Path | None:
    for d in _meetings():
        if d.name == meeting_id:
            return d
    return None


def handle_meeting_list(args: Dict[str, Any], **_kw) -> str:
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    ms = _meetings()[:max(1, limit)]
    return _json({"success": True, "count": len(ms), "meetings": [_describe(d) for d in ms]})


def handle_meeting_transcript(args: Dict[str, Any], **_kw) -> str:
    mid = (args.get("meeting_id") or "").strip()
    if mid:
        d = _find(mid)
        if d is None:
            return _err(f"no meeting with id '{mid}' — call meeting_list first")
    else:
        ms = _meetings()
        if not ms:
            return _err("no meetings recorded yet")
        d = ms[0]

    lines = _lines(d)
    try:
        last = int(args.get("last")) if args.get("last") is not None else None
    except (TypeError, ValueError):
        last = None
    shown = lines[-last:] if last and last > 0 else lines

    return _json({
        "success": True,
        **_describe(d),
        "truncated": len(shown) != len(lines),
        "transcript": "\n".join(shown),
    })


def handle_meeting_search(args: Dict[str, Any], **_kw) -> str:
    q = (args.get("query") or "").strip()
    if not q:
        return _err("query is required")
    try:
        limit = int(args.get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30

    hits: List[Dict[str, Any]] = []
    needle = q.lower()
    for d in _meetings():
        for ln in _lines(d):
            if needle in ln.lower():
                hits.append({"meeting_id": d.name, "line": ln})
                if len(hits) >= max(1, limit):
                    return _json({"success": True, "query": q, "count": len(hits),
                                  "truncated": True, "hits": hits})
    return _json({"success": True, "query": q, "count": len(hits),
                  "truncated": False, "hits": hits})


def check_meeting_requirements() -> bool:
    """Tools stay available even with no meetings yet — they self-report empty."""
    return True
