# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [CalVer](https://calver.org/) (`YYYYMMDDHH`).

## [2026080613] — 2026-08-06

### Added

- **`~/.config/shunt/shunt.toml`** — a TOML config that replaces the `hosts` file:

  ```toml
  key = "~/.ssh/id_ed25519_shunt"        # default identity for every host below

  [hosts]
  web-01  = "user@203.0.113.10"
  special = { target = "user@203.0.113.30", key = "~/.ssh/id_ed25519_special" }
  ```

  A bare string is the target; the inline table adds a per-host `key`, which wins over
  the top-level default. See [`shunt.toml.example`](shunt.toml.example).

  **Why:** the address lived in `hosts` while the identity for it lived in
  `~/.ssh/config` — one piece of knowledge in two files, which drifts apart in silence.
  Add a machine on one side, leave its key on the other, and access is gone without a
  single message. Owning the config removes the dependency on someone else's file, and
  `tomllib` has been in the standard library since 3.11, so this costs no new dependency.

- **`src/shunt/config.py`** — the only module that knows the config format. Both the CLI
  and the hook resolve hosts through it; each passes its own config directory, so the
  knowledge of the *format* lives in the module and the knowledge of the *location* stays
  with the caller.

### Changed

- `shunt install` now writes to `shunt.toml` instead of appending a `hosts` line. Still
  idempotent — an entry with the same alias is replaced, never duplicated — and it leaves
  the rest of the file, comments included, untouched. A `--key` is written down as you
  typed it (`~/…` stays `~/…`, so the file travels between machines) and expanded only
  when handed to ssh.
- `shunt hosts` prints the **resolved** hosts and the file they came from, rather than
  dumping raw file text that may now be in either of two formats.
- A broken config is loud: the CLI dies with the reason instead of resolving to no hosts.
  The hook does the opposite on purpose — it falls back to running **locally**, because a
  traceback in front of every bash command would be worse than staying home.
- `hosts.example` is gone, replaced by `shunt.toml.example`. The old format is still
  read; it is simply no longer the shape recommended to someone writing a config today.

### Backwards compatibility

With no `shunt.toml`, the old `~/.config/shunt/hosts` file is read exactly as before and
everything keeps working. shunt says once, **on stderr**, where the new place is — stdout
is a protocol for both callers (the hook writes JSON there, the CLI passes remote output
through), so a notice may never go that way. **Nothing is migrated automatically**: your
config file is yours, and moving it is your move, not the tool's. If both files exist,
`shunt.toml` is the one that counts.

## [2026080610] — 2026-08-06

**Breaking:** the `daemon` transport is gone. shunt now speaks ssh, and only ssh.

### Removed

- **The `daemon` transport** — `daemon.py`, its systemd unit, the inline TCP client
  inside the hook, and the token it needed; along with `shunt install --mode
  secure|nonsecure` and `--port`, and the `SHUNT_TOKEN` / `SHUNT_PORT` / `SHUNT_HOST`
  environment variables.

  **Why:** the daemon existed for speed — to avoid paying an ssh handshake on every
  command. ssh here runs with `ControlMaster`, which amortizes that handshake to
  milliseconds after the first call (measured: ~0.24–0.36 s per command without it,
  ~0.01 s with it once the master connection is up). The problem the daemon was built
  to solve is already solved by ssh — with no open port, no shared token, and nothing
  installed on the server. On top of that, every file operation (`read`, `edit`,
  `checkout`, `commit`, `cp`, `bg`, `get`) required ssh anyway; the daemon carried
  only the redirected bare bash. So: not "nobody used it" — ssh caught up and passed
  it.

- **The daemon hardening guide in `SECURITY.md`** (restricting the port, ssh tunnel,
  Tailscale, token rotation) — it protected a component that no longer exists. What in
  it was true of ssh as well stayed: a least-privileged remote account, a dedicated key
  you can revoke on suspicion, and the fact that the command text lands in the agent
  transcript and the audit log.

### Changed

- `shunt install <user>@<host> [--alias A] [--key PATH]` — no `--mode`, no `--port`.
- A host is still `<alias> ssh <target> [key=PATH]` in `~/.config/shunt/hosts`. The
  `ssh` word remains required: a line naming any other transport is **not** treated as
  a host, so an old `daemon` line fails loudly (`unknown host: <alias>`) instead of
  silently becoming some other destination.
- `@<alias>` now reports `REMOTE → <alias> (<target>)` — the transport dropped out of
  the message, there being only one.

### If you were running the daemon

1. Re-register the host over ssh:
   `shunt install user@<host> --alias <alias> [--key ~/.ssh/id_ed25519]`.
2. Locally, delete `~/.config/shunt/token` and `~/.config/shunt/token.<alias>`.
3. On the server: `systemctl disable --now shunt-daemon`, then remove
   `/etc/systemd/system/shunt-daemon.service`, `/opt/shunt/` and `/etc/shunt/`.

A session that was already routed to a daemon host when you upgraded resolves nothing
and therefore runs **locally** again — the hook's standing behaviour for a target it
cannot resolve. Re-issue `@<alias>` after re-registering the host, and `@status` will
confirm where bash is going.

**The old tags stay.** `v2026062322` and `v2026062407` still ship the daemon and are
not going anywhere — if you need it, stay on the earlier release. The past is not
erased; it just stops being carried forward.

---

## [2026062407] — 2026-06-24

### Added

- **`checkout` / `commit`** — edit remote files with native local tools and push
  back atomically. `checkout @host /path` pulls the file into a local sandbox and
  records the SHA; `commit` writes it back, refusing if the remote changed since
  checkout (optimistic SHA-lock). Supports `--list` and `--abandon`.

### Fixed

- File-descriptor leak in helper reads (checkout path cleanup on failure now
  closes the output file before attempting `unlink`).

### Security

- Path-traversal guard on `checkout`: a remote path containing `..` that would
  escape the `~/.config/shunt/checkouts/` sandbox is rejected before any ssh
  call is made.

---

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
