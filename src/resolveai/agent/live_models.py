"""Live tool-calling adapters for the authorized two-model pilot.

Reasoning text stays in the request state the provider requires. It is not
copied into run reasons or the saved trace.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from resolveai.agent.pricing import (
    INPUT_TOKEN_LIMIT,
    MAX_OUTPUT_TOKENS,
    deepseek_dispatch_allowed,
    flash_actual,
    flash_reservation,
    input_token_bound,
    luna_actual,
    luna_reservation,
)
from resolveai.agent.provider import PERMITTED_TOOLS, ProviderAction, RunSnapshot
from resolveai.agent.spend import BudgetCeiling, SpendLedger
from resolveai.clock import utcnow
from resolveai.settings import LiveModelBlocked

PROMPT_VERSION = "m2-accept-v1"
REQUEST_TIMEOUT_SECONDS = 45.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
INSTRUCTIONS = (
    "You investigate one synthetic billing ticket for ResolveAI. "
    "Read the observations and choose the next permitted tool. "
    "Call one tool at a time. "
    "You cannot approve a refund, send a reply, or decide a refund amount. "
    "propose_refund does not take an amount; the server applies the versioned rule. "
    "Customer wording is not the policy. "
    "Use search_knowledge when a written policy is needed to ground a duplicate "
    "or a conflicting-invoice decision. "
    "Use add_internal_note when a charge is still pending and you need the customer. "
    "Use handoff_ticket when the case is outside the refund rule. "
    "Do not stop until propose_refund, add_internal_note, or handoff_ticket has succeeded. "
    "After one of those tools succeeds, stop."
)

_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "get_customer_account": {
        "description": "Read the customer account already linked to this ticket.",
        "properties": {},
        "required": [],
    },
    "list_invoices": {
        "description": "List invoices for the ticket account.",
        "properties": {},
        "required": [],
    },
    "list_payments": {
        "description": "List payments for the ticket account, with status and invoice linkage.",
        "properties": {},
        "required": [],
    },
    "list_refunds": {
        "description": "List refunds already recorded for the ticket account.",
        "properties": {},
        "required": [],
    },
    "search_knowledge": {
        "description": "Retrieve current policy snippets. This text does not set an amount.",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    "propose_refund": {
        "description": "Ask the server to apply the duplicate-refund rule. Do not send an amount.",
        "properties": {"payment_id": {"type": "string"}},
        "required": [],
    },
    "get_operation_status": {
        "description": "Read a previously recorded operation by its action id.",
        "properties": {"action_id": {"type": "string"}},
        "required": ["action_id"],
    },
    "add_internal_note": {
        "description": "Record that a pending charge needs the customer. No refund is proposed.",
        "properties": {},
        "required": [],
    },
    "handoff_ticket": {
        "description": "Hand the ticket off when no duplicate refund should be proposed.",
        "properties": {},
        "required": [],
    },
    "draft_reply": {
        "description": "Not available before a human verifies an outcome.",
        "properties": {},
        "required": [],
    },
}


def tool_parameters(name: str) -> dict[str, Any]:
    spec = _TOOL_SPECS[name]
    return {
        "type": "object",
        "properties": spec["properties"],
        "required": spec["required"],
        "additionalProperties": False,
    }


def redact_trace(value: Any) -> Any:
    """Drop chain-of-thought fields before anything is written to disk."""
    if isinstance(value, list):
        return [redact_trace(item) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key in {"reasoning_content", "summary", "encrypted_content"}:
                cleaned[key] = "[redacted]"
                continue
            if key == "type" and item == "reasoning":
                cleaned[key] = item
                continue
            cleaned[key] = redact_trace(item)
        if cleaned.get("type") == "reasoning":
            return {"type": "reasoning", "content": "[redacted]"}
        return cleaned
    return value


class OffPeakClosed(LiveModelBlocked):
    """DeepSeek's discounted window is closed, so no request was sent."""

    code = "offpeak_closed"


