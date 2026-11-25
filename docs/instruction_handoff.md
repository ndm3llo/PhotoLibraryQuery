# docs/instruction_handoff.md: how to keep handoff.md current

`handoff.md` is the project's memory between chats. This file says how to update it and how to start from it.

**The owner's two phrases**
- **To end a chat:** "Follow docs/instruction_handoff.md" (or "update the handoff"). Do Part A.
- **To start a new chat:** paste the prompt in Part B.

---

## Part A: updating handoff.md at the end of a chat

Do these in order. Do not skip the verification: handoff.md must be true, not remembered.

### 1. Read before writing
Read `handoff.md` in full, then skim `docs/ARCHITECTURE.md` and `docs/UI_SPEC.md` for anything this chat changed.

### 2. Verify the facts with commands (never from memory)
Run, and use the real output:
```bash
cd /Users/nicolas/photoLibraryQuery
git log --oneline | head -5
git status --short
source .venv/bin/activate && unset postgres_url DATABASE_URL MIGRATION_DATABASE_URL
alembic current            # if the direct host fails with a DNS error, say so; do not guess the version
ls migrations/versions app/api app/services app/web scripts docs
docker ps                  # tests need the test database; start Docker Desktop if it is not running
export TEST_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5433/postgres
pytest -q && ruff check .  # report the real counts
```
If something could not be verified (database unreachable, tests not run), write "not verified" next to that fact.

### 3. Go back through the conversation and collect
- **Decisions** the owner made (exact wording matters: record what they chose and why).
- **Things proposed but not confirmed** (mark them clearly as unconfirmed).
- **What was built or changed** (files, behaviour, tests).
- **Bugs found and how they were fixed** (and any still open).
- **Questions still open for the owner**, and anything they said they would do.
- **Environment gotchas** discovered.
- **Anything that changed the plan** or the order of work.

### 4. Edit handoff.md
Keep its section structure. Rules:
- **Update in place.** Replace stale facts (test count, migration number, git state, "state" tables). Do not append a second copy.
- **Decisions are cumulative.** Section 2 only grows; if a decision is reversed, mark the old one `(superseded <date>: ...)`
  rather than deleting it.
- **Rewrite** sections 3 (phases), 11 (open questions), 12 (next steps), 13 (known issues), 14 (git state) so they describe
  *now*. Remove questions that were answered, and move the answer into section 2.
- **Add a short dated entry** to a "Session log" section at the bottom (create it if missing): date, 5-10 bullets of what
  happened, and what the next chat should do first. Keep only the last 5 entries; fold older ones into sections 2 and 9.
- Update the **Last updated** line with the date and a one-line description of the session.
- **Be concrete:** file paths, commands, numbers, ids. Link to files instead of copying large content.
- **Keep it scannable:** headings, short bullets, tables for status. Aim for under about 400 lines; move detail into
  `docs/ARCHITECTURE.md` or `docs/UI_SPEC.md` and link to it.
- **Mark uncertainty:** "believed", "not verified", "proposed, not confirmed".
- **Never include secrets or personal data:** no passwords, tokens, API keys, `.env` values, or email addresses.
  (Public identifiers such as the album id of a test album are fine.)

### 5. Keep the other docs consistent
- Plan or checklist changes belong in `docs/ARCHITECTURE.md` (tick boxes, add tasks, record decisions).
- Screen and wording decisions belong in `docs/UI_SPEC.md`.
- If a new convention or gotcha will bite a future session, it belongs in handoff.md section 4.

### 6. Report to the owner (short)
- What you updated, in 3-5 bullets.
- Anything you could not verify.
- The first thing the next chat should do.
- A **suggested commit message** for uncommitted work. Do **not** commit or push unless they ask.

---

## Part B: starting a new chat

Paste this as the first message of a new chat:

> Read `handoff.md` in the photoLibraryQuery folder, then `docs/ARCHITECTURE.md` and `docs/UI_SPEC.md`. Reply in five
> lines or fewer: what the project is, where we are, what is unresolved, and what you suggest we do first. Don't re-ask
> anything the handoff already answers. Then wait for me.

### What the new Claude must do
1. Read `handoff.md` first, completely. It is the source of truth for decisions and state; the architecture doc is the plan.
2. Confirm the state before relying on it: `git status`, `alembic current`, and whether Docker is up (see handoff section 4).
3. Respect the owner's working style (handoff section 15): plain language, short, verify before claiming done, ask only
   for decisions that are genuinely theirs, never commit unless asked, never expose secrets.
4. Respect the decisions in handoff section 2. If something seems wrong, say so and ask; do not silently change it.
5. Treat anything marked unconfirmed or not verified as open.

### Optional: make this automatic
If the owner wants every new chat in this folder to read the handoff without being asked, add a `CLAUDE.md` in the project
root containing one line: `At the start of every session read handoff.md, and follow docs/instruction_handoff.md when asked to hand off.`
(Claude Code reads `CLAUDE.md` automatically.) It was not created by default.
