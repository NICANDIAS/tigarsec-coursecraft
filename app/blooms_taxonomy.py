"""
The revised Bloom's Taxonomy (Anderson & Krathwohl, 2001) - a standard, public-domain
educational framework. Kept as static data so the SOW generation prompt always has the
full taxonomy available without ever needing to fetch anything from the internet at
generation time, as explicitly asked for.

Six cognitive levels, lowest to highest demand, each with characteristic verbs a learning
outcome statement should use to signal what students are actually expected to DO -
directly parallel to the "command word" concept already used in marking (explain,
evaluate, justify, etc. define what LEVEL of response a criterion actually requires).
"""

BLOOMS_LEVELS = [
    {
        "level": "Remember",
        "description": "Recall facts and basic concepts",
        "verbs": ["define", "list", "recall", "identify", "name", "state", "describe", "label"],
    },
    {
        "level": "Understand",
        "description": "Explain ideas or concepts",
        "verbs": ["explain", "summarise", "classify", "discuss", "interpret", "outline", "compare", "illustrate"],
    },
    {
        "level": "Apply",
        "description": "Use information in new situations",
        "verbs": ["apply", "demonstrate", "use", "implement", "solve", "execute", "carry out", "operate"],
    },
    {
        "level": "Analyse",
        "description": "Draw connections among ideas",
        "verbs": ["analyse", "differentiate", "examine", "organise", "investigate", "break down", "compare and contrast", "categorise"],
    },
    {
        "level": "Evaluate",
        "description": "Justify a stance or decision",
        "verbs": ["evaluate", "assess", "justify", "critique", "recommend", "argue", "defend", "appraise"],
    },
    {
        "level": "Create",
        "description": "Produce new or original work",
        "verbs": ["design", "construct", "develop", "formulate", "produce", "devise", "compose", "plan"],
    },
]


def as_prompt_text() -> str:
    """Formats the taxonomy as plain text ready to drop straight into an AI prompt -
    built once from the static data above, never fetched."""
    lines = ["Bloom's Taxonomy (lowest to highest cognitive demand):"]
    for entry in BLOOMS_LEVELS:
        verbs = ", ".join(entry["verbs"])
        lines.append(f"- {entry['level']} ({entry['description']}): {verbs}")
    return "\n".join(lines)


def verb_to_level(verb: str) -> str | None:
    """Given a command word (e.g. from a spec criterion), returns which Bloom's level it
    belongs to - used to help keep a learning outcome statement's chosen verb consistent
    with the actual demand level of the criterion it's built from."""
    verb_lower = verb.strip().lower()
    for entry in BLOOMS_LEVELS:
        if verb_lower in entry["verbs"]:
            return entry["level"]
    return None
