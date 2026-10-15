"""
Spec "readers" - one small, rule-based reader per spec STYLE (no AI).

Each reader module offers:
    looks_like(text) -> bool        cheap fingerprint check
    read(text, pdf_path=None) -> dict | None
        {"units": [...], "unmatched": [...], "extras": {...}, "ruleset": "...",
         "problems": [...], "notes": [...]}

"units" always uses the shape every CourseCraft tool already reads
(units -> learning_outcomes -> criteria {code, description}); readers may ADD extra keys
(e.g. "bands", "content_area") which older code simply ignores. "extras" are stored next to
"units" in SpecVersion.structured_json (qualification sizes, grading scheme, content areas).
"""
