# google_meet plugin

Let the hermes agent create or join a Google Meet call, transcribe it,
optionally speak in it, and do the followup work afterwards.

## What ships

| Version | What | Status |
|---|---|---|
| v1 | Create + join a room and scrape live captions to a transcript file (Playwright, listen-only) | ✓ ships by default |
| v2 | Realtime duplex audio: bot speaks in-call via OpenAI Realtime + BlackHole/PulseAudio null-sink | ✓ opt in with `mode='realtime'` |
| v3 | Remote node host: run the bot on a different machine than the gateway | ✓ opt in with `node='<name>'` |

## Architecture

```
┌─ gateway (Linux box, where hermes runs) ────────────────────────────┐
│                                                                      │
│   agent → meet_join(url, mode='realtime', node='my-mac')             │
│         │                                                            │
│         └─ NodeClient ─── ws ────┐                                   │
│                                  │                                   │
└──────────────────────────────────┼───────────────────────────────────┘
                                   │ wss (token auth)
                                   ▼
┌─ node host (user's Mac, signed-in Chrome lives here) ───────────────┐
│                                                                      │
│   NodeServer (from `hermes meet node run`)                           │
│     │                                                                │
│     ├─ start_bot → process_manager.start() → spawns meet_bot         │
│     │                                                                │
│     └─ meet_bot (Playwright)                                         │
│        ├─ Chromium → meet.google.com                                 │
│        ├─ caption scraper → transcript.txt                           │
│        └─ (realtime mode only) RealtimeSpeaker thread                │
│             ↓                                                        │
│           OpenAI Realtime WS → speaker.pcm                           │
│             ↓                                                        │
│           paplay → null-sink → .monitor → virtual-source             │
│                                              ↓                       │
│                              Chrome fake mic (PULSE_SOURCE)          │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

Without v3: the whole right column runs on the gateway machine.
Without v2: the "realtime" path is skipped; transcribe runs alone.

### How realtime audio works (`mode='realtime'`)

Only the transcribe path runs by default. When realtime is enabled, the bot can
*speak* into the call; here is the full chain from `meet_say` to audible voice:

1. **`meet_say(text)` → queue.** `enqueue_say` rejects the call unless the active
   meeting is in `mode='realtime'`, otherwise appends `{id, text}` to
   `say_queue.jsonl` and returns immediately (non-blocking).
2. **Speaker thread.** A `RealtimeSpeaker` thread polls that queue and processes
   one entry at a time.
3. **OpenAI Realtime WS → `speaker.pcm`.** `RealtimeSession.speak()` sends the
   text over `wss://api.openai.com/v1/realtime`, receives `response.audio.delta`
   frames (base64 **PCM16, 24 kHz mono**), decodes them, and **appends the raw
   PCM to `speaker.pcm`**.
4. **PCM pump → null-sink.** A pump streams the growing `speaker.pcm` into a
   virtual speaker in near-real-time — `paplay` (Linux) into a PulseAudio
   **null-sink**, or `ffmpeg` (macOS) into **BlackHole**.
5. **Monitor → virtual mic.** On Linux the null-sink's `.monitor` is the master
   of a **virtual-source** (a fake microphone). Chrome is launched with
   `PULSE_SOURCE=<virtual-source>` and `--use-fake-ui-for-media-stream`, so that
   virtual mic *is* Chrome's microphone — and Google Meet hears the bot.

So the bot never sends WebRTC audio directly: it plays generated speech into a
fake speaker, and Chrome's fake mic picks it up off that speaker's monitor.

## Files

