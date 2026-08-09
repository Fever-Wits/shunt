"""
shunt — pretool.py · PreToolUse hook
(matcher: Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob)

Two jobs, one file — because both need the same answer to "where is this session routed?":

  Bash   → REWRITE: the command runs on the chosen remote machine, transparently.
  others → WARN: these tools do NOT follow the mode (the hook rewrites bash only), so
           running them in remote mode is reported instead of silently doing the wrong
           thing. Never blocked — remote-mode-plus-local-file is legitimate as often as
           it is a mistake; only the silence was the defect.

A rewrite may carry a note along (the first command after a switch · something that
cannot be taken back leaving for another machine) — said, never blocked, see emit().

Switching: @<alias> / @local / @status — PER-SESSION (does not clash across parallel sessions).

The transport is ssh + ControlMaster: zero open ports, zero shared token, encrypted.
Proven: cwd state-file per session, live streaming, speed. It is the only one — the hosts
come from ~/.config/shunt/shunt.toml (config.py, shared with the CLI).

Why hook + updatedInput (rather than replacing the shell): official, documented mechanism;
we stay independent of undocumented env settings.

CRITICAL: the rewritten command runs in a strict sandbox (root, no ~/.config/shunt, BUT network
OK). That is why the client is the `ssh` binary, which is available in the sandbox.
"""

import json  # `re` costs nothing extra — json loads it anyway
import os
import re
import shlex
import sys
import time

try:
    from shunt import config
except ImportError:
    # The hook is wired into settings.json by ABSOLUTE PATH (see README), so it may be
    # started as a plain script: `python3 …/src/shunt/pretool.py` puts …/src/shunt on
    # sys.path and never …/src, leaving its own package unimportable. The parent
    # directory is what makes it resolvable; an installed package never gets here.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from shunt import config

CONF = os.environ.get("SHUNT_CONF", os.path.expanduser("~/.config/shunt"))

REWRITE_MARKER = "#shunt-rewritten\n"


# Tools that do NOT follow @host mode. The hook rewrites Bash and nothing else, so
# these keep working on the LOCAL disk while the session "feels" remote. Both failures
# are silent — hence a warning instead of letting them be discovered from wrong output.
# Grep/Glob are here for the AGENT, not for the human: a person searching a machine
# types `grep`/`find` into bash, which the hook redirects correctly. An agent reaches
# for the Grep tool instead, gets hits from the local disk, and reads them as facts
# about the far one — and it reaches for it far more often than for Read.
# ⚠ A name added here warns nobody on its own: the matcher in settings.json decides
# which tools ever reach the hook. HOOK_MATCHER (cli.py, printed by `shunt install`)
# is the other half of the same fact.
LOCAL_DISK_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob")


def emit(command, notice=None):
    """Return a rewritten command to Claude Code and exit.

    A `notice` rides in the SAME reply as the rewrite, deliberately not through warn():
    warn() exits, and an exit before the rewrite would run the command HERE, on the very
    machine the caller believes they left. Verified against the harness (2.1.225):
    updatedInput and additionalContext are delivered together, rewrite intact.
    """
    out = {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"command": command}}
    if notice:
        out["additionalContext"] = notice
    print(json.dumps({"hookSpecificOutput": out}))
    sys.exit(0)


def echo(msg):
    emit("echo " + shlex.quote(msg))


def warn(msg):
    """Say something into the agent's context and allow the call (never block).

    Blocking would be wrong here: working remotely with local file tools is legitimate
    as often as it is a mistake. Only the SILENCE is the defect.
    """
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}}))
    sys.exit(0)


DAYS_PER_MONTH = 30  # `drop_months` is a human unit, not a calendar one — see _trim_audit


# ── one record = one line ──────────────────────────────────────────────────────
# The log THINKS in records; every reader of it counts LINES — the trimmer dates a cut
# from the head of a line, `shunt log -n N` shows N of them. A multi-line command written
# raw breaks that equality, and the damage is quiet: the trimmer compares `line[:10]`
# against the cutoff, so a continuation line starting with a space falls out (" " < "2")
# while one starting with a letter survives ("p" > "2") — a kept, recent command loses
# part of its body and the fragments look like records of their own. So the command is
# folded onto one line on the way in and unfolded on the way out.
UNESCAPES = {"n": "\n", "r": "\r", "\\": "\\"}


