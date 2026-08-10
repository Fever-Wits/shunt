"""
shunt — edit_helper.py · server-side editor by CONTENT (not by line number).

Runs on the remote machine (or locally). Input = JSON on stdin, output = JSON on stdout.
Zero external dependencies (stdlib only). Payload via stdin → ZERO shell escaping (binary-safe).

Semantics like the built-in Edit: `old → new`, requires UNIQUENESS (expected matches; otherwise refuse).
Byte-exact: the match and the replacement happen on the raw bytes, so nothing outside the
match is ever rewritten — not a byte that is invalid UTF-8, not a line ending elsewhere in
the file. `normalized: true` means only that the match was found in a line-ending variant.
Triangulated design (web research 2026-06-22) + borrowings: a pattern from prior internal tooling
(checksum/atomic), desktop-commander (count-and-refuse + fuzzy-diag), Aider/Claude str_replace
(address by content).

Input (JSON):
  {"file": str, "old": str, "new": str, "expected": 1, "base_sha": null|str, "dry_run": false}
Output (JSON, status):
  ok        → {status, count, new_sha, verified, diff, normalized}
              plus "warnings":[str] when the write LANDED but something around it did not —
              a chown that could not follow (the file changed owner), a directory fsync that
              failed (written and readable, not yet durable). Absent when there are none.
  not_found → {status, hint}                          (0 matches)
  ambiguous → {status, count, expected, hint}         (count-and-refuse)
  conflict  → {status, current_sha, base_sha}         (optimistic SHA-256 lock)
  error     → {status, message}
"""

import difflib
import hashlib
import json
import os
import sys
import tempfile


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def out(d):
    print(json.dumps(d, ensure_ascii=False))


def eol_variants(old: bytes, new: bytes):
    """The (old, new) pairs to look for, in order: as given · all-LF · all-CRLF.

    A line ending is the commonest reason a literal match fails, so the variants exist —
    but they only ever change what is LOOKED FOR. The file is never rewritten into
    another style: that would touch lines nobody asked about, and the diff (computed
    from the real bytes) would not even show it. Only the match itself changes.
    """
    lf = (old.replace(b"\r\n", b"\n"), new.replace(b"\r\n", b"\n"))
    crlf = (lf[0].replace(b"\n", b"\r\n"), lf[1].replace(b"\n", b"\r\n"))
    return [(old, new), lf, crlf]


def display_lines(b: bytes):
    """Bytes as lines for the diff — lossy on purpose, and never written back."""
    return b.decode("utf-8", "replace").splitlines(keepends=True)


