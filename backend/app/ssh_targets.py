from __future__ import annotations

import ipaddress
import logging
import os
import shlex
import shutil
import sys
import tempfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .agent_env import helper_script
from .config import settings
from .crypto import SecretUnavailable, decrypt
from .models import RelayNode, Target, User
from .access import level_for
from . import relay

log = logging.getLogger("aiops.ssh")

#: The ProxyCommand helper, referenced where it is installed rather than copied
#: into each run directory — it holds no secret, so there is nothing per-run
#: about it. `ssh` runs it as the agent user, which cannot read the application
#: source, so in the image this resolves to the copy installed for that side of
#: the boundary rather than to the one in this package.
PROXY_HELPER = helper_script(
    "relay_connect.py", os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay_connect.py")
)

#: How a run's credentials are exposed. The agent runs as a different user in
#: the same group as the app, so "the agent and nobody else" is the group bit
#: with the world bits clear. Nothing here is group-writable: the agent reads
#: its config and keys, it does not get to edit them into reaching a system it
#: was not given.
#:
#: 0640 on a private key is below what ssh normally tolerates, but ssh only
#: applies that rule to a key owned by the user running it (sshkey_perm_ok:
#: "if the key owned by a different user, then we don't care"). These are owned
#: by the app, read by the agent, so the check does not fire — verified end to
#: end against a real host, because it is the kind of thing that changes.
RUN_DIR_MODE = 0o750
RUN_FILE_MODE = 0o640
RUN_EXEC_MODE = 0o750
#: For the one file the agent has to write back to rather than only read.
RUN_SHARED_MODE = 0o660

#: How many Host patterns one subnet may spend before the config stops trying
#: to be exact and widens to the enclosing octet boundary. ssh matches Host
#: patterns linearly and a config nobody can read is its own kind of hazard;
#: /25 would need 128 addresses and /17 would need 128 globs.
#:
#: Widening is safe *only* because the config is not the boundary — see
#: `subnet_patterns` and the gate in relay.py, which compares the real CIDR.
MAX_HOST_PATTERNS = 32


class SshContext:
    """Per-run SSH materials: a config, its keys, and shims that use them.

    Credentials are written to a private directory for the life of one run and
    removed afterwards, rather than living permanently in the container's home
    volume. They cannot be kept out of the filesystem entirely — ssh has to
    read a key from somewhere, and an ssh-agent would let the same process sign
    with it anyway — so the protection that actually matters is lifetime and
    file mode, not indirection.

    `ssh` is reached through a shim on PATH so the agent can type `ssh <slug>`
    with no flags, exactly as it would on a workstation.
    """

    def __init__(
        self, root: str, env: dict[str, str], names: list[str], relay_token: str | None = None
    ):
        self.root = root
        self.env = env
        self.names = names
        #: What this run may ask the relay forwarder for. Revoked with the rest.
        self.relay_token = relay_token

    def cleanup(self) -> None:
        relay.tokens.revoke(self.relay_token)
        shutil.rmtree(self.root, ignore_errors=True)


def _write_private(path: str, content: str) -> None:
    """Write a secret at its final permissions, before any content lands.

    The mode is applied by open() rather than a chmod afterwards, so there is no
    instant at which the file exists with the process umask's idea of who may
    read it.
    """
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, RUN_FILE_MODE)
    with os.fdopen(handle, "w") as fh:
        fh.write(content if content.endswith("\n") else content + "\n")
    # O_CREAT's mode is masked by the umask; these files have exactly one
    # audience and it is not negotiable.
    os.chmod(path, RUN_FILE_MODE)


async def visible_targets(db: AsyncSession, user: User | None) -> list[Target]:
    """Systems this user may reach.

    A session with no owner gets none: credentials are private to the person
    who stored them, so an unattributable run must not inherit anybody's. That
    is deliberately stricter than failing open, which would hand every stored
    system to any turn whose owner had been deleted.
    """
    if user is None:
        return []
    rows = list(await db.scalars(select(Target).order_by(Target.name)))
    return [t for t in rows if level_for(t, user) is not None]