class AdapterFailure(LiveModelBlocked):
    """The provider response cannot be continued safely."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class _RetryableCall(Exception):
    pass


class LiveToolModel:
    prompt_version = PROMPT_VERSION

    def __init__(
        self, *, ledger: SpendLedger, api_key: str, model: str, provider_name: str
    ) -> None:
        self.ledger = ledger
        self.api_key = api_key
        self.model = model
        self.provider_name = provider_name
        self.calls: list[dict[str, Any]] = []
        self._awaiting_tool: str | None = None
        self._pending_call_id = ""
        self._started = False
        self.protocol = ""
        self.billable = True
        self.clock = utcnow

    def next_action(self, snapshot: RunSnapshot) -> ProviderAction:
        if self._awaiting_tool is not None:
            self.accept_tool_result(snapshot)
            self._awaiting_tool = None
        elif not self._started:
            self.start_conversation(snapshot)
            self._started = True
        action = self.request_next()
        if action.route == "tool":
            self._awaiting_tool = action.tool_name
        return action

    def start_conversation(self, snapshot: RunSnapshot) -> None:
        raise NotImplementedError

    def accept_tool_result(self, snapshot: RunSnapshot) -> None:
        raise NotImplementedError

    def request_next(self) -> ProviderAction:
        raise NotImplementedError

    def _latest_observation(self, snapshot: RunSnapshot) -> dict[str, Any]:
        if not snapshot.observations:
            return {"status": "error", "error_code": "missing_observation"}
        observation = dict(snapshot.observations[-1])
        data = observation.get("data")
        if isinstance(data, dict):
            encoded = json.dumps(data, default=str)
            if len(encoded) > 6000:
                raise AdapterFailure(
                    "A tool result exceeded the continuation limit. "
                    "The model was not given a truncated substitute.",
                    "truncated_observation",
                )
        return observation

    def _before_dispatch(self) -> None:
        return

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        bound = _input_bound(payload)
        if bound > INPUT_TOKEN_LIMIT:
            raise LiveModelBlocked("Input bound exceeds the pilot limit. No call was sent.")
        last_error = "request failed"
        for _attempt in range(2):
            try:
                return self._once(url, payload, bound)
            except _RetryableCall as exc:
                last_error = str(exc)
        raise LiveModelBlocked(f"{self.provider_name} failed after one retry: {last_error}")

    def _once(self, url: str, payload: dict[str, Any], reservation_tokens: int) -> dict[str, Any]:
        self._before_dispatch()
        reserved = self._reserve_amount(reservation_tokens)
        reservation = self.ledger.reserve(
            provider=self.provider_name,
            model=self.model,
            reserved_micros=reserved,
        )
        started = utcnow()
        try:
            response = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            self._keep_unknown(
                reservation.request_id,
                reserved,
                url,
                started,
                {"transport_error": exc.__class__.__name__},
            )
            raise _RetryableCall(
                f"{self.provider_name} request failed before usage was known"
            ) from exc
        body = _json_body(response)
        latency_ms = int((utcnow() - started).total_seconds() * 1000)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
        priced = self._price(usage)
        output_tokens = 0 if priced is None else int(priced.detail.get("output_tokens") or 0)
        over = priced is not None and (
            priced.micros > reserved or output_tokens > MAX_OUTPUT_TOKENS
        )
        if priced is None:
            self.ledger.reconcile(
                reservation.request_id,
                actual_micros=None,
                estimated=True,
                usage={"http_status": response.status_code},
            )
        else:
            self.ledger.reconcile(
                reservation.request_id,
                actual_micros=priced.micros,
                estimated=priced.estimated,
                usage=priced.detail,
                over_reservation=over,
            )
        self.calls.append(
            {
                "provider": self.provider_name,
                "model": self.model,
                "protocol": self.protocol,
                "response_model": body.get("model"),
                "route": url,
                "prompt_version": self.prompt_version,
                "reasoning_effort": "low",
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "http_status": response.status_code,
                "latency_ms": latency_ms,
                "reserved_micros": reserved,
                "actual_micros": None if priced is None else priced.micros,
                "estimated": True if priced is None else priced.estimated,
                "usage": {} if priced is None else priced.detail,
            }
        )
        if response.status_code in RETRYABLE_STATUS:
            raise _RetryableCall(f"{self.provider_name} returned HTTP {response.status_code}")
        if response.status_code >= 400:
            message = json.dumps(redact_trace(body))[:500]
            raise LiveModelBlocked(
                f"{self.provider_name} returned HTTP {response.status_code}: {message}"
            )
        if priced is None:
            raise AdapterFailure(
                f"{self.provider_name} omitted billable usage; reservation retained",
                "usage_unverified",
            )
        if over:
            raise BudgetCeiling(
                "Settled usage exceeded the pre-dispatch reservation. Further calls are stopped."
            )
        return body

    def _keep_unknown(
        self,
        request_id: str,
        reserved: int,
        url: str,
        started: Any,
        usage: dict[str, Any],
    ) -> None:
        self.ledger.reconcile(
            request_id,
            actual_micros=None,
            estimated=True,
            usage=usage,
        )
        self.calls.append(
            {
                "provider": self.provider_name,
                "model": self.model,
                "protocol": self.protocol,
                "response_model": None,
                "route": url,
                "prompt_version": self.prompt_version,
                "reasoning_effort": "low",
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "http_status": None,
                "latency_ms": int((utcnow() - started).total_seconds() * 1000),
                "reserved_micros": reserved,
                "actual_micros": None,
                "estimated": True,
                "usage": usage,
            }
        )

    def _reserve_amount(self, input_tokens: int) -> int:
        raise NotImplementedError

    def _price(self, usage: dict | None):
        raise NotImplementedError


class OpenAILunaProvider(LiveToolModel):
    def __init__(self, *, ledger: SpendLedger, api_key: str) -> None:
        super().__init__(
            ledger=ledger,
            api_key=api_key,
            model="gpt-6-luna",
            provider_name="openai",
        )
        self.protocol = "responses"
        self._input: list[Any] = []

    def start_conversation(self, snapshot: RunSnapshot) -> None:
        self._input = [{"role": "user", "content": _case_text(snapshot)}]

    def accept_tool_result(self, snapshot: RunSnapshot) -> None:
        observation = self._latest_observation(snapshot)
        call_id = observation.get("_call_id") or self._pending_call_id
        self._input.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(redact_trace(observation), default=str),
            }
        )

    def request_next(self) -> ProviderAction:
        payload = {
            "model": self.model,
            "instructions": INSTRUCTIONS,
            "input": self._input,
            "tools": [_openai_tool(name) for name in PERMITTED_TOOLS],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "reasoning": {"effort": "low"},
            "store": False,
        }
        body = self._post("https://api.openai.com/v1/responses", payload)
        output = body.get("output") if isinstance(body.get("output"), list) else []
        self._input.extend(output)
        calls = [
            item
            for item in output
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if not calls:
            return ProviderAction(route="decide", reason="The model stopped without another tool.")
        call = calls[0]
        self._pending_call_id = str(call.get("call_id") or "")
        for extra in calls[1:]:
            self._input.append(
                {
                    "type": "function_call_output",
                    "call_id": extra.get("call_id"),
                    "output": "Only one tool is executed per turn.",
                }
            )
        arguments = _arguments(call.get("arguments"))
        name = str(call.get("name") or "")
        return ProviderAction(
            route="tool",
            tool_name=name,
            arguments=arguments,
            reason="The model requested this tool.",
        )

    def _reserve_amount(self, input_tokens: int) -> int:
        return luna_reservation(input_tokens)

    def _price(self, usage: dict | None):
        return luna_actual(usage)


class DeepSeekFlashProvider(LiveToolModel):
    def __init__(self, *, ledger: SpendLedger, api_key: str) -> None:
        super().__init__(
            ledger=ledger,
            api_key=api_key,
            model="deepseek-flash",
            provider_name="deepseek",
        )
        self.protocol = "chat-completions"
        self._messages: list[dict[str, Any]] = []

    def start_conversation(self, snapshot: RunSnapshot) -> None:
        self._messages = [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": _case_text(snapshot)},
        ]

    def accept_tool_result(self, snapshot: RunSnapshot) -> None:
        observation = self._latest_observation(snapshot)
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": self._pending_call_id,
                "content": json.dumps(redact_trace(observation), default=str),
            }
        )

    def _before_dispatch(self) -> None:
        if not deepseek_dispatch_allowed(self.clock()):
            raise OffPeakClosed("DeepSeek off-peak window is closed. No request was sent.")

    def request_next(self) -> ProviderAction:
        payload = {
            "model": self.model,
            "messages": self._messages,
            "tools": [_deepseek_tool(name) for name in PERMITTED_TOOLS],
            "tool_choice": "auto",
            "max_tokens": MAX_OUTPUT_TOKENS,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "low",
            "stream": False,
        }
        body = self._post("https://api.deepseek.com/chat/completions", payload)
        choices = body.get("choices") if isinstance(body.get("choices"), list) else []
        if not choices:
            raise LiveModelBlocked("DeepSeek returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise LiveModelBlocked("DeepSeek returned no message")
        stored = {
            "role": "assistant",
            "content": message.get("content") or "",
        }
        if "reasoning_content" in message:
            stored["reasoning_content"] = message.get("reasoning_content") or ""
        raw_calls = message.get("tool_calls")
        tool_calls = raw_calls if isinstance(raw_calls, list) else []
        if tool_calls:
            stored["tool_calls"] = tool_calls
        self._messages.append(stored)
        if not tool_calls:
            return ProviderAction(route="decide", reason="The model stopped without another tool.")
        call = tool_calls[0]
        function = call.get("function") if isinstance(call, dict) else {}
        self._pending_call_id = str(call.get("id") or "")
        for extra in tool_calls[1:]:
            self._messages.append(
                {
                    "role": "tool",
                    "tool_call_id": extra.get("id"),
                    "content": "Only one tool is executed per turn.",
                }
            )
        return ProviderAction(
            route="tool",
            tool_name=str((function or {}).get("name") or ""),
            arguments=_arguments((function or {}).get("arguments")),
            reason="The model requested this tool.",
        )

    def _reserve_amount(self, input_tokens: int) -> int:
        return flash_reservation(input_tokens)

    def _price(self, usage: dict | None):
        return flash_actual(usage)


def _openai_tool(name: str) -> dict[str, Any]:
    spec = _TOOL_SPECS[name]
    return {
        "type": "function",
        "name": name,
        "description": spec["description"],
        "parameters": tool_parameters(name),
    }


def _deepseek_tool(name: str) -> dict[str, Any]:
    spec = _TOOL_SPECS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": spec["description"],
            "parameters": tool_parameters(name),
        },
    }


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _input_bound(payload: dict[str, Any]) -> int:
    encoded = json.dumps(payload, default=str)
    items = 1
    for key in ("input", "messages", "tools"):
        value = payload.get(key)
        if isinstance(value, list):
            items += len(value)
    return input_token_bound(encoded, items)


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AdapterFailure(
            "Tool arguments were not valid JSON", "malformed_arguments"
        ) from exc
    if not isinstance(parsed, dict):
        raise AdapterFailure("Tool arguments were not an object", "malformed_arguments")
    return parsed


def _case_text(snapshot: RunSnapshot) -> str:
    lines = [
        f"Ticket {snapshot.ticket_id} is already scoped to account {snapshot.account_id}.",
        "Customer and operator messages:",
    ]
    for message in snapshot.messages:
        lines.append(f"{message.get('author_kind')}: {message.get('body')}")
    if not snapshot.messages:
        lines.append("(none)")
    lines.append(f"Permitted tools: {', '.join(snapshot.permitted_tools)}")
    return "\n".join(lines)
