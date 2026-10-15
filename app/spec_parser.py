"""
Spec parsing: rule-based first, AI as a fallback only.

Pearson/BTEC/T-Level specs follow fairly consistent structural conventions - unit headers,
"Learning aim X" (BTEC) or "Learning outcome N"/"LO" (T-Level style) headers, and criteria
codes (P1, M1, D1, or numbered mark bands) each followed by their description. Most of that
can be pulled out directly with pattern matching - no LLM call, no timeout risk, done in a
fraction of a second, and deterministic (it either matches or it doesn't, no generation to
go wrong).

The AI chunking approach (parse_spec_chunked, still used as the fallback engine) is now only
invoked on whatever the rule-based pass couldn't confidently extract - typically a small
fraction of the document, not all of it. This is why this is much faster and more reliable
than sending the whole document to the model: most of a well-formatted spec never touches
the LLM at all.

This is a first version tuned against known Pearson formatting conventions, not against your
actual document - real-world tuning against real specs is expected. If matches come back low
on a real document, the patterns below are the place to adjust; check parse_progress after a
parse for a breakdown of how much was caught by rules vs sent to AI.
"""
import os
import re
from .llm_gateway import complete_json
from .spec_readers import btec_practice, lo_ac_units

# Hard ceiling on total AI calls per document during parsing (across discovery + all
# per-section fallback chunks combined). This is the actual fix for "parsing took hours" -
# a document that needs more AI than this budget allows STOPS CLEANLY instead of grinding
# indefinitely, reporting exactly what was and wasn't covered. Override via env if needed,
# but raising it re-introduces the unbounded-wait risk this exists to prevent.
MAX_AI_CALLS_PER_PARSE = int(os.getenv("MAX_AI_CALLS_PER_PARSE", "15"))


class CallBudget:
    """Shared across every AI call made during one parse_spec_hybrid run (discovery +
    every chunk of every unmatched section) - this is what actually bounds the worst case
    instead of letting a document that needs many AI calls run indefinitely."""

    def __init__(self, limit: int, cancel_check=None):
        self.limit = limit
        self.used = 0
        self._cancel_check = cancel_check

    def exhausted(self) -> bool:
        return self.used >= self.limit

    def use_one(self):
        self.used += 1

    def cancelled(self) -> bool:
        return bool(self._cancel_check and self._cancel_check())

CHUNK_CHAR_SIZE = 6000

UNIT_HEADER_RE = re.compile(r'^[ \t]*[Uu]nit\s+(\d+)\s*[:\-–]?\s*(.*)$', re.MULTILINE)
LEARNING_AIM_RE = re.compile(r'^[ \t]*[Ll]earning\s+[Aa]im\s+([A-Z])\b\s*[:\-–]?\s*(.*)$', re.MULTILINE)
LEARNING_OUTCOME_RE = re.compile(r'^[ \t]*(?:[Ll]earning\s+[Oo]utcome|LO)\s*(\d+)\s*[:\-–]?\s*(.*)$', re.MULTILINE)
CRITERION_RE = re.compile(r'^[ \t]*([PMD]\d{1,2})\b\s*[:\-–.]?\s+(.+)$', re.MULTILINE)

# T-Level style: "3   Core Component" / "4   Occupational Specialisms" as the two top-level
# containers (real section headers only - the table of contents has the same words but always
# with a trailing page number, which \s*$ here deliberately excludes).
COMPONENT_HEADER_RE = re.compile(r'^[ \t]*\d+[ \t]+(Core Component|Occupational Specialisms)[ \t]*$', re.MULTILINE)
# Named specialisms inside the Occupational Specialisms container, e.g. "1. Digital Infrastructure"
OS_NAME_RE = re.compile(r'^[ \t]*\d+\.[ \t]+([A-Z][A-Za-z /&\-]+?)[ \t]*$', re.MULTILINE)
CONTENT_AREA_RE = re.compile(r'^[ \t]*Content area[ \t]+(\d+)[ \t]*:[ \t]*(.+)$', re.MULTILINE)
# Decimal content codes, e.g. "1.1", "1.1.1", "7.3.10" - 2 to 4 numeric parts
DECIMAL_CODE_RE = re.compile(r'^[ \t]*(\d+(?:\.\d+){1,3})\b[.\)]?[ \t]+(.+)$', re.MULTILINE)

SPEC_CHUNK_SYSTEM_PROMPT = """You are helping a UK further-education lecturer digitise a course \
specification (e.g. Pearson BTEC/T-Level) into structured data. You are being given ONE EXCERPT \
of a larger document - other excerpts are being processed separately (some by pattern-matching,
some by you) and will be merged later.

Return ONLY raw JSON (no markdown fences, no commentary) matching exactly this shape:
{
  "units": [
    {
      "name": "string - unit name/number",
      "learning_outcomes": [
        {
          "code": "string - e.g. LO2",
          "title": "string - short description of the learning outcome",
          "criteria": [
            {
              "code": "string - e.g. P2, M1, D1, or a mark-band label",
              "description": "string - the assessment criterion description, kept close to source wording"
            }
          ]
        }
      ]
    }
  ]
}

Only extract units/LOs/criteria that are actually present in THIS excerpt - if this excerpt \
doesn't contain a complete unit, extract whatever partial content is here (e.g. just the LOs \
visible, even without a unit title if it's not repeated in this excerpt - use "Unknown unit" \
as the name in that case, it will be merged correctly later). If this excerpt contains no \
relevant unit/LO/criteria content at all (e.g. it's a contents page or introduction), return \
{"units": []}. Do not invent content that isn't in the excerpt."""


