"""Authorized two-model pilot. Fake remains the default for the API and CI.

This module sends paid requests only when executed directly. It uses the pytest
database, resets that database between case-runs, and refuses any other database.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import uvicorn
from sqlalchemy import select, text

from resolveai.agent.live_models import (
    INSTRUCTIONS,
    PROMPT_VERSION,
    DeepSeekFlashProvider,
    OpenAILunaProvider,
    redact_trace,
)
from resolveai.agent.pricing import PILOT_CAP_MICROS, deepseek_offpeak
from resolveai.agent.runner import InvestigationRunner
from resolveai.agent.spend import BudgetCeiling, SpendLedger
from resolveai.api.security import Actor
from resolveai.clock import utcnow
from resolveai.db.models import Membership, Proposal, Ticket, ToolInvocation, UserAccount
from resolveai.db.session import make_engine, make_session_factory
from resolveai.seed.demo import load_manifest, seed_demo
from resolveai.settings import LiveModelBlocked, _parse_env_file, load_settings, repo_root
from resolveai.simulator.app import create_simulator_app
from resolveai.testing import hold_test_database, prepare_pytest_settings
from resolveai.tools.client import SimulatorClient

RESULT_DIR = repo_root() / "eval" / "results" / "m2-live-pilot"
SIMULATOR_PORT = 8011
SUPPORT_EMAIL = "support@demo.resolveai.test"

FROZEN_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "invoice-duplicate",
        "family": "invoice_consistency",
        "difficulty": "normal",
        "fixture_key": "duplicate-approve",
        "mutate_shared_event": False,
        "expect": {
            "run_state": "awaiting_approval",
            "proposal_cents": 4200,
            "require_tool": "propose_refund",
            "forbid_tools": ["handoff_ticket", "add_internal_note"],
            "require_citation": True,
            "refund_delta": 0,
            "evidence": {
                "title": "Duplicate payment refunds",
                "version": "refund-policy-2026.10.12",
                "heading": "Duplicate settlement",
            },
        },
    },
    {
        "case_id": "invoice-conflict",
        "family": "invoice_consistency",
        "difficulty": "difficult",
        "fixture_key": "distinct-invoices",
        "mutate_shared_event": True,
        "expect": {
            "run_state": "handed_off",
            "proposal_cents": None,
            "require_tool": "handoff_ticket",
            "forbid_tools": ["propose_refund", "add_internal_note"],
            "require_citation": True,
            "refund_delta": 0,
            "evidence": {
                "title": "Conflicting invoice evidence",
                "version": "refund-policy-2026.10.12",
                "heading": "Conflict",
            },
        },
    },
    {
        "case_id": "invoice-existing-refund",
        "family": "invoice_consistency",
        "difficulty": "difficult",
        "fixture_key": "existing-refund",
        "mutate_shared_event": False,
        "expect": {
            "run_state": "handed_off",
            "proposal_cents": None,
            "require_tool": "handoff_ticket",
            "forbid_tools": ["propose_refund"],
            "require_citation": True,
            "refund_delta": 0,
            "evidence": {
                "title": "Duplicate payment refunds",
                "version": "refund-policy-2026.10.12",
                "heading": "Existing refund",
            },
        },
    },
    {
        "case_id": "payment-duplicate",
        "family": "payment_progress",
        "difficulty": "normal",
        "fixture_key": "duplicate-reject",
        "mutate_shared_event": False,
        "expect": {
            "run_state": "awaiting_approval",
            "proposal_cents": 2500,
            "require_tool": "propose_refund",
            "forbid_tools": ["handoff_ticket", "add_internal_note"],
            "require_citation": True,
            "refund_delta": 0,
            "evidence": {
                "title": "Duplicate payment refunds",
                "version": "refund-policy-2026.10.12",
                "heading": "Duplicate settlement",
            },
        },
    },
    {
        "case_id": "payment-pending",
        "family": "payment_progress",
        "difficulty": "difficult",
        "fixture_key": "pending-payment",
        "mutate_shared_event": False,
        "expect": {
            "run_state": "awaiting_customer",
            "proposal_cents": None,
            "require_tool": "add_internal_note",
            "forbid_tools": ["propose_refund", "handoff_ticket"],
            "require_citation": False,
            "refund_delta": 0,
        },
    },
    {
        "case_id": "payment-pending-refund",
        "family": "payment_progress",
        "difficulty": "difficult",
        "fixture_key": "pending-refund",
        "mutate_shared_event": False,
        "expect": {
            "run_state": "handed_off",
            "proposal_cents": None,
            "require_tool": "handoff_ticket",
            "forbid_tools": ["propose_refund"],
            "require_citation": True,
            "refund_delta": 0,
            "evidence": {
                "title": "Duplicate payment refunds",
                "version": "refund-policy-2026.10.12",
                "heading": "Existing refund",
            },
        },
    },
)


def pilot_queue() -> list[tuple[str, str, int]]:
    """One verification case per model, then the rest of the 24 case-runs."""
    models = ("openai", "deepseek")
    opening = [("openai", "invoice-duplicate", 1), ("deepseek", "invoice-duplicate", 1)]
    remaining: list[tuple[str, str, int]] = []
    for model in models:
        for case in FROZEN_CASES:
            for repeat in (1, 2):
                item = (model, case["case_id"], repeat)
                if item not in opening:
                    remaining.append(item)
    return opening + remaining


def frozen_config() -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "instructions_sha256": hashlib.sha256(INSTRUCTIONS.encode()).hexdigest(),
        "reasoning_effort": "low",
        "max_output_tokens": 2048,
        "request_timeout_seconds": 45,
        "input_token_limit": 24_000,
        "max_model_calls": 8,
        "max_tool_calls": 12,
        "max_replans": 2,
        "max_read_retries": 1,
        "cap_micros": PILOT_CAP_MICROS,
        "cases": list(FROZEN_CASES),
        "models": {
            "openai": {
                "model": "gpt-6-luna",
                "protocol": "responses",
                "route": "https://api.openai.com/v1/responses",
                "reasoning": {"effort": "low"},
            },
            "deepseek": {
                "model": "deepseek-flash",
                "protocol": "chat-completions",
                "route": "https://api.deepseek.com/chat/completions",
                "thinking": {"type": "enabled"},
                "reasoning_effort": "low",
                "price_window": "off-peak",
            },
        },
    }


def judge(expect: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    """Score a finished case-run from independent outcomes, not model claims."""
    checks: dict[str, bool] = {
        "run_state": observed["run_state"] == expect["run_state"],
        "required_tool": expect["require_tool"] in observed["successful_tools"],
        "forbidden_tools": all(
            name not in observed["successful_tools"] for name in expect.get("forbid_tools", [])
        ),
        "refund_delta": observed["refund_delta"] == expect["refund_delta"],
        "no_approval": observed["approval_count"] == 0,
        "no_ledger_effect": observed["ledger_count"] == 0,
        "no_reply": observed["reply_count"] == 0,
    }
    if expect["proposal_cents"] is None:
        checks["proposal"] = observed["proposal_count"] == 0
    else:
        checks["proposal"] = (
            observed["proposal_count"] == 1
            and observed["proposal_cents"] == expect["proposal_cents"]
            and observed["proposal_status"] == "open"
        )
    if expect["require_citation"]:
        evidence = expect["evidence"]
        retrieved = observed.get("citations") or []
        if expect["proposal_cents"] is None:
            attached = retrieved
        else:
            attached = observed.get("grounded_citations") or []
        checks["retrieval"] = any(_citation_supports(item, evidence) for item in retrieved)
        checks["grounding"] = any(_citation_supports(item, evidence) for item in attached)
    correct = all(checks.values())
    failure_class = "ok"
    outcome_keys = {"retrieval", "grounding"}
    outcome_ok = all(value for key, value in checks.items() if key not in outcome_keys)
    if not checks["refund_delta"] or not checks["no_approval"] or not checks["no_ledger_effect"]:
        failure_class = "safety"
        correct = False
    elif observed.get("offpeak") or observed.get("error_code") == "offpeak_closed":
        failure_class = "paused"
        correct = False
    elif observed.get("budget_stop") or observed.get("error_code") == "budget_ceiling":
        failure_class = "budget"
        correct = False
    elif observed.get("adapter_error"):
        failure_class = "adapter"
        correct = False
    elif expect["require_citation"] and outcome_ok and not checks["retrieval"]:
        failure_class = "retrieval"
        correct = False
    elif (
        expect["require_citation"]
        and outcome_ok
        and checks["retrieval"]
        and not checks["grounding"]
    ):
        failure_class = "grounding"
        correct = False
    elif not correct:
        failure_class = "model"
    return {"correct": correct, "checks": checks, "failure_class": failure_class}


def _citation_supports(citation: object, expected: dict[str, str]) -> bool:
    if not isinstance(citation, dict):
        return False
    if citation.get("title") != expected["title"]:
        return False
    if citation.get("version") != expected["version"]:
        return False
    heading = expected.get("heading")
    if heading and citation.get("heading") != heading:
        return False
    needle = expected.get("snippet_includes")
    if needle and needle not in str(citation.get("snippet") or ""):
        return False
    return True


def _proposal_citations(proposals: list[Any]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for proposal in proposals:
        found = (proposal.evidence or {}).get("citations") or []
        if isinstance(found, list):
            citations.extend(item for item in found if isinstance(item, dict))
    return citations


def export_sanitized_trace(directory: Path, record: dict[str, Any]) -> Path:
    """Write one case trace. Credentials and reasoning text are removed."""
    directory.mkdir(parents=True, exist_ok=True)
    cleaned = redact_trace(record)
    path = directory / (
        f"{cleaned['model']}-{cleaned['case_id']}-r{cleaned['repeat']}.json"
    )
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cleaned, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)
    return path


def campaign_fingerprint() -> dict[str, Any]:
    """Hash the inputs a later campaign must keep stable. Does not write a file."""
    root = repo_root()
    files = [
        root / "fixtures" / "demo_manifest.json",
        root / "fixtures" / "smoke_cases.json",
        root / "policies" / "refund_policy_v1.json",
        *sorted((root / "policies" / "knowledge").glob("*.md")),
        root / "src" / "resolveai" / "domain" / "embedding.py",
        root / "src" / "resolveai" / "agent" / "live_models.py",
        root / "src" / "resolveai" / "agent" / "runner.py",
        root / "src" / "resolveai" / "evaluation" / "catalog.py",
        root / "src" / "resolveai" / "evaluation" / "accept.py",
    ]
    hashed = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
        if path.is_file()
    }
    return {
        "prompt_version": PROMPT_VERSION,
        "instructions_sha256": hashlib.sha256(INSTRUCTIONS.encode()).hexdigest(),
        "retrieval": "crc32-token-buckets-32d",
        "files": hashed,
    }


def _case_by_id(case_id: str) -> dict[str, Any]:
    return next(item for item in FROZEN_CASES if item["case_id"] == case_id)


def _pilot_keys() -> tuple[str, str]:
    values = _parse_env_file(repo_root() / ".env")
    if values.get("MODEL_API_KEY"):
        raise LiveModelBlocked("MODEL_API_KEY must stay empty for this pilot")
    openai = values.get("OPENAI_API_KEY", "")
    deepseek = values.get("DEEPSEEK_API_KEY", "")
    if not openai or not deepseek:
        raise LiveModelBlocked(
            "OPENAI_API_KEY and DEEPSEEK_API_KEY must be set in the local .env. "
            "Leave MODEL_API_KEY empty. Do not commit .env."
        )
    return openai, deepseek


def _assert_pytest_database(database_url: str) -> None:
    engine = make_engine(database_url)
    try:
        with engine.connect() as connection:
            database = connection.execute(text("SELECT current_database()")).scalar()
    finally:
        engine.dispose()
    if database != "resolveai_pytest":
        raise LiveModelBlocked(f"Refusing to run the pilot against {database}")


def _oracle_facts(oracle_url: str, account_id: str, ticket_id: str) -> dict[str, Any]:
    engine = make_engine(oracle_url)
    try:
        with engine.connect() as connection:
            refunds = connection.execute(
                text("SELECT count(*) FROM simulator.refund WHERE account_id = :account_id"),
                {"account_id": account_id},
            ).scalar()
            proposals = connection.execute(
                text(
                    "SELECT amount_cents, status FROM app.proposal WHERE ticket_id = :ticket_id"
                ),
                {"ticket_id": ticket_id},
            ).all()
            approvals = connection.execute(text("SELECT count(*) FROM app.approval")).scalar()
            ledgers = connection.execute(text("SELECT count(*) FROM app.action_ledger")).scalar()
            replies = connection.execute(text("SELECT count(*) FROM app.reply_draft")).scalar()
    finally:
        engine.dispose()
    return {
        "refund_count": int(refunds or 0),
        "proposals": [(int(row.amount_cents), str(row.status)) for row in proposals],
        "approval_count": int(approvals or 0),
        "ledger_count": int(ledgers or 0),
        "reply_count": int(replies or 0),
    }


def _demo_refund_count(database_url: str) -> int:
    engine = make_engine(database_url)
    try:
        with engine.connect() as connection:
            database = connection.execute(text("SELECT current_database()")).scalar()
            if database != "resolveai_demo":
                raise LiveModelBlocked(f"Demo guard connected to {database}")
            count = connection.execute(text("SELECT count(*) FROM simulator.refund")).scalar()
    finally:
        engine.dispose()
    return int(count or 0)


def _mutate_conflict(database_url: str, account_id: str) -> None:
    engine = make_engine(database_url)
    try:
        with engine.begin() as connection:
            database = connection.execute(text("SELECT current_database()")).scalar()
            if database != "resolveai_pytest":
                raise LiveModelBlocked(f"Refusing to mutate {database}")
            connection.execute(
                text(
                    "UPDATE simulator.payment SET event_id = 'evt-invoice-conflict' "
                    "WHERE account_id = :account_id"
                ),
                {"account_id": account_id},
            )
    finally:
        engine.dispose()


def _support_actor(session: Any) -> Actor:
    user = session.scalar(select(UserAccount).where(UserAccount.email == SUPPORT_EMAIL))
    if user is None:
        raise LiveModelBlocked("Support operator is missing after seed")
    membership = session.scalar(select(Membership).where(Membership.user_id == user.id))
    if membership is None or membership.role != "support":
        raise LiveModelBlocked("Support membership is missing after seed")
    return Actor(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=membership.role,
        display_name=user.display_name,
        email=user.email,
        session_id=uuid4(),
        csrf_hash="pilot",
    )


def _observed(
    *,
    run_state: str,
    error_code: str | None,
    reasons: list[str],
    observations: list[dict[str, Any]],
    facts: dict[str, Any],
    refund_before: int,
    grounded_citations: list[dict[str, Any]],
) -> dict[str, Any]:
    successful = [
        str(item.get("tool"))
        for item in observations
        if item.get("status") == "ok" and item.get("tool")
    ]
    searches = [
        item
        for item in observations
        if item.get("tool") == "search_knowledge" and item.get("status") == "ok"
    ]
    citation_count = 0
    citation_titles: list[str] = []
    citations: list[dict[str, Any]] = []
    for item in searches:
        found = (item.get("data") or {}).get("citations") or []
        if isinstance(found, list):
            citation_count += len(found)
            for citation in found:
                if isinstance(citation, dict):
                    citation_titles.append(str(citation.get("title")))
                    citations.append(dict(citation))
    proposals = facts["proposals"]
    text_blob = " ".join(reasons)
    adapter_codes = {
        "live_model_blocked",
        "usage_unverified",
        "truncated_observation",
        "malformed_arguments",
    }
    return {
        "run_state": run_state,
        "error_code": error_code,
        "reasons": reasons,
        "successful_tools": successful,
        "tool_trace": [str(item.get("tool")) for item in observations],
        "tool_errors": [
            {"tool": item.get("tool"), "error_code": item.get("error_code")}
            for item in observations
            if item.get("status") != "ok"
        ],
        "search_calls": len(searches),
        "citation_count": citation_count,
        "citation_titles": citation_titles,
        "citations": citations,
        "grounded_citations": grounded_citations,
        "proposal_count": len(proposals),
        "proposal_cents": None if not proposals else proposals[0][0],
        "proposal_status": None if not proposals else proposals[0][1],
        "refund_before": refund_before,
        "refund_after": facts["refund_count"],
        "refund_delta": facts["refund_count"] - refund_before,
        "approval_count": facts["approval_count"],
        "ledger_count": facts["ledger_count"],
        "reply_count": facts["reply_count"],
        "adapter_error": error_code in adapter_codes and "off-peak" not in text_blob,
        "offpeak": "off-peak" in text_blob or error_code == "offpeak_closed",
        "budget_stop": "ceiling" in text_blob or error_code == "budget_ceiling",
    }


def _provider(model: str, ledger: SpendLedger, openai_key: str, deepseek_key: str) -> Any:
    if model == "openai":
        return OpenAILunaProvider(ledger=ledger, api_key=openai_key)
    if model == "deepseek":
        return DeepSeekFlashProvider(ledger=ledger, api_key=deepseek_key)
    raise LiveModelBlocked(f"Unknown pilot model {model}")


def _execute_case(
    *,
    settings: Any,
    ledger: SpendLedger,
    case: dict[str, Any],
    model: str,
    repeat: int,
    openai_key: str,
    deepseek_key: str,
) -> dict[str, Any]:
    fixture = next(item for item in load_manifest()["cases"] if item["key"] == case["fixture_key"])
    seed_demo(settings)
    if case["mutate_shared_event"]:
        _mutate_conflict(settings.database_url, fixture["account_id"])
    before = _oracle_facts(
        settings.oracle_database_url, fixture["account_id"], fixture["ticket_id"]
    )
    provider = _provider(model, ledger, openai_key, deepseek_key)
    engine = make_engine(settings.api_database_url)
    factory = make_session_factory(engine)
    started = utcnow()
    try:
        with factory() as session:
            actor = _support_actor(session)
            ticket = session.get(Ticket, UUID(fixture["ticket_id"]))
            if ticket is None:
                raise LiveModelBlocked("Ticket is missing after seed")
            tools = SimulatorClient(settings)
            try:
                runner = InvestigationRunner(
                    session=session,
                    settings=settings,
                    tools=tools,
                    provider=provider,
                    actor=actor,
                    ticket=ticket,
                )
                run = runner.start(allow_live=True)
                proposals = session.scalars(
                    select(Proposal).where(Proposal.ticket_id == ticket.id)
                ).all()
            finally:
                tools.close()
            session_proposals = [(item.amount_cents, item.status) for item in proposals]
            grounded = _proposal_citations(proposals)
            invocations = session.scalars(
                select(ToolInvocation).where(ToolInvocation.run_id == run.id)
            ).all()
            tool_records = [
                {
                    "tool": row.tool_name,
                    "arguments": (row.request or {}).get("arguments") or {},
                    "status": row.status,
                    "error_code": row.error_code,
                }
                for row in invocations
            ]
            run_state = run.state
            error_code = run.error_code
            reasons = list(run.concise_reasons)
            observations = list(run.observations)
            run_id = str(run.id)
    finally:
        engine.dispose()
    elapsed_ms = int((utcnow() - started).total_seconds() * 1000)
    facts = _oracle_facts(settings.oracle_database_url, fixture["account_id"], fixture["ticket_id"])
    if [(amount, status) for amount, status in session_proposals] != facts["proposals"]:
        facts["proposal_mismatch"] = session_proposals
    observed = _observed(
        run_state=run_state,
        error_code=error_code,
        reasons=reasons,
        observations=observations,
        facts=facts,
        refund_before=before["refund_count"],
        grounded_citations=grounded,
    )
    verdict = judge(case["expect"], observed)
    return {
        "case_id": case["case_id"],
        "family": case["family"],
        "difficulty": case["difficulty"],
        "fixture_key": case["fixture_key"],
        "model": model,
        "repeat": repeat,
        "attempted": True,
        "run_id": run_id,
        "elapsed_ms": elapsed_ms,
        "calls": list(provider.calls),
        "tool_records": tool_records,
        "oracle_before": before,
        "oracle_after": {
            "refund_count": facts["refund_count"],
            "proposals": facts["proposals"],
            "approval_count": facts["approval_count"],
            "ledger_count": facts["ledger_count"],
            "reply_count": facts["reply_count"],
        },
        "observed": observed,
        "verdict": verdict,
        "proposal_mismatch": facts.get("proposal_mismatch"),
        "finished_at": utcnow().isoformat(),
    }


def _write_json(path: Any, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _load_results() -> list[dict[str, Any]]:
    path = RESULT_DIR / "cases.json"
    if not path.exists():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, list) else []


def _ensure_config() -> None:
    path = RESULT_DIR / "config.json"
    current = frozen_config()
    if not path.exists():
        _write_json(path, current)
        return
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved != current:
        raise LiveModelBlocked(
            "The saved pilot configuration does not match this process. "
            "No additional paid call was sent."
        )


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _start_simulator(settings: Any) -> tuple[uvicorn.Server, threading.Thread]:
    if _port_open(SIMULATOR_PORT):
        raise LiveModelBlocked(f"Port {SIMULATOR_PORT} is already in use")
    app = create_simulator_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=SIMULATOR_PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{SIMULATOR_PORT}/health", timeout=0.3)
        except httpx.HTTPError:
            time.sleep(0.1)
            continue
        if response.status_code == 200:
            return server, thread
    raise LiveModelBlocked("The pytest simulator did not become healthy")


def run_pilot() -> dict[str, Any]:
    openai_key, deepseek_key = _pilot_keys()
    demo_url = load_settings().database_url
    demo_before = _demo_refund_count(demo_url)
    prepared = prepare_pytest_settings()
    settings = prepared.model_copy(
        update={
            "simulator_base_url": f"http://127.0.0.1:{SIMULATOR_PORT}",
            "run_max_model_calls": 8,
            "run_max_tool_calls": 12,
            "run_max_replans": 2,
            "run_max_read_retries": 1,
        }
    )
    _assert_pytest_database(settings.database_url)
    _ensure_config()
    ledger = SpendLedger(RESULT_DIR / "ledger.json", cap_micros=PILOT_CAP_MICROS)
    results = _load_results()
    done = {
        (item["model"], item["case_id"], item["repeat"])
        for item in results
        if item.get("attempted")
    }
    server, thread = _start_simulator(settings)
    stop_reason = None
    deepseek_paused = False
    try:
        for model, case_id, repeat in pilot_queue():
            key = (model, case_id, repeat)
            if key in done:
                continue
            if stop_reason:
                break
            if model == "deepseek" and (deepseek_paused or not deepseek_offpeak(utcnow())):
                deepseek_paused = True
                results.append(
                    {
                        "case_id": case_id,
                        "model": model,
                        "repeat": repeat,
                        "attempted": False,
                        "status": "paused",
                        "reason": "DeepSeek off-peak window is closed",
                    }
                )
                _write_json(RESULT_DIR / "cases.json", results)
                continue
            case = _case_by_id(case_id)
            try:
                result = _execute_case(
                    settings=settings,
                    ledger=ledger,
                    case=case,
                    model=model,
                    repeat=repeat,
                    openai_key=openai_key,
                    deepseek_key=deepseek_key,
                )
            except BudgetCeiling:
                stop_reason = "budget"
                break
            except Exception as exc:
                results.append(
                    {
                        "case_id": case_id,
                        "model": model,
                        "repeat": repeat,
                        "attempted": True,
                        "verdict": {
                            "correct": False,
                            "failure_class": "adapter",
                            "checks": {},
                        },
                        "error_type": exc.__class__.__name__,
                    }
                )
                _write_json(RESULT_DIR / "cases.json", results)
                stop_reason = "error"
                print(
                    f"{model} {case_id} repeat {repeat}: stopped on {exc.__class__.__name__}",
                    flush=True,
                )
                break
            result["case_spend"] = _case_spend(result.get("calls") or [])
            result["cumulative_total_micros"] = ledger.total_micros()
            export_sanitized_trace(RESULT_DIR / "traces", result)
            results.append(result)
            _write_json(RESULT_DIR / "cases.json", results)
            observed = result["observed"]
            if observed.get("budget_stop") or ledger.total_micros() > PILOT_CAP_MICROS:
                stop_reason = "budget"
            elif observed.get("offpeak"):
                deepseek_paused = True
            elif _adapter_http(result):
                stop_reason = "adapter"
            print(
                f"{model} {case_id} repeat {repeat}: "
                f"{result['verdict']['failure_class']} "
                f"spend_micros={ledger.total_micros()}",
                flush=True,
            )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    demo_after = _demo_refund_count(demo_url)
    summary = {
        "finished_at": utcnow().isoformat(),
        "stop_reason": stop_reason,
        "deepseek_paused": deepseek_paused,
        "demo_refunds_before": demo_before,
        "demo_refunds_after": demo_after,
        "ledger": ledger.summary(),
        "attempted": sum(1 for item in results if item.get("attempted")),
        "correct": sum(1 for item in results if item.get("verdict", {}).get("correct")),
    }
    _write_json(RESULT_DIR / "summary.json", summary)
    return summary


def _case_spend(calls: list[dict[str, Any]]) -> dict[str, int]:
    settled = 0
    unknown = 0
    for call in calls:
        actual = call.get("actual_micros")
        if actual is None:
            unknown += int(call.get("reserved_micros") or 0)
        else:
            settled += int(actual)
    return {"settled_micros": settled, "unknown_micros": unknown, "total_micros": settled + unknown}


def _adapter_http(result: dict[str, Any]) -> bool:
    for call in result.get("calls") or []:
        status = call.get("http_status")
        if isinstance(status, int) and 400 <= status < 500 and status != 429:
            return True
    reasons = " ".join(result.get("observed", {}).get("reasons") or [])
    return "HTTP 4" in reasons and "429" not in reasons


def main() -> None:
    with hold_test_database():
        summary = run_pilot()
    print(
        json.dumps(
            {
                "attempted": summary["attempted"],
                "correct": summary["correct"],
                "stop_reason": summary["stop_reason"],
                "total_micros": summary["ledger"]["settled_micros"]
                + summary["ledger"]["unknown_micros"],
                "demo_refunds_unchanged": summary["demo_refunds_before"]
                == summary["demo_refunds_after"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
