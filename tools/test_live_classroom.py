"""
Tests for live-lesson classroom tools: quick polls ("Poll it"), small groups, and students' own
notes. No AI, no database.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_live_classroom.py
Expected last line: "ALL 6 TESTS PASSED"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import live_activities as la  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def quick_polls_come_in_ready_made_kinds():
    yes = la.poll_screen("Ready to move on?", "yesno")
    assert yes["kind"] == "poll" and yes["questions"][0]["choices"] == ["Yes", "No", "Not sure"]
    assert la.poll_screen("", "abcd")["questions"][0]["prompt"] == "Look at the board and answer"  # asked out loud
    own = la.poll_screen("Best protocol?", "custom", "HTTPS\nSSH\n\nHTTPS\nFTP")
    assert own["questions"][0]["choices"] == ["HTTPS", "SSH", "FTP"]  # blanks and repeats dropped
    assert la.poll_screen("x", "custom", "only one") is None
    assert la.poll_screen("One word for today?", "text")["questions"][0]["kind"] == "text"
    assert yes["questions"][0]["key"] != la.poll_screen("again", "yesno")["questions"][0]["key"]  # never clash


@test
def poll_results_are_counted_like_a_vote():
    screen = la._number([la.poll_screen("Ready?", "yesno")])[0]
    key = screen["questions"][0]["key"]
    t = la.tally(screen, [{"question_key": key, "choice": 0, "name": "A"}, {"question_key": key, "choice": 0, "name": "B"},
                          {"question_key": key, "choice": 2, "name": "C"}])
    assert t[key]["counts"] == [2, 0, 1]


@test
def groups_are_random_even_and_nobody_is_alone():
    for n in range(2, 30):
        for size in (2, 3, 4, 5):
            groups = la.make_groups(list(range(n)), size, seed=n)
            sizes = {}
            for g in groups.values():
                sizes[g] = sizes.get(g, 0) + 1
            assert len(groups) == n and min(sizes.values()) >= 2 or n < 2, (n, size, sizes)
            assert max(sizes.values()) - min(sizes.values()) <= 1, (n, size, sizes)


@test
def a_latecomer_joins_the_smallest_group():
    assert la.smallest_group({1: 1, 2: 1, 3: 2}) == 2
    assert la.smallest_group({}) == 1


@test
def students_notes_have_the_slides_and_their_own_answers():
    outline = {"slides": [{"type": "content", "title": "What is a CVE?", "guiding_question": "How are vulnerabilities tracked?",
                           "key_points": ["A CVE is a public ID"], "check_question": "What does CVE stand for?"},
                          {"type": "objectives_review", "objectives_check": [{"objective": "Explain CVEs"}]}]}
    screens = la.screens_from_deck(outline)
    keys = [q["key"] for s in screens for q in s["questions"]]
    notes = la.student_notes("CVEs", screens, {keys[0]: {"text": "Public IDs", "choice": None, "image": True},
                                               keys[2]: {"text": None, "choice": 2, "image": False}}, "20 Oct 2026")
    assert notes.startswith("CVEs\nMy lesson notes - 20 Oct 2026")
    assert "  - A CVE is a public ID" in notes and "My answer: Public IDs (photo sent)" in notes
    assert "Q: What does CVE stand for?\n  My answer: (not answered)" in notes and "My answer: Got it" in notes


@test
def quiz_answers_in_notes_say_whether_they_were_right():
    from app import quiz as qz
    screens = la._number(qz.screens_from_quiz([{"question": "2+2?", "choices": ["3", "4"], "answer": 1, "explanation": ""}], 20, "Maths"))
    key = screens[1]["questions"][0]["key"]
    assert "My answer: 3 - the answer was: 4" in la.student_notes("Quiz", screens, {key: {"choice": 0}})
    assert "My answer: 4 - right" in la.student_notes("Quiz", screens, {key: {"choice": 1}})


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