def _strip_repeated_boilerplate_lines(text: str, min_length: int = 20, min_repeats: int = 4) -> str:
    """
    Detects and removes running headers/footers - lines that repeat many times across a
    document, which is exactly what page headers/footers do (e.g. "T Level Technical
    Qualification in X, Version 1.0 - May 2025, <page number>" repeating on every page).
    This matters because when this kind of boilerplate leaks into a criterion's extracted
    text, it can genuinely confuse downstream AI calls - confirmed as the likely cause of
    a scheme-of-work generation returning document metadata instead of a teaching plan,
    when a spec's footer appeared 245 times across its criteria text.

    IMPORTANT: lines matching a known structural header pattern (unit/content-area/etc.
    headers) are NEVER treated as boilerplate, even if they repeat many times - Pearson
    specs legitimately reuse the same content-area title text across multiple Occupational
    Specialisms, which repeats just as often as a genuine footer would. Only lines that
    DON'T match a real structural pattern are candidates for stripping.

    Trailing page numbers vary per occurrence, so lines are normalized (trailing digits
    stripped) before counting repeats, so real content isn't mistaken for boilerplate.
    """
    structural_patterns = [
        UNIT_HEADER_RE, LEARNING_AIM_RE, LEARNING_OUTCOME_RE, CRITERION_RE,
        COMPONENT_HEADER_RE, OS_NAME_RE, CONTENT_AREA_RE, DECIMAL_CODE_RE,
        GATEWAY_UNIT_NUMBER_RE,
    ]

    def is_structural(line: str) -> bool:
        return any(pattern.match(line) for pattern in structural_patterns)

    lines = text.split("\n")

    def normalize(line):
        stripped = line.strip()
        return re.sub(r'\s*\d+\s*$', '', stripped)

    counts = {}
    for line in lines:
        if is_structural(line):
            continue
        norm = normalize(line)
        if len(norm) >= min_length:
            counts[norm] = counts.get(norm, 0) + 1

    boilerplate_norms = {norm for norm, count in counts.items() if count >= min_repeats}
    line_stripped = "\n".join(
        line for line in lines
        if is_structural(line) or normalize(line) not in boilerplate_norms
    )

    # Handles the case where a footer got fused onto the same line as real content at an
    # exact page boundary (no line break inserted there) - e.g. "...unusual activity 208
    # Version 1.0 - May 2025 o segregation of duties..." - a whole-line check can't catch
    # this since it's mid-line, so this targets the recognisable "Version X.X - Month YYYY"
    # shape directly, with an optional page number on either side, wherever it appears.
    # Uses [ \t] rather than \s so it can never span across a newline and merge separate
    # lines together - that would break every line-anchored structural regex downstream.
    return re.sub(
        r'[ \t]*\d{0,4}[ \t]*Version[ \t]+\d+\.\d+[ \t]*-[ \t]*[A-Za-z]+[ \t]+\d{4}[ \t]*\d{0,4}[ \t]*',
        ' ',
        line_stripped,
    )


def _normalize_text(text: str) -> str:
    """pdftotext inserts a raw form-feed (\\x0c) at page breaks, often with no space before
    the next character - this breaks '^' line-start regex matching since \\x0c doesn't count
    as a fresh line the way \\n does. Converting it to a newline fixes that without losing
    any content. Also strips repeated running headers/footers - see
    _strip_repeated_boilerplate_lines."""
    text = text.replace("\x0c", "\n").replace("\r\n", "\n")
    return _strip_repeated_boilerplate_lines(text)


def _split_oversized(paragraph: str, chunk_size: int) -> list[str]:
    """A "paragraph" can be huge when a PDF has few blank lines (confirmed: the Media spec
    produced one 94,675-character piece, which no AI call could ever process - so the whole
    document was silently skipped). Split it at LINE breaks so no piece is bigger than
    chunk_size; a single line longer than that is cut as a last resort."""
    if len(paragraph) <= chunk_size:
        return [paragraph]
    pieces, current = [], ""
    for line in paragraph.split("\n"):
        while len(line) > chunk_size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:chunk_size])
            line = line[chunk_size:]
        if current and len(current) + len(line) + 1 > chunk_size:
            pieces.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current.strip():
        pieces.append(current)
    return pieces


def chunk_text(text: str, chunk_size: int = CHUNK_CHAR_SIZE) -> list[str]:
    """Splits on paragraph boundaries so chunks don't cut sentences/tables in half where avoidable.
    No chunk is ever bigger than chunk_size (see _split_oversized)."""
    paragraphs = [piece for para in re.split(r"\n\s*\n", text) for piece in _split_oversized(para, chunk_size)]
    chunks = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) > chunk_size:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        chunks.append(current)
    return chunks if chunks else [text]


def _norm_title(title) -> str:
    return " ".join((title or "").lower().split())


def number_learning_outcomes(units: list[dict]) -> int:
    """If a unit's learning outcomes have missing or repeated codes (common in AI output,
    e.g. every one called "LO"), number THAT unit's outcomes LO1, LO2, ... so each can be
    told apart, saved and linked. Units whose codes are already unique are not touched.
    Returns how many units were renumbered."""
    renumbered = 0
    for unit in units:
        los = unit.get("learning_outcomes", [])
        codes = [(lo.get("code") or "").strip().lower() for lo in los]
        if any(not c for c in codes) or len(set(codes)) != len(codes):
            for n, lo in enumerate(los, start=1):
                lo["code"] = f"LO{n}"
            renumbered += 1
    return renumbered


def _merge_unit_results(all_units_lists: list[list[dict]]) -> list[dict]:
    """Merges units/LOs found across chunks/sources, deduping by (case-insensitive) name/code
    so a unit found by both the rule-based pass and an AI fallback doesn't end up duplicated."""
    merged = {}
    order = []
    for units in all_units_lists:
        for unit in units:
            name = (unit.get("name") or "Unknown unit").strip()
            key = name.lower()
            if key not in merged:
                # Keep any extra unit keys a reader added (code, module, glh...) - older
                # results only have name + learning_outcomes, so they come out unchanged.
                extra = {k: v for k, v in unit.items() if k not in ("name", "learning_outcomes")}
                merged[key] = {**extra, "name": name, "learning_outcomes": []}
                order.append(key)
            # A learning outcome is a DUPLICATE only if both its code AND its title match one
            # already kept (the same LO found twice, e.g. by rules and by AI). Before, a matching
            # code alone was enough - so when AI labelled every outcome "LO", all but the first
            # were silently thrown away. Different titles under one code are now kept, and
            # number_learning_outcomes() gives them usable codes afterwards.
            existing = {((lo.get("code") or "").lower(), _norm_title(lo.get("title")))
                        for lo in merged[key]["learning_outcomes"]}
            for lo in unit.get("learning_outcomes", []):
                ident = ((lo.get("code") or "").lower(), _norm_title(lo.get("title")))
                if ident not in existing:
                    merged[key]["learning_outcomes"].append(lo)
                    existing.add(ident)
    return [merged[k] for k in order]


