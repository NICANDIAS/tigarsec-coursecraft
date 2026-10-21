"""
Tests for Stage 14 - plan the term, and homework mode. No AI, no database.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_stage14.py
Expected last line: "ALL 14 TESTS PASSED"
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import term_plan as tp  # noqa: E402
from app import homework as hw  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


WEEKS = [{"week": 1, "title": "Induction"}, {"week": 2, "title": "Threats"}, {"week": 3, "title": "Risk"},
         {"week": 5, "title": "Scanning"}, {"week": 6, "title": "Pen testing"}, {"week": 7, "title": "Firewalls"}]


# ------------------------------------------------------------------ plan the term
@test
def every_week_is_listed_and_half_term_shows_as_a_gap():
    rows = tp.plan_rows(WEEKS, 2, {1})
    assert [r["week"] for r in rows] == [1, 2, 3, None, 5, 6, 7]
    assert rows[3]["gap"] and rows[0]["made"] and rows[0]["past"] and rows[1]["now"] and not rows[2]["made"]


@test
def with_a_college_template_the_real_dates_and_half_term_are_shown():
    """The printed scheme's calendar: weeks numbered 1, 2, 3... but half term sits between 3 and 4."""
    weeks = [{"week": n, "title": f"Topic {n}"} for n in range(1, 7)]
    d = lambda m, day: datetime.date(2026, m, day)
    dates = {1: d(9, 22), 2: d(9, 29), 3: d(10, 6), 4: d(10, 13), 5: d(10, 20), 6: d(11, 3)}
    breaks = [{"after_week": 5, "date": d(10, 27), "label": "Half Term"}]
    rows = tp.plan_rows(weeks, 5, {1, 2, 3, 4, 6}, dates, breaks, today=d(10, 21))
    assert [r["week"] for r in rows] == [1, 2, 3, 4, 5, None, 6]
    assert rows[5] == {"week": None, "gap": True, "label": "Half Term", "date": d(10, 27)}
    assert rows[4]["now"] and rows[4]["date"] == d(10, 20) and not rows[4]["past"] and rows[3]["past"]
    rows = tp.plan_rows(weeks, 5, {1, 2, 3, 4}, dates, breaks, today=d(10, 21))
    assert tp.quick_pick(rows, 5, "half") == [5]            # stops at the real half term
    assert tp.quick_pick(rows, 5, "rest") == [5, 6]


@test
def quick_picks_skip_made_past_and_gap_weeks():
    rows = tp.plan_rows(WEEKS, 2, {1, 6})
    assert tp.quick_pick(rows, 2, "next4") == [2, 3, 5, 7]
    assert tp.quick_pick(rows, 2, "half") == [2, 3]          # stops at the half-term gap
    assert tp.quick_pick(rows, 2, "rest") == [2, 3, 5, 7]    # week 6 already made
    assert tp.quick_pick(rows, None, "half") == [2, 3]       # no date yet: from the start, made weeks skipped


@test
def lesson_length_and_quiz_size_stay_sensible():
    assert tp.clamp_minutes("1.5") == 90 and tp.clamp_minutes("0.1") == 30 and tp.clamp_minutes("99") == 360
    assert tp.clamp_minutes("abc") == 60
    assert tp.clamp_quiz("8") == 8 and tp.clamp_quiz("1") == 3 and tp.clamp_quiz("50") == 15 and tp.clamp_quiz(None) == 8


@test
def each_week_and_the_whole_plan_report_progress():
    assert tp.item_status("queued", None, True) == "waiting"
    assert tp.item_status("generating", None, True) == "working"
    assert tp.item_status("ready", "generating", True) == "working"
    assert tp.item_status("ready", None, False) == "ready" and tp.item_status("ready", "ready", True) == "ready"
    assert tp.item_status("ready", "failed", True) == "failed" and tp.item_status(None, None, True) == "gone"
    p = tp.progress(["ready", "failed", "working", "waiting", "gone"])
    assert p == {"done": 2, "total": 4, "ready": 1, "failed": 1, "pct": 50, "finished": False}
    assert tp.estimate_minutes(4, True, False) == (12, 20)


@test
def the_template_rows_become_the_schemes_dates_and_breaks():
    from types import SimpleNamespace
    from app import sow_calendar as sc
    d = lambda m, day: datetime.date(2026, m, day)
    rows = [{"date": d(9, 8), "available": False, "label": "Induction"},      # before week 1 - not a break
            {"date": d(9, 15), "available": False, "label": "Enrichment"},
            {"date": d(9, 22), "available": True, "label": ""}, {"date": d(9, 29), "available": True, "label": ""},
            {"date": d(10, 27), "available": False, "label": "Half Term"},
            {"date": d(11, 3), "available": True, "label": ""}, {"date": d(11, 10), "available": True, "label": ""}]
    sc._template_rows = lambda path, labels: rows
    sow = SimpleNamespace(format_sample_file_path="/x/template.docx", append_eligible_labels=None,
                          weeks_json=[{"week": 1}, {"week": 2}, {"week": 3}])
    cal = sc.scheme_calendar(sow)
    assert cal["from_template"] and cal["week_dates"] == {1: d(9, 22), 2: d(9, 29), 3: d(11, 3)}
    assert cal["breaks"] == [{"date": d(10, 27), "label": "Half Term", "after_week": 2}]
    assert sc.scheme_calendar(SimpleNamespace(format_sample_file_path=None, weeks_json=[{"week": 1}]))["from_template"] is False


