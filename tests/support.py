from sqlalchemy import event, text
from starlette.testclient import TestClient

ORIGIN = "http://127.0.0.1:5173"
NORTHWIND_TENANT_ID = "11111111-1111-4111-8111-111111111111"
HARBOR_TENANT_ID = "11111111-1111-4111-8111-111111111112"


def bound_sessions(engine, tenant_id: str = NORTHWIND_TENANT_ID):
    from resolveai.db.session import make_session_factory

    factory = make_session_factory(engine)

    def open_session():
        session = factory()
        session.info["tenant_id"] = str(tenant_id)
        return session

    return open_session


def seeing_engine(url: str, tenant_id: str = NORTHWIND_TENANT_ID):
    from resolveai.db.session import make_engine

    engine = make_engine(url)

    @event.listens_for(engine, "begin")
    def _apply(connection) -> None:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    return engine


def case_by_key(manifest: dict, key: str) -> dict:
    return next(item for item in manifest["cases"] if item["key"] == key)


def login(client: TestClient, email: str, password: str) -> dict[str, str]:
    client.cookies.delete("resolveai_session")
    client.cookies.delete("resolveai_csrf")
    response = client.post(
        "/sessions",
        json={"email": email, "password": password},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    token = client.cookies.get("resolveai_csrf")
    assert token
    return {"Origin": ORIGIN, "X-CSRF-Token": token}


def support_headers(client: TestClient) -> dict[str, str]:
    return login(client, "support@demo.resolveai.test", "support-demo-pass")


def approver_headers(client: TestClient) -> dict[str, str]:
    return login(client, "approver@demo.resolveai.test", "approver-demo-pass")


def settle_recovery(settings: object) -> None:
    from resolveai.worker.__main__ import drain

    drain(settings)  # type: ignore[arg-type]


def execute_and_settle(
    client: TestClient,
    settings: object,
    proposal: dict,
    headers: dict[str, str],
):
    response = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=headers,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    if response.status_code != 200:
        return response
    settle_recovery(settings)
    ticket_id = response.json()["data"]["ticket"]["id"]
    return client.get(f"/tickets/{ticket_id}", headers=headers)
