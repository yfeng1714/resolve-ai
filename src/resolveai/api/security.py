from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import Membership, UserAccount, UserSession
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


def authenticate(
    session: Session, email: str, password: str
) -> tuple[UserAccount, Membership] | None:
    normalized = email.strip().lower()
    user = session.scalar(select(UserAccount).where(UserAccount.email == normalized))
    if user is None:
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, user.password_hash):
        return None
    membership = session.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.tenant_id == user.tenant_id,
        )
    )
    if membership is None:
        return None
    return user, membership


def actor_from_request(request: Request, session: Session) -> Actor:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise APIError(401, "unauthenticated", "Sign in required")
    row = session.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if row is None or row.expires_at <= utcnow():
        raise APIError(401, "unauthenticated", "Sign in required")
    user = session.get(UserAccount, row.user_id)
    membership = session.scalar(
        select(Membership).where(
            Membership.user_id == row.user_id,
            Membership.tenant_id == row.tenant_id,
        )
    )
    if user is None or membership is None:
        raise APIError(401, "unauthenticated", "Sign in required")
    return Actor(
        user_id=user.id,
        tenant_id=row.tenant_id,
        role=membership.role,
        display_name=user.display_name,
        email=user.email,
        session_id=row.id,
        csrf_hash=row.csrf_hash,
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
