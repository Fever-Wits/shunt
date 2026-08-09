# shunt — Architecture

shunt gives an AI coding agent **transparent remote hands**: the bash commands
the agent already runs are redirected, unchanged, to a chosen remote machine.
The agent does not learn a new tool to "run things remotely" — it keeps writing
ordinary `Bash` calls, and a hook quietly re-routes them. A small companion CLI
covers the operations a bare bash redirect cannot express cleanly (reading,
content-addressed editing, file copy, background jobs, check-out/commit).

The design goal is to ride a **documented, stable surface** of the agent host
(the `PreToolUse` hook and its `updatedInput` contract) instead of replacing or
wrapping the shell. That makes shunt independent of undocumented environment
behaviour and robust across agent-host upgrades.

---

## 1. The shape, in one picture

```
agent writes "ls -la"
   │
   ▼
PreToolUse hook (pretool.py)
   │  @host / @local / @status  → switch the session's routing, echo the result
   │  local mode                → do nothing
   │  remote mode               → rewrite the command for the transport ↓
   ▼
transport: ssh (ControlMaster master socket; no open port; encrypted)
   ▼
output comes back identically · exit code is propagated · cwd is kept per session
```

Transparency comes from the **hook**, not from the transport. That is why the
transport can be reasoned about (or one day replaced) without the agent noticing
anything at all.

---

## 2. Components

shunt is **five** Python modules with zero third-party dependencies (stdlib
only, Python 3.11+ — `tomllib` for the config is the reason for the floor).

| Module | Role | Where it runs |
|---|---|---|
| `pretool.py` | `PreToolUse` hook — transparent execution by command rewrite, plus the mode-boundary warnings | Local (agent host) |
| `cli.py` | the `shunt` CLI — the 12 subcommands of §6 | Local (agent host) |
| `config.py` | the host configuration — the only module that knows its format | Local (agent host) |
| `edit_helper.py` | edit-by-content (SHA lock + count-and-refuse + atomic write + verify) | Remote machine |
| `write_helper.py` | full-file write with the same SHA lock and verification — what `shunt commit` pushes with | Remote machine |

The two helpers are never installed anywhere. Their source is piped to a remote
`python3 -` at the moment they are needed, with the JSON payload passed as a
single base64 argv. The remote side therefore needs nothing but `python3`.

---

## 3. `pretool.py` — the transparent-execution hook

`pretool.py` is registered as a `PreToolUse` hook with the matcher
`Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob`. **Only `Bash` is
ever rewritten**; the wider matcher exists for the mode-boundary warnings below.

On every matched tool call the agent host invokes the hook with the call as JSON
on stdin. For a `Bash` call the hook applies these checks, in this order:

1. **Already rewritten?** A command starting with the `#shunt-rewritten` comment
   line is left alone. The agent host may re-invoke the hook on a command the
   hook itself produced; without this guard the command would be wrapped in ssh
   plumbing twice.
2. **A `shunt …` CLI call?** Left untouched, to run locally — the CLI does its
   own transport.
3. **A switch** (`@<alias>`, `@local`, `@status`)? Update the session's routing
   state and echo a confirmation instead of running anything.
4. **Is this session routed somewhere?** If not — do nothing, the command runs
   locally as it always did.
5. Otherwise **rewrite** it into an `ssh` invocation (§4) and return that.

The rewrite is returned as JSON on stdout:

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

Every other matched tool takes the second job of the file — the warnings below —
and is otherwise left exactly as it came.

### Where the mode stops

The mode covers **bash and nothing else**: the hook rewrites `Bash` and no other
tool. File and search tools keep touching the local disk while the session feels
remote, and a spawned agent inherits the routing and runs its own bash on the far
machine — reading absent local files as facts about the world. Both failures are
silent, which is why the wider matcher exists: on every matched non-bash tool the
hook answers with `additionalContext` and no `updatedInput` — nothing is
rewritten, only said. That is one of three shapes the reply takes; the other two
are `updatedInput` alone (the plain rewrite above) and **both in one reply**, when
a rewritten command carries a note with it (`pretool.emit(command, notice)` — the
first command after a switch, or one that cannot be taken back). The note rides in
the same reply as the rewrite deliberately: sent as a reply of its own it would
return before the rewrite, leaving the command to run here, on the machine the
caller believes they left.

