"""The Memoria web app: how the pages are served, and its pure browser logic (run with Node)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

APP_JS = Path(__file__).resolve().parent.parent / "app" / "web" / "app.js"
TOKEN = "Ab3_-" * 9


@pytest.fixture
def client(app_db, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    from app.api import web
    app = FastAPI()
    app.include_router(web.router)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/app", "/join"])
def test_app_and_invite_pages_only_run_our_own_script(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "/assets/app.js" in r.text
    policy = r.headers["content-security-policy"]
    assert "script-src 'self';" in policy and "unsafe" not in policy and "default-src 'none'" in policy
    assert "connect-src 'self' https://test.supabase.co;" in policy
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-frame-options"] == "DENY"
    assert "<script>" not in r.text and " style=" not in r.text, "inline code would be blocked"


def test_assets_are_served(client):
    js, css = client.get("/assets/app.js"), client.get("/assets/app.css")
    assert js.status_code == css.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")
    assert css.headers["content-type"].startswith("text/css")


def test_the_app_never_builds_pages_from_html_text():
    source = APP_JS.read_text()
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert risky not in source, risky


def run(expression: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    script = f"const app = require({json.dumps(str(APP_JS))}); console.log(JSON.stringify({expression}))"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def entry(path, hash_):
    return run(f"app.parseEntry({json.dumps(path)}, {json.dumps(hash_)})")


def test_invite_links_are_recognised():
    assert entry("/join", "#" + TOKEN) == {"invite": TOKEN, "route": ""}
    for broken in ("", "#short", "#" + TOKEN + "<script>"):
        assert entry("/join", broken) == {"badInvite": True, "route": ""}
    assert "invite" not in entry("/app", "#" + TOKEN)


def test_email_links_and_app_addresses():
    back = entry("/app", "#access_token=abc&refresh_token=def&expires_in=3600&type=recovery")
    assert back["type"] == "recovery" and back["auth"]["access_token"] == "abc" and back["route"] == ""
    assert entry("/app", "#error=access_denied&error_description=Link+expired")["error"] == "Link expired"
    assert entry("/app", "#/album/123") == {"route": "/album/123"}
    assert entry("/app", "") == {"route": ""}


@pytest.mark.parametrize("status,body,words", [
    (400, {"error_code": "invalid_credentials", "msg": "Invalid login credentials"}, "don't match"),
    (400, {"error": "invalid_grant", "error_description": "Email not confirmed"}, "tap the link"),
    (422, {"error_code": "user_already_exists", "msg": "User already registered"}, "already have an account"),
    (429, {"msg": "anything"}, "Too many tries"),
    (422, {"error_code": "weak_password", "msg": "Password should be at least 6 characters"}, "longer"),
    (400, {"error_code": "email_address_invalid", "msg": "Email address is invalid"}, "Check the email"),
    (500, {}, "Something went wrong"),
])
def test_sign_in_errors_are_in_plain_words(status, body, words):
    assert words in run(f"app.authMessage({status}, {json.dumps(body)})")


def test_server_errors_say_album_and_hide_internals():
    assert "only owner" in run("app.apiMessage(409, 'A library must keep at least one owner')")
    assert "album" in run("app.apiMessage(404, 'Library not found')")
    assert "Something went wrong" in run("app.apiMessage(500, 'Traceback: secret')")
    assert "Something went wrong" in run("app.apiMessage(422, [{msg: 'field required'}])")


def test_photos_are_grouped_by_the_local_month():
    items = [{"taken_local": "2026-07-31T23:30:00"}, {"taken_local": None}, {"taken_local": "2026-07-01T00:10:00"},
             {"taken_local": "2025-12-31T20:00:00"}]
    groups = run(f"app.byMonth({json.dumps(items)})")
    assert [(g["label"], len(g["items"])) for g in groups] == [("July 2026", 2), ("December 2025", 1), ("No date", 1)]
    assert run("[app.photoCount(1), app.photoCount(1248)]") == ["1 photo", "1,248 photos"]


def test_filters_become_query_parameters():
    build = "(f => app.filterQuery(Object.assign(app.noFilters(), f), '11-25'))"
    assert run(f"{build}({{}})") == ""
    assert run(f"{build}({{people: ['a'], match: 'any'}})") == "person=a", "match only matters for several people"
    assert run(f"{build}({{people: ['a', 'b'], match: 'any', uploaders: ['u']}})") == \
        "person=a&person=b&match=any&uploader=u"
    assert run(f"{build}({{when: {{kind: 'year', year: 2019}}}})") == "year=2019"
    assert run(f"{build}({{when: {{kind: 'range', from: '2019-01-02'}}}})") == "date_from=2019-01-02"
    assert run(f"{build}({{when: {{kind: 'range'}}}})") == "", "picking no dates filters nothing"
    assert run(f"{build}({{when: {{kind: 'today'}}}})") == "on_day=11-25"
    assert run(f"{build}({{when: {{kind: 'undated'}}}})") == "undated=true"


def test_the_chosen_filters_are_described_in_words():
    words = "(f => app.filterSummary(Object.assign(app.noFilters(), f), {a: 'Mia', b: 'Leo', u: 'me'}))"
    assert run(f"{words}({{}})") == ""
    assert run(f"{words}({{people: ['a', 'b']}})") == "Mia and Leo"
    assert run(f"{words}({{people: ['a', 'b'], match: 'any'}})") == "Mia or Leo"
    assert run(f"{words}({{people: ['a', 'b', 'c']}})") == "3 people"
    assert run(f"{words}({{uploaders: ['u'], when: {{kind: 'year', year: 2019}}}})") == "2019 · Added by me"
    assert run(f"{words}({{people: ['a'], when: {{kind: 'undated'}}}})") == "Mia · No date"
    assert run(f"{words}({{when: {{kind: 'range'}}}})") == ""


def test_face_square_stays_inside_the_photo():
    assert run("app.faceBox([100, 100, 200, 200], 1000, 800)") == {"x": 65, "y": 65, "side": 170}
    assert run("app.faceBox([0, 0, 100, 100], 1000, 800)") == {"x": 0, "y": 0, "side": 170}
    assert run("app.faceBox([900, 700, 1000, 800], 1000, 800)") == {"x": 830, "y": 630, "side": 170}
    assert run("app.faceBox([0, 0, 900, 900], 1000, 800)")["side"] == 800
    assert run("[app.faceBox(null, 10, 10), app.faceBox([1, 2, 3, 4], null, 10)]") == [None, None]