def _split_by_headers(text: str, header_re: "re.Pattern"):
    """
    Splits text at each header_re match. Returns (blocks, preamble) where blocks is a list
    of (groups_tuple, block_text_after_this_header_up_to_the_next) and preamble is whatever
    came before the first match. Returns (None, text) if the pattern doesn't match at all.
    """
    matches = list(header_re.finditer(text))
    if not matches:
        return None, text
    blocks = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((m.groups(), text[start:end]))
    return blocks, text[:matches[0].start()]


def _extract_criteria(text: str) -> tuple[list[dict], bool]:
    """Returns (criteria_list, found_any). Each criterion's description runs from its code
    to the next matched code (or end of the block)."""
    matches = list(CRITERION_RE.finditer(text))
    if not matches:
        return [], False
    criteria = []
    for i, m in enumerate(matches):
        code = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        desc = re.sub(r"\s+", " ", (m.group(2) + " " + text[start:end])).strip()
        criteria.append({"code": code, "description": desc[:1000]})
    return criteria, True


def parse_spec_rule_based(raw_text: str) -> tuple[list[dict], list[dict]]:
    """
    Returns (units, unmatched_sections). unmatched_sections is a list of
    {"context": "human-readable location", "text": "..."} for anything that didn't match
    the expected Pearson-style patterns - these get sent to the AI fallback, not the
    document as a whole.
    """
    units = []
    unmatched_sections = []

    unit_blocks, preamble = _split_by_headers(raw_text, UNIT_HEADER_RE)
    if unit_blocks is None:
        # No "Unit N" headers found anywhere - this doesn't look like a standard Pearson
        # layout at all, so the whole document goes to the AI fallback rather than guessing.
        return [], [{"context": "whole document (no 'Unit N' headers found)", "text": raw_text}]

    if len(preamble.strip()) > 200:
        unmatched_sections.append({"context": "document preamble (before first Unit heading)", "text": preamble})

    for (unit_num, unit_title), unit_text in unit_blocks:
        unit_name = f"Unit {unit_num}" + (f": {unit_title.strip()}" if unit_title.strip() else "")
        learning_outcomes = []

        # BTEC-style "Learning aim A/B/C" first, then T-Level-style "Learning outcome N"/"LO N".
        lo_blocks, lo_preamble = _split_by_headers(unit_text, LEARNING_AIM_RE)
        code_label = "Learning aim {}"
        if lo_blocks is None:
            lo_blocks, lo_preamble = _split_by_headers(unit_text, LEARNING_OUTCOME_RE)
            code_label = "LO{}"

        if lo_blocks is None:
            unmatched_sections.append({
                "context": f"{unit_name} (no 'Learning aim'/'Learning outcome' headers found)",
                "text": unit_text,
            })
            continue

        if len(lo_preamble.strip()) > 200:
            unmatched_sections.append({"context": f"{unit_name} preamble", "text": lo_preamble})

        for (code_raw, lo_title), lo_text in lo_blocks:
            lo_code = code_label.format(code_raw)
            criteria, found_any = _extract_criteria(lo_text)
            if not found_any:
                unmatched_sections.append({"context": f"{unit_name} / {lo_code}", "text": lo_text})
                continue
            learning_outcomes.append({"code": lo_code, "title": lo_title.strip(), "criteria": criteria})

        if learning_outcomes:
            units.append({"name": unit_name, "learning_outcomes": learning_outcomes})

    return units, unmatched_sections


def _extract_decimal_tree(text: str) -> tuple[list[dict], bool]:
    """
    Handles both observed depths in T-Level specs: a 3-level structure where "1.1 Title"
    is a grouping with children "1.1.1", "1.1.2"... (Core Component style), and a 2-level
    structure where "1.1" IS the leaf content statement itself with no further children
    (Occupational Specialism style). Whichever it is is detected per-code, not assumed.
    """
    matches = list(DECIMAL_CODE_RE.finditer(text))
    if not matches:
        return [], False

    entries = []
    for i, m in enumerate(matches):
        code = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = re.sub(r"\s+", " ", (m.group(2) + " " + text[start:end])).strip()
        entries.append({"code": code, "title_line": m.group(2).strip(), "body": body})

    code_set = {e["code"] for e in entries}

    def has_children(code):
        return any(c != code and c.startswith(code + ".") for c in code_set)

    groups = {}
    group_order = []

    for e in entries:
        code = e["code"]
        if has_children(code):
            continue  # this code is a subtopic grouping, not a leaf criterion

        parts = code.split(".")
        group_code = None
        for depth in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:depth])
            if candidate in code_set and has_children(candidate):
                group_code = candidate
                break

        if group_code is None:
            # No real subtopic heading exists above this leaf (the flatter, 2-level style) -
            # use a label that can never collide with a real decimal code like "1.1".
            group_code = f"general-{parts[0]}"

        if group_code not in groups:
            group_entry = next((x for x in entries if x["code"] == group_code), None)
            if group_entry:
                group_title = group_entry["title_line"]
            else:
                group_title = "General content (not further sub-grouped in the source document)"
            groups[group_code] = {"code": group_code, "title": group_title, "criteria": []}
            group_order.append(group_code)

        groups[group_code]["criteria"].append({"code": code, "description": e["body"][:1000]})

    return [groups[k] for k in group_order], True


def _extract_content_areas(text: str, scope_name: str) -> tuple[list[dict], list[dict]]:
    units = []
    unmatched = []
    ca_blocks, _preamble = _split_by_headers(text, CONTENT_AREA_RE)
    if ca_blocks is None:
        unmatched.append({"context": f"{scope_name} (no 'Content area' headings found)", "text": text})
        return units, unmatched

    for (ca_num, ca_title), ca_text in ca_blocks:
        unit_name = f"{scope_name} - Content area {ca_num}: {ca_title.strip()}"
        learning_outcomes, found_any = _extract_decimal_tree(ca_text)
        if found_any and learning_outcomes:
            for lo in learning_outcomes:
                if lo["code"][0].isdigit() and not lo["code"].startswith(f"{ca_num}."):
                    lo["code"] = f"{ca_num}.{lo['code']}"
            units.append({
                "name": unit_name,
                "learning_outcomes": [
                    {"code": lo["code"], "title": lo["title"], "criteria": lo["criteria"]}
                    for lo in learning_outcomes
                ],
            })
        else:
            unmatched.append({"context": unit_name, "text": ca_text})

    return units, unmatched


