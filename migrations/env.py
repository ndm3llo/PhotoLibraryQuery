"""Alembic environment.

Database URL resolution (first hit wins):
  1. an engine/URL passed programmatically via config.attributes["connection"] (tests)
  2. MIGRATION_DATABASE_URL
  3. postgres_url (the app's existing variable)

Migrations are hand-written SQL, so there is no autogenerate target metadata.
"""
import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

load_dotenv()

config = context.config


def _url() -> str:
    url = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("postgres_url")
    if not url:
        raise RuntimeError("Set MIGRATION_DATABASE_URL (or postgres_url) to run migrations.")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
