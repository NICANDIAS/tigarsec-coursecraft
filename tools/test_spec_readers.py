"""
Tests for the spec style readers (Stage 2). No AI, no database.
The Media, C&G and A level tests use their PDFs in golden/pdfs/ if they are there (SKIP otherwise).

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_spec_readers.py
Expected last line: "ALL 16 TESTS PASSED" (or fewer run + SKIP lines if the PDF is missing)
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
from app.spec_readers import lo_ac_units as loac  # noqa: E402
from app.spec_readers import content_amplification as ca  # noqa: E402

MEDIA_PDF = ROOT / "golden" / "pdfs" / "media_btec_cmp.pdf"
CG_PDF = ROOT / "golden" / "pdfs" / "cityguilds_6502_l4.pdf"
AL_PDF = ROOT / "golden" / "pdfs" / "alevel_business_eduqas.pdf"
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


def cg_result():
    if not CG_PDF.exists():
        raise Skip("golden/pdfs/cityguilds_6502_l4.pdf not found")
    if "cg" not in _media_cache:
        from app.docx_annotator import extract_text_from_pdf
        raw = extract_text_from_pdf(str(CG_PDF), include_table_copies=False)
        _media_cache["cg"] = sp.parse_spec_hybrid(raw, known_patterns=[], max_ai_calls=1, pdf_path=str(CG_PDF))
    return _media_cache["cg"]


@test
def cityguilds_units_outcomes_and_criteria():
    r = cg_result()
    assert [u["code"] for u in r["units"]] == ["301", "401", "402", "403", "404"]
    u401 = r["units"][1]
    assert u401["name"] == "Unit 401: Planning to meet the needs of learners in education and training", u401["name"]
    assert (u401["level"], u401["credits"], u401["glh"], u401["group"]) == (4, 3, 15, "mandatory")
    assert u401["learning_outcomes"][2]["title"] == "be able to implement the minimum core in planning inclusive teaching and learning"
    assert [c["code"] for c in u401["learning_outcomes"][1]["criteria"]] == ["2.1", "2.2", "2.3", "2.4", "2.5"]
    assert u401["aim"].startswith("The purpose of the unit is to enable the learner to agree")
    assert r["quality"]["verdict"] == "good" and r["quality"]["ai_calls_used"] == 0


@test
def cityguilds_rules_and_listed_optional_units():
    extras = cg_result()["extras"]
    assert extras["qualification"]["rules"][0].startswith("candidates must achieve a minimum of 36 credits")
    listed = {u["number"]: u for u in extras["qualification"]["units_listed"]}
    assert listed["307"]["group"] == "optional" and listed["301"]["group"] == "mandatory"
    assert extras["grading"]["type"] == "met_not_met"
    assert any("optional unit pack" in n for n in cg_result()["quality"]["notes"])


@test
def footer_filter_keeps_repeated_real_lines():
    lines = ["Aim: The purpose of the unit is to enable the"] * 5 + ["education and training"] * 6 + \
            [f"City & Guilds Level 4 Certificate in Education and Training (6502) {n}" for n in range(20, 26)]
    kept = loac._drop_running_footers(lines)
    assert kept.count("Aim: The purpose of the unit is to enable the") == 5
    assert kept.count("education and training") == 6
    assert not any("City & Guilds Level 4" in l for l in kept), "page footers must go"


@test
def lo_ac_fingerprint_ignores_other_styles():
    media_like = "A1: Skills (405 GLH)\nA1.1 Explore ideas\nAC1: Informing ideas\n"
    assert not loac.looks_like(media_like)
    assert not loac.looks_like("3   Core Component\nContent area 1: X\n1.1.1 Know.\n")



def al_result():
    if not AL_PDF.exists():
        raise Skip("golden/pdfs/alevel_business_eduqas.pdf not found")
    if "al" not in _media_cache:
        from app.docx_annotator import extract_text_from_pdf
        raw = extract_text_from_pdf(str(AL_PDF), include_table_copies=False)
        _media_cache["al"] = sp.parse_spec_hybrid(raw, known_patterns=[], max_ai_calls=1, pdf_path=str(AL_PDF))
    return _media_cache["al"]


@test
def alevel_components_topics_and_statements():
    r = al_result()
    units = r["units"]
    assert r["ruleset_used"].startswith("Content / Amplification"), r["ruleset_used"]
    assert [u["name"] for u in units] == ["Component 1: Business Opportunities and Functions", "Component 2: Business Analysis and Strategy",
                                         "Component 3: Business in a Changing World", "Appendix A: Quantitative skills"]
    assert [len(u["learning_outcomes"]) for u in units] == [43, 10, 12, 1]
    assert [sum(len(lo["criteria"]) for lo in u["learning_outcomes"]) for u in units] == [225, 79, 79, 9]
    c1 = {}
    for lo in units[0]["learning_outcomes"]:
        c1.setdefault(lo["title"], lo)  # first one: "Business finance" is also a topic under Finance (as in the spec)
    # the three topics a lecturer checked by hand against the PDF (incl. one that runs onto the next page)
    assert len(c1["Business location"]["criteria"]) == 3 and len(c1["Business finance"]["criteria"]) == 3
    rc = c1["Business revenue and costs"]["criteria"]
    assert len(rc) == 12 and rc[-1]["description"] == "Evaluate the usefulness of break-even to a business and its stakeholders"
    assert rc[0]["code"] == "8.1" and c1["Business revenue and costs"]["code"] == "LO8"
    assert units[0]["assessment"] == "Written examination: 2 hours 15 minutes, 33⅓% of qualification"
    assert r["quality"]["verdict"] == "good" and r["quality"]["ai_calls_used"] == 0


@test
def alevel_tricky_layouts():
    units = al_result()["units"]
    titles = [lo["title"] for u in units for lo in u["learning_outcomes"]]
    # two topics in one ruled box are kept apart; a hyphen at a line end is joined
    assert "Analysing non-financial performance" in titles and "Aims and objectives" in titles
    # a heading row over sub-topics ("PEST factors") is carried into their names
    assert "PEST factors - Economic" in titles and "PEST factors" not in titles
    markets = next(lo for lo in units[0]["learning_outcomes"] if lo["title"] == "Markets")["criteria"]
    assert any(c["description"].endswith("ranging from perfect competition to monopoly") for c in markets)
    assert not any(c["description"][0].islower() for u in units[:3] for lo in u["learning_outcomes"] for c in lo["criteria"])


@test
def alevel_assessment_objectives_and_weightings():
    exam = al_result()["extras"]["assessment"]
    assert [a["code"] for a in exam["objectives"]] == ["AO1", "AO2", "AO3", "AO4"]
    assert exam["objectives"][3]["description"].startswith("Evaluate quantitative and qualitative information")
    assert exam["weightings"][-1] == {"name": "Overall weighting", "AO1": "24-26%", "AO2": "24-26%", "AO3": "24-26%", "AO4": "24-26%"}
    assert len(exam["components"]) == 3


@test
def content_amplification_needs_the_pdf_and_its_own_layout():
    assert ca.read("Content Amplification\n" * 5, None) is None  # no PDF: the columns can't be told apart
    assert not ca.looks_like("Unit 401 Planning\nThe learner will:\nThe learner can:")
    blocks = ca._blocks([{"text": "Explain", "top": 100, "x0": 0, "size": 11}, {"text": "it", "top": 112.6, "x0": 0, "size": 11},
                         {"text": "Next", "top": 128.5, "x0": 0, "size": 11}], rules=[120])
    assert [t for _, t in blocks] == ["Explain it", "Next"]  # wrapped line joined; a ruled line splits


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
