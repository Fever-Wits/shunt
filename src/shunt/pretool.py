#!/usr/bin/env python3
"""
shunt — pretool.py · PreToolUse hook
(matcher: Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob)

Two jobs, one file — because both need the same answer to "where is this session routed?":

  Bash   → REWRITE: the command runs on the chosen remote machine, transparently.
  others → WARN: these tools do NOT follow the mode (the hook rewrites bash only), so
           running them in remote mode is reported instead of silently doing the wrong
           thing. Never blocked — remote-mode-plus-local-file is legitimate as often as
           it is a mistake; only the silence was the defect.

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


def emit(command):
    """Return a rewritten command to Claude Code and exit."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"command": command}}}))
    sys.exit(0)


def echo(msg):
    emit("echo " + shlex.quote(msg))


def warn(msg):
    """Say something into the agent's context and allow the call (never block).

    Blocking would be wrong here: working remotely with local file tools is legitimate
    as often as it is a mistake. Only the SILENCE is the defect.
    """
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": msg}}))
    sys.exit(0)


DAYS_PER_MONTH = 30     # `drop_months` is a human unit, not a calendar one — see _trim_audit


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
            f.write("%s sid=%s host=%s :: %s\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%S"), sid, alias, escape_cmd(cmd)))
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
    return time.strftime("%Y-%m-%d",
                         time.localtime(time.mktime(t) + months * DAYS_PER_MONTH * 86400))


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


RECORD_HEAD = re.compile(r"\d{4}-\d{2}-\d{2}T")   # exactly how audit() opens every line


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
            records[-1] += line          # inherited spill-over: rare, and records are short
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
    cutoff = _cut_date(lines[0], drop_months)            # oldest line dates the cut
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
    if total > ceiling:                     # last resort: the history itself is the flood
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


def read_target(sid):
    """Alias of the host this session is routed to, or "" for local."""
    try:
        with open(os.path.join(CONF, "target." + sid)) as f:
            return f.read().strip()
    except Exception:
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
    if not alias:
        return                                  # local — nothing to warn about
    if tool == "Agent":
        # Every spawn matters: the agent inherits the mode and reads missing local
        # files as facts about the world. Observed once as "the Bash tool briefly
        # lost access to the working directory" — it had not; it was elsewhere.
        warn("⚠ shunt: you are on @%s — a spawned agent INHERITS this and will run "
             "its bash there, reading absent local files as facts. Switch with "
             "`@local` first, or make sure the agent is meant to work on %s."
             % (alias, alias))
    if tool in LOCAL_DISK_TOOLS and not _warned_before(sid, alias):
        warn("⚠ shunt: you are on @%s, but %s works on the LOCAL disk — the mode covers "
             "bash only. Remote file → `shunt read/edit @%s …`; remote search → "
             "`shunt run @%s \"grep -rn PATTERN /path\"`. (said once per host)"
             % (alias, tool, alias, alias))


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


def ssh_command(host, cmd, sid):
    """ssh + ControlMaster; cwd is kept per session via a remote state-file.

    Deliberately NO -tt, even though it would fix the one thing this transport does not
    do — killing a command on the far side when the local ssh dies. Measured and
    rejected: a pty makes `python3 -` (how `edit`/`commit` deliver their helper) never
    see EOF, so those commands hang; every pager and stdin reader hangs with them; and
    a controlling terminal EXISTS, so any program can open /dev/tty and block. That last
    one is why a list of workarounds cannot close it. An interrupted command keeps
    running there until it ends on its own — use `shunt bg` for long work.
    """
    key = host["key"]
    sock = "/tmp/shunt-cm-%s-%%r@%%h:%%p.sock" % sid  # per-session AND PER-DESTINATION
    # (%r/%h/%p filled in by ssh, same shape as the CLI's) — otherwise @web-01 then @web-02
    # in the same session share one socket and commands go to the wrong host. %r is the
    # USER: two aliases onto one machine with different accounts (the config allows it)
    # would otherwise ride the first one's master and run as the wrong account.
    state = "/tmp/shunt-cwd-%s" % sid
    # trap EXIT captures the code + updates cwd on EVERY exit (incl. `exit N` in the command)
    trap_action = "rc=$?; pwd > %s 2>/dev/null; exit $rc" % shlex.quote(state)
    remote = (
        'cd "$(cat %s 2>/dev/null || echo "$HOME")" 2>/dev/null || cd ~\n' % shlex.quote(state)
        + "trap " + shlex.quote(trap_action) + " EXIT\n"
        + cmd
    )
    opts = ["-o", "StrictHostKeyChecking=accept-new",
            "-o", "ControlMaster=auto", "-o", "ControlPath=" + sock,
            "-o", "ControlPersist=300", "-o", "BatchMode=yes"]
    if key:
        opts = ["-i", key] + opts
    return (REWRITE_MARKER
            + "ssh " + " ".join(shlex.quote(o) for o in opts)
            + " " + shlex.quote(host["target"])
            + " " + shlex.quote(remote))


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
            warn_if_off_mode(tool, sid)   # exits via warn() when it has something to say
        except Exception:
            pass                          # fail-open: never break someone else's tool
        sys.exit(0)

    target_file = os.path.join(CONF, "target." + sid)
    cmd = (data.get("tool_input") or {}).get("command", "")

    # guard against double rewriting — marker is a bash comment prepended by ssh_command
    if cmd.lstrip().startswith("#shunt-rewritten"):
        sys.exit(0)

    s = cmd.strip()

    # shunt CLI commands run LOCALLY (they do the transport themselves) → do not redirect
    if s == "shunt" or s.startswith("shunt "):
        sys.exit(0)

    # --- switches ---
    if s == "@local":
        try:
            os.remove(target_file)
        except OSError:
            pass
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
        echo("[shunt] mode: LOCAL")
        sys.exit(0)
    elif s == "@status":
        t = read_target(sid)
        echo("[shunt] " + (("REMOTE → " + t) if t else "LOCAL"))
        sys.exit(0)
    elif s.startswith("@") and len(s) > 1 and " " not in s:
        alias = s[1:]
        host = resolve_host(alias)
        if not host:
            echo("[shunt] unknown host: " + alias)
            sys.exit(0)
        os.makedirs(CONF, exist_ok=True)
        try:
            with open(target_file, "w") as f:
                f.write(alias)
        except Exception:
            sys.exit(0)
        echo("[shunt] mode: REMOTE → %s (%s)" % (alias, host["target"]))
        sys.exit(0)

    # --- remote execution ---
    alias = read_target(sid)
    if alias:
        host = resolve_host(alias)
        if not host:
            # The session is routed to a host that no longer resolves — a renamed alias,
            # a broken shunt.toml. Letting the command through unchanged runs it HERE,
            # on the machine the caller believes they left, while `@status` still says
            # REMOTE: `rm -rf /var/log/*` meant for a server deletes the local one.
            # Refusing is not the same as a traceback in front of every command — the
            # third option is the one @unknown already uses: say it, run nothing.
            echo("[shunt] cannot resolve @%s — command NOT run (it would have run "
                 "LOCALLY). Check `shunt hosts`, then `@%s` again or `@local`."
                 % (alias, alias))
            sys.exit(0)                # echo() already exits; said out loud, as above
        # sidecar: record active routing target + append to audit log (fire-and-forget)
        try:
            with open(os.path.join(CONF, "active-host." + sid), "w") as f:
                f.write(alias)
        except Exception:
            pass
        audit(sid, alias, cmd)
        emit(ssh_command(host, cmd, sid))

    sys.exit(0)


if __name__ == "__main__":
    main()
