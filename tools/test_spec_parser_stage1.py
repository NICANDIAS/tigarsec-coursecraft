"""
Small automatic tests for the Stage 1 parser changes. No AI, no database, no PDFs needed.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_spec_parser_stage1.py
Expected last line: "ALL 16 TESTS PASSED"
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


@test
def ai_outcomes_sharing_one_code_are_kept_and_numbered():
    # Real case: AI labelled every learning outcome "LO". All but the first used to be dropped.
    def ai(**kwargs):
        if not kwargs.get("prompt", "").startswith("This excerpt is from"):
            raise RuntimeError("layout discovery fails")
        return {"units": [{"name": "Component 1", "learning_outcomes": [
            {"code": "LO", "title": "Cash flow", "criteria": [{"code": "C1", "description": "Construct cash flow forecasts"}]},
            {"code": "LO", "title": "Budgets", "criteria": [{"code": "C1", "description": "Explain budgets"}]},
            {"code": "LO", "title": "Cash flow", "criteria": [{"code": "C1", "description": "Construct cash flow forecasts"}]},
        ]}]}
    original = sp.complete_json
    sp.complete_json = ai
    try:
        result = sp.parse_spec_hybrid("Some business content\n" * 30, max_ai_calls=3)
    finally:
        sp.complete_json = original
    los = result["units"][0]["learning_outcomes"]
    assert [lo["title"] for lo in los] == ["Cash flow", "Budgets"], [lo["title"] for lo in los]  # true duplicate dropped, different title kept
    assert [lo["code"] for lo in los] == ["LO1", "LO2"], [lo["code"] for lo in los]
    assert any("numbered LO1" in n for n in result["quality"]["notes"])



@test
def a_unit_split_between_ai_excerpts_comes_back_whole():
    # Real case (A level Business, pages 8-12): the 2nd AI excerpt started part-way through
    # "Business finance", came back as a separate "Unknown unit" and was missed in the preview.
    prompts = []

    def ai(**kwargs):
        prompts.append(kwargs["prompt"])
        if len(prompts) == 1:
            return {"units": [{"name": "Business Opportunities and Functions", "learning_outcomes": [
                {"code": "LO", "title": "Enterprise", "criteria": [{"code": "1", "description": "Explain the meaning of enterprise"}]},
                {"code": "LO", "title": "Business finance", "criteria": [{"code": "C1", "description": "Explain the sources of finance"}]}]}]}
        return {"units": [{"name": "Unknown unit", "learning_outcomes": [
            {"code": "LO", "title": "Business finance", "criteria": [
                {"code": "C1", "description": "Analyse the advantages and disadvantages of each source of finance"},
                {"code": "C2", "description": "Explain the sources of finance"}]},
            {"code": "LO", "title": "Business revenue and costs", "criteria": [
                {"code": "C1", "description": "Explain what is meant by break-even"}]}]}]}

    text = "\n".join(f"Explain topic number {i} in some detail for learners" for i in range(150))  # 2 excerpts
    original = sp.complete_json
    sp.complete_json = ai
    try:
        named = sp.read_piece(text, "Component 1: Business Opportunities")
        prompts.clear()
        unnamed = sp.read_piece(text, "")
    finally:
        sp.complete_json = original
    assert len(prompts) == 2 and "previous excerpt ended with" in prompts[1] and "previous excerpt" not in prompts[0]
    for result in (named, unnamed):
        assert len(result["units"]) == 1, [u["name"] for u in result["units"]]
        los = result["units"][0]["learning_outcomes"]
        assert [lo["title"] for lo in los] == ["Enterprise", "Business finance", "Business revenue and costs"]
        finance = [c["description"] for c in los[1]["criteria"]]
        assert finance == ["Explain the sources of finance", "Analyse the advantages and disadvantages of each source of finance"], finance
        assert [c["code"] for c in los[1]["criteria"]] == ["C1", "C2"]
        assert any("joined back together" in n for n in result["notes"])
    assert named["units"][0]["name"] == "Component 1: Business Opportunities"
    assert unnamed["units"][0]["name"] == "Business Opportunities and Functions"


@test
def separate_named_units_from_ai_stay_separate_without_a_typed_name():
    def ai(**kwargs):
        return {"units": [{"name": "Unit 1: Alpha", "learning_outcomes": [{"code": "LO1", "title": "A", "criteria": [{"code": "1.1", "description": "x"}]}]},
                          {"name": "Unit 2: Beta", "learning_outcomes": [{"code": "LO1", "title": "B", "criteria": [{"code": "1.1", "description": "y"}]}]}]}
    original = sp.complete_json
    sp.complete_json = ai
    try:
        result = sp.read_piece("Some unit text here\n" * 20, "")
    finally:
        sp.complete_json = original
    assert [u["name"] for u in result["units"]] == ["Unit 1: Alpha", "Unit 2: Beta"]



@test
def a_renamed_continuation_is_joined_and_odd_codes_are_tidied():
    # Real case, 2nd try: the AI called the continued outcome "Business sources of finance" and
    # gave codes like "LO-revenue-costs". It is now told the exact last title and can flag it.
    prompts = []

    def ai(**kwargs):
        prompts.append(kwargs["prompt"])
        if len(prompts) == 1:
            return {"units": [{"name": "Unknown unit", "learning_outcomes": [
                {"code": "LO6", "title": "Business location", "criteria": [{"code": "1", "description": "Explain location factors"}]},
                {"code": "LO7", "title": "Business finance", "criteria": [{"code": "1", "description": "Explain the sources of finance"}]}]}]}
        return {"units": [{"name": "Unknown unit", "learning_outcomes": [
            {"code": "LO-sources-of-finance", "title": "Business sources of finance", "continues_previous": True, "criteria": [
                {"code": "a", "description": "Analyse each source of finance"},
                {"code": "b", "description": "Evaluate different sources of finance"}]},
            {"code": "LO-revenue-costs", "title": "Business revenue and costs", "criteria": [
                {"code": "x", "description": "Explain what is meant by break-even"}]}]}]}

    text = "\n".join(f"Explain topic number {i} in some detail for learners" for i in range(150))
    original = sp.complete_json
    sp.complete_json = ai
    try:
        result = sp.read_piece(text, "Component 1: Business Opportunities")
    finally:
        sp.complete_json = original
    assert 'titled "Business finance"' in prompts[1], prompts[1][:400]
    los = result["units"][0]["learning_outcomes"]
    assert [lo["title"] for lo in los] == ["Business location", "Business finance", "Business revenue and costs"], [lo["title"] for lo in los]
    assert [lo["code"] for lo in los] == ["LO6", "LO7", "LO8"] or [lo["code"] for lo in los] == ["LO1", "LO2", "LO3"], [lo["code"] for lo in los]
    assert [c["code"] for c in los[1]["criteria"]] == ["C1", "C2", "C3"], los[1]["criteria"]
    assert all("continues_previous" not in lo for lo in los)



@test
def a_failed_ai_read_says_why_in_plain_english():
    def ai(**kwargs):
        raise RuntimeError("Error code: 429 - rate_limit_error: too many requests")
    original = sp.complete_json
    sp.complete_json = ai
    try:
        result = sp.read_piece("\n".join(f"Explain topic {i} for learners" for i in range(20)), "Component 2")
    finally:
        sp.complete_json = original
    why = [n for n in result["notes"] if n.startswith("Why:")]
    assert why and "busy or its usage limit" in why[0], result["notes"]
    assert "cut off" not in sp.explain_ai_error("x") and "too long" in sp.explain_ai_error("hit the max_tokens limit")



@test
def a_piece_whose_answer_is_too_long_is_read_in_halves():
    # Real case (A level pages 13-20): many short criteria per page, so the AI's answer for a
    # 6,000-character piece was cut off ("max_tokens") twice and the piece was lost.
    import re as _re
    topics = [f"Topic{t}" for t in range(6)]
    text = "\n".join(f"{topic} Explain point {n} about {topic.lower()} for learners" for topic in topics for n in range(25))

    def ai(**kwargs):
        excerpt = kwargs["prompt"].split(":\n\n", 2)[-1].split("\n\n", 1)[-1]
        if len(excerpt) > 2500:
            raise ValueError("[Provider used: anthropic] The response was cut off before it finished, because it hit the max_tokens limit")
        los = {}
        for line in excerpt.splitlines():
            m = _re.match(r"(Topic\d) (Explain point \d+ .*)", line)
            if m:
                los.setdefault(m.group(1), []).append({"code": "x", "description": m.group(2)})
        return {"units": [{"name": "Unknown unit", "learning_outcomes": [
            {"code": "LO", "title": k, "criteria": v} for k, v in los.items()]}]}

    original = sp.complete_json
    sp.complete_json = ai
    try:
        result = sp.read_piece(text, "Component 1: Business Functions", max_ai_calls=20)
    finally:
        sp.complete_json = original
    los = result["units"][0]["learning_outcomes"]
    assert [lo["title"] for lo in los] == topics, [lo["title"] for lo in los]
    assert all(len(lo["criteria"]) == 25 for lo in los), [len(lo["criteria"]) for lo in los]
    assert not any("could not be read" in n for n in result["notes"]), result["notes"]


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
