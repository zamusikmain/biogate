"""Dashboard counters."""

from typing import Any

from app.services.repository._base import RepositoryBase


class StatsRepository(RepositoryBase):
    def admin_stats(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT
                  (SELECT COUNT(*) FROM users) AS total_users,
                  (SELECT COUNT(*) FROM biometric_templates) AS enrolled_users,
                  SUM(CASE WHEN date(timestamp)=date('now') THEN 1 ELSE 0 END) AS verifications_today,
                  SUM(CASE WHEN result='VERIFIED' THEN 1 ELSE 0 END) AS successful_verifications,
                  SUM(CASE WHEN result='REJECTED' THEN 1 ELSE 0 END) AS rejected_verifications,
                  AVG(inference_time_ms) AS average_inference_time_ms,
                  AVG(CASE WHEN result='VERIFIED' THEN similarity_score END) AS average_success_similarity,
                  SUM(CASE WHEN reason_code LIKE 'LIVENESS%'
                      OR reason_code='CHALLENGE_TIMEOUT' THEN 1 ELSE 0 END) AS liveness_failures,
                  SUM(CASE WHEN reason_code IN (
                      'IMAGE_BLURRY','BAD_LIGHTING','FACE_TOO_SMALL'
                  ) THEN 1 ELSE 0 END) AS quality_failures
                  FROM verification_attempts"""
            ).fetchone()
        return dict(row) if row else {}

    def stats(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("""
                SELECT (SELECT COUNT(*) FROM biometric_templates) AS enrolled_users,
                       COUNT(*) AS total_verifications,
                       SUM(CASE WHEN result='VERIFIED' THEN 1 ELSE 0 END) AS successful_verifications,
                       SUM(CASE WHEN result='REJECTED' THEN 1 ELSE 0 END) AS rejected_verifications,
                       AVG(inference_time_ms) AS average_inference_time_ms
                FROM verification_attempts
            """).fetchone()
        return dict(row) if row else {}

    def v3_admin_stats(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT
                (SELECT COUNT(*) FROM users) total_users,
                (SELECT COUNT(*) FROM users WHERE status='ACTIVE') active_users,
                (SELECT COUNT(*) FROM users WHERE status='BLOCKED') blocked_users,
                (SELECT COUNT(*) FROM users WHERE status='DISABLED') disabled_users,
                (SELECT COUNT(*) FROM biometric_templates) enrolled_users,
                (SELECT COUNT(*) FROM access_events
                    WHERE date(timestamp)=date('now') AND result='SUCCESS') successful_today,
                (SELECT COUNT(*) FROM access_events
                    WHERE date(timestamp)=date('now') AND result<>'SUCCESS') denied_today,
                (SELECT COUNT(*) FROM access_events
                    WHERE date(timestamp)=date('now') AND result='UNKNOWN') unknown_today,
                (SELECT COUNT(*) FROM biometric_requests WHERE status='PENDING_REVIEW') pending_biometric_requests,
                (SELECT COUNT(*) FROM security_events WHERE reviewed_at IS NULL) security_alerts"""
            ).fetchone()
        return dict(row) if row else {}
