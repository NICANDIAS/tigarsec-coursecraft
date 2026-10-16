"""
Route safety check - catches the known "severed decorator" bug in router files.

What can go wrong: an edit puts new code between "@router.get(...)" and the function it
belongs to, or deletes a decorator line. The app still starts, but a page silently stops
working (404) or a URL runs the wrong function.

This tool reads every file in app/routers/ (it does NOT run the app) and lists every route:
    METHOD  /path  ->  file:function
  python tools/check_routes.py snapshot   # save today's list to golden/routes_expected.txt
  python tools/check_routes.py check      # compare with the saved list

"check" reports routes that DISAPPEARED or MOVED to a different function (always a problem),
routes that are NEW (fine if you meant to add them - then run "snapshot" again),
functions that look like page handlers but have NO route decorator (usually a severed one), and
routes HIDDEN by an earlier one: e.g. "/courses/{id}/students/import" added after
"/courses/{id}/students/{student_id}" - the earlier route catches the address first, so the
new page never opens (the app answers "student_id must be a number" instead).
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTERS = ROOT / "app" / "routers"
EXPECTED = ROOT / "golden" / "routes_expected.txt"
METHODS = {"get", "post", "put", "patch", "delete"}


def _route_of(decorator):
    """Returns (METHOD, path) for @router.<method>("/path", ...), else None."""
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return None
    if decorator.func.attr not in METHODS or not decorator.args:
        return None
    first = decorator.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return decorator.func.attr.upper(), first.value
    return None


def _uses_depends(fn) -> bool:
    for default in fn.args.defaults + fn.args.kw_defaults:
        if isinstance(default, ast.Call) and getattr(default.func, "id", None) == "Depends":
            return True
    return False


def scan():
    routes, orphans = [], []
    for path in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:  # top-level functions only - that is where routes live
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            found = [r for r in (_route_of(d) for d in node.decorator_list) if r]
            for method, url in found:
                routes.append(f"{method:6} {url}  ->  {path.name}:{node.name}")
            if not found and _uses_depends(node) and not node.name.startswith("_"):
                orphans.append(f"{path.name}:{node.name} (line {node.lineno})")
    return sorted(routes), orphans


def _registration_order():
    """Router files in the order main.py includes them (that is the order addresses are matched)."""
    main_py = (ROOT / "app" / "main.py").read_text()
    import re
    return re.findall(r"include_router\((\w+)\.router", main_py)


def hidden_routes():
    """Routes that can never be reached because an EARLIER route with a {placeholder} in the
    same place matches the same address first."""
    order = {name: i for i, name in enumerate(_registration_order())}
    ordered = []
    for path in ROUTERS.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for method, url in (r for r in (_route_of(d) for d in node.decorator_list) if r):
                    ordered.append(((order.get(path.stem, 999), node.lineno), method, url, f"{path.name}:{node.name}"))
    ordered.sort()
    found = []
    for i, (_, method, url, where) in enumerate(ordered):
        parts = url.strip("/").split("/")
        for _, m2, url2, where2 in ordered[:i]:
            p2 = url2.strip("/").split("/")
            if m2 != method or len(p2) != len(parts) or url2 == url:
                continue
            if all(a == b or (b.startswith("{") and b.endswith("}")) for a, b in zip(parts, p2)):
                found.append(f"{method} {url} ({where}) is hidden by {url2} ({where2})")
                break
    return found


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    routes, orphans = scan()
    if cmd == "snapshot":
        EXPECTED.parent.mkdir(parents=True, exist_ok=True)
        EXPECTED.write_text("\n".join(routes) + "\n")
        print(f"Saved {len(routes)} routes to {EXPECTED.relative_to(ROOT)}")
        return 0
    if not EXPECTED.exists():
        print("No saved list yet - run: python tools/check_routes.py snapshot")
        return 1
    expected = [line for line in EXPECTED.read_text().splitlines() if line.strip()]
    exp_by_url = {line.split("->")[0].strip(): line for line in expected}
    now_by_url = {line.split("->")[0].strip(): line for line in routes}
    problems = 0
    for url, line in exp_by_url.items():
        if url not in now_by_url:
            problems += 1
            print(f"MISSING  {line}")
        elif now_by_url[url] != line:
            problems += 1
            print(f"MOVED    {line}\n     now {now_by_url[url]}")
    for url in now_by_url.keys() - exp_by_url.keys():
        print(f"NEW      {now_by_url[url]}")
    for o in orphans:
        problems += 1
        print(f"NO ROUTE {o}  <- has Depends(...) but no @router decorator: check it")
    for h in hidden_routes():
        problems += 1
        print(f"HIDDEN   {h}")
    print(f"\n{len(routes)} routes found, {problems} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
