from typing import Any
from uuid import uuid4

from fastapi.responses import JSONResponse

from resolveai.clock import utcnow


def request_id_from(value: str | None) -> str:
    if value and len(value) <= 80 and value.replace("-", "").isalnum():
        return value
    return uuid4().hex


def envelope(
    *,
    data: Any,
    request_id: str,
    error_code: str | None = None,
    message: str | None = None,
    retryable: bool = False,
    operation_id: str | None = None,
    resource_version: int | None = None,
    status_code: int = 200,
) -> JSONResponse:
    status = "ok" if error_code is None else "error"
    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "data": data,
            "error_code": error_code,
            "message": message,
            "retryable": retryable,
            "operation_id": operation_id,
            "resource_version": resource_version,
            "observed_at": utcnow().isoformat(),
            "request_id": request_id,
        },
    )
