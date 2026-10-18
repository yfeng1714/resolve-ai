"""Aggregate spend for the authorized pilot. One file, one request at a time."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from resolveai.agent.pricing import PILOT_CAP_MICROS
from resolveai.settings import LiveModelBlocked


class BudgetCeiling(LiveModelBlocked):
    """The next reservation would pass the authorized total, or a settled call overran."""

    code = "budget_ceiling"


@dataclass
class Reservation:
    request_id: str
    reserved_micros: int


class SpendLedger:
    def __init__(self, path: Path, cap_micros: int = PILOT_CAP_MICROS) -> None:
        self.path = path
        self.cap_micros = cap_micros
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = path.with_suffix(".lock")
        if not self.path.exists():
            self._write(
                {"settled_micros": 0, "unknown_micros": 0, "breached": False, "entries": []}
            )

    def total_micros(self) -> int:
        with self._lock():
            state = self._read()
        return int(state["settled_micros"]) + int(state["unknown_micros"])

    def reserve(self, *, provider: str, model: str, reserved_micros: int) -> Reservation:
        if reserved_micros <= 0:
            raise LiveModelBlocked("A live request needs a positive cost reservation")
        with self._lock():
            state = self._read()
            used = int(state["settled_micros"]) + int(state["unknown_micros"])
            if state.get("breached"):
                raise BudgetCeiling(
                    "A previous request settled above its reservation. No call was sent."
                )
            if used + reserved_micros > self.cap_micros:
                raise BudgetCeiling(
                    "The next request would exceed the spending ceiling. No call was sent."
                )
            request_id = uuid4().hex
            state["unknown_micros"] = int(state["unknown_micros"]) + reserved_micros
            state["entries"].append(
                {
                    "request_id": request_id,
                    "provider": provider,
                    "model": model,
                    "reserved_micros": reserved_micros,
                    "status": "unknown",
                    "actual_micros": None,
                    "estimated": None,
                    "usage": {},
                }
            )
            self._write(state)
        return Reservation(request_id=request_id, reserved_micros=reserved_micros)

    def reconcile(
        self,
        request_id: str,
        *,
        actual_micros: int | None,
        estimated: bool | None,
        usage: dict,
        over_reservation: bool = False,
    ) -> None:
        with self._lock():
            state = self._read()
            entry = next(
                (item for item in state["entries"] if item["request_id"] == request_id),
                None,
            )
            if entry is None:
                raise LiveModelBlocked(
                    "Spend ledger is missing a reservation that was already made"
                )
            reserved = int(entry["reserved_micros"])
            if entry["status"] == "settled":
                return
            state["unknown_micros"] = int(state["unknown_micros"]) - reserved
            if actual_micros is None:
                state["unknown_micros"] = int(state["unknown_micros"]) + reserved
                entry["status"] = "unknown"
                entry["estimated"] = True
            else:
                settled = int(actual_micros)
                state["settled_micros"] = int(state["settled_micros"]) + settled
                entry["status"] = "settled"
                entry["actual_micros"] = settled
                entry["estimated"] = bool(estimated)
                if settled > reserved or over_reservation:
                    state["breached"] = True
                    entry["over_reservation"] = True
            entry["usage"] = usage
            self._write(state)

    def summary(self) -> dict:
        with self._lock():
            state = self._read()
        return {
            "cap_micros": self.cap_micros,
            "settled_micros": int(state["settled_micros"]),
            "unknown_micros": int(state["unknown_micros"]),
            "breached": bool(state.get("breached")),
            "entries": list(state["entries"]),
        }

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"settled_micros": 0, "unknown_micros": 0, "breached": False, "entries": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, state: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
