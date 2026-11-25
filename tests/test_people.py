"""Matching and clustering rules (pure) and how they are applied to a library (database)."""
import uuid

import numpy as np
import pytest
from sqlalchemy import text

from app.services import people

MODEL = "test-model"
rng = np.random.default_rng(7)


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def person_vec():
    return unit(rng.normal(size=512))


def near(base, dist):
    """A unit vector at (approximately exactly) cosine distance `dist` from base."""
    noise = rng.normal(size=512).astype(np.float32)
    noise -= (noise @ base) * base
    noise = unit(noise)
    cos = 1.0 - dist
    return unit(cos * base + np.sqrt(1 - cos**2) * noise)


A, B = uuid.uuid4(), uuid.uuid4()
VA, VB = person_vec(), person_vec()


# ----------------------------------------------------------------------------- match_face

def test_a_close_good_quality_face_is_matched_automatically():
    m = people.match_face(near(VA, 0.30), 0.9, [(A, VA), (B, VB)])
    assert m.person_id == A and m.tier == "auto" and m.distance == pytest.approx(0.30, abs=0.01)


def test_between_the_cutoffs_it_is_only_a_suggestion():
    m = people.match_face(near(VA, 0.55), 0.9, [(A, VA)])
    assert m.person_id == A and m.tier == "suggest"


def test_beyond_the_suggest_cutoff_there_is_no_match():
    assert people.match_face(near(VA, 0.75), 0.9, [(A, VA)]) is None


def test_a_low_quality_face_is_never_applied_automatically():
    assert people.match_face(near(VA, 0.20), 0.3, [(A, VA)]).tier == "suggest"


def test_a_persons_best_face_counts_not_the_average():
    far, close = near(VA, 0.58), near(VA, 0.25)
    m = people.match_face(near(close, 0.28), 0.9, [(A, far), (A, close), (A, near(VA, 0.7))])
    assert m.person_id == A and m.tier == "auto"


def test_the_identical_image_is_not_evidence():
    assert people.match_face(VA, 0.9, [(A, VA)]) is None


def test_excluded_people_are_never_returned():
    q = near(VA, 0.2)
    assert people.match_face(q, 0.9, [(A, VA)], excluded=frozenset({A})) is None
    m = people.match_face(q, 0.9, [(A, VA), (B, near(VA, 0.5))], excluded=frozenset({A}))
    assert m.person_id == B and m.tier == "suggest"


def test_runner_up_is_reported_so_a_margin_rule_can_be_added():
    q = near(VA, 0.30)
    m = people.match_face(q, 0.9, [(A, VA), (B, near(VA, 0.34))])
    assert m.person_id == A and m.runner_up_distance is not None and m.runner_up_distance > m.distance
    assert people.match_face(q, 0.9, [(A, VA)]).runner_up_distance is None


def test_empty_gallery_matches_nobody():
    assert people.match_face(VA, 0.9, []) is None


# ---------------------------------------------------------------------------- cluster_faces

def test_clusters_separate_people_and_come_back_largest_first():
    embs = [near(VA, 0.2) for _ in range(5)] + [near(VB, 0.2) for _ in range(3)] + [person_vec()]
    clusters = people.cluster_faces(embs)
    assert [len(c) for c in clusters] == [5, 3, 1]
    assert set(clusters[0]) == set(range(5)) and set(clusters[1]) == set(range(5, 8))


def test_clustering_does_not_chain_two_people_through_a_lookalike():
    """A chain A - m - B where each step is close must not merge A with B (single linkage would)."""
    base = person_vec()
    mid = near(base, 0.4)
    other = near(mid, 0.4)
    assert 1 - float(base @ other) > 0.60, "fixture: the ends really are different people"
    clusters = people.cluster_faces([base, mid, other], max_distance=0.45)
    assert not any(0 in c and 2 in c for c in clusters)


def test_clustering_is_deterministic_and_handles_empty_and_single():
    assert people.cluster_faces([]) == []
    assert people.cluster_faces([VA]) == [[0]]
    embs = [near(VA, 0.2) for _ in range(6)]
    assert people.cluster_faces(embs, order=[0.9] * 6) == people.cluster_faces(embs, order=[0.9] * 6)


# ---------------------------------------------------------------------------- database

