"""Runtime configuration. M1 refuses live model calls and non-local databases."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.engine.url import make_url

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
LOCAL_DATABASES = frozenset({"resolveai_demo", "resolveai_pytest"})


class SettingsError(RuntimeError):
    pass


class LiveModelBlocked(RuntimeError):
    """Raised when a billable or non-fake model call is requested."""


class Settings(BaseModel):
    resolveai_env: str = "demo"
    database_url: str = (
        "postgresql+psycopg://resolveai_demo:resolveai_local_demo@127.0.0.1:55432/resolveai_demo"
    )
    api_database_url: str = (
        "postgresql+psycopg://resolveai_api:resolveai_api_local@127.0.0.1:55432/resolveai_demo"
    )
    simulator_database_url: str = (
        "postgresql+psycopg://resolveai_simulator:resolveai_simulator_local"
        "@127.0.0.1:55432/resolveai_demo"
    )
    oracle_database_url: str = (
        "postgresql+psycopg://resolveai_oracle:resolveai_oracle_local"
        "@127.0.0.1:55432/resolveai_demo"
    )
    simulator_base_url: str = "http://127.0.0.1:8001"
    simulator_token: str = "resolveai_simulator_local_token"
    simulator_allow_faults: bool = False
    web_origin: str = "http://127.0.0.1:5173"
    model_provider: str = "fake"
    model_name: str = ""
    model_api_key: str = ""
    embedding_provider: str = "fake"
    embedding_model: str = ""
    llm_budget_usd: Decimal = Decimal("0")
    allow_live: bool = False
    run_max_model_calls: int = Field(default=12, ge=1)
    run_max_tool_calls: int = Field(default=20, ge=1)
    run_max_replans: int = Field(default=2, ge=0)
    run_max_read_retries: int = Field(default=2, ge=0)
    session_ttl_hours: int = Field(default=12, ge=1, le=24)


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "policies").is_dir():
            return parent
    raise SettingsError("Could not locate the ResolveAI repository root")


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _local_database(url: str, label: str) -> None:
    try:
        parsed = make_url(url)
    except Exception as exc:  # noqa: BLE001 - configuration boundary
        raise SettingsError(f"{label} is not a valid database URL") from exc
    host = (parsed.host or "").lower()
    if host not in LOCAL_HOSTS or parsed.database not in LOCAL_DATABASES:
        raise SettingsError(
            f"{label} must target a local ResolveAI database ({', '.join(sorted(LOCAL_DATABASES))})"
        )


def _local_http(url: str, label: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in LOCAL_HOSTS:
        raise SettingsError(f"{label} must be an http(s) URL on localhost")


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Load settings from the process environment, filling gaps from `.env`.

    Passing `environ` isolates the loader from the developer `.env` and process
    environment so tests can exercise specific configurations. This loader always
    stays on the fake provider. Owner live mode uses `load_owner_live_settings`.
    """
    settings = _build_settings(_merged_environ(environ))
    ensure_fake_provider(settings)
    return settings


def load_owner_live_settings(environ: dict[str, str] | None = None) -> tuple[Settings, str]:
    """Load an owner-started Luna process. The default API does not call this.

    Requires `RESOLVEAI_ALLOW_LIVE=1`, provider `openai`, model `gpt-6-luna`,
    a positive `LLM_BUDGET_USD`, and `OPENAI_API_KEY` in the same environment.
    `MODEL_API_KEY` stays empty. The returned key is not stored on Settings.
    """
    merged = _merged_environ(environ)
    if merged.get("RESOLVEAI_ALLOW_LIVE") != "1":
        raise LiveModelBlocked(
            "Live mode requires RESOLVEAI_ALLOW_LIVE=1 in the process environment"
        )
    if merged.get("MODEL_API_KEY"):
        raise LiveModelBlocked("MODEL_API_KEY must stay empty; live mode reads OPENAI_API_KEY")
    settings = _build_settings(merged).model_copy(
        update={"allow_live": True, "model_api_key": ""}
    )
    key = merged.get("OPENAI_API_KEY", "")
    if (
        settings.model_provider != "openai"
        or settings.model_name != "gpt-6-luna"
        or settings.llm_budget_usd <= 0
        or not key
    ):
        raise LiveModelBlocked(
            "Live mode requires MODEL_PROVIDER=openai, MODEL_NAME=gpt-6-luna, "
            "a positive LLM_BUDGET_USD, and OPENAI_API_KEY"
        )
    if settings.embedding_provider != "fake":
        raise LiveModelBlocked("Live retrieval embeddings are not part of this local build")
    return settings, key


