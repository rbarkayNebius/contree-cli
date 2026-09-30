---
icon: gear
---

# Configuration & Profiles

Profiles let you store credentials for multiple projects or environments
and switch between them. This section walks through setting up profiles,
switching contexts, and understanding how configuration is resolved.

## Creating profiles

Each profile stores a token and API URL for a specific project or
environment. When you first ran `contree auth`, it created a profile
called `default`. Add more with `--profile`:

```bash
contree auth --profile=personal
contree auth --profile=sandbox
```

Each command prompts for a token securely (no echo), verifies it against
the API, and writes it to `~/.config/contree/auth.ini`.

The resulting config file looks like this:

```ini
[DEFAULT]
profile = default

[profile:default]
token = eyJ...
url = https://api.tokenfactory.nebius.com/sandboxes
type = iam
project = project-id-default

[profile:personal]
token = eyJ...
url = https://api.tokenfactory.nebius.com/sandboxes
type = iam
project = project-id-personal

[profile:sandbox]
token = eyJ...
url = https://api.tokenfactory.nebius.com/sandboxes
type = iam
project = project-id-sandbox
```

Each profile is a `[profile:NAME]` section with `token`, `url`, `type`,
and `project` keys. The `[DEFAULT]` section stores the name of the active
profile.

## Listing profiles

See all saved profiles and which one is active:

```bash
contree auth ls
```

The output shows the profile name, URL, a token hash (first 16 chars of
SHA256), active status, and a health check result.

`auth ls` verifies each profile against the API with a 2-second timeout.
Possible status values:

- `ok` — token is valid and has the required sandbox permission
- `timeout` — server did not respond in time
- `error` — bad token or network error
- `offline mode` — you passed `-O` / `--offline`
- `no url` — the profile has no API URL configured (re-run `contree auth`)
- `inactive` — token authenticates, but the configured project does not
  grant the sandbox permission this CLI needs

Skip the network check:

```bash
contree auth ls -O
```

For automation, use structured output:

```bash
contree -o json auth ls
```

## Switching profiles

### Persistent switch

Change the active profile for all future commands:

```bash
contree auth switch personal
```

### Per-command override

Use `-p` / `--profile` on any command:

```bash
contree -p personal images
contree -p sandbox run -- uname -a
```

### Environment variable

Override for the entire shell session:

```bash
export CONTREE_PROFILE=sandbox
contree images        # uses sandbox
```

### Inline token

Pass `--token` and `--url` directly:

```bash
contree --token=eyJ... --url=https://api.tokenfactory.nebius.com/sandboxes images
```

:::{warning}
Avoid `--token` on the command line in production — the token is
visible in process listings and shell history.
:::

## Removing profiles

Delete a profile and its session database:

```bash
contree auth remove personal
contree auth rm personal -y    # skip confirmation
```

If the removed profile was active, the CLI switches to the first
remaining profile.

## Profiles and sessions

Each profile has its own session database
(`~/.config/contree/sessions-{profile}.db`), so:

- **Same profile, same terminal** — resumes the existing session
- **Different profile, same terminal** — different session, different data

Switching from `default` to `personal` does not affect your `default`
sessions — you can switch back and continue where you left off.

:::{tip}
To share a session across profiles (rare), set `CONTREE_SESSION`:

```bash
export CONTREE_SESSION=shared-session
```
:::

## Data storage

All data lives in `CONTREE_HOME` (default `$XDG_CONFIG_HOME/contree`,
falling back to `~/.config/contree` when `XDG_CONFIG_HOME` is unset):

| Path | Purpose |
|------|---------|
| `auth.ini` | Profile credentials and settings (created with mode `0600`) |
| `cli.ini` | Optional user-editable defaults for the CLI |
| `cli/sessions/{profile}.db` | Per-profile sessions, history, branches, cache |
| `cli/skills.db` | Installed agent skill registry |

Override with `$CONTREE_HOME`:

```bash
export CONTREE_HOME=/custom/path
```

The directory, and every file the CLI reads or writes in it, must be
owned by you and not writable by other users; otherwise the CLI refuses
to run. Missing directories are created with mode `0700`.

### `cli.ini`

`cli.ini` is meant for hand-editing. Create it yourself; the CLI never
writes to it. Two kinds of sections are supported:

