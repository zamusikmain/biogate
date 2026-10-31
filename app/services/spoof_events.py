import threading
import time
from typing import Any

from app.services.biometrics.anti_spoofing import SpoofDetectedError
from app.services.repository import Repository

# Live capture previews post a frame every second or so; one event per minute per person and
# context is enough for the security log while a photo is held up to the camera.
PREVIEW_EVENT_COOLDOWN_SECONDS = 60.0


class SpoofEventRecorder:
    """Writes SPOOF_DETECTED to the security log and notifies administrators."""

    def __init__(self, repository: Repository, cooldown_seconds: float = PREVIEW_EVENT_COOLDOWN_SECONDS):
        self.repository = repository
        self.cooldown_seconds = cooldown_seconds
        self._last_preview_event: dict[tuple[str, int | None], float] = {}
        self._lock = threading.Lock()

    def record(
        self,
        error: SpoofDetectedError,
        context: str,
        user_id: int | None = None,
        access_event_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        preview: bool = False,
    ) -> int | None:
        """Return the security event id, or None when a preview event was throttled."""
        if preview and not self._preview_allowed(context, user_id):
            return None
        details = {
            "context": context,
            "live_score": round(error.live_score, 4),
            "threshold": error.threshold,
            **(metadata or {}),
        }
        event_id = self.repository.add_security_event("SPOOF_DETECTED", "WARNING", user_id, details, access_event_id)
        self.repository.add_notification(
            None,
            "ADMIN",
            "SPOOF_DETECTED",
            "SPOOF_DETECTED",
            {"access_event_id": access_event_id} if access_event_id is not None else None,
        )
        return event_id

    def _preview_allowed(self, context: str, user_id: int | None) -> bool:
        key = (context, user_id)
        now = time.monotonic()
        with self._lock:
            last = self._last_preview_event.get(key)
            if last is not None and now - last < self.cooldown_seconds:
                return False
            self._last_preview_event[key] = now
            return True