def parse_spec_rule_based_tlevel(raw_text: str) -> tuple[list[dict], list[dict]]:
    """T-Level style: Core Component + named Occupational Specialisms, each broken into
    'Content area N' sections with decimal-numbered content rather than Unit/P-M-D structure."""
    units = []
    unmatched_sections = []

    component_matches = list(COMPONENT_HEADER_RE.finditer(raw_text))
    if not component_matches:
        return [], [{"context": "whole document (no 'Core Component'/'Occupational Specialisms' headers found)", "text": raw_text}]

    for i, m in enumerate(component_matches):
        component_name = m.group(1)
        start = m.end()
        end = component_matches[i + 1].start() if i + 1 < len(component_matches) else len(raw_text)
        component_text = raw_text[start:end]

        if component_name == "Core Component":
            u, unmatched = _extract_content_areas(component_text, "Core Component")
            units.extend(u)
            unmatched_sections.extend(unmatched)
        else:
            os_blocks, _preamble = _split_by_headers(component_text, OS_NAME_RE)
            if os_blocks is None:
                unmatched_sections.append({
                    "context": "Occupational Specialisms (no named specialism headings found)",
                    "text": component_text,
                })
                continue
            for (os_name,), os_text in os_blocks:
                scope = f"Occupational Specialism: {os_name.strip()}"
                u, unmatched = _extract_content_areas(os_text, scope)
                units.extend(u)
                unmatched_sections.extend(unmatched)

    return units, unmatched_sections


GATEWAY_UNIT_NUMBER_RE = re.compile(r'Unit Number:\s*([A-Za-z0-9/]+)')
_GATEWAY_BOILERPLATE_LINE_RE = re.compile(
    r'^(Page \d+ of \d+|Qualification Specification:.*|Level:.*|Credit Value:.*|GLH:.*|Specialist Units|Mandatory Units?)$',
    re.IGNORECASE,
)


def parse_spec_rule_based_gateway(raw_text: str) -> tuple[list[dict], list[dict]]:
    """
    Gateway Qualifications style (seen in "Suite of Digital and IT Qualifications"):
    a unit title line, then "Unit Number: <code>", "Unit Aim: ...", then a "LEARNING
    OUTCOMES / ASSESSMENT CRITERIA" table where LOs are bare numbers (1, 2, 3...) and
    criteria are decimal-coded (1.1, 1.2...) - reuses _extract_decimal_tree for that part,
    same as the T-Level parser, since the criteria numbering convention is the same shape.
    """
    matches = list(GATEWAY_UNIT_NUMBER_RE.finditer(raw_text))
    if not matches:
        return [], [{"context": "whole document (no 'Unit Number:' headers found)", "text": raw_text}]

    units = []
    unmatched = []
    for i, m in enumerate(matches):
        unit_code = m.group(1)

        preceding_lines = [
            line.strip() for line in raw_text[:m.start()].splitlines()[-6:]
            if line.strip() and not _GATEWAY_BOILERPLATE_LINE_RE.match(line.strip())
        ]
        unit_title = preceding_lines[-1] if preceding_lines else unit_code

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
        unit_text = raw_text[start:end]

        unit_name = f"Unit {unit_code}: {unit_title}"
        learning_outcomes, found_any = _extract_decimal_tree(unit_text)
        if found_any and learning_outcomes:
            units.append({"name": unit_name, "learning_outcomes": learning_outcomes})
        else:
            unmatched.append({"context": unit_name, "text": unit_text})

    return units, unmatched


def _ai_parse_text_block(text: str, label: str, progress_callback=None, budget: "CallBudget" = None,
                         stats: dict | None = None) -> tuple[list[dict], bool]:
    """Runs the AI-chunking fallback over one unmatched section (not the whole document).
    Returns (units_found, succeeded). Stops early (succeeded=False) if the shared call
    budget runs out or cancellation is requested, rather than continuing indefinitely."""
    chunks = chunk_text(text)
    all_units_lists = []
    any_chunk_failed = False

    def count_skipped(chunk_list):
        # Only the pieces that were really NOT read count as skipped (stats["failed_chars"]).
        if stats is not None:
            stats["failed_chars"] = stats.get("failed_chars", 0) + sum(len(c) for c in chunk_list)

    for i, chunk in enumerate(chunks, start=1):
        if budget and budget.exhausted():
            count_skipped(chunks[i - 1:])
            any_chunk_failed = True
            if progress_callback:
                progress_callback(f"Stopped: reached the {budget.limit}-call safety limit for this parse.")
            break
        if budget and budget.cancelled():
            count_skipped(chunks[i - 1:])
            any_chunk_failed = True
            if progress_callback:
                progress_callback("Parsing cancelled.")
            break

        result = None
        last_error = None
        context = ""
        if i > 1:
            # A chunk can start part-way through a topic (the topic name was at the end of the
            # previous chunk). Show the AI where the last one stopped so it files these
            # criteria under the right outcome instead of a nameless "Unknown" one.
            tail = [l for l in chunks[i - 2].splitlines() if l.strip()][-6:]
            context = ("For context only - the previous excerpt ended with these lines (already "
                       "extracted, do NOT extract them again):\n" + "\n".join(tail)[-700:] +
                       "\n\nIf this excerpt begins part-way through a unit or learning outcome, "
                       "use that same unit name and learning outcome title for the content that "
                       "continues it.\n\n")
        for attempt in (1, 2):
            if progress_callback:
                suffix = " (retry)" if attempt == 2 else ""
                progress_callback(f"AI reviewing '{label}' - part {i} of {len(chunks)}{suffix}...")
            try:
                if budget:
                    budget.use_one()
                result = complete_json(
                    system=SPEC_CHUNK_SYSTEM_PROMPT, prompt=f"This excerpt is from: {label}\n\n{context}Excerpt {i} of {len(chunks)}:\n\n{chunk}",
                    max_tokens=4000, for_spec_parsing=True,
                )
                break
            except Exception as e:
                last_error = e
                continue
        if result is not None:
            all_units_lists.append(result.get("units", []))
        else:
            any_chunk_failed = True
            count_skipped([chunk])
            if progress_callback:
                progress_callback(f"'{label}' part {i} of {len(chunks)} failed twice, skipping: {last_error}")

    return _merge_unit_results(all_units_lists), not any_chunk_failed


