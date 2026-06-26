# hermes-agent-plugins

A collection of [hermes-agent](https://github.com/NousResearch/hermes-agent)
plugins. Each top-level directory is one installable plugin.

> _Community plugins for hermes-agent — not officially affiliated with or
> endorsed by Nous Research._

> Install target the hermes plugin system directly — drop into
> `~/.hermes/plugins/<name>/` and enable. The `hermes plugins install` command
> does this for you straight from GitHub, including from a subdirectory of a
> monorepo like this one.

## Plugins

| Plugin | What it does | Status |
|---|---|---|
| [`google_meet`](google_meet/) | Create + join a Google Meet, transcribe live captions, optionally speak in-call, and follow up afterwards. | ✅ |

_(more to come — add a new top-level directory per plugin, each with its own `plugin.yaml`.)_

## Install

Replace `<owner>` with this repo's GitHub owner.

```bash
# Install one plugin from its subdirectory in this monorepo, and enable it.
hermes plugins install <owner>/hermes-agent-plugins/google_meet --enable

# Or install disabled, then enable later.
hermes plugins install <owner>/hermes-agent-plugins/google_meet --no-enable
hermes plugins enable google_meet
```

This clones the subdirectory into `~/.hermes/plugins/google_meet/` and gates it
via `plugins.enabled` in your hermes config. Manage with:

```bash
hermes plugins list
hermes plugins update google_meet
hermes plugins remove google_meet
```

### Manual install (no network / air-gapped)

```bash
cp -r google_meet ~/.hermes/plugins/google_meet
# then enable it
hermes plugins enable google_meet
```

## Per-plugin setup

Each plugin documents its own prerequisites in its directory README. For
`google_meet` you must run its one-time browser auth (`hermes meet auth`) so the
bot joins with a signed-in Google session — see [`google_meet/README.md`](google_meet/README.md).

## License

**MIT — free to use, modify, and redistribute, including commercially.** No fee,
no permission required; the only condition is keeping the copyright + license
notice in copies (standard MIT).

The `google_meet` plugin is a derivative of the original `google_meet` plugin
shipped with hermes-agent (© Nous Research, MIT); enhancements and any
additional plugins © Dou Inc. Both copyright lines are retained in
[LICENSE](LICENSE) as MIT requires — this does not restrict your use in any way.
