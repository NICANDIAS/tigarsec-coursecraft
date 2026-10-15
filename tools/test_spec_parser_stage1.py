"""
Small automatic tests for the Stage 1 parser changes. No AI, no database, no PDFs needed.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_spec_parser_stage1.py
Expected last line: "ALL 10 TESTS PASSED"
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# AI switched off: any accidental AI call raises, which these tests treat as "AI section failed".
_stub = types.ModuleType("app.llm_gateway")


def _no_ai(**_kwargs):
    raise RuntimeError("AI disabled in tests")


_stub.complete_json = _no_ai
sys.modules["app.llm_gateway"] = _stub

from app import spec_parser as sp  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def chunks_never_exceed_the_cap():
    # One giant "paragraph" with no blank lines - the exact shape that broke the Media spec.
    giant = "\n".join(f"line {i} " + "x" * 80 for i in range(2000))  # ~180,000 chars
    chunks = sp.chunk_text(giant)
    assert max(len(c) for c in chunks) <= sp.CHUNK_CHAR_SIZE, max(len(c) for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == giant.replace("\n", ""), "text was lost while splitting"


@test
def a_single_huge_line_is_still_split():
    chunks = sp.chunk_text("y" * 20000)
    assert max(len(c) for c in chunks) <= sp.CHUNK_CHAR_SIZE
    assert sum(len(c) for c in chunks) == 20000


@test
def table_copies_are_removed_but_normal_text_stays():
    text = (
        "1.1.1 Know the definition of computational thinking.\n"
        "1.1.2 Know when to use it.\n"
        "\n"
        "[Table 1 on this page, reconstructed from the document's actual grid for accuracy]\n"
        "1.1.1 |  | Know the definition of computational thinking. |\n"
        "1.1.2 |  | Know when to use it. |\n"
        "1.1.3 Next page starts here.\n"
    )
    clean, removed = sp.strip_table_copies(text)
    assert removed == 1
    assert "|" not in clean
    assert clean.count("1.1.1") == 1 and clean.count("1.1.2") == 1
    assert "1.1.3 Next page starts here." in clean, "the next page's first line must be kept"


@test
def text_without_table_copies_is_unchanged():
    text = "Unit 1: Something\nLearning aim A: x\nP1 Explain things\n"
    clean, removed = sp.strip_table_copies(text)
    assert removed == 0 and clean == text


@test
def repeated_codes_inside_one_lo_are_dropped_first_copy_kept():
    units = [{"name": "U", "learning_outcomes": [{"code": "1.1", "title": "t", "criteria": [
        {"code": "1.1.1", "description": "first"},
        {"code": "1.1.2", "description": "b"},
        {"code": "1.1.1", "description": "second copy"},
    ]}]}]
    dropped = sp.dedupe_criteria(units)
    crit = units[0]["learning_outcomes"][0]["criteria"]
    assert dropped == 1
    assert [c["code"] for c in crit] == ["1.1.1", "1.1.2"]
    assert crit[0]["description"] == "first"


@test
def nothing_found_is_failed_not_parsed():
    q = sp.assess_parse_quality({"units": [], "failed_sections": ["whole document"], "failed_section_chars": 100}, 100)
    assert q["verdict"] == "failed", q


@test
def media_style_result_is_needs_review():
    # Recreates what the live Media spec showed: one "Unknown unit" + the whole document skipped.
    result = {
        "units": [{"name": "Unknown unit", "learning_outcomes": [
            {"code": "Unknown LO", "title": "Assessment criteria mark-band grid",
             "criteria": [{"code": "AC1", "description": "Informing ideas ..."}]}]}],
        "failed_sections": ["whole document (no 'Unit N' headers found)"],
        "failed_section_chars": 94675,
        "budget_stopped_early": False,
    }
    q = sp.assess_parse_quality(result, 140606)
    assert q["verdict"] == "needs_review", q
    assert q["text_skipped_percent"] == 67.3, q["text_skipped_percent"]
    assert any("Unknown unit" in p for p in q["problems"]), q["problems"]


@test
def clean_rule_based_result_is_good():
    result = {"units": [{"name": "Unit 1: X", "learning_outcomes": [
        {"code": "LO1", "title": "Know things", "criteria": [{"code": "P1", "description": "Explain"}]}]}]}
    q = sp.assess_parse_quality(result, 1000)
    assert q["verdict"] == "good" and q["problems"] == [], q


@test
def full_parse_never_reports_parsed_for_a_document_it_cannot_read():
    # A document with no recognisable structure; AI is off, so every AI attempt fails.
    result = sp.parse_spec_hybrid("Some intro text.\n" * 50, max_ai_calls=3)
    assert result["units"] == []
    assert result["quality"]["verdict"] == "failed", result["quality"]
    assert result["quality"]["text_skipped_chars"] > 0


@test
def partly_read_document_reports_only_the_unread_part():
    # AI reads the first piece, then fails on the rest: the report must NOT say 100% skipped.
    calls = {"n": 0}

    def half_working_ai(**kwargs):
        if not kwargs.get("prompt", "").startswith("This excerpt is from"):
            raise RuntimeError("layout discovery fails")  # the one-off "work out the layout" call
        calls["n"] += 1
        if calls["n"] <= 2:  # the first reading call (and its retry slot) works
            return {"units": [{"name": "Component 1", "learning_outcomes": [
                {"code": "LO1", "title": "Enterprise", "criteria": [{"code": "1", "description": "Explain enterprise"}]}]}]}
        raise RuntimeError("AI failed")

    original = sp.complete_json
    sp.complete_json = half_working_ai
    try:
        text = "\n".join(f"Explain topic number {i} in some detail for learners" for i in range(800))  # ~4 chunks
        result = sp.parse_spec_hybrid(text, max_ai_calls=15)
    finally:
        sp.complete_json = original
    pct = result["quality"]["text_skipped_percent"]
    assert result["units"] and 0 < pct < 100, pct
    assert result["quality"]["verdict"] == "needs_review"


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