def escape_cmd(cmd):
    r"""Fold a command onto ONE line — the unit the log is counted and trimmed in.

    The backslash is escaped FIRST and it is what makes the folding reversible: without
    it a command that already contained the two characters `\` `n` would come back as a
    newline. `\r` is here for the reader rather than the writer — python reads the log in
    text mode, where a lone carriage return splits a line exactly as a newline does.
    """
    return cmd.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def unescape_cmd(text):
    r"""The inverse of escape_cmd — the command as it was typed, for human eyes.

    One left-to-right pass, not three replaces: `\\n` is an escaped backslash followed by
    the letter n, and any order of replaces would read it as a newline. An unknown escape
    (`\t` in a line written before folding existed) is left exactly as it lies.
    """
    out, i = [], 0
    while i < len(text):
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if text[i] == "\\" and nxt in UNESCAPES:
            out.append(UNESCAPES[nxt])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def audit(sid, alias, cmd, conf=None):
    """Record one command sent to a host; trim if the log has grown past its ceiling.

    The log is an ARCHIVE, not a window: the question people bring to it is "where did we
    download that from, two months ago", and a short history answers it with silence.
    So size is the trigger and age is only the unit in which room gets freed.

    Trimming lives HERE rather than in someone's habit — a rule that needs remembering is
    a rule that gets forgotten. The cost per recorded command is the append, one small
    read of shunt.toml for the [audit] settings — the second of the run, since resolving
    the host has already read the file — and one `getsize`. At any ordinary rate (~15 KB
    per six weeks) the ceiling is never reached at all, which is the point: a fuse that
    blows regularly is a policy in disguise.

    `conf` lets the OTHER caller in: cli.py records its own subcommands here (the CLI is
    the path we recommend to agents, so leaving it out of the log left the recommended
    path unaudited) and passes its own config dir, the way config.py is called too — the
    format is shared, the location stays with the caller. Absent → this hook's own.

    Fire-and-forget by design — an audit line must never break the command it records.

    One command is ONE line: it goes in folded (see escape_cmd) and `shunt log` unfolds
    it. A command that spans lines would otherwise be counted and trimmed as several.
    """
    conf = conf or CONF
    path = os.path.join(conf, "audit.log")
    try:
        with open(path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} sid={sid} host={alias} :: {escape_cmd(cmd)}\n")
        cfg = config.audit_settings(conf)
        ceiling = int(cfg["trim_at_mb"] * 1_000_000)
        if os.path.getsize(path) > ceiling:
            _trim_audit(path, cfg["drop_months"], ceiling)
    except Exception:
        pass


def _months_after(iso_date, months):
    """ISO date `months` later, counting a month as 30 days.

    Deliberately not calendar months: adding one to the 31st has no honest answer, and an
    audit log does not need one. Thirty days is predictable and explainable, which matters
    more here than being exact.
    """
    t = time.strptime(iso_date, "%Y-%m-%d")
    return time.strftime("%Y-%m-%d", time.localtime(time.mktime(t) + months * DAYS_PER_MONTH * 86400))


def _cut_date(oldest_line, drop_months):
    """The date the age-cut runs to, or None when the oldest line carries no date.

    A record always starts with one — unless a write was torn, something was appended by
    hand, or the file was inherited from before commands were folded and this line is the
    tail of one. Raising on that would be silent murder: the caller swallows
    exceptions (an audit line may never break a command), so ONE unreadable line would
    stop every future trim and the log would grow without a ceiling and without a word.
    None instead, and the size cut below does the freeing — a fuse that gives up on a
    bad line is not a fuse.
    """
    try:
        return _months_after(oldest_line[:10], drop_months)
    except (ValueError, OverflowError):
        return None


RECORD_HEAD = re.compile(r"\d{4}-\d{2}-\d{2}T")  # exactly how audit() opens every line


def _has_date(line):
    """True when the line OPENS a record — the date the trimmer and `shunt log` read.

    Everything else is a continuation line from a log written before commands were
    folded; it is the tail of the record above it, not a record of its own.

    The SHAPE, not a parse: this runs once per line of the whole file, and the full
    date arithmetic in _cut_date costs 24 µs a call — measured — which is a minute of
    silence inside a hook when the trim finally fires on a 100 MB log. A date that has
    the shape but no meaning (month 13) still resolves to None in _cut_date, where the
    parse actually happens.
    """
    return RECORD_HEAD.match(line) is not None


def log_records(lines):
    """Group physical lines into RECORDS — one recorded command each, text and all.

    Since commands are folded, a record IS a line and this is the identity. Logs written
    before that hold commands whose body spilled over many lines; the spilled lines carry
    no date, so they are given back to the record above instead of passing for records of
    their own. That is what lets `-n N` mean N commands, and the trimmer drop N whole
    commands, in an inherited file as well as a new one.

    Shared with the CLI (`shunt log`) rather than copied: how the log is written and how
    it is read back are one piece of knowledge, and a second copy drifts the day one side
    changes.
    """
    records = []
    for line in lines:
        if records and not _has_date(line):
            records[-1] += line  # inherited spill-over: rare, and records are short
        else:
            records.append(line)
    return records


def log_text(record):
    """One record as a human reads it — the folded newlines put back.

    Only the command is unfolded, because only the command was folded. A stray piece with
    no ` :: ` at all comes from a log written before folding existed and is shown exactly
    as it lies on disk — nothing there was written by us.
    """
    head, sep, cmd = record.partition(" :: ")
    if not sep:
        return record
    return head + sep + unescape_cmd(cmd)


