# Family Photo Album: Target Architecture & Migration Plan

Status: proposed · Written 2026-11-19 · Audience: engineers and coding agents working in this repo

This document is the single source of truth for moving the prototype in `app/` to a production design. Work is broken into phases at the end (section 10) with checkboxes and acceptance criteria. If you are an agent picking up a task, read sections 1 and 11 first, then the phase you are assigned.

---

## 1. Goals, scope, non-goals

**Goal:** a private, family-only photo album where anyone can ask for "Mia, summer 2019" and get the right photos, with members signing in by email or passkey and faces used only to find people in photos.

**In scope**

- Person-centric data model: named people who do not need accounts (children, grandparents, deceased relatives).
- One search that combines person(s), time range, and free text.
- Sign-in with email magic link or passkey (managed by Supabase Auth). No passwords.
- Reliable bulk import of large archives (20k+ photos, mostly iPhone HEIC).
- Tenant isolation enforced by the database (Row Level Security), not by endpoint code.

**Non-goals for v1**

- Public sharing, social features, video, pet recognition.
- Face login. Decided against: faces are used only to find people in photos, never to authenticate (section 5.4).
- Replacing Python, FastAPI, Postgres, or R2. The stack is fine; the data model, pipeline, and security are what change.

**"Industry standard" means**

| Area | Standard adopted |
| --- | --- |
| Identity | Managed auth (Supabase Auth), passkeys, short-lived JWTs |
| Authorization | Postgres RLS keyed on library membership |
| Uploads | Presigned direct-to-storage; the API never streams file bytes |
| Background work | Durable job queue, retries, per-photo status |
| Media delivery | Private bucket, short-lived signed URLs (no public bucket URL) |
| Biometrics | Explicit consent, encryption, deletion on request, retention limit |
| Delivery | Alembic migrations, CI, tests, structured logs, backups |

---

## 2. Target architecture

```
 Client (PWA / mobile)
    │  1. JWT (Supabase Auth)            2. presigned PUT
    ▼                                       ▼
 ┌──────────────┐   enqueue job    ┌──────────────┐        ┌───────────────┐
 │  API         │ ───────────────► │  Postgres    │        │ Cloudflare R2 │
 │  FastAPI     │  (Procrastinate) │  + pgvector  │        │  (private)    │
 │  (no ML)     │ ◄─────────────── │  + PostGIS   │        │ originals/    │
 └──────┬───────┘   reads (RLS)    │  + RLS       │        │ display/      │
        │                          └──────▲───────┘        │ thumbs/       │
        │ signed GET URLs                 │ writes         └──────▲────────┘
        └──────────────────────────────── │ ───────────────────────┤
                                   ┌──────┴───────┐  download/upload
                                   │  Worker      │ ───────────────┘
                                   │  InsightFace │
                                   │  CLIP, HEIC  │
                                   └──────────────┘
```

| Component | Choice | Why |
| --- | --- | --- |
| API | FastAPI + SQLModel/SQLAlchemy | Already in use; keep ML out of it so it starts fast and scales cheaply |
| Worker | Same repo, separate entrypoint and container | Heavy models load once per worker, not per web process |
| Queue | Procrastinate (Postgres-backed) | One fewer service than Redis; retries, scheduling, and visibility are built in. Swap to Arq/Redis only if throughput demands |
| Database | Supabase Postgres: pgvector, PostGIS, RLS | One store for vectors, time, location, and permissions makes "person + time + album" a single query |
| Object store | R2, private bucket | Cheap, no egress fees |
| Auth | Supabase Auth | Passkeys, magic links, JWT; RLS reads `auth.uid()` |
| Face model | InsightFace `buffalo_l` (RetinaFace-class detector + ArcFace 512-d) | More accurate than DeepFace/Facenet512, especially on children; gives bbox, landmarks, quality in one pass |
| Text/image model | CLIP `clip-ViT-B-32` (keep) | Works; 512-d |
| Migrations | Alembic | Replaces `SQLModel.metadata.create_all` |

---

## 3. Data model

Isolation unit is the **library** (one per family). Albums are optional groupings inside a library.

```sql
-- Identity & tenancy
profile(id uuid pk references auth.users, handle text unique not null,
        display_name text not null, created_at timestamptz default now())

library(id uuid pk, name text, created_by uuid references profile, created_at timestamptz)

library_member(library_id uuid references library, profile_id uuid references profile,
               role text check (role in ('owner','editor','viewer')),
               primary key (library_id, profile_id))

-- People are NOT accounts. A person may optionally be linked to a profile.
person(id uuid pk, library_id uuid not null references library,
       display_name text not null, birth_date date, death_date date,
       profile_id uuid references profile,            -- nullable
       cover_face_id uuid,                            -- fk added after face exists
       recognition_opt_out boolean not null default false,  -- never match or suggest this person
       created_by uuid references profile, created_at timestamptz,
       unique (library_id, profile_id))

-- Nicknames: "Grandma", "Nana", "Abuela". The display_name is always searchable too.
-- Not unique per library: two people may both be "Grandma" (search then asks which one).
person_alias(id uuid pk, library_id uuid not null references library,
             person_id uuid not null references person on delete cascade,
             alias text not null, created_by uuid references profile)
-- index on (library_id, lower(unaccent(alias)))

-- Reference faces that identify a person without anyone labeling photos: a self-enrolled
-- selfie, a guardian-enrolled selfie of a child, or an uploaded reference photo.
-- Stored as plain vectors because they are searched against.
person_reference(id uuid pk, library_id uuid not null references library,
                 person_id uuid not null references person on delete cascade,
                 embedding vector(512) not null, model_version text not null, quality real,
                 source text not null check (source in ('self_selfie','guardian_selfie','uploaded_photo')),
                 consent_id uuid not null references biometric_consent,
                 created_by uuid references profile, created_at timestamptz default now())

-- Photos
photo(id uuid pk, library_id uuid not null references library,
      uploader_id uuid not null references profile,
      status text not null check (status in ('pending','processing','ready','failed')),
      error text,
      original_key text, display_key text, thumb_key text,   -- R2 keys, never full URLs
      width int, height int, content_sha256 text, phash bigint,
      taken_at timestamptz, taken_at_source text, taken_at_confidence real, taken_tz text,
      location geography(point, 4326),
      clip_embedding vector(512),
      created_at timestamptz default now(),
      unique (library_id, content_sha256))

album(id uuid pk, library_id uuid not null references library, name text, created_by uuid, created_at timestamptz)
album_photo(album_id uuid references album, photo_id uuid references photo, primary key (album_id, photo_id))

-- Faces found in photos
face_cluster(id uuid pk, library_id uuid not null, person_id uuid references person)

face(id uuid pk, library_id uuid not null references library,
     photo_id uuid not null references photo on delete cascade,
     bbox int[4] not null, det_score real, quality real,
     embedding vector(512) not null, model_version text not null,
     cluster_id uuid references face_cluster,
     person_id uuid references person,
     person_source text check (person_source in ('manual','confirmed','auto')),
     person_confidence real)

-- Consent to be recognized in photos (selfie enrollment, guardian enrollment, labeling).
biometric_consent(id uuid pk, profile_id uuid references profile, notice_version text,
                  granted_at timestamptz not null, revoked_at timestamptz)

audit_log(id bigserial pk, at timestamptz default now(), actor uuid, library_id uuid,
          action text, target text, ip inet, meta jsonb)
```

