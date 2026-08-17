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

from app import attachments as store, browsing  # noqa: E402
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

    # --- screenshots the agent's browser took --------------------------------
    # The third kind of bytes this application serves, and the one that arrives
    # from a page somebody else wrote. It is stored the way an attachment is —
    # inside the session's own directory in the attachments volume — so that
    # reopening a finished conversation shows what the agent's browser saw. The
    # checks below are about that lifetime, and about the guards on a name that
    # comes from the agent's side of the boundary.
    PNG = b"\x89PNG\r\n\x1a\n"
    SHOT = PNG + os.urandom(2048)
    # Written through the same call the loopback endpoint uses, so what the test
    # exercises is the storing code and not a fixture's idea of where files go.
    browsing.keep_shot(sid, run_id, "screenshot-001.png", SHOT)
    shots_dir = browsing.run_shots_dir(sid, run_id)
    check("a capture is kept inside the session's own directory, with its uploads",
          shots_dir.resolve().is_relative_to(store.session_dir(sid).resolve()), str(shots_dir))

    r = c.get(f"/api/runs/{run_id}/screenshots/screenshot-001.png")
    check("a screenshot the agent's browser took downloads", r.status_code == 200,
          f"{r.status_code} {r.text[:160]}")
    check("the bytes are the ones on disk", r.content == SHOT, f"{len(r.content)} bytes back")
    check("it is labelled as the image it is",
          r.headers.get("content-type", "").startswith("image/png"),
          r.headers.get("content-type", "<none>"))
    check("browsers are told not to guess a different type for it",
          r.headers.get("x-content-type-options") == "nosniff")
    check("and it is served as a download, not as a page",
          "attachment" in r.headers.get("content-disposition", ""),
          r.headers.get("content-disposition", "<none>"))

    # The lifetime that is the point of storing them here at all: the turn that
    # took it is long over, and its per-run scratch directory with it.
    scratch = Path(browsing.make_run_dir(run_id))
    browsing.grants.issue(
        run_id, {"routes": [], "subnets": [], "systems": []}, None, "test", str(scratch)
    )
    browsing.grants.revoke(run_id)
    check("ending the run takes the agent's own scratch copies with it",
          not scratch.exists(), str(scratch))
    r = c.get(f"/api/runs/{run_id}/screenshots/screenshot-001.png")
    check("but the operator's copy survives the end of the turn",
          r.status_code == 200 and r.content == SHOT, f"{r.status_code} {len(r.content)} bytes")

    # The name is generated by AIOps, so anything that is not one of its own is
    # not a name to sanitise — it is a request to refuse. Checked on the way in
    # as well as on the way out: the write side takes its name from the agent
    # too, and a guard only on reads would already have the file on disk.
    for bad in [
        "screenshot-1.png",
        "screenshot-001.PNG",
        "screenshot-001.png.exe",
        "..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "%2Fetc%2Fpasswd",
        "notes.txt",
    ]:
        r = c.get(f"/api/runs/{run_id}/screenshots/{bad}")
        check(f"asking for {bad!r} is refused",
              r.status_code in (400, 404, 405) and b"root:" not in r.content,
              f"{r.status_code} {r.content[:60]!r}")
    for bad in ["screenshot-1.png", "../../../etc/cron.d/x", "/etc/cron.d/x", "", "shot.png"]:
        try:
            browsing.keep_shot(sid, run_id, bad, SHOT)
            refused = False
        except browsing.ShotRefused:
            refused = True
        check(f"and storing one called {bad!r} is refused too", refused)

    # A link under a name the endpoint *does* accept is the way out that the
    # name check alone would not close. The store is app-owned rather than
    # group-writable by the agent, unlike the run's scratch directory, so this
    # is now a guard against a future mistake rather than against today's agent
    # — which is exactly when a test is worth having.
    try:
        (shots_dir / "screenshot-009.png").symlink_to("/etc/passwd")
        planted = True
    except (OSError, NotImplementedError):
        planted = False
    if planted:
        r = c.get(f"/api/runs/{run_id}/screenshots/screenshot-009.png")
        check("a symlink planted under an accepted name is refused",
              r.status_code == 404 and b"root:" not in r.content,
              f"{r.status_code} {r.content[:60]!r}")
        check("and the resolver behind it returns nothing at all",
              browsing.stored_shot(sid, run_id, "screenshot-009.png") is None)
        (shots_dir / "screenshot-009.png").unlink()
    else:
        print("[SKIP] planted-symlink check — this platform would not create one")

    r = c.get(f"/api/runs/{run_id}/screenshots/screenshot-002.png")
    check("a screenshot that was never taken is 404", r.status_code == 404, str(r.status_code))
    r = c.get(f"/api/runs/{run_id + 9999}/screenshots/screenshot-001.png")
    check("so is one on a run that does not exist", r.status_code == 404, str(r.status_code))

    # Requirement 4: turns that ran before any of this existed have nothing
    # stored, and must read as an ordinary absence rather than a fault.
    older = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "an earlier turn"})
    check("a second turn to stand in for one from before this existed",
          older.status_code == 202, older.text[:200])
    wait_idle(c, sid)
    r = c.get(f"/api/runs/{older.json()['id']}/screenshots/screenshot-001.png")
    check("a turn that never stored a capture degrades to 404, not to an error",
          r.status_code == 404, f"{r.status_code} {r.text[:160]}")
    check("saying why, rather than pretending the run never existed",
          "turn" in r.text.lower(), r.text[:160])

    # --- what a capture has to be, and how much of it there may be ----------
    try:
        browsing.keep_shot(sid, run_id, "screenshot-002.png", b"<html>not an image")
        typed = False
    except browsing.ShotRefused:
        typed = True
    check("bytes that are not a PNG are not stored as one", typed)
    try:
        browsing.keep_shot(sid, run_id, "screenshot-001.png", SHOT)
        overwrote = True
    except browsing.ShotRefused:
        overwrote = False
    check("a second capture cannot replace one the operator was already shown",
          not overwrote)

    big = PNG + b"x" * settings.browser_screenshot_max_bytes
    try:
        browsing.keep_shot(sid, run_id, "screenshot-003.png", big)
        bounded = False
    except browsing.ShotRefused:
        bounded = True
    check("one capture larger than the per-capture ceiling is refused", bounded)

    # The per-turn cap, which used to be the only bound because the directory
    # went away at the end of the run. It still holds, and it holds in the app
    # rather than only in the agent's own process.
    was_cap = settings.browser_max_screenshots
    settings.browser_max_screenshots = 3
    try:
        for n in (2, 3, 4):
            try:
                browsing.keep_shot(sid, run_id, f"screenshot-{n:03d}.png", SHOT)
                stored_ok = True
            except browsing.ShotRefused:
                stored_ok = False
            if n < 4:
                check(f"capture {n} of a turn under the cap is stored", stored_ok)
            else:
                check("the per-turn cap still holds, so a loop cannot fill the disk",
                      not stored_ok)
    finally:
        settings.browser_max_screenshots = was_cap

    # And the bound that replaces "deleted at the end of the run": what one
    # whole conversation may keep, across every turn in it.
    was_budget = settings.browser_session_screenshot_bytes
    settings.browser_session_screenshot_bytes = browsing.session_shot_bytes(sid) + 10
    try:
        browsing.keep_shot(sid, run_id + 1, "screenshot-001.png", SHOT)
        budgeted = False
    except browsing.ShotRefused:
        budgeted = True
    finally:
        settings.browser_session_screenshot_bytes = was_budget
    check("a session that has spent its screenshot budget keeps no more", budgeted)

    # --- and they go when the session does -----------------------------------
    # Not a second retention mechanism: `discard_session` is the one an upload
    # already uses, and screenshots live inside the directory it removes. Driven
    # through the real DELETE so the wiring is what is being checked, in its own
    # session so the rest of this suite still has one.
    doomed = c.post(
        "/api/sessions",
        json={"title": "deleted later", "provider": "claude",
              "workspace_id": workspace.get("id")},
    )
    check("a session to delete exists", doomed.status_code == 201, doomed.text[:200])
    did = doomed.json()["id"]
    d_run = c.post(f"/api/sessions/{did}/prompt", json={"prompt": "photograph something"})
    check("with a turn under it", d_run.status_code == 202, d_run.text[:200])
    wait_idle(c, did)
    d_run_id = d_run.json()["id"]
    browsing.keep_shot(did, d_run_id, "screenshot-001.png", SHOT)
    r = c.get(f"/api/runs/{d_run_id}/screenshots/screenshot-001.png")
    check("its screenshot is there while the session is", r.status_code == 200,
          f"{r.status_code} {r.text[:120]}")
    d_shots = browsing.run_shots_dir(did, d_run_id)
    check("there are captures on disk to lose", files_under(d_shots) != [], str(d_shots))

    r = c.delete(f"/api/sessions/{did}")
    check("the session deletes", r.status_code in (200, 204), f"{r.status_code} {r.text[:160]}")
    check("deleting the session takes its screenshots with it",
          not d_shots.exists() and not store.session_dir(did).exists(), str(d_shots))
    r = c.get(f"/api/runs/{d_run_id}/screenshots/screenshot-001.png")
    check("and the endpoint stops serving them", r.status_code == 404, str(r.status_code))

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
        ("GET", f"/api/runs/{run_id}/screenshots/screenshot-001.png"),
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
