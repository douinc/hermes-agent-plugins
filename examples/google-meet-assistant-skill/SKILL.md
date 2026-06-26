---
name: google-meet-assistant
description: "Any Google Meet task from Discord — create a meeting room ('회의 만들어줘', 'make a meet link'), join & record a call ('회의 기록해줘', '이 회의 들어와줘', 'transcribe this meet'), or summarize a finished meeting ('방금 회의 요약해줘', 'summarize the meeting'). Does create → join → live Korean transcript → Discord markdown summary."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [google-meet, create, transcription, summary, discord, productivity]
    related_skills: [teams-meeting-pipeline]
---

# Google Meet Assistant

## Overview
This skill governs the full Google Meet flow with the organization's authenticated Google account: creating meeting links to share, joining calls (no lobby wait), scraping the live captions into a transcript, and posting readable Discord-markdown summaries — live or after the meeting.

## When to Use
Trigger on natural requests like these (Korean or English) — map each to the right tool:
- **Create** a room → `meet_create`, then share the link. e.g. "회의 만들어줘", "구글 미트 하나 열어줘", "make a meet link".
- **Join / record** a call → `meet_join`. e.g. "이 회의 들어와줘", "회의 기록해줘", "방금 링크 회의 녹화해줘", "transcribe this meet <url>".
- **Summarize** a meeting → read the transcript file, post a Discord summary. e.g. "방금 회의 요약해줘", "회의 내용 정리해줘", "summarize the meeting".
- Do NOT use for Microsoft Teams or Feishu meetings unless explicitly asked.

## Core Workflow

### 0. Creating a Meet (optional)
- **First decide: now or scheduled?** If the request names a *future* start time and/or asks to put it on the calendar (e.g. "3시 회의 만들고 캘린더에 잡아줘", "내일 2시 미팅 잡아줘"), do NOT use this immediate path — go to **§0.5 Scheduled join**. The default auto-join below joins *right now*, which would leave the bot recording an empty room until the meeting actually starts. Only use this section when the meeting is starting now/imminently (within ~2 minutes).
- When asked to create/make a meeting, call `meet_create`. It uses the bot's signed-in account to generate a fresh link and returns `{ "url": "https://meet.google.com/...", "joined": true, ... }`.
- **By default `meet_create` also auto-joins and starts recording** the new room (the bot owns it on its org account, so it enters with no lobby). You do NOT need a separate `meet_join` call for the create-and-record flow. Check `joined: true` in the result, then verify with `meet_status`.
- **CRITICAL — create means create-AND-join.** For any plain "make a meeting" request ("회의 만들어줘", "미트 하나 열어줘", "make a meet link") the bot must enter the room *immediately* on creation, even if the user says nothing about joining/recording. Just call `meet_create` **with no extra params** — it now auto-joins unconditionally. Never just post the link and stop.
- **Only set `link_only: true`** when the user *explicitly* wants a link-only result with no bot in the room — i.e. they say something like "회의 **링크만** 만들어줘", "just give me a link", "don't join / 들어가진 마". If they did not explicitly say link-only, leave it unset and the bot auto-joins. (Note: the old `join` param is gone — auto-join is enforced in code so a stray `join:false` can no longer keep the bot out of its own room.) Pass `mode: "realtime"` / `duration` through `meet_create` to control the recording.
- Prefer `meet_create` for room creation. It uses the Meet/Chrome signed-in session, not `~/.hermes/google_token.json`; missing Google Workspace OAuth does not block this primary path.
- Post the link to the Discord channel for people to join.
- If the tool surface is unavailable but the `google_meet` plugin files are present, a terminal fallback is:
  ```bash
  PYTHONPATH=$HOME/.hermes/plugins python -m google_meet.meet_create
  ```
  It prints `MEET_URL=https://meet.google.com/...`; share that URL.
- **Fallback when `meet_create` fails with UI drift** (for example `MEET_CREATE_FAILED: 'New meeting' button not found`): create a short Google Calendar event with Meet conference data and share its `hangoutLink`. Use the `google-workspace` skill/API, and use your team's default calendar (configure `<YOUR_TEAM_CALENDAR_ID>` for your org). Verify the created event by reading it back and confirm `status: confirmed` plus a real `hangoutLink`/video entry point before sharing. Keep the event title generic and scoped to the immediate meeting topic.
  - **Preserve the auto-join intent on this fallback.** `meet_create` normally auto-joins the new room; when you fall back to the Calendar path you must do that step yourself — after you have the `hangoutLink`, call `meet_join(url=hangoutLink)` (unless the user only wanted a link to share). Then verify with `meet_status` (`inCall: true`).