**Notes**

- `taken_at_source` is one of `exif_original | exif_digitized | quicktime | filename | file_mtime | neighbor_inferred | manual`. A time-search product is only as good as its timestamps, so provenance and confidence are stored.
- `face.person_source` separates human-confirmed labels from model guesses. Only `manual` and `confirmed` faces act as reference exemplars for a person. `auto` is never fed back as ground truth, so errors can't snowball.
- Every table with `library_id` is RLS-protected (below). `library_id` is denormalized onto `face` on purpose so policies and filters stay one hop.

**Row Level Security (pattern for every tenant table)**

```sql
create function is_member(lib uuid) returns boolean language sql stable security definer as $$
  select exists (select 1 from library_member where library_id = lib and profile_id = auth.uid())
$$;

alter table photo enable row level security;
create policy photo_read   on photo for select using (is_member(library_id));
create policy photo_write  on photo for insert with check (is_member(library_id) and uploader_id = auth.uid());
create policy photo_update on photo for update using (
  exists (select 1 from library_member m where m.library_id = photo.library_id
          and m.profile_id = auth.uid() and m.role in ('owner','editor')));
```

The API connects as the user (forwarding the JWT, `set local role authenticated` + `request.jwt.claims`) for all request-scoped reads and writes. The worker uses the service role and always sets `library_id` explicitly. `biometric_consent` and `audit_log` have **no** user-facing select policy; only the service role touches them.

**Indexes**

- `photo (library_id, taken_at desc)`, `photo (library_id, status)`, `photo (library_id, content_sha256)` (unique, above).
- `face (library_id, person_id)`, `face (photo_id)`, `face (cluster_id)`.
- Vector search at family scale (well under ~500k faces per library): an exact scan scoped by `library_id` is milliseconds and has perfect recall, so start without a vector index. If needed later, add HNSW (`vector_cosine_ops`) and enable pgvector's `hnsw.iterative_scan`, because HNSW plus a restrictive `WHERE` otherwise returns too few rows.
- `photo.clip_embedding`: same approach, HNSW once a library exceeds ~100k photos.

---

## 4. Ingestion pipeline

Client never sends file bytes to the API.

1. `POST /uploads` with `[{filename, size, content_type, last_modified}]`. The API inserts `photo` rows (`status=pending`) and returns `{photo_id, presigned_put_url}` for each. Key layout: `{library_id}/originals/{photo_id}`.
2. Client PUTs the file directly to R2.
3. `POST /uploads/complete {photo_ids}` enqueues `process_photo(photo_id)`.
4. Worker, idempotent and safe to retry:
   1. Download original; compute `sha256`. If another ready photo in the same library has it, mark this one `duplicate` of that and stop.
   2. Decode (Pillow + `pillow-heif`), **apply EXIF orientation** (`ImageOps.exif_transpose`) before detection, or sideways faces are missed. Set `Image.MAX_IMAGE_PIXELS` to guard decompression bombs.
   3. Produce `display` (WebP, long edge 2048) and `thumb` (WebP, long edge 400). Keep the original untouched.
   4. Resolve `taken_at` with the ladder below. Extract GPS.
   5. Detect faces; for each keep bbox, detection score, quality, embedding. Discard faces below a minimum size (about 40 px on the short side of the bbox) or detection score.
   6. CLIP image embedding; 64-bit pHash.
   7. One transaction: write faces, update the photo, set `status=ready`.
5. Failures retry with exponential backoff (3 attempts), then `status=failed` with `error` set. The UI shows failed photos with a retry button.

**Near-duplicate handling:** exact duplicates are caught by `sha256`. pHash within Hamming distance ≤ 4 is *flagged* (a `near_duplicate_of` suggestion), never silently dropped, and always scoped to one library. The prototype compares pHash globally and by exact equality; that can reject another family's photo.

**Date resolution ladder (first hit wins; record the source)**

1. `DateTimeOriginal` (tag 36867, in the Exif sub-IFD `0x8769`) + `OffsetTimeOriginal` (36880).
2. `DateTimeDigitized` (36868).
3. QuickTime / HEIC creation date.
4. Filename patterns: `IMG_YYYYMMDD…`, `YYYY-MM-DD…`, `IMG-YYYYMMDD-WA…`.
5. Client `last_modified` at upload (low confidence).
6. Neighbor inference: same uploader and upload batch, nearest in sequence (low confidence).
7. Manual (UI: edit one photo, or "shift dates for this batch").

Do **not** use tag 306 (`DateTime`) as a primary source. It is the file-modified time and is the value the prototype currently ends up with. Remove the debug `print` dumps from `extract_metadata.py`.

**Media delivery:** the bucket is private. `GET /photos/{id}/url?size=thumb|display|original` checks RLS (select on `photo`) and returns a signed R2 URL valid ~10 minutes. The prototype's use of `R2_PUBLIC_URL` means anyone with a link can view a photo; that must go.

---

## 5. Authentication

Sign-in is handled entirely by Supabase Auth. There is no face login.

### 5.1 Flow

1. An owner invites someone by email to a library (`POST /libraries/{id}/invites`). The invite is a single-use, expiring token tied to the email address and a role.
2. The invitee opens the link and signs in with an **email magic link** or a **passkey**. Supabase issues a short-lived JWT plus a refresh token.
3. The API verifies the JWT on every request (signature, expiry, audience) and passes the user id to the database, where RLS enforces access (section 3).
4. On first sign-in a `profile` row is created with a unique `handle` and `display_name`; accepting the invite adds a `library_member` row with the invited role.

