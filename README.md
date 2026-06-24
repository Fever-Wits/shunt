# shunt

**Transparent remote hands for an AI coding agent.**

`shunt` is a Claude Code `PreToolUse(Bash)` hook that transparently redirects the
agent's bash commands to a chosen remote machine — the agent keeps running plain
`ls`, `grep`, `make`, and each one executes on the host you've switched to, with
per-session working directory kept across commands. Alongside the hook ships a small
`shunt` CLI for the things bash alone can't do well over a wire: `read`, `edit`,
`cp`, `bg`, `get`, `log`, `checkout`, and `commit`.

What you can do: **edit remote files** with your normal local tools · **run commands transparently** on a remote host · **long tasks** in the background with `bg` + `--status` to monitor.

Two transports: **ssh** (secure — no open ports, no shared token) and **daemon**
(a token-guarded TCP listener, only for a trusted LAN).

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
        "matcher": "Bash",
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

**Restart the Claude Code session** after editing `settings.json` — hooks are read
at session start.

---

## Quickstart

First, register a host (or hand-edit your hosts file — see [Configuration](#configuration)):

```bash
shunt install user@203.0.113.10 --alias web-01 --key ~/.ssh/id_ed25519
```

This adds an ssh hosts entry, prints the hook line, and runs a connection test.

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
@web-01          →  [shunt] mode: REMOTE → web-01 (user@203.0.113.10, ssh)
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
| `@<alias>`  | Route this session's bash to `<alias>` → `[shunt] mode: REMOTE → <alias> (<target>, <transport>)` |
| `@local`    | Stop redirecting; bash runs locally again → `[shunt] mode: LOCAL` |
| `@status`   | Show current routing → `[shunt] REMOTE → <alias>` or `[shunt] LOCAL` |

While routed, every bash command runs on the remote host. The hook keeps the
working directory per session (so `cd foo` then `ls` works as expected) and appends
each routed command to `~/.config/shunt/audit.log`.

`shunt ...` commands themselves always run locally — the CLI does its own
transport, so it is never redirected.

### `shunt hosts`

Print the configured hosts.

```bash
$ shunt hosts
web-01   ssh     user@203.0.113.10    key=~/.ssh/id_ed25519
lan      daemon  203.0.113.50:8766
```

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
otherwise the edit is refused. The edit is verified after write (SHA-256), written
atomically, and CRLF line endings are preserved.

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
  quoting). Recognized keys: `old`, `new`, `expected`, `base_sha` (optimistic lock):
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
code. Implemented with **`systemd-run`** at the system level → **requires root /
sudo on the remote host**. Prints `JOB=<unit>`.

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

Download a URL **on the server itself** (`wget -b`, in the background). Defaults the
destination to the remote cwd (`.`).

```bash
$ shunt get @web-01 https://example.com/big.iso /opt/downloads
downloading in background; progress: shunt read @web-01 /tmp/shunt-wget-….log or tail -f …
```

### `shunt log [-n N]`

Tail the local audit log of redirected commands (`~/.config/shunt/audit.log`).
Default 50 lines; `-n N` for the last `N`.

```bash
$ shunt log -n 20
2026-06-23T10:15:02 sid=… host=web-01 :: hostname
2026-06-23T10:15:09 sid=… host=web-01 :: make -j8 all
```

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

### `shunt install <user>@<host> [--mode secure|nonsecure] [--alias A] [--key PATH] [--port P]`

Register a host and print the hook line.

```bash
# secure (default) — ssh + ControlMaster; no open ports, no token
shunt install user@203.0.113.10 --alias web-01 --key ~/.ssh/id_ed25519

# nonsecure — uploads the daemon, generates a token, brings up a systemd unit
shunt install user@203.0.113.50 --alias lan --mode nonsecure --port 8766
```

- `--mode secure` (default) — writes an ssh hosts line and tests the connection.
- `--mode nonsecure` — generates a token, `scp`s `daemon.py` to `/opt/shunt/`,
  writes `/etc/shunt/daemon.env` (chmod 600), installs and starts the
  `shunt-daemon` systemd unit, and stores the token locally (chmod 600). **Opens a
  TCP port on the server** — see [Security](#security).
- `--alias A` — host alias (defaults to the IP with dots → dashes).
- `--key PATH` — ssh identity file (used for both ssh and scp).
- `--port P` — daemon port for nonsecure mode (default `8766`).

---

## Configuration

### `~/.config/shunt/hosts`

One host per line: `<alias>  <transport>  <target>  [key=PATH]`. Lines starting
with `#` and blank lines are ignored.

```
# secure (ssh) — no open port, no token; target is user@host:
web-01   ssh     user@203.0.113.10    key=~/.ssh/id_ed25519

# nonsecure (daemon) — token-guarded TCP, trusted LAN only; target is host:port:
lan      daemon  203.0.113.50:8766
```

See [`hosts.example`](hosts.example). For the daemon transport, the token lives in
`~/.config/shunt/token` (and a per-alias `~/.config/shunt/token.<alias>` for
multi-daemon setups), both chmod 600 — written automatically by `shunt install
--mode nonsecure`.

### Environment variables

| Variable | Used by | Meaning |
|----------|---------|---------|
| `SHUNT_CONF` | CLI + hook | Config directory (default `~/.config/shunt`) |
| `SHUNT_HOST` | daemon | Bind address of the daemon (daemon default `127.0.0.1`; `shunt install --mode nonsecure` sets it to `0.0.0.0`) |
| `SHUNT_PORT` | daemon | Daemon listen port (default `8766`) |
| `SHUNT_TOKEN` | daemon | Shared token the daemon requires (constant-time compared) |
| `SHUNT_EDIT_MAX_BYTES` | edit helper | Max file size for `shunt edit` (default 64 MiB; larger → use `shunt cp` + local edit) |

---

## Security

`shunt` gives an AI agent a shell on another machine. Understand the trust boundary
before you use the nonsecure transport.

- **secure (ssh)** — the default. Transport is plain ssh + ControlMaster:
  **zero open ports, zero shared token**, fully encrypted. Use this for anything
  reachable from an untrusted network.
- **nonsecure (daemon)** — as installed, the daemon binds **`0.0.0.0:<port>`**,
  i.e. an **open shell port on your LAN**: anyone on the network who has the token
  gets a shell as the daemon's user. Only run this on a **trusted LAN**. The systemd
  unit ships with a commented-out `User=`/`Group=` so you can drop it to a non-root
  user; for anything beyond a trusted LAN, prefer an ssh tunnel / Tailscale and
  `--mode secure`.

See [SECURITY.md](SECURITY.md) for the full threat model.

---

## How it works

The hook returns an `updatedInput` from `PreToolUse` so the rewritten command runs
in Claude Code's normal sandbox — an official, documented mechanism. The ssh
transport keeps cwd in a per-session remote state file and captures exit codes via a
`trap EXIT`; the daemon transport is a self-contained inline TCP client with a
random per-connection end marker.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the details.

---

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT — see [LICENSE](LICENSE).