def subnet_patterns(network: ipaddress.IPv4Network) -> list[str]:
    """ssh_config Host globs covering a CIDR.

    ssh matches a Host line with shell globs, which cannot express a prefix
    length: `198.51.100.0/25` has no glob spelling at all. Three cases, and the
    honest thing is to say which one each range falls into:

    * A range that is octet-aligned (/24, /16) *is* a glob — `198.51.100.*` and
      `192.168.*` match exactly its members and nothing else.
    * A range that is not can still be covered exactly by a *set* of them: the
      /24s inside a /20, or the individual addresses inside a /27. That is
      preferred while the set stays small.
    * Beyond that the set is collapsed to the enclosing octet boundary, which
      is **wider than the range** — a /25 is written as the whole /24 around
      it, so `198.51.100.200` matches a Host pattern for a node allowed only
      `198.51.100.0/25`.

    That last case is deliberate and safe for exactly one reason: the pattern
    decides only whether ssh *tries* the relay, and the try lands on the gate
    in relay.py, which holds the real network and refuses. A glob is a routing
    hint. It is never permission.
    """
    if network.prefixlen >= 24:
        addresses = list(network)
        if len(addresses) <= MAX_HOST_PATTERNS:
            return [str(a) for a in addresses]
    else:
        blocks = list(network.subnets(new_prefix=24))
        if len(blocks) <= MAX_HOST_PATTERNS:
            return [str(b.network_address).rsplit(".", 1)[0] + ".*" for b in blocks]
    # Widen to the octet boundary at or above this prefix: /25..31 -> the /24,
    # /17..23 -> the /16. Never below /16, which validation already refuses.
    octets = network.prefixlen // 8
    covering = network.supernet(new_prefix=octets * 8)
    parts = str(covering.network_address).split(".")
    return [".".join(parts[:octets]) + ".*"] if octets < 4 else [str(covering.network_address)]


