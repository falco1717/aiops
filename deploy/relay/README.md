# AIOps relay nodes

A relay node is a machine on another network that AIOps opens connections
*through*. It is a jump point and nothing else.

The agents keep running on the AIOps server. Nothing about a node is a place
work happens: it holds one outbound connection open, and when AIOps asks, it
opens a TCP connection on its own network and copies bytes. It never receives a
provider login, an SSH key, a prompt, or anything an agent said — the SSH
session is end-to-end between the AIOps container and the far host, so what
crosses the node is ciphertext it holds no key for.

```
    AIOps server                                    the other network
 ┌───────────────────────────┐                   ┌─────────────────────┐
 │ claude / codex            │                   │                     │
 │   └ ssh <name>            │                   │   relay node        │
 │       └ ProxyCommand ─────┼── forwarder ──┐   │     └ TCP ──► host  │
 └───────────────────────────┘  (127.0.0.1)  │   └─────────▲───────────┘
                                             └── websocket ┘
                                              (dialled out by the node)
```

The node dials out and holds the connection, so there is no inbound firewall
rule to open and it works behind NAT.

## Installing one

1. In AIOps, go to **Relay nodes** and register one. You get a one-time
   enrolment token, readable in that response and never again.
2. Run the installer for the platform, with that token.
3. Back in AIOps, **approve** it. Until an administrator does, the node is
   refused at the socket — it is not merely hidden from the UI.
4. On a stored system under **Systems**, set *Reach it via* to the node.

### Linux (systemd)

```sh
sudo ./install.sh --url https://aiops.example.com --token <enrolment token>
```

Creates a system account `aiops-relay` with no shell, installs the agent at
`/opt/aiops-relay`, its credential at `/var/lib/aiops-relay/credential` (mode
0600), and the unit `aiops-relay-node.service`. The unit is
`aiops-relay-node.service` in this directory with the paths filled in, so what
is reviewed here is what is installed.

```sh
systemctl status aiops-relay-node
journalctl -u aiops-relay-node -f     # every connection it is asked to open
sudo aiops-relay-uninstall            # removes all of it, including the account
```

### Windows

```powershell
.\install.ps1 -Url https://aiops.example.com -Token <enrolment token>
```

Needs Python 3 (`winget install --id Python.Python.3.12 --scope machine`). The
agent is the same one file as on Linux and in Docker: a second implementation
in PowerShell would be a second thing to get right, and only one of them could
be covered by the test suite. Windows will not run a script as a service, so
the installer compiles a small service host with the C# compiler that ships
with the .NET Framework — nothing is downloaded — and that host runs the agent
and restarts it if it stops.

The service runs as the virtual account `NT SERVICE\AIOpsRelayNode`, which has
no password and no rights beyond its own state directory.

```powershell
Get-Service AIOpsRelayNode
Get-Content "$env:ProgramFiles\AIOps Relay Node\aiops-relay.log" -Wait
.\uninstall.ps1                       # add -KeepLog to keep the record
```

### Docker

```sh
AIOPS_RELAY_URL=https://aiops.example.com \
AIOPS_RELAY_TOKEN=<enrolment token> \
  docker compose up -d --build
```

`network_mode: host` is set deliberately: the point of a node is the network it
sits on, and a bridge network is a different one.

```sh
docker logs -f aiops-relay-node
docker compose down -v                # -v matters: it drops the credential
```

## Security

**Enrolment** is a one-time token, stored hashed, cleared the moment it is
spent. A replay finds nothing to match, and an expired token is refused with
the same message as an invalid one. Attempts are rate-limited on the same
throttle as the sign-in form.

**Every reconnect authenticates.** Enrolment returns a long-lived credential,
also stored hashed, and the node presents it on the control channel *and* on
each individual proxied connection. Nothing is trusted because it was trusted
before.

**Approval is separate from access.** A node carries no traffic until an
administrator approves it. Approving one does not give that administrator the
right to route through it — a node is a way into somebody's network, so it is
owned and shared exactly like a stored credential, and administrators get no
implicit access to either.

**Revocation is immediate.** The live connection is closed within the
heartbeat, the credential stops authorising anything, and the agent is told
*why*, so it stops rather than reconnecting forever. Anything routed through it
then fails — it never silently falls back to connecting directly.

**A node is told the minimum.** For each connection it gets a host and a port
and nothing else. AIOps will not ask for an address that was not already
materialised for that run from a system its owner may reach, so a compromised
agent cannot turn a node into a port scanner.

## Deliberately not stealthy

This was ruled out at design time. The node runs under its own named account as
a named service, logs every address it is asked to connect to, keeps its
credential in one file, and is removed by one command. There is no persistence
beyond the service, no process hiding, no anti-forensics, and nothing touches
security tooling on the machine. If you want to know what a node has been
doing, read its journal; if you want it gone, run the uninstaller.

## The agent

`aiops_relay_node.py` is stdlib-only, including its websocket client. A relay
has to install cleanly on whatever is already on a machine, and a dependency is
something that must be resolved over a network — possibly the very one AIOps
cannot reach yet.

```
--url URL           where AIOps is
--token TOKEN       one-time enrolment token (only needed once)
--state-dir DIR     where the credential lives (default /var/lib/aiops-relay)
--insecure          skip TLS verification; for a self-signed AIOps only
--enrol-only        exchange the token for a credential and exit
```

Each also reads from the environment: `AIOPS_RELAY_URL`, `AIOPS_RELAY_TOKEN`,
`AIOPS_RELAY_STATE_DIR`, `AIOPS_RELAY_INSECURE`.