def _trim_audit(path, drop_months, ceiling):
    """Free room by dropping the OLDEST `drop_months` of history — not by keeping only
    the newest ones. The distinction is the whole design: a log holding five years loses
    its first two months and keeps the rest.

    If that is not enough — everything in the file is recent, because something wrote a
    month's worth of commands in an hour — the oldest records go until it fits. Without
    this the fuse fails in exactly the case it exists for.

    Both cuts work in RECORDS, never in lines. A command inherited from a log written
    before folding spans several lines, and only the first of them carries a date: judging
    the rest by `line[:10]` is a coin toss that mutilates a command it meant to keep, and
    a size cut landing inside one leaves a dateless line at the front — which is the head
    the NEXT trim dates its cut from, so age-trimming would then be dead for good.

    Written via a temp file and os.replace, so a crash mid-trim cannot leave half a log.
    ⚠ What falls out is gone for good.
    ⚠ Parallel sessions append to one file. Appends are safe; a trim coinciding with
    another session's append can lose a line or two. Accepted for an audit trail — said
    plainly so it is not read as a guarantee.
    """
    with open(path) as f:
        lines = f.readlines()
    if not lines:
        return

    records = log_records(lines)
    cutoff = _cut_date(lines[0], drop_months)  # oldest line dates the cut
    if cutoff is None:
        # The oldest line has no readable date, so age cannot address anything. Size
        # below still can — and it drops from the front, so the damaged line is the
        # first to go.
        kept = records
    else:
        # Every record starts on a dated line here: the first line is dated, so an
        # undated one always has a record above it to belong to.
        kept = [rec for rec in records if rec[:10] >= cutoff]
    if not kept:
        # Every record falls inside the span we meant to drop: the log is not OLD, it is
        # FAST — a month's worth written in an hour. Age can free nothing here, and
        # cutting by it would empty the file, losing the newest records too. Leave it to
        # size below, which drops from the front and keeps the tail.
        kept = records

    total = sum(len(rec) for rec in kept)
    if total > ceiling:  # last resort: the history itself is the flood
        start, acc = len(kept), 0
        while start > 0 and acc + len(kept[start - 1]) <= ceiling:
            start -= 1
            acc += len(kept[start])
        kept = kept[start:]

    tmp = path + ".trim"
    with open(tmp, "w") as f:
        f.writelines(kept)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# The routing file is read as THREE states, not two (see read_target). This is the third:
# the file is there and it does not name a host. An object and not a string, so nothing can
# produce it by accident and `is` is the only way to ask for it.
BROKEN_TARGET = object()


def read_target(sid):
    """Alias of the host this session is routed to · "" for local · BROKEN_TARGET.

    ABSENT is the ordinary local state — no switch was ever made, so there is nothing to
    say. PRESENT while naming no host is a different thing entirely: a write that was
    torn, a file someone emptied, a directory in the way. Both used to come back as "",
    and the next command then ran HERE without a word, on the machine the caller may well
    believe they left. For a tool whose default is "run it here", every quiet fall to the
    default is a fall to the wrong machine — so the two are told apart, and the callers
    refuse instead of assuming.

    A HALF-written alias is not this state and needs no new handling: it names a host that
    does not resolve, and main() already refuses on that.
    """
    try:
        with open(os.path.join(CONF, "target." + sid)) as f:
            alias = f.read().strip()
    except FileNotFoundError:
        return ""  # never switched — the ordinary local session
    except Exception:
        return BROKEN_TARGET  # it IS there; we cannot say where it points
    return alias or BROKEN_TARGET


