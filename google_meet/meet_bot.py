"""Headless Google Meet bot — Playwright + live-caption scraping.

Runs as a standalone subprocess spawned by ``process_manager.py``. Reads config
from env vars, writes status + transcript to files under
``$HERMES_HOME/workspace/meetings/<meeting-id>/``. The main hermes process
reads those files via the ``meet_*`` tools — no IPC beyond filesystem.

The scraping strategy mirrors OpenUtter (sumansid/openutter): we don't parse
WebRTC audio, we enable Google Meet's built-in live captions and observe the
captions container in the DOM via a MutationObserver. This is lossy and
English-biased but it is:

* deterministic (no API keys, no STT billing),
* works behind Meet's normal login / admission,
* survives Meet UI rewrites fairly well because the caption container has a
  stable ARIA role.

Run standalone for debugging::

    HERMES_MEET_URL=https://meet.google.com/abc-defg-hij \\
    HERMES_MEET_OUT_DIR=/tmp/meet-debug \\
    HERMES_MEET_HEADED=1 \\
    python -m google_meet.meet_bot

No meet.google.com URL → exits non-zero. Any URL that doesn't start with
``https://meet.google.com/`` is rejected (explicit-by-design).
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# Match ``https://meet.google.com/abc-defg-hij`` or ``.../lookup/...`` — the
# short three-segment code or a lookup URL. Anything else is rejected.
MEET_URL_RE = re.compile(
    r"^https://meet\.google\.com/("
    r"[a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,}"
    r"|lookup/[^/?#]+"
    r"|new"
    r")(?:[/?#].*)?$"
)


# Filenames the bot reads/writes in ``HERMES_MEET_OUT_DIR``.
SAY_QUEUE_FILENAME = "say_queue.jsonl"
SAY_PCM_FILENAME = "speaker.pcm"


def _is_safe_meet_url(url: str) -> bool:
    """Return True if *url* is a Google Meet URL we're willing to navigate to."""
    if not isinstance(url, str):
        return False
    return bool(MEET_URL_RE.match(url.strip()))


def _meeting_id_from_url(url: str) -> str:
    """Extract the 3-segment meeting code from a Meet URL.

    For ``https://meet.google.com/abc-defg-hij`` → ``abc-defg-hij``.
    For ``.../lookup/<id>`` or ``/new`` we fall back to a timestamped id — the
    bot won't know the real code until after redirect, and callers pass this
    through to filename anyway.
    """
    m = re.search(
        r"meet\.google\.com/([a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,})",
        url or "",
    )
    if m:
        return m.group(1)
    return f"meet-{int(time.time())}"


# ---------------------------------------------------------------------------
# Status + transcript file writers
# ---------------------------------------------------------------------------

