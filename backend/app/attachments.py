"""Files that move between the operator and the agent, in both directions.

Two separate jobs live here because they share one adversary — a path chosen by
somebody else:

* an *upload* names its own file, so the name is treated as hostile text and is
  never used to decide where anything is written;
* a *download* names an existing file, so the path is resolved and required to
  stay inside a permitted root before anything is opened.

Nothing below trusts a client-supplied Content-Type either. What a file is
served as is derived from its extension, and only from a list of types that a
browser will not execute.
"""
from __future__ import annotations

import mimetypes
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, status

from .config import settings

#: Names Windows refuses to create, with or without an extension. AIOps runs on
#: Linux, but these files travel — onto an operator's laptop, into a zip, onto a
#: share — and a `NUL` in a directory listing is a trap for whoever gets it next.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: Characters that are legal on Linux but break, or mean something, elsewhere.
_HOSTILE_CHARS = '<>:"|?*'

#: Types a download may be labelled with. Anything else is served as an opaque
#: byte stream: user content lives on the same origin as the session cookie, so
#: labelling it text/html or image/svg+xml would be stored XSS with extra steps.
_SERVABLE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/avif",
    "image/tiff",
    "application/pdf",
    "application/json",
    "application/zip",
    "text/plain",
    "text/csv",
    "text/markdown",
}

#: Sent with every response that carries bytes somebody else chose. The
#: Content-Type is already restricted to types a browser will not execute (see
#: `download_type`); this stops it guessing a different one anyway.
#:
#: Lives here rather than in one router because more than one endpoint serves
#: those bytes now — an upload, a file the agent wrote, and a screenshot its
#: browser took — and the header set is a property of *serving user content*,
#: not of any one of them.
DOWNLOAD_HEADERS = {"X-Content-Type-Options": "nosniff"}

#: Directories the files panel never walks into. A workspace is usually a git
#: repo with its dependencies installed; none of that is agent output.
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


class AttachmentTooLarge(Exception):
    pass


def safe_filename(raw: str | None) -> str:
    """Reduce a client-supplied filename to a single harmless path component.

    Callers must still not build a path out of the result alone — every upload
    gets its own generated directory — but this is the layer that has to hold if
    that ever changes, so it assumes the input is an attack.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1]
    # Drops NUL, newlines, and the bidirectional overrides that let "annexe.txt"
    # arrive on screen as something ending in ".exe".
    name = "".join(ch for ch in name if ch.isprintable() and ch not in _HOSTILE_CHARS)
    name = name.strip()
    # "..", "." and "...." all collapse to nothing here, which is the point.
    name = name.lstrip(".").rstrip(". ")
    if not name:
        return "upload"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name[:120]


def download_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed if guessed in _SERVABLE_TYPES else "application/octet-stream"


def session_dir(session_id: str) -> Path:
    return Path(settings.attachments_root) / session_id


def stored_path(session_id: str, attachment_id: str, filename: str) -> Path:
    return session_dir(session_id) / attachment_id / filename


async def save_upload(session_id: str, attachment_id: str, filename: str, upload) -> int:
    """Stream an upload to disk, refusing it the moment it exceeds the cap.

    Checking the declared length instead would trust a header, and checking
    after the read would mean the disk had already taken the hit.
    """
    target = stored_path(session_id, attachment_id, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with open(target, "wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_attachment_bytes:
                    raise AttachmentTooLarge
                fh.write(chunk)
    except BaseException:
        shutil.rmtree(target.parent, ignore_errors=True)
        raise
    return total


def discard(session_id: str, attachment_id: str) -> None:
    shutil.rmtree(session_dir(session_id) / attachment_id, ignore_errors=True)


def discard_session(session_id: str) -> None:
    """Deleting a session takes its uploads with it — the rows cascade away, and
    orphaned bytes nothing can name any more are just a slow leak."""
    shutil.rmtree(session_dir(session_id), ignore_errors=True)


def prompt_suffix(rows) -> str:
    """What gets appended to a prompt so the agent knows the files are there.

    Both CLIs can read a path off disk, and Claude reads images that way too, so
    telling the agent where they are is the whole mechanism — there is no upload
    channel into the model to use instead.
    """
    if not rows:
        return ""
    lines = [
        "",
        "--- Files attached by the user ---",
        "The user attached the following file(s) to the message above. They are "
        "on this machine at the paths listed; read them from disk (images "
        "included) rather than asking for their contents.",
    ]
    for row in rows:
        path = stored_path(row.session_id, row.id, row.filename)
        lines.append(f"- {row.filename} ({row.content_type}, {human_size(row.size)}): {path}")
    return "\n".join(lines)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@dataclass
class ListedFile:
    path: str
    size: int
    modified: float


def resolve_inside(root: Path, relative: str) -> Path:
    """The permitted path for `relative`, or a 400.

    `resolve()` both flattens `..` and follows symlinks, so one comparison
    against the root covers a traversal and a link planted to point out of it.
    An absolute `relative` is covered too: `root / "/etc/passwd"` is
    "/etc/passwd" under pathlib, which is exactly what this rejects.
    """
    try:
        candidate = (root / relative).resolve()
    except OSError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unreadable path") from exc
    if not candidate.is_relative_to(root):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path is outside this session's files")
    return candidate


def walk(root: Path) -> tuple[list[ListedFile], bool]:
    """Files under `root`, bounded on count and depth.

    Returns the files and whether the limit stopped the walk early. A workspace
    is frequently a repo with a build tree in it, so an unbounded walk would be
    both slow and useless; the caller shows the operator what the rule was.
    """
    limit = settings.session_files_max
    max_depth = settings.session_files_max_depth
    found: list[ListedFile] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        depth = 0 if rel_dir == Path(".") else len(rel_dir.parts)
        dirnames[:] = (
            []
            if depth >= max_depth
            else sorted(d for d in dirnames if d not in _SKIP_DIRS)
        )
        for name in sorted(filenames):
            if len(found) >= limit:
                return _newest_first(found), True
            entry = Path(dirpath) / name
            if entry.is_symlink() and not _links_inside(entry, root):
                continue
            try:
                info = entry.stat()
            except OSError:
                continue  # broken link, or vanished mid-walk
            if not stat.S_ISREG(info.st_mode):
                continue
            found.append(
                ListedFile(
                    path=str(entry.relative_to(root)).replace(os.sep, "/"),
                    size=info.st_size,
                    modified=info.st_mtime,
                )
            )
    return _newest_first(found), truncated


def _links_inside(entry: Path, root: Path) -> bool:
    try:
        return entry.resolve().is_relative_to(root)
    except OSError:
        return False


def _newest_first(found: list[ListedFile]) -> list[ListedFile]:
    """What the agent just wrote is what the operator came for."""
    return sorted(found, key=lambda f: f.modified, reverse=True)