@pytest.fixture
def lib(app_db):
    """One library with two named people (Ann: 2 confirmed faces, Bo: none yet) and a pile of unlabeled faces."""
    import app.db.rls as rls

    owner, lid = uuid.uuid4(), uuid.uuid4()
    ann, bo = uuid.uuid4(), uuid.uuid4()
    eng = app_db.engine()
    with eng.begin() as c:
        c.execute(text("INSERT INTO auth.users (id, email) VALUES (:i, :e)"), {"i": owner, "e": f"{owner}@t.dev"})
        c.execute(text("INSERT INTO profile (id, display_name, handle) VALUES (:i,'o',:h)"), {"i": owner, "h": str(owner)})
        c.execute(text("INSERT INTO library (id, name, created_by) VALUES (:l,'L',:o)"), {"l": lid, "o": owner})
        c.execute(text("INSERT INTO person (id, library_id, display_name) VALUES (:a,:l,'Ann'),(:b,:l,'Bo')"),
                  {"a": ann, "b": bo, "l": lid})
    eng.dispose()

    class L:
        pass

    L.id, L.owner, L.ann, L.bo, L.rls, L.eng = lid, owner, ann, bo, rls, app_db.engine()

    def add_face(vec, source=None, person=None, quality=0.9):
        fid, pid = uuid.uuid4(), uuid.uuid4()
        with L.eng.begin() as c:
            c.execute(text("INSERT INTO photo (id, library_id, uploader_id, status) VALUES (:p,:l,:o,'ready')"),
                      {"p": pid, "l": lid, "o": owner})
            c.execute(text("""INSERT INTO face (id, photo_id, library_id, embedding, model_version, quality,
                                                person_id, person_source)
                              VALUES (:f,:p,:l,:e,:m,:q,:pe,:s)"""),
                      {"f": fid, "p": pid, "l": lid, "e": "[" + ",".join(map(str, vec.tolist())) + "]",
                       "m": MODEL, "q": quality, "pe": person, "s": source})
        return fid

    L.add_face = staticmethod(add_face)
    L.row = staticmethod(lambda fid: L.eng.connect().execute(
        text("SELECT person_id, person_source, cluster_id FROM face WHERE id = :f"), {"f": fid}).one())
    yield L
    L.eng.dispose()


def test_assign_labels_only_good_matches_and_marks_them_auto(lib):
    lib.add_face(VA, "confirmed", lib.ann)
    lib.add_face(near(VA, 0.25), "confirmed", lib.ann)
    good = lib.add_face(near(VA, 0.28), None)
    maybe = lib.add_face(near(VA, 0.55), None)
    stranger = lib.add_face(person_vec(), None)
    with lib.rls.service_session() as s:
        out = people.assign_library_faces(s, lib.id, MODEL)
    assert out == {"considered": 3, "labeled": 1}
    assert lib.row(good)[:2] == (lib.ann, "auto")
    assert lib.row(maybe)[0] is None and lib.row(stranger)[0] is None, "suggestions are never stored"


def test_ground_truth_is_never_overwritten_by_a_later_run(lib):
    lib.add_face(VA, "confirmed", lib.ann)
    mine = lib.add_face(near(VA, 0.25), "manual", lib.bo)  # a person said this is Bo, even though it looks like Ann
    with lib.rls.service_session() as s:
        people.assign_library_faces(s, lib.id, MODEL)
    assert lib.row(mine)[:2] == (lib.bo, "manual")


def test_auto_labels_are_recomputed_and_dropped_when_the_evidence_goes(lib):
    anchor = lib.add_face(VA, "confirmed", lib.ann)
    f = lib.add_face(near(VA, 0.28), None)
    with lib.rls.service_session() as s:
        people.assign_library_faces(s, lib.id, MODEL)
    assert lib.row(f)[0] == lib.ann
    with lib.eng.begin() as c:
        c.execute(text("UPDATE face SET person_id = NULL, person_source = NULL WHERE id = :f"), {"f": anchor})
    with lib.rls.service_session() as s:
        people.assign_library_faces(s, lib.id, MODEL)
    assert lib.row(f)[0] is None, "auto labels must not outlive the confirmed face that justified them"


def test_automatic_labels_do_not_become_evidence_for_more_automatic_labels(lib):
    lib.add_face(VA, "confirmed", lib.ann)
    lib.add_face(near(VA, 0.40), None)                      # matches Ann at 0.40
    chain = lib.add_face(near(VA, 0.40), None)              # about 0.8 from the anchor: no chain through the auto one
    with lib.rls.service_session() as s:
        people.assign_library_faces(s, lib.id, MODEL)
    assert lib.row(chain)[0] in (None, lib.ann)
    with lib.rls.service_session() as s:
        n_before = s.execute(text("SELECT count(*) FROM face WHERE person_source='auto' AND library_id=:l"),
                             {"l": lib.id}).scalar()
        people.assign_library_faces(s, lib.id, MODEL)
        n_after = s.execute(text("SELECT count(*) FROM face WHERE person_source='auto' AND library_id=:l"),
                            {"l": lib.id}).scalar()
    assert n_before == n_after, "running twice must be stable"