class _BotState:
    """Single-process mutable state, flushed to ``status.json`` on each change."""

    def __init__(self, out_dir: Path, meeting_id: str, url: str):
        self.out_dir = out_dir
        self.meeting_id = meeting_id
        self.url = url
        self.in_call = False
        self.captioning = False
        self.captions_enabled_attempted = False
        self.caption_language: Optional[str] = None
        self.lobby_waiting = False
        self.join_attempted_at: Optional[float] = None
        self.joined_at: Optional[float] = None
        self.last_caption_at: Optional[float] = None
        self.transcript_lines = 0
        self.error: Optional[str] = None
        self.exited = False
        # v2 realtime fields.
        self.realtime = False
        self.realtime_ready = False
        self.realtime_device: Optional[str] = None
        self.audio_bytes_out: int = 0
        self.last_audio_out_at: Optional[float] = None
        self.last_barge_in_at: Optional[float] = None
        self.leave_reason: Optional[str] = None
        # Rolling-caption state. Meet grows a caption in place word-by-word,
        # so we keep a per-speaker in-progress utterance and only finalize it
        # when a non-continuation arrives. ``_final_lines`` holds completed
        # lines; ``_current`` maps speaker -> {"ts", "text"} for the live one.
        self._final_lines: list = []
        self._current: dict = {}
        # Per-speaker set of EVERY finalized (normalized) utterance. Meet keeps
        # a growing backlog of past caption rows in the DOM and the observer
        # re-scans + re-emits ALL of them on every mutation, so an utterance
        # finalized minutes ago reappears every tick. We remember all finalized
        # texts (not just the last few) and drop exact re-emits in O(1). The old
        # code kept only the last 8 per speaker, so anything deeper in the
        # backlog was mistaken for new speech and re-finalized with a fresh
        # timestamp — which exploded the transcript to 70k+ duplicate, badly
        # mis-timestamped lines (a 2k-utterance meeting became 150k lines).
        self._final_seen: dict = {}
        # Block-id tracking (the live observer now tags each caption row with a
        # stable id — see _CAPTION_OBSERVER_JS). ``_blocks`` maps an internal
        # monotonic sequence -> {speaker, text, ts} for every utterance seen
        # this session, rendered in first-seen order; ``_active`` maps the DOM
        # ``blockId`` -> that sequence for rows still growing. Each row becomes
        # exactly one transcript line, so concurrent same-speaker rows no longer
        # collide in a single slot. The legacy single-slot path (``_current`` /
        # ``_final_seen`` above) is kept for replaying historical raw logs that
        # predate block ids (entries with no blockId).
        self._blocks: dict = {}
        self._active: dict = {}
        self._block_seq: int = 0
        out_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = out_dir / "transcript.txt"
        self.status_path = out_dir / "status.json"
        # Ground-truth raw capture: every scraped caption batch BEFORE dedup,
        # so the merge logic can be debugged/replayed against exactly what the
        # DOM observer saw. On by default; set HERMES_MEET_RAW_CAPTIONS=0 to skip.
        self.raw_path = out_dir / "raw_captions.jsonl"
        self._raw_enabled = os.environ.get("HERMES_MEET_RAW_CAPTIONS", "1") not in ("0", "false", "False", "")
        # Resume: if a transcript already exists (the bot is rejoining a meeting
        # that process_manager chose to preserve), load it so new captions APPEND
        # instead of overwriting from empty. process_manager archives stale
        # transcripts, so whatever is here belongs to the session we're continuing.
        self._resume_from_existing()
        self._flush()

    # -------- transcript ------------------------------------------------

    def _resume_from_existing(self) -> None:
        """Load an existing transcript.txt so a rejoining bot continues the SAME
        file (appends) instead of overwriting from empty.

        We deliberately do NOT seed the per-speaker echo set (``_final_seen``)
        from history. The dedup only needs to suppress re-emission of the CURRENT
        browser session's on-screen caption backlog, and a fresh (re)join loads
        an empty captions panel — Meet doesn't replay earlier captions to a
        newly-joined participant. Seeding from history would instead wrongly drop
        genuine repeats in a later meeting that reuses the same Meet code (e.g. a
        recurring standup), so the echo set stays scoped to this process."""
        if not self.transcript_path.is_file():
            return
        try:
            existing = self.transcript_path.read_text(encoding="utf-8")
        except OSError:
            return
        for ln in existing.splitlines():
            if ln.strip():
                self._final_lines.append(ln)
        self.transcript_lines = len(self._final_lines)

    def record_raw_batch(self, entries: list) -> None:
        """Append a raw scraped caption batch (pre-dedup) to raw_captions.jsonl.

        Ground truth for debugging the merge logic: the DOM observer re-emits
        every visible caption block on each mutation, so the same utterance
        shows up many times here with browser-side timestamps (``ts`` = the JS
        ``Date.now()`` in ms). ``record_caption`` is what collapses these into
        the transcript; if that misbehaves (duplication / wrong timestamps),
        this file lets us see what the bot actually saw and replay it.
        """
        if not self._raw_enabled or not entries:
            return
        now = time.time()
        lines = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            lines.append(json.dumps(
                {
                    "drain_t": now,
                    "js_ts": e.get("ts"),
                    "speaker": e.get("speaker", ""),
                    "text": e.get("text", ""),
                    "blockId": e.get("blockId"),
                },
                ensure_ascii=False,
            ))
        if lines:
            with self.raw_path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

    def _fmt_line(self, ts: float, speaker: str, text: str) -> str:
        return f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] {speaker}: {text}"

    @staticmethod
    def _norm(s: str) -> str:
        """Whitespace+punctuation-stripped form for comparing caption growth."""
        return re.sub(r"[\s.,!?…·~\-]+", "", s or "")

    def _finalize(self, speaker: str) -> None:
        """Move a speaker's in-progress utterance into the finalized list."""
        cur = self._current.pop(speaker, None)
        if cur and cur["text"]:
            self._final_lines.append(self._fmt_line(cur["ts"], speaker, cur["text"]))
            self._final_seen.setdefault(speaker, set()).add(self._norm(cur["text"]))

    def _rewrite_transcript(self) -> None:
        """Rewrite transcript.txt = resumed/prior lines + this session's content.

        This session's content is one line per tracked block (block-id mode,
        rendered in first-seen order) plus any legacy single-slot in-progress
        lines (``_current``, only populated when replaying pre-block-id logs).
        In a live meeting only the block lines are present.
        """
        block_lines = [self._fmt_line(b["ts"], b["speaker"], b["text"])
                       for _seq, b in sorted(self._blocks.items())]
        live = [self._fmt_line(c["ts"], spk, c["text"]) for spk, c in self._current.items()]
        body = "\n".join(self._final_lines + block_lines + live)
        self.transcript_lines = len(self._final_lines) + len(block_lines) + len(live)
        tmp = self.transcript_path.with_suffix(".txt.tmp")
        tmp.write_text(body + ("\n" if body else ""), encoding="utf-8")
        tmp.replace(self.transcript_path)

    @staticmethod
    def _continues(n_new: str, n_cur: str) -> bool:
        """True if normalized ``n_new`` is the same utterance as ``n_cur`` still
        growing/refining (one a prefix of the other, or a long shared prefix)."""
        if not n_new or not n_cur:
            return False
        common = len(os.path.commonprefix([n_new, n_cur]))
        return (
            n_new.startswith(n_cur)
            or n_cur.startswith(n_new)
            or common >= max(10, int(0.6 * min(len(n_new), len(n_cur))))
        )

    def _record_by_block(self, speaker: str, text: str, block_id: str) -> None:
        """Record a caption keyed by its stable DOM block id.

        Each Meet caption row maps to one ``block_id`` that survives the row's
        word-by-word growth, so we keep one utterance per id and just grow it.
        Concurrent rows from the same speaker get distinct ids and never evict
        each other. A genuine later repeat ("네네" said again) is a NEW row =
        new id = new line, and an unchanged backlog re-emit is the SAME id =
        no-op — so no echo-guard is needed. If Meet recycles a node for a brand
        new utterance, the new text won't continue the old one: we detach the id
        and start a fresh block (the old one stays frozen in first-seen order).
        """
        speaker = (speaker or "").strip() or "Unknown"
        text = (text or "").strip()
        if not text:
            return
        self.last_caption_at = time.time()
        seq = self._active.get(block_id)
        if seq is not None:
            blk = self._blocks.get(seq)
            if blk is not None:
                if text == blk["text"]:
                    return
                if self._continues(self._norm(text), self._norm(blk["text"])):
                    # same row still growing → keep the richer text
                    if len(text) > len(blk["text"]):
                        blk["text"] = text
                        if speaker != "Unknown":
                            blk["speaker"] = speaker
                        self._rewrite_transcript()
                    return
                # node recycled for a new utterance → detach; old block frozen
                del self._active[block_id]
        # new utterance (fresh id, or a recycled id starting a new turn)
        self._block_seq += 1
        self._blocks[self._block_seq] = {"speaker": speaker, "text": text, "ts": time.time()}
        self._active[block_id] = self._block_seq
        self._rewrite_transcript()
        self._flush()

    def record_caption(self, speaker: str, text: str, block_id: Optional[str] = None) -> None:
        """Record a caption. Live captures carry a stable ``block_id`` (per-row
        tracking); historical raw logs without one fall back to the legacy
        single-slot growth merge below."""
        if block_id:
            self._record_by_block(speaker, text, block_id)
        else:
            self._record_legacy(speaker, text)

    def _record_legacy(self, speaker: str, text: str) -> None:
        """Record a caption, merging Meet's word-by-word in-place growth.

        Meet updates the same caption block as a speaker talks, so each
        mutation is the full utterance-so-far. We treat a new text that
        continues the current one (prefix either way) as the SAME utterance
        and keep the longer version, instead of appending a new line per word.
        """
        speaker = (speaker or "").strip() or "Unknown"
        text = (text or "").strip()
        if not text:
            return
        self.last_caption_at = time.time()
        n_new = self._norm(text)
        cur = self._current.get(speaker)
        if cur is not None:
            cur_text = cur["text"]
            if text == cur_text:
                return
            # Meet revises punctuation/spacing as the utterance grows
            # ("새롭게." → "새롭게 트"), so compare on a normalized form.
            n_cur = self._norm(cur_text)
            common = len(os.path.commonprefix([n_new, n_cur]))
            same = (
                n_new.startswith(n_cur)
                or n_cur.startswith(n_new)
                or common >= max(10, int(0.6 * min(len(n_new), len(n_cur))))
            )
            if same:
                # same utterance still growing/refining → keep the longer text
                if len(text) > len(cur_text):
                    cur["text"] = text
                    self._rewrite_transcript()
                return
        # Not a continuation of the current utterance. Before treating it as a
        # new one, check whether it's just an ALREADY-FINALIZED caption block
        # that Meet still keeps in the DOM and re-emits every tick. Meet retains
        # a long backlog of past rows, so this must check ALL prior finalized
        # utterances for this speaker — not just the last few. Without it, an
        # old block and the live one keep finalizing each other and the
        # transcript explodes (150k duplicate, mis-timestamped lines).
        if n_new in self._final_seen.get(speaker, ()):
            return
        if cur is not None:
            self._finalize(speaker)
        self._current[speaker] = {"ts": time.time(), "text": text}
        self._rewrite_transcript()
        self._flush()

    # -------- status file ----------------------------------------------

    def _flush(self) -> None:
        data = {
            "meetingId": self.meeting_id,
            "url": self.url,
            "inCall": self.in_call,
            "captioning": self.captioning,
            "captionsEnabledAttempted": self.captions_enabled_attempted,
            "captionLanguage": self.caption_language,
            "lobbyWaiting": self.lobby_waiting,
            "joinAttemptedAt": self.join_attempted_at,
            "joinedAt": self.joined_at,
            "lastCaptionAt": self.last_caption_at,
            "transcriptLines": self.transcript_lines,
            "transcriptPath": str(self.transcript_path),
            "error": self.error,
            "exited": self.exited,
            "pid": os.getpid(),
            # v2 realtime telemetry.
            "realtime": self.realtime,
            "realtimeReady": self.realtime_ready,
            "realtimeDevice": self.realtime_device,
            "audioBytesOut": self.audio_bytes_out,
            "lastAudioOutAt": self.last_audio_out_at,
            "lastBargeInAt": self.last_barge_in_at,
            "leaveReason": self.leave_reason,
        }
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.status_path)

    def set(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)
        self._flush()


