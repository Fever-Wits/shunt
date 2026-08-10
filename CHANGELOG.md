# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [CalVer](https://calver.org/) (`YYYYMMDDHH`).

## [2026081100] — 2026-08-11

One theme again, one step further in: **shunt refusing to answer from something it has not
read.** The previous release taught it to say which machine you are standing on; this one
covers the cases where it cannot tell — a hook input that arrives broken, a systemd unit
that does not exist, a line whose second half cannot run where the first half is going.
Two entries change behaviour you may have relied on; both are marked ⚠.

### Added

- **The hook now has an answer for an input it cannot read.** ⚠ **Behaviour change.**
  Everything this hook decides comes out of the JSON the harness hands it, and an
  unreadable one used to mean silence — after which the harness ran your **original**
  command. On a session routed to a server, that is the accident the whole tool exists to
  prevent: `rm -rfv /srv/old-release`, written for the far machine, deleting the local
  tree, because the one file that could have said "you are routed away" was the file that
  could not be read. Three answers now, by what can still be told apart:

  - **Nothing parses, or there is no `tool_name`** → the call is **denied**: exit **2**,
    the reason on stderr. This is the only place in shunt that denies a tool call. With no
    tool name, a bash command and a file read are the same shape, and only one of them is
    safe to let through. It costs you the file tools too, in that one state, and the way
    back in is a terminal outside the session.
  - **Readable, but `session_id` or the command is missing** → **bash alone** is refused,
    with the usual sentence in place of your command:

    ```
    [shunt] hook input incomplete (session_id) — routing unknown, remote commands
    disabled, command NOT run; fix the hook (file tools still work).
    ```

  - **`Read` / `Write` / `Edit` / `Grep` / `Glob` / `Agent`** keep working in that second
    state and are told **every time**. They are harmless on the local disk, and they are
    what repairs the hook from inside a session that no longer has bash — an `Edit` on
    `pretool.py` needs no shell at all. The once-per-session budget every other message
    here is kept on cannot be used: it is a file named after the session, and the session
    id is what is missing.

- **`bg --status` no longer dresses a missing unit as a finished job.**
  `systemctl show` invents an answer for a unit it has never heard of — every property at
  its default, `Result=success`, `SubState=dead`, `ExecMainStatus=0`, at exit 0 — so a
  mistyped job name was indistinguishable from a clean completion, in the one hand here
  that runs with nobody watching. `LoadState` is now asked as a question: the properties
  are still printed, **contradicted rather than hidden**, and the call comes back non-zero:

  ```
  shunt: no such job shunt-typo on this host — the status above is systemd answering
  about NOTHING, not about a job that ran. `shunt bg @<host> --list` shows the jobs it knows.
  ```

  A host that cannot answer at all — no systemd, no permission — says *that* instead, and
  is not allowed to pass for "no such job".

- **`shunt …` reached after a separator is refused while remote.** The mirror of a guard
  that already existed for lines *beginning* with `shunt`. A line such as
  `cat payload.json | shunt edit @web-01 /etc/nginx.conf --stdin` carries no `shunt`
  prefix, so nothing looked at it, and the whole line was shipped to a machine where
  `shunt` is not installed. It failed loudly there — which is not the same as clearly:
  what failed was the `shunt` half, while the half in front of the pipe had already run
  over there, against the far machine's files. In a local session the same line is
  ordinary work and still runs.

- **The config directory sweeps itself.** The far side has swept its per-session files
  since they were introduced; this side never did, so `~/.config/shunt/` collected
  `active-host.<id>` · `warned.<id>` · `switched.<id>` for every session that ever went
  remote. They are now removed once they are 30 days old, on a **switch** — the same rare
  moment the far side's sweep is paid for, never in front of an ordinary command.
  `target.<id>` is deliberately **not** swept: it is written once, at the switch, so an old
  timestamp there means a session that switched a while ago, not a session that is gone —
  and taking it away would send that session's next command to the local machine without a
  word.

### Fixed

- **A rewritten command keeps the rest of your request.** ⚠ **Behaviour change, in your
  favour.** The hook handed the harness only the rewritten `command`, and measurement
  showed the other fields did not survive the trip: a Bash call carrying
  `run_in_background: true` and `timeout: 600000` came back in the **foreground**, with the
  timeout back at its default — so a ten-minute job on a server was cut short for no
  visible reason. The whole input is now handed back with only `command` changed. (The
  hook reference describes `updatedInput` as *merged*; the harness measured here
  *replaced*. Passing everything back is correct under either reading, which is why the
  fix does not depend on that question.)

