# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [CalVer](https://calver.org/) (`YYYYMMDDHH`).

## [2026090112] - 2026-09-01

Two themes. **A justification outliving the design it was written for** - the comments and
the public docs described the environment the rewritten command runs in, a description
written for a transport that is gone. And **a diagnosis that named the wrong machine** - a
host name long enough to overflow ssh's connection-reuse socket made ssh refuse the
connection outright, and the failure read as the host being down.

### Changed

- **The comments and docs describe what the code does, not the environment it runs in.**
  A review of every statement about the agent's execution environment found several
  written for the `daemon` transport - which had to read a token at run time - and left
  behind when it went, growing broader each time they were restated. Code and
  documentation were gone through together: `pretool.py`, `SECURITY.md`,
  `ARCHITECTURE.md`, `AGENTS.md` and `CONTRIBUTING.md` now state that host, key path and
  session id are resolved in the hook process and only the finished string travels, and
  state nothing about what that environment permits - shunt does not depend on the answer.
  Behaviour is unchanged.

### Fixed

- **A long host name no longer misdiagnoses as an unreachable machine.** The socket that
  makes ssh's connection reuse possible is a file, and before binding it ssh writes a
  temporary path 17 bytes longer than the real one and renames it into place - so the
  usable limit is not the platform's raw socket-path size but that size minus 17: 90 bytes
  on Linux, 86 on macOS. The path is built from `/tmp/shunt-cm-` + the session id (36
  bytes) + `-` + `user@host:port` + `.sock`. A host name long enough to exceed it made ssh
  connect, authenticate, and only then fail to bind the socket (exit 255), and the
  transport epilogue then read that as "`@alias` is down or unreachable" - a diagnosis for
  a machine that had already answered. The switch now measures the path itself and, past
  the limit, says the true cause instead: use the IP in place of the long name, or set
  `control_master = false` for that host in `shunt.toml` - every command then runs there
  without connection reuse (each pays its own ssh handshake). Default is `true`; nothing
  changes for a host whose path already fits. This covers bash through the hook; `shunt
  run` / `read` / `edit` to the same host can still hit the limit, with no notice - the
  CLI keeps its own socket and has no flag yet.

## [2026083009] - 2026-08-30

Two themes. **A tool that states only what it has verified** - a refusal naming a
separator that was not one, a fuse that emptied the log it protects, an audit line a
session id could split, a check whose docstring promised more than it did. And **code
that runs on a machine nobody chose** - the two file helpers execute with the *server's*
`python3`, and real hosts answered in five different minor versions.
Entries that change behaviour you may have relied on are marked ⚠.

### Fixed

- **⚠ `shunt ...` lines with a shell variable are no longer refused.** In remote mode the
  guard that keeps `shunt` commands on the local machine treated a bare `$` as a place a
  second command could begin. It cannot: `$VAR` is an expansion, and no shell re-splits a
  command at a `;` that arrived out of one. So `shunt read @h "$f"` and
  `shunt cp $HOME/x @h:/tmp/` were refused - with a message claiming "everything past the
  `$`" would run here, which was not true of the line in front of it. Command substitution
  is now spelled out (`$(`) so it can still be *named* in the refusal; `(` caught it either
  way.
- **⚠ Line numbers for `shunt read` are ASCII digits, and nothing else.** The range was
  handed straight to `int()`, which accepts far more than anyone asked for: `-5:9`,
  `" 7 ":9`, `1_0:20`, `+8:9` and every Unicode decimal (`U+0663:U+0664`) all reached the remote
  `awk`. The contract is now written in the guard rather than inherited from Python's
  character table; those five shapes get a usage refusal.
- **⚠ A flag without its value refuses instead of crashing.** `shunt edit ... --expected`
  with nothing after it, `--expected abc`, and `shunt install ... --alias` / `--key` at the
  end of a line each left a raw Python traceback in front of the caller. They now answer
  with a usage line, through the same one place that already refused for `bg --name` and
  `log -n` - and each hand keeps its own wording.
- **⚠ The audit log cannot be split by a session id or a host alias.** Only the command was
  folded onto one line; the other two fields reached the record raw, and a newline in
  either broke the one-record-one-line equality that `shunt log -n N` and the trimmer both
  count on.
- **The trimmer can no longer empty the log.** When a single record was larger than the
  whole ceiling, the size cut walked back from the end, found nothing that fit, and left an
  empty slice - the fuse burning the thing it protects. It now keeps the newest record.
- **⚠ `shunt commit` reports a write it could not verify.** A verify-read that failed came
  back without `verified`, so a caller testing that one field got `None` from a helper that
  had just failed to prove anything: falsy by accident, which reads the same as falsy by
  answer until someone asks `is False`.
- **⚠ Malformed helper requests answer in JSON instead of tracebacks - or wrong results.**
  On the `shunt edit --stdin` path the JSON belongs to the caller, and several fields were
  read outside any guard. Two classes: a non-numeric `expected` or a non-string `file` /
  `old` raised a traceback on the far machine; worse, `base_sha: 7` was truthy and unequal
  to any digest, so the optimistic lock answered `conflict` - the helper telling you
  somebody else had edited the file, having verified nothing of the kind - while
  `dry_run: {}` is falsy and produced a real **write** where a preview was asked for
  (`dry_run: "false"` did the reverse). All of them now refuse with
  `bad request: ...` and touch nothing.
- **The double-rewrite guard reads its own constant** rather than a second copy of the
  marker string, and five unreachable `sys.exit(0)` lines after a function that always
  exits are gone.

### Added

- **⚠ The file helpers say what python they need.** They are shipped as source and executed
  by the *server's* `python3`; measured on real hosts rather than assumed: **3.7 to
  3.13** - five minor versions, none of them chosen by the tool. `MIN_PYTHON = (3, 3)` is
  now declared in the first lines of both - measured against the code they contain
  (`os.replace`, the atomic rename both stand on), **not** inherited from the CLI's own
  3.11, which hosts in that range fall below. Under the floor a helper answers with a diagnosis
  (`python 3.2 on this host, shunt file helpers need 3.3+`) before touching anything and
  exits non-zero. It used to answer with the symptom - `module 'os' has no attribute
  'replace'` - which sends a reader looking for a bug in shunt rather than at an old
  server. -> These two files are the one place in the package that avoids f-strings: an
  f-string in either would raise the real floor to 3.6 and make the guard unreachable.
- **⚠ `shunt install` asks which `python3` the server has,** prints it, and says plainly
  when it is below the helpers' floor - without refusing the registration. A host with no
  `python3` at all (exit 127) is likewise reached, reported and registered: the helpers are
  two subcommands out of eleven, and refusing would trade a whole machine for one feature.
- **A missing `python3` gets a line from shunt, not only from the remote shell.**
  `bash: line 1: python3: command not found` names the missing command and nothing about
  what it costs or where it was already reported. `edit` and `commit` now add that half -
  the 127 twin of the existing 255 transport notice - without touching the caller's exit
  code.