### 5.2 Rules

- Roles are `owner`, `editor`, `viewer`. Only owners invite, change roles, or remove members. The last owner of a library cannot be removed or demoted.
- Invites expire after 7 days and can be revoked. Accepting requires the signed-in email to match the invited email.
- Sessions are short-lived (JWT about 1 hour) with refresh-token rotation. A member can sign out other devices.
- Rate-limit sign-in and invite endpoints; log sign-ins, invites, and role changes to `audit_log`.
- Never accept `user_id` or `library_id` from the client without an RLS membership check.

### 5.3 Library deletion

Not exposed yet (see Phase 2 limitations): deleting a library must also delete its objects in R2.

### 5.4 Decision: no face login

Considered and rejected. A face photo is not a secret, and in this product the album is a library of each member's face, so anyone who sees a few photos has the spoofing material. Doing it safely would need liveness detection, trusted-device binding, lockout, and a second consent, for a convenience feature. Faces are used for **finding people in photos** (section 6), where a wrong match costs a mislabeled photo, not account access. If this is revisited, treat it as a separate project with its own threat model.

---

## 6. Search and people

### 6.1 Naming people

There are two ways a person gets connected to their face. Self-enrollment (A) means most active members never have to be named by anyone else; labeling (B) covers everyone who doesn't enroll.

**A. Claim yourself (optional selfie)**

1. A signed-in member opts in and accepts the *recognition* notice They take 3 to 5 selfies (varied angle and lighting; single face, minimum size, not blurred). Each becomes a `person_reference` row with `source='self_selfie'`.
2. This creates their `person` (or claims an existing one) with `profile_id` set. Claiming a person that already has labeled faces needs approval from an owner or editor, so nobody can claim someone else's identity.
3. A background job matches all unlabeled faces in the library against the new references. Matches above `T_auto` are stored as `auto`; matches above `T_suggest` become suggestions delivered **to that person** ("Is this you?"), not to the rest of the family. Confirming sets `person_source='confirmed'`.
4. Selfies are recent, so older photos (large age gap) match weakly and mostly surface as suggestions. The app nudges: "Confirm a few older photos to improve results."
5. **Children and others without accounts:** an owner or editor can enroll references for a child's `person`, which records guardian consent. This is the only case where one member's selfie upload stands in for another person.
6. **Opting out:** a person can set `recognition_opt_out`. Their references are deleted, their `person_id` labels cleared, and they are excluded from future matching and suggestions even if someone else tries to label them.

**B. Label clusters (everyone else)**

1. **Cluster.** After each import, assign each new face to the nearest existing cluster in the library if cosine distance < `T_cluster`, otherwise start a new cluster. Periodically re-cluster the library (HDBSCAN or agglomerative) as a background job.
2. **Label.** The UI shows clusters largest-first ("Who is this?"). A member names a cluster once (or merges it into an existing person); all its faces get `person_id`, `person_source='manual'`.
3. **Suggest.** For unlabeled faces, if the nearest exemplar of any person is closer than `T_suggest`, store a suggestion; the UI asks "Is this Mia?". Confirming sets `person_source='confirmed'`. Auto-assign without confirmation only above a stricter `T_auto`, stored as `auto` and visibly marked.
4. **Exemplars** for a person = faces with `manual`/`confirmed` source plus their `person_reference` rows. These are what matching uses. A reference counts as dated at its `created_at`, so age-aware matching (6.2) treats a selfie as belonging to that era.
5. **Aliases.** Anyone with editor rights can add nicknames to a person. A search by name matches `display_name` and every alias.

### 6.2 Age-aware matching

One reference face fails across childhood. Matching a candidate face from a photo taken at time *t*:

1. Prefer exemplars taken within ±3 years of *t* (if the person has any); take the minimum distance among those.
2. If none exist in that window, fall back to all exemplars with the stricter threshold `T_cross_era`.
3. If the person has `birth_date`, expose "age at photo" in the UI and allow searching by age ("Mia at 3").

Thresholds `T_*` are parameters in config, tuned by the evaluation harness (6.4).

### 6.3 Query API

`GET /search`

| Param | Meaning |
| --- | --- |
| `person` (repeatable) | person ids to find |
| `name` (repeatable) | names or nicknames, resolved case- and accent-insensitively against `display_name` and `person_alias`. If a name matches more than one person (two "Grandma"s), the response lists the candidates for the UI to choose instead of guessing |
| `mode` | `all` (everyone in the same photo) or `any` |
| `from`, `to` | date range, applied to `taken_at` |
| `q` | optional free text, ranked with CLIP text embedding |
| `include_inferred_dates` | default true; false limits to EXIF-sourced dates |
| `cursor`, `limit` | keyset pagination on `(taken_at, id)` |

Execution order: library/RLS filter, then date filter (uses the `(library_id, taken_at)` index), then face match against the requested person(s), then optional CLIP re-rank, then thumbnails with signed URLs. Response includes per photo: `taken_at`, `taken_at_source`, matched faces with bbox (for highlight), and `match_kind` (`manual | confirmed | auto | suggested`). The prototype returns the nearest *photo* with no bbox and uses a 0.6 cosine-distance cutoff, which is far too loose for these embeddings.

### 6.4 Evaluation harness

Build before tuning any threshold. Use the labeled sets already in the repo (`app_imgs/ronald`, `app_imgs/natalie`) plus any family photos the owner labels. `scripts/eval_faces.py` reports, per threshold: precision and recall for person search, false accept rate for login, and results bucketed by age gap between query and match. Thresholds in config must cite the eval run that chose them. Commit the numbers, not the photos.

---

## 7. Security, privacy, compliance

*This is engineering guidance, not legal advice. Have counsel review the notice and retention text before real users are invited.*

