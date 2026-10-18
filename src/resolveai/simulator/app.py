from collections.abc import Iterator
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from resolveai.db.session import make_engine, make_session_factory
from resolveai.errors import APIError
from resolveai.http import envelope, request_id_from
from resolveai.settings import Settings, load_settings
from resolveai.simulator.service import (
    account_view,
    apply_refund,
    invoices_view,
    operation_view,
    payments_view,
    refunds_view,
)
from resolveai.tools.client import FAULT_HEADER, accepted_fault


class RefundCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["refund"]
    action_id: str = Field(min_length=8, max_length=80)
    account_id: UUID
    payment_id: UUID
    event_id: str = Field(min_length=1, max_length=80)
    amount_cents: int = Field(gt=0)
    currency: Literal["USD"]
    payload_hash: str = Field(min_length=64, max_length=64)
    expected_payment_version: int = Field(ge=1)
    policy_version: str = Field(min_length=1, max_length=80)


def create_simulator_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="ResolveAI Simulator")
    app.state.settings = settings
    engine = make_engine(settings.simulator_database_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    @app.middleware("http")
    async def request_ids(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = request_id_from(request.headers.get("x-request-id"))
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    def sessions() -> Iterator:
        session = app.state.session_factory()
        try:
            yield session
        finally:
            session.close()

    def scope_account(request: Request) -> UUID:
        authorization = request.headers.get("authorization", "")
        if authorization != f"Bearer {settings.simulator_token}":
            raise APIError(401, "denied", "Simulator credential was rejected")
        raw = request.headers.get("x-scope-account-id")
        if not raw:
            raise APIError(403, "denied", "Account scope is required")
        try:
            return UUID(raw)
        except ValueError as exc:
            raise APIError(403, "denied", "Account scope was rejected") from exc

    @app.get("/health")
    def health(request: Request):
        database = "ok"
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - health must not leak connection details
            database = "unavailable"
        code = 200 if database == "ok" else 503
        return envelope(
            data={"service": "simulator", "database": database},
            request_id=request.state.request_id,
            error_code=None if code == 200 else "unavailable",
            status_code=code,
        )

    @app.get("/v1/accounts/{account_id}")
    def read_account(account_id: UUID, request: Request):
        scoped = scope_account(request)
        if scoped != account_id:
            raise APIError(403, "denied", "Account scope does not match the resource")
        session = next(sessions())
        try:
            data = account_view(session, account_id)
        finally:
            session.close()
        return envelope(data=data, request_id=request.state.request_id)

    @app.get("/v1/accounts/{account_id}/invoices")
    def read_invoices(account_id: UUID, request: Request):
        return _scoped_read(request, account_id, invoices_view)

    @app.get("/v1/accounts/{account_id}/payments")
    def read_payments(account_id: UUID, request: Request):
        return _scoped_read(request, account_id, payments_view)

    @app.get("/v1/accounts/{account_id}/refunds")
    def read_refunds(account_id: UUID, request: Request):
        return _scoped_read(request, account_id, refunds_view)

    @app.get("/v1/operations/{action_id}")
    def read_operation(action_id: str, request: Request):
        scoped = scope_account(request)
        session = next(sessions())
        try:
            data = operation_view(session, scoped, action_id)
        finally:
            session.close()
        return envelope(
            data=data,
            request_id=request.state.request_id,
            operation_id=data.get("operation_id"),
        )

    @app.post("/v1/refunds")
    def create_refund(command: RefundCommand, request: Request):
        scoped = scope_account(request)
        if command.account_id != scoped:
            raise APIError(403, "denied", "Account scope does not match the refund")
        fault = accepted_fault(settings, request.headers.get(FAULT_HEADER.lower()))
        # Starlette header lookup is case-insensitive; also check the canonical name.
        if fault is None:
            fault = accepted_fault(settings, request.headers.get(FAULT_HEADER))
        session = app.state.session_factory()
        try:
            created, data = apply_refund(session, command)
            session.commit()
        except APIError:
            session.rollback()
            raise
        except IntegrityError as exc:
            session.rollback()
            raise APIError(
                422,
                "duplicate_business",
                "An open or settled refund already covers this event",
            ) from exc
        finally:
            session.close()
        if created and fault == "commit_then_fail":
            return envelope(
                data={"action_id": command.action_id},
                request_id=request.state.request_id,
                error_code="unknown",
                message="The refund response was lost after the business record committed",
                status_code=500,
                operation_id=data.get("operation_id"),
            )
        return envelope(
            data=data,
            request_id=request.state.request_id,
            operation_id=data.get("operation_id"),
            resource_version=data.get("resource_version"),
        )

    def _scoped_read(request: Request, account_id: UUID, reader):  # type: ignore[no-untyped-def]
        scoped = scope_account(request)
        if scoped != account_id:
            raise APIError(403, "denied", "Account scope does not match the resource")
        session = app.state.session_factory()
        try:
            data = reader(session, account_id)
        finally:
            session.close()
        return envelope(data=data, request_id=request.state.request_id)

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError):
        return envelope(
            data=None,
            request_id=getattr(request.state, "request_id", "unknown"),
            error_code=exc.error_code,
            message=exc.message,
            retryable=exc.retryable,
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, _exc: RequestValidationError):
        return envelope(
            data=None,
            request_id=getattr(request.state, "request_id", "unknown"),
            error_code="validation",
            message="Request was rejected",
            status_code=422,
        )

    return app


app = create_simulator_app()