def test_opted_out_people_are_never_matched(lib):
    lib.add_face(VA, "confirmed", lib.ann)
    f = lib.add_face(near(VA, 0.25), None)
    with lib.eng.begin() as c:
        c.execute(text("UPDATE person SET recognition_opt_out = true WHERE id = :a"), {"a": lib.ann})
    with lib.rls.service_session() as s:
        assert people.assign_library_faces(s, lib.id, MODEL)["labeled"] == 0
    assert lib.row(f)[0] is None


def test_a_rejected_person_is_not_offered_for_that_face_again(lib):
    lib.add_face(VA, "confirmed", lib.ann)
    f = lib.add_face(near(VA, 0.25), None)
    with lib.eng.begin() as c:
        c.execute(text("INSERT INTO face_rejection (face_id, person_id, library_id) VALUES (:f,:p,:l)"),
                  {"f": f, "p": lib.ann, "l": lib.id})
    with lib.rls.service_session() as s:
        assert people.assign_library_faces(s, lib.id, MODEL)["labeled"] == 0


def test_a_selfie_reference_lets_a_person_be_matched_with_no_labeled_photos(lib):
    with lib.eng.begin() as c:
        c.execute(text("""INSERT INTO person_reference (library_id, person_id, embedding, model_version, source)
                          VALUES (:l,:p,:e,:m,'selfie')"""),
                  {"l": lib.id, "p": lib.bo, "e": "[" + ",".join(map(str, VB.tolist())) + "]", "m": MODEL})
    f = lib.add_face(near(VB, 0.3), None)
    with lib.rls.service_session() as s:
        people.assign_library_faces(s, lib.id, MODEL)
    assert lib.row(f)[:2] == (lib.bo, "auto")


def test_only_the_same_model_is_compared(lib):
    lib.add_face(VA, "confirmed", lib.ann)
    f = lib.add_face(near(VA, 0.25), None)
    with lib.rls.service_session() as s:
        assert people.assign_library_faces(s, lib.id, "some-other-model")["considered"] == 0
    assert lib.row(f)[0] is None


def test_recluster_groups_unlabeled_faces_and_keeps_named_clusters(lib):
    named = lib.add_face(near(VB, 0.1), None)
    with lib.rls.service_session() as s:
        people.recluster_library(s, lib.id, MODEL)
    cid = lib.row(named)[2]
    with lib.eng.begin() as c:  # a person names this cluster "Bo"
        c.execute(text("UPDATE face_cluster SET person_id = :p WHERE id = :c"), {"p": lib.bo, "c": cid})
        c.execute(text("UPDATE face SET person_id = :p, person_source = 'manual' WHERE id = :f"),
                  {"p": lib.bo, "f": named})
    group = [lib.add_face(near(VA, 0.15), None) for _ in range(4)]
    lone = lib.add_face(person_vec(), None)
    with lib.rls.service_session() as s:
        out = people.recluster_library(s, lib.id, MODEL)
        s.flush()
    assert out == {"faces": 5, "clusters": 2}
    assert len({lib.row(f)[2] for f in group}) == 1
    assert lib.row(group[0])[2] != lib.row(lone)[2]
    assert lib.row(named)[2] == cid, "a named cluster is never rebuilt"
    with lib.rls.service_session() as s:
        assert s.execute(text("SELECT person_id FROM face_cluster WHERE id = :c"), {"c": cid}).scalar() == lib.bo


def test_recluster_twice_does_not_pile_up_clusters(lib):
    for _ in range(3):
        lib.add_face(near(VA, 0.15), None)
    with lib.rls.service_session() as s:
        people.recluster_library(s, lib.id, MODEL)
        out = people.recluster_library(s, lib.id, MODEL)
        n = s.execute(text("SELECT count(*) FROM face_cluster WHERE library_id = :l"), {"l": lib.id}).scalar()
    assert out == {"faces": 3, "clusters": 1} and n == 1


# ------------------------------------------------------------------------------- type-ahead

def _c(name, qualifier=None, aliases=()):
    return {"id": uuid.uuid4(), "display_name": name, "qualifier": qualifier, "aliases": list(aliases)}