- **Biometric data:** face embeddings are biometric identifiers. In some jurisdictions (e.g. Illinois BIPA, GDPR Art. 9, Texas CUBI) collection needs written notice, informed consent, and a published retention/destruction schedule. This applies to the embeddings of every face in photos, including children and people who never signed up, and to selfie references.
- **Consent model:** library owners accept a notice when enabling face features. Each member consents separately to *recognition* (being matched in photos, including selfie enrollment); a person who does not consent can set `recognition_opt_out`. Revoking recognition deletes that person's `person_reference` rows and clears their labels (see `recognition_opt_out`). Persons without accounts are labeled by a member; provide "remove this person's face data" which deletes their embeddings and unlinks `person_id`.
- **Retention:** delete biometrics on account/person deletion and on consent revocation. Default cap of 3 years after last activity for selfie references, owner-configurable.
- **Secrets:** move credentials out of `.env` into a secret manager for deployed environments; `.env` is gitignored and never committed. Rotate R2, Hugging Face, and database credentials before the repo is ever pushed anywhere, since `.env` has lived unprotected in a non-git directory.
- **Transport/storage:** TLS everywhere; provider encryption at rest. Face embeddings are not encrypted at the column level because they must be searched; access is limited by RLS and the service role.
- **Input hardening:** upload size cap, content-type sniffing (not trusting the extension), pixel cap, per-user upload rate limits, strict CORS to the web origin.
- **Authorization:** all request-scoped DB access runs under RLS. No endpoint accepts a `user_id` from the body or form (the prototype does).
- **Logging:** structured logs with request id, user id, library id; no face images, embeddings, or signed URLs in logs.
- **Audit:** `audit_log` for invites, role changes, person deletion, biometric consent changes, bulk exports.
- **Export/delete:** a member can export their uploaded originals and delete their account; an owner can delete a library (cascades to R2 objects via a cleanup job).

---

## 8. Operations

- **Local dev:** `docker compose up` brings up Postgres (pgvector + PostGIS image), a local S3-compatible store (MinIO) in place of R2, the API, and the worker. `make dev` wraps it.
- **Migrations:** Alembic, one baseline plus incremental. RLS policies live in migrations.
- **Tests:** `pytest`. Unit tests for date ladder, filename parsing, clustering assignment, age-window matching; integration tests against a throwaway Postgres with RLS **specifically asserting that user A in library 1 cannot read library 2** (the most important test in the repo); invite and role-rule tests (last owner cannot be removed, invite email must match).
- **CI (GitHub Actions):** ruff, mypy, pytest with a Postgres service, migration up/down check.
- **Observability:** structured JSON logs, Sentry for errors, metrics for queue depth, per-photo processing seconds, failure rate, search latency (p50/p95), face-login success/failure by reason (internal only).
- **Backups:** Supabase point-in-time recovery; R2 object versioning plus a periodic copy of originals to a second bucket or provider. Originals are the irreplaceable asset.
- **Capacity (approximate, measure to confirm):** CPU face detection plus embedding is on the order of 0.3 to 1 s per photo, so a 20,000-photo archive is several hours on one CPU worker. Parallelize with more worker replicas; a GPU is optional.
- **Environments:** `local`, `staging`, `prod`, each with its own Supabase project and R2 bucket.

---

## 9. Target repository layout

```
app/
  main.py                  # app factory, router wiring only
  config.py                # pydantic-settings; thresholds with eval-run citations
  auth/                    # JWT dependency, RLS session, invites and roles
  api/                     # uploads.py, photos.py, people.py, search.py, albums.py, admin.py
  db/                      # session.py, models.py
  services/                # storage.py, metadata.py, faces.py, clip.py, clustering.py, matching.py
  worker/                  # tasks.py (process_photo, recluster), entrypoint
migrations/                # alembic
scripts/                   # eval_faces.py, bulk_import.py
tests/                     # unit/, integration/
docs/                      # this file
```

Mapping from current files:

| Current | Becomes |
| --- | --- |
| `app/models/model.py` | `app/db/models.py` + Alembic migrations |
| `app/api/upload.py`, `upload_folder.py` | `app/api/uploads.py` (presigned) + `worker/tasks.py` |
| `app/api/search.py`, `search_face.py`, `search_faces_in_db.py` | `app/api/search.py` (single endpoint) |
| `app/api/delete_all.py` | removed from prod; dev-only script |
| `app/services/img_face_recognition.py` | `app/services/faces.py` (InsightFace) |
| `app/services/extract_metadata.py` | `app/services/metadata.py` (date ladder, no prints) |
| `app/services/ai.py` | `app/services/clip.py` (+ phash moved to `metadata.py`) |
| `app/services/storage.py` | same name; adds presign, private buckets |
| `scripts/bulk_upload.py`, `synch_storage.py` | `scripts/bulk_import.py` using the same upload API |

---

## 10. Migration plan

Phases are ordered by dependency. Each phase ends in a working system. Check boxes as completed.

### Phase 0: Hygiene (half a day)

- [x] `git init`; add `.gitignore` (`.env`, `.venv`, `__pycache__`, `.pytest_cache`, `.DS_Store`, `ex_photos/`, `bsmall/`, `app_imgs/`, `instances_val2017.json`).
- [ ] Rotate all credentials in `.env` (owner action; cannot be done by an agent). `.env.example` with names only is done.
- [x] Fix `requirements.txt`: add `geoalchemy2`, `sentence-transformers`, `imagehash`, `pillow-heif`, `deepface` (until replaced), `alembic`; pin versions; split dev deps into `requirements-dev.txt`.
- [x] Remove the debug EXIF `print` blocks.
- [x] Add `ruff` and a minimal CI workflow (`.github/workflows/ci.yml`, `pyproject.toml`, `tests/test_smoke.py`).

**Done when:** a fresh clone installs and `pytest` runs green in CI.

### Phase 1: Schema, migrations, RLS (done in code; see "Run against Supabase" below)

Implemented as the **expand** half of an expand/contract migration: new structures sit beside the legacy ones so the prototype keeps working. Each later phase drops the legacy columns it replaces (listed per phase below).

- [x] Add Alembic (`migrations/`); `0001` baselines the prototype schema, `0002` migrates to the tenancy schema.
- [x] Add `library`, `library_member`, `person`, new `photo` and `face` columns, `face_cluster`, `album_photo`, `biometric_consent`, `face_credential`, `audit_log`; backfill existing rows into one library.
- [x] Enable RLS and policies for every tenant table; service-only tables have no user grant.
- [x] `app/db/rls.py` (`rls_session`, `service_session`) and `app/auth/deps.py` (`get_rls_session`; `current_user_id` rejects everything until Phase 2).
- [x] `init_db()` no longer calls `create_all`.
- [x] Integration tests: `tests/test_rls_isolation.py` (27 cases) and `tests/test_migration_backfill.py`, run in CI against a pgvector + PostGIS container.
- [ ] **Run against Supabase (owner action):** back up, then `alembic stamp 0001 && alembic upgrade head` using the direct connection string in `MIGRATION_DATABASE_URL`. See README.

