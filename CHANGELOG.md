# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [CalVer](https://calver.org/) (`YYYYMMDDHH`).

## [2026062322] — 2026-06-23

Initial public release. Transparent remote hands for an AI coding agent:
redirect the agent's bash to a chosen remote host via a Claude Code hook, with
no change to how the agent writes commands.

### Added

- **Transparent `@host` bash redirect via PreToolUse hook** (`pretool.py`).
  Switch routing per-session with `@<alias>` / `@local` / `@status`; bare bash
  commands are then rewritten to run on the selected host. Remote `cwd` is kept
  per-session via a state-file, so `cd` persists across commands.
- **Two transports**, configured per-host in `~/.config/shunt/hosts`:
  - `ssh` — **secure**: `ssh` + `ControlMaster` multiplexing. Zero open ports,
    zero shared token, encrypted; per-session + per-destination control socket.
  - `daemon` — **nonsecure**: TCP + token, fast on a trusted LAN
    (`daemon.py`, a stdlib `ThreadingTCPServer` with per-session `cwd`,
    constant-time token check, client-disconnect process-group kill).
- **`shunt` CLI** for operations the hook does not cover:
  - `read @host <file> [start:end]` — content with line numbers for orientation.
  - `edit @host <file> OLD NEW [--expected N] [--dry-run] | --stdin` — edit by
    content (see below).
  - `cp <src> <dst>` — `rsync` with one side `@host:/path`.
  - `bg @host <cmd> [--name LABEL] | --list | --status JOB | --stop JOB` —
    long-running jobs via `systemd-run` (survive disconnect, preserve exit code).
  - `get @host <url> [dest]` — background download (`wget -b`) on the server.
  - `log [-n N]` — tail of the local audit log.
  - `hosts` — show configured hosts; `install <user>@<host>` — provision a host
    (`--mode secure|nonsecure`).
- **Edit-by-content** (`edit_helper.py`, stdlib-only, runs on the remote side):
  `old → new` semantics like the built-in editor, with a uniqueness check
  (count-and-refuse on ambiguity), an **optimistic SHA-256 lock** (`base_sha`
  rejects a write if the file changed since it was read), **atomic write**
  (temp in the same directory → `fsync` → `os.replace` → dir `fsync`, preserving
  mode/owner), and **verify-after-write** (re-hash the file and confirm).
  CRLF normalization with original line-ending style preserved on write.

### Security

- **Rewrite-marker guard** — every redirected command is prefixed with a
  `#shunt-rewritten` marker; the hook refuses to rewrite an already-rewritten
  command, preventing double-redirection loops.
- **Fire-and-forget audit log** — every redirected command is appended to
  `~/.config/shunt/audit.log` (timestamp, session id, host, command); logging
  failures never block execution. Tail it with `shunt log`.
- **Per-alias token** — daemon mode stores a `token.<alias>` (chmod 600) used
  first, falling back to the shared `token` for single-daemon setups. The token
  reaches the inline client via an environment variable, not `argv`, so it is
  not visible in `ps` to other local users.
- **Edit size guard** — `edit_helper.py` refuses files over
  `SHUNT_EDIT_MAX_BYTES` (default 64 MiB), directing the caller to `shunt cp` +
  a local edit instead.
- Daemon defaults to binding `127.0.0.1` (LAN exposure is explicit opt-in via
  `SHUNT_HOST`), uses constant-time token comparison, and warns when run as root.
  Nonsecure mode is documented as trusted-LAN-only; untrusted networks should
  use the `ssh` transport (no token, no open port).
