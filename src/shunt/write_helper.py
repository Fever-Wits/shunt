"""
shunt - write_helper.py - server-side full-file writer with optimistic SHA lock.

Sibling of edit_helper.py.  Runs on the remote machine.
Input: JSON as base64 argv (inline-deploy) or JSON from stdin (interactive).
Output: JSON to stdout.  Zero external dependencies (stdlib only).

Input JSON:
  {"file": str, "content_b64": str, "base_sha": null|str}

  file         - absolute (or relative) path to write.
  content_b64  - base64-encoded bytes of the full new file content.
  base_sha     - sha256 of the expected CURRENT remote content, or null to skip the check
                 (null is only safe for new files; for existing files always supply it).

Output JSON:
  ok       -> {"status":"ok",  "new_sha":str, "verified":true}
             plus "warnings":[str] when the write LANDED but something around it did not -
             a chown that could not follow (the file changed owner), a directory fsync that
             failed (written and readable, not yet durable). Absent when there are none.
  conflict -> {"status":"conflict", "current_sha":str, "base_sha":str}
  error    -> {"status":"error", "message":str}
             a verify-after-write failure adds "verified": false - both of its shapes, the
             read that could not happen and the read that came back wrong.
"""

import base64
import hashlib
import json
import os
import sys
import tempfile

# -- the version floor, stated before any logic runs ---------------------------
# This file executes on SOMEBODY ELSE'S python3 - whatever the far machine happens to
# have. Measured on real hosts rather than assumed: 3.7 to 3.13 - five minor versions,
# none of them chosen by the tool. The floor is DECLARED here rather than inherited from
# whatever the author's laptop ran - hosts in that range fall below the CLI's own 3.11,
# and an inherited floor would have cut every one of them off.
#
# The imports above deliberately come first and are safe to: every one of them is 2.x-era
# stdlib, so none can fail on an interpreter this guard is meant to catch. Keeping them
# there is also what keeps the file free of a lint exception nobody else in this tree has.
#
# WARNING: A runtime guard can only catch APIs. SYNTAX from the future is a SyntaxError at
# COMPILE time - the whole file, before line one runs - and then this never speaks at all.
# So the floor has a second half, and it lives in the tests: both helpers must stay
# PARSEABLE at MIN_PYTHON (tests/test_helpers_far_side.py). Write an f-string in here
# and this guard becomes dead code without one test going red - except that one.
MIN_PYTHON = (3, 3)  # os.replace: the atomic rename both helpers stand on. New in 3.3.

if sys.version_info < MIN_PYTHON:
    # The helpers' own answer shape - JSON on stdout - so the caller reads this the way it
    # reads every other refusal (`shunt edit` prints it verbatim, `cmd_commit` parses it).
    # Written by hand rather than through json.dumps: there is nothing here to escape, and
    # the fewer moving parts between an old interpreter and its own diagnosis, the better.
    # Exit 1, not 0: `shunt edit` promises non-zero unless the edit was applied, and this
    # is the strongest possible "not applied".
    sys.stdout.write(
        '{"status": "error", "message": "python %d.%d on this host, shunt file helpers '
        'need %d.%d+ (os.replace, for the atomic rename)"}\n'
        % (sys.version_info[0], sys.version_info[1], MIN_PYTHON[0], MIN_PYTHON[1])
    )
    sys.exit(1)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def out(d):
    print(json.dumps(d, ensure_ascii=False))