**Done when:** an integration test proves a member of library 1 cannot select, insert, or update anything in library 2, and passes in CI. (Verified locally, including a deliberate policy break that the suite catches. CI itself has not run yet.)

**Where the implementation differs from the section 3 sketch**

| Sketch | As built | Why |
| --- | --- | --- |
| `photo.library_id` NOT NULL | nullable | The legacy upload endpoint does not set it. Make NOT NULL in Phase 2. |
| `profile.id` references `auth.users` | no FK yet | Profiles pre-date Supabase Auth wiring. Add the FK in Phase 2. |
| `photo.status` in 4 values | adds `duplicate` | Ingestion marks exact duplicates (section 4). |
| `taken_at_source` list | adds `legacy` | Existing dates came from the unreliable tag-306 path. |
| `face.embedding` | still `face_embedding` | Legacy search code references the old name. Rename in Phase 4. |
| `photo.uploader_id` only | also keeps `user_id` | Legacy endpoints. Drop `user_id` in Phase 2. |
| Library creation by insert | `create_library(name)` SQL function | Direct inserts are denied; the function atomically makes the caller owner. |
| Faces written by users | users can only update (label) faces | Faces are created and deleted by the worker (service role) only. |

**Security notes**

- Existing tables created by the prototype had **no RLS**. On Supabase, tables in `public` are reachable through the Data API with the anon key unless RLS is on. Until `0002` is applied, treat the current database as readable by anyone holding the anon key.
- `0002` drops `profile.face_embedding` (unconsented biometric data, unused). The downgrade re-adds it empty.
- Backfill role mapping: heaviest uploader becomes `owner`; anyone who uploaded a photo or created an album becomes `editor`; everyone else `viewer`. Review the result before inviting anyone.

### Phase 2: Auth (email and passkey) and membership (done)

- [x] Supabase JWT verification (`app/auth/jwt.py`): ES256/RS256 only, public keys from the project's JWKS endpoint, signature + expiry + audience + issuer + subject required; HS256 and `alg: none` rejected.
- [x] Profile creation on first sign-in (`app/auth/profiles.py`); unique `handle`.
- [x] `GET/PATCH /api/me`; libraries (`/api/libraries`): create, list, rename, members (list, change role, remove, leave), invites (create, list, revoke); `POST /api/invites/accept`.
- [x] Invites: single-use, 7-day expiry, bound to an email and a role, token returned once and stored only as a SHA-256 hash; accepting requires the signed-in, verified email to match. Delivery is by link that the owner shares (no email sending, so no service-role key is needed).
- [x] Last-owner guard in the database (trigger), plus owner-only member management in RLS.
- [x] Every prototype route requires sign-in and takes the user from the token; `user_id` is gone from all requests. Uploads go into a library the caller can write to; image serving checks library membership; `wipe-data` is dev-only, per library, owners only.
- [x] RLS session re-applies the user identity on every transaction (`after_begin`), so code after a mid-request `commit()` cannot silently run as the table owner.
- [x] `/login` developer page and `/api/config` (public project URL and publishable key only).
- [x] Audit log entries for library, invite and role events. Audit failures are logged and never fail a completed request.
- [x] Migrations `0004` (invites, last-owner trigger) and `0005` (drop `photo.user_id`, `albummemberlink`, `face_credential`; `photo.library_id` and `uploader_id` required; `profile.id` FK to `auth.users`, added NOT VALID).
- [x] `scripts/link_owner.py`: re-points a pre-existing profile (library membership, uploads, albums) to a real sign-in account in one transaction; dry run by default.
- [x] Tests: token checks, 20 endpoint tests (roles, invites, isolation, audit), anonymous-access test over every prototype route, rehearsed migrations on a database shaped like production.

**Remaining owner steps**

