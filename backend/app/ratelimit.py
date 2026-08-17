from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .config import settings


@dataclass
class _Bucket:
    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


class LoginThrottle:
    """Brute-force protection for the sign-in endpoint.

    Tracked per username *and* per client address, so one attacker cannot lock
    out a legitimate user by hammering their name from elsewhere, and cannot
    dodge the limit by rotating usernames from one address.

    In-process by design: AIOps is single-node (the runner and event hub are
    too). If it ever scales out, this needs to move to the database or Redis.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = defaultdict(_Bucket)

    def _key(self, kind: str, value: str) -> str:
        return f"{kind}:{value.lower()}"

    def retry_after(self, username: str, client_ip: str | None) -> int:
        """Seconds the caller must wait, or 0 if they may try now."""
        now = time.monotonic()
        worst = 0.0
        for key in self._keys(username, client_ip):
            bucket = self._buckets.get(key)
            if bucket and bucket.locked_until > now:
                worst = max(worst, bucket.locked_until - now)
        return int(worst) + 1 if worst else 0

    def _prune(self, now: float) -> None:
        """Drop buckets that are neither locked nor holding recent failures.

        Without this the map grows one entry per distinct username tried, which
        an attacker rotating usernames can inflate without bound.
        """
        window = settings.login_failure_window_seconds
        for key, bucket in list(self._buckets.items()):
            if bucket.locked_until > now:
                continue
            if bucket.failures and now - bucket.failures[-1] <= window:
                continue
            del self._buckets[key]

    def record_failure(self, username: str, client_ip: str | None) -> None:
        now = time.monotonic()
        window = settings.login_failure_window_seconds
        # Cheap and bounded: only runs on a failed sign-in, which is rare.
        if len(self._buckets) > 512:
            self._prune(now)
        for key in self._keys(username, client_ip):
            bucket = self._buckets[key]
            bucket.failures.append(now)
            while bucket.failures and now - bucket.failures[0] > window:
                bucket.failures.popleft()
            if len(bucket.failures) >= settings.login_max_failures:
                bucket.locked_until = now + settings.login_lockout_seconds
                bucket.failures.clear()

    def record_success(self, username: str, client_ip: str | None) -> None:
        for key in self._keys(username, client_ip):
            self._buckets.pop(key, None)

    def _keys(self, username: str, client_ip: str | None) -> list[str]:
        keys = [self._key("user", username)]
        if client_ip:
            keys.append(self._key("ip", client_ip))
        return keys


def client_address(request) -> str | None:
    """Best guess at the browser's IP, honouring the proxy's forwarded header.

    Proxy headers are applied to this application (see `loopback.build_asgi`),
    but read X-Forwarded-For explicitly so the throttle still sees distinct
    clients if that ever changes.

    Never use this to decide whether a caller is allowed to do something. The
    value is taken from a header the caller writes, so anyone can make it say
    anything; it is fit for spreading a rate limit across clients and for
    nothing else. `loopback.peer_is_loopback` is the one that reads the real
    transport peer.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


throttle = LoginThrottle()