def main():
    try:
        if len(sys.argv) > 1:  # inline deployment: JSON as base64 argv
            req = json.loads(base64.b64decode(sys.argv[1]))
        else:  # local / interactive: JSON from stdin
            req = json.load(sys.stdin)
    except Exception as e:
        return out({"status": "error", "message": "bad request: %s" % e})

    # One guard over the field reads, the twin of edit_helper's: on the `--stdin` path the
    # JSON is the caller's, and `os.path.realpath(7)` or `len(None)` raised outside any try,
    # leaving a traceback where this file promises JSON.
    try:
        path = os.path.realpath(req.get("file", ""))
        content_b64 = req.get("content_b64", "")
        base_sha = req.get("base_sha")  # null -> skip check (new file)
        if not isinstance(content_b64, (str, bytes)):
            raise TypeError("content_b64 must be a base64 string")
        if base_sha is not None and not isinstance(base_sha, str):
            # NOT a traceback - a WRONG ANSWER, which is worse. `base_sha: 7` is truthy and
            # unequal to any hex digest, so the optimistic lock reported `conflict`: the
            # helper telling its caller that somebody else edited the file, having verified
            # nothing of the kind (Sec. 2). A refusal names the field; a conflict sends the
            # reader to look for an edit that never happened.
            raise TypeError("base_sha must be a hex string or null")
    except Exception as e:
        return out({"status": "error", "message": "bad request: %s" % e})

    # --- size guard (before decoding, to catch inflated payloads early) ---
    MAX = int(os.environ.get("SHUNT_EDIT_MAX_BYTES", 64 * 1024 * 1024))
    # rough check: base64 is ~4/3 of binary; a 64-MB file ~ 87 MB of b64
    if len(content_b64) > MAX * 2:
        return out(
            {
                "status": "error",
                "message": "payload too large (%d b64 chars); limit %d bytes raw" % (len(content_b64), MAX),
            }
        )

    try:
        new_bytes = base64.b64decode(content_b64)
    except Exception as e:
        return out({"status": "error", "message": "base64 decode failed: %s" % e})

    if len(new_bytes) > MAX:
        return out(
            {
                "status": "error",
                "message": "content too large (%d bytes > limit %d); use shunt cp + local edit" % (len(new_bytes), MAX),
            }
        )

    # --- read current state ---
    file_existed = os.path.exists(path)
    if file_existed:
        try:
            with open(path, "rb") as f:
                cur_bytes = f.read()
        except Exception as e:
            return out({"status": "error", "message": "read failed: %s" % e})
        cur_sha = sha256(cur_bytes)
    else:
        cur_sha = None

    # --- optimistic SHA lock ---
    if base_sha is not None and base_sha != cur_sha:
        return out(
            {
                "status": "conflict",
                "current_sha": cur_sha,
                "base_sha": base_sha,
                "hint": "the file has changed; re-checkout and try again",
            }
        )

    # --- atomic write: mkstemp in same dir -> write -> fsync -> chmod/chown -> replace -> fsync dir ---
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        return out({"status": "error", "message": "mkdir failed: %s" % e})

    tmp = None
    warnings = []  # things that went wrong AFTER the content was safe - said, never fatal
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".shunt-write-")
        try:
            os.write(fd, new_bytes)
            os.fsync(fd)
        finally:
            os.close(fd)
        if file_existed:
            st = os.stat(path)
            os.chmod(tmp, st.st_mode)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except Exception as e:
                # Swallowed, this is how a file changes hands with nobody told. The temp
                # file belongs to whoever runs this helper, and os.replace carries THAT
                # owner onto the path: the content lands correctly and the OWNERSHIP is
                # the damage - an authorized_keys or a unit file that sshd/systemd then
                # refuses, or worse, accepts from the wrong user. The write still stands,
                # so this is a warning and not an error; saying it is the whole fix.
                warnings.append(
                    "chown to %d:%d failed (%s) - the file now belongs to %d:%d instead"
                    % (st.st_uid, st.st_gid, getattr(e, "strerror", None) or e, os.geteuid(), os.getegid())
                )
        os.replace(tmp, path)  # atomic on the same filesystem
        tmp = None
    except Exception as e:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return out({"status": "error", "message": "write failed: %s" % e})

    # The directory flush sits OUTSIDE the guard above, and the move is the fix: os.replace
    # has already happened, so the new content IS the file. What a failure here costs is
    # DURABILITY - a machine that loses power before the kernel flushes the rename can come
    # back with the old name - not the write. Reported as "write failed" (where it used to
    # live) it made `commit` leave base_sha at the old value, and the NEXT commit then read
    # a remote sha that no longer matched: a CONFLICT invented by a write that succeeded.
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception as e:
        warnings.append(
            "fsync of the directory %s failed (%s) - the file is written and readable now, "
            "but the rename is not flushed to disk: a crash could bring the old file back"
            % (d, getattr(e, "strerror", None) or e)
        )

    # --- verify-after-write ---
    # Both ways this check can fail carry `verified: false`. They did not: the mismatch two
    # lines down said it and the unreadable read beside it did not, so a caller testing that
    # one field got `None` from a helper that had just failed to prove anything - falsy by
    # luck rather than by answer. "Could not read it back" and "read it back and it is
    # wrong" stay SEPARATE messages (the twin in edit_helper keeps the same two), because a
    # caller acts differently on unproven and on proven-wrong.
    try:
        with open(path, "rb") as f:
            vsha = sha256(f.read())
    except Exception as e:
        return out({"status": "error", "message": "verify-read failed: %s" % e, "verified": False})

    expected_sha = sha256(new_bytes)
    if vsha != expected_sha:
        return out({"status": "error", "message": "verify mismatch after write", "new_sha": vsha, "verified": False})

    res = {"status": "ok", "new_sha": vsha, "verified": True}
    if warnings:
        # Present only when there IS one, so the ordinary answer keeps the exact shape
        # every reader of it already knows.
        res["warnings"] = warnings
    out(res)


if __name__ == "__main__":
    main()
