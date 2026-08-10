# shunt

**Transparent remote hands for an AI coding agent.**

`shunt` is a Claude Code `PreToolUse` hook that transparently redirects the
agent's bash commands to a chosen remote machine — the agent keeps running plain
`ls`, `grep`, `make`, and each one executes on the host you've switched to, with
per-session working directory kept across commands. Alongside the hook ships a small
`shunt` CLI for the things bash alone can't do well over a wire: `run`, `read`, `edit`,
`cp`, `bg`, `get`, `log`, `checkout` and `commit` — plus `hosts`, `install`, and a bare
`shunt` that prints its own map.

So: **commands run transparently** on the machine you chose · **remote files are edited
with your normal local tools** (`checkout` → edit → `commit`, with an optimistic SHA-lock)
· **long work goes to the background** and survives the disconnect.

One transport: **ssh** + `ControlMaster` — no open ports, no shared token, nothing
of shunt's installed on the server.

---

## How it works

A `PreToolUse` hook sees every bash command before it runs. While the session is routed to
a host, the hook hands back that command **rewritten**: wrapped in `ssh` to that host, with
the session's remembered working directory restored in front of it and a `trap EXIT` behind
it that carries the exit code home. Claude Code runs the rewritten string in its normal
sandbox, through the documented `updatedInput` mechanism — the agent goes on writing plain
`make`, and the rewritten `ssh …` line is what lands in the transcript (so a routed command
is not the place for a secret). `ControlMaster` keeps one multiplexed connection alive
between commands, so the handshake is paid once and the rest feel local-fast.

**Nothing of shunt's is installed on the far side** — no daemon, no agent, no package. It
does use ordinary tools that are already there: a POSIX shell for every routed command,
`python3` for `shunt edit` / `commit` (and the `install` check), `sha256sum` for `commit`,
`wget` for `get`, `systemd-run` / `systemctl` / `journalctl` for `bg`, and `rsync` on both
machines for `cp`. It also *writes* one thing over there: `$HOME/.cache/shunt/cwd-<session>`,
created `mkdir -m 700`, holding the directory you last worked in — swept of entries older
than 30 days once per switch, and nothing outside that name is touched.

Two things do not travel. `@<alias>`, `@local` and `@status` are bash lines the hook
intercepts and answers itself: that is the switch, one setting per session. And the `shunt`
CLI always runs **here**, opening its own connections for the work bash does badly over a
wire — reading and editing remote files, copying, background jobs, downloads, the audit
log.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the details.

---

## What it saves you

- **The ssh wrapping.** You stop writing `ssh host "…"` around every command — and stop
  forgetting it on the one command where it mattered.
- **The quoting.** Anything that survives one shell only to be parsed by another has to be
  escaped for both, and the escaping is where remote one-liners go to die. shunt quotes the
  payload once, mechanically, so pipes, quotes, `$(…)` and newlines arrive as you typed
  them.
- **The `cd` bookkeeping.** ssh gives every command a fresh login shell, so `cd build`
  followed by `make` builds in `$HOME`. shunt remembers the directory per session on the
  far host, and the next command starts where the last one left off.
- **The multiplexing setup.** No `~/.ssh/config` stanza to write and keep in sync: the
  ControlMaster socket is keyed per session *and* per destination — user, host and port —
  so two aliases pointing at one machine through different accounts never share a tunnel.

---

## What it guards

**Every guard here exists because something real happened.** None of them blocks you on
suspicion; each one exists because a specific silence cost somebody a specific thing, and
the cheapest fix was a sentence.

