import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.services.repository import Repository

ACCOUNT_COOKIE_NAME = "biogate_session"


class AccountAuthError(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class AccountSession:
    id: str
    user_id: int
    login: str
    full_name: str
    role: str
    status: str
    csrf_token: str
    login_method: str
    must_change_password: bool


class AccountAuthService:
    def __init__(self, repository: Repository, settings: Settings):
        self.repository = repository
        self.settings = settings
        self.hasher = PasswordHasher(type=Type.ID)
        self._password_failures: dict[str, list[datetime]] = {}
        self._rate_lock = threading.RLock()

    def validate_password(self, password: str) -> None:
        if len(password) < 12:
            raise AccountAuthError("PASSWORD_TOO_SHORT")
        if len(password) > 1024:
            raise AccountAuthError("PASSWORD_TOO_LONG")
        if not any("a" <= character <= "z" for character in password):
            raise AccountAuthError("PASSWORD_LOWERCASE_REQUIRED")
        if not any("A" <= character <= "Z" for character in password):
            raise AccountAuthError("PASSWORD_UPPERCASE_REQUIRED")
        if not any("0" <= character <= "9" for character in password):
            raise AccountAuthError("PASSWORD_DIGIT_REQUIRED")
        if not any(not character.isalnum() and not character.isspace() for character in password):
            raise AccountAuthError("PASSWORD_SPECIAL_REQUIRED")

    def hash_password(self, password: str) -> str:
        self.validate_password(password)
        return self.hasher.hash(password)

    def verify_password(self, password_hash: str | None, password: str) -> bool:
        if not password_hash:
            return False
        try:
            return bool(self.hasher.verify(password_hash, password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def password_login(
        self, login: str, password: str, *, client_address: str = "", user_agent: str = ""
    ) -> tuple[AccountSession, str]:
        if not self.settings.allow_password_login:
            raise AccountAuthError("PASSWORD_LOGIN_DISABLED")
        rate_key = self._rate_key(login, client_address)
        if self._password_rate_limited(rate_key):
            self.repository.add_access_event(
                {
                    "method": "PASSWORD",
                    "result": "DENIED",
                    "reason": "LOGIN_RATE_LIMITED",
                    "client_address": client_address,
                    "user_agent": user_agent,
                }
            )
            raise AccountAuthError("LOGIN_RATE_LIMITED")
        user = self.repository.get_user_by_login(login)
        now = datetime.now(UTC)
        if user and not bool(user.get("password_enabled", 1)):
            self._denied_status(user, "PASSWORD_DISABLED", client_address, user_agent)
        if user and user.get("locked_until") and datetime.fromisoformat(str(user["locked_until"])) > now:
            raise AccountAuthError("LOGIN_RATE_LIMITED")
        valid = self.verify_password(str(user.get("password_hash")) if user else None, password)
        if not user or not valid:
            self._record_password_failure(rate_key, now)
            if user:
                attempts = int(user.get("failed_login_count") or 0) + 1
                locked_until = None
                if attempts >= self.settings.password_login_max_attempts:
                    locked_until = (now + timedelta(seconds=self.settings.password_login_cooldown_seconds)).isoformat()
                    self.repository.add_security_event(
                        "MULTIPLE_FAILED_PASSWORDS", "WARNING", int(str(user["id"])), {"attempts": attempts}
                    )
                self.repository.record_failed_login(int(str(user["id"])), locked_until)
            event_id = self.repository.add_access_event(
                {
                    "user_id": int(str(user["id"])) if user else None,
                    "method": "PASSWORD",
                    "result": "DENIED",
                    "reason": "INVALID_CREDENTIALS",
                    "client_address": client_address,
                    "user_agent": user_agent,
                }
            )
            self.repository.add_audit(
                "LOGIN_FAILED",
                int(str(user["id"])) if user else None,
                "DENIED",
                {"access_event_id": event_id, "reason": "INVALID_CREDENTIALS"},
            )
            self.repository.add_security_event(
                "LOGIN_FAILED",
                "WARNING",
                int(str(user["id"])) if user else None,
                {"client_address": client_address},
                event_id,
            )
            raise AccountAuthError("INVALID_CREDENTIALS")
        if user["status"] == "BLOCKED":
            self._denied_status(user, "BLOCKED", client_address, user_agent)
        if user["status"] == "DISABLED":
            self._denied_status(user, "DISABLED", client_address, user_agent)
        session, cookie = self.create_session(user, "PASSWORD", client_address, user_agent)
        with self._rate_lock:
            self._password_failures.pop(rate_key, None)
        self.repository.record_login_success(int(str(user["id"])), "PASSWORD")
        event_id = self.repository.add_access_event(
            {
                "user_id": int(str(user["id"])),
                "method": "PASSWORD",
                "result": "SUCCESS",
                "reason": "AUTHENTICATED",
                "session_id": session.id,
                "client_address": client_address,
                "user_agent": user_agent,
            }
        )
        user_id = int(str(user["id"]))
        self.repository.add_audit("PASSWORD_LOGIN_SUCCESS", user_id, "SUCCESS", {"access_event_id": event_id})
        self.repository.add_notification(user_id, None, "LOGIN", "PASSWORD_LOGIN_SUCCESS")
        return session, cookie

    def _denied_status(self, user: dict[str, object], result: str, client_address: str, user_agent: str) -> None:
        event_id = self.repository.add_access_event(
            {
                "user_id": int(str(user["id"])),
                "method": "PASSWORD",
                "result": result,
                "reason": f"ACCOUNT_{result}",
                "client_address": client_address,
                "user_agent": user_agent,
            }
        )
        self.repository.add_security_event(f"{result}_USER_ATTEMPT", "WARNING", int(str(user["id"])), {}, event_id)
        raise AccountAuthError(f"ACCOUNT_{result}")

    def create_session(
        self, user: dict[str, object], method: str, client_address: str = "", user_agent: str = ""
    ) -> tuple[AccountSession, str]:
        secret = self.settings.session_secret.get_secret_value()
        if len(secret) < 32:
            raise AccountAuthError("AUTH_NOT_CONFIGURED")
        now = datetime.now(UTC)
        session_id = secrets.token_urlsafe(24)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = now + timedelta(seconds=self.settings.account_session_ttl_seconds)
        idle_expires = now + timedelta(seconds=self.settings.account_session_idle_seconds)
        self.repository.create_account_session(
            {
                "id": session_id,
                "user_id": int(str(user["id"])),
                "token_hash": self._token_hash(token),
                "csrf_token": csrf,
                "created_at": now.isoformat(),
                "expires_at": expires.isoformat(),
                "idle_expires_at": idle_expires.isoformat(),
                "last_seen_at": now.isoformat(),
                "login_method": method,
                "user_agent": user_agent[:300],
                "client_address": client_address[:100],
            }
        )
        session = AccountSession(
            session_id,
            int(str(user["id"])),
            str(user["login"]),
            str(user["full_name"]),
            str(user["role"]),
            str(user["status"]),
            csrf,
            method,
            bool(user["must_change_password"]),
        )
        unsigned = f"{session_id}.{token}"
        signature = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        return session, f"{unsigned}.{signature}"

    def get_session(self, cookie: str | None) -> AccountSession | None:
        if not cookie:
            return None
        parts = cookie.split(".")
        if len(parts) != 3 or len(self.settings.session_secret.get_secret_value()) < 32:
            return None
        session_id, token, signature = parts
        unsigned = f"{session_id}.{token}"
        expected = hmac.new(
            self.settings.session_secret.get_secret_value().encode(), unsigned.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        record = self.repository.get_account_session(session_id)
        if (
            not record
            or record["revoked_at"]
            or not hmac.compare_digest(str(record["token_hash"]), self._token_hash(token))
        ):
            return None
        now = datetime.now(UTC)
        if (
            datetime.fromisoformat(str(record["expires_at"])) <= now
            or datetime.fromisoformat(str(record["idle_expires_at"])) <= now
        ):
            self.repository.revoke_session(session_id, "EXPIRED")
            return None
        if record["status"] != "ACTIVE":
            self.repository.revoke_session(session_id, "ACCOUNT_NOT_ACTIVE")
            return None
        idle = now + timedelta(seconds=self.settings.account_session_idle_seconds)
        self.repository.touch_account_session(session_id, now.isoformat(), idle.isoformat())
        return AccountSession(
            session_id,
            int(record["user_id"]),
            str(record["login"]),
            str(record["full_name"]),
            str(record["role"]),
            str(record["status"]),
            str(record["csrf_token"]),
            str(record["login_method"]),
            bool(record["must_change_password"]),
        )

    def change_password(self, session: AccountSession, current_password: str, new_password: str) -> None:
        user = self.repository.get_user_by_id(session.user_id)
        if not user or not self.verify_password(user.get("password_hash"), current_password):
            raise AccountAuthError("CURRENT_PASSWORD_INVALID")
        password_hash = self.hash_password(new_password)
        self.repository.set_password_hash(session.user_id, password_hash, False, "PASSWORD_CHANGED")
        self.repository.revoke_user_sessions(session.user_id, "PASSWORD_CHANGED", except_session_id=session.id)
        self.repository.add_security_event("PASSWORD_CHANGED", "INFO", session.user_id, {})
        self.repository.add_notification(session.user_id, None, "PASSWORD_CHANGED", "PASSWORD_CHANGED")

    def admin_reset_password(
        self,
        user_id: int,
        password: str | None = None,
        *,
        must_change_password: bool = True,
        reason: str,
        admin_id: int,
        administrator: str,
    ) -> str | None:
        temporary = password or self._temporary_password()
        password_hash = self.hash_password(temporary)
        mode = "TEMPORARY" if must_change_password else "PERMANENT"
        source_audit_event_id = self.repository.set_password_hash(
            user_id,
            password_hash,
            must_change_password,
            "PASSWORD_RESET_BY_ADMIN",
            {"reason": reason, "admin_id": admin_id, "administrator": administrator, "mode": mode},
        )
        self.repository.revoke_user_sessions(user_id, "PASSWORD_RESET_BY_ADMIN")
        self.repository.add_security_event(
            "PASSWORD_RESET",
            "WARNING",
            user_id,
            {"method": "ADMIN", "mode": mode, "source_audit_event_id": source_audit_event_id},
        )
        self.repository.add_notification(user_id, None, "PASSWORD_RESET", "PASSWORD_RESET_BY_ADMIN")
        return temporary if password is None else None

    def issue_reset_authorization(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        with self.repository.db.write() as conn:
            conn.execute(
                """INSERT INTO password_reset_authorizations(id,user_id,token_hash,created_at,expires_at)
                   VALUES(?,?,?,?,?)""",
                (
                    secrets.token_urlsafe(18),
                    user_id,
                    self._token_hash(token),
                    now.isoformat(),
                    (now + timedelta(seconds=self.settings.recovery_authorization_ttl_seconds)).isoformat(),
                ),
            )
        return token

    def consume_reset_authorization(self, token: str, new_password: str) -> None:
        token_hash = self._token_hash(token)
        now = datetime.now(UTC)
        with self.repository.db.write() as conn:
            row = conn.execute(
                """SELECT * FROM password_reset_authorizations
                   WHERE token_hash=? AND used_at IS NULL""",
                (token_hash,),
            ).fetchone()
            if not row or datetime.fromisoformat(str(row["expires_at"])) <= now:
                raise AccountAuthError("RESET_AUTHORIZATION_INVALID")
            password_hash = self.hash_password(new_password)
            conn.execute("UPDATE password_reset_authorizations SET used_at=? WHERE id=?", (now.isoformat(), row["id"]))
        user_id = int(row["user_id"])
        self.repository.set_password_hash(user_id, password_hash, False, "PASSWORD_SELF_SERVICE_RESET")
        self.repository.revoke_user_sessions(user_id, "PASSWORD_SELF_SERVICE_RESET")
        self.repository.add_security_event("PASSWORD_RESET", "INFO", user_id, {"method": "BIOMETRIC"})
        self.repository.add_notification(user_id, None, "PASSWORD_RESET", "PASSWORD_SELF_SERVICE_RESET")

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _temporary_password() -> str:
        return f"Bg3-{secrets.token_urlsafe(14)}-7"

    def generate_temporary_password(self) -> str:
        return self._temporary_password()

    def _rate_key(self, login: str, client_address: str) -> str:
        material = f"{client_address}|{login.casefold()}"
        secret = self.settings.session_secret.get_secret_value()
        return hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()

    def _password_rate_limited(self, key: str) -> bool:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=self.settings.password_login_window_seconds)
        with self._rate_lock:
            recent = [attempt for attempt in self._password_failures.get(key, []) if attempt >= cutoff]
            self._password_failures[key] = recent
            if len(recent) < self.settings.password_login_max_attempts:
                return False
            return (now - recent[-1]).total_seconds() < self.settings.password_login_cooldown_seconds

    def _record_password_failure(self, key: str, now: datetime) -> None:
        with self._rate_lock:
            self._password_failures.setdefault(key, []).append(now)
