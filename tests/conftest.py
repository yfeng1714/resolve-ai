from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from starlette.testclient import TestClient

from resolveai.api.app import create_api_app
from resolveai.seed.demo import load_manifest, seed_demo
from resolveai.simulator.app import create_simulator_app

SIMULATOR_PORT = 8011


@pytest.fixture(scope="session")
def settings():
    from resolveai.testing import hold_test_database, prepare_pytest_settings

    with hold_test_database():
        prepared = prepare_pytest_settings()
        yield prepared.model_copy(update={"simulator_base_url": f"http://127.0.0.1:{SIMULATOR_PORT}"})


@pytest.fixture(scope="session")
def migrated(settings):
    yield settings


@pytest.fixture(scope="session")
def simulator_app(settings, migrated):
    del migrated
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
            break
    else:
        raise RuntimeError("simulator test server did not become healthy")
    yield app
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def api_app(settings, simulator_app):
    del simulator_app
    return create_api_app(settings)


@pytest.fixture
def seeded(settings, migrated):
    del migrated
    seed_demo(settings)
    yield


@pytest.fixture
def client(api_app, seeded) -> Iterator[TestClient]:
    del seeded
    with TestClient(api_app) as test_client:
        yield test_client


@pytest.fixture
def manifest() -> dict:
    return load_manifest()


@pytest.fixture
def restore_runtime(api_app, settings):
    provider = api_app.state.provider_factory
    tools = api_app.state.tool_client_factory
    snapshot = settings.model_dump()
    yield
    api_app.state.provider_factory = provider
    api_app.state.tool_client_factory = tools
    for name, value in snapshot.items():
        setattr(settings, name, value)