def main():
    try:
        if len(sys.argv) > 1:  # inline deployment: JSON as base64 argv
            import base64

            req = json.loads(base64.b64decode(sys.argv[1]))
        else:  # local/interactive: JSON from stdin
            req = json.load(sys.stdin)
    except Exception as e:
        return out({"status": "error", "message": f"bad request: {e}"})

    path = os.path.realpath(req.get("file", ""))  # resolve symlink → edit the target, not the link;
    # resolved_path is included in responses so the caller can see what was actually edited
    old = req.get("old", "")
    new = req.get("new", "")
    expected = int(req.get("expected", 1))
    base_sha = req.get("base_sha")
    dry = bool(req.get("dry_run", False))

    if not old:
        return out({"status": "error", "message": "old is empty"})
    try:
        st0 = os.stat(path)
    except Exception as e:
        return out({"status": "error", "message": f"stat failed: {e}", "resolved_path": path})
    MAX = int(os.environ.get("SHUNT_EDIT_MAX_BYTES", str(64 * 1024 * 1024)))
    if st0.st_size > MAX:
        return out(
            {
                "status": "error",
                "message": f"file too large ({st0.st_size} bytes > limit {MAX}); use shunt cp + local edit",
                "resolved_path": path,
            }
        )
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return out({"status": "error", "message": f"read failed: {e}", "resolved_path": path})

    cur_sha = sha256(raw)
    # optimistic lock: was the file touched between my read and this write?
    if base_sha and base_sha != cur_sha:
        return out(
            {
                "status": "conflict",
                "current_sha": cur_sha,
                "base_sha": base_sha,
                "hint": "the file has changed; re-read and try again",
                "resolved_path": path,
            }
        )

    # Match and replace on BYTES, like the sibling write_helper.py. Decoding the file to
    # text and writing that text back is how a helper that answers `ok` destroys a file:
    # `errors="replace"` turns every byte that is not UTF-8 into U+FFFD, and the whole
    # file is then re-encoded from the damaged text. Here nothing outside the match is
    # ever rewritten — not one byte, not one line ending.
    old_b = old.encode("utf-8")
    new_b = new.encode("utf-8")

    # match: as given first; if 0 → the line-ending variants, where most failures die
    count, normalized = 0, False
    for variant, (o, n) in enumerate(eol_variants(old_b, new_b)):
        count = raw.count(o)
        if count:
            normalized = variant > 0
            break

    if count == 0:
        return out(
            {
                "status": "not_found",
                "hint": "old not found (tried as given, all-LF and all-CRLF); add "
                "unique context, or check the file is UTF-8",
                "resolved_path": path,
            }
        )
    if count != expected:
        return out(
            {
                "status": "ambiguous",
                "count": count,
                "expected": expected,
                "hint": "add surrounding context for uniqueness",
                "resolved_path": path,
            }
        )

    new_bytes = raw.replace(o, n)
    # The diff is computed from the bytes on disk and the bytes about to be written —
    # decoded only for display. Computed from anything else it can HIDE a change: it
    # once showed one edited line while the whole file was being converted to CRLF.
    diff = "".join(
        difflib.unified_diff(display_lines(raw), display_lines(new_bytes), fromfile=path, tofile=path + " (edited)")
    )

    if dry:
        return out(
            {
                "status": "ok",
                "dry_run": True,
                "count": count,
                "new_sha": sha256(new_bytes),
                "diff": diff,
                "normalized": normalized,
                "resolved_path": path,
            }
        )

    # atomic write: temp in the SAME directory + fsync(data) + rename + fsync(dir)
    d = os.path.dirname(path) or "."
    tmp = None
    warnings = []  # things that went wrong AFTER the content was safe — said, never fatal
    try:
        st = os.stat(path)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".shunt-edit-")
        try:
            os.write(fd, new_bytes)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, st.st_mode)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except Exception as e:
            # Swallowed, this is how a file changes hands with nobody told. The temp file
            # belongs to whoever runs this helper, and os.replace carries THAT owner onto
            # the path: the edit lands correctly and the OWNERSHIP is the damage — an
            # authorized_keys or a unit file that sshd/systemd then refuses, or worse,
            # accepts from the wrong user. The edit still stands, so this is a warning and
            # not an error; saying it is the whole fix. Twin of the one in write_helper.py.
            warnings.append(
                f"chown to {st.st_uid}:{st.st_gid} failed ({getattr(e, 'strerror', None) or e}) — "
                f"the file now belongs to {os.geteuid()}:{os.getegid()} instead"
            )
        os.replace(tmp, path)  # atomic on the same filesystem
        tmp = None
    except Exception as e:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return out({"status": "error", "message": f"write failed: {e}"})

    # The directory flush sits OUTSIDE the guard above, and the move is the fix: os.replace
    # has already happened, so the edited content IS the file. What a failure here costs is
    # DURABILITY — a machine that loses power before the kernel flushes the rename can come
    # back with the old file — not the edit. Reported as "write failed" (where it used to
    # live) it told the caller their edit had not been applied while it had, which is the
    # one lie this helper's verify-after-write exists to make impossible.
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception as e:
        warnings.append(
            f"fsync of the directory {d} failed ({getattr(e, 'strerror', None) or e}) — the file is "
            "written and readable now, but the rename is not flushed to disk: a crash could bring "
            "the old file back"
        )

    # verify-after-write (our niche — no SSH MCP does this)
    with open(path, "rb") as f:
        vsha = sha256(f.read())
    ok = vsha == sha256(new_bytes)
    res = {
        "status": "ok" if ok else "error",
        "count": count,
        "new_sha": vsha,
        "verified": ok,
        "diff": diff,
        "normalized": normalized,
        "resolved_path": path,
    }
    if not ok:
        res["message"] = "verify mismatch after write"
    if warnings:
        # Present only when there IS one, so the ordinary answer keeps the exact shape
        # every reader of it already knows.
        res["warnings"] = warnings
    out(res)


if __name__ == "__main__":
    main()
