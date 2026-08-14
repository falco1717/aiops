"""Re-encrypt every stored credential under a new AIOPS_SECRET_KEY.

AIOPS_SECRET_KEY is the key `app/crypto.py` derives the credential-encryption
key from. Changing it in `.env` and restarting does not migrate anything: every
`targets.private_key_enc`, `passphrase_enc` and `password_enc` in the database
was written under the old key, so after the restart they are simply unreadable
and every stored credential has to be typed in again. This script is the
migration — it reads each value with the old key and writes it back with the
new one, in one transaction, so the rotation costs nobody a re-entry.

Run it *before* changing `.env`, from the app container so that it reaches the
database the same way the application does, with both keys supplied through the
environment — never on the command line, where `ps` would show them. The image
carries only `backend/app`, so the script is copied in rather than being there
already:

    sudo docker compose cp backend/tools/rotate_secret_key.py app:/tmp/rotate.py
    sudo docker compose exec -T \
      -e AIOPS_ROTATE_OLD_KEY="$(sed -n 's/^AIOPS_SECRET_KEY=//p' .env)" \
      -e AIOPS_ROTATE_NEW_KEY="<the new key>" \
      app python3 /tmp/rotate.py --dry-run

Drop `--dry-run` to commit. Then put the new key in `.env` and recreate.

Safety properties, in the order they matter:

* Nothing is committed unless *every* row round-trips. Each new ciphertext is
  decrypted again with the new key and compared against the plaintext that came
  out of the old one, in memory; one mismatch or one undecryptable row rolls the
  whole transaction back and exits non-zero.
* It is safe to run twice. A value that no longer decrypts under the old key but
  does under the new one has already been migrated, and is left exactly as it
  is rather than being counted as a failure.
* It never prints, logs or writes a plaintext credential, or a hash of one. The
  per-row report is lengths and ciphertext digests only.
* The derivation is not reimplemented here. `app.crypto` does it, and this
  swaps the configured secret around calls into it, so a change to the
  derivation cannot leave this script behind.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import os
import sys

# Run from anywhere. In a checkout the application package is one directory up
# (backend/tools -> backend/app); in the image the script has been copied
# somewhere temporary and the package is under the working directory (/app/app).
for _candidate in (
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    os.getcwd(),
):
    if os.path.isdir(os.path.join(_candidate, "app")) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from app import crypto  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Target  # noqa: E402
from sqlalchemy import select  # noqa: E402

#: The encrypted columns. Adding a credential column to the model without
#: adding it here is how a rotation silently orphans it, so the count is
#: asserted against the model below.
FIELDS = ("private_key_enc", "passphrase_enc", "password_enc")


class RotationFailed(RuntimeError):
    """A row could not be migrated, so nothing is."""


@contextlib.contextmanager
def using_secret(secret: str):
    """Run `app.crypto` against a given secret instead of the configured one.

    The point of borrowing the application's own encrypt/decrypt rather than
    deriving a Fernet key here is that the derivation stays in one place. It is
    `settings` that decides which secret those functions use, so that is what
    moves.
    """
    previous = settings.secret_key
    settings.secret_key = secret
    try:
        yield
    finally:
        settings.secret_key = previous


def digest(value: str | None) -> str:
    """A short fingerprint of a *ciphertext*, for the operator's log."""
    if value is None:
        return "-"
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _rotate_value(old_key: str, new_key: str, ciphertext: str) -> tuple[str, bool]:
    """Return (ciphertext under the new key, whether anything changed).

    Raises RotationFailed if the value cannot be read with either key, or if the
    value written back does not decrypt to what was read.
    """
    try:
        with using_secret(old_key):
            plaintext = crypto.decrypt(ciphertext)
    except crypto.SecretUnavailable:
        # Either already migrated, or encrypted under a third key we were not
        # given. Only the first of those is survivable, so check.
        try:
            with using_secret(new_key):
                crypto.decrypt(ciphertext)
        except crypto.SecretUnavailable:
            raise RotationFailed(
                "value decrypts under neither the old nor the new key"
            ) from None
        return ciphertext, False

    if plaintext is None:  # pragma: no cover - decrypt() only returns None for empty
        raise RotationFailed("value decrypted to nothing")

    with using_secret(new_key):
        rewritten = crypto.encrypt(plaintext)
        # The whole point of the exercise: prove the new ciphertext reads back
        # as the same secret before anything is committed.
        check = crypto.decrypt(rewritten)
    if check != plaintext:
        raise RotationFailed("re-encrypted value did not decrypt to the original")
    return rewritten, True


