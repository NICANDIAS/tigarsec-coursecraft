"""
Tests for the spec review/edit rules (app/spec_editing.py). No database, no AI.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_spec_editing.py
Expected last line: "ALL 9 TESTS PASSED"
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import spec_editing as se  # noqa: E402

UNIT = {
    "name": "Assessment unit A1: Skills Development", "code": "A1", "glh": 405,
    "learning_outcomes": [
        {"code": "A1.1", "title": "Explore investigation skills", "content_area": "Informing ideas", "criteria": [
            {"code": "A1.AC1", "description": "Informing ideas - U: weak | Pass: ok | Merit: good | Distinction: great",
             "bands": {"U": "weak", "Pass": "ok", "Merit": "good", "Distinction": "great"}}]},
        {"code": "A1.2", "title": "Problem solving", "criteria": [
            {"code": "P1", "description": "Explain a thing"}, {"code": "P2", "description": "Describe a thing"}]},
    ],
}
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def fresh():
    return copy.deepcopy(UNIT)


@test
def unchanged_form_changes_nothing_and_keeps_extra_keys():
    form = {"unit-name": UNIT["name"]}
    new, changes, errors = se.edit_unit(fresh(), form, set())
    assert not errors and changes == [], (changes, errors)
    assert new == UNIT, "extra keys (glh, bands, content_area) must be kept"


@test
def rename_unit_and_detect_clash():
    new, changes, errors = se.edit_unit(fresh(), {"unit-name": "A1 renamed"}, {"Other unit"})
    assert new["name"] == "A1 renamed" and any("renamed" in c for c in changes)
    _, _, errors = se.edit_unit(fresh(), {"unit-name": "other UNIT"}, {"Other unit"})
    assert errors, "a name clash (any capitals) must be refused"


@test
def edit_band_wording_rebuilds_description():
    form = {"unit-name": UNIT["name"], "c-0-0-band-Merit": "very good", "c-0-0-title": "Informing ideas"}
    new, changes, errors = se.edit_unit(fresh(), form, set())
    crit = new["learning_outcomes"][0]["criteria"][0]
    assert not errors
    assert crit["bands"]["Merit"] == "very good"
    assert crit["description"] == "Informing ideas - U: weak | Pass: ok | Merit: very good | Distinction: great"
    assert changes == ["criterion A1.1/A1.AC1: Merit wording changed"], changes


@test
def delete_and_add_criteria():
    form = {"unit-name": UNIT["name"], "c-1-1-delete": "on", "new-c-1-code": "P3", "new-c-1-desc": "Analyse"}
    new, changes, errors = se.edit_unit(fresh(), form, set())
    codes = [c["code"] for c in new["learning_outcomes"][1]["criteria"]]
    assert not errors and codes == ["P1", "P3"], (codes, errors)
    assert len(changes) == 2


@test
def duplicate_codes_are_refused_and_nothing_changes():
    unit = fresh()
    form = {"unit-name": UNIT["name"], "c-1-1-code": "P1"}
    new, changes, errors = se.edit_unit(unit, form, set())
    assert errors and new is unit and changes == []


@test
def add_and_delete_learning_outcomes():
    form = {"unit-name": UNIT["name"], "lo-0-delete": "on", "new-lo-code": "A1.6", "new-lo-title": "Extra"}
    new, changes, errors = se.edit_unit(fresh(), form, set())
    assert not errors and [lo["code"] for lo in new["learning_outcomes"]] == ["A1.2", "A1.6"]


@test
def empty_codes_are_refused():
    _, _, errors = se.edit_unit(fresh(), {"unit-name": UNIT["name"], "lo-1-code": ""}, set())
    assert errors
    _, _, errors = se.edit_unit(fresh(), {"unit-name": UNIT["name"], "new-c-0-desc": "no code given"}, set())
    assert errors


@test
def reviewing_every_unit_turns_needs_review_into_good():
    units = [fresh()]
    quality = {"verdict": "needs_review", "problems": ["1 part(s) of the document could not be read and were skipped"], "notes": []}
    q = se.updated_quality(quality, units, [], "Tester")
    assert q["verdict"] == "needs_review", "not checked yet"
    q = se.updated_quality(q, units, [UNIT["name"]], "Tester")
    assert q["verdict"] == "good" and q["problems"] == [], q
    assert any(n.startswith("Checked by a person: 1 of 1") for n in q["notes"])


@test
def structural_problem_blocks_good_even_when_reviewed():
    unit = fresh()
    unit["learning_outcomes"][1]["criteria"] = []
    q = se.updated_quality({"verdict": "good", "problems": [], "notes": []}, [unit], [unit["name"]], "Tester")
    assert q["verdict"] == "needs_review" and any("no criteria" in p for p in q["problems"]), q


if __name__ == "__main__":
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    if failed:
        print(f"\n{failed} OF {len(TESTS)} TESTS FAILED")
        sys.exit(1)
    print(f"\nALL {len(TESTS)} TESTS PASSED")
