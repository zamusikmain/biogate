import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

RISK_LEVEL_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


@dataclass(frozen=True)
class RiskRule:
    reason_code: str
    event_types: frozenset[str]
    threshold: int
    window: timedelta
    level: str


RISK_RULES = (
    RiskRule(
        "MULTIPLE_FAILED_LOGINS",
        frozenset({"LOGIN_FAILED", "MULTIPLE_FAILED_PASSWORDS"}),
        3,
        timedelta(minutes=10),
        "MEDIUM",
    ),
    RiskRule(
        "MULTIPLE_FAILED_LOGINS",
        frozenset({"LOGIN_FAILED", "MULTIPLE_FAILED_PASSWORDS"}),
        5,
        timedelta(minutes=10),
        "HIGH",
    ),
    RiskRule("REPEATED_UNKNOWN_FACE", frozenset({"UNKNOWN_FACE"}), 2, timedelta(minutes=10), "MEDIUM"),
    RiskRule("REPEATED_UNKNOWN_FACE", frozenset({"UNKNOWN_FACE"}), 3, timedelta(minutes=10), "HIGH"),
    RiskRule(
        "REPEATED_BIOMETRIC_FAILURES",
        frozenset({"MULTIPLE_FAILED_FACE_ATTEMPTS", "LIVENESS_FAILED", "FACE_LOGIN_FAILED"}),
        2,
        timedelta(minutes=10),
        "MEDIUM",
    ),
    RiskRule(
        "REPEATED_BIOMETRIC_FAILURES",
        frozenset({"MULTIPLE_FAILED_FACE_ATTEMPTS", "LIVENESS_FAILED", "FACE_LOGIN_FAILED"}),
        3,
        timedelta(minutes=10),
        "HIGH",
    ),
    RiskRule(
        "RECOVERY_ABUSE",
        frozenset({"PASSWORD_RECOVERY_FAILED", "RECOVERY_FAILED", "RECOVERY_RATE_LIMITED"}),
        2,
        timedelta(minutes=15),
        "HIGH",
    ),
    RiskRule(
        "RECOVERY_ABUSE",
        frozenset({"PASSWORD_RECOVERY_FAILED", "RECOVERY_FAILED", "RECOVERY_RATE_LIMITED"}),
        3,
        timedelta(minutes=15),
        "CRITICAL",
    ),
    RiskRule("BLOCKED_ACCOUNT_ATTEMPTS", frozenset({"BLOCKED_USER_ATTEMPT"}), 1, timedelta(minutes=15), "HIGH"),
    RiskRule("DISABLED_ACCOUNT_ATTEMPTS", frozenset({"DISABLED_USER_ATTEMPT"}), 1, timedelta(minutes=15), "HIGH"),
    RiskRule("EXCESSIVE_PASSWORD_RESET", frozenset({"PASSWORD_RESET"}), 2, timedelta(minutes=30), "HIGH"),
    RiskRule("EXCESSIVE_PASSWORD_RESET", frozenset({"PASSWORD_RESET"}), 3, timedelta(minutes=30), "CRITICAL"),
)


class RiskEngine:
    """Deterministic risk classification from technical security events only."""

    def evaluate(self, current: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        anchor = datetime.fromisoformat(str(current["timestamp"]))
        matching: list[dict[str, Any]] = []
        for rule in RISK_RULES:
            cutoff = anchor - rule.window
            count = sum(
                1
                for event in events
                if event.get("event_type") in rule.event_types
                and self._same_subject(current, event)
                and cutoff <= datetime.fromisoformat(str(event["timestamp"])) <= anchor
            )
            if count >= rule.threshold:
                matching.append(
                    {
                        "risk_level": rule.level,
                        "reason_code": rule.reason_code,
                        "event_count": count,
                        "window_seconds": int(rule.window.total_seconds()),
                    }
                )
        if not matching:
            return {"risk_level": "LOW", "reason_code": None, "event_count": 0, "window_seconds": 0}
        return max(matching, key=lambda item: RISK_LEVEL_ORDER[str(item["risk_level"])])

    @staticmethod
    def _same_subject(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
        user_id = current.get("user_id")
        if user_id is not None:
            return bool(candidate.get("user_id") == user_id)
        if candidate.get("user_id") is not None:
            return False
        return RiskEngine._metadata(current).get("client_address") == RiskEngine._metadata(candidate).get(
            "client_address"
        )

    @staticmethod
    def _metadata(event: dict[str, Any]) -> dict[str, Any]:
        value = event.get("metadata", {})
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return {}
        return value if isinstance(value, dict) else {}


def annotate_risk(rows: list[dict[str, Any]], context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    engine = RiskEngine()
    return [{**row, "risk": engine.evaluate(row, context)} for row in rows]
