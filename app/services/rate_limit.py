"""Sliding-window attempt counters kept in SQLite.

Failed passwords, recovery starts and face challenge starts used to live in
process dictionaries, so a restart wiped every lockout and two uvicorn workers
would each count separately. Keys are HMAC digests computed by the callers, so
no login or address is stored in clear text.
"""

import time
from dataclasses import dataclass

from app.db.database import Database

PASSWORD_FAILURES = "PASSWORD_FAILURE"  # noqa: S105 -- bucket name, not a credential
ADMIN_LOGIN_FAILURES = "ADMIN_LOGIN_FAILURE"
ADMIN_LOGIN_LOCKS = "ADMIN_LOGIN_LOCK"
RECOVERY_STARTS = "RECOVERY_START"
FACE_CHALLENGE_STARTS = "FACE_CHALLENGE_START"

# Rows older than this are never needed by any window and are deleted lazily.
RETENTION_SECONDS = 24 * 60 * 60
_PRUNE_EVERY_SECONDS = 60.0


@dataclass(frozen=True)
class WindowStats:
    count: int
    last: float | None


class RateLimitStore:
    def __init__(self, database: Database):
        self.db = database
        self._last_prune = 0.0

    def stats(self, bucket: str, key: str, window_seconds: float, now: float | None = None) -> WindowStats:
        moment = time.time() if now is None else now
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MAX(occurred_at) FROM rate_limit_events WHERE bucket=? AND key=? AND occurred_at>=?",
                (bucket, key, moment - window_seconds),
            ).fetchone()
        return WindowStats(int(row[0]), float(row[1]) if row[1] is not None else None)

    def record(self, bucket: str, key: str, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO rate_limit_events(bucket,key,occurred_at) VALUES(?,?,?)",
                (bucket, key, moment),
            )
            if moment - self._last_prune >= _PRUNE_EVERY_SECONDS:
                conn.execute("DELETE FROM rate_limit_events WHERE occurred_at<?", (moment - RETENTION_SECONDS,))
                self._last_prune = moment

    def record_if_under(
        self, bucket: str, key: str, limit: int, window_seconds: float, now: float | None = None
    ) -> bool:
        """Atomically record an attempt unless the window already holds ``limit`` of them."""
        moment = time.time() if now is None else now
        with self.db.write() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM rate_limit_events WHERE bucket=? AND key=? AND occurred_at>=?",
                (bucket, key, moment - window_seconds),
            ).fetchone()[0]
            if int(count) >= limit:
                return False
            conn.execute(
                "INSERT INTO rate_limit_events(bucket,key,occurred_at) VALUES(?,?,?)",
                (bucket, key, moment),
            )
        return True

    def clear(self, bucket: str, key: str) -> None:
        with self.db.write() as conn:
            conn.execute("DELETE FROM rate_limit_events WHERE bucket=? AND key=?", (bucket, key))

    def clear_prefix(self, bucket: str, prefix: str) -> None:
        """Delete every key of a bucket that starts with ``prefix`` (hex digests, no LIKE wildcards)."""
        with self.db.write() as conn:
            conn.execute(
                "DELETE FROM rate_limit_events WHERE bucket=? AND substr(key,1,?)=?",
                (bucket, len(prefix), prefix),
            )
