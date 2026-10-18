"""Local checks for the pilot ledger, prices, and adapters. These make no network calls."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from resolveai.agent.live_models import (
    DeepSeekFlashProvider,
    OpenAILunaProvider,
    redact_trace,
)
from resolveai.agent.pricing import (
    deepseek_offpeak,
    flash_actual,
    flash_reservation,
    luna_actual,
    luna_reservation,
    tokens_cost,
)
from resolveai.agent.provider import RunSnapshot
from resolveai.agent.spend import BudgetCeiling, SpendLedger
from resolveai.evaluation.live_pilot import FROZEN_CASES, judge, pilot_queue
from resolveai.settings import LiveModelBlocked


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _snapshot() -> RunSnapshot:
    return RunSnapshot(
        observations=[],
        messages=[{"revision": 1, "author_kind": "customer", "body": "I was charged twice."}],
        tenant_id="tenant",
        account_id="account",
        ticket_id="ticket",
    )


def _luna_usage(input_tokens: int, output_tokens: int, cached: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {"reasoning_tokens": min(output_tokens, 10)},
    }


def test_token_cost_rounds_up() -> None:
    assert tokens_cost(0, 100_000) == 0
    assert tokens_cost(1, 100_000) == 1
    assert tokens_cost(1_000_000, 100_000) == 100_000


def test_reservations_cover_capped_output() -> None:
    for input_tokens, cached, output_tokens in (
        (1, 0, 1),
        (1_000, 400, 2048),
        (20_000, 0, 2048),
        (272_000, 1_000, 2048),
    ):
        actual = luna_actual(_luna_usage(input_tokens, output_tokens, cached))
        assert actual is not None
        assert actual.micros <= luna_reservation(input_tokens)
        assert actual.detail["reasoning_tokens"] == min(output_tokens, 10)
    flash = flash_actual(
        {
            "prompt_cache_hit_tokens": 500,
            "prompt_cache_miss_tokens": 1500,
            "completion_tokens": 2048,
            "completion_tokens_details": {"reasoning_tokens": 20},
        }
    )
    assert flash is not None
    assert flash.micros <= flash_reservation(2000)
    assert flash.estimated is False


def test_offpeak_window() -> None:
    utc = UTC
    assert deepseek_offpeak(datetime(2026, 10, 17, 21, 6, tzinfo=utc))
    assert deepseek_offpeak(datetime(2026, 10, 19, 0, 59, tzinfo=utc))
    assert not deepseek_offpeak(datetime(2026, 10, 19, 1, 0, tzinfo=utc))
    assert not deepseek_offpeak(datetime(2026, 10, 19, 3, 59, tzinfo=utc))
    assert deepseek_offpeak(datetime(2026, 10, 19, 4, 0, tzinfo=utc))
    assert deepseek_offpeak(datetime(2026, 10, 19, 5, 30, tzinfo=utc))
    assert not deepseek_offpeak(datetime(2026, 10, 19, 6, 0, tzinfo=utc))
    assert not deepseek_offpeak(datetime(2026, 10, 19, 9, 59, tzinfo=utc))
    assert deepseek_offpeak(datetime(2026, 10, 19, 10, 0, tzinfo=utc))
    with pytest.raises(ValueError):
        deepseek_offpeak(datetime(2026, 10, 19, 10, 0))


def test_ledger_reconciles_actual_and_keeps_unknown(tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json", cap_micros=1_000)
    first = ledger.reserve(provider="openai", model="gpt-6-luna", reserved_micros=100)
    ledger.reconcile(
        first.request_id, actual_micros=40, estimated=False, usage={"output_tokens": 1}
    )
    ledger.reconcile(first.request_id, actual_micros=999, estimated=False, usage={})
    unknown = ledger.reserve(provider="deepseek", model="deepseek-flash", reserved_micros=25)
    ledger.reconcile(unknown.request_id, actual_micros=None, estimated=True, usage={})
    summary = ledger.summary()
    assert summary["settled_micros"] == 40
    assert summary["unknown_micros"] == 25
    assert ledger.total_micros() == 65
    with pytest.raises(BudgetCeiling):
        ledger.reserve(provider="openai", model="gpt-6-luna", reserved_micros=1_000)


def test_openai_call_reserves_then_reconciles_without_logging_reasoning(
    monkeypatch, tmp_path
) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")
    secret = "private-luna-thought"

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        assert url == "https://api.openai.com/v1/responses"
        assert headers["Authorization"] == "Bearer test-openai"
        assert json["model"] == "gpt-6-luna"
        assert json["reasoning"] == {"effort": "low"}
        assert json["max_output_tokens"] == 2048
        assert json["parallel_tool_calls"] is False
        assert json["tools"][0]["type"] == "function"
        assert "function" not in json["tools"][0]
        return _Response(
            200,
            {
                "model": "gpt-6-luna-2026-10-01",
                "output": [
                    {"type": "reasoning", "summary": secret, "encrypted_content": "cipher"},
                    {
                        "type": "function_call",
                        "name": "list_payments",
                        "call_id": "call_1",
                        "arguments": "{}",
                    },
                ],
                "usage": _luna_usage(120, 30),
            },
        )

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    action = provider.next_action(_snapshot())
    assert action.tool_name == "list_payments"
    assert secret not in action.reason
    assert secret not in str(provider.calls)
    assert any(item.get("type") == "reasoning" for item in provider._input)
    assert redact_trace(provider._input)[1]["content"] == "[redacted]"
    summary = ledger.summary()
    assert summary["unknown_micros"] == 0
    assert summary["settled_micros"] > 0
    assert summary["settled_micros"] <= summary["entries"][0]["reserved_micros"]


def test_over_cap_does_not_dispatch(monkeypatch, tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json", cap_micros=1)
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")
    called = {"count": 0}

    def fake_post(*_args, **_kwargs):
        called["count"] += 1
        return _Response(200, {})

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    with pytest.raises(BudgetCeiling):
        provider.next_action(_snapshot())
    assert called["count"] == 0
    assert ledger.total_micros() == 0


def test_client_error_is_not_retried_and_keeps_the_reservation(monkeypatch, tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")
    called = {"count": 0}

    def fake_post(*_args, **_kwargs):
        called["count"] += 1
        return _Response(400, {"error": {"message": "bad schema"}})

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    with pytest.raises(LiveModelBlocked):
        provider.next_action(_snapshot())
    assert called["count"] == 1
    assert ledger.summary()["unknown_micros"] > 0
    assert ledger.summary()["settled_micros"] == 0


def test_retryable_status_is_attempted_once_more(monkeypatch, tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")
    called = {"count": 0}

    def fake_post(*_args, **_kwargs):
        called["count"] += 1
        if called["count"] == 1:
            return _Response(503, {})
        return _Response(
            200,
            {
                "model": "gpt-6-luna",
                "output": [{"type": "message", "content": "done"}],
                "usage": _luna_usage(10, 5),
            },
        )

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    action = provider.next_action(_snapshot())
    assert action.route == "decide"
    assert called["count"] == 2
    summary = ledger.summary()
    assert summary["unknown_micros"] > 0
    assert summary["settled_micros"] > 0
    assert len(summary["entries"]) == 2


def test_timeout_keeps_both_reservations(monkeypatch, tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")

    def fake_post(*_args, **_kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    with pytest.raises(LiveModelBlocked):
        provider.next_action(_snapshot())
    summary = ledger.summary()
    assert len(summary["entries"]) == 2
    assert summary["unknown_micros"] > 0
    assert summary["settled_micros"] == 0


def test_deepseek_keeps_reasoning_locally_and_refuses_peak(monkeypatch, tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = DeepSeekFlashProvider(ledger=ledger, api_key="test-deepseek")
    secret = "private-deepseek-thought"

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        assert url == "https://api.deepseek.com/chat/completions"
        assert json["model"] == "deepseek-flash"
        assert json["thinking"] == {"type": "enabled"}
        assert json["reasoning_effort"] == "low"
        assert json["tools"][0]["function"]["name"]
        return _Response(
            200,
            {
                "model": "deepseek-v4.1-flash",
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": secret,
                            "tool_calls": [
                                {
                                    "id": "call_ds",
                                    "type": "function",
                                    "function": {"name": "list_refunds", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 80,
                    "completion_tokens": 15,
                },
            },
        )

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    monkeypatch.setattr("resolveai.agent.pricing.deepseek_offpeak", lambda _moment: True)
    action = provider.next_action(_snapshot())
    assert action.tool_name == "list_refunds"
    assert provider._messages[-1]["reasoning_content"] == secret
    assert secret not in action.reason
    assert secret not in str(provider.calls)
    assert ledger.summary()["settled_micros"] > 0

    monkeypatch.setattr("resolveai.agent.pricing.deepseek_offpeak", lambda _moment: False)
    blocked = DeepSeekFlashProvider(ledger=SpendLedger(tmp_path / "other.json"), api_key="k")
    called = {"count": 0}

    def unexpected(*_args, **_kwargs):
        called["count"] += 1
        return _Response(200, {})

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", unexpected)
    with pytest.raises(LiveModelBlocked, match="off-peak"):
        blocked.next_action(_snapshot())
    assert called["count"] == 0


def test_input_limit_does_not_reserve(monkeypatch, tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")
    monkeypatch.setattr(
        "resolveai.agent.live_models.httpx.post", lambda *_args, **_kwargs: _Response(200, {})
    )
    with pytest.raises(LiveModelBlocked, match="Input bound"):
        provider._post("https://api.openai.com/v1/responses", {"input": "x" * 25_000})
    assert ledger.total_micros() == 0


def test_queue_and_scoring_are_frozen() -> None:
    queue = pilot_queue()
    assert len(queue) == 24
    assert len(set(queue)) == 24
    assert queue[0] == ("openai", "invoice-duplicate", 1)
    assert queue[1] == ("deepseek", "invoice-duplicate", 1)
    families = {case_id.split("-")[0] for _model, case_id, _repeat in queue}
    assert families == {"invoice", "payment"}
    observed = {
        "run_state": "awaiting_approval",
        "successful_tools": ["list_payments", "search_knowledge", "propose_refund"],
        "search_calls": 1,
        "citation_count": 1,
        "proposal_count": 1,
        "proposal_cents": 4200,
        "proposal_status": "open",
        "refund_delta": 0,
        "approval_count": 0,
        "ledger_count": 0,
        "reply_count": 0,
        "citations": [_support()],
        "grounded_citations": [_support()],
    }
    expect = next(item["expect"] for item in FROZEN_CASES if item["case_id"] == "invoice-duplicate")
    assert judge(expect, observed)["correct"] is True
    observed["refund_delta"] = 1
    safety = judge(expect, observed)
    assert safety["correct"] is False
    assert safety["failure_class"] == "safety"
    pending = {
        **observed,
        "run_state": "awaiting_customer",
        "successful_tools": ["list_refunds", "add_internal_note"],
        "proposal_count": 0,
        "proposal_cents": None,
        "proposal_status": None,
        "refund_delta": 0,
        "search_calls": 0,
        "citation_count": 0,
    }
    pending_expect = next(
        item["expect"] for item in FROZEN_CASES if item["case_id"] == "payment-pending"
    )
    assert judge(pending_expect, pending)["correct"] is True


def _support(**overrides: str) -> dict[str, str]:
    citation = {
        "title": "Duplicate payment refunds",
        "version": "refund-policy-2026.10.12",
        "heading": "Duplicate settlement",
        "snippet": "A second settled USD payment",
        "evidence_id": "evidence-1",
    }
    citation.update(overrides)
    return citation


def test_wrong_document_and_ungrounded_evidence_fail() -> None:
    expect = next(item["expect"] for item in FROZEN_CASES if item["case_id"] == "invoice-duplicate")
    observed = {
        "run_state": "awaiting_approval",
        "successful_tools": ["search_knowledge", "propose_refund"],
        "search_calls": 1,
        "citation_count": 1,
        "proposal_count": 1,
        "proposal_cents": 4200,
        "proposal_status": "open",
        "refund_delta": 0,
        "approval_count": 0,
        "ledger_count": 0,
        "reply_count": 0,
        "citations": [_support(title="Completely unrelated policy")],
        "grounded_citations": [_support(title="Completely unrelated policy")],
    }
    wrong = judge(expect, observed)
    assert wrong["correct"] is False
    assert wrong["failure_class"] == "retrieval"
    observed["citations"] = [_support()]
    observed["grounded_citations"] = [_support(version="refund-policy-2020")]
    stale = judge(expect, observed)
    assert stale["failure_class"] == "grounding"
    observed["grounded_citations"] = [_support(heading="Existing refund")]
    section = judge(expect, observed)
    assert section["correct"] is False
    assert section["failure_class"] == "grounding"
    observed["citations"] = []
    observed["grounded_citations"] = []
    observed["search_calls"] = 0
    missing = judge(expect, observed)
    assert missing["correct"] is False


def test_byte_bound_rejects_an_undercounted_reservation(monkeypatch, tmp_path) -> None:
    from resolveai.agent.pricing import input_token_bound, luna_reservation

    payload = "a" * 9_000
    old_estimate = (len(payload) + 2) // 3
    bound = input_token_bound(payload, 1)
    assert bound > old_estimate
    ledger = SpendLedger(tmp_path / "ledger.json", cap_micros=luna_reservation(old_estimate))
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")
    called = {"count": 0}

    def fake_post(*_args, **_kwargs):
        called["count"] += 1
        return _Response(200, {})

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    with pytest.raises(BudgetCeiling):
        provider._post("https://api.openai.com/v1/responses", {"input": payload})
    assert called["count"] == 0


def test_over_reservation_usage_stops_the_next_call(monkeypatch, tmp_path) -> None:
    from resolveai.agent.spend import BudgetCeiling

    ledger = SpendLedger(tmp_path / "ledger.json", cap_micros=2_000_000)
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")
    called = {"count": 0}

    def fake_post(*_args, **_kwargs):
        called["count"] += 1
        return _Response(
            200,
            {
                "model": "gpt-6-luna",
                "output": [{"type": "message", "content": "done"}],
                "usage": _luna_usage(500_000, 10),
            },
        )

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    with pytest.raises(BudgetCeiling):
        provider.next_action(_snapshot())
    assert ledger.summary()["breached"] is True
    assert called["count"] == 1
    follow = OpenAILunaProvider(ledger=ledger, api_key="test-openai")

    def unexpected(*_args, **_kwargs):
        called["count"] += 1
        return _Response(200, {})

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", unexpected)
    with pytest.raises(BudgetCeiling):
        follow.next_action(_snapshot())
    assert called["count"] == 1


def test_deepseek_retry_stops_when_the_window_closes(monkeypatch, tmp_path) -> None:
    from datetime import UTC, datetime

    from resolveai.agent.live_models import OffPeakClosed

    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = DeepSeekFlashProvider(ledger=ledger, api_key="test-deepseek")
    moments = iter(
        (
            datetime(2026, 10, 19, 4, 0, tzinfo=UTC),
            datetime(2026, 10, 19, 6, 0, 1, tzinfo=UTC),
        )
    )
    provider.clock = lambda: next(moments)
    called = {"count": 0}

    def fake_post(*_args, **_kwargs):
        called["count"] += 1
        return _Response(503, {})

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    with pytest.raises(OffPeakClosed):
        provider.next_action(_snapshot())
    assert called["count"] == 1
    assert ledger.summary()["unknown_micros"] > 0


def test_malformed_arguments_are_not_empty_tool_calls(monkeypatch, tmp_path) -> None:
    from resolveai.agent.live_models import AdapterFailure

    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")

    def fake_post(*_args, **_kwargs):
        return _Response(
            200,
            {
                "model": "gpt-6-luna",
                "output": [
                    {
                        "type": "function_call",
                        "name": "list_payments",
                        "call_id": "call_1",
                        "arguments": "not-json",
                    }
                ],
                "usage": _luna_usage(20, 5),
            },
        )

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    with pytest.raises(AdapterFailure, match="JSON"):
        provider.next_action(_snapshot())


def test_truncated_tool_result_is_not_sent_onward(monkeypatch, tmp_path) -> None:
    from resolveai.agent.live_models import AdapterFailure

    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")

    def fake_post(*_args, **_kwargs):
        return _Response(
            200,
            {
                "model": "gpt-6-luna",
                "output": [
                    {
                        "type": "function_call",
                        "name": "list_payments",
                        "call_id": "call_1",
                        "arguments": "{}",
                    }
                ],
                "usage": _luna_usage(20, 5),
            },
        )

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    provider.next_action(_snapshot())
    snapshot = _snapshot()
    snapshot.observations = [
        {"tool": "list_payments", "status": "ok", "data": {"blob": "x" * 7000}}
    ]
    with pytest.raises(AdapterFailure, match="continuation"):
        provider.next_action(snapshot)


def test_continuation_keeps_reasoning_state_out_of_the_trace(monkeypatch, tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    provider = OpenAILunaProvider(ledger=ledger, api_key="test-openai")
    secret = "private-luna-thought"
    seen: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        del url, headers, timeout
        if seen:
            seen["second"] = json
            return _Response(
                200,
                {
                    "model": "gpt-6-luna",
                    "output": [{"type": "message", "content": "done"}],
                    "usage": _luna_usage(30, 5),
                },
            )
        seen["first"] = True
        return _Response(
            200,
            {
                "model": "gpt-6-luna",
                "output": [
                    {"type": "reasoning", "summary": secret, "encrypted_content": "cipher"},
                    {
                        "type": "function_call",
                        "name": "list_payments",
                        "call_id": "call_1",
                        "arguments": "{}",
                    },
                ],
                "usage": _luna_usage(20, 5),
            },
        )

    monkeypatch.setattr("resolveai.agent.live_models.httpx.post", fake_post)
    provider.next_action(_snapshot())
    follow = _snapshot()
    follow.observations = [{"tool": "list_payments", "status": "ok", "data": {"payments": []}}]
    provider.next_action(follow)
    second = seen["second"]
    assert isinstance(second, dict)
    assert any(item.get("type") == "reasoning" for item in second["input"])
    assert secret not in str(provider.calls)
