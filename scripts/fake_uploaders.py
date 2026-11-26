"""TEST DATA ONLY: pretend that some of the people in an album added its photos, to try the "Added by" filter.

    python scripts/fake_uploaders.py --library "Face test"            # show what would happen, change nothing
    python scripts/fake_uploaders.py --library "Face test" --apply
    python scripts/fake_uploaders.py --library "Face test" --undo     # remove the fake users, restore the uploader

For photos imported with `bulk_import.py --label-folders`, filenames start with the folder name ("gustavo__x.jpg").
Each folder is credited to the person with that name (or, if nobody has it, the person labelled most in those
photos). Each gets a fake account that joins as an editor. The accounts have no password and an address ending in
.invalid, which can never receive email, so nobody can sign in as them. The people themselves are not changed or
linked to the accounts. Back up first (scripts/backup_db.py).
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

DOMAIN = "@fake-uploader.invalid"
SKIP_FOLDERS = {"others"}  # strangers: these photos stay with whoever added them
# Columns the sign-in service expects to be empty text rather than missing, where they exist.
BLANK_COLUMNS = ("confirmation_token", "recovery_token", "email_change_token_new", "email_change",
                 "email_change_token_current", "phone_change", "phone_change_token", "reauthentication_token")


def find_library(s, wanted: str):
    rows = s.execute(text("SELECT id, name FROM library WHERE CAST(id AS text) = :w OR name = :w"), {"w": wanted}).all()
    if len(rows) != 1:
        raise SystemExit(f"expected exactly one album called {wanted!r}, found {len(rows)}")
    return rows[0]


def fake_members(s, library_id):
    return s.execute(text(
        "SELECT u.id, u.email, to_jsonb(u) -> 'raw_user_meta_data' AS meta, p.display_name FROM auth.users u "
        "JOIN profile p ON p.id = u.id JOIN library_member m ON m.profile_id = u.id "
        "WHERE m.library_id = :l AND u.email LIKE :d ORDER BY p.display_name"),
        {"l": library_id, "d": "%" + DOMAIN}).all()


def plan(s, library_id) -> list[dict]:
    """Which folder of photos goes to which person."""
    folders = s.execute(text(
        "SELECT split_part(original_filename, '__', 1) AS folder, count(*) AS photos FROM photo "
        "WHERE library_id = :l AND position('__' in original_filename) > 1 GROUP BY 1 ORDER BY 1"),
        {"l": library_id}).all()
    chosen = []
    for f in folders:
        if f.folder.lower() in SKIP_FOLDERS:
            continue
        person = s.execute(text(
            "SELECT id, display_name FROM person WHERE library_id = :l AND lower(display_name) = lower(:n) "
            "AND qualifier IS NULL"), {"l": library_id, "n": f.folder}).first()
        how = "same name as the folder"
        if person is None:
            person = s.execute(text(
                "SELECT x.id, x.display_name FROM face c JOIN photo p ON p.id = c.photo_id "
                "JOIN person x ON x.id = c.person_id WHERE p.library_id = :l AND c.person_source <> 'auto' "
                "AND split_part(p.original_filename, '__', 1) = :n "
                "GROUP BY x.id ORDER BY count(DISTINCT p.id) DESC, x.display_name LIMIT 1"),
                {"l": library_id, "n": f.folder}).first()
            how = "named most often in these photos"
        if person is not None:
            chosen.append({"folder": f.folder, "photos": f.photos, "person_id": person.id,
                           "name": person.display_name, "why": how})
    return chosen


def apply(s, library_id) -> list[dict]:
    if fake_members(s, library_id):
        raise SystemExit("this album already has fake uploaders; run --undo first")
    owner = s.execute(text("SELECT profile_id FROM library_member WHERE library_id = :l AND role = 'owner' "
                           "ORDER BY joined_at NULLS FIRST, profile_id LIMIT 1"), {"l": library_id}).scalar_one()
    columns = {r[0] for r in s.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'auth' AND table_name = 'users'"))}
    chosen = plan(s, library_id)
    users: dict = {}  # one account per person, even if two folders are theirs
    for entry in chosen:
        user_id = users.get(entry["person_id"])
        if user_id is None:
            user_id = users[entry["person_id"]] = uuid.uuid4()
            tag = user_id.hex[:8]
            values = {"id": user_id, "email": f"{tag}{DOMAIN}"}
            optional = {"instance_id": "00000000-0000-0000-0000-000000000000", "aud": "authenticated",
                        "role": "authenticated", "raw_app_meta_data": "{}",
                        "raw_user_meta_data": json.dumps({"name": entry["name"], "restore_to": str(owner),
                                                          "library": str(library_id)}),
                        **{c: "" for c in BLANK_COLUMNS}}
            values.update({k: v for k, v in optional.items() if k in columns})
            names = list(values)
            stamps = [c for c in ("created_at", "updated_at") if c in columns]
            s.execute(text(f"INSERT INTO auth.users ({', '.join(names + stamps)}) "
                           f"VALUES ({', '.join([':' + n for n in names] + ['now()'] * len(stamps))})"), values)
            s.execute(text("INSERT INTO profile (id, handle, display_name) VALUES (:i, :h, :n)"),
                      {"i": user_id, "h": f"test-uploader-{tag}", "n": entry["name"]})
            s.execute(text("INSERT INTO library_member (library_id, profile_id, role) VALUES (:l, :u, 'editor')"),
                      {"l": library_id, "u": user_id})
        s.execute(text("UPDATE photo SET uploader_id = :u WHERE library_id = :l AND uploader_id = :o "
                       "AND split_part(original_filename, '__', 1) = :f AND position('__' in original_filename) > 1"),
                  {"u": user_id, "o": owner, "l": library_id, "f": entry["folder"]})
    return chosen


def undo(s, library_id) -> int:
    members = fake_members(s, library_id)
    for m in members:
        meta = m.meta if isinstance(m.meta, dict) else json.loads(m.meta or "{}")
        restore_to = meta.get("restore_to") or s.execute(text(
            "SELECT profile_id FROM library_member WHERE library_id = :l AND role = 'owner' AND profile_id <> :u "
            "LIMIT 1"), {"l": library_id, "u": m.id}).scalar_one()
        s.execute(text("UPDATE photo SET uploader_id = :o WHERE uploader_id = :u"), {"o": restore_to, "u": m.id})
        s.execute(text("DELETE FROM library_member WHERE profile_id = :u"), {"u": m.id})
        s.execute(text("DELETE FROM profile WHERE id = :u"), {"u": m.id})
        s.execute(text("DELETE FROM auth.users WHERE id = :u AND email LIKE :d"), {"u": m.id, "d": "%" + DOMAIN})
    return len(members)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", required=True, help="album name or id")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--undo", action="store_true")
    args = ap.parse_args()
    from app.db.rls import service_session
    with service_session() as s:  # one transaction: everything happens, or nothing does
        library = find_library(s, args.library)
        if args.undo:
            print(f"removed {undo(s, library.id)} fake uploaders from {library.name!r}")
            return
        chosen = apply(s, library.id) if args.apply else plan(s, library.id)
    print(f"{'Done' if args.apply else 'Would do'} in {library.name!r}:")
    for e in chosen:
        print(f"  {e['photos']:4} photos from folder {e['folder']!r} -> {e['name']}  ({e['why']})")
    if not args.apply:
        print("nothing was changed; add --apply")


if __name__ == "__main__":
    main()
