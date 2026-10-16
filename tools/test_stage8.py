"""
Tests for Stage 8 - importing a class list (ProSolution / CSV / Excel / pasted) and live quizzes
(multiple choice, countdown, points, leaderboard). No AI, no database.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_stage8.py
Expected last line: "ALL 14 TESTS PASSED"
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import class_list_import as ci  # noqa: E402
from app import quiz as qz  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


PROSOLUTION_CSV = """Dudley College of Technology
Class List Report,,Printed 16/10/2026
,,,,,
Student Ref,Forename,Surname,Preferred Name,Date of Birth,Email,Group Code
S1234567,Amira,KHAN,,01/02/2008,a.khan@student.example,TLCOMP2A
S1234568,Ben,O'NEILL,Benny,02/03/2008,b.oneill@student.example,TLCOMP2A
S1234569,Chloe,MCDONALD-SMITH,,03/04/2008,c.mcd@student.example,TLCOMP2B
S1234567,Amira,KHAN,,01/02/2008,a.khan@student.example,TLCOMP2A
"""


def _people(text, columns=None):
    rows = ci.read_text(text)
    header = ci.find_header_row(rows)
    return ci.build_rows(rows, header, columns or ci.guess_columns(rows[header]))


@test
def the_heading_row_is_found_under_a_report_title():
    rows = ci.read_text(PROSOLUTION_CSV)
    assert ci.find_header_row(rows) == 3
    cols = ci.guess_columns(rows[3])
    assert cols["ref"] == 0 and cols["forename"] == 1 and cols["preferred"] == 3 and cols["surname"] == 2
    assert cols["group"] == 6 and cols["fullname"] is None
    # the preferred name is used where one is filled in, otherwise the forename
    people = ci.build_rows(rows, 3, cols)
    assert [p["name"] for p in people[:2]] == ["Amira Khan", "Benny O'Neill"]


@test
def only_name_reference_and_group_are_kept():
    people = _people(PROSOLUTION_CSV, {"ref": 0, "forename": 1, "surname": 2, "fullname": None, "group": 6})
    assert people[0] == {"name": "Amira Khan", "ref": "S1234567", "group": "TLCOMP2A"}
    assert set(people[0]) == {"name", "ref", "group"}  # no email, no date of birth
    assert "@" not in repr(people)


@test
def capital_surnames_are_tidied_but_mixed_case_is_kept():
    assert ci.tidy_name("Ben O'NEILL") == "Ben O'Neill"
    assert ci.tidy_name("Chloe MCDONALD-SMITH") == "Chloe McDonald-Smith"
    assert ci.tidy_name("ZOË ÉLODIE") == "Zoë Élodie"
    assert ci.tidy_name("Jo McDonald") == "Jo McDonald"           # typed in mixed case - left alone
    assert ci.tidy_name("KHAN, Amira") == "Amira Khan"           # "SURNAME, Forename"
    assert ci.tidy_name("  de   Souza ") == "de Souza"


@test
def a_list_pasted_from_a_screen_works():
    pasted = "Learner ID\tLearner Name\tCourse\n10001\tZara Ahmed\tL3 IT\n10002\tTom Price\tL3 IT\n"
    rows = ci.read_text(pasted)
    assert ci.find_header_row(rows) == 0
    people = _people(pasted)
    assert people == [{"name": "Zara Ahmed", "ref": "10001", "group": "L3 IT"}, {"name": "Tom Price", "ref": "10002", "group": "L3 IT"}]


@test
def an_excel_export_is_read_with_numbers_kept_whole():
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.append(["Class list - TLCOMP2A"])
    sheet.append([])
    sheet.append(["Student No", "First Name", "Last Name", "Mobile"])
    sheet.append([20231234, "Ade", "OKAFOR", "07700900000"])
    buf = io.BytesIO()
    book.save(buf)
    rows = ci.read_table("ClassList.xlsx", buf.getvalue())
    header = ci.find_header_row(rows)
    people = ci.build_rows(rows, header, ci.guess_columns(rows[header]))
    assert people == [{"name": "Ade Okafor", "ref": "20231234", "group": ""}]  # not "20231234.0"
    try:
        ci.read_table("old.xls", b"x")
        assert False, "old .xls should be refused with a plain message"
    except ValueError as e:
        assert "xlsx" in str(e)


@test
def several_classes_in_one_file_can_be_told_apart():
    people = _people(PROSOLUTION_CSV, {"ref": 0, "forename": 1, "surname": 2, "fullname": None, "group": 6})
    assert ci.groups_in(people) == [("TLCOMP2A", 3), ("TLCOMP2B", 1)]


@test
def the_import_plan_adds_updates_reactivates_and_never_deletes():
    existing = [
        {"id": 1, "name": "Amira Khan", "ref": "S1234567", "active": True},     # same -> no change
        {"id": 2, "name": "Benjamin O'Neill", "ref": "s1234568", "active": True},  # name changed in ProSolution
        {"id": 3, "name": "Dan Old", "ref": "S0000001", "active": True},        # left the course
        {"id": 4, "name": "Chloe McDonald-Smith", "ref": "", "active": False},   # archived, matched by name
    ]
    people = _people(PROSOLUTION_CSV, {"ref": 0, "forename": 1, "surname": 2, "fullname": None, "group": 6})
    plan = ci.plan_import(existing, people)
    assert [p["id"] for p in plan["same"]] == [1]
    assert plan["update"][0]["id"] == 2 and plan["update"][0]["changes"] == {"name": "Ben O'Neill", "ref": "S1234568"}
    assert plan["reactivate"][0]["id"] == 4 and plan["reactivate"][0]["changes"] == {"ref": "S1234569"}
    assert plan["add"] == [] and plan["duplicates"] == 1 and plan["archive"] == []
    plan = ci.plan_import(existing, people, archive_missing=True)
    assert [p["id"] for p in plan["archive"]] == [3]  # archived (marks kept), only when asked


@test
def same_name_but_a_different_student_number_is_a_different_person():
    existing = [{"id": 9, "name": "Sam Lee", "ref": "A1", "active": True}]
    plan = ci.plan_import(existing, [{"name": "Sam Lee", "ref": "B2", "group": ""}])
    assert len(plan["add"]) == 1 and not plan["same"]


QUESTIONS = {"questions": [
    {"question": "What does a firewall do?", "choices": ["Filters network traffic", "Stores passwords", "Speeds up Wi-Fi", "Backs up files"],
     "answer": 0, "explanation": "It allows or blocks traffic using rules."},
    {"question": "Which is phishing?", "choices": ["A fake bank email", "", "A software update", "A fake bank email"], "answer": 0},
    {"question": "", "choices": ["a", "b"], "answer": 0},                      # no question -> dropped
    {"question": "Bad answer index", "choices": ["a", "b"], "answer": 5},       # dropped
    {"question": "What does a firewall do?", "choices": ["x", "y"], "answer": 1},  # repeat -> dropped
]}


@test
def quiz_questions_are_checked_and_the_right_answer_moves_around():
    questions, dropped = qz.clean_quiz(QUESTIONS, seed=3)
    assert len(questions) == 2 and dropped == 3
    assert questions[1]["choices"].count("A fake bank email") == 1 and "" not in questions[1]["choices"]
    for q in questions:
        assert q["choices"][q["answer"]] in ("Filters network traffic", "A fake bank email")
    positions = set()
    for seed in range(12):
        qs, _ = qz.clean_quiz(QUESTIONS, seed=seed)
        positions.add(qs[0]["answer"])
    assert len(positions) >= 3  # the AI's habit of putting the right answer first is undone


@test
def the_right_answer_never_reaches_a_phone_before_the_reveal():
    questions, _ = qz.clean_quiz(QUESTIONS, seed=1)
    screens = qz.screens_from_quiz(questions, 20, "Security basics")
    assert [s["kind"] for s in screens] == ["quiz_intro", "quiz_question", "quiz_question", "leaderboard"]
    hidden = qz.public_screen(screens[1], revealed=False)
    assert "answer" not in hidden["questions"][0] and "explanation" not in hidden["questions"][0]
    assert "answer" in screens[1]["questions"][0]  # the stored screen is untouched
    assert qz.public_screen(screens[1], revealed=True)["questions"][0]["answer"] == questions[0]["answer"]


@test
def points_reward_right_and_fast_answers():
    assert qz.points_for(False, 1, 20) == 0
    assert qz.points_for(True, 0, 20) == 1000
    assert qz.points_for(True, 10, 20) == 750
    assert qz.points_for(True, 20, 20) == 500 and qz.points_for(True, 25, 20) == 500  # within the grace period
    assert qz.is_open(21.5, 20, False) and not qz.is_open(23, 20, False) and not qz.is_open(1, 20, True)


@test
def the_leaderboard_ranks_ties_together():
    names = {1: "Amira", 2: "Ben", 3: "Chloe", 4: "Dan"}
    responses = [{"participant": 1, "points": 900, "correct": True}, {"participant": 2, "points": 900, "correct": True},
                 {"participant": 3, "points": 600, "correct": True}, {"participant": 3, "points": 0, "correct": False}]
    board = qz.leaderboard(responses, names)
    assert [(t["rank"], t["name"], t["points"]) for t in board] == [(1, "Amira", 900), (1, "Ben", 900), (3, "Chloe", 600), (4, "Dan", 0)]


@test
def a_quiz_typed_in_by_hand_is_checked_the_same_way():
    assert qz.clean_question({"question": "2+2?", "choices": ["4", "5", "", ""], "answer": "0"}) == \
        {"question": "2+2?", "choices": ["4", "5"], "answer": 0, "explanation": ""}
    assert qz.clean_question({"question": "2+2?", "choices": ["4", "", "", ""], "answer": 0}) is None   # one answer only
    assert qz.clean_question({"question": "2+2?", "choices": ["4", "5", "", ""], "answer": 2}) is None  # ticked a blank
    assert qz.clamp_seconds("3") == 5 and qz.clamp_seconds("999") == 120 and qz.clamp_seconds("x") == 20


@test
def the_ai_is_given_the_lessons_own_content():
    outline = {"lesson_title": "Network security", "slides": [
        {"type": "objectives", "objectives": ["Explain what a firewall does"]},
        {"type": "content", "title": "Firewalls", "key_points": ["Filter traffic using rules"]},
        {"type": "real_life_example", "title": "A school network", "scenario": "Blocks gaming sites."}]}
    prompt = qz.build_quiz_prompt(outline, 6)
    for text in ("Write 6", "Network security", "Explain what a firewall does", "Filter traffic using rules", "Blocks gaming sites"):
        assert text in prompt, text


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