def test_type_ahead_ranks_exact_then_prefix_then_nickname_then_word():
    cands = [_c("Rosa Ortiz"), _c("Sarah"), _c("Sarah", "cousin"), _c("Ben", aliases=["Sari"]),
             _c("Ann", "wife of Sarah")]
    out = people.rank_names("sarah", cands)
    assert [(c["display_name"], c["qualifier"]) for c in out] == [("Sarah", None), ("Sarah", "cousin"), ("Ann", "wife of Sarah")]
    out = people.rank_names("sa", cands)
    assert [c["display_name"] for c in out] == ["Sarah", "Sarah", "Ben", "Ann"]
    assert out[2]["via_alias"] == "Sari" and out[0]["via_alias"] is None


def test_type_ahead_ignores_case_and_spacing_and_empty_text():
    cands = [_c("Rosa Ortiz")]
    assert people.rank_names("  ROSA  ", cands)
    assert people.rank_names("ortiz", cands), "a later word of the name matches too"
    assert people.rank_names("", cands) == [] and people.rank_names("   ", cands) == []
    assert people.rank_names("zzz", cands) == []


def test_type_ahead_is_limited_and_does_not_treat_input_as_a_pattern():
    cands = [_c(f"Sam {i}") for i in range(20)]
    assert len(people.rank_names("sam", cands, limit=5)) == 5
    assert people.rank_names("%", cands) == [] and people.rank_names(".*", cands) == []


def test_group_ids_survive_a_regroup_and_new_faces_join_the_existing_group(lib):
    first = [lib.add_face(near(VA, 0.15)) for _ in range(3)]
    other = [lib.add_face(near(VB, 0.15)) for _ in range(2)]
    with lib.rls.service_session() as s:
        people.recluster_library(s, lib.id, MODEL)
    ids_before = (lib.row(first[0])[2], lib.row(other[0])[2])
    assert ids_before[0] != ids_before[1]
    newcomer = lib.add_face(near(VA, 0.15))
    with lib.rls.service_session() as s:
        people.recluster_library(s, lib.id, MODEL)
        people.recluster_library(s, lib.id, MODEL)
    assert (lib.row(first[0])[2], lib.row(other[0])[2]) == ids_before, "group ids must not change on a regroup"
    assert lib.row(newcomer)[2] == ids_before[0]
    assert {lib.row(f)[2] for f in first} == {ids_before[0]}


def test_a_group_that_vanishes_is_deleted_and_an_auto_labeled_face_leaves_its_group(lib):
    lib.add_face(VA, "confirmed", lib.ann)
    a = [lib.add_face(near(VA, 0.25)) for _ in range(2)]
    with lib.rls.service_session() as s:
        people.recluster_library(s, lib.id, MODEL)
    assert lib.row(a[0])[2] is not None
    with lib.rls.service_session() as s:
        people.assign_library_faces(s, lib.id, MODEL)      # both now match Ann automatically
        people.recluster_library(s, lib.id, MODEL)
        n = s.execute(text("SELECT count(*) FROM face_cluster WHERE library_id = :l"), {"l": lib.id}).scalar()
    assert n == 0 and lib.row(a[0])[2] is None and lib.row(a[0])[:2] == (lib.ann, "auto")


def test_type_ahead_ignores_capitals_accents_and_spacing_in_the_query_and_the_names():
    cands = [_c("María José"), _c("Zoë", aliases=["Abuela"]), _c("STRASSE"), _c("Maya", "Blonde")]
    for query in ("maria", "MARIA", "  María  ", "mArÍa jose", "jose"):
        assert [c["display_name"] for c in people.rank_names(query, cands)] == ["María José"], query
    assert [c["display_name"] for c in people.rank_names("zoe", cands)] == ["Zoë"]
    assert people.rank_names("ABUELA", cands)[0]["via_alias"] == "Abuela", "the stored nickname keeps its spelling"
    assert [c["display_name"] for c in people.rank_names("strasse", cands)] == ["STRASSE"]
    assert [c["display_name"] for c in people.rank_names("MAYA BLONDE", cands)] == ["Maya"]


def test_fold_is_stable_for_already_plain_text_and_keeps_non_latin_letters():
    assert people.fold("Mimi") == "mimi" and people.fold("  A  B ") == "a b"
    assert people.fold("Ñandú") == "nandu" and people.fold("Ana") == people.fold("ANA")
    assert people.fold("李明") == "李明", "names in other scripts must not be mangled"
