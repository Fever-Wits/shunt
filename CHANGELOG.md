# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [CalVer](https://calver.org/) (`YYYYMMDDHH`).

## [2026080707] — 2026-08-07

### Added

- **`Grep` and `Glob` are warned about too.** They read the same **local** disk as `Read`
  while the session feels remote, and until now they did it in silence. The gap is mostly
  an **agent's**: a person searching a machine types `grep` or `find` into bash, which the
  hook redirects correctly — an agent reaches for the `Grep` tool instead, far more often
  than for `Read`, and reads local hits as facts about the far machine.

  **This changes the hook registration.** The matcher is now
  `Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob` — see the README. Keeping
  the old one leaves the redirection working exactly as before and simply loses these two
  warnings; `shunt install` now prints the wider line. The tuple behind it was renamed
  `FILE_TOOLS` → `LOCAL_DISK_TOOLS`, because searching is not editing a file.

  The warning is still **one per host per session**, now shared by all seven tools: a line
  on every `Grep` call would become wallpaper, and wallpaper is silent exactly when it
  should speak. So the single line names both ways out — remote file →
  `shunt read/edit`, remote search → `shunt run @host "grep -rn PATTERN /path"`.

### Fixed

- **Documentation: the CLI does not share the session's remote `cwd`.** The `shunt get`
  entry said its default destination `.` was "the remote cwd". It is not: the per-session
  directory lives in a state file only the hook reads, so every `shunt run` / `read` /
  `edit` / `get` starts in the ssh **login** directory (usually `$HOME`). Nothing changed
  in the code — the promise did. Give the CLI absolute paths.

## [2026080623] — 2026-08-06

Five of these were **silent**: they answered `ok`, or said nothing at all, while doing
something other than what was asked — on other people's files and other people's
machines. Two of them change behaviour you may have relied on; both are called out below.

### Fixed

- **`shunt edit` no longer damages a file it reports as edited.** The helper decoded the
  file (`errors="replace"`), edited the *text* and wrote the text back. So every byte that
  was not valid UTF-8 came back as U+FFFD — one latin-1 character in a comment was enough
  to corrupt a config — and a file with mixed line endings was converted **whole**. Both
  were reported as `{"status": "ok", "verified": true}` with a diff showing only the line
  you asked about, because the diff was computed *before* the conversion.

  The match and the replacement now happen on the **raw bytes**: nothing outside the
  matched region is rewritten, and the diff is computed from the bytes on disk and the
  bytes about to be written. Line-ending tolerance is unchanged in effect — the *needle*
  is retried as all-LF and as all-CRLF, and `normalized: true` still means "matched in a
  variant" — but the file is no longer rewritten into another style. The honest edge: the
  needle arrives as JSON and can only be UTF-8, so a needle that is not returns
  `not_found` instead of a guess; for latin-1 **text**, use `checkout`/`commit`, which
  never decode.

- **A session routed to a host that no longer resolves now runs nothing.** ⚠ **Behaviour
  change.** Previously a renamed alias or a broken `shunt.toml` made the hook fall back to
  running the command **locally** — while `@status` still said REMOTE. A `rm -rf
  /var/log/*` meant for a server deleted the local one. The hook cannot raise (a traceback
  in front of every bash command is worse than anything it would report), so it takes the
  third way it already uses for an unknown `@alias`: the command is replaced by the reason
  nothing ran — `[shunt] cannot resolve @web-01 — command NOT run …`.

- **A failed `checkout` no longer destroys the local file it was refreshing.** The pull
  opened the local path for writing, which truncates it the moment the process starts —
  before ssh has said a word — and then unlinked it when ssh failed. Checking a file out
  again over an unreachable host therefore threw away every uncommitted edit in it. The
  pull now lands in a `.part` file beside the target and is moved into place only on
  success.

- **`shunt edit` exits non-zero when the edit did not happen.** ⚠ **Behaviour change** for
  anything reading its exit code. The helper answers in JSON and always exits 0 —
  `not_found`, `ambiguous` and `conflict` included — and the CLI passed ssh's code straight
  back, so `shunt edit … && deploy` deployed an unedited file. The code now follows the
  status: `0` only for `ok`. A transport failure keeps ssh's own code. The JSON still goes
  to stdout, unchanged, so the reason stays readable.

- **`--dry-run` is honoured on the `--stdin` path too.** It was read only on the OLD/NEW
  path, so `shunt edit @host <file> --stdin --dry-run` **wrote** — with a flag on the
  command line asking it not to. It may only add safety: a payload that already asks for a
  dry run is never turned into a write.

- **The ControlMaster socket is keyed on the ssh user as well** (`%r@%h:%p`, the shape the
  CLI already used). Two aliases pointing at one machine with different accounts —
  `deploy@web-01` and `root@web-01`, which the config allows — shared the first one's
  master connection, so the second ran as the **wrong account**, silently, with entirely
  plausible output.

