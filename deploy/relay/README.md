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

Add `--proxy` and `--ca-bundle` if this network needs them; see *Getting out of
a corporate network* below.

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
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Url https://aiops.example.com -Token <enrolment token>
```

Run it from an elevated PowerShell, inside the folder you unzipped. The
`-ExecutionPolicy Bypass` is not decoration: Windows client SKUs block every
script by default, and a zip fetched with a browser marks each file it contains
as internet-sourced (Mark-of-the-Web), which makes even a RemoteSigned machine
reject the script as unsigned — that is the `is not digitally signed` error.
`Bypass` clears both and is scoped to the one process, so it changes nothing on
the machine. If it still refuses, the policy is coming from Group Policy: check
`Get-ExecutionPolicy -List` for a `MachinePolicy` or `UserPolicy` entry.

Needs Python 3, installed **for the whole machine**:

```powershell
winget install --id Python.Python.3.12 --scope machine
```

`--scope machine` is not a preference. A per-user Python — anything under
`C:\Users\<name>\`, which covers the Microsoft Store app execution aliases, the
Python install manager's runtimes in `%LocalAppData%\Python\pythoncore-*`, and
a plain per-user installer — cannot be used by a service. Measured on a real
node: with the interpreter at
`%LocalAppData%\Python\pythoncore-3.14-64\python.exe` the service reported
*Running* and the agent process died instantly with exit code `-1` and not one
line of output; repointed at `C:\Program Files\Python312\python.exe`, with
nothing else changed, it connected within a second. The installer now refuses a
per-user interpreter rather than writing it into the service's configuration,
and says so with the command above.

The agent is the same one file as on Linux and in Docker: a second
implementation in PowerShell would be a second thing to get right, and only one
of them could be covered by the test suite. Windows will not run a script as a
service, so the installer compiles a small service host with the C# compiler
that ships with the .NET Framework — nothing is downloaded — and that host runs
the agent and restarts it if it stops.

**Re-running the installer repairs a node.** It re-resolves the interpreter and
rewrites the service's configuration, and it leaves the state directory alone,
so the node keeps the credential it already has and needs no new enrolment
token:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Url https://aiops.example.com
```

After starting the service the installer waits for the agent to say something,
and fails loudly if it does not. That is the only check made under the account
the service actually runs as; everything before it is checked as the
administrator running the script, and the whole failure above lives in the gap
between those two views of the machine.

If a log holds only `service host:` lines and no timestamped agent output, the
agent process never started. The reason is in the Application event log under
the service's own name — not in that file — and the `errors:` command below is
how to read it.

The service runs as the virtual account `NT SERVICE\AIOpsRelayNode`, which has
no password and no rights beyond its own state directory. Both PowerShell
scripts are ASCII-only and stored as UTF-8 with a BOM, because Windows
PowerShell 5.1 reads a BOM-less `.ps1` as ANSI and one em-dash in a string is
enough to stop the file parsing at all.

The log lives in the state directory, which is the one place the service
account can write. Reading it needs an elevated shell, as does uninstalling.

```powershell
Get-Service AIOpsRelayNode
Get-Content "$env:ProgramData\AIOps Relay Node\aiops-relay.log" -Wait
Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='AIOpsRelayNode'}
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1   # -KeepLog keeps the record
```

### Docker

```sh
AIOPS_RELAY_URL=https://aiops.example.com \
AIOPS_RELAY_TOKEN=<enrolment token> \
  docker compose up -d --build --wait
```

`--wait` is what gives the Docker path the same contract as the other two: the
container is healthy only once the node has actually reached AIOps, so a node
that will never connect exits non-zero here instead of sitting in a restart
loop while you are told it came up. Ask it why with:

```sh
docker compose exec relay-node python3 /opt/aiops-relay/aiops_relay_node.py --diagnose
```

`network_mode: host` is set deliberately: the point of a node is the network it
sits on, and a bridge network is a different one.

```sh
docker logs -f aiops-relay-node
docker compose down -v                # -v matters: it drops the credential
```

## An installer that does not lie about the result

All three refuse before they make a mess, and none of them declares success
over a node that has not connected.

**Before anything is written**, each checks what it can: that the interpreter
is one the service account can actually use, and that this machine can reach
AIOps at all — by running the agent's own `--diagnose`. A network the node
cannot get out of stops the install there, with the reason printed, before a
service exists and before the one-time enrolment token is spent.

**After the service starts**, each waits up to a minute for the node to say it
got there. The agent writes one word into its state directory — `connected`,
`pending`, `revoked`, `unauthenticated` — and the installers read that rather
than scraping a log they may not be able to read. `pending` counts as success:
a node that reached AIOps and was told it is not approved yet has done its
part. Anything else fails loudly, prints what the service had to say, and gives
the exact `--diagnose` command to ask the node why.

