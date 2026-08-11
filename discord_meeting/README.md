# discord_meeting

Turn Discord voice channels into meeting records the agent can act on.

The bot follows people into a voice channel, transcribes everything that is
said to a per-meeting file, and otherwise stays completely quiet — no replies,
no messages in the channel — until someone actually addresses it. Afterwards,
from *any* text channel, you can ask the agent to summarise the meeting, pull
out decisions, or file issues from it.

```
someone joins a voice channel   → bot joins, meeting starts
people talk                     → transcript accumulates, channel stays silent
"헤르메스, 방금 내용 정리해줘"    → bot answers, and keeps listening for 30s
someone moves to another room   → meeting closed, bot follows within ~1s
last person leaves              → meeting closed, participants recorded, bot leaves
later, in #general              → "아까 회의 정리해줘" → agent reads the transcript
```

Everything is plugin-side. **No hermes core files are modified**, so a
`hermes update` cannot silently revert it.

## Install

```bash
hermes plugins install douinc/hermes-agent-plugins/discord_meeting --enable --force
hermes gateway restart          # the gateway loads plugin code at startup
```

The skill that tells the agent *when* to use the tools is not auto-discovered
from the plugin directory — hermes only scans `~/.hermes/skills/`. Install it:

```bash
mkdir -p ~/.hermes/skills/productivity/discord-meeting
cp ~/.hermes/plugins/discord_meeting/SKILL.md \
   ~/.hermes/skills/productivity/discord-meeting/SKILL.md
```

## Tools

| Tool | Use |
|---|---|
| `meeting_list` | Recorded meetings, newest first |
| `meeting_transcript` | Read one; omit `meeting_id` for the latest |
| `meeting_search` | Find which meeting mentioned something |

There is deliberately no `meeting_summary`: summarising is the agent's own
reasoning, not a lookup, and wrapping it in a tool buries it behind a second
model.

## Command

```
/meeting listen        answer every utterance in this channel
/meeting quiet         transcript only, but the wake word still summons (default)
/meeting mute          answer nothing at all, wake word included
/meeting autojoin on   follow people into this voice channel (default)
/meeting autojoin off  require /voice channel instead
/meeting status        show both settings
```

Settings are per channel and persist across restarts.

## Waking the bot

Two ways, deliberately:

- **Wake word** — "헤르메스 …" at the *start* of an utterance. Convenient, but
  speech-to-text is unreliable about it: both `base` and `large-v3` render
  "헤르메스" as "에르메스" often enough that the variants are all matched.
  Because "에르메스" is also a brand name, only utterances that *open* with it
  count.
- **`/meeting listen`** — a slash command never passes through Whisper, so it
  always means what it says. This is the reliable path.

Addressing the bot opens a **30-second follow-up window** for that speaker, so
the sentence after the summons reaches the agent without repeating the wake
word. Utterances are cut on 1.5s of silence, so without this the summons and
the request arrive as two events and the request is filed as meeting chatter.

## Storage

```
$HERMES_HOME/workspace/discord-meetings/<guild>/<channel>-<YYYYMMDD-HHMMSS>/
    transcript.txt   [HH:MM:SS] 표시명: 발화
    raw.jsonl        every utterance incl. ones filtered as noise
    meta.json        start, end, participants
```

Meeting boundaries come from **voice-channel membership**, not a timer: the
meeting ends when the last person leaves. An idle-gap rule (default 30 min) is
the fallback if the listener cannot attach. A 90-second grace window after a
meeting closes catches transcripts still in flight, so a slow Whisper result
lands in the meeting it was spoken in instead of stranding a phantom one.

## Hallucination filtering

Whisper invents fluent sentences from silence — Korean models especially the
"자막 제공 …" / "시청해주셔서 감사합니다" family learnt from subtitled video.
Those never reach `transcript.txt` (they stay in `raw.jsonl`, flagged, so an
over-aggressive filter can be audited), and they can never wake the bot.

The filter is deliberately conservative: in a meeting record, deleting
something a person said is worse than keeping one stray artifact.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_DISCORD_MEETING_WAKE` | `헤르메스,에르메스,허메스,…` | Wake-word variants |
| `HERMES_DISCORD_MEETING_GATE` | `1` | `0` = answer everything |
| `HERMES_DISCORD_MEETING_FOLLOWUP_S` | `30` | Follow-up window |
| `HERMES_DISCORD_MEETING_GAP_MIN` | `30` | Idle-gap fallback boundary |
| `HERMES_DISCORD_MEETING_GRACE_S` | `90` | Late-transcript grace after close |
| `HERMES_DISCORD_VOICE_ECHO` | `0` | `1` restores the full `[Voice]` echo |
| `HERMES_STT_COMPUTE_TYPE` | `int8` | faster-whisper compute type |
| `HERMES_DISCORD_MIN_SPEECH_S` | `1.0` | Shortest utterance worth transcribing |

## Speech-to-text notes

The plugin forces `compute_type=int8`. hermes asks faster-whisper for `auto`,
which resolves to float32 on a CPU-only host — measurably slower on the same
audio (10s of audio: 10.0s float32 vs 6.2s int8, `large-v3`).

Check whether your GPU is actually being used before blaming the model:

```python
import ctranslate2; ctranslate2.get_cuda_device_count()   # 0 means CPU-only
```

A machine with an NVIDIA GPU can still report 0 if the ctranslate2 wheel has no
CUDA support for that architecture, in which case transcription is CPU-bound no
matter what the model is. `medium` runs ~1.6× faster than `large-v3` if
conversational latency matters more than transcript accuracy.

## Consent

The bot records everything said in the channel. Tell participants. This plugin
does not announce itself.