- **`Agent`** — warned on **every** spawn; each one inherits the mode anew, so a
  once-per-session warning would miss all the later ones.
- **`Read` / `Write` / `Edit` / `MultiEdit` / `NotebookEdit` / `Grep` / `Glob`**
  (`pretool.LOCAL_DISK_TOOLS`) — warned **once per host** (state in
  `<conf>/warned.<session_id>`, cleared by `@local`). Keyed by host rather than by
  session: switching `@web-01 → @web-02` warns again, because the old warning no
  longer describes where the tools are pointed.

The search tools carry an **agent's** failure rather than a human's: a person
looking through a machine types `grep` or `find` into bash, which the hook
redirects correctly. An agent reaches for the `Grep` tool instead — far more often
than for `Read` — and reads local hits as facts about the far machine. The budget
is shared by the whole list, not spent per tool, because a line on every `Grep`
call would become wallpaper; that is why the one warning names both remedies at
once (remote file → `shunt read/edit`, remote search → `shunt run`).

⚠ **The list is half the fact.** `LOCAL_DISK_TOOLS` says which tools are warned
about; the matcher in `settings.json` says which ever reach the hook at all. A name
added to one and not the other warns nobody. `HOOK_MATCHER` (`cli.py`, printed by
`shunt install`) is guarded against the tuple by
`tests/test_hook_hint.py`; the prose copies of the matcher — this section, the
`pretool.py` docstring, the README snippet, `AGENTS.md` — are guarded by the eye.
Widening it also needs a line in a file shunt does not own, the user's
`~/.claude/settings.json`: `shunt install` **prints** it, the owner installs it.

It **never blocks** — remote mode plus a local file is legitimate as often as it
is a mistake; only the silence was the defect. The whole branch is wrapped
fail-open: an error inside it can never break someone else's tool call.

---

## 4. The transport — ssh + ControlMaster

One transport carries every routed command. There is no second one, and the CLI
uses the same options as the hook, from one place in the code, so the two cannot
drift apart.

- **Zero open ports, zero shared secret.** Authentication is ordinary ssh keys;
  nothing of shunt's runs on the server.
- **ControlMaster connection reuse.** `ControlMaster=auto`,
  `ControlPath=<socket>`, `ControlPersist=300` (seconds) keep a multiplexed
  master connection alive, so later commands skip the TCP and auth handshake.
- **Per-session, per-destination control socket.** The hook's socket path embeds
  the agent session id **and** the ssh `%r`/`%h`/`%p` tokens:
  `/tmp/shunt-cm-<sid>-%r@%h:%p.sock` — the same shape the CLI uses. Per-session
  keeps parallel agent sessions from colliding; per-destination prevents the
  dangerous silent bug where two different hosts in one session would share a
  master and one host's commands would be delivered to the other. The `%r` is
  the ssh **user**: the config allows two aliases onto one machine with
  different accounts (`deploy@web-01`, `root@web-01`), and without it the second
  would ride the first one's master and run as the wrong account.
- **`BatchMode=yes` / `StrictHostKeyChecking=accept-new`** keep it
  non-interactive and first-connect-friendly.

What the reuse is worth, measured on a LAN with a bare `ssh … true`, three runs
(2026-08-05): **0.24–0.36 s** per call without a master, **0.25 s** for the first
call with one (it brings the master up) and **0.01 s** for every call after. The
gain lives in the repetitions, not in the first command. Measured across the full
cycle instead — python start plus hook plus ssh — the same setup is **0.27–0.32 s**
cold and **0.017–0.033 s** warm.

