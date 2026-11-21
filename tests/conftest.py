"""Shared fixtures.

Integration tests need a throwaway Postgres with pgvector + PostGIS:

    docker compose up -d db
    export TEST_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5433/postgres

The app's own database is never touched. They are skipped when TEST_DATABASE_URL is not set.
"""
import os

import pytest

# SAFETY: app.db.session reads DATABASE_URL first and falls back to postgres_url from .env,
# which points at the real Supabase project. Pin it to an unreachable address so that no test can
# ever connect to the real database by accident. The `app_db` fixture replaces it with the
# throwaway test database before anything imports app.db.
os.environ["DATABASE_URL"] = "postgresql+psycopg2://invalid:invalid@127.0.0.1:1/invalid"

from dbutil import TestDb, _admin_url  # noqa: E402


@pytest.fixture
def make_db():
    """Factory for fresh databases (migration tests); all are dropped at teardown."""
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


@pytest.fixture(scope="session")
def app_db():
    """One fully migrated database shared by every test that imports the app (`app.db.*`).

    Tests must import app modules inside their fixtures/tests (after this runs), never at module top level.
    """
    db = TestDb(_admin_url())
    db.create()
    db.upgrade("head")
    os.environ["DATABASE_URL"] = db.url
    yield db
    db.drop()


@pytest.fixture(scope="session")
def s3_endpoint():
    """An in-process S3-compatible server (moto), so upload tests need no external service.
    NOTE: moto does not verify signatures, so signing problems (e.g. R2 rejecting checksum headers)
    are only caught by the live check in scripts/check_storage.py."""
    import socket

    from moto.server import ThreadedMotoServer

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture(scope="session")
def storage(s3_endpoint):
    from app.services.storage import Storage, set_storage

    st = Storage.create(endpoint_url=s3_endpoint, access_key="test", secret_key="test", bucket="test-bucket",
                        region="us-east-1")  # R2 uses "auto"; moto needs a real region
    st.client.create_bucket(Bucket="test-bucket")
    set_storage(st)
    yield st
    set_storage(None)
