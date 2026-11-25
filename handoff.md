# handoff.md: where the project stands

**Product name:** Memoria (working repo name: photoLibraryQuery)
**Last updated:** 2026-11-23, end of the session that built the people tools, the `/people` page, and the UI mockups.
**To update this file at the end of a chat:** follow [instruction_handoff.md](docs/instruction_handoff.md).
**To start a new chat:** read this file first, then [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (the plan) and
[docs/UI_SPEC.md](docs/UI_SPEC.md) (the screens). Do not re-ask anything answered below.

---

## 1. What this is

A private, **non-commercial** photo-sharing website for the owner's family **and friends**, especially good at finding
photos of specific people at specific times ("photos of Gustavo in 2019"). It is a **website** that works on phones
(add to home screen), not a store app. The least technical user is **a senior who is not tech literate**, so everything
must be simple. The owner (the person you are talking to) is not a software engineer: explain in plain language, keep
answers short, and say what you did and what they should do.

## 2. Decisions the owner has made (do not re-litigate)

**Product and people**
- Faces are for **finding people in photos only**. **No face login.** (Face ID on an iPhone is just the browser
  filling a saved password.)
- Sign-in is **email + password** (with Face ID autofill on iPhone). Sign-up asks only **"What's your name?"**.
- Selfie: taken **at sign-up (account level), skippable**. When joining or creating an album the app asks
  **"Can this album recognize you?"** with a "Very recommended" note. **Consent must be explicit** (nothing
  pre-ticked; Yes and Not now equally easy). Only a numeric face fingerprint is kept, never the photo.
- **"This is me" is the preferred path** for naming people. Owners and editors still name people without accounts
  (children, elders, the deceased). **No editor approval to claim a person.** A person can be claimed once; claims are
  audit-logged and the owner can unlink one.
- **Nicknames are asked per album** (when joining or creating one), not at sign-up, because different groups call you
  different things. Sign-up copy must **never assume "family"**: friends too.
- **Same name, two people:** allowed, but name + **short description** ("cousin", "blonde") must be unique per album
  (database column `qualifier`, shown to users as "Short description"). Nickname = another name people use (many allowed,
  may repeat, used for search). Saving a name that already exists **asks "same person?" with pictures**, never
  silently merges.
- **Nobody has to name everyone.** "Who is this?" only offers groups whose faces appear in **at least 3 different
  photos** (`FACE_GROUP_MIN_PHOTOS`). Rarer faces are hidden, counted, with a "show them anyway" switch.
- **Never push:** no notifications or nagging to name people. Prompts appear once, at the moment they are needed.
- **Invites default to the editor role** (owners can pick viewer for guests).
- **Roles:** owner (manage members/settings), editor (upload, delete photos, name/merge/correct people), viewer (read-only).
  Owners and editors name people; viewers can see names and search.
- **Search matching ignores capitals, accents and extra spaces** ("maria" finds "María"). Invite codes and emails are
  case-insensitive. (`app.services.people.fold`; Phase 5 search must use it.)
- Originals are kept **byte for byte**. Hosting will probably be a **cloud server**. Use is **strictly non-commercial**
  (this is what makes the InsightFace model license acceptable).
- Own name: **Memoria** (chosen). Availability and trademarks not checked.

**Proposed by Claude, accepted in discussion but not formally confirmed by the owner**
- **Join:** one "Invite people" button gives a **link and a six-character code** (like `K7M-4PX`, no 0/O/1/I,
  case-insensitive), **not tied to an email address**, 30-day expiry, one-tap reset, owner sees who joined, optional
  "approve new members" switch (off by default), failed-code attempts rate-limited.
- **Collections** (curated sub-sets inside an album) are **deferred**; search by person/date covers most uses. Separate
  albums (different people) already work. If collections are added, rename the unused `album` table to `collection` first.
- Open sign-up needs **abuse limits** before launch (albums per account, storage per album, upload rate).

## 3. Where the plan stands (phases)

| Phase | What | State |
|---|---|---|
| 0-1 | Migrations, tenant isolation (Row Level Security), CI | done, committed |
| 2 | Sign-in, albums ("libraries"), roles, invites | done, committed |
| 3 | Photo ingestion: signed uploads, worker, dates, duplicates | done, committed |
| 4a | Face model (InsightFace buffalo_l), evaluation harness | done, committed |
| 4b | People tools: API, regroup job, `/people` page, nicknames, merge, review | **done**, tested live on a test album |
| 4c | Sign-up/join flow, account selfie + consent, invite codes, claim a person, opt-out/delete, guardian for children, "Is this you?" | **not started** (mockups exist) |
| 4d-e | Aging test (same person across years), margin rule for auto-labels | not started |
| 5 | Search by person + time, filters, nicknames, CLIP semantic search, places | not started |
| 6 | The real app screens (photo grid, viewer, people, search) | mockups only |
| 7 | Hosting, backups, monitoring, limits | not started |

Full detail and checklists: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 4. Environment and how to run things

Python 3.11 venv in `.venv`. Always run from the project folder.

```bash
source .venv/bin/activate
unset postgres_url DATABASE_URL MIGRATION_DATABASE_URL     # VS Code can inject stale values from .env
uvicorn app.main:app --reload                               # the site: /login /upload /people /docs
python -m app.worker                                        # REQUIRED for uploads to finish (restart after code changes)
```

Tests (need Docker Desktop running):
```bash
docker compose up -d db
export TEST_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5433/postgres
pytest -q          # last run: 293 passed
ruff check .       # clean
```
Tests can never touch the real database (conftest pins `DATABASE_URL` to an unreachable address).

**Gotchas that have bitten us**
- **Docker Desktop must be running** for DB tests (errors "connection refused" on port 5433 mean it stopped).
- **The direct database host is IPv6-only**; on a network without IPv6 migrations fail with "could not translate host
  name". Fix (documented in `.env.example`): set `MIGRATION_DATABASE_URL` to the **session-mode pooler**: same as
  `postgres_url` but port **5432** (never 6543 for migrations). Verified working. *The owner has been told but may not have
  changed their `.env` yet.*
- **The worker does not hot-reload.** After changing `app/services/*`, restart it.
- Sign-in tokens last about **1 hour**. `/people` renews them quietly; `/upload` and the import script do not.
- Use **one address** (`localhost:8000`, not `127.0.0.1`): browsers keep separate sign-ins per address.
- Password-authentication errors usually mean a stale shell variable (see the `unset` line).
- `.env` holds real secrets: never print or commit it. Values: `postgres_url` (pooler 6543), `MIGRATION_DATABASE_URL`,
  Cloudflare R2 keys + bucket, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `hugging_face_token`.

**Before any live migration:** `python scripts/backup_db.py --label pre-XXXX`, rehearse on a local database restored from
that backup (`--restore-to`), check row counts and a downgrade/upgrade round trip, then `alembic upgrade head`.
Never `create_all`. Live database is at **0008 (head)**.

## 5. Architecture snapshot

**Stack:** FastAPI, SQLModel/SQLAlchemy, Alembic (hand-written SQL migrations), Postgres on Supabase with pgvector and
PostGIS, Supabase Auth (JWTs verified against public keys, ES256/RS256 only), Cloudflare R2 (private bucket,
presigned URLs), a Postgres table job queue and a worker process.

**Isolation:** every table has an album (`library`) id; Row Level Security enforces it; the API session re-applies
`SET LOCAL ROLE authenticated` plus the user's claims on every transaction (`app/db/rls.py`). `service_session()`
bypasses RLS (worker/admin only).

**Terms:** UI says "album"; the database says `library`. The existing `album`/`album_photo` tables (sub-sets) are unused.

**Key code**
- `app/api/`: `me`, `libraries` (+members, invites, accept), `uploads`, `photos`, `people` (new), `web` (pages),
  prototypes still present: `search` (CLIP text search, no threshold), `search_face`, `search_faces_in_db`, `delete_all` (dev-only).
- `app/services/`: `pipeline` (process_photo, enrich_photo, recluster_people), `jobs`, `imaging`, `extract_metadata`
  (date ladder), `storage`, `faces` (InsightFace), `people` (matching, clustering, `fold`), `ai` (CLIP).
- `app/web/`: `login.html`, `upload.html`, `people.html` (developer-grade pages; the real UI is Phase 6).
- `migrations/versions/` 0001-0008. `scripts/`: `backup_db`, `bulk_import` (`--label-folders`), `eval_faces`,
  `reenrich`, `link_owner`, `check_storage`, `phone_login`.
- `docs/ARCHITECTURE.md` (plan, decisions), `docs/UI_SPEC.md` (screens, wording, matching rules).

**Photo pipeline:** upload straight to R2 (signed PUT) -> `process_photo` (orientation, WebP display + thumb, real
capture date with provenance, GPS, exact + near duplicate detection) -> `enrich_photo` (CLIP vector, faces) ->
`recluster_people`. Dates use a ladder (EXIF original, digitized, filename patterns, file time) and `taken_local`
(local wall-clock) is what date filters must use.

## 6. Face recognition (the core)

- **Model:** InsightFace `buffalo_l` (`MODEL_VERSION = insightface-buffalo_l-v1`): detect on a 2048 px copy, embed from
  the full-resolution original. 512-d, cosine distance. Replaced DeepFace/Facenet512.
- **Cutoffs** (`app/config.py`): `FACE_AUTO_MAX = 0.45` (may label automatically, needs quality >= 0.5),
  `FACE_SUGGEST_MAX = 0.60` (offer as a suggestion), `FACE_GROUP_MIN_PHOTOS = 3`.
- **Evidence (336 labeled test photos, ~97 certain faces, 79 stranger faces incl. look-alikes):** at 0.45: 88% right,
  0% wrong, 0% strangers matched; at 0.60: 92%, 0%, 0%; strangers start matching at 0.70 (15%). Closest stranger
  to anyone: 0.62 (a dad look-alike). Weak spots: faces under 60 px (10/14), fia (4/6), few faces for garo.
- **Real-flow check on the "Face test" album:** of 635 faces, 507 labeled (464 by the owner, 43 automatic), 128 unnamed
  (all single-photo faces). Automatic labels: 13/13 right on certain faces; 7 more were of the owner. Owner's
  group-naming: 75/77 right (2 wrong faces from grouping: fia photo under "Carmela", marcus photo under "Dev";
  **not yet removed**, a good test of the Faces panel).
- **Rules:** labels a person made (`person_source` manual/confirmed) are ground truth and never overwritten;
  automatic labels (`auto`) are recomputed each run and never count as evidence; suggestions are computed on demand and
  never stored; opted-out people are never matched; a rejected person is never offered for that face again; only
  same-model faces are compared.
- **Clustering:** centroid-based with a cap so every member is within the suggest cutoff of every other (no chaining);
  **group ids are stable across regroups** (a bug found in testing).
- **Open:** a **margin rule** (auto-label up to a looser distance only if the best person is clearly closer than the
  runner-up) is to be brainstormed and tested on the full set; the **aging test** needs dated solo photos across years;
  the model license is non-commercial research only.

## 7. People tools (built this session)

- **API** (`app/api/people.py`): list/create/update/delete people; merge (refuses if the duplicate has an account or enrolled
  selfies); nicknames; `GET /lookup` type-ahead; `GET /groups` (repeat-threshold, suggestions, `min_photos`); name a group;
  reject a group; `GET /{id}/faces` (review order, or `best=true` to show who someone is); remove/move faces; label/reject a
  single face. Writes need owner/editor (checked in code and by RLS).
- **Page** `/people`: groups with "Looks like X?", type-ahead name box, the "same person?" question with pictures, short
  description prompts, nicknames, rename/merge/delete, a Faces panel to correct mistakes, silent session renewal.
  The save-name decision is a tested pure function (`decideSave`, `tests/test_people_page.py`, needs Node).
- **Schema (0008):** `person_alias`, `person_reference` (selfie embeddings, biometric RLS, no API write path),
  `face_rejection`, `person.recognition_opt_out`, `person.qualifier` (unique with the name), `face_cluster.model_version`.
- **Bugs the first real test found and fixed:** "Group not found" (regroup created new group ids; now stable); the page
  silently merged a look-alike into an existing person (now asks); the qualifier was ignored after "No, a different
  person" (decision now tested); whitespace-only names; `Group not found` recovery in the UI.

## 8. UI direction (mockups exist)

- **Mockups (private design canvas, 12 phone screens):** https://claude.ai/artifact/WsxtnxUYsLnZ4WMQspHEBw
  (sign in, create account, selfie, home/albums, join with code, names, recognize prompt, invite, photos with filter chips,
  filter sheet, search, "Which Maya?"). Working notes in [docs/UI_SPEC.md](docs/UI_SPEC.md).
- **Style:** like Google/Apple Photos; follows the phone's light/dark setting; big text (17-19 px), 56-58 px buttons,
  one clear action per screen. Phone first; iPad/laptop later.
- **Filter sheet:** People (Together = all in the picture, or Any of them; default Together, owner not yet asked to
  confirm), When (any time, year, pick dates, no date, on this day), Added by; button shows the matching count.
- **Not yet designed:** create-album screens, the "Is this you?" screen (same name, with photos), photo viewer, People tab,
  upload, settings, empty/error states, iPad/laptop.

## 9. Discussions and reasoning worth remembering

- **Why selfie at account level but consent per album:** a selfie reference belongs to a person *in an album*; asking per
  album keeps each group's consent separate and deleting an album deletes its copy. If the embedding model changes,
  people must re-take selfies (we keep no photo).
- **Why not unbound join links with editor default without limits:** anyone with the link can join; editors can delete
  photos. Mitigations: expiry, reset, owner sees joins, optional approval.
- **Why the grouping looked "mixed":** folders hold group photos, so a folder name is not the identity of every face.
  Certain identity = single-face photos only. Tightening the clustering showed no benefit on certain faces.
- **CLIP semantic search ("Preeya with cat"):** vectors already exist for every photo (`clip-ViT-B-32`). Plan: filter by
  person/date first, rank the rest by text similarity, show "Best matches" when weak. A newer model would find things
  better; recording `clip_model` and a re-embed job is the first Phase 5 task (safe now: all photos are test data).
- **Places:** GPS to place names for exact places, CLIP for scenes. Many chat-forwarded photos lost their GPS/dates.
- **Slow import:** `process_photo` took about 6 s per photo (cause not found; guesses: network round trips and the import
  script's constant polling). 4 workers gave ~0.58 jobs/s. Profile before hosting.

## 10. Live data and infrastructure

- **Supabase** project (database + auth); **Cloudflare R2** bucket `user-photos` (still publicly readable: turn off).
  R2 CORS allows `localhost:8000`, `127.0.0.1:8000`, and (owner added in Cloudflare only) `10.0.0.7:8000`: remove later.
- **Albums in the live database:** "Family" (the owner's real one, 4 old disposable photos) and **"Face test"**
  (id `692905e1-ac62-4672-8da0-3afb93881ca8`): 330 test photos imported from `app_imgs/`, about 20 people named.
- **Test data (gitignored):** `app_imgs/<person>/` (ananya, ari, dad, fia, garo, gustavo, ingrid, marcus, natalie, ronald)
  and `app_imgs/others/looks_like_*` (look-alike strangers; the folder `nicolas` is the owner). Imported with
  `--label-folders`, so each filename starts with its folder (the answer key).
- **Backups:** `backups/*.json` (gitignored, owner-only), including `pre-0008-*` and `pre-ananya-split-*`.
- Leftover prototype folders in the repo root (`bsmall/`, `ex_photos/`, `instances_val2017.json`) and a stray screenshot PNG:
  safe to delete or gitignore (ask the owner).

## 11. Open questions for the owner

1. "Together" vs "Any of them" as the default people filter (recommended: Together).
2. Is "Abuela's 80th" a separate album (different people) or a collection (same people)? Affects the collections decision.
3. Confirm the join design in section 2 (link + code, not email-bound).
4. Are the seven `looks_like_gustavo/nicolas` photos the owner? (Believed yes.)
5. Margin-rule brainstorm (when it is time for 4d).

## 12. Next steps (in order)

1. **Owner:** remove the two wrong faces (Carmela, Dev) via the Faces panel and report how it felt; review the mockups and
   send notes; commit (suggested message in the previous chat; see git state below).
2. **Claude:** draw the **create-album** and **"Is this you?"** screens.
3. **Claude:** build **Phase 4c** from the approved mockups: password sign-up, home (albums, create, join), invite link +
   code (new tables), account-level selfie with consent record (new table), the names step, claim a person, opt-out and
   data deletion, per-album recognition consent, guardian handling for children, abuse limits.
4. **Claude:** Phase 5 starting with `photo.clip_model`; then search (people, dates, filters, nicknames, CLIP, places).
5. Aging test and margin rule; real photo browser; hosting (profile the slow worker first), backups, monitoring, own SMTP.

**Owner to-do (outside the code):** turn off R2 public access; later remove the `10.0.0.7:8000` CORS entry; set up own email
sending in Supabase before inviting relatives; switch `.env` `MIGRATION_DATABASE_URL` to the session pooler (port 5432);
collect older solo photos of one person across years; keep adding look-alike/solo photos.

## 13. Known issues and risks

- Small evaluation set (about 100 certain faces); aging untested; fia and tiny faces are weak.
- Auto labels in group photos cannot be verified from folder names (about 23 of 43).
- Worker throughput is low and unexplained; no hosting plan tested yet.
- `/upload` page does not renew sign-in; Supabase's built-in email limit (429) blocked magic links once, and password
  sign-in reduces but does not remove the need for own SMTP.
- Stale `pending` uploads are never cleaned; neighbour-photo date inference not implemented; library deletion cleanup
  (R2 objects) not built; `delete_all` dev endpoint exists.
- The prototype search endpoints (`/api/search`, `/api/search-face`, `/api/debug/...`) still exist and are not product-grade.

## 14. Git state at the time of writing

- Committed: Phases 0-3 and "Add face recognition and people (Phase 4a-4b)" (`88ec216`).
- **Uncommitted (17 files):** accent/case-insensitive name matching (`app/services/people.py`, `tests/test_people.py`),
  invites default to editor (`app/api/libraries.py`, test), `docs/UI_SPEC.md` (new), doc and README updates,
  `.env.example`, `handoff.md` and `instruction_handoff.md` (new). Some are staged. The assistant never commits unless asked.

## 15. How the owner likes to work

- Plain language, short answers, step-by-step. Say what you did and exactly what they should do (commands in code blocks).
- Verify live before claiming done; be honest about what was not verified (for example, UI not seen in a browser).
- Mutation-check important tests; rehearse migrations on a restored backup; back up before live changes.
- Ask only for decisions that are genuinely theirs; otherwise recommend and proceed. Never commit or push unless asked.
- Never put secrets, tokens, passwords or personal emails in files or tool calls.

---

## 16. Session log (last 5)

**2026-11-23 (this handoff was first written)**
- Built and tested the people tools (API, regroup job, `/people` page), migration 0008 applied live after a backup and a
  rehearsal on a restored copy.
- Imported 330 test photos into a "Face test" album; the owner named about 20 people; checked labels against the folder
  answer key (auto 13/13, manual 75/77). Found and fixed four page/regroup bugs (section 7).
- Designed 12 phone screens as mockups, chose the name Memoria, settled sign-up (name only), per-album nicknames, the
  selfie flow, join link + code, and the filter sheet; added accent- and case-insensitive name matching.
- Recorded the CLIP semantic-search plan as Phase 5 tasks.
- **Next chat should first:** review the owner's notes on the mockups, then draw the create-album and "Is this you?" screens,
  then start Phase 4c.