The transport requires only the `ssh` binary on the local side. That is not an
aesthetic choice: the rewritten command runs in the agent's restricted sandbox,
which has network access but **no access to `~/.config/shunt`**. A client shunt
would have to deploy, or one that needed to read the config at execution time,
could not work there. `ssh` is already present, and the hook bakes the whole
invocation into the rewritten string.

### No pty: an interrupted command keeps running

shunt deliberately does **not** pass `ssh -tt`. The consequence is worth knowing
before you rely on it: **interrupting a foreground command stops your side only.**
The process on the far machine has no controlling terminal, so nothing sends it
SIGHUP, and it runs to its own end. This is a known boundary, not a pending fix —
start long work with `shunt bg` (which can be stopped as a unit), and kill a
stray with `shunt run @host 'pkill -f …'`.

Allocating a pty was measured against live machines and rejected, because it
does kill the process but breaks nearly everything else:

| | without `-tt` (as shipped) | with `-tt` |
|---|---|---|
| `shunt edit` / `shunt commit` (helper source over ssh stdin, `python3 -`) | works | **hangs** — EOF never reaches the far side, python drops into its REPL |
| a command that reads stdin (`cat`, `read -r`) | clean EOF | **hangs** |
| a pager (`systemctl status`, `journalctl -n 200`) | plain output | **hangs**; `less` also truncates long lines to the pty width — data loss |
| stdout / stderr | separate | merged |
| tty-sensitive programs | plain text | ANSI escapes, column layout |
| line endings | `\n` | `\r\n` when the local stdin is a tty |
| interrupted foreground command | keeps running remotely | dies |

Mitigations do not save it. A prelude on the far side (`exec </dev/null` plus
`PAGER=cat GIT_PAGER=cat SYSTEMD_PAGER=cat SYSTEMD_COLORS=0 TERM=dumb`) removes
the hangs, but the escapes and the merged stderr remain, and the env list grows
with every new program that checks `isatty`. Deeper: with a pty there **is** a
controlling terminal, so any program can open `/dev/tty` and block on it — a
`sudo` password prompt, `debconf` — which simply cannot happen without one.

---

## 5. Per-session state: routing, working directory, audit

Routing is **per agent session**, so parallel sessions never fight over a single
global "current host".

- **State file.** The active alias for a session is stored in
  `<conf>/target.<session_id>`. Absence means "local"; a file that is present but names
  no host (empty, a directory, unreadable) is a third reading — UNKNOWN: bash is
  refused, never read as local.
- **`@<alias>`** writes that alias to the session's state file and confirms
  `REMOTE → <alias> (<target>)`. An unknown alias is reported and changes
  nothing.
- **`@local`** removes the state file (and the active-host sidecar, the warning
  marker and the armed switch marker) → commands run locally again.
- **`@status`** reports the current mode for this session.

On each routed command the hook also:

- writes a **sidecar** `<conf>/active-host.<session_id>` recording the current
  routing target (useful for external status displays), and
- appends a record to `<conf>/audit.log`
  (`<timestamp> sid=<id> host=<alias> :: <command>`), surfaced by `shunt log`.

Both are fire-and-forget — a failure to write them never blocks command
execution. If the active alias no longer resolves to a configured host — it was
renamed or deleted while the session was routed to it, or the config broke — the
hook **runs nothing**: the command is replaced by a line saying why. There were
three options, not two. Raising would put a traceback in front of every bash
command; falling back to local would run the command on your own machine while
`@status` still says REMOTE, which is how a `rm -rf` meant for a server finds the
wrong disk. Saying it out loud and executing nothing is the third — the same move
the hook already makes for an unknown `@alias`. `shunt hosts` says what is wrong
with the file.

### Working-directory persistence

