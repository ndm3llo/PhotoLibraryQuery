"""The test-data script that credits photos to fake accounts, and takes them away again."""
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fake_uploaders.py"


@pytest.fixture(scope="module")
def tool(app_db):
    spec = importlib.util.spec_from_file_location("fake_uploaders", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(s):
    owner, lib, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    s.execute(text("INSERT INTO auth.users (id, email) VALUES (:i, :e)"), {"i": owner, "e": f"{owner}@test.dev"})
    s.execute(text("INSERT INTO profile (id, display_name, handle) VALUES (:i, 'Owner', :h)"),
              {"i": owner, "h": owner.hex})
    for library in (lib, other):
        s.execute(text("INSERT INTO library (id, name, created_by) VALUES (:l, :n, :o)"),
                  {"l": library, "n": f"fake-{library.hex}", "o": owner})
        s.execute(text("INSERT INTO library_member (library_id, profile_id, role) VALUES (:l, :o, 'owner')"),
                  {"l": library, "o": owner})
    people = {name: uuid.uuid4() for name in ("Gus", "Ranjan")}
    for name, pid in people.items():
        s.execute(text("INSERT INTO person (id, library_id, display_name) VALUES (:i, :l, :n)"),
                  {"i": pid, "l": lib, "n": name})
    vec = "[" + ",".join(["0.1"] * 512) + "]"
    for library, filename, person in [(lib, "gus__1.jpg", None), (lib, "gus__2.jpg", None),
                                      (lib, "dad__1.jpg", "Ranjan"), (lib, "others__1.jpg", None),
                                      (lib, "plain.jpg", None), (other, "gus__9.jpg", None)]:
        photo = uuid.uuid4()
        s.execute(text("INSERT INTO photo (id, library_id, uploader_id, status, original_filename) "
                       "VALUES (:i, :l, :o, 'ready', :f)"), {"i": photo, "l": library, "o": owner, "f": filename})
        if person:
            s.execute(text("INSERT INTO face (id, photo_id, library_id, embedding, person_id, person_source) "
                           f"VALUES (gen_random_uuid(), :p, :l, '{vec}', :x, 'manual')"),
                      {"p": photo, "l": library, "x": people[person]})
    return owner, lib, other


def uploaders(s, library):
    return dict(s.execute(text("SELECT p.original_filename, x.display_name FROM photo p JOIN profile x "
                               "ON x.id = p.uploader_id WHERE p.library_id = :l"), {"l": library}).all())


def test_apply_then_undo_leaves_everything_as_it_was(tool):
    from app.db.rls import service_session
    with service_session() as s:
        owner, lib, other = build(s)
    with service_session() as s:
        before = uploaders(s, lib)
        assert [(e["folder"], e["name"]) for e in tool.plan(s, lib)] == [("dad", "Ranjan"), ("gus", "Gus")]
        assert uploaders(s, lib) == before, "planning changes nothing"
        tool.apply(s, lib)
    with service_session() as s:
        assert uploaders(s, lib) == {"gus__1.jpg": "Gus", "gus__2.jpg": "Gus", "dad__1.jpg": "Ranjan",
                                     "others__1.jpg": "Owner", "plain.jpg": "Owner"}
        assert uploaders(s, other) == {"gus__9.jpg": "Owner"}, "other albums are untouched"
        made = tool.fake_members(s, lib)
        assert len(made) == 2 and all(m.email.endswith(".invalid") for m in made)
        with pytest.raises(SystemExit):
            tool.apply(s, lib)
    with service_session() as s:
        assert tool.undo(s, lib) == 2
    with service_session() as s:
        assert uploaders(s, lib) == before
        assert tool.fake_members(s, lib) == []
        ids = [str(m.id) for m in made]
        for table in ("auth.users", "profile"):
            assert s.execute(text(f"SELECT count(*) FROM {table} WHERE CAST(id AS text) = ANY(:i)"),
                             {"i": ids}).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM library_member WHERE profile_id = :o"), {"o": owner}).scalar() == 2
