"""
Tests for Stage 6 - spec-aware teaching, exam practice questions, live lessons. No AI, no
database, no PDFs needed (the real-PDF checks SKIP if the PDFs are missing).

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_stage6.py
Expected last line: "ALL 11 TESTS PASSED"
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: (_ for _ in ()).throw(RuntimeError("AI disabled in tests"))
_stub.get_effective_provider = lambda *a, **k: "none"
sys.modules["app.llm_gateway"] = _stub

from app import live_activities as la  # noqa: E402
from app import practice_questions as pq  # noqa: E402
from app import spec_context as sc  # noqa: E402

TESTS, SKIPPED = [], []


class Skip(Exception):
    pass


def test(fn):
    TESTS.append(fn)
    return fn


OUTLINE = {"slides": [
    {"type": "title", "title": "Cash flow", "subtitle": "Lesson 3"},
    {"type": "retrieval_starter", "title": "Do Now", "questions": ["What is revenue?", "What is a cost?"], "time_minutes": 5},
    {"type": "objectives", "objectives": ["Explain cash flow", "Construct a forecast"]},
    {"type": "content", "title": "What is cash flow?", "key_points": ["Money in", "Money out"], "check_question": "Why can a profitable firm run out of cash?"},
    {"type": "real_life_example", "title": "A cafe", "scenario": "A cafe buys stock in bulk...", "discussion_question": "What should it do?"},
    {"type": "activity", "title": "Build a forecast", "instructions": ["Use the sheet", "Work in pairs"], "grouping": "pairs"},
    {"type": "retrieval_plenary", "title": "Check", "questions": ["Define net cash flow"]},
    {"type": "objectives_review", "objectives_check": [{"objective": "Explain cash flow", "self_assess_prompt": "Can I explain it?"},
                                                       {"objective": "Construct a forecast"}]},
]}

EXAM_SPEC = {
    "units": [
        {"name": "Component 1: Business", "code": "Component 1", "assessment": "Written examination: 2 hours 15 minutes, 33⅓% of qualification",
         "learning_outcomes": [{"code": "LO1", "title": "Cash flow", "criteria": [{"code": "1.1", "description": "Explain what is meant by cash flow"}]}]},
        {"name": "Appendix A: Quantitative skills", "code": "Appendix A",
         "learning_outcomes": [{"code": "LO1", "title": "Skills", "criteria": [{"code": "1.1", "description": "calculate percentages"}]}]},
    ],
    "assessment": {"objectives": [{"code": "AO1", "description": "Demonstrate knowledge"}, {"code": "AO2", "description": "Apply knowledge"},
                                  {"code": "AO3", "description": "Analyse"}, {"code": "AO4", "description": "Evaluate"}],
                   "weightings": [{"name": "Component 1", "AO1": "12-14%", "AO2": "7-9%", "AO3": "6-8%", "AO4": "6-8%"}]},
}


@test
def every_deck_slide_becomes_a_live_screen_with_its_own_questions():
    screens = la.screens_from_deck(OUTLINE)
    assert [s["kind"] for s in screens] == ["title", "retrieval_starter", "objectives", "content", "real_life_example",
                                           "activity", "retrieval_plenary", "objectives_review"]
    assert [len(s["questions"]) for s in screens] == [0, 2, 0, 1, 1, 0, 1, 2]
    assert screens[3]["body"] == ["Money in", "Money out"] and screens[3]["questions"][0]["prompt"].startswith("Why can")
    review = screens[7]["questions"]
    assert all(q["kind"] == "confidence" and q["choices"] == la.CONFIDENCE for q in review)
    keys = [q["key"] for s in screens for q in s["questions"]]
    assert len(keys) == len(set(keys)) == 7  # every question has its own key


@test
def a_practice_set_becomes_one_screen_per_question():
    screens = la.screens_from_practice([{"question": "Explain cash flow.", "marks": 2, "ao": ["AO1"], "context": ""},
                                        {"question": "Evaluate...", "marks": 12, "ao": ["AO3", "AO4"], "context": "Ada runs a cafe."}])
    assert screens[0]["title"] == "Question 1 (2 marks · AO1)" and screens[1]["title"] == "Question 2 (12 marks · AO3 · AO4)"
    assert screens[1]["body"] == ["Ada runs a cafe."] and screens[1]["questions"][0]["long"] is True and not screens[0]["questions"][0]["long"]


@test
def answers_and_polls_are_counted_per_question():
    screen = la.screens_from_deck(OUTLINE)[7]
    k0, k1 = screen["questions"][0]["key"], screen["questions"][1]["key"]
    t = la.tally(screen, [{"question_key": k0, "name": "Ada", "choice": 0, "text": None},
                          {"question_key": k0, "name": "Ben", "choice": 2, "text": None},
                          {"question_key": k1, "name": "Ada", "choice": 2, "text": None}])
    assert t[k0]["counts"] == [1, 0, 1] and t[k1]["counts"] == [0, 0, 1] and t[k0]["count"] == 2


@test
def join_codes_are_readable_and_unique():
    codes = {la.new_join_code(set()) for _ in range(200)}
    assert all(len(c) == 6 and not set(c) & set("01OIL") for c in codes)
    assert la.new_join_code({"AAAAAA"}) != "AAAAAA"
    assert la.clean_name("  <b>Ada</b>   Lovelace the very long name indeed  ") == "<b>Ada</b> Lovelace the very l"  # pages escape it
    assert len(la.clean_answer("x" * 5000)) == la.MAX_ANSWER_CHARS


@test
def practice_questions_are_checked_before_they_are_shown():
    result = {"questions": [
        {"question": "Explain what is meant by cash flow.", "marks": 2, "ao": ["AO1"], "mark_scheme": ["money in and out"]},
        {"question": "Evaluate whether...", "marks": 12, "ao": ["ao4", "AO3"], "mark_scheme": ["..."],
         "levels": [{"level": "Level 3 (9-12)", "descriptor": "well-balanced evaluation"}], "context": "Ada runs a cafe."},
        {"question": "", "marks": 4, "ao": ["AO1"]},           # no question text
        {"question": "Silly", "marks": 99, "ao": ["AO1"]},      # silly marks
        {"question": "Unknown objective", "marks": 3, "ao": ["AO9"]},
        "not a question"]}
    questions, dropped = pq.clean_questions(result, ["AO1", "AO2", "AO3", "AO4"])
    assert len(questions) == 2 and dropped == 4
    assert questions[1]["ao"] == ["AO3", "AO4"] and questions[1]["levels"][0]["descriptor"] == "well-balanced evaluation"
    assert pq.ao_summary(questions) == {"AO1": 2, "AO3": 6, "AO4": 6}
    assert pq.clean_questions({"nope": 1}, ["AO1"]) == ([], 0)


@test
def the_practice_prompt_uses_the_specs_own_statements_and_objectives():
    unit = EXAM_SPEC["units"][0]
    prompt = pq.build_prompt(EXAM_SPEC, unit, unit["learning_outcomes"], 6)
    for needle in ("Write 6 practice questions", "Explain what is meant by cash flow", "AO2: Apply knowledge",
                   "Written examination: 2 hours 15 minutes", "AO1 12-14%", "calculate percentages"):
        assert needle in prompt, needle


@test
def exam_specs_get_objectives_other_specs_get_nothing_extra():
    unit = EXAM_SPEC["units"][0]
    context = sc.spec_teaching_context(EXAM_SPEC, unit)
    assert "AO4: Evaluate" in context and "QUANTITATIVE SKILLS" in context and "label each question" in context
    plain = {"units": [{"name": "Unit 1", "learning_outcomes": [{"code": "LO1", "title": "x", "criteria": [{"code": "P1", "description": "y"}]}]}]}
    assert sc.spec_teaching_context(plain, plain["units"][0]) == ""   # e.g. the Digital specs: prompts unchanged
    assert sc.has_exam_objectives(EXAM_SPEC) and not sc.has_exam_objectives(plain)
    assert sc.criterion_extra({"description": "x"}) == "" and "guidance: check" in sc.criterion_extra({"guidance": "check"})


@test
def media_teaching_content_is_found_for_the_outcome_being_taught():
    spec = {"content_areas": {"A": {"Informing ideas": {"summary": "Exploration of ideas.", "items": [
        {"level": 1, "text": "Interpreting a brief"}, {"level": 2, "text": "Clients and their needs"}]}}}}
    unit = {"name": "A1", "module": "A", "learning_outcomes": [{"code": "A1.1", "title": "t", "content_area": "Informing ideas", "criteria": []}]}
    text = sc.spec_teaching_context(spec, unit)
    assert "Teaching content the spec lists for 'Informing ideas'" in text and "- Interpreting a brief" in text and "  - Clients" in text


@test
def cityguilds_unit_aim_and_guidance_are_passed_on():
    unit = {"name": "Unit 301", "aim": "To understand roles.", "guidance": "Use examples from practice.", "learning_outcomes": []}
    text = sc.spec_teaching_context({}, unit)
    assert "Unit aim (from the spec): To understand roles." in text and "Delivery guidance" in text


@test
def context_is_capped_so_prompts_stay_small():
    unit = {"name": "U", "aim": "a " * 5000, "guidance": "g " * 5000, "learning_outcomes": []}
    assert len(sc.spec_teaching_context({}, unit)) <= sc.MAX_CONTEXT_CHARS + 4


@test
def real_specs_digital_unchanged_alevel_gets_objectives():
    import json
    pdfs = ROOT / "golden" / "pdfs"
    if not (pdfs / "alevel_business_eduqas.pdf").exists():
        raise Skip("golden PDFs not found")
    from app import spec_parser as sp
    from app.docx_annotator import extract_text_from_pdf
    for name, expect_empty in (("digital_dss", True), ("digital_dsd", True), ("alevel_business_eduqas", False)):
        pdf = str(pdfs / f"{name}.pdf")
        r = sp.parse_spec_hybrid(extract_text_from_pdf(pdf, include_table_copies=False), known_patterns=[], max_ai_calls=1, pdf_path=pdf)
        spec = {"units": r["units"], **r["extras"]}
        context = sc.spec_teaching_context(spec, r["units"][0])
        assert (context == "") == expect_empty, (name, context[:80])
    assert "AO1" in context and "2 hours 15 minutes" in context


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
