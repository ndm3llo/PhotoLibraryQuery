# photoLibraryQuery

Family-focused photo album that is good at finding photos of specific people at specific times.

The production design and phased migration plan live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Read it before changing anything.

**Picking the project up in a new chat:** read [handoff.md](handoff.md) first (current state, decisions, next steps);
the rules for keeping it up to date are in [instruction_handoff.md](docs/instruction_handoff.md). Screens and wording:
[docs/UI_SPEC.md](docs/UI_SPEC.md).

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
pytest -q        # DB tests are skipped when TEST_DATABASE_URL is unset; uploads use an in-process S3 server (moto)
```

CI runs the same commands on every push. Tests create and drop their own throwaway databases and
refuse to run against a non-local host.

## Sign-in setup (Supabase Auth)

The app (`/app`) signs people in with email and password; the older `/login` page uses an emailed link. The API verifies Supabase tokens using your project's public
signing keys, so no secret is needed. In `.env`:

```
SUPABASE_URL=https://<your-ref>.supabase.co
SUPABASE_ANON_KEY=<publishable / anon key>   # public by design; never the service_role key
APP_BASE_URL=http://localhost:8000
```

In the Supabase dashboard: Authentication > Providers > Email enabled; Authentication > URL
Configuration > Site URL `http://localhost:8000` and redirect `http://localhost:8000/**` (the app's confirmation
and password-reset emails return to `/app`). Invite links start with `APP_BASE_URL`.

Then run the API, open <http://localhost:8000/login>, and request a link. After the first sign-in,
link your pre-existing profile (library, photos) to your account:

```bash
python scripts/link_owner.py --email you@example.com           # dry run
python scripts/link_owner.py --email you@example.com --apply
```

To invite someone, an owner opens the album in the app and taps **Invite people** (a link, with a code as backup).
The older email-bound invite, `POST /api/libraries/{id}/invites`, still works.

## Run it

```bash
uvicorn app.main:app --reload     # the site: open http://localhost:8000/app (uploads still use /upload)
python -m app.worker              # the background worker: REQUIRED for uploads to finish processing
```

Both read `.env`. Uploads go straight from the browser to the (private) R2 bucket with short-lived
signed links; the worker then validates each photo, fixes its orientation, finds its real capture date,
makes web copies, detects duplicates, and adds the search and face vectors.

Import a whole folder through the same API:

```bash
export ALBUM_TOKEN=<copy from /login>
python scripts/bulk_import.py ~/Pictures/family --library <library-id>
```

Before any live migration, snapshot the database and rehearse on a restored local copy:

```bash
python scripts/backup_db.py --label pre-0009            # writes backups/ (gitignored, owner-only)
# local DB at the current version, then:
python scripts/backup_db.py --restore-to $DATABASE_URL backups/<file>.json && alembic upgrade head
```

**Test data:** the live "Face test" album has fake "Added by" accounts made by `scripts/fake_uploaders.py`
(remove them with `--undo` before real use).

Check the real bucket (signed uploads, CORS, public access) any time storage settings change:

```bash
python scripts/check_storage.py
```

R2 setup: apply `infra/r2-cors.json` as the bucket's CORS policy (add your real domain's origin when you
have one) and turn off public access on the bucket.

## Layout

- `app/`: FastAPI app (api, db, models, services)
- `migrations/`: Alembic migrations (schema, RLS policies)
- `docker/`, `docker-compose.yml`: local Postgres
- `scripts/`: `link_owner.py`, `bulk_import.py`, `check_storage.py`, `backup_db.py`, `eval_faces.py`
- `infra/`: Cloudflare R2 CORS policy
- `tests/`: pytest suite
- `docs/`: architecture and migration plan
