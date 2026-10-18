"""HTTP client for the billing simulator.

The application never writes simulator tables with its own database role.
Unknown write responses are not marked retryable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx

from resolveai.clock import utcnow
from resolveai.domain.hashing import refund_payload
from resolveai.settings import Settings

FAULT_HEADER = "X-ResolveAI-Test-Fault"
COMMIT_THEN_FAIL = "commit_then_fail"


@dataclass(frozen=True)
class ToolResult:
    status: str
    data: dict[str, Any]
    error_code: str | None
    retryable: bool
    operation_id: str | None
    resource_version: int | None
    observed_at: str
    request_id: str


def accepted_fault(settings: Settings, requested: str | None) -> str | None:
    if not settings.simulator_allow_faults:
        return None
    if requested == COMMIT_THEN_FAIL:
        return requested
    return None


class SimulatorClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.Client(base_url=settings.simulator_base_url, timeout=5.0)

    def close(self) -> None:
        self._client.close()

    def get_customer_account(self, account_id: UUID) -> ToolResult:
        return self._read("GET", f"/v1/accounts/{account_id}", account_id)

    def list_invoices(self, account_id: UUID) -> ToolResult:
        return self._read("GET", f"/v1/accounts/{account_id}/invoices", account_id)

    def list_payments(self, account_id: UUID) -> ToolResult:
        return self._read("GET", f"/v1/accounts/{account_id}/payments", account_id)

    def list_refunds(self, account_id: UUID) -> ToolResult:
        return self._read("GET", f"/v1/accounts/{account_id}/refunds", account_id)

    def get_operation(self, account_id: UUID, action_id: str) -> ToolResult:
        return self._read("GET", f"/v1/operations/{action_id}", account_id)

    def create_refund(
        self,
        *,
        account_id: UUID,
        payment_id: UUID,
        event_id: str,
        amount_cents: int,
        action_id: str,
        payload_hash: str,
        expected_payment_version: int,
        policy_version: str,
        fault: str | None = None,
    ) -> ToolResult:
        body = refund_payload(
            action_id=action_id,
            account_id=account_id,
            payment_id=payment_id,
            event_id=event_id,
            amount_cents=amount_cents,
            policy_version=policy_version,
        )
        body["payload_hash"] = payload_hash
        body["expected_payment_version"] = expected_payment_version
        return self._write(
            "POST",
            "/v1/refunds",
            account_id,
            json_body=body,
            fault=fault,
        )

    def _headers(self, account_id: UUID, *, fault: str | None, request_id: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.simulator_token}",
            "X-Scope-Account-Id": str(account_id),
            "X-Request-Id": request_id,
            "Accept": "application/json",
        }
        allowed = accepted_fault(self.settings, fault)
        if allowed:
            headers[FAULT_HEADER] = allowed
        return headers

    def _read(self, method: str, url: str, account_id: UUID) -> ToolResult:
        return self._send(method, url, account_id, write=False, json_body=None, fault=None)

    def _write(
        self,
        method: str,
        url: str,
        account_id: UUID,
        *,
        json_body: dict[str, Any],
        fault: str | None,
    ) -> ToolResult:
        return self._send(method, url, account_id, write=True, json_body=json_body, fault=fault)

    def _send(
        self,
        method: str,
        url: str,
        account_id: UUID,
        *,
        write: bool,
        json_body: dict[str, Any] | None,
        fault: str | None,
    ) -> ToolResult:
        request_id = uuid4().hex
        try:
            response = self._client.request(
                method,
                url,
                headers=self._headers(account_id, fault=fault, request_id=request_id),
                json=json_body,
            )
        except httpx.TransportError:
            return ToolResult(
                status="error",
                data={},
                error_code="unknown" if write else "unavailable",
                retryable=not write,
                operation_id=None,
                resource_version=None,
                observed_at=utcnow().isoformat(),
                request_id=request_id,
            )
        return _parse(response, write=write, fallback_request_id=request_id)


def _parse(response: httpx.Response, *, write: bool, fallback_request_id: str) -> ToolResult:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    error_code = body.get("error_code")
    if response.status_code >= 500:
        error_code = error_code or "unknown"
    ok = response.status_code < 400 and body.get("status") == "ok" and error_code is None
    retryable = (not write) and response.status_code in {502, 503, 504}
    return ToolResult(
        status="ok" if ok else "error",
        data=data,
        error_code=None if ok else (error_code or "simulator_error"),
        retryable=retryable,
        operation_id=body.get("operation_id") or data.get("operation_id"),
        resource_version=body.get("resource_version") or data.get("resource_version"),
        observed_at=str(body.get("observed_at") or utcnow().isoformat()),
        request_id=str(body.get("request_id") or fallback_request_id),
    )