DISCOVER_PATTERN_SYSTEM_PROMPT = """You are helping identify the structural pattern used in a course \
specification document, so a REGULAR EXPRESSION based parser (Python's re module) can extract its \
content automatically on every future document of this same format - without needing AI again.

Look at the excerpt and identify:
1. How a new "unit"/"content area" (a major content grouping) is announced - the exact heading pattern.
2. Optionally, a sub-heading/topic level within a unit, if one exists.
3. How an individual assessable criterion or content statement is announced (e.g. a code like "P1" \
or "1.1.1", followed by its description).

Return ONLY raw JSON (no markdown fences, no commentary) matching exactly this shape:
{
  "label": "short human-readable name for this format, e.g. 'OCR Cambridge Technical style'",
  "unit_header_regex": "Python regex, MULTILINE, exactly 2 capturing groups (code)(title), anchored with ^ to match the start of a unit/content-area heading line",
  "sub_heading_regex": "Python regex, MULTILINE, exactly 2 capturing groups, or null if there's no sub-heading level",
  "criterion_regex": "Python regex, MULTILINE, exactly 2 capturing groups (code)(description start), anchored with ^ to match the start of a criterion/content-statement line",
  "description": "1-2 sentence explanation for a human to sanity-check"
}

All regexes assume re.MULTILINE is applied - use ^ to anchor to line starts. Do not use variable-width \
lookbehind assertions. Keep patterns as simple as possible while specific enough to avoid matching \
ordinary prose. If you can't identify a confident pattern from this excerpt, return {"unit_header_regex": null}."""


def discover_pattern_via_ai(sample_text: str, progress_callback=None, budget: "CallBudget" = None):
    """
    Called once per genuinely new spec format (not matched by any hardcoded or previously
    learned pattern). Asks the AI to describe the structure as regex, which then gets
    validated and - if it works - reused for free on every future spec of this format.
    Returns None if the AI couldn't identify a confident pattern or returned something
    unusable, in which case the caller falls back to the old full-document AI chunking.
    """
    if budget and (budget.exhausted() or budget.cancelled()):
        return None
    if progress_callback:
        progress_callback(
            "No known format matched - asking AI to identify this document's structure once "
            "(future specs of this same format won't need AI at all after this)..."
        )
    try:
        if budget:
            budget.use_one()
        result = complete_json(
            system=DISCOVER_PATTERN_SYSTEM_PROMPT,
            prompt=f"Document excerpt:\n\n{sample_text[:12000]}",
            max_tokens=1500,
            for_spec_parsing=True,
        )
    except Exception:
        return None

    if not result.get("unit_header_regex") or not result.get("criterion_regex"):
        return None
    return result


def _extract_criteria_dynamic(text: str, criterion_re) -> tuple[list[dict], bool]:
    matches = list(criterion_re.finditer(text))
    if not matches:
        return [], False
    criteria = []
    for i, m in enumerate(matches):
        code = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        desc = re.sub(r"\s+", " ", (m.group(2) + " " + text[start:end])).strip()
        criteria.append({"code": code, "description": desc[:1000]})
    return criteria, True


def apply_dynamic_pattern(raw_text: str, pattern: dict) -> tuple[list[dict], list[dict]]:
    """
    Generic single-level-of-nesting extraction driven by regex STRINGS (from an AI-discovered
    or previously-learned pattern) rather than hardcoded constants. Used for both freshly
    AI-discovered patterns and ones already saved from a previous spec of the same format.
    Fails safe: any regex compile error or a pattern that matches nothing just means "this
    pattern doesn't apply here" - never raises, always falls through to the AI-chunking path.
    """
    try:
        unit_re = re.compile(pattern["unit_header_regex"], re.MULTILINE)
        criterion_re = re.compile(pattern["criterion_regex"], re.MULTILINE)
        sub_re = re.compile(pattern["sub_heading_regex"], re.MULTILINE) if pattern.get("sub_heading_regex") else None
    except re.error:
        return [], [{"context": "whole document (learned pattern failed to compile - it may need retiring)", "text": raw_text}]

    unit_blocks, _preamble = _split_by_headers(raw_text, unit_re)
    if unit_blocks is None:
        return [], [{"context": "whole document (learned pattern found no matching headers)", "text": raw_text}]

    units = []
    unmatched = []
    for (code, title), unit_text in unit_blocks:
        unit_name = f"{code}: {title.strip()}" if title.strip() else str(code)
        sub_blocks, _sub_preamble = (_split_by_headers(unit_text, sub_re) if sub_re else (None, unit_text))

        learning_outcomes = []
        if sub_blocks:
            for (sub_code, sub_title), sub_text in sub_blocks:
                criteria, found = _extract_criteria_dynamic(sub_text, criterion_re)
                if found:
                    learning_outcomes.append({"code": str(sub_code), "title": sub_title.strip(), "criteria": criteria})
                else:
                    unmatched.append({"context": f"{unit_name} / {sub_code}", "text": sub_text})
        else:
            criteria, found = _extract_criteria_dynamic(unit_text, criterion_re)
            if found:
                learning_outcomes.append({"code": "General", "title": "", "criteria": criteria})
            else:
                unmatched.append({"context": unit_name, "text": unit_text})

        if learning_outcomes:
            units.append({"name": unit_name, "learning_outcomes": learning_outcomes})

    return units, unmatched


TABLE_COPY_LABEL_RE = re.compile(r"^\[Table \d+ on this page, reconstructed from the document's actual grid for accuracy\]$")


