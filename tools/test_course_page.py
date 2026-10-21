"""
Tests for the course page redesign's "This week" card (app/course_overview.py). No AI, no database.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_course_page.py
Expected last line: "ALL 8 TESTS PASSED"
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.course_overview import this_week, week_number, current_week  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


START = datetime.date(2026, 9, 7)  # a Monday
WEEKS = [{"week": 1, "title": "Induction"}, {"week": 2, "title": "Threats"}, {"week": 4, "title": "Risk"}, {"week": 5, "title": "Scanning"}]


def sow(**k):
    base = {"id": 5, "title": "Autumn", "status": "ready", "start_date": START, "weeks": WEEKS}
    base.update(k)
    return base


@test
def week_one_is_the_first_seven_days():
    assert week_number(START, START) == 1 and week_number(START, START + datetime.timedelta(days=6)) == 1
    assert week_number(START, START + datetime.timedelta(days=7)) == 2 and week_number(None, START) is None


@test
def this_week_follows_the_calendar_and_shows_next_week():
    tw = this_week([sow()], None, [], START + datetime.timedelta(days=8))
    assert tw["state"] == "teaching" and tw["week"] == 2 and tw["title"] == "Threats" and tw["total"] == 5
    assert tw["next_title"] == "Risk" and tw["deck"] is None


@test
def a_week_the_plan_skips_shows_the_next_planned_week():
    tw = this_week([sow()], None, [], START + datetime.timedelta(days=15))  # week 3 = half term, not in the plan
    assert tw["week"] == 4 and tw["title"] == "Risk"


@test
def the_lesson_made_from_this_week_is_found():
    decks = [{"id": 9, "status": "ready", "source_sow_id": 5, "source_week": 2}, {"id": 8, "status": "ready", "source_sow_id": 6, "source_week": 2}]
    assert this_week([sow()], None, decks, START + datetime.timedelta(days=8))["deck"]["id"] == 9


@test
def before_the_start_after_the_end_and_with_no_date():
    before = this_week([sow()], None, [], START - datetime.timedelta(days=3))
    assert before["state"] == "before" and before["days_to_start"] == 3 and before["week"] == 1
    assert this_week([sow()], None, [], START + datetime.timedelta(days=60))["state"] == "finished"
    tw = this_week([sow(start_date=None)], None, [], START)
    assert tw["state"] == "no_date" and tw["week"] == 1
    assert this_week([sow(start_date=None)], START, [], START + datetime.timedelta(days=8))["week"] == 2  # falls back to the course start


@test
def no_ready_scheme_means_no_card():
    assert this_week([], START, [], START) is None
    assert this_week([sow(status="generating")], START, [], START) is None
    assert this_week([sow(weeks=[])], START, [], START) is None


@test
def the_college_calendar_beats_counting_weeks_from_the_start_date():
    """Weeks 1-5 of the scheme, but two induction weeks before them and half term after week 5."""
    d = lambda m, day: datetime.date(2026, m, day)
    dates = {1: d(9, 22), 2: d(9, 29), 3: d(10, 6), 4: d(10, 13), 5: d(10, 20), 6: d(11, 3)}
    start = d(9, 8)  # counting from here would say week 7 on 20 Oct
    assert week_number(start, d(10, 21)) == 7
    now = current_week(list(dates), start, d(10, 21), dates)
    assert now["state"] == "teaching" and now["week"] == 5 and now["source"] == "calendar"
    brk = current_week(list(dates), start, d(10, 28), dates)
    assert brk["state"] == "break" and brk["week"] == 6 and brk["days_to_start"] == 6
    assert current_week(list(dates), start, d(9, 15), dates)["state"] == "before"
    tw = this_week([sow(weeks=[{"week": n, "title": f"T{n}"} for n in range(1, 7)], week_dates=dates, start_date=start)], None, [], d(10, 28))
    assert tw["state"] == "break" and tw["week"] == 6 and tw["title"] == "T6"


@test
def the_lecturers_correction_wins_and_moves_on_each_week():
    anchor = {"week": 5, "monday": "2026-10-19"}
    weeks = list(range(1, 15))
    assert current_week(weeks, START, datetime.date(2026, 10, 21), None, anchor) == \
        {"state": "teaching", "week": 5, "days_to_start": 0, "source": "corrected"}
    assert current_week(weeks, START, datetime.date(2026, 10, 26), None, anchor)["week"] == 6
    assert current_week(weeks, START, datetime.date(2026, 10, 21), {1: START}, anchor)["week"] == 5  # beats a template too


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
