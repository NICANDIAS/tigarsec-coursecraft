"""
Pure logic for scoring a teacher's fit for a unit as a RANKING TIE-BREAKER only - never a
pass/fail gate. Qualification status (TeacherUnitQualification.status == "confirmed") is
the only real eligibility gate, checked entirely separately in timetabling.py; this module
never sees or affects it. Deliberately zero DB dependency, matching coverage.py and
timetable_generator.py, so it can be tested thoroughly with mock data before ever touching
a real route.

Combines years of experience, an admin's own discretionary rating, CPD history, and IV
outcome history - every factor optional, every factor weighted per the college's own
SkillRankingWeights (a real, editable policy table, not a fixed formula - the same
principle already used for TeachingHoursBand, since different colleges will reasonably
weight these factors differently).
"""
import datetime

IV_OUTCOME_SCORES = {"no_concerns": 1.0, "minor_action": 0.0, "significant_concern": -1.0}


def _recency_decay(event_date, today, half_life_years: float) -> float:
    """
    Older evidence counts for less, gradually - never vanishing at an arbitrary cutoff. A
    half-life model: an event exactly half_life_years old counts for half as much as a
    brand new one, one two half-lives old counts for a quarter, and so on. A half-life of
    0 (or less) is treated as "no decay" rather than a division error.
    """
    if half_life_years <= 0:
        return 1.0
    age_years = (today - event_date).days / 365.25
    if age_years <= 0:
        return 1.0
    return 0.5 ** (age_years / half_life_years)


def _cpd_score(cpd_records: list, today, half_life_years: float) -> float:
    """Sum of each entry's recency-decayed weight - more recent, more numerous CPD scores
    higher, but a handful of old entries never permanently inflates a score the way a flat
    count would."""
    return sum(_recency_decay(r["date"], today, half_life_years) for r in cpd_records)


def _iv_score(iv_outcomes: list, today, half_life_years: float) -> float:
    """Each outcome contributes its own base score (no_concerns positive, a significant
    concern negative, minor action neutral), decayed by recency - an old, since-resolved
    concern shouldn't permanently follow someone, but a recent one should still weigh in."""
    total = 0.0
    for o in iv_outcomes:
        base = IV_OUTCOME_SCORES.get(o["outcome_level"], 0.0)
        total += base * _recency_decay(o["date"], today, half_life_years)
    return total


def score_teacher_for_unit(years_experience, admin_rating, cpd_records: list, iv_outcomes: list, weights: dict, today=None) -> float:
    """
    Returns a single comparable score for ranking purposes only. Every input is optional;
    a factor with no data simply contributes nothing, so a college using only some
    factors gets an honest score based on what it actually has - never a penalised or
    fabricated one for what it doesn't. weights is a plain dict matching
    SkillRankingWeights' own field names, kept as a dict rather than the ORM row itself so
    this module's zero-DB-dependency design stays intact; a college that sets a weight to
    0 has that factor make no contribution at all, exactly as they intended.

    cpd_records / iv_outcomes are lists of plain dicts with at least a "date" key
    (iv_outcomes also needs "outcome_level") - not ORM rows, for the same reason.
    """
    if today is None:
        today = datetime.date.today()

    score = 0.0
    if years_experience is not None:
        score += weights.get("weight_experience", 1.0) * years_experience
    if admin_rating is not None:
        score += weights.get("weight_admin_rating", 1.0) * admin_rating
    if cpd_records:
        score += weights.get("weight_cpd", 1.0) * _cpd_score(cpd_records, today, weights.get("cpd_half_life_years", 3.0))
    if iv_outcomes:
        score += weights.get("weight_iv", 1.0) * _iv_score(iv_outcomes, today, weights.get("iv_half_life_years", 2.0))
    return score


def has_any_ranking_data(years_experience, admin_rating, cpd_records: list, iv_outcomes: list) -> bool:
    """
    Whether there's genuinely anything to rank this teacher on at all. Lets a caller tell
    "this teacher scored 0 because their history has no negatives" apart from "this
    teacher has no ranking data whatsoever" - the latter should fall back to today's
    load-balancing order rather than be treated as a real, deliberate score of 0.
    """
    return bool(years_experience is not None or admin_rating is not None or cpd_records or iv_outcomes)
