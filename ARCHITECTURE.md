# shunt — Architecture

shunt gives an AI coding agent **transparent remote hands**: the bash commands
the agent already runs are redirected, unchanged, to a chosen remote machine.
The agent does not learn a new tool to "run things remotely" — it keeps writing
ordinary `Bash` calls, and a hook quietly re-routes them. A small companion CLI
covers the operations a bare bash redirect cannot express cleanly (reading,
content-addressed editing, file copy, background jobs).

The design goal is to ride a **documented, stable surface** of the agent host
(the `PreToolUse` hook and its `updatedInput` contract) instead of replacing or
wrapping the shell. That makes shunt independent of undocumented environment
behaviour and robust across agent-host upgrades.

---

## 1. Components

shunt is four Python modules with zero third-party dependencies (stdlib only).

| Module | Role | Where it runs |
|---|---|---|
| `pretool.py` | `PreToolUse` Bash hook — transparent execution by command rewrite | Local (agent host) |
| `cli.py` | the `shunt` CLI — read / edit / cp / bg / get / log / hosts / install | Local (agent host) |
| `daemon.py` | optional nonsecure TCP + token transport server | Remote machine |
| `edit_helper.py` | server-side edit-by-content (SHA lock + atomic write + verify) | Remote machine (or local) |

### 1.1 `pretool.py` — the transparent-execution hook

`pretool.py` is registered as a `PreToolUse` hook with the matcher `Bash`. On
every bash command the agent issues, the agent host invokes the hook with the
tool call as JSON on stdin. The hook decides whether the command should run
locally (do nothing) or remotely (rewrite it), and returns its decision as JSON
on stdout:

```json
{ "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": { "command": "<rewritten command>" } } }
```

`updatedInput` is the key mechanism: rather than executing the command itself,
the hook hands the agent host a **replacement command string** to run in the
agent's normal sandbox. The agent host still runs the (rewritten) command, still
streams its stdout/stderr live, and still gets a real exit code. From the
agent's point of view nothing changed — it sees the output of `ls`, except `ls`
ran on another machine.

The hook handles four kinds of input:

1. **Switch commands** (`@<alias>`, `@local`, `@status`) — update per-session
   routing state and echo a confirmation.
2. **`shunt ...` CLI calls** — left untouched to run locally (the CLI does its
   own transport).
3. **Bare bash, routed to a remote host** — rewritten into either an `ssh`
   invocation or an inline daemon-client invocation, depending on the host's
   transport.
4. **Already-rewritten commands** — detected by a marker and passed through
   (double-rewrite guard).

### 1.2 `cli.py` — the `shunt` CLI

Some operations cannot be expressed as a clean "redirect this bash command":
they need structured I/O, server-side helpers, or local tools like `rsync`.
The CLI covers them. It always uses the **ssh** transport (these operations
require an ssh-reachable host).

| Subcommand | What it does |
|---|---|
| `shunt hosts` | print the configured hosts |
| `shunt read @host <file> [start:end]` | content with line numbers (orientation); `cat -n` or an `awk` line-range |
| `shunt edit @host <file> OLD NEW [--expected N] [--dry-run]` | edit by content via `edit_helper.py` on the far side |
| `shunt edit @host <file> --stdin` | same, with a JSON payload from stdin (multi-line / binary-safe) |
| `shunt cp <src> <dst>` | `rsync` with one side as `@host:/path` |
| `shunt bg @host <cmd> [--name LABEL]` | start a long task as a `systemd-run` transient unit; prints `JOB=<unit>` |
| `shunt bg @host --list / --status JOB / --stop JOB` | manage those background jobs |
| `shunt get @host <url> [dest]` | server-side background download (`wget -b`) |
| `shunt log [-n N]` | tail the local audit log |
| `shunt install user@host [--alias A] [--key PATH] [--mode secure\|nonsecure] [--port N]` | provision a host |

`shunt bg` uses transient `systemd-run` units (`--collect
--remain-after-exit`), so a long job **survives the ssh disconnect** and its
exit code is preserved for later `--status` inspection. An optional `--name`
slugifies into a readable unit name (`shunt-<label>`); otherwise a random unit
name is generated.

`shunt install` provisions a host end-to-end. In **secure** mode it verifies
`python3` on the server, writes an idempotent `hosts` line, and prints the hook
snippet to add to the agent host's settings (it deliberately does **not** edit
the user's settings file). In **nonsecure** mode it additionally generates a
token, pre-flights the port, uploads `daemon.py` and the systemd unit, writes a
`chmod 600` env file, and brings the daemon up with `systemctl enable --now`.

### 1.3 `daemon.py` — the nonsecure transport server

