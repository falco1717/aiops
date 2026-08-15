/**
 * How a person is named on screen, decided in exactly one place.
 *
 * A user has a `username` — their unique login — and an optional
 * `display_name`, which is what they would rather be called. The display name
 * wins wherever a person is named, but it is nullable and it is deliberately
 * *not* unique: two people called "Walt" is a real situation, and the username
 * is what tells them apart.
 *
 * This exists as one function rather than `u.display_name || u.username`
 * scattered through the views because the scattered form does not fail when it
 * is missed — it quietly shows a login name to somebody who chose another name,
 * in one screen out of eight, and nobody notices until they complain. The
 * server keeps the matching single resolver in `app/names.py`.
 */

/** The least a thing must have to be nameable. `UserSummary` and `User` both do. */
export type Named = { username: string; display_name?: string | null };

/** What to call this person. Never empty. */
export function displayName(person: Named | null | undefined): string {
  if (!person) return "someone";
  const chosen = (person.display_name ?? "").trim();
  return chosen || person.username;
}

/**
 * The name, plus the username in brackets when they differ.
 *
 * For the places where picking the wrong person has a consequence — granting
 * access to a stored credential, handing a session over — because display
 * names are not unique and "Walt" alone is not enough to choose between two
 * of them.
 */
export function fullName(person: Named | null | undefined): string {
  if (!person) return "someone";
  const shown = displayName(person);
  return shown === person.username ? shown : `${shown} (${person.username})`;
}

/** Look an id up in a directory and name them. `unknown` covers a stale id. */
export function nameById<T extends Named & { id: number }>(
  people: T[],
  id: number | null | undefined,
  unknown = "someone",
): string {
  if (id === null || id === undefined) return unknown;
  const found = people.find((p) => p.id === id);
  return found ? displayName(found) : unknown;
}
