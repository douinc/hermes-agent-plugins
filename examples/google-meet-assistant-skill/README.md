# google-meet-assistant (example orchestration skill)

An **optional** hermes *skill* that drives the full workflow on top of the
`google_meet` plugin: create → join → live transcript → post a markdown summary
to your chat platform.

This is an example, not part of the plugin install. To use it, copy `SKILL.md`
(and the `references/` file) into your hermes skills directory, e.g.
`~/.hermes/skills/google-meet-assistant/`.

## Configure for your org

The skill is platform/language-agnostic in principle but the shipped copy uses
some defaults you should adjust:

- `<YOUR_TEAM_CALENDAR_ID>` — set to your Google Calendar id for the scheduled /
  calendar-event fallback path (or remove that path if you don't use it).
- Chat platform: the examples mention Discord; swap for your gateway platform.
- Transcript language: examples assume Korean captions; set your meeting
  language (the plugin forces the caption language on the bot's account).

The `google_meet` plugin works fully on its own without this skill — the skill
just encodes the create→join→summarize conventions for the agent.
