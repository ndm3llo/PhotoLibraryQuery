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

## Sign-in setup (Supabase Auth)

Sign-in is by emailed magic link. The API verifies Supabase tokens using your project's public
signing keys, so no secret is needed. In `.env`:

```
SUPABASE_URL=https://<your-ref>.supabase.co
SUPABASE_ANON_KEY=<publishable / anon key>   # public by design; never the service_role key
APP_BASE_URL=http://localhost:8000
```

In the Supabase dashboard: Authentication > Providers > Email enabled; Authentication > URL
Configuration > Site URL `http://localhost:8000` and redirect `http://localhost:8000/**`.

Then run the API, open <http://localhost:8000/login>, and request a link. After the first sign-in,
link your pre-existing profile (library, photos) to your account:

```bash
python scripts/link_owner.py --email you@example.com           # dry run
python scripts/link_owner.py --email you@example.com --apply
```

To invite someone, an owner calls `POST /api/libraries/{id}/invites` (try it in `/docs` after
clicking Authorize with the token from `/login`) and sends them the returned link.

## Run the (prototype) API

```bash
uvicorn app.main:app --reload
```

## Layout

- `app/`: FastAPI app (api, db, models, services)
- `migrations/`: Alembic migrations (schema, RLS policies)
- `docker/`, `docker-compose.yml`: local Postgres
- `scripts/`: one-off utilities (`link_owner.py`; `bulk_upload.py` and `synch_storage.py` are stale prototypes)
- `tests/`: pytest suite
- `docs/`: architecture and migration plan
