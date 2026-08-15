"""The relay-node installer, packaged for download.

The Nodes page prints a command per platform — `sudo ./install.sh …`,
`.\\install.ps1 …` — and until now there was no way to *get* those files onto
the machine being installed. On Linux you probably have the repository already;
on a fresh Windows box you have nothing, which is exactly where the instruction
mattered most. So the runtime image now carries `deploy/relay`, and this module
hands out the subset each platform needs as a zip.

**The enrolment token is deliberately not in the download.** It would be easy
to template it into a file and save the operator a paste, and it would be
wrong: the token is short-lived and single-use, it is the one credential in the
system nobody is told to look after, and a zip in a Downloads folder carrying
one is a secret nobody expected to have made — surviving the token's own
lifetime, copied wherever the folder is copied. The token belongs in the
command, where it is transient and visibly a secret. These bundles are
therefore identical for every caller and every node, which is also what makes
them cacheable and what makes "did the download leak it?" a question with a
permanent answer.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent

#: Where `deploy/relay` ended up. In the runtime image the Dockerfile copies it
#: to /app/relay, beside the `app` package; in a checkout it is still
#: deploy/relay at the repository root. Resolved once, at import, so a missing
#: copy in the image shows up as a clear 500 rather than a puzzling empty zip.
_SEARCH_PATHS = (
    _HERE.parent / "relay",  # /app/relay — the runtime image
    _HERE.parent.parent / "deploy" / "relay",  # a source checkout
)


def relay_dir() -> Path | None:
    for candidate in _SEARCH_PATHS:
        if (candidate / "aiops_relay_node.py").is_file():
            return candidate
    return None


#: What each platform actually needs, named explicitly rather than globbed.
#: A glob would have shipped `__pycache__` the first time anyone ran the agent
#: out of the source directory, and would quietly start shipping whatever else
#: lands there later.
#:
#: `aiops_relay_node.py` is in all three because it is the agent — the same one
#: file on every platform — and the README is in all three because a bundle
#: that explains itself is the difference between an installer and a mystery.
BUNDLES: dict[str, tuple[str, ...]] = {
    "linux": (
        "aiops_relay_node.py",
        "install.sh",
        "aiops-relay-node.service",
        "README.md",
    ),
    "windows": (
        "aiops_relay_node.py",
        "install.ps1",
        "uninstall.ps1",
        "README.md",
    ),
    "docker": (
        "aiops_relay_node.py",
        "Dockerfile",
        "docker-compose.yml",
        "README.md",
    ),
}

#: The scripts that have to arrive executable for the documented command to be
#: the documented command. Zip stores a mode; without one, `unzip` gives 0644
#: and `sudo ./install.sh` is "Permission denied".
_EXECUTABLE = {"install.sh", "aiops_relay_node.py"}

#: A fixed timestamp, so the same source produces the same bytes every time.
#: Nothing here varies per caller, and a download whose checksum changes on
#: every request cannot be checked against anything.
_MTIME = (2024, 1, 1, 0, 0, 0)


def bundle_name(platform: str) -> str:
    return f"aiops-relay-node-{platform}.zip"


def build_bundle(platform: str) -> bytes:
    """The zip for one platform, as bytes.

    Every member is copied with `read_bytes` and written with `writestr`, never
    through text mode. Both PowerShell scripts are UTF-8 *with* a BOM and
    ASCII-only on purpose: Windows PowerShell 5.1 reads a BOM-less `.ps1` as
    ANSI, and `uninstall.ps1` then fails to parse at all rather than failing
    visibly. Any decode/re-encode step in this function would strip or rewrite
    that BOM and break the thing this bundle exists to deliver, so there is
    none, and `test_installer.py` compares hashes through the round trip.
    """
    members = BUNDLES.get(platform)
    if members is None:
        raise KeyError(platform)

    source = relay_dir()
    if source is None:
        raise FileNotFoundError(
            "the relay installer files are missing from this deployment "
            f"(looked in: {', '.join(str(p) for p in _SEARCH_PATHS)})"
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in members:
            path = source / name
            if not path.is_file():
                raise FileNotFoundError(f"{name} is missing from {source}")
            info = zipfile.ZipInfo(name, date_time=_MTIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if name in _EXECUTABLE else 0o644
            info.external_attr = mode << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()