A naive redirect would lose `cd` between calls — each remote command would start
in the home directory. shunt keeps cwd **per session**: a state file on the
**remote** host, `$HOME/.cache/shunt/cwd-<session_id>`, holds the last directory.
The wrapper does `cd "$(cat <state> || echo $HOME)"`, then installs a `trap … EXIT`
that records `$?` and writes `pwd` back to the state file on **every** exit —
including an explicit `exit N` inside the command — so both the cwd and the real
exit code survive.

`$HOME` is expanded **over there**, by the far shell, and it is the account you
land in — not the account running the hook. The directory is created `-m 700`
(the file is a trail of the directories you work in), and the write is grouped
and silenced *as a whole*: a bare `pwd > FILE 2>/dev/null` silences `pwd` but not
the **shell's** "No such file or directory" for a file it cannot open — that one
is raised before `pwd` ever runs, since redirections are applied left to right,
and it would surface in the stderr of *your* command as if your own work had
produced it. `.cache` rather than a state directory of its own is deliberate:
losing this file costs one forgotten `cd`, so being swept by a cache cleaner is
exactly the semantics wanted.

The first command after a switch pays the housekeeping the others are too quiet
to pay. It **probes** the write out loud — silenced on every ordinary command, a
home that cannot be written to would otherwise cost the session its memory of
every `cd` without a word — and it sweeps `cwd-*` files older than 30 days,
because a session id is born and never dies and the directory would otherwise
grow one file per session forever. Doing either on every command would mean a
`find` over the host's disk to delete nothing, several times a minute, and a line
of output where silence is the contract. The price, plainly: a home that becomes
unwritable *mid*-session is not reported until the next switch — the hook builds
a command, it never sees what came back.

⚠ That state lives on the **far** machine, in the **landing account's** home, and
carries the session id in its name — so it is per session **and** per host **and**
per account. Switching `@web-01 → @web-02` inside one session carries no directory
over: the second host reads its own state file, which does not exist yet, so the
first command there starts in `$HOME`. Two aliases onto one machine with different
accounts (`deploy@web-01`, `root@web-01`) keep separate directories for the same
reason. Two aliases that land in the **same** account on the same machine are the
one case that does carry over — one home, one file, one shell world — so
switching between them keeps the directory. The ControlMaster socket is the local
counterpart of the same rule (per session *and* per destination); it is the one
path that stays in `/tmp`, on purpose, because it is a local file meant to die
with the machine.

⚠ **The CLI does not read that state at all** — `ssh_argv()` carries no `cd`, so
every `shunt run` / `read` / `edit` / `get` starts in the ssh **login** directory
(usually `$HOME`), not where the session last `cd`-ed. Give the CLI absolute paths,
and read `get`'s default destination `.` as that login directory. Also a known
boundary rather than a pending fix: a cwd for the CLI would mean a session where
there is none — which is exactly why `shunt run` exists.

### The audit log is an archive; trimming it is a fuse

