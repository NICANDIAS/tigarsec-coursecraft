from sqlalchemy.orm import Session

from .models import TimetablingSettings, SkillRankingWeights

_DEFAULT_SETTINGS = TimetablingSettings(id=1, working_day_start="09:00", working_day_end="17:00", min_break_minutes=15, break_after_hours=3.0)
_DEFAULT_SKILL_WEIGHTS = SkillRankingWeights(id=1, weight_experience=1.0, weight_admin_rating=1.0, weight_cpd=1.0, weight_iv=1.0, cpd_half_life_years=3.0, iv_half_life_years=2.0)


def get_timetabling_settings(db: Session) -> TimetablingSettings:
    """
    Gets the single settings row (always id=1), creating it with sensible defaults
    (09:00-17:00, no fixed lunch block, a 15-minute break for sessions of 3+ hours) if it
    doesn't exist yet - same reasoning and same defensive pattern as get_system_settings in
    system_settings.py: fall back to safe in-memory defaults rather than raising, so a
    missing table can never become a new way to lock an admin out of the page that would
    let them fix it.

    Also backfills break_after_hours if it's ever found NULL on an existing row -
    confirmed as a real, live bug: adding this column later to a table that already had a
    settings row left that row's value as NULL (a bare ALTER TABLE ADD COLUMN with no
    DEFAULT does this in SQLite regardless of the model's own declared default), which
    silently disabled the whole mid-session break feature rather than using the intended
    3-hour default - the generator's own "is this set at all" check correctly saw NULL and
    treated the break as switched off. Persisted here so this fix-up only ever needs to
    happen once per install, not on every single read.
    """
    try:
        settings = db.get(TimetablingSettings, 1)
        if settings is None:
            settings = TimetablingSettings(id=1, working_day_start="09:00", working_day_end="17:00", min_break_minutes=15, break_after_hours=3.0)
            db.add(settings)
            db.commit()
            db.refresh(settings)
        elif settings.break_after_hours is None:
            settings.break_after_hours = 3.0
            db.commit()
        return settings
    except Exception:
        db.rollback()
        return _DEFAULT_SETTINGS


def get_skill_ranking_weights(db: Session) -> SkillRankingWeights:
    """
    Gets the single skill-ranking-weights row (always id=1), creating it with sensible
    defaults (every factor weighted equally at 1.0, a 3-year CPD half-life, a 2-year IV
    half-life) if it doesn't exist yet - same defensive pattern as get_timetabling_settings
    above: fall back to safe in-memory defaults rather than raising.
    """
    try:
        weights = db.get(SkillRankingWeights, 1)
        if weights is None:
            weights = SkillRankingWeights(id=1, weight_experience=1.0, weight_admin_rating=1.0, weight_cpd=1.0, weight_iv=1.0, cpd_half_life_years=3.0, iv_half_life_years=2.0)
            db.add(weights)
            db.commit()
            db.refresh(weights)
        return weights
    except Exception:
        db.rollback()
        return _DEFAULT_SKILL_WEIGHTS
