"""Link a pre-existing profile (created before sign-in existed) to a real Supabase account.

Why: the prototype's profile row has an arbitrary id. After you sign in for the first time,
Supabase creates a NEW user id. Everything that belonged to the old profile (your library
membership, uploads, albums) must move to that id, or you would sign in as a stranger to your
own data. This script re-points every foreign key in one transaction.

    python scripts/link_owner.py --email you@example.com            # dry run (rolls back)
    python scripts/link_owner.py --email you@example.com --apply    # do it

If you used the API before running this, a fresh profile for your account may already exist; it
is reused and takes over the old profile's handle and display name.

Connects with MIGRATION_DATABASE_URL (falling back to DATABASE_URL / postgres_url). Never prints
secrets.
"""
import argparse
import os
import sys
import uuid

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True, help="email you signed in with")
    ap.add_argument("--profile-id", help="old profile id (default: the only profile with no sign-in account)")
    ap.add_argument("--apply", action="store_true", help="commit the changes (default is a dry run)")
    args = ap.parse_args()

    url = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL") or os.getenv("postgres_url")
    if not url:
        print("Set MIGRATION_DATABASE_URL", file=sys.stderr)
        return 2
    engine = create_engine(url)

    with engine.connect() as conn:
        tx = conn.begin()
        try:
            users = conn.execute(text("SELECT id FROM auth.users WHERE lower(email) = lower(:e)"),
                                 {"e": args.email}).all()
            if len(users) != 1:
                print(f"Expected exactly one sign-in account for that email, found {len(users)}. "
                      "Sign in once first (magic link), then re-run.", file=sys.stderr)
                return 1
            new_id = users[0][0]

            if args.profile_id:
                old_id = uuid.UUID(args.profile_id)
            else:
                candidates = conn.execute(text(
                    "SELECT p.id FROM profile p WHERE NOT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = p.id)"
                )).all()
                if len(candidates) != 1:
                    print(f"Expected exactly one unlinked profile, found {len(candidates)}. "
                          "Pass --profile-id explicitly.", file=sys.stderr)
                    return 1
                old_id = candidates[0][0]

            if old_id == new_id:
                print("Already linked: that profile id is the sign-in account id. Nothing to do.")
                return 0

            old = conn.execute(text("SELECT handle, display_name, avatar_url FROM profile WHERE id = :i"),
                               {"i": old_id}).one_or_none()
            if old is None:
                print("Old profile not found.", file=sys.stderr)
                return 1

            exists = conn.execute(text("SELECT 1 FROM profile WHERE id = :i"), {"i": new_id}).scalar()
            if not exists:
                conn.execute(text("INSERT INTO profile (id, handle, display_name, avatar_url) "
                                  "VALUES (:i, :h, :n, :a)"),
                             {"i": new_id, "h": f"tmp-{uuid.uuid4().hex}", "n": old.display_name, "a": old.avatar_url})

            fks = conn.execute(text("""
                SELECT c.conrelid::regclass::text AS tbl, a.attname AS col
                FROM pg_constraint c
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                WHERE c.contype = 'f' AND c.confrelid = 'public.profile'::regclass
                  AND array_length(c.conkey, 1) = 1
                ORDER BY 1, 2""")).all()
            moved = {}
            for tbl, col in fks:
                res = conn.execute(text(f'UPDATE {tbl} SET "{col}" = :n WHERE "{col}" = :o'),
                                   {"n": new_id, "o": old_id})
                if res.rowcount:
                    moved[f"{tbl}.{col}"] = res.rowcount

            conn.execute(text("DELETE FROM profile WHERE id = :o"), {"o": old_id})
            conn.execute(text("UPDATE profile SET handle = :h, display_name = :n, avatar_url = :a WHERE id = :i"),
                         {"h": old.handle, "n": old.display_name, "a": old.avatar_url, "i": new_id})

            conn.execute(text("INSERT INTO audit_log (actor, action, target, meta) "
                              "VALUES (:a, 'profile.link_owner', :t, CAST(:m AS jsonb))"),
                         {"a": new_id, "t": str(old_id), "m": '{"script": "link_owner"}'})

            print(f"Linking profile {old_id} -> sign-in account {new_id}")
            for k, v in moved.items():
                print(f"  moved {v:>3} row(s) in {k}")
            if not moved:
                print("  (no rows referenced the old profile)")

            if args.apply:
                tx.commit()
                print("APPLIED.")
                try:
                    with engine.begin() as c2:
                        c2.execute(text("ALTER TABLE profile VALIDATE CONSTRAINT profile_id_auth_fkey"))
                    print("Validated profile -> auth.users foreign key.")
                except Exception as e:
                    print(f"Note: could not validate profile_id_auth_fkey ({type(e).__name__}); "
                          "other unlinked profiles may remain.")
            else:
                tx.rollback()
                print("DRY RUN: nothing was changed. Re-run with --apply to commit.")
            return 0
        except Exception:
            tx.rollback()
            raise


if __name__ == "__main__":
    sys.exit(main())
