"""M2 acceptance batch for gpt-6-luna. The default API stays on the fake provider.

This spends money only when executed directly. It uses resolveai_pytest, resets
that database between catalog cases, and refuses any other database. The hard
cap for this batch is $2, including retries and failures. It is not increased
automatically. Results go to a new directory. The 2026-10-17 files under
`eval/results/m2-luna-acceptance/` stay as historical evidence.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import traceback
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import uvicorn
from sqlalchemy import select, text
from starlette.testclient import TestClient

from resolveai.agent.live_models import PROMPT_VERSION, redact_trace
from resolveai.agent.spend import SpendLedger
from resolveai.api.app import create_api_app
from resolveai.clock import utcnow
from resolveai.db.models import Run, ToolInvocation
from resolveai.db.session import make_engine, make_session_factory
from resolveai.evaluation.catalog import catalog_ids, execute_catalog_case
from resolveai.evaluation.live_pilot import campaign_fingerprint
from resolveai.seed.demo import load_manifest, seed_demo
from resolveai.settings import LiveModelBlocked, _parse_env_file, load_settings, repo_root
from resolveai.simulator.app import create_simulator_app
from resolveai.testing import hold_test_database, prepare_pytest_settings

RESULT_DIR = repo_root() / "eval" / "results" / "m2-luna-rereview"
SIMULATOR_PORT = 18081
ACCEPTANCE_CAP_USD = Decimal("2")
CAP_MICROS = int(ACCEPTANCE_CAP_USD * 1_000_000)


def _keys() -> str:
    values = _parse_env_file(repo_root() / ".env")
    if values.get("MODEL_API_KEY"):
        raise LiveModelBlocked("MODEL_API_KEY must stay empty")
    key = values.get("OPENAI_API_KEY", "")
    if not key:
        raise LiveModelBlocked("OPENAI_API_KEY is missing from the local .env")
    return key


def _demo_refunds(url: str) -> int:
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            database = connection.execute(text("SELECT current_database()")).scalar()
            if database != "resolveai_demo":
                raise LiveModelBlocked(f"Demo guard connected to {database}")
            count = connection.execute(text("SELECT count(*) FROM simulator.refund")).scalar()
    finally:
        engine.dispose()
    return int(count or 0)


def _acceptance_settings():
    prepared = prepare_pytest_settings()
    database = make_engine(prepared.database_url)
    try:
        with database.connect() as connection:
            name = connection.execute(text("SELECT current_database()")).scalar()
    finally:
        database.dispose()
    if name != "resolveai_pytest":
        raise LiveModelBlocked(f"Refusing to run acceptance against {name}")
    return prepared.model_copy(
        update={
            "simulator_base_url": f"http://127.0.0.1:{SIMULATOR_PORT}",
            "web_origin": "http://127.0.0.1:15173",
            "allow_live": True,
            "model_provider": "openai",
            "model_name": "gpt-6-luna",
            "model_api_key": "",
            "llm_budget_usd": ACCEPTANCE_CAP_USD,
        }
    )


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _start_simulator(settings) -> tuple[uvicorn.Server, threading.Thread]:
    if _port_open(SIMULATOR_PORT):
        raise LiveModelBlocked(f"Port {SIMULATOR_PORT} is already in use")
    app = create_simulator_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=SIMULATOR_PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{SIMULATOR_PORT}/health", timeout=0.3)
        except httpx.HTTPError:
            time.sleep(0.1)
            continue
        if response.status_code == 200:
            return server, thread
    raise LiveModelBlocked("The acceptance simulator did not become healthy")


def _fixture_for_case(case_id: str, manifest: dict) -> dict[str, Any]:
    smoke = json.loads((repo_root() / "fixtures" / "smoke_cases.json").read_text(encoding="utf-8"))
    label = next(item for item in smoke["cases"] if item["case_id"] == case_id)
    aliases = {
        "synthetic-conflict": "distinct-invoices",
        "pending-payment-followup": "pending-payment",
        "approver-only": "duplicate-approve",
        "stale-version": "duplicate-approve",
        "withdrawal": "duplicate-approve",
        "stale-payment": "duplicate-approve",
        "lost-response": "duplicate-approve",
        "model-limit": "duplicate-approve",
        "live-blocked": "duplicate-approve",
    }
    fixture_key = aliases.get(label["fixture_key"], label["fixture_key"])
    if fixture_key not in {item["key"] for item in manifest["cases"]}:
        fixture_key = "duplicate-approve"
    return next(item for item in load_manifest()["cases"] if item["key"] == fixture_key)


def _oracle_facts(settings, ticket_id: str, account_id: str) -> dict[str, Any]:
    engine = make_engine(settings.oracle_database_url)
    try:
        with engine.connect() as connection:
            refunds = connection.execute(
                text(
                    """
                    SELECT id::text AS id, payment_id::text AS payment_id,
                           amount_cents, status
                    FROM simulator.refund
                    WHERE account_id = :account_id
                    ORDER BY id
                    """
                ),
                {"account_id": account_id},
            ).mappings()
            approvals = connection.execute(
                text("SELECT decision FROM app.approval ORDER BY created_at")
            ).mappings()
            replies = connection.execute(
                text(
                    """
                    SELECT status FROM app.reply_draft
                    WHERE ticket_id = :ticket_id
                    ORDER BY revision
                    """
                ),
                {"ticket_id": ticket_id},
            ).mappings()
            ledgers = connection.execute(
                text(
                    """
                    SELECT action_type, status FROM app.action_ledger
                    ORDER BY created_at
                    """
                )
            ).mappings()
            return {
                "refunds": [dict(row) for row in refunds],
                "approvals": [dict(row) for row in approvals],
                "replies": [dict(row) for row in replies],
                "ledgers": [dict(row) for row in ledgers],
            }
    finally:
        engine.dispose()


def _case_trace(settings, case_id: str, manifest: dict) -> dict[str, Any]:
    from resolveai.db.models import Run, ToolInvocation
    from resolveai.db.session import make_session_factory

    chosen = _fixture_for_case(case_id, manifest)
    ticket_id = chosen["ticket_id"]
    account_id = chosen["account_id"]
    engine = make_engine(settings.oracle_database_url)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            runs = session.scalars(
                select(Run).where(Run.ticket_id == ticket_id).order_by(Run.created_at)
            ).all()
            exported = []
            for run in runs:
                invocations = session.scalars(
                    select(ToolInvocation).where(ToolInvocation.run_id == run.id)
                ).all()
                exported.append(
                    {
                        "run_id": str(run.id),
                        "state": run.state,
                        "error_code": run.error_code,
                        "provider": run.provider,
                        "prompt_version": run.prompt_version,
                        "message_revision": run.message_revision,
                        "model_calls": run.model_calls,
                        "tool_calls": run.tool_calls,
                        "tools": [
                            {
                                "tool": row.tool_name,
                                "arguments": (row.request or {}).get("arguments") or {},
                                "status": row.status,
                                "error_code": row.error_code,
                            }
                            for row in invocations
                        ],
                        "observations": list(run.observations),
                    }
                )
            return redact_trace(
                {
                    "case_id": case_id,
                    "ticket_id": ticket_id,
                    "runs": exported,
                    "oracle_after": _oracle_facts(settings, ticket_id, account_id),
                }
            )
    finally:
        engine.dispose()


def _latest_trace(settings, ticket_hint: str | None) -> dict[str, Any]:
    engine = make_engine(settings.api_database_url)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            run = session.scalar(select(Run).order_by(Run.created_at.desc()))
            if run is None:
                return {}
            invocations = session.scalars(
                select(ToolInvocation).where(ToolInvocation.run_id == run.id)
            ).all()
            return redact_trace(
                {
                    "run_id": str(run.id),
                    "ticket_id": str(run.ticket_id),
                    "state": run.state,
                    "error_code": run.error_code,
                    "provider": run.provider,
                    "prompt_version": run.prompt_version,
                    "model_calls": run.model_calls,
                    "tool_calls": run.tool_calls,
                    "budget_usd_micros": run.budget_usd_micros,
                    "tools": [
                        {
                            "tool": row.tool_name,
                            "arguments": (row.request or {}).get("arguments") or {},
                            "status": row.status,
                            "error_code": row.error_code,
                        }
                        for row in invocations
                    ],
                    "observations": list(run.observations),
                    "ticket_hint": ticket_hint,
                }
            )
    finally:
        engine.dispose()


def _campaign_identity() -> dict[str, Any]:
    return {
        "model": "gpt-6-luna",
        "reasoning_effort": "low",
        "prompt_version": PROMPT_VERSION,
        "cap_usd": str(ACCEPTANCE_CAP_USD),
        "protocol": "responses",
        "route": "https://api.openai.com/v1/responses",
        "fingerprint": campaign_fingerprint(),
    }


def _mismatch_summary() -> dict[str, Any]:
    return {
        "finished_at": utcnow().isoformat(),
        "stop_reason": "config_mismatch",
        "attempted": 0,
        "passed": 0,
        "demo_refunds_before": None,
        "demo_refunds_after": None,
        "ledger": {
            "entries": [],
            "settled_micros": 0,
            "unknown_micros": 0,
            "breached": False,
        },
    }


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def _passed_case_ids() -> set[str]:
    path = RESULT_DIR / "attempts.jsonl"
    if not path.exists():
        return set()
    passed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("passed") is True and item.get("case_id"):
            passed.add(item["case_id"])
    return passed


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def run_acceptance() -> dict[str, Any]:
    identity = _campaign_identity()
    config_path = RESULT_DIR / "config.json"
    if config_path.exists():
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        if saved.get("identity") != identity:
            return _mismatch_summary()
        campaign_id = str(saved["campaign_id"])
    else:
        campaign_id = uuid4().hex
        _write(
            config_path,
            {
                "campaign_id": campaign_id,
                "identity": identity,
                "started_at": utcnow().isoformat(),
            },
        )
    key = _keys()
    demo_url = load_settings().database_url
    demo_before = _demo_refunds(demo_url)
    settings = _acceptance_settings()
    fake_settings = settings.model_copy(
        update={
            "allow_live": False,
            "model_provider": "fake",
            "model_name": "",
            "llm_budget_usd": Decimal("0"),
        }
    )
    ledger_path = RESULT_DIR / "ledger.json"
    manifest = load_manifest()
    done = _passed_case_ids()
    server, thread = _start_simulator(settings)
    live_app = create_api_app(settings, live_api_key=key, ledger_path=ledger_path)
    fake_app = create_api_app(fake_settings)
    ledger = SpendLedger(ledger_path, cap_micros=CAP_MICROS)
    stop_reason = None
    attempts: list[dict[str, Any]] = []
    try:
        for case_id in catalog_ids():
            if case_id in done:
                continue
            if stop_reason:
                break
            before = ledger.total_micros()
            started = time.perf_counter()
            app = fake_app if case_id == "safe-04" else live_app
            active_settings = fake_settings if case_id == "safe-04" else settings
            passed = False
            error = None
            oracle_before = None
            try:
                seed_demo(active_settings)
                fixture = _fixture_for_case(case_id, manifest)
                oracle_before = _oracle_facts(
                    active_settings, fixture["ticket_id"], fixture["account_id"]
                )
                with TestClient(app) as client:
                    execute_catalog_case(case_id, client, active_settings, manifest, app)
                passed = True
            except Exception as exc:  # noqa: BLE001 - record the case and continue
                error = f"{exc.__class__.__name__}: {exc}"
                traceback.print_exc()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            attempt_id = uuid4().hex
            trace = _case_trace(active_settings, case_id, manifest)
            if trace:
                trace["oracle_before"] = oracle_before
            if trace:
                _write(RESULT_DIR / "traces" / case_id / f"{attempt_id}.json", trace)
            record = {
                "attempt_id": attempt_id,
                "campaign_id": campaign_id,
                "case_id": case_id,
                "provider": "fake" if case_id == "safe-04" else "openai",
                "model": "" if case_id == "safe-04" else "gpt-6-luna",
                "prompt_version": PROMPT_VERSION,
                "passed": passed,
                "elapsed_ms": elapsed_ms,
                "error": None if passed else str(error)[:500],
                "spend_delta_micros": ledger.total_micros() - before,
                "cumulative_micros": ledger.total_micros(),
                "run_count": 0 if not trace else len(trace.get("runs") or []),
                "finished_at": utcnow().isoformat(),
            }
            _append_jsonl(RESULT_DIR / "attempts.jsonl", record)
            attempts.append(record)
            print(
                f"{case_id}: {'pass' if passed else 'fail'} "
                f"spend_micros={ledger.total_micros()}",
                flush=True,
            )
            summary = ledger.summary()
            if summary.get("breached") or ledger.total_micros() > CAP_MICROS:
                stop_reason = "budget"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    demo_after = _demo_refunds(demo_url)
    summary = {
        "finished_at": utcnow().isoformat(),
        "stop_reason": stop_reason,
        "attempted": len(attempts),
        "passed": sum(1 for item in attempts if item.get("passed")),
        "demo_refunds_before": demo_before,
        "demo_refunds_after": demo_after,
        "ledger": SpendLedger(ledger_path, cap_micros=CAP_MICROS).summary(),
    }
    _write(RESULT_DIR / "summary.json", summary)
    return summary


def serve_demo() -> None:
    """Reseed the pytest database and serve the live API for the browser demo."""
    key = _keys()
    settings = _acceptance_settings()
    seed_demo(settings)
    _start_simulator(settings)
    app = create_api_app(settings, live_api_key=key, ledger_path=RESULT_DIR / "ledger.json")
    uvicorn.run(app, host="127.0.0.1", port=18080, log_level="warning")


def main() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        with hold_test_database():
            serve_demo()
        return
    with hold_test_database():
        summary = run_acceptance()
    print(
        json.dumps(
            {
                "attempted": summary["attempted"],
                "passed": summary["passed"],
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
