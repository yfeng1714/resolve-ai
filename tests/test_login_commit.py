"""Login must be durable before the success response is visible."""

import socket
import threading

import httpx
import uvicorn
from sqlalchemy.orm import Session, sessionmaker

from resolveai.api.app import create_api_app
from resolveai.db.models import UserSession


def _serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            return server, thread, port, sock
        threading.Event().wait(0.02)
    raise RuntimeError("login probe server did not start")


def _stop(server, thread, app, sock) -> None:
    server.should_exit = True
    thread.join(5)
    app.state.engine.dispose()
    sock.close()


def test_login_response_waits_for_the_session_commit(settings, seeded) -> None:
    del seeded
    reached, release = threading.Event(), threading.Event()

    class GatedSession(Session):
        def commit(self):
            if any(isinstance(row, UserSession) for row in self.new):
                reached.set()
                if not release.wait(5):
                    raise RuntimeError("commit gate timed out")
            return super().commit()

    app = create_api_app(settings)
    app.state.session_factory = sessionmaker(
        bind=app.state.engine, class_=GatedSession, expire_on_commit=False, autoflush=False
    )
    server, thread, port, sock = _serve(app)
    result: dict[str, httpx.Response] = {}

    def sign_in(client: httpx.Client) -> None:
        result["login"] = client.post(
            "/sessions",
            headers={"Origin": settings.web_origin},
            json={
                "email": "support@demo.resolveai.test",
                "password": "support-demo-pass",
            },
        )

    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            worker = threading.Thread(target=sign_in, args=(client,))
            worker.start()
            assert reached.wait(2)
            assert worker.is_alive()
            release.set()
            worker.join(5)
            login = result["login"]
            assert login.status_code == 200, login.text
            queue = client.get("/tickets")
            assert queue.status_code == 200, queue.text
    finally:
        release.set()
        _stop(server, thread, app, sock)


def test_failed_login_commit_does_not_issue_a_usable_session(settings, seeded) -> None:
    del seeded

    class FailingSession(Session):
        def commit(self):
            if any(isinstance(row, UserSession) for row in self.new):
                raise RuntimeError("commit refused")
            return super().commit()

    app = create_api_app(settings)
    app.state.session_factory = sessionmaker(
        bind=app.state.engine, class_=FailingSession, expire_on_commit=False, autoflush=False
    )
    server, thread, port, sock = _serve(app)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            login = client.post(
                "/sessions",
                headers={"Origin": settings.web_origin},
                json={
                    "email": "support@demo.resolveai.test",
                    "password": "support-demo-pass",
                },
            )
            assert login.status_code != 200
            assert "resolveai_session" not in login.cookies
            queue = client.get("/tickets")
            assert queue.status_code == 401
    finally:
        _stop(server, thread, app, sock)
