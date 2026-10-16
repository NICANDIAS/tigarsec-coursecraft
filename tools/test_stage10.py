"""
Tests for Stage 10 - marking feeds teaching (class marking report, next steps) and ready-made
differentiation (support / stretch versions of every activity). No AI, no database.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_stage10.py
Expected last line: "ALL 12 TESTS PASSED"
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import marking_insights as mi  # noqa: E402
from app import differentiation as df  # noqa: E402
from app import class_feedback as cf  # noqa: E402
from app.live_activities import screens_from_deck  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


UNIT = {"name": "Unit 2 Networking", "learning_outcomes": [
    {"code": "A", "title": "Network types", "criteria": [
        {"code": "P1", "description": "Explain two network topologies."},
        {"code": "P10", "description": "Describe a network protocol."},
        {"code": "M1", "description": "Compare the performance of two topologies."},
        {"code": "D1", "description": "Evaluate the choice of topology for a given business."}]}]}
CRITERIA = mi.unit_criteria(UNIT)


def student(name, marks):
    return {"student_id": hash(name), "name": name, "marks": [{"criterion_ref": r, "final_mark": m, "comment": c} for r, m, c in marks]}


CLASS = [
    student("Amira Khan", [("P1 Explain", "Met", ""), ("M1 Compare", "Not met", "Amira only described one topology, no comparison."),
                           ("D1", "Not met", "No evaluation.")]),
    student("Ben O'Neill", [("P1", "Met", ""), ("M1", "Partly met", "Ben's comparison lists features but does not say which performs better."),
                            ("D1", "Not met", "")]),
    student("Chloe Price", [("P1", "Met", ""), ("M1", "Not met", "Chloe compares cost, not performance."), ("D1", "Not met", "")]),
    student("Dan Old", [("P1", "Not met", "Only one topology explained."), ("M1", "Met", ""), ("D1", "Met", "")]),
]


@test
def criteria_are_matched_by_whole_code():
    assert mi.match_criterion("P1 - Explain two topologies", CRITERIA)["code"] == "P1"
    assert mi.match_criterion("P10 Describe a protocol", CRITERIA)["code"] == "P10"   # never mixed up with P1
    assert mi.match_criterion("Unit 2 M1", CRITERIA)["code"] == "M1"
    assert mi.match_criterion("Q4", CRITERIA) is None


@test
def the_report_counts_each_criterion_and_finds_class_wide_gaps():
    rows = {r["code"]: r for r in mi.criterion_report(CLASS, CRITERIA)}
    assert set(rows) == {"P1", "M1", "D1"}  # P10 was never marked - not shown
    assert (rows["P1"]["met"], rows["P1"]["not_met"], rows["P1"]["met_pct"], rows["P1"]["gap"]) == (3, 1, 75, False)
    assert (rows["M1"]["met"], rows["M1"]["partly"], rows["M1"]["not_met"], rows["M1"]["gap"]) == (1, 1, 2, True)
    assert rows["M1"]["not_yet"] == ["Amira Khan", "Ben O'Neill", "Chloe Price"]
    assert rows["D1"]["gap"] and rows["D1"]["met_pct"] == 25


@test
def too_few_marked_students_is_not_a_class_wide_gap():
    rows = mi.criterion_report(CLASS[:2], CRITERIA)
    assert not any(r["gap"] for r in rows)


@test
def next_steps_follow_the_btec_rule_pass_first():
    dan = mi.next_steps(CLASS[3]["marks"], CRITERIA)          # missing a Pass criterion -> Pass first, even though M1/D1 are met
    assert dan["target"] == "Pass" and [s["code"] for s in dan["steps"]] == ["P1"]
    ben = mi.next_steps(CLASS[1]["marks"], CRITERIA)          # Pass secured -> Merit next, D1 not mentioned yet
    assert ben["target"] == "Merit" and ben["steps"] == [{"code": "M1", "description": "Compare the performance of two topologies.",
                                                          "status": "partly met"}]
    assert "Next steps to reach Merit:" in mi.next_steps_text(ben) and "M1: Compare" in mi.next_steps_text(ben)
    assert mi.next_steps([{"criterion_ref": "P1", "final_mark": "Met"}], CRITERIA) == {"target": None, "steps": []}


@test
def specs_without_pass_merit_distinction_list_everything_not_yet_met():
    tlevel = [{"code": "1.1", "description": "Explain IP addressing.", "lo": ""}, {"code": "1.2", "description": "Explain DNS.", "lo": ""}]
    plan = mi.next_steps([{"criterion_ref": "1.1", "final_mark": "Partly met"}, {"criterion_ref": "1.2", "final_mark": "Met"}], tlevel)
    assert plan == {"target": None, "steps": [{"code": "1.1", "description": "Explain IP addressing.", "status": "partly met"}]}


@test
def no_student_name_goes_to_the_ai():
    rows = mi.criterion_report(CLASS, CRITERIA)
    prompt = mi.insight_prompt("Unit 2", rows, [s["name"] for s in CLASS])
    assert "compares cost, not performance" in prompt and "M1" in prompt
    for name in ("Amira", "Ben", "O'Neill", "Chloe", "Khan", "Price", "Dan"):
        assert name not in prompt, name
    assert "Criterion M1" in prompt and "Criterion D1" in prompt and "Criterion P1" not in prompt  # only the gaps are sent


@test
def the_ai_summary_is_checked_and_feeds_the_next_lesson():
    rows = mi.criterion_report(CLASS, CRITERIA)
    insight = mi.clean_insight({"criteria": [
        {"code": "m1", "common_problems": ["Describes instead of comparing", "Compares cost not performance"], "reteach_tip": "Model a comparison table."},
        {"code": "P1", "common_problems": ["not a gap"]}, {"code": "Z9", "common_problems": ["unknown"]}]}, rows)
    assert set(insight) == {"M1"} and insight["M1"]["reteach_tip"] == "Model a comparison table."
    gaps = mi.deck_gaps("Unit 2 Networking", rows, insight)
    assert sorted(g["key"] for g in gaps) == ["crit:D1", "crit:M1"]
    line = cf.describe(next(g for g in gaps if g["key"] == "crit:M1"))
    assert line.startswith('Assignment criterion "M1 Compare the performance') and "only 1 of 4 students" in line
    assert "Common problems: Describes instead of comparing; Compares cost not performance." in line
    block = cf.with_revisit("Last lesson: topologies.", gaps)
    assert cf.HEADER in block and "Amira" not in block


OUTLINE = {"lesson_title": "Topologies", "slides": [
    {"type": "objectives", "objectives": ["Compare star and mesh topologies"]},
    {"type": "content", "title": "Star", "key_points": ["Central switch"]},
    {"type": "activity", "title": "Topology hunt", "instructions": ["Find three topologies in the photos", "Label each one"], "grouping": "pairs",
     "resource": {"needed": True, "title": "Photo sheet", "content": "Photo 1: office\\nPhoto 2: data centre"}},
    {"type": "practical", "title": "Build a star", "instructions": "Use Packet Tracer to build a star network."},
]}
AI_VERSIONS = {"activities": [
    {"slide": 2, "support": {"steps": ["Look at photo 1", "Find the switch"], "scaffolds": ["A star topology has...", "Key word: switch - connects devices"]},
     "stretch": {"steps": ["Say which topology suits a hospital and why"], "challenge": "What happens if the central switch fails?"}},
    {"slide": 3, "support": {"steps": ["Open Packet Tracer", "Drag one switch"]}, "stretch": {"steps": []}},
    {"slide": 1, "support": {"steps": ["not an activity"]}},           # a content slide - ignored
    {"slide": "x"}, "nonsense",
]}


@test
def the_ai_is_given_each_activity_with_its_handout():
    prompt = df.build_prompt(OUTLINE)
    for text in ("Compare star and mesh topologies", "Activity slide 2: Topology hunt", "Label each one", "Photo sheet",
                 "Activity slide 3: Build a star", "Use Packet Tracer"):
        assert text in prompt, text
    assert "Central switch" not in prompt


@test
def only_real_activities_get_versions_and_the_outline_is_a_new_copy():
    versions = df.clean_differentiation(AI_VERSIONS, OUTLINE)
    assert set(versions) == {2, 3} and "stretch" not in versions[3]  # an empty stretch is dropped, not shown blank
    updated = df.apply(OUTLINE, versions)
    assert updated is not OUTLINE and "differentiation" not in OUTLINE["slides"][2]  # the original is untouched
    assert updated["slides"][2]["differentiation"]["stretch"]["challenge"].startswith("What happens")
    assert df.has_differentiation(updated) and not df.has_differentiation(OUTLINE) and df.needs_differentiation(OUTLINE)


@test
def worksheets_are_printed_as_support_core_and_stretch():
    from docx import Document
    from app.resource_export import render_resources_docx, worksheet_count
    updated = df.apply(OUTLINE, df.clean_differentiation(AI_VERSIONS, OUTLINE))
    assert worksheet_count(OUTLINE) == 1 and worksheet_count(updated) == 3   # the handout + two differentiated activities
    path = os.path.join(tempfile.mkdtemp(), "res.docx")
    assert render_resources_docx(updated, "Topologies", path) == 3
    text = "\n".join(p.text for p in Document(path).paragraphs)
    for heading in ("Topology hunt - Support", "Topology hunt - Core", "Topology hunt - Stretch", "Build a star - Support", "Build a star - Core"):
        assert heading in text, heading
    assert "Build a star - Stretch" not in text and "Key word: switch" in text and "What happens if the central switch fails?" in text
    assert "1. Use Packet Tracer to build a star network." in text  # a one-paragraph practical is one step


@test
def live_lessons_carry_support_and_stretch_to_phones():
    updated = df.apply(OUTLINE, df.clean_differentiation(AI_VERSIONS, OUTLINE))
    hunt = next(s for s in screens_from_deck(updated) if s["title"] == "Topology hunt")
    assert hunt["support"] == {"steps": ["Look at photo 1", "Find the switch"], "help": ["A star topology has...", "Key word: switch - connects devices"]}
    assert hunt["stretch"]["challenge"] == "What happens if the central switch fails?"
    plain = next(s for s in screens_from_deck(OUTLINE) if s["title"] == "Topology hunt")
    assert "support" not in plain and "stretch" not in plain


@test
def the_lesson_writer_knows_about_marked_work():
    from app.pptx_generator import LESSON_OUTLINE_SYSTEM_PROMPT as prompt
    flat = " ".join(prompt.split())
    assert "RESULTS FROM THE CLASS'S RECENT CHECKS AND MARKED WORK" in flat and "never write the assignment answer" in flat


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
