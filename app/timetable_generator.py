"""
Automated timetable generation - Phase 3 of timetabling (see the requirements analysis
and Phases 1/2: rooms/staff/qualifications, then sessions/clash detection).

Design agreed with the user before any of this was written:
  - Place the hardest-to-satisfy requirements first (fewest qualified teachers, fewest
    suitable rooms, most hours, most specific day preferences) - so an easy requirement
    never grabs a slot that was the only option for something much harder.
  - A staffing or room shortfall does NOT stop generation - the session is still created
    with a valid day/time, teacher_id or room_id left empty, and clearly flagged as
    needing one - matching the user's own explicit instruction: "the timetable is still
    created but without the lecturer name... we should be able to add the lecturer name
    (obviously with checks)."
  - A genuinely impossible requirement (no valid time exists at all, e.g. more hours than
    could ever fit) is reported plainly, never silently dropped.
  - A specific preferred day is a strong preference, not an absolute wall - tried first on
    its own, but the generator still finds a placement elsewhere rather than failing
    outright if the preferred day truly doesn't work.
  - Not yet handled, deliberately deferred (matching the same scope notes already on the
    Sessions page): one-off StaffUnavailability, and room type/equipment matching where no
    unit-level requirement data exists.

Every function below was built and verified in isolation with plain Python data before
being wired to real database objects - see the test scripts this was developed against.
This module intentionally returns plain dicts, not ORM objects, so it can be run and
inspected as a preview before anything is written to the database.
"""
import math

DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def _time_to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _minutes_to_time(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _actual_teaching_minutes(start: str, end: str, lunch_start: str | None, lunch_end: str | None) -> int:
    """
    Clock-time span minus any lunch overlap - the real teaching/contact time, as distinct
    from the full clock-time a session occupies when it spans a lunch break. Needed
    because a session that spans lunch runs longer on the clock than its actual teaching
    content - a 6-hour lesson spanning a 1-hour lunch occupies 7 clock-hours but is still
    only 6 hours of real teaching, and both matter for different reasons: the full clock
    span for room/teacher/course overlap checks (they're genuinely occupied that whole
    time), but only the real teaching time for a teacher's weekly hours ceiling.
    """
    total = _time_to_minutes(end) - _time_to_minutes(start)
    if lunch_start and lunch_end:
        ls, le = _time_to_minutes(lunch_start), _time_to_minutes(lunch_end)
        overlap_start = max(_time_to_minutes(start), ls)
        overlap_end = min(_time_to_minutes(end), le)
        if overlap_end > overlap_start:
            total -= (overlap_end - overlap_start)
    return total


def split_hours_into_blocks(hours_per_week: float, preferred_days_count: int | None) -> list:
    """
    Turns a weekly hours figure into a list of session-block lengths, one per day to be
    used. Rounds to the nearest half-hour; the last block absorbs any rounding drift so
    the blocks always sum to exactly hours_per_week.

    No preference given defaults to blocks of roughly 3 hours (matching the 1-3 hour
    block pattern a real T-Level example timetable showed), capped at 5 days since a
    week only has 5 working days.
    """
    if not preferred_days_count or preferred_days_count < 1:
        preferred_days_count = max(1, math.ceil(hours_per_week / 3))
    preferred_days_count = min(preferred_days_count, 5)
    base = round((hours_per_week / preferred_days_count) * 2) / 2
    blocks = [base] * preferred_days_count
    diff = hours_per_week - sum(blocks)
    if blocks and diff != 0:
        blocks[-1] = round((blocks[-1] + diff) * 2) / 2
    return blocks


def needs_lunch_split(block_hours: float, working_start: str, working_end: str, lunch_start: str | None,
                       lunch_end: str | None, break_minutes: int, break_after_hours: float | None) -> bool:
    """
    True if a single continuous session (with just its own mid-session break) cannot fit
    purely before or purely after lunch - the same fit condition already used in
    generate_candidate_slots, checked here separately so a block needing to span lunch can
    be routed to a real two-session split instead of one long continuous block. A block
    that fits fine as one normal session (e.g. a short afternoon class) is never
    needlessly split.
    """
    if not (lunch_start and lunch_end):
        return False
    ws, we = _time_to_minutes(working_start), _time_to_minutes(working_end)
    ls, le = _time_to_minutes(lunch_start), _time_to_minutes(lunch_end)
    own_break = break_minutes if (break_after_hours is not None and block_hours >= break_after_hours) else 0
    effective_min = int(block_hours * 60) + own_break
    fits_pre = ws + effective_min <= ls
    fits_post = le + effective_min <= we
    return not fits_pre and not fits_post


def compute_lunch_split(block_hours: float, working_start: str, lunch_start: str, lunch_end: str,
                         break_minutes: int, break_after_hours: float | None) -> tuple:
    """
    Splits a lunch-spanning block into a morning and afternoon session, matching the real
    described practice: the class stops for lunch and resumes as a second session, shown
    as two separate timetable entries rather than one long block that happens to include
    lunch. The morning fills as much of the pre-lunch window as the block needs (up to its
    full capacity); the remainder goes to the afternoon. Each half independently gets its
    own mid-session break if IT reaches the threshold - the same rule applied to any other
    class, not a special case for a split one. Lunch itself keeps its configured duration
    but starts right when the morning session actually ends, since a break pushing the
    morning slightly later pushes lunch itself the same amount later that day.

    Returns (morning_start, morning_end, morning_hours, afternoon_start, afternoon_end, afternoon_hours).
    """
    ws = _time_to_minutes(working_start)
    ls, le = _time_to_minutes(lunch_start), _time_to_minutes(lunch_end)
    lunch_duration = le - ls

    pre_lunch_capacity_hours = (ls - ws) / 60
    morning_hours = min(block_hours, pre_lunch_capacity_hours)
    afternoon_hours = block_hours - morning_hours

    morning_break = break_minutes if (break_after_hours is not None and morning_hours >= break_after_hours) else 0
    morning_start = ws
    morning_end = ws + int(morning_hours * 60) + morning_break

    afternoon_start = morning_end + lunch_duration
    afternoon_break = break_minutes if (break_after_hours is not None and afternoon_hours >= break_after_hours) else 0
    afternoon_end = afternoon_start + int(afternoon_hours * 60) + afternoon_break

    return (_minutes_to_time(morning_start), _minutes_to_time(morning_end), morning_hours,
            _minutes_to_time(afternoon_start), _minutes_to_time(afternoon_end), afternoon_hours)


def generate_candidate_slots(working_start: str, working_end: str, lunch_start: str | None,
                              lunch_end: str | None, block_hours: float, break_minutes: int = 0,
                              break_after_hours: float | None = None, step_minutes: int = 30) -> list:
    """
    Candidate (start, end) time strings for a block of block_hours within working hours.
    Prefers slots that don't overlap the lunch block; if the block is too long to fit in
    either the pre-lunch or post-lunch window, falls back to letting it span lunch as one
    continuous slot (a real, normal pattern for a long blocked session, e.g. an
    industry-placement day) rather than rejecting it outright.

    A session of break_after_hours or more gets break_minutes added to its clock-time span
    - a real, described college practice: a 3-hour session actually needs 3 hours 15
    minutes on the clock, since there's a genuine break partway through it. Applied
    uniformly across every branch below (not just the lunch-spanning one), since this
    applies to any qualifying session regardless of whether it also happens to span lunch.
    """
    ws, we = _time_to_minutes(working_start), _time_to_minutes(working_end)
    block_min = int(block_hours * 60)
    extra_break_min = break_minutes if (break_after_hours is not None and block_hours >= break_after_hours) else 0
    effective_block_min = block_min + extra_break_min
    has_lunch = bool(lunch_start and lunch_end)
    slots = []
    if has_lunch:
        ls, le = _time_to_minutes(lunch_start), _time_to_minutes(lunch_end)
        for start in range(ws, ls - block_min + 1, step_minutes):
            slots.append((_minutes_to_time(start), _minutes_to_time(start + effective_block_min)))
        for start in range(le, we - effective_block_min + 1, step_minutes):
            slots.append((_minutes_to_time(start), _minutes_to_time(start + effective_block_min)))
        if not slots:
            lunch_duration = le - ls
            extended_block_min = effective_block_min + lunch_duration
            for start in range(ws, we - extended_block_min + 1, step_minutes):
                slots.append((_minutes_to_time(start), _minutes_to_time(start + extended_block_min)))
    else:
        for start in range(ws, we - effective_block_min + 1, step_minutes):
            slots.append((_minutes_to_time(start), _minutes_to_time(start + effective_block_min)))
    return slots


def _times_overlap(day1, s1, e1, day2, s2, e2) -> bool:
    return day1 == day2 and s1 < e2 and s2 < e1


def _teacher_is_free(teacher_id, day, s, e, placed) -> bool:
    return not any(x["teacher_id"] == teacher_id and _times_overlap(day, s, e, x["day"], x["start"], x["end"]) for x in placed)


def _room_is_free(room_id, day, s, e, placed) -> bool:
    return not any(x["room_id"] == room_id and _times_overlap(day, s, e, x["day"], x["start"], x["end"]) for x in placed)


def _course_is_free(course_id, day, s, e, placed) -> bool:
    return not any(x["course_id"] == course_id and _times_overlap(day, s, e, x["day"], x["start"], x["end"]) for x in placed)


def _already_committed_hours(course_id, unit_name, existing_sessions, lunch_start=None, lunch_end=None) -> float:
    """
    Sums real teaching time already committed for this exact requirement (course + unit),
    across whatever existing sessions already cover it. Needed because existing_sessions
    was previously only used for conflict checking (is this teacher/room/course already
    busy at this time) - there was no check for "has this requirement already been
    satisfied at all", so regenerating would blindly try to place a requirement's FULL
    hours again even when real, committed sessions already covered some or all of it.
    """
    total = 0.0
    for s in existing_sessions:
        if s.get("course_id") == course_id and s.get("unit_name") == unit_name:
            if "teaching_hours" in s:
                total += s["teaching_hours"]
            else:
                total += _actual_teaching_minutes(s["start"], s["end"], lunch_start, lunch_end) / 60
    return total


def _pick_best_teacher(qualified_free_teachers: list, placed: list, req_key, lunch_start=None, lunch_end=None):
    """
    Picks the best available teacher for this specific requirement: primarily by skill
    ranking score for THIS unit (see skill_ranking.py - each teacher dict carries a
    pre-computed skill_scores lookup keyed by (spec_version_id, unit_name), computed by
    the caller since this module stays DB-free), then by fewest hours already assigned as
    the tie-break - the same load-balancing this function always did, confirmed directly
    via dummy-data testing at scale, now used as the secondary criterion rather than the
    only one.

    A teacher with no skill-ranking data at all for this unit contributes a neutral 0,
    which naturally falls back to pure load-balancing among everyone in that position -
    the "no ranking data available" case degrading honestly rather than being penalised or
    arbitrarily favoured. A college that has entered no skill data anywhere gets exactly
    today's existing behaviour, unchanged.
    """
    if not qualified_free_teachers:
        return None
    def sort_key(t):
        skill_score = (t.get("skill_scores") or {}).get(req_key, 0.0)
        hours = _hours_for_teacher(t["id"], placed, lunch_start, lunch_end)
        return (-skill_score, hours)
    return min(qualified_free_teachers, key=sort_key)


def _hours_for_teacher(teacher_id, placed, lunch_start=None, lunch_end=None) -> float:
    total = 0.0
    for x in placed:
        if x["teacher_id"] == teacher_id:
            if "teaching_hours" in x:
                total += x["teaching_hours"]
            else:
                total += _actual_teaching_minutes(x["start"], x["end"], lunch_start, lunch_end) / 60
    return total


def room_shortfall_reason(req: dict, rooms: list) -> str:
    """
    Explains why a requirement might not be getting a room, checked in the order that
    actually rules rooms out: existing at all, then type, then building, then capacity.
    Shown when a session ends up without a room, so the cause is visible directly rather
    than a silent "needs a room" with no way to tell whether it's a capacity issue, a
    type/building mismatch, or just a scheduling clash.
    """
    if not rooms:
        return "No active rooms exist at all."
    needed = req.get("student_count", 0)
    matching_type = [r for r in rooms if not req.get("preferred_room_type") or r["room_type"] == req["preferred_room_type"]]
    if not matching_type:
        return f"No active room is of the required type ({req.get('preferred_room_type')})."
    matching_building = [r for r in matching_type if not req.get("preferred_building") or r["building"] == req["preferred_building"]]
    if not matching_building:
        return f"No active room (of the right type) is in the preferred building ({req.get('preferred_building')})."
    biggest = max((r["capacity"] for r in matching_building), default=0)
    if biggest < needed:
        return f"The largest suitable room holds {biggest}, but this needs space for {needed}."
    return "A suitable room exists but was already booked at every time tried."


def requirement_difficulty(hours_per_week: float, qualified_teacher_count: int,
                            suitable_room_count: int, preferred_days_count: int | None) -> float:
    """
    Higher = harder to place = attempted first, so a requirement with a scarce but REAL
    teacher (e.g. exactly one qualified, available person) secures its slot before an
    easier requirement can take it.

    A requirement with ZERO qualified teachers or zero suitable rooms is different in
    kind, not just degree - it's unplaceable for that reason no matter which day it gets,
    so going first buys it nothing and can only cost another requirement its one real
    chance at a scarce teacher. Confirmed as a genuine bug during end-to-end testing: an
    earlier version scored these as hardest (highest priority), which let a
    no-qualified-teacher requirement grab the one day a different, one-teacher requirement
    actually needed - scored lowest here instead, so these are placed last.
    """
    if qualified_teacher_count == 0 or suitable_room_count == 0:
        return -1000.0
    score = 100.0 / qualified_teacher_count
    score += 50.0 / suitable_room_count
    score += hours_per_week
    if preferred_days_count:
        score += max(0, 5 - preferred_days_count) * 10
    return score


def place_requirement(req: dict, teachers: list, rooms: list, settings: dict, placed: list, used_days_for_req: set) -> list:
    """
    Places one requirement's blocks, appending accepted sessions to `placed` in place.

    req: {course_id, unit_name, hours_per_week, preferred_days_count, preferred_days,
          student_count}
    teachers: [{id, non_teaching_days, max_teaching_hours (or None if unknown),
                qualified_units}]  - qualified_units is a set of (spec_version_id,
                unit_name) tuples, not bare unit names - the same unit name can exist in
                more than one spec, so matching by name alone risked a false-positive
                qualification match. None means "no unit_name/spec to check against" (a
                whole-course requirement).
    rooms: [{id, capacity, room_type, building}]
    settings: {start, end, lunch_start, lunch_end}

    Returns a list of per-block result dicts: either
      {"status": "placed", "session": {...}, "needs_teacher": bool, "needs_room": bool}
    or
      {"status": "unplaced", "block_hours": float, "reason": str}
    """
    blocks = split_hours_into_blocks(req["hours_per_week"], req.get("preferred_days_count"))
    results = []
    for block_hours in blocks:
        day_filter_passes = ([req["preferred_days"]] if req.get("preferred_days") else []) + [None]
        placed_this_block = False
        for day_filter in day_filter_passes:
            if placed_this_block:
                break
            days_to_try = [d for d in DAYS_OF_WEEK if d not in used_days_for_req]
            if day_filter:
                days_to_try = [d for d in days_to_try if d in day_filter]
            best = None
            best_score = -999999
            split_needed = needs_lunch_split(
                block_hours, settings["start"], settings["end"], settings.get("lunch_start"),
                settings.get("lunch_end"), settings.get("break_minutes", 0), settings.get("break_after_hours"),
            )
            for day in days_to_try:
                if split_needed:
                    m_start, m_end, m_hours, a_start, a_end, a_hours = compute_lunch_split(
                        block_hours, settings["start"], settings["lunch_start"], settings["lunch_end"],
                        settings.get("break_minutes", 0), settings.get("break_after_hours"),
                    )
                    if not (_course_is_free(req["course_id"], day, m_start, m_end, placed)
                            and _course_is_free(req["course_id"], day, a_start, a_end, placed)):
                        continue
                    qualified_free_teachers = [
                        t for t in teachers
                        if (t["qualified_units"] is None or (req.get("spec_version_id"), req["unit_name"]) in t["qualified_units"])
                        and day not in (t.get("non_teaching_days") or [])
                        and _teacher_is_free(t["id"], day, m_start, m_end, placed)
                        and _teacher_is_free(t["id"], day, a_start, a_end, placed)
                        and (t["max_teaching_hours"] is None or (_hours_for_teacher(t["id"], placed, settings.get("lunch_start"), settings.get("lunch_end")) + block_hours) <= t["max_teaching_hours"] + 1e-9)
                    ]
                    suitable_free_rooms = [
                        r for r in rooms
                        if r["capacity"] >= req.get("student_count", 0)
                        and (not req.get("preferred_room_type") or r["room_type"] == req["preferred_room_type"])
                        and (not req.get("preferred_building") or r["building"] == req["preferred_building"])
                        and _room_is_free(r["id"], day, m_start, m_end, placed)
                        and _room_is_free(r["id"], day, a_start, a_end, placed)
                    ]
                    teacher = _pick_best_teacher(qualified_free_teachers, placed, (req.get("spec_version_id"), req["unit_name"]), settings.get("lunch_start"), settings.get("lunch_end"))
                    room = suitable_free_rooms[0] if suitable_free_rooms else None
                    score = 0
                    if day_filter and day in day_filter:
                        score += 10
                    if teacher:
                        score += 5
                    if room:
                        score += 5
                    if score > best_score:
                        best_score = score
                        best = {
                            "day": day, "split": True, "teacher": teacher, "room": room,
                            "morning": (m_start, m_end, m_hours), "afternoon": (a_start, a_end, a_hours),
                        }
                    continue
                slots = generate_candidate_slots(
                    settings["start"], settings["end"], settings.get("lunch_start"), settings.get("lunch_end"),
                    block_hours, settings.get("break_minutes", 0), settings.get("break_after_hours"),
                )
                for start, end in slots:
                    if not _course_is_free(req["course_id"], day, start, end, placed):
                        continue
                    qualified_free_teachers = [
                        t for t in teachers
                        if (t["qualified_units"] is None or (req.get("spec_version_id"), req["unit_name"]) in t["qualified_units"])
                        and day not in (t.get("non_teaching_days") or [])
                        and _teacher_is_free(t["id"], day, start, end, placed)
                        and (t["max_teaching_hours"] is None or (_hours_for_teacher(t["id"], placed, settings.get("lunch_start"), settings.get("lunch_end")) + block_hours) <= t["max_teaching_hours"] + 1e-9)
                    ]
                    suitable_free_rooms = [
                        r for r in rooms
                        if r["capacity"] >= req.get("student_count", 0)
                        and (not req.get("preferred_room_type") or r["room_type"] == req["preferred_room_type"])
                        and (not req.get("preferred_building") or r["building"] == req["preferred_building"])
                        and _room_is_free(r["id"], day, start, end, placed)
                    ]
                    teacher = _pick_best_teacher(qualified_free_teachers, placed, (req.get("spec_version_id"), req["unit_name"]), settings.get("lunch_start"), settings.get("lunch_end"))
                    room = suitable_free_rooms[0] if suitable_free_rooms else None
                    score = 0
                    if day_filter and day in day_filter:
                        score += 10
                    if teacher:
                        score += 5
                    if room:
                        score += 5
                    if score > best_score:
                        best_score = score
                        best = {"day": day, "split": False, "start": start, "end": end, "teacher": teacher, "room": room}
            if best and best.get("split"):
                m_start, m_end, m_hours = best["morning"]
                a_start, a_end, a_hours = best["afternoon"]
                teacher_id = best["teacher"]["id"] if best["teacher"] else None
                room_id = best["room"]["id"] if best["room"] else None
                morning_session = {
                    "course_id": req["course_id"], "unit_name": req["unit_name"],
                    "day": best["day"], "start": m_start, "end": m_end,
                    "teacher_id": teacher_id, "room_id": room_id, "teaching_hours": m_hours,
                }
                afternoon_session = {
                    "course_id": req["course_id"], "unit_name": req["unit_name"],
                    "day": best["day"], "start": a_start, "end": a_end,
                    "teacher_id": teacher_id, "room_id": room_id, "teaching_hours": a_hours,
                }
                placed.append(morning_session)
                placed.append(afternoon_session)
                used_days_for_req.add(best["day"])
                for session in (morning_session, afternoon_session):
                    results.append({
                        "status": "placed", "session": session,
                        "needs_teacher": teacher_id is None, "needs_room": room_id is None,
                    })
                placed_this_block = True
                continue
            if best:
                session = {
                    "course_id": req["course_id"], "unit_name": req["unit_name"],
                    "day": best["day"], "start": best["start"], "end": best["end"],
                    "teacher_id": best["teacher"]["id"] if best["teacher"] else None,
                    "room_id": best["room"]["id"] if best["room"] else None,
                    "teaching_hours": block_hours,
                }
                placed.append(session)
                used_days_for_req.add(best["day"])
                results.append({
                    "status": "placed", "session": session,
                    "needs_teacher": best["teacher"] is None, "needs_room": best["room"] is None,
                })
                placed_this_block = True
        if not placed_this_block:
            results.append({"status": "unplaced", "block_hours": block_hours, "reason": "No valid day/time slot found at all"})
    return results


def generate_timetable(requirements: list, teachers: list, rooms: list, settings: dict, existing_sessions: list) -> dict:
    """
    Runs the full generator over every requirement. requirements is a list of req dicts
    (see place_requirement's docstring), already annotated with student_count.

    Each requirement's hours are first reduced by whatever existing_sessions already
    commit to that exact (course, unit) - a requirement already fully covered is skipped
    entirely rather than having its full hours placed again, and a partially-covered one
    only has its remaining hours placed. Without this, regenerating would blindly re-place
    a requirement's FULL hours every time, even when real, committed sessions from an
    earlier run already satisfied some or all of it.

    Requirements are sorted hardest-first before placement, using each one's own real
    qualified-teacher and suitable-room counts, so a requirement with only one qualified
    teacher secures that teacher before an easier requirement can take their time.

    Returns {"placed": [...session dicts...], "needs_teacher_count": int,
             "needs_room_count": int, "unplaced": [...requirement-level failures...],
             "already_satisfied": [...requirements skipped as already fully covered...]}
    """
    placed = list(existing_sessions)
    unplaced_report = []
    already_satisfied_report = []
    scored = []
    for req in requirements:
        committed = _already_committed_hours(req["course_id"], req["unit_name"], existing_sessions, settings.get("lunch_start"), settings.get("lunch_end"))
        remaining_hours = req["hours_per_week"] - committed
        if remaining_hours <= 1e-9:
            already_satisfied_report.append({"course_id": req["course_id"], "unit_name": req["unit_name"], "hours_per_week": req["hours_per_week"]})
            continue
        req = {**req, "hours_per_week": remaining_hours}
        qualified = [t for t in teachers if t["qualified_units"] is None or (req.get("spec_version_id"), req["unit_name"]) in t["qualified_units"]]
        suitable_rooms = [
            r for r in rooms
            if r["capacity"] >= req.get("student_count", 0)
            and (not req.get("preferred_room_type") or r["room_type"] == req["preferred_room_type"])
        ]
        difficulty = requirement_difficulty(req["hours_per_week"], len(qualified), len(suitable_rooms), req.get("preferred_days_count"))
        scored.append((difficulty, req))
    scored.sort(key=lambda x: -x[0])

    needs_teacher_count = 0
    needs_room_count = 0
    room_shortfall_by_req = {}
    for difficulty, req in scored:
        used_days_for_req = set()
        results = place_requirement(req, teachers, rooms, settings, placed, used_days_for_req)
        req_key = (req["course_id"], req["unit_name"])
        for r in results:
            if r["status"] == "unplaced":
                unplaced_report.append({"course_id": req["course_id"], "unit_name": req["unit_name"], **r})
                continue
            if r["needs_teacher"]:
                needs_teacher_count += 1
            if r["needs_room"]:
                needs_room_count += 1
                if req_key not in room_shortfall_by_req:
                    room_shortfall_by_req[req_key] = room_shortfall_reason(req, rooms)
                r["session"]["room_shortfall_reason"] = room_shortfall_by_req[req_key]

    new_sessions = placed[len(existing_sessions):]
    return {
        "placed": new_sessions,
        "needs_teacher_count": needs_teacher_count,
        "needs_room_count": needs_room_count,
        "unplaced": unplaced_report,
        "already_satisfied": already_satisfied_report,
    }