- **Tests for the far side.** The helpers' python floor, the guard's exact answer, that the
  atomic write's temp file is created in the **target's** directory (`os.replace` is atomic
  only within one filesystem), and that a write the machine refuses comes back named rather
  than as a traceback. Plus a three-mechanism check that the helpers stay parseable at the
  declared floor - a runtime guard can only catch APIs, and syntax from the future is a
  `SyntaxError` before its first line runs.

### Changed

- `ARCHITECTURE.md` gains the forms of a hook reply (including the note written into a
  spawned agent's prompt, and the two forms that are easy to miss when counting) and the
  far side's python floor. `SECURITY.md` states plainly that the hook edits one tool input
  besides `Bash`. The claim that *only* `Bash` is ever rewritten was true until that note
  existed and is now corrected where it appeared.

## [2026081019] - 2026-08-10

One theme: **the tool answering for something it did not do.** Places where a failure had
no voice - a hook that died, a command re-split on the way out, a pull that ate the work it
was asked to refresh, writes that reported the wrong thing about themselves, a warning that
looked at the wrong word, and a socket that trusted a directory anyone can write to.
Entries that change behaviour you may have relied on are marked ⚠.

### Security

- **The CLI's ControlMaster socket left `/tmp`.** ⚠ **Behaviour change for anyone already
  running shunt** - see *What you will notice* below. It was
  `/tmp/shunt-cm-cli-%r@%h:%p.sock`: a name anybody can work out, in a directory anybody can
  write to. `%r` is the *remote* account, so nothing in that name belongs to the local user
  - on a shared machine two different local users reaching the same target computed the very
  same path. The hook's own socket carries a per-session id and cannot be guessed; this one
  has to stay predictable, because a multiplexed socket is only worth having if the *next*
  `shunt` call finds the master the last one left behind. Randomising the name would have
  closed the hole by removing the reason the socket exists, so the **place** moved instead:
  `$XDG_RUNTIME_DIR/shunt/` when the environment offers one (per-user, `0700` by its own
  spec, on tmpfs, cleared at logout), `~/.cache/shunt/` otherwise, created with mode `700`.
  The name itself is unchanged. The hook's socket stays where it was, and that asymmetry is
  deliberate - it is documented in `ARCHITECTURE.md` and `SECURITY.md`.

  **What you will notice:** the first `shunt` call after upgrading opens a fresh connection
  instead of reusing the master under the old path - once, not every time. A master still
  listening on `/tmp` is orphaned by the move: nothing will reuse it, and it closes itself
  after `ControlPersist` (5 minutes), deleting its socket on the way out. There is nothing
  to clean up by hand; a stale `/tmp/shunt-cm-cli-*.sock` left by a master that was killed
  rather than closed can be removed at your leisure. If the private directory cannot be
  created, commands still run - what is lost is connection reuse, not the command.

  Measured while moving it, because it decides the design: a `ControlPath` that does not fit
  a unix socket is **fatal** - `ControlPath too long ... >= 108 bytes`, exit 255, but only
  after ssh has already connected and authenticated. 107 bytes is the longest that binds
  on Linux and macOS allows 103; the new
  location costs about 18 bytes more than `/tmp`, and an ordinary destination (a six-letter
  account, a 38-character FQDN, port 22) lands near 87. Falling back to `/tmp` for a longer
  one was rejected: it would restore the exposure silently, in the one case nobody watches.

### Added

- **A crash in the hook now stops the command instead of releasing it.** ⚠ **Behaviour
  change.** A hook that raises exits non-zero-but-not-2, and the harness reads that as a
  **non-blocking** error: it prints the message and runs your **original** command. On a
  session routed to a server that is `rm -rf /srv/old`, written for the far machine,
  deleting the local tree - the accident this tool exists to prevent, reached through
  shunt's own bug rather than through anything you did. Two fixes in this file's history
  have exactly that shape (`os.makedirs` outside its guard; the routing file written
  without an atomic rename), each closing one path after the fact.

  `main()` is now a thin roof over the decision function, and it answers by the same
  question the broken-input branch answers - who can still repair the hook:

  - **Bash** -> **denied**: exit **2**, the reason **and the traceback** on stderr, nothing
    run. It is the only tool that can act on the wrong machine, and whether this session is
    routed away is exactly what the crash leaves unknown.
  - **every other tool** -> **runs, and is told**, with the traceback inside the message.
    `Read`/`Edit`/`Grep`/`Agent` touch only the local disk, and an `Edit` on `pretool.py`
    is how the hook gets repaired from inside a session that has lost bash. Blocking these
    too would wall up the one door out, every session, until someone reached a terminal
    outside it.
  - **a crash before `tool_name` could be read** -> everything is stopped, because until
    the tool is known a bash command and a file read are the same shape.

  Deliberate answers (a rewrite, a refusal, a switch, the unreadable-input denial) leave
  through `SystemExit` and are untouched.

- **`shunt checkout` refuses to overwrite local edits.** ⚠ **Behaviour change.** A
  re-checkout replaced the local file whole. When that file held uncommitted work, the work
  was gone - no undo, no second copy - and the path in was the tool's own advice: `commit`
  on a moved remote printed *"re-checkout to pick up remote changes, then re-apply your
  edits"*. A checkout whose local file no longer matches the SHA recorded when it was
  pulled now refuses (exit **2**) and names the three ways on:

  ```
  shunt: refusing to overwrite local edits: /home/you/.config/shunt/checkouts/web-01/etc/nginx.conf
    this file no longer matches what was checked out, so it holds changes that exist
    nowhere else - and a checkout replaces it whole.
      checked-out sha: 1f0a...
      local file sha : 9c72...
    - keep them and push  -> shunt commit /home/you/.config/shunt/checkouts/...
    - keep them, stop tracking -> shunt checkout --abandon /home/you/.config/shunt/checkouts/...
    - DROP them, take the remote copy -> shunt checkout @web-01 /etc/nginx.conf --force
  ```

  `--force` may be written anywhere on the line. A local file that is **gone**, or one
  identical to what was pulled, is still refreshed silently - the first is how a deleted
  checkout is repaired and must stay possible. The gate sits before anything is written
  and before ssh is dialled. `commit`'s conflict message now points at `--force` instead of
  at a bare re-checkout, which would walk into this refusal.

### Fixed

- **A flag's value is no longer mistaken for the command it hides.** `sudo -u www rm -rf
  /srv/x` produced **no warning at all**: the scan steps over the words that stand in front
  of a command (`sudo`, then anything beginning with `-`), so it stepped over `-u` and took
  `www` - the flag's value - for the command, and never looked at the `rm` behind it. The
  loudest line this hook has was silent on the shape that most often carries a destructive
  command: one run as another account. Options that keep their value in the next word are
  now stepped over with it, for `sudo` and `doas`, bundles included (`-nu www`). The list is
  bounded by one rule - an option whose value cannot itself *be* a command - which is why
  `env` is not in it (`env -S 'rm -rf /x'` hands over a command, and skipping it would skip
  the answer) and why `sudo -h` is not either (`--help` and `--host` at once, decided by what
  follows). `-uwww` and `--user=www` carry the value inside the word and always worked.
- **`shunt commit` says why the far-side helper never answered.** The helper is run with its
  output captured, but only **stdout** was read. When it did not get as far as answering - no
  `python3` over there, the process killed, a permission that stopped it at the first import
  - stdout is empty and everything explaining it is in stderr and the exit code, both of
  which went on the floor. The report was `ERROR /path - unexpected response: ` and nothing
  else. It now names the ssh exit code, says *(nothing on stdout)* rather than trailing off,
  and prints the last lines of the far side's stderr under the file they belong to. Only on
  that branch: on a write that landed, remote stderr is login banners, and a commit that
  succeeded must not read like trouble.
- **The edit helper's read-back can fail without taking the helper down.** After writing, it
  reads the file again to prove the content landed. That read was unguarded, so a permission
  changed mid-flight, an I/O error, or the path swapped for a directory killed the helper
  with a traceback where its JSON answer belongs - and the caller read *that* as
  `unexpected response` about a file that **was** already written: precisely the lie the
  read-back exists to prevent, arriving through the read-back. It now answers
  `verify-read failed: ...` beside `status: error`, keeping the facts already established
  (which file, how many matches) and reporting `verified: false` with a null `new_sha` -
  unproven, which is not the same claim as *proven wrong*. Its twin in the write helper had
  been guarded all along; this closes the asymmetry.
- **The audit line for `shunt bg` quotes the way the command does.** The command sent to the
  far side is assembled with `shlex.join`; its log line was assembled with a plain space
  join, so an argument containing a space was recorded as two. The log is the standing answer
  to *what did I run on somebody else's machine*, and a witness that quotes differently from
  the executor is worse than no witness. The line still records the invocation as typed - so
  `--list`, `--status` and `--stop` stay in the log - and now reads back with `shlex.split`
  into exactly the arguments that were given. Log lines written before this change are
  unaffected and still say what they said.
- **`shunt bg` no longer re-splits the command it was given.** It joined the remaining
  arguments with spaces and handed the result to `bash -lc` over there, so
  `shunt bg @web-01 rm -rf "/var/lib/My App"` arrived as `rm -rf /var/lib/My App` - two
  paths, neither of them the one that was typed, on the hand that runs with nobody watching
  the screen and does not come back to be corrected. It now assembles the command exactly
  as `shunt run` does: one argument verbatim (pipes and redirects intact), several
  re-quoted. Quoting the whole command as one argument worked before and still works. ⚠ The
  README documented the old behaviour explicitly; that paragraph is corrected.
- **`shunt bg @host --name LABEL` with no command left is refused.** `--name` is stripped
  out of the command, so a line that was nothing but the flag joined to `""` and started a
  systemd unit around an empty string: `JOB=shunt-deploy`, and nothing ran. The sibling
  refusals (`--status` / `--stop` without a job) already answered this shape.
- **A `chown` that fails is no longer swallowed.** The far-side helpers write to a temp
  file and rename it into place, so the file takes the owner of whoever ran the helper
  unless `chown` can put it back. When it could not, a bare `except: pass` meant the
  content landed perfectly and the **ownership** was the damage - on an `authorized_keys`
  or a unit file, that is the whole of the accident. It now comes back as a warning beside
  `status: ok`, naming both the intended and the actual owner.
- **A failed directory `fsync` is no longer reported as a failed write.** It runs *after*
  the rename, so by the time it can fail the new content already **is** the file; what is
  lost is durability, not the write. Reported as `write failed`, it made `shunt commit`
  leave `base_sha` at the old value - and the next commit then read a remote SHA that no
  longer matched and announced a `CONFLICT` invented by a write that had succeeded. Also a
  warning beside `status: ok` now.
- **`shunt commit` prints the helper's warnings.** `shunt edit` shows them for free (it
  prints the helper's JSON verbatim); `commit` parses that JSON and dropped anything it did
  not print - the same silence one layer up. They are warnings, not verdicts: the exit code
  and `base_sha` still say the write landed, because it did.

## [2026081017] - 2026-08-10

One theme again, one step further in: **shunt refusing to answer from something it has not
read.** The previous release taught it to say which machine you are standing on; this one
covers the cases where it cannot tell - a hook input that arrives broken, a systemd unit
that does not exist, a line whose second half cannot run where the first half is going.
Two entries change behaviour you may have relied on; both are marked ⚠.

### Added

- **The hook now has an answer for an input it cannot read.** ⚠ **Behaviour change.**
  Everything this hook decides comes out of the JSON the harness hands it, and an
  unreadable one used to mean silence - after which the harness ran your **original**
  command. On a session routed to a server, that is the accident the whole tool exists to
  prevent: `rm -rfv /srv/old-release`, written for the far machine, deleting the local
  tree, because the one file that could have said "you are routed away" was the file that
  could not be read. Three answers now, by what can still be told apart:

  - **Nothing parses, or there is no `tool_name`** -> the call is **denied**: exit **2**,
    the reason on stderr. This is the only place in shunt that denies a tool call. With no
    tool name, a bash command and a file read are the same shape, and only one of them is
    safe to let through. It costs you the file tools too, in that one state, and the way
    back in is a terminal outside the session.
  - **Readable, but `session_id` or the command is missing** -> **bash alone** is refused,
    with the usual sentence in place of your command:

    ```
    [shunt] hook input incomplete (session_id) - routing unknown, remote commands
    disabled, command NOT run; fix the hook (file tools still work).
    ```

  - **`Read` / `Write` / `Edit` / `Grep` / `Glob` / `Agent`** keep working in that second
    state and are told **every time**. They are harmless on the local disk, and they are
    what repairs the hook from inside a session that no longer has bash - an `Edit` on
    `pretool.py` needs no shell at all. The once-per-session budget every other message
    here is kept on cannot be used: it is a file named after the session, and the session
    id is what is missing.

- **`bg --status` no longer dresses a missing unit as a finished job.**
  `systemctl show` invents an answer for a unit it has never heard of - every property at
  its default, `Result=success`, `SubState=dead`, `ExecMainStatus=0`, at exit 0 - so a
  mistyped job name was indistinguishable from a clean completion, in the one hand here
  that runs with nobody watching. `LoadState` is now asked as a question: the properties
  are still printed, **contradicted rather than hidden**, and the call comes back non-zero:

  ```
  shunt: no such job shunt-typo on this host - the status above is systemd answering
  about NOTHING, not about a job that ran. `shunt bg @<host> --list` shows the jobs it knows.
  ```

  A host that cannot answer at all - no systemd, no permission - says *that* instead, and
  is not allowed to pass for "no such job".

- **`shunt ...` reached after a separator is refused while remote.** The mirror of a guard
  that already existed for lines *beginning* with `shunt`. A line such as
  `cat payload.json | shunt edit @web-01 /etc/nginx.conf --stdin` carries no `shunt`
  prefix, so nothing looked at it, and the whole line was shipped to a machine where
  `shunt` is not installed. It failed loudly there - which is not the same as clearly:
  what failed was the `shunt` half, while the half in front of the pipe had already run
  over there, against the far machine's files. In a local session the same line is
  ordinary work and still runs.

- **The config directory sweeps itself.** The far side has swept its per-session files
  since they were introduced; this side never did, so `~/.config/shunt/` collected
  `active-host.<id>` - `warned.<id>` - `switched.<id>` for every session that ever went
  remote. They are now removed once they are 30 days old, on a **switch** - the same rare
  moment the far side's sweep is paid for, never in front of an ordinary command.
  `target.<id>` is deliberately **not** swept: it is written once, at the switch, so an old
  timestamp there means a session that switched a while ago, not a session that is gone -
  and taking it away would send that session's next command to the local machine without a
  word.

### Fixed

- **A rewritten command keeps the rest of your request.** ⚠ **Behaviour change, in your
  favour.** The hook handed the harness only the rewritten `command`, and measurement
  showed the other fields did not survive the trip: a Bash call carrying
  `run_in_background: true` and `timeout: 600000` came back in the **foreground**, with the
  timeout back at its default - so a ten-minute job on a server was cut short for no
  visible reason. The whole input is now handed back with only `command` changed. (The
  hook reference describes `updatedInput` as *merged*; the harness measured here
  *replaced*. Passing everything back is correct under either reading, which is why the
  fix does not depend on that question.)

- **A session with no `session_id` is no longer routed by somebody else's switch.** The
  hook fell back to the literal slot `default`, so two sessions arriving without an id
  shared one routing file - and a switch made by either would send the other's commands to
  that host. There is no fallback now; that input is refused (see above).

- **Two comments in the source claimed "only Bash is ever rewritten".** It has not been
  true since a spawned agent's prompt started carrying a frame: `Agent` calls are rewritten
  too. A stale comment is read far more often than a manual.

## [2026081009] - 2026-08-10

One theme: shunt saying **which machine you are standing on - before the command, not
after it.** One entry is the exception that proves the rule: when the transport itself
fails there is nothing to say beforehand, so it is said after. Two entries change timing
or an exit code you may have relied on; each is marked ⚠.

### Added

- **`@<alias>` now asks the machine whether it answers.** ⚠ **Behaviour change** - the
  switch used to be pure bookkeeping and returned in milliseconds; it now waits for a real
  ssh handshake, about **3 seconds** against a host that is not there (5 in the worst
  case). What you see when it works:

  ```
  [shunt] mode: REMOTE -> web-01 (deploy@web-01) - connected
  ```

  and when it does not:

  ```
  [shunt] mode: REMOTE -> web-01 (deploy@web-01) - switch written, but @web-01 did not
  answer the check - nothing will run until it does. ssh: deploy@web-01: Permission
  denied (publickey,password).
  ```

  **The switch stands either way.** A host may be rebooting, and a session that has said
  where it wants to be is not sent home behind its own back - the routing is written
  *before* the probe, so a probe that hangs can never cost you the switch. There is a
  third line, `- could not check whether it answers (...)`, for when the check itself could
  not be made: a failed probe proves only that *this check* did not get through, and a
  refused key, a changed host key and a broken login shell all answer from a machine that
  is perfectly awake. The reason always comes from ssh, never guessed.

  **Why:** the old switch wrote a file and said REMOTE. The first thing that found out
  whether the host was reachable was your next real command - at the moment you had
  stopped thinking about machines. The check also warms the connection the next command
  wants.

- **`@local` leaves the same one-shot ticket `@<alias>` does, and the first command after
  it says where it is going:**

  ```
  note: shunt: first command since `@local` - this one runs HERE, on the local machine.
  (said once per switch)
  ```

  Going home is a switch like any other, and the command right after it is the one that
  acts out of habit - the habit just points the other way. The dance
  `@a -> @b -> @local -> @c` used to announce every step except the one that comes back. One
  file holds both directions, so the last switch wins.

- **A spawned agent is told where it is standing.** The *parent* has been warned on every
  `Agent` spawn for a while; the child - the one that would actually act on it - was told
  nothing. It ran `ls`, read a disk it had never seen, and reported what it found as the
  truth about the world. The child's prompt now arrives with a short frame appended: what
  routes its bash, that its own file tools are **not** routed and stay on the local disk,
  and that `@local` is one session-wide setting shared with its parent and with any agent
  working beside it - so switching is never a private choice. The parent's warning still
  goes out, in the same response. If the frame cannot be written (an unusual `Agent` input,
  or a brief long enough to overflow the reply), the parent's warning goes out alone: the
  cost is a note not written, never a warning lost.

- **Three state failures now shout instead of passing in silence.** All three say the same
  thing - your shunt config directory is broken - and each used to cost you a message you
  never knew you were owed:

  - the switch ticket **cannot be written**: the switch stands, but there will be no
    reminder on the next command, and the far side's once-per-switch housekeeping does not
    run;
  - it **cannot be removed**: it stands, so the reminder repeats on every command (and the
    line says that it will), and the housekeeping is skipped rather than bought again on
    every command from then on;
  - it **cannot be read**: nothing will be said about which machine the command runs on,
    and the line says whether it will be back.

  These repeat deliberately, off the once-per-session budget every other message here is
  kept on - that budget is itself a file in the very directory that is broken. For a fault
  of the class "fix it now", repeating is the behaviour that fits.

- **`exit 255` now says what it means.** When ssh cannot reach the host - connection
  refused, no route, a machine that went down mid-session - ssh exits **255**, and until
  now that number arrived bare. It reads as a verdict from whatever you believed you were
  running, and the search for the bug starts in the wrong program. The rewritten command
  now carries a local epilogue that looks at ssh's own exit code and, on 255 alone, adds
  one line to stderr:

  ```
  [shunt] exit 255 = ssh transport failure - @web-01 is down or unreachable; your command
  almost certainly never ran. Check the host, or @local.
  ```

  **Not a behaviour change:** the exit code is handed on untouched - 255 stays 255 - and
  every other code (0, 1, 42) passes through with nothing added to either stream. Nothing
  is read back from the far side; only the number ssh hands to the local shell.
  "Almost certainly" is literal rather than cautious: a remote command is free to exit 255
  on its own account, rarely, and shunt does not state what it has not verified.

### Fixed

- **`shunt bg @host --list` now reports a listing that could not be made.** ⚠ **Behaviour
  change** for anything reading its exit code. The command ended in `|| true`: a far side
  with no systemd, no permission, or a bad invocation came back **exit 0 with no output** -
  indistinguishable from "this host has no jobs". `systemctl list-units` already exits 0
  when the glob matches nothing, so the guard never bought the empty listing anything; it
  paid out only when the question could not be answered at all. An empty list is still a
  success; a real failure is now non-zero, and systemctl's own reason reaches your
  terminal. This closes the family the previous release opened, where `bg --stop`,
  `log -n` and `bg --name` each stopped reporting a success they had not verified - those
  three are unchanged here and still stand.

- **A failed `@<alias>` check no longer stutters `ssh: ssh:`.** Whatever ssh gives as a
  reason is attributed to ssh, so it cannot be read as shunt's own verdict - but ssh's
  transport failures already open with that attribution, so the line came back doubled:

  ```
  ... did not answer the check ... ssh: ssh: connect to host 203.0.113.9 port 22: Connection
  timed out
  ```

  The prefix is now added only where it is missing. The other shape of failure -
  `deploy@web-01: Permission denied (publickey,password)`, which names an account rather
  than a program - still gets it. Cosmetic: it never lied and never went quiet.

### Changed

- **Internal, visible only if you import the module:** `pretool._remote_script()` and
  `pretool.ssh_command()` now take `housekeeping=` where they took `switched=`. Nothing on
  the command line changes. The old name said "a switch happened", which was never what
  the flag decided: it means "this command is the one that pays for the far side's
  once-per-switch housekeeping" - true only when the ticket was actually punched, not
  merely present.

## [2026080920] - 2026-08-09

Most of this release is the hook learning to say **where a command actually went** - and
refusing when it cannot say. Several things that used to happen in silence are now loud,
and six of them change behaviour you may have relied on; each is marked ⚠. Two ask
something of you: the session's remote working directory moves and **is not migrated**,
and a `shunt ...` line with a `;` in it is refused while the session is remote. Scripts
reading exit codes should read the entries for `bg --stop`, the checkout manifest, `log
-n` and `bg --name` - all four changed one.

### Changed

- **The session's remote working directory is remembered in
  `$HOME/.cache/shunt/cwd-<session-id>`** on the far host, no longer
  `/tmp/shunt-cwd-<session-id>`. ⚠ **Behaviour change**, and **nothing is migrated**: the
  first command after upgrading starts in the ssh login directory (usually `$HOME`)
  instead of where you left off - once per session, per host, per account. `cd` again and
  the new file takes over. It happens without a message, because a missing state file is
  also what a brand-new session looks like; there is nothing wrong to report.

  **Why:** `/tmp` is shared, and the path carried only the session id. Two accounts on one
  machine - `deploy@web-01` and `root@web-01`, which the config allows - reached for the
  same file, so one of them read the *other's* working directory or failed to write its
  own without a word. `$HOME/.cache` is per-account by construction; the directory is
  created `mkdir -m 700`. The old `/tmp/shunt-cwd-*` files are not read, not moved and not
  deleted - they are orphans now, and `/tmp` clears them in its own time.

  Two smaller things ride along. shunt sweeps `cwd-*` files older than 30 days out of that
  directory on the first command after a switch - nothing outside that name is touched.
  And if the directory cannot be written, it says so once instead of losing every `cd`
  from then on in silence:

  ```
  shunt: cannot write /home/you/.cache/shunt - this session will not remember its
  working directory (every command starts at $HOME)
  ```

### Added

- **A warning before a command that cannot be taken back.** While a session is on a host,
  a line running `rm`, `rmdir`, `mv`, `dd`, `shred`, `truncate`, `mkfs*`, `wipefs`,
  `reboot`, `poweroff`, `halt` or `shutdown`, a recursive `chown -R` / `chmod -R`, a
  `find ... -delete`, a `git clean` or `git ... --hard`, a `docker rm` / `rmi` / `prune`, or a
  `>` that truncates a file, now arrives with a line naming the machine:

  ```
  ⚠ shunt: you are on @web-01 - this runs THERE and cannot be taken back: git ... --hard,
  docker ... rm. Check which machine you meant; `@local` first if it is this one.
  ```

  It **warns and runs**: nothing is blocked and no exit code changes. **Why:** every other
  guard here answers "which machine am I on?" when you ask it. This one answers when you
  do not ask, at the one moment the answer is expensive. And unlike the warnings that
  speak once per session, this one speaks **every time** - a destructive command is not a
  state you should get used to.

  `> /dev/null` and its variants are excluded; redirecting into the bin truncates nothing
  that matters. A shell comparison like `[[ $a > $b ]]` will still trip it - on a warning
  that costs one line of text, the false alarm is the safer side to be wrong on. The
  warning travels in the **same** hook response as the redirected command, which is a
  shape the hook did not emit before: `additionalContext` and `updatedInput` together.

- **A reminder on the first command after `@<alias>`:**

  ```
  note: shunt: first command since `@web-01` - it runs THERE, not here. (said once per switch)
  ```

  The switch is a line you type and forget; the command after it is where believing you
  are still at home does its damage. It is spent on that first command and does not come
  back until you switch again. A new file, `switched.<session-id>`, holds it next to
  `target.<session-id>` in `~/.config/shunt/`.

- **A working directory that has gone away now says so** - `shunt: /srv/release-42 cannot
  be entered (gone or not accessible); running in $HOME instead`. The fallback to `$HOME`
  is unchanged; it simply used to be silent, so a command written for one directory ran in
  another and returned perfectly ordinary output from the wrong place.

### Fixed

- **A `shunt ...` line with something after it is refused while the session is remote.** ⚠
  **Behaviour change.** `shunt ...` runs on *this* machine - that is what it is for - but so
  did everything past the `;`, and that part never asked to. On a session routed to a
  server, `shunt hosts; rm -rf /var/log/*` deleted the **local** log directory without a
  word, because the whole line was handed back unrewritten. A line beginning with `shunt`
  that also contains `;`, `&`, `|`, a backtick, `$`, `(` or a newline now runs nothing and
  says why.

  The cost is real and belongs here: legitimate one-liners go with it. `shunt run @web-01
  "systemctl status nginx | head"` is refused while the session is remote, and so is
  `shunt edit @host f "a" "b;c"` - the separator is looked for in the raw text, inside
  quotes included. Send the `shunt ...` part as its own command (it runs here in any mode)
  and the rest as another, or `@local` first if the whole line was meant for this machine.
  A plain redirect is not in the class, so `shunt read @host /etc/nginx.conf > local.txt`
  still works.

- **A routing file that cannot be read is refused, not read as "local".** ⚠ **Behaviour
  change.** `target.<session-id>` had two readings - a host, or nothing - and a file that
  was empty, truncated, a directory or unreadable counted as *nothing*, which means
  **local**. A session that had been routed away therefore came home without saying so,
  and `@status` confirmed `LOCAL` with a straight face. There is a third reading now: bash
  runs nothing and names the file, `@status` answers `UNKNOWN`, and the file tools warn
  once that they are reading the local disk either way. `@local` to be local on purpose,
  or `@<alias>` to route again.

  A half-typed alias is *not* this state - `@web` for `@web-01` is refused at the switch
  itself (`[shunt] unknown host: web`) and the routing file is left untouched. The file is
  also written atomically now (temporary file, then rename), so an interrupted switch can
  no longer leave behind the empty file this entry is about.

- **A switch that fails says so, instead of failing quietly or lying.** A read-only config
  directory made `@web-02` exit without a word - the session stayed where it was while you
  believed it had moved - and `@local` printed `[shunt] mode: LOCAL` unconditionally,
  including when it had just failed to remove the file that keeps you remote. Both now
  report the failure and, more usefully, where you actually are:

  ```
  [shunt] switch to @web-02 FAILED - could not write
  /home/you/.config/shunt/target.s1 (Permission denied). Nothing changed; the session is
  STILL on @web-01. Fix that and try `@web-02` again.
  ```

  ⚠ The atomic write brings one new failure mode: switching now needs a **writable config
  directory**, not merely a writable file, because the temporary file is created beside the
  target. It fails loudly, in the shape above. On the other side, an *empty* directory
  sitting where `target.<session-id>` belongs is now removed rather than being a trap with
  no way out - bash refused, and `@local` was powerless to lift it.

- **`shunt bg --stop` reports what systemd actually did.** ⚠ **Behaviour change** for
  anything reading its exit code. The stop and the `echo` were joined by `;`, so `stopped`
  was printed and `0` returned no matter what happened - a mistyped unit name answered
  exactly like a real one while the job kept running. `stopped <job>` is now printed only
  on success, and systemctl's own message and exit code come back otherwise. Stopping a
  job twice is one of those failures: `systemd-run --collect` discards the unit when it
  ends, so the second `--stop` gets `Unit ... not loaded.` and exit **5**.

- **A checkout manifest that cannot be read stops the operation.** ⚠ **Behaviour change.**
  Every read of it caught every exception and returned an empty manifest - and an empty
  manifest means "nothing is checked out" further down. So a corrupt file made `checkout
  --list` print `(no checkouts)`, and `commit` say `no checkouts in manifest` and exit
  `0`, while the file recording every checkout's `base_sha` sat there unreadable. It now
  exits `2` and names the parse error:

  ```
  shunt: cannot read the checkout manifest
  /home/you/.config/shunt/checkouts/manifest.json: Expecting property name enclosed in
  double quotes: line 1 column 2 (char 1)
    it holds every checkout's base_sha, so nothing is listed, committed or checked out
    until it can be read (move it aside to start over - the local files stay where they
    are).
  ```

  Valid JSON of the wrong shape goes through the same door: `null` used to pass for
  "empty" and a list crashed with a traceback.

- **A `checkout` that meets a corrupt manifest no longer overwrites your local file
  first.** The manifest was loaded *after* the fetched copy had been moved into place, so
  the refusal above arrived having already destroyed the local edits it went on to claim
  it had left alone. It is read before anything is fetched: the file, the manifest and the
  absence of a stray `.part` beside it are all left exactly as they were.

- **`shunt log -n` and `shunt bg --name` refuse a value they cannot use.** ⚠ **Behaviour
  change**; both exit `2` and do nothing. An unparseable `-n` was swallowed and fell back
  to **50** records - and fifty records look like a complete answer, which is how someone
  concludes that a command was never sent to a server at all. `--name` with no label left
  the flag in the command, so `shunt bg @host "deploy.sh" --name` sent `deploy.sh --name`
  to the far machine and ran it.

## [2026080707] - 2026-08-07

### Added

- **`Grep` and `Glob` are warned about too.** They read the same **local** disk as `Read`
  while the session feels remote, and until now they did it in silence. The gap is mostly
  an **agent's**: a person searching a machine types `grep` or `find` into bash, which the
  hook redirects correctly - an agent reaches for the `Grep` tool instead, far more often
  than for `Read`, and reads local hits as facts about the far machine.

  **This changes the hook registration.** The matcher is now
  `Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob` - see the README. Keeping
  the old one leaves the redirection working exactly as before and simply loses these two
  warnings; `shunt install` now prints the wider line. The tuple behind it was renamed
  `FILE_TOOLS` -> `LOCAL_DISK_TOOLS`, because searching is not editing a file.

  The warning is still **one per host per session**, now shared by all seven tools: a line
  on every `Grep` call would become wallpaper, and wallpaper is silent exactly when it
  should speak. So the single line names both ways out - remote file ->
  `shunt read/edit`, remote search -> `shunt run @host "grep -rn PATTERN /path"`.

### Fixed

- **Documentation: the CLI does not share the session's remote `cwd`.** The `shunt get`
  entry said its default destination `.` was "the remote cwd". It is not: the per-session
  directory lives in a state file only the hook reads, so every `shunt run` / `read` /
  `edit` / `get` starts in the ssh **login** directory (usually `$HOME`). Nothing changed
  in the code - the promise did. Give the CLI absolute paths.

## [2026080623] - 2026-08-06

Five of these were **silent**: they answered `ok`, or said nothing at all, while doing
something other than what was asked - on other people's files and other people's
machines. Two of them change behaviour you may have relied on; both are called out below.

### Fixed

- **`shunt edit` no longer damages a file it reports as edited.** The helper decoded the
  file (`errors="replace"`), edited the *text* and wrote the text back. So every byte that
  was not valid UTF-8 came back as U+FFFD - one latin-1 character in a comment was enough
  to corrupt a config - and a file with mixed line endings was converted **whole**. Both
  were reported as `{"status": "ok", "verified": true}` with a diff showing only the line
  you asked about, because the diff was computed *before* the conversion.

  The match and the replacement now happen on the **raw bytes**: nothing outside the
  matched region is rewritten, and the diff is computed from the bytes on disk and the
  bytes about to be written. Line-ending tolerance is unchanged in effect - the *needle*
  is retried as all-LF and as all-CRLF, and `normalized: true` still means "matched in a
  variant" - but the file is no longer rewritten into another style. The honest edge: the
  needle arrives as JSON and can only be UTF-8, so a needle that is not returns
  `not_found` instead of a guess; for latin-1 **text**, use `checkout`/`commit`, which
  never decode.

- **A session routed to a host that no longer resolves now runs nothing.** ⚠ **Behaviour
  change.** Previously a renamed alias or a broken `shunt.toml` made the hook fall back to
  running the command **locally** - while `@status` still said REMOTE. A `rm -rf
  /var/log/*` meant for a server deleted the local one. The hook cannot raise (a traceback
  in front of every bash command is worse than anything it would report), so it takes the
  third way it already uses for an unknown `@alias`: the command is replaced by the reason
  nothing ran - `[shunt] cannot resolve @web-01 - command NOT run ...`.

- **A failed `checkout` no longer destroys the local file it was refreshing.** The pull
  opened the local path for writing, which truncates it the moment the process starts -
  before ssh has said a word - and then unlinked it when ssh failed. Checking a file out
  again over an unreachable host therefore threw away every uncommitted edit in it. The
  pull now lands in a `.part` file beside the target and is moved into place only on
  success.

- **`shunt edit` exits non-zero when the edit did not happen.** ⚠ **Behaviour change** for
  anything reading its exit code. The helper answers in JSON and always exits 0 -
  `not_found`, `ambiguous` and `conflict` included - and the CLI passed ssh's code straight
  back, so `shunt edit ... && deploy` deployed an unedited file. The code now follows the
  status: `0` only for `ok`. A transport failure keeps ssh's own code. The JSON still goes
  to stdout, unchanged, so the reason stays readable.

- **`--dry-run` is honoured on the `--stdin` path too.** It was read only on the OLD/NEW
  path, so `shunt edit @host <file> --stdin --dry-run` **wrote** - with a flag on the
  command line asking it not to. It may only add safety: a payload that already asks for a
  dry run is never turned into a write.

- **The ControlMaster socket is keyed on the ssh user as well** (`%r@%h:%p`, the shape the
  CLI already used). Two aliases pointing at one machine with different accounts -
  `deploy@web-01` and `root@web-01`, which the config allows - shared the first one's
  master connection, so the second ran as the **wrong account**, silently, with entirely
  plausible output.

- **The audit log counts commands, not lines.** A multi-line command was written raw, so
  one command became several lines - and every reader of the log counts lines: the trimmer
  dates its cut from the first ten characters of one, `shunt log -n N` showed N of them.
  A continuation line starting with a space fell out of a cut while one starting with a
  letter survived, so a **kept** command lost part of its body and the fragments passed for
  records of their own. Commands are now folded onto one line on the way in (`\n` -> `\\n`)
  and unfolded by `shunt log`; both trim cuts move whole records. Logs written before this
  are read correctly too: a line without a date belongs to the record above it.

- **One unreadable line no longer disarms the trimmer forever.** The cut date was parsed
  from the oldest line, and the exception was swallowed by the fire-and-forget wrapper - so
  a single torn line stopped every future trim, and the log grew past its ceiling without a
  word. The parse now yields `None` and the size cut does the freeing; it drops from the
  front, so the damaged line is the first to go.

### Added

- **The CLI writes to the audit log too.** `run`, `edit`, `cp`, `bg`, `get` and `commit`
  each append one record (`sid=cli`, the subcommand in brackets: `:: [run] uptime`). The
  hook recorded every redirected bash command while the CLI recorded nothing - and
  `shunt run` is the path recommended to agents, which made the recommended path the
  unaudited one. Read-only subcommands stay out: they bring something back rather than
  sending something out. `shunt log` shows both halves together.

## [2026080614] - 2026-08-06

### Added

- **`shunt run @host <cmd>`** - one command on a host, without a session.

  ```bash
  shunt run @web-01 hostname
  shunt run @web-01 "ls /etc | wc -l"       # quoted -> the pipe runs on the server
  ```

  **Why:** the hook covers **interactive** bash - it needs a session to know where that
  session is routed. A script, a cron job or a spawned sub-agent has no mode of its own,
  so until now the only way to make an agent work on another machine was to leave the
  session in remote mode and let the agent inherit it *silently* - the very trap the
  warnings below are about. `run` gives somewhere to stand instead of only something to
  avoid.

  Quoting: a **single** argument passes through verbatim, so pipes, redirects and `$(...)`
  survive; **several** arguments are re-quoted, so `shunt run @h echo "a b"` stays two
  words on the far side. The remote exit code is passed through, not swallowed.

- **Warnings at the boundary of the mode.** The hook now also sees `Agent`, `Read`,
  `Write`, `Edit`, `MultiEdit` and `NotebookEdit`, and says out loud that the mode does
  **not** cover them: only `Bash` is ever rewritten. A spawned agent is warned on every
  spawn (each one inherits the routing anew); a file tool once per host (switching hosts
  re-arms it, `@local` clears it).

  It **never blocks** - working remotely with a local file is legitimate as often as it
  is a mistake; only the silence was the defect. The branch is fail-open: an error inside
  it can never break someone else's tool call.

  **This changes the hook registration.** The matcher is now
  `Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit` - see the README. Keeping the old
  `Bash`-only matcher leaves the redirection working exactly as before and simply loses
  the warnings; `shunt install` now prints the wider line.

- **`shunt` with no arguments introduces itself** - an "I want to... -> reach for" map
  instead of a usage line. shunt is not an MCP server; nothing announces it to whoever
  reaches for it, so this is its only way to explain itself in one call without loading
  documentation. Asking (`shunt help`, `-h`, `--help`) exits **0**; the bare call prints
  the same map but exits **2**, because a script that dropped its subcommand must not
  silently "succeed".

- **The audit log trims itself**, configured by a new `[audit]` section:

  ```toml
  [audit]
  trim_at_mb  = 100    # trim only once the log grows past this
  drop_months = 2      # then the OLDEST months go - the rest of the history stays
  ```

  The log is an **archive** and trimming is a **fuse**, not a retention policy: size is
  the trigger, and age is only the unit in which room gets freed. A log holding five
  years loses its first two months and keeps the rest. If age can free nothing - the file
  is not old but *fast*, a month's worth of lines written in an hour - the oldest lines
  go until it fits, because otherwise the fuse would fail in exactly the case it exists
  for. Bad or missing values fall back to the defaults above; a setting must never be the
  reason a command fails.

### Fixed

- **`shunt cp` now gets the same ssh options as every other subcommand.** They were
  written twice - once for `ssh`, once inside `cmd_cp` for `rsync -e` - and the copy had
  fallen behind: it lacked `BatchMode=yes` (so `cp` could sit forever on a password
  prompt inside a script) and `ControlMaster`/`ControlPersist` (so it opened a fresh
  connection every time, for nothing). There is now one `ssh_opts()` both read from.

- **`shunt commit` no longer stops at a stale manifest entry.** It walks every checked-out
  file; when one entry named a host that is no longer configured, the whole run died - so
  a single outdated entry silently dropped every file queued behind it, and the ones
  already pushed gave no hint that the rest never went. It now reports that entry, sets a
  non-zero exit code and carries on. The failure stays visible; it just stops taking
  hostages. A manifest entry outliving its host is ordinary, not exceptional.

## [2026080613] - 2026-08-06

### Added

- **`~/.config/shunt/shunt.toml`** - a TOML config that replaces the `hosts` file:

  ```toml
  key = "~/.ssh/id_ed25519_shunt"        # default identity for every host below

  [hosts]
  web-01  = "user@203.0.113.10"
  special = { target = "user@203.0.113.30", key = "~/.ssh/id_ed25519_special" }
  ```

  A bare string is the target; the inline table adds a per-host `key`, which wins over
  the top-level default. See [`shunt.toml.example`](shunt.toml.example).

  **Why:** the address lived in `hosts` while the identity for it lived in
  `~/.ssh/config` - one piece of knowledge in two files, which drifts apart in silence.
  Add a machine on one side, leave its key on the other, and access is gone without a
  single message. Owning the config removes the dependency on someone else's file, and
  `tomllib` has been in the standard library since 3.11, so this costs no new dependency.

- **`src/shunt/config.py`** - the only module that knows the config format. Both the CLI
  and the hook resolve hosts through it; each passes its own config directory, so the
  knowledge of the *format* lives in the module and the knowledge of the *location* stays
  with the caller.

### Changed

- `shunt install` now writes to `shunt.toml` instead of appending a `hosts` line. Still
  idempotent - an entry with the same alias is replaced, never duplicated - and it leaves
  the rest of the file, comments included, untouched. A `--key` is written down as you
  typed it (`~/...` stays `~/...`, so the file travels between machines) and expanded only
  when handed to ssh.
- `shunt hosts` prints the **resolved** hosts and the file they came from, rather than
  dumping raw file text that may now be in either of two formats.
- A broken config is loud: the CLI dies with the reason instead of resolving to no hosts.
  The hook does the opposite on purpose - it falls back to running **locally**, because a
  traceback in front of every bash command would be worse than staying home.
- `hosts.example` is gone, replaced by `shunt.toml.example`. The old format is still
  read; it is simply no longer the shape recommended to someone writing a config today.

### Backwards compatibility

With no `shunt.toml`, the old `~/.config/shunt/hosts` file is read exactly as before and
everything keeps working. shunt says once, **on stderr**, where the new place is - stdout
is a protocol for both callers (the hook writes JSON there, the CLI passes remote output
through), so a notice may never go that way. **Nothing is migrated automatically**: your
config file is yours, and moving it is your move, not the tool's. If both files exist,
`shunt.toml` is the one that counts.

## [2026080610] - 2026-08-06

**Breaking:** the `daemon` transport is gone. shunt now speaks ssh, and only ssh.

### Removed

- **The `daemon` transport** - `daemon.py`, its systemd unit, the inline TCP client
  inside the hook, and the token it needed; along with `shunt install --mode
  secure|nonsecure` and `--port`, and the `SHUNT_TOKEN` / `SHUNT_PORT` / `SHUNT_HOST`
  environment variables.

  **Why:** the daemon existed for speed - to avoid paying an ssh handshake on every
  command. ssh here runs with `ControlMaster`, which amortizes that handshake to
  milliseconds after the first call (measured: ~0.24-0.36 s per command without it,
  ~0.01 s with it once the master connection is up). The problem the daemon was built
  to solve is already solved by ssh - with no open port, no shared token, and nothing
  installed on the server. On top of that, every file operation (`read`, `edit`,
  `checkout`, `commit`, `cp`, `bg`, `get`) required ssh anyway; the daemon carried
  only the redirected bare bash. So: not "nobody used it" - ssh caught up and passed
  it.

- **The daemon hardening guide in `SECURITY.md`** (restricting the port, ssh tunnel,
  Tailscale, token rotation) - it protected a component that no longer exists. What in
  it was true of ssh as well stayed: a least-privileged remote account, a dedicated key
  you can revoke on suspicion, and the fact that the command text lands in the agent
  transcript and the audit log.

### Changed

- `shunt install <user>@<host> [--alias A] [--key PATH]` - no `--mode`, no `--port`.
- A host is still `<alias> ssh <target> [key=PATH]` in `~/.config/shunt/hosts`. The
  `ssh` word remains required: a line naming any other transport is **not** treated as
  a host, so an old `daemon` line fails loudly (`unknown host: <alias>`) instead of
  silently becoming some other destination.
- `@<alias>` now reports `REMOTE -> <alias> (<target>)` - the transport dropped out of
  the message, there being only one.

### If you were running the daemon

1. Re-register the host over ssh:
   `shunt install user@<host> --alias <alias> [--key ~/.ssh/id_ed25519]`.
2. Locally, delete `~/.config/shunt/token` and `~/.config/shunt/token.<alias>`.
3. On the server: `systemctl disable --now shunt-daemon`, then remove
   `/etc/systemd/system/shunt-daemon.service`, `/opt/shunt/` and `/etc/shunt/`.

A session that was already routed to a daemon host when you upgraded resolves nothing
and therefore runs **locally** again - the hook's standing behaviour for a target it
cannot resolve. Re-issue `@<alias>` after re-registering the host, and `@status` will
confirm where bash is going.

**The old tags stay.** `v2026062322` and `v2026062407` still ship the daemon and are
not going anywhere - if you need it, stay on the earlier release. The past is not
erased; it just stops being carried forward.

---

## [2026062407] - 2026-06-24

### Added

- **`checkout` / `commit`** - edit remote files with native local tools and push
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

## [2026062322] - 2026-06-23

Initial public release. Transparent remote hands for an AI coding agent:
redirect the agent's bash to a chosen remote host via a Claude Code hook, with
no change to how the agent writes commands.

### Added

- **Transparent `@host` bash redirect via PreToolUse hook** (`pretool.py`).
  Switch routing per-session with `@<alias>` / `@local` / `@status`; bare bash
  commands are then rewritten to run on the selected host. Remote `cwd` is kept
  per-session via a state-file, so `cd` persists across commands.
- **Two transports**, configured per-host in `~/.config/shunt/hosts`:
  - `ssh` - **secure**: `ssh` + `ControlMaster` multiplexing. Zero open ports,
    zero shared token, encrypted; per-session + per-destination control socket.
  - `daemon` - **nonsecure**: TCP + token, fast on a trusted LAN
    (`daemon.py`, a stdlib `ThreadingTCPServer` with per-session `cwd`,
    constant-time token check, client-disconnect process-group kill).
- **`shunt` CLI** for operations the hook does not cover:
  - `read @host <file> [start:end]` - content with line numbers for orientation.
  - `edit @host <file> OLD NEW [--expected N] [--dry-run] | --stdin` - edit by
    content (see below).
  - `cp <src> <dst>` - `rsync` with one side `@host:/path`.
  - `bg @host <cmd> [--name LABEL] | --list | --status JOB | --stop JOB` -
    long-running jobs via `systemd-run` (survive disconnect, preserve exit code).
  - `get @host <url> [dest]` - background download (`wget -b`) on the server.
  - `log [-n N]` - tail of the local audit log.
  - `hosts` - show configured hosts; `install <user>@<host>` - provision a host
    (`--mode secure|nonsecure`).
- **Edit-by-content** (`edit_helper.py`, stdlib-only, runs on the remote side):
  `old -> new` semantics like the built-in editor, with a uniqueness check
  (count-and-refuse on ambiguity), an **optimistic SHA-256 lock** (`base_sha`
  rejects a write if the file changed since it was read), **atomic write**
  (temp in the same directory -> `fsync` -> `os.replace` -> dir `fsync`, preserving
  mode/owner), and **verify-after-write** (re-hash the file and confirm).
  CRLF normalization with original line-ending style preserved on write.

### Security

- **Rewrite-marker guard** - every redirected command is prefixed with a
  `#shunt-rewritten` marker; the hook refuses to rewrite an already-rewritten
  command, preventing double-redirection loops.
- **Fire-and-forget audit log** - every redirected command is appended to
  `~/.config/shunt/audit.log` (timestamp, session id, host, command); logging
  failures never block execution. Tail it with `shunt log`.
- **Per-alias token** - daemon mode stores a `token.<alias>` (chmod 600) used
  first, falling back to the shared `token` for single-daemon setups. The token
  reaches the inline client via an environment variable, not `argv`, so it is
  not visible in `ps` to other local users.
- **Edit size guard** - `edit_helper.py` refuses files over
  `SHUNT_EDIT_MAX_BYTES` (default 64 MiB), directing the caller to `shunt cp` +
  a local edit instead.
- Daemon defaults to binding `127.0.0.1` (LAN exposure is explicit opt-in via
  `SHUNT_HOST`), uses constant-time token comparison, and warns when run as root.
  Nonsecure mode is documented as trusted-LAN-only; untrusted networks should
  use the `ssh` transport (no token, no open port).
