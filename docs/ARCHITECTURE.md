# Family Photo Album: Target Architecture & Migration Plan

Status: proposed · Written 2026-11-19 · Audience: engineers and coding agents working in this repo

This document is the single source of truth for moving the prototype in `app/` to a production design. Work is broken into phases at the end (section 10) with checkboxes and acceptance criteria. If you are an agent picking up a task, read sections 1 and 11 first, then the phase you are assigned.

---

## 1. Goals, scope, non-goals

**Goal:** a private, family-only photo album where anyone can ask for "Mia, summer 2019" and get the right photos, and where members sign in with a name and a face picture.

**In scope**

- Person-centric data model: named people who do not need accounts (children, grandparents, deceased relatives).
- One search that combines person(s), time range, and free text.
- Sign-in with name + face, with a safe fallback (section 5).
- Reliable bulk import of large archives (20k+ photos, mostly iPhone HEIC).
- Tenant isolation enforced by the database (Row Level Security), not by endpoint code.

**Non-goals for v1**

- Public sharing, social features, video, pet recognition.
- Face recognition as the *only* authentication factor (section 5.1 explains why).
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
       created_by uuid references profile, created_at timestamptz,
       unique (library_id, profile_id))

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

-- Face sign-in (separate from photo faces; see section 5)
biometric_consent(id uuid pk, profile_id uuid references profile, notice_version text,
                  granted_at timestamptz not null, revoked_at timestamptz)
face_credential(id uuid pk, profile_id uuid references profile, embedding_enc bytea not null,
                model_version text, consent_id uuid references biometric_consent, created_at timestamptz)

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

The API connects as the user (forwarding the JWT, `set local role authenticated` + `request.jwt.claims`) for all request-scoped reads and writes. The worker uses the service role and always sets `library_id` explicitly. `face_credential`, `biometric_consent`, and `audit_log` have **no** user-facing select policy; only the service role touches them.

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

## 5. Authentication: sign in with name + face

### 5.1 Threat model and the key design decision

Face sign-in is convenient, but **a face photo is not a secret**. In this product the album itself is a library of photos of each member's face, so anyone who sees a family member's photos (a shared link, a compromised account, a screenshot) has the spoofing material. Therefore:

- Face is a **verification step on a trusted device**, not a root credential. A new device must first be bound through a stronger factor (passkey or email magic link).
- Verification is **1:1** (does this selfie match *this* account's enrolled face), never 1:N across all users.
- Liveness detection is required at both enrollment and login.
- Rate limiting and lockout apply.

### 5.2 Account and enrollment

1. User is invited to a library by an owner (email link). They create an account with email magic link or passkey (Supabase Auth). They pick a unique `handle` and `display_name`.
2. Notice and consent screen (section 7). Records a `biometric_consent` row. No consent, no enrollment.
3. Enrollment: capture 3 to 5 selfies with varied angle/lighting. Each must pass liveness and quality checks (single face, minimum size, not blurred, eyes open). The worker computes embeddings, stores them encrypted in `face_credential.embedding_enc`.
4. The device is registered as trusted (passkey created, or a device-bound refresh token stored in the platform keychain).

### 5.3 Login flow

```
Client                         API (auth service)                  Worker/ML
  │ POST /auth/face/start {handle, device_id}                          │
  │ ───────────────────────────────►  rate-limit; look up profile      │
  │ ◄─ {challenge, liveness_session}   (same response whether or not  │
  │                                     the handle exists)             │
  │ capture selfie + liveness proof                                    │
  │ POST /auth/face/verify {selfie, liveness_proof, challenge} ───────►│
  │                                    embed selfie ◄──────────────────│
  │                                    cosine vs. enrolled templates   │
  │                                    pass = max sim ≥ T and          │
  │                                           device is trusted        │
  │ ◄─ session (short-lived JWT + refresh)   fail = generic error      │
```

Rules:

- **Trusted device required.** Unknown device means face is not offered; the user does magic link or passkey, then optionally enrolls the device for face unlock.
- **Uniform errors.** Never reveal whether a handle exists or whether the face or the device failed. Constant-ish response time.
- **Lockout:** 5 failed attempts per account per 15 minutes and 20 per IP per hour; after that, require magic link. Log every attempt to `audit_log`.
- **Threshold `T`** is set from an evaluation (target false accept rate ≤ 1 in 10,000 on the labeled set, then add margin), not copied from a library default. Store `model_version`; re-enroll on model change.
- **Liveness:** use an established service or on-device API (e.g. a managed face-liveness provider, or platform biometrics) rather than rolling our own. A simple "blink or turn your head" challenge is a minimum; passive liveness is better. Decide the provider in the open questions.
- **Session minting:** Supabase Auth has no native "face" provider. After a successful verify, the server uses the admin API to create a normal Supabase session for that user so RLS (`auth.uid()`) keeps working unchanged. Confirm the exact admin call for the pinned Supabase version when implementing; keep that endpoint server-only, rate-limited, and audited.

### 5.4 Face credential storage

- Separate table and separate model call from photo-face search. Login templates are never mixed into the photo `face` table, and photo faces are never used to log anyone in.
- Embeddings encrypted at the application layer (envelope encryption, key from the secret manager). Matching is done in app memory for one account's few templates, so encryption does not break search.
- Deletion: revoking consent or deleting the account hard-deletes `face_credential` rows within 24 hours and writes an audit entry.

---

## 6. Search and people

### 6.1 Naming people

1. **Cluster.** After each import, assign each new face to the nearest existing cluster in the library if cosine distance < `T_cluster`, otherwise start a new cluster. Periodically re-cluster the library (HDBSCAN or agglomerative) as a background job.
2. **Label.** The UI shows clusters largest-first ("Who is this?"). A member names a cluster once (or merges it into an existing person); all its faces get `person_id`, `person_source='manual'`.
3. **Suggest.** For unlabeled faces, if the nearest exemplar of any person is closer than `T_suggest`, store a suggestion; the UI asks "Is this Mia?". Confirming sets `person_source='confirmed'`. Auto-assign without confirmation only above a stricter `T_auto`, stored as `auto` and visibly marked.
4. **Exemplars** for a person = faces with `manual`/`confirmed` source. These are what matching uses.

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

- **Biometric data:** face embeddings are biometric identifiers. In some jurisdictions (e.g. Illinois BIPA, GDPR Art. 9, Texas CUBI) collection needs written notice, informed consent, and a published retention/destruction schedule. This applies to login templates *and* to embeddings of people in photos, including children and people who never signed up.
- **Consent model:** library owners accept a notice when enabling face features; each member consents separately for face login. Persons without accounts are labeled by a member; provide "remove this person's face data" which deletes their embeddings and unlinks `person_id`.
- **Retention:** delete biometrics on account/person deletion and on consent revocation. Default cap of 3 years after last activity for login templates, owner-configurable.
- **Secrets:** move credentials out of `.env` into a secret manager for deployed environments; `.env` is gitignored and never committed. Rotate R2, Hugging Face, and database credentials before the repo is ever pushed anywhere, since `.env` has lived unprotected in a non-git directory.
- **Transport/storage:** TLS everywhere; provider encryption at rest; app-level encryption for `face_credential`.
- **Input hardening:** upload size cap, content-type sniffing (not trusting the extension), pixel cap, per-user upload rate limits, strict CORS to the web origin.
- **Authorization:** all request-scoped DB access runs under RLS. No endpoint accepts a `user_id` from the body or form (the prototype does).
- **Logging:** structured logs with request id, user id, library id; no face images, embeddings, or signed URLs in logs.
- **Audit:** `audit_log` for login attempts, role changes, person deletion, biometric consent changes, bulk exports.
- **Export/delete:** a member can export their uploaded originals and delete their account; an owner can delete a library (cascades to R2 objects via a cleanup job).

---

## 8. Operations

- **Local dev:** `docker compose up` brings up Postgres (pgvector + PostGIS image), a local S3-compatible store (MinIO) in place of R2, the API, and the worker. `make dev` wraps it.
- **Migrations:** Alembic, one baseline plus incremental. RLS policies live in migrations.
- **Tests:** `pytest`. Unit tests for date ladder, filename parsing, clustering assignment, age-window matching; integration tests against a throwaway Postgres with RLS **specifically asserting that user A in library 1 cannot read library 2** (the most important test in the repo); login flow tests with mocked liveness and embeddings.
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
  auth/                    # JWT dependency, RLS session, face login routes
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

### Phase 2: Auth (email and passkey) and membership

- [ ] Supabase Auth JWT verification dependency (`app/auth/`).
- [ ] Profile creation on first sign-in; `handle` uniqueness.
- [ ] Library creation, invite-by-email, role management endpoints.
- [ ] Remove `user_id` from every form and body; derive it from the token.
- [ ] Contract: drop `photo.user_id` and the `albummemberlink` table; make `photo.library_id` NOT NULL; add `profile.id` FK to `auth.users`.

**Done when:** all endpoints reject anonymous requests and no endpoint accepts a client-supplied user id.

### Phase 3: Ingestion

- [ ] `POST /uploads` and `/uploads/complete` with presigned PUT; MinIO for local dev.
- [ ] Procrastinate worker with `process_photo`, retries, `status` and `error`.
- [ ] HEIC decode, EXIF transpose, WebP display and thumb, sha256 dedup, pHash flag.
- [ ] Date ladder with provenance; unit tests with fixtures covering each rung.
- [ ] Private bucket plus signed GET endpoint; remove `R2_PUBLIC_URL` use and the `.dev/` string splitting.
- [ ] `scripts/bulk_import.py` that drives the real API.
- [ ] Contract: drop `photo.storage_path`, `thumbnail_path`, `p_hash`, `captured_at` (replaced by keys, `phash`, `taken_at`).

**Done when:** importing `app_imgs/` produces `ready` photos with correct orientation, sensible `taken_at_source`, and a killed worker mid-batch resumes without duplicates.

### Phase 4: Faces and people

- [ ] Swap DeepFace for InsightFace in `services/faces.py`; store bbox, score, quality, `model_version`; minimum-size filter.
- [ ] Re-embed existing photos (migration job) and drop old vectors.
- [ ] Clustering service and `recluster` job.
- [ ] People endpoints: list clusters, name or merge, confirm or reject suggestion, delete person data.
- [ ] `scripts/eval_faces.py` and first committed eval report.

- [ ] Contract: rename `face.face_embedding` to `embedding`; drop `face.album_id` and `photo.album_id` (replaced by `album_photo`).

**Done when:** on the labeled folders the eval reports precision ≥ 0.95 at the chosen thresholds, and thresholds in `config.py` cite that report.

### Phase 5: Search

- [ ] Single `GET /search` per section 6.3 with keyset pagination and signed thumbnail URLs.
- [ ] Age-window matching (6.2).
- [ ] Delete `search_face.py`, `search_faces_in_db.py`, and the old `/search` router.

**Done when:** "person X, from A to B" returns only photos dated in range, with bbox and match kind, and p95 latency is under 500 ms on a 20k-photo library.

### Phase 6: Face sign-in

- [ ] Decide the liveness provider (open question 1).
- [ ] `biometric_consent` flow and notice text (legal review).
- [ ] Enrollment endpoint, encrypted `face_credential`, trusted-device registration.
- [ ] `/auth/face/start` and `/auth/face/verify` with rate limiting, uniform errors, lockout, audit.
- [ ] Session minting via the Supabase admin API.
- [ ] Threshold `T` chosen from an eval run targeting FAR ≤ 1e-4.
- [ ] Revocation and deletion job.

**Done when:** a registered trusted device can sign in by name and face; an unknown device cannot; a printed photo or screen replay of an enrolled face is rejected by liveness in manual testing; lockout and audit entries verified.

### Phase 7: Client

- [ ] PWA (or native) with: upload and progress, people grid, "who is this?" naming, search with person chips and a timeline scrubber, photo viewer with face boxes, date editing.
- [ ] Device import (iOS photo library) if native is chosen (open question 3).

**Done when:** a non-technical family member can onboard, import, and find "Mia, 2019" without help.

### Phase 8: Production readiness

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

1. **Liveness provider:** managed service (cost, vendor data handling) or on-device platform biometrics (iOS/Android) for face unlock? This also affects whether a PWA is enough.
2. **Hosting:** stay on Supabase and R2, or self-host Postgres? Affects the secret-manager and backup choices.
3. **Client:** PWA only, or a native iOS app for photo-library import?
4. **Library model:** one library per family is assumed. Do you need people shared across two libraries (e.g. two sides of a family)?
5. **Legal:** which jurisdictions will members be in? That decides the consent and retention text.
6. **Existing data:** can `ex_photos/` and `bsmall/` (COCO images) be deleted from the repo? They look like test fixtures unrelated to family photos.