|                                        | Linux | Windows | Docker |
|----------------------------------------|-------|---------|--------|
| Interpreter the service account can run | yes, by running it as that account | yes, by path and by DACL — see below | n/a, the image carries its own |
| Refuses a home-directory interpreter    | yes (`ProtectHome=yes` hides it anyway) | yes (any `C:\Users\...`) | n/a |
| `--diagnose` preflight before enrolling | yes, as the service account | yes, as the installing administrator | run it in the container |
| Waits for the node to actually connect  | yes | yes | yes, via the healthcheck and `--wait` |
| Repairs a broken install on re-run      | yes | yes | recreate the container |
| Proxy and CA bundle persisted           | `/etc/aiops-relay/node.env` | `agent.cfg` | environment |

The one asymmetry worth knowing: on Linux the interpreter check *runs* the
interpreter as the account the service will use, which settles the question.
On Windows it cannot — the virtual service account does not exist until the
service does — so it reads the file's permissions and refuses anything inside
a user profile. That is a heuristic, and it is backed by the real test after
`Start-Service`, which does run as the service account.

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
--proxy URL         HTTP CONNECT proxy to reach AIOps through
--ca-bundle PATH    PEM file of extra certificate authorities to trust
--insecure          skip TLS verification; for a self-signed AIOps only
--enrol-only        exchange the token for a credential and exit
--diagnose          say why this machine cannot reach AIOps, and stop
```

Each also reads from the environment: `AIOPS_RELAY_URL`, `AIOPS_RELAY_TOKEN`,
`AIOPS_RELAY_STATE_DIR`, `AIOPS_RELAY_PROXY`, `AIOPS_RELAY_CA_BUNDLE`,
`AIOPS_RELAY_INSECURE`.

## Getting out of a corporate network

A node dials out. On a network that will not let it, the reason is one of a
small number of things, and the agent now names which:

**A proxy.** If this network requires an HTTP proxy to reach anything outside
it, give the node one. The proxy is used for the running connection *and* for
enrolment, which also dials out.

```sh
sudo ./install.sh --url https://aiops.example.com --token <token> \
                  --proxy http://proxy.corp.example:8080
```

Credentials go in the URL (`http://user:password@proxy:8080`) and are sent as
HTTP Basic in the `CONNECT` request. They are never written to a log, and the
node's own AIOps credential never crosses the proxy hop — it travels inside the
TLS session that is established after the tunnel opens.

The proxy is chosen in this order, first match wins:

1. `--proxy` (or `AIOPS_RELAY_PROXY`), which is what the installers persist
2. `NO_PROXY` / `no_proxy` — if it covers the AIOps host, no proxy is used
3. `HTTPS_PROXY`, then `https_proxy`
4. `ALL_PROXY`, then `all_proxy`
5. on Windows, the machine's WinHTTP settings and their bypass list

`NO_PROXY` deliberately does not override an explicit `--proxy`: somebody who
names a proxy on the command line has said what they want. It overrules
everything that is inferred rather than stated.

Windows reads **WinHTTP** (`netsh winhttp show proxy`), not the per-user
Internet Options settings. The relay runs as a service under an account with no
interactive logon and no user hive of its own, so a proxy configured in
somebody's browser is in a registry hive that process cannot see. WinHTTP is
the machine-wide one a service inherits; an administrator populates it with
`netsh winhttp import proxy source=ie`.

**TLS inspection.** A gateway that re-signs certificates makes verification
fail. The fix is to trust that gateway's authority, not to stop verifying:

```sh
sudo ./install.sh --url https://aiops.example.com --token <token> \
                  --ca-bundle /etc/pki/corp-root.pem
```

`--ca-bundle` is additive — the system's own authorities stay trusted — so
verification keeps happening. Reach for it before `--insecure`, which turns
verification off entirely for the handshake the enrolment credential travels
in. On both installers the file is copied in beside the node's configuration,
because the path you pass is usually somewhere the service account cannot read.

**Which of them it is.** `--diagnose` runs the whole dial one stage at a time
and prints a conclusion in words:

```
1. Name resolution    OK            aiops.example.com -> 203.0.113.10
2. Proxy              in use        http://proxy.corp.example:8080 (from HTTPS_PROXY)
3. TCP connection     OK            the proxy at proxy.corp.example:8080 in 0.03s
4. Proxy tunnel       FAILED        HTTP/1.1 407 Proxy Authentication Required
5. TLS                not attempted
6. AIOps handshake    not attempted

Conclusion
  The proxy is reachable and will not tunnel to AIOps.

  PROXY: http://proxy.corp.example:8080 refused to open a tunnel to
  aiops.example.com:443. It answered: 'HTTP/1.1 407 Proxy Authentication
  Required'. That is 'proxy authentication required': put the credentials in
  the proxy URL, as --proxy http://user:password@host:port.
```

It also prints the interpreter it is running under and the account it is
running as, because a node can fail with a perfect network and an interpreter
its service account cannot execute — and nothing about the network says so.

Each of these is its own log line at runtime too, so a node that cannot connect
says which one it is: a name that did not resolve, a refused port, a connection
that timed out, a proxy that would not tunnel (with the proxy's own status
line), a certificate that was not trusted (naming `--ca-bundle`), or AIOps
answering 401/403 — which is a revoked or deleted credential and not a network
fault at all.