### 0.5 Scheduled join (future start time) — DO NOT auto-join now
Use this whenever the bot should enter the room at a *future* time (a named time like "3시", "내일 2시", "Friday 10am"), whether that is a brand-new room or an existing link the user wants recorded later.

**Why a separate path.** `meet_create`'s default auto-join enters the room *immediately*. For a meeting hours away that means the bot sits alone recording an empty room until people arrive. So: get the link now, but make the bot join *at the scheduled time* via a one-shot cron job.

**The calendar is OPTIONAL — keep the two concerns separate.** A future start time does NOT by itself mean a calendar event. The required piece is the scheduled *join* (the cron job). A calendar event is an *additional* layer you add only when the user asks to put it on the calendar, or when the meeting has attendees who need an invite. Decide each independently:

| What the user asked | Scheduled join (cron) | Calendar event |
|---|---|---|
| "3시에 이 회의 들어와서 녹화해줘" (existing link, no calendar) | ✅ | ❌ |
| "3시에 미트 하나 열어서 들어가 있어줘" (no calendar mention) | ✅ | ❌ (skip unless asked) |
| "3시 회의 만들고 캘린더에 잡아줘" / attendees to invite | ✅ | ✅ |

Steps (always do 1–2 and 4–5; do 3 only when a calendar event is wanted):
1. **Resolve the time** to a concrete local ISO timestamp (e.g. "3시" today → `2026-06-25T15:00:00`). If ambiguous or already past today, pick the next future occurrence.
2. **Get the link without joining.** New room → `meet_create(link_only=true)` (the "meeting for later" link does not expire, so it is still valid at the scheduled time). Existing meeting → just use the URL the user gave.
3. **(Calendar layer, only if wanted)** Create the event with the Google Calendar MCP `create_event` at that time, Meet URL in the location/description. Use your team's default calendar (`<YOUR_TEAM_CALENDAR_ID>`). **Capture the returned event `id`** — when there is an event, it becomes the source of truth and the cron re-checks it (see verifying prompt). Use the Calendar MCP (not the `google-workspace` script), because reschedule/cancel need `get_event`/`update_event`/`delete_event`, which the script does not have.
4. **Schedule the auto-join** as a one-shot cron a minute before start (so the bot is in before participants):
   - With a calendar event: `cronjob(action='create', schedule='<ISO start − 1m>', name='meet-autojoin:<eventId>', skills=['google-meet-assistant'], prompt=<calendar-verifying join prompt>)`
   - Without a calendar event: `cronjob(action='create', schedule='<ISO start − 1m>', name='meet-autojoin:<meetCode>', skills=['google-meet-assistant'], prompt=<plain join prompt>)` — the cron itself holds the time; there is nothing to re-check.
   - The deterministic `name` is how you find this job later to reschedule or cancel.
5. **Tell the channel** the link is shared and the bot will join automatically at the scheduled time (e.g. "링크 공유했고, 봇은 15:00에 자동 입장합니다"). Do NOT imply the bot is in the room now.