def prepare(
    targets: list[Target],
    nodes: dict[int, RelayNode] | None = None,
    subnet_nodes: list[RelayNode] | None = None,
    *,
    who: str = "",
) -> SshContext | None:
    """Materialise an SSH config for one run. None when there is nothing to do.

    `nodes` are the relay nodes the given systems are bound to. A system bound
    to one is never written as a direct connection, whatever state the node is
    in: if the route cannot be made to work the connection has to fail, because
    an ssh config that quietly reaches the host another way is exactly the
    outcome someone bound it to a node to prevent.

    `subnet_nodes` are nodes the requester may route through that have been
    given an explicit CIDR, so `ssh user@198.51.100.42` works for a host nobody
    stored. They carry no credential — reachability and authentication are
    different grants, and spraying a stored key at every address on a subnet
    would be the second one nobody asked for.

    `who` names the requester and turn, for the relay's own log lines.
    """
    subnet_nodes = subnet_nodes or []
    if not targets and not subnet_nodes:
        return None
    nodes = nodes or {}

    root = tempfile.mkdtemp(prefix="aiops-ssh-")
    os.chmod(root, RUN_DIR_MODE)
    keys_dir = os.path.join(root, "keys")
    bin_dir = os.path.join(root, "bin")
    os.makedirs(keys_dir)
    os.makedirs(bin_dir)
    os.chmod(keys_dir, RUN_DIR_MODE)
    os.chmod(bin_dir, RUN_DIR_MODE)

    known_hosts = os.path.join(root, "known_hosts")
    known_lines = [t.known_host_key.strip() for t in targets if t.known_host_key]
    _write_private(known_hosts, "\n".join(known_lines) if known_lines else "")
    # Two files, because "the agent may record what it learns" and "the agent
    # may not edit what the operator pinned" are both required and only the
    # second survives one file. ssh writes a learned key to the first file it
    # was given, so the writable one goes first — and it is only offered to
    # accept-new hosts. A strict host is verified against the pinned file
    # alone, which the agent cannot write, so it cannot forge its way past
    # StrictHostKeyChecking by inventing an entry for a host it is dialling.
    learned_hosts = os.path.join(root, "known_hosts.learned")
    _write_private(learned_hosts, "")
    os.chmod(learned_hosts, RUN_SHARED_MODE)

    lines: list[str] = [
        "# Generated by AIOps for one run. Do not edit — it is deleted afterwards.",
        "",
    ]
    passwords: dict[str, str] = {}
    askpass: dict[str, str] = {}
    usable: list[str] = []
    routes: set[tuple[str, str, int]] = set()

    for target in targets:
        try:
            key = decrypt(target.private_key_enc)
            password = decrypt(target.password_enc)
            passphrase = decrypt(target.passphrase_enc)
        except SecretUnavailable as exc:
            # One unreadable credential must not take the whole run down.
            log.warning("target %s skipped: %s", target.slug, exc)
            continue

        lines += [
            f"Host {target.slug}",
            f"    HostName {target.hostname}",
            f"    User {target.username}",
            f"    Port {target.port}",
            f"    UserKnownHostsFile {known_hosts}"
            if target.host_key_policy == "strict"
            else f"    UserKnownHostsFile {learned_hosts} {known_hosts}",
            f"    StrictHostKeyChecking {'yes' if target.host_key_policy == 'strict' else 'accept-new'}",
        ]

        if target.relay_node_id:
            node = nodes.get(target.relay_node_id)
            # "-" is the helper's own signal for a binding that no longer
            # resolves. It refuses rather than falling back to a direct dial.
            slug = node.slug if node is not None else "-"
            if node is None:
                log.warning(
                    "target %s is bound to relay node %s, which no longer exists",
                    target.slug,
                    target.relay_node_id,
                )
            else:
                routes.add((slug, target.hostname, target.port))
            lines += [
                f"    # Reached through relay node {slug}, not from this server.",
                f"    ProxyCommand {shlex.quote(sys.executable)} {shlex.quote(PROXY_HELPER)} "
                f"{shlex.quote(slug)} %h %p",
            ]

        if target.auth_type == "key" and key:
            key_path = os.path.join(keys_dir, target.slug)
            _write_private(key_path, key)
            lines += [
                f"    IdentityFile {key_path}",
                # Without this ssh offers every key it can find, which on a host
                # that rate-limits auth attempts locks the account out.
                "    IdentitiesOnly yes",
            ]
            if passphrase:
                # ssh will not take a passphrase on stdin, so an encrypted key
                # was being stored, dutifully re-encrypted on rotation, and then
                # silently failing to authenticate. SSH_ASKPASS is the documented
                # way in; REQUIRE=force makes ssh use it whether or not the run
                # happens to have a terminal.
                helper = os.path.join(bin_dir, f"askpass-{target.slug}")
                _write_askpass(helper, passphrase)
                askpass[target.slug] = helper
        elif target.auth_type == "password" and password:
            passwords[target.slug] = password
            lines += ["    PubkeyAuthentication no", "    PreferredAuthentications password"]
        else:
            log.warning("target %s has no usable credential for %s auth", target.slug, target.auth_type)
            lines += ["    # No credential stored — this host will prompt and fail."]

        lines.append("")
        usable.append(target.slug)

    # Subnets, after every stored system: ssh takes the first value it is given
    # for a keyword, so a host that is both stored and inside an allowed range
    # keeps the credential and port its operator saved for it.
    subnets: list[relay.SubnetRule] = []
    for node in subnet_nodes:
        rules = relay.subnet_rules(node)
        subnets += rules
        for rule in rules:
            network, ports = rule.network, rule.ports
            patterns = subnet_patterns(network)
            lines += [
                # The Host globs are built from the CIDR and have never
                # mentioned a port — ssh will try any port against a matching
                # pattern either way, and the gate is what decides. So all-ports
                # changes nothing here but this comment, which is the only place
                # the config says what the node was actually opened to.
                f"# {network} reached through relay node {node.slug}"
                + (f" ({relay.ports_phrase(ports, rule.all_ports)})" if ports else ""),
                "# No credential is stored for these hosts — supply a user and a key.",
                # The patterns can be wider than the range; the relay refuses
                # anything outside it. See subnet_patterns().
                "Host " + " ".join(patterns),
                f"    UserKnownHostsFile {learned_hosts} {known_hosts}",
                "    StrictHostKeyChecking accept-new",
                f"    ProxyCommand {shlex.quote(sys.executable)} {shlex.quote(PROXY_HELPER)} "
                f"{shlex.quote(node.slug)} %h %p",
                "",
            ]

    config = os.path.join(root, "config")
    _write_private(config, "\n".join(lines))

    env: dict[str, str] = {}
    _write_shim(bin_dir, "ssh", config, passwords)
    _write_shim(bin_dir, "scp", config, passwords)
    _write_shim(bin_dir, "sftp", config, passwords)
    env["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    env["AIOPS_SSH_CONFIG"] = config

    for slug, password in passwords.items():
        # sshpass reads one password from the environment; each shim picks the
        # variable matching the host it was asked for.
        env[f"AIOPS_SSHPASS_{_env_key(slug)}"] = password

    for slug, helper in askpass.items():
        # Picked up by the shim, which exports SSH_ASKPASS only for the host it
        # was asked for — one env var per target keeps a passphrase away from
        # every other connection in the same run.
        env[f"AIOPS_ASKPASS_{_env_key(slug)}"] = helper

    relay_token: str | None = None
    if routes or subnets:
        # In the environment rather than on the ProxyCommand line, for the same
        # reason as the passwords above: an argument list is readable by anyone
        # who can run `ps`. The token only ever authorises the connections
        # already materialised for this run, and dies with it.
        relay_token = relay.tokens.issue(routes, subnets, who)
        env["AIOPS_RELAY_TOKEN"] = relay_token
        env["AIOPS_RELAY_ADDR"] = (
            f"{settings.relay_forwarder_host}:{relay.hub.forwarder_port or 0}"
        )

    return SshContext(root, env, usable, relay_token)


def _env_key(slug: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in slug).upper()


def _write_askpass(path: str, passphrase: str) -> None:
    """A one-line program whose only job is to print one key's passphrase.

    Executable rather than readable data because that is the only shape ssh
    accepts, and per-target because SSH_ASKPASS is a single variable — one
    shared helper would hand every passphrase to every connection.
    """
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, RUN_EXEC_MODE)
    with os.fdopen(handle, "w", newline="\n") as fh:
        # Single-quoted with the shell's own escaping, so a passphrase
        # containing quotes or backslashes cannot end the string early.
        fh.write(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(passphrase)}\n")
    os.chmod(path, RUN_EXEC_MODE)