# ---------------------------------------------------------------------------
# Playwright bot entry point
# ---------------------------------------------------------------------------

# JavaScript injected into the Meet tab to observe captions. Captures
# {speaker, text} tuples via a MutationObserver on the caption container,
# and exposes ``window.__hermesMeetDrain()`` to pull new entries. This
# mirrors the OpenUtter caption scraping approach.
_CAPTION_OBSERVER_JS = r"""
(() => {
  if (window.__hermesMeetInstalled) return;
  window.__hermesMeetInstalled = true;
  window.__hermesMeetQueue = [];

  const captionSelector = '[role="region"][aria-label*="aption" i], ' +
                          '[role="region"][aria-label*="자막"], ' +  // Korean UI
                          'div.vNKgIf, ' +           // current region class (2026)
                          'div[jsname="YSxPC"], ' +  // legacy
                          'div[jsname="tgaKEf"]';    // current (Apr 2026)

  // Each visible caption row (one speaker turn) is a stable DOM node that Meet
  // grows in place word-by-word. We tag every such node with a durable id so
  // the Python side can track each row INDEPENDENTLY instead of collapsing all
  // of one speaker's concurrent rows into a single slot. Without this, when a
  // speaker has two rows on screen at once (e.g. a lingering "네네." backchannel
  // and a growing sentence), the two rows fight over one slot and the
  // transcript both shatters the sentence into per-word lines and re-finalizes
  // the backchannel thousands of times. The id is keyed on the node via a
  // WeakMap and prefixed with the install time so ids stay unique even if the
  // observer is reinstalled after a page reload (counter restarts at 0).
  const __install = Date.now();
  let __blkCounter = 0;
  const __blkIds = new WeakMap();   // node -> stable id
  const __blkLast = new WeakMap();  // node -> last pushed text (skip re-emits)

  function blockId(node) {
    let id = __blkIds.get(node);
    if (id === undefined) { id = __install + '-' + (++__blkCounter); __blkIds.set(node, id); }
    return id;
  }

  function pushEntry(node, speaker, text) {
    if (!text || !text.trim()) return;
    text = text.trim();
    // Skip unchanged re-emits of the same row. Meet keeps a growing backlog of
    // past rows in the DOM and re-scans ALL of them on every mutation; pushing
    // only when a row's text actually changes keeps the queue (and the raw
    // capture log) small without losing any state transition.
    if (node) {
      if (__blkLast.get(node) === text) return;
      __blkLast.set(node, text);
    }
    window.__hermesMeetQueue.push({
      ts: Date.now(),
      speaker: (speaker || '').trim(),
      text: text,
      blockId: node ? blockId(node) : null,
    });
  }

  function scan(root) {
    // Each speaker turn is a block (div.nMcdL) holding a name header
    // (span.NWpY1d, current 2026) and a text node (div.ygicle). NOTE: the
    // jsname="dsyhDe" element is the OUTER widget that *wraps* the caption
    // region, NOT a per-speaker row — looking for it inside the region finds
    // nothing, which is why speakers used to come back empty ("Unknown").
    // Selectors are layered for resilience across Meet rewrites; if no text
    // node matches we strip the speaker name off the block's full text.
    let blocks = root.querySelectorAll('div.nMcdL');
    if (!blocks.length) {
      blocks = root.querySelectorAll('div[jsname="dsyhDe"], div.CNusmb, div.TBMuR');
    }
    if (blocks.length) {
      blocks.forEach((block) => {
        const spkEl = block.querySelector('span.NWpY1d, div.KcIKyf, div.zs7s8d');
        const txtEl = block.querySelector('div.ygicle, div.bh44bd, div.iTTPOb');
        const speaker = spkEl ? spkEl.innerText.trim() : '';
        let text = txtEl ? txtEl.innerText : '';
        if (!text) {
          text = (block.innerText || '').trim();
          if (speaker && text.indexOf(speaker) === 0) {
            text = text.slice(speaker.length).trim();
          }
        }
        pushEntry(block, speaker, text);
      });
      return;
    }
    // Ultimate fallback: last non-empty line, no speaker (no stable node).
    const text = (root.innerText || '').split('\n').filter(Boolean).pop();
    pushEntry(null, '', text);
  }

  function attach() {
    const el = document.querySelector(captionSelector);
    if (!el) return false;
    const obs = new MutationObserver(() => scan(el));
    obs.observe(el, { childList: true, subtree: true, characterData: true });
    scan(el);
    return true;
  }

  // Try now and retry on interval — the caption region only appears after
  // captions are enabled and someone speaks.
  if (!attach()) {
    const iv = setInterval(() => { if (attach()) clearInterval(iv); }, 1500);
  }

  window.__hermesMeetDrain = () => {
    const out = window.__hermesMeetQueue.slice();
    window.__hermesMeetQueue = [];
    return out;
  };
})();
"""


def _enable_captions_js() -> str:
    """Return a small JS snippet that tries to click the 'Turn on captions' button.

    Best-effort — Meet's caption toggle is keyboard-accessible via ``c``. We
    dispatch that keystroke as a cheap fallback. Real click targeting is too
    brittle to rely on.
    """
    return r"""
    (() => {
      const ev = new KeyboardEvent('keydown', {
        key: 'c', code: 'KeyC', keyCode: 67, which: 67, bubbles: true,
      });
      document.body.dispatchEvent(ev);
      return true;
    })();
    """


# JS that clicks the language ``option`` whose label/text matches the target.
# Meet's language list is a *virtualized* listbox of ~100 options that don't
# reliably surface in Playwright's accessibility tree (get_by_role("option")
# returns 0), so we match on the live DOM and fire a real pointer sequence.
_PICK_CAPTION_LANG_JS = r"""
(target) => {
  const opts = [...document.querySelectorAll('[role=option]')];
  const el = opts.find(o => ((o.getAttribute('aria-label') || '').trim() === target)
                         || ((o.innerText || '').trim() === target));
  if (!el) return { found: false, count: opts.length };
  el.scrollIntoView({ block: 'center' });
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
  }
  return { found: true, label: (el.getAttribute('aria-label') || el.innerText || '').trim() };
}
"""