**Plain join prompt** (no calendar — the cron's schedule IS the source of truth):
> `meet_join(url=<the Meet link>)` and record; verify with `meet_status` (`inCall: true`).

**Calendar-verifying join prompt** (only when a calendar event exists — re-check the event, then act, so external edits self-correct):
> Re-read Google Calendar event `<eventId>` via the Calendar MCP `get_event`.
> - If it is missing or `status == "cancelled"`: do NOT join. Remove this cron job (`cronjob action='list'` → `action='remove'` this job's id) and optionally note in the channel that the meeting was cancelled.
> - If its start is still more than ~3 minutes in the future (it was moved later): do NOT join — reschedule this job (`cronjob action='update'` to the new start − 1m) and exit.
> - Otherwise (starting around now): `meet_join(url=<the Meet link>)` and record; verify with `meet_status` (`inCall: true`).

### 0.6 Editing or cancelling a scheduled join (via Hermes)
Mirror whatever layers exist — always fix the cron job; fix the calendar event too only if one was created.
- **Reschedule:** if there is a calendar event, `update_event` to the new time first. Then `cronjob action='list'`, find the `meet-autojoin:*` job, and `cronjob action='update'` its schedule to the new start (− 1m). Confirm in the channel.
- **Cancel:** if there is a calendar event, `delete_event` (or mark cancelled). Then `cronjob action='list'` → `cronjob action='remove'` the matching job. If the bot is already in the room (meeting had started), also `meet_leave`. Confirm.
- Always `list` first to get the real job id — never guess job ids.

### 1. Joining a Meet
- The `google_meet` plugin must be enabled.
- **Authentication is automatic.** The bot always uses the saved signed-in session at `~/.hermes/workspace/meetings/auth.json`, so it joins org-account meetings directly without host admission. `hermes meet auth` must have been completed once to create that session. (Org policy blocks anonymous/guest joins, so the authenticated session is required, not optional.)
- **`guest_name` does NOT control authentication.** It is only the display name typed into the "Your name" field on a *guest* (unauthenticated) join. On the authenticated path used here, Meet shows the bot account's own name and `guest_name` is ignored. You normally do not need to set it.
- Call `meet_join(url=...)` and let it run in the background — do not block. Verify with `meet_status` (`"inCall": true`).
- **One meeting at a time:** a new `meet_join` replaces any bot already running.

### 2. Reading the Transcript
- The plugin working file is `~/.hermes/workspace/meetings/<meeting-id>/transcript.txt` (meeting-id = the Meet code, e.g. `abc-defg-hij`).
- Do not insert an additional date/timestamp directory into the live transcript path. Keep meeting artifacts directly under the meeting-id folder so `meet_status`, `meet_transcript`, and manual reads all point to the same location.
- If a durable copy is explicitly needed later, copy it separately after the meeting; do not change the active bot output directory.
- Read transcripts with the `read_file` tool. Each line is `[HH:MM:SS] <speaker>: <text>`.
- **Speakers are identified** by their Google account display name (e.g. `Jane Kim`). Use the real names from the transcript in summaries. If a line shows `Unknown`, the caption DOM selectors shifted — flag it rather than guessing who spoke; never invent names.
- **Language:** captions are Korean. If a transcript comes back as garbled English, the bot account's Meet caption language needs to be set to Korean once (persists per account) — flag this rather than summarizing garbage.

### 3. Bot Lifecycle — Important for Summarizing
- The bot **auto-leaves** when everyone else has left the call (`leaveReason: "all_left"`). This is normal end-of-meeting behavior.
- **As long as others are detected in the room, the bot never leaves on silence** — a quiet-but-attended meeting keeps recording. `silence_timeout` (default 45 min, `HERMES_MEET_SILENCE_SECONDS`) is only a *fallback* that fires when participant-count detection is failing/stale (e.g. Meet renamed its DOM class), so the bot doesn't sit forever in an ended room it can no longer read. It does NOT fire while the count reliably shows people present.
- **The transcript file persists after the bot leaves.** To summarize a finished meeting, just `read_file` the existing `transcript.txt` — the bot does NOT need to still be in-call, and you should NOT re-join to summarize.
- A new bot joining the *same* meeting-id wipes that folder's transcript at start, so summarize before re-recording the same room.

### 4. Formatting & Summarizing for Discord
- **No separate HTML files.** Deliver the summary directly as Discord message text unless the user explicitly asks for a file.
- Use structured Discord-compatible markdown:
  - Headers (`### Header`)
  - Bold (`**bold**`)
  - Bullets (`* Item`)
  - Quote/status blocks (`> Quote`)
- Focus on decisions, action items, and key discussion points. Keep it brief and scannable. No flattery or emojis.

## Common Pitfalls
1. **Inventing speakers:** use the real names in the transcript; if a line is `Unknown`, say so — never fabricate who spoke.
2. **Re-joining to summarize:** the transcript persists on disk after the bot leaves; read the file instead of re-joining.
3. **Generating HTML files:** deliver formatted markdown text in Discord, not HTML, unless asked.
4. **Plain unstyled text:** always use headers/bold/lists so summaries are team-ready.
5. **Blocking during join:** run `meet_join` in the background; verify with `meet_status`.
4. **Immediate Exit or Disconnection:** If the bot exits immediately after joining (e.g., `exited: true` in status), it can be manually run and debugged via the terminal (omitting `--guest-name` to ensure organization account auto-join) using:
   ```bash
   hermes meet join <URL>
   ```
   *For details on the container sandbox lifecycle (SIGTERM) and organization account auto-joins, see `references/sandbox-signals-and-autojoin.md`.*
   ```
