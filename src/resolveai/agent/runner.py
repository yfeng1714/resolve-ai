"""Runs the investigation graph until it reaches a human wait state."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from resolveai.agent.budget import admit_model_call
from resolveai.agent.graph import build_investigation_graph
from resolveai.agent.provider import (
    PERMITTED_TOOLS,
    FakeProvider,
    Provider,
    ProviderAction,
    RunSnapshot,
)
from resolveai.clock import utcnow
from resolveai.db.models import Message, Run, Ticket, ToolInvocation
from resolveai.domain.observations import parse_payments, parse_refunds
from resolveai.domain.policy import evaluate_refund
from resolveai.errors import APIError
from resolveai.locale import phrase, phrase_reason
from resolveai.retrieval.corpus import search_knowledge
from resolveai.services.audit import audit
from resolveai.services.input import latest_input_revision
from resolveai.services.proposals import create_refund_proposal
from resolveai.services.replies import append_message
from resolveai.settings import LiveModelBlocked, Settings, ensure_fake_provider
from resolveai.tools import ALL_TOOLS, READ_TOOLS
from resolveai.tools.client import ToolResult

_TERMINAL = {"completed", "handed_off", "failed", "cancelled"}


class InvestigationRunner:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        tools: Any,
        provider: Provider,
        actor: Any,
        ticket: Ticket,
    ) -> None:
        self.session = session
        self.settings = settings
        self.tools = tools
        self.provider = provider
        self.actor = actor
        self.ticket = ticket
        self.run: Run | None = None
        self.model_calls = 0
        self.tool_calls = 0
        self.replans = 0
        self.read_retries = 0
        self.pending_tool: str | None = None
        self.pending_args: dict[str, Any] = {}
        self.pending_reason = ""
        self.stop_reason = ""
        self.halted = False
        self.awaiting_customer = False
        self.investigated_revision = 0
        self._run_visible = False
        self._holding_intent = False
        self._preserved_cancellation = False

    def start(self, *, allow_live: bool = False) -> Run:
        if self.actor.role != "support":
            raise APIError(403, "denied", "Investigation requires the support role")
        try:
            if not allow_live:
                ensure_fake_provider(self.settings)
            elif isinstance(self.provider, FakeProvider):
                ensure_fake_provider(self.settings)
        except Exception as exc:
            raise APIError(403, "live_model_blocked", str(exc)) from exc
        if allow_live and isinstance(self.provider, FakeProvider):
            raise APIError(403, "live_model_blocked", "A live run requires a live provider")
        if not allow_live and getattr(self.provider, "billable", False):
            raise APIError(
                403,
                "live_model_blocked",
                "The API process only runs the fake provider",
            )
        active = self.session.scalar(
            select(Run.id).where(
                Run.ticket_id == self.ticket.id,
                Run.state.notin_(tuple(_TERMINAL)),
            )
        )
        if active is not None:
            raise APIError(409, "run_in_progress", "This ticket already has an open run")
        message_revision = latest_input_revision(self.session, self.ticket.id)
        provider_name = getattr(self.provider, "provider_name", None)
        provider_name = provider_name or self.settings.model_provider
        now = utcnow()
        run = Run(
            tenant_id=self.actor.tenant_id,
            ticket_id=self.ticket.id,
            state="analyzing",
            message_revision=message_revision or 0,
            observations=[],
            concise_reasons=[],
            provider=provider_name,
            prompt_version=self.provider.prompt_version,
            model_calls=0,
            tool_calls=0,
            replans=0,
            read_retries=0,
            budget_usd_micros=0,
            error_code=None,
            created_at=now,
            updated_at=now,
        )
        self.session.add(run)
        self.session.flush()
        self.run = run
        self.investigated_revision = message_revision or 0
        audit(
            self.session,
            tenant_id=self.actor.tenant_id,
            event_type="run_started",
            actor_user_id=self.actor.user_id,
            ticket_id=self.ticket.id,
            run_id=run.id,
            payload={"provider": run.provider, "prompt_version": run.prompt_version},
        )
        self._checkpoint()
        graph = build_investigation_graph(self)
        graph.invoke(
            {
                "route": "select",
                "model_calls": 0,
                "tool_calls": 0,
                "replans": 0,
                "read_retries": 0,
                "stop_reason": "",
            }
        )
        return run

    def select_action(self) -> ProviderAction:
        if self.halted:
            return ProviderAction(route="stop", reason=self.stop_reason or "stopped")
        if self.model_calls >= self.settings.run_max_model_calls:
            self._fail("budget_exhausted", phrase("model_limit"))
            return ProviderAction(route="stop", reason=self.stop_reason)
        if self.tool_calls >= self.settings.run_max_tool_calls:
            self._fail("budget_exhausted", phrase("tool_limit"))
            return ProviderAction(route="stop", reason=self.stop_reason)
        self._release_before_wait()
        started = utcnow()
        try:
            if self.run is not None and self.settings.model_provider != "fake":
                ledger = getattr(self.provider, "ledger", None)
                admit_model_call(
                    self.session,
                    ticket_id=self.ticket.id,
                    settings=self.settings,
                    estimate_micros=1,
                    spent_micros=None if ledger is None else ledger.total_micros(),
                    credential_present=bool(getattr(self.provider, "api_key", "")),
                )
            elif self.run is not None:
                admit_model_call(
                    self.session,
                    ticket_id=self.ticket.id,
                    settings=self.settings,
                    estimate_micros=0,
                )
            action = self.provider.next_action(self._snapshot())
        except LiveModelBlocked as exc:
            if not self._claim_current_intent():
                return ProviderAction(route="stop", reason=self.stop_reason)
            self._fail(getattr(exc, "code", "live_model_blocked"), str(exc))
            return ProviderAction(route="stop", reason=self.stop_reason)
        if not self._claim_current_intent():
            return ProviderAction(route="stop", reason=self.stop_reason)
        elapsed_ms = int((utcnow() - started).total_seconds() * 1000)
        self.model_calls += 1
        self._append_reason(action.reason)
        self.pending_reason = action.reason
        if self.run is not None:
            audit(
                self.session,
                tenant_id=self.actor.tenant_id,
                event_type="model_step",
                actor_user_id=self.actor.user_id,
                ticket_id=self.ticket.id,
                run_id=self.run.id,
                payload={
                    "provider": getattr(self.provider, "provider_name", None)
                    or self.settings.model_provider,
                    "prompt_version": self.provider.prompt_version,
                    "latency_ms": elapsed_ms,
                    "estimated_usd_micros": _provider_spend(self.provider),
                    "tool_name": action.tool_name,
                },
            )
        if action.route == "tool":
            self.pending_tool = action.tool_name
            self.pending_args = dict(action.arguments)
            if self.run is not None and self.run.state == "analyzing":
                self.run.state = "gathering_evidence"
        elif action.route == "stop":
            self._fail(action.error_code or "stopped", action.reason)
        self._checkpoint()
        return action

    def note_replan(self) -> str:
        if self.halted:
            return "stop"
        self.replans += 1
        self._append_reason(phrase("replanned"))
        if self.replans > self.settings.run_max_replans:
            self._fail("budget_exhausted", phrase("replan_limit"))
            return "stop"
        self._checkpoint()
        return "select"

    def finish(self) -> None:
        if self.run is None or self.halted:
            return
        if self.run.state in {"analyzing", "gathering_evidence", "proposing"}:
            self._fail("incomplete", phrase("incomplete"))
            return
        self._checkpoint()

    def invoke_pending_tool(self) -> None:
        if self.halted:
            return
        name = self.pending_tool or ""
        if name not in ALL_TOOLS:
            if not self._claim_current_intent():
                return
            self._record(
                name or "unknown",
                ToolResult(
                    status="error",
                    data={},
                    error_code="unknown_tool",
                    retryable=False,
                    operation_id=None,
                    resource_version=None,
                    observed_at=utcnow().isoformat(),
                    request_id=uuid4().hex,
                ),
            )
            return
        while True:
            if self.tool_calls >= self.settings.run_max_tool_calls:
                self._fail("budget_exhausted", phrase("tool_limit"))
                return
            if name in {"propose_refund", "add_internal_note", "handoff_ticket"}:
                if not self._claim_current_intent():
                    return
                result = self._dispatch(name)
            else:
                self._release_before_wait()
                result = self._dispatch(name)
                if not self._claim_current_intent():
                    return
            self.tool_calls += 1
            self._record(name, result)
            if result.status == "ok" or not result.retryable or name not in READ_TOOLS:
                return
            if self.read_retries >= self.settings.run_max_read_retries:
                return
            self.read_retries += 1

    def _dispatch(self, name: str) -> ToolResult:
        account_id = self.ticket.account_id
        if name == "get_customer_account":
            return self.tools.get_customer_account(account_id)
        if name == "list_invoices":
            return self.tools.list_invoices(account_id)
        if name == "list_payments":
            return self.tools.list_payments(account_id)
        if name == "list_refunds":
            return self.tools.list_refunds(account_id)
        if name == "search_knowledge":
            query = str(self.pending_args.get("query") or "").strip()
            if not query:
                return _local_error("validation", "query is required")
            citations = search_knowledge(
                self.session,
                tenant_id=self.actor.tenant_id,
                role=self.actor.role,
                query=query,
            )
            return ToolResult(
                status="ok",
                data={"citations": citations, "query": query},
                error_code=None,
                retryable=False,
                operation_id=None,
                resource_version=None,
                observed_at=utcnow().isoformat(),
                request_id=uuid4().hex,
            )
        if name == "get_operation_status":
            action_id = str(self.pending_args.get("action_id") or "")
            if not action_id:
                return _local_error("validation", "action_id is required")
            return self.tools.get_operation(account_id, action_id)
        if name == "propose_refund":
            return self._propose()
        if name == "add_internal_note":
            return self._note()
        if name == "handoff_ticket":
            return self._handoff()
        if name == "draft_reply":
            return _local_error(
                "outcome_not_verified",
                "A reply cannot be drafted before verification",
            )
        return _local_error("unknown_tool", "Tool is not registered")

    def _propose(self) -> ToolResult:
        if self.run is None:
            return _local_error("internal", "Run is missing")
        missing = [name for name in READ_TOOLS if self._latest(name) is None]
        if missing:
            return _local_error(
                "insufficient_evidence",
                "Billing reads are incomplete",
                {"missing": missing},
            )
        decision = evaluate_refund(self._payment_views(), self._refund_views(), utcnow())
        amounts_missing = decision.payment_id is None or decision.amount_cents is None
        if decision.outcome != "eligible" or amounts_missing:
            return _local_error(
                "not_eligible",
                "No duplicate refund is available",
                {"outcome": decision.outcome, "reasons": decision.reasons},
            )
        supplied = self.pending_args.get("payment_id")
        if supplied and str(supplied) != str(decision.payment_id):
            return _local_error(
                "target_not_eligible",
                "The requested payment is not the eligible duplicate",
                {"outcome": "handoff", "reasons": ["target_not_eligible"]},
            )
        self.run.state = "proposing"
        evidence = self._evidence(decision.payment_id)
        proposal = create_refund_proposal(
            self.session,
            tenant_id=self.actor.tenant_id,
            ticket_id=self.ticket.id,
            run_id=self.run.id,
            actor_user_id=self.actor.user_id,
            account_id=self.ticket.account_id,
            payment_id=decision.payment_id,
            event_id=decision.event_id or "",
            amount_cents=decision.amount_cents,
            payment_version=decision.payment_version or 1,
            evidence=evidence,
            observed_input_revision=self.investigated_revision,
        )
        self.run.state = "awaiting_approval"
        self.ticket.status = "awaiting_approval"
        self.ticket.updated_at = utcnow()
        return ToolResult(
            status="ok",
            data={
                "proposal_id": str(proposal.id),
                "revision": proposal.revision,
                "amount_cents": proposal.amount_cents,
                "currency": "USD",
                "action_id": proposal.action_id,
                "payload_hash": proposal.payload_hash,
            },
            error_code=None,
            retryable=False,
            operation_id=None,
            resource_version=decision.payment_version,
            observed_at=utcnow().isoformat(),
            request_id=uuid4().hex,
        )

    def _note(self) -> ToolResult:
        decision = evaluate_refund(self._payment_views(), self._refund_views(), utcnow())
        reason = _reason_text(decision.reasons) or phrase_reason("pending_payment_present")
        body = phrase("note_no_refund", reasons=reason)
        append_message(self.session, ticket=self.ticket, author_kind="internal", body=body)
        if self.run is not None:
            self.run.state = "awaiting_customer"
        self.awaiting_customer = True
        return ToolResult(
            status="ok",
            data={"body": body, "ignored_model_text": True},
            error_code=None,
            retryable=False,
            operation_id=None,
            resource_version=None,
            observed_at=utcnow().isoformat(),
            request_id=uuid4().hex,
        )

    def _handoff(self) -> ToolResult:
        decision = evaluate_refund(self._payment_views(), self._refund_views(), utcnow())
        reason = _reason_text(decision.reasons) or phrase_reason("unsupported_case")
        body = phrase("note_handoff", reasons=reason)
        append_message(self.session, ticket=self.ticket, author_kind="internal", body=body)
        if self.run is not None:
            self.run.state = "handed_off"
        self.ticket.status = "handed_off"
        self.ticket.updated_at = utcnow()
        audit(
            self.session,
            tenant_id=self.actor.tenant_id,
            event_type="handoff",
            actor_user_id=self.actor.user_id,
            ticket_id=self.ticket.id,
            run_id=None if self.run is None else self.run.id,
            payload={"reasons": decision.reasons},
        )
        return ToolResult(
            status="ok",
            data={"body": body, "ignored_model_text": True, "reasons": decision.reasons},
            error_code=None,
            retryable=False,
            operation_id=None,
            resource_version=None,
            observed_at=utcnow().isoformat(),
            request_id=uuid4().hex,
        )

    def _record(self, name: str, result: ToolResult) -> None:
        if self.run is None:
            return
        ignored = sorted(set(self.pending_args) - _allowed_args(name))
        observation = {
            "tool": name,
            "status": result.status,
            "data": result.data,
            "error_code": result.error_code,
            "retryable": result.retryable,
            "operation_id": result.operation_id,
            "resource_version": result.resource_version,
            "observed_at": result.observed_at,
            "request_id": result.request_id,
            "reason": self.pending_reason,
            "ignored_fields": ignored,
        }
        observations = list(self.run.observations)
        observations.append(observation)
        self.run.observations = observations
        self.session.add(
            ToolInvocation(
                tenant_id=self.actor.tenant_id,
                run_id=self.run.id,
                tool_name=name,
                request={
                    "arguments": self.pending_args,
                    "ignored_fields": ignored,
                    "account_id": str(self.ticket.account_id),
                },
                response=observation,
                status=result.status,
                error_code=result.error_code,
                retryable=result.retryable,
                operation_id=result.operation_id,
                resource_version=result.resource_version,
                observed_at=utcnow(),
                request_id=result.request_id,
            )
        )
        self._checkpoint()

    def _evidence(self, payment_id: UUID) -> dict[str, Any]:
        account = self._latest("get_customer_account") or {}
        invoices = (self._latest("list_invoices") or {}).get("data", {}).get("invoices", [])
        payment = next((item for item in self._payment_views() if item.id == payment_id), None)
        invoice_number = None
        if payment is not None and payment.invoice_id is not None:
            for invoice in invoices:
                if invoice.get("id") == str(payment.invoice_id):
                    invoice_number = invoice.get("number")
        data = account.get("data") or {}
        citations = (self._latest("search_knowledge") or {}).get("data", {}).get("citations", [])
        return {
            "customer_name": data.get("display_name") or self.ticket.customer_label,
            "invoice_number": invoice_number,
            "citations": citations if isinstance(citations, list) else [],
            "observation_request_ids": []
            if self.run is None
            else [item.get("request_id") for item in self.run.observations],
        }

    def _payment_views(self):
        latest = self._latest("list_payments")
        if latest is None or latest.get("status") != "ok":
            return []
        return parse_payments(latest.get("data") or {})

    def _refund_views(self):
        latest = self._latest("list_refunds")
        if latest is None or latest.get("status") != "ok":
            return []
        return parse_refunds(latest.get("data") or {})

    def _latest(self, tool_name: str) -> dict[str, Any] | None:
        if self.run is None:
            return None
        found = None
        for item in self.run.observations:
            if item.get("tool") == tool_name and item.get("status") == "ok":
                found = item
        return found

    def _snapshot(self) -> RunSnapshot:
        messages: list[dict[str, Any]] = []
        if self.run is not None:
            rows = self.session.scalars(
                select(Message)
                .where(
                    Message.ticket_id == self.ticket.id,
                    Message.author_kind.in_(("customer", "operator")),
                )
                .order_by(Message.revision)
            ).all()
            messages = [
                {"revision": row.revision, "author_kind": row.author_kind, "body": row.body}
                for row in rows
            ]
        return RunSnapshot(
            observations=[] if self.run is None else list(self.run.observations),
            messages=messages,
            tenant_id=str(self.ticket.tenant_id),
            account_id=str(self.ticket.account_id),
            ticket_id=str(self.ticket.id),
            permitted_tools=PERMITTED_TOOLS,
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
            replans=self.replans,
            awaiting_customer=self.awaiting_customer,
        )

    def _append_reason(self, reason: str) -> None:
        if self.run is None or not reason:
            return
        reasons = list(self.run.concise_reasons)
        reasons.append(reason)
        self.run.concise_reasons = reasons

    def _release_before_wait(self) -> None:
        """Commit local work so a provider or tool wait does not hold a row lock."""
        if self._preserved_cancellation:
            return
        self.session.commit()
        self._holding_intent = False
        self._run_visible = self.run is not None

    def _claim_current_intent(self) -> bool:
        """Lock the ticket and keep the run only if its investigated input is current."""
        if self._preserved_cancellation:
            self.halted = True
            return False
        if self.run is None:
            return False
        if self._holding_intent:
            return True
        if not self._run_visible:
            return True
        run_id = self.run.id
        ticket_id = self.ticket.id
        investigated = self.investigated_revision
        self.session.rollback()
        self.session.expire_all()
        ticket = self.session.scalar(
            select(Ticket).where(Ticket.id == ticket_id).with_for_update()
        )
        run = self.session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        current = latest_input_revision(self.session, ticket_id)
        stale = (
            ticket is None
            or run is None
            or run.state == "cancelled"
            or run.message_revision != investigated
            or current != investigated
        )
        if stale:
            self.session.rollback()
            self.session.expire_all()
            self._close_abandoned_run(run_id)
            self.run = self.session.get(Run, run_id)
            self.ticket = self.session.get(Ticket, ticket_id)
            self.halted = True
            self._preserved_cancellation = True
            self._holding_intent = False
            self.stop_reason = "The investigation stopped because the request changed."
            return False
        self.run = run
        self.ticket = ticket
        self._holding_intent = True
        return True

    def _close_abandoned_run(self, run_id: UUID) -> None:
        """A discarded result must not leave a nonterminal run behind.

        A run already cancelled by a new customer or operator message stays as it is.
        """
        run = self.session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or run.state in _TERMINAL:
            self.session.rollback()
            return
        run.state = "cancelled"
        run.error_code = "input_changed"
        run.updated_at = utcnow()
        self.session.commit()

    def _fail(self, code: str, reason: str) -> None:
        if self._preserved_cancellation:
            self.halted = True
            return
        if self._run_visible and not self._claim_current_intent():
            return
        self.halted = True
        self.stop_reason = reason
        self._append_reason(reason)
        if self.run is not None:
            self.run.state = "failed"
            self.run.error_code = code
        self._checkpoint()

    def _checkpoint(self) -> None:
        if self.run is None or self._preserved_cancellation:
            return
        self.run.model_calls = self.model_calls
        self.run.tool_calls = self.tool_calls
        self.run.replans = self.replans
        self.run.read_retries = self.read_retries
        self.run.updated_at = utcnow()
        self.run.budget_usd_micros = _provider_spend(self.provider)
        self.session.commit()
        self._holding_intent = False
        self._run_visible = True


def _provider_spend(provider: Any) -> int:
    calls = getattr(provider, "calls", None)
    if not isinstance(calls, list):
        return 0
    total = 0
    for call in calls:
        if not isinstance(call, dict):
            continue
        actual = call.get("actual_micros")
        reserved = int(call.get("reserved_micros") or 0)
        total += reserved if actual is None else int(actual)
    return total


def _reason_text(codes: list[str]) -> str:
    return ", ".join(phrase_reason(code) for code in codes)


def _allowed_args(tool_name: str) -> set[str]:
    if tool_name == "propose_refund":
        return {"payment_id"}
    if tool_name == "get_operation_status":
        return {"action_id"}
    if tool_name == "search_knowledge":
        return {"query"}
    return set()


def _local_error(code: str, message: str, data: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(
        status="error",
        data={"message": message, **(data or {})},
        error_code=code,
        retryable=False,
        operation_id=None,
        resource_version=None,
        observed_at=utcnow().isoformat(),
        request_id=uuid4().hex,
    )
