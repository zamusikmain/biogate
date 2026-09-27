import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.services.repository import Repository

ADMIN_COOKIE_NAME = "biogate_admin_session"


class AuthNotConfiguredError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class LoginRateLimitedError(Exception):
    pass


@dataclass(frozen=True)
class AdminSession:
    session_id: str
    username: str
    csrf_token: str
    expires_at: float


@dataclass
class LoginAttemptBucket:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class AdminAuthService:
    """Local, process-bound admin sessions with signed opaque cookies."""

    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository
        self.password_hasher = PasswordHasher(type=Type.ID)
        self._sessions: dict[str, AdminSession] = {}
        self._attempts: dict[str, LoginAttemptBucket] = {}
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.admin_username
            and self.settings.admin_password_hash.get_secret_value()
            and len(self.settings.session_secret.get_secret_value()) >= 32
        )

    def login(self, username: str, password: str, client_key: str) -> tuple[AdminSession, str]:
        if not self.configured:
            raise AuthNotConfiguredError
        now = time.time()
        with self._lock:
            self._check_rate_limit(client_key, now)

        configured_username = self.settings.admin_username
        password_hash = self.settings.admin_password_hash.get_secret_value()
        username_matches = hmac.compare_digest(username, configured_username)
        password_matches = False
        try:
            password_matches = self.password_hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            password_matches = False

        if not username_matches or not password_matches:
            with self._lock:
                self._record_failure(client_key, now)
            self.repository.add_audit("ADMIN_LOGIN_FAILED", None, "REJECTED", {"reason_code": "INVALID_CREDENTIALS"})
            raise InvalidCredentialsError

        session_id = secrets.token_urlsafe(32)
        session = AdminSession(
            session_id=session_id,
            username=configured_username,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + self.settings.admin_session_ttl_seconds,
        )
        with self._lock:
            self._attempts.pop(client_key, None)
            self._sessions[session_id] = session
            self._remove_expired_sessions(now)
        self.repository.add_audit("ADMIN_LOGIN_SUCCESS", None, "SUCCESS", {"username": configured_username})
        return session, self._signed_cookie(session_id)

    def get_session(self, cookie_value: str | None) -> AdminSession | None:
        session_id = self._verified_session_id(cookie_value)
        if session_id is None:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= now:
                self._sessions.pop(session_id, None)
                return None
            return session

    def validate_csrf(self, session: AdminSession, token: str | None) -> bool:
        return bool(token and hmac.compare_digest(session.csrf_token, token))

    def logout(self, cookie_value: str | None) -> None:
        session_id = self._verified_session_id(cookie_value)
        session = None
        if session_id:
            with self._lock:
                session = self._sessions.pop(session_id, None)
        self.repository.add_audit(
            "ADMIN_LOGOUT",
            None,
            "SUCCESS",
            {"username": session.username if session else "unknown"},
        )

    def expire_for_test(self, cookie_value: str) -> None:
        """Expire one session deterministically without exposing session internals."""
        session_id = self._verified_session_id(cookie_value)
        if not session_id:
            return
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                self._sessions[session_id] = AdminSession(
                    session.session_id,
                    session.username,
                    session.csrf_token,
                    time.time() - 1,
                )

    def _signed_cookie(self, session_id: str) -> str:
        signature = hmac.new(
            self.settings.session_secret.get_secret_value().encode(),
            session_id.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{session_id}.{signature}"

    def _verified_session_id(self, cookie_value: str | None) -> str | None:
        if not cookie_value or not self.configured:
            return None
        session_id, separator, signature = cookie_value.partition(".")
        if not separator or not session_id or not signature:
            return None
        expected = self._signed_cookie(session_id).rsplit(".", 1)[1]
        return session_id if hmac.compare_digest(signature, expected) else None

    def _check_rate_limit(self, client_key: str, now: float) -> None:
        bucket = self._attempts.get(client_key)
        if not bucket:
            return
        if bucket.locked_until > now:
            self.repository.add_audit("ADMIN_LOGIN_FAILED", None, "REJECTED", {"reason_code": "RATE_LIMITED"})
            raise LoginRateLimitedError
        bucket.failures = [
            moment for moment in bucket.failures if now - moment <= self.settings.admin_login_window_seconds
        ]
        if not bucket.failures:
            self._attempts.pop(client_key, None)

    def _record_failure(self, client_key: str, now: float) -> None:
        bucket = self._attempts.setdefault(client_key, LoginAttemptBucket())
        bucket.failures = [
            moment for moment in bucket.failures if now - moment <= self.settings.admin_login_window_seconds
        ]
        bucket.failures.append(now)
        if len(bucket.failures) >= self.settings.admin_login_max_attempts:
            bucket.failures.clear()
            bucket.locked_until = now + self.settings.admin_login_cooldown_seconds

    def _remove_expired_sessions(self, now: float) -> None:
        expired = [session_id for session_id, session in self._sessions.items() if session.expires_at <= now]
        for session_id in expired:
            self._sessions.pop(session_id, None)
