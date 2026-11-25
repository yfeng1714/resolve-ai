"""Account-scoped incident classification. This does not prove a cause."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from resolveai.domain.cancellation import canonical_instant, parse_instant
from resolveai.domain.hashing import canonical_hash

SERVICES = ("api", "billing", "auth")
REGIONS = ("us-east", "eu-west")
STATUSES = ("investigating", "identified", "resolved")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CREDIT = re.compile(r"\bcredit\b")


def incident_requested(messages: list[dict[str, Any]]) -> bool:
    text = "\n".join(str(item.get("body") or "") for item in messages).lower()
    return "api failure" in text or compensation_requested(messages)


def compensation_requested(messages: list[dict[str, Any]]) -> bool:
    text = "\n".join(str(item.get("body") or "") for item in messages).lower()
    return "compensation" in text or "sla credit" in text or _CREDIT.search(text) is not None


def validate_request_id(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request id is not valid")
    return value


def snapshot_digest(rows: list[dict[str, Any]]) -> str:
    ordered = sorted(rows, key=lambda item: str(item["id"]))
    return canonical_hash({"incidents": ordered})


def parse_snapshot(account_id: str, payload: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Return canonical rows, or None when the payload is not a valid list."""
    raw = (payload or {}).get("incidents")
    if not isinstance(raw, list):
        return None
    parsed: list[dict[str, Any]] = []
    for item in raw:
        row = _parse_row(account_id, item)
        if row is None:
            return None
        parsed.append(row)
    return parsed


def _parse_row(account_id: str, item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if str(item.get("account_id") or "") != account_id:
        return None
    service = str(item.get("service_code") or "")
    region = str(item.get("region_code") or "")
    status = str(item.get("status") or "")
    if service not in SERVICES or region not in REGIONS or status not in STATUSES:
        return None
    try:
        version = item.get("resource_version")
        if type(version) is not int or version < 1:
            return None
        started = _instant(item.get("started_at"))
        resolved = item.get("resolved_at")
        resolved_at = None if resolved in (None, "") else _instant(resolved)
    except (TypeError, ValueError):
        return None
    if status == "resolved":
        if resolved_at is None or resolved_at < started:
            return None
    elif resolved_at is not None:
        return None
    references = item.get("reference_ids")
    if not isinstance(references, list) or any(not isinstance(value, str) for value in references):
        return None
    identity = _incident_identity(item.get("id"))
    if identity is None:
        return None
    return {
        "id": identity,
        "account_id": account_id,
        "service_code": service,
        "region_code": region,
        "status": status,
        "started_at": started,
        "resolved_at": resolved_at,
        "reference_ids": list(references),
        "resource_version": version,
        "summary": str(item.get("summary") or ""),
    }


def _incident_identity(value: Any) -> str | None:
    if not isinstance(value, str) or value == "":
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _instant(value: Any) -> str:
    if isinstance(value, datetime):
        return canonical_instant(value)
    text = str(value)
    parse_instant(text)
    return text


def classify_incidents(
    rows: list[dict[str, Any]],
    *,
    service_code: str,
    region_code: str,
    reported_started_at: str,
    request_id: str | None,
    compensation: bool,
) -> dict[str, Any]:
    """Return one outcome. Earlier rules win. Confirmed is a record match only."""
    full: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    resolved_outside: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    for row in rows:
        kind = _class_row(
            row,
            service_code=service_code,
            region_code=region_code,
            reported_started_at=reported_started_at,
            request_id=request_id,
        )
        item = {**row, "match_class": kind}
        if kind == "full":
            full.append(item)
        elif kind == "request_mismatch":
            mismatched.append(item)
        elif kind == "resolved_outside":
            resolved_outside.append(item)
        elif kind == "partial":
            partial.append(item)
    evidence = full + mismatched + resolved_outside + partial
    if compensation:
        return _outcome("compensation", "compensation_review", None, evidence)
    if len(full) == 1:
        return _outcome("confirmed", "record_match", full[0], evidence)
    if len(full) > 1:
        return _outcome("several", "several_matches", None, evidence)
    if mismatched:
        return _outcome("possible", "request_id_not_listed", None, evidence)
    if resolved_outside:
        return _outcome("possible", "resolved_before_report", None, evidence)
    if partial:
        return _outcome("possible", "partial_overlap", None, evidence)
    return _outcome("none", "no_overlap", None, evidence)


def _class_row(
    row: dict[str, Any],
    *,
    service_code: str,
    region_code: str,
    reported_started_at: str,
    request_id: str | None,
) -> str:
    same_service = row["service_code"] == service_code
    same_region = row["region_code"] == region_code
    overlaps = _overlaps(row, reported_started_at)
    resolved_at = row["resolved_at"]
    if (
        same_service
        and same_region
        and resolved_at is not None
        and reported_started_at >= resolved_at
    ):
        return "resolved_outside"
    if same_service and same_region and overlaps:
        if request_id is None or request_id in row["reference_ids"]:
            return "full"
        return "request_mismatch"
    if same_service or same_region or overlaps:
        return "partial"
    return "unrelated"


def _overlaps(row: dict[str, Any], reported_started_at: str) -> bool:
    if reported_started_at < row["started_at"]:
        return False
    resolved_at = row["resolved_at"]
    return resolved_at is None or reported_started_at < resolved_at


def _outcome(
    kind: str,
    reason: str,
    selected: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "reason_code": reason,
        "incident_id": None if selected is None else selected["id"],
        "incident_resource_version": None if selected is None else selected["resource_version"],
        "evidence": [_public_candidate(item) for item in evidence],
    }


def _public_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "match_class": item["match_class"],
        "service_code": item["service_code"],
        "region_code": item["region_code"],
        "status": item["status"],
        "started_at": item["started_at"],
        "resolved_at": item["resolved_at"],
        "resource_version": item["resource_version"],
    }


def reply_body(outcome: dict[str, Any], *, customer_name: str) -> str:
    kind = outcome["kind"]
    if kind == "confirmed":
        selected = next(
            item for item in outcome["evidence"] if item.get("match_class") == "full"
        )
        return (
            f"Hello {customer_name},\n\n"
            f"Incident {selected['id']} matches the reported {selected['service_code']} "
            f"service in {selected['region_code']}. Its status is {selected['status']}. "
            "This match does not show that the incident caused the errors. "
            "The record does not include a repair time.\n"
        )
    if kind == "several":
        names = ", ".join(
            item["id"] for item in outcome["evidence"] if item.get("match_class") == "full"
        )
        return (
            f"Hello {customer_name},\n\n"
            f"More than one incident matches the report: {names}. "
            "None was selected, and none is confirmed as the cause.\n"
        )
    if kind == "possible":
        resolved = next(
            (
                item["resolved_at"]
                for item in outcome["evidence"]
                if item["match_class"] == "resolved_outside"
            ),
            None,
        )
        when = f" One record resolved at {resolved}." if resolved else ""
        return (
            f"Hello {customer_name},\n\n"
            "The incident records are not confirmed as the cause of this report."
            f"{when}\n"
        )
    if kind == "compensation":
        return (
            f"Hello {customer_name},\n\n"
            "A person must review any credit or refund. None was approved.\n"
        )
    if kind == "none":
        return (
            f"Hello {customer_name},\n\n"
            "No matching incident was found on this account. "
            "That is not a statement that the service is healthy.\n"
        )
    return (
        f"Hello {customer_name},\n\nThe incident result is unknown. No repair time is available.\n"
    )
