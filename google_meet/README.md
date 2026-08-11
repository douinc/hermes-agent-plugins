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
   plugin reinstalls/updates). Needs a display.

   **On a headless box** (no display), use the bundled helper instead — it
   brings up a virtual display + a loopback-only VNC server, runs the sign-in on
   it, and cleans up after itself:

   ```bash
   sudo apt install -y xvfb x11vnc autocutsel xdotool   # once
   ./tools/meet-auth-vnc                                # from the plugin dir
   ```

   Then tunnel in from a machine with a browser and sign in:
   `ssh -f -N -L 5901:localhost:5900 <host>` → open `vnc://localhost:5901`.
   (Paste inside the VNC window is **Ctrl+V**; `autocutsel` is what makes it
   work at all, since a bare X display has no clipboard owner.)

3. **`hermes meet oauth`** — *optional*, and only worth it if you have a GCP
   project. It grants one extra scope so `meet_create` calls the **Meet REST
   API** instead of driving a throwaway browser. See
   [Why `meet_create` has two backends](#why-meet_create-has-two-backends).

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
`hermes meet auth` rather than re-joining. Both flavours of expiry raise it:
Meet loading the anonymous guest screen, and a fully revoked session where the
navigation never reaches Meet at all and Google redirects to
`accounts.google.com` (the bot exits within seconds instead of sitting out the
lobby timeout).

> **Judging session health:** never trust cookie expiry dates. Google revokes
> sessions server-side while the cookies on disk still read as valid for months.
> The only real check is to load `meet.google.com` on the persistent profile and
> see whether the signed-in UI (a "New meeting" button) or a "Sign in" call to
> action comes back.

### Why `meet_create` has two backends

`meet_create` can make a room two ways, chosen by `HERMES_MEET_CREATE_MODE`
(default `auto`):

| Backend | Needs | Cost of a create |
|---|---|---|
| `browser` | nothing (the cookie session from `hermes meet auth`) | ~9s — launches a throwaway Chromium and clicks through the UI |
| `api` | a GCP project with the [Meet API](https://console.cloud.google.com/apis/library/meet.googleapis.com) enabled, plus `hermes meet oauth` | ~2s — one `POST` to `v2/spaces` |

`auto` uses `api` when a usable OAuth credential is present and falls back to
`browser` otherwise — so an install that never opted into OAuth behaves exactly
as it always has, and an install whose OAuth later breaks degrades instead of
failing.

**What the API backend actually buys you — and what it does not.**

It does **not** reduce how often you re-authenticate. Read that twice before you
go enable an API for it. Joining a call and scraping captions has no API at all;
the join bot needs the browser cookie session, and nothing here changes that. If
your session expires every N days, it will still expire every N days.

What it does buy:

- **Speed.** ~2s instead of ~9s per create (measured).
- **`meet_create` keeps working when the cookie session is dead** — it no longer
  depends on it. Note this is of limited comfort if you also need to *join* the
  room you just made.
- **It removes a pattern that plausibly accelerates session revocation.** The
  browser backend has to inject the cookie session into a *fresh, throwaway*
  Chromium, because the join bot's persistent profile is locked whenever the bot
  is in a call. A virgin automated browser presenting long-lived auth cookies is
  the shape of a stolen session, and Google is known to revoke on such signals.
  **We have not measured this** — we have not shown that removing the browser
  backend lengthens session life. Treat it as removing a plausible risk, not as
  a proven fix.

If `meet_create` speed is irrelevant to you and you have no GCP project, the
browser backend is a perfectly reasonable place to stay. That is why it remains
the zero-config default.

```bash
hermes meet oauth          # one-time consent; adds only the Meet scope
hermes meet setup          # shows which backend meet_create will use
```

`hermes meet oauth` **adds** `meetings.space.created` to whatever scopes the
token already carries (it is typically shared with other Google integrations)
and backs the old token up first — it never drops an existing scope. On a
headless box, tunnel the OAuth redirect: `ssh -L 8080:localhost:8080 <host>`,
then run `hermes meet oauth --no-browser` and open the printed URL locally.

Standard use of the Meet API is free; `spaces.create` is quota-limited (100/min
per project) rather than billed.

### The thing that actually forces you to re-authenticate

If your Google Workspace has a **web session duration** policy, the bot's cookie
session is force-expired on that schedule no matter which backend you use — so
joining and transcribing break on a fixed cycle. You can spot it in the sign-in
URL: it carries `passive=<seconds>` (the Workspace default is `1209600`, i.e. 14
days).

The fix is to give the bot's own OU a longer or unlimited session:
**Admin console → Security → Access and data control → Google Session control →
Web session duration**. Two things to know before you go looking:

- It is a **different setting** from *Google Cloud console and SDK session
  control*, which sits right next to it and governs only Cloud/`gcloud`.
- It requires **Business Plus, Enterprise, Frontline Standard/Plus, Education, or
  Cloud Identity Premium**. On Business Starter/Standard the setting is not
  there at all, and you are stuck with the 14-day cycle.

Scope it to an OU containing only the bot account. Applying "never expires" to an
OU with human users in it is a real security regression.

When the session does expire, `tools/meet-auth-vnc` gets you signed back in from
a headless box in a couple of minutes.

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
| `HERMES_MEET_CREATE_MODE` | `auto` | Which `meet_create` backend to use: `auto` (Meet API if a credential is present, else browser), `api` (fail if unavailable), or `browser` (never use the API). |
| `HERMES_MEET_OAUTH_TOKEN` | `<home>/google_token.json` | Authorized-user JSON for the Meet API backend. Written by `hermes meet oauth`. |
| `HERMES_MEET_CLIENT_SECRET` | `<home>/google_client_secret.json` | OAuth client secrets (Desktop app) used by `hermes meet oauth`. |
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
