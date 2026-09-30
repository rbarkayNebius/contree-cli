---
icon: code-branch
---

# Sessions, Branches & Rollback

Sessions track the current image and its history as you run commands. Every
non-disposable `contree run` produces a new image, and the session records
the chain. Sessions also support **branching** and **rollback** for
experimentation.

## Session key

Every session is identified by a **session key** -- an arbitrary string.
The CLI computes one automatically so that each terminal window gets its
own session without any extra setup.

The auto-generated key is a deterministic UUID5 derived from three values:

| Component | Source | Purpose |
|-----------|--------|---------|
| `profile` | Active config profile name | Isolates sessions per profile |
| `ppid` | Parent process ID (`os.getppid()`) | The shell that launched the CLI |
| `tty` | TTY device of stdin (`os.ttyname()`) | Distinguishes terminal windows |

**In practice this means:**

- Open a new terminal tab -- new `ppid` + `tty` -- **new session**.
- Run `contree` commands in the same terminal -- same `ppid` + `tty` --
  **same session** (resumes where you left off).
- Switch profiles -- different `profile` -- **new session**.

## Viewing session state

::::{tab-set}
:class: no-sync

:::{tab-item} CLI
```bash
contree session            # show current session info
contree session list       # list all sessions
contree session show       # show full history DAG
```
:::

:::{tab-item} Shell
```text
contree session
contree session list
contree session show
```
:::
::::

## Branching

Create a branch to experiment without affecting the main line:

::::{tab-set}
:class: no-sync

:::{tab-item} CLI
```bash
contree session branch experiment
contree session checkout experiment
contree run apt-get install -y curl
```
:::

:::{tab-item} Shell
```text
contree session branch experiment
contree session checkout experiment
apt-get install -y curl
```
:::
::::

Not happy? Switch back:

::::{tab-set}
:class: no-sync

:::{tab-item} CLI
```bash
contree session checkout main
```
:::

:::{tab-item} Shell
```text
contree session checkout main
```
:::
::::

Branches share history entries -- creating a branch just creates a new
pointer at the current position.

Create a branch from another branch:

::::{tab-set}
:class: no-sync

:::{tab-item} CLI
```bash
contree session branch hotfix --from main
```
:::

:::{tab-item} Shell
```text
contree session branch hotfix --from main
```
:::
::::

List branches (`*` marks the active one):

::::{tab-set}
:class: no-sync

:::{tab-item} CLI
```bash
contree session branch
```
:::

:::{tab-item} Shell
```text
contree session branch
```
:::
::::

## Rollback

Move the branch pointer in the history chain. The argument distinguishes
absolute jumps from relative navigation:

| Argument | Meaning |
|---|---|
| _(none)_ | Back one entry (default) |
| `-- -N` | Back N entries (the `--` stops argparse from parsing `-N` as a flag) |
| `+N` | Forward N entries |
| `N` (bare positive) | **Absolute** jump to history id `N` |

::::{tab-set}
:class: no-sync

:::{tab-item} CLI
```bash
contree session rollback           # back one entry
contree session rollback -- -3     # back three entries
contree session rollback +1        # forward one entry
contree session rollback 42        # absolute jump to history id 42
```
:::

:::{tab-item} Shell
```text
contree session rollback
contree session rollback -- -3
contree session rollback +1
contree session rollback 42
```
:::
::::

:::{warning}
A bare positive number is an **absolute** history id, not "back N steps".
Use `--` followed by a negative number for relative back-navigation.
Inspect with `contree session show` first to avoid surprise jumps.
:::

The history entries still exist and can be recovered by creating a branch at
a specific point.

## Starting a fresh session

Because the auto-generated key is deterministic, the same terminal always
resumes the same session. Use `--new` (`-N`) to start a fresh session:

::::{tab-set}
:class: no-sync

:::{tab-item} CLI
```bash
# bash / zsh
eval $(contree use -N tag:python:3.11-slim)

# fish
eval (contree use -N tag:python:3.11-slim)
```

Without `eval`, the new session is **not active** until you export the
printed variable into your shell. You can also copy-paste the `export`
(or `set -gx`) line that `contree use` prints.
:::

:::{tab-item} Shell
```text
contree use -N tag:python:3.11-slim
```

Inside the interactive shell, no `eval` is needed -- the new session is
activated automatically.
:::
::::

You can also set `CONTREE_SESSION` to any string:

```bash
export CONTREE_SESSION=tutorial
contree use tag:python:3.11-slim
```

Unset it to go back to the automatic key:

```bash
unset CONTREE_SESSION
```

## Sharing a session across terminals

`contree use` prints the session key. Export it in another terminal to
attach to the same session:

```bash
# Terminal 1
contree use tag:ubuntu:latest
# output: export CONTREE_SESSION=<key>

# Terminal 2 -- paste the line above
export CONTREE_SESSION=<key>
contree run ls /     # operates on the same session
```

:::{note}
This pattern is CLI-only. The interactive shell manages sessions internally
and does not require manual session key export.
:::

## Storage

Session data is stored in a per-profile SQLite database at
`~/.config/contree/cli/sessions/{profile}.db`. Override the data
directory with `CONTREE_HOME`:

```bash
export CONTREE_HOME="$HOME/.local/share/contree-data"
```

`CONTREE_HOME` holds your credentials, so point it at a directory only
you can write to. Never use a fixed name under a shared directory such
as `/tmp`: another user could create it first. The CLI refuses to use a
`CONTREE_HOME` (or files in it) owned by another user or writable by
others.

---

You can experiment freely with branches. Next: {doc}`files`.
