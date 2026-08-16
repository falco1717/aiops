"""Cache headers on the built frontend.

This suite exists because of a bug no other test could see. The app shell went
out with no `Cache-Control` and no `Expires`, which does not mean "do not
cache" — it means the browser may pick a freshness lifetime for itself (RFC
9111 §4.2.2, in practice a tenth of the age since `Last-Modified`). It did.
index.html is the only file that names the content-hashed bundle, so a visitor
who already had the page kept loading yesterday's JavaScript off disk for the
rest of the day, and three shipped features looked broken to the person who
shipped them. Nothing was wrong with the features and nothing failed in CI.

So the two properties are pinned here directly:

* everything that can return the **app shell** — `/`, a deep link, an unknown
  path — must carry a revalidate directive and a validator, and answer a
  conditional request with a 304 that still carries the directive;
* everything under **/assets** is content-addressed and must be immutable for
  a year, because a new build gives it a new name rather than new bytes.

It builds its own static directory and mounts it with the application's own
`mount_spa`, so it runs in a clean clone where `app/static` does not exist.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import (  # noqa: E402
    IMMUTABLE_CACHE_CONTROL,
    REVALIDATE_CACHE_CONTROL,
    mount_spa,
)

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# --- a stand-in for what `vite build` drops into app/static ----------------
root = Path(tempfile.mkdtemp(prefix="aiops-static-"))
outside = root.parent / (root.name + "-outside")
outside.mkdir()
(outside / "secret.txt").write_text("not servable")

(root / "assets").mkdir()
(root / "index.html").write_text(
    '<!doctype html><html><head><script type="module" '
    'src="/assets/index-AAAAAAAA.js"></script></head><body></body></html>'
)
(root / "assets" / "index-AAAAAAAA.js").write_text("console.log('bundle one')\n")
(root / "assets" / "index-BBBBBBBB.css").write_text(":root{color:red}\n")
(root / "mark.svg").write_text("<svg/>")

app = FastAPI()
mount_spa(app, root)
client = TestClient(app)

SHELL_PATHS = ["/", "/sessions/abcd-1234", "/nodes", "/some/deep/unknown/route"]

# --- the app shell ---------------------------------------------------------
r = client.get("/")
check("the document is served", r.status_code == 200, str(r.status_code))
check("the document carries an explicit cache directive",
      r.headers.get("cache-control") == REVALIDATE_CACHE_CONTROL,
      repr(r.headers.get("cache-control")))
check("which is a revalidate, not a store ban",
      "no-cache" in (r.headers.get("cache-control") or "")
      and "no-store" not in (r.headers.get("cache-control") or ""),
      repr(r.headers.get("cache-control")))
check("the document carries a validator so revalidation is cheap",
      bool(r.headers.get("etag")) and bool(r.headers.get("last-modified")),
      f"etag={r.headers.get('etag')!r} last-modified={r.headers.get('last-modified')!r}")
check("the document is html", "text/html" in r.headers.get("content-type", ""),
      r.headers.get("content-type", ""))

# The whole point of `no-cache` over `no-store`: the browser still asks, and
# the answer is usually 304 with no body.
cond = client.get("/", headers={"If-None-Match": r.headers["etag"]})
check("a conditional request for the document is answered 304",
      cond.status_code == 304, str(cond.status_code))
check("and the 304 repeats the cache directive",
      cond.headers.get("cache-control") == REVALIDATE_CACHE_CONTROL,
      repr(cond.headers.get("cache-control")))
check("and the 304 has no body", not cond.content, repr(cond.content[:40]))

stale = client.get("/", headers={"If-None-Match": '"something-else"'})
check("a stale validator gets the new document, not a 304",
      stale.status_code == 200 and b"index-AAAAAAAA.js" in stale.content,
      str(stale.status_code))

# --- every path that can return the shell, not just / ----------------------
# A deep link is how the owner actually opens the app. If /sessions/<id> were
# served by a different code path the bug would survive the fix.
for path in SHELL_PATHS:
    d = client.get(path)
    check(f"{path} returns the shell with the same headers",
          d.status_code == 200
          and d.content == r.content
          and d.headers.get("cache-control") == REVALIDATE_CACHE_CONTROL
          and bool(d.headers.get("etag")),
          f"{d.status_code} cc={d.headers.get('cache-control')!r}")

# --- content-hashed assets -------------------------------------------------
for name in ("index-AAAAAAAA.js", "index-BBBBBBBB.css"):
    a = client.get(f"/assets/{name}")
    cc = a.headers.get("cache-control") or ""
    check(f"/assets/{name} is served", a.status_code == 200, str(a.status_code))
    check(f"/assets/{name} is cacheable for a year and immutable",
          cc == IMMUTABLE_CACHE_CONTROL and "max-age=31536000" in cc and "immutable" in cc,
          repr(cc))
    check(f"/assets/{name} still carries a validator", bool(a.headers.get("etag")),
          repr(a.headers.get("etag")))

asset = client.get("/assets/index-AAAAAAAA.js")
asset_cond = client.get("/assets/index-AAAAAAAA.js",
                        headers={"If-None-Match": asset.headers["etag"]})
check("a conditional request for an asset is answered 304",
      asset_cond.status_code == 304, str(asset_cond.status_code))
check("and the asset 304 keeps the immutable directive",
      asset_cond.headers.get("cache-control") == IMMUTABLE_CACHE_CONTROL,
      repr(asset_cond.headers.get("cache-control")))

# A rebuild renames the bundle. The old name must 404 rather than fall through
# to the shell, or a stale reference would silently be answered with HTML.
missing = client.get("/assets/index-ZZZZZZZZ.js")
check("an asset name from an older build is a 404, not the shell",
      missing.status_code == 404, str(missing.status_code))

# --- files beside the shell that the build does not fingerprint ------------
svg = client.get("/mark.svg")
check("an un-fingerprinted file next to the shell revalidates too",
      svg.status_code == 200 and svg.headers.get("cache-control") == REVALIDATE_CACHE_CONTROL,
      f"{svg.status_code} {svg.headers.get('cache-control')!r}")

# --- unchanged behaviour ---------------------------------------------------
notfound = client.get("/api/nope")
check("an unknown /api path is still a JSON 404, not the shell",
      notfound.status_code == 404 and notfound.json() == {"detail": "Not Found"},
      str(notfound.status_code))

# Percent-encoded so httpx does not normalise the `..` away before it is sent:
# the route sees it decoded, which is the case the guard in `spa` is for.
leak = client.get(f"/%2e%2e/{outside.name}/secret.txt")
check("a traversal out of the static root does not serve the file",
      b"not servable" not in leak.content, str(leak.status_code))
for probe in ("/%2e%2e/%2e%2e/etc/passwd", "/%2e%2e%2fetc%2fpasswd", "/etc/passwd"):
    p = client.get(probe)
    check(f"{probe} leaks nothing",
          b"not servable" not in p.content and b"root:" not in p.content,
          str(p.status_code))

shutil.rmtree(root, ignore_errors=True)
shutil.rmtree(outside, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
