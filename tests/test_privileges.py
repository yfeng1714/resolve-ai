import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from resolveai.db.session import make_engine


def _rejects(engine, statement: str) -> None:
    with engine.connect() as connection:
        with pytest.raises(ProgrammingError):
            connection.execute(text(statement))
            connection.commit()


def test_runtime_roles_cannot_cross_schemas(settings, seeded) -> None:
    del seeded
    api = make_engine(settings.api_database_url)
    simulator = make_engine(settings.simulator_database_url)
    oracle = make_engine(settings.oracle_database_url)
    admin = make_engine(settings.database_url)
    try:
        with api.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM app.ticket")).scalar_one() == 6
        _rejects(api, "SELECT count(*) FROM simulator.refund")
        _rejects(simulator, "SELECT count(*) FROM app.ticket")
        with oracle.connect() as connection:
            refunds = connection.execute(text("SELECT count(*) FROM simulator.refund"))
            assert refunds.scalar_one() == 2
        _rejects(
            oracle,
            """
            INSERT INTO simulator.customer (id, display_name, email, created_at)
            VALUES ('12121212-1212-4121-8121-121212121212', 'Nope', 'nope@demo.test', now())
            """,
        )
        with admin.connect() as connection:
            version = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one()
        assert version == "0.8.7"
    finally:
        api.dispose()
        simulator.dispose()
        oracle.dispose()
        admin.dispose()
