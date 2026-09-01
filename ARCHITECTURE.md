# shunt - Architecture

shunt gives an AI coding agent **transparent remote hands**: the bash commands
the agent already runs are redirected, unchanged, to a chosen remote machine.
The agent does not learn a new tool to "run things remotely" - it keeps writing
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
   |
   v
PreToolUse hook (pretool.py)
   |  @host / @local / @status  -> switch the session's routing, echo the result
   |  local mode                -> do nothing
   |  remote mode               -> rewrite the command for the transport v
   v
transport: ssh (ControlMaster master socket; no open port; encrypted)
   v
output comes back identically - exit code is propagated - cwd is kept per session
```

Transparency comes from the **hook**, not from the transport. That is why the
transport can be reasoned about (or one day replaced) without the agent noticing
anything at all.

---

## 2. Components

shunt is **five** Python modules with zero third-party dependencies (stdlib
only, Python 3.11+ - `tomllib` for the config is the reason for the floor).

| Module | Role | Where it runs |
|---|---|---|
| `pretool.py` | `PreToolUse` hook - transparent execution by command rewrite, plus the mode-boundary warnings | Local (agent host) |
| `cli.py` | the `shunt` CLI - the 12 subcommands of Sec. 6 | Local (agent host) |
| `config.py` | the host configuration - the only module that knows its format | Local (agent host) |
| `edit_helper.py` | edit-by-content (SHA lock + count-and-refuse + atomic write + verify) | Remote machine |
| `write_helper.py` | full-file write with the same SHA lock and verification - what `shunt commit` pushes with | Remote machine |

The two helpers are never installed anywhere. Their source is piped to a remote
`python3 -` at the moment they are needed, with the JSON payload passed as a
single base64 argv. The remote side therefore needs nothing but `python3`.

---

## 3. `pretool.py` - the transparent-execution hook

`pretool.py` is registered as a `PreToolUse` hook with the matcher
`Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob`. **Only `Bash` is
ever rewritten for TRANSPORT**; the wider matcher exists for the mode-boundary
warnings below. That is not the same as "only `Bash` is ever rewritten": an
`Agent` call is handed back with a note written into the child's prompt - see
*What a spawned agent is told* at the end of this section.

On every matched tool call the agent host invokes the hook with the call as JSON
on stdin. For a `Bash` call the hook applies these checks, in this order:

1. **Already rewritten?** A command starting with the `#shunt-rewritten` comment
   line is left alone. The agent host may re-invoke the hook on a command the
   hook itself produced; without this guard the command would be wrapped in ssh
   plumbing twice.
2. **A `shunt ...` CLI call?** Left untouched, to run locally - the CLI does its
   own transport.
3. **A switch** (`@<alias>`, `@local`, `@status`)? Update the session's routing
   state and echo a confirmation instead of running anything.
4. **Is this session routed somewhere?** If not - do nothing, the command runs
   locally as it always did.
5. Otherwise **rewrite** it into an `ssh` invocation (Sec. 4) and return that.

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
agent's point of view nothing changed - it sees the output of `ls`, except `ls`
ran on another machine.

Every other matched tool takes the second job of the file - the warnings below -
and is otherwise left exactly as it came.

### Where the mode stops

The mode covers **bash and nothing else**: the hook rewrites `Bash` and no other
tool. File and search tools keep touching the local disk while the session feels
remote, and a spawned agent inherits the routing and runs its own bash on the far
machine - reading absent local files as facts about the world. Both failures are
silent, which is why the wider matcher exists.

The reply takes **four** shapes, and whether a permission decision rides along is
part of what tells them apart:

- **`updatedInput` + `permissionDecision: allow`** - the plain rewrite above
  (`pretool.emit(command)`).
- **Both fields, same decision** - `pretool.emit(command, notice)`, when a
  rewritten command carries a note with it: the first command after a switch, or
  one that cannot be taken back. The note rides in the same reply as the rewrite
  deliberately: sent as a reply of its own it would return before the rewrite,
  leaving the command to run here, on the machine the caller believes they left.
- **`additionalContext` alone, no decision** - `pretool.warn()`, the warning on a
  local-disk tool. Nothing is rewritten, only said.