| Path | Purpose |
|---|---|
| `plugin.yaml` | manifest |
| `__init__.py` | `register(ctx)` — registers 6 tools (`meet_create`/`join`/`status`/`transcript`/`leave`/`say`) + `on_session_end` hook + `hermes meet` CLI |
| `meet_bot.py` | Playwright bot subprocess (standalone, `python -m google_meet.meet_bot`) |
| `meet_create.py` | Playwright subprocess that creates a new Meet link (standalone, `python -m google_meet.meet_create`) |
| `process_manager.py` | local bot lifecycle + `enqueue_say` |
| `tools.py` | agent-facing tools + node-routing helper |
| `cli.py` | `hermes meet setup / install / auth / join / status / transcript / say / stop / node ...` |
| `replay_raw_captions.py` | replay a captured `raw_captions.jsonl` through the dedup logic (debugging) |
| `audio_bridge.py` | v2: PulseAudio null-sink (Linux) + BlackHole probe (macOS) |
| `realtime/openai_client.py` | v2: `RealtimeSession` + `RealtimeSpeaker` (file-queue → OpenAI Realtime WS → PCM) |
| `node/protocol.py` | v3: message envelope + validation |
| `node/registry.py` | v3: `$HERMES_HOME/workspace/meetings/nodes.json` |
| `node/server.py` | v3: `NodeServer` (runs on host machine) |
| `node/client.py` | v3: `NodeClient` (used by tool handlers + CLI on gateway) |
| `node/cli.py` | v3: `hermes meet node {run,list,approve,remove,status,ping}` |
| `SKILL.md` | agent usage guide |

## Local quick start

```bash
hermes plugins enable google_meet
hermes meet install                                      # pip + Chromium
hermes meet setup                                        # preflight
hermes meet auth                                         # sign in (see Setup below)
hermes meet join https://meet.google.com/abc-defg-hij    # transcribe
```

## Setup (one-time): authentication & profile

`hermes plugins install` only copies the plugin code. The browser, login, and
profile are set up separately, once:

1. **`hermes meet install`** — installs Playwright + Chromium (add `--realtime`
   for the audio bridge).
2. **`hermes meet auth`** — opens a headed browser, you sign in to the Google
   account the bot should use, and the session is saved. This creates
   `auth.json` and seeds a **persistent Chromium profile**, both under
   `~/.hermes/workspace/meetings/` (outside this plugin dir, so they survive
   plugin reinstalls/updates). Needs a display — over SSH use Xvfb + x11vnc.

**Is auth optional?** Effectively no for normal use:

- **Required** for `meet_create` (the room is created on the signed-in account)
  and for joining your **organization's** meetings without sitting in the lobby
  (org policy usually blocks anonymous/guest joins).
- **Skippable only** for joining a public meeting as a guest — then the bot
  waits in the lobby for a host to admit it (`leaveReason: "lobby_timeout"` if
  not admitted within `HERMES_MEET_LOBBY_TIMEOUT`).

The **`chrome-profile/`** directory is created and managed automatically by the
bot at runtime (a real on-disk Chromium profile keeps the session sticky); you
never create it by hand. If `meet_status` ever reports
`leaveReason: "not_authenticated"`, the saved session expired — re-run
`hermes meet auth` rather than re-joining.

## Configuration

All behavior is environment-driven — set these wherever your hermes gateway
reads env (e.g. `~/.hermes/.env` or the service environment). Everything has a
sensible default; you usually only set the language pair.

### Language / locale (most common)

| Env var | Default | What it does |
|---|---|---|
| `HERMES_MEET_LOCALE` | `en-US` | Browser locale → Meet derives its default UI/caption language from `navigator.language`. Set to your locale, e.g. `ko-KR`, `ja-JP`, `es-ES`. |
| `HERMES_MEET_CAPTION_LANG` | _(unset)_ | Force Meet's live-caption "meeting language" to this **exact option label** in the account's UI language (e.g. `한국어`, `English`, `日本語`, `Español`). Unset = don't touch it. Set this if captions come back in the wrong language (Meet remembers caption language per profile). |
| `HERMES_MEET_CAPTION_LANG_TRIES` | `6` | How many times to retry asserting the caption language after joining. |

> Example for a Korean team: `HERMES_MEET_LOCALE=ko-KR` and
> `HERMES_MEET_CAPTION_LANG=한국어`.

### Join / leave behavior

