"""Helpers for DB integration tests (importable, unlike conftest)."""
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
STUB_SQL = (ROOT / "docker" / "db" / "supabase_stub.sql").read_text()


def _admin_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; skipping DB integration tests")
    host = make_url(url).host
    assert host in ("localhost", "127.0.0.1", "::1", "db"), (
        f"Refusing to run destructive tests against non-local host {host!r}"
    )
    return url


def _alembic(url: str, target: str, downgrade: bool = False) -> None:
    os.environ["MIGRATION_DATABASE_URL"] = url
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    (command.downgrade if downgrade else command.upgrade)(cfg, target)


class TestDb:
    """A throwaway database with the Supabase stub applied."""

    def __init__(self, admin_url: str):
        self.admin_url = admin_url
        self.name = f"photos_test_{uuid.uuid4().hex[:10]}"
        self.url = make_url(admin_url).set(database=self.name).render_as_string(hide_password=False)

    def create(self):
        admin = create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{self.name}"'))
        admin.dispose()
        eng = create_engine(self.url)
        with eng.begin() as c:
            c.exec_driver_sql(STUB_SQL)
        eng.dispose()

    def drop(self):
        admin = create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))
        admin.dispose()

    def upgrade(self, target: str = "head"):
        _alembic(self.url, target)

    def downgrade(self, target: str):
        _alembic(self.url, target, downgrade=True)

    def engine(self):
        return create_engine(self.url)
