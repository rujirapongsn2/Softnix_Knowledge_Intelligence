from .models import AuditLog


def record_audit(db, action: str, actor_id: str | None = None, target_type: str | None = None,
                 target_id: str | None = None, metadata: dict | None = None) -> None:
    db.add(AuditLog(actor_user_id=actor_id, action=action, target_type=target_type,
                    target_id=target_id, metadata_json=metadata or {}))
