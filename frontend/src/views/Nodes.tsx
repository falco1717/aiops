import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { NodeEnrolment, NodeGrant, RelayNode, User, UserSummary } from "../types";

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
  const [editingId, setEditingId] = useState<number | null>(null);
  const [issued, setIssued] = useState<NodeEnrolment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setItems(await api.nodes());
      setUsers(await api.userDirectory().catch(() => []));
      // Not everyone may ask, and being refused is not an error worth showing.
      setPending(me.is_admin ? await api.pendingNodes().catch(() => []) : []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
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
        await api.updateNode(editingId, { name, description: description || null, grants });
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
          <textarea className="mono" rows={2} readOnly value={issued.install_hint} />
          <p className="hint">
            Run that on the machine, from <code>deploy/relay</code>. There is a PowerShell
            installer and a Docker Compose file beside it. The node will enrol and then
            wait: it carries no traffic until an administrator approves it below.
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
                  <span style={{ margin: 0, flex: 1 }}>{u.username}</span>
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
              {node.networks.slice(0, 6).map((net) => (
                <span className="pill" key={net}>
                  {net}
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