`audit.log` is written to be *read months later* ("where did we download that
from?"), so nothing is dropped on a schedule. Size is the trigger: past
`trim_at_mb` (default **100 MB**) the oldest `drop_months` (default **2**, a month
counted as a flat 30 days) go and the rest stays — a log holding five years loses
its first two months, not everything but the last two. If age can free nothing —
the file is not old but *fast*, a month's worth of commands written in an hour —
the oldest records go until it fits, because otherwise the fuse would fail in
exactly the case it exists for. The rewrite goes through a temp file and
`os.replace`, so a crash mid-trim cannot leave half a log.

Both halves of what left this machine are in it: bash the hook redirected
(`sid=<session>`) and CLI subcommands that reached a host (`sid=cli`, the
subcommand in brackets — `run`, `edit`, `cp`, `bg`, `get`, `commit`). The CLI
calls the hook's own `audit()` and passes its config dir, so where the log lives
and when it is trimmed stay one piece of knowledge. Read-only subcommands stay
out: they bring something back rather than sending something out.

**One command is one record, and a record is one line.** Everything that reads the
log counts lines — the trimmer dates its cut from the first ten characters of one,
`shunt log -n N` shows N of them — so a multi-line command written raw would be
counted, and cut, as several. It is folded on the way in (`\n` → `\\n`) and
unfolded by `shunt log`. Both cuts move whole records; a line inherited from an
older log that carries no date belongs to the record above it, not to itself.

The cost per recorded command is the append, one small read of `shunt.toml` for
the `[audit]` settings, and one `getsize`; at any ordinary rate (measured: ~15 KB
per six weeks) the ceiling is never reached at all. That is the intent — a fuse
that blows regularly is a retention policy in disguise. Two honest limits: what
falls out is gone for good, and parallel sessions append to one file, so a trim
coinciding with another session's append can lose a record or two.

### The rewrite marker

Every rewritten command is prefixed with the bash comment line
`#shunt-rewritten`, which is what the double-rewrite guard of §3 looks for.

---

## 6. `cli.py` — the `shunt` CLI

Some operations cannot be expressed as a clean "redirect this bash command":
they need structured I/O, server-side helpers, or local tools like `rsync`.
The CLI covers them, over the same ssh transport as the hook. **Twelve**
subcommands:

| Subcommand | What it does |
|---|---|
| `shunt` / `shunt help` / `-h` | print the map of the tool (see below) |
| `shunt hosts` | print the configured hosts, as resolved |
| `shunt run @host <cmd>` | one command on the host, no session needed; output and exit code pass through |
| `shunt read @host <file> [start:end]` | content with line numbers (orientation); `cat -n`, or an `awk` line-range |
| `shunt edit @host <file> OLD NEW [--expected N] [--dry-run]` | edit by content via `edit_helper.py` on the far side |
| `shunt edit @host <file> --stdin` | same, with a JSON payload from stdin (multi-line safe) |
| `shunt cp <src> <dst>` | `rsync` with one side as `@host:/path` |
| `shunt bg @host <cmd> [--name LABEL]` | start a long task as a `systemd-run` transient unit; prints `JOB=<unit>` |
| `shunt bg @host --list / --status JOB / --stop JOB` | manage those background jobs |
| `shunt get @host <url> [dest]` | server-side background download (`wget -b`) |
| `shunt log [-n N]` | the last N records of the local audit log (default 50); N counts commands, not lines |
| `shunt checkout @host <path> / --list / --abandon <local>` | pull a remote file into a local sandbox (§7) |
| `shunt commit [<local>] / --abandon <local>` | push edited files back, conflict-checked (§7) |
| `shunt install user@host [--alias A] [--key PATH]` | register a host |

Asking for help and forgetting a subcommand are not the same event: both print
the map on stdout, but `shunt help` exits 0 while a bare `shunt` exits **2**, so
a script that dropped an argument does not silently "succeed".

`shunt run` exists because the hook covers **interactive** bash: it needs a
session to know where that session is routed. A script, a cron job or a spawned
sub-agent has none, so it names the host explicitly instead. It is also the
explicit path for an agent that must genuinely work on another machine — better
than leaving the session in remote mode and letting the agent inherit it
silently. Quoting follows the same split as the hook: a single argument is handed
over verbatim (pipes and redirects survive), several arguments are re-quoted (so
`echo "a b"` stays two words).

`shunt bg` uses transient `systemd-run` units (`--collect --remain-after-exit`),
so a long job **survives the ssh disconnect** and its exit code is preserved for
later `--status` inspection (which shows the last 60 journal lines plus
`ExecMainStatus` / `ExecMainCode` / `Result` / `SubState`). An optional `--name`
slugifies into a readable unit name (`shunt-<label>`); otherwise a random one is
generated. ⚠ The units are **system-level** — no `--user`, no `sudo` — so the ssh
user must be allowed to create system units, which in practice means root on that
host. Without that right, `bg` fails; nothing else in shunt needs it.

`shunt install` provisions a host: it verifies `python3` on the server, writes an
idempotent entry into `shunt.toml` (an entry with the same alias is replaced,
never duplicated, and the rest of the file — comments included — survives), and
prints the hook snippet to add to the agent host's settings. It deliberately does
**not** edit the user's settings file, and it installs nothing on the server.

---

## 7. Remote files: read, edit, check out

The obvious answer — mount the remote filesystem with `sshfs` — was rejected: it
mounts globally, so unrelated sessions collide, and it hangs when the network
drops. Instead, two decisions carry this part:

- **Do the work on the server** (one hop) rather than pulling a file down,
  editing it and pushing it back (3–4 steps, two transfers, and a race window
  for a one-line change).
- **Address the change by content, not by line number.** Line numbers age the
  moment an earlier edit shifts the file; content anchors do not.

### Edit-by-content semantics

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
   write. A full content hash rather than mtime+size on purpose: one-second
   timestamp resolution and same-size rewrites both let a change through.
4. **Uniqueness / count-and-refuse.** It counts occurrences of `old`. `0` →
   `not_found`; a count that differs from `expected` → `ambiguous` (with a hint
   to add context). It only writes when the match count equals `expected`. There
   is no fuzzy fallback that writes: near-misses are diagnostics, never applied.
5. **Byte-exactness, and line-ending tolerance inside it.** The match and the
   replacement happen on the file's **raw bytes**, never on a decoded copy of it:
   only the matched region is rewritten, so a byte that is not valid UTF-8 and a
   line ending anywhere else in the file survive untouched. If the needle does not
   match as given, the needle — not the file — is retried as all-LF and then as
   all-CRLF, and `normalized: true` says only that one of those matched. Decoding
   the file instead is how a helper that answers `ok` destroys what it edits:
   `errors="replace"` turns every non-UTF-8 byte into U+FFFD and the whole file is
   re-encoded from the damaged text, while the diff — computed before the
   conversion — shows nothing of it. The honest edge: the needle arrives as JSON
   and can therefore only be UTF-8, so a needle that is not gets `not_found`
   rather than a guess. For latin-1 *text*, use `checkout`/`commit`, which never
   decode at all.
6. **Atomic write.** It writes to a temp file **in the same directory**,
   `fsync`s the data, preserves mode and owner, `os.replace`s it over the target
   (atomic on one filesystem), then `fsync`s the directory. Both `fsync`s matter:
   without them a crash can leave a zero-length file where the target used to be.
7. **Verify-after-write.** It re-reads the file and confirms the hash matches
   what it intended to write, reporting `verified` in the result. This is what
   catches the silently-failed write that otherwise looks like success.
8. **Dry-run.** `--dry-run` returns the unified diff and the would-be hash
   without touching the file.

Statuses: `ok`, `not_found`, `ambiguous`, `conflict`, `error`. Successful
results include the match count, the new SHA, the `verified` flag, a unified
diff (computed from the bytes on disk and the bytes about to be written, so it
cannot hide a change), and whether normalization was applied.

The helper always exits **0** — its answer is the JSON, not the code. `shunt edit`
therefore reads the status and exits `0` only for `ok`, so `shunt edit … && deploy`
does not deploy a file that was never edited; a transport failure keeps ssh's own
code, which says more than a generic `1`. As a line of its own, though: while the
session is routed to a host, a `shunt …` line carrying `&&` runs nothing.

⚠ The atomic replace swaps the file's inode. If the edited path is on an NFS
mount, another client holding an open handle to the old inode can see `ESTALE`.
Ordinary local filesystems are unaffected.

### Check out and commit — the fallback for heavy work

For many changes to one file, the per-edit round trip stops paying. `shunt
checkout @host <path>` pulls the file (via `cat` over ssh) into a local sandbox
under `<conf>/checkouts/<alias>/<path>`, where the agent's full native editing
tools apply, and records it in `<conf>/checkouts/manifest.json` together with the
remote path and the SHA-256 it was pulled at. A remote path containing `..` that
would escape that sandbox is refused. The pull lands in a `.part` file next to the
target and is `os.replace`d into position only after ssh has succeeded: opening
the target itself for writing would truncate it before ssh had said a word, so a
failed **re**-checkout would destroy the very local edits it was called to refresh.

`shunt commit` walks the manifest (or one named file), asks the far side for the
file's current `sha256sum`, and refuses with `CONFLICT` if it no longer matches
the recorded one — the remote changed since checkout, so a blind overwrite would
destroy someone else's edit. When it matches, the local bytes are pushed through
`write_helper.py`, which repeats the same SHA lock, atomic write and
verify-after-write on its own side, and the manifest's SHA is advanced to the new
one. `--abandon` drops a manifest entry without pushing; the local file stays
where it is.

A commit run is **per entry, not all-or-nothing**. A conflict, an unreadable local
file, or an entry naming a host that is no longer configured is reported on its own
line (`CONFLICT` / `SKIP`) and sets a non-zero exit code, but the remaining files
are still pushed. A manifest entry outliving its host is ordinary rather than
exceptional, and one stale entry must not abandon everything queued behind it.

⚠ Asymmetric size guard: `SHUNT_EDIT_MAX_BYTES` (64 MiB) caps the **push**
direction — `shunt edit` and `shunt commit`. `shunt checkout` has **no** cap; it
`cat`s whatever is there. Check the size of a remote file before checking it out.

### Transfers

- `shunt cp` uses **`rsync`**, not `scp` — delta transfer plus a built-in atomic
  temp-then-rename — over the same ssh options and control socket.
- `shunt get` runs `wget -b` **on the server itself**, so a large download never
  travels through the agent's connection and does not hold it open; progress is
  a log file on the far side.

---

## 8. Why a hook + `updatedInput` (not replacing the shell)

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
  restricted sandbox, which is exactly why the transport leans on the
  always-present `ssh` binary.

---

## 9. Configuration layout

All local state lives under `~/.config/shunt/` (override with `SHUNT_CONF`):

```
shunt.toml                 the hosts:  alias = "user@host", or the inline-table form
                             { target = "user@host", key = "~/.ssh/id" };
                             a top-level `key` is the default identity;
                             an optional [audit] section tunes trim_at_mb / drop_months
hosts                      the previous format, still read when shunt.toml is absent:
                             <alias> ssh <target> [key=PATH]  (one host per line)
target.<session_id>        active alias for a session; absent = local; present but
                             naming no host (empty, a directory, unreadable) = UNKNOWN:
                             bash is refused, never read as local
active-host.<session_id>   sidecar: current routing target (for status displays)
warned.<session_id>        the host this session was already warned about (see §3)
switched.<session_id>      armed by `@<alias>`, spent by the first command after it —
                             the one that pays the far side's housekeeping (see §5)
audit.log                  one record per command sent to a host (hook and CLI alike),
                             one line each — a multi-line command is folded
checkouts/manifest.json    checked-out files: local path → host, remote path, base SHA
checkouts/<alias>/…        the checked-out files themselves
```

`config.py` is the only module that parses either of the two host formats — the CLI and
the hook both resolve through it, so the two cannot drift apart. Each caller passes its
own config directory; the knowledge of the **format** lives in the module, the knowledge
of the **location** stays with the caller.

Everything a host needs is in one file on purpose. An address kept here with its
identity over in `~/.ssh/config` breaks silently the day only one of them is
edited. The config is written atomically (temp file in the same directory,
`fsync`, `os.replace`), so a reader — and the hook reads it before every command
— sees either the whole old file or the whole new one. A broken config is loud in
the CLI, which reports the reason and stops; in the hook it splits with the
session: an unrouted one runs locally, never having asked the config where to go,
and a routed one finds its alias unresolvable and runs nothing, for the reason
given in §5. In the legacy format, a line that does not name `ssh` as the
transport is not turned into some other kind of destination — it is skipped, and
the skipped lines are counted out loud.

The remote side needs nothing pre-installed beyond `python3` (used only by
`shunt edit` / `shunt commit`, deployed inline), plus `systemd` if you use
`shunt bg` and `wget` if you use `shunt get`.

---

## 10. What another hook can rely on

The mode boundary of §3 hits *other people's* hooks too. A guard that validates
something against the local filesystem — "before `mv`, show a grep for
references"; "refuse `rm` outside this directory" — keeps checking the local disk
while the command runs somewhere else. It guards the wrong disk in both
directions, and neither failure announces itself.

Two facts make that fixable:

1. **Nothing is hidden from the guard.** Every `PreToolUse` hook receives the
   *original* tool input, not the previous hook's `updatedInput`. Another guard
   sees the real `mv …`, not shunt's ssh wrapper, and can still block it.
2. **Where the session is routed is readable**, in one file:

```
<conf>/target.<session_id>     # contains the host alias; ABSENT means local
                               # PRESENT but naming no host (empty, a directory,
                               # unreadable) = UNKNOWN — bash is refused, never
                               # read as local
                               # <conf> is $SHUNT_CONF or ~/.config/shunt
```

It is written when the session switches (`@<alias>`) and removed on `@local`, so
it carries the session's *intent*, not its last command. **The name and the
meaning of that file are a public contract** and will not change without a note
here. The neighbouring `active-host.<session_id>` is a side-effect trace written
at execution time for status displays — not a contract; do not build on it.

What a guard does with that knowledge is its own decision — skip the check, or
apply a stricter one because a remote command deserves more scrutiny. One rule is
worth keeping either way: **fail open.** A guard that crashes because shunt is
absent or the file is unreadable breaks commands that had nothing to do with any
of this.

---

## 11. Design decisions at a glance

| Aspect | Decision | Why |
|---|---|---|
| Language | Python 3.11+, stdlib only | the work is network-bound; helpers deploy as source; `tomllib` removes the last dependency |
| Transport | ssh + ControlMaster, the only one | reuses what the installer already needed; no new attack surface. Every file operation needs ssh anyway, so a second transport would carry only the bare redirected bash — at the price of an open port, a shared token and code on both sides |
| Config | own `shunt.toml` under `~/.config/shunt` | address and identity in one place; split across two files, they break silently |
| pty | never (`-tt` measured and rejected) | it would kill interrupted commands, at the cost of hanging every pager and stdin reader (§4) |
| Long jobs | `systemd-run` transient units | native exit code, progress without touching the process, clean kill of the whole cgroup tree |
| Reading a remote file | explicit `shunt read` / `checkout` | file tools are never transparently redirected — reliability over magic (§3) |
| Writing a remote file | explicit `shunt edit` / `checkout` + `commit` | same reason; plus the SHA lock and verification a transparent rewrite could not offer |
| On the server | nothing but `python3` | nothing to deploy, upgrade, or leave behind |

---

## 12. Known limits

Collected in one place, because each is a boundary rather than a pending fix:

- **An interrupted foreground command keeps running** on the far machine (§4).
- **cwd does not follow you across hosts** — the state file lives on each host,
  in the landing account's home, so the first command after a switch starts in
  `$HOME` there. Two aliases landing in the *same* account on the same machine
  are the exception: one home, one file, so the directory does carry over (§5).
- **The CLI does not share the session's cwd** — every subcommand starts in the
  ssh login directory (§5).
- **`shunt checkout` has no size cap**, while `edit` and `commit` do (§7).
- **`shunt bg` needs the right to create system-level systemd units** on the
  remote host (§6).
- **Audit trimming is lossy by design**, and a trim racing another session's
  append can drop a line (§5).
- **The mode covers bash only** — file tools, search tools and spawned agents get
  a warning, not a fence (§3).