def _clear_routing(path):
    """Take the routing file away — "" when it is gone, the REASON when it is not.

    A directory in that place is not the exotic case, it is the trapping one: it reads as
    the unreadable state, so every bash command is refused — and `@local`, the one remedy
    for that state, could not win either, because os.remove can never take a directory.
    A refusal whose only way out cannot run leaves a session with no way out from inside.
    rmdir takes an empty one; a full one is reported WITH its path, to be removed by hand.
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        return ""  # already local — nothing to remove
    except IsADirectoryError:
        try:
            os.rmdir(path)
        except OSError as e:
            return e.strerror or str(e)
    except OSError as e:
        return e.strerror or str(e)
    return ""


def _warned_before(sid, alias):
    """True if this session was already warned about THIS host.

    Keyed by host, not just session: switching @web-01 → @web-02 warns again, because the
    file tools are pointed somewhere new and the old warning no longer describes it.

    ONE budget for every tool in LOCAL_DISK_TOOLS, not one per tool. Grep is called often
    enough that a line per tool would become wallpaper — and wallpaper is silent exactly
    when it needs to speak. That is why the warning names both remedies at once.
    """
    p = os.path.join(CONF, "warned." + sid)
    try:
        with open(p) as f:
            if f.read().strip() == alias:
                return True
    except Exception:
        pass
    try:
        os.makedirs(CONF, exist_ok=True)
        with open(p, "w") as f:
            f.write(alias)
    except Exception:
        pass  # best-effort: a repeated warning beats a crash
    return False


def warn_if_off_mode(tool, sid):
    """Warn when a tool that ignores @host mode runs while the session is remote."""
    alias = read_target(sid)
    if alias is BROKEN_TARGET:
        # Neither local nor a host: the file that says where this session is routed is
        # there and unreadable. Silence would leave these tools reading the LOCAL disk
        # under a mode nobody can name — the very silence this function exists to end —
        # and naming a host we have not read would be worse, since the hook may never
        # state what it has not verified. Same once-per-value budget as below, so it
        # cannot become wallpaper; "?" is not an alias any switch can write.
        if tool == "Agent":
            # OFF that budget, exactly as in the readable case below: every spawn is a new
            # agent inheriting the state, and the budget is SHARED with the file tools —
            # one Grep would spend it and the next agent would be born into silence. What
            # it inherits is worse than a wrong disk, too: while the routing is unreadable
            # main() refuses every bash command, so the agent works with no bash at all.
            warn(
                "⚠ shunt: this session's routing state is unreadable — whether you are "
                "local or on a host cannot be said, and a spawned agent INHERITS that "
                "state: its bash commands are REFUSED until the routing is settled, and "
                "its file tools read the LOCAL disk meanwhile. `@status` to look, then "
                "`@local` or `@<alias>` to settle it."
            )
        if not _warned_before(sid, "?"):
            warn(
                "⚠ shunt: this session's routing state is unreadable — whether you are "
                f"local or on a host cannot be said. {tool} works on the LOCAL disk either "
                "way. `@status` to look, then `@local` or `@<alias>` to settle it. "
                "(said once)"
            )
        return
    if not alias:
        return  # local — nothing to warn about
    if tool == "Agent":
        # Every spawn matters: the agent inherits the mode and reads missing local
        # files as facts about the world. Observed once as "the Bash tool briefly
        # lost access to the working directory" — it had not; it was elsewhere.
        warn(
            f"⚠ shunt: you are on @{alias} — a spawned agent INHERITS this and will run "
            "its bash there, reading absent local files as facts. Switch with "
            f"`@local` first, or make sure the agent is meant to work on {alias}."
        )
    if tool in LOCAL_DISK_TOOLS and not _warned_before(sid, alias):
        warn(
            f"⚠ shunt: you are on @{alias}, but {tool} works on the LOCAL disk — the mode covers "
            f"bash only. Remote file → `shunt read/edit @{alias} …`; remote search → "
            f'`shunt run @{alias} "grep -rn PATTERN /path"`. (said once per host)'
        )


# ── the one line that must NOT be redirected ──────────────────────────────────
# `shunt …` does its own transport, so it runs HERE whatever mode the session is in. The
# PREFIX, though, only speaks for the FIRST command on the line: everything past a `;`, a
# `|`, a `&&` or a `$(…)` is a different command, and letting the prefix speak for it runs
# THAT one here, on the machine the caller believes they left. The two directions of the
# mistake are not symmetric — a shunt command sent to a host comes back loudly as "command
# not found", while `shunt hosts; rm -rf /var/log/*` succeeds, locally, in silence: the
# mirror of a guarded error is rarely guarded as well.
# So only a line that can hold no second command passes; the rest is SAID and not run —
# the failure falls to refusal and never to the default, and the default here is HERE.
# Split on the RAW text, deliberately: a separator inside quotes costs a refusal nobody
# needed, never a command that leaves unnoticed.
# ⚠ The boundary, named so it is not read as an oversight: a REDIRECTION (`>` `>>` `2>`
# `<`) is deliberately NOT in the class. It cannot hide a second command — one still needs
# a separator that IS in the class — and adding it would refuse the legitimate
# `shunt read @h /etc/nginx.conf > local.txt`. What a redirection DOES do is silent: its
# target lands on THIS machine. Which is where the `shunt` CLI runs anyway, so nothing
# crosses a machine boundary unnoticed — the write is beside the tool that made it.
SHELL_BREAK = re.compile(r"[;&|`$(\n]")


def _shunt_cli_here_or_refuse(s, sid):
    """Let a `shunt …` line run locally — or say why it will not run at all. Never returns.

    A refusal and not a warning: warn() ALLOWS the call, and allowing it is precisely the
    defect. echo() is what this file already refuses with (see the unresolvable alias in
    main()) — the command is replaced by the message, so the caller reads what happened
    instead of learning it from what it did.

    A LOCAL session passes through untouched: nothing is routed anywhere, so
    `shunt hosts | grep web` is ordinary work and refusing it would be noise. The price of
    the other side, said plainly: in remote mode that same pipe is refused too, because
    nothing here can tell which half of the line was meant for the far machine. The way
    out is one line, and the message carries it.
    """
    hit = SHELL_BREAK.search(s)
    if not hit:
        sys.exit(0)  # one shunt command, one machine — as it always was
    alias = read_target(sid)
    if alias == "":
        sys.exit(0)  # local session — there is no other machine in play
    where = f"on @{alias}" if alias is not BROKEN_TARGET else "routed somewhere this hook cannot read"
    # Named out here rather than inside the message: a `\n` may not appear in an f-string
    # expression before python 3.12, and this file still runs on 3.11.
    seen = "newline" if hit.group(0) == "\n" else hit.group(0)
    echo(
        "[shunt] NOT run. `shunt …` runs on THIS machine — and so would the rest of this "
        f"line, everything past the `{seen}`, while the session is {where}. Send the "
        "`shunt …` part as its own command (it runs here in any mode) and the rest as "
        "another; or `@local` first, if the whole line was meant for this machine."
    )


# ── what is said before a command leaves for another machine ──────────────────
# Two NARROW cases, never one wide one. A line that is always there stops being read,
# and a check nobody reads is worse than no check: the warning that matters drowns in
# the ones that did not. Both ride along with the rewrite (emit's `notice`) — see emit().

# Commands that cannot be taken back on the far side, by themselves, with no flag needed.
# The list is deliberately SHORT: a missed warning costs less than a warning on every
# `ls`, so anything that only SOMETIMES destroys is either shaped below or left out.
IRREVERSIBLE = {
    "rm",
    "rmdir",
    "mv",
    "dd",
    "shred",
    "truncate",
    "mkfs",
    "wipefs",
    "reboot",
    "poweroff",
    "halt",
    "shutdown",
}

# Ordinary commands that turn irreversible in ONE shape. The flag or subcommand is what
# does it, so the bare command stays silent — `git status`, `docker ps`, `chmod 644`,
# `find -name` are the daily work and must not speak.
IRREVERSIBLE_WITH = {
    "chown": ("-R", "--recursive"),
    "chmod": ("-R", "--recursive"),
    "find": ("-delete",),
    "git": ("clean", "--hard"),
    "docker": ("rm", "rmi", "prune"),
}

# Where a new command can begin: a shell separator, or `-exec`, which exists to introduce
# one (`find … -exec rm {} \;`). Splitting the RAW text means a separator inside quotes
# opens a phantom segment — that can only ADD a warning, never hide one, which is the
# right direction for something that never blocks.
COMMAND_BREAK = re.compile(r"[\n;&|(){}`]+|\$\(|\s-exec(?:dir)?\s")

# Words that stand in FRONT of the real command without being it.
NOT_THE_COMMAND = ("sudo", "doas", "env", "time", "nohup", "xargs", "command", "then", "else", "do")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=")

# A lone `>` truncates whatever it points at. `>>` appends, `2>&1` and `>&2` only move a
# stream, and `->` inside a word (`--pretty=%h -> %s`) is not a redirect at all.
# `/dev/null` is a lone `>` and is excluded anyway: there is nothing there to lose. Not
# politeness — `2>/dev/null` is the commonest shape in an agent's bash, our own remote
# script carries one, and without this the loudest line the hook has rode along with
# ordinary commands. That is the wallpaper the two-narrow-cases rule above IRREVERSIBLE
# exists to prevent. The word boundary keeps `> /dev/nullish` — somebody's real file —
# on the loud side.
OVERWRITE = re.compile(r"(?<![->])>(?![>&])(?!\s*/dev/null\b)")
OVERWRITE_NAME = "> (truncates its target)"


def _command_of(words):
    """The name of the command a segment runs — "" when it runs none.

    A leading `sudo`, a shell keyword, an option or a VAR=value is stepped over, and a
    path is reduced to its last part, so /bin/rm is still rm.
    """
    for word in words:
        if word in NOT_THE_COMMAND or word.startswith("-") or ASSIGNMENT.match(word):
            continue
        return os.path.basename(word).strip("'\"")
    return ""


def irreversible_in(cmd):
    """The irreversible actions in `cmd`, named as a reader would name them.

    Command POSITION, not substring: `alarm`, `--format` and `grep rm` merely CONTAIN a
    command name, and a hook that fires on those teaches the reader to skip its lines.
    """
    found = []
    for segment in COMMAND_BREAK.split(cmd):
        words = segment.split()
        name = _command_of(words)
        shapes = [flag for flag in IRREVERSIBLE_WITH.get(name, ()) if flag in words]
        if name in IRREVERSIBLE or name.startswith("mkfs."):  # mkfs.ext4, mkfs.xfs, …
            hit = name
        elif shapes:
            # the PAIR that spotted it, rather than a command line nobody typed:
            # `git reset --hard` is seen as `git` + `--hard`
            hit = f"{name} … {shapes[0]}"
        else:
            continue
        if hit not in found:
            found.append(hit)
    if OVERWRITE.search(cmd):
        found.append(OVERWRITE_NAME)
    return found


def _just_switched(sid, alias):
    """True on the FIRST command after `@alias`, and only that one.

    Remembered in a file next to the session's other state, the same shape as
    _warned_before: the switch arms the marker, the first command spends it. TWO riders
    sit on that one fact — the reminder (remote_notice) and the far side's housekeeping
    (_remote_script) — which is why it is read in exactly one place; the other half of
    that is in remote_notice.

    Once and not per command, for both of them: a repeated line would be wallpaper — and
    wallpaper is silent exactly when it needs to speak — and a repeated sweep would be a
    `find` over someone's disk, several times a minute, to delete nothing. The case the
    reminder exists for is "I forgot I had switched", which lives in that one command too.
    """
    path = os.path.join(CONF, "switched." + sid)
    try:
        with open(path) as f:
            fresh = f.read().strip() == alias
    except Exception:
        return False
    try:
        os.remove(path)
    except OSError:
        pass  # best-effort: a repeated reminder beats a crash
    return fresh


def remote_notice(sid, alias, cmd, switched):
    """What to say before this command leaves for @alias — "" when there is nothing.

    `switched` is handed IN rather than asked here: the same one-shot fact also decides
    what the remote script does with that command (its housekeeping rides on it), and the
    marker can only be spent once — whoever asked first would silently starve the other.

    ⚠ `cmd` is the command as the CALLER typed it, never the rewritten one. The rewrite
    carries our own `find … -delete` on a switch, which irreversible_in() would report as
    the caller's doing — a warning about a command nobody wrote teaches the reader to skip
    the ones that matter.
    """
    lines = []
    if switched:
        lines.append(f"ℹ shunt: first command since `@{alias}` — it runs THERE, not here. (said once per switch)")
    hits = irreversible_in(cmd)
    if hits:
        lines.append(
            f"⚠ shunt: you are on @{alias} — this runs THERE and cannot be taken "
            f"back: {', '.join(hits)}. Check which machine you meant; `@local` "
            "first if it is this one."
        )
    return "\n".join(lines)


def resolve_host(alias):
    """Alias → {'alias', 'target', 'key'} or None.

    None also covers a broken config: a hook that raises would put a traceback in front
    of every bash command. What the caller does with the None is the other half — see
    main(): a session routed somewhere unresolvable does not fall home, it refuses.
    `shunt hosts` says what is wrong with the file.
    """
    try:
        return config.resolve(CONF, alias)
    except Exception:
        return None


# ── where the far side remembers this session's directory ─────────────────────
# A path written for the REMOTE shell, never for os.path: `$HOME` is the account we LAND
# IN over there, which is routinely not the one running this hook (a local user → remote
# root), and the rewritten command runs in a sandbox with no home of ours at all.
# Expanding it HERE would bake a local home into a remote path — a directory that is not
# on the far machine — and the state would quietly never be written again.
# ⚠ Never shlex.quote THIS: single quotes hand `$HOME` over as five literal characters,
# and the far shell then makes a directory by that name wherever the command landed. Only
# the session id is quoted (see _state_file) — it is the part that arrives from outside.
# ⚠ Not a twin of the ControlMaster socket in ssh_command: that one is a LOCAL file and
# stays in /tmp on purpose. These two look alike and are not.
# `.cache` and not a state directory of its own: losing this file costs one forgotten
# `cd`, so being swept by a cache cleaner is exactly the semantics we want.
REMOTE_STATE_DIR = '"$HOME"/.cache/shunt'

# A session id is born and never dies, so the directory would grow one file per session
# forever. Swept ONCE per switch (see _remote_script) — on every command it would be a
# `find` over someone's disk to delete nothing, several times a minute.
REMOTE_STATE_TRIM = f"find {REMOTE_STATE_DIR} -maxdepth 1 -name 'cwd-*' -mtime +30 -delete 2>/dev/null"


def _state_file(sid):
    """This session's cwd file, as the FAR shell will read it.

    The id is quoted because it arrives from outside (the harness' JSON); the directory
    around it must NOT be — see REMOTE_STATE_DIR.
    """
    return f"{REMOTE_STATE_DIR}/cwd-{shlex.quote(sid)}"


def _state_write(sid):
    """The far-shell group that records the cwd — the one place that knows how it is written.

    Grouped and silenced AS A WHOLE, deliberately: `pwd > FILE 2>/dev/null` silences pwd,
    but the "No such file or directory" for a file that cannot be OPENED comes from the
    shell, before pwd ever runs, and redirections are applied left to right — so it lands
    in the stderr of the CALLER's command, where it reads as output of their own work.
    The braces put /dev/null in place first, in every POSIX shell, instead of trusting one
    of them to order the two our way.

    `mkdir` rides WITH the write and not with the read: only the writer needs the directory
    (a missing one reads as "no cwd yet" — the cat falls back to $HOME), and riding along
    heals a directory removed between two commands. `-m 700` because the file is a trail of
    the directories someone works in; it applies at CREATION only, so a directory that is
    already there keeps the permissions its owner gave it.
    """
    return f"{{ mkdir -m 700 -p {REMOTE_STATE_DIR}; pwd > {_state_file(sid)}; }} 2>/dev/null"


def _remote_script(cmd, sid, switched=False):
    """What runs on the far machine: restore the cwd → (housekeeping) → arm the trap → run.

    `switched` marks the FIRST command after `@alias` — the single moment a session pays
    for housekeeping, and the only place it can be paid: the write is silenced on every
    other command, so a home that cannot be written to would cost the session its memory
    of every `cd` without a word. Once per switch it is PROBED instead — the same write
    the trap will do, out loud when it fails. A line on every command would be wallpaper,
    and wallpaper is silent exactly when it needs to speak (the same budget the file-tool
    warning is kept on, see _warned_before).
    ⊸ the price, said plainly: a home that becomes unwritable MID-session is not reported
      until the next switch. Nothing here can see the far side between commands — the hook
      builds the command, it never sees what came back.

    The restore SPEAKS when it cannot land. `cd X || cd ~` is the textbook shape of a cd
    whose failure nobody looks at, and on the next line an arbitrary command: the caller's
    own, which assumes it is where it left off. A directory removed on the far side
    between two commands would silently move `rm -rf ./*` from /srv/old-release to $HOME.
    It is said and not refused — the session must stay usable when its remembered
    directory is swept — and it fires ONLY on a real failure: with no state file yet the
    cd goes to $HOME and succeeds, so the ordinary first command after a switch says
    nothing.
    ⚠ The message says CANNOT BE ENTERED, not "is gone": cd fails just as readily on a
      directory that still exists — permissions changed, a mount that fell away — and a
      message that names the wrong cause sends the reader looking in the wrong place.
      What is verified here is the cd's failure; the reason is not.
    """
    # The path goes through a variable so the message can NAME it — a report that cannot
    # say which directory it is leaves the reader to guess. Unset right after, because
    # what follows is the caller's shell and it is not ours to leave things in.
    lines = [
        f'__shunt_cwd="$(cat {_state_file(sid)} 2>/dev/null || echo "$HOME")"',
        'cd "$__shunt_cwd" 2>/dev/null || { echo "shunt: $__shunt_cwd"'
        ' "cannot be entered (gone or not accessible); running in $HOME instead"'
        " >&2; cd ~; }",
        "unset __shunt_cwd",
    ]
    if switched:
        lines.append(REMOTE_STATE_TRIM)
        lines.append(
            f'{_state_write(sid)} || echo "shunt: cannot write" {REMOTE_STATE_DIR} '
            f'"- this session will not remember its working directory '
            f'(every command starts at $HOME)" >&2'
        )
    # trap EXIT captures the code + updates cwd on EVERY exit (incl. `exit N` in the
    # command). `rc` is taken first and spent last, so no bookkeeping in here can change
    # what the caller's command returned.
    trap_action = f"rc=$?; {_state_write(sid)}; exit $rc"
    lines.append(f"trap {shlex.quote(trap_action)} EXIT")
    return "\n".join(lines) + "\n" + cmd


def ssh_command(host, cmd, sid, switched=False):
    """ssh + ControlMaster; cwd is kept per session via a remote state-file.

    ⚠ The ControlMaster socket below is the one path here that stays in /tmp, and on
    purpose: it is a LOCAL file, it is meant to die with the machine, and a unix socket
    path has ~104 characters to live in. The cwd state-file is the FAR side's and lives in
    that side's home — see REMOTE_STATE_DIR. The two are not twins.

    Deliberately NO -tt, even though it would fix the one thing this transport does not
    do — killing a command on the far side when the local ssh dies. Measured and
    rejected: a pty makes `python3 -` (how `edit`/`commit` deliver their helper) never
    see EOF, so those commands hang; every pager and stdin reader hangs with them; and
    a controlling terminal EXISTS, so any program can open /dev/tty and block. That last
    one is why a list of workarounds cannot close it. An interrupted command keeps
    running there until it ends on its own — use `shunt bg` for long work.
    """
    key = host["key"]
    sock = f"/tmp/shunt-cm-{sid}-%r@%h:%p.sock"  # per-session AND PER-DESTINATION
    # (%r/%h/%p filled in by ssh, same shape as the CLI's) — otherwise @web-01 then @web-02
    # in the same session share one socket and commands go to the wrong host. %r is the
    # USER: two aliases onto one machine with different accounts (the config allows it)
    # would otherwise ride the first one's master and run as the wrong account.
    remote = _remote_script(cmd, sid, switched)
    opts = [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPath=" + sock,
        "-o",
        "ControlPersist=300",
        "-o",
        "BatchMode=yes",
    ]
    if key:
        opts = ["-i", key] + opts
    return (
        REWRITE_MARKER
        + "ssh "
        + " ".join(shlex.quote(o) for o in opts)
        + " "
        + shlex.quote(host["target"])
        + " "
        + shlex.quote(remote)
    )


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    sid = data.get("session_id") or "default"
    tool = data.get("tool_name") or ""

    # Tools other than Bash are never rewritten — but some of them silently ignore
    # @host mode, so they get a warning instead of nothing.
    if tool != "Bash":
        try:
            warn_if_off_mode(tool, sid)  # exits via warn() when it has something to say
        except Exception:
            pass  # fail-open: never break someone else's tool
        sys.exit(0)

    target_file = os.path.join(CONF, "target." + sid)
    cmd = (data.get("tool_input") or {}).get("command", "")

    # guard against double rewriting — marker is a bash comment prepended by ssh_command
    if cmd.lstrip().startswith("#shunt-rewritten"):
        sys.exit(0)

    s = cmd.strip()

    # shunt CLI commands run LOCALLY (they do the transport themselves) → do not redirect.
    # Only as far as the line IS one command, though — see _shunt_cli_here_or_refuse.
    if s == "shunt" or s.startswith("shunt "):
        _shunt_cli_here_or_refuse(s, sid)  # never returns: runs here, or says why not

    # --- switches ---
    if s == "@local":
        alias_before = read_target(sid)  # named in the failure message below, if any
        failed = _clear_routing(target_file)
        if failed and alias_before is BROKEN_TARGET:
            # Two failures at once, and neither may be dressed as the other: the routing
            # is UNREADABLE, so nothing here can say the session is remote — bash is not
            # sent anywhere, it is refused. Naming a machine would be the very claim the
            # unreadable state forbids: the hook never states what it has not verified.
            # echo() exits.
            echo(
                "[shunt] @local FAILED — the routing state is unreadable AND could not "
                f"be cleared ({failed}). No machine can be named and none is being used: "
                f"bash stays REFUSED until {target_file} is removed by hand."
            )
        if failed:
            # The one place this hook may actively LIE: saying LOCAL while still
            # routed remote sends the next command — and an operator's own hands —
            # to the far machine believing it is here. Say the truth instead and
            # stop; the routing file is untouched, so the session IS still remote.
            echo(
                f"[shunt] @local FAILED — could not leave @{alias_before} ({failed}). "
                "Session is STILL REMOTE; the next command still runs THERE, not here. "
                "Fix it and retry `@local`."
            )
        # best-effort: remove active-host sidecar for this session
        try:
            os.remove(os.path.join(CONF, "active-host." + sid))
        except OSError:
            pass
        # forget the file-tool warning too → entering remote mode again warns again
        try:
            os.remove(os.path.join(CONF, "warned." + sid))
        except OSError:
            pass
        # and the armed switch-marker: there is no far machine left to point at, or to
        # keep house on
        try:
            os.remove(os.path.join(CONF, "switched." + sid))
        except OSError:
            pass
        echo("[shunt] mode: LOCAL")
        sys.exit(0)
    elif s == "@status":
        t = read_target(sid)
        if t is BROKEN_TARGET:
            # "LOCAL" here would be the one answer the caller acts on and the one the
            # hook has not verified. echo() exits.
            echo(
                f"[shunt] UNKNOWN — {target_file} is there but names no host. `@local` to "
                "be local on purpose, or `@<alias>` to route again."
            )
        echo("[shunt] " + (("REMOTE → " + t) if t else "LOCAL"))
        sys.exit(0)
    elif s.startswith("@") and len(s) > 1 and " " not in s:
        alias = s[1:]
        host = resolve_host(alias)
        if not host:
            echo("[shunt] unknown host: " + alias)
            sys.exit(0)
        before = read_target(sid)  # what still stands if the write does not land
        try:
            # The directory is made INSIDE the guard, with the write it exists for. Its
            # failure is the same event as the write's — the switch does not happen — and
            # outside the guard it was not an event at all but a traceback: a hook that
            # raises is a non-blocking error to the harness, which then runs the ORIGINAL
            # line, so `@web-01` went to bash while nobody said the switch had not
            # happened. Narrow on purpose to name: resolve_host() above has just read
            # CONF/shunt.toml, so the directory IS there — this closes the window between
            # the two calls (a parallel session's cleanup, a tmp reaper), not an open path.
            os.makedirs(CONF, exist_ok=True)
            # Through the config module's own atomic writer, because open(…, "w")
            # TRUNCATES first: a write dying in between leaves a file that exists and
            # names no host — the state read_target used to hand back as "local". A
            # temp file and os.replace put it there whole or not at all, and a switch
            # that fails leaves the PREVIOUS routing standing instead of a blank.
            config._write_atomic(target_file, alias)
        except Exception as e:
            # Exiting quietly here is the same lie from the other side: the caller reads
            # no refusal, believes they are on @alias, and their next command runs on the
            # machine they thought they had left. Say what stands instead. The atomic
            # write WIDENED this, too — mkstemp needs the DIRECTORY writable where
            # open(…, "w") needed only the file, so a config dir nobody can write to now
            # fails every switch. echo() exits: no marker is armed, nothing is announced,
            # and the previous routing is untouched.
            stands = (
                "routed somewhere this hook cannot read"
                if before is BROKEN_TARGET
                else f"on @{before}"
                if before
                else "LOCAL"
            )
            reason = getattr(e, "strerror", None) or e
            echo(
                f"[shunt] switch to @{alias} FAILED — could not write {target_file} "
                f"({reason}). Nothing changed; the session is STILL {stands}. "
                f"Fix that and try `@{alias}` again."
            )
        # arm the one-shot marker: the command AFTER a switch is the one that forgets it,
        # and the one that pays for the far side's housekeeping — see _remote_script
        try:
            with open(os.path.join(CONF, "switched." + sid), "w") as f:
                f.write(alias)
        except Exception:
            pass  # a missing marker may not cost the switch
        echo(f"[shunt] mode: REMOTE → {alias} ({host['target']})")
        sys.exit(0)

    # --- remote execution ---
    alias = read_target(sid)
    if alias is BROKEN_TARGET:
        # The same shape as the unresolvable alias below, for the same reason: the one
        # thing that may not happen is this command running HERE while nobody can say
        # whether the session was routed away. Say it, run nothing. echo() exits.
        echo(
            f"[shunt] {target_file} is there but names no host — command NOT run (it "
            "would have run LOCALLY, and this session may be routed away from here). "
            "`@local` to be local on purpose, or `@<alias>` to route again."
        )
    if alias:
        host = resolve_host(alias)
        if not host:
            # The session is routed to a host that no longer resolves — a renamed alias,
            # a broken shunt.toml. Letting the command through unchanged runs it HERE,
            # on the machine the caller believes they left, while `@status` still says
            # REMOTE: `rm -rf /var/log/*` meant for a server deletes the local one.
            # Refusing is not the same as a traceback in front of every command — the
            # third option is the one @unknown already uses: say it, run nothing.
            echo(
                f"[shunt] cannot resolve @{alias} — command NOT run (it would have run "
                f"LOCALLY). Check `shunt hosts`, then `@{alias}` again or `@local`."
            )
            sys.exit(0)  # echo() already exits; said out loud, as above
        # sidecar: record active routing target + append to audit log (fire-and-forget)
        try:
            with open(os.path.join(CONF, "active-host." + sid), "w") as f:
                f.write(alias)
        except Exception:
            pass
        audit(sid, alias, cmd)
        try:
            # ONE question, two riders: the note below and the once-per-switch
            # housekeeping inside the script. Asked HERE because the answer is spent —
            # _just_switched removes the marker it reads.
            switched = _just_switched(sid, alias)
            notice = remote_notice(sid, alias, cmd, switched)
        except Exception:
            # fail-quiet: neither a note nor a sweep may cost the command its rewrite —
            # an unrewritten command runs HERE, on the machine the caller believes they left
            switched, notice = False, ""
        emit(ssh_command(host, cmd, sid, switched), notice)

    sys.exit(0)


if __name__ == "__main__":
    main()
