"""
Stage 10 (A): marking feeds teaching. Pure functions, no database.

From a class's marked work on one unit:
  - criterion_report: how many students met / partly met / did not meet each criterion, and
    which criteria the class as a whole found hard (the "gaps");
  - next_steps: for ONE student, the specific criteria to work on next - for P/M/D specs, the
    criteria still missing for the next grade up, in the spec's own words;
  - the AI prompt that summarises the COMMON mistakes behind each gap from the markers'
    comments - with every student's name removed first;
  - gap lines for the next lesson's PowerPoint (see class_feedback.py).
"""
import re

from .coverage import classify_mark, _code_appears_in

MIN_MARKED = 3          # fewer marked students than this is not enough evidence for a class-wide gap
GAP_MET_BELOW = 0.6     # a criterion fewer than 60% of the class fully met is a gap
MAX_COMMENTS_PER_CRITERION = 12
BAND_ORDER = ["P", "M", "D"]
BAND_NAMES = {"P": "Pass", "M": "Merit", "D": "Distinction"}


def unit_criteria(unit: dict) -> list:
    """[{"code", "description", "lo"}] for every criterion in a spec unit, in spec order."""
    out = []
    for lo in (unit or {}).get("learning_outcomes", []):
        for c in lo.get("criteria", []):
            if c.get("code"):
                out.append({"code": c["code"], "description": " ".join(str(c.get("description") or "").split()),
                            "lo": lo.get("title") or lo.get("code") or ""})
    return out


def match_criterion(criterion_ref: str, criteria: list):
    """Which spec criterion a mark's criterion_ref refers to ("P3 - Explain..." -> P3).
    Whole-code matching, so P1 never matches P10. Longest code first, so 1.1.2 beats 1.1."""
    for c in sorted(criteria, key=lambda c: -len(c["code"])):
        if _code_appears_in(c["code"], criterion_ref):
            return c
    return None


def _band(code: str):
    m = re.match(r"^\s*([PMD])\d+\b", code or "", re.IGNORECASE)
    return m.group(1).upper() if m else None


def criterion_report(students: list, criteria: list) -> list:
    """students: [{"student_id", "name", "marks": [{"criterion_ref", "final_mark", "comment"}]}]
    (one entry per student - their latest marked submission for the unit).
    -> one row per spec criterion that anyone was marked on:
       {"code", "description", "met", "partly", "not_met", "unclear", "marked", "met_pct",
        "gap": bool, "not_yet": [names], "comments": [comments of students who didn't fully meet it]}"""
    rows = {c["code"]: {"code": c["code"], "description": c["description"], "met": 0, "partly": 0, "not_met": 0,
                        "unclear": 0, "marked": 0, "not_yet": [], "comments": []} for c in criteria}
    for s in students:
        for m in s.get("marks", []):
            c = match_criterion(m.get("criterion_ref", ""), criteria)
            if not c:
                continue
            row = rows[c["code"]]
            kind = classify_mark(m.get("final_mark"))
            row["marked"] += 1
            row["met" if kind == "met" else "partly" if kind == "partly_met" else "not_met" if kind == "not_met" else "unclear"] += 1
            if kind in ("partly_met", "not_met"):
                row["not_yet"].append(s["name"])
                if (m.get("comment") or "").strip():
                    row["comments"].append({"name": s["name"], "text": m["comment"].strip()})
    out = []
    for c in criteria:
        row = rows[c["code"]]
        if not row["marked"]:
            continue
        judged = row["met"] + row["partly"] + row["not_met"]
        row["met_pct"] = round(100 * row["met"] / judged) if judged else None
        row["gap"] = judged >= MIN_MARKED and row["met"] / judged < GAP_MET_BELOW
        out.append(row)
    return out


