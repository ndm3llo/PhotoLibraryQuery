"""Filtering an album by people, dates and who added the photos (real HTTP routes, real Postgres)."""
import uuid

import pytest
from apiutil import User, build_ctx, join, make_library
from sqlalchemy import text

VEC = "[" + ",".join(["0.1"] * 512) + "]"


@pytest.fixture(scope="module")
def ctx(app_db, storage):
    mp = pytest.MonkeyPatch()
    from app.api import libraries, photos
    yield build_ctx(mp, [(libraries.router, "/api/libraries"), (libraries.invites_router, "/api/invites"),
                         (photos.router, "/api/photos")])
    mp.undo()


class Album:
    """Photos:  both  (Mia + Leo, 2019-07-04, owner)    mia   (Mia twice, 2020-07-04, helper)
                leo   (Leo, 31 Dec 2019 late evening in UTC-5, owner)    nobody (no date, owner)
                busy  (Mia + Leo, still processing)"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.owner, self.helper, self.viewer = (User(ctx, x) for x in ("owner", "helper", "viewer"))
        self.id = make_library(self.owner)
        join(self.owner, self.id, self.helper, "editor")
        join(self.owner, self.id, self.viewer, "viewer")
        self.mia, self.leo = self.person("Mia"), self.person("Leo", "cousin")
        self.both = self.photo(self.owner, "2019-07-04 12:00+00")
        self.mia_only = self.photo(self.helper, "2020-07-04 09:00+00")
        self.leo_only = self.photo(self.owner, "2020-01-01 02:30+00", tz="-05:00")
        self.nobody = self.photo(self.owner, None)
        self.busy = self.photo(self.owner, "2019-07-04 12:00+00", status="processing")
        self.face(self.both, self.mia, size=40)
        self.face(self.both, self.leo)
        self.face(self.mia_only, self.mia, size=300)
        self.face(self.mia_only, self.mia, size=100)
        self.face(self.leo_only, self.leo)
        self.face(self.nobody, None)
        self.face(self.busy, self.mia)
        self.face(self.busy, self.leo)

    def sql(self, statement, **params):
        with self.ctx.engine.begin() as c:
            c.execute(text(statement), params)

    def person(self, name, qualifier=None):
        pid = uuid.uuid4()
        self.sql("INSERT INTO person (id, library_id, display_name, qualifier) VALUES (:i, :l, :n, :q)",
                 i=pid, l=self.id, n=name, q=qualifier)
        return str(pid)

    def photo(self, uploader, taken, tz=None, status="ready"):
        pid = uuid.uuid4()
        self.sql("INSERT INTO photo (id, library_id, uploader_id, status, taken_at, taken_tz, width, height, thumb_key)"
                 " VALUES (:i, :l, :u, :s, CAST(:t AS timestamptz), :z, 1000, 800, :k)",
                 i=pid, l=self.id, u=uploader.id, s=status, t=taken, z=tz, k=f"{self.id}/thumbs/{pid}.webp")
        return str(pid)

    def face(self, photo, person, size=100):
        self.sql("INSERT INTO face (id, photo_id, library_id, embedding, bbox, person_id, person_source) "
                 f"VALUES (:i, :p, :l, '{VEC}', ARRAY[10, 10, 10 + :s, 10 + :s], CAST(:x AS uuid), "
                 "CASE WHEN CAST(:x AS uuid) IS NULL THEN NULL ELSE 'manual' END)",
                 i=uuid.uuid4(), p=photo, l=self.id, s=size, x=person)

    def find(self, user=None, **params):
        """The matching photo ids, after checking the count endpoint agrees."""
        user = user or self.owner
        query = {"library_id": self.id, **params}
        listed = user.call("GET", "/api/photos", params=query)
        counted = user.call("GET", "/api/photos/count", params=query)
        assert listed.status_code == counted.status_code == 200, listed.text + counted.text
        ids = {p["id"] for p in listed.json()["items"]}
        if params:
            assert counted.json()["count"] == len(ids)
        return ids


@pytest.fixture(scope="module")
def album(ctx):
    return Album(ctx)


def test_without_filters_everything_shows_including_photos_still_processing(album):
    a = album
    assert a.find() == {a.both, a.mia_only, a.leo_only, a.nobody, a.busy}


def test_one_person(album):
    a = album
    assert a.find(person=a.mia) == {a.both, a.mia_only}, "ready photos only, matching the count"


def test_together_means_everyone_is_in_the_same_photo(album):
    a = album
    assert a.find(person=[a.mia, a.leo]) == {a.both}
    assert a.find(person=[a.mia, a.leo], match="all") == {a.both}
    assert a.find(person=[a.mia, a.mia]) == {a.both, a.mia_only}, "choosing the same person twice changes nothing"


def test_any_of_them(album):
    a = album
    assert a.find(person=[a.mia, a.leo], match="any") == {a.both, a.mia_only, a.leo_only}


def test_dates_use_the_local_day_where_the_photo_was_taken(album):
    a = album
    assert a.find(year=2019) == {a.both, a.leo_only}, "9:30 pm on New Year's Eve is still 2019"
    assert a.find(year=2020) == {a.mia_only}
    assert a.find(date_from="2019-12-31", date_to="2019-12-31") == {a.leo_only}
    assert a.find(date_from="2020-01-01") == {a.mia_only}
    assert a.find(date_to="2019-07-04") == {a.both}
    assert a.find(on_day="07-04") == {a.both, a.mia_only}
    assert a.find(undated=True) == {a.nobody}


def test_added_by_and_combinations(album):
    a = album
    assert a.find(uploader=str(a.helper.id)) == {a.mia_only}
    assert a.find(uploader=[str(a.helper.id), str(a.owner.id)], person=a.mia) == {a.both, a.mia_only}
    assert a.find(person=a.mia, year=2019) == {a.both}
    assert a.find(person=a.mia, year=2019, uploader=str(a.helper.id)) == set()


def test_filters_survive_pagination(album):
    a = album
    query = {"library_id": a.id, "person": [a.mia, a.leo], "match": "any", "limit": 1}
    seen, cursor = [], None
    for _ in range(5):
        page = a.owner.call("GET", "/api/photos", params={**query, **({"cursor": cursor} if cursor else {})}).json()
        seen += [p["id"] for p in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [a.mia_only, a.leo_only, a.both], "newest first, each once"


@pytest.mark.parametrize("params", [{"on_day": "13-01"}, {"on_day": "7-4"}, {"year": 12}, {"match": "some"},
                                    {"person": "nobody"}, {"date_from": "yesterday"}])
def test_nonsense_is_rejected(album, params):
    for path in ("/api/photos", "/api/photos/count"):
        r = album.owner.call("GET", path, params={"library_id": album.id, **params})
        assert r.status_code == 422, f"{path} {params} -> {r.status_code}"


def test_choices_list_people_years_and_uploaders(album):
    a = album
    r = a.viewer.call("GET", "/api/photos/filters", params={"library_id": a.id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(p["display_name"], p["qualifier"], p["photos"]) for p in body["people"]] == \
        [("Leo", "cousin", 2), ("Mia", None, 2)]
    mia = body["people"][1]["face"]
    assert mia["bbox"] == [10, 10, 310, 310] and a.mia_only in mia["thumbnail_url"], "her largest face"
    assert body["years"] == [{"year": 2020, "photos": 1}, {"year": 2019, "photos": 2}]
    assert body["undated"] == 1
    uploaders = {u["id"]: (u["photos"], u["is_me"]) for u in body["uploaders"]}
    assert uploaders == {str(a.owner.id): (3, False), str(a.helper.id): (1, False)}
    mine = a.helper.call("GET", "/api/photos/filters", params={"library_id": a.id}).json()["uploaders"]
    assert {u["id"] for u in mine if u["is_me"]} == {str(a.helper.id)}


def test_opted_out_people_are_not_offered(ctx):
    a = Album(ctx)
    a.sql("UPDATE person SET recognition_opt_out = true WHERE id = :p", p=a.mia)
    body = a.owner.call("GET", "/api/photos/filters", params={"library_id": a.id}).json()
    assert [p["display_name"] for p in body["people"]] == ["Leo"]


def test_someone_who_left_still_has_their_photos_listed(ctx):
    a = Album(ctx)
    assert a.helper.call("DELETE", f"/api/libraries/{a.id}/members/{a.helper.id}").status_code == 204
    body = a.owner.call("GET", "/api/photos/filters", params={"library_id": a.id}).json()
    assert "Someone who left" in [u["name"] for u in body["uploaders"]]


def test_outsiders_learn_nothing(ctx, album):
    outsider = User(ctx, "outsider")
    own = make_library(outsider)
    for path in ("/api/photos", "/api/photos/count", "/api/photos/filters"):
        assert outsider.call("GET", path, params={"library_id": album.id}).status_code == 404
    # Someone else's person id, tried against your own album or without naming one, finds nothing.
    for params in ({"library_id": own, "person": album.mia}, {"person": album.mia}):
        assert outsider.call("GET", "/api/photos", params=params).json()["items"] == []
    assert outsider.call("GET", "/api/photos/count",
                         params={"library_id": own, "person": album.mia}).json() == {"count": 0}
    assert ctx.client.get("/api/photos/filters", params={"library_id": album.id}).status_code == 401


@pytest.mark.parametrize("source,dated", [("exif_original", True), ("exif_digitized", True), ("filename", True),
                                          ("manual", True), ("file_mtime", False), ("legacy", False)])
def test_a_date_taken_from_the_file_itself_counts_as_no_date(ctx, source, dated):
    a = Album(ctx)
    a.sql("UPDATE photo SET taken_at_source = :s WHERE id = :p", s=source, p=a.mia_only)
    assert a.find(year=2020) == ({a.mia_only} if dated else set())
    assert a.find(undated=True) == ({a.nobody} if dated else {a.nobody, a.mia_only})
    assert (a.mia_only in a.find(on_day="07-04")) is dated
    choices = a.owner.call("GET", "/api/photos/filters", params={"library_id": a.id}).json()
    assert choices["undated"] == (1 if dated else 2)
    assert (2020 in [y["year"] for y in choices["years"]]) is dated
    listed = a.owner.call("GET", "/api/photos", params={"library_id": a.id}).json()["items"]
    item = next(p for p in listed if p["id"] == a.mia_only)
    assert (item["taken_local"] is not None) is dated and item["taken_at"], "the stored value is kept"


def test_undated_photos_come_after_dated_ones_across_pages(ctx):
    a = Album(ctx)
    a.sql("UPDATE photo SET taken_at = now(), taken_at_source = 'file_mtime' WHERE id = :p", p=a.mia_only)
    seen, cursor = [], None
    for _ in range(10):
        query = {"library_id": a.id, "limit": 1, "status": "ready", **({"cursor": cursor} if cursor else {})}
        page = a.owner.call("GET", "/api/photos", params=query).json()
        seen += [p["id"] for p in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen[:2] == [a.leo_only, a.both], "dated, newest first"
    assert sorted(seen[2:]) == sorted([a.mia_only, a.nobody]) and len(seen) == 4, "then undated, each once"


# ----------------------------------------------------------------------------- the viewer

def details(user, photo):
    return user.call("GET", f"/api/photos/{photo}/details")


def test_the_list_carries_a_signed_link_to_the_sharp_copy(ctx):
    a = Album(ctx)
    a.sql("UPDATE photo SET display_key = :k WHERE id = :p", k=f"{a.id}/display/{a.both}.webp", p=a.both)
    items = {p["id"]: p for p in a.viewer.call("GET", "/api/photos", params={"library_id": a.id}).json()["items"]}
    url = items[a.both]["display_url"]
    assert f"{a.id}/display/{a.both}.webp" in url and "Signature" in url
    assert items[a.mia_only]["display_url"] is None, "no sharp copy, no link"


def test_details_name_each_person_once_from_left_to_right(ctx):
    a = Album(ctx)
    a.sql("UPDATE face SET bbox = ARRAY[500, 10, 600, 110] WHERE photo_id = :p AND person_id = :x",
          p=a.both, x=a.mia)
    body = details(a.viewer, a.both).json()
    assert [(p["display_name"], p["qualifier"], p["bbox"][0]) for p in body["people"]] == \
        [("Leo", "cousin", 10), ("Mia", None, 500)]
    assert (body["width"], body["height"]) == (1000, 800)
    twice = details(a.owner, a.mia_only).json()["people"]
    assert [(p["display_name"], p["bbox"]) for p in twice] == [("Mia", [10, 10, 310, 310])], "her larger face"
    assert details(a.owner, a.nobody).json()["people"] == [], "unnamed faces are left out"


def test_details_say_who_added_the_photo(ctx):
    a = Album(ctx)
    assert details(a.helper, a.mia_only).json()["added_by"]["is_me"] is True
    other = details(a.owner, a.mia_only).json()["added_by"]
    assert other["is_me"] is False and other["name"].startswith("helper")
    assert a.helper.call("DELETE", f"/api/libraries/{a.id}/members/{a.helper.id}").status_code == 204
    assert details(a.owner, a.mia_only).json()["added_by"] == {"name": "Someone who left", "is_me": False}


def test_details_hide_opted_out_people_and_outsiders_get_nothing(ctx):
    a = Album(ctx)
    a.sql("UPDATE person SET recognition_opt_out = true WHERE id = :p", p=a.mia)
    assert [p["display_name"] for p in details(a.owner, a.both).json()["people"]] == ["Leo"]
    outsider = User(ctx, "outsider")
    make_library(outsider)
    assert details(outsider, a.both).status_code == 404
    assert ctx.client.get(f"/api/photos/{a.both}/details").status_code == 401
