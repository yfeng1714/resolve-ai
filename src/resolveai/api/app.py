import secrets
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from resolveai.agent.provider import FakeProvider
from resolveai.agent.runner import InvestigationRunner
from resolveai.api.present import ticket_detail, ticket_summary
from resolveai.api.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Actor,
    actor_from_request,
    authenticate,
    require_write_guards,
    session_lifetime,
)
from resolveai.clock import utcnow
from resolveai.db.models import Run, Ticket, UserSession
from resolveai.db.session import make_engine, make_session_factory
from resolveai.domain.passwords import hash_token
from resolveai.errors import APIError
from resolveai.http import envelope, request_id_from
from resolveai.locale import get_locale, push_locale, reset_locale
from resolveai.retrieval.corpus import get_evidence
from resolveai.services.audit import audit
from resolveai.services.execution import (
    approve_proposal,
    execute_proposal,
    reconcile_action,
    reject_proposal,
    reject_reply,
    send_reply,
)
from resolveai.services.input import invalidate_pending_intent
from resolveai.services.replies import append_message
from resolveai.settings import LiveModelBlocked, Settings, load_settings, repo_root
from resolveai.tools.client import FAULT_HEADER, SimulatorClient, accepted_fault


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=4000)


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    payload_hash: str = Field(min_length=64, max_length=64)


class ReplyRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    body_hash: str = Field(min_length=64, max_length=64)


def _runtime_view(settings: Settings) -> dict[str, object]:
    """Nonsecret process facts. Credentials are never included."""
    return {
        "model_provider": settings.model_provider,
        "model_name": settings.model_name,
        "llm_budget_usd": str(settings.llm_budget_usd),
        "allow_live": settings.allow_live,
        "run_max_model_calls": settings.run_max_model_calls,
        "run_max_tool_calls": settings.run_max_tool_calls,
        "run_max_replans": settings.run_max_replans,
    }


