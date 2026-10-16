"""
Tests for Stage 9 - last lesson's live results shape the next lesson's PowerPoint.
No AI, no database.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_stage9.py
Expected last line: "ALL 6 TESTS PASSED"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import class_feedback as cf  # noqa: E402
from app import quiz as qz  # noqa: E402
from app.live_activities import _number, CONFIDENCE  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


QUESTIONS = [
    {"question": "What does a firewall do?", "choices": ["Filters network traffic", "Stores passwords", "Speeds up Wi-Fi"], "answer": 0},
    {"question": "Which port does HTTPS use?", "choices": ["80", "443"], "answer": 1},
]
SCREENS = _number(qz.screens_from_quiz(QUESTIONS, 20, "Network security") +
                  [{"kind": "objectives_review", "title": "How confident are you?", "body": [], "questions": [
                      {"prompt": "Explain what a firewall does", "kind": "confidence", "choices": CONFIDENCE}]}])


def answers(screen, key, choices, names="ABCDEFGHIJ"):
    return [{"question_key": key, "choice": c, "text": None, "screen": screen, "name": names[i]} for i, c in enumerate(choices)]


def keys():
    return [q["key"] for s in SCREENS for q in s["questions"]]


@test
def a_question_most_of_the_class_got_wrong_is_a_gap_with_its_misconception():
    k = keys()
    responses = answers(1, k[0], [1, 1, 1, 0, 2]) + answers(2, k[1], [1, 1, 1, 1, 0])  # 20% right / 80% right
    gaps = cf.learning_gaps(SCREENS, responses)
    assert [g["text"] for g in gaps] == ["What does a firewall do?"]
    g = gaps[0]
    assert g["percent"] == 20 and g["right"] == "Filters network traffic" and g["common_wrong"] == "Stores passwords"


@test
def an_objective_the_class_is_unsure_about_is_a_gap():
    k = keys()
    gaps = cf.learning_gaps(SCREENS, answers(4, k[2], [0, 1, 1, 2]))  # 3 of 4 not sure
    assert len(gaps) == 1 and gaps[0]["kind"] == "confidence" and gaps[0]["percent"] == 75
    assert cf.learning_gaps(SCREENS, answers(4, k[2], [2, 2, 2, 1])) == []  # mostly "Got it" -> fine


@test
def too_few_answers_is_not_enough_evidence():
    k = keys()
    assert cf.learning_gaps(SCREENS, answers(1, k[0], [1, 1])) == []


@test
def the_text_for_the_ai_has_no_student_names():
    k = keys()
    responses = answers(1, k[0], [1, 1, 1, 0], names=["Amira", "Ben", "Chloe", "Dan"])
    block = cf.prompt_block(cf.learning_gaps(SCREENS, responses))
    assert block.startswith(cf.HEADER) and "only 25% answered correctly" in block and '"Stores passwords"' in block
    for name in ("Amira", "Ben", "Chloe", "Dan"):
        assert name not in block


@test
def the_results_are_added_to_the_lecturers_own_text_and_replaced_not_stacked():
    k = keys()
    gaps = cf.learning_gaps(SCREENS, answers(1, k[0], [1, 1, 1, 0]))
    once = cf.with_revisit("Last lesson covered firewalls.", gaps)
    assert once.startswith("Last lesson covered firewalls.") and cf.HEADER in once
    twice = cf.with_revisit(once, gaps)
    assert twice.count(cf.HEADER) == 1 and twice == once
    assert cf.with_revisit("Just text", []) == "Just text"


@test
def the_lesson_writer_is_told_how_to_revisit():
    from app.pptx_generator import LESSON_OUTLINE_SYSTEM_PROMPT as prompt
    assert "RESULTS FROM LAST LESSON'S LIVE CHECKS" in prompt and '"Revisit: <short topic>"' in prompt
    assert "why the most common wrong answer is wrong" in prompt and cf.HEADER.startswith("RESULTS FROM LAST LESSON'S LIVE CHECKS")


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
