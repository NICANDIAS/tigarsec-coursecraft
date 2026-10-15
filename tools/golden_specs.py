"""
Golden spec tests - catches a parser change that silently breaks a spec that already works.

HOW IT WORKS (simple version)
  1. "record" runs the CURRENT parser on a real spec PDF and saves what came out
     (unit names, LO codes, criterion codes, and a fingerprint of each description)
     to golden/expected/<name>.json. That file is the "known good" answer.
  2. "check" re-runs the parser on every recorded PDF and compares with the saved answer.
     Same result -> PASS. Anything different -> FAIL, with a plain-English list of what changed.

SAFETY
  - AI is switched OFF inside this tool (the AI gateway is replaced by a stub that always
    refuses). So it costs nothing, gives the same answer every run, and tests only the
    rule-based part - the part that must never silently change.
  - It never opens the database and never writes anywhere except golden/expected/.
  - The spec PDFs themselves live in golden/pdfs/ which is git-ignored (publisher documents,
    not our source code). Only the small expected/*.json files are committed.

USAGE (from ~/projects/coursecraft):
  python tools/golden_specs.py record <name> <path-to-pdf>   # save today's result as "known good"
  python tools/golden_specs.py check                         # compare every recorded spec
  python tools/golden_specs.py show <name>                   # print a short summary of a recorded spec
"""
import hashlib
import json
import shutil
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "golden"
EXPECTED = GOLDEN / "expected"
PDFS = GOLDEN / "pdfs"
sys.path.insert(0, str(ROOT))

# --- switch AI off BEFORE the parser module is imported ---------------------------------
_stub = types.ModuleType("app.llm_gateway")


def _no_ai(**_kwargs):
    raise RuntimeError("AI is disabled inside golden tests")


_stub.complete_json = _no_ai
sys.modules["app.llm_gateway"] = _stub

from app import spec_parser  # noqa: E402  (must come after the stub)
from app.docx_annotator import extract_text_from_pdf  # noqa: E402


def _fingerprint(text: str) -> str:
    """Short hash of a description - spots a changed description without storing the whole text."""
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:10]


def snapshot(pdf_path: Path) -> dict:
    # Same as a real spec upload: no duplicate table copies (see routers/specs.py upload_spec)
    raw = extract_text_from_pdf(str(pdf_path), include_table_copies=False)
    result = spec_parser.parse_spec_hybrid(raw, known_patterns=[], max_ai_calls=1)
    units = []
    for unit in result.get("units", []):
        units.append({
            "name": unit.get("name"),
            "learning_outcomes": [
                {
                    "code": lo.get("code"),
                    "title": lo.get("title"),
                    "criteria": [[c.get("code"), _fingerprint(c.get("description"))] for c in lo.get("criteria", [])],
                }
                for lo in unit.get("learning_outcomes", [])
            ],
        })
    return {
        "source_pdf": pdf_path.name,
        "ruleset_used": result.get("ruleset_used"),
        "verdict": (result.get("quality") or {}).get("verdict"),
        "rule_based_unit_count": result.get("rule_based_unit_count"),
        "sections_needing_ai": result.get("ai_fallback_section_count"),
        "totals": {
            "units": len(units),
            "learning_outcomes": sum(len(u["learning_outcomes"]) for u in units),
            "criteria": sum(len(lo["criteria"]) for u in units for lo in u["learning_outcomes"]),
        },
        "units": units,
    }