def create_api_app(
    settings: Settings | None = None,
    live_api_key: str = "",
    ledger_path: Path | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="ResolveAI")
    app.state.settings = settings
    engine = make_engine(settings.api_database_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.allow_live = False
    app.state.provider_factory = FakeProvider
    if settings.allow_live:
        from resolveai.agent.live_models import OpenAILunaProvider
        from resolveai.agent.spend import SpendLedger

        if not live_api_key:
            raise LiveModelBlocked(
                "Live mode requires the owner API key to be passed in process memory"
            )
        ledger = SpendLedger(
            ledger_path or (repo_root() / "eval" / "results" / "luna-spend.json"),
            cap_micros=int(settings.llm_budget_usd * 1_000_000),
        )
        app.state.allow_live = True
        app.state.provider_factory = lambda: OpenAILunaProvider(ledger=ledger, api_key=live_api_key)
    app.state.tool_client_factory = SimulatorClient
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "X-CSRF-Token",
            "X-Request-Id",
            "X-ResolveAI-Locale",
            FAULT_HEADER,
        ],
    )

    @app.middleware("http")
    async def request_ids(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = request_id_from(request.headers.get("x-request-id"))
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.middleware("http")
    async def locales(request: Request, call_next):  # type: ignore[no-untyped-def]
        header = request.headers.get("x-resolveai-locale") or request.headers.get("accept-language")
        token = push_locale(header)
        try:
            response = await call_next(request)
            response.headers["Content-Language"] = get_locale()
            return response
        finally:
            reset_locale(token)

    def get_db(request: Request) -> Iterator[Session]:
        db = request.app.state.session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def current_actor(request: Request, db: Session = Depends(get_db)) -> Actor:
        return actor_from_request(request, db)

    def guarded_actor(request: Request, actor: Actor = Depends(current_actor)) -> Actor:
        require_write_guards(request, actor)
        return actor

    def _ticket(db: Session, actor: Actor, ticket_id: UUID) -> Ticket:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None or ticket.tenant_id != actor.tenant_id:
            raise APIError(404, "not_found", "Ticket was not found")
        return ticket

    def _detail(db: Session, actor: Actor, ticket_id: UUID, request: Request) -> Response:
        payload = ticket_detail(db, actor, ticket_id)
        payload["process"] = _runtime_view(settings)
        return envelope(
            data=payload,
            request_id=request.state.request_id,
        )

    @app.get("/health")
    def health(request: Request) -> Response:
        database = "ok"
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - do not leak the database URL
            database = "unavailable"
        code = 200 if database == "ok" else 503
        return envelope(
            data={
                "service": "api",
                "database": database,
                **_runtime_view(settings),
            },
            request_id=request.state.request_id,
            error_code=None if code == 200 else "unavailable",
            status_code=code,
        )

    @app.post("/sessions")
    def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> Response:
        if request.headers.get("origin", "") != settings.web_origin:
            raise APIError(403, "csrf", "Origin was rejected")
        found = authenticate(db, body.email, body.password)
        if found is None:
            raise APIError(401, "invalid_credentials", "Sign-in failed")
        user, membership = found
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = utcnow()
        db.add(
            UserSession(
                tenant_id=user.tenant_id,
                user_id=user.id,
                token_hash=hash_token(token),
                csrf_hash=hash_token(csrf),
                expires_at=now + session_lifetime(settings.session_ttl_hours),
                created_at=now,
            )
        )
        audit(
            db,
            tenant_id=user.tenant_id,
            event_type="login",
            actor_user_id=user.id,
            payload={"role": membership.role},
        )
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            raise APIError(503, "unavailable", "Sign-in could not be saved") from exc
        response = envelope(
            data={
                "email": user.email,
                "display_name": user.display_name,
                "role": membership.role,
            },
            request_id=request.state.request_id,
        )
        max_age = settings.session_ttl_hours * 3600
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=max_age,
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            httponly=False,
            samesite="lax",
            secure=False,
            max_age=max_age,
            path="/",
        )
        return response

    @app.get("/sessions/current")
    def current(request: Request, actor: Actor = Depends(current_actor)) -> Response:
        return envelope(
            data={"email": actor.email, "display_name": actor.display_name, "role": actor.role},
            request_id=request.state.request_id,
        )

    @app.delete("/sessions/current")
    def logout(
        request: Request,
        actor: Actor = Depends(guarded_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        row = db.get(UserSession, actor.session_id)
        if row is not None:
            db.delete(row)
        audit(
            db,
            tenant_id=actor.tenant_id,
            event_type="logout",
            actor_user_id=actor.user_id,
            payload={},
        )
        response = envelope(data={"signed_out": True}, request_id=request.state.request_id)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return response

    @app.get("/tickets")
    def list_tickets(
        request: Request,
        actor: Actor = Depends(current_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        rows = db.scalars(
            select(Ticket)
            .where(Ticket.tenant_id == actor.tenant_id)
            .order_by(Ticket.updated_at.desc())
        ).all()
        return envelope(
            data={"tickets": [ticket_summary(row) for row in rows]},
            request_id=request.state.request_id,
        )

    @app.get("/tickets/{ticket_id}")
    def get_ticket(
        ticket_id: UUID,
        request: Request,
        actor: Actor = Depends(current_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        return _detail(db, actor, ticket_id, request)

    @app.get("/evidence/{evidence_id}")
    def read_evidence(
        evidence_id: UUID,
        request: Request,
        actor: Actor = Depends(current_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        evidence = get_evidence(
            db,
            tenant_id=actor.tenant_id,
            role=actor.role,
            evidence_id=evidence_id,
        )
        if evidence is None:
            raise APIError(404, "not_found", "Evidence was not found")
        return envelope(data=evidence, request_id=request.state.request_id)

    @app.post("/tickets/{ticket_id}/messages")
    def post_message(
        ticket_id: UUID,
        body: MessageRequest,
        request: Request,
        actor: Actor = Depends(guarded_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        if actor.role != "support":
            raise APIError(403, "denied", "Messages require the support role")
        ticket = db.scalar(
            select(Ticket)
            .where(Ticket.id == ticket_id, Ticket.tenant_id == actor.tenant_id)
            .with_for_update()
        )
        if ticket is None:
            raise APIError(404, "not_found", "Ticket was not found")
        append_message(db, ticket=ticket, author_kind="operator", body=body.body)
        invalidate_pending_intent(db, ticket)
        ticket.updated_at = utcnow()
        return _detail(db, actor, ticket_id, request)

    @app.post("/tickets/{ticket_id}/runs")
    def start_run(
        ticket_id: UUID,
        request: Request,
        actor: Actor = Depends(guarded_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        ticket = _ticket(db, actor, ticket_id)
        tools = request.app.state.tool_client_factory(request.app.state.settings)
        try:
            runner = InvestigationRunner(
                session=db,
                settings=request.app.state.settings,
                tools=tools,
                provider=request.app.state.provider_factory(),
                actor=actor,
                ticket=ticket,
            )
            runner.start(allow_live=bool(request.app.state.allow_live))
        finally:
            tools.close()
        return _detail(db, actor, ticket_id, request)

    @app.get("/runs/{run_id}")
    def get_run(
        run_id: UUID,
        request: Request,
        actor: Actor = Depends(current_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        run = db.get(Run, run_id)
        if run is None or run.tenant_id != actor.tenant_id:
            raise APIError(404, "not_found", "Run was not found")
        detail = ticket_detail(db, actor, run.ticket_id)
        run_payload = next(item for item in detail["runs"] if item["id"] == str(run.id))
        return envelope(
            data={
                "run": run_payload,
                "proposals": [
                    item for item in detail["proposals"] if item["run_id"] == str(run.id)
                ],
                "actions": detail["actions"],
                "replies": [item for item in detail["replies"] if item["id"]],
            },
            request_id=request.state.request_id,
        )

    @app.post("/proposals/{proposal_id}/approve")
    def approve(
        proposal_id: UUID,
        body: RevisionRequest,
        request: Request,
        actor: Actor = Depends(guarded_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        proposal = approve_proposal(
            db,
            actor=actor,
            proposal_id=proposal_id,
            revision=body.revision,
            payload_hash=body.payload_hash,
        )
        return _detail(db, actor, proposal.ticket_id, request)

    @app.post("/proposals/{proposal_id}/reject")
    def reject(
        proposal_id: UUID,
        body: RevisionRequest,
        request: Request,
        actor: Actor = Depends(guarded_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        tools = request.app.state.tool_client_factory(request.app.state.settings)
        try:
            proposal = reject_proposal(
                db,
                actor=actor,
                proposal_id=proposal_id,
                revision=body.revision,
                payload_hash=body.payload_hash,
                tools=tools,
            )
        finally:
            tools.close()
        return _detail(db, actor, proposal.ticket_id, request)

    @app.post("/proposals/{proposal_id}/execute")
    def execute(
        proposal_id: UUID,
        body: RevisionRequest,
        request: Request,
        actor: Actor = Depends(guarded_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        from resolveai.db.models import Proposal

        proposal = db.get(Proposal, proposal_id)
        if proposal is None or proposal.tenant_id != actor.tenant_id:
            raise APIError(404, "not_found", "Proposal was not found")
        fault = accepted_fault(request.app.state.settings, request.headers.get(FAULT_HEADER))
        tools = request.app.state.tool_client_factory(request.app.state.settings)
        try:
            execute_proposal(
                db,
                actor=actor,
                proposal_id=proposal_id,
                revision=body.revision,
                payload_hash=body.payload_hash,
                tools=tools,
                fault=fault,
            )
        finally:
            tools.close()
        return _detail(db, actor, proposal.ticket_id, request)

    @app.post("/actions/{action_id}/reconcile")
    def reconcile(
        action_id: str,
        request: Request,
        actor: Actor = Depends(guarded_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        tools = request.app.state.tool_client_factory(request.app.state.settings)
        try:
            ledger = reconcile_action(db, actor=actor, action_id=action_id, tools=tools)
        finally:
            tools.close()
        if ledger.proposal_id is None:
            raise APIError(404, "not_found", "Action was not found")
        from resolveai.db.models import Proposal

        proposal = db.get(Proposal, ledger.proposal_id)
        if proposal is None:
            raise APIError(404, "not_found", "Action was not found")
        return _detail(db, actor, proposal.ticket_id, request)

    @app.post("/reply-drafts/{reply_id}/approve-and-send")
    def approve_and_send(
        reply_id: UUID,
        body: ReplyRevisionRequest,
        request: Request,
        actor: Actor = Depends(guarded_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        draft = send_reply(
            db,
            actor=actor,
            reply_id=reply_id,
            revision=body.revision,
            body_hash_value=body.body_hash,
        )
        return _detail(db, actor, draft.ticket_id, request)

    @app.post("/reply-drafts/{reply_id}/reject")
    def reject_draft(
        reply_id: UUID,
        body: ReplyRevisionRequest,
        request: Request,
        actor: Actor = Depends(guarded_actor),
        db: Session = Depends(get_db),
    ) -> Response:
        from resolveai.db.models import ReplyDraft

        existing = db.get(ReplyDraft, reply_id)
        if existing is None or existing.tenant_id != actor.tenant_id:
            raise APIError(404, "not_found", "Reply draft was not found")
        reject_reply(
            db,
            actor=actor,
            reply_id=reply_id,
            revision=body.revision,
            body_hash_value=body.body_hash,
        )
        return _detail(db, actor, existing.ticket_id, request)

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError) -> Response:
        return envelope(
            data=None,
            request_id=getattr(request.state, "request_id", "unknown"),
            error_code=exc.error_code,
            message=exc.message,
            retryable=exc.retryable,
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, _exc: RequestValidationError) -> Response:
        return envelope(
            data=None,
            request_id=getattr(request.state, "request_id", "unknown"),
            error_code="validation",
            message="Request was rejected",
            status_code=422,
        )

    return app


app = create_api_app()
