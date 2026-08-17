"""The application's half of the agent browser.

The browser itself runs on the other side of the isolation boundary — a
Playwright Chromium started by an MCP server that is a subprocess of the agent.
The MCP server is the agent's, at the agent's uid; the browser under it is not,
and runs as a third user in a group of its own so that a page which takes over
a renderer cannot read the run's decrypted SSH keys. What lives here is
everything neither of them is allowed to decide for itself:

* **what it may reach** — built from exactly the same rows the run's ssh config
  is built from, so a browser and an `ssh` in the same turn have identical
  reach and neither can be widened without widening the other;
* **how long its artefacts live** — the agent's scratch copies get one
  directory per run, deleted with the run like the per-run ssh materials; the
  screenshots the *operator* is shown are kept with the session instead, in the
  same place and with the same lifetime as an uploaded attachment;
* **the credential** — decrypted here, scoped to the person who asked for the
  turn, and handed to the browser to type rather than to the agent to read.

Nothing here is the authorisation. A browser request for an internal host is
authorised by `RelayTokens.allows()` when the stream is opened, the same call
and the same chokepoint an `ssh` connection passes through. The reach document
below only tells the browser *which node to ask about*, in the same way the
generated ssh config only tells ssh which ProxyCommand to run: get it wrong and
the gate refuses, which is why it can be handed to the agent's side at all.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import attachments, relay
from .config import settings
from .models import RelayNode, Target

log = logging.getLogger("aiops.browser")

#: Same reasoning as the per-run ssh directory: owned by the app, readable and
#: writable by the agent's group, closed to everyone else — which now includes
#: the browser user, and deliberately. Screenshots are written into it by
#: Playwright's client, a thread of the agent's MCP bridge, not by Chromium; the
#: browser hands back bytes and never opens a file here. So the directory is
#: written by the agent and read by the agent, and giving the browser user
#: access to it would only re-open a path nothing needs.
RUN_DIR_MODE = 0o770


def reach_document(
    targets: list[Target] | None = None,
    nodes: dict[int, RelayNode] | None = None,
    subnet_nodes: list[RelayNode] | None = None,
) -> dict:
    """What the browser is told about this run's network reach.

    Two kinds, exactly as `ssh_targets.prepare` materialises them and for the
    same reasons: exact (node, host, port) triples for systems an operator
    stored, and (node, network, ports) rules for subnets a node was opened to.

    A stored system's triple carries its **ssh** port, which is almost never the
    port its web interface listens on — so browsing an internal application is
    in practice a subnet rule, and the port has to be one the node allows. That
    is deliberate and is why the refusal text names the port and the node: an
    agent should not be able to widen a node's reach by asking nicely, and an
    operator should be told exactly what to add.

    `all_ports` is the operator's answer to having to enumerate them, and it
    travels here as its own key rather than as a value inside `ports`, so the
    browser reads it the way the gate does. It is only ever copied from the
    rule the gate itself was issued, so the document cannot claim it for a node
    that will not be granted it — and it still says nothing about *which
    addresses*, which is the half neither side may relax.
    """
    nodes = nodes or {}
    routes = [
        {
            "node": nodes[t.relay_node_id].slug,
            "host": t.hostname,
            "port": t.port,
        }
        for t in (targets or [])
        if t.relay_node_id and t.relay_node_id in nodes
    ]
    subnets = [
        # `all_ports` is copied straight off the same `SubnetRule` the gate was
        # issued, so the document cannot claim it for a node the gate will not
        # grant it for. It is written on every entry rather than only the true
        # ones: a key that is sometimes absent is a key a reader has to guess
        # the meaning of, and the guess this feature cannot afford is "true".
        {
            "node": rule.slug,
            "cidr": str(rule.network),
            "ports": list(rule.ports),
            "all_ports": bool(rule.all_ports),
        }
        for node in (subnet_nodes or [])
        for rule in relay.subnet_rules(node)
    ]
    #: Which stored systems `login` will accept a slug for. Never a secret and
    #: never a hostname the agent could not already see in its ssh briefing —
    #: only whether AIOps holds a password it could inject.
    systems = [
        {
            "slug": t.slug,
            "hostname": t.hostname,
            "username": t.username,
            "has_password": bool(t.password_enc),
        }
        for t in (targets or [])
        if t.password_enc
    ]
    return {"routes": routes, "subnets": subnets, "systems": systems}


def describe(reach: dict) -> str:
    """The paragraph the agent is given about its browser.

    Written in the same shape as `ssh_targets.describe`, and for the same
    reason: an agent that does not know a refusal is a policy limit reads it as
    a broken network and spends the turn retrying.
    """
    lines = [
        "You have a real browser (Chromium) available through the "
        "`mcp__aiops_browser__*` tools: `navigate`, `read_page`, `screenshot`, `click`, "
        "`fill` and `login`. It renders JavaScript, so use it rather than WebFetch for "
        "any application whose page is built in the browser.",
        "Reading a page and screenshotting it happen silently. Clicking, typing and "
        "signing in are put to the operator for approval when the session asks about "
        "tool calls, exactly as a shell command is.",
    ]

    subnets = reach.get("subnets") or []
    if subnets:
        listed = "\n".join(
            f"- {entry['cidr']} on "
            f"{relay.ports_phrase(entry['ports'], bool(entry.get('all_ports')))}"
            f" — via relay node {entry['node']}"
            for entry in subnets
        )
        lines.append(
            "Addresses on these networks can be browsed; the connection is routed through "
            "the relay node automatically:\n"
            f"{listed}\n"
            "Browse them by address (http://192.168.1.10:8989), not by name — a node's "
            "networks are matched by address. An address or a port outside these rules is "
            "refused by AIOps rather than attempted: that is a policy limit, not a network "
            "fault, and the refusal says which port to add to which node. Public addresses "
            "are never routed through a node, and private addresses are never dialled "
            "directly from this server."
        )
    else:
        lines.append(
            "No internal network is reachable in this turn, so the browser can reach public "
            "sites only. A private address is refused rather than dialled from this server."
        )

    systems = reach.get("systems") or []
    if systems:
        listed = ", ".join(f"`{s['slug']}` ({s['username']}@{s['hostname']})" for s in systems)
        lines.append(
            "AIOps holds a password for these stored systems and can type it into a login "
            f"form for you: {listed}. Call `login` with the system's short name and the "
            "selectors for the fields — the password is injected into the page by AIOps, is "
            "never given to you, and is filtered out of every page read and screenshot "
            "afterwards. Do not ask the operator for it and do not try to read it back."
        )
    return "\n\n".join(lines)


class BrowserGrant:
    """One run's browser context: its reach, and who is looking at it."""

    def __init__(self, reach: dict, requester_id: int | None, who: str, directory: str) -> None:
        self.reach = reach
        #: The person the turn was asked for — the same scope a stored
        #: credential is resolved against everywhere else in AIOps.
        self.requester_id = requester_id
        self.who = who
        self.directory = directory