def _write_shim(bin_dir: str, command: str, config: str, passwords: dict[str, str]) -> None:
    """A tiny wrapper so `ssh <slug>` needs no flags.

    For password targets it hands the password to sshpass through the
    environment, which keeps it off the process's argument list where anyone
    running `ps` would see it.
    """
    real = shutil.which(command) or f"/usr/bin/{command}"
    script = f"""#!/bin/sh
# Generated by AIOps. Routes {command} through this run's target config.
CONFIG="{config}"
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    *)
      host=${{arg##*@}}
      host=${{host%%:*}}
      key=$(printf '%s' "$host" | tr -c 'a-zA-Z0-9' '_' | tr 'a-z' 'A-Z')
      eval "pw=\\${{AIOPS_SSHPASS_$key:-}}"
      if [ -n "$pw" ]; then
        SSHPASS="$pw" exec sshpass -e "{real}" -F "$CONFIG" "$@"
      fi
      eval "ap=\\${{AIOPS_ASKPASS_$key:-}}"
      if [ -n "$ap" ]; then
        # DISPLAY is set because ssh builds before 8.4 only consult SSH_ASKPASS
        # when they think a GUI exists; REQUIRE covers the builds that do not.
        SSH_ASKPASS="$ap" SSH_ASKPASS_REQUIRE=force DISPLAY="${{DISPLAY:-:0}}" \\
          exec "{real}" -F "$CONFIG" "$@"
      fi
      break
      ;;
  esac
done
exec "{real}" -F "$CONFIG" "$@"
"""
    path = os.path.join(bin_dir, command)
    with open(path, "w", newline="\n") as fh:
        fh.write(script)
    os.chmod(path, RUN_EXEC_MODE)


def describe(
    targets: list[Target],
    nodes: dict[int, RelayNode] | None = None,
    subnet_nodes: list[RelayNode] | None = None,
) -> str:
    """A line for the system prompt, so the agent knows what it can reach.

    A relayed system is named as such: the agent connects to it the same way,
    but knowing the hop exists is what lets it read "the relay node is not
    connected" as an infrastructure problem rather than a broken credential.

    Subnets are described separately and in different words, because they are a
    different offer. A stored system comes with its credential; a subnet is
    reachability only, and an agent told otherwise would spend a turn hunting
    for a password AIOps was never given.
    """
    nodes = nodes or {}
    subnet_nodes = subnet_nodes or []
    blocks: list[str] = []

    if targets:
        listed = "\n".join(
            f"- `{t.slug}` — {t.username}@{t.hostname}:{t.port}"
            + (f" ({t.description.strip()})" if t.description else "")
            + (
                f" [via relay node {nodes[t.relay_node_id].name}]"
                if t.relay_node_id and t.relay_node_id in nodes
                else ""
            )
            for t in targets
        )
        blocks.append(
            "Systems configured in AIOps that you can reach directly. Credentials are "
            "already in place, so use the short name and do not ask for one:\n"
            f"{listed}\n"
            "Connect with `ssh <name>` (scp and sftp work the same way)."
        )

    ranges = [
        (rule, node) for node in subnet_nodes for rule in relay.subnet_rules(node)
    ]
    if ranges:
        listed = "\n".join(
            f"- {rule.network} on {relay.ports_phrase(rule.ports, rule.all_ports)}"
            f" — via relay node {node.name}"
            for rule, node in ranges
        )
        blocks.append(
            "Networks reachable through a relay node. Any address in these ranges can be "
            "connected to by writing it out — `ssh user@192.168.1.10` — and the connection "
            "is routed through the node automatically:\n"
            f"{listed}\n"
            "No credential is stored for these, unlike the named systems above: give a "
            "username and a key or password, or ask the operator for one. Addresses "
            "outside these ranges, and other ports, are refused by AIOps rather than "
            "attempted — that refusal is a policy limit, not a network fault."
        )

    return "\n\n".join(blocks)