- **Both fields and no decision at all** - the `Agent` spawn
  (`pretool._tell_the_spawn_and_its_parent`). One reply, two readers: the child's
  **whole** tool input comes back with a context frame appended to its `prompt`,
  and the parent's warning rides along as `additionalContext`. The decision is
  left out on purpose - this hook has no business granting a spawn the caller's
  own permission rules might want to stop, and a note is not a pass. That
  combination is undocumented (every published example pairs `updatedInput` with a
  decision), so it was measured against the harness rather than assumed, the same
  way `emit()` was. If the child's input carries no string `prompt`, or the reply
  would outgrow `AGENT_REPLY_BUDGET` (9000 characters, deliberately under the
  harness's documented ~10k cap for hook output), the frame is dropped and the
  parent's warning goes out alone: the mistake costs a note not written, never a
  warning lost.

- **`Agent`** - warned on **every** spawn; each one inherits the mode anew, so a
  once-per-session warning would miss all the later ones. The spawn itself is told
  as well, in that same reply: a short frame is appended to the child's prompt -
  what routes its bash, that its own file tools stay on the local disk, and that
  `@local` is one session-wide setting shared with the parent and with any agent
  working beside it, so switching is never a private choice.
- **`Read` / `Write` / `Edit` / `MultiEdit` / `NotebookEdit` / `Grep` / `Glob`**
  (`pretool.LOCAL_DISK_TOOLS`) - warned **once per host** (state in
  `<conf>/warned.<session_id>`, cleared by `@local`). Keyed by host rather than by
  session: switching `@web-01 -> @web-02` warns again, because the old warning no
  longer describes where the tools are pointed.

The search tools carry an **agent's** failure rather than a human's: a person
looking through a machine types `grep` or `find` into bash, which the hook
redirects correctly. An agent reaches for the `Grep` tool instead - far more often
than for `Read` - and reads local hits as facts about the far machine. The budget
is shared by the whole list, not spent per tool, because a line on every `Grep`
call would become wallpaper; that is why the one warning names both remedies at
once (remote file -> `shunt read/edit`, remote search -> `shunt run`).

⚠ **The list is half the fact.** `LOCAL_DISK_TOOLS` says which tools are warned
about; the matcher in `settings.json` says which ever reach the hook at all. A name
added to one and not the other warns nobody. `HOOK_MATCHER` (`cli.py`, printed by
`shunt install`) is guarded against the tuple by
`tests/test_hook_hint.py`; the prose copies of the matcher - this section, the
`pretool.py` docstring, the README snippet, `AGENTS.md` - are guarded by the eye.
Widening it also needs a line in a file shunt does not own, the user's
`~/.claude/settings.json`: `shunt install` **prints** it, the owner installs it.

It **never blocks** - remote mode plus a local file is legitimate as often as it
is a mistake; only the silence was the defect. The whole branch is wrapped
fail-open: an error inside it can never break someone else's tool call.


### The forms of a reply

The hook has a small, fixed set of ways to answer, and the difference between them
is not tone - it is **what is left of the caller's call**: does anything run, whose
is it, and who can still repair the hook from inside the session.

| Form | Where | What happens to the call | When |
|---|---|---|---|
| rewrite | `emit(command, tool_input, notice=)` | OUR string runs, carrying the caller's whole input | the ordinary remote path; a `notice` rides in the same reply |
| replacement | `echo(msg)` -> `emit(..., None)` | the command is TAKEN AWAY and a sentence put in its place | every refusal with a readable input (unresolvable alias, unreadable routing, `shunt ...` on a compound line) |
| voice | `warn(msg)` | the call runs UNTOUCHED, the text enters the agent's context | the mode boundary (file tool, agent), an incomplete input on a non-`Bash` tool, the ticket after `@local`, a crash on a non-`Bash` tool |
| denial | `block(msg)` | the agent host STOPS the call (exit 2, reason on stderr) | the input cannot be parsed, carries no `tool_name`, or the hook crashed on `Bash` |
| note into a prompt | `_tell_the_spawn_and_its_parent` - its own `print` + `sys.exit(0)` | the call runs with a CHANGED `tool_input`: a note appended to the child's prompt, plus `additionalContext` for the parent | an `Agent` spawned while the session is routed |
| silence | a bare `sys.exit(0)` | the call runs unchanged and NOTHING is said | every ordinary case: a local session, an already-rewritten command, a single `shunt ...` line, a matched tool with nothing to report |

Two of these are easy to miss when counting. The fifth is neither `emit` nor
`warn`: it assembles its own JSON because it needs a combination neither offers -
`updatedInput` **and** `additionalContext`, but **no** `permissionDecision`, since
this hook has no business granting a spawn the caller's own permission rules might
want to stop. The sixth is the most common answer in the life of a session and has
no function carrying it: it is the absence of all the others.

`emit` hands back the caller's **whole** input rather than the one field it
changed. Measured against agent host 2.1.226: given a `Bash` call carrying
`run_in_background: true` and `timeout: 600000`, a rewrite naming only `command`
ran in the FOREGROUND with both fields gone, while the reference describes
`updatedInput` as merged rather than replaced. Handing everything back is correct
under both readings.

### What a spawned agent is told

The parent has been warned about spawns for a while. The child was told nothing -
and the child is the one that acts: it runs `ls`, reads a disk it has never seen,
and reports what it found as the truth about the world. Observed once as *"the Bash
tool briefly lost access to the working directory."* It had not; it was elsewhere.

So an `Agent` call made while the session is routed comes back with a short frame
(`AGENT_FRAME`, written into the call by `_tell_the_spawn_and_its_parent`)
appended to the child's own prompt: what routes the commands, what that means for
its two hands (bash goes there, file tools stay here), and the way out with its
price. A bare fact would not carry - an agent reading "you are on @web-01" has no
reason to think anything follows from it.

⚠ **The price is measured, not assumed.** The agent host gives a child the
PARENT's `session_id`. Every file this hook keys on - routing, ticket, warning
budget - is therefore ONE slot shared by the parent and by every agent running at
that moment. `@local` from a child is not a private choice: it moves everybody. A
note offering it without saying so would hand the child a way to reroute its parent
mid-command, which is worse than the silence it was written to end.

The reply is capped (`AGENT_REPLY_BUDGET`). It carries the child's entire prompt,
so its size belongs to the caller, and a reply cut in half is not JSON - the agent
host would log a parse error, treat it as non-blocking, and run the ORIGINAL call,
costing the parent the warning it had been getting all along. An oversized reply
degrades to that warning alone: the direction of the mistake is a note not written,
never a warning lost.

-> When the routing state is UNREADABLE there is no note into the prompt, only the
warning to the parent. That state announces itself to the child at once - its very
first bash command comes back refused, with both ways out named. The remote state is
the silent one, where commands succeed on a machine nobody mentioned.

---

## 4. The transport - ssh + ControlMaster

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
  `/tmp/shunt-cm-<sid>-%r@%h:%p.sock`. Per-session
  keeps parallel agent sessions from colliding; per-destination prevents the
  dangerous silent bug where two different hosts in one session would share a
  master and one host's commands would be delivered to the other. The `%r` is
  the ssh **user**: the config allows two aliases onto one machine with
  different accounts (`deploy@web-01`, `root@web-01`), and without it the second
  would ride the first one's master and run as the wrong account.
- **The CLI's socket keys on the same tokens, and lives somewhere else.**
  `shunt-cm-cli-%r@%h:%p.sock` in `$XDG_RUNTIME_DIR/shunt/` when the environment
  offers one, `~/.cache/shunt/` otherwise, created at mode 700. The asymmetry is
  the point: the hook's name carries a session id and cannot be guessed, so a
  world-writable directory hands nobody a path to occupy first. The
  CLI has no such part in its name - it must stay predictable, because the whole
  value of a muxed socket is that the *next* `shunt` call finds the master the
  last one left - so its **place** carries the privacy instead. A `ControlPath`
  too long for a unix socket is fatal, which makes the directory a budget: an
  ordinary destination lands near 87 bytes, close to the ceiling measured on
  Linux (90; test_ssh_opts pins it).
- **`BatchMode=yes` / `StrictHostKeyChecking=accept-new`** keep it
  non-interactive and first-connect-friendly.

What the reuse is worth: without a master every call pays a full ssh handshake;
with one, the first call brings the master up and every call after rides it. The
gain lives in the repetitions, not in the first command.

The transport requires only the `ssh` binary on the local side. That is not an
aesthetic choice: ssh is what carries the command to the far host either way, so
a client of ours would be a second program to deploy on top of it. The hook resolves
the host, the key path and the session id before it returns, and only the finished
string travels, with the whole invocation baked into it.

### No pty: an interrupted command keeps running

shunt deliberately does **not** pass `ssh -tt`. The consequence is worth knowing
before you rely on it: **interrupting a foreground command stops your side only.**
The process on the far machine has no controlling terminal, so nothing sends it
SIGHUP, and it runs to its own end. This is a known boundary, not a pending fix -
start long work with `shunt bg` (which can be stopped as a unit), and kill a
stray with `shunt run @host 'pkill -f ...'`.

Allocating a pty was measured against live machines and rejected, because it
does kill the process but breaks nearly everything else:

| | without `-tt` (as shipped) | with `-tt` |
|---|---|---|
| `shunt edit` / `shunt commit` (helper source over ssh stdin, `python3 -`) | works | **hangs** - EOF never reaches the far side, python drops into its REPL |
| a command that reads stdin (`cat`, `read -r`) | clean EOF | **hangs** |
| a pager (`systemctl status`, `journalctl -n 200`) | plain output | **hangs**; `less` also truncates long lines to the pty width - data loss |
| stdout / stderr | separate | merged |
| tty-sensitive programs | plain text | ANSI escapes, column layout |
| line endings | `\n` | `\r\n` when the local stdin is a tty |
| interrupted foreground command | keeps running remotely | dies |

Mitigations do not save it. A prelude on the far side (`exec </dev/null` plus
`PAGER=cat GIT_PAGER=cat SYSTEMD_PAGER=cat SYSTEMD_COLORS=0 TERM=dumb`) removes
the hangs, but the escapes and the merged stderr remain, and the env list grows
with every new program that checks `isatty`. Deeper: with a pty there **is** a
controlling terminal, so any program can open `/dev/tty` and block on it - a
`sudo` password prompt, `debconf` - which simply cannot happen without one.

---

## 5. Per-session state: routing, working directory, audit

Routing is **per agent session**, so parallel sessions never fight over a single
global "current host".

- **State file.** The active alias for a session is stored in
  `<conf>/target.<session_id>`. Absence means "local"; a file that is present but names
  no host (empty, a directory, unreadable) is a third reading - UNKNOWN: bash is
  refused, never read as local.
- **`@<alias>`** writes that alias to the session's state file and then **asks the
  machine**: a single `ssh ... true` with `ConnectTimeout=3` (`PROBE_TIMEOUT`, itself
  bounded by a `PROBE_DEADLINE` two seconds wider for everything that option does
  not cover), carrying the **same** ssh options a real command would - a green
  probe over a key the command will not use is worth less than no probe at all. The
  confirmation reads `REMOTE -> <alias> (<target>) - connected`, or says the host did
  not answer the check, or that the check could not be made at all: three answers,
  because a failed `ssh ... true` proves only that *this check* did not get through -
  a refused key, a changed host key and a broken login shell all answer from a
  machine that is perfectly awake. **The switch stands in every case.** A host may
  be rebooting, and a session that has said where it wants to be is not sent home
  behind its own back; the routing is therefore written *before* the probe, so a
  probe that hangs cannot cost the session its routing. The price, said plainly:
  `@<alias>` used to take milliseconds and now takes as long as the probe does. An
  unknown alias is reported and changes nothing.
- **`@local`** removes the state file (and the active-host sidecar, the warning
  marker and the armed switch marker) -> commands run locally again.
- **`@status`** reports the current mode for this session.

On each routed command the hook also:

- writes a **sidecar** `<conf>/active-host.<session_id>` recording the current
  routing target (useful for external status displays), and
- appends a record to `<conf>/audit.log`
  (`<timestamp> sid=<id> host=<alias> :: <command>`), surfaced by `shunt log`.

Both are fire-and-forget - a failure to write them never blocks command
execution. If the active alias no longer resolves to a configured host - it was
renamed or deleted while the session was routed to it, or the config broke - the
hook **runs nothing**: the command is replaced by a line saying why. There were
three options, not two. Raising would put a traceback in front of every bash
command *for a state the hook understands perfectly well* - and worse, a hook that
raises exits non-blocking, so the harness would then run the original line here
(the crash umbrella in `main()` exists for the exceptions nobody foresaw, and it
answers them by denying bash outright rather than by raising); falling back to
local would run the command on your own machine while
`@status` still says REMOTE, which is how a `rm -rf` meant for a server finds the
wrong disk. Saying it out loud and executing nothing is the third - the same move
the hook already makes for an unknown `@alias`. `shunt hosts` says what is wrong
with the file.

### Working-directory persistence

A naive redirect would lose `cd` between calls - each remote command would start
in the home directory. shunt keeps cwd **per session**: a state file on the
**remote** host, `$HOME/.cache/shunt/cwd-<session_id>`, holds the last directory.
The wrapper does `cd "$(cat <state> || echo $HOME)"`, then installs a `trap ... EXIT`
that records `$?` and writes `pwd` back to the state file on **every** exit -
including an explicit `exit N` inside the command - so both the cwd and the real
exit code survive.

`$HOME` is expanded **over there**, by the far shell, and it is the account you
land in - not the account running the hook. The directory is created `-m 700`
(the file is a trail of the directories you work in), and the write is grouped
and silenced *as a whole*: a bare `pwd > FILE 2>/dev/null` silences `pwd` but not
the **shell's** "No such file or directory" for a file it cannot open - that one
is raised before `pwd` ever runs, since redirections are applied left to right,
and it would surface in the stderr of *your* command as if your own work had
produced it. `.cache` rather than a state directory of its own is deliberate:
losing this file costs one forgotten `cd`, so being swept by a cache cleaner is
exactly the semantics wanted.

The first command after a switch pays the housekeeping the others are too quiet
to pay. It **probes** the write out loud - silenced on every ordinary command, a
home that cannot be written to would otherwise cost the session its memory of
every `cd` without a word - and it sweeps `cwd-*` files older than 30 days,
because a session id is born and never dies and the directory would otherwise
grow one file per session forever. Doing either on every command would mean a
`find` over the host's disk to delete nothing, several times a minute, and a line
of output where silence is the contract. The price, plainly: a home that becomes
unwritable *mid*-session is not reported until the next switch - the hook builds
a command, it never sees what came back.

It pays only if its ticket was actually punched. Two riders sit on one marker and
they read it differently: the "this runs THERE" reminder rides on the marker
**standing**, the housekeeping on it having been **removed**. A config directory
that cannot delete therefore keeps repeating the line and skips the sweep, rather
than buying a `find` over someone else's disk on every command for the rest of the
session. Going home arms the same ticket - `@local` writes its own mark into it -
and the first command after that gets the mirrored reminder, with no far side to
keep house on.

⚠ That state lives on the **far** machine, in the **landing account's** home, and
carries the session id in its name - so it is per session **and** per host **and**
per account. Switching `@web-01 -> @web-02` inside one session carries no directory
over: the second host reads its own state file, which does not exist yet, so the
first command there starts in `$HOME`. Two aliases onto one machine with different
accounts (`deploy@web-01`, `root@web-01`) keep separate directories for the same
reason. Two aliases that land in the **same** account on the same machine are the
one case that does carry over - one home, one file, one shell world - so
switching between them keeps the directory. The ControlMaster socket is the local
counterpart of the same rule (per session *and* per destination). The hook's stays
in `/tmp`, on purpose - a local file meant to die with the machine, under a name
carrying a session id that nobody outside can guess. The CLI's cannot borrow that
argument, because its name has to stay predictable to be reused between calls, so
it lives in a private per-user directory instead; see the control-socket entry
above.

⚠ **The CLI does not read that state at all** - `ssh_argv()` carries no `cd`, so
every `shunt run` / `read` / `edit` / `get` starts in the ssh **login** directory
(usually `$HOME`), not where the session last `cd`-ed. Give the CLI absolute paths,
and read `get`'s default destination `.` as that login directory. Also a known
boundary rather than a pending fix: a cwd for the CLI would mean a session where
there is none - which is exactly why `shunt run` exists.

### The audit log is an archive; trimming it is a fuse

`audit.log` is written to be *read months later* ("where did we download that
from?"), so nothing is dropped on a schedule. Size is the trigger: past
`trim_at_mb` (default **100 MB**) the oldest `drop_months` (default **2**, a month
counted as a flat 30 days) go and the rest stays - a log holding five years loses
its first two months, not everything but the last two. If age can free nothing -
the file is not old but *fast*, a month's worth of commands written in an hour -
the oldest records go until it fits, because otherwise the fuse would fail in
exactly the case it exists for. The rewrite goes through a temp file and
`os.replace`, so a crash mid-trim cannot leave half a log.

Both halves of what left this machine are in it: bash the hook redirected
(`sid=<session>`) and CLI subcommands that reached a host (`sid=cli`, the
subcommand in brackets - `run`, `edit`, `cp`, `bg`, `get`, `commit`). The CLI
calls the hook's own `audit()` and passes its config dir, so where the log lives
and when it is trimmed stay one piece of knowledge. Read-only subcommands stay
out: they bring something back rather than sending something out.

**One command is one record, and a record is one line.** Everything that reads the
log counts lines - the trimmer dates its cut from the first ten characters of one,
`shunt log -n N` shows N of them - so a multi-line command written raw would be
counted, and cut, as several. It is folded on the way in (`\n` -> `\\n`) and
unfolded by `shunt log`. Both cuts move whole records; a line inherited from an
older log that carries no date belongs to the record above it, not to itself.

The cost per recorded command is the append, one small read of `shunt.toml` for
the `[audit]` settings, and one `getsize`; at any ordinary rate (measured: ~15 KB
per six weeks) the ceiling is never reached at all. That is the intent - a fuse
that blows regularly is a retention policy in disguise. Two honest limits: what
falls out is gone for good, and parallel sessions append to one file, so a trim
coinciding with another session's append can lose a record or two.

### What wraps the payload - the marker, and the epilogue

Every rewritten command is prefixed with the bash comment line
`#shunt-rewritten`, which is what the double-rewrite guard of Sec. 3 looks for.

It also has a **tail**, and the tail runs **locally**: after the ssh call comes
`__shunt_rc=$?`, a test for **255**, and `exit "$__shunt_rc"`. 255 is the code ssh
reserves for its own failures, and bare it is indistinguishable from a verdict by the
caller's own program - so on 255 alone one line goes to stderr naming the transport and
the host, and the exit code is then handed on untouched. This is the only thing the hook
says about what came *back*: it never reads the far side (nothing here can - the hook
builds a command and never sees a result), only the number the local ssh process leaves
behind. Position matters: nothing may follow the `exit`, so the epilogue is the last
thing in the string.

The far side's own exit code reaches that point through the `trap ... EXIT` of
*Working-directory persistence* above - taken first, spent last - so neither the trap's
bookkeeping nor the epilogue's sentence can change what the caller's command returned.

---

## 6. `cli.py` - the `shunt` CLI

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
| `shunt checkout @host <path> [--force] / --list / --abandon <local>` | pull a remote file into a local sandbox (Sec. 7); refuses over local edits unless `--force` |
| `shunt commit [<local>] / --abandon <local>` | push edited files back, conflict-checked (Sec. 7) |
| `shunt install user@host [--alias A] [--key PATH]` | register a host |

Asking for help and forgetting a subcommand are not the same event: both print
the map on stdout, but `shunt help` exits 0 while a bare `shunt` exits **2**, so
a script that dropped an argument does not silently "succeed".

`shunt run` exists because the hook covers **interactive** bash: it needs a
session to know where that session is routed. A script, a cron job or a spawned
sub-agent has none, so it names the host explicitly instead. It is also the
explicit path for an agent that must genuinely work on another machine - better
than leaving the session in remote mode and letting the agent inherit it
silently. Quoting follows the same split as the hook: a single argument is handed
over verbatim (pipes and redirects survive), several arguments are re-quoted (so
`echo "a b"` stays two words).

`shunt bg` uses transient `systemd-run` units (`--collect --remain-after-exit`),
so a long job **survives the ssh disconnect** and its exit code is preserved for
later `--status` inspection (which shows the last 60 journal lines plus
`ExecMainStatus` / `ExecMainCode` / `Result` / `SubState`). An optional `--name`
slugifies into a readable unit name (`shunt-<label>`); otherwise a random one is
generated. ⚠ The units are **system-level** - no `--user`, no `sudo` - so the ssh
user must be allowed to create system units, which in practice means root on that
host. Without that right, `bg` fails; nothing else in shunt needs it.

`shunt install` provisions a host: it verifies `python3` on the server, writes an
idempotent entry into `shunt.toml` (an entry with the same alias is replaced,
never duplicated, and the rest of the file - comments included - survives), and
prints the hook snippet to add to the agent host's settings. It deliberately does
**not** edit the user's settings file, and it installs nothing on the server.

---

## 7. Remote files: read, edit, check out

The obvious answer - mount the remote filesystem with `sshfs` - was rejected: it
mounts globally, so unrelated sessions collide, and it hangs when the network
drops. Instead, two decisions carry this part:

- **Do the work on the server** (one hop) rather than pulling a file down,
  editing it and pushing it back (3-4 steps, two transfers, and a race window
  for a one-line change).
- **Address the change by content, not by line number.** Line numbers age the
  moment an earlier edit shifts the file; content anchors do not.

### Edit-by-content semantics

`edit_helper.py` performs an `old -> new` replacement with the same care as an
interactive editor's string-replace, plus protections appropriate to a possibly
shared remote file:

1. **Symlink resolution.** The target path is resolved with `realpath`, so the
   edit lands on the real file, not the link; the resolved path is echoed back
   in every response.
2. **Size guard.** Files above a limit (`SHUNT_EDIT_MAX_BYTES`, default 64 MiB)
   are refused with advice to `shunt cp` and edit locally.
3. **Optimistic SHA-256 lock.** The caller may pass `base_sha` (the hash it read
   the file at). If the file's current hash differs, the helper returns
   `conflict` instead of writing - catching a concurrent change between read and
   write. A full content hash rather than mtime+size on purpose: one-second
   timestamp resolution and same-size rewrites both let a change through.
4. **Uniqueness / count-and-refuse.** It counts occurrences of `old`. `0` ->
   `not_found`; a count that differs from `expected` -> `ambiguous` (with a hint
   to add context). It only writes when the match count equals `expected`. There
   is no fuzzy fallback that writes: near-misses are diagnostics, never applied.
5. **Byte-exactness, and line-ending tolerance inside it.** The match and the
   replacement happen on the file's **raw bytes**, never on a decoded copy of it:
   only the matched region is rewritten, so a byte that is not valid UTF-8 and a
   line ending anywhere else in the file survive untouched. If the needle does not
   match as given, the needle - not the file - is retried as all-LF and then as
   all-CRLF, and `normalized: true` says only that one of those matched. Decoding
   the file instead is how a helper that answers `ok` destroys what it edits:
   `errors="replace"` turns every non-UTF-8 byte into U+FFFD and the whole file is
   re-encoded from the damaged text, while the diff - computed before the
   conversion - shows nothing of it. The honest edge: the needle arrives as JSON
   and can therefore only be UTF-8, so a needle that is not gets `not_found`
   rather than a guess. For latin-1 *text*, use `checkout`/`commit`, which never
   decode at all.
6. **Atomic write.** It writes to a temp file **in the same directory**,
   `fsync`s the data, preserves mode and owner, `os.replace`s it over the target
   (atomic on one filesystem), then `fsync`s the directory. Both `fsync`s matter:
   without them a crash can leave a zero-length file where the target used to be.
   The two steps that can fail *after the content is already safe* report rather
   than lie: a `chown` that cannot follow (the file keeps the helper's owner -
   the content lands, the **ownership** is the damage) and a directory `fsync`
   that fails (written and readable, not yet durable) come back as `warnings`
   beside `status: ok`. The first used to be swallowed whole; the second used to
   be reported as `write failed` although it runs *after* the rename, which made
   `commit` leave `base_sha` behind and invent a `CONFLICT` on the next push.
7. **Verify-after-write.** It re-reads the file and confirms the hash matches
   what it intended to write, reporting `verified` in the result. This is what
   catches the silently-failed write that otherwise looks like success.
8. **Dry-run.** `--dry-run` returns the unified diff and the would-be hash
   without touching the file.

Statuses: `ok`, `not_found`, `ambiguous`, `conflict`, `error`. Successful
results include the match count, the new SHA, the `verified` flag, a unified
diff (computed from the bytes on disk and the bytes about to be written, so it
cannot hide a change), whether normalization was applied, and - only when there
is one - a `warnings` list naming what failed around the write while the write
itself stood (see item 6). `shunt edit` prints the JSON verbatim, so those show
by themselves; `shunt commit` parses it and prints them on their own line.

The helper always exits **0** - its answer is the JSON, not the code. `shunt edit`
therefore reads the status and exits `0` only for `ok`, so `shunt edit ... && deploy`
does not deploy a file that was never edited; a transport failure keeps ssh's own
code, which says more than a generic `1`. As a line of its own, though: while the
session is routed to a host, a `shunt ...` line carrying `&&` runs nothing.

⚠ The atomic replace swaps the file's inode. If the edited path is on an NFS
mount, another client holding an open handle to the old inode can see `ESTALE`.
Ordinary local filesystems are unaffected.

### Check out and commit - the fallback for heavy work

For many changes to one file, the per-edit round trip stops paying. `shunt
checkout @host <path>` pulls the file (via `cat` over ssh) into a local sandbox
under `<conf>/checkouts/<alias>/<path>`, where the agent's full native editing
tools apply, and records it in `<conf>/checkouts/manifest.json` together with the
remote path and the SHA-256 it was pulled at. A remote path containing `..` that
would escape that sandbox is refused. The pull lands in a `.part` file next to the
target and is `os.replace`d into position only after ssh has succeeded: opening
the target itself for writing would truncate it before ssh had said a word, so a
failed **re**-checkout would destroy the very local edits it was called to refresh.

A **successful** re-checkout used to destroy them too, and that is the sharper of
the two: the local file is replaced whole, with no undo and no second copy, and
the way in was the tool's own advice - `commit`'s conflict message said
"re-checkout, then re-apply your edits". So the manifest read, which already sits
before anything is written, now carries a second gate: if the local file's SHA no
longer matches the `base_sha` recorded at checkout, the command **refuses**
(exit 2) and names the three ways on - `shunt commit <path>`,
`shunt checkout --abandon <path>` (keeps the file, stops tracking it), or
`--force` to drop the edits and take the remote copy. A local file that is *gone*,
or identical to what was pulled, is still refreshed without a word: the first is
how a deleted checkout is repaired and has to stay possible. `commit`'s conflict
message now points at `--force`, since a bare re-checkout would meet this refusal.

`shunt commit` walks the manifest (or one named file), asks the far side for the
file's current `sha256sum`, and refuses with `CONFLICT` if it no longer matches
the recorded one - the remote changed since checkout, so a blind overwrite would
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

⚠ Asymmetric size guard: `SHUNT_EDIT_MAX_BYTES` (default 64 MiB) is read by the
helpers **on the far host**, so a value exported locally never reaches it - ssh
carries no environment. The two helpers measure different things: for
`shunt edit` it is the **remote file being opened** (`st_size`), for
`shunt commit` the **content being sent** (raw bytes at the limit, with an
inflated base64 payload rejected before decoding, at twice it). `shunt checkout`
has **no** cap; it `cat`s whatever is there. Check the size of a remote file
before checking it out.

### Transfers

- `shunt cp` uses **`rsync`**, not `scp` - delta transfer plus a built-in atomic
  temp-then-rename - over the same ssh options and control socket.
- `shunt get` runs `wget -b` **on the server itself**, so a large download never
  travels through the agent's connection and does not hold it open; progress is
  a log file on the far side.


### The far side's python

`shunt edit` and `shunt commit` send `edit_helper.py` / `write_helper.py` over ssh
stdin and run them with **the server's** `python3`. The test suite proves their
logic locally, on whatever interpreter happens to be here; what it cannot prove is
the environment they actually meet. Measured on real hosts rather than assumed:
**3.7 to 3.13** - five minor versions, none of them chosen by the tool.

**The floor is written down, not inherited.** `MIN_PYTHON = (3, 3)` stands in the
first lines of both helpers. The number is measured against the code they contain:
`os.replace` - the atomic rename both stand on - is new in 3.3; the next newest is
`os.makedirs(exist_ok=)` at 3.2; everything else is 2.x-era stdlib. ⚠ The CLI needs
**3.11** for `tomllib`, and that does **not** apply on the far side: hosts in that
range fall below it, and an inherited floor would have cut off every one.

Below the floor a helper answers in its own format - JSON on stdout,
`{"status": "error", "message": "python A.B on this host, shunt file helpers need
3.3+ ..."}` - and exits non-zero, before touching anything. The wording matters more
than it looks: the old failure was *also* JSON, but it named the symptom
(`write failed: module 'os' has no attribute 'replace'`), which sends a reader
looking for a bug in shunt rather than at an old server. The guard's text is
byte-identical in both helpers, which a test enforces: they cannot import one
another - nothing of shunt's exists over there - so a test is what holds them
together.

⚠ **A runtime guard can only catch APIs.** Syntax from the future is a `SyntaxError`
at COMPILE time - the whole file, before its first line runs - and then the guard
never speaks at all. So the floor has a second half in the tests: both helpers must
stay PARSEABLE at `MIN_PYTHON`, and that takes three mechanisms, because none is
whole. `ast.parse(feature_version=)` rejects the walrus, `match`, `async def`,
numeric underscores and positional-only parameters - and, on 3.12+, accepts an f-string
(the parser rewrite in PEP 701 dropped that gate; older interpreters reject it). Either
way the node scan is what makes the check version-independent: a scan by AST node type
catches the f-string - and misses PEP 448 (`{**a}`, `[*a]`,
`f(*a, *b)`, `f(**a, **b)`) and PEP 614 (`@a[0].b`), which are new SHAPES of old
nodes. A third scan, by shape, catches those. ⚠ One blind spot remains for all
three and is named rather than left to be found: parentheses around several context
managers (3.9) are invisible to every one of them.
-> This is why the two helpers are the one place in this package that does **not**
use f-strings: an f-string in either would raise the real floor to 3.6 and make the
guard unreachable - a claim of protection that cannot fire.

**At registration.** `shunt install` asks the machine for its version
(`python3 -c "...sys.version_info..."`, not the `--version` banner) and prints it. Below
the floor it says so and registers the host anyway: the helpers are two subcommands
out of eleven, and bash, `run`, `cp`, `bg` and `get` do not touch them. Refusing
would trade a whole machine for one feature. A host with **no** `python3` at all
(exit 127 from the remote shell) is treated the same way - reached, registered, and
told what is unavailable - while any other non-zero code is the transport failing
and is refused as before.

---

## 8. Why a hook + `updatedInput` (not replacing the shell)

shunt could have intercepted execution by swapping the agent's shell or relying
on environment quirks. It deliberately does not. The `PreToolUse` hook and its
`updatedInput` field are a **documented, supported contract**: the hook inspects
the tool call and returns a replacement command, and the agent host runs it
through its normal execution path - same streaming, same exit-code handling,
same sandbox. Building on that surface means:

- **Stability across upgrades** - no dependence on undocumented internals.
- **Transparency** - the agent keeps issuing ordinary `Bash` calls; redirection
  is invisible to it.
- **Nothing extra to deploy for a routed command** - rewritten commands carry the whole invocation and lean on
  the always-present `ssh` binary.

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
warned.<session_id>        the host this session was already warned about (see Sec. 3)
switched.<session_id>      the one-shot ticket, armed in BOTH directions: `@<alias>`
                             writes the alias, `@local` writes the mark `@local` - one
                             file, so the last switch wins. The first command after it
                             spends it and says where it is about to run (there, or
                             HERE). Only a spending that SUCCEEDS pays the far side's
                             once-per-switch housekeeping: a ticket that cannot be
                             removed keeps repeating its line and buys nothing, and a
                             local ticket has no far side to keep house on (see Sec. 5)
audit.log                  one record per command sent to a host (hook and CLI alike),
                             one line each - a multi-line command is folded
checkouts/manifest.json    checked-out files: local path -> host, remote path, base SHA
checkouts/<alias>/...        the checked-out files themselves
```

`config.py` is the only module that parses either of the two host formats - the CLI and
the hook both resolve through it, so the two cannot drift apart. Each caller passes its
own config directory; the knowledge of the **format** lives in the module, the knowledge
of the **location** stays with the caller.

Everything a host needs is in one file on purpose. An address kept here with its
identity over in `~/.ssh/config` breaks silently the day only one of them is
edited. The config is written atomically (temp file in the same directory,
`fsync`, `os.replace`), so a reader - and the hook reads it before every command
- sees either the whole old file or the whole new one. A broken config is loud in
the CLI, which reports the reason and stops; in the hook it splits with the
session: an unrouted one runs locally, never having asked the config where to go,
and a routed one finds its alias unresolvable and runs nothing, for the reason
given in Sec. 5. In the legacy format, a line that does not name `ssh` as the
transport is not turned into some other kind of destination - it is skipped, and
the skipped lines are counted out loud.

The remote side needs nothing pre-installed beyond `python3` (used only by
`shunt edit` / `shunt commit`, deployed inline), plus `systemd` if you use
`shunt bg` and `wget` if you use `shunt get`.

---

## 10. What another hook can rely on

The mode boundary of Sec. 3 hits *other people's* hooks too. A guard that validates
something against the local filesystem - "before `mv`, show a grep for
references"; "refuse `rm` outside this directory" - keeps checking the local disk
while the command runs somewhere else. It guards the wrong disk in both
directions, and neither failure announces itself.

Two facts make that fixable:

1. **Nothing is hidden from the guard.** Every `PreToolUse` hook receives the
   *original* tool input, not the previous hook's `updatedInput`. Another guard
   sees the real `mv ...`, not shunt's ssh wrapper, and can still block it.
2. **Where the session is routed is readable**, in one file:

```
<conf>/target.<session_id>     # contains the host alias; ABSENT means local
                               # PRESENT but naming no host (empty, a directory,
                               # unreadable) = UNKNOWN - bash is refused, never
                               # read as local
                               # <conf> is $SHUNT_CONF or ~/.config/shunt
```

It is written when the session switches (`@<alias>`) and removed on `@local`, so
it carries the session's *intent*, not its last command. **The name and the
meaning of that file are a public contract** and will not change without a note
here. The neighbouring `active-host.<session_id>` is a side-effect trace written
at execution time for status displays - not a contract; do not build on it.

What a guard does with that knowledge is its own decision - skip the check, or
apply a stricter one because a remote command deserves more scrutiny. One rule is
worth keeping either way: **fail open.** A guard that crashes because shunt is
absent or the file is unreadable breaks commands that had nothing to do with any
of this.

---

## 11. Design decisions at a glance

| Aspect | Decision | Why |
|---|---|---|
| Language | Python 3.11+, stdlib only | the work is network-bound; helpers deploy as source; `tomllib` removes the last dependency |
| Transport | ssh + ControlMaster, the only one | reuses what the installer already needed; no new attack surface. Every file operation needs ssh anyway, so a second transport would carry only the bare redirected bash - at the price of an open port, a shared token and code on both sides |
| Config | own `shunt.toml` under `~/.config/shunt` | address and identity in one place; split across two files, they break silently |
| pty | never (`-tt` measured and rejected) | it would kill interrupted commands, at the cost of hanging every pager and stdin reader (Sec. 4) |
| Long jobs | `systemd-run` transient units | native exit code, progress without touching the process, clean kill of the whole cgroup tree |
| Reading a remote file | explicit `shunt read` / `checkout` | file tools are never transparently redirected - reliability over magic (Sec. 3) |
| Writing a remote file | explicit `shunt edit` / `checkout` + `commit` | same reason; plus the SHA lock and verification a transparent rewrite could not offer |
| On the server | nothing but `python3` **3.3+** | nothing to deploy, upgrade, or leave behind. The floor is measured against the helpers' own code (`os.replace`), not inherited from the CLI's 3.11 - see Sec. 7 |

---

## 12. Known limits

Collected in one place, because each is a boundary rather than a pending fix:

- **An interrupted foreground command keeps running** on the far machine (Sec. 4).
- **cwd does not follow you across hosts** - the state file lives on each host,
  in the landing account's home, so the first command after a switch starts in
  `$HOME` there. Two aliases landing in the *same* account on the same machine
  are the exception: one home, one file, so the directory does carry over (Sec. 5).
- **The CLI does not share the session's cwd** - every subcommand starts in the
  ssh login directory (Sec. 5).
- **`shunt checkout` has no size cap**, while `edit` and `commit` do (Sec. 7).
- **`shunt bg` needs the right to create system-level systemd units** on the
  remote host (Sec. 6).
- **Audit trimming is lossy by design**, and a trim racing another session's
  append can drop a line (Sec. 5).
- **The mode covers bash only** - file tools, search tools and spawned agents get
  a warning, not a fence (Sec. 3).
- **A host with no `python3` cannot be edited** - `edit` and `commit` are
  unavailable there; the shell's own `command not found` comes back with a line
  from shunt naming what it costs. Everything else about that host works (Sec. 7).
- **The macOS ceiling (86 bytes) for the socket-length warning is derived, not
  measured.** Only Linux is measured (90, OpenSSH 9.6p1) - a macOS host near the
  edge may not behave exactly as the number promises (Sec. 4).
- **The CLI (`shunt run` / `read` / `edit` / `cp`) carries no `control_master`
  switch and no warning** for that same limit - its socket lives elsewhere and
  reuse can stop working there with nothing said (Sec. 6).
- **An ordinary destination already crosses that ceiling on macOS** - a user name
  plus a 38-character FQDN puts the CLI's own socket over the macOS budget today,
  and there is no opt-out flag for that path yet (Sec. 6).