A small stdlib `ThreadingTCPServer` for use on a **trusted LAN**. It reads a
one-line JSON request, checks the token in constant time
(`hmac.compare_digest`), runs the command under `bash -c`, and streams raw
stdout/stderr back, followed by a trailer that carries the exit code and the new
working directory. It maintains **per-session** working directories keyed by
`sid`. It binds `127.0.0.1` by default; LAN exposure (`0.0.0.0`) is an explicit
opt-in via `SHUNT_HOST`. Running as root triggers a warning — the shipped
systemd unit documents running it as a dedicated non-root user.

Config is entirely via environment: `SHUNT_TOKEN` (required — the daemon
refuses to start without it), `SHUNT_PORT` (default 8766), `SHUNT_HOST`
(default `127.0.0.1`).

### 1.4 `edit_helper.py` — server-side edit-by-content

A zero-dependency editor that runs on the remote machine and edits a file by
**content**, not by line number — the same semantics as a built-in
`old → new` string-replace edit, including a **uniqueness requirement**. Input
is JSON, output is JSON status. It is deployed inline (its source is piped to a
remote `python3 -`, with the payload passed as base64 argv), so nothing has to
be pre-installed on the server beyond `python3`. Its safety mechanisms are
described in §5.

---

## 2. Transports

A host's transport is declared in the config and decides how a routed command
reaches the remote machine.

### 2.1 Secure transport (ssh + ControlMaster) — recommended

The default. The hook rewrites a bare command into an `ssh` invocation:

- **Zero open ports, zero shared secret.** Authentication is ordinary ssh keys.
- **ControlMaster connection reuse.** Options
  `ControlMaster=auto`, `ControlPath=<socket>`, `ControlPersist=300` keep a
  multiplexed master connection alive, so subsequent commands skip the TCP +
  auth handshake and feel local-fast.
- **Per-session, per-destination control socket.** The socket path embeds the
  agent session id **and** the ssh `%h`/`%p` tokens:
  `/tmp/shunt-cm-<sid>-%h-%p.sock`. Per-session keeps parallel agent sessions
  from colliding; per-destination prevents the dangerous silent bug where two
  different hosts in one session would share a master and one host's commands
  would be delivered to the other. (The CLI uses an analogous
  `%r@%h:%p`-keyed socket.)
- **`BatchMode=yes` / `StrictHostKeyChecking=accept-new`** keep it
  non-interactive and first-connect-friendly.

The command actually run on the far side is wrapped so that working directory
persists across calls (see §4) and the exit code is faithfully propagated.

The secure transport requires only the `ssh` binary on the local side — which is
available even in the agent's restricted command sandbox.

### 2.2 Nonsecure transport (TCP + token)

For a **trusted LAN** where the extra ssh round-trips are unwanted. The daemon
(§1.3) listens on a TCP port; the hook rewrites a bare command into an **inline,
self-contained** TCP client.

Two constraints shape this design:

- **The rewritten command runs in a strict sandbox** (root, no access to
  `~/.config/shunt`, but network allowed). So the daemon client cannot be a file
  on disk or read config — it is a small Python program embedded as a string in
  the rewritten command and run via `python3 -c`.
- **The token must not leak via `argv`.** Other local users can read process
  arguments (`ps`), so the token is passed to the inline client through its
  **environment** (`SHUNT_TOK=...`), not as a positional argument.

The wire protocol is one line of JSON
(`{"token","cmd","cwd","sid","mark"}`) from client to daemon, then the raw
output stream, terminated by a **client-chosen random marker** followed by
`<exit>__PWD__<cwd>`. The marker is random per connection so that a command
which happens to print a fixed string cannot spoof the end-of-stream trailer.

Threat-model note baked into the code and docs: in nonsecure mode the command —
including the inline `SHUNT_TOK=` assignment — ends up in the agent transcript,
and the daemon (when exposed via `SHUNT_HOST=0.0.0.0`) grants a shell to anyone
on the network who holds the token. Use it only on a trusted LAN; otherwise use
the secure ssh transport, which has no token at all.

---

## 3. Per-session switching and routing

Routing is **per agent session**, so parallel sessions never fight over a single
global "current host".

- **State file.** The active alias for a session is stored in
  `<conf>/target.<session_id>`. Absence means "local".
- **`@<alias>`** writes that alias to the session's state file and confirms
  `REMOTE → <alias> (<target>, <transport>)`.
- **`@local`** removes the state file (and the active-host sidecar) → commands
  run locally again.
- **`@status`** reports the current mode for this session.

On each routed command the hook also:

- writes a **sidecar** `<conf>/active-host.<session_id>` recording the current
  routing target (useful for external status displays), and
- appends a line to `<conf>/audit.log`
  (`<timestamp> sid=<id> host=<alias> :: <command>`), surfaced by `shunt log`.