class BrowserGrants:
    """What each live run's browser may do, in memory and dying with the run.

    Deliberately the same shape as `RelayTokens`: a snapshot taken when the turn
    starts, held nowhere but this process, and revoked in the same `finally`
    that removes the run's ssh materials. A browser cannot outlive its grant
    because the MCP server is a child of the agent process, which the runner
    reaps — but the grant is dropped anyway, so a stray process that survived a
    reap finds nothing to ask about.
    """

    def __init__(self) -> None:
        self._grants: dict[int, BrowserGrant] = {}

    def issue(
        self, run_id: int, reach: dict, requester_id: int | None, who: str, directory: str
    ) -> BrowserGrant:
        grant = BrowserGrant(reach, requester_id, who, directory)
        self._grants[run_id] = grant
        return grant

    def get(self, run_id: int) -> BrowserGrant | None:
        return self._grants.get(run_id)

    def revoke(self, run_id: int) -> None:
        grant = self._grants.pop(run_id, None)
        if grant is not None and grant.directory:
            # The agent's scratch copies, which exist so it can open its own
            # captures by path. They go with the run exactly like the decrypted
            # key the same run's ssh config used. What the operator is shown is
            # a different copy in a different place — see `keep_shot` — and is
            # not touched by this.
            shutil.rmtree(grant.directory, ignore_errors=True)


grants = BrowserGrants()


def make_run_dir(run_id: int) -> str:
    """A private directory this run's browser may write screenshots into.

    The agent's own copies, so that `screenshot` can hand back a path the agent
    can open. Nothing here is ever served: the copy the operator sees is the one
    `keep_shot` writes, out of the agent's reach entirely.
    """
    path = tempfile.mkdtemp(prefix=f"aiops-browser-{run_id}-")
    os.chmod(path, RUN_DIR_MODE)
    return path


#: The only names `Browser.screenshot` generates, and therefore the only names
#: that are stored or resolved. Written as an exact shape rather than as a
#: sanitiser: there is nothing here a person named, so anything that is not one
#: of ours is not a name to clean up but a request to refuse.
SHOT_NAME = re.compile(r"^screenshot-\d{3}\.png$")

#: What a PNG starts with. Checked because the stored bytes are later served
#: labelled `image/png`, and a label should not be the only thing making that
#: true. Cheap, and it also catches an empty or truncated capture before it
#: becomes a broken picture in somebody's transcript.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


#: Absent on Windows, where these suites are sometimes run while developing.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ShotRefused(Exception):
    """A capture AIOps will not keep, with the sentence saying why.

    Not an error the agent has to handle: the picture it took is still on disk
    in the run's own directory and still readable by it. Only the operator's
    copy is declined, and the tool result says so in as many words.
    """


# =====================================================================
# Where a screenshot lives, and for how long
# =====================================================================
# With the session, not with the run. A screenshot you can only see while the
# turn is still running is close to useless for reviewing what an agent did —
# reopening a finished conversation is exactly when somebody wants to look at
# what its browser saw.
#
# So the bytes go where an uploaded attachment goes: inside
# `attachments_root/<session id>/`, which is a named volume that survives a
# rebuild, and which `attachments.discard_session` removes when the session is
# deleted. That call already exists and already runs on session delete, so
# screenshots inherit the whole retention story rather than getting a second one
# that has to be kept in step with it.
#
# Two properties fall out of the directory choice and are worth stating:
#
# * an attachment's own directory is a uuid4, so the fixed name `screenshots`
#   can never collide with one;
# * the attachments volume is written by the *app* and is not in the agent's
#   group at all — unlike the run directory, which has to be agent-writable.
#   Nothing the agent can create is ever opened here.