def next_steps(marks: list, criteria: list) -> dict:
    """For one student. marks: [{"criterion_ref", "final_mark"}].
    -> {"target": "Merit" | None, "steps": [{"code", "description", "status"}]}
    P/M/D specs: the criteria still missing for the NEXT grade (all of Pass first, then Merit,
    then Distinction - the BTEC rule). Other specs: every criterion not yet fully met."""
    marked = []
    for m in marks:
        c = match_criterion(m.get("criterion_ref", ""), criteria)
        if c:
            marked.append((c, classify_mark(m.get("final_mark"))))
    missing = [(c, kind) for c, kind in marked if kind in ("not_met", "partly_met")]
    if not missing:
        return {"target": None, "steps": []}
    banded = [(c, kind) for c, kind in missing if _band(c["code"])]
    if banded and len(banded) == len(missing):
        for band in BAND_ORDER:
            in_band = [(c, kind) for c, kind in banded if _band(c["code"]) == band]
            if in_band:
                return {"target": BAND_NAMES[band], "steps": [
                    {"code": c["code"], "description": c["description"], "status": "partly met" if kind == "partly_met" else "not met"}
                    for c, kind in in_band]}
    return {"target": None, "steps": [{"code": c["code"], "description": c["description"],
                                        "status": "partly met" if kind == "partly_met" else "not met"} for c, kind in missing]}


def next_steps_text(plan: dict) -> str:
    """Plain text for the student's feedback (pasted into Teams with the rest)."""
    if not plan["steps"]:
        return ""
    head = f"Next steps to reach {plan['target']}:" if plan["target"] else "Next steps:"
    return "\n".join([head] + [f"- {s['code']}: {s['description']} ({s['status']})" for s in plan["steps"]])


def _scrub(text: str, names: list) -> str:
    """Removes every student's name (full, first and last) from a comment before it goes to the AI."""
    parts = set()
    for n in names:
        for p in [n] + n.split():
            if len(p) >= 2:
                parts.add(p)
    for p in sorted(parts, key=len, reverse=True):
        text = re.sub(r"(?<!\w)" + re.escape(p) + r"(?:'s)?(?!\w)", "the student", text, flags=re.IGNORECASE)
    return text


INSIGHT_SYSTEM_PROMPT = """You help a UK further education lecturer understand what their class \
found hard in a marked assignment, so they can re-teach it. For each criterion you are given the \
criterion wording and the markers' comments on students who did NOT fully meet it. Identify the \
2-4 COMMON problems behind those comments (patterns shared by several students - not one-offs), in \
plain English, and give one short, practical re-teaching idea for the next lesson. Do not mention \
or guess any student's identity. Only use what the comments say.

Return ONLY compact raw JSON (no markdown fences, no commentary), exactly this shape:
{"criteria": [{"code": "string", "common_problems": ["string", ...], "reteach_tip": "string"}]}"""


def insight_prompt(unit_name: str, rows: list, all_names: list) -> str:
    lines = [f"Unit: {unit_name}", ""]
    for r in rows:
        if not r["gap"]:
            continue
        lines.append(f"Criterion {r['code']}: {r['description']}")
        lines.append(f"  {r['met']} of {r['met'] + r['partly'] + r['not_met']} students fully met it.")
        for c in r["comments"][:MAX_COMMENTS_PER_CRITERION]:
            lines.append(f"  - Marker's comment: {_scrub(c['text'], all_names)[:600]}")
        lines.append("")
    return "\n".join(lines).strip()


def clean_insight(result, rows: list) -> dict:
    """{code: {"common_problems": [...], "reteach_tip": str}} - only for criteria that are gaps."""
    gap_codes = {r["code"].lower(): r["code"] for r in rows if r["gap"]}
    out = {}
    for item in (result.get("criteria") if isinstance(result, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        code = gap_codes.get(str(item.get("code", "")).strip().lower())
        problems = [" ".join(str(p).split())[:300] for p in (item.get("common_problems") or []) if str(p).strip()][:4]
        if code and problems:
            out[code] = {"common_problems": problems, "reteach_tip": " ".join(str(item.get("reteach_tip") or "").split())[:400]}
    return out


def deck_gaps(unit_name: str, rows: list, insight: dict | None) -> list:
    """Gaps in the same shape as class_feedback.learning_gaps, for the next lesson's PowerPoint."""
    gaps = []
    for r in rows:
        if not r["gap"]:
            continue
        judged = r["met"] + r["partly"] + r["not_met"]
        found = (insight or {}).get(r["code"]) or {}
        gaps.append({"key": f"crit:{r['code']}", "kind": "marking", "text": f"{r['code']} {r['description']}",
                     "unit": unit_name, "answered": judged, "percent": r["met_pct"],
                     "met": r["met"], "problems": found.get("common_problems", []), "right": "", "common_wrong": "",
                     "score": r["met"] / judged if judged else 0})
    gaps.sort(key=lambda g: g["score"])
    return gaps