# ------------------------------------------------------------------ homework
CLASS = [{"id": 1, "name": "Amira Khan", "ref": "S100"}, {"id": 2, "name": "Benjamin Tunde Okafor", "ref": "S200"},
         {"id": 3, "name": "Zoë O'Neill", "ref": "S300"}, {"id": 4, "name": "Sam Lee", "ref": "S400"},
         {"id": 5, "name": "Sam Lee", "ref": "S401"}]


@test
def a_student_is_found_however_they_type_their_name():
    assert hw.match_student("  amira   KHAN ", CLASS) == ("ok", 1)
    assert hw.match_student("Benjamin Okafor", CLASS) == ("ok", 2)        # first + last name is enough
    assert hw.match_student("Zoe O'Neill", CLASS) == ("ok", 3)            # accents don't matter
    assert hw.match_student("zoe o neill", CLASS) == ("ok", 3) and hw.match_student("Zoe ONeill", CLASS) == ("ok", 3)
    assert hw.match_student("Zoe O\u2019Neill", CLASS) == ("ok", 3)  # the curly apostrophe phones type


@test
def two_students_with_one_name_are_told_apart_by_reference():
    assert hw.match_student("Sam Lee", CLASS) == ("many", None)
    assert hw.match_student("Sam Lee", CLASS, "s401") == ("ok", 5)


@test
def nobody_gets_in_with_a_wrong_or_partial_name():
    assert hw.match_student("Amira", CLASS) == ("none", None)            # first name alone is not enough
    assert hw.match_student("Ben Smith", CLASS) == ("none", None)
    assert hw.match_student("", CLASS) == ("none", None)
    assert hw.match_student("Ben Okafor", CLASS, "S100") == ("none", None)  # a reference that isn't theirs


QUESTIONS = [{"question": "HTTPS port?", "choices": ["80", "443"], "answer": 1, "explanation": "443 is HTTPS."},
             {"question": "Star topology centre?", "choices": ["switch", "ring"], "answer": 0, "explanation": ""},
             {"question": "CVE stands for?", "choices": ["a", "b", "c"], "answer": 2, "explanation": ""}]


@test
def homework_is_marked_and_unanswered_questions_count_as_wrong():
    r = hw.mark(QUESTIONS, {"0": 1, "1": 1})
    assert r == {"score": 1, "total": 3, "right": [True, False, None]}
    assert hw.clean_choice("2", QUESTIONS[2]) == 2 and hw.clean_choice("5", QUESTIONS[0]) is None and hw.clean_choice("x", QUESTIONS[0]) is None


@test
def the_lecturer_sees_how_the_class_did_on_each_question():
    stats = hw.question_stats(QUESTIONS, [{"0": 1, "1": 1}, {"0": 0, "1": 1}, {"0": 1}])
    assert stats[0]["pct_right"] == 67 and stats[0]["common_wrong"] == "80"
    assert stats[1]["pct_right"] == 0 and stats[1]["common_wrong"] == "ring"
    assert stats[2]["pct_right"] is None and stats[2]["answered"] == 0


@test
def due_dates_are_read_as_uk_times_and_lateness_is_noticed():
    due = hw.parse_due("2026-10-24", "17:00")
    assert due == datetime.datetime(2026, 10, 24, 17, 0)
    assert hw.parse_due("2026-10-24") == datetime.datetime(2026, 10, 24, 23, 59)
    assert hw.parse_due("") is None and hw.parse_due("24/10/2026") is None
    assert hw.is_late(due, due + datetime.timedelta(minutes=1)) and not hw.is_late(due, due) and not hw.is_late(None, due)
    assert hw.due_text(due, now=datetime.datetime(2026, 10, 20)) == "Due Sat 24 Oct, 17:00"
    assert hw.due_text(due, now=datetime.datetime(2026, 10, 25)).startswith("Was due")


@test
def results_show_answers_only_when_the_lecturer_allows():
    shown = hw.review(QUESTIONS, {"0": 0}, True)
    assert shown[0] == {"question": "HTTPS port?", "picked": "80", "right": False, "answer": "443", "explanation": "443 is HTTPS."}
    assert shown[1]["picked"] is None and shown[1]["right"] is None
    hidden = hw.review(QUESTIONS, {"0": 0}, False)
    assert "answer" not in hidden[0] and "explanation" not in hidden[0]


@test
def uk_time_is_used_not_the_servers_utc_clock():
    now = hw.local_now()
    assert now.tzinfo is None and abs((now - datetime.datetime.now()).total_seconds()) < 3 * 3600


if __name__ == "__main__":
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e!r}")
    if failed:
        print(f"\n{failed} OF {len(TESTS)} TESTS FAILED")
        sys.exit(1)
    print(f"\nALL {len(TESTS)} TESTS PASSED")