async def rotate(old_key: str, new_key: str, *, commit: bool) -> int:
    missing = [f for f in FIELDS if not hasattr(Target, f)]
    if missing:
        raise RotationFailed(f"model has no column {missing} — update FIELDS")
    model_columns = {c.key for c in Target.__table__.columns if c.key.endswith("_enc")}
    if model_columns != set(FIELDS):
        raise RotationFailed(
            f"encrypted columns on targets are {sorted(model_columns)}, "
            f"but this script rotates {sorted(FIELDS)}"
        )

    changed = skipped = 0
    async with SessionLocal() as db:
        targets = list(await db.scalars(select(Target).order_by(Target.id)))
        print(f"targets to migrate: {len(targets)}")
        for target in targets:
            for field in FIELDS:
                ciphertext = getattr(target, field)
                if not ciphertext:
                    continue
                try:
                    rewritten, did = _rotate_value(old_key, new_key, ciphertext)
                except RotationFailed as exc:
                    raise RotationFailed(
                        f"target {target.id} ({target.slug}) {field}: {exc}"
                    ) from None
                if not did:
                    skipped += 1
                    print(
                        f"  target {target.id:>4} {target.slug:<24} {field:<16} "
                        f"already under the new key, left alone "
                        f"(len={len(ciphertext)} sha256={digest(ciphertext)})"
                    )
                    continue
                setattr(target, field, rewritten)
                changed += 1
                print(
                    f"  target {target.id:>4} {target.slug:<24} {field:<16} "
                    f"len {len(ciphertext)}->{len(rewritten)}  "
                    f"sha256 {digest(ciphertext)} -> {digest(rewritten)}  "
                    f"round-trip OK"
                )
        if not commit:
            await db.rollback()
            print(f"\nDRY RUN — rolled back. would re-encrypt {changed} value(s), "
                  f"leave {skipped} already migrated.")
            return 0
        await db.commit()
        print(f"\ncommitted: re-encrypted {changed} value(s), "
              f"left {skipped} already migrated.")

    # Read back through a fresh session, with only the new key, the way the
    # application will after it is restarted.
    async with SessionLocal() as db:
        targets = list(await db.scalars(select(Target).order_by(Target.id)))
        for target in targets:
            for field in FIELDS:
                ciphertext = getattr(target, field)
                if not ciphertext:
                    continue
                with using_secret(new_key):
                    crypto.decrypt(ciphertext)
        print(f"read-back with the new key alone: every value decrypts "
              f"({len(targets)} target(s))")
    return changed


def read_key(name: str, path: str | None) -> str:
    if path:
        with open(path) as fh:
            return fh.read().strip()
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set. Supply the key in the environment (or with the "
            f"matching --*-key-file), never as a command-line argument."
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-encrypt stored credentials under a new AIOPS_SECRET_KEY.",
        epilog="Keys are read from AIOPS_ROTATE_OLD_KEY / AIOPS_ROTATE_NEW_KEY.",
    )
    parser.add_argument("--old-key-file", help="file holding the current key")
    parser.add_argument("--new-key-file", help="file holding the replacement key")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify every row round-trips, then roll back",
    )
    args = parser.parse_args()

    old_key = read_key("AIOPS_ROTATE_OLD_KEY", args.old_key_file)
    new_key = read_key("AIOPS_ROTATE_NEW_KEY", args.new_key_file)
    if old_key == new_key:
        raise SystemExit("the old and new keys are identical — nothing to do")

    print(f"database: {settings.database_url.split('@')[-1]}")
    print(f"old key: len={len(old_key)}  new key: len={len(new_key)}")
    print(f"mode: {'DRY RUN' if args.dry_run else 'COMMIT'}\n")
    try:
        asyncio.run(rotate(old_key, new_key, commit=not args.dry_run))
    except RotationFailed as exc:
        print(f"\nFAILED — nothing was committed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
