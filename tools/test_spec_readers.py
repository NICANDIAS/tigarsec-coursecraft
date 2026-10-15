"""
Tests for the spec style readers (Stage 2). No AI, no database.
The Media tests use golden/pdfs/media_btec_cmp.pdf if it is there (SKIP otherwise).

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_spec_readers.py
Expected last line: "ALL 8 TESTS PASSED" (or fewer run + SKIP lines if the PDF is missing)
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: (_ for _ in ()).throw(RuntimeError("AI disabled in tests"))
sys.modules["app.llm_gateway"] = _stub

from app import spec_parser as sp  # noqa: E402
from app.spec_readers import btec_practice as bp  # noqa: E402

MEDIA_PDF = ROOT / "golden" / "pdfs" / "media_btec_cmp.pdf"
TESTS, SKIPPED = [], []
_media_cache = {}


def test(fn):
    TESTS.append(fn)
    return fn


class Skip(Exception):
    pass


def media_result():
    if not MEDIA_PDF.exists():
        raise Skip("golden/pdfs/media_btec_cmp.pdf not found")
    if "r" not in _media_cache:
        from app.docx_annotator import extract_text_from_pdf
        raw = extract_text_from_pdf(str(MEDIA_PDF), include_table_copies=False)
        _media_cache["r"] = sp.parse_spec_hybrid(raw, known_patterns=[], max_ai_calls=1, pdf_path=str(MEDIA_PDF))
    return _media_cache["r"]


@test
def media_units_objectives_and_codes():
    r = media_result()
    names = [u["name"] for u in r["units"]]
    assert names == ["Assessment unit A1: Skills Development", "Assessment unit A2: Creative Project",
                     "Assessment unit B1: Personal Progression", "Assessment unit B2: Creative Industry Response"], names
    b1 = r["units"][2]["learning_outcomes"]
    assert [lo["code"] for lo in b1] == ["B1.1", "B1.2", "B1.3", "B1.4", "B1.5"]
    assert b1[0]["title"] == "Inform ideas for progression into a creative career", b1[0]["title"]
    assert b1[3]["title"].startswith("Demonstrate professional practice"), "'D emonstrate' split was not repaired"
    assert r["units"][0]["learning_outcomes"][0]["criteria"][0]["code"] == "A1.AC1"
    assert r["quality"]["verdict"] == "good", r["quality"]


@test
def media_grade_bands_are_split_correctly():
    crit = media_result()["units"][3]["learning_outcomes"][4]["criteria"][0]  # B2.5 -> AC5
    assert crit["code"] == "B2.AC5"
    bands = crit["bands"]
    assert bands["U"].startswith("Lack of clarity seen in outcomes"), bands["U"]
    assert bands["Pass"].startswith("Generally competent communication"), bands["Pass"]
    assert bands["Distinction"].startswith("Fluent and imaginative communication"), bands["Distinction"]
    assert "Merit" not in bands["Pass"] and "Pearson" not in bands["Distinction"], "text from outside the grid leaked in"


@test
def media_points_rule_sizes_and_content():
    extras = media_result()["extras"]
    assert extras["grading"]["points"]["B2"] == {"U": 0, "Pass": 3, "Merit": 6, "Distinction": 9}
    assert extras["grading"]["points"]["A1"]["Distinction"] == 3
    assert "minimum of Pass in all criteria" in extras["qualification"]["pass_rule"]
    sizes = {s["name"]: s["units"] for s in extras["qualification"]["sizes"]}
    assert sizes == {"540 Diploma": ["A1", "A2"], "Extended Diploma": ["A1", "A2", "B1", "B2"]}, sizes
    for module in ("A", "B"):
        assert set(extras["content_areas"][module]) == {"Informing ideas", "Problem solving", "Technical skill",
                                                        "Professional practice", "Communication"}
    tech = [i["text"] for i in extras["content_areas"]["A"]["Technical skill"]["items"] if i["level"] == 1]
    assert tech[0] == "Moving image", tech


@test
def media_without_pdf_is_flagged_not_guessed():
    from app.docx_annotator import extract_text_from_pdf
    if not MEDIA_PDF.exists():
        raise Skip("golden/pdfs/media_btec_cmp.pdf not found")
    raw = extract_text_from_pdf(str(MEDIA_PDF), include_table_copies=False)
    r = sp.parse_spec_hybrid(raw, known_patterns=[], max_ai_calls=1, pdf_path=None)
    assert len(r["units"]) == 4
    assert r["quality"]["verdict"] == "needs_review"
    assert any("No original PDF" in p for p in r["quality"]["problems"])
    assert "bands" not in r["units"][0]["learning_outcomes"][0]["criteria"][0]


@test
def fingerprint_ignores_other_styles():
    t_level = "3   Core Component\nContent area 1: Problem solving\n1.1 Computational thinking\n1.1.1 Know things.\n"
    btec_units = "Unit 1: Something\nLearning aim A: Do\nP1 Explain\nP2 Describe\nM1 Analyse\n"
    assert not bp.looks_like(t_level) and not bp.looks_like(btec_units)


@test
def objective_titles_are_cleaned_and_fragments_rejected():
    text = ("A1: Skills Development (405 GLH)\nA2: Project (135 GLH)\n"
            "A1.1 Explore investigation skills 1. A ssessment objectives are\n"
            "A1.1 Explore investigation skills\n"
            "A1.2 D emonstrate problem solving\n"
            "A1.3 and A2.1.\n")
    units, order, objectives = bp._read_units(text)
    assert order == ["A1", "A2"]
    assert objectives[("A1", 1)] == "Explore investigation skills"
    assert objectives[("A1", 2)] == "Demonstrate problem solving"
    assert ("A1", 3) not in objectives, "sentence fragment accepted as an objective"


@test
def content_bullets_get_the_right_levels():
    parsed = bp._parse_content([
        "Short summary of the area.",
        "• Moving image",
        "Learners can explore film and TV.",
        "○ Editing, e.g. cuts",
        "– visual language, e.g.:",
        "• colour theory",
        "– media and techniques",
        "• Sound",
    ])
    levels = [(i["level"], i["text"]) for i in parsed["items"]]
    assert parsed["summary"] == "Short summary of the area."
    assert levels == [(1, "Moving image"), (2, "Editing, e.g. cuts"), (3, "visual language, e.g.:"),
                      (4, "colour theory"), (3, "media and techniques"), (1, "Sound")], levels
    assert parsed["items"][0]["note"] == "Learners can explore film and TV."


@test
def extra_unit_keys_survive_merging():
    merged = sp._merge_unit_results([[{"name": "U1", "code": "A1", "glh": 405, "learning_outcomes": []}]])
    assert merged[0]["code"] == "A1" and merged[0]["glh"] == 405
    plain = sp._merge_unit_results([[{"name": "U1", "learning_outcomes": []}]])
    assert plain == [{"name": "U1", "learning_outcomes": []}], "old-style units must come out unchanged"


if __name__ == "__main__":
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Skip as s:
            SKIPPED.append(fn.__name__)
            print(f"SKIP  {fn.__name__}: {s}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    ran = len(TESTS) - len(SKIPPED)
    if failed:
        print(f"\n{failed} OF {ran} TESTS FAILED")
        sys.exit(1)
    print(f"\nALL {ran} TESTS PASSED" + (f" ({len(SKIPPED)} skipped)" if SKIPPED else ""))