def strip_table_copies(text: str) -> tuple[str, int]:
    """
    Removes the extra "[Table N on this page, reconstructed...]" copies that the PDF
    extractor adds for marking (see docx_annotator._extract_pdf_page_text). For a spec they
    are harmful: the parser reads the same criteria twice. New spec uploads no longer get
    these copies at all; this cleans text that was ALREADY saved with them (any spec
    uploaded since the 14 Sep 2026 extractor change), so a re-parse is clean too.

    A copy = the label line, then the rows that follow it. Rows are " | "-joined cells, so
    we drop following lines that contain "|" or are blank, and stop at the first normal
    line. Known limit: a ONE-column table has no "|" in its rows, so its rows stay (they
    are rare, and harmless duplicates of a single line). Returns (clean_text, copies_removed).
    """
    lines = text.split("\n")
    out = []
    removed = 0
    i = 0
    while i < len(lines):
        if TABLE_COPY_LABEL_RE.match(lines[i].strip()):
            removed += 1
            i += 1
            while i < len(lines) and ("|" in lines[i] or not lines[i].strip()):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), removed


def dedupe_criteria(units: list[dict]) -> int:
    """Safety net: inside ONE learning outcome a criterion code should appear once. Keeps the
    first copy (from the normal text, which reads cleanest) and drops later repeats.
    Returns how many were dropped. Codes are never renamed, so nothing linked to them moves."""
    dropped = 0
    for unit in units:
        for lo in unit.get("learning_outcomes", []):
            seen = set()
            kept = []
            for crit in lo.get("criteria", []):
                code = (crit.get("code") or "").strip().lower()
                if code and code in seen:
                    dropped += 1
                    continue
                if code:
                    seen.add(code)
                kept.append(crit)
            lo["criteria"] = kept
    return dropped


def _add_outcomes(target: dict, los: list) -> None:
    """Adds learning outcomes to a unit. An outcome with the same title as one already there is
    the SAME outcome continued (it was split between two AI excerpts), so its criteria join that
    one: exact repeats are dropped, and if two different criteria now share a code, that
    outcome's criteria are numbered C1, C2, ... so none is lost or blocks saving."""
    existing = target.setdefault("learning_outcomes", [])
    for lo in los:
        title = _norm_title(lo.get("title"))
        match = next((x for x in existing if title and _norm_title(x.get("title")) == title), None)
        if match is None:
            existing.append(lo)
            continue
        seen = {_norm_title(c.get("description")) for c in match.get("criteria", [])}
        for crit in lo.get("criteria", []):
            if _norm_title(crit.get("description")) not in seen:
                match.setdefault("criteria", []).append(crit)
                seen.add(_norm_title(crit.get("description")))
        codes = [(c.get("code") or "").strip().lower() for c in match.get("criteria", [])]
        if any(not c for c in codes) or len(set(codes)) != len(codes):
            for n, crit in enumerate(match["criteria"], start=1):
                crit["code"] = f"C{n}"


def combine_ai_pieces(units: list[dict], one_unit_name: str = "") -> tuple[list[dict], bool]:
    """AI reads a piece in excerpts, and an excerpt that starts mid-unit comes back as a
    separate "Unknown unit". For a piece the person named (they told us it is ONE unit), all
    parts become that one unit. Otherwise an "Unknown unit" part is joined to the unit before
    it. Returns (units, changed)."""
    if len(units) < 2:
        return units, False
    if one_unit_name:
        first = {k: v for k, v in units[0].items() if k != "learning_outcomes"}
        first["learning_outcomes"] = []
        for unit in units:
            _add_outcomes(first, unit.get("learning_outcomes", []))
        return [first], True
    combined = []
    for unit in units:
        if combined and _looks_unknown(unit.get("name")):
            _add_outcomes(combined[-1], unit.get("learning_outcomes", []))
        else:
            combined.append(unit)
    return combined, len(combined) != len(units)


def _looks_unknown(value: str) -> bool:
    return (value or "").strip().lower().startswith("unknown")


def assess_parse_quality(result: dict, total_chars: int) -> dict:
    """
    Plain-English honesty report for one parse. verdict is one of:
      "failed"       - nothing usable was found (0 units)
      "needs_review" - something usable, but parts were skipped or look wrong
      "good"         - nothing suspicious detected
    "good" means "no warning signs", not "a human has checked every line" - that is what
    the review screen (Stage 2) is for.
    """
    units = result.get("units", [])
    lo_count = sum(len(u.get("learning_outcomes", [])) for u in units)
    crit_count = sum(len(lo.get("criteria", [])) for u in units for lo in u.get("learning_outcomes", []))
    skipped_chars = result.get("failed_section_chars", 0)
    skipped_pct = round(100 * skipped_chars / total_chars, 1) if total_chars else 0.0

    problems = list(result.get("reader_problems") or [])
    if result.get("failed_sections"):
        problems.append(
            f"About {skipped_pct}% of the document's text could not be read and was skipped "
            f"(in: {', '.join(result['failed_sections'])[:400]}). Use 'Review and edit' to check what was found, "
            f"or 'Add a unit' to read the missing part from its pages."
        )
    if result.get("budget_stopped_early"):
        problems.append(
            f"Stopped at the safety limit of {result.get('ai_call_budget')} AI calls - the rest of the document was not read."
        )
    unknown_units = [u.get("name") for u in units if _looks_unknown(u.get("name"))]
    if unknown_units:
        problems.append(f"{len(unknown_units)} unit(s) have no real name (shown as 'Unknown unit').")
    unknown_los = sum(1 for u in units for lo in u.get("learning_outcomes", [])
                      if _looks_unknown(lo.get("code")) or _looks_unknown(lo.get("title")))
    if unknown_los:
        problems.append(f"{unknown_los} learning outcome(s) have no real code or title (shown as 'Unknown').")
    empty_units = [u.get("name") for u in units if not u.get("learning_outcomes")]
    if empty_units:
        problems.append(f"{len(empty_units)} unit(s) have no learning outcomes: {', '.join(empty_units)[:300]}")
    empty_los = sum(1 for u in units for lo in u.get("learning_outcomes", []) if not lo.get("criteria"))
    if empty_los:
        problems.append(f"{empty_los} learning outcome(s) have no criteria.")

    notes = list(result.get("reader_notes") or [])
    if result.get("table_copies_removed"):
        notes.append(f"Ignored {result['table_copies_removed']} duplicate table copies in the saved text.")
    if result.get("lo_units_renumbered"):
        notes.append(f"Learning outcomes in {result['lo_units_renumbered']} unit(s) had missing or repeated codes, so they were numbered LO1, LO2, ...")
    if result.get("duplicate_criteria_removed"):
        notes.append(f"Removed {result['duplicate_criteria_removed']} repeated criteria (same code twice in one learning outcome).")
    if result.get("ai_fallback_section_count"):
        notes.append(f"{result['ai_fallback_section_count']} section(s) were read by AI rather than by rules - worth a quick check.")

    if not units:
        verdict = "failed"
    elif problems:
        verdict = "needs_review"
    else:
        verdict = "good"

    return {
        "verdict": verdict,
        "units": len(units),
        "learning_outcomes": lo_count,
        "criteria": crit_count,
        "ruleset_used": result.get("ruleset_used"),
        "sections_sent_to_ai": result.get("ai_fallback_section_count", 0),
        "ai_calls_used": result.get("ai_calls_used", 0),
        "text_skipped_chars": skipped_chars,
        "text_skipped_percent": skipped_pct,
        "problems": problems,
        "notes": notes,
    }


