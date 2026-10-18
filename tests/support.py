from starlette.testclient import TestClient

ORIGIN = "http://127.0.0.1:5173"


def case_by_key(manifest: dict, key: str) -> dict:
    return next(item for item in manifest["cases"] if item["key"] == key)


def login(client: TestClient, email: str, password: str) -> dict[str, str]:
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
