"""Append-only audit trail (service-role writes only; no user-facing access)."""
import ipaddress
import json
import logging
from uuid import UUID

from sqlalchemy import text

from app.db.rls import service_session

logger = logging.getLogger(__name__)


def valid_ip(ip: str | None) -> str | None:
    """The client 'host' is not always an IP (proxies, unix sockets, test clients); store NULL then."""
    if not ip:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def audit(actor: UUID | None, action: str, *, library_id: UUID | None = None,
          target: str | None = None, ip: str | None = None, meta: dict | None = None) -> None:
    """Record an event. Called after the action has committed, so a failure here is logged loudly
    but never turns a completed action into an error response."""
    try:
        with service_session() as s:
            s.execute(
                text("INSERT INTO audit_log (actor, library_id, action, target, ip, meta) "
                     "VALUES (:actor, :lib, :action, :target, CAST(:ip AS inet), CAST(:meta AS jsonb))"),
                {"actor": actor, "lib": library_id, "action": action, "target": target,
                 "ip": valid_ip(ip), "meta": json.dumps(meta) if meta else None},
            )
    except Exception:
        logger.exception("audit write failed: action=%s library=%s", action, library_id)