def parse_spec_hybrid(
    raw_text: str,
    progress_callback=None,
    known_patterns: list[dict] | None = None,
    max_ai_calls: int | None = None,
    cancel_check=None,
    pdf_path: str | None = None,
) -> dict:
    """
    The main entry point. Order of attempts:
    1. BTEC-style hardcoded rules (Unit N / Learning aim / P-M-D)
    2. T-Level-style hardcoded rules (Core Component / Occupational Specialisms / Content area)
    3. Gateway Qualifications-style hardcoded rules (Unit Number/Unit Aim/decimal criteria)
    4. Any previously AI-discovered patterns for other spec formats (known_patterns - loaded
       from the database by the caller, since this module has no DB access itself)
    5. If NONE of those match: ask AI once to discover a new pattern, validate it actually
       produces units, and use it - this is what gets returned as "newly_discovered_pattern"
       for the caller to persist, so the next spec of this format skips straight to step 3.
    5. Only whatever's still unmatched after all of the above goes through per-section AI
       chunking (never the whole document, unless nothing above matched at all).

    ALL AI calls across steps 4 and 5 combined share one call budget (max_ai_calls, default
    MAX_AI_CALLS_PER_PARSE) - this is what bounds the worst case to a fixed, short amount of
    AI work instead of a document needing many calls just running indefinitely. cancel_check,
    if given, is polled between calls so a user-triggered cancellation takes effect promptly.
    """
    budget = CallBudget(max_ai_calls or MAX_AI_CALLS_PER_PARSE, cancel_check=cancel_check)

    if progress_callback:
        progress_callback("Running pattern-based extraction...")

    raw_text, table_copies_removed = strip_table_copies(raw_text)
    # Text BEFORE the general clean-up below: that clean-up deletes every line repeated 4+
    # times, which also deletes real words in some layouts (wrapped unit titles, the
    # "Aim:" line every unit shares). Readers that do their own careful footer removal use this.
    lightly_cleaned = raw_text.replace("\x0c", "\n").replace("\r\n", "\n")
    raw_text = _normalize_text(raw_text)
    total_chars = len(raw_text)
    rule_units, unmatched_sections = parse_spec_rule_based(raw_text)
    ruleset_used = "BTEC-style (Unit/Learning aim/P-M-D)" if rule_units else "no hardcoded/learned pattern matched"
    newly_discovered_pattern = None

    if not rule_units:
        tlevel_units, tlevel_unmatched = parse_spec_rule_based_tlevel(raw_text)
        if tlevel_units:
            rule_units, unmatched_sections = tlevel_units, tlevel_unmatched
            ruleset_used = "T-Level-style (Core Component/Occupational Specialisms/Content area)"

    if not rule_units:
        gateway_units, gateway_unmatched = parse_spec_rule_based_gateway(raw_text)
        if gateway_units:
            rule_units, unmatched_sections = gateway_units, gateway_unmatched
            ruleset_used = "Gateway Qualifications-style (Unit Number/Unit Aim/decimal criteria)"

    # Style readers (spec_readers/): tried only after the original three, so a spec those
    # already handle is never touched by them. pdf_path lets a reader use the page layout.
    reader_extras, reader_problems, reader_notes = {}, [], []
    if not rule_units:
        style = btec_practice.read(raw_text, pdf_path)
        if not (style and style["units"]):
            style = lo_ac_units.read(lightly_cleaned, pdf_path)
        if style and style["units"]:
            rule_units, unmatched_sections = style["units"], style["unmatched"]
            ruleset_used = style["ruleset"]
            reader_extras, reader_problems, reader_notes = style["extras"], style["problems"], style["notes"]

    if not rule_units and known_patterns:
        for pattern in known_patterns:
            units, unmatched = apply_dynamic_pattern(raw_text, pattern)
            if units:
                rule_units, unmatched_sections = units, unmatched
                ruleset_used = f"learned pattern: {pattern.get('label', 'unnamed')}"
                break

    if not rule_units and not budget.cancelled():
        discovered = discover_pattern_via_ai(raw_text, progress_callback=progress_callback, budget=budget)
        if discovered:
            units, unmatched = apply_dynamic_pattern(raw_text, discovered)
            if units:
                rule_units, unmatched_sections = units, unmatched
                ruleset_used = f"AI-discovered pattern: {discovered.get('label', 'new format')}"
                newly_discovered_pattern = discovered

    ai_unit_lists = [rule_units]
    failed_labels = []
    failed_section_chars = 0
    budget_stopped_early = False
    was_cancelled = False

    if unmatched_sections:
        if progress_callback:
            progress_callback(
                f"Pattern-based extraction ({ruleset_used}) found {len(rule_units)} unit(s) directly. "
                f"{len(unmatched_sections)} section(s) need AI review..."
            )
        for section in unmatched_sections:
            if budget.cancelled():
                was_cancelled = True
                break
            if budget.exhausted():
                budget_stopped_early = True
                break
            stats = {}
            units_found, ok = _ai_parse_text_block(section["text"], section["context"], progress_callback, budget=budget, stats=stats)
            ai_unit_lists.append(units_found)
            if not ok:
                failed_labels.append(section["context"])
                # Was: the WHOLE section counted as skipped even when most of it was read
                # (an A level read 65 criteria yet showed "100% skipped"). Now only the
                # pieces that really failed or were never reached count.
                failed_section_chars += stats.get("failed_chars", len(section["text"]))
    else:
        if progress_callback:
            progress_callback(f"Pattern-based extraction ({ruleset_used}) found {len(rule_units)} unit(s) - no AI needed.")

    # budget.exhausted() here catches BOTH cases: stopped between sections (already flagged
    # above) and stopped partway through a single section's own chunk loop (only visible
    # after the fact, since that loop's internal break doesn't bubble up a signal directly).
    # Same reasoning as budget_stopped_early above - cancellation can also take effect
    # partway through a single section's own chunk loop, not just between sections.
    if budget.cancelled():
        was_cancelled = True
    if budget.exhausted():
        budget_stopped_early = True

    # Sections never reached because the budget ran out / parse was cancelled count as
    # skipped text too - otherwise the report would under-state how much was lost.
    attempted = len(ai_unit_lists) - 1
    for section in unmatched_sections[attempted:]:
        failed_labels.append(section["context"])
        failed_section_chars += len(section["text"])

    merged_units = _merge_unit_results(ai_unit_lists)
    lo_units_renumbered = number_learning_outcomes(merged_units)
    duplicate_criteria_removed = dedupe_criteria(merged_units)

    result = {
        "units": merged_units,
        "rule_based_unit_count": len(rule_units),
        "ai_fallback_section_count": len(unmatched_sections),
        "ai_calls_used": budget.used,
        "ai_call_budget": budget.limit,
        "ruleset_used": ruleset_used,
        "newly_discovered_pattern": newly_discovered_pattern,
        "was_cancelled": was_cancelled,
        "budget_stopped_early": budget_stopped_early,
    }
    if failed_labels:
        result["failed_sections"] = failed_labels
    result["failed_section_chars"] = failed_section_chars
    result["table_copies_removed"] = table_copies_removed
    result["duplicate_criteria_removed"] = duplicate_criteria_removed
    result["lo_units_renumbered"] = lo_units_renumbered
    result["extras"] = reader_extras
    result["reader_problems"] = reader_problems
    result["reader_notes"] = reader_notes
    result["quality"] = assess_parse_quality(result, total_chars)
    return result


