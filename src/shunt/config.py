#!/usr/bin/env python3
"""
shunt — config.py · the hosts, read (and written) in ONE place.

~/.config/shunt/shunt.toml:

    key = "~/.ssh/id_ed25519_shunt"           # default identity for every host below

    [hosts]
    web-01  = "user@203.0.113.10"
    web-02  = "user@203.0.113.20"
    special = { target = "user@203.0.113.30", key = "~/.ssh/another_key" }

A bare string IS the target; the inline table adds a per-host `key`, which wins over the
top-level default. Everything a host needs lives here on purpose: an address split between
this file and ~/.ssh/config breaks silently when only one side is edited — machines added
on one side, the identity left on the other, and access is gone without a word.

Legacy: with no shunt.toml the old `hosts` file (`<alias> ssh <target> [key=PATH]`) is still
read, so an existing setup keeps working; a notice on stderr says where the new place is.
Nothing is migrated automatically — that is the owner's move, not the tool's.

Both callers (cli.py, pretool.py) pass their own conf dir: the knowledge of the FORMAT
lives here, the knowledge of the LOCATION stays with the caller (which is also what lets
the tests point it at a temp dir).
"""
import json
import os
import sys
import tempfile
import tomllib  # stdlib since Python 3.11

TOML_NAME = "shunt.toml"
LEGACY_NAME = "hosts"

_legacy_notice_said = False     # the legacy notice is said ONCE per process


def config_path(conf_dir):
    """The file the hosts come from: shunt.toml, else the legacy `hosts`, else None."""
    for name in (TOML_NAME, LEGACY_NAME):
        path = os.path.join(conf_dir, name)
        if os.path.exists(path):
            return path
    return None


def load_hosts(conf_dir):
    """{alias: {'alias', 'target', 'key'}} — empty when nothing is configured.

    Raises on a broken file (a config error must be loud — silence is what started this).
    """
    path = config_path(conf_dir)
    if not path:
        return {}
    if os.path.basename(path) == TOML_NAME:
        return _load_toml(path)
    hosts, dropped = _load_legacy(path)
    _say_legacy_once(conf_dir, path, dropped)
    return hosts


def resolve(conf_dir, alias):
    """One host by alias (leading '@' optional), or None."""
    return load_hosts(conf_dir).get(alias.lstrip("@"))


AUDIT_DEFAULTS = {"trim_at_mb": 100, "drop_months": 2}


def audit_settings(conf_dir):
    """The [audit] section, with defaults for whatever is unset.

        [audit]
        trim_at_mb  = 100    # size at which trimming fires
        drop_months = 2      # how much of the OLDEST history goes when it does

    The defaults say what the log IS: an archive, with trimming as a fuse rather than a
    policy. 100 MB is out of reach at any ordinary rate (measured: ~15 KB per six weeks),
    and that is the intent — a fuse that blows regularly is a policy in disguise. Keeping
    the history long is the whole point: the question people bring to an audit log is
    "where did we download that from, two months ago", and a short window answers it with
    silence.

    Bad or missing values fall back to the defaults rather than raising: the log must
    never be the reason a command fails.
    """
    out = dict(AUDIT_DEFAULTS)
    try:
        with open(os.path.join(conf_dir, TOML_NAME), "rb") as f:
            section = tomllib.load(f).get("audit") or {}
    except Exception:
        return out
    for key in out:
        value = section.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            out[key] = value
    return out


def add_host(conf_dir, alias, target, key=None):
    """Write the host into shunt.toml and return the line written.

    Idempotent: an entry with the same alias is REPLACED, never duplicated. The rest of
    the file — comments included — survives, because it is the owner's file, not ours.
    """
    os.makedirs(conf_dir, exist_ok=True)
    path = os.path.join(conf_dir, TOML_NAME)
    if not os.path.exists(path):
        line = _entry_line(alias, target, None)     # a fresh file: the key becomes the default
        _write_atomic(path, _fresh_document(line, key))
        return line

    with open(path, "rb") as f:
        default_key = tomllib.load(f).get("key")    # raises on a broken file → we do not clobber it
    line = _entry_line(alias, target, key if key and key != default_key else None)
    with open(path) as f:
        lines = f.read().splitlines()
    start, end = _hosts_section(lines)
    body = lines[start:end]
    for i, existing in enumerate(body):
        if _defines(existing, alias):
            body[i] = line          # in place: the comment above it still describes it
            break
    else:
        body.append(line)
    lines[start:end] = body
    _write_atomic(path, "\n".join(lines) + "\n")
    return line


# ── reading ───────────────────────────────────────────────────────────────────

