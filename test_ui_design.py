"""Delivery and accessibility contracts for the redesigned local and public UI."""

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.resources = []
        self.tabs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag in {"script", "img"} and attrs.get("src"):
            self.resources.append(attrs["src"])
        if tag == "link" and attrs.get("href"):
            self.resources.append(attrs["href"])
        if attrs.get("role") == "tab":
            self.tabs.append(attrs)


class UiDesignTests(unittest.TestCase):
    def test_pages_have_unique_ids_and_self_contained_assets(self):
        for name in ("index.html", "download.html"):
            with self.subTest(page=name):
                parser = PageParser()
                parser.feed((WEB / name).read_text(encoding="utf-8"))
                self.assertEqual(len(parser.ids), len(set(parser.ids)))
                for resource in parser.resources:
                    self.assertTrue(resource.startswith("assets/"), resource)
                    self.assertTrue((WEB / resource).is_file(), resource)
                self.assertEqual(len([r for r in parser.resources if "lucide" in r]), 1)

    def test_public_gallery_tabs_point_to_existing_panels(self):
        parser = PageParser()
        parser.feed((WEB / "download.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.tabs), 3)
        self.assertEqual(sum(t.get("aria-selected") == "true" for t in parser.tabs), 1)
        for tab in parser.tabs:
            self.assertIn(tab["aria-controls"], parser.ids)
            if tab["aria-selected"] == "false":
                self.assertEqual(tab["tabindex"], "-1")

    def test_workspace_preserves_controls_and_resets_only_the_active_mode(self):
        page = (WEB / "index.html").read_text(encoding="utf-8")
        script = (WEB / "assets/workspace-1.9.0.js").read_text(encoding="utf-8")
        for element in ("modeSegments", "authState", "refreshButton", "fromInput",
                        "toInput", "sessionPicker", "applyButton", "resetFiltersButton",
                        "customTime", "syncState", "windowControls"):
            self.assertIn(f'id="{element}"', page)
        self.assertIn("state.modeFilters[state.mode] = createDefaultModeFilters()[state.mode]", script)
        self.assertIn("restoreModeFilters(state.mode)", script)
        self.assertNotIn("localStorage.clear", script)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("button.textContent = '↻'", page)

    def test_styles_have_one_light_theme_and_reduced_motion(self):
        css = (WEB / "assets/theme-1.9.2.css").read_text(encoding="utf-8")
        self.assertEqual(css.count(":root"), 1)
        self.assertIn("color-scheme: light", css)
        for dark_color in ("#121415", "#151819", "#181b1d", "#16191b", "#352326"):
            self.assertNotIn(dark_color, css)
        self.assertNotIn("linear-gradient", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("minmax(0, 1fr)", css)
        self.assertIn(".custom-time", css)
        self.assertIn(".workspace-status", css)

    def test_new_external_scripts_parse_in_node(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for UI release validation")
        for filename in ("workspace-1.9.0.js", "download-1.9.0.js"):
            result = subprocess.run([node, "--check", str(WEB / "assets" / filename)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_hero_bitmap_exists_and_download_is_not_a_fake_action(self):
        css = (WEB / "assets/download-page-v4.css").read_text(encoding="utf-8")
        for relative in re.findall(r'url\("([^"]+)"\)', css):
            self.assertTrue((WEB / "assets" / relative).is_file(), relative)
        page = (WEB / "download.html").read_text(encoding="utf-8")
        self.assertIn('<base href="/">', page)
        self.assertIn('href="downloads/DeltaStatsAssistant-Setup.exe"', page)
        self.assertIn('href="downloads/DeltaStatsAssistant.zip"', page)
        self.assertIn("虚构演示数据", page)


if __name__ == "__main__":
    unittest.main()