#### `[cli]` section: per-flag defaults

Keys here become argparse defaults. Use the argparse `dest` name (not
the flag name):

| Key | Maps to flag | Notes |
|-----|--------------|-------|
| `log_level` | `--log-level` | One of `debug`, `info`, `warning`, `error`, `critical` |
| `output_format` | `-f` / `--format` | One of the formatter names (`default`, `json`, `json-pretty`, `csv`, `tsv`, `table`) |
| `editor` | `--editor` (file edit) | Fallback when neither `--editor` nor `$EDITOR` is set; if absent the CLI searches `vim` then `nano` on `PATH` and falls back to `vi` |

Example:

```ini
[cli]
log_level = debug
output_format = json
editor = nvim
```

Precedence: CLI flag > environment variable > `cli.ini` > built-in
default. A `cli.ini` setting always loses to an explicit flag.

Only the keys above are honoured. Other keys (for example `url`,
`token`, `project` or `config_path`) are ignored with a warning; pass
those as flags or store them in a profile.

#### `[profile:NAME]` sections: CLI-scoped profiles

`cli.ini` accepts the same `[profile:NAME]` sections as `auth.ini` and
supports the same fields:

| Key | Required | Notes |
|-----|----------|-------|
| `url` | yes for JWT, optional for IAM | API base URL |
| `type` | optional | `jwt` (default) or `iam` |
| `project` | IAM only | Project ID |
| `token` | optional | API bearer token |

The two files are merged at load time and `auth.ini` wins on conflict.

What `cli.ini` is for: profiles (or any field, including `token`) you
want only the `contree` CLI to see. The CLI merges `cli.ini` with
`auth.ini`. Other contree-related tooling that talks to the API
directly (the SDK, the MCP server) reads only `auth.ini`. Use
`cli.ini` when you need a profile that should be invisible to those
direct-API consumers, or to keep `auth.ini` minimal and shared.

Example, CLI-only profile alongside the shared one:

```ini
# ~/.config/contree/auth.ini  (read by CLI + SDK + MCP, mode 0600)
[DEFAULT]
profile = default

[profile:default]
url = https://contree.dev
token = eyJhbGciOi...
```

```ini
# ~/.config/contree/cli.ini  (read only by the CLI)
[profile:cli-sandbox]
url = https://staging.contree.dev
token = eyJhbGciOi...different
```

The active profile is still selected by the `profile` key in
`[DEFAULT]` of `auth.ini` (or by `--profile` / `$CONTREE_PROFILE`).

## Environment variables

Read at runtime by any command:

| Variable | Description |
|----------|-------------|
| `CONTREE_HOME` | Data directory (default `$XDG_CONFIG_HOME/contree`, or `~/.config/contree`) |
| `XDG_CONFIG_HOME` | XDG base config dir, used to derive the default `CONTREE_HOME` |
| `CONTREE_PROFILE` | Active profile name (selects which profile commands use) |
| `CONTREE_SESSION` | Explicit session key (overrides auto-generated) |

Read only by `contree auth` (registration-time fallbacks for omitted flags):

| Variable | Used for |
|----------|----------|
| `CONTREE_TOKEN` / `NEBIUS_API_KEY` | `--token` |
| `CONTREE_URL` | `--url` |
| `CONTREE_PROJECT` / `NEBIUS_AI_PROJECT` | `--project` |

## Resolution precedence

For token, URL, and project at runtime:

1. CLI flag (`--token`, `--url`, `--project`) — overrides profile for the
   current invocation only
2. Saved profile field
3. Built-in default URL for IAM: `https://api.tokenfactory.nebius.com/sandboxes`

Environment variables are not consulted at runtime; to refresh credentials
from environment variables, run `contree auth` (which reads
`CONTREE_TOKEN` / `NEBIUS_API_KEY`, `CONTREE_URL`, and `CONTREE_PROJECT` /
`NEBIUS_AI_PROJECT` as fallbacks for the corresponding flags).

For profiles:

1. `-p` / `--profile` flag
2. `CONTREE_PROFILE` environment variable
3. `profile` key in config `[DEFAULT]` section
4. Falls back to `default`

---

See {doc}`/commands/auth` for the full auth command reference, or
{doc}`/commands/index` for all commands.