def shots_root(session_id: str) -> Path:
    """Every screenshot kept for one session, across all of its turns."""
    return attachments.session_dir(session_id) / "screenshots"


def run_shots_dir(session_id: str, run_id: int) -> Path:
    """One turn's screenshots. Kept per run because the names restart at 001
    with every turn, and because that is how the transcript addresses them."""
    return shots_root(session_id) / str(int(run_id))


def stored_shot(session_id: str, run_id: int, name: str) -> str | None:
    """The file one of this turn's screenshots is in, or None.

    Ways to get None, and the caller says the same thing about all of them
    because they are the same fact from outside: the run never had a browser,
    the turn ran before AIOps kept screenshots at all, the capture was refused
    for size, or that is not a name this module writes.

    The resolve-and-compare is not redundant with the pattern above, and it is
    kept even though this root — unlike the run directory — is not writable by
    the agent. `resolve()` flattens `..` and follows links, so one comparison
    against the root covers a traversal, a link planted under an accepted name,
    and an absolute path smuggled in as `name` (`root / "/etc/passwd"` is
    "/etc/passwd" under pathlib, which is exactly what this rejects). It is the
    same guard `attachments.resolve_inside` applies to a workspace download, and
    it is the layer that has to hold if the storage ever moves somewhere an
    agent can write again.
    """
    if not SHOT_NAME.match(name or ""):
        return None
    try:
        root = run_shots_dir(session_id, run_id).resolve()
        candidate = (root / name).resolve()
    except OSError:
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return str(candidate)


def session_shot_bytes(session_id: str) -> int:
    """How much of the session's screenshot budget is already spent.

    Read off the disk rather than tracked in a counter: the directory is the
    only record there is, and a counter would be a second one to keep true
    across restarts, failed writes and a deleted session.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(shots_root(session_id), followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def keep_shot(session_id: str, run_id: int, name: str, data: bytes) -> int:
    """Store one capture for the session, or say why it will not be.

    `data` is the return value of the single masked `page.screenshot(...)` call
    that also wrote the agent's own copy — not a file read back off a path the
    agent chose. That is deliberate and is the strongest form of the guard
    above: there is no filesystem object between the mask and this write for
    anything to be substituted for.

    Three bounds, because the growth profile changed when the lifetime did:

    * `browser_max_screenshots` per turn — the original cap, re-checked here
      rather than trusted to the agent's own process, which sets it from an
      environment variable in a process tree the agent controls;
    * `browser_screenshot_max_bytes` for one capture;
    * `browser_session_screenshot_bytes` for everything one conversation keeps,
      which is the bound that actually replaces "deleted at the end of the run".

    Over budget refuses the new capture rather than evicting an old one: an
    operator scrolling back should not find that a picture they were shown an
    hour ago has been quietly deleted to make room for one they have not seen.
    """
    if not SHOT_NAME.match(name or ""):
        raise ShotRefused("that is not a name AIOps generates for a screenshot")
    if not data.startswith(PNG_MAGIC):
        raise ShotRefused("those bytes are not a PNG")
    if len(data) > settings.browser_screenshot_max_bytes:
        raise ShotRefused(
            f"the capture is {attachments.human_size(len(data))} and one screenshot may be "
            f"at most {attachments.human_size(settings.browser_screenshot_max_bytes)}"
        )

    root = run_shots_dir(session_id, run_id)
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    target = (resolved / name).resolve()
    if target.parent != resolved:
        raise ShotRefused("that name does not resolve inside this turn's screenshots")

    kept = sum(1 for _ in os.scandir(resolved))
    if kept >= settings.browser_max_screenshots:
        raise ShotRefused(
            f"this turn has already stored {settings.browser_max_screenshots} screenshots, "
            "which is the limit"
        )
    used = session_shot_bytes(session_id)
    if used + len(data) > settings.browser_session_screenshot_bytes:
        raise ShotRefused(
            "this conversation has reached the "
            f"{attachments.human_size(settings.browser_session_screenshot_bytes)} it may keep "
            "in screenshots; delete the conversation, or some of it, to free the space"
        )

    # O_EXCL so a second capture cannot replace one the operator has already
    # been shown, and O_NOFOLLOW so this never writes through a link even if
    # something one day can put one here. AIOps runs on Linux; the fallback is
    # for a developer's machine, where the flag does not exist and O_EXCL alone
    # already refuses an existing name of any kind.
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o640)
    except FileExistsError as exc:
        raise ShotRefused("a screenshot of that name is already stored for this turn") from exc
    except OSError as exc:
        raise ShotRefused(f"the capture could not be written ({exc.strerror})") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(target)
        raise
    return len(data)
