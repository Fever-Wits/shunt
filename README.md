# shunt

**Transparent remote hands for an AI coding agent.**

`shunt` is a Claude Code `PreToolUse` hook that transparently redirects the
agent's bash commands to a chosen remote machine — the agent keeps running plain
`ls`, `grep`, `make`, and each one executes on the host you've switched to, with
per-session working directory kept across commands. Alongside the hook ships a small
`shunt` CLI for the things bash alone can't do well over a wire: `run`, `read`, `edit`,
`cp`, `bg`, `get`, `log`, `checkout` and `commit` — plus `hosts`, `install`, and a bare
`shunt` that prints its own map.

What you can do: **edit remote files** with your normal local tools · **run commands transparently** on a remote host · **long tasks** in the background with `bg` + `--status` to monitor.

One transport: **ssh** + `ControlMaster` — no open ports, no shared token, nothing
of shunt's installed on the server.

---

## Install

`shunt` is a zero-dependency Python package (stdlib only, requires Python 3.11+).
Install it into an isolated environment with [pipx](https://pipx.pypa.io) or
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/Fever-Wits/shunt.git
cd shunt

pipx install .          # → puts a `shunt` command on your PATH
# or:
uv tool install .
```

### Wire up the hook

The CLI is only half of shunt. The transparent redirection comes from the
`pretool.py` hook. Point Claude Code at it by adding this to your
`~/.claude/settings.json` under `hooks.PreToolUse`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /ABSOLUTE/PATH/TO/shunt/src/shunt/pretool.py"
          }
        ]
      }
    ]
  }
}
```

Use the absolute path to `pretool.py` in your clone (or wherever the package was
installed). `shunt install` will print the exact line for you.

**The matcher is wider than `Bash` on purpose.** Only `Bash` is ever rewritten; the
other tools are matched so the hook can *warn* that the mode does not cover them — see
[Where the mode stops](#where-the-mode-stops). Register only `Bash` and you keep the
redirection but lose the warnings.

**Restart the Claude Code session** after editing `settings.json` — hooks are read
at session start.

---

## Quickstart

First, register a host (or hand-edit your config file — see [Configuration](#configuration)):

```bash
shunt install user@203.0.113.10 --alias web-01 --key ~/.ssh/id_ed25519
```

This adds a host entry to `~/.config/shunt/shunt.toml`, prints the hook line, and runs a
connection test.

Now, **inside Claude Code**, smoke-test the hook. Run:

```
@status
```

If the hook is wired correctly you'll see:

```
[shunt] LOCAL
```

That `[shunt] LOCAL` confirms install completion. Switch to your host and your
bash now runs there:

```
@web-01          →  [shunt] mode: REMOTE → web-01 (user@203.0.113.10)
hostname         →  web-01
pwd              →  /home/user        (cwd is remembered per session)
@local           →  [shunt] mode: LOCAL
```

---

## Usage

### Switching where bash runs (the hook)

These are typed as bare bash commands inside Claude Code; the hook intercepts them.
Routing is **per session**, so parallel Claude sessions don't clash.

| Command     | Effect |
|-------------|--------|
| `@<alias>`  | Route this session's bash to `<alias>` → `[shunt] mode: REMOTE → <alias> (<target>)` |
| `@local`    | Stop redirecting; bash runs locally again → `[shunt] mode: LOCAL` |
| `@status`   | Show current routing → `[shunt] REMOTE → <alias>` or `[shunt] LOCAL` |

While routed, every bash command runs on the remote host. The hook keeps the
working directory per session (so `cd foo` then `ls` works as expected) and appends
each routed command to `~/.config/shunt/audit.log`.

`shunt ...` commands themselves always run locally — the CLI does its own
transport, so it is never redirected.

Two behaviours worth knowing before you rely on the transparency:

- **An interrupted command is not killed on the far side.** No pty is allocated, so
  nothing sends the remote process SIGHUP — it keeps running there until it finishes on
  its own. Allocating one (`ssh -tt`) was measured and rejected: it hangs every pager and
  stdin reader, merges stderr into stdout, and colours the output. Start long work with
  `shunt bg` instead, and stop it with `shunt bg @host --stop JOB`.
- **If the routed alias stops resolving** — renamed, deleted, or the config broke — the
  hook runs **nothing**. It cannot raise (a traceback in front of every bash command is
  worse than anything it would report), and it will not quietly run the command here:
  the session still says REMOTE, so `rm -rf /var/log/*` meant for a server would hit your
  own disk. Instead the command is replaced by the reason it did not go:
  `[shunt] cannot resolve @web-01 — command NOT run …`. Fix the config, or `@local`.

### Where the mode stops

**The mode covers `bash` and nothing else.** File tools (`Read`, `Write`, `Edit`,
`MultiEdit`, `NotebookEdit`) and search tools (`Grep`, `Glob`) keep working on the
**local** disk while the session feels remote, and a spawned agent **inherits** the mode
and runs its own commands on the far machine — reading absent local files as facts about
the world. Both failures are silent, so the hook says them out loud instead: it warns
when an agent is spawned in remote mode, and once per host when one of those tools runs
there.

The search tools are in that list for the **agent**: a person looking through a machine
types `grep` or `find` into bash, and the hook sends it to the right place. An agent
reaches for the `Grep` tool instead — far more often than for `Read` — and reads local
hits as facts about the far machine.

It **never blocks** — working remotely with a local file is legitimate as often as it is
a mistake; only the silence was the defect. For a remote file use `shunt read` /
`shunt edit` / `shunt checkout`; for a remote search,
`shunt run @host "grep -rn PATTERN /path"`; for an agent that genuinely must work on the
far machine, give it `shunt run @host …` explicitly rather than leaving the session in
remote mode.

One warning per host per session, shared by all of those tools: `Grep` is called often
enough that a line per call would become wallpaper — and wallpaper is silent exactly when
it should speak. That is why the one line names both remedies.

### The CLI does not share the session's `cwd`

The hook keeps a working directory per session for `@host` mode; the CLI never reads that
state file, so every `shunt run` / `read` / `edit` / `get` starts in the **ssh login
directory** (usually `$HOME`). Give absolute paths, and read `shunt get`'s default
destination (`.`) as that login directory — not as wherever the session last `cd`-ed.

### `shunt hosts`

Print the configured hosts.

```bash
$ shunt hosts
# /home/you/.config/shunt/shunt.toml
web-01       user@203.0.113.10  key=/home/you/.ssh/id_ed25519
web-02       user@203.0.113.20
```

The hosts as **resolved**, with the file they came from on the first line — not the raw
text, which may be in either supported format.

### `shunt run @host <cmd>` — for scripts, cron and agents

The `@<alias>` toggle works on **interactive** bash: the hook needs a session to know
where that session is routed. A script, a cron job or a spawned sub-agent has no mode of
its own, so `shunt run` executes one command explicitly and passes the output and the
exit code straight through.

```bash
shunt run @web-01 hostname
shunt run @web-01 "ls /etc | wc -l"       # quoted → the pipe runs on the server
```

Quoting: a **single** argument is handed over verbatim, so pipes, redirects and `$(…)`
survive; **several** arguments are re-quoted, so `shunt run @web-01 echo "a b"` stays two
words on the far side.

This is also the explicit path for an agent that must work on another machine — better
than leaving the session in remote mode and letting the agent inherit it silently (see
[Where the mode stops](#where-the-mode-stops)).

### `shunt read @host <file> [start:end]`

Show a remote file's content with line numbers (for orientation before an edit).
Optionally restrict to a line range.

```bash
$ shunt read @web-01 /etc/hostname
     1	web-01

$ shunt read @web-01 /var/log/app.log 100:120
   100	...
```

### `shunt edit @host <file> OLD NEW [--expected N] [--dry-run]`

Edit a remote file **by content** (not by line number), with the same semantics as
the built-in Edit tool: `OLD` must occur exactly once (or `--expected N` times),
otherwise the edit is refused. The edit is verified after write (SHA-256) and written
atomically. It is applied to the **raw bytes**: only the matched region is rewritten, so
line endings elsewhere in the file and bytes that are not valid UTF-8 come back exactly
as they were. The exit code follows the answer — **0 only when the file was changed** —
so `shunt edit … && deploy` is safe to write.

```bash
$ shunt edit @web-01 /opt/app/config.ini "debug = false" "debug = true"
{"status": "ok", "count": 1, "new_sha": "…", "verified": true, "diff": "…", …}
```

- `--dry-run` — show the diff and resulting SHA without writing:
  ```bash
  $ shunt edit @web-01 /opt/app/config.ini "old" "new" --dry-run
  {"status": "ok", "dry_run": true, "count": 1, "diff": "…", …}
  ```
- `--expected N` — require exactly `N` matches (default 1); a mismatch returns
  `{"status": "ambiguous", "count": …, "expected": …}`.
- `--stdin` — for multi-line edits, pass a JSON payload on stdin (avoids shell
  quoting). Recognized keys: `old`, `new`, `expected`, `base_sha` (optimistic lock).
  `--dry-run` works here too:
  ```bash
  echo '{"old":"line one\nline two","new":"replacement","expected":1}' \
    | shunt edit @web-01 /opt/app/main.py --stdin
  ```

### `shunt cp <src> <dst>`

`rsync` between local and remote — exactly one side must be `@host:/path`.

```bash
$ shunt cp ./build.tar.gz @web-01:/opt/app/
$ shunt cp @web-01:/var/log/app.log ./app.log
```

### `shunt bg @host <cmd> [--name LABEL]`

Run a long task on the remote host that survives disconnect and preserves its exit
code. Implemented with **`systemd-run`** transient units at the **system** level (no
`--user`, and shunt never invokes `sudo` itself) → the ssh user must be allowed to
create system units, which in practice means **root on that host**. This is the only
feature that needs it. Prints `JOB=<unit>`.

```bash
$ shunt bg @web-01 "make -j8 all" --name nightly-build
JOB=shunt-nightly-build
```

- `--name LABEL` — human-readable unit name (sanitized to `shunt-<label>`); without
  it a random unit name is generated.
- `shunt bg @host --list` — list `shunt-*` units.
- `shunt bg @host --status JOB` — last log lines + exit status of a job.
- `shunt bg @host --stop JOB` — stop a job and reset its failed state.

```bash
$ shunt bg @web-01 --list
$ shunt bg @web-01 --status shunt-nightly-build
$ shunt bg @web-01 --stop shunt-nightly-build
```

### `shunt get @host <url> [dest_dir]`

Download a URL **on the server itself** (`wget -b`, in the background). The default
destination (`.`) is the ssh **login** directory — the CLI does not share the session's
remote `cwd` (see [above](#the-cli-does-not-share-the-sessions-cwd)).

```bash
$ shunt get @web-01 https://example.com/big.iso /opt/downloads
downloading in background; progress: shunt read @web-01 /tmp/shunt-wget-….log or tail -f …
```

### `shunt log [-n N]`

The local record of what left this machine (`~/.config/shunt/audit.log`): bash the hook
redirected, and the CLI subcommands that reached a host (`sid=cli`, the subcommand in
brackets). Default the last 50 **records**; `-n N` for the last `N`. One command is one
record, however many lines it was typed on.

```bash
$ shunt log -n 20
2026-06-23T10:15:02 sid=… host=web-01 :: hostname
2026-06-23T10:15:09 sid=… host=web-01 :: make -j8 all
2026-06-23T10:16:41 sid=cli host=web-01 :: [edit] /opt/app/config.ini
```

The log is an **archive**, and trimming it is a **fuse**, not a retention policy: it is
left alone until it grows past `trim_at_mb`, and only then do the oldest `drop_months`
of history go — the rest stays. Both are configurable, see
[Configuration](#configuration). At any ordinary rate the ceiling is never reached.

### Edit remote files (`checkout` / `commit`)

`checkout` pulls a remote file to a local sandbox so you can edit it with normal
tools (Read, Edit, Write). `commit` pushes it back atomically with an optimistic
SHA-lock — if the remote changed since your checkout it refuses, avoiding a
blind overwrite.

```bash
shunt checkout @web-01 /opt/app/config.ini   # pulls file locally, prints local path
# … edit with your usual tools …
shunt commit                                  # pushes all pending checkouts back
```

- `shunt checkout --list` — show current checkouts (local path, remote `@host:/path`, base SHA).
- `shunt checkout --abandon <local_path>` / `shunt commit --abandon <local_path>` — drop the manifest entry without pushing (local file stays on disk).
- A **failed** checkout leaves the local file alone. The pull is written beside it and
  moved into place only once it has fully arrived, so checking a file out again over an
  unreachable host cannot destroy the edits you have not committed yet.

A bare `shunt commit` pushes **every** pending checkout and is per-entry, not
all-or-nothing: a conflict, an unreadable local file, or an entry whose host is no longer
configured is reported on its own line (`CONFLICT` / `SKIP`) and makes the exit code
non-zero, while the rest of the files still go.

### `shunt install <user>@<host> [--alias A] [--key PATH]`

Register a host and print the hook line: checks `python3` on the server, writes the host
into `~/.config/shunt/shunt.toml`, and tests the connection. Nothing is installed on the
server, and your `settings.json` is printed for you to edit, never edited for you.

```bash
shunt install user@203.0.113.10 --alias web-01 --key ~/.ssh/id_ed25519
```

- `--alias A` — host alias (defaults to the host part of `user@host` with dots → dashes).
- `--key PATH` — ssh identity file.

The entry is written **idempotently**: an existing entry with the same alias is replaced,
never duplicated, and the rest of the file — your comments included — survives.

---

## Configuration

### `~/.config/shunt/shunt.toml`

```toml
# The identity used for every host below, unless the host names its own.
key = "~/.ssh/id_ed25519_shunt"

[hosts]
web-01  = "user@203.0.113.10"
web-02  = "user@203.0.113.20"
special = { target = "user@203.0.113.30", key = "~/.ssh/id_ed25519_special" }
```

A bare string **is** the target (`user@host`); the inline table adds a per-host `key`,
which wins over the top-level default. With no `key` anywhere, ssh picks the identity
itself, as it always did.

Everything a host needs lives in this one file on purpose: an address kept here and its
identity over in `~/.ssh/config` breaks silently the day only one of them is edited —
machines added on one side, the identity left on the other, and access is gone without a
word. A broken config is loud rather than empty: the CLI says what is wrong with the file
instead of resolving to no hosts.

An optional `[audit]` section tunes the audit log:

```toml
[audit]
trim_at_mb  = 100    # trim only once the log grows past this
drop_months = 2      # then the OLDEST months go — the rest of the history stays
```

Size is the trigger; age is only the unit in which room gets freed. Should everything in
the file be recent — something wrote a month's worth of commands in an hour — the oldest
records go until it fits, because otherwise the fuse would fail in exactly the case it
exists for. Both cuts move whole records, never half a command. Bad or missing values fall back to the defaults above: a setting must never
be the reason a command fails.

See [`shunt.toml.example`](shunt.toml.example).

**The older `hosts` file still works.** If `shunt.toml` is absent, shunt reads
`~/.config/shunt/hosts` in the previous format — one host per line,
`<alias>  ssh  <target>  [key=PATH]` — and says once, on stderr, where the new place is.
Nothing is migrated for you; move when you want to, or don't. Should both files exist,
`shunt.toml` is the one that counts.

### Environment variables

| Variable | Used by | Meaning |
|----------|---------|---------|
| `SHUNT_CONF` | CLI + hook | Config directory (default `~/.config/shunt`) |
| `SHUNT_EDIT_MAX_BYTES` | edit + write helpers | Max file size shunt will **push** — `shunt edit` and `shunt commit` (default 64 MiB; larger → use `shunt cp` + local edit). It does **not** cap the pull direction: `shunt checkout` fetches whatever is there, so check the size of a big remote file first. |

---

## Security

`shunt` gives an AI agent a shell on another machine. That is the trust boundary:
whoever drives the agent can run anything on every host you add, with the rights of
the user in that host's target.

The transport is plain ssh + ControlMaster: **zero open ports, zero shared token**,
fully encrypted, nothing of shunt's installed on the server. ssh protects the
channel; it does not protect the machine from its legitimate user — so use a
dedicated key and the least-privileged account that can do the job.

See [SECURITY.md](SECURITY.md) for the full threat model.

---

## How it works

The hook returns an `updatedInput` from `PreToolUse` so the rewritten command runs
in Claude Code's normal sandbox — an official, documented mechanism. The ssh
transport keeps cwd in a per-session remote state file and captures exit codes via a
`trap EXIT`; `ControlMaster` reuses one multiplexed connection, so the handshake is
paid once and later commands feel local-fast.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the details.

---

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT — see [LICENSE](LICENSE).
