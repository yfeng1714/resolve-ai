from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from resolveai.domain.passwords import hash_password, hash_token, verify_password
from resolveai.errors import APIError

SESSION_COOKIE = "resolveai_session"
CSRF_COOKIE = "resolveai_csrf"
CSRF_HEADER = "x-csrf-token"

_DUMMY_HASH: str | None = None


@dataclass(frozen=True)
class Actor:
    user_id: UUID
    tenant_id: UUID
    role: str
    display_name: str
    email: str
    session_id: UUID
    csrf_hash: str


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("resolveai-dummy-password")
    return _DUMMY_HASH


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    tenant_id: UUID
    role: str
    display_name: str
    email: str


def _uuid(value: object) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def authenticate(session: Session, email: str, password: str) -> Principal | None:
    row = session.execute(
        text(
            """
            SELECT user_id, tenant_id, password_hash, role, display_name, email
            FROM app.lookup_login(:email)
            """
        ),
        {"email": email},
    ).mappings().one_or_none()
    if row is None:
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, str(row["password_hash"])):
        return None
    return Principal(
        user_id=_uuid(row["user_id"]),
        tenant_id=_uuid(row["tenant_id"]),
        role=str(row["role"]),
        display_name=str(row["display_name"]),
        email=str(row["email"]),
    )


def actor_from_cookie(request: Request) -> Actor | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    auth = request.app.state.auth_factory()
    try:
        row = auth.execute(
            text(
                """
                SELECT session_id, user_id, tenant_id, role, display_name, email, csrf_hash
                FROM app.lookup_session(:token_hash)
                """
            ),
            {"token_hash": hash_token(token)},
        ).mappings().one_or_none()
    finally:
        auth.close()
    if row is None:
        return None
    return Actor(
        user_id=_uuid(row["user_id"]),
        tenant_id=_uuid(row["tenant_id"]),
        role=str(row["role"]),
        display_name=str(row["display_name"]),
        email=str(row["email"]),
        session_id=_uuid(row["session_id"]),
        csrf_hash=str(row["csrf_hash"]),
    )


def require_write_guards(request: Request, actor: Actor) -> None:
    origin = request.headers.get("origin", "")
    if origin != request.app.state.settings.web_origin:
        raise APIError(403, "csrf", "Origin was rejected")
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not header or header != cookie or hash_token(header) != actor.csrf_hash:
        raise APIError(403, "csrf", "CSRF token was rejected")


def session_lifetime(hours: int) -> timedelta:
    return timedelta(hours=hours)