- **A session with no `session_id` is no longer routed by somebody else's switch.** The
  hook fell back to the literal slot `default`, so two sessions arriving without an id
  shared one routing file — and a switch made by either would send the other's commands to
  that host. There is no fallback now; that input is refused (see above).

- **Two comments in the source claimed "only Bash is ever rewritten".** It has not been
  true since a spawned agent's prompt started carrying a frame: `Agent` calls are rewritten
  too. A stale comment is read far more often than a manual.

## [2026081009] — 2026-08-10

One theme: shunt saying **which machine you are standing on — before the command, not
after it.** One entry is the exception that proves the rule: when the transport itself
fails there is nothing to say beforehand, so it is said after. Two entries change timing
or an exit code you may have relied on; each is marked ⚠.

### Added

- **`@<alias>` now asks the machine whether it answers.** ⚠ **Behaviour change** — the
  switch used to be pure bookkeeping and returned in milliseconds; it now waits for a real
  ssh handshake, about **3 seconds** against a host that is not there (5 in the worst
  case). What you see when it works:

  ```
  [shunt] mode: REMOTE → web-01 (deploy@web-01) — connected
  ```

  and when it does not:

  ```
  [shunt] mode: REMOTE → web-01 (deploy@web-01) — switch written, but @web-01 did not
  answer the check — nothing will run until it does. ssh: deploy@web-01: Permission
  denied (publickey,password).
  ```

  **The switch stands either way.** A host may be rebooting, and a session that has said
  where it wants to be is not sent home behind its own back — the routing is written
  *before* the probe, so a probe that hangs can never cost you the switch. There is a
  third line, `— could not check whether it answers (…)`, for when the check itself could
  not be made: a failed probe proves only that *this check* did not get through, and a
  refused key, a changed host key and a broken login shell all answer from a machine that
  is perfectly awake. The reason always comes from ssh, never guessed.

  **Why:** the old switch wrote a file and said REMOTE. The first thing that found out
  whether the host was reachable was your next real command — at the moment you had
  stopped thinking about machines. The check also warms the connection the next command
  wants.

- **`@local` leaves the same one-shot ticket `@<alias>` does, and the first command after
  it says where it is going:**

  ```
  ℹ shunt: first command since `@local` — this one runs HERE, on the local machine.
  (said once per switch)
  ```

  Going home is a switch like any other, and the command right after it is the one that
  acts out of habit — the habit just points the other way. The dance
  `@a → @b → @local → @c` used to announce every step except the one that comes back. One
  file holds both directions, so the last switch wins.

- **A spawned agent is told where it is standing.** The *parent* has been warned on every
  `Agent` spawn for a while; the child — the one that would actually act on it — was told
  nothing. It ran `ls`, read a disk it had never seen, and reported what it found as the
  truth about the world. The child's prompt now arrives with a short frame appended: what
  routes its bash, that its own file tools are **not** routed and stay on the local disk,
  and that `@local` is one session-wide setting shared with its parent and with any agent
  working beside it — so switching is never a private choice. The parent's warning still
  goes out, in the same response. If the frame cannot be written (an unusual `Agent` input,
  or a brief long enough to overflow the reply), the parent's warning goes out alone: the
  cost is a note not written, never a warning lost.

- **Three state failures now shout instead of passing in silence.** All three say the same
  thing — your shunt config directory is broken — and each used to cost you a message you
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
  kept on — that budget is itself a file in the very directory that is broken. For a fault
  of the class "fix it now", repeating is the behaviour that fits.

- **`exit 255` now says what it means.** When ssh cannot reach the host — connection
  refused, no route, a machine that went down mid-session — ssh exits **255**, and until
  now that number arrived bare. It reads as a verdict from whatever you believed you were
  running, and the search for the bug starts in the wrong program. The rewritten command
  now carries a local epilogue that looks at ssh's own exit code and, on 255 alone, adds
  one line to stderr:

  ```
  [shunt] exit 255 = ssh transport failure — @web-01 is down or unreachable; your command
  almost certainly never ran. Check the host, or @local.
  ```

  **Not a behaviour change:** the exit code is handed on untouched — 255 stays 255 — and
  every other code (0, 1, 42) passes through with nothing added to either stream. Nothing
  is read back from the far side; only the number ssh hands to the local shell.
  "Almost certainly" is literal rather than cautious: a remote command is free to exit 255
  on its own account, rarely, and shunt does not state what it has not verified.

### Fixed