def _merged_environ(environ: dict[str, str] | None) -> dict[str, str]:
    if environ is None:
        file_values = _parse_env_file(repo_root() / ".env")
        return {**file_values, **dict(os.environ)}
    return dict(environ)


def _build_settings(merged: dict[str, str]) -> Settings:
    def pick(name: str, default: str) -> str:
        value = merged.get(name)
        return default if value is None or value == "" else value

    try:
        budget = Decimal(pick("LLM_BUDGET_USD", "0"))
    except InvalidOperation as exc:
        raise SettingsError("LLM_BUDGET_USD must be a decimal") from exc

    try:
        settings = Settings(
            resolveai_env=pick("RESOLVEAI_ENV", "demo"),
            database_url=pick("DATABASE_URL", Settings().database_url),
            api_database_url=pick("API_DATABASE_URL", Settings().api_database_url),
            simulator_database_url=pick(
                "SIMULATOR_DATABASE_URL", Settings().simulator_database_url
            ),
            oracle_database_url=pick("ORACLE_DATABASE_URL", Settings().oracle_database_url),
            simulator_base_url=pick("SIMULATOR_BASE_URL", Settings().simulator_base_url),
            simulator_token=pick("SIMULATOR_TOKEN", Settings().simulator_token),
            simulator_allow_faults=pick("SIMULATOR_ALLOW_FAULTS", "0") in {"1", "true", "TRUE"},
            web_origin=pick("WEB_ORIGIN", Settings().web_origin),
            model_provider=pick("MODEL_PROVIDER", "fake"),
            model_name=merged.get("MODEL_NAME", ""),
            model_api_key=merged.get("MODEL_API_KEY", ""),
            embedding_provider=pick("EMBEDDING_PROVIDER", "fake"),
            embedding_model=merged.get("EMBEDDING_MODEL", ""),
            llm_budget_usd=budget,
            run_max_model_calls=int(pick("RUN_MAX_MODEL_CALLS", "12")),
            run_max_tool_calls=int(pick("RUN_MAX_TOOL_CALLS", "20")),
            run_max_replans=int(pick("RUN_MAX_REPLANS", "2")),
            run_max_read_retries=int(pick("RUN_MAX_READ_RETRIES", "2")),
        )
    except ValidationError as exc:
        raise SettingsError(str(exc)) from exc

    if settings.resolveai_env != "demo":
        raise SettingsError("RESOLVEAI_ENV must be demo for this local build")
    for label, url in (
        ("DATABASE_URL", settings.database_url),
        ("API_DATABASE_URL", settings.api_database_url),
        ("SIMULATOR_DATABASE_URL", settings.simulator_database_url),
        ("ORACLE_DATABASE_URL", settings.oracle_database_url),
    ):
        _local_database(url, label)
    _local_http(settings.simulator_base_url, "SIMULATOR_BASE_URL")
    _local_http(settings.web_origin, "WEB_ORIGIN")
    return settings


def ensure_fake_provider(settings: Settings) -> None:
    """Fail closed before any model call. A positive budget does not enable a provider."""
    if (
        settings.model_provider != "fake"
        or settings.embedding_provider != "fake"
        or settings.llm_budget_usd != 0
        or settings.model_api_key != ""
        or settings.model_name != ""
    ):
        raise LiveModelBlocked(
            "M1 allows only the fake provider with an empty credential and a zero budget"
        )