Both are fire-and-forget — a failure to write them never blocks command
execution. If the active alias no longer resolves to a configured host, the hook
**falls back to local** rather than erroring.

### The rewrite marker and the double-rewrite guard

Every rewritten command is prefixed with the bash comment line
`#shunt-rewritten`. Because the agent host may re-invoke the hook on a command
the hook itself produced, the hook checks for this marker first and, if present,
exits without touching the command. This prevents a command from being wrapped
in ssh/daemon plumbing twice.

---

## 4. Working-directory persistence

A naive redirect would lose `cd` between calls — each remote command would start
in the home directory. shunt keeps cwd **per session** on both transports using
the same trick: the command is wrapped to (a) `cd` into the last remembered
directory on entry and (b) capture the directory on exit.

- **Secure (ssh).** A remote state file `/tmp/shunt-cwd-<sid>` holds the last
  cwd. The wrapper does `cd "$(cat <state> || echo $HOME)"`, then installs a
  `trap ... EXIT` that records `$?` and writes `pwd` back to the state file on
  **every** exit — including an explicit `exit N` inside the command — so both
  the cwd and the real exit code survive.
- **Nonsecure (daemon).** The daemon keeps `sid → cwd` in memory. The same
  `trap EXIT` pattern prints a `<mark><exit>__PWD__<cwd>` trailer; the daemon
  parses the trailing `__PWD__` and updates its per-session map.

---

## 5. Edit-by-content semantics

`edit_helper.py` performs an `old → new` replacement with the same care as an
interactive editor's string-replace, plus protections appropriate to a possibly
shared remote file:

1. **Symlink resolution.** The target path is resolved with `realpath`, so the
   edit lands on the real file, not the link; the resolved path is echoed back
   in every response.
2. **Size guard.** Files above a limit (`SHUNT_EDIT_MAX_BYTES`, default 64 MiB)
   are refused with advice to `shunt cp` and edit locally.
3. **Optimistic SHA-256 lock.** The caller may pass `base_sha` (the hash it read
   the file at). If the file's current hash differs, the helper returns
   `conflict` instead of writing — catching a concurrent change between read and
   write.
4. **Uniqueness / count-and-refuse.** It counts occurrences of `old`. `0` →
   `not_found`; a count that differs from `expected` → `ambiguous` (with a hint
   to add context). It only writes when the match count equals `expected`.
5. **CRLF tolerance.** If an exact match fails, it retries on normalized line
   endings (CRLF→LF); on write it restores the original CRLF style if the file
   used it.
6. **Atomic write.** It writes to a temp file **in the same directory**,
   `fsync`s the data, preserves mode/owner, `os.replace`s it over the target
   (atomic on one filesystem), then `fsync`s the directory.
7. **Verify-after-write.** It re-reads the file and confirms the hash matches
   what it intended to write, reporting `verified` in the result.
8. **Dry-run.** `--dry-run` returns the unified diff and the would-be hash
   without touching the file.

Statuses: `ok`, `not_found`, `ambiguous`, `conflict`, `error`. Successful
results include the match count, the new SHA, the `verified` flag, a unified
diff, and whether normalization was applied.

---

## 6. Why a hook + `updatedInput` (not replacing the shell)

shunt could have intercepted execution by swapping the agent's shell or relying
on environment quirks. It deliberately does not. The `PreToolUse` hook and its
`updatedInput` field are a **documented, supported contract**: the hook inspects
the tool call and returns a replacement command, and the agent host runs it
through its normal execution path — same streaming, same exit-code handling,
same sandbox. Building on that surface means:

- **Stability across upgrades** — no dependence on undocumented internals.
- **Transparency** — the agent keeps issuing ordinary `Bash` calls; redirection
  is invisible to it.
- **Sandbox compatibility** — rewritten commands run in the agent's existing
  restricted sandbox, which is exactly why the secure transport leans on the
  always-present `ssh` binary and the nonsecure transport ships a fully inline,
  config-free client.

---

## 7. Configuration layout

All local state lives under `~/.config/shunt/` (override with `SHUNT_CONF`):

```
hosts                      one host per line:  <alias> <transport> <target> [key=PATH]
                             transport = ssh    → target = user@host   (secure)
                             transport = daemon → target = host:port   (nonsecure)
token                      shared daemon token (chmod 600)
token.<alias>              per-alias daemon token (multi-daemon setups; falls back to `token`)
target.<session_id>        active alias for a session (absent = local)
active-host.<session_id>   sidecar: current routing target (for status displays)
audit.log                  one line per routed command
```

On the remote side, the nonsecure daemon uses `/opt/shunt/daemon.py`,
`/etc/shunt/daemon.env` (chmod 600), and a systemd unit
`shunt-daemon.service`; the secure transport needs nothing pre-installed beyond
`python3` (used only by `shunt edit`, deployed inline).
