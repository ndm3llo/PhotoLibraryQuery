"""The 'Save name' decision on the people page is plain JavaScript. It has been wrong twice, so its rules are
extracted from the page and checked with Node (skipped when Node is not installed)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parent.parent / "app" / "web" / "people.html").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")

ANANYA = {"id": "a1", "display_name": "Ananya", "qualifier": None, "via_alias": None}
SARAH_COUSIN = {"id": "s2", "display_name": "Sarah", "qualifier": "cousin", "via_alias": None}
BY_NICKNAME = {"id": "b1", "display_name": "Ben", "qualifier": None, "via_alias": "Sari"}


def decide(picked, typed, qualifier, plain):
    match = re.search(r"// <decide>(.*?)// </decide>", PAGE, re.S)
    assert match, "decideSave markers are missing from people.html"
    script = match.group(1) + f"\nconsole.log(JSON.stringify(decideSave(...{json.dumps([picked, typed, qualifier, plain])})));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_an_empty_box_asks_for_a_name():
    assert decide(None, "  ", "", None)["action"] == "need-name"


def test_a_new_name_nobody_has_just_creates_the_person():
    assert decide(None, "Maya", "", None) == {"action": "create", "name": "Maya"}


def test_a_name_that_exists_is_always_asked_about_never_joined_silently():
    assert decide(None, "Ananya", "", ANANYA)["action"] == "ask"
    assert decide(ANANYA, "Ananya", "", None)["action"] == "ask", "picking the plain entry is still ambiguous"


def test_saying_no_and_adding_a_qualifier_creates_a_different_person_even_when_the_name_was_picked():
    """The reported bug: pick the existing 'Maya', answer 'No', type a description, save: it asked again."""
    assert decide(ANANYA, "Ananya", "look-alike", None) == {"action": "create", "name": "Ananya", "qualifier": "look-alike"}
    assert decide(None, "Ananya", "look-alike", ANANYA) == {"action": "create", "name": "Ananya", "qualifier": "look-alike"}


def test_the_qualifier_is_trimmed_and_blank_means_no_qualifier():
    assert decide(None, "Maya", "  cousin ", None)["qualifier"] == "cousin"
    assert decide(ANANYA, "Ananya", "   ", None)["action"] == "ask"


def test_a_specific_entry_is_unambiguous_and_joins_directly():
    assert decide(SARAH_COUSIN, "Sarah, cousin", "", None) == {"action": "join", "person": SARAH_COUSIN}
    assert decide(BY_NICKNAME, "Sari", "", None) == {"action": "join", "person": BY_NICKNAME}
