import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fullName, nameById } from "../names";
import type { Team, User, UserSummary } from "../types";

/**
 * Teams, and who is in them.
 *
 * Membership is what makes a team's sessions visible, so only administrators
 * can change it. Everyone else gets this page read-only, because knowing which
 * teams you are in is what makes the "share with a team" control on a session
 * mean anything.
 */
export default function Teams({ me }: { me: User }) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [users, setUsers] = useState<UserSummary[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [members, setMembers] = useState<number[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setTeams(await api.teams());
      setUsers(await api.userDirectory().catch(() => []));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const reset = () => {
    setEditingId(null);
    setName("");
    setDescription("");
    setMembers([]);
  };

  const edit = (team: Team) => {
    setEditingId(team.id);
    setName(team.name);
    setDescription(team.description ?? "");
    setMembers(team.member_ids);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const payload = { name, description: description || null, member_ids: members };
      if (editingId) await api.patchTeam(editingId, payload);
      else await api.createTeam(payload);
      reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (team: Team) => {
    const consequence =
      team.session_count > 0
        ? `\n\n${team.session_count} session(s) belong to it. They are not deleted, but ` +
          "only their owners will still see them."
        : "";
    if (!confirm(`Delete the team "${team.name}"?${consequence}`)) return;
    try {
      await api.deleteTeam(team.id);
      if (editingId === team.id) reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const toggle = (userId: number) =>
    setMembers((prev) =>
      prev.includes(userId) ? prev.filter((id) => id !== userId) : [...prev, userId],
    );

  const nameOf = (userId: number) => nameById(users, userId, `user ${userId}`);

  return (
    <div className="main">
      <h1>Teams</h1>
      <p className="subtitle">
        A team is a shared space: every member sees every session that belongs to it, and
        can answer the approvals a paused agent is waiting on. Stored systems are not
        shared this way — those stay private to whoever put the credential in.
      </p>
      {error && <div className="error-banner">{error}</div>}

      {me.is_admin && (
        <form className="card" onSubmit={submit}>
          <div className="grid-2">
            <label>
              <span>Name</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Platform"
                required
              />
            </label>
            <label>
              <span>Description</span>
              <input value={description} onChange={(e) => setDescription(e.target.value)} />
            </label>
          </div>

          <fieldset className="sharing">
            <legend>Members</legend>
            {users.length === 0 ? (
              <p className="hint">There are no other users yet.</p>
            ) : (
              users.map((u) => (
                <label key={u.id} className="row share-row">
                  {/* Both names: membership grants sight of the team's sessions,
                      and display names are not unique. */}
                  <span style={{ margin: 0, flex: 1 }}>{fullName(u)}</span>
                  <input
                    type="checkbox"
                    checked={members.includes(u.id)}
                    onChange={() => toggle(u.id)}
                  />
                </label>
              ))
            )}
          </fieldset>

          <div className="row">
            <button className="primary" type="submit" disabled={busy || !name.trim()}>
              {editingId ? "Save changes" : "Create team"}
            </button>
            {editingId && (
              <button type="button" onClick={reset}>
                Cancel
              </button>
            )}
          </div>
        </form>
      )}

      {teams.length === 0 ? (
        <div className="empty">
          {me.is_admin ? "No teams yet. Create one above." : "You are not in any team yet."}
        </div>
      ) : (
        teams.map((team) => (
          <div className="card" key={team.id}>
            <div className="row">
              <h2 style={{ margin: 0, flex: 1 }}>{team.name}</h2>
              <span className="pill">
                {team.session_count} session{team.session_count === 1 ? "" : "s"}
              </span>
              {team.member_ids.includes(me.id) && <span className="pill ok">you</span>}
            </div>
            {team.description && (
              <div style={{ color: "var(--text-dim)", fontSize: 13, margin: "6px 0" }}>
                {team.description}
              </div>
            )}
            <div className="hint" style={{ marginTop: 8 }}>
              {team.member_ids.length === 0
                ? "Nobody is in this team, so its sessions are visible to their owners alone."
                : `Members: ${team.member_ids.map(nameOf).sort().join(", ")}`}
            </div>
            {me.is_admin && (
              <div className="row" style={{ marginTop: 12 }}>
                <button onClick={() => edit(team)}>Edit &amp; members</button>
                <button className="danger" onClick={() => remove(team)}>
                  Delete
                </button>
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
}