def compare(expected: dict, actual: dict, limit: int = 15) -> list[str]:
    """Returns a list of human-readable differences (empty list = identical)."""
    diffs = []
    for key in ("ruleset_used", "verdict", "rule_based_unit_count", "sections_needing_ai", "totals"):
        if expected.get(key) != actual.get(key):
            diffs.append(f"{key}: was {expected.get(key)!r}, now {actual.get(key)!r}")

    exp_units = {u["name"]: u for u in expected["units"]}
    act_units = {u["name"]: u for u in actual["units"]}
    for name in exp_units.keys() - act_units.keys():
        diffs.append(f"unit LOST: {name}")
    for name in act_units.keys() - exp_units.keys():
        diffs.append(f"unit NEW: {name}")
    for name in exp_units.keys() & act_units.keys():
        e_los = {lo["code"]: lo for lo in exp_units[name]["learning_outcomes"]}
        a_los = {lo["code"]: lo for lo in act_units[name]["learning_outcomes"]}
        for code in e_los.keys() - a_los.keys():
            diffs.append(f"{name}: LO LOST {code}")
        for code in a_los.keys() - e_los.keys():
            diffs.append(f"{name}: LO NEW {code}")
        for code in e_los.keys() & a_los.keys():
            e_crit = dict(map(tuple, e_los[code]["criteria"]))
            a_crit = dict(map(tuple, a_los[code]["criteria"]))
            for c in e_crit.keys() - a_crit.keys():
                diffs.append(f"{name} / {code}: criterion LOST {c}")
            for c in a_crit.keys() - e_crit.keys():
                diffs.append(f"{name} / {code}: criterion NEW {c}")
            for c in e_crit.keys() & a_crit.keys():
                if e_crit[c] != a_crit[c]:
                    diffs.append(f"{name} / {code}: description CHANGED for {c}")
    if len(diffs) > limit:
        diffs = diffs[:limit] + [f"... and {len(diffs) - limit} more difference(s)"]
    return diffs


def cmd_record(name: str, pdf: str):
    src = Path(pdf).expanduser().resolve()
    if not src.exists():
        sys.exit(f"File not found: {src}")
    PDFS.mkdir(parents=True, exist_ok=True)
    EXPECTED.mkdir(parents=True, exist_ok=True)
    dest = PDFS / f"{name}.pdf"
    if src != dest:
        shutil.copyfile(src, dest)
    snap = snapshot(dest)
    out = EXPECTED / f"{name}.json"
    if out.exists():
        old = json.loads(out.read_text())
        diffs = compare(old, snap)
        print(f"Replacing existing known-good result for '{name}'. Differences from the old one:")
        print("\n".join("  - " + d for d in diffs) if diffs else "  (none)")
    out.write_text(json.dumps(snap, indent=1, ensure_ascii=False))
    t = snap["totals"]
    print(f"Recorded '{name}': {snap['ruleset_used']} -> {t['units']} units, "
          f"{t['learning_outcomes']} LOs, {t['criteria']} criteria "
          f"({snap['sections_needing_ai']} section(s) would need AI).")


def cmd_check() -> int:
    files = sorted(EXPECTED.glob("*.json"))
    if not files:
        print("Nothing recorded yet. Use: python tools/golden_specs.py record <name> <pdf>")
        return 1
    failures = 0
    for f in files:
        name = f.stem
        pdf = PDFS / f"{name}.pdf"
        if not pdf.exists():
            print(f"SKIP  {name}: PDF missing at {pdf.relative_to(ROOT)} (re-run 'record' for it)")
            failures += 1
            continue
        diffs = compare(json.loads(f.read_text()), snapshot(pdf))
        if diffs:
            failures += 1
            print(f"FAIL  {name}")
            for d in diffs:
                print(f"        - {d}")
        else:
            print(f"PASS  {name}")
    print(f"\n{len(files) - failures} of {len(files)} spec(s) unchanged.")
    return 1 if failures else 0


def cmd_show(name: str):
    snap = json.loads((EXPECTED / f"{name}.json").read_text())
    t = snap["totals"]
    print(f"{name}: {snap['ruleset_used']} | {t['units']} units, {t['learning_outcomes']} LOs, {t['criteria']} criteria")
    for u in snap["units"]:
        crit = sum(len(lo["criteria"]) for lo in u["learning_outcomes"])
        print(f"  - {u['name'][:90]}  ({len(u['learning_outcomes'])} LOs, {crit} criteria)")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 3 and args[0] == "record":
        cmd_record(args[1], args[2])
    elif args == ["check"]:
        sys.exit(cmd_check())
    elif len(args) == 2 and args[0] == "show":
        cmd_show(args[1])
    else:
        print(__doc__)
        sys.exit(2)
