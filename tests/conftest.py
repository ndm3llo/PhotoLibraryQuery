"""Shared fixtures.

Integration tests need a throwaway Postgres with pgvector + PostGIS:

    docker compose up -d db
    export TEST_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5433/postgres

Each test module gets its own freshly created database; the app's own database is never touched.
They are skipped when TEST_DATABASE_URL is not set.
"""
import pytest
from dbutil import TestDb, _admin_url


@pytest.fixture
def make_db():
    """Factory for fresh databases; all are dropped at teardown."""
    admin_url = _admin_url()
    created: list[TestDb] = []

    def _make() -> TestDb:
        db = TestDb(admin_url)
        db.create()
        created.append(db)
        return db

    yield _make
    for db in created:
        db.drop()


@pytest.fixture(scope="module")
def make_db_module():
    db = TestDb(_admin_url())
    db.create()
    yield db
    db.drop()
