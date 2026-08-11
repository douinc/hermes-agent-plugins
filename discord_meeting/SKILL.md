---
name: discord-meeting
description: Read and act on transcripts of Discord voice-channel meetings — summarise, file issues, answer "what did we decide?" — from any text channel, not just the room the meeting happened in.
tags: [meetings, discord, transcription, voice]
---

# Discord meeting transcripts

Voice-channel conversations are transcribed and stored automatically. You do
not join, start, or stop anything — the recording already happened. Your job is
to **read the transcript and do the work the user asks for on top of it**.

## When this applies

Any request that refers to something *said out loud* rather than typed:

- "아까 회의 내용 정리해줘" / "summarise the meeting"
- "회의에서 정한 거 이슈로 만들어줘"
- "우리가 가격 얘기했던 게 언제였지?"
- "미팅룸에서 나온 액션 아이템 뽑아줘"

These come in from **ordinary text channels**, and usually the user will not
say which meeting. That is normal — resolve it yourself.

## Tools

| Tool | Use it for |
|---|---|
| `meeting_list` | What meetings exist. Call first when the meeting is ambiguous. |
| `meeting_transcript` | Read one. Omit `meeting_id` for the most recent. `last=N` for a tail. |
| `meeting_search` | Find which meeting mentioned a topic, then read that one. |

## How to work

1. **"방금/아까 회의"** → `meeting_transcript()` with no arguments. The newest
   meeting is almost always the one meant; don't interrogate the user first.
2. **A topic, no meeting** → `meeting_search(query=...)`, then
   `meeting_transcript(meeting_id=...)` on the hit.
3. **Several candidates** → `meeting_list()`, show the user the ids with their
   start times and speakers, and ask which one.
4. Then summarise, extract decisions, file issues, draft the recap — with your
   normal tools. There is no `meeting_summary` tool on purpose: summarising is
   your reasoning, not a lookup.

## Reading the transcript honestly

Lines look like `[19:07:44] 표시명: 헤르메스 내가 하는 말 들여`.

- Speech-to-text **makes mistakes**, especially on short utterances and proper
  nouns. If a line is garbled or a number looks implausible, say so rather than
  reporting it as fact — quote the raw line and flag it as uncertain.
- The **first line of a meeting may show a numeric user id** instead of a name;
  display names are resolved in the background and appear from the next
  utterance on. Same speaker, two labels.
- A meeting is cut after ~30 minutes of silence in the channel, so one
  conversation is one folder — but a long break mid-discussion can split it.
  If a transcript starts mid-topic, check `meeting_list` for the folder before
  it.
- Utterances addressed to the bot (starting with the wake word) are in the
  transcript too — they are part of what was said in the room.

## What you never do

- Never claim a meeting was recorded that `meeting_list` does not show.
- Never fill gaps in a garbled transcript with plausible-sounding invention.
  Missing is missing.
