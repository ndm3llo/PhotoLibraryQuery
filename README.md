# photoLibraryQuery

Family-focused photo album that is good at finding photos of specific people at specific times.

The production design and phased migration plan live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Read it before changing anything.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then fill in values; never commit .env
```

## Local database and migrations

The schema is owned by Alembic. Never use `create_all`.

```bash
docker compose up -d db          # Postgres + pgvector + PostGIS on localhost:5433
export DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5433/photos
export MIGRATION_DATABASE_URL=$DATABASE_URL
alembic upgrade head
```

An **existing** database that already has the prototype tables (e.g. the current Supabase project)
must be stamped first, and backed up before migrating:

```bash
alembic stamp 0001
alembic upgrade head
```

`docker/db/supabase_stub.sql` imitates the Supabase roles and `auth.uid()` for local use only.
Never run it against a real Supabase project.

## Checks

```bash
ruff check .
export TEST_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5433/postgres
pytest -q        # DB tests are skipped when TEST_DATABASE_URL is unset
```

CI runs the same commands on every push. Tests create and drop their own throwaway databases and
refuse to run against a non-local host.

## Run the (prototype) API

```bash
uvicorn app.main:app --reload
```

## Layout

- `app/`: FastAPI app (api, db, models, services)
- `migrations/`: Alembic migrations (schema, RLS policies)
- `docker/`, `docker-compose.yml`: local Postgres
- `scripts/`: one-off utilities (bulk upload, storage sync)
- `tests/`: pytest suite
- `docs/`: architecture and migration plan