# Kept for anyone who wants a pure-AI re-parse of a whole document (e.g. via a future
# "force full AI parse" option) - the hybrid approach above is the default entry point now.
def parse_spec_chunked(raw_text: str, progress_callback=None) -> dict:
    units, ok = _ai_parse_text_block(raw_text, "whole document", progress_callback)
    result = {"units": units}
    if not ok:
        result["failed_chunks"] = True
    return result


def read_piece(text: str, unit_name: str = "", max_ai_calls: int = 4, allow_ai: bool = True) -> dict:
    """
    Stage 3 fix tool: read a SMALL piece of a spec (a few pages, or text a person pasted)
    and return the unit(s) in it, for a preview the person approves before anything is saved.

    Rules first, AI last: every rule-based reader is tried; if none fits, decimal-numbered
    criteria ("1.1 ...") are taken as one unit; only then is AI used, on this small piece
    only (where it is far more reliable than on a whole document), capped at max_ai_calls.
    Returns {"units", "method", "notes"}. Never raises for a bad piece - units may be [].
    """
    notes = []
    piece, _ = strip_table_copies(text or "")
    light = piece.replace("\x0c", "\n").replace("\r\n", "\n")
    normal = _normalize_text(piece)
    name = (unit_name or "").strip()

    attempts = [
        ("rules: unit / learning outcome / assessment criteria", lambda: (lo_ac_units.read(light) or {}).get("units")),
        ("rules: BTEC 'Practice' style", lambda: (btec_practice.read(normal) or {}).get("units")),
        ("rules: BTEC units (learning aims, P/M/D)", lambda: parse_spec_rule_based(normal)[0]),
        ("rules: T Level content areas", lambda: parse_spec_rule_based_tlevel(normal)[0]),
        ("rules: Gateway units", lambda: parse_spec_rule_based_gateway(normal)[0]),
    ]
    units, method = [], None
    for label, attempt in attempts:
        try:
            found = attempt() or []
        except Exception:
            found = []
        if found:
            units, method = found, label
            break

    if not units:
        single = lo_ac_units.read_unit_text(light, name or "New unit")
        if single:
            units, method = [single], "rules: learning outcomes / assessment criteria (no unit header)"
    if not units:
        groups, found = _extract_decimal_tree(normal)
        if found and sum(len(g["criteria"]) for g in groups) >= 2:
            units, method = [{"name": name or "New unit", "learning_outcomes": groups}], "rules: numbered criteria (1.1, 1.2 ...)"
    if not units and allow_ai and normal.strip():
        budget = CallBudget(max_ai_calls)
        found, ok = _ai_parse_text_block(normal, name or "pasted section", budget=budget)
        units = [u for u in found if u.get("learning_outcomes")]
        units, joined = combine_ai_pieces(units, name)
        if joined:
            notes.append("The AI read this in parts; the parts were joined back together"
                         + (f" as one unit, '{name}'." if name else "."))
        method = f"AI ({budget.used} call(s))"
        if not ok:
            notes.append("Part of this text could not be read by AI - check the preview carefully.")
    if not units:
        return {"units": [], "method": method or "nothing recognised", "notes": notes or [
            "Nothing that looks like units, outcomes or criteria was found in this text."]}

    if number_learning_outcomes(units):
        notes.append("Learning outcomes had missing or repeated codes, so they were numbered LO1, LO2, ...")
    dedupe_criteria(units)
    if name and len(units) == 1:
        units[0]["name"] = name
    if any((u.get("name") or "").lower().startswith("unknown") for u in units):
        notes.append("A unit has no real name - type one before adding it.")
    return {"units": units, "method": method, "notes": notes}