- [x] Set `SUPABASE_ANON_KEY`, sign in at `/login`, and run `scripts/link_owner.py --apply` (done: the existing library and 4 photos now belong to the owner's real account; `profile_id_auth_fkey` validated).

**Known limitations (accepted for now)**

- Library deletion is intentionally not exposed: it would orphan the library's files in R2. A cleanup job arrives with Phase 3/7.
- Supabase lets anyone request a sign-in link, so strangers can create an account. They can see nothing (no library membership, RLS) but it is noise; add captcha or disable public sign-up later.
- Supabase's built-in email sender is rate-limited; set up a custom SMTP provider before inviting many people.
- Passkeys are available in Supabase Auth but not wired into `/login`; the real client (Phase 6) adds them.

**Done when:** all endpoints reject anonymous requests and no endpoint accepts a client-supplied user id. (Verified by `tests/test_prototype_routes_require_auth.py` and against the running app.)

### Phase 3: Ingestion (done; one owner step remains)

**Decisions (owner, 2026-11-20)**

1. **Existing data:** the 4 photos already in the database are test data and may be deleted; no migration of old R2 objects. New object keys are library-scoped: `{library_id}/originals/{photo_id}`, `{library_id}/display/{photo_id}.webp`, `{library_id}/thumbs/{photo_id}.webp`. The bucket becomes private (owner turns off public access in Cloudflare once the new flow works).
2. **Queue:** a small Postgres table queue (`SELECT ... FOR UPDATE SKIP LOCKED`, lease with expiry, attempts, exponential backoff, dead-letter status) instead of Procrastinate. Reasons: works through the Supabase transaction pooler (Procrastinate needs a session/direct connection for LISTEN/NOTIFY), no extra dependency, and plenty for this scale. Revisit if throughput or scheduling needs outgrow it.
3. **Two stages:** `process_photo` (fast: copies, dates, status `ready`) then `enrich_photo` (CLIP and faces, re-runnable per `model_version`), so Phase 4 can re-embed without re-importing.
4. **Originals are kept byte-for-byte** (HEIC stays HEIC; no JPEG re-save). Alongside: a WebP display copy and a WebP thumbnail. Photos only; no video or Live Photos.
5. **Hosting:** the owner plans to run on a cloud server. The API and worker are separate processes configured only by environment variables, so both containerize cleanly in Phase 7. Worker needs enough RAM for the ML models.
6. **Limits:** JPEG, PNG, HEIC, WebP; 50 MB per file (revisit for RAW).

**Owner steps for this phase**

- [x] Add the CORS policy in `infra/r2-cors.json` to the R2 bucket (done and verified live by `scripts/check_storage.py`: preflight allows the origin and `PUT`, refuses a foreign origin).
- [ ] Turn off public access on the bucket (Cloudflare: R2 > bucket > Settings > Public access / r2.dev subdomain / custom domain). `scripts/check_storage.py` currently reports `STILL PUBLIC`. Nothing in the app uses the public URL any more, so this is safe to do now.
- [ ] When a real domain exists, add its origin to the CORS policy (never `*` for uploads).

**Tasks (all done)**

- [x] `POST /api/uploads` and `/api/uploads/complete` with presigned PUT. Only `Content-Type` is signed; boto3 checksum headers disabled (`request_checksum_calculation="when_required"`). Per-file validation (type by content-type or extension, 50 MB cap), library-scoped keys, owners and editors only, completion verifies existence and size and is idempotent.
- [x] Table-based job queue (`app/services/jobs.py`) and worker (`python -m app.worker`): `FOR UPDATE SKIP LOCKED`, leases, at-least-once redelivery after a crash, exponential backoff, dead-letter, permanent-failure fast path. Tested with 8 competing workers and a simulated crash.
- [x] Stage 1 `process_photo` and stage 2 `enrich_photo` (`app/services/pipeline.py`): content-based validation (not the filename), decompression-bomb guard, EXIF orientation applied, WebP display (2048 px) and thumbnail (400 px) with ICC profile kept, original stored byte-for-byte, sha256 exact-duplicate detection per library (duplicate's storage freed), perceptual-hash near-duplicate flag, GPS to PostGIS.
- [x] Date ladder with provenance (`app/services/extract_metadata.py`): `DateTimeOriginal` (read from the Exif sub-IFD, fixing the prototype's tag-306 bug) with UTC offset, then `DateTimeDigitized`, filename patterns (including Pixel milliseconds), then browser file mtime. Manual dates are never overwritten by reprocessing.
- [x] Private bucket access only through signed links: `GET /api/photos/{id}/url?size=thumb|display|original` after an RLS check; thumbnails included (signed) in list, detail and search results. The old byte-proxy and public-URL code is removed.
- [x] Photo endpoints: list (keyset pagination, newest capture date first), detail, retry failed, correct the date by hand, delete (row, faces, then files via a queued job).
- [x] `/upload` developer page (progress per file, CORS hint on failure) and `scripts/bulk_import.py` (drives the real API; folder import with duplicate-safe re-runs).
- [x] Contract migration `0006`: dropped `storage_path`, `thumbnail_path`, `p_hash`, `captured_at`; new photo columns; `job` table (service role only); unique content hash only applies to non-duplicates.
- [x] Live checks: `scripts/check_storage.py` against the real R2 bucket; the whole pipeline (real HEIC photos, real CLIP and face models) against the real Supabase and R2 in a throwaway library, then cleaned up. Old test photos deleted before the migration.
- [x] Tests: 178 total, including real HTTP uploads to an in-process S3 server (moto), the queue under concurrency and crashes, dates, imaging (orientation, color modes, HEIC round trip, bombs), permissions, pagination.

**Done when:** importing real photos produces `ready` photos with correct orientation and sensible `taken_at_source`, and a killed worker mid-batch resumes without duplicates. (Verified: 32 sample photos decoded; crash recovery tested; live run on real infrastructure.)

**Known limitations (accepted for now)**

- The worker must be running (`python -m app.worker`); uploads queue up but do not process without it. Hosting it is a Phase 7 task.
- 10 of the 32 sample photos carry no embedded date (stripped by messaging apps or screenshots) and fall back to file time. Neighbor-based date inference (guess from photos taken around the same upload) is not implemented; use the manual date correction for now, and see Phase 6 for batch date shifting.
- Rows left in `pending` (the user never finished the upload) are not cleaned up yet.
- Near-duplicate detection scans the library's hashes per photo: fine at family scale (tens of thousands), revisit if a library grows far larger.
- moto does not verify upload signatures, so signing is covered only by the live `scripts/check_storage.py`; run it after any storage or boto3 change.
- Search endpoints are still the prototype versions (Phase 5 replaces them), and the API process still loads the CLIP model at startup for text search.

### Phase 4: Faces and people (4a done; 4b in progress; 4c to 4e remain)

**Decisions (owner, 2026-11-21)**

1. **Who can name people:** owners and editors only. Viewers can see names and search by them.
2. **Test data:** `app_imgs/` (gitignored): 7 people plus an `others/` folder of strangers, 222 photos. Many carry no metadata (they were sent through chat apps) and many are small (360 px), which is expected and useful as the hard case. Garo is evaluated from the one close-up supplied (plus a second solo face the detector found).
3. **Use is non-commercial** (private family album), which is what makes the InsightFace models acceptable (see the license note below).
4. **"This is me" is the preferred path** (owner, 2026-11-21): members who enroll selfies are found automatically; owners/editors still name people without accounts (children, elders, the deceased). It is offered once after joining, skippable, never nagged.
5. **No editor approval for claiming a person.** Any member may claim an unclaimed person; a person can be claimed once; a claim never changes existing labels (selfies only produce correctable automatic matches); claims are audit-logged and the owner can unlink one. If the selfie does not resemble the person's labeled photos the member gets a warning, not a block.
6. **Open sign-up.** Anyone can sign in, then create a group or join by an emailed invite link (invites stay bound to one email). Needs abuse limits before launch: groups per account, storage quota per group, upload rate limit.
7. **Nobody has to name everyone** (owner, 2026-11-22): "Who is this?" lists only unnamed groups whose faces appear in at least `FACE_GROUP_MIN_PHOTOS` (3) *different photos* (three faces in one photo do not count). Rarer faces are only counted ("N faces seen in only a few photos are hidden"), with a "show them anyway" switch; a group that keeps appearing crosses the line on its own. Pull-only, like everything else here.
8. **Same name, two people:** allowed, but the pair (name, `qualifier`) is unique per library (`uq_person_name`), so two Sarahs must be told apart ("Sarah" and "Sarah, cousin"). Enrollment: a name that matches an unclaimed person offers "is this you?" with photos; a claimed one offers a new person with a qualifier; no match creates one. Search asks "which Sarah?" when ambiguous.

**4c progress (started 2026-11-25; screens and rules in [UI_SPEC.md](UI_SPEC.md))**

- [x] Join by link or six-character code: migration `0009` (applied to the live
  database 2026-11-25 after a backup and a rehearsal on a restored copy: data unchanged, downgrade and re-upgrade clean),
  `app/services/join.py`, `GET/PUT/DELETE /api/libraries/{id}/join-link`, `.../reset`, `POST /api/invites/join`,
  `library_member.joined_at`. One reusable link per album, editor or viewer, 30 days, owners only. Wrong codes are
  limited per account and per address; link tokens are not limited. Token and code are stored readable (owners only,
  RLS) so they can be shown again. This replaces decision 6's email-bound invites for the app; the email invite
  endpoints remain. Tests: `tests/test_join_links.py` (the safety rules were mutation-checked).
- [ ] The `/join` page that the link opens (comes with the sign-up and join screens).
- [x] Removing a member expires the link and code (owner, 2026-11-25; database trigger, so any path does it). Leaving
  on your own does not.
- [ ] **Open:** someone changed to viewer can leave and rejoin through an editor link, regaining editor.
- [ ] Hosting (Phase 7): pass the real client address through any proxy, or all users share one address limit.
- [ ] Still to do in 4c: sign-up and album screens, account selfie and consent, per-album recognition consent, names
  and claim a person, opt-out and data deletion, guardian handling, abuse limits.

**4a. Face model (done)**

- [x] **Benchmark and switch.** Same labeled faces, same measures. Facenet512 (DeepFace, the prototype's model): nearest stranger at distance 0.48, same-person 90th percentile 0.35 (a gap of 0.13). InsightFace `buffalo_l`: nearest stranger 0.79, same-person 90th percentile 0.42 (a gap of 0.37), 99% closed-set accuracy vs 96% on the common faces, 7/7 vs 5/7 on faces under 60 px, about 4x faster. Adopted. DeepFace, RetinaFace, TensorFlow and tf_keras are removed from `requirements.txt`.
- [x] **Zoom/crop/flip/color-order tricks tested on the old model: none helped** (they made strangers look more similar). The one real zoom is embedding from the full-resolution original, which `app/services/faces.py` does: detect on a 2048 px copy, map the landmarks back, embed from the original's pixels.
- [x] `app/services/faces.py`: `FaceEngine` interface, `InsightFaceEngine`, `MODEL_VERSION = insightface-buffalo_l-v1`, minimum confidence 0.5 and minimum face size 32 px, `quality = det_score * min(1, px/112)`.
- [x] Worker enrichment uses it: faces come from the original (falls back to the display copy if the original is gone), stored with bbox, det_score, quality and model version. **Re-running with the same model never touches faces** (people attach labels to them); a new model replaces only the old model's unlabeled faces. New `photo.faces_model` records which model produced a photo's faces.
- [x] Migration `0007`: `face.face_embedding` renamed to `embedding`; legacy `photo.album_id` and `face.album_id` dropped; `photo.faces_model`. Applied to the live database; the 3 existing photos were re-processed (`scripts/reenrich.py`).
- [x] Prototype face search switched to the new engine and cutoffs (`FACE_AUTO_MAX`, `FACE_SUGGEST_MAX` in `app/config.py`); only same-model faces are compared.
- [x] **Evaluation harness** `scripts/eval_faces.py` (cached; `--check` fails if the auto cutoff makes any wrong or stranger match). Result on 81 known faces and 14 strangers, embedded exactly as the worker does: at `FACE_AUTO_MAX = 0.45`, 88% right, 0% wrong, 0% strangers; at `FACE_SUGGEST_MAX = 0.60`, 91% right, 0% wrong, 0% strangers. The model's few mistakes sit at distance 0.70+, so the cutoffs filter them. Nearest stranger 0.78. Cutoffs are provisional until the labeled set grows (only 14 strangers, small faces under 60 px are 9/12).
- [x] **License.** InsightFace's code is MIT, but its pretrained models (including `buffalo_l`) and training data are "for non-commercial research purposes only" per its README (read 2025-11-21; a 2025-11-24 note routes licensing questions to recognition-oss-pack@insightface.ai). Acceptable for a private, non-commercial family album as a judgment call (not legal advice); revisit before any paid or commercial use. The model sits behind `FaceEngine` and every face records `model_version`, so it can be swapped. Fallback to benchmark with the same harness: a permissively licensed model such as OpenCV's SFace (license to be verified). The model downloads (about 280 MB) on first use; the real-model tests run only when it is present (set `RUN_FACE_MODEL_TESTS=1` in CI to force them).

**4b to 4e (remaining)**

- [x] Migration `0008` (applied to the live database 2026-11-21 after a backup and a rehearsal on a restored copy: row counts unchanged, downgrade and re-upgrade clean): `person_alias`, `person_reference` (embeddings only, biometric: readable by owners/editors and the person themselves, no API write path), `face_rejection` (a rejected suggestion never returns), `person.recognition_opt_out`, `person.qualifier` (unique with the name), `face_cluster.model_version`. Cross-library links are blocked by the policies. Tests: `tests/test_people_schema.py`.
- [x] `app/services/people.py`: `match_face` (auto vs suggest tiers; reports the runner-up so the margin rule can be added), `cluster_faces` (centroid plus a every-member distance cap so look-alikes cannot chain people together), `assign_library_faces` (auto labels are recomputed, ground truth never touched, opt-outs and rejections honored, suggestions never stored) and `recluster_library` (named clusters are never rebuilt). Tests: `tests/test_people.py`.
- [x] `recluster_people` job (`app/services/pipeline.py`): queued 30 s after a photo's faces are found and 5 s after any edit, one per library (a burst of uploads or edits becomes one run); loops if faces arrive mid-run.
- [ ] **Open design question (decided with the owner, to brainstorm before building): margin rule.** Auto-label up to a looser distance only when the best person is clearly closer than the runner-up (for example by 0.10), to be tested on the full evaluation set. **Constraint: labeling is never pushed.** Suggestions appear only when someone opens the "Who is this?" page; no notifications, banners, or prompts asking people to label.
  **Amended by the owner 2026-11-25:** a one-time "Who is this?" pop-up for people who appear often is allowed; see
  [UI_SPEC.md](UI_SPEC.md) ("Help, settings and naming") for its limits.
- [x] People endpoints, `app/api/people.py` (tests: `tests/test_people_api.py`): list/create/update/delete people; merge (refuses if the duplicate is linked to an account or has enrolled selfies); `GET /groups` (unnamed groups, largest first, with an on-demand suggestion; editors only; read-only, stores and queues nothing); name a group (existing or new person); label a face (`manual`, or `suggestion` = confirmed); reject a face for a person (remembered). Every change needs owner/editor, checked in the endpoint and again by Row Level Security.
- [ ] Delete a person's data on opt-out (4c).
- [x] Aliases: add/remove endpoints (the same nickname may belong to two people on purpose; search disambiguates).
- [ ] Alias/name resolution shared with search (Phase 5).
- [x] `/people` page (`app/web/people.html`): everyone sees the people list; owners/editors also get the "Who is this?" groups (face crops, suggestion yes/no, type-ahead name box that distinguishes picking an existing person from creating a new one, qualifier prompt on a name clash), nicknames, rename, merge, delete. Pull-only: it never notifies or prompts. Group-level reject: `POST /groups/{id}/reject`. Saving a name that already exists asks "same person?" instead of silently joining them (found in the first real test: a look-alike stranger was merged into Ananya). Group ids are stable across regroups (found in the same test: "Group not found"). Each person has a **Faces** panel to review and correct mistakes: `GET /{id}/faces` (automatic labels first, least confident first), `POST /{id}/faces/remove` (unlabel and remember the rejection), `POST /{id}/faces/move` (to another person, as ground truth); a corrected face is never re-labeled the old way by a regroup. **First real-photo test: 2026-11-22 on a 'Face test' group of 330 photos; two UI bugs found and fixed.**
- [ ] Self-enrollment: recognition consent, selfie quality checks, claim-yourself endpoint, owner/editor approval when claiming an already-labeled person, backfill-match job.
- [ ] Guardian enrollment for a child's person; opt-out and data deletion.
- [ ] "Is this you?" suggestion inbox for the enrolled person.
- [ ] Age robustness test: dated solo photos of the same person across years (gustavo, natalie) once faces are labeled.

**Done when:** on the labeled folders the eval reports precision ≥ 0.95 at the chosen thresholds, and thresholds in `config.py` cite that report; a member who enrolls selfies is matched in the library's photos without anyone labeling them; opting out removes their references and labels and stops all future matches.

### Phase 5: Search

**Phase 5, semantic search (decided 2026-11-23, deferred):** "Preeya with cat" = filter by person and date first, then rank
the rest by CLIP text-to-image similarity (the vectors already exist: `photo.clip_embedding`, `clip-ViT-B-32`; the
prototype `/api/search` ranks without a threshold). To do, in order: (1) record which model made each vector
(`photo.clip_model`, as `photo.faces_model` does for faces); (2) benchmark a newer model (a larger CLIP or SigLIP) on real
searches such as beach, dog, cake; (3) a re-embedding job (safe while the photos are test data; with real photos, run it in
the background and keep the old vectors until it finishes); (4) a minimum score and a "Best matches" label so a search with
no real match is not presented as a result; (5) place: GPS to place names for exact places, CLIP for scenes. Named pets would
be a separate feature.

**Matching rules for Phase 5 (owner, 2026-11-23):** every name comparison (people, nicknames, short descriptions, search
text) uses `app.services.people.fold`: capitals, accents and extra spaces are ignored, so "maria" finds "María". Invite
codes and emails are case-insensitive. **Album filters:** people (together or any), dates (year, range, on this day, no
date; local dates via `taken_local`), added by; the count of matching photos is shown before applying.


- [ ] Single `GET /search` per section 6.3 with keyset pagination and signed thumbnail URLs.
- [ ] Age-window matching (6.2).
- [ ] Name search through aliases, with disambiguation when a name matches several people.
- [ ] Date filters (`from`, `to`) and any "on this day" logic compare the LOCAL wall-clock date (`taken_local`: the stored instant shifted by the camera's recorded UTC offset, as the photos API already returns), never the UTC date. A 8:37 pm photo in UTC-5 belongs to that evening's date, not the next day's.
- [ ] Delete `search_face.py`, `search_faces_in_db.py`, and the old `/search` router.

**Done when:** "person X, from A to B" returns only photos dated in range, with bbox and match kind, and p95 latency is under 500 ms on a 20k-photo library.

### Phase 6: Client

- [ ] PWA (or native) with: upload and progress, people grid, "who is this?" naming, search with person chips and a timeline scrubber, photo viewer with face boxes, date editing.
- [ ] Device import (iOS photo library) if native is chosen (open question 3).

**Done when:** a non-technical family member can onboard, import, and find "Mia, 2019" without help.

### Phase 7: Production readiness

- [ ] Staging and prod environments; secret manager; Dockerfiles for API and worker.
- [ ] Sentry, metrics, dashboards, alerts on queue depth and failure rate.
- [ ] Backups and a tested restore.
- [ ] Data export and account deletion.

**Done when:** a restore drill succeeds and an owner can delete a library end to end.

---

## 11. Notes for agents working in this repo

- Work one phase at a time. Do not start a phase whose predecessor's "Done when" is unmet.
- Every change that touches tenant data needs an RLS test. If you add a table with `library_id`, add its policies in the same migration.
- Never log or print embeddings, face images, signed URLs, or tokens.
- Thresholds live in `app/config.py` with a comment pointing to the eval report that justified them. Do not change a threshold without re-running `scripts/eval_faces.py` and committing the new report.
- Schema changes go through Alembic only. Never `create_all`.
- Do not add new endpoints that accept `user_id` or `library_id` from the client without checking membership via RLS.
- Keep ML imports out of `app/api/`. The API process must start without loading models.
- When a decision below is still open, ask the owner instead of picking one silently.

---

## 12. Open questions for the owner

1. **Hosting:** the owner plans a cloud server for the API and worker; which provider is still open. Stay on Supabase and R2 for now. Affects the secret-manager and backup choices.
2. **Client:** PWA only, or a native iOS app for photo-library import?
3. **Library model:** one library per family is assumed. Do you need people shared across two libraries (e.g. two sides of a family)?
4. **Legal:** which jurisdictions will members be in? That decides the consent and retention text.
5. **Existing data:** can `ex_photos/` and `bsmall/` (COCO images) be deleted from the repo? They look like test fixtures unrelated to family photos.
