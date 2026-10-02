import json
from typing import Any

SENSITIVE_EVENT_KEYS = (
    "password",
    "secret",
    "token",
    "hash",
    "embedding",
    "template",
    "authorization",
    "session",
)


def safe_event_metadata(value: Any) -> Any:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value
    if isinstance(value, dict):
        return {
            key: safe_event_metadata(item)
            for key, item in value.items()
            if key == "must_change_password"
            or not any(fragment in key.casefold() for fragment in SENSITIVE_EVENT_KEYS)
        }
    if isinstance(value, list):
        return [safe_event_metadata(item) for item in value]
    return value


def safe_event_row(row: dict[str, Any]) -> dict[str, Any]:
    safe = {
        key: value
        for key, value in row.items()
        if not any(fragment in key.casefold() for fragment in SENSITIVE_EVENT_KEYS)
    }
    for key in ("metadata", "candidate_metadata", "action_metadata"):
        if key in safe:
            safe[key] = safe_event_metadata(safe[key])
    return safe
