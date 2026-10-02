"""TIGARSEC look for CourseCraft - self-test (standard library only).
Run from the project folder:   python3 -m unittest tests.test_tigarsec_theme -v
Checks: every text colour is readable on what it sits on (4.5:1, the design kit's rule) in BOTH
themes and inside the navy sidebar; the stylesheet is linked after style.css in both base
templates and NOT in presentation mode; every image it uses exists and is valid SVG."""
from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app/static/css/tigarsec-theme.css").read_text()


def block(selector_start: str) -> dict[str, str]:
    opener = "" if selector_start.endswith("{") else r"[^{]*\{"
    m = re.search(re.escape(selector_start) + opener + r"(.*?)\}", CSS, re.S)
    assert m, selector_start
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", m.group(1)))


def rgb(c: str, under: tuple | None = None) -> tuple:
    c = c.strip()
    if c.startswith("#"):
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b, a = [float(x) for x in re.findall(r"[\d.]+", c)]
    base = under or (255, 255, 255)
    return tuple(round(a * v + (1 - a) * u) for v, u in zip((r, g, b), base))     # blend onto what is under it


def contrast(fg: tuple, bg: tuple) -> float:
    def lum(c):
        ch = [v / 255 for v in c]
        ch = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


LIGHT, DARK = block(":root,"), block('html[data-theme="dark"] {')
SIDEBAR = block(".sidebar {")


class Readability(unittest.TestCase):
    def check(self, name, fg, bg):
        ratio = contrast(fg, bg)
        self.assertGreaterEqual(ratio, 4.5, f"{name}: {ratio:.2f}:1")

    def test_both_themes(self):
        for theme, t in (("light", LIGHT), ("dark", DARK)):
            card, page = rgb(t["--bg-elevated"]), rgb(t["--bg"])
            for fg in ("--text", "--text-muted", "--ts-link", "--success", "--warning", "--danger"):
                self.check(f"{theme} {fg} on cards", rgb(t[fg]), card)
                self.check(f"{theme} {fg} on page", rgb(t[fg]), page)
            self.check(f"{theme} button text on amber", rgb(t["--accent-text"]), rgb(t["--accent"]))
            self.check(f"{theme} text on secondary buttons", rgb(t["--text"]), rgb(t["--btn-secondary-bg"]))
            self.check(f"{theme} hover text on secondary buttons", rgb(t["--ts-link"]), rgb(t["--btn-secondary-bg-hover"]))

    def test_navy_sidebar_in_both_themes(self):
        for theme, t in (("light", LIGHT), ("dark", DARK)):
            navy = rgb(t["--ts-sidebar"])
            hover = rgb(SIDEBAR["--bg"], under=navy)
            active = rgb("rgba(224, 165, 38, 0.12)", under=navy)
            for fg in ("--text", "--text-muted", "--ts-link"):
                self.check(f"{theme} sidebar {fg}", rgb(SIDEBAR[fg]), navy)
                self.check(f"{theme} sidebar {fg} on hover", rgb(SIDEBAR[fg]), hover)
            self.check(f"{theme} active menu item", rgb(t["--accent"]), active)

    def test_amber_is_never_link_text_on_white(self):
        self.assertNotEqual(LIGHT["--ts-link"].lower(), LIGHT["--accent"].lower())


class Wiring(unittest.TestCase):
    def test_linked_after_style_css_in_both_base_templates(self):
        for name in ("base.html", "base_auth.html"):
            html = (ROOT / "app/templates" / name).read_text()
            self.assertEqual(html.count("/static/css/tigarsec-theme.css"), 1, name)
            self.assertLess(html.index("/static/css/style.css"), html.index("/static/css/tigarsec-theme.css"), name)

    def test_presentation_mode_untouched(self):
        self.assertNotIn("tigarsec-theme.css", (ROOT / "app/templates/presentation_present.html").read_text())

    def test_every_image_exists_and_is_valid_svg(self):
        urls = re.findall(r'url\("(/static/[^"]+)"\)', CSS)
        self.assertEqual(len(urls), 3, urls)
        for u in urls:
            path = ROOT / "app" / u.lstrip("/")
            self.assertTrue(path.exists(), u)
            ET.parse(path)

    def test_design_kit_is_current(self):
        self.assertIn("design kit 1.0.1", (ROOT / "app/static/tigarsec/tigarsec.css").read_text())

    def test_css_braces_balance(self):
        self.assertEqual(CSS.count("{"), CSS.count("}"))


if __name__ == "__main__":
    unittest.main()
