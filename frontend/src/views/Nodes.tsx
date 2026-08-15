import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fullName } from "../names";
import type {
  InstallCommand,
  NodeEnrolment,
  NodeGrant,
  RelayNode,
  User,
  UserSummary,
} from "../types";

/**
 * Relay nodes — machines on other networks that agents reach through.
 *
 * Two things this page is careful about. The enrolment token is shown once,
 * here, in the response that mints it; it is stored hashed and cannot be read
 * back, so the panel says so rather than letting someone come back for it
 * later. And approval is deliberately separate from access: an administrator
 * approves a node without thereby being able to route through it, which is why
 * the pending list is its own section rather than part of the list below.
 */
export default function Nodes({ me }: { me: User }) {
  const [items, setItems] = useState<RelayNode[]>([]);
  const [pending, setPending] = useState<RelayNode[]>([]);
  const [users, setUsers] = useState<UserSummary[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [grants, setGrants] = useState<NodeGrant[]>([]);
  const [cidrs, setCidrs] = useState("");
  const [ports, setPorts] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [issued, setIssued] = useState<NodeEnrolment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    // Fetched independently, because these were sequential awaits in one try:
    // the node list throwing meant the pending list was never even requested,
    // so a broken listing also hid the approval card — and approving a node is
    // the one thing you cannot do any other way from here.
    const [nodes, directory, waiting] = await Promise.allSettled([
      api.nodes(),
      api.userDirectory(),
      // Not everyone may ask, and being refused is not an error worth showing.
      me.is_admin ? api.pendingNodes() : Promise.resolve([]),
    ]);
    if (nodes.status === "fulfilled") setItems(nodes.value);
    if (directory.status === "fulfilled") setUsers(directory.value);
    if (waiting.status === "fulfilled") setPending(waiting.value);

    const failed = nodes.status === "rejected" ? nodes.reason : null;
    setError(
      failed ? (failed instanceof Error ? failed.message : String(failed)) : null,
    );
  }, [me.is_admin]);

  useEffect(() => {
    void load();
  }, [load]);

  // A node's state is mostly what the far machine is doing, so it will not
  // change because of anything on this page.
  useEffect(() => {
    const timer = setInterval(() => void load(), 10000);
    return () => clearInterval(timer);
  }, [load]);

  const reset = () => {
    setName("");
    setDescription("");
    setGrants([]);
    setCidrs("");
    setPorts("");
    setEditingId(null);
  };

  const act = async (work: () => Promise<unknown>) => {
    setError(null);
    setBusy(true);
    try {
      await work();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    await act(async () => {
      if (editingId) {
        await api.updateNode(editingId, {
          name,
          description: description || null,
          grants,
          // Sent on every save of an existing node, including when the box was
          // cleared — that is how reach is taken away again, and leaving the
          // field out would make clearing it a no-op.
          allowed_cidrs: splitList(cidrs),
          allowed_ports: splitList(ports).map(Number),
        });
      } else {
        setIssued(await api.createNode({ name, description: description || null, grants }));
      }
      reset();
    });
  };

  const edit = (node: RelayNode) => {
    setEditingId(node.id);
    setName(node.name);
    setDescription(node.description ?? "");
    setGrants(node.grants.filter((g) => g.user_id !== node.owner_id));
    // Prefilled with a *suggestion* when nothing has been allowed yet — the
    // /24 around whatever address the node reported, which is the answer
    // almost every single-LAN node wants. It is only a filled-in box: nothing
    // is granted until this form is saved, and clearing it before saving
    // leaves the node exactly as reachable as it was.
    setCidrs(
      node.allowed_cidrs.length > 0 ? node.allowed_cidrs.join(", ") : (suggestCidr(node) ?? ""),
    );
    setPorts(node.allowed_ports.join(", "));
  };

  const remove = (node: RelayNode) =>
    confirm(
      `Delete "${node.name}"? The machine keeps running until you uninstall it there.`,
    ) && act(() => api.deleteNode(node.id));

  const revoke = (node: RelayNode) =>
    confirm(
      `Revoke "${node.name}"? Its connection is closed now and anything routed ` +
        `through it stops working. This cannot be undone — a revoked node has to be ` +
        `registered again.`,
    ) && act(() => api.revokeNode(node.id));

  const setGrant = (userId: number, level: "" | "use" | "manage") =>
    setGrants((prev) => {
      const rest = prev.filter((g) => g.user_id !== userId);
      return level ? [...rest, { user_id: userId, level }] : rest;
    });

  const levelOf = (userId: number) => grants.find((g) => g.user_id === userId)?.level ?? "";

  return (
    <div className="main">
      <h1>Relay nodes</h1>
      <p className="subtitle">
        A machine on another network that AIOps opens connections through. It dials out
        and holds the connection open, so nothing has to be forwarded to it. Agents keep
        running here — a node is only ever told a host and a port, and never receives a
        provider login, an SSH key, or anything an agent said.
      </p>
      {error && <div className="error-banner">{error}</div>}

      {issued && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Install {issued.node.name}</h2>
          <p className="hint">
            This token is readable now and never again — AIOps stores only a hash of it.
            It works once, and expires
            {issued.expires_at ? ` on ${new Date(issued.expires_at).toLocaleString()}` : " shortly"}.
          </p>
          <InstallPicker issued={issued} />
          <p className="hint">
            The node enrols and then waits: it carries no traffic until an administrator
            approves it below.
          </p>
          <div className="row">
            <button onClick={() => void navigator.clipboard?.writeText(issued.enrolment_token)}>
              Copy token
            </button>
            <button onClick={() => setIssued(null)}>Done</button>
          </div>
        </div>
      )}

      {pending.length > 0 && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Waiting for approval</h2>
          <p className="hint">
            These have enrolled and are asking to connect. Approving one lets it carry
            traffic; it does not give you access to route through it.
          </p>
          {pending.map((node) => (
            <div className="row" key={node.id} style={{ flexWrap: "wrap", marginTop: 8 }}>
              <span style={{ flex: 1 }}>
                <strong>{node.name}</strong>{" "}
                <span className="hint">
                  {node.reported_hostname ?? "unknown host"}
                  {node.networks.length > 0 ? ` · ${node.networks.slice(0, 3).join(", ")}` : ""}
                  {node.version ? ` · agent ${node.version}` : ""}
                </span>
              </span>
              <button className="primary" disabled={busy} onClick={() => void act(() => api.approveNode(node.id))}>
                Approve
              </button>
              <button className="danger" disabled={busy} onClick={() => void revoke(node)}>
                Reject
              </button>
            </div>
          ))}
        </div>
      )}

      <form className="card" onSubmit={submit}>
        <label>
          <span>Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Salt Network"
            required
          />
        </label>
        <label>
          <span>Description</span>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="jprod-sb, 10.10.20.0/28"
          />
        </label>

        {editingId !== null && (
          <fieldset className="sharing">
            <legend>Networks agents may reach through it</legend>
            <p className="hint">
              Without this, only the stored systems pointed at this node are reachable
              through it. Naming a range here lets an agent connect to any address in it —
              <code> ssh user@198.51.100.42</code> — for people who can already route
              through the node. No credential comes with it: reachability and a login are
              separate things, and AIOps only grants the first.
            </p>
            <label>
              <span>Allowed networks</span>
              <input
                value={cidrs}
                onChange={(e) => setCidrs(e.target.value)}
                placeholder="198.51.100.0/24"
              />
            </label>
            <p className="hint">
              Comma separated, in CIDR form. Private ranges only, /16 or narrower — a relay
              node is a route into one network, and anything wider would make AIOps a way
              out to the internet from somebody else's address. Leave empty for no subnet
              access. Whatever the node reports about itself is ignored here.
            </p>
            <label>
              <span>Allowed ports</span>
              <input
                value={ports}
                onChange={(e) => setPorts(e.target.value)}
                placeholder="22"
              />
            </label>
            <p className="hint">
              Comma separated. Empty means 22 alone. Anything outside these ranges and
              ports is refused when the connection is opened, not merely left out of the
              agent's config.
            </p>
          </fieldset>
        )}

        <fieldset className="sharing">
          <legend>Who else can route through it</legend>
          <p className="hint">
            A node is a way into a network, so it is private like a stored credential:
            nobody else sees it — administrators included — until you name them here.
          </p>
          {users.filter((u) => u.id !== me.id).length === 0 ? (
            <p className="hint">There is nobody else to share with yet.</p>
          ) : (
            users
              .filter((u) => u.id !== me.id)
              .map((u) => (
                <label key={u.id} className="row share-row">
                  {/* Both names: this hands somebody a way into another
                      network, and display names are not unique. */}
                  <span style={{ margin: 0, flex: 1 }}>{fullName(u)}</span>
                  <select
                    value={levelOf(u.id)}
                    onChange={(e) => setGrant(u.id, e.target.value as "" | "use" | "manage")}
                  >
                    <option value="">No access</option>
                    <option value="use">Can use</option>
                    <option value="manage">Can manage</option>
                  </select>
                </label>
              ))
          )}
        </fieldset>

        <div className="row">
          <button className="primary" type="submit" disabled={busy || !name.trim()}>
            {editingId ? "Save changes" : "Register node"}
          </button>
          {editingId && (
            <button type="button" onClick={reset}>
              Cancel
            </button>
          )}
        </div>
      </form>

      {items.length === 0 ? (
        <div className="empty">No relay nodes yet. Register one above.</div>
      ) : (
        items.map((node) => (
          <div className="card" key={node.id}>
            <div className="row">
              <h2 style={{ margin: 0, flex: 1 }}>{node.name}</h2>
              <code className="pill">{node.slug}</code>
              <span className={`pill ${statusTone(node)}`}>{statusLabel(node)}</span>
            </div>
            <div style={{ color: "var(--text-dim)", fontSize: 13, margin: "6px 0 10px" }}>
              {node.reported_hostname ?? "not yet enrolled"}
              {node.version ? ` · agent ${node.version}` : ""}
              {node.description ? ` · ${node.description}` : ""}
            </div>

            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              {/* Two different lists that look alike and are not. The plain
                  pills are what the node says it can see; the highlighted ones
                  are what it has been allowed to be used for. */}
              {node.networks.slice(0, 6).map((net) => (
                <span className="pill" key={net} title="Reported by the node — not access">
                  {net}
                </span>
              ))}
              {node.allowed_cidrs.map((cidr) => (
                <span
                  className="pill ok"
                  key={`allowed-${cidr}`}
                  title={`Agents may reach any address in ${cidr} on port ${node.allowed_ports.join(", ")}`}
                >
                  ↔ {cidr}
                </span>
              ))}
              {node.target_count > 0 && (
                <span className="pill">
                  {node.target_count} system{node.target_count === 1 ? "" : "s"} behind it
                </span>
              )}
              {node.enrolment_pending && <span className="pill">enrolment token outstanding</span>}
            </div>

            <div className="row" style={{ marginTop: 12, flexWrap: "wrap" }}>
              <span className="hint" style={{ flex: 1 }}>
                {node.last_seen_at
                  ? `Last seen ${new Date(node.last_seen_at).toLocaleString()}`
                  : "Never connected"}
              </span>
              {(node.my_level === "owner" || node.my_level === "manage") && (
                <>
                  <button onClick={() => edit(node)}>Edit &amp; sharing</button>
                  {node.status !== "revoked" && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        void act(async () => setIssued(await api.reissueNodeToken(node.id)))
                      }
                    >
                      New enrolment token
                    </button>
                  )}
                  {node.status !== "revoked" && (
                    <button className="danger" disabled={busy} onClick={() => void revoke(node)}>
                      Revoke
                    </button>
                  )}
                  <button className="danger" disabled={busy} onClick={() => void remove(node)}>
                    Delete
                  </button>
                </>
              )}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

/** A comma or whitespace separated box, as the list it means. */
function splitList(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

/**
 * The /24 around whatever address the node reported, as a starting suggestion.
 *
 * A suggestion and nothing more: it lands in a form field that has to be
 * saved, so seeing it grants nothing. Derived from `reported_hostname` first
 * and then from the networks the node claims, because either can be the only
 * one that is an address — a node behind DHCP reports a name for itself and
 * its subnet in the list, and a node with no DNS reports the reverse.
 *
 * The server re-validates whatever comes back, so a wrong guess here is
 * refused there rather than quietly accepted.
 */
function suggestCidr(node: RelayNode): string | null {
  const candidates = [node.reported_hostname ?? "", ...node.networks];
  for (const candidate of candidates) {
    const address = candidate.trim().split("/")[0];
    const octets = address.split(".");
    if (octets.length !== 4) continue;
    if (!octets.every((o) => /^\d{1,3}$/.test(o) && Number(o) <= 255)) continue;
    return `${octets[0]}.${octets[1]}.${octets[2]}.0/24`;
  }
  return null;
}

/** Status and reachability are different questions, and both matter. */
function statusLabel(node: RelayNode): string {
  if (node.status === "revoked") return "revoked";
  if (node.status === "pending") return node.enrolled_at ? "waiting for approval" : "not enrolled";
  return node.online ? "connected" : "approved, not connected";
}

function statusTone(node: RelayNode): string {
  if (node.status === "revoked") return "failed";
  if (node.status === "approved" && node.online) return "ok";
  return "";
}


/**
 * The install command, per platform.
 *
 * Three tabs rather than one line plus prose: the installers share no argument
 * between them — `--url` against `-Url` against an environment variable — so
 * "there is a PowerShell installer beside it" left the reader to guess at a
 * command, which is how you end up pasting Linux flags into PowerShell.
 */
function InstallPicker({ issued }: { issued: NodeEnrolment }) {
  // Older servers only sent install_hint; keep working against one.
  const commands: InstallCommand[] = issued.install?.length
    ? issued.install
    : [{ platform: "linux", label: "Linux (systemd)", command: issued.install_hint, note: null }];

  const [platform, setPlatform] = useState(() => guessPlatform(commands));
  const chosen = commands.find((c) => c.platform === platform) ?? commands[0];
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(chosen.command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked; the box is selectable */
    }
  };

  return (
    <div className="install-picker">
      <div className="row install-tabs" role="tablist" aria-label="Install command">
        {commands.map((c) => (
          <button
            key={c.platform}
            type="button"
            role="tab"
            aria-selected={c.platform === chosen.platform}
            className={`install-tab${c.platform === chosen.platform ? " active" : ""}`}
            onClick={() => setPlatform(c.platform)}
          >
            {c.label}
          </button>
        ))}
      </div>
      {/* Step one, above the command, because it is what has to happen first
          and because the command used to say "run it from deploy/relay" to
          people who had no way to get deploy/relay. A plain link, not a fetch:
          the endpoint is cookie-authenticated and same-origin, so the browser
          does the download itself and the file lands wherever the operator
          keeps downloads. The enrolment token is not in it — it is in the
          command below, which is the only place a single-use secret belongs. */}
      <div className="row install-get">
        <a className="install-download" href={installerUrl(chosen.platform)} download>
          Download installer
        </a>
        <span className="hint">
          {`aiops-relay-node-${chosen.platform}.zip — unzip it on the machine you are installing.`}
        </span>
      </div>
      <textarea className="mono" rows={2} readOnly value={chosen.command} />
      {chosen.note && <p className="hint">{chosen.note}</p>}
      <div className="row">
        <button type="button" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy command"}
        </button>
      </div>
    </div>
  );
}

/**
 * Derived from the platform rather than sent by the server: it is the same
 * three names the tabs already are, and adding a URL to every InstallCommand
 * would put a per-node field on something that does not vary per node.
 */
function installerUrl(platform: string): string {
  return `/api/nodes/installer/${encodeURIComponent(platform)}`;
}

/** Open on the platform the operator is most likely standing on. */
function guessPlatform(commands: InstallCommand[]): string {
  const ua = navigator.userAgent;
  const guess = /Windows/i.test(ua) ? "windows" : "linux";
  return commands.some((c) => c.platform === guess) ? guess : commands[0].platform;
}
