"""Covers files moving in both directions: uploads to the agent, downloads back.

Everything here is about one question — can a name chosen by somebody else
decide where bytes are read or written? The upload half answers it by never
letting the client's filename reach a path, and the download half by resolving
every requested path and refusing anything that leaves the permitted root. Those
two properties get the most checks; the rest of the feature is smoke.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.getcwd())

for _stale in ("./test-attachments.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

ATTACH_ROOT = tempfile.mkdtemp(prefix="aiops-attach-test-")
# Small enough that the oversize check does not have to push 25 MB through the
# test client to prove the cap works.
MAX_BYTES = 64 * 1024

os.environ["AIOPS_DATABASE_URL"] = "sqlite+aiosqlite:///./test-attachments.db"
os.environ["AIOPS_ATTACHMENTS_ROOT"] = ATTACH_ROOT
os.environ["AIOPS_MAX_ATTACHMENT_BYTES"] = str(MAX_BYTES)
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault(
    "AIOPS_WORKSPACE_ROOT", os.path.join(os.getcwd(), ".test-attachment-workspaces")
)

from fastapi.testclient import TestClient  # noqa: E402

from app import attachments as store  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


ROOT = Path(ATTACH_ROOT).resolve()


def files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def wait_idle(client, session_id, timeout=20):
    """Let the queued turn settle.

    There is no agent CLI in the test image, so a run fails within moments — but
    while one is in flight the API answers a second prompt with 409 busy, which
    would mask the validation this suite is actually checking.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = client.get(f"/api/sessions/{session_id}/runs").json()
        if not any(r["status"] in ("queued", "running") for r in runs):
            return True
        time.sleep(0.2)
    return False


# --- the sanitiser, on input the HTTP layer might have cleaned up for us ----
# These go straight at the function so the checks cannot pass merely because
# httpx or python-multipart stripped the path on the way in.
for hostile, expected in [
    ("../../etc/passwd", "passwd"),
    ("/etc/passwd", "passwd"),
    ("..\\..\\windows\\system32\\x", "x"),
    ("....//....//etc/shadow", "shadow"),
    ("..", "upload"),
    (".", "upload"),
    ("", "upload"),
    (None, "upload"),
    ("evil\x00.png", "evil.png"),
    ("NUL", "_NUL"),
    ("com1.txt", "_com1.txt"),
    (".ssh/authorized_keys", "authorized_keys"),
    ("report .txt.", "report .txt"),
]:
    got = store.safe_filename(hostile)
    check(f"filename {hostile!r} is reduced to {expected!r}", got == expected, repr(got))

check(
    "a sanitised name is always a single path component",
    all(
        os.sep not in store.safe_filename(n) and "/" not in store.safe_filename(n)
        for n in ("../../etc/passwd", "a/b/c", "a\\b\\c", "/abs/path")
    ),
)
check(
    "a long name cannot be used to blow up the path",
    len(store.safe_filename("a" * 5000)) <= 120,
)
check(
    "a scriptable type is never what a download is labelled",
    store.download_type("x.html") == "application/octet-stream"
    and store.download_type("x.svg") == "application/octet-stream"
    and store.download_type("x.js") == "application/octet-stream"
    and store.download_type("x.xhtml") == "application/octet-stream",
)
check(
    "an image still gets its real type, so thumbnails render",
    store.download_type("shot.png") == "image/png",
)


