import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import TokenKey, User

password_hasher = PasswordHasher()

INGEST_SCOPE = "documents:write"


def password_hash(value: str) -> str:
    return password_hasher.hash(value)


def verify_password(value: str, encoded: str) -> bool:
    try:
        return password_hasher.verify(encoded, value)
    except Exception:
        return False


def create_session_token(user: User, kind: str, ttl: timedelta) -> str:
    settings = get_settings()
    payload = {"sub": user.id, "kind": kind, "exp": datetime.now(timezone.utc) + ttl}
    return jwt.encode(payload, settings.app_secret_key, algorithm="HS256")


def create_token_secret() -> str:
    return get_settings().token_prefix + secrets.token_urlsafe(43)


def token_digest(secret: str) -> str:
    key = get_settings().token_hash_secret.encode()
    return hmac.new(key, secret.encode(), hashlib.sha256).hexdigest()


def error(code: str, message: str, http_status: int = 401):
    raise HTTPException(http_status, {"code": code, "message": message, "retryable": False})


def current_admin(request: Request, db: Session = Depends(get_db)) -> User:
    raw = request.cookies.get("skip_access")
    if not raw:
        error("AUTH_TOKEN_MISSING", "Authentication required")
    try:
        payload = jwt.decode(raw, get_settings().app_secret_key, algorithms=["HS256"])
        if payload.get("kind") != "access":
            raise jwt.InvalidTokenError()
    except jwt.PyJWTError:
        error("AUTH_TOKEN_INVALID", "Invalid session")
    user = db.get(User, payload["sub"])
    if not user or not user.is_active:
        error("AUTH_TOKEN_INVALID", "Invalid session")
    return user


def refresh_admin(request: Request, db: Session = Depends(get_db)) -> User:
    raw = request.cookies.get("skip_refresh")
    if not raw:
        error("AUTH_REFRESH_MISSING", "Refresh session required")
    try:
        payload = jwt.decode(raw, get_settings().app_secret_key, algorithms=["HS256"])
        if payload.get("kind") != "refresh":
            raise jwt.InvalidTokenError()
    except jwt.PyJWTError:
        error("AUTH_REFRESH_INVALID", "Refresh session is invalid")
    user = db.get(User, payload["sub"])
    if not user or not user.is_active:
        error("AUTH_REFRESH_INVALID", "Refresh session is invalid")
    return user


def bearer_token(request: Request, db: Session = Depends(get_db)) -> TokenKey:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        error("AUTH_TOKEN_MISSING", "Bearer token required")
    secret = authorization[7:]
    digest = token_digest(secret)
    token = db.query(TokenKey).filter(TokenKey.token_hash == digest).first()
    if not token or not hmac.compare_digest(digest, token.token_hash):
        error("AUTH_TOKEN_INVALID", "Invalid token")
    if token.status == "revoked" or token.revoked_at:
        error("AUTH_TOKEN_REVOKED", "Token is revoked")
    if token.status != "active":
        error("AUTH_TOKEN_INVALID", "Token is inactive")
    if token.expires_at and token.expires_at < datetime.utcnow():
        error("AUTH_TOKEN_EXPIRED", "Token expired")
    token.last_used_at = datetime.utcnow()
    db.commit()
    return token


def authorize(token: TokenKey, tool: str, kb_ids: list[str]) -> None:
    # Membership is positive: an empty allowed_tools list grants nothing. It used
    # to mean "every tool", which made any capability checked here reachable by
    # every legacy credential. Migration 0023 wrote the explicit tool list onto
    # those rows so their authority is unchanged.
    if tool not in (token.allowed_tools or []):
        error("AUTH_TOOL_NOT_ALLOWED", "Tool is not allowed", status.HTTP_403_FORBIDDEN)
    if token.allowed_knowledge_base_ids and not set(kb_ids).issubset(set(token.allowed_knowledge_base_ids)):
        error("AUTH_KNOWLEDGE_BASE_NOT_ALLOWED", "Knowledge base is not allowed", status.HTTP_403_FORBIDDEN)


def ingest_token(request: Request, db: Session = Depends(get_db)) -> TokenKey:
    """Authenticate a machine caller that is allowed to write documents.

    Ingestion never falls back to a wildcard on either axis: the scope must be
    granted explicitly, and the token must name the one Knowledge Base it may
    write to. This is a dedicated field, separate from allowed_knowledge_base_ids
    (the MCP read axis), so a token's read scope and write scope never have to
    match — create_token() already enforces this field is set whenever the scope
    is granted; the check here is defense in depth.
    """
    token = bearer_token(request, db)
    if INGEST_SCOPE not in (token.allowed_scopes or []):
        error("AUTH_SCOPE_NOT_ALLOWED", "Token is not allowed to ingest documents", status.HTTP_403_FORBIDDEN)
    if not token.allowed_ingest_knowledge_base_id:
        error("AUTH_KNOWLEDGE_BASE_NOT_ALLOWED", "Token has no Knowledge Base scope for ingestion", status.HTTP_403_FORBIDDEN)
    return token
