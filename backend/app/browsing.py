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
* **how long its artefacts live** — one directory per run, deleted with the run
  like the per-run ssh materials;
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

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import relay
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
            # Screenshots can show a logged-in internal application. They exist
            # for the turn that took them and are not kept afterwards, exactly
            # like the decrypted key the same run's ssh config used.
            shutil.rmtree(grant.directory, ignore_errors=True)


grants = BrowserGrants()


def make_run_dir(run_id: int) -> str:
    """A private directory this run's browser may write screenshots into."""
    path = tempfile.mkdtemp(prefix=f"aiops-browser-{run_id}-")
    os.chmod(path, RUN_DIR_MODE)
    return path


#: The only names `Browser.screenshot` generates, and therefore the only names
#: the transcript will resolve. Written as an exact shape rather than as a
#: sanitiser: there is nothing here a person named, so anything that is not one
#: of ours is not a name to clean up but a request to refuse.
SHOT_NAME = re.compile(r"^screenshot-\d{3}\.png$")


def screenshot_path(run_id: int, name: str) -> str | None:
    """The file one of this run's screenshots is in, or None.

    Three ways to get None, and the caller says the same thing about all three
    because they are the same fact from outside: the run never had a browser,
    the run has ended and its directory went with it, or that is not a name this
    module writes.

    The resolve-and-compare at the end is not redundant with the pattern above.
    The run directory is group-writable by the agent — it has to be, because
    reading a screenshot by path is how the agent looks at one — so a symlink
    called `screenshot-001.png` pointing at a file only the app can read is
    something the agent could plant. `resolve()` follows it, and the comparison
    then puts it outside the root and refuses it.
    """
    grant = grants.get(run_id)
    if grant is None or not grant.directory:
        return None
    if not SHOT_NAME.match(name or ""):
        return None
    try:
        root = Path(grant.directory).resolve()
        candidate = (root / name).resolve()
    except OSError:
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return str(candidate)