- **`shunt bg @host --list` now reports a listing that could not be made.** ⚠ **Behaviour
  change** for anything reading its exit code. The command ended in `|| true`: a far side
  with no systemd, no permission, or a bad invocation came back **exit 0 with no output** —
  indistinguishable from "this host has no jobs". `systemctl list-units` already exits 0
  when the glob matches nothing, so the guard never bought the empty listing anything; it
  paid out only when the question could not be answered at all. An empty list is still a
  success; a real failure is now non-zero, and systemctl's own reason reaches your
  terminal. This closes the family the previous release opened, where `bg --stop`,
  `log -n` and `bg --name` each stopped reporting a success they had not verified — those
  three are unchanged here and still stand.

- **A failed `@<alias>` check no longer stutters `ssh: ssh:`.** Whatever ssh gives as a
  reason is attributed to ssh, so it cannot be read as shunt's own verdict — but ssh's
  transport failures already open with that attribution, so the line came back doubled:

  ```
  … did not answer the check … ssh: ssh: connect to host 203.0.113.9 port 22: Connection
  timed out
  ```

  The prefix is now added only where it is missing. The other shape of failure —
  `deploy@web-01: Permission denied (publickey,password)`, which names an account rather
  than a program — still gets it. Cosmetic: it never lied and never went quiet.

### Changed

- **Internal, visible only if you import the module:** `pretool._remote_script()` and
  `pretool.ssh_command()` now take `housekeeping=` where they took `switched=`. Nothing on
  the command line changes. The old name said "a switch happened", which was never what
  the flag decided: it means "this command is the one that pays for the far side's
  once-per-switch housekeeping" — true only when the ticket was actually punched, not
  merely present.

## [2026080920] — 2026-08-09

Most of this release is the hook learning to say **where a command actually went** — and
refusing when it cannot say. Several things that used to happen in silence are now loud,
and six of them change behaviour you may have relied on; each is marked ⚠. Two ask
something of you: the session's remote working directory moves and **is not migrated**,
and a `shunt …` line with a `;` in it is refused while the session is remote. Scripts
reading exit codes should read the entries for `bg --stop`, the checkout manifest, `log
-n` and `bg --name` — all four changed one.

### Changed

- **The session's remote working directory is remembered in
  `$HOME/.cache/shunt/cwd-<session-id>`** on the far host, no longer
  `/tmp/shunt-cwd-<session-id>`. ⚠ **Behaviour change**, and **nothing is migrated**: the
  first command after upgrading starts in the ssh login directory (usually `$HOME`)
  instead of where you left off — once per session, per host, per account. `cd` again and
  the new file takes over. It happens without a message, because a missing state file is
  also what a brand-new session looks like; there is nothing wrong to report.

  **Why:** `/tmp` is shared, and the path carried only the session id. Two accounts on one
  machine — `deploy@web-01` and `root@web-01`, which the config allows — reached for the
  same file, so one of them read the *other's* working directory or failed to write its
  own without a word. `$HOME/.cache` is per-account by construction; the directory is
  created `mkdir -m 700`. The old `/tmp/shunt-cwd-*` files are not read, not moved and not
  deleted — they are orphans now, and `/tmp` clears them in its own time.

  Two smaller things ride along. shunt sweeps `cwd-*` files older than 30 days out of that
  directory on the first command after a switch — nothing outside that name is touched.
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
  `find … -delete`, a `git clean` or `git … --hard`, a `docker rm` / `rmi` / `prune`, or a
  `>` that truncates a file, now arrives with a line naming the machine:

  ```
  ⚠ shunt: you are on @web-01 — this runs THERE and cannot be taken back: git … --hard,
  docker … rm. Check which machine you meant; `@local` first if it is this one.
  ```

  It **warns and runs**: nothing is blocked and no exit code changes. **Why:** every other
  guard here answers "which machine am I on?" when you ask it. This one answers when you
  do not ask, at the one moment the answer is expensive. And unlike the warnings that
  speak once per session, this one speaks **every time** — a destructive command is not a
  state you should get used to.

  `> /dev/null` and its variants are excluded; redirecting into the bin truncates nothing
  that matters. A shell comparison like `[[ $a > $b ]]` will still trip it — on a warning
  that costs one line of text, the false alarm is the safer side to be wrong on. The
  warning travels in the **same** hook response as the redirected command, which is a
  shape the hook did not emit before: `additionalContext` and `updatedInput` together.

- **A reminder on the first command after `@<alias>`:**

  ```
  ℹ shunt: first command since `@web-01` — it runs THERE, not here. (said once per switch)
  ```

  The switch is a line you type and forget; the command after it is where believing you
  are still at home does its damage. It is spent on that first command and does not come
  back until you switch again. A new file, `switched.<session-id>`, holds it next to
  `target.<session-id>` in `~/.config/shunt/`.

