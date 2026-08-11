# Security notes

This repo contains **plugin source only**. No credentials, tokens, transcripts,
or recordings are committed. A few things to keep in mind when running or
forking it:

## Runtime artifacts are sensitive — never commit them

The plugin creates these under your hermes home (`~/.hermes/workspace/meetings/`),
**not** in this repo. They are listed in `.gitignore` as a safety net, but be
deliberate:

- **`auth.json`** — a saved, signed-in Google browser session. This is
  effectively a credential: anyone with it can act as that Google account.
  Never commit it, never paste it, store it with restrictive file permissions.
- **`transcript.txt` / `raw_captions.jsonl`** — verbatim meeting content; treat
  as confidential (it may contain personal or business data).
- **`node_token.json`** — the remote-node bearer token (auto-generated).
- **`*.log`, `status.json`, `.active.json`, `*.pcm`** — runtime state/recordings.

## Remote node host (opt-in, `node=` feature)

- The node server binds to `127.0.0.1` by default and authenticates gateways
  with a randomly generated bearer token (no hardcoded default).
- It does **not** terminate TLS. If you expose it beyond localhost (e.g. over a
  LAN to run the bot on another machine), put it behind a trusted network
  (VPN/SSH tunnel/reverse proxy with TLS). Do not expose the port to the
  public internet.

## Meeting privacy

- The bot only joins `meet.google.com` URLs explicitly passed to it — no
  calendar scanning, no auto-dial.
- Captioning records what participants say. Make sure participants are aware a
  transcribing bot is present, per your local laws and meeting norms.

## Reporting

Found a vulnerability? Please open a private report / contact the maintainer
rather than filing a public issue with exploit details.