with TestClient(app) as c:
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})

    r = c.post("/api/workspaces", json={"name": "attach-demo", "path": "attach-demo"})
    workspace = r.json() if r.status_code == 201 else {}
    check("a workspace to hold the agent's output exists", r.status_code == 201, r.text[:200])
    ws_dir = Path(workspace["path"]).resolve()

    r = c.post(
        "/api/sessions",
        json={"title": "attachments", "provider": "claude", "workspace_id": workspace.get("id")},
    )
    check("session created", r.status_code == 201, r.text[:200])
    sid = r.json()["id"]

    # --- upload, list, download round trip ---------------------------------
    PAYLOAD = b"\x89PNG\r\n\x1a\n" + os.urandom(4096)
    r = c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("screenshot.png", PAYLOAD, "image/png")},
    )
    check("uploading a file succeeds", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    first = r.json() if r.status_code == 201 else {}
    check("the stored size is the real one", first.get("size") == len(PAYLOAD), str(first.get("size")))

    r = c.get(f"/api/sessions/{sid}/attachments")
    check(
        "the upload appears in the session's list",
        r.status_code == 200 and [a["id"] for a in r.json()] == [first["id"]],
        r.text[:200],
    )

    r = c.get(f"/api/sessions/{sid}/attachments/{first['id']}/download")
    check("it downloads", r.status_code == 200, str(r.status_code))
    check("the bytes are identical", r.content == PAYLOAD, f"{len(r.content)} bytes back")
    check(
        "it is served as a download, not as a page",
        "attachment" in r.headers.get("content-disposition", ""),
        r.headers.get("content-disposition", "<none>"),
    )
    check(
        "the content type is the image's own",
        r.headers.get("content-type", "").startswith("image/png"),
        r.headers.get("content-type", "<none>"),
    )
    check(
        "browsers are told not to guess a different type",
        r.headers.get("x-content-type-options") == "nosniff",
    )

    # User content on the same origin as the session cookie must never come back
    # as something a browser will execute.
    r = c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("payload.html", b"<script>alert(document.cookie)</script>", "text/html")},
    )
    check("an html upload is accepted", r.status_code == 201, r.text[:200])
    html_id = r.json()["id"]
    check(
        "but it is not stored as an html content type",
        r.json()["content_type"] == "application/octet-stream",
        r.json()["content_type"],
    )
    r = c.get(f"/api/sessions/{sid}/attachments/{html_id}/download")
    check(
        "and it is not served as one either",
        "text/html" not in r.headers.get("content-type", ""),
        r.headers.get("content-type", "<none>"),
    )

    # --- hostile filenames over real HTTP ----------------------------------
    for hostile, expected in [
        ("../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\x", "x"),
    ]:
        marker = f"landed-{expected}".encode()
        r = c.post(
            f"/api/sessions/{sid}/attachments",
            files={"file": (hostile, marker, "application/octet-stream")},
        )
        ok = r.status_code == 201
        check(f"uploading {hostile!r} is accepted rather than 500", ok, r.text[:200])
        if not ok:
            continue
        row = r.json()
        check(f"{hostile!r} is stored as {expected!r}", row["filename"] == expected, row["filename"])
        landed = ROOT / sid / row["id"] / expected
        check(
            f"{hostile!r} landed at the generated path, inside the attachments root",
            landed.is_file()
            and landed.resolve().is_relative_to(ROOT)
            and landed.read_bytes() == marker,
            str(landed),
        )
        check(
            f"{hostile!r} downloads back as itself",
            c.get(f"/api/sessions/{sid}/attachments/{row['id']}/download").content == marker,
        )

    check(
        "nothing was written outside the attachments root",
        all(p.resolve().is_relative_to(ROOT) for p in files_under(ROOT)),
        str([str(p) for p in files_under(ROOT) if not p.resolve().is_relative_to(ROOT)]),
    )

    # --- two uploads of one name -------------------------------------------
    A, B = b"first-copy" * 100, b"second-copy" * 100
    ids = []
    for payload in (A, B):
        r = c.post(
            f"/api/sessions/{sid}/attachments",
            files={"file": ("screenshot.png", payload, "image/png")},
        )
        ids.append(r.json()["id"] if r.status_code == 201 else None)
    check("two uploads of the same name both succeed", all(ids) and ids[0] != ids[1], str(ids))
    if all(ids):
        got_a = c.get(f"/api/sessions/{sid}/attachments/{ids[0]}/download").content
        got_b = c.get(f"/api/sessions/{sid}/attachments/{ids[1]}/download").content
        check("neither overwrote the other", got_a == A and got_b == B,
              f"{len(got_a)}/{len(got_b)} bytes")
        check(
            "they are on disk under separate generated ids",
            (ROOT / sid / ids[0] / "screenshot.png").is_file()
            and (ROOT / sid / ids[1] / "screenshot.png").is_file(),
        )

    # --- the size cap -------------------------------------------------------
    before = len(files_under(ROOT))
    r = c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("huge.bin", b"x" * (MAX_BYTES + 4096), "application/octet-stream")},
    )
    check("an oversize upload is refused", r.status_code == 413, f"{r.status_code} {r.text[:200]}")
    check("and leaves nothing behind on disk", len(files_under(ROOT)) == before,
          f"{len(files_under(ROOT))} vs {before}")
    r = c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("just-under.bin", b"x" * (MAX_BYTES - 16), "application/octet-stream")},
    )
    check("a file just under the cap is still accepted", r.status_code == 201, str(r.status_code))
    under_id = r.json()["id"] if r.status_code == 201 else None

    # --- removing before sending -------------------------------------------
    if under_id:
        r = c.delete(f"/api/sessions/{sid}/attachments/{under_id}")
        check("an unsent attachment can be removed", r.status_code == 204, str(r.status_code))
        check("its bytes are gone too", not (ROOT / sid / under_id).exists())

    # --- what the agent is actually told ------------------------------------
    r = c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("notes.txt", b"read me", "text/plain")},
    )
    sent_id = r.json()["id"]
    r = c.post(
        f"/api/sessions/{sid}/prompt",
        json={"prompt": "look at this", "attachment_ids": [sent_id]},
    )
    check("a prompt carrying an attachment is accepted", r.status_code == 202, r.text[:300])
    run_id = r.json().get("id") if r.status_code == 202 else None
    check("the turn settles", wait_idle(c, sid))

    r = c.get(f"/api/sessions/{sid}/transcript")
    body = r.json()
    check("the transcript carries the session's attachments", "attachments" in body, r.text[:160])
    sent = [a for a in body.get("attachments", []) if a["id"] == sent_id]
    check(
        "the sent file is bound to the run it went with",
        len(sent) == 1 and sent[0]["run_id"] == run_id,
        str(sent[:1]),
    )
    check(
        "the operator's own prompt is not polluted with container paths",
        all(ATTACH_ROOT not in (run["prompt"] or "") for run in body["runs"]),
        str([run["prompt"] for run in body["runs"]])[:200],
    )
    check(
        "an attachment already sent cannot be deleted out from under the transcript",
        c.delete(f"/api/sessions/{sid}/attachments/{sent_id}").status_code == 409,
    )

    # The runner passes ORM rows; the API returns the same field names, so the
    # transcript's own copy stands in for one.
    suffix = store.prompt_suffix([SimpleNamespace(**sent[0])] if sent else [])
    check(
        "the agent is handed the absolute path of each attachment",
        str(store.stored_path(sid, sent_id, "notes.txt")) in suffix and "attached" in suffix,
        suffix.strip().replace("\n", " ")[:140],
    )

    r = c.post(
        f"/api/sessions/{sid}/prompt",
        json={"prompt": "second", "attachment_ids": [sent_id]},
    )
    check("an attachment cannot be re-sent with a later turn", r.status_code == 400,
          f"{r.status_code} {r.text[:160]}")
    check("the rejected turn settles", wait_idle(c, sid))
    r = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "x", "attachment_ids": ["made-up"]})
    check("an unknown attachment id is refused", r.status_code == 400,
          f"{r.status_code} {r.text[:160]}")

    other = c.post("/api/sessions", json={"provider": "claude"}).json()
    r = c.get(f"/api/sessions/{other['id']}/attachments/{sent_id}/download")
    check("one session cannot download another's attachment", r.status_code == 404, str(r.status_code))

    # --- files the agent produced -------------------------------------------
    (ws_dir / "report.txt").write_bytes(b"the agent wrote this")
    (ws_dir / "nested").mkdir(exist_ok=True)
    (ws_dir / "nested" / "deep.log").write_bytes(b"nested output")
    (ws_dir / ".git").mkdir(exist_ok=True)
    (ws_dir / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main")
    in_range = ws_dir / "d1" / "d2" / "d3"
    in_range.mkdir(parents=True, exist_ok=True)
    (in_range / "in-range.txt").write_bytes(b"still listed")
    (in_range / "d4").mkdir(exist_ok=True)
    (in_range / "d4" / "too-deep.txt").write_bytes(b"below the depth limit")

    r = c.get(f"/api/sessions/{sid}/files")
    check("the session's files are listed", r.status_code == 200, r.text[:200])
    listing = r.json() if r.status_code == 200 else {"files": [], "root": ""}
    names = {f["path"] for f in listing["files"]}
    check("the listing names the directory it walked", listing.get("root") == str(ws_dir),
          str(listing.get("root")))
    check("a file the agent wrote is listed", "report.txt" in names, str(sorted(names))[:200])
    check("so is one a level down", "nested/deep.log" in names, str(sorted(names))[:200])
    check("but .git is not walked", not any(n.startswith(".git") for n in names),
          str(sorted(names))[:200])
    check("a file at the depth limit is still listed", "d1/d2/d3/in-range.txt" in names,
          str(sorted(names))[:200])
    check("one below it is not", "d1/d2/d3/d4/too-deep.txt" not in names,
          str(sorted(names))[:200])
    check("the caps are reported so the UI can state the rule",
          listing.get("max_files") == settings.session_files_max
          and listing.get("max_depth") == settings.session_files_max_depth)

    r = c.get(f"/api/sessions/{sid}/files/download", params={"path": "report.txt"})
    check("a listed file downloads", r.status_code == 200 and r.content == b"the agent wrote this",
          f"{r.status_code} {r.content[:40]!r}")
    check("as a download, not a page",
          "attachment" in r.headers.get("content-disposition", ""),
          r.headers.get("content-disposition", "<none>"))
    r = c.get(f"/api/sessions/{sid}/files/download", params={"path": "nested/deep.log"})
    check("so does a nested one", r.status_code == 200 and r.content == b"nested output",
          str(r.status_code))

    # --- traversal out of the workspace --------------------------------------
    for escape in [
        "../../../etc/passwd",
        "..%2F..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/etc/passwd",
        "%2Fetc%2Fpasswd",
        "nested/../../../../etc/passwd",
    ]:
        r = c.get(f"/api/sessions/{sid}/files/download?path={escape}")
        check(
            f"downloading {escape!r} is refused",
            r.status_code in (400, 404) and b"root:" not in r.content,
            f"{r.status_code} {r.content[:60]!r}",
        )

    # A link is the other way out: the path stays inside, the file does not.
    try:
        (ws_dir / "escape-link").symlink_to("/etc/passwd")
        made_link = True
    except (OSError, NotImplementedError):
        made_link = False
    if made_link:
        r = c.get(f"/api/sessions/{sid}/files/download", params={"path": "escape-link"})
        check("a symlink pointing outside the workspace is refused",
              r.status_code in (400, 404) and b"root:" not in r.content,
              f"{r.status_code} {r.content[:60]!r}")
        r = c.get(f"/api/sessions/{sid}/files")
        check("and it is not offered in the listing",
              "escape-link" not in {f["path"] for f in r.json()["files"]})
    else:
        print("[SKIP] symlink checks — this platform would not create one")

    # --- the count cap ------------------------------------------------------
    # Last, because it buries everything else in the listing.
    flood = ws_dir / "flood"
    flood.mkdir(exist_ok=True)
    for n in range(settings.session_files_max + 5):
        (flood / f"out-{n:04d}.bin").write_bytes(b"x")
    r = c.get(f"/api/sessions/{sid}/files")
    listing = r.json()
    check("the listing stops at the cap", len(listing["files"]) <= settings.session_files_max,
          str(len(listing["files"])))
    check("and says so rather than truncating silently", listing["truncated"] is True)

    # --- authentication ------------------------------------------------------
    c.post("/api/auth/logout")
    unauthenticated = [
        ("POST", f"/api/sessions/{sid}/attachments"),
        ("GET", f"/api/sessions/{sid}/attachments"),
        ("GET", f"/api/sessions/{sid}/attachments/{sent_id}/download"),
        ("DELETE", f"/api/sessions/{sid}/attachments/{sent_id}"),
        ("GET", f"/api/sessions/{sid}/files"),
        ("GET", f"/api/sessions/{sid}/files/download?path=report.txt"),
    ]
    for method, path in unauthenticated:
        kwargs = {"files": {"file": ("a.txt", b"a", "text/plain")}} if method == "POST" else {}
        r = c.request(method, path, **kwargs)
        check(f"unauthenticated {method} {path.split(sid)[-1]} is 401",
              r.status_code == 401, f"{r.status_code} {r.text[:120]}")


shutil.rmtree(ATTACH_ROOT, ignore_errors=True)
# Only the directory this suite made: the workspace root may be shared with the
# other suites in the same run.
shutil.rmtree(ws_dir, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
