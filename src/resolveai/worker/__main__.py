"""Run refund recovery until interrupted.

The worker is the only process that admits a refund or calls the simulator.
"""

from __future__ import annotations

import os
import time

from resolveai.db.session import make_engine, make_session_factory
from resolveai.services.recovery import process_available
from resolveai.settings import load_settings
from resolveai.tools.client import SimulatorClient


def drain(settings: object, *, limit: int = 20) -> int:
    engine = make_engine(settings.worker_database_url)  # type: ignore[attr-defined]
    factory = make_session_factory(engine)
    tools = SimulatorClient(settings)  # type: ignore[arg-type]
    completed = 0
    try:
        for _ in range(limit):
            with factory() as session:
                if not process_available(session, tools):
                    break
            completed += 1
    finally:
        tools.close()
        engine.dispose()
    return completed


def _serve_health(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            return

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    health_port = os.environ.get("RESOLVEAI_WORKER_HEALTH_PORT")
    if health_port:
        import threading

        threading.Thread(
            target=_serve_health, args=(int(health_port),), daemon=True
        ).start()
    settings = load_settings()
    engine = make_engine(settings.worker_database_url)
    factory = make_session_factory(engine)
    tools = SimulatorClient(settings)
    try:
        while True:
            with factory() as session:
                worked = process_available(session, tools)
            if not worked:
                time.sleep(0.2)
    finally:
        tools.close()
        engine.dispose()


if __name__ == "__main__":
    main()