def _select_caption_language(page, state) -> bool:
    """Best-effort: force Meet's live-caption "meeting language".

    Meet remembers the caption language *per profile*, independent of
    ``navigator.language``. A profile once set to English keeps transcribing
    Korean speech as garbled English even though we launch with ``--lang=ko-KR``.
    So rather than trust the persisted preference, we set it explicitly each
    session.

    Once captions are on, Meet renders the live-caption controls — including a
    ``회의 언어`` / "Language of the meeting" combobox — directly in the DOM (no
    menu navigation needed). We open that combobox and pick the target language
    from its (virtualized) option list via JS.

    Set ``HERMES_MEET_CAPTION_LANG`` to the language you want to force, using
    Meet's own option label in the account's UI language (e.g. ``한국어``,
    ``English``, ``日本語``, ``Español``). When UNSET (the default) the bot does
    NOT touch the language — Meet keeps whatever the profile/navigator already
    uses. Set it explicitly if you hit the per-profile "garbled wrong-language
    captions" issue described above.

    Fully best-effort: any failure is swallowed and the existing setting stays.
    """
    lang_label = os.environ.get("HERMES_MEET_CAPTION_LANG", "").strip()
    if not lang_label:
        return False

    # Combobox accessible-name candidates (ko-KR UI first, English fallback).
    combo_names = ("회의 언어", "Language of the meeting", "Meeting language")
    # Post-join info dialogs ("your video may look different…") overlay the page
    # and intercept clicks — clear them before touching the caption controls.
    dismiss_labels = ("확인", "Got it", "OK", "닫기", "Close")

    try:
        # 1. Dismiss any blocking post-join modal.
        for _ in range(3):
            cleared = False
            for nm in dismiss_labels:
                try:
                    b = page.get_by_role("button", name=nm, exact=True).first
                    if b.count() and b.is_visible():
                        b.click(timeout=2_000)
                        cleared = True
                        break
                except Exception:
                    continue
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            if not cleared:
                break
            page.wait_for_timeout(400)

        # 1b. Ensure captions are actually on — the modal may have swallowed the
        #     earlier enable click, and the language combobox only renders while
        #     captions are active. Idempotent: once on, the toggle reads
        #     "자막 사용 중지" and won't match these labels.
        for nm in ("자막 사용", "Turn on captions", "Captions"):
            try:
                b = page.get_by_role("button", name=nm, exact=False).first
                if b.count() and b.is_visible():
                    b.click(timeout=3_000)
                    state.set(captions_enabled_attempted=True)
                    page.wait_for_timeout(800)
                    break
            except Exception:
                continue

        # 2. Locate the meeting-language combobox (present once captions are on).
        combo = None
        for nm in combo_names:
            c = page.get_by_role("combobox", name=nm, exact=False).first
            if c.count():
                combo = c
                break
        if combo is None:
            print("[caption-lang] combobox not found yet", flush=True)
            return False
        # Already on the target language? Nothing to do.
        try:
            if lang_label in (combo.inner_text() or ""):
                state.set(caption_language=lang_label)
                return True
        except Exception:
            pass

        # 3. Open the dropdown (force-click bypasses the ripple-overlay that
        #    otherwise intercepts the pointer) and pick the language via JS.
        try:
            combo.scroll_into_view_if_needed(timeout=3_000)
        except Exception:
            pass
        try:
            combo.click(force=True, timeout=4_000)
        except Exception:
            pass
        page.wait_for_timeout(900)
        res = page.evaluate(_PICK_CAPTION_LANG_JS, lang_label)
        picked = bool(res and res.get("found"))
        print(f"[caption-lang] open+pick {lang_label!r}: {res}", flush=True)
        page.wait_for_timeout(800)

        # 4. Verify the combobox now reflects the target; close the panel.
        if picked:
            try:
                if lang_label not in (combo.inner_text() or ""):
                    picked = False
            except Exception:
                pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        if picked:
            state.set(caption_language=lang_label)
        return picked
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _start_realtime_speaker(
    *,
    rt: dict,
    out_dir: Path,
    bridge_info: dict,
    api_key: str,
    model: str,
    voice: str,
    instructions: str,
    stop_flag: dict,
    state: "_BotState",
) -> None:
    """Wire up the OpenAI Realtime session + speaker thread + PCM pump.

    The speaker thread reads text lines from ``say_queue.jsonl``, sends each
    to OpenAI Realtime, and writes PCM audio into ``speaker.pcm``. A
    separate *pump* thread forwards that PCM into the OS audio sink so
    Chrome's fake mic picks it up. On Linux we pipe to ``paplay`` against
    the null-sink; on macOS the caller is expected to have the BlackHole
    device selected as default input.
    """
    try:
        from .realtime.openai_client import (
            RealtimeSession,
            RealtimeSpeaker,
        )
    except Exception as e:
        state.set(error=f"realtime import failed: {e}")
        return

    pcm_path = out_dir / SAY_PCM_FILENAME
    queue_path = out_dir / SAY_QUEUE_FILENAME
    processed_path = out_dir / "say_processed.jsonl"
    # Reset the sink file so we start clean each session.
    pcm_path.write_bytes(b"")
    # Make sure the queue exists so the speaker poller doesn't error on
    # first iteration.
    queue_path.touch()

    try:
        session = RealtimeSession(
            api_key=api_key,
            model=model,
            voice=voice,
            instructions=instructions,
            audio_sink_path=pcm_path,
            sample_rate=24000,
        )
        session.connect()
    except Exception as e:
        state.set(error=f"realtime connect failed: {e}")
        return

    rt["session"] = session

    def _stop_fn():
        return stop_flag.get("stop", False)

    rt["speaker_stop"] = lambda: stop_flag.__setitem__("stop", stop_flag.get("stop", False))

    speaker = RealtimeSpeaker(
        session=session,
        queue_path=queue_path,
        processed_path=processed_path,
    )

    def _speaker_loop():
        try:
            speaker.run_until_stopped(_stop_fn)
        except Exception as e:
            state.set(error=f"realtime speaker crashed: {e}")

    t_speaker = threading.Thread(target=_speaker_loop, name="meet-speaker", daemon=True)
    t_speaker.start()
    rt["speaker_thread"] = t_speaker

    # PCM pump: feeds speaker.pcm (24kHz s16le mono) into the OS audio
    # device that Chrome's fake mic reads from. Different tools per
    # platform, but the contract is the same — block-read the growing
    # PCM file and stream it to the device in near-real-time.
    platform_tag = (bridge_info or {}).get("platform")
    if platform_tag == "linux":
        import subprocess as _sp

        sink = (bridge_info or {}).get("write_target") or "hermes_meet_sink"
        try:
            proc = _sp.Popen(
                [
                    "paplay",
                    "--raw",
                    "--rate=24000",
                    "--format=s16le",
                    "--channels=1",
                    f"--device={sink}",
                    str(pcm_path),
                ],
                stdin=_sp.DEVNULL,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            rt["pcm_pump"] = proc
        except FileNotFoundError:
            state.set(error="paplay not found — install pulseaudio-utils for realtime on Linux")
    elif platform_tag == "darwin":
        # macOS: use ffmpeg to tail-read speaker.pcm and write it to the
        # BlackHole output device. The user must have BlackHole selected
        # as the default input in System Settings → Sound for Chrome to
        # pick it up. We prefer ffmpeg because it's scriptable and can
        # target AVFoundation devices by name; fall back to afplay-ing
        # the file in a tight loop if ffmpeg is absent.
        import shutil as _shutil
        import subprocess as _sp

        device_name = (bridge_info or {}).get("write_target") or "BlackHole 2ch"
        if _shutil.which("ffmpeg"):
            try:
                # -re: read input at native frame rate.
                # -f avfoundation -i: speaker path as raw PCM.
                # -f s16le -ar 24000 -ac 1 -i <pcm>: interpret the file.
                # -f audiotoolbox -audio_device_index: write to BlackHole.
                # Simpler: output as raw via coreaudio using "-f audiotoolbox".
                # ffmpeg's audiotoolbox output picks the current default
                # output device, which isn't what we want. Instead we use
                # -f avfoundation with the named device as OUTPUT via
                # -vn and the device name.
                proc = _sp.Popen(
                    [
                        "ffmpeg",
                        "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-re",
                        "-f", "s16le", "-ar", "24000", "-ac", "1",
                        "-i", str(pcm_path),
                        "-f", "audiotoolbox",
                        "-audio_device_index", _mac_audio_device_index(device_name),
                        "-",
                    ],
                    stdin=_sp.DEVNULL,
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
                rt["pcm_pump"] = proc
            except FileNotFoundError:
                state.set(error="ffmpeg not found — install via `brew install ffmpeg` for realtime on macOS")
            except Exception as e:
                state.set(error=f"macOS pcm pump failed to start: {e}")
        else:
            state.set(error="ffmpeg not found — install via `brew install ffmpeg` for realtime on macOS")


def _mac_audio_device_index(device_name: str) -> str:
    """Return the ffmpeg ``-audio_device_index`` for *device_name*, as a string.

    Probes ``ffmpeg -f avfoundation -list_devices true -i ''`` (which prints
    the device table on stderr) and matches *device_name* case-insensitively.
    Defaults to ``"0"`` if the device can't be found — caller will get a
    misrouted stream but not a crash, and the error will be obvious.
    """
    import subprocess as _sp

    try:
        out = _sp.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return "0"
    # ffmpeg prints the table on stderr. Lines look like:
    #   [AVFoundation indev @ 0x...] [0] BlackHole 2ch
    import re as _re

    needle = device_name.strip().lower()
    for line in (out.stderr or "").splitlines():
        m = _re.search(r"\[(\d+)\]\s+(.+)$", line)
        if not m:
            continue
        if m.group(2).strip().lower() == needle:
            return m.group(1)
    return "0"


def _meet_home() -> Path:
    """The ``workspace/meetings`` directory under HERMES_HOME."""
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "workspace" / "meetings"


def _profile_dir() -> Path:
    """Persistent Chromium profile dir for the Meet bot.

    Using a real on-disk profile (rather than re-injecting a storage_state
    snapshot into a throwaway context each run) is what keeps the Google
    session alive across runs: Chromium refreshes the rotating
    ``__Secure-*PSIDTS`` cookies in place, and Google trusts a returning
    profile far more than a fresh automated context, so the bot stays signed
    in instead of dropping to the guest 'Ask to join' screen.
    """
    override = os.environ.get("HERMES_MEET_PROFILE_DIR", "").strip()
    p = Path(override) if override else (_meet_home() / "chrome-profile")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _open_persistent_context(pw, *, headless, args, context_args, seed_auth=""):
    """Launch a persistent-profile browser context, with lock retry + seeding.

    Retries briefly because a just-replaced bot may still hold Chromium's
    profile ``SingletonLock`` for a moment. On a first run (empty profile)
    seeds cookies from a legacy ``auth.json`` storage_state so an existing
    sign-in carries over without an immediate re-auth.
    """
    profile = _profile_dir()
    try:
        first_run = not any(profile.iterdir())
    except OSError:
        first_run = True

    last_err = None
    ctx = None
    for _ in range(6):
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(profile), headless=headless, args=args, **context_args
            )
            break
        except Exception as e:  # profile locked by an exiting bot, etc.
            last_err = e
            time.sleep(1.0)
    if ctx is None:
        raise last_err or RuntimeError("failed to open persistent context")

    if first_run and seed_auth and Path(seed_auth).is_file():
        try:
            data = json.loads(Path(seed_auth).read_text(encoding="utf-8"))
            if data.get("cookies"):
                ctx.add_cookies(data["cookies"])
        except Exception:
            pass
    return ctx


def _export_storage_state(context, path="") -> None:
    """Mirror the live session out to ``auth.json`` (best-effort).

    Keeps the portable storage_state fresh so the short-lived ``meet_create``
    subprocess (which uses an ephemeral context to avoid the profile lock)
    always has current cookies.
    """
    target = path.strip() if path else str(_meet_home() / "auth.json")
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=target)
    except Exception:
        pass


