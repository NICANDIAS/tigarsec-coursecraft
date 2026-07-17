"""
Compares submissions for the same target unit within a course against each other, to
catch students copying from one another - not a replacement for a proper Turnitin-style
corpus-wide check (which compares against the whole internet/academic archive, not just
your own class), but a genuinely useful, free, fast first line of defence for the specific
case you asked about: multiple students in the same class copying each other.
"""
from difflib import SequenceMatcher

HIGH_SIMILARITY_THRESHOLD = 0.6  # above this, worth a look
VERY_HIGH_SIMILARITY_THRESHOLD = 0.85  # above this, strongly suggests direct copying


def compute_similarity(text_a: str, text_b: str) -> float:
    return SequenceMatcher(None, text_a or "", text_b or "").ratio()


def find_similar_pairs(submissions_with_text: list[dict]) -> list[dict]:
    """
    submissions_with_text: [{"submission_id": int, "student_name": str, "text": str}, ...]
    Returns pairs above the threshold, sorted highest similarity first.
    """
    results = []
    for i in range(len(submissions_with_text)):
        for j in range(i + 1, len(submissions_with_text)):
            a, b = submissions_with_text[i], submissions_with_text[j]
            if not a["text"].strip() or not b["text"].strip():
                continue
            score = compute_similarity(a["text"], b["text"])
            if score >= HIGH_SIMILARITY_THRESHOLD:
                results.append({
                    "submission_a_id": a["submission_id"], "student_a": a["student_name"],
                    "submission_b_id": b["submission_id"], "student_b": b["student_name"],
                    "similarity": round(score * 100, 1),
                    "severity": "very_high" if score >= VERY_HIGH_SIMILARITY_THRESHOLD else "high",
                })
    return sorted(results, key=lambda r: r["similarity"], reverse=True)


def find_matches_for_one(target_text: str, others_with_text: list[dict]) -> list[dict]:
    """
    Checks ONE submission's text against a list of others (e.g. every other student's
    submission for the same assignment), returning matches above the threshold - this is
    what runs automatically on upload, rather than requiring a separate manual batch check.
    others_with_text: [{"submission_id": int, "student_name": str, "text": str}, ...]
    """
    results = []
    if not target_text.strip():
        return results
    for other in others_with_text:
        if not other["text"].strip():
            continue
        score = compute_similarity(target_text, other["text"])
        if score >= HIGH_SIMILARITY_THRESHOLD:
            results.append({
                "submission_id": other["submission_id"], "student_name": other["student_name"],
                "similarity": round(score * 100, 1),
                "severity": "very_high" if score >= VERY_HIGH_SIMILARITY_THRESHOLD else "high",
            })
    return sorted(results, key=lambda r: r["similarity"], reverse=True)