⚠ **Know what "refuses" means here.** With the two exceptions named below, the hook does not
deny the tool call. It replaces your command with an `echo` of the reason, so **the call
succeeds — exit 0, the reason on stdout** — and nothing runs on either machine. It is built that way on purpose: the hook
should never raise (a traceback in front of every bash command is worse than anything it
would report), and a refusal that ran the command locally would be the very accident these
guards exist to prevent. If it raises **anyway**, that is the second exception — the call
is denied rather than let through, because the harness would otherwise run your original
command; see *A crash in the hook stops the command* below. The **whole line** is replaced, chain and all: nothing in
`make deploy && ./verify` runs, not even `./verify`. The cost is the code — a refusal
reports **0**, so anything reading only that number (a `&&` in your *next* command, a
`set -e` script, an agent's next step) takes it for success. Read the output, not just the
exit code.

### For the session

| Guard | What it does |
|---|---|
| **The switch asks the machine** | `@web-01` opens a real ssh handshake before it answers: `— connected`, or `— switch written, but @web-01 did not answer the check …` carrying ssh's own reason, or `— could not check whether it answers (…)` when the question could not be put at all. About **3 s** against a host that is not there (**5 s** worst case). The routing is written *before* the probe, so a probe that hangs can never cost you the switch, and a machine that is merely rebooting stays yours. |
| **Both directions get a ticket** | The first command after any switch says where it is going: `it runs THERE, not here.` after `@alias`, `this one runs HERE, on the local machine.` after `@local`. Going home is a switch like any other, and the command right after it is the one that acts out of habit. |
| **Irreversible commands are announced** | While remote, a line running `rm`, `rmdir`, `mv`, `dd`, `shred`, `truncate`, `mkfs` (and `mkfs.*`), `wipefs`, `reboot`, `poweroff`, `halt`, `shutdown`, a recursive `chown -R` / `chmod -R`, `find … -delete`, `git clean` / `git … --hard`, `docker rm` / `rmi` / `prune`, or a `>` that truncates its target arrives with the machine named in front of it. It **warns and runs** — nothing is blocked, no exit code changes — and it speaks every time, not once per session. It looks past the words that stand in front of a command, `sudo`/`doas` included, and past a flag that keeps its value in the next word: `sudo -u www rm -rf /srv` used to read `www` as the command and say nothing at all. Two limits, both deliberate: it reads the command as written, so a `>` inside quotes (`echo "a > b"`) warns although nothing is truncated — silencing that would also silence `ssh host "… > log"`, where it is real — and `sudo -h somehost rm …` still passes unmentioned, because `-h` is `--help` and `--host` at once and a warner may not guess which. |
| **Three states of the routing file, not two** | The per-session routing file is missing (an ordinary local session, and the silence there is the point), names a host (remote), or **is there and names nothing** — a torn write, an emptied file, a directory in its place. The third never falls back to local: the command is refused and the reason is printed. For a tool whose default is "run it here", every quiet fall to the default is a fall to the wrong machine. The same refusal covers an alias that stops resolving — renamed, deleted, or a config that broke — because the session still says REMOTE while the command would run here. A switch to a host that is not configured is refused up front (`[shunt] unknown host: …`) and leaves the previous routing exactly as it was. |
| **An input the hook cannot read stops bash, not your hands** | Everything this hook decides, it decides from the JSON the harness hands it — so what happens when that JSON is not whole *is* the safety. Three answers, by what can still be told apart. **Unparsable, or with no `tool_name`:** the one case in this whole table where the call is **denied outright — exit 2, reason on stderr**, because a bash command and a file read are the same shape when the tool's name is what went missing, and letting bash through would run it *here*. **Readable but missing `session_id` or the command:** bash alone is refused, with the usual sentence; `Read`/`Edit`/`Grep`/`Glob` and `Agent` keep working and are told **every time** — they are the hands that repair the hook from inside a session that has no bash, and the once-per-session budget cannot be kept without a session id to key it on. A missing `session_id` used to fall back to the literal slot `default`, which two id-less sessions would then *share* — so a switch made by one could route the other's commands. |
| **A compound `shunt …` line is refused while remote** | And equally while the routing cannot be read. `shunt` runs *here* — and so would everything past a `;`, `&`, `\|`, a backtick, `$`, `(` or a newline, on the machine you believe you left. Redirections (`>`, `>>`, `2>`, `<`) are deliberately **not** in that class: they cannot hide a second command, and refusing them would refuse `shunt read @web-01 /etc/nginx/nginx.conf > local.txt`. In a local session the same line is ordinary work and runs. **The mirror shape is refused too:** `shunt …` reached *after* a separator — `cat payload.json \| shunt edit @web-01 /etc/nginx.conf --stdin` — carries no `shunt` prefix for the rule above to see, so the whole line used to be shipped to a machine where `shunt` is not installed. It fails loudly there, which is not the same as clearly: the half in *front* of the pipe had already run over there, on the far machine's files. |
| **A broken config directory shouts** | The switch ticket that cannot be written, read or removed; the routing that `@local` cannot clear; the sidecar files that cannot be cleaned up — each says so, with the errno and with what you lose until it is fixed. A `@<alias>` whose routing cannot be *written* is the loudest: it says the switch **did not happen**, names what the session is still on, and changes nothing. These repeat, deliberately off the once-per-session budget every other message here is kept on: that budget is itself a file in the directory that is broken. |
| **`exit 255` is named** | ssh's own failures exit **255**, and bare it reads as a verdict from whatever you thought you were running. One line on stderr says the transport failed and the command almost certainly never left — and the exit code is handed on untouched, 255 included. On every other code this guard adds nothing at all. |
| **The remembered directory speaks when it is gone** | The far side restores your session's directory before every command. If it cannot be entered — swept, unmounted, permissions changed — the command still runs, in `$HOME`, and a line on stderr says so by name — with both paths as the far shell expands them: `shunt: /srv/release cannot be entered (gone or not accessible); running in /home/deploy instead`. Silence here is how `rm -rf ./*` meant for a release directory happens in a home directory instead. It says CANNOT BE ENTERED rather than "is gone", because a wrong cause sends you looking in the wrong place. Once per switch it also *probes* the write, so a `$HOME` that cannot be written to is reported instead of costing you every `cd` from then on. |
| **A job systemd never heard of is not a finished job** | `systemctl show` invents an answer for a unit it does not know: every property comes back at its default — `Result=success`, `SubState=dead`, `ExecMainStatus=0` — at exit 0. So `shunt bg @web-01 --status shunt-typo` read exactly like a job that had completed cleanly, in the one hand here that runs with nobody watching the screen. `bg --status` now asks `LoadState` as a question: the properties are still printed — contradicted, not hidden — and a unit that is not there comes back **non-zero** with `shunt: no such job … the status above is systemd answering about NOTHING`. A host that cannot answer at all (no systemd, no permission) says *that* instead, rather than passing for "no such job". |
| **A crash in the hook stops the command** | The second place in this table where a call is **denied** — exit **2**, the reason *and the traceback* on stderr — and the only one that fires for a bug of shunt's own. A hook that raises exits non-zero-but-not-2, which the harness reads as a **non-blocking** error: it shows the message and runs your **original** command. On a routed session that is `rm -rf /srv/old`, written for a server, deleting the local tree — the accident this whole tool exists to prevent, arriving through shunt's own traceback. An unforeseen exception is now treated as the unknown state it is, and answered by the same question as the row above: **bash is denied**, while **every other tool runs and is told**, traceback included — they touch only the local disk, and an `Edit` on `pretool.py` is what repairs the hook from inside a session that has lost bash. Only a crash landing *before* `tool_name` could be read stops everything, because until then a bash command and a file read are the same shape. Deliberate refusals are untouched — they leave through a different door and always did. |
| **A re-checkout does not eat local edits** | `shunt checkout` over a file whose contents no longer match the SHA recorded when it was pulled **refuses** (exit 2) instead of replacing it. The path in was the tool's own advice: `commit` on a moved remote said "re-checkout, then re-apply your edits", and the re-checkout destroyed the edits it was telling you to re-apply — silently, with no undo and no second copy. The refusal names all three ways on (`commit` · `--abandon` · `--force`), and `commit`'s conflict message now names `--force` too. A file that is **gone**, or identical to what was pulled, is still refreshed without a word. |
| **A write that half-worked says so** | The far-side helpers used to answer for two steps they could not vouch for. A `chown` that failed was swallowed whole — the file kept the owner of whoever ran the helper, so the content landed perfectly and the **ownership** was the damage (an `authorized_keys`, a unit file). A directory `fsync` that failed was reported as `write failed` although it runs *after* the rename, so the write had in fact landed — and `commit` then left `base_sha` behind, inventing a `CONFLICT` on the next push. Both now come back as `warnings` beside a `status: ok`, printed by `shunt commit` and already visible in `shunt edit`'s output. |
| **Some bad arguments are refused rather than guessed** | Where a wrong argument used to produce a plausible-looking *answer*, it now refuses with exit **2**: `shunt log -n 5OO` (a letter O) printed the default 50 records, which look exactly like the whole truth; `shunt bg … --name` with no label handed `--name` to the far side as part of your command; `bg --status` / `--stop` with no job did the same. This is a list, not a rule — other malformed arguments still fail with a traceback. |

### For a spawned agent

| Guard | What it does |
|---|---|
| **The child is told where it stands** | A spawned agent's prompt arrives with a short frame appended: that a hook routes its bash to `@<alias>`, that its own file tools are **not** routed and stay on the local disk, and that `@local` is one session-wide setting shared with its parent and with any agent working beside it — so switching is never a private choice. Two cases drop the frame and send the parent's warning alone: an `Agent` input with no string prompt, and a reply that would exceed **9000** characters — a brief long enough to overflow it costs a note, never a warning. |
| **The parent is warned on every spawn** | Spawning while remote warns the parent that the child inherits the mode and will read absent local files as facts. No budget, no once-per-session: each spawn is a new agent that has been told nothing, and a budget shared with the file tools would let one `Grep` silence it. When the routing cannot be read at all, the **parent** hears something sharper — that the child's bash will be **refused** until the routing is settled. Nothing is appended to the child's prompt in that case, deliberately: unlike the remote state, this one announces itself to the child on its very first bash command, which comes back refused with both ways out named. |
| **File tools say they stayed home** | `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Grep` and `Glob` keep working on the **local** disk while the session feels remote. One line, once per host per session, names both remedies — `Grep` is called often enough that a line per call would become wallpaper, and wallpaper is silent exactly when it should speak. `@local` clears that budget, so coming back to the same host warns you again. `Grep` and `Glob` are on the list for the agent rather than for you: a person searching a machine types `grep` into bash and the hook sends it to the right place, while an agent reaches for the tool. |
| **Exit codes mean what they say** | The far side's code comes back through a `trap EXIT` that takes it first and spends it last, so no bookkeeping of shunt's can change what your command returned; `run`, `get`, `bg`, `checkout` and `install` hand a remote code straight back instead of flattening it to one number, and `cp` hands back the local `rsync`'s. Where a code *must* be translated, it is because the wire lies: `shunt edit`'s helper answers in JSON and always exits 0 — `not_found`, `ambiguous` and `conflict` included — so shunt reads the status instead and returns 0 only on `ok`. ⚠ `--dry-run` that finds its match is also `ok`, so it too exits 0 without changing anything: `shunt edit … --dry-run && deploy` deploys. |

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

**The matcher is wider than `Bash` on purpose.** `Bash` is the only tool ever sent to
another machine. `Agent` is touched in a second way — the child's prompt comes back with a
short frame appended, see [What it guards](#what-it-guards) — and the file and search tools
are matched only so the hook can *warn* that the mode does not cover them, see
[Where the mode stops](#where-the-mode-stops). Register only `Bash` and you keep the
redirection but lose both the frame and the warnings.

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
@web-01          →  [shunt] mode: REMOTE → web-01 (user@203.0.113.10) — connected
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
| `@<alias>`  | Route this session's bash to `<alias>` → `[shunt] mode: REMOTE → <alias> (<target>) — connected` (the tail is the live check; see [What it guards](#what-it-guards)) |
| `@local`    | Stop redirecting; bash runs locally again → `[shunt] mode: LOCAL` |
| `@status`   | Show current routing → `[shunt] REMOTE → <alias>`, `[shunt] LOCAL`, or `[shunt] UNKNOWN — …` when the routing file is there but names no host |

While routed, every bash command runs on the remote host. The hook keeps the
working directory per session (so `cd foo` then `ls` works as expected) and appends
each routed command to `~/.config/shunt/audit.log`.

`shunt ...` commands themselves always run locally — the CLI does its own
transport, so it is never redirected.

What the hook says while you work — the switch probe, the first-command ticket, the
warning before something irreversible, the refusal when the routing cannot be read — is
catalogued under [What it guards](#what-it-guards). Two things stay here: one is a
property of the transport rather than a guard, the other is the detail behind a
catalogue row.

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

**While the session is remote, a `shunt …` line may not carry a `;`, `&`, `|`, a
backtick, `$`, `(` or a newline.** Everything past the separator would run *here*, on the
machine you believe you left, so the hook runs nothing and says why — the quoted pipe
above is ordinary work in a local session and refused in a remote one. Send the
`shunt …` part as its own command (it runs here in any mode) and the rest as another, or
`@local` first if the whole line was meant for this machine. A form without those
characters — `shunt run @web-01 "grep -rn PATTERN /path"` — works in either mode.

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
as they were. The exit code follows the **answer**, not ssh: `not_found`, `ambiguous`,
`conflict` and `error` are non-zero, so `shunt edit … && deploy` will not deploy an
unchanged file. Two things to hold: a failed **transport** returns ssh's own code, and
`--dry-run` counts as success — a preview that found its match exits **0** while changing
nothing, so never chain a dry run. Write it as its own line, too: a `shunt …` line carrying
`&&` is refused while the session is remote (see under `shunt run` above).

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

**Quoting is the same as `shunt run`.** One argument is passed through verbatim, so a
quoted command keeps its pipes and redirects; several arguments are re-quoted, so a word
that carried a space stays one word. Until 2026-08-10 the words were joined with spaces
and **not** re-quoted, so `shunt bg @web-01 rm -rf "/var/lib/My App"` arrived over there
as two paths — see the CHANGELOG. Quoting the whole command as one argument, the way the
example does, worked before and works now. `--name` no longer eats the whole line either:
`shunt bg @host --name deploy` with no command left is refused instead of starting a
systemd unit around an empty string.

- `--name LABEL` — human-readable unit name, sanitized to `shunt-<label>`: lower-cased,
  every character outside `a-z0-9-` replaced by `-`, leading and trailing `-` dropped.
  Without the flag — or when the label sanitizes down to nothing, as `!!!` does — a random
  unit name is generated. **`--name` with no label at all is refused** (exit 2): it used to
  be handed to the far side as part of your command, so
  `shunt bg @web-01 "deploy.sh" --name` ran `deploy.sh --name` there.
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
redirected, and exactly six CLI subcommands — `run`, `edit`, `cp`, `bg` (every shape,
`--list` and `--status` included), `get`, `commit` (`sid=cli`, the subcommand in brackets).
`read` and `checkout` reach a host and are deliberately **not** logged: they only fetch.
`install` reaches one too and is not logged either — its own output is the record. `hosts`
and `log` never leave this machine at all. Default the last 50 **records**; `-n N` for the
last `N`. One command is one record, however many lines it was typed on.

**A `-n` that is not a number is refused** (exit 2) rather than quietly falling back to the
default: `-n 5OO` with a letter O printed 50 records, and 50 records look exactly like the
whole answer — which is how somebody concludes a command was never run on a server. There
is no upper bound; `-n 0` prints nothing, and a negative `N` is read as its absolute value.

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
- A **successful** one leaves it alone too, now, when it holds work: if the local file no
  longer matches the SHA recorded at checkout, `checkout` **refuses** (exit 2) and names
  the three ways on — `shunt commit <path>` to push the edits, `shunt checkout --abandon
  <path>` to keep the file and stop tracking it, or `--force` to drop them and take the
  remote copy. Add `--force` anywhere on the line. `commit`'s conflict message points at
  that flag, because a bare re-checkout now walks into this refusal.

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
| `SHUNT_EDIT_MAX_BYTES` | the helpers, **on the server** | Size ceiling, default **64 MiB** (`67108864`). ⚠ It is read in the environment of the **remote** process, and shunt sends no environment over ssh — setting it in your local shell does nothing. Set it for the ssh account on the far host. The two helpers measure different things: for `shunt edit` it caps the **remote file being opened**; for `shunt commit` it caps the **content being sent** — the raw bytes at exactly that number, with an inflated base64 payload rejected even before decoding, at twice it. Neither caps the pull direction — `shunt checkout` fetches whatever is there, so check the size of a big remote file first. Over the ceiling → use `shunt cp` and edit locally. |

---

## Security

`shunt` gives an AI agent a shell on another machine. That is the trust boundary:
whoever drives the agent can run anything on every host you add, with the rights of
the user in that host's target.

The transport is plain ssh + ControlMaster: **zero open ports, zero shared token**,
fully encrypted, nothing of shunt's installed on the server. ssh protects the
channel; it does not protect the machine from its legitimate user — so use a
dedicated key and the least-privileged account that can do the job.

⚠ **A rewritten command carries `permissionDecision: allow`.** That is how a `PreToolUse`
hook hands a replacement command back, and it means Claude Code does not ask you about the
bash it is about to run on the far host. If you rely on Bash permission rules or prompts as
a safety net, they are not the net while a session is routed — the least-privileged remote
account is. (An `Agent` spawn is deliberately different: the hook appends its frame without
any `permissionDecision`, so your own rules still decide whether the spawn happens.)

See [SECURITY.md](SECURITY.md) for the full threat model.

---

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT — see [LICENSE](LICENSE).
