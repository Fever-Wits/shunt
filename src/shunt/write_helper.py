"""
shunt — write_helper.py · server-side full-file writer with optimistic SHA lock.

Sibling of edit_helper.py.  Runs on the remote machine.
Input: JSON as base64 argv (inline-deploy) or JSON from stdin (interactive).
Output: JSON to stdout.  Zero external dependencies (stdlib only).

Input JSON:
  {"file": str, "content_b64": str, "base_sha": null|str}

  file         — absolute (or relative) path to write.
  content_b64  — base64-encoded bytes of the full new file content.
  base_sha     — sha256 of the expected CURRENT remote content, or null to skip the check
                 (null is only safe for new files; for existing files always supply it).

Output JSON:
  ok       → {"status":"ok",  "new_sha":str, "verified":true}
  conflict → {"status":"conflict", "current_sha":str, "base_sha":str}
  error    → {"status":"error", "message":str}
"""
import base64
import hashlib
import json
import os
import sys
import tempfile


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def out(d):
    print(json.dumps(d, ensure_ascii=False))


def main():
    try:
        if len(sys.argv) > 1:           # inline deployment: JSON as base64 argv
            req = json.loads(base64.b64decode(sys.argv[1]))
        else:                           # local / interactive: JSON from stdin
            req = json.load(sys.stdin)
    except Exception as e:
        return out({"status": "error", "message": f"bad request: {e}"})

    path = os.path.realpath(req.get("file", ""))
    content_b64 = req.get("content_b64", "")
    base_sha = req.get("base_sha")      # null → skip check (new file)

    # --- size guard (before decoding, to catch inflated payloads early) ---
    MAX = int(os.environ.get("SHUNT_EDIT_MAX_BYTES", 64 * 1024 * 1024))
    # rough check: base64 is ~4/3 of binary; a 64-MB file ≈ 87 MB of b64
    if len(content_b64) > MAX * 2:
        return out({"status": "error",
                    "message": f"payload too large ({len(content_b64)} b64 chars); "
                               f"limit {MAX} bytes raw"})

    try:
        new_bytes = base64.b64decode(content_b64)
    except Exception as e:
        return out({"status": "error", "message": f"base64 decode failed: {e}"})

    if len(new_bytes) > MAX:
        return out({"status": "error",
                    "message": f"content too large ({len(new_bytes)} bytes > limit {MAX}); "
                               "use shunt cp + local edit"})

    # --- read current state ---
    file_existed = os.path.exists(path)
    if file_existed:
        try:
            with open(path, "rb") as f:
                cur_bytes = f.read()
        except Exception as e:
            return out({"status": "error", "message": f"read failed: {e}"})
        cur_sha = sha256(cur_bytes)
    else:
        cur_sha = None

    # --- optimistic SHA lock ---
    if base_sha is not None and base_sha != cur_sha:
        return out({"status": "conflict", "current_sha": cur_sha, "base_sha": base_sha,
                    "hint": "the file has changed; re-checkout and try again"})

    # --- atomic write: mkstemp in same dir → write → fsync → chmod/chown → replace → fsync dir ---
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        return out({"status": "error", "message": f"mkdir failed: {e}"})

    tmp = None
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
            except Exception:
                pass
        os.replace(tmp, path)           # atomic on the same filesystem
        tmp = None
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception as e:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return out({"status": "error", "message": f"write failed: {e}"})

    # --- verify-after-write ---
    try:
        with open(path, "rb") as f:
            vsha = sha256(f.read())
    except Exception as e:
        return out({"status": "error", "message": f"verify-read failed: {e}"})

    expected_sha = sha256(new_bytes)
    if vsha != expected_sha:
        return out({"status": "error", "message": "verify mismatch after write",
                    "new_sha": vsha, "verified": False})

    out({"status": "ok", "new_sha": vsha, "verified": True})


if __name__ == "__main__":
    main()