def run_bot() -> int:  # noqa: C901 — orchestration, explicit branches
    url = os.environ.get("HERMES_MEET_URL", "").strip()
    out_dir_env = os.environ.get("HERMES_MEET_OUT_DIR", "").strip()
    headed = os.environ.get("HERMES_MEET_HEADED", "").lower() in {"1", "true", "yes"}
    auth_state = os.environ.get("HERMES_MEET_AUTH_STATE", "").strip()
    guest_name = os.environ.get("HERMES_MEET_GUEST_NAME", "Hermes Agent")
    duration_s = _parse_duration(os.environ.get("HERMES_MEET_DURATION", ""))
    # v2: optional realtime mode. Enabled when HERMES_MEET_MODE=realtime.
    mode = os.environ.get("HERMES_MEET_MODE", "transcribe").strip().lower()
    realtime_model = os.environ.get("HERMES_MEET_REALTIME_MODEL", "gpt-realtime")
    realtime_voice = os.environ.get("HERMES_MEET_REALTIME_VOICE", "alloy")
    realtime_instructions = os.environ.get("HERMES_MEET_REALTIME_INSTRUCTIONS", "")
    realtime_api_key = os.environ.get("HERMES_MEET_REALTIME_KEY") or os.environ.get("OPENAI_API_KEY", "")

    if not url or not _is_safe_meet_url(url):
        sys.stderr.write(
            "google_meet bot: refusing to launch — HERMES_MEET_URL must be a "
            "meet.google.com URL. got: %r\n" % url
        )
        return 2
    if not out_dir_env:
        sys.stderr.write("google_meet bot: HERMES_MEET_OUT_DIR is required\n")
        return 2

    out_dir = Path(out_dir_env)
    meeting_id = _meeting_id_from_url(url)
    state = _BotState(out_dir=out_dir, meeting_id=meeting_id, url=url)

    # SIGTERM → exit cleanly so the parent ``meet_leave`` gets a finalized
    # transcript. We set a flag instead of raising so the Playwright context
    # teardown runs in the finally block below.
    stop_flag = {"stop": False}

    def _on_signal(_sig, _frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # v2 realtime: provision virtual audio device + start speaker thread.
    # We track these in a dict so the finally block can tear them down
    # regardless of how we exit. If anything in the realtime setup fails we
    # fall back to transcribe mode with a status flag.
    rt = {
        "enabled": mode == "realtime",
        "bridge": None,            # AudioBridge | None
        "bridge_info": None,       # dict | None
        "session": None,           # RealtimeSession | None
        "speaker_thread": None,    # threading.Thread | None
        "speaker_stop": None,      # callable | None
    }
    if rt["enabled"]:
        if not realtime_api_key:
            state.set(error="realtime mode requested but no API key in HERMES_MEET_REALTIME_KEY/OPENAI_API_KEY — falling back to transcribe")
            rt["enabled"] = False
        else:
            try:
                from .audio_bridge import AudioBridge
                bridge = AudioBridge()
                rt["bridge_info"] = bridge.setup()
                rt["bridge"] = bridge
                state.set(realtime=True, realtime_device=rt["bridge_info"].get("device_name"))
            except Exception as e:
                state.set(error=f"audio bridge setup failed: {e} — falling back to transcribe")
                rt["enabled"] = False

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        state.set(error=f"playwright not installed: {e}", exited=True)
        sys.stderr.write(
            "google_meet bot: playwright is not installed. Run "
            "`pip install playwright && python -m playwright install chromium`\n"
        )
        if rt["bridge"]:
            rt["bridge"].teardown()
        return 3

    # Chrome env: if realtime is live on Linux, point PULSE_SOURCE at the
    # virtual source so Chrome's fake mic reads the audio we generate.
    chrome_env = os.environ.copy()
    chrome_args = [
        "--use-fake-ui-for-media-stream",
        "--disable-blink-features=AutomationControlled",
    ]
    if not rt["enabled"]:
        # Transcribe-only: the bot never sends media — it only receives
        # captions. A fake device would broadcast a green test-pattern video
        # and a beep tone into the call, so we attach NO media device and
        # join fully muted (camera + mic off).
        pass
    elif rt["bridge_info"] and rt["bridge_info"].get("platform") == "linux":
        chrome_env["PULSE_SOURCE"] = rt["bridge_info"].get("device_name", "")

    try:
        with sync_playwright() as pw:
            # Playwright's launch() doesn't take env; we set PULSE_SOURCE
            # via the process env before launch so the child Chrome inherits it.
            for k, v in chrome_env.items():
                os.environ[k] = v
            context_args = {
                "viewport": {"width": 1280, "height": 800},
                "user_agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                # Drive navigator.language → Meet derives its default UI /
                # live-caption language from this. Set HERMES_MEET_LOCALE to your
                # locale (e.g. ko-KR, ja-JP, es-ES) so speech transcribes in the
                # right language; pair with HERMES_MEET_CAPTION_LANG to force it.
                "locale": os.environ.get("HERMES_MEET_LOCALE", "en-US"),
                "permissions": ["microphone", "camera"],
            }
            # Persistent on-disk profile (not a throwaway context + injected
            # storage_state): keeps the Google session signed in across runs
            # so the bot gets the org-account "Join now" path instead of the
            # guest "Ask to join" lobby. Seeds from auth.json on first run.
            context = _open_persistent_context(
                pw,
                headless=not headed,
                args=chrome_args,
                context_args=context_args,
                seed_auth=auth_state,
            )
            browser = context.browser  # None for persistent contexts
            page = context.pages[0] if context.pages else context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                state.set(error=f"navigate failed: {e}", exited=True)
                return 4

            # Guest-mode: Meet shows a name field before "Ask to join". When
            # we're authed, we instead see "Join now".
            _try_guest_name(page, guest_name)
            _click_join(page, state)

            # Install caption observer and attempt to enable captions.
            try:
                page.evaluate(_enable_captions_js())
                state.set(captions_enabled_attempted=True)
            except Exception:
                pass
            try:
                page.evaluate(_CAPTION_OBSERVER_JS)
            except Exception as e:
                state.set(error=f"caption observer install failed: {e}")

            # Note: in_call=False until admission is confirmed (we detect
            # either the Leave button or the caption region, signalling we
            # made it past the lobby).
            state.set(captioning=True, join_attempted_at=time.time())

            # v2 realtime: start the speaker thread reading from the
            # plugin-side say queue. The thread reads JSONL lines written by
            # meet_say, calls OpenAI Realtime, and streams the audio PCM to
            # the virtual sink that Chrome's fake-mic is pointed at.
            if rt["enabled"]:
                _start_realtime_speaker(
                    rt=rt,
                    out_dir=out_dir,
                    bridge_info=rt["bridge_info"],
                    api_key=realtime_api_key,
                    model=realtime_model,
                    voice=realtime_voice,
                    instructions=realtime_instructions,
                    stop_flag=stop_flag,
                    state=state,
                )
                if rt["session"] is not None:
                    state.set(realtime_ready=True)

            # Admission + drain loop. Runs until SIGTERM, duration expiry,
            # or the page detects "You were removed / you left the
            # meeting". Responsible for:
            #   * detecting admission (Leave button visible → in_call=True)
            #   * timing out stuck-in-lobby (default 5 minutes)
            #   * draining scraped captions into the transcript
            #   * triggering realtime barge-in when a human speaks while
            #     the bot is generating audio
            #   * periodically flushing realtime counters into status.json
            deadline = (time.time() + duration_s) if duration_s else None
            lobby_deadline = time.time() + float(
                os.environ.get("HERMES_MEET_LOBBY_TIMEOUT", "300")
            )
            last_admission_check = 0.0
            # Count consecutive "guest / not-signed-in" pre-join screens. A
            # valid org session never shows the guest name field, so a few
            # hits (~9s) is a definitive "session is invalid" signal — we fail
            # fast with an actionable error instead of silently sitting until
            # the misleading 300s lobby_timeout.
            guest_hits = 0
            # End-of-meeting detection. Primary signal: everyone else left,
            # so the participant count drops to 1 (just the bot). We only act
            # on it once we've actually seen others present, and after a short
            # grace period, to ignore transient blips. Fallback: no captions
            # for a long stretch (covers the case where the count class moved).
            last_presence_check = 0.0
            seen_others = False
            alone_since: Optional[float] = None
            alone_grace = float(os.environ.get("HERMES_MEET_ALONE_SECONDS", "25"))
            # Last reliable (non-None) participant count and when we read it.
            # Gates the silence fallback below: while we can still confirm that
            # others are present, the bot must NEVER leave on caption silence.
            last_good_count: Optional[int] = None
            last_good_count_at = 0.0
            present_staleness = float(
                os.environ.get("HERMES_MEET_PRESENCE_STALE_SECONDS", "90")
            )
            # Safety net — fires ONLY when participant detection is NOT currently
            # confirming people are present (e.g. Meet renamed the count class).
            # When the count reliably shows >=2 people, a quiet meeting is never
            # ended early. Generous (45 min) now that it is a true fallback.
            silence_timeout = float(os.environ.get("HERMES_MEET_SILENCE_SECONDS", "2700"))
            # Caption-language enforcement: the panel/combobox (and any blocking
            # post-join modal) can take a few seconds to render after admission,
            # so a single attempt is unreliable. Retry every few seconds until it
            # sticks (caption_language set) or we run out of attempts.
            caption_lang_tries = 0
            caption_lang_max_tries = int(os.environ.get("HERMES_MEET_CAPTION_LANG_TRIES", "6"))
            last_caption_lang_try = 0.0
            while not stop_flag["stop"]:
                now = time.time()
                if deadline and now > deadline:
                    state.set(leave_reason="duration_expired")
                    break

                # Re-assert the caption language until confirmed.
                if (
                    state.in_call
                    and not state.caption_language
                    and caption_lang_tries < caption_lang_max_tries
                    and (now - last_caption_lang_try) > 4.0
                ):
                    last_caption_lang_try = now
                    caption_lang_tries += 1
                    try:
                        _select_caption_language(page, state)
                    except Exception:
                        pass

                # Admission detection every ~3s until admitted.
                if not state.in_call and (now - last_admission_check) > 3.0:
                    last_admission_check = now
                    # Retry the join click — the button frequently isn't
                    # rendered yet on the initial attempt right after
                    # domcontentloaded, so one shot is unreliable. Safe to
                    # repeat: once in-call the button is gone (no-op).
                    _click_join(page, state)
                    admitted = _detect_admission(page)
                    if admitted:
                        state.set(
                            in_call=True,
                            lobby_waiting=False,
                            joined_at=now,
                        )
                        # Turn captions on by clicking the toolbar button.
                        # A synthetic 'c' keypress is ignored by Meet as
                        # untrusted, so the click is the reliable path. This
                        # block runs once (guarded by `not state.in_call`).
                        for cap_label in ("자막 사용", "Turn on captions", "Captions"):
                            try:
                                cbtn = page.get_by_role(
                                    "button", name=cap_label, exact=False
                                ).first
                                if cbtn.count() and cbtn.is_visible():
                                    cbtn.click(timeout=3_000)
                                    state.set(captions_enabled_attempted=True)
                                    break
                            except Exception:
                                continue
                        # Force the live-caption language (default 한국어) so
                        # Korean speech isn't transcribed as garbled English.
                        # Best-effort; runs once alongside enabling captions.
                        try:
                            _select_caption_language(page, state)
                        except Exception:
                            pass
                    elif _detect_guest_screen(page):
                        # Not signed in: Meet shows the guest sign-in/name
                        # screen instead of the org "Join now" path. Fail fast
                        # with an actionable error so the agent can prompt a
                        # re-auth rather than waiting out the lobby timeout.
                        guest_hits += 1
                        if guest_hits >= 3:
                            state.set(
                                error=(
                                    "not signed in — Meet is showing the guest "
                                    "sign-in screen (no 'Join now'/'지금 참여'). "
                                    "The saved Google session is invalid or "
                                    "expired. Re-authenticate with: "
                                    "hermes meet auth"
                                ),
                                leave_reason="not_authenticated",
                            )
                            break
                    elif now > lobby_deadline:
                        state.set(
                            error=(
                                "lobby timeout — host never admitted the bot "
                                f"within {int(lobby_deadline - state.join_attempted_at) if state.join_attempted_at else 0}s"
                            ),
                            leave_reason="lobby_timeout",
                        )
                        break
                    elif _detect_denied(page):
                        state.set(
                            error="host denied admission",
                            leave_reason="denied",
                        )
                        break

                try:
                    queued = page.evaluate("window.__hermesMeetDrain && window.__hermesMeetDrain()")
                    if isinstance(queued, list):
                        state.record_raw_batch(queued)
                        for entry in queued:
                            if not isinstance(entry, dict):
                                continue
                            speaker = str(entry.get("speaker", ""))
                            text = str(entry.get("text", ""))
                            block_id = entry.get("blockId")
                            state.record_caption(speaker=speaker, text=text, block_id=block_id)
                            # Barge-in: if the bot is currently generating
                            # audio AND a real human just spoke, cancel the
                            # in-flight response so we don't talk over them.
                            if rt["enabled"] and rt["session"] is not None:
                                if _looks_like_human_speaker(speaker, guest_name):
                                    try:
                                        cancelled = rt["session"].cancel_response()
                                        if cancelled:
                                            state.set(last_barge_in_at=now)
                                    except Exception:
                                        pass
                except Exception:
                    # Meet reloaded or we got booted — try to detect and
                    # exit gracefully rather than spinning.
                    if page.is_closed():
                        state.set(leave_reason="page_closed")
                        break

                # End-of-meeting detection (only while in-call).
                if state.in_call and (now - last_presence_check) > 5.0:
                    last_presence_check = now
                    cnt = _participant_count(page)
                    if cnt is not None:
                        last_good_count = cnt
                        last_good_count_at = now
                    if cnt is not None and cnt >= 2:
                        seen_others = True
                        alone_since = None
                    elif seen_others and cnt is not None and cnt <= 1:
                        # Everyone else left — bot is alone (detection working).
                        if alone_since is None:
                            alone_since = now
                        elif now - alone_since >= alone_grace:
                            state.set(leave_reason="all_left")
                            break
                    # Silence fallback — ONLY when we cannot currently confirm
                    # others are present. If a recent reliable count showed >=2
                    # people, the bot stays no matter how long the room is quiet;
                    # this only arms when participant detection is failing/stale.
                    others_confirmed = (
                        last_good_count is not None
                        and last_good_count >= 2
                        and (now - last_good_count_at) <= present_staleness
                    )
                    if (
                        silence_timeout > 0
                        and not others_confirmed
                        and state.last_caption_at
                        and now - state.last_caption_at > silence_timeout
                    ):
                        state.set(leave_reason="silence_timeout")
                        break

                # Fold the realtime session's byte/timestamp counters into
                # the status file so meet_status can surface them.
                if rt["session"] is not None:
                    state.set(
                        audio_bytes_out=getattr(rt["session"], "audio_bytes_out", 0),
                        last_audio_out_at=getattr(rt["session"], "last_audio_out_at", None),
                    )

                time.sleep(1.0)

            # Try to leave cleanly — click "Leave call" button if present.
            try:
                page.evaluate(
                    "() => { const b = document.querySelector('button[aria-label*=\"eave call\"]');"
                    " if (b) b.click(); }"
                )
            except Exception:
                pass

            # Mirror the (possibly refreshed) session back to auth.json so the
            # ephemeral meet_create path keeps current cookies.
            _export_storage_state(context, auth_state)
            context.close()
            if browser is not None:
                browser.close()
            # v2: teardown PCM pump, speaker thread, and audio bridge.
            if rt.get("pcm_pump"):
                try:
                    rt["pcm_pump"].terminate()
                    rt["pcm_pump"].wait(timeout=3)
                except Exception:
                    pass
            if rt["speaker_stop"]:
                try:
                    rt["speaker_stop"]()
                except Exception:
                    pass
            if rt["speaker_thread"] is not None:
                try:
                    rt["speaker_thread"].join(timeout=5.0)
                except Exception:
                    pass
            if rt["session"]:
                try:
                    rt["session"].close()
                except Exception:
                    pass
            if rt["bridge"]:
                try:
                    rt["bridge"].teardown()
                except Exception:
                    pass
            state.set(in_call=False, captioning=False, exited=True)
            return 0

    except Exception as e:
        state.set(error=f"unhandled: {e}", exited=True)
        return 1


def _try_guest_name(page, guest_name: str) -> None:
    """If Meet is showing a guest-name input, type *guest_name* into it."""
    try:
        # Meet's guest name input has placeholder "Your name".
        locator = page.locator('input[aria-label*="name" i]').first
        if locator.count() and locator.is_visible():
            locator.fill(guest_name, timeout=2_000)
    except Exception:
        pass


def _detect_admission(page) -> bool:
    """True if we're clearly past the lobby and in the call itself.

    Uses a JS-side probe because Meet's DOM structure varies by client
    version. We check several high-signal indicators and declare admission
    on the first hit:

      1. Leave-call button is present (``aria-label`` contains "eave call").
      2. Caption region has appeared (we installed the observer and it attached).
      3. The participant list container is visible.

    Conservative by default — returns False on any error.
    """
    probe = r"""
    (() => {
      const leave = document.querySelector(
        'button[aria-label*="eave call" i], button[aria-label*="나가기"]'
      );
      if (leave) return true;
      if (window.__hermesMeetInstalled) {
        const caps = document.querySelector(
          '[role="region"][aria-label*="aption" i], ' +
          '[role="region"][aria-label*="자막"], ' +
          'div[jsname="YSxPC"], div[jsname="tgaKEf"]'
        );
        if (caps) return true;
      }
      const parts = document.querySelector(
        '[aria-label*="articipants" i], [aria-label*="참여자"]'
      );
      if (parts) return true;
      return false;
    })();
    """
    try:
        return bool(page.evaluate(probe))
    except Exception:
        return False


def _detect_denied(page) -> bool:
    """True when Meet is showing a 'you were denied' / 'no one admitted' page."""
    probe = r"""
    (() => {
      const text = document.body ? document.body.innerText || '' : '';
      // English only — matches what shows up when the host denies or
      // removes a guest.
      if (/You can't join this video call/i.test(text)) return true;
      if (/You were removed from the meeting/i.test(text)) return true;
      if (/No one responded to your request to join/i.test(text)) return true;
      return false;
    })();
    """
    try:
        return bool(page.evaluate(probe))
    except Exception:
        return False


def _detect_guest_screen(page) -> bool:
    """True when Meet shows the guest / not-signed-in pre-join screen.

    A signed-in org account always gets the "Join now / 지금 참여" button and
    never a name field. So the presence of the guest name input (or an
    'Ask to join' + 'Sign in' combo) with no 'Join now' means the saved
    session failed to authenticate and the bot would otherwise knock as an
    anonymous guest and stall in the lobby. Conservative: False on error.
    """
    probe = r"""
    (() => {
      const txt = document.body ? (document.body.innerText || '') : '';
      // Signed in → "Join now" present, never the guest screen.
      if (/지금 참여|Join now/i.test(txt)) return false;
      // Guest name input (strongest signal — signed-in users never see it).
      let nameInput = false;
      for (const el of document.querySelectorAll('input')) {
        const a = (el.getAttribute('aria-label') || '') + ' ' +
                  (el.getAttribute('placeholder') || '');
        if (/이름|your name/i.test(a)) { nameInput = true; break; }
      }
      if (nameInput) return true;
      // Fallback: an explicit "Sign in" affordance alongside "Ask to join".
      const askToJoin = /참여 요청|Ask to join/i.test(txt);
      const signIn = /(로그인|Sign in)/.test(txt);
      return askToJoin && signIn;
    })();
    """
    try:
        return bool(page.evaluate(probe))
    except Exception:
        return False


def _participant_count(page) -> Optional[int]:
    """Read the in-call participant count from Meet's header badge.

    Meet renders the live count in ``div.fs3avc`` (obfuscated class, current
    as of 2026). Returns the integer, or None if the element is absent or
    unparseable (e.g. Meet changed the class) so callers can fall back to a
    caption-silence timeout instead of mis-detecting "everyone left".
    """
    try:
        n = page.evaluate(
            r"""
            () => {
              const el = document.querySelector('div.fs3avc');
              if (!el) return null;
              const n = parseInt((el.textContent || '').trim(), 10);
              return Number.isFinite(n) ? n : null;
            }
            """
        )
        return int(n) if isinstance(n, (int, float)) else None
    except Exception:
        return None


def _looks_like_human_speaker(speaker: str, bot_guest_name: str) -> bool:
    """Whether a caption line's speaker is probably a human, not our bot echo.

    Meet attributes captions to the speaker's display name. When Chrome is
    reading our fake mic, Meet still attributes captions to *our* bot name
    (because the bot is the one "speaking"). We don't want those to trigger
    barge-in. Anything else — real participant names — does.

    Conservative: unknown / blank speakers (common when caption scraping
    falls back to raw text) do NOT trigger barge-in, because we can't tell
    whether it was a human or us.
    """
    if not speaker or not speaker.strip():
        return False
    spk = speaker.strip().lower()
    if spk in {"unknown", "you", bot_guest_name.strip().lower()}:
        return False
    return True


def _click_join(page, state: _BotState) -> None:
    """Click 'Join now' or 'Ask to join' if either button is visible.

    Flags ``lobby_waiting`` when we hit the "waiting for host to admit you"
    state so the agent can surface that in status.
    """
    # Localized button labels. UI language follows the signed-in account,
    # so cover English + Korean. "ask to join" variants flag lobby_waiting.
    # Match on a substring of the accessible name (= aria-label). The Korean
    # join button's aria-label is "마이크 및 카메라 없이 지금 참여", so match "지금 참여".
    join_labels = ("Join now", "지금 참여")
    ask_labels = ("Ask to join", "참여 요청", "입장 요청")
    for label in (*join_labels, *ask_labels):
        try:
            btn = page.get_by_role("button", name=label, exact=False).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=3_000)
                if label in ask_labels:
                    state.set(lobby_waiting=True)
                break
        except Exception:
            continue


def _parse_duration(raw: str) -> Optional[float]:
    """Parse ``30m`` / ``2h`` / ``90`` (seconds) → float seconds, or None."""
    if not raw:
        return None
    raw = raw.strip().lower()
    try:
        if raw.endswith("h"):
            return float(raw[:-1]) * 3600
        if raw.endswith("m"):
            return float(raw[:-1]) * 60
        if raw.endswith("s"):
            return float(raw[:-1])
        return float(raw)
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover — subprocess entry point
    sys.exit(run_bot())
