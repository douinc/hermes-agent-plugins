#!/usr/bin/env python3
"""Replay raw_captions.jsonl through the caption dedup logic.

The meet bot writes every scraped caption batch (pre-dedup) to
``raw_captions.jsonl`` — the ground-truth stream of what the DOM observer saw.
This tool feeds that stream back through ``_BotState.record_caption`` to
reconstruct the transcript deterministically. Use it to:

  * confirm WHY a transcript exploded / got mis-timestamped (inspect the raw),
  * rebuild a correct transcript.txt even if the live dedup misbehaved,
  * regression-test a dedup change: same raw in, compare blow-up before/after.

Usage:
    python3 replay_raw_captions.py <raw_captions.jsonl> [out_transcript.txt]

It loads meet_bot.py by path so it runs without the plugin package's heavier
imports (hermes_constants / playwright).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path


def _load_botstate():
    path = Path(__file__).with_name("meet_bot.py")
    spec = importlib.util.spec_from_file_location("meet_bot_replay", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._BotState


def replay(raw_path: Path, out_path: Path | None = None) -> dict:
    BotState = _load_botstate()
    out_dir = raw_path.parent
    state = BotState(out_dir, meeting_id="replay", url="replay")
    # Don't let the replay re-append to the real raw log.
    state._raw_enabled = False

    n_raw = 0
    with raw_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_raw += 1
            state.record_caption(
                speaker=str(e.get("speaker", "")),
                text=str(e.get("text", "")),
                block_id=e.get("blockId"),
            )

    # Finalize anything still live so the transcript is complete. (Block-id mode
    # keeps everything in ``_blocks`` already; only the legacy slot path needs
    # an explicit flush of in-progress utterances.)
    for spk in list(state._current.keys()):
        state._finalize(spk)
    state._rewrite_transcript()

    body = state.transcript_path.read_text(encoding="utf-8")
    lines = [l for l in body.splitlines() if l]
    uniq = {l.split("] ", 1)[1] for l in lines if "] " in l}

    if out_path is not None:
        out_path.write_text(body, encoding="utf-8")

    stats = {
        "raw_entries": n_raw,
        "transcript_lines": len(lines),
        "unique_utterances": len(uniq),
        "dup_factor": round(len(lines) / max(1, len(uniq)), 2),
        "transcript_path": str(out_path or state.transcript_path),
    }
    return stats, lines


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    raw_path = Path(argv[0])
    if not raw_path.is_file():
        print(f"not found: {raw_path}", file=sys.stderr)
        return 1
    out_path = Path(argv[1]) if len(argv) > 1 else None
    stats, lines = replay(raw_path, out_path)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    c = Counter(l.split("] ", 1)[1] for l in lines if "] " in l)
    worst = [(t, n) for t, n in c.most_common(5) if n > 1]
    if worst:
        print("\nmost-duplicated (should be empty after a good dedup):")
        for t, n in worst:
            print(f"  {n:5d}x  {t[:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
