"""Disposable database used by pytest and Playwright.

Both suites share ``resolveai_pytest`` on the local Postgres server. They must
not run at the same time, and neither one reseeds ``resolveai_demo``.
"""

from __future__ import annotations

import atexit
import fcntl
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

from resolveai.seed.demo import seed_demo
from resolveai.settings import Settings, load_settings, repo_root

PYTEST_DATABASE = "resolveai_pytest"
E2E_API_PORT = 18080
E2E_SIMULATOR_PORT = 18081
E2E_WEB_PORT = 15173
E2E_WEB_ORIGIN = f"http://127.0.0.1:{E2E_WEB_PORT}"
READY_PATH = ".pytest-db.ready"


def swap_database(url: str, database: str = PYTEST_DATABASE) -> str:
    if database != PYTEST_DATABASE:
        raise RuntimeError("Only the pytest database can be prepared by this helper")
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def pytest_database_settings(base: Settings | None = None) -> Settings:
    base = base or load_settings()
    return base.model_copy(
        update={
            "database_url": swap_database(base.database_url),
            "api_database_url": swap_database(base.api_database_url),
            "simulator_database_url": swap_database(base.simulator_database_url),
            "oracle_database_url": swap_database(base.oracle_database_url),
        }
    )


def e2e_settings(base: Settings | None = None) -> Settings:
    return pytest_database_settings(base).model_copy(
        update={
            "simulator_base_url": f"http://127.0.0.1:{E2E_SIMULATOR_PORT}",
            "web_origin": E2E_WEB_ORIGIN,
        }
    )


def shell_exports(settings: Settings) -> str:
    values = {
        "DATABASE_URL": settings.database_url,
        "API_DATABASE_URL": settings.api_database_url,
        "SIMULATOR_DATABASE_URL": settings.simulator_database_url,
        "ORACLE_DATABASE_URL": settings.oracle_database_url,
        "SIMULATOR_BASE_URL": settings.simulator_base_url,
        "SIMULATOR_TOKEN": settings.simulator_token,
        "WEB_ORIGIN": settings.web_origin,
        "RESOLVEAI_API_PROXY": f"http://127.0.0.1:{E2E_API_PORT}",
    }
    return "".join(f"export {key}='{value}'\n" for key, value in values.items())


@contextmanager
def hold_test_database() -> Iterator[None]:
    path = repo_root() / ".pytest-db.lock"
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(
            "resolveai_pytest is already in use. Wait for pytest or Playwright to finish."
        ) from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def ensure_pytest_database(admin_url: str) -> None:
    maintenance = make_url(admin_url).set(database="postgres")
    engine = create_engine(
        maintenance.render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": PYTEST_DATABASE},
            ).scalar()
            if exists:
                return
            connection.execute(text(f'CREATE DATABASE "{PYTEST_DATABASE}"'))
    finally:
        engine.dispose()


def migrate(settings: Settings) -> None:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": settings.database_url,
            "API_DATABASE_URL": settings.api_database_url,
            "SIMULATOR_DATABASE_URL": settings.simulator_database_url,
            "ORACLE_DATABASE_URL": settings.oracle_database_url,
            "PYTHONPATH": str(repo_root() / "src"),
        }
    )
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root(),
        env=env,
    )


def prepare_pytest_settings() -> Settings:
    settings = pytest_database_settings()
    ensure_pytest_database(load_settings().database_url)
    migrate(settings)
    return settings


def hold_for_browser() -> None:
    ready = repo_root() / READY_PATH
    ready.unlink(missing_ok=True)
    with hold_test_database():
        settings = e2e_settings()
        ensure_pytest_database(load_settings().database_url)
        migrate(settings)
        seed_demo(settings)
        ready.write_text("ok\n", encoding="utf-8")

        def _stop(signum: int, _frame: object) -> None:
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        try:
            signal.pause()
        finally:
            ready.unlink(missing_ok=True)


def clear_ready_marker() -> None:
    (repo_root() / READY_PATH).unlink(missing_ok=True)


def _stop_server(signum: int, process: subprocess.Popen[bytes] | None) -> None:
    clear_ready_marker()
    if process is not None and process.poll() is None:
        process.terminate()
    raise SystemExit(128 + signum)


def serve_simulator() -> None:
    """Prepare the pytest database, hold its lock, and run the simulator.

    Playwright starts web servers before global setup and treats a database
    health failure as a startup timeout. This command creates the database
    before the server listens.
    """
    ready = repo_root() / READY_PATH
    clear_ready_marker()
    process: subprocess.Popen[bytes] | None = None

    def _stop(signum: int, _frame: object) -> None:
        _stop_server(signum, process)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    atexit.register(clear_ready_marker)
    with hold_test_database():
        try:
            settings = e2e_settings()
            ensure_pytest_database(load_settings().database_url)
            migrate(settings)
            seed_demo(settings)
            ready.write_text("ok\n", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "DATABASE_URL": settings.database_url,
                    "API_DATABASE_URL": settings.api_database_url,
                    "SIMULATOR_DATABASE_URL": settings.simulator_database_url,
                    "ORACLE_DATABASE_URL": settings.oracle_database_url,
                    "SIMULATOR_BASE_URL": settings.simulator_base_url,
                    "SIMULATOR_TOKEN": settings.simulator_token,
                    "WEB_ORIGIN": settings.web_origin,
                    "PYTHONPATH": str(repo_root() / "src"),
                    "UV_CACHE_DIR": str(repo_root() / ".uv-cache"),
                }
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "resolveai.simulator.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(E2E_SIMULATOR_PORT),
                    "--app-dir",
                    "src",
                ],
                cwd=repo_root(),
                env=env,
            )
            code = process.wait()
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            clear_ready_marker()
        raise SystemExit(code)


def wait_ready() -> None:
    """Write the readiness marker and wait until SIGTERM removes it."""
    ready = repo_root() / READY_PATH
    clear_ready_marker()
    ready.write_text("ok\n", encoding="utf-8")

    def _stop(signum: int, _frame: object) -> None:
        _stop_server(signum, None)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    atexit.register(clear_ready_marker)
    signal.pause()


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else ""
    if command == "prepare":
        with hold_test_database():
            prepare_pytest_settings()
        return
    if command == "env":
        sys.stdout.write(shell_exports(e2e_settings()))
        return
    if command == "hold":
        hold_for_browser()
        return
    if command == "serve-simulator":
        serve_simulator()
        return
    if command == "wait-ready":
        wait_ready()
        return
    raise SystemExit(
        "usage: python -m resolveai.testing [prepare|env|hold|serve-simulator|wait-ready]"
    )


if __name__ == "__main__":
    main(sys.argv)