- **The audit log counts commands, not lines.** A multi-line command was written raw, so
  one command became several lines — and every reader of the log counts lines: the trimmer
  dates its cut from the first ten characters of one, `shunt log -n N` showed N of them.
  A continuation line starting with a space fell out of a cut while one starting with a
  letter survived, so a **kept** command lost part of its body and the fragments passed for
  records of their own. Commands are now folded onto one line on the way in (`\n` → `\\n`)
  and unfolded by `shunt log`; both trim cuts move whole records. Logs written before this
  are read correctly too: a line without a date belongs to the record above it.

- **One unreadable line no longer disarms the trimmer forever.** The cut date was parsed
  from the oldest line, and the exception was swallowed by the fire-and-forget wrapper — so
  a single torn line stopped every future trim, and the log grew past its ceiling without a
  word. The parse now yields `None` and the size cut does the freeing; it drops from the
  front, so the damaged line is the first to go.

### Added

- **The CLI writes to the audit log too.** `run`, `edit`, `cp`, `bg`, `get` and `commit`
  each append one record (`sid=cli`, the subcommand in brackets: `:: [run] uptime`). The
  hook recorded every redirected bash command while the CLI recorded nothing — and
  `shunt run` is the path recommended to agents, which made the recommended path the
  unaudited one. Read-only subcommands stay out: they bring something back rather than
  sending something out. `shunt log` shows both halves together.

## [2026080614] — 2026-08-06

### Added

- **`shunt run @host <cmd>`** — one command on a host, without a session.

  ```bash
  shunt run @web-01 hostname
  shunt run @web-01 "ls /etc | wc -l"       # quoted → the pipe runs on the server
  ```

  **Why:** the hook covers **interactive** bash — it needs a session to know where that
  session is routed. A script, a cron job or a spawned sub-agent has no mode of its own,
  so until now the only way to make an agent work on another machine was to leave the
  session in remote mode and let the agent inherit it *silently* — the very trap the
  warnings below are about. `run` gives somewhere to stand instead of only something to
  avoid.

  Quoting: a **single** argument passes through verbatim, so pipes, redirects and `$(…)`
  survive; **several** arguments are re-quoted, so `shunt run @h echo "a b"` stays two
  words on the far side. The remote exit code is passed through, not swallowed.

- **Warnings at the boundary of the mode.** The hook now also sees `Agent`, `Read`,
  `Write`, `Edit`, `MultiEdit` and `NotebookEdit`, and says out loud that the mode does
  **not** cover them: only `Bash` is ever rewritten. A spawned agent is warned on every
  spawn (each one inherits the routing anew); a file tool once per host (switching hosts
  re-arms it, `@local` clears it).

  It **never blocks** — working remotely with a local file is legitimate as often as it
  is a mistake; only the silence was the defect. The branch is fail-open: an error inside
  it can never break someone else's tool call.

  **This changes the hook registration.** The matcher is now
  `Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit` — see the README. Keeping the old
  `Bash`-only matcher leaves the redirection working exactly as before and simply loses
  the warnings; `shunt install` now prints the wider line.

- **`shunt` with no arguments introduces itself** — an "I want to… → reach for" map
  instead of a usage line. shunt is not an MCP server; nothing announces it to whoever
  reaches for it, so this is its only way to explain itself in one call without loading
  documentation. Asking (`shunt help`, `-h`, `--help`) exits **0**; the bare call prints
  the same map but exits **2**, because a script that dropped its subcommand must not
  silently "succeed".

- **The audit log trims itself**, configured by a new `[audit]` section:

  ```toml
  [audit]
  trim_at_mb  = 100    # trim only once the log grows past this
  drop_months = 2      # then the OLDEST months go — the rest of the history stays
  ```

  The log is an **archive** and trimming is a **fuse**, not a retention policy: size is
  the trigger, and age is only the unit in which room gets freed. A log holding five
  years loses its first two months and keeps the rest. If age can free nothing — the file
  is not old but *fast*, a month's worth of lines written in an hour — the oldest lines
  go until it fits, because otherwise the fuse would fail in exactly the case it exists
  for. Bad or missing values fall back to the defaults above; a setting must never be the
  reason a command fails.

### Fixed

- **`shunt cp` now gets the same ssh options as every other subcommand.** They were
  written twice — once for `ssh`, once inside `cmd_cp` for `rsync -e` — and the copy had
  fallen behind: it lacked `BatchMode=yes` (so `cp` could sit forever on a password
  prompt inside a script) and `ControlMaster`/`ControlPersist` (so it opened a fresh
  connection every time, for nothing). There is now one `ssh_opts()` both read from.

- **`shunt commit` no longer stops at a stale manifest entry.** It walks every checked-out
  file; when one entry named a host that is no longer configured, the whole run died — so
  a single outdated entry silently dropped every file queued behind it, and the ones
  already pushed gave no hint that the rest never went. It now reports that entry, sets a
  non-zero exit code and carries on. The failure stays visible; it just stops taking
  hostages. A manifest entry outliving its host is ordinary, not exceptional.

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