| Env var | Default | What it does |
|---|---|---|
| `HERMES_MEET_ALONE_SECONDS` | `25` | Grace period before leaving once the bot is the only participant left (after others were seen). |
| `HERMES_MEET_SILENCE_SECONDS` | `2700` | Fallback auto-leave after this many seconds of caption silence — only when participant-count detection is failing/stale (never while people are confirmed present). |
| `HERMES_MEET_PRESENCE_STALE_SECONDS` | `90` | How long a participant-count reading stays "fresh" for the present-people check that gates the silence fallback. |
| `HERMES_MEET_LOBBY_TIMEOUT` | `300` | Seconds to wait in the lobby for host admission (guest joins) before giving up. |
| `HERMES_MEET_GUEST_NAME` | `Hermes Agent` | Display name on an **unauthenticated** guest join. Ignored on the signed-in path. |

### Storage / runtime

| Env var | Default | What it does |
|---|---|---|
| `HERMES_MEET_AUTH_STATE` | `<home>/workspace/meetings/auth.json` | Path to the saved signed-in browser session. |
| `HERMES_MEET_RETENTION_DAYS` | `30` | Auto-prune meeting folders (transcripts can hold sensitive content) older than this; `0` keeps everything. |
| `HERMES_MEET_RAW_CAPTIONS` | `1` | Capture ground-truth `raw_captions.jsonl` before dedup (for debugging/replay); `0` to skip. |
| `HERMES_MEET_HEADED` | _(unset)_ | Set to `1` to run Chromium headed (debugging). |

### Realtime mode (opt-in, `mode='realtime'`)

`HERMES_MEET_REALTIME_KEY` (OpenAI Realtime key), `HERMES_MEET_REALTIME_MODEL`,
`HERMES_MEET_REALTIME_VOICE`, `HERMES_MEET_REALTIME_INSTRUCTIONS`. See below.

## Realtime mode

Linux (preferred, most automated):
```bash
hermes meet install --realtime                     # installs pulseaudio-utils
echo 'OPENAI_API_KEY=sk-...' >> ~/.hermes/.env
hermes meet join https://meet.google.com/abc-defg-hij --mode realtime
# then from the agent or CLI:
hermes meet say "Good morning everyone, I'm the note-taker bot."
```

macOS:
```bash
hermes meet install --realtime     # runs: brew install blackhole-2ch ffmpeg
# then — manually! — open System Settings → Sound → Input → BlackHole 2ch
echo 'OPENAI_API_KEY=sk-...' >> ~/.hermes/.env
hermes meet join https://meet.google.com/abc-defg-hij --mode realtime
```

On macOS, hermes will **not** switch your system audio input automatically — the
user has to do it. This is deliberate: switching default input on a whim would
be a surprising side effect.

## Remote node host

On the node machine (e.g. user's Mac with a signed-in Chrome):
```bash
pip install playwright websockets
python -m playwright install chromium
hermes plugins enable google_meet
hermes meet node run --display-name my-mac --host 0.0.0.0 --port 18789
# prints the bearer token on first run; copy it
```

On the gateway:
```bash
hermes meet node approve my-mac ws://<mac-ip>:18789 <token>
hermes meet node ping my-mac
# now any meet_* tool call accepts node='my-mac' (or 'auto')
```

## Safety

- URL gate: only `https://meet.google.com/abc-defg-hij`, `/new`, `/lookup/<id>`.
- No calendar scanning, no auto-dial, no auto-consent announcement.
- Node server uses bearer-token auth; no key exchange, no TLS termination
  built in — run it on a LAN or behind a reverse proxy you trust.
- One active meeting per (gateway, node) pair. A second `meet_join` leaves the first.
- `meet_say` refuses unless the active meeting was started with `mode='realtime'`.

## Out of scope

- **Calendar scanning** — deliberately not implemented. Join URLs must be explicit.
- **Multi-tenant node sharing** — a node serves one gateway at a time.
- **Windows** — audio bridging isn't tested; `register()` no-ops on Windows.
- **System audio input switching on macOS** — user responsibility, not the bot's.