- **A working directory that has gone away now says so** — `shunt: /srv/release-42 cannot
  be entered (gone or not accessible); running in $HOME instead`. The fallback to `$HOME`
  is unchanged; it simply used to be silent, so a command written for one directory ran in
  another and returned perfectly ordinary output from the wrong place.

### Fixed

- **A `shunt …` line with something after it is refused while the session is remote.** ⚠
  **Behaviour change.** `shunt …` runs on *this* machine — that is what it is for — but so
  did everything past the `;`, and that part never asked to. On a session routed to a
  server, `shunt hosts; rm -rf /var/log/*` deleted the **local** log directory without a
  word, because the whole line was handed back unrewritten. A line beginning with `shunt`
  that also contains `;`, `&`, `|`, a backtick, `$`, `(` or a newline now runs nothing and
  says why.

  The cost is real and belongs here: legitimate one-liners go with it. `shunt run @web-01
  "systemctl status nginx | head"` is refused while the session is remote, and so is
  `shunt edit @host f "a" "b;c"` — the separator is looked for in the raw text, inside
  quotes included. Send the `shunt …` part as its own command (it runs here in any mode)
  and the rest as another, or `@local` first if the whole line was meant for this machine.
  A plain redirect is not in the class, so `shunt read @host /etc/nginx.conf > local.txt`
  still works.

- **A routing file that cannot be read is refused, not read as "local".** ⚠ **Behaviour
  change.** `target.<session-id>` had two readings — a host, or nothing — and a file that
  was empty, truncated, a directory or unreadable counted as *nothing*, which means
  **local**. A session that had been routed away therefore came home without saying so,
  and `@status` confirmed `LOCAL` with a straight face. There is a third reading now: bash
  runs nothing and names the file, `@status` answers `UNKNOWN`, and the file tools warn
  once that they are reading the local disk either way. `@local` to be local on purpose,
  or `@<alias>` to route again.

  A half-typed alias is *not* this state — `@web` for `@web-01` is refused at the switch
  itself (`[shunt] unknown host: web`) and the routing file is left untouched. The file is
  also written atomically now (temporary file, then rename), so an interrupted switch can
  no longer leave behind the empty file this entry is about.

- **A switch that fails says so, instead of failing quietly or lying.** A read-only config
  directory made `@web-02` exit without a word — the session stayed where it was while you
  believed it had moved — and `@local` printed `[shunt] mode: LOCAL` unconditionally,
  including when it had just failed to remove the file that keeps you remote. Both now
  report the failure and, more usefully, where you actually are:

  ```
  [shunt] switch to @web-02 FAILED — could not write
  /home/you/.config/shunt/target.s1 (Permission denied). Nothing changed; the session is
  STILL on @web-01. Fix that and try `@web-02` again.
  ```

  ⚠ The atomic write brings one new failure mode: switching now needs a **writable config
  directory**, not merely a writable file, because the temporary file is created beside the
  target. It fails loudly, in the shape above. On the other side, an *empty* directory
  sitting where `target.<session-id>` belongs is now removed rather than being a trap with
  no way out — bash refused, and `@local` was powerless to lift it.

- **`shunt bg --stop` reports what systemd actually did.** ⚠ **Behaviour change** for
  anything reading its exit code. The stop and the `echo` were joined by `;`, so `stopped`
  was printed and `0` returned no matter what happened — a mistyped unit name answered
  exactly like a real one while the job kept running. `stopped <job>` is now printed only
  on success, and systemctl's own message and exit code come back otherwise. Stopping a
  job twice is one of those failures: `systemd-run --collect` discards the unit when it
  ends, so the second `--stop` gets `Unit … not loaded.` and exit **5**.

- **A checkout manifest that cannot be read stops the operation.** ⚠ **Behaviour change.**
  Every read of it caught every exception and returned an empty manifest — and an empty
  manifest means "nothing is checked out" further down. So a corrupt file made `checkout
  --list` print `(no checkouts)`, and `commit` say `no checkouts in manifest` and exit
  `0`, while the file recording every checkout's `base_sha` sat there unreadable. It now
  exits `2` and names the parse error:

  ```
  shunt: cannot read the checkout manifest
  /home/you/.config/shunt/checkouts/manifest.json: Expecting property name enclosed in
  double quotes: line 1 column 2 (char 1)
    it holds every checkout's base_sha, so nothing is listed, committed or checked out
    until it can be read (move it aside to start over — the local files stay where they
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
  to **50** records — and fifty records look like a complete answer, which is how someone
  concludes that a command was never sent to a server at all. `--name` with no label left
  the flag in the command, so `shunt bg @host "deploy.sh" --name` sent `deploy.sh --name`
  to the far machine and ran it.

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
