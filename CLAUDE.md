# hermes-agent-plugins

Public, open-source repo. Everything committed here is world-readable the
moment it is pushed, and stays reachable by commit SHA even after the working
tree is cleaned — GitHub keeps unreferenced blobs, and clones and forks keep
their own copies. **Sanitizing after the fact is not a fix.** The check has to
happen before the commit.

## Before every commit: scan the staged diff

Run this and read the output. Do not commit on a clean result you did not
actually look at.

```bash
git diff --cached -U0 | grep -E '^\+' | grep -nEi \
  -e '(^|[^0-9])[0-9]{17,19}([^0-9]|$)' \
  -e '\[[0-9]{2}:[0-9]{2}:[0-9]{2}\][^]]*[^ :]+ ?:' \
  -e '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' \
  -e '/(home|Users)/[a-z]' \
  -e '(sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-|AIza[0-9A-Za-z_-]{30,})' \
  -e '(Observed|observed).{0,40}20[0-9]{2}-[0-9]{2}-[0-9]{2}'
```

It catches, in order: Discord/Slack snowflake IDs, transcript lines, email
addresses, absolute home paths, API tokens, and dated debugging notes. Hits are
not automatically violations — judge each one. Do not add a Korean character
class to this command; `[가-힣]` fails with "Invalid collation character" under
the C locale.

## What must never be committed

Real values from a live install, even inside a comment or an example:

- **Account identifiers** — Discord user/guild/channel IDs, Google account
  names, Slack IDs. Use `123456789012345678`.
- **Real people** — names, display names, handles. Use `표시명` / `Alice`.
- **Anything anyone actually said** — transcript lines, meeting content,
  captured captions, even one line quoted to illustrate a format.
- **Credentials and sessions** — tokens, `auth.json`, cookies, OAuth client
  secrets, bearer tokens. `.gitignore` lists the runtime artifacts as a safety
  net, but it only catches the files, not a value pasted into source.
- **Host details** — absolute home paths, hostnames, specific GPU/hardware
  models, LAN addresses. An unusual machine model identifies its owner.
- **Meet codes and URLs** from real meetings. Use `abc-defg-hij`.

Runtime artifacts (transcripts, recordings, sessions, tokens) live under
`$HERMES_HOME`, never in this repo. See SECURITY.md.

## Comments explain the mechanism, not the incident

Comments here carry real design rationale, and that is worth keeping. What does
not belong is the debugging diary that produced it: dates, "observed on
<date>", how many people were in the meeting where it broke, the specific box
it was reproduced on, pasted log excerpts with timestamps.

Keep the reproducible numbers that justify a decision (`float32 10.0s vs int8
6.2s`, "every move burns the full 30s timeout"). Drop the narrative that dates
and locates them.

## Plugins live in two places — keep them identical

A plugin under development exists twice: here, and installed at
`~/.hermes/plugins/<name>/` where the running gateway loads it. Fixing only one
means the next sync silently reverts the other. After editing either, verify:

```bash
diff -rq --exclude=__pycache__ --exclude=README.md \
  ~/.hermes/plugins/discord_meeting discord_meeting
```

Plugin code is loaded at gateway startup, so `hermes gateway restart` is
required before a change is live. The restart drains in-flight agent turns
first and can take minutes; the gateway refuses new turns while draining.