def _host(alias, target, key):
    return {"alias": alias, "target": target,
            "key": os.path.expanduser(key) if key else None}


def _load_toml(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    default_key = data.get("key")
    hosts = {}
    for alias, entry in (data.get("hosts") or {}).items():
        if isinstance(entry, str):
            target, key = entry, default_key
        elif isinstance(entry, dict):
            target, key = entry.get("target"), entry.get("key", default_key)
        else:
            target, key = None, None
        if not target:
            raise ValueError(
                f'{path}: host "{alias}" must be "user@host" or '
                '{ target = "user@host", key = "~/.ssh/id" }')
        hosts[alias] = _host(alias, target, key)
    return hosts


def _load_legacy(path):
    """Old format, one host per line: `<alias> ssh <target> [key=PATH]`.

    Returns (hosts, dropped) — a line in any other shape is skipped and COUNTED: ssh is
    the only transport, so a line that does not name it carries something that is not an
    ssh target and must not silently become a host; and a host that disappears without a
    word is exactly the silent failure this config is built against.
    """
    hosts, dropped = {}, 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split()
            if len(p) >= 3 and p[1] == "ssh":
                key = next((o[4:] for o in p[3:] if o.startswith("key=")), None)
                hosts[p[0]] = _host(p[0], p[2], key)
            else:
                dropped += 1
    return hosts, dropped


def _say_legacy_once(conf_dir, legacy_path, dropped=0):
    """Where the config belongs now — said once, on STDERR.

    stdout is a protocol for both callers (the hook writes JSON there, the CLI passes
    remote output through), so a notice may never go that way.
    """
    global _legacy_notice_said
    if _legacy_notice_said:
        return
    _legacy_notice_said = True
    new_path = os.path.join(conf_dir, TOML_NAME)
    sys.stderr.write(
        f"shunt: reading the legacy host list {legacy_path}\n"
        f"       the new place is {new_path} (see shunt.toml.example) — nothing is migrated\n"
        "       automatically; the legacy file keeps working until you move it\n")
    if dropped:
        sys.stderr.write(
            f"       {dropped} line(s) skipped: not in the `<alias> ssh <target>` form —\n"
            "       shunt speaks ssh only\n")


# ── writing ───────────────────────────────────────────────────────────────────

def _write_atomic(path, text):
    """Put the file in place in ONE step: temp file in the SAME directory → os.replace.

    Two `shunt install` runs at once would otherwise interleave their writes and leave a
    half config behind; whoever reads it — the hook does, before every command — must see
    either the whole old file or the whole new one. fsync before the rename, because
    without it a crash leaves a zero-length file where a config used to be.

    The boundary, on purpose: this is read-modify-write with no lock, so two simultaneous
    installs can still cost one of the two entries (run install again and it is back).
    A LOST entry is recoverable; a TORN file is not — only the second one is guarded here.
    """
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode)    # the owner's permissions survive
        os.replace(tmp, path)                       # atomic on POSIX, same filesystem
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(d, os.O_RDONLY)                   # the rename itself must reach the disk
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _toml_str(value):
    """A TOML basic string. json.dumps produces exactly that escaping for our values."""
    return json.dumps(value, ensure_ascii=False)


def _toml_key(alias):
    """Bare key when TOML allows it (A-Za-z0-9_-), quoted otherwise."""
    bare = alias and all((c.isascii() and c.isalnum()) or c in "_-" for c in alias)
    return alias if bare else _toml_str(alias)


def _entry_line(alias, target, key):
    if key:
        return f"{_toml_key(alias)} = {{ target = {_toml_str(target)}, key = {_toml_str(key)} }}"
    return f"{_toml_key(alias)} = {_toml_str(target)}"


def _fresh_document(entry_line, key):
    head = "# shunt hosts — see shunt.toml.example for the full form\n"
    if key:
        head += f"key = {_toml_str(key)}      # default identity for every host below\n"
    return head + "\n[hosts]\n" + entry_line + "\n"


def _hosts_section(lines):
    """(start, end) of the body of [hosts]; appends the header when it is missing."""
    for i, line in enumerate(lines):
        if line.strip() == "[hosts]":
            for j in range(i + 1, len(lines)):
                if lines[j].lstrip().startswith("["):
                    return i + 1, j
            return i + 1, len(lines)
    lines.append("")
    lines.append("[hosts]")
    return len(lines), len(lines)


def _defines(line, alias):
    """True if this line assigns THIS alias (bare or quoted key)."""
    name, sep, _ = line.partition("=")
    return bool(sep) and name.strip().strip('"') == alias
